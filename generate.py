"""Generate synthetic ITCH-like tick data and bulk-load it with COPY.

The data mimics a parsed NASDAQ ITCH feed:
  * rows arrive in global timestamp order (the property BRIN indexes rely on)
  * activity is skewed across symbols (Zipf) and U-shaped across the day
  * quotes follow a log random walk on a $0.01 tick grid; spreads widen at the open
  * trades print at the prevailing bid/ask (plus ~10% hidden midpoint prints)

    python generate.py --days 10                      # ~15M trades, ~40M quotes
    python generate.py --days 1 --quotes-per-day 500000 --trades-per-day 200000
"""
import argparse
import io
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import db

SYMBOLS = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet A"), ("META", "Meta Platforms"), ("TSLA", "Tesla"), ("AVGO", "Broadcom"),
    ("AMD", "Advanced Micro Devices"), ("NFLX", "Netflix"), ("COST", "Costco"), ("PEP", "PepsiCo"),
    ("ADBE", "Adobe"), ("CSCO", "Cisco"), ("INTC", "Intel"), ("QCOM", "Qualcomm"),
    ("TXN", "Texas Instruments"), ("AMGN", "Amgen"), ("INTU", "Intuit"), ("ISRG", "Intuitive Surgical"),
    ("BKNG", "Booking"), ("SBUX", "Starbucks"), ("MDLZ", "Mondelez"), ("GILD", "Gilead"),
    ("ADP", "ADP"), ("VRTX", "Vertex"), ("REGN", "Regeneron"), ("LRCX", "Lam Research"),
    ("MU", "Micron"), ("PANW", "Palo Alto Networks"), ("SNPS", "Synopsys"), ("CDNS", "Cadence"),
    ("KLAC", "KLA"), ("MAR", "Marriott"), ("ORLY", "O'Reilly"), ("CTAS", "Cintas"),
    ("MRVL", "Marvell"), ("ABNB", "Airbnb"), ("FTNT", "Fortinet"), ("PYPL", "PayPal"),
    ("CRWD", "CrowdStrike"), ("WDAY", "Workday"), ("ADSK", "Autodesk"), ("ROP", "Roper"),
    ("PCAR", "PACCAR"), ("MNST", "Monster"), ("CPRT", "Copart"), ("ODFL", "Old Dominion"),
    ("FAST", "Fastenal"), ("DDOG", "Datadog"),
]

SESSION_SECONDS = 6.5 * 3600  # 09:30 -> 16:00
TICK = 0.01


def trading_days(start: date, n: int):
    d = start
    while n:
        if d.weekday() < 5:
            yield d
            n -= 1
        d += timedelta(days=1)


def intraday_times(rng, n):
    """U-shaped arrival times (busy open/close), microsecond resolution, sorted."""
    u = rng.beta(0.65, 0.65, size=n)
    us = np.sort((u * SESSION_SECONDS * 1e6).astype(np.int64))
    return us


def gen_symbol_day(rng, n_quotes, n_trades, p0, daily_vol):
    q_us = intraday_times(rng, n_quotes)
    # Log random walk scaled so the day's total variance matches daily_vol.
    steps = rng.standard_normal(n_quotes) * (daily_vol / np.sqrt(n_quotes))
    mid = p0 * np.exp(np.cumsum(steps))
    # Spread in ticks: mostly 1-2 ticks, wider in the first 15 minutes and for pricier names.
    open_factor = np.where(q_us < 15 * 60 * 1e6, 3.0, 1.0)
    price_factor = max(1.0, p0 / 150.0)
    spread_ticks = 1 + np.floor(rng.geometric(0.55, n_quotes) * open_factor * price_factor * 0.6).astype(np.int64)
    bid = np.round(np.floor(mid / TICK - spread_ticks / 2) * TICK, 2)
    ask = np.round(bid + spread_ticks * TICK, 2)
    bid_sz = rng.geometric(0.25, n_quotes) * 100
    ask_sz = rng.geometric(0.25, n_quotes) * 100

    t_us = intraday_times(rng, n_trades)
    # Prevailing quote for each trade (last quote at or before the trade).
    idx = np.clip(np.searchsorted(q_us, t_us, side="right") - 1, 0, n_quotes - 1)
    t_us = np.maximum(t_us, q_us[idx])  # never trade before the first quote
    buy = rng.random(n_trades) < 0.5
    price = np.where(buy, ask[idx], bid[idx])
    hidden = rng.random(n_trades) < 0.10
    price = np.where(hidden, np.round((bid[idx] + ask[idx]) / 2, 4), price)
    size = np.maximum(1, np.round(rng.lognormal(4.6, 1.1, n_trades))).astype(np.int64)
    round_lot = rng.random(n_trades) < 0.6
    size = np.where(round_lot, np.maximum(100, np.round(size / 100).astype(np.int64) * 100), size)

    quotes = dict(us=q_us, bid_px=bid, bid_sz=bid_sz, ask_px=ask, ask_sz=ask_sz)
    trades = dict(us=t_us, price=price, size=size, side=np.where(buy, "B", "S"))
    return quotes, trades, float(mid[-1])


def copy_df(conn, table, df, columns):
    buf = io.StringIO()
    # No float_format: pandas then uses shortest round-trip repr (fast path); values are already rounded.
    df.to_csv(buf, index=False, header=False)
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv)") as cp:
            cp.write(buf.getvalue())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--start", default="2026-03-02")
    ap.add_argument("--quotes-per-day", type=int, default=4_000_000)
    ap.add_argument("--trades-per-day", type=int, default=1_500_000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    n_sym = len(SYMBOLS)
    weights = 1.0 / np.arange(1, n_sym + 1) ** 1.1           # Zipf-ish activity
    weights /= weights.sum()
    prices = rng.uniform(20, 600, n_sym).round(2)
    vols = rng.uniform(0.01, 0.035, n_sym)                     # daily vol 1%-3.5%

    with db.connect() as conn:
        conn.execute(open(db.ROOT / "sql" / "schema.sql").read())
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO symbols (symbol, name) VALUES (%s, %s)", SYMBOLS)
        conn.commit()

        match_id = 0
        t_total = time.time()
        for day in trading_days(date.fromisoformat(args.start), args.days):
            t0 = time.time()
            session_open = np.datetime64(datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=30), "us")
            q_parts, t_parts = [], []
            for i, (sym, _) in enumerate(SYMBOLS):
                nq = max(50, int(args.quotes_per_day * weights[i] * rng.uniform(0.8, 1.2)))
                nt = max(20, int(args.trades_per_day * weights[i] * rng.uniform(0.8, 1.2)))
                q, t, prices[i] = gen_symbol_day(rng, nq, nt, prices[i], vols[i])
                q["symbol"] = np.full(nq, sym)
                t["symbol"] = np.full(nt, sym)
                q_parts.append(pd.DataFrame(q))
                t_parts.append(pd.DataFrame(t))

            # Merge every symbol into one feed ordered by time, like the wire.
            qdf = pd.concat(q_parts).sort_values("us", kind="stable")
            tdf = pd.concat(t_parts).sort_values("us", kind="stable")
            # Pre-format timestamps in C (datetime_as_string) -- far faster than to_csv's date_format.
            qdf.insert(0, "ts", np.datetime_as_string(session_open + qdf.pop("us").to_numpy().astype("timedelta64[us]")))
            tdf.insert(0, "ts", np.datetime_as_string(session_open + tdf.pop("us").to_numpy().astype("timedelta64[us]")))
            tdf["match_id"] = np.arange(match_id + 1, match_id + len(tdf) + 1)
            match_id += len(tdf)

            copy_df(conn, "quotes", qdf[["ts", "symbol", "bid_px", "bid_sz", "ask_px", "ask_sz"]],
                    ["ts", "symbol", "bid_px", "bid_sz", "ask_px", "ask_sz"])
            copy_df(conn, "trades", tdf[["ts", "symbol", "price", "size", "side", "match_id"]],
                    ["ts", "symbol", "price", "size", "side", "match_id"])
            conn.commit()
            print(f"{day}  quotes={len(qdf):>10,}  trades={len(tdf):>10,}  ({time.time() - t0:.1f}s)", flush=True)

        print("VACUUM ANALYZE ...", flush=True)
        conn.autocommit = True
        conn.execute("VACUUM (ANALYZE) trades")
        conn.execute("VACUUM (ANALYZE) quotes")
        conn.execute("VACUUM (ANALYZE) symbols")
        for tbl in ("trades", "quotes"):
            n, size = conn.execute(
                f"SELECT count(*), pg_size_pretty(pg_total_relation_size('{tbl}')) FROM {tbl}").fetchone()
            print(f"{tbl:7s} {n:>12,} rows  {size}")
        print(f"done in {time.time() - t_total:.0f}s")


if __name__ == "__main__":
    main()
