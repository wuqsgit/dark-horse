# AlphaDog V3.0 完整规格文档

> 版本：3.0  
> 更新：2026-06-29  
> 环境：Binance 永续合约测试网 (testnet.binancefuture.com)

---

## 1 系统概览

### 1.1 定位

AlphaDog 是基于**多因子评分**的 Binance 永续合约**全自动**交易系统，通过**四个维度**（技术面、期货面、链上面、热度面）综合评分选币，自动执行开平仓。

### 1.2 核心数据流

```
Binance Spot API ─→ Pipeline(每10min) ─→ SQLite ─→ Engine(每5min评分) ─→ Trader(每5min执行)
                                            ↑                              ↑
                                  Dune API(每30min链上数据)          Binance Futures API
```

### 1.3 核心参数

| 参数 | 值 |
|------|-----|
| 测试网 | testnet.binancefuture.com |
| 初始资金 | $5,000 USDT |
| 最大持仓 | 3 |
| 最小评分 | 50 |
| 评分间隔 | 5分钟 |
| 交易循环 | 5分钟 |
| 数据采集 | 现货10min / 链上30min |

---

## 2 系统架构

### 2.1 进程组成

| 进程 | 入口文件 | 职责 | 调度 |
|------|----------|------|------|
| pipeline | pipeline/main.py | 数据采集 | 10min(现货)/30min(链上) |
| engine | engine/run.py | 评分+回测 | 5min评分 / 1h回测 |
| trading | trader/runner.py | 交易执行 | 5min循环 |
| api | api/main.py | REST API | 持续(:8000) |
| frontend | frontend/ | Web前端 | 持续(:3000) |

### 2.2 文件结构

```
alphadog/
├── pipeline/
│   ├── main.py           # 入口，APScheduler调度
│   ├── binance_http.py    # 币安HTTP采集器
│   └── dune_collector.py # Dune链上数据采集
├── engine/
│   ├── run.py           # 评分+回测调度
│   ├── scoring.py        # 评分引擎核心
│   └── factor_weights.json # 因子权重配置
├── trader/
│   ├── runner.py        # 交易主循环
│   ├── execution.py   # 执行引擎(决策+下单)
│   ├── exchange.py  # Binance API封装
│   ├── risk.py    # 风控模块
│   ├── config.py  # 配置常量
│   ├── models.py # 数据模型
│   └── import_trades.py # 已禁用
├── strategies/
│   └── token_profiles.json # 代币分类+阈值
├── api/
│   └── main.py        # FastAPI应用
├── shared/
│   └── db.py       # 数据库操作
├── frontend/        # Vite+React前端
├── supervisord.conf # Supervisor配置
└── alphadog.db   # SQLite数据库
```

---

## 3 数据管道 Pipeline

### 3.1 Binance数据采集器

**文件**：pipeline/binance_http.py  
**类**：BinanceHTTPCollector

使用 httpx.AsyncClient 直连币安API，并发限制 asyncio.Semaphore(5)，批量 asyncio.gather 每批10个。

#### 3.1.1 获取交易对列表

```python
get_top_pairs(limit=200)
```

- 端点：GET /fapi/v1/ticker/24hr
- 获取所有USDT交易对
- 按24h成交量降序，取前200
- 过滤：成交量 > $100k

#### 3.1.2 采集K线数据

```python
collect_all(symbols)
```

**1h K线**：
- 端点：GET /api/v3/klines?symbol={pair}&interval=1h&limit=48
- 存入表：candles_1h

**15m K线**：
- 端点：GET /api/v3/klines?symbol={pair}&interval=15m&limit=48
- 存入表：candles_15m

#### 3.1.3 采集期货数据

- 资金费率：GET /fapi/v1/premiumIndex（批量预缓存）
- 持仓量：GET /fapi/v1/openInterest?symbol={pair}（逐对）
- 存入表：futures_data

#### 3.1.4 更新活跃币列表

```python
upsert_symbol(pair) → symbols表
```

### 3.2 Dune链上采集器

**文���**：pipeline/dune_collector.py  
**类**：DuneCollector

每30min采集，存入 onchain_flows 表。

**采集字段**：
- cex_net_flow_usd：24h净流量(USD)
- cex_net_flow_14d_usd：14天净流量(USD)
- cex_net_outflow_ratio：流出比率

---

## 4 评分引擎 Engine

### 4.1 调度

**文件**：engine/run.py  
**调度器**：APScheduler (AsyncIOScheduler)

| 任务 | 间隔 | 说明 |
|------|------|------|
| run_scoring | 5min | 对所有活跃币评分 |
| run_backtest | 1h | 回测验证 |

**评分数据流**：
```
fetch_active_symbols() 
  → fetch_klines_1h(symbols)
  → fetch_klines_15m(symbols)
  → fetch_futures(symbols)
  → fetch_onchain(symbols)
  → ScoringEngine.score_all(df_1h, df_15m, df_fut, df_onc)
  → insert_scores(db_rows) → alpha_scores表
```

**回测数据流**：
```
fetch_historical_scores()
  → fetch_price_history(symbols)
  → ScoringEngine.compute_backtest(df_scores, df_prices)
  → insert_backtest(db_rows)
  → auto-tune()
  → backtest_review表
```

### 4.2 评分引擎核心

**文件**：engine/scoring.py  
**类**：ScoringEngine

#### 4.2.1 四维评分体系

| 维度 | 权重 | 说明 | 数据来源 |
|------|------|------|--------|
| 技术面 | 50% | K线形态/均线/动量 | 1h/15m K线 |
| 期货面 | 25% | 持仓量/资金费率/溢价 | futures_data |
| 链上面 | 15% | CEX净流量 | onchain_flows |
| 热度面 | 10% | 成交量异常/社交信号 | 成交量 |

#### 4.2.2 七项子因子

**配置文件**：factor_weights.json

| 因子 | 权重 | 描述 |
|------|------|------|
| vol_quality | 20% | 成交量质量 |
| chip | 25% | 筹码分析 |
| absorption | 15% | 吸筹/派发 |
| position | 10% | 价格位置 |
| rsi | 12% | RSI动量 |
| atr | 8% | ATR波动率 |
| vol_ratio | 10% | 量比 |

**补充因子**：

| 因子 | 权重 | 映射 |
|------|------|------|
| vol_anomaly | 8 | 成交量异常→80/65/50/35/20 |
| vol_trend | 3 | 成交量趋势→80/65/50/35/20 |

#### 4.2.3 因子计算逻辑

**vol_quality（成交量质量）**：
- 比较当前成交量与48h均值
- 价量配合程度

**chip（筹码分析）**：
- 根据价格与MA20/MA60关系
- 判定筹码阶段：accumulation/distribution/reaccumulation/static

**absorption（吸筹/派发）**：
- 价格变动与成交量变动比值
- 吸筹：量增价稳
- 派发：量增价跌

**position（价格位置）**：
- 当前价格在48h区间相对位置
- 0-20%：低位
- 20-50%：中位
- 50-80%：中高位
- 80-100%：高位

**rsi（RSI动量）**：
- 14周期RSI
- 超买：>70
- 超卖：<30

**atr（波动率）**：
- 14周期ATR归一化
- 偏低/正常/偏高/极高

**vol_ratio（量比）**：
- 当前成交量与均量比值

#### 4.2.4 评分输出

```python
{
    "time": "2026-06-29T08:00:00",
    "symbol": "BTCUSDT",
    "composite_score": 72.5,
    "composite_summary": "趋势↑MA20+3.5%|量比1.2|RSI62|ATR0.8%|波动正常|筹码吸筹|吸筹强度0.3",
    "risk_label": "normal",
    "chip_phase": "accumulation",
    "trend_state": "uptrend",
    "trend_direction": "up",
    "volatility_level": "正常",
    "price_position": "中位",
    "relative_strength": 65.0,
    "market_price": 65000.0,
    "raw_features": {
        "vol_quality": 0.75,
        "chip": 0.8,
        "absorption": 0.6,
        "position": 0.5,
        "rsi": 62,
        "atr": 0.008,
        "vol_ratio": 1.2
    },
    "scan_id": "scan_20260629_0800"
}
```

#### 4.2.5 评分等级

| 等级 | 评分 | 含义 |
|------|------|------|
| S1 | ≥80 | 极强，优先开仓 |
| S2 | 70-80 | 强信��� |
| A1 | 60-70 | 中等偏强 |
| A2 | 55-60 | 中等 |
| B | 45-55 | 中性 |
| C | 30-45 | 弱，不开仓 |
| D | <30 | 极弱，平仓 |

#### 4.2.6 摘要字符串格式

管道符分隔：
```
趋势↑MA20+3.5%|量比1.2|RSI62|ATR0.8%|波动正常|筹码吸筹|吸筹强度0.3
```

---

## 5 交易执行层 Trader

### 5.1 交易主循环

**文件**：trader/runner.py  
**函数**：trading_loop()

每300秒执行：

```
1. get_balance()           → 检查余额
2. get_positions()       → 获取持仓
3. fetch_latest_scan()   → 获取最新评分
4. _log_category_ranking() → 打印分类排名
5. engine.decide()       → 决策
6. engine.execute()     → 执行
7. 对账机制           → trades表 vs 币安API
8. sleep(300)
```

### 5.2 执行引擎

**文件**：trader/execution.py  
**类**：ExecutionEngine

#### 5.2.1 决策函数 decide()

**阶段1：持仓管理（5种退出）**

| 退出条件 | 操作 | 触发 |
|---------|------|------|
| 硬止损 | 全平 | 浮亏≤-12% |
| 移动止盈 | 全平 | TP3后回撤12% |
| 强弱退出(≥3项) | 全平 | 评分<30+方向反+回撤>4%+筹码派发+高波低分 |
| 弱减50%(≥2项) | 减50% | 上述≥2且无移动止盈 |
| 弱减25%(≥1项) | 减25% | 上述≥1且TP1未触发 |
| TP1 | 减25% | 浮盈≥12% |
| TP2 | 减25% | 浮盈≥20%(TP1完成) |
| 无操作 | 保留 | 条件不满足 |

**弱信号5项检查**（每项+1）：
1. 评分 < 30
2. 方向反转（最新方向≠持仓方向）
3. 价格回撤 > 4%
4. 筹码派发 (chip_phase="distribution")
5. 高波动+低分 (volatility_level偏高/极高 且 评分<45)

**分批止盈规则**：
- TP1：浮盈≥12%，标记tp1_done=True
- TP2：浮盈≥20%，标记tp2_done=True，激活移动止盈trailing_active=True

**阶段2：开新仓**

**计算可用仓位数**：
```python
avail = max_positions - 当前持仓数 + 拟平仓数
```

**资金分层**：

| 类别 | 比例 | 池子金额 | 单仓上限 |
|------|------|---------|---------|
| 蓝筹 | 40% | $400 | 25% |
| 基本面 | 30% | $150 | 15% |
| 叙事/庄股 | 20% | $100 | 10% |
| Meme | 10% | $50 | 5% |

**分组选币流程**：
1. 评分降序遍历
2. 跳过已有持仓
3. 跳过已加入操作列表
4. 硬过滤检查 meets_hard_filters()
5. 判定类别 token_profiles.json
6. 同类只取评分最高
7. 选够可用仓位

**入场时序过滤**：
- 价格高位/超买 + 评分<65 → 跳过
- 高波动 + 非上升趋势 + 评分<60 → 跳过
- 相对强度<30 → 跳过
- EMA20斜率<-10% + 评分<55 → 跳过

**开仓参数**：
```python
{
    "action": "open",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "position_side": "LONG",
    "quantity": 0.01,
    "entry_price": 65000.0,
    "stop_loss": 63700.0,
    "take_profit": 67925.0,
    "leverage": 10,
    "tp1_price": 6780.0,
    "tp2_price": 6800.0,
    "tp3_price": 6925.0,
    "tp1_qty_pct": 0.25,
    "tp2_qty_pct": 0.25,
    "atr_value": 325.0,
    "reason": "评分S1+吸筹+趋势向上",
    "grade": "S1",
    "score": 82.5,
    "chasing_flag": 0,
    "invested": 400.0
}
```

#### 5.2.2 执行函数 execute()

**开仓 _execute_open()**：
1. set_leverage(symbol, leverage)
2. place_market_order(symbol, side, qty)
3. place_stop_order(reduceOnly MARKET) - 测试网用策略替代
4. place_take_profit_order - 测试网跳过
5. 初始化持仓跟踪

**全平 _execute_close()**：
1. 市价单全平 (qty=9999)
2. record_trade(source='system')
3. 清除跟踪状态

**减仓 _execute_partial_close()**：
1. 估算PNL（当前缺陷：非币安真实值）
2. 市价单减仓
3. record_trade(source='system')

### 5.3 交易所封装

**文件**：trader/exchange.py  
**类**：BinanceFutures

基于 httpx.Client，HMAC-SHA256签名。

#### 5.3.1 核心方法

```python
# 获取余额
get_balance(include_upnl=True) → /fapi/v2/account

# 获取持仓（建议用positionRisk）
get_positions() → /fapi/v2/positionRisk

# 设置杠杆
set_leverage(symbol, leverage) → /fapi/v1/leverage

# 市价单
place_market_order(symbol, side, qty) → /fapi/v1/order

# 止损单
place_stop_order(symbol, side, qty, stop_price) → /fapi/v1/order

# 止盈单
place_take_profit_order(symbol, side, qty, tp_price) → /fapi/v1/order

# 交易对信息
get_symbol_info(symbol) → /fapi/v1/exchangeInfo

# 获取可交易对
get_trading_symbols() → /fapi/v1/exchangeInfo

# 标记价格
get_mark_price(symbol) → /fapi/v1/premiumIndex

# K线
get_klines(symbol, interval, limit) → /fapi/v1/klines

# ATR计算
get_atr(symbol, period=14) → 从4h K线计算
```

#### 5.3.2 持仓格式

```python
{
    "symbol": "BTCUSDT",
    "positionSide": "LONG",
    "side": "LONG",
    "quantity": 0.01,
    "entry_price": 65000.0,
    "mark_price": 65100.0,
    "unrealized_pnl": 10.0,
    "leverage": 10
}
```

### 5.4 风控系统

**文件**：trader/risk.py

#### 5.4.1 仓位计算 calculate_position()

```python
calculate_position(exchange, symbol, price, balance) → dict
```

1. 获取ATR（4h K线14周期）
2. 风险预算 = 余额 × risk_per_trade_pct (5%)
3. 止损距离 = ATR × atr_multiplier_stop (2.0)
4. 止盈距离 = ATR × atr_multiplier_take_profit (4.5)
5. 预期仓位 = 风险预算 / 止损距离
6. 动态杠杆 = 期望金额 / (价格 × 仓位)
7. 返回 {quantity, leverage, stop_loss, take_profit, atr_value}

#### 5.4.2 方向判定 determine_side()

```python
determine_side(score_row) → "LONG" | "SHORT" | None
```

- 趋势向上 + 评分≥55 + 强度≥40 → LONG
- 趋势向下 + 评分≥55 + 强度≥40 → SHORT
- 否则 None

#### 5.4.3 硬过滤 meets_hard_filters()

```python
meets_hard_filters(score_row) → (passed: bool, reason: str)
```

**五项硬性拒绝**：
1. 成交量 < $1M → 拒绝
2. 波动率 > 偏低 → 拒绝（即只允许偏低/正常）
3. 价格位置 = 高位 → 拒绝
4. 相对强度 < 25 → 拒绝
5. 资金费率 > 0.1% → 拒绝

#### 5.4.4 分批止盈 calc_tp_levels()

```python
calc_tp_levels(entry_price, side, take_profit) → dict
```

```python
{
    "tp1_price": entry + tp*0.4,   # 40%
    "tp2_price": entry + tp*0.7,   # 70%
    "tp3_price": entry + tp*1.0,  # 100%
    "tp1_qty_pct": 0.25,
    "tp2_qty_pct": 0.25
}
```

#### 5.4.5 移动止盈 calc_trailing_stop()

```python
calc_trailing_stop(current_pnl_pct, highest_pnl_pct) → bool
```

从最高点回撤12%时触发全平。

---

## 6 配置常量

### 6.1 交易所配置

**文件**：trader/config.py

```python
EXCHANGE_CONFIG = {
    "testnet": True,
    "api_key": "***",
    "api_secret": "***",
    "base_url": "https://testnet.binancefuture.com",
}
```

### 6.2 交易参数

```python
TRADING_CONFIG = {
    "total_capital": 5000,
    "position_size_pct": 0.10,        # 10%
    "risk_per_trade_pct": 0.05,     # 5%
    "max_positions": 3,
    "min_score": 50,
    "rebalance_interval_min": 5,
}
```

### 6.3 硬过滤

```python
HARD_FILTERS = {
    "min_volume_usdt": 1_000_000,
    "max_volatility_level": "偏低",
    "disallowed_price_positions": ["高位"],
    "max_funding_rate": 0.001,
}
```

### 6.4 ATR参数

```python
ATR_CONFIG = {
    "period": 14,
    "atr_multiplier_stop": 2.0,
    "atr_multiplier_take_profit": 4.5,
}
```

### 6.5 止盈参数

```python
TAKE_PROFIT_CONFIG = {
    "tp1_pct": 0.12,    # 12%
    "tp2_pct": 0.20,     # 20%
    "tp3_pct": 0.30,    # 30%
    "tp1_qty_pct": 0.25,
    "tp2_qty_pct": 0.25,
    "trailing回撤pct": 0.12,
}
```

### 6.6 止损参数

```python
STOP_LOSS_CONFIG = {
    "hard_stop_pct": 0.12,  # 12%硬止损
}
```

---

## 7 API层

### 7.1 FastAPI端点

**文件**：api/main.py

| 端点 | 方法 | 描述 |
|------|------|------|
| /api/trading/status | GET | 完整状态 |
| /api/trading/positions | GET | 持仓列表 |
| /api/trading/balance | GET | 余额 |
| /api/trading/trades | GET | 交易记录(支持分页) |
| /api/trading/recent_trades | GET | 最近20条 |
| /api/trading/closed | GET | 已平仓统计 |
| /api/trading/total_trades | GET | 总交易数 |
| /api/trading/performance | GET | 历史性能 |
| /api/trading/positions_history | GET | 持仓历史 |
| /api/trading/scores | GET | 最新评分 |
| /api/trading/score_history | GET | 评分历史 |
| /api/trading/backtest | GET | 回测结果 |
| /api/trading/backtest-review | GET | 回测复盘 |
| /api/trading/factor-analysis | GET | 因子分析 |

### 7.2 状态响应格式

```json
{
    "success": true,
    "data": {
        "wallet_balance": 4883.13,
        "unrealized_pnl": 45.06,
        "total_equity": 4928.19,
        "trades_pnl": 37.98,
        "income_pnl": -70.50,
        "positions": [...],
        "trades_pnl_24h": 0,
        "total_trades_count": 14,
        "win_rate": 100.0,
        "current_drawdown": -0.024,
        "risk_level": "low"
    }
}
```

---

## 8 数据库

### 8.1 表结构

**数据库**：alphadog.db（WAL模式）

#### 8.1.1 symbols表

```sql
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT UNIQUE NOT NULL,
    is_active INTEGER DEFAULT 1,
    category TEXT,
    first_seen TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

#### 8.1.2 candles_1h表

```sql
CREATE TABLE candles_1h (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    quote_vol REAL,
    UNIQUE(time, symbol)
);
```

#### 8.1.3 candles_15m表

```sql
CREATE TABLE candles_15m (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    quote_vol REAL,
    UNIQUE(time, symbol)
);
```

#### 8.1.4 futures_data表

```sql
CREATE TABLE futures_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open_interest REAL,
    funding_rate REAL,
    mark_price REAL,
    UNIQUE(time, symbol)
);
```

#### 8.1.5 onchain_flows表

```sql
CREATE TABLE onchain_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cex_net_flow_usd REAL,
    cex_net_flow_14d_usd REAL,
    cex_net_outflow_ratio REAL,
    UNIQUE(time, symbol)
);
```

#### 8.1.6 alpha_scores表

```sql
CREATE TABLE alpha_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    composite_score REAL,
    composite_summary TEXT,
    risk_label TEXT,
    chip_phase TEXT,
    trend_state TEXT,
    trend_direction TEXT,
    volatility_level TEXT,
    price_position TEXT,
    relative_strength REAL,
    market_price REAL,
    raw_features TEXT,
    scan_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(time, symbol)
);
```

#### 8.1.7 trades表

```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    entry_time TEXT,
    exit_time TEXT,
    grade_at_entry TEXT,
    score_at_entry REAL,
    created_at TEXT DEFAULT (datetime('now')),
    source TEXT DEFAULT 'system',
    income_id TEXT
);
```

#### 8.1.8 backtest_results表

```sql
CREATE TABLE backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    grade TEXT,
    return_6h REAL,
    return_12h REAL,
    return_24h REAL,
    return_48h REAL,
    max_drawdown REAL,
    win_12h INTEGER,
    win_24h INTEGER,
    scan_id TEXT
);
```

#### 8.1.9 backtest_review表

```sql
CREATE TABLE backtest_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,
    review_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

#### 8.1.10 positions_history表

```sql
CREATE TABLE positions_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    quantity REAL,
    entry_price REAL,
    mark_price REAL,
    unrealized_pnl REAL,
    stop_loss REAL,
    take_profit REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
```

#### 8.1.11 factor_analysis表

```sql
CREATE TABLE factor_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,
    factor_name TEXT NOT NULL,
    result TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

---

## 9 代币分类

### 9.1 四类资产

**文件**：strategies/token_profiles.json

| 类别 | 评分阈值 | 风险系数 | 持仓上限 | 典型币种 |
|------|---------|---------|---------|----------|----------|
| 蓝筹 | 75 | 0.7 | 25% | BTC/ETH/SOL/XRP |
| 基本面 | 55 | 0.9 | 15% | UNI/AAVE/ARB |
| 叙事/庄股 | 53 | 1.15 | 10% | ORDI/PEPE/SATS |
| Meme | 49 | 1.3 | 5% | DOGE/WIF/JUP |

### 9.2 动态阈值

每1h回测后自动调整（auto-tune），当前仅优化胜率（待改进为EV）。

---

## 10 部署

### 10.1 Supervisor配置

**文件**：supervisord.conf

```
[program:pipeline]
command=python3 pipeline/main.py
stdout_logfile=/tmp/alphadog_pipeline.log

[program:engine]
command=python3 engine/run.py
stdout_logfile=/tmp/alphadog_engine.log

[program:trading]
command=python3 trader/runner.py
stdout_logfile=/tmp/alphadog_trading.log

[program:api]
command=uvicorn api.main:app --port 8000
stdout_logfile=/tmp/alphadog_api.log

[program:frontend]
command=npx vite --port 3000
stdout_logfile=/tmp/alphadog_frontend.log
```

### 10.2 启动命令

```bash
supervisord -c supervisord.conf
supervisorctl -s unix:///tmp/alphadog_supervisor.sock restart all
```

---

## 11 V3.0新增功能

### 11.1 EV + R:R双重开仓过滤

**目的**：避免高评分但盈亏比差的交易

**实现**：
1. 计算预期收益空间（最近压力位/历史高点）
2. 计算风险空间（ATR止损）
3. R:R = 收益空间 / 风险空间
4. 仅允许 R:R≥2 或 EV>0 的交易

### 11.2 交易冷却机制

**目的**：避免连续止损

**实现**：
- 止损后冷却6-24h
- 连续2次止损延长冷却
- 每日最大亏损次数限制

### 11.3 突破确认

**目的**：过滤假突破

**实现**：
- 价格突破最近20根K线高点
- 成交量放大至1.5倍以上
- 再配合Alpha Score达标

### 11.4 Entry Alpha 与 Hold Alpha分离

**目的**：区分开仓和持仓决策

**实现**：
- Entry Alpha：关注未来收益空间、突破质量
- Hold Alpha：关注趋势结束、资金流出

### 11.5 Score Decay评分衰减

**目的**���更平滑的持仓管理

**实现**：
- 记录Entry Score
- 衰减20分减25%
- 衰减30分减50%
- 衰减40分全平

### 11.6 ATR动态止盈

**目的**：自适应不同波动率

**实现**：
- TP1 = 2×ATR
- TP2 = 4×ATR
- TP3 = 6×ATR

### 11.7 Trade Quality Engine

**目的**：综合评估交易质量

**实现**：
- 综合Alpha Score/R:R/市场状态/流动性/相关性
- Trade Quality Score ≥80 → 标准仓
- 60-80 → 半仓
- <60 → 放弃

### 11.8 订单簿过滤

**目的**：基于真实流动性决策

**实现**：
- 盘口深度分析
- 大额挂单位置
- 盘口失衡程度

### 11.9 Exit Optimizer

**目的**：优化平仓规则

**实现**：
- 模拟多种退出方案
- 对比收益差异
- 反向优化参数

---

## 12 回测升级

### 12.1 事件驱动回测

**目的**：精确模拟止盈止损顺序

**实现**：
- 模拟下单/成交/滑点
- 按时间顺序触发条件

### 12.2 Walk Forward Validation

**目的**：防止过拟合

**实现**：
- 滚动训练/测试
- 保留Out-of-Sample测试集

### 12.3 Monte Carlo分析

**目的**：评估最坏情况

**实现**：
- 随机重排交易数千次
- 计算最大回撤分布

### 12.4 真实交易成本

**目的**：准确回测收益

**实现**：
- 手续费模型
- 滑点模型
- 资金费率模型

---

*文档完成：2026-06-29*