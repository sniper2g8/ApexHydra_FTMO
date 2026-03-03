-- =============================================================================
-- ApexHydra FTMO — Database Schema (Supabase/PostgreSQL)
-- Run this on a fresh Supabase project for FTMO-compliant trading.
--
-- FTMO rules enforced:
--   • Max Daily Loss: 5% of initial capital (equity cannot go below
--     previous midnight balance − 5% of initial)
--   • Max Loss: 10% of initial (equity cannot go below
--     max(initial, trailing peak) − 10% of initial)
--   • Min Trading Days: 4 (evaluation only; tracked in ftmo_compliance)
--   • Best Day Rule: best day ≤ 50% of positive days' profit (informational)
-- =============================================================================

-- Bot state: one row (or latest wins). FTMO defaults: 5% daily, 10% total.
CREATE TABLE IF NOT EXISTS bot_state (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  is_running        BOOLEAN NOT NULL DEFAULT false,
  mode              TEXT NOT NULL DEFAULT 'SAFE',
  capital           NUMERIC(18,2) NOT NULL DEFAULT 100000,
  initial_capital    NUMERIC(18,2),
  max_daily_dd      NUMERIC(5,4) NOT NULL DEFAULT 0.05,
  max_total_dd      NUMERIC(5,4) NOT NULL DEFAULT 0.10,
  max_concurrent_trades INT NOT NULL DEFAULT 5,
  max_trades_per_day   INT NOT NULL DEFAULT 20,
  risk_day_start    TIMESTAMPTZ,
  entry_throttle_mins  INT NOT NULL DEFAULT 5,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT chk_max_daily_dd CHECK (max_daily_dd >= 0 AND max_daily_dd <= 0.50),
  CONSTRAINT chk_max_total_dd CHECK (max_total_dd >= 0 AND max_total_dd <= 0.50)
);

COMMENT ON COLUMN bot_state.initial_capital IS 'FTMO: Initial Simulated Capital; used for 5%/10% limits. NULL = use capital.';
COMMENT ON COLUMN bot_state.max_daily_dd IS 'FTMO: Max Daily Loss = 5% of initial (0.05).';
COMMENT ON COLUMN bot_state.max_total_dd IS 'FTMO: Max Loss = 10% of initial (0.10).';

-- Equity snapshots (append-only). MT5 posts balance/equity here.
CREATE TABLE IF NOT EXISTS equity (
  id          BIGSERIAL PRIMARY KEY,
  timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  balance     NUMERIC(18,2) NOT NULL,
  equity      NUMERIC(18,2) NOT NULL,
  margin      NUMERIC(18,2) DEFAULT 0,
  drawdown    NUMERIC(8,6) DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_equity_timestamp ON equity (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_equity_balance ON equity (balance DESC);

-- Trades: opened via /open, closed via /close.
CREATE TABLE IF NOT EXISTS trades (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL,
  direction   TEXT NOT NULL,
  lot         NUMERIC(12,4),
  opened_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at   TIMESTAMPTZ,
  pnl         NUMERIC(18,2),
  strategy    TEXT,
  regime      TEXT,
  confidence  NUMERIC(6,4),
  features    JSONB,
  ticket      BIGINT,
  CONSTRAINT chk_direction CHECK (direction IN ('BUY', 'SELL', 'CLOSED'))
);

CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades (opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_pnl ON trades (pnl) WHERE pnl IS NOT NULL;

-- Market regime per symbol (one row per symbol, upserted).
CREATE TABLE IF NOT EXISTS market_regime (
  symbol      TEXT PRIMARY KEY,
  regime      TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- News blackouts: no new trades while active.
CREATE TABLE IF NOT EXISTS news_blackouts (
  id           BIGSERIAL PRIMARY KEY,
  source       TEXT NOT NULL,
  title        TEXT,
  currencies   TEXT,
  impact       TEXT,
  event_time   TIMESTAMPTZ,
  expires_at   TIMESTAMPTZ NOT NULL,
  active       BOOLEAN NOT NULL DEFAULT true,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_blackouts_active ON news_blackouts (active) WHERE active = true;

-- Deposits/withdrawals so P&L and drawdown are correct.
CREATE TABLE IF NOT EXISTS transactions (
  id            BIGSERIAL PRIMARY KEY,
  type          TEXT NOT NULL,
  amount        NUMERIC(18,2) NOT NULL,
  balance_after NUMERIC(18,2),
  event_time    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reported_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ticket        BIGINT UNIQUE,
  CONSTRAINT chk_txn_type CHECK (type IN ('deposit', 'withdrawal'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions (type);
CREATE INDEX IF NOT EXISTS idx_transactions_event_time ON transactions (event_time DESC);

-- Strategy scores (forward test / backtest).
CREATE TABLE IF NOT EXISTS strategies (
  id          BIGSERIAL PRIMARY KEY,
  strategy    TEXT UNIQUE NOT NULL,
  fwd_score   NUMERIC(8,6) DEFAULT 0,
  is_active   BOOLEAN NOT NULL DEFAULT false,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO strategies (strategy, fwd_score, is_active) VALUES
  ('trend_following', 0.5, true),
  ('mean_reversion',  0.5, false),
  ('breakout',        0.5, false)
ON CONFLICT (strategy) DO NOTHING;

-- Forward test history.
CREATE TABLE IF NOT EXISTS forward_results (
  id          BIGSERIAL PRIMARY KEY,
  tested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  tf_score    NUMERIC(8,6),
  mr_score    NUMERIC(8,6),
  bo_score    NUMERIC(8,6)
);

CREATE INDEX IF NOT EXISTS idx_forward_results_tested_at ON forward_results (tested_at DESC);

-- Backtest results (weekly).
CREATE TABLE IF NOT EXISTS backtest_results (
  id             BIGSERIAL PRIMARY KEY,
  symbol         TEXT NOT NULL,
  strategy       TEXT NOT NULL,
  score          NUMERIC(8,6),
  sharpe         NUMERIC(8,4),
  win_rate       NUMERIC(6,4),
  max_dd         NUMERIC(6,4),
  profit_factor  NUMERIC(8,4),
  num_trades     INT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Learning log (online PPO updates).
CREATE TABLE IF NOT EXISTS learning_log (
  id          BIGSERIAL PRIMARY KEY,
  strategy    TEXT NOT NULL,
  num_trades  INT NOT NULL,
  learned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_learning_log_learned_at ON learning_log (learned_at DESC);

-- Symbol rotation (enabled/score per symbol).
CREATE TABLE IF NOT EXISTS symbol_scores (
  symbol      TEXT PRIMARY KEY,
  enabled     BOOLEAN NOT NULL DEFAULT true,
  score       NUMERIC(8,6) DEFAULT 0.5,
  win_rate    NUMERIC(6,4) DEFAULT 0.5,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Regime history for cold-start recovery.
CREATE TABLE IF NOT EXISTS regime_history (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL,
  regime      TEXT NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_regime_history_symbol_detected ON regime_history (symbol, detected_at DESC);

-- Signal cache: /signal → /open handoff (cross-container).
CREATE TABLE IF NOT EXISTS signal_cache (
  symbol      TEXT PRIMARY KEY,
  direction   TEXT,
  strategy    TEXT,
  regime      TEXT,
  confidence  NUMERIC(6,4),
  features    JSONB,
  cached_at   TIMESTAMPTZ
);

-- EA command queue (e.g. close_all).
CREATE TABLE IF NOT EXISTS bot_commands (
  id               BIGSERIAL PRIMARY KEY,
  command          TEXT NOT NULL,
  issued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  executed         BOOLEAN NOT NULL DEFAULT false,
  executed_at      TIMESTAMPTZ,
  acknowledged_at  TIMESTAMPTZ,
  ack_source       TEXT,
  ack_closed       INT
);

CREATE INDEX IF NOT EXISTS idx_bot_commands_executed ON bot_commands (executed);

-- EA logs (for dashboard / debugging).
CREATE TABLE IF NOT EXISTS ea_logs (
  id         BIGSERIAL PRIMARY KEY,
  ea_time    TIMESTAMPTZ,
  symbol     TEXT,
  level      TEXT NOT NULL DEFAULT 'INFO',
  message    TEXT,
  logged_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ea_logs_logged_at ON ea_logs (logged_at DESC);

-- Model performance (PPO vs heuristic agreement).
CREATE TABLE IF NOT EXISTS model_performance (
  id                BIGSERIAL PRIMARY KEY,
  timestamp         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  symbol            TEXT,
  strategy          TEXT,
  agreement         NUMERIC(4,2),
  ppo_confidence    NUMERIC(6,4),
  final_confidence  NUMERIC(6,4)
);

CREATE INDEX IF NOT EXISTS idx_model_performance_timestamp ON model_performance (timestamp DESC);

-- System logs.
CREATE TABLE IF NOT EXISTS system_logs (
  id          BIGSERIAL PRIMARY KEY,
  timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  severity    TEXT NOT NULL,
  event_type  TEXT,
  message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_logs_timestamp ON system_logs (timestamp DESC);

-- FTMO compliance tracking (optional but recommended).
CREATE TABLE IF NOT EXISTS ftmo_compliance (
  id                    BIGSERIAL PRIMARY KEY,
  trading_day_date      DATE NOT NULL UNIQUE,
  balance_at_midnight   NUMERIC(18,2) NOT NULL,
  daily_pnl             NUMERIC(18,2) DEFAULT 0,
  trailing_peak         NUMERIC(18,2) NOT NULL,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE ftmo_compliance IS 'FTMO: one row per trading day (CE(S)T midnight); trailing_peak = max(initial, all prior EOD balances).';

-- =============================================================================
-- Seed: single bot_state row with FTMO defaults (1-Step Challenge style).
-- Set capital and initial_capital to your FTMO account size (e.g. 100000).
-- =============================================================================
INSERT INTO bot_state (
  is_running,
  mode,
  capital,
  initial_capital,
  max_daily_dd,
  max_total_dd,
  max_concurrent_trades,
  max_trades_per_day,
  entry_throttle_mins
) VALUES (
  false,
  'SAFE',
  100000,
  100000,
  0.05,
  0.10,
  5,
  20,
  5
);

-- Run the INSERT only once for a fresh DB.

-- =============================================================================
-- Migration for existing DBs (run if bot_state already exists without FTMO cols)
-- =============================================================================
-- ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS initial_capital NUMERIC(18,2);
-- UPDATE bot_state SET initial_capital = COALESCE(initial_capital, capital), max_daily_dd = 0.05, max_total_dd = 0.10 WHERE id = (SELECT id FROM bot_state ORDER BY updated_at DESC LIMIT 1);
