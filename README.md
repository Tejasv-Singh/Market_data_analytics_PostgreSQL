# Market data analytics in PostgreSQL

Load ~55 million ITCH-style ticks into Postgres, write the analytics queries a
trading desk actually runs (OHLCV bars, VWAP, rolling and effective spreads), then
measure each one under `EXPLAIN (ANALYZE, BUFFERS)` while you add indexes one by
one. The goal is to understand *why* a plan changes, not just that it does.

**Findings:** [`FINDINGS.md`](FINDINGS.md). **Raw numbers:** [`results/REPORT.md`](results/REPORT.md).
**Every plan:** `results/plans/<config>/<query>.txt`.

## Quick start

```bash
pip install -r requirements.txt
python db.py start          # portable Postgres 16 in ./pgdata (pgserver), tuned on first start
python generate.py          # 10 trading days: ~15M trades + ~40M quotes  (~12 min, ~4.5 GB)
python bench.py             # query x index matrix + experiments          (~45 min)
python report.py            # results/REPORT.md + charts
python db.py psql           # poke around yourself
```

To use your own Postgres instead of the bundled one, set `DATABASE_URL` before
running any script. For a quicker first pass:
`python generate.py --days 2 --quotes-per-day 1000000 --trades-per-day 400000`.

## The dataset

`generate.py` simulates what you'd have after parsing a NASDAQ TotalView-ITCH 5.0 file:

| table | rows (10 days) | from ITCH messages | columns |
|---|---:|---|---|
| `trades` | ~15M | `P` / `E` / `C` executions | `ts, symbol, price, size, side, match_id` |
| `quotes` | ~40M | top of book rebuilt from `A F X D U` | `ts, symbol, bid_px, bid_sz, ask_px, ask_sz` |

It aims to reproduce the properties that matter for query planning:

* **Rows arrive in timestamp order across all symbols.** That matches the wire, and
  it's why `pg_stats.correlation` is 1.0 for `ts` but about 0.1 for `symbol`. BRIN
  depends entirely on this.
* **Activity is skewed.** A Zipf distribution over 50 symbols gives AAPL ~22% of
  prints and DDOG ~0.4%, so selectivity depends on which symbol you ask for.
* **Intraday volume is U-shaped**, with a busy open and close. Spreads are wider in
  the first 15 minutes and for higher-priced names.
* Trades print at the prevailing bid or ask, with about 10% hidden midpoint prints.
  This gives the effective-spread query something real to measure.

Tables are created with **no indexes and no primary key**, so the `none` baseline
really has no indexes. `match_id` would be the natural PK.

## The queries (`sql/queries/`)

| file | what | SQL technique |
|---|---|---|
| `q01_ohlcv_1m_one_symbol` | 1-min bars, one symbol, one day | `date_bin`, `first_value`/`last_value` over a full-partition frame, then `GROUP BY` |
| `q02_ohlcv_5m_all_symbols` | 5-min bars, whole market, one day | same, partitioned by symbol (a "big slice") |
| `q03_vwap_daily_all` | daily VWAP for every symbol over all days | plain aggregate over every row |
| `q04_vwap_running_and_rolling` | session VWAP + trailing 5-min VWAP per trade | `ROWS UNBOUNDED PRECEDING` and `RANGE INTERVAL '5 minutes' PRECEDING` |
| `q05_rolling_spread` | spread in bps, trailing 1-min and 100-quote averages | time-based `RANGE` frame vs count-based `ROWS` frame |
| `q06_time_weighted_spread` | time-weighted spread per minute | `lead(ts)` to weight each quote by how long it was alive |
| `q07_effective_spread_asof` | prevailing quote for each trade → effective spread | as-of join with `LATERAL ... ORDER BY ts DESC LIMIT 1` |
| `q08_latest_trades_rare_symbol` | last 20 prints of a thin name | top-N point query |

Postgres has no `first()`/`last()` aggregates. The bars use window functions, as
the project asks. `(array_agg(price ORDER BY ts))[1]` is the usual one-pass
alternative; try both under `EXPLAIN ANALYZE`.

## What `bench.py` does

**Part 1: query × index matrix.** For each configuration (`none`, `brin_ts`,
`btree_ts`, `btree_sym_ts`, `covering`, `brin_plus_covering`), the script drops all
indexes, builds that configuration's indexes (timing them and recording their
size), and runs every query. Each query gets one warm-up run, saved as a text plan,
then N timed `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` runs. From the JSON it
extracts the median time, the access path, the buffers touched, the rows removed by
filter and by recheck, and the lossy heap blocks.

**Part 2: experiments that show when indexes *don't* help:**

1. **Group estimate:** q03's real bottleneck (a sort) and the `CREATE STATISTICS` fix.
2. **Low cardinality:** a B-tree on `side` (2 values).
3. **BRIN on an unordered column:** BRIN on `symbol`.
4. **Non-sargable predicate:** `date_trunc('hour', ts) = …` vs a range, plus the expression-index fix.
5. **Selectivity sweep:** the window grows from 1 minute to 10 days under none/BRIN/B-tree.
6. **BRIN `pages_per_range`:** index size vs wasted heap reads.
7. **BRIN and physical order:** the same table with its rows shuffled.

Statement timeout is 60 s. A query that hits it is recorded as `timeout`, with its
*estimated* plan saved.

## Layout

```
db.py              server lifecycle (pgserver) + connection helper; DATABASE_URL override
generate.py        synthetic ITCH-like generator -> COPY
bench.py           EXPLAIN ANALYZE harness
report.py          results.json -> REPORT.md + charts
sql/schema.sql     tables (no indexes)
sql/queries/       the 8 analytics queries (psycopg %(name)s params)
results/           results.json, REPORT.md, query_times.png, selectivity.png, plans/
FINDINGS.md        what the numbers mean
```
