-- 5-minute OHLCV bars for every symbol over one trading day (~1.5M trades in, ~4k bars out).
-- Same shape as q01 but a whole day of the market: a "big slice" query.
WITH ticks AS (
    SELECT symbol,
           date_bin('5 minutes', ts, %(day)s::timestamp) AS bar,
           price, size,
           first_value(price) OVER bar_w AS open,
           last_value(price)  OVER bar_w AS close
    FROM trades
    WHERE ts >= %(day)s::timestamp
      AND ts <  %(day)s::timestamp + interval '1 day'
    WINDOW bar_w AS (PARTITION BY symbol, date_bin('5 minutes', ts, %(day)s::timestamp)
                     ORDER BY ts, match_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
)
SELECT symbol, bar,
       min(open) AS open, max(price) AS high, min(price) AS low, min(close) AS close,
       sum(size) AS volume, count(*) AS n_trades
FROM ticks
GROUP BY symbol, bar
ORDER BY symbol, bar;
