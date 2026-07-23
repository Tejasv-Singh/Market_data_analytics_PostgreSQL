"""Benchmark harness: run every query under several index configurations with
EXPLAIN (ANALYZE, BUFFERS), then run targeted experiments that show when and
why indexes do *not* help.

    python bench.py                    # everything (~15-25 min on the 10-day dataset)
    python bench.py --part scenarios   # just the query x index-config matrix
    python bench.py --part experiments # just the "why doesn't this index help" experiments
    python bench.py --runs 5

Writes:
    results/results.json                 machine-readable numbers (report.py turns it into REPORT.md)
    results/plans/<scenario>/<query>.txt full text plans, one per query/config
"""
import argparse
import json
import re
import statistics
import time
from pathlib import Path

import psycopg
from psycopg import ClientCursor

import db

ROOT = Path(__file__).resolve().parent
QUERY_DIR = ROOT / "sql" / "queries"
RESULTS = ROOT / "results"
TIMEOUT = "60s"

PARAMS = {
    "day": "2026-03-04",       # a mid-sample trading day
    "symbol": "MSFT",          # 2nd most active name
    "rare_symbol": "DDOG",     # least active name (~0.4% of prints)
}

# Each scenario starts from a table with zero indexes.
SCENARIOS = [
    ("none", "No indexes: every query is a sequential scan.", []),
    ("brin_ts", "BRIN on ts (both tables).", [
        "CREATE INDEX trades_ts_brin ON trades USING brin (ts)",
        "CREATE INDEX quotes_ts_brin ON quotes USING brin (ts)",
    ]),
    ("btree_ts", "B-tree on ts (both tables).", [
        "CREATE INDEX trades_ts_btree ON trades (ts)",
        "CREATE INDEX quotes_ts_btree ON quotes (ts)",
    ]),
    ("btree_sym_ts", "Composite B-tree on (symbol, ts).", [
        "CREATE INDEX trades_sym_ts ON trades (symbol, ts)",
        "CREATE INDEX quotes_sym_ts ON quotes (symbol, ts)",
    ]),
    ("covering", "Composite B-tree (symbol, ts) INCLUDE the columns queries read -> index-only scans.", [
        "CREATE INDEX trades_sym_ts_cov ON trades (symbol, ts) INCLUDE (match_id, price, size)",
        "CREATE INDEX quotes_sym_ts_cov ON quotes (symbol, ts) INCLUDE (bid_px, ask_px)",
    ]),
    ("brin_plus_covering", "What you'd ship: tiny BRIN for time slices + covering B-tree for per-symbol work.", [
        "CREATE INDEX trades_ts_brin ON trades USING brin (ts)",
        "CREATE INDEX quotes_ts_brin ON quotes USING brin (ts)",
        "CREATE INDEX trades_sym_ts_cov ON trades (symbol, ts) INCLUDE (match_id, price, size)",
        "CREATE INDEX quotes_sym_ts_cov ON quotes (symbol, ts) INCLUDE (bid_px, ask_px)",
    ]),
]


# --------------------------------------------------------------------------- helpers

def connect():
    conn = psycopg.connect(db.uri(), autocommit=True, cursor_factory=ClientCursor)
    conn.execute(f"SET statement_timeout = '{TIMEOUT}'")
    return conn


def drop_indexes(conn, tables=("trades", "quotes")):
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = ANY(%s) AND schemaname = 'public'",
        (list(tables),)).fetchall()
    for (name,) in rows:
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')


def build_indexes(conn, ddl_list):
    built = []
    conn.execute("SET statement_timeout = 0")  # the timeout is for queries, not index builds
    for ddl in ddl_list:
        name = ddl.split("INDEX", 1)[1].split()[0]
        t0 = time.perf_counter()
        conn.execute(ddl)
        secs = time.perf_counter() - t0
        size = conn.execute("SELECT pg_relation_size(%s)", (name,)).fetchone()[0]
        built.append({"name": name, "ddl": ddl, "build_s": round(secs, 2), "bytes": size})
        print(f"    built {name:22s} {secs:6.1f}s  {size / 2**10:11,.0f} KB", flush=True)
    conn.execute("ANALYZE trades")
    conn.execute("ANALYZE quotes")
    conn.execute(f"SET statement_timeout = '{TIMEOUT}'")
    return built


def walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def summarize_plan(plan_json):
    """Pull the numbers that matter out of an EXPLAIN (FORMAT JSON) result."""
    top = plan_json[0]
    root = top["Plan"]
    scans, removed_filter, removed_recheck, lossy = [], 0, 0, 0
    for n in walk(root):
        nt = n["Node Type"]
        if "Scan" in nt and nt not in ("Subquery Scan", "CTE Scan", "Function Scan"):
            target = n.get("Index Name") or n.get("Relation Name") or ""
            label = f"{nt} [{target}]" if target else nt
            if label not in scans:
                scans.append(label)
        removed_filter += n.get("Rows Removed by Filter", 0) * max(1, n.get("Actual Loops", 1))
        removed_recheck += n.get("Rows Removed by Index Recheck", 0)
        lossy += n.get("Lossy Heap Blocks", 0)
    return {
        "exec_ms": top["Execution Time"],
        "plan_ms": top["Planning Time"],
        "scans": scans,
        "rows_out": root.get("Actual Rows", 0),
        "shared_hit": root.get("Shared Hit Blocks", 0),
        "shared_read": root.get("Shared Read Blocks", 0),
        "rows_removed_by_filter": removed_filter,
        "rows_removed_by_recheck": removed_recheck,
        "lossy_heap_blocks": lossy,
        "est_rows": root.get("Plan Rows"),
        "total_cost": root.get("Total Cost"),
    }


def explain(conn, sql, params, runs, plan_file=None, settings=()):
    """Warm-up run (saved as a text plan), then `runs` timed JSON runs. Returns summary dict."""
    with conn.cursor() as cur:
        for s in settings:
            cur.execute(s)
        try:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, SETTINGS) " + sql, params)
            text = "\n".join(r[0] for r in cur.fetchall())
            if plan_file:
                plan_file.parent.mkdir(parents=True, exist_ok=True)
                plan_file.write_text(text + "\n")
            samples = []
            for _ in range(runs):
                cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
                samples.append(summarize_plan(cur.fetchone()[0]))
        except psycopg.errors.QueryCanceled:
            if plan_file:
                plan_file.parent.mkdir(parents=True, exist_ok=True)
                plan_file.write_text(f"-- cancelled after statement_timeout={TIMEOUT}\n")
                cur.execute("EXPLAIN " + sql, params)
                plan_file.write_text(plan_file.read_text() + "-- estimated plan:\n" +
                                     "\n".join(r[0] for r in cur.fetchall()) + "\n")
            return {"timeout": True, "exec_ms": None, "scans": []}
        finally:
            cur.execute("RESET ALL")
            cur.execute(f"SET statement_timeout = '{TIMEOUT}'")
    best = samples[-1]
    times = [s["exec_ms"] for s in samples]
    best["exec_ms"] = statistics.median(times)
    best["exec_ms_all"] = times
    best["timeout"] = False
    return best


def fmt_ms(r):
    return "TIMEOUT" if r.get("timeout") else f"{r['exec_ms']:10.1f} ms"


# --------------------------------------------------------------------------- part 1

def run_scenarios(conn, runs):
    queries = sorted(QUERY_DIR.glob("q*.sql"))
    out = []
    for name, desc, ddl in SCENARIOS:
        print(f"\n=== scenario: {name} -- {desc}", flush=True)
        drop_indexes(conn)
        built = build_indexes(conn, ddl)
        results = {}
        for qf in queries:
            r = explain(conn, qf.read_text(), PARAMS, runs, RESULTS / "plans" / name / (qf.stem + ".txt"))
            results[qf.stem] = r
            print(f"  {qf.stem:34s} {fmt_ms(r)}   {', '.join(r['scans'])}", flush=True)
        out.append({"name": name, "description": desc, "indexes": built, "queries": results})
    drop_indexes(conn)
    return out


# --------------------------------------------------------------------------- part 2

ONE_HOUR = """SELECT count(*), sum(size) FROM {table}
WHERE ts >= '2026-03-04 10:00' AND ts < '2026-03-04 11:00'"""


def exp_low_cardinality(conn, runs):
    """A B-tree on a 2-value column: the planner (correctly) refuses to use it."""
    sql = "SELECT count(*), sum(size) FROM trades WHERE side = 'B'"
    drop_indexes(conn)
    rows = [("no index", explain(conn, sql, None, runs))]
    idx = build_indexes(conn, ["CREATE INDEX trades_side ON trades (side)"])
    rows.append(("btree(side), planner's choice", explain(conn, sql, None, runs)))
    rows.append(("btree(side), seq scan disabled", explain(conn, sql, None, runs, settings=[
        "SET enable_seqscan = off"])))
    drop_indexes(conn)
    return {"id": "low_cardinality", "title": "B-tree on a low-cardinality column (side = 'B'/'S')",
            "sql": sql, "indexes": idx, "variants": rows}


def exp_brin_uncorrelated(conn, runs):
    """BRIN only works when the column's values are clustered on disk. symbol isn't."""
    sql = "SELECT count(*), sum(size) FROM trades WHERE symbol = 'DDOG'"
    drop_indexes(conn)
    corr = conn.execute("SELECT attname, round(correlation::numeric, 3) FROM pg_stats "
                        "WHERE tablename = 'trades' AND attname IN ('ts', 'symbol')").fetchall()
    rows = [("no index", explain(conn, sql, None, runs))]
    idx = build_indexes(conn, ["CREATE INDEX trades_sym_brin ON trades USING brin (symbol)"])
    rows.append(("brin(symbol)", explain(conn, sql, None, runs)))
    rows.append(("brin(symbol), seq scan disabled", explain(conn, sql, None, runs, settings=[
        "SET enable_seqscan = off"])))
    drop_indexes(conn)
    idx += build_indexes(conn, ["CREATE INDEX trades_sym_btree ON trades (symbol)"])
    rows.append(("btree(symbol)", explain(conn, sql, None, runs)))
    drop_indexes(conn)
    return {"id": "brin_uncorrelated", "title": "BRIN on a column that is not physically ordered (symbol)",
            "sql": sql, "indexes": idx, "variants": rows,
            "notes": {"pg_stats.correlation": {k: float(v) for k, v in corr}}}


def exp_brin_physical_order(conn, runs):
    """Same data, same BRIN, but rows shuffled on disk -> BRIN becomes useless."""
    drop_indexes(conn)
    print("    building trades_shuffled (ORDER BY random()) ...", flush=True)
    conn.execute("SET statement_timeout = 0")
    conn.execute("DROP TABLE IF EXISTS trades_shuffled")
    conn.execute("CREATE TABLE trades_shuffled AS SELECT * FROM trades ORDER BY random()")
    conn.execute("VACUUM ANALYZE trades_shuffled")
    conn.execute(f"SET statement_timeout = '{TIMEOUT}'")
    corr = conn.execute("SELECT tablename, round(correlation::numeric, 3) FROM pg_stats "
                        "WHERE tablename IN ('trades', 'trades_shuffled') AND attname = 'ts'").fetchall()
    idx = build_indexes(conn, ["CREATE INDEX trades_ts_brin ON trades USING brin (ts)",
                               "CREATE INDEX trades_shuf_ts_brin ON trades_shuffled USING brin (ts)"])
    rows = [("time-ordered table, brin(ts)", explain(conn, ONE_HOUR.format(table="trades"), None, runs)),
            ("shuffled table, brin(ts)", explain(conn, ONE_HOUR.format(table="trades_shuffled"), None, runs)),
            ("shuffled table, brin(ts), seq scan disabled", explain(
                conn, ONE_HOUR.format(table="trades_shuffled"), None, runs, settings=["SET enable_seqscan = off"]))]
    idx += build_indexes(conn, ["CREATE INDEX trades_shuf_ts_btree ON trades_shuffled (ts)"])
    rows.append(("shuffled table, btree(ts)", explain(conn, ONE_HOUR.format(table="trades_shuffled"), None, runs)))
    conn.execute("DROP TABLE trades_shuffled")
    drop_indexes(conn)
    return {"id": "brin_physical_order", "title": "BRIN depends on physical row order (1-hour window, shuffled copy)",
            "sql": ONE_HOUR.format(table="<table>"), "indexes": idx, "variants": rows,
            "notes": {"pg_stats.correlation(ts)": {k: float(v) for k, v in corr}}}


def exp_non_sargable(conn, runs):
    """Wrapping the indexed column in a function hides it from the index."""
    drop_indexes(conn)
    idx = build_indexes(conn, ["CREATE INDEX trades_ts_btree ON trades (ts)"])
    fn_sql = "SELECT count(*), sum(size) FROM trades WHERE date_trunc('hour', ts) = '2026-03-04 10:00'"
    rows = [("date_trunc('hour', ts) = ...  with btree(ts)", explain(conn, fn_sql, None, runs)),
            ("ts >= ... AND ts < ...  with btree(ts)", explain(conn, ONE_HOUR.format(table="trades"), None, runs))]
    idx += build_indexes(conn, ["CREATE INDEX trades_hour_expr ON trades (date_trunc('hour', ts))"])
    rows.append(("date_trunc('hour', ts) = ...  with expression index", explain(conn, fn_sql, None, runs)))
    drop_indexes(conn)
    return {"id": "non_sargable", "title": "Functions on the indexed column (non-sargable predicates)",
            "sql": fn_sql, "indexes": idx, "variants": rows}


def exp_selectivity(conn, runs):
    """Widen a time window until the index stops paying for itself."""
    windows = [("1 minute", "1 minute"), ("15 minutes", "15 minutes"), ("1 hour", "1 hour"),
               ("1 day", "1 day"), ("3 days", "3 days"), ("all 10 days", "20 days")]
    tmpl = ("SELECT count(*), sum(size) FROM trades "
            "WHERE ts >= '2026-03-02 09:30' AND ts < timestamp '2026-03-02 09:30' + interval '{w}'")
    rows, idx = [], []
    for cfg, ddl in [("no index", []),
                     ("brin(ts)", ["CREATE INDEX trades_ts_brin ON trades USING brin (ts)"]),
                     ("btree(ts)", ["CREATE INDEX trades_ts_btree ON trades (ts)"])]:
        drop_indexes(conn)
        idx += build_indexes(conn, ddl)
        for label, w in windows:
            rows.append((f"{cfg} | {label}", explain(conn, tmpl.format(w=w), None, runs)))
    drop_indexes(conn)
    return {"id": "selectivity", "title": "Selectivity sweep: how wide can the window get before indexes stop helping?",
            "sql": tmpl.format(w="<window>"), "indexes": idx, "variants": rows}


def exp_brin_pages_per_range(conn, runs):
    """BRIN granularity trade-off: smaller ranges = bigger index, less wasted heap reading."""
    rows, idx = [], []
    for ppr in (8, 32, 128, 512):
        drop_indexes(conn)
        idx += build_indexes(conn, [
            f"CREATE INDEX trades_ts_brin_{ppr} ON trades USING brin (ts) WITH (pages_per_range = {ppr})"])
        rows.append((f"pages_per_range={ppr}", explain(conn, ONE_HOUR.format(table="trades"), None, runs)))
    drop_indexes(conn)
    return {"id": "brin_pages_per_range", "title": "BRIN pages_per_range trade-off (1-hour window)",
            "sql": ONE_HOUR.format(table="trades"), "indexes": idx, "variants": rows}


def exp_group_estimate(conn, runs):
    """q03 is slow for a reason no index fixes: a bad group-count estimate forces a disk sort."""
    sql = (QUERY_DIR / "q03_vwap_daily_all.sql").read_text()
    drop_indexes(conn)
    rows = [("as-is (planner guesses ~15M groups)", explain(conn, sql, None, runs))]
    conn.execute("SET statement_timeout = 0")
    conn.execute("CREATE STATISTICS trades_day_sym (ndistinct) ON (ts::date), symbol FROM trades")
    conn.execute("ANALYZE trades")
    conn.execute(f"SET statement_timeout = '{TIMEOUT}'")
    rows.append(("CREATE STATISTICS on (ts::date, symbol)", explain(conn, sql, None, runs)))
    conn.execute("DROP STATISTICS trades_day_sym")
    conn.execute("ANALYZE trades")
    c_sql = re.sub(r"^(\s+)symbol,", r'\1symbol COLLATE "C" AS symbol,', sql, count=1, flags=re.M)
    assert c_sql != sql
    rows.append(("as-is, symbol compared with C collation", explain(conn, c_sql, None, runs)))
    return {"id": "group_estimate", "title": "When the bottleneck isn't the scan: q03's sort and its group estimate",
            "sql": sql, "indexes": [], "variants": rows}


EXPERIMENTS = [exp_group_estimate, exp_low_cardinality, exp_brin_uncorrelated, exp_non_sargable, exp_selectivity,
               exp_brin_pages_per_range, exp_brin_physical_order]


def run_experiments(conn, runs):
    out = []
    for fn in EXPERIMENTS:
        print(f"\n=== experiment: {fn.__name__}", flush=True)
        res = fn(conn, runs)
        for label, r in res["variants"]:
            print(f"  {label:52s} {fmt_ms(r)}   {', '.join(r['scans'])}", flush=True)
        res["variants"] = [{"label": label, **r} for label, r in res["variants"]]
        out.append(res)
    return out


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["all", "scenarios", "experiments"], default="all")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    out_file = RESULTS / "results.json"
    data = json.loads(out_file.read_text()) if out_file.exists() else {}

    with connect() as conn:
        data["meta"] = {
            "server_version": conn.execute("SHOW server_version").fetchone()[0],
            "params": PARAMS,
            "runs": args.runs,
            "tables": {t: dict(zip(("rows", "bytes"), conn.execute(
                f"SELECT reltuples::bigint, pg_relation_size('{t}') FROM pg_class WHERE relname = '{t}'").fetchone()))
                for t in ("trades", "quotes")},
            "settings": dict(conn.execute(
                "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name = ANY(%s)",
                (list(db.TUNING),)).fetchall()),
        }
        if args.part in ("all", "scenarios"):
            data["scenarios"] = run_scenarios(conn, args.runs)
            out_file.write_text(json.dumps(data, indent=2, default=str))
        if args.part in ("all", "experiments"):
            data["experiments"] = run_experiments(conn, args.runs)
            out_file.write_text(json.dumps(data, indent=2, default=str))
    print(f"\nwrote {out_file}")


if __name__ == "__main__":
    main()
