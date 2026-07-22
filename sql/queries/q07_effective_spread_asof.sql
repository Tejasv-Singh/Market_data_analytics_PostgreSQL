-- Effective spread: for each trade, find the prevailing quote (the latest quote at
-- or before the trade) -- an "as-of join" -- and measure 2*|price - mid| / mid.
-- LATERAL + ORDER BY ts DESC LIMIT 1 is the classic Postgres as-of join; it is
-- only fast if an index can jump straight to (symbol, ts) and walk backwards.
SELECT t.ts, t.price, t.size, q.bid_px, q.ask_px,
       2 * abs(t.price - (q.bid_px + q.ask_px) / 2) / ((q.bid_px + q.ask_px) / 2) * 1e4 AS eff_spread_bps
FROM trades t
CROSS JOIN LATERAL (
    SELECT bid_px, ask_px
    FROM quotes
    WHERE quotes.symbol = t.symbol
      AND quotes.ts <= t.ts
    ORDER BY quotes.ts DESC
    LIMIT 1
) q
WHERE t.symbol = %(symbol)s
  AND t.ts >= %(day)s::timestamp + interval '10 hours'
  AND t.ts <  %(day)s::timestamp + interval '10 hours 30 minutes';
