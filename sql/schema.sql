-- Tick-level market data, modelled on what you get after parsing NASDAQ ITCH 5.0:
--   trades  <- 'P' (non-cross trade) / 'E','C' (order executed) messages
--   quotes  <- top-of-book reconstructed from 'A','F','X','D','U' order messages
--
-- Deliberately created with NO indexes and NO primary keys: the benchmark adds
-- them one scenario at a time so you can see exactly what each one buys.
--
-- Timestamps are exchange-local (America/New_York) wall-clock time, so they are
-- plain `timestamp`. ITCH carries nanoseconds; Postgres keeps microseconds.
-- Prices are numeric(12,4) because ITCH prices are fixed-point with 4 decimals.

DROP TABLE IF EXISTS trades CASCADE;
DROP TABLE IF EXISTS quotes CASCADE;
DROP TABLE IF EXISTS symbols CASCADE;

CREATE TABLE symbols (
    symbol      text PRIMARY KEY,
    name        text NOT NULL,
    lot_size    int  NOT NULL DEFAULT 100
);

CREATE TABLE trades (
    ts          timestamp     NOT NULL,
    symbol      text          NOT NULL,
    price       numeric(12,4) NOT NULL,
    size        int           NOT NULL,
    side        char(1)       NOT NULL,   -- aggressor: 'B' buy, 'S' sell
    match_id    bigint        NOT NULL
);

CREATE TABLE quotes (
    ts          timestamp     NOT NULL,
    symbol      text          NOT NULL,
    bid_px      numeric(12,4) NOT NULL,
    bid_sz      int           NOT NULL,
    ask_px      numeric(12,4) NOT NULL,
    ask_sz      int           NOT NULL
);
