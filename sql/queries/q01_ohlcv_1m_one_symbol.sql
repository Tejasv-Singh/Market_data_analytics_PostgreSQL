-- 1-minute OHLCV bars for one symbol over one trading day.
-- Postgres has no first()/last() aggregate, so open/close come from window
-- functions over each bar, then a GROUP BY collapses the bar to one row.
-- (Ties on ts are broken by match_id so open/close are deterministic.)
WITH ticks AS (
    SELECT date_bin('1 minute', ts, %(day)s::timestamp) AS bar,
           price, size,
           first_value(price) OVER bar_w AS open,
           last_value(price)  OVER bar_w AS close
    FROM trades
    WHERE symbol = %(symbol)s
      AND ts >= %(day)s::timestamp
      AND ts <  %(day)s::timestamp + interval '1 day'
    WINDOW bar_w AS (PARTITION BY date_bin('1 minute', ts, %(day)s::timestamp)
                     ORDER BY ts, match_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
)
SELECT bar,
       min(open)   AS open,
       max(price)  AS high,
       min(price)  AS low,
       min(close)  AS close,
       sum(size)   AS volume,
       count(*)    AS n_trades
FROM ticks
GROUP BY bar
ORDER BY bar;
