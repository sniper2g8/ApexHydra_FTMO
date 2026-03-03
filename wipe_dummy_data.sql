-- =============================================================================
-- Wipe dummy data — run this on an existing DB to clear all data and reset
-- for your FTMO demo account. The EA will register real capital via /equity.
--
-- Run in Supabase SQL Editor. This keeps table structure and required rows.
-- =============================================================================

-- Clear all transactional/historical data
TRUNCATE TABLE equity CASCADE;
TRUNCATE TABLE trades CASCADE;
TRUNCATE TABLE transactions CASCADE;
TRUNCATE TABLE ea_logs CASCADE;
TRUNCATE TABLE ftmo_compliance CASCADE;
TRUNCATE TABLE regime_history CASCADE;
TRUNCATE TABLE signal_cache CASCADE;
TRUNCATE TABLE bot_commands CASCADE;
TRUNCATE TABLE forward_results CASCADE;
TRUNCATE TABLE learning_log CASCADE;
TRUNCATE TABLE model_performance CASCADE;
TRUNCATE TABLE system_logs CASCADE;

-- Clear market_regime and backtest_results (optional; repopulated by app)
DELETE FROM market_regime;
DELETE FROM backtest_results;

-- Reset bot_state: no dummy capital — FTMO demo will register via EA /equity.
-- Keeps one row so dashboard and app keep working. Set Initial capital in
-- dashboard to your FTMO account size for 5%/10% limits.
UPDATE bot_state
SET
  capital         = 0,
  initial_capital = NULL,
  is_running      = false,
  risk_day_start  = NULL,
  updated_at      = NOW()
WHERE id = (SELECT id FROM bot_state ORDER BY updated_at DESC LIMIT 1);

-- If you have multiple bot_state rows, keep only the latest and delete the rest:
-- DELETE FROM bot_state WHERE id NOT IN (SELECT id FROM bot_state ORDER BY updated_at DESC LIMIT 1);

-- symbol_scores and strategies are kept (app needs strategy names; symbols
-- can be re-seeded by Modal when the EA runs). To reset symbol_scores:
-- DELETE FROM symbol_scores;
