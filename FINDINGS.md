# Findings

Dataset: 10 trading days, 15.07M trades (981 MB heap) and 40.5M quotes (2.6 GB heap).
PostgreSQL 16.2 on a 16-core / 16 GB Windows laptop, warm cache. Times are medians of
3 runs after a warm-up run. All numbers come from [`results/REPORT.md`](results/REPORT.md),
and the full plans are in `results/plans/`.

![query times](results/query_times.png)

## Headline table (ms)

| query | none | BRIN(ts) | B-tree(ts) | B-tree(symbol, ts) | covering | BRIN + covering |
|---|---:|---:|---:|---:|---:|---:|
| q01 1-min bars, 1 symbol | 1,212 | 428 | 459 | 349 | 348 | 355 |
| q02 5-min bars, all symbols | 4,853 | **9,320** | **9,507** | 5,025 | 4,486 | **9,660** |
| q03 daily VWAP, everything | 37,393 | 37,712 | 37,861 | 38,235 | 38,980 | 39,210 |
| q04 running + 5-min VWAP | 1,760 | 1,089 | 1,342 | 1,021 | 976 | 966 |
| q05 rolling spread | 7,783 | 5,291 | 6,138 | 5,227 | 5,064 | 5,174 |
| q06 time-weighted spread | 4,150 | 2,232 | 3,020 | 2,176 | 2,008 | 2,057 |
| q07 as-of join (effective spread) | >60 s | >60 s | 184 | 311 | 250 | 257 |
| q08 last 20 prints, thin symbol | 808 | 821 | 1.3 | 0.1 | 0.1 | 0.1 |

Index sizes: BRIN(ts) is **48 KB** on trades and 104 KB on quotes. The B-tree on `ts`
is 323 MB and 868 MB. The covering index is 845 MB and 1.9 GB. BRIN is about 7,000x
smaller than the B-tree on the same column.

## 1. Point lookups and as-of joins are where B-trees pay off

* **q08 (last 20 prints of DDOG):** 808 ms → 0.1 ms with `(symbol, ts)`. Pages
  touched drop from 125,808 to 19. Postgres descends the tree to `DDOG`, walks
  backwards along `ts`, and stops after 20 rows. Plain `btree(ts)` also works (1.3 ms):
  it walks the whole market backwards and filters, throwing away 6,262 rows from other
  symbols first.
* **q07 (as-of join):** with no index, every trade triggers a scan of the 40M-row
  quote table, so the query never finishes. BRIN doesn't help either. BRIN can say
  which block ranges *might* contain `ts <= x`, but it can't return "the latest row
  before x", so `ORDER BY ts DESC LIMIT 1` has nothing to use. Any B-tree makes it
  sub-second.
* **Which B-tree is right depends on the symbol.** For busy MSFT, `btree(ts)` (184 ms)
  actually beat `(symbol, ts)` (311 ms), because walking back from any timestamp hits
  an MSFT quote within a few entries. For a thin name the order flips (a one-off run with
  `symbol = DDOG`, saved in `results/asof_rare_symbol.json`):

  | as-of join, 30 min | MSFT (15,055 trades) | DDOG (459 trades) |
  |---|---:|---:|
  | btree(ts) | 184 ms | 56 ms (~11 pages per trade) |
  | btree(symbol, ts) | 311 ms | 10.5 ms (~6 pages per trade) |

  With `btree(ts)`, the work per trade grows with how rare the symbol is. With
  `(symbol, ts)` it stays flat. That flat cost is what you want in production.

## 2. BRIN is excellent, but only for time slices of time-ordered data

![selectivity](results/selectivity.png)

* Sum over a 1-minute window: seq scan 426 ms, BRIN 7.7 ms, B-tree 7.8 ms. A 48 KB BRIN
  ties a 323 MB B-tree here.
* Wider windows keep BRIN competitive: 1 day takes 168 ms with BRIN vs 207 ms with the
  B-tree. BRIN reads block ranges sequentially through a bitmap. The B-tree's index
  scan walks tuple by tuple.
* At the full 10 days, both indexes are ignored and the planner picks a seq scan. That's
  the right call: no index can make "read everything" cheaper.
* **BRIN depends on physical order.** I copied the table with `ORDER BY random()`.
  `pg_stats.correlation` for `ts` fell from 1.0 to −0.002, and the same 1-hour query
  went from 85 ms (BRIN) to 898 ms (seq scan). When I forced the planner to use BRIN, it
  rechecked and discarded 2.97M rows across 32k lossy blocks, because every block
  range now covers the whole day. A B-tree on the shuffled table still worked (344 ms)
  but touched 224k pages: one random heap visit per row.
* **BRIN on `symbol` is useless** (correlation 0.107). Every block range contains every
  symbol, so the bitmap marks the whole table and 3.0M rows get rechecked. That's
  694 ms, *slower* than the 560 ms seq scan. `btree(symbol)` takes 95 ms.
* **`pages_per_range` barely mattered** on this data (8 → 512: 86 / 91 / 95 / 83 ms).
  The index grew from 24 KB to 536 KB, but the 1-hour window spans thousands of blocks
  either way, so the few extra blocks at the edges are noise. Granularity would matter
  for very narrow windows or less tightly ordered data. On this dataset it doesn't.

## 3. Indexes that don't help, and the reasons

* **q03 (daily VWAP over all rows):** about 37–39 s under every configuration.
  It has to read all 15M rows, so every plan is a seq scan. The time is *not* in the
  scan, though (4.5 s). It's in the sort below the aggregate: 15M rows, external
  merge, 444 MB spilled to disk. The planner guessed **15M groups** for
  `GROUP BY ts::date, symbol`, because it has no statistics on the expression
  `ts::date`. With that guess, sort-then-group looked cheaper than hashing.
  `CREATE STATISTICS ... (ndistinct) ON (ts::date), symbol` fixes the estimate to the
  true 500 groups. The planner then switches to a 4-worker partial aggregate:
  **38.2 s → 3.0 s, a 12.6x speed-up with no index at all**. Comparing `symbol` under
  the `C` collation instead of `English_India.1252` saves another third of the sort
  (38.2 → 24.0 s), because locale-aware string comparison is expensive.
* **Low cardinality (`side`, 2 values):** the planner ignores `btree(side)` and keeps
  the seq scan (1,113 ms). When I forced the index scan, it was actually *faster*
  (786 ms) despite touching more pages (138k vs 126k). The whole table was in cache,
  so random reads weren't the penalty the cost model assumes. Even then, the index
  saves only about 30% on half the table, which isn't worth 100 MB of index plus slower
  writes.
* **Non-sargable predicates:** `WHERE date_trunc('hour', ts) = '10:00'` cannot use
  `btree(ts)`. It became a seq scan that discarded 14.8M rows (742 ms). The same
  question as a range, `ts >= '10:00' AND ts < '11:00'`, took 73 ms. An expression
  index on `date_trunc('hour', ts)` also fixes it (68 ms), but rewriting the predicate
  is free.

## 4. A "better" index can make a query slower (q02)

5-min bars for the whole market over one day: **4.9 s with no index, 9.3 s with
BRIN(ts), 9.5 s with B-tree(ts).** The index did make the scan faster: the BRIN bitmap
heap scan took 0.4 s, vs 0.76 s per worker for the parallel seq scan. But it changed the
shape of the plan:

* No index: a 4-worker Parallel Seq Scan → five in-memory quicksorts (~30 MB each) → Gather Merge.
* BRIN: a single-process Bitmap Heap Scan → one 1.5M-row sort that no longer fits in
  `work_mem` (128 MB) → **external merge, 78 MB on disk** → about 7 s in the sort alone.

The planner charged for the bitmap scan but underestimated the cost of losing
parallelism on the sort. `(symbol, ts)` doesn't hurt q02, because the planner can't use
that index for an all-symbols time range and stays on the parallel seq scan. The lesson:
**read the whole plan, not just the scan node.** Fixes to try next: raise `work_mem` for
this query, or pre-aggregate bars in a materialized view.

## 5. After the index, the bottleneck moves into the window functions

On q05, the covering index produces 584k MSFT quotes in **0.31 s** with an index-only
scan and zero heap fetches. The query still takes 5.1 s, because the two `WindowAgg`
nodes over `numeric` take the rest. The same holds for q04 and q06. Indexing gave about
1.5–2x on these queries, and the remaining cost is arithmetic.

An obvious-looking optimisation backfired: casting the spread to `float8` before the
window functions made q05 run **past the 60 s timeout** (from 7.1 s). Moving-window
aggregates in Postgres rely on an *inverse transition function*, which removes the row
that leaves the frame instead of recomputing the whole frame. `avg(numeric)` has one.
`avg(float8)` deliberately doesn't (removing values from a float sum loses precision),
so every row re-aggregates its entire 1-minute frame. That makes the window cost
O(n × frame) instead of O(n).

## 6. What I'd actually ship

`brin(ts)` plus `(symbol, ts) INCLUDE (...)`, i.e. the `brin_plus_covering` column:

* 48 KB + 104 KB of BRIN covers every time-slice scan and every retention job
  (`DELETE ... WHERE ts < ...`).
* The covering B-tree handles all per-symbol work with index-only scans: bars, VWAP,
  spreads, and as-of joins at a flat cost per trade regardless of symbol liquidity.
* Plus extended statistics on `(ts::date, symbol)`. That was the single biggest win in
  the whole project, and it isn't an index.
* Then watch q02-style plans. If BRIN turns a parallel plan into a serial one, raise
  `work_mem` or keep minute bars in a materialized view.

## Caveats

* Synthetic data. Real ITCH has auctions, halts, odd-lot noise and much burstier
  arrivals. Physical time order is the property that matters most here, and real
  feeds have it too.
* Everything was cached (data ≈ 3.6 GB, RAM 16 GB, shared_buffers 2 GB), so these
  are CPU and memory timings, not disk timings. On cold cache, the B-tree vs BRIN gap
  for wide windows would grow in BRIN's favour.
* Single runs on a laptop. Differences under about 10% (e.g. q04 BRIN vs covering)
  are noise.
