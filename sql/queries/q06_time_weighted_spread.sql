-- Time-weighted average quoted spread per minute for one symbol/day.
-- Each quote is "alive" until the next one arrives (lead(ts)), so it is weighted
-- by how long it was the prevailing quote -- a plain avg() would over-weight bursts.
WITH q AS (
    SELECT ts,
           (ask_px - bid_px) / ((ask_px + bid_px) / 2) * 1e4               AS spread_bps,
           lead(ts, 1, %(day)s::timestamp + interval '16 hours') OVER (ORDER BY ts) AS next_ts
    FROM quotes
    WHERE symbol = %(symbol)s
      AND ts >= %(day)s::timestamp
      AND ts <  %(day)s::timestamp + interval '1 day'
)
SELECT date_bin('1 minute', ts, %(day)s::timestamp)                        AS minute,
       sum(spread_bps * extract(epoch FROM next_ts - ts))
         / nullif(sum(extract(epoch FROM next_ts - ts)), 0)                AS tw_spread_bps,
       count(*)                                                            AS n_updates
FROM q
GROUP BY 1
ORDER BY 1;
