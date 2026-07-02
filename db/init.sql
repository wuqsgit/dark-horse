-- TimescaleDB 初始化

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 1. 活跃交易对列表
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT,
    quote_asset TEXT DEFAULT 'USDT',
    is_active BOOLEAN DEFAULT TRUE,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 1h K线 (TimescaleDB hypertable)
CREATE TABLE IF NOT EXISTS candles_1h (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    quote_vol   DOUBLE PRECISION,
    trades      BIGINT,
    UNIQUE(time, symbol)
);
SELECT create_hypertable('candles_1h', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_candles_1h_symbol ON candles_1h(symbol, time DESC);

-- 3. 15m K线 (用于短期计算)
CREATE TABLE IF NOT EXISTS candles_15m (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    quote_vol   DOUBLE PRECISION,
    trades      BIGINT,
    UNIQUE(time, symbol)
);
SELECT create_hypertable('candles_15m', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_candles_15m_symbol ON candles_15m(symbol, time DESC);

-- 4. 合约数据
CREATE TABLE IF NOT EXISTS futures_data (
    time            TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    open_interest   DOUBLE PRECISION,
    funding_rate    DOUBLE PRECISION,
    mark_price      DOUBLE PRECISION,
    UNIQUE(time, symbol)
);
SELECT create_hypertable('futures_data', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_futures_symbol ON futures_data(symbol, time DESC);

-- 5. 链上流向数据 (Dune)
CREATE TABLE IF NOT EXISTS onchain_flows (
    time                TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    chain               TEXT,
    cex_inflow_usd      DOUBLE PRECISION,
    cex_outflow_usd     DOUBLE PRECISION,
    cex_net_flow_usd    DOUBLE PRECISION,
    cex_net_flow_14d_usd DOUBLE PRECISION,
    cex_net_outflow_ratio DOUBLE PRECISION,
    window_hours        INTEGER DEFAULT 24,
    UNIQUE(time, symbol)
);
SELECT create_hypertable('onchain_flows', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_onchain_symbol ON onchain_flows(symbol, time DESC);

-- 6. 评分结果 (历史追踪)
CREATE TABLE IF NOT EXISTS alpha_scores (
    time                TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    composite_score     DOUBLE PRECISION,
    composite_summary   TEXT,
    risk_label          TEXT,
    chip_phase          TEXT,
    trend_state         TEXT,
    trend_direction     TEXT,
    volatility_level    TEXT,
    price_position      TEXT,
    relative_strength   DOUBLE PRECISION,
    market_price        DOUBLE PRECISION,
    raw_features        JSONB,
    scan_id             TEXT,
    UNIQUE(time, symbol)
);
SELECT create_hypertable('alpha_scores', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_scores_symbol ON alpha_scores(symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_scores_composite ON alpha_scores(composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_scores_summary ON alpha_scores(composite_summary);
CREATE INDEX IF NOT EXISTS idx_scores_scan ON alpha_scores(scan_id);

-- 7. 回测结果
CREATE TABLE IF NOT EXISTS backtest_results (
    id              SERIAL PRIMARY KEY,
    run_time        TIMESTAMPTZ DEFAULT NOW(),
    symbol          TEXT NOT NULL,
    grade           TEXT,
    grade_score     DOUBLE PRECISION,
    grade_time      TIMESTAMPTZ,
    price_at_grade  DOUBLE PRECISION,
    return_6h       DOUBLE PRECISION,
    return_12h      DOUBLE PRECISION,
    return_24h      DOUBLE PRECISION,
    return_48h      DOUBLE PRECISION,
    max_drawdown    DOUBLE PRECISION,
    win_12h         BOOLEAN,
    win_24h         BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_bt_symbol ON backtest_results(symbol);
CREATE INDEX IF NOT EXISTS idx_bt_grade ON backtest_results(grade);

-- 8. 用户表 (5人)
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role        TEXT DEFAULT 'viewer',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 9. 个人收藏
CREATE TABLE IF NOT EXISTS user_favorites (
    user_id     INTEGER REFERENCES users(id),
    symbol      TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, symbol)
);

-- 插入默认用户 (密码: admin123)
INSERT INTO users (username, password_hash, role) VALUES
    ('admin', 'pbkdf2:sha256:600000$salt$hash', 'admin')
ON CONFLICT (username) DO NOTHING;
