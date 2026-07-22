-- Rolling quoted spread (in basis points of mid) for one symbol/day, per quote update:
--   spread_bps_1m   : trailing 1-minute average  (time-based RANGE frame)
--   spread_bps_100q : trailing 100-update average (count-based ROWS frame)
WITH q AS (
    SELECT ts,
           (ask_px - bid_px) / ((ask_px + bid_px) / 2) * 1e4 AS spread_bps
    FROM quotes
    WHERE symbol = %(symbol)s
      AND ts >= %(day)s::timestamp
      AND ts <  %(day)s::timestamp + interval '1 day'
)
SELECT ts, spread_bps,
       avg(spread_bps) OVER (ORDER BY ts RANGE BETWEEN interval '1 minute' PRECEDING AND CURRENT ROW) AS spread_bps_1m,
       avg(spread_bps) OVER (ORDER BY ts ROWS  BETWEEN 99 PRECEDING AND CURRENT ROW)                AS spread_bps_100q
FROM q;
