-- "Show me the last 20 prints" for a thinly traded symbol: a needle-in-haystack
-- point query, the textbook case for a composite B-tree.
SELECT ts, price, size, side
FROM trades
WHERE symbol = %(rare_symbol)s
ORDER BY ts DESC
LIMIT 20;
