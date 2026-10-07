"""FinMind vs TEJ: where the two sources differ, and which one is right.

The comparison is made on returns, not price levels. Both adjusted series are
back-adjusted to "today", so their levels differ by a per-stock constant
whenever the two were refreshed on different days; daily returns do not.

Three FinMind series are compared against TEJ's adjusted close:

  fm      finmind.v_adj_daily_prices  -- what a user of this warehouse gets
  fm_up   raw ticker files first      -- what FinMind's per-ticker endpoint serves
  raw     finmind.v_daily_prices      -- unadjusted, the yardstick for "who adjusted"

``fm`` and ``fm_up`` differ only because the processed layer lets whole-market
day files override per-ticker history; for an adjusted series a day file is a
snapshot of an older adjustment basis.

Outputs: outputs/tables/cmp_*.csv
"""

from common import FINMIND_RAW, LISTED, connect, save, setup_output

setup_output()
con = connect()

AGREE = 0.001      # 10 bp: rounding noise (TEJ keeps 4 decimals, FinMind 6)
MATERIAL = 0.01    # 1 %: a real disagreement about that day's return
# Daily price limit: 7 % until 2015-05-29, 10 % from 2015-06-01. +0.5 % slack
# for tick rounding at low prices.
LIMIT = "CASE WHEN date >= DATE '2015-06-01' THEN 0.105 ELSE 0.075 END"

# --------------------------------------------------------------------------
# FinMind adjusted series as the per-ticker endpoint serves it (ticker wins).
# --------------------------------------------------------------------------
raw_dir = FINMIND_RAW.as_posix()
con.execute(f"""
CREATE TEMP TABLE fm_adj_up AS
WITH t AS (
    SELECT TRY_CAST(date AS DATE) AS date, stock_id, CAST(close AS DOUBLE) AS close
    FROM read_parquet('{raw_dir}/adj-daily-prices-ticker/*.parquet')
), m AS (
    SELECT TRY_CAST(date AS DATE) AS date, stock_id, CAST(close AS DOUBLE) AS close
    FROM read_parquet('{raw_dir}/adj-daily-prices-market/*.parquet')
)
SELECT * FROM t
UNION ALL
SELECT * FROM m ANTI JOIN t USING (stock_id, date)
""")

con.execute("""
CREATE TEMP TABLE px AS
SELECT t.stock_id, t.stock_name, t.date, t.market,
       t.close AS tej, t.bid, t.offer, t.return_pct AS tej_return_pct,
       t.volume_k_shares * 1000 AS tej_vol, t.turnover_k * 1000 AS tej_amt, t.transactions AS tej_tx,
       a.close AS fm, a.source AS fm_source, u.close AS fm_up,
       r.close AS raw, r.volume AS fm_vol, r.turnover_value AS fm_amt, r.transactions AS fm_tx
FROM tej.v_adj_daily_prices AS t
JOIN fm.v_adj_daily_prices AS a USING (stock_id, date)
LEFT JOIN fm_adj_up AS u USING (stock_id, date)
LEFT JOIN fm.v_daily_prices AS r USING (stock_id, date)
""")

con.execute(f"""
CREATE TEMP TABLE rets AS
WITH b AS (
    SELECT *,
           lag(tej) OVER w AS p_tej, lag(fm) OVER w AS p_fm, lag(fm_up) OVER w AS p_up, lag(raw) OVER w AS p_raw,
           date - lag(date) OVER w AS gap, row_number() OVER w AS seq
    FROM px
    WINDOW w AS (PARTITION BY stock_id ORDER BY date)
)
SELECT stock_id, stock_name, date, market, fm_source, seq, tej, fm, fm_up, raw, p_raw,
       tej / p_tej - 1 AS r_tej,
       fm / p_fm - 1 AS r_fm,
       fm_up / nullif(p_up, 0) - 1 AS r_up,
       raw / nullif(p_raw, 0) - 1 AS r_raw,
       -- A "clean" pair: FinMind recorded a regular trade on both days. Its
       -- unadjusted close is 0 on a day without one.
       (raw > 0 AND p_raw > 0) AS clean,
       {LIMIT} AS lim
FROM b
WHERE gap <= 7 AND p_tej > 0 AND p_fm > 0
""")

# --------------------------------------------------------------------------
# 1. Coverage
# --------------------------------------------------------------------------
save(con, "cmp_universe_overlap", """
WITH t AS (SELECT stock_id, is_current FROM tej.v_securities),
     fa AS (SELECT DISTINCT stock_id FROM fm.v_adj_daily_prices),
     fr AS (SELECT DISTINCT stock_id FROM fm.v_daily_prices)
SELECT count(*) AS tej_securities,
       count(*) FILTER (WHERE stock_id IN (SELECT stock_id FROM fr)) AS also_in_fm_unadjusted,
       count(*) FILTER (WHERE stock_id IN (SELECT stock_id FROM fa)) AS also_in_fm_adjusted,
       count(*) FILTER (WHERE NOT is_current) AS tej_stopped_trading,
       count(*) FILTER (WHERE NOT is_current AND stock_id IN (SELECT stock_id FROM fr)) AS stopped_in_fm_unadjusted,
       count(*) FILTER (WHERE NOT is_current AND stock_id IN (SELECT stock_id FROM fa)) AS stopped_in_fm_adjusted,
       (SELECT count(*) FROM fa WHERE stock_id NOT IN (SELECT stock_id FROM t)) AS fm_adjusted_not_in_tej,
       (SELECT count(*) FROM fr WHERE stock_id NOT IN (SELECT stock_id FROM t)) AS fm_unadjusted_not_in_tej
FROM t
""")

# Share of TEJ's exchange-listed rows that FinMind also holds, by year. This is
# the survivorship picture: TEJ keeps every company that later delisted.
save(con, "cmp_coverage_by_year", f"""
SELECT year(t.date) AS year, count(*) AS tej_listed_rows,
       count(DISTINCT t.stock_id) AS tej_listed_securities,
       count(DISTINCT r.stock_id) AS in_fm_unadjusted_securities,
       count(DISTINCT a.stock_id) AS in_fm_adjusted_securities,
       round(100.0 * count(r.stock_id) / count(*), 2) AS fm_unadjusted_row_pct,
       round(100.0 * count(a.stock_id) / count(*), 2) AS fm_adjusted_row_pct
FROM tej.v_adj_daily_prices AS t
LEFT JOIN fm.v_daily_prices AS r USING (stock_id, date)
LEFT JOIN fm.v_adj_daily_prices AS a USING (stock_id, date)
WHERE t.market IN {LISTED} AND t.date <= (SELECT max(date) FROM fm.v_daily_prices)
GROUP BY 1 ORDER BY 1
""")

save(con, "cmp_overlap_size", """
SELECT count(*) AS overlap_rows, count(DISTINCT stock_id) AS securities, min(date) AS first_date, max(date) AS last_date
FROM px
""")

# --------------------------------------------------------------------------
# 2. Price levels (why levels are the wrong thing to compare)
# --------------------------------------------------------------------------
save(con, "cmp_level_by_year", """
SELECT year(date) AS year, count(*) AS n,
       round(100.0 * count(*) FILTER (WHERE abs(tej - fm) <= 0.01) / count(*), 2) AS close_within_1c_pct,
       round(100.0 * count(*) FILTER (WHERE abs(tej - fm) / fm <= 0.001) / count(*), 2) AS close_within_10bp_pct,
       round(median(abs(tej - fm) / fm) * 100, 5) AS median_rel_diff_pct,
       round(quantile_cont(abs(tej - fm) / fm, 0.95) * 100, 3) AS p95_rel_diff_pct
FROM px WHERE fm > 0 GROUP BY 1 ORDER BY 1
""")

save(con, "cmp_latest_common_date", """
SELECT date, count(*) AS n, count(*) FILTER (WHERE tej = fm) AS exact_close,
       count(*) FILTER (WHERE abs(tej - fm) <= 0.01) AS within_1c,
       count(*) FILTER (WHERE abs(tej - fm) / fm > 0.001) AS off_by_more_than_10bp
FROM px WHERE date = (SELECT max(date) FROM px) GROUP BY 1
""")

# --------------------------------------------------------------------------
# 3. Daily returns
# --------------------------------------------------------------------------
save(con, "cmp_return_agreement", f"""
SELECT CASE WHEN market IN {LISTED} THEN 'listed (TSE/OTC/TIB)' ELSE 'emerging (REG)' END AS segment,
       clean AS both_days_traded, count(*) AS n,
       round(100.0 * count(*) FILTER (WHERE abs(r_tej - r_fm) <= {AGREE}) / count(*), 4) AS agree_within_10bp_pct,
       count(*) FILTER (WHERE abs(r_tej - r_fm) > {MATERIAL}) AS disagree_gt_1pct,
       round(corr(r_tej, r_fm), 6) AS correlation
FROM rets GROUP BY ALL ORDER BY 1, 2 DESC
""")

# Classify every material disagreement on a listed market.
con.execute(f"""
CREATE TEMP TABLE disagreements AS
SELECT *,
       CASE
         WHEN NOT clean THEN 'A no-trade day (FinMind unadjusted close = 0)'
         -- This warehouse, not FinMind: either the per-ticker series agrees
         -- with TEJ and only the processed layer does not (the dedupe bug
         -- fixed on 2026-10-04), or the day lies after the last weekly
         -- re-fetch, where market-day rows can sit on a newer basis than the
         -- ticker history before them.
         WHEN abs(r_tej - r_up) <= {AGREE} OR fm_source = 'market'
              THEN 'B pipeline: adjustment bases mixed (stale snapshot, or not yet re-fetched)'
         WHEN abs(r_fm - r_raw) <= {AGREE} AND abs(r_tej - r_raw) > {AGREE}
              THEN 'C FinMind left a corporate action unadjusted'
         WHEN abs(r_tej - r_raw) <= {AGREE} AND abs(r_fm - r_raw) > {AGREE}
              THEN 'D FinMind adjusted where the unadjusted price shows no event'
         ELSE 'E both adjusted, by different amounts'
       END AS cause
FROM rets
WHERE market IN {LISTED} AND abs(r_tej - r_fm) > {MATERIAL}
""")

save(con, "cmp_disagreement_causes", """
SELECT cause, count(*) AS days, count(DISTINCT stock_id) AS securities,
       round(median(abs(r_raw)) * 100, 2) AS median_abs_raw_return_pct,
       round(median(abs(r_tej)) * 100, 2) AS median_abs_tej_return_pct,
       round(median(abs(r_fm)) * 100, 2) AS median_abs_fm_return_pct,
       count(*) FILTER (WHERE abs(r_tej) > lim) AS tej_beyond_limit,
       count(*) FILTER (WHERE abs(r_fm) > lim) AS fm_beyond_limit
FROM disagreements GROUP BY 1 ORDER BY 1
""")

save(con, "cmp_disagreement_causes_by_year", """
PIVOT (SELECT year(date) AS year, left(cause, 1) AS cause FROM disagreements)
ON cause USING count(*) GROUP BY year ORDER BY year
""")

# When does FinMind start adjusting? Cause C by exchange and year, next to the
# number of unadjusted limit breaches (roughly, ex-right/ex-dividend days).
save(con, "cmp_unadjusted_actions_by_market_year", f"""
SELECT year(date) AS year,
       count(*) FILTER (WHERE market = 'TSE' AND clean AND abs(r_raw) > lim) AS tse_unadjusted_breaches,
       count(*) FILTER (WHERE market = 'TSE' AND clean AND abs(r_raw) > lim AND abs(r_fm) > lim) AS tse_still_in_fm,
       count(*) FILTER (WHERE market = 'TSE' AND clean AND abs(r_raw) > lim AND abs(r_tej) > lim) AS tse_still_in_tej,
       count(*) FILTER (WHERE market = 'OTC' AND clean AND abs(r_raw) > lim) AS otc_unadjusted_breaches,
       count(*) FILTER (WHERE market = 'OTC' AND clean AND abs(r_raw) > lim AND abs(r_fm) > lim) AS otc_still_in_fm,
       count(*) FILTER (WHERE market = 'OTC' AND clean AND abs(r_raw) > lim AND abs(r_tej) > lim) AS otc_still_in_tej
FROM rets WHERE seq > 6 GROUP BY 1 ORDER BY 1
""")

# No-trade days where TEJ moves anyway: is TEJ sitting at the price limit?
# (Exchange rule: a session with no trade but an unfilled limit-up bid or
# limit-down offer closes at that limit price.)
save(con, "cmp_no_trade_disagreements", """
SELECT count(*) AS days,
       count(*) FILTER (WHERE abs(r_fm) <= 0.0005) AS fm_flat,
       count(*) FILTER (WHERE abs(r_tej) BETWEEN lim - 0.012 AND lim) AS tej_at_limit,
       count(*) FILTER (WHERE abs(r_tej) > lim) AS tej_beyond_limit,
       count(*) FILTER (WHERE abs(r_fm) > lim) AS fm_beyond_limit
FROM disagreements WHERE left(cause, 1) = 'A'
""")

# Trading halts (gap > 7 calendar days). Capital reductions suspend trading,
# so they only show up here, not in the day-to-day pairs above.
save(con, "cmp_halt_resumptions", f"""
WITH b AS (
    SELECT *, lag(tej) OVER w AS p_tej, lag(fm) OVER w AS p_fm, lag(raw) OVER w AS p_raw,
           date - lag(date) OVER w AS gap
    FROM px WINDOW w AS (PARTITION BY stock_id ORDER BY date)
), h AS (
    SELECT *, tej / p_tej - 1 AS r_tej, fm / p_fm - 1 AS r_fm, raw / p_raw - 1 AS r_raw
    FROM b WHERE gap > 7 AND raw > 0 AND p_raw > 0 AND p_fm > 0 AND market IN {LISTED}
)
SELECT count(*) AS resumptions,
       count(*) FILTER (WHERE abs(r_tej - r_fm) > {MATERIAL}) AS sources_disagree,
       count(*) FILTER (WHERE abs(r_raw) > 0.25) AS unadjusted_jump_gt_25pct,
       count(*) FILTER (WHERE abs(r_raw) > 0.25 AND abs(r_tej) <= 0.25) AS tej_removed_jump,
       count(*) FILTER (WHERE abs(r_raw) > 0.25 AND abs(r_fm) <= 0.25) AS fm_removed_jump,
       count(*) FILTER (WHERE abs(r_raw) > 0.25 AND abs(r_fm - r_raw) <= {AGREE}) AS fm_left_unadjusted
FROM h
""")

# FinMind adjustments with no visible event in the unadjusted price (cause D).
save(con, "cmp_finmind_spurious_adjustments", """
SELECT stock_id, stock_name, date, market, round(r_raw * 100, 2) AS raw_pct, round(r_tej * 100, 2) AS tej_pct,
       round(r_fm * 100, 2) AS fm_pct, round(r_up * 100, 2) AS fm_ticker_pct
FROM disagreements WHERE left(cause, 1) = 'D' ORDER BY abs(r_fm - r_tej) DESC
""")

# --------------------------------------------------------------------------
# 4. Plausibility: an adjusted return cannot exceed the daily price limit
# --------------------------------------------------------------------------
# Listed markets, both days traded, and past each security's first five rows
# (no limit applies in the first five sessions after listing).
save(con, "cmp_beyond_limit", f"""
SELECT count(*) AS n,
       count(*) FILTER (WHERE abs(r_raw) > lim) AS unadjusted_beyond_limit,
       count(*) FILTER (WHERE abs(r_fm) > lim) AS fm_processed_beyond_limit,
       count(*) FILTER (WHERE abs(r_up) > lim) AS fm_ticker_beyond_limit,
       count(*) FILTER (WHERE abs(r_tej) > lim) AS tej_beyond_limit,
       count(*) FILTER (WHERE abs(r_fm) > 0.5) AS fm_processed_gt_50pct,
       count(*) FILTER (WHERE abs(r_up) > 0.5) AS fm_ticker_gt_50pct,
       count(*) FILTER (WHERE abs(r_tej) > 0.5) AS tej_gt_50pct
FROM rets WHERE market IN {LISTED} AND clean AND seq > 6
""")

save(con, "cmp_beyond_limit_by_year", f"""
SELECT year(date) AS year, count(*) AS n,
       count(*) FILTER (WHERE abs(r_raw) > lim) AS unadjusted,
       count(*) FILTER (WHERE abs(r_fm) > lim) AS fm_processed,
       count(*) FILTER (WHERE abs(r_up) > lim) AS fm_ticker,
       count(*) FILTER (WHERE abs(r_tej) > lim) AS tej
FROM rets WHERE market IN {LISTED} AND clean AND seq > 6 GROUP BY 1 ORDER BY 1
""")

# What TEJ still leaves beyond the limit: real events or residual errors?
save(con, "cmp_tej_beyond_limit_sample", f"""
SELECT stock_id, stock_name, date, market, round(r_raw * 100, 2) AS raw_pct, round(r_tej * 100, 2) AS tej_pct,
       round(r_fm * 100, 2) AS fm_pct, round(r_up * 100, 2) AS fm_ticker_pct
FROM rets WHERE market IN {LISTED} AND clean AND seq > 6 AND abs(r_tej) > lim
ORDER BY abs(r_tej) DESC LIMIT 40
""")

# --------------------------------------------------------------------------
# 5. What the disagreements cost: whole-period total return per security
# --------------------------------------------------------------------------
save(con, "cmp_total_return_gap", f"""
WITH s AS (
    SELECT stock_id, count(*) AS n,
           abs(sum(ln(1 + r_tej)) - sum(ln(1 + r_fm))) AS gap_processed,
           abs(sum(ln(1 + r_tej)) - sum(ln(1 + r_up))) AS gap_ticker
    FROM rets WHERE market IN {LISTED} AND r_up IS NOT NULL AND r_tej > -1 AND r_fm > -1 AND r_up > -1
    GROUP BY 1 HAVING count(*) >= 250
)
SELECT 'fm processed vs tej' AS pair, count(*) AS securities,
       count(*) FILTER (WHERE gap_processed <= 0.01) AS within_1pct,
       count(*) FILTER (WHERE gap_processed > 0.01 AND gap_processed <= 0.10) AS gap_1_to_10pct,
       count(*) FILTER (WHERE gap_processed > 0.10 AND gap_processed <= 0.50) AS gap_10_to_50pct,
       count(*) FILTER (WHERE gap_processed > 0.50) AS gap_over_50pct
FROM s
UNION ALL
SELECT 'fm ticker vs tej', count(*),
       count(*) FILTER (WHERE gap_ticker <= 0.01), count(*) FILTER (WHERE gap_ticker > 0.01 AND gap_ticker <= 0.10),
       count(*) FILTER (WHERE gap_ticker > 0.10 AND gap_ticker <= 0.50), count(*) FILTER (WHERE gap_ticker > 0.50)
FROM s
""")

# Same measure from 2008 on, where FinMind adjusts both exchanges.
save(con, "cmp_total_return_gap_since_2008", f"""
WITH s AS (
    SELECT stock_id, abs(sum(ln(1 + r_tej)) - sum(ln(1 + r_up))) AS gap
    FROM rets WHERE market IN {LISTED} AND date >= DATE '2008-01-01' AND r_up IS NOT NULL AND r_tej > -1 AND r_up > -1
    GROUP BY 1 HAVING count(*) >= 250
)
SELECT count(*) AS securities, count(*) FILTER (WHERE gap <= 0.01) AS within_1pct,
       count(*) FILTER (WHERE gap > 0.01 AND gap <= 0.10) AS gap_1_to_10pct,
       count(*) FILTER (WHERE gap > 0.10) AS gap_over_10pct, round(median(gap) * 100, 4) AS median_gap_pct
FROM s
""")

# TEJ breaches the limit where the unadjusted price does not: adjustments
# that made a return *less* plausible. Candidates for TEJ-side errors.
save(con, "cmp_tej_suspicious", f"""
SELECT stock_id, stock_name, date, market, round(r_raw * 100, 2) AS raw_pct, round(r_tej * 100, 2) AS tej_pct,
       round(r_up * 100, 2) AS fm_ticker_pct
FROM rets
WHERE market IN {LISTED} AND clean AND seq > 6 AND abs(r_tej) > lim AND abs(r_raw) <= lim
ORDER BY abs(r_tej) DESC
""", quiet=True)

save(con, "cmp_tej_suspicious_summary", f"""
SELECT count(*) AS tej_breach_while_unadjusted_within_limit,
       count(*) FILTER (WHERE abs(r_up) > lim) AS finmind_ticker_also_breaches,
       count(*) FILTER (WHERE abs(r_up) <= lim) AS only_tej_breaches
FROM rets
WHERE market IN {LISTED} AND clean AND seq > 6 AND abs(r_tej) > lim AND abs(r_raw) <= lim
""")

# --------------------------------------------------------------------------
# 6. The pipeline artefact, measured at the raw layer
# --------------------------------------------------------------------------
save(con, "cmp_stale_market_snapshots", f"""
WITH t AS (
    SELECT TRY_CAST(date AS DATE) AS date, stock_id, CAST(close AS DOUBLE) AS close
    FROM read_parquet('{raw_dir}/adj-daily-prices-ticker/*.parquet')
), m AS (
    SELECT TRY_CAST(date AS DATE) AS date, stock_id, CAST(close AS DOUBLE) AS close
    FROM read_parquet('{raw_dir}/adj-daily-prices-market/*.parquet')
)
SELECT count(*) AS rows_in_both, count(DISTINCT stock_id) AS securities,
       count(*) FILTER (WHERE abs(m.close / t.close - 1) > 0.001) AS rows_market_differs,
       count(DISTINCT stock_id) FILTER (WHERE abs(m.close / t.close - 1) > 0.001) AS securities_affected,
       min(date) AS first_market_day, max(date) AS last_market_day
FROM m JOIN t USING (stock_id, date) WHERE t.close > 0
""")

# --------------------------------------------------------------------------
# 7. No-trade days
# --------------------------------------------------------------------------
save(con, "cmp_finmind_zero_price_by_year", """
SELECT year AS year, count(*) AS n_rows, count(*) FILTER (WHERE close = 0) AS close_is_zero,
       count(*) FILTER (WHERE close = 0 AND volume > 0) AS zero_price_with_volume,
       round(100.0 * count(*) FILTER (WHERE close = 0) / count(*), 3) AS zero_pct
FROM fm.v_daily_prices GROUP BY 1 ORDER BY 1
""")

# On a no-trade day FinMind's adjusted series carries the last close forward.
# TEJ instead reports a reference close. Restrict to days where the adjustment
# factor is 1 (yesterday's adjusted close equals the unadjusted one) so TEJ's
# close can be compared with its own unadjusted bid / offer.
save(con, "cmp_no_trade_day_rule", f"""
WITH d AS (
    SELECT x.*, lag(x.tej) OVER w AS p_tej, lag(x.fm) OVER w AS p_fm, lag(x.raw) OVER w AS p_raw
    FROM px AS x WINDOW w AS (PARTITION BY stock_id ORDER BY date)
)
SELECT count(*) AS no_trade_days_at_factor_1,
       count(*) FILTER (WHERE abs(fm - p_fm) < 0.0051) AS fm_carries_last_close,
       count(*) FILTER (WHERE abs(tej - p_tej) < 0.0051) AS tej_equals_last_close,
       count(*) FILTER (WHERE abs(tej - p_tej) >= 0.0051 AND abs(tej - bid) < 0.0051) AS tej_moved_to_bid,
       count(*) FILTER (WHERE abs(tej - p_tej) >= 0.0051 AND abs(tej - offer) < 0.0051) AS tej_moved_to_offer,
       count(*) FILTER (WHERE abs(tej - p_tej) >= 0.0051 AND abs(tej - bid) >= 0.0051 AND abs(tej - offer) >= 0.0051) AS tej_other
FROM d
WHERE raw = 0 AND p_raw > 0 AND abs(p_tej - p_raw) < 0.0051 AND market IN {LISTED}
""")

save(con, "cmp_tej_zero_volume_rows", """
SELECT market, count(*) AS n_rows, count(*) FILTER (WHERE volume_k_shares = 0) AS zero_volume_rows,
       round(100.0 * count(*) FILTER (WHERE volume_k_shares = 0) / count(*), 3) AS zero_volume_pct
FROM tej.v_adj_daily_prices GROUP BY 1 ORDER BY 2 DESC
""")

# --------------------------------------------------------------------------
# 8. Volume, turnover, transactions
# --------------------------------------------------------------------------
save(con, "cmp_activity_by_year", """
SELECT year(date) AS year, count(*) AS n,
       round(100.0 * count(*) FILTER (WHERE abs(tej_vol - fm_vol) > 1000) / count(*), 3) AS volume_off_gt_1000_shares_pct,
       round(100.0 * count(*) FILTER (WHERE abs(tej_amt - fm_amt) > 1000) / count(*), 3) AS turnover_off_gt_1000_ntd_pct,
       round(100.0 * count(*) FILTER (WHERE tej_tx IS DISTINCT FROM fm_tx) / count(*), 3) AS transactions_differ_pct
FROM px GROUP BY 1 ORDER BY 1
""")

save(con, "cmp_volume_mismatch_shape", """
SELECT CASE WHEN date < DATE '2004-02-11' THEN 'before 2004-02-11' ELSE 'from 2004-02-11' END AS period,
       count(*) AS mismatched_rows,
       round(100.0 * count(*) FILTER (WHERE fm_vol > tej_vol) / count(*), 2) AS fm_larger_pct,
       round(quantile_cont(fm_vol / nullif(tej_vol, 0), 0.25), 4) AS ratio_p25,
       round(median(fm_vol / nullif(tej_vol, 0)), 4) AS ratio_median,
       round(quantile_cont(fm_vol / nullif(tej_vol, 0), 0.75), 4) AS ratio_p75
FROM px WHERE abs(tej_vol - fm_vol) > 1000 GROUP BY 1 ORDER BY 1
""")

# Is the pre-2004 mismatch a unit error (FinMind in lots / thousand NTD)?
save(con, "cmp_volume_unit_error", """
SELECT year(date) AS year, count(*) AS n,
       count(*) FILTER (WHERE fm_vol > 0 AND abs(tej_vol / fm_vol / 1000 - 1) < 0.01) AS fm_volume_is_1_1000th,
       count(*) FILTER (WHERE fm_amt > 0 AND abs(tej_amt / fm_amt / 1000 - 1) < 0.01) AS fm_turnover_is_1_1000th,
       round(100.0 * count(*) FILTER (WHERE fm_vol > 0 AND abs(tej_vol / fm_vol / 1000 - 1) < 0.01) / count(*), 2) AS volume_pct
FROM px WHERE date < DATE '2008-01-01' GROUP BY 1 ORDER BY 1
""")

save(con, "cmp_volume_unit_error_sample", """
SELECT stock_id, stock_name, date, tej_vol AS tej_volume_shares, fm_vol AS fm_volume, tej_amt AS tej_turnover_ntd,
       fm_amt AS fm_turnover, raw AS fm_unadjusted_close, round(fm_amt / nullif(fm_vol, 0), 2) AS fm_implied_price
FROM px WHERE stock_id IN ('2330', '2317') AND date BETWEEN DATE '2001-03-05' AND DATE '2001-03-09' ORDER BY 1, 3
""")

# FinMind's unadjusted coverage of TEJ's listed rows dips in 2007. Where?
save(con, "cmp_finmind_2007_hole", f"""
SELECT strftime(t.date, '%Y-%m') AS month, t.market, count(*) AS tej_rows,
       count(*) - count(r.stock_id) AS missing_in_finmind,
       count(DISTINCT t.date) AS tej_days, count(DISTINCT r.date) AS finmind_days
FROM tej.v_adj_daily_prices AS t LEFT JOIN fm.v_daily_prices AS r USING (stock_id, date)
WHERE t.market IN ('TSE','OTC') AND t.date BETWEEN DATE '2006-10-01' AND DATE '2008-03-31'
GROUP BY 1, 2 ORDER BY 1, 2
""")

# --------------------------------------------------------------------------
# 9. TEJ internal consistency: its own return field vs its own adjusted close
# --------------------------------------------------------------------------
save(con, "cmp_tej_internal_return_check", f"""
WITH b AS (
    SELECT market, date, close, return_pct, lag(close) OVER w AS p, date - lag(date) OVER w AS gap
    FROM tej.v_adj_daily_prices WINDOW w AS (PARTITION BY stock_id ORDER BY date)
)
SELECT CASE WHEN market IN {LISTED} THEN 'listed' ELSE 'emerging' END AS segment, count(*) AS n,
       count(*) FILTER (WHERE return_pct IS NULL) AS return_null,
       round(100.0 * count(*) FILTER (WHERE abs(return_pct / 100 - (close / p - 1)) <= 0.0005) / count(*), 4) AS matches_within_5bp_pct,
       count(*) FILTER (WHERE abs(return_pct / 100 - (close / p - 1)) > 0.01) AS differs_gt_1pct
FROM b WHERE gap <= 7 AND p > 0 GROUP BY 1 ORDER BY 1
""")

save(con, "cmp_tej_internal_return_sample", f"""
WITH b AS (
    SELECT stock_id, stock_name, market, date, close, return_pct, volume_k_shares, lag(close) OVER w AS p,
           date - lag(date) OVER w AS gap
    FROM tej.v_adj_daily_prices WINDOW w AS (PARTITION BY stock_id ORDER BY date)
)
SELECT year(date) AS year, count(*) AS rows_differ_gt_1pct,
       count(*) FILTER (WHERE volume_k_shares = 0) AS on_zero_volume_days,
       count(*) FILTER (WHERE abs(return_pct) < 0.0001) AS tej_return_field_is_zero
FROM b WHERE gap <= 7 AND p > 0 AND market IN {LISTED} AND abs(return_pct / 100 - (close / p - 1)) > 0.01
GROUP BY 1 ORDER BY 1
""")

# --------------------------------------------------------------------------
# 10. Case studies (series saved for plotting)
# --------------------------------------------------------------------------
save(con, "case_6669_stale_basis", """
SELECT date, tej, fm AS fm_processed, fm_up AS fm_ticker, raw AS fm_unadjusted, fm_source
FROM px WHERE stock_id = '6669' AND date BETWEEN DATE '2026-08-10' AND DATE '2026-09-16' ORDER BY 1
""")

save(con, "case_2540_capital_reduction", """
SELECT date, tej, fm AS fm_processed, fm_up AS fm_ticker, raw AS fm_unadjusted
FROM px WHERE stock_id = '2540' AND date BETWEEN DATE '2004-10-01' AND DATE '2008-06-30' ORDER BY 1
""", quiet=True)

save(con, "case_largest_disagreements", """
SELECT cause, stock_id, stock_name, date, round(r_raw * 100, 2) AS raw_pct, round(r_tej * 100, 2) AS tej_pct,
       round(r_fm * 100, 2) AS fm_pct, round(r_up * 100, 2) AS fm_ticker_pct
FROM disagreements WHERE left(cause, 1) IN ('B', 'C', 'D', 'E')
QUALIFY row_number() OVER (PARTITION BY left(cause, 1) ORDER BY abs(r_tej - r_fm) DESC) <= 6
ORDER BY cause, abs(r_tej - r_fm) DESC
""")

save(con, "cmp_disagreement_top_securities", """
SELECT stock_id, any_value(stock_name) AS stock_name, any_value(market) AS market, count(*) AS days,
       count(*) FILTER (WHERE left(cause, 1) = 'C') AS c_days, count(*) FILTER (WHERE left(cause, 1) = 'D') AS d_days,
       count(*) FILTER (WHERE left(cause, 1) = 'E') AS e_days,
       min(date) AS first_day, max(date) AS last_day
FROM disagreements WHERE left(cause, 1) IN ('C', 'D', 'E') GROUP BY 1 ORDER BY days DESC LIMIT 12
""")

con.close()
