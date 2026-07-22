-- Daily VWAP and volume for every symbol over the whole dataset.
-- Reads every trade: no index can make "touch all rows" cheaper than a sequential scan.
SELECT ts::date                        AS day,
       symbol,
       sum(price * size) / sum(size)   AS vwap,
       sum(size)                       AS volume,
       count(*)                        AS n_trades
FROM trades
GROUP BY 1, 2
ORDER BY 1, 2;
