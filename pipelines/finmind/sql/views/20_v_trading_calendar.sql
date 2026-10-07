-- Trading calendar: official FinMind dates unioned with dates derived from the
-- price history. The derived segment covers 1994-10-01..1998-12-31 and includes
-- Saturdays, because the exchange traded on Saturdays until 1998.
--
-- Note this extends into the future (FinMind publishes the full year), so use
-- v_last_trading_day rather than max(date) when you mean "latest data".
CREATE OR REPLACE VIEW v_trading_calendar AS
SELECT
    date,
    source,
    year(date)                  AS year,
    month(date)                 AS month,
    dayofweek(date)             AS day_of_week,
    dayofweek(date) IN (0, 6)   AS is_weekend
FROM read_parquet('{{DB_ROOT}}/processed/expanded-tw-trading-dates/expanded_tw_trading_dates.parquet');
