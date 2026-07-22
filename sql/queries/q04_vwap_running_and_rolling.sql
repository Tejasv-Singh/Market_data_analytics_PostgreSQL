-- Intraday VWAP for one symbol/day, two ways, per trade:
--   vwap_cum : session-to-date VWAP      (ROWS frame, unbounded preceding)
--   vwap_5m  : trailing 5-minute VWAP    (RANGE frame with an interval offset)
SELECT ts, price, size,
       sum(price * size) OVER cum / sum(size) OVER cum AS vwap_cum,
       sum(price * size) OVER r5  / sum(size) OVER r5  AS vwap_5m
FROM trades
WHERE symbol = %(symbol)s
  AND ts >= %(day)s::timestamp
  AND ts <  %(day)s::timestamp + interval '1 day'
WINDOW cum AS (ORDER BY ts, match_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
       r5  AS (ORDER BY ts RANGE BETWEEN interval '5 minutes' PRECEDING AND CURRENT ROW);
