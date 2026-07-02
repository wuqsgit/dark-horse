# AlphaDog — 自动交易系统产品需求文档 (PRD)

> 版本：3.0  
> 最后更新：2026-06-27  
> 运行环境：Binance 永续合约测试网 (testnet.binancefuture.com)

> **⚠️ 本版包含改进方案**：2.1 版本记录了当前系统设计 + 完整的优化建议。
> **标记说明**:
> - 🔧 = 对**现有**章节的改进/增强
> - 🆕 = 新增独立章节/功能
> 
> V2.1 新增 8 项建议：
> 1. 🆕 §4.5 Alpha Score 训练数据管道（ML 模型）
> 2. 🔧 §9.2.3 因子稳定性评估（IC_IR 条件限制）
> 3. 🆕 §9.5 真实交易成本模型（手续费+滑点+资金费率）
> 4. 🆕 §5.9 Portfolio Risk Engine（组合风控引擎）
> 5. 🆕 §4.6 市场状态识别系统（Market Regime Engine）
> 6. 🆕 §12 自动复盘系统
> 7. 🆕 §9.6 回测幸存者偏差修复
> 8. 🆕 §5.10 动态仓位管理（EV 驱动 + Kelly 后续升级）

> V3.0 新增 12 项建议：
> 1. 🆕 §5.11 Expected Value（EV）+ Risk Reward（R:R）双重开仓过滤
> 2. 🆕 §5.12 交易冷却机制（Trade Cooldown）
> 3. 🆕 §5.13 Breakout Confirmation（突破确认）
> 4. 🆕 §4.7 Entry Alpha 与 Hold Alpha 分离
> 5. 🔧 §5.3 Score Decay（评分衰减机制）
> 6. 🔧 §5.4 ATR 动态止盈替代固定百分比止盈
> 7. 🆕 §5.14 Trade Quality Engine（交易质量评分）
> 8. 🆕 §5.15 真实订单簿（Order Book）过滤
> 9. 🔧 §9.7 升级回测为事件驱动回测（Event Driven Backtest）
> 10. 🆕 §9.8 Walk Forward Validation 与 Out-of-Sample Testing
> 11. 🆕 §9.9 Monte Carlo 风险分析
> 12. 🆕 §5.16 Exit Optimizer（自动退出优化器）

---

## 目录

1. [系统概览](#1-系统概览)
2. [系统架构](#2-系统架构)
3. [数据管道 Pipeline](#3-数据管道-pipeline)
4. [评分引擎 Engine](#4-评分引擎-engine)
   - [4.5 🆕 Alpha Score 训练数据管道](#-45-改进方案--alpha-score-训练数据管道)
   - [4.6 🆕 市场状态识别系统](#-46-改进方案--市场状态识别系统market-regime-engine)
   - [4.7 🆕 Entry Alpha 与 Hold Alpha 分离](#-47-改进方案--entry-alpha-与-hold-alpha-分离)
5. [交易执行层 Trader](#5-交易执行层-trader)
   - [5.9 🆕 Portfolio Risk Engine](#-59-改进方案--portfolio-risk-engine组合风控引擎)
   - [5.10 🆕 动态仓位管理](#-510-改进方案--动态仓位管理)
   - [5.11 🆕 EV + R:R 双重开仓过滤](#-511-改进方案--expected-valueev-risk-rewardrr-双重开仓过滤)
   - [5.12 🆕 交易冷却机制](#-512-改进方案--trade-cooldown交易冷却机制)
   - [5.13 🆕 Breakout Confirmation](#-513-改进方案--breakout-confirmation突破确认)
   - [5.14 🆕 Trade Quality Engine](#-514-改进方案--trade-quality-engine交易质量评分)
   - [5.15 🆕 真实订单簿过滤](#-515-改进方案--真实订单簿order-book过滤)
   - [5.16 🆕 Exit Optimizer](#-516-改进方案--exit-optimizer自动退出优化器)
6. [风控系统 Risk](#6-风控系统-risk)
7. [API 层](#7-api-层)
8. [数据库](#8-数据库)
9. [回测系统升级](#9--改进方案--回测系统完整升级)
   - [9.2.3 🔧 因子稳定性评估](#-923-改进因子稳定性评估防止过拟合)
   - [9.5 🆕 真实交易成本模型](#-95-改进方案--真实交易成本模型)
   - [9.6 🆕 幸存者偏差修复](#-96-改进方案--修复回测幸存者偏差)
   - [9.7 🔧 事件驱动回测](#-97-改进方案--升级回测为事件驱动回测event-driven-backtest)
   - [9.8 🆕 Walk Forward Validation](#-98-改进方案--walk-forward-validation与-out-of-sample-testing)
   - [9.9 🆕 Monte Carlo 风险分析](#-99-改进方案--monte-carlo-风险分析)
10. [交易记录方案](#10-交易记录方案)
11. [部署与监控](#11-部署与监控)
12. [🆕 自动复盘系统](#12-自动复盘系统)

---

## 1. 系统概览

### 1.1 定位

AlphaDog 是一个基于**多因子评分**的 Binance 永续合约**半自动/全自动**交易系统。它不依赖单一技术指标，而是通过**四个维度（技术面、期货面、链上面、热度面）** 的综合评分，选出高分交易对并自动执行开平仓。

### 1.2 核心数据流

```
Binance Spot API ─→ Pipeline(每10min) ─→ SQLite ─→ Engine(每5min评分) ─→ Trader(每5min决策执行)
                                              ↑                            ↑
                                    Dune API(每30min链上数据)         Binance Futures API(查持仓/下单)
```

### 1.3 关键约束

| 项目 | 值 |
|------|-----|
| 测试网 | `testnet.binancefuture.com` |
| 初始资金 | $5,000 USDT |
| 最大同时持仓 | 3 |
| 最小入场评分 | 50（全局） |
| 评分间隔 | 每 5 分钟 |
| 交易循环 | 每 5 分钟 |
| 数据采集 | 现货 10min / 链上 30min |

---

## 2. 系统架构

### 2.1 进程组成

通过 Supervisor 管理的 5 个独立进程：

| 进程 | 文件 | 职责 | 调度 |
|------|------|------|------|
| `pipeline` | `pipeline/main.py` | 从币安和 Dune 采集原始数据 | 每 10min(现货) / 每 30min(链上) |
| `engine` | `engine/run.py` | 定时评分 + 回测 + 自动调参 | 评分每 5min / 回测每 1h |
| `trading` | `trader/runner.py` | 交易主循环：拉评分→决策→执行 | 每 5min |
| `api` | `api/main.py` | REST API（FastAPI） | 持续监听 :8000 |
| `frontend` | `frontend/` | Vite 前端 | 持续监听 :3000 |

### 2.2 文件树

```
alphadog/
├── pipeline/              # 数据采集层
│   ├── main.py            # Pipeline 入口（APScheduler）
│   ├── binance_http.py    # 币安 HTTP 采集器
│   └── dune_collector.py  # Dune Analytics 链上数据采集
├── engine/                # 评分引擎
│   ├── run.py             # Engine 入口（评分+回测调度）
│   ├── scoring.py         # ScoringEngine 核心评分逻辑
│   └── factor_weights.json # 因子权重配置
├── trader/                # 交易执行层
│   ├── runner.py          # 交易主循环入口
│   ├── execution.py       # ExecutionEngine（决策+下单）
│   ├── exchange.py        # BinanceFutures API 封装
│   ├── risk.py            # 仓位计算/硬过滤/方向判定
│   ├── config.py          # 配置常量
│   ├── models.py          # 数据模型
│   ├── import_trades.py   # 已禁用（历史数据导入）
├── strategies/            # 策略配置
│   └── token_profiles.json # 代币分类+阈值配置
├── api/                   # REST API
│   └── main.py            # FastAPI 应用
├── shared/                # 共享层
│   └── db.py              # 数据库操作
├── bot/                   # 旧版 bot（未使用）
├── frontend/              # 前端（Vite + React）
├── supervisord.conf       # Supervisor 进程管理配置
└── alphadog.db            # SQLite 数据库
```

### 2.3 配置常量 (`trader/config.py`)

```python
# 交易所
EXCHANGE_CONFIG = {
    "testnet": True,
    "api_key": "***",
    "api_secret": "***",
    "base_url": "https://testnet.binancefuture.com",
}

# 交易参数
TRADING_CONFIG = {
    "total_capital": 5000,
    "position_size_pct": 0.10,       # 每仓占总资金 10%
    "risk_per_trade_pct": 0.05,      # 每仓风险预算 5%
    "max_positions": 3,              # 最大持仓数
    "min_score": 50,                 # 最低评分
    "rebalance_interval_min": 5,     # 决策间隔
}

# 硬过滤
HARD_FILTERS = {
    "min_volume_usdt": 1_000_000,          # 最小 24h 成交量
    "max_volatility_level": "偏低",         # 最大允许波动率（"偏低"仅允许低波动）
    "disallowed_price_positions": ["高位"], # 禁止入场价格位置
    "max_funding_rate": 0.001,             # 最大资金费率
}
```

---

## 3. 数据管道 Pipeline

### 3.1 职责

从币安现货 API 和 Dune Analytics 采集原始数据，写入 SQLite。

### 3.2 Binance 数据采集器 (`pipeline/binance_http.py`)

**类：`BinanceHTTPCollector`**

使用 `httpx.AsyncClient` 直连币安 API（不依赖 ccxt），并发限制 `asyncio.Semaphore(5)`，批量 `asyncio.gather` 每批 10 个。

#### 3.2.1 获取交易对列表

```python
get_top_pairs(limit=200)
```

- 调 `/fapi/v1/ticker/24hr` 获取所有 USDT 交易对
- 按 24h 成交量排序，取前 200 个（每 10min 刷新）
- 过滤条件：成交量 > $100k

#### 3.2.2 采集数据

```python
collect_all(symbols)
```

对每个交易对同时采集：

##### (a) 1h K 线
- `GET /api/v3/klines?symbol={pair}&interval=1h&limit=48`
- 最近 48 根（约 2 天）
- 存入 `candles_1h`

##### (b) 15m K 线
- `GET /api/v3/klines?symbol={pair}&interval=15m&limit=48`
- 最近 48 根（约 12 小时）
- 存入 `candles_15m`

##### (c) 期货数据
- 资金费率：从 `/fapi/v1/premiumIndex` 批量获取（预缓存）
- 持仓量：逐对调 `/fapi/v1/openInterest?symbol={pair}`
- 存入 `futures_data`

##### (d) 更新活跃币列表
- 调 `upsert_symbol(pair)` 写入 `symbols` 表

### 3.3 Dune 链上数据采集器 (`pipeline/dune_collector.py`)

**类：`DuneCollector`**

每 30min 从 Dune Analytics 采集主流代币的 CEX 净流量数据。

采集字段：
- `cex_net_flow_usd` — 24h 净流量
- `cex_net_flow_14d_usd` — 14 天净流量
- `cex_net_outflow_ratio` — 流出比率

存入 `onchain_flows` 表。

---

## 4. 评分引擎 Engine

### 4.1 调度 (`engine/run.py`)

使用 `APScheduler (AsyncIOScheduler)`：

| 任务 | 间隔 | 描述 |
|------|------|------|
| `run_scoring` | 每 5min | 对所有活跃币种评分 |
| `run_backtest` | 每 1h | 对历史评分做回测验证 |

#### 评分数据流

```
fetch_active_symbols() → fetch_klines_1h(symbols)
                       → fetch_klines_15m(symbols)
                       → fetch_futures(symbols)
                       → fetch_onchain(symbols)
                       → ScoringEngine.score_all(df_1h, df_15m, df_fut, df_onc)
                       → insert_scores(db_rows)  # 写入 alpha_scores 表
```

#### 回测数据流（每 1h）

```
fetch_historical_scores() → fetch_price_history(symbols)
                          → ScoringEngine.compute_backtest(df_scores, df_prices)
                          → insert_backtest(db_rows)
                          → 自动调参（auto-tune）
                          → 生成回测复盘（backtest_review）
```

### 4.2 当前评分引擎 (`engine/scoring.py`)

**类：`ScoringEngine`**

#### 4.2.1 评分维度（当前）

四个维度，总权重 100%：

| 维度 | 权重 | 说明 | 数据来源 |
|------|------|------|----------|
| **技术面 (technical)** | 50% | K 线形态、均线、动量 | 1h/15m K线 |
| **期货面 (futures)** | 25% | 持仓量、资金费率、溢价 | futures_data |
| **链上面 (onchain)** | 15% | CEX 净流量 | onchain_flows |
| **热度面 (heat)** | 10% | 成交量异常、社交信号 | 成交量数据 |

#### 4.2.2 七项子权重 (`factor_weights.json`)（当前）

| 因子 | 权重 | 描述 |
|------|------|------|
| `vol_quality` | 0.20 (20%) | 成交量质量 |
| `chip` | 0.25 (25%) | 筹码分析 |
| `absorption` | 0.15 (15%) | 吸筹/派发 |
| `position` | 0.10 (10%) | 价格位置 |
| `rsi` | 0.12 (12%) | RSI 动量 |
| `atr` | 0.08 (8%) | ATR 波动率 |
| `vol_ratio` | 0.10 (10%) | 量比 |

补充因子（`custom_factors`）：

| 因子 | 权重 | 映射 |
|------|------|------|
| `vol_anomaly` | 8 | 成交量异常程度→分数(80/65/50/35/20) |
| `vol_trend` | 3 | 成交量趋势(6h/24h)→分数(80/65/50/35/20) |

#### 4.2.3 评分函数（当前）

```python
score_all(df_1h, df_15m, df_fut, df_onc) → list[dict]
```

对每个活跃币种，按时间对齐数据，逐行计算：

##### 因子计算逻辑

1. **vol_quality（成交量质量）**：比较当前成交量与 48h 均值，价量配合程度
2. **chip（筹码分析）**：根据价格与均线（MA20/MA60）关系判定筹码阶段
3. **absorption（吸筹/派发）**：价格变动与成交量变动的比值
4. **position（价格位置）**：当前价格在 48h 区间中的相对位置
5. **rsi（RSI 动量）**：14 周期 RSI，超买超卖判断
6. **atr（波动率）**：14 周期 ATR 归一化值
7. **vol_ratio（量比）**：当前成交量与均量比值

##### 输出字段

每个评分输出包含：

```python
{
    "time": datetime,          # 评分时间戳
    "symbol": "BTCUSDT",       # 交易对
    "composite_score": 72.5,   # 综合评分 0-100
    "composite_summary": str,  # 特征摘要（管道符分隔）
    "risk_label": str,         # 风险标签
    "chip_phase": str,         # 筹码阶段（accumulation/distribution/...）
    "trend_state": str,        # 趋势状态
    "trend_direction": str,    # 趋势方向（up/down/sideways）
    "volatility_level": str,   # 波动率等级（偏低/正常/偏高/极高）
    "price_position": str,     # 价格位置（低位/中位/高位/overbought/oversold）
    "relative_strength": float,# 相对强度 0-100
    "market_price": float,     # 当时市价
    "raw_features": dict,      # 原始特征（JSON）
    "scan_id": str,            # 轮次ID
}
```

#### 4.2.4 等级映射（当前）

| 等级 | 评分范围 | 含义 |
|------|---------|------|
| **S1** | ≥ 80 | 极强信号，优先开仓 |
| **S2** | 70–80 | 强信号 |
| **A1** | 60–70 | 中等偏强 |
| **A2** | 55–60 | 中等 |
| **B** | 45–55 | 中性（需要其他因素确认） |
| **C** | 30–45 | 弱，不考虑开仓 |
| **D** | < 30 | 极弱，考虑平仓 |

#### 4.2.5 评分摘要字符串格式 (`composite_summary`)（当前）

管道符分隔的关键特征摘要，例如：
```
趋势↑MA20+3.5%|量比1.2|RSI62|ATR0.8%|波动正常|筹码吸筹|吸筹强度0.3
```

含义：
- `趋势↑MA20+3.5%` — EMA20 上升斜率 +3.5%
- `量比1.2` — 成交量与均值比
- `RSI62` — 14周期 RSI
- `ATR0.8%` — 波动率占比
- `波动正常` / `偏低` / `偏高` / `极高`
- `筹码吸筹` / `派发` / `震荡`
- `吸筹强度0.3` — 吸筹能量值

### 4.3 当前回测与自动调参

#### 4.3.1 回测逻辑

```python
compute_backtest(df_scores, df_prices) → list[dict]
```

对每个评分记录，追踪其后 6h/12h/24h/48h 的价格变动：
- `return_6h/12h/24h/48h` — 各时间窗口收益率
- `max_drawdown` — 最大回撤
- `win_12h/win_24h` — 是否盈利（bool）

#### 4.3.2 当前自动调参 (`auto-tune`) — 仅优化胜率

每轮回测后，对每个代币类别：

1. 收集该类别 24h 胜率样本（≥30 个）
2. 在 `[max(35, 当前阈值-10), 当前阈值]` 区间以 1 为步进枚举阈值
3. **选择胜率最高的阈值**（当前缺陷：胜率高不代表收益高）
4. 若新阈值与当前不同，自动写入 `token_profiles.json`
5. 记录调参历史到 `backtest_review`

### 4.4 🔧 改进方案 — 评分引擎优化

#### 🔧 4.4.1 核心问题分析

当前评分体系本质上在回答**"这个币当前看起来怎么样"**，而非**"这个币未来12小时能赚钱吗"**。

所有当前因子（RSI、ATR、Position、Vol Ratio、Chip）都在描述**当前状态**，而非**未来收益期望**。

典型失败案例：Meme 币 RSI=78、量比=3.5、趋势向上 → 评分 S1 (82 分) → 开仓买入 → 接最后一棒。

因为系统无法区分**上涨初期/中期/末期**，所有阶段都给予高分。

#### 🔧 4.4.2 改进：Alpha Score（替代当前评分）

核心思路：**从"评价状态"改为"预测未来收益"**

```python
score = 0.4 × P(return_12h > 5%)
      + 0.3 × P(return_24h > 10%)
      - 0.3 × P(max_drawdown > 8%)
```

利用已有回测数据（`return_6h/12h/24h/48h` + `max_drawdown`），训练每个因子的预测能力。

#### 🔧 4.4.3 改进：引入 EV（期望收益）而非纯胜率

当前 Auto-Tune 只优化胜率，但**胜率高不代表赚钱**：

| 策略 | 胜率 | 平均盈利 | 平均亏损 | EV |
|------|------|---------|---------|-----|
| A | 70% | +3% | -10% | **-0.9** ❌ |
| B | 45% | +18% | -6% | **+4.5** ✅ |

统一评价标准：

```python
EV = win_rate × avg_win_pct - lose_rate × avg_loss_pct

fitness = 0.4 × EV + 0.3 × sharpe + 0.2 × win_rate - 0.1 × max_drawdown
```

评分和调参都以 EV 为核心指标。

#### 🔧 4.4.4 改进：增加趋势阶段识别（market_phase）

新增分类：

| 阶段 | 判定条件 | 操作限制 |
|------|---------|---------|
| `accumulation` | 成交量温和放大，价格在MA附近 | 允许做多 |
| `breakout` | 价格突破MA60，量比>1.5 | 允许做多 |
| `trend` | EMA20斜率>0，价格在MA20上方 | 允许做多，正常风控 |
| `euphoria` | RSI>75 + 资金费率高 + 价格远离EMA20 | **禁止追多** |
| `distribution` | 量价背离，主力派发 | 禁止开多 |

#### 🔧 4.4.5 改进：取消 Meme 分类的权重补偿

当前问题：叙事/庄股 × 1.1、Meme × 1.2 会导致垃圾币评分虚高。

**建议：取消所有权重补偿，统一评分标准。** 类别差异只影响**仓位大小**（蓝筹大仓、Meme 小仓），不影响**是否开仓**。

#### 🔧 4.4.6 改进：增加市场环境因子（market_regime）

当前评分完全不考虑 BTC 状态和市场风险偏好，导致 BTC 暴跌时山寨币评分仍然 80+。

新增调节因子：

```python
market_regime_score = 0   # 默认中性

if BTC > MA20:
    market_regime_score += 10
elif BTC < MA20:
    market_regime_score -= 10

if BTC连续跌3天:
    market_regime_score -= 15  # 禁止做多

if BTC > MA20 and 量比 > 1.2:
    market_regime_score += 5    # 趋势加强
```

### 🆕 4.5 改进方案 — Alpha Score 训练数据管道

**问题：** 当前 Alpha Score 已升级为未来收益预测框架，但尚未建立完整的数据训练链路。
概率预测仍依赖人工规则组合，本质上仍是经验评分系统而非数据驱动模型。

#### 🆕 4.5.1 新增 training_samples 数据表

将每一次评分时的**全部特征快照**永久保存，同时记录未来各窗口收益率。

```sql
CREATE TABLE training_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    feature_json TEXT,          -- 全部特征快照（技术面/期货面/链上面/热度面所有因子）
    return_6h REAL,            -- 未来 6h 收益率
    return_12h REAL,           -- 未来 12h 收益率
    return_24h REAL,           -- 未来 24h 收益率
    return_48h REAL,           -- 未来 48h 收益率
    max_drawdown REAL,         -- 未来 48h 最大回撤
    market_regime TEXT,        -- 当时市场状态
    created_at TEXT DEFAULT (datetime('now'))
);
```

所有评分行为自动生成训练样本。回测运行后更新该次评分对应的未来收益字段。

#### 🆕 4.5.2 引入轻量 ML 模型

当 `training_samples` 表样本量达到 **5,000~10,000 条**后，引入 LightGBM 或 XGBoost 模型训练：

**预测目标：**
| 目标 | 说明 |
|------|------|
| `P(Return_12h > 5%)` | 12 小时涨幅超 5% 的概率 |
| `P(Return_24h > 10%)` | 24 小时涨幅超 10% 的概率 |
| `P(MaxDrawdown > 8%)` | 最大回撤超 8% 的概率 |

**训练流程：**
```
1. 从 training_samples 读取特征 + 标签
2. 按 70/30 拆分训练集/验证集
3. 训练 LightGBM 回归模型（3 个目标各独立训练）
4. 保存模型到 model/ 目录
5. Alpha Score 不再由人工权重计算 → 直接由 ML 模型输出
```

**预期效果：** 从"经验策略机器人"升级为"数据驱动量化系统"

### 🆕 4.6 改进方案 — 市场状态识别系统（Market Regime Engine）

当前 §4.4.6 仅以简单的 BTC-MA20 判断作为市场环境调节。
此方案将其升级为独立的市场状态识别引擎。

**核心思路：** 绝大多数山寨币走势受 BTC 影响，个币评分应叠加市场状态修正。

#### 🆕 4.6.1 市场状态分类

| 状态 | 条件 | 操作限制 |
|------|------|---------|
| **Bull Market** | BTC > MA60 + MA20斜率>0 + 量比>1.0 | 正常开仓，仓位上限 +20% |
| **Bear Market** | BTC < MA60 + MA60斜率<0 | 所有开仓评分 -15 |
| **Sideways Market** | BTC在MA60±5% + 波动率偏低 | 缩小止盈至 ATR×3 |
| **Panic Market** | BTC 单日跌>8% + 量比>2.0 | 禁止做多，仅允许做空 |

**参考指标维度：**
- **BTC 趋势**：MA20 / MA60 相对关系、斜率
- **BTC 波动率**：ATR 比率、布林带宽度
- **BTC 成交量**：24h 量比
- **BTC Dominance**：参考币市主导率
- **Stablecoin 流入/流出**：市场情绪辅助判断

#### 🆕 4.6.2 对个币评分的修正

```python
def apply_market_regime(score, regime_state):
    if regime_state == "Bear Market":
        return score - 15  # 熊市所有开仓评分下调
    elif regime_state == "Panic Market":
        return 0 if is_long else score  # 恐慌禁止做多
    elif regime_state == "Bull Market":
        return score  # 牛市不做修正（防止虚高）
    return score  # 横盘不变
```

---

## 5. 交易执行层 Trader

### 5.1 交易主循环 (`trader/runner.py`)

```python
trading_loop()  # 主循环
```

每 300 秒执行一轮：

```
1. 检查账户余额（get_balance）
2. 获取当前持仓（get_positions）
3. 获取最新评分 Top（fetch_latest_scan）
4. 打印分类排名（_log_category_ranking）
5. 决策（engine.decide(top_symbols, positions)）
6. 执行（engine.execute(actions)）
7. 对账（对比 trades 表 vs 币安 income API）
8. 等待 300 秒
```

#### 5.1.1 分类排名日志

按四个类别（蓝筹/基本面/叙事/Meme）分别打印 Top 5 评分 + 未开仓原因：

- 📌 已持仓
- ✅ 本次开仓
- 评分 < 类别阈值
- 波动率偏高/极高
- 价格位 high/overbought
- 相对强度 < 30

### 5.2 执行引擎 (`trader/execution.py`)

**类：`ExecutionEngine`**

#### 5.2.1 决策函数

```python
decide(top_symbols, current_positions) → list[action]
```

分两阶段：

##### 阶段 1：持仓管理（5 种退出方式）

对每个已有持仓依次检查：

| 退出条件 | 操作 | 触发条件 |
|---------|------|---------|
| **硬止损** | 全平 | 浮亏 ≤ -12% |
| **移动止盈** | 全平 | TP3 后回撤 12% |
| **强弱退出 (≥3)** | 全平 | 评分<30 + 方向反 + 回撤>4% + 筹码派发 + 高波低分 中≥3 |
| **弱减50% (≥2)** | 减仓 50% | 上述条件≥2 且未激活移动止盈 |
| **弱减25% (≥1)** | 减仓 25% | 上述条件≥1 且 TP1 未触发 |
| **分批止盈 TP1** | 减仓 25% | 浮盈 ≥ 12% |
| **分批止盈 TP2** | 减仓 25% | 浮盈 ≥ 20%（TP1 已完成） |
| **无操作** | 保留 | 条件均不满足 |

**弱信号 5 项检查**（每项 +1 计数）：

1. **评分 < 30** — 评分跌至极弱
2. **方向反转** — 最新评分推荐方向与原持仓相反
3. **价格回撤 > 4%** — 从最高浮盈回撤超过 4%
4. **筹码派发** — `chip_phase == "distribution"`
5. **高波动 + 低分** — `volatility_level` 为偏高/极高 且 评分 < 45

**分批止盈规则**：
- TP1（25%）：浮盈 ≥ 12%，标记 `tp1_done=True`
- TP2（25%）：浮盈 ≥ 20%，标记 `tp2_done=True`，同时激活移动止盈 `trailing_active=True`

##### 阶段 2：开新仓（分组选币 + 分层资金）

**步骤 A：计算可用仓位数**

```python
avail = max_positions - 当前持仓数 + 本次拟平仓数
```

**步骤 B：资金分层**

总资金 $5,000 按类别比例分成四个资金池：

| 类别 | 资金池比例 | 池子金额 |
|------|-----------|---------|
| 蓝筹 | 40% | $400/仓（max 25%单仓） |
| 基本面 | 30% | $150/仓（max 15%单仓） |
| 叙事/庄股 | 20% | $100/仓（max 10%单仓） |
| Meme/超高风险 | 10% | $50/仓（max 5%单仓） |

**步骤 C：分组选币**

1. 从评分 Top 列表降序遍历
2. 跳过已有持仓、已加入操作列表的币
3. 应用硬过滤（`meets_hard_filters`）
4. 按 `token_profiles.json` 判定类别
5. 同类只取评分最高的一个
6. 直到选够可用仓位数

**步骤 D：入场时序过滤**

对每个候选币：

1. 价格位置为"高位"或"超买" + 评分 < 65 → 跳过
2. 高波动 + 非上升趋势 + 评分 < 60 → 跳过
3. 相对强度 < 30 → 跳过
4. EMA20 斜率 < -10% + 评分 < 55 → 跳过

**步骤 E：开仓参数**

```python
{
    "action": "open",
    "symbol": sym,
    "side": "BUY" or "SELL",         # 交易所 side
    "position_side": "LONG" or "SHORT",  # 方向
    "quantity": qty,                    # 仓位数量
    "entry_price": price,               # 入场价
    "stop_loss": stop_price,            # 止损价（ATR 计算）
    "take_profit": tp_price,            # 止盈价
    "leverage": lev,                    # 动态杠杆
    "tp1_price": ...,                   # 分批止盈
    "tp2_price": ...,
    "tp3_price": ...,
    "tp1_qty_pct": 0.25,               # 首批减仓比例
    "tp2_qty_pct": 0.25,               # 二批减仓比例
    "atr_value": ...,                   # ATR 值
    "reason": str,                      # 开仓理由
    "grade": str,                       # 评分等级
    "score": float,                     # 评分
    "chasing_flag": int,                # 追高标记（-5 或 0）
    "invested": float,                  # 实际投入金额
}
```

#### 5.2.2 执行函数

```python
execute(actions) → list[result]
```

##### 开仓 (`_execute_open`)

1. 设置杠杆
2. 市价单入场
3. 挂止损单（`reduceOnly MARKET` — 测试网限制）
4. 挂 TP1 减仓单（测试网跳过，由策略引擎替代平仓）
5. 初始化持仓跟踪状态

##### 全平 (`_execute_close`)

1. 市价单平仓（qty=9999 全平）
2. 记录交易到 `trades` 表（`record_trade`，source='system'）
3. 清除跟踪状态

##### 减仓 (`_execute_partial_close`)

1. 先记录交易（**当前缺陷**：估算 PnL = 未实现盈亏 × 减仓比例，而非币安真实值）
2. 市价单减仓指定比例
3. 记录到 `trades` 表（source='system'）

### 5.3 交易所封装 (`trader/exchange.py`)

**类：`BinanceFutures`**

基于 `httpx.Client`（同步），HMAC-SHA256 签名。

#### API 方法

| 方法 | 端点 | 用途 |
|------|------|------|
| `get_balance(include_upnl)` | `/fapi/v2/account` | 获取 USDT 余额 |
| `get_margin_balance()` | `/fapi/v2/account` | 全账户权益明细 |
| `get_positions()` | **当前: `/fapi/v2/account`** → `positionAmt` 解析 | 获取持仓 |
| `set_leverage(symbol, leverage)` | `/fapi/v1/leverage` | 设置杠杆 |
| `place_market_order(symbol, side, qty)` | `/fapi/v1/order` | 市价单（自动精度调整） |
| `place_stop_order(...)` | `/fapi/v1/order` | 止损单（测试网用 reduceOnly MARKET） |
| `place_take_profit_order(...)` | `/fapi/v1/order` | 止盈单（测试网跳过，返回模拟结果） |
| `get_symbol_info(symbol)` | `/fapi/v1/exchangeInfo` | 获取交易对精度 |
| `get_trading_symbols()` | `/fapi/v1/exchangeInfo` | 获取所有 TRADING 状态的对 |
| `get_mark_price(symbol)` | `/fapi/v1/premiumIndex` | 获取标记价格 |
| `get_klines(symbol, interval, limit)` | `/fapi/v1/klines` | 获取 K 线 |
| `get_atr(symbol, period=14)` | 从 4h K线计算 | ATR 波动率计算 |

#### 持仓格式（当前）

```python
{
    "symbol": "BTCUSDT",
    "side": "LONG" or "SHORT",
    "quantity": abs(positionAmt),
    "entry_price": entryPrice,
    "mark_price": notional / abs(amt),
    "unrealized_pnl": unrealizedProfit,
    "leverage": leverage,
}
```

### 5.4 🔧 改进方案 — 开仓逻辑优化

#### 🔧 5.4.1 问题：分类阈值量纲不统一

当前各分类阈值：
- 蓝筹 75
- 基本面 55
- 叙事 53
- Meme 49

同一个评分体系下，BTC 要 75 分才能开，Meme 只要 49 分，完全不合理。

**改进：统一开仓标准（≥60），类别只影响仓位大小**

| 类别 | 开仓门槛 | 仓位比例 |
|------|---------|---------|
| 蓝筹 | ≥60 | 25% |
| 基本面 | ≥60 | 15% |
| 叙事/庄股 | ≥60 | 10% |
| Meme | ≥60 | 5% |

#### 🔧 5.4.2 改进：增加连续评分确认

当前单次评分开仓，噪音信号多。

**改进：连续 2 轮评分确认**

```python
if score_t0 >= 60 and score_t1 >= 60:  # t0 和 t1 两轮评分均达标
    允许开仓
else:
    等待确认
```

#### 🔧 5.4.3 改进：增加时间止损

当前只有价格止损（ATR×2），缺时间维度。

```python
if 持仓时间 > 12h and 浮盈 < 2%:
    平仓  # 避免死拿垃圾单
```

### 5.5 🔧 改进方案 — 持仓接口修复

#### 🔧 5.5.1 问题分析

当前持仓来自 `/fapi/v2/account` 后解析 `positionAmt`，存在三个已知问题：

1. **零仓位误判**：`"0.000"` 在 Python 中 `if 0.000:` 为 False，但如果字符串被转换为 `"0.000"` 仍然为 True
2. **Hedge Mode 未处理**：Binance 返回 `positionSide: "LONG"/"SHORT"/"BOTH"`，代码只认 side
3. **mark_price 推导错误**：`mark_price = notional / abs(amt)`，当 `notional=0` 且 `amt≠0` 时会除零

#### 🔧 5.5.2 改进：改用 `/fapi/v2/positionRisk`

```python
def get_positions(self) -> list:
    data = self._request("GET", "/fapi/v2/positionRisk", signed=True)
    positions = []
    for pos in data:
        amt = float(pos.get("positionAmt", 0))
        if abs(amt) < 0.001:  # 改用绝对值比较代替 if positionAmt
            continue
        positions.append({
            "symbol": pos["symbol"],
            "positionSide": pos.get("positionSide", "BOTH"),  # 记录 Hedge Mode
            "side": "LONG" if amt > 0 else "SHORT",
            "quantity": abs(amt),
            "entry_price": float(pos.get("entryPrice", 0)),
            "mark_price": float(pos.get("markPrice", 0)),
            "unrealized_pnl": float(pos.get("unRealizedProfit", 0)),
            "leverage": int(pos.get("leverage", 1)),
        })
    return positions
```

#### 🔧 5.5.3 改进：持仓快照入库

每轮 `get_positions()` 结果写入 `positions_history` 表，用于回测复盘和 PnL 曲线分析。

### 5.6 风控系统 (`trader/risk.py`)

#### 5.6.1 仓位计算

```python
calculate_position(exchange, symbol, price, balance) → dict
```

根据 ATR 计算动态仓位：

1. 获取 ATR（从 4h K线 14 周期）
2. 风险预算 = 资金池余额 × `risk_per_trade_pct` (5%)
3. 止损距离 = ATR × `atr_multiplier_stop` (2.0)
4. 止盈距离 = ATR × `atr_multiplier_take_profit` (4.5)
5. 预期仓位 = 风险预算 / 止损距离
6. 动态杠杆 = 期望投入金额 / (价格 × 仓位)
7. 返回 `{quantity, leverage, stop_loss, take_profit, atr_value}`

#### 5.6.2 方向判定

```python
determine_side(score_row) → "LONG" or "SHORT" or None
```

基于评分特征判定多空方向：

- 趋势向上 (`trend_direction == "up"`) + 评分 ≥ 55 + 强度 ≥ 40 → LONG
- 趋势向下 + 评分 ≥ 55 + 强度 ≥ 40 → SHORT
- 否则返回 None（无法判定）

#### 5.6.3 硬过滤

```python
meets_hard_filters(score_row) → (passed: bool, reason: str)
```

五项硬性拒绝条件：

1. **成交量 ≥ $1M** — `quote_vol` < 1,000,000 则拒绝
2. **波动率 ≤ 偏低** — `volatility_level` > "偏低" 则拒绝（即只允许"偏低"和"正常"）
3. **价格位置非高位** — 不允许 `price_position == "高位"`
4. **相对强度 ≥ 25** — `relative_strength` < 25 则拒绝
5. **资金费率 ≤ 0.1%** — 从 `composite_summary` 解析，超 0.1% 拒绝

#### 5.6.4 分批止盈价计算

```python
calc_tp_levels(entry_price, side, take_profit) → dict
```

以 ATR 止盈距离为基础的三档止盈：

```python
{
    "tp1_price": entry ± tp*0.4,    # 40%
    "tp2_price": entry ± tp*0.7,    # 70%
    "tp3_price": entry ± tp*1.0,    # 100%
    "tp1_qty_pct": 0.25,            # 每档减 25%
    "tp2_qty_pct": 0.25,
}
```

#### 5.6.5 移动止盈触发

```python
calc_trailing_stop(current_pnl_pct, highest_pnl_pct) → bool
```

从最高点回撤 12% 时触发全平。

### 5.7 🔧 改进方案 — 增加组合风险控制

当前最多 3 仓，但可能 DOGE + PEPE + SHIB 全是同类风险。

**改进：增加相关性过滤器**

```python
CORRELATION_GROUPS = [
    ["BTC", "ETH", "SOL"],          # 主流
    ["DOGE", "SHIB", "PEPE", "WIF"],  # Meme
    ["LINK", "ATOM", "DOT", "NEAR"],  # 基础设施
]

已持仓类别中，同组只允许持有一个。
```

### 🆕 5.9 改进方案 — Portfolio Risk Engine（组合风控引擎）

**问题：** 当前仅限制最大持仓数量(3)，但可能同时持有 SOL、WIF、JUP 三个仓位。
这三个仓位全部暴露于 Solana 生态风险——当 SOL 暴跌时三个仓位同时亏损。

**解决方案：** 新增组合风控引擎，从单币风控升级为组合风控。

#### 🆕 5.9.1 监控维度

| 维度 | 说明 |
|------|------|
| **Sector Exposure** | 所属赛道暴露度（DeFi / Meme / AI / L1 / L2 / RWA / GameFi） |
| **Chain Exposure** | 所属链暴露度（Solana / Ethereum / Cosmos / BSC） |
| **Correlation Matrix** | 实时 24h 相关性矩阵（基于收盘价计算） |
| **Beta Exposure** | 对 BTC 的 Beta 系数 |

#### 🆕 5.9.2 暴露度限制规则

| 维度 | 上限 | 触发动作 |
|------|------|---------|
| Solana 生态（SOL+WIF+JUP...） | ≤ 40% 总资金 | 禁止新开 Solana 系仓 |
| Meme 资产（DOGE+PEPE+SHIB...） | ≤ 20% 总资金 | 禁止新开 Meme 仓 |
| 单一叙事/赛道（如 AI 叙事） | ≤ 30% 总资金 | 禁止同一叙事新仓 |
| 单币种 | ≤ 10% 总资金 | 禁止加仓 |

#### 🆕 5.9.3 实现方式

```python
def check_portfolio_risk(positions, new_symbol):
    from collections import defaultdict
    exposure = defaultdict(float)
    for p in positions:
        sector = get_sector(p["symbol"])
        chain = get_chain(p["symbol"])
        exposure[f"sector:{sector}"] += p["invested"]
        exposure[f"chain:{chain}"] += p["invested"]
    # 新币的加入是否会超限？
    new_sector = get_sector(new_symbol)
    total_risk = sum(exposure.values()) + estimated_invested
    if exposure.get(f"sector:{new_sector}", 0) / total_risk > SECTOR_LIMIT:
        return False, f"{new_sector}赛道暴露超限"
    return True, "OK"
```

**预期效果：** 显著降低系统性回撤，避免赛道集中风险

### 🆕 5.10 改进方案 — 动态仓位管理

**问题：** 当前系统主要决定"买不买"，但核心决策应该是"买多少"。

当前所有评分≥60 的币都使用固定 10% 仓位，忽视了评分质量的差异。

**改进：根据 Expected Value 动态计算仓位**

#### 🆕 5.10.1 EV 触发仓位档位

```python
EV_THRESHOLDS = [
    (8,  1.0),   # EV > 8%  → 满仓（最大仓位）
    (6,  0.75),  # EV 6-8%  → 75% 仓位
    (4,  0.50),  # EV 4-6%  → 50% 仓位
    (2,  0.25),  # EV 2-4%  → 25% 仓位
    (0,  0),     # EV < 2%  → 不开仓
]
```

当前 Alpha Score 中的 EV（期望收益）评价正适合做此用途。

#### 🆕 5.10.2 后续升级方向

1. **Kelly Criterion（凯利公式）**：
   ```python
   f* = (p * b - q) / b
   # p=胜率, b=赔率(平均盈利/平均亏损), q=1-p
   ```
2. **Risk Parity（风险平价）**：
   - 根据各仓位历史波动率分配资金
   - 高波动币种分配更少资金，低波动币种分配更多
3. **CVaR 优化**：
   - 以条件风险价值为目标优化仓位分配

**预期效果：** 从"刚性的10%仓位"升级为"风报比驱动的动态仓位"

### 5.8 代币分类与阈值 (`strategies/token_profiles.json`)

#### 四类资产

| 类别 | 评分阈值 | 风险系数 | 持仓上限 | 说明 |
|------|---------|---------|---------|------|
| **蓝筹** | 75 | 0.7 | 25% | BTC/ETH/SOL/XRP 等，高确定性 |
| **基本面** | 55 | 0.9 | 15% | DeFi/L2/AI，有协议数据支撑 |
| **叙事/庄股** | 53 | 1.15 | 10% | 强庄/铭文/GameFi，有催化 |
| **Meme/超高风险** | 49 | 1.3 | 5% | 纯社区币，高频开仓主力 |

**⚠️ 当前缺陷**：叙事/庄股 1.1×、Meme 1.2× 权重补偿会导致垃圾币评分虚高。**改进方向：取消补偿，统一评分标准。**

#### 代币映射

约 120 个代币按上述四类预分类。完整列表见 `token_profiles.json`。

#### 动态阈值（auto-tune）

每 1h 回测后自动调整。**当前仅优化胜率，改进方向：优化 EV（期望收益）。**

---

## 6. API 层

### 6.1 FastAPI 应用 (`api/main.py`)

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/trading/status` | GET | 完整交易状态（余额、持仓、账户、PnL） |
| `/api/trading/positions` | GET | 当前持仓列表 |
| `/api/trading/balance` | GET | 账户余额 |
| `/api/trading/trades` | GET | 交易记录（支持分页、symbol 过滤） |
| `/api/trading/recent_trades` | GET | 最近 20 条交易（system源排前） |
| `/api/trading/closed` | GET | 已平仓订单统计 |
| `/api/trading/total_trades` | GET | 总交易数（按 symbol 分组） |
| `/api/trading/performance` | GET | 历史性能统计 |
| `/api/trading/positions_history` | GET | 持仓历史 |
| `/api/trading/scores` | GET | 最新评分（支持 category 过滤） |
| `/api/trading/score_history` | GET | 评分历史（symbol, limit 参数） |
| `/api/trading/backtest` | GET | 回测结果 |
| `/api/trading/backtest-review` | GET | 回测复盘 |
| `/api/trading/factor-analysis` | GET | 因子分析 |

### 6.2 API 状态响应格式

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

## 7. 数据库

### 7.1 SQLite 数据库

文件：`alphadog/alphadog.db`

WAL 模式。

### 7.2 数据表概览

| 表名 | 用途 | 关键列 |
|------|------|--------|
| `symbols` | 活跃交易对列表 | symbol, is_active, first_seen |
| `candles_1h` | 1 小时 K 线 | time, symbol, OHLCV, quote_vol |
| `candles_15m` | 15 分钟 K 线 | time, symbol, OHLCV, quote_vol |
| `futures_data` | 期货数据 | open_interest, funding_rate, mark_price |
| `onchain_flows` | 链上资金流 | cex_net_flow_usd/usd_14d/outflow_ratio |
| `alpha_scores` | 评分结果 | composite_score, composite_summary, raw_features |
| `trades` | 交易记录 | symbol, side, pnl, exit_reason, source |
| `backtest_results` | 回测结果 | grade, returns, win_12h/24h |
| `backtest_review` | 回测复盘 | review_json (JSON blob) |
| `positions_history` | 持仓快照 | time, symbol, unrealized_pnl, stop_loss |
| `factor_analysis` | 因子分析 | run_time, result |

### 7.3 trades 表特殊设计

数据库版本支持 `source` 字段：

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
    source TEXT DEFAULT 'system',    -- system / income_auto / historical_import
    income_id TEXT                    -- 币安 income API 的交易 ID
);
```

**三层数据架构**：
- **L0（权威层）**：币安 income API — 最终盈亏溯源依据
- **L1（缓存层）**：source='system' — 系统回调实时写入
- **L2（补录层）**：source='income_auto' — 从币安 API 自动补录

### 7.4 🔧 改进方案 — 新增 factor_performance 表

用于因子归因分析（详见第 9 章回测改进）：

```sql
CREATE TABLE factor_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL DEFAULT (datetime('now')),
    factor_name TEXT NOT NULL,        -- e.g. "RSI", "funding_rate", "chip"
    bucket TEXT NOT NULL,             -- e.g. "70-80", ">0.08%"
    samples INTEGER,                  -- 样本数
    win_rate REAL,                    -- 胜率
    avg_return REAL,                  -- 平均收益率
    avg_drawdown REAL,                -- 平均最大回撤
    ev REAL                           -- 期望收益
);
```

---

## 8. 交易记录方案

### 8.1 写入流程（当前）

```
开仓/平仓/减仓 → execution.py 调用 record_trade() → trades 表写入 (source='system')
                      ↓
对账机制 (每轮循环) → 对比 trades 表 vs 币安 income API
                      ↓
                    差异 > $1 → 警告日志
```

### 8.2 ⚠️ 当前关键问题：交易记录不准

**根本原因**：系统**自己估算 PnL** 而非使用币安 API 的 `REALIZED_PNL`。

特别是**部分减仓**时：

```python
# execution.py 当前做法（错误）：
pnl = pos["unrealized_pnl"] * pct  # 估算值

# 币安实际值（来自 income API）：
REALIZED_PNL = 2.34  # 真实值
```

这导致 `trades` 表 PnL 与币安对不上，胜率、回撤等统计数据全部不准。

### 8.3 已禁用功能

`import_trades.py` — 手动导入历史数据功能已被禁用，启动时直接报错退出。

### 8.4 对账机制（当前）

在 `runner.py` 交易循环末尾：

1. 查询 `trades` 表所有 `source IN ('system','income_auto')` 的总 PnL
2. 从币安 income API 拉取最近 1000 条 `REALIZED_PNL` 记录
3. 计算差值，`> $1` 记录警告日志

### 8.5 🔧 改进方案 — Trade Ledger 重构

#### 8.5.1 全新设计：币安 Income API 作为单⼀真相源

原则：**不再自己计算 PnL，全权依赖 Binance Income API**。

```python
# 每轮循环从 Binance 拉取 REALIZED_PNL
records = ex.fetch_income(income_type="REALIZED_PNL", limit=1000)
for r in records:
    if r["tradeId"] not in local_trade_ids:
        insert into trades (
            symbol, side, qty, price, pnl, realized_pnl,
            source='income_auto', trade_id=r['tradeId']
        )
```

#### 8.5.2 新增 orders 表（记录下单意图）

```sql
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT,          -- MARKET / LIMIT / STOP
    quantity REAL,
    price REAL,
    status TEXT,              -- pending / filled / cancelled
    reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

#### 8.5.3 新增 fills 表（记录真实成交）

```sql
CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    order_id INTEGER REFERENCES orders(id),
    side TEXT NOT NULL,
    quantity REAL,
    price REAL,
    realized_pnl REAL,        -- 来自 Binance
    fee REAL,
    fee_asset TEXT,
    trade_id TEXT,             -- Binance tradeId（唯一）
    created_at TEXT DEFAULT (datetime('now'))
);
```

**最终 trades 表改为由 fills 表聚合生成**，确保每条记录都可追溯到币安的 `tradeId`。

---

## 9. 🔧 改进方案 — 回测系统完整升级

### 9.1 当前回测缺陷

1. **只调阈值，不调因子** — 没有因子归因分析，无法发现哪些因子在亏钱
2. **只优化胜率** — 应优化 EV（期望收益）和 Sharpe 比率
3. **没有交易模拟** — 当前只是评分后看价格变动，没有模拟止损/止盈/减仓

### 9.2 改进：增加因子归因分析

#### 9.2.1 自动统计每个因子的各区间表现

| 因子 | Bucket | 样本数 | 胜率 | 平均收益 | EV |
|------|--------|-------|------|---------|-----|
| RSI | 70-80 | 132 | 41% | -2.8% | -1.65 |
| RSI | 50-60 | 214 | 67% | +5.4% | +3.62 |
| 资金费率 | >0.08% | 98 | 33% | -7.0% | -4.69 |
| 筹码吸筹 | — | 187 | 71% | +9.2% | +6.53 |
| 筹码派发 | — | 203 | 29% | -4.1% | -2.91 |

#### 9.2.2 自动权重调整（因子 IC）

```python
# 每 24h 重新计算：
for factor in all_factors:
    ic = correlation(factor_score, future_return)
    if ic > 0.05:
        factor_weights[factor] *= 1.1  # 表现好 → 加权
    elif ic < -0.05:
        factor_weights[factor] *= 0.5  # 表现差 → 降权
    else:
        pass  # 无预测力，不变
```

形成真正的**自学习闭环**

#### 🔧 9.2.3 改进：因子稳定性评估（防止过拟合）

**问题：** 当前自动权重调整仅看短期 IC，存在严重过拟合风险。

**典型案例：**
```
过去 24h Meme 币集体上涨 → RSI 因子 IC = 0.12（正相关）
→ 系统自动提升 RSI 权重
→ 第二天市场切换，RSI 失效
→ 系统开始连续亏损
```

**改进：引入信息比率 IC_IR 作为调整前提条件**

```python
IC_IR = IC_Mean / IC_Std    # 信息比率

调整条件：
  ✓ 样本数 ≥ 500
  ✓ 时间跨度 ≥ 14 天
  ✓ IC_IR ≥ 0.3
  
只有同时满足上述条件，才允许调整因子权重
```

**为什么 IC_IR > 0.3？**
- IC_Mean 衡量因子预测力的平均值（方向）
- IC_Std 衡量预测力的波动（噪音）
- IC_IR 衡量"信噪比"——信号有多稳定
- IC_IR < 0.3 说明信号几乎被噪音淹没，调整权重只会放大噪音的影响

**实现方式：**

在 `factor_performance` 表中增加累积统计字段：

```sql
ALTER TABLE factor_performance ADD COLUMN ic_mean REAL;
ALTER TABLE factor_performance ADD COLUMN ic_std REAL;
ALTER TABLE factor_performance ADD COLUMN ic_ir REAL;
```

`adjust_weights_24h()` 函数在调整前首先检查 IC_IR 条件：

```python
def can_adjust_weight(factor_name):
    """检查因子是否满足调整条件"""
    stats = get_factor_stats(factor_name)
    if stats["samples"] < 500:
        return False
    if stats["days_span"] < 14:
        return False
    if stats["ic_ir"] < 0.3:
        return False
    return True
```

**预期效果：** 防止短期行情导致的模型失真，仅对具有稳定预测力的因子做权重调整。

### 9.3 改进：回测增加真实交易模拟

当前回测只是"评分后观察价格变动"，应该模拟完整交易流程：

```
score >= 阈值
  ↓
以当前价格开仓
  ↓
追踪价格，模拟：
  ├─ 止损触发 → 记录亏损退出
  ├─ TP1 触发 → 减仓 25%
  ├─ TP2 触发 → 再减 25%
  ├─ 移动止盈 → 全平
  ├─ 弱信号退出 → 全平/减仓
  └─ 时间止损 → 退出
  ↓
记录真实交易路径
```

### 9.4 Auto-Tune 优化目标升级

```python
fitness = (
    0.35 * expectancy   # 期望收益
    + 0.25 * sharpe      # 夏普比率
    + 0.20 * profit_factor # 盈利因子
    + 0.10 * win_rate    # 胜率（权重最低）
    - 0.10 * max_drawdown # 惩罚大回撤
)
```

**自动调优范围**：
- 因子权重（`factor_weights.json`）
- 开仓阈值（`token_profiles.json`）
- 止损倍数（`atr_multiplier_stop`）
- TP 倍数（`atr_multiplier_take_profit`）

### 🆕 9.5 改进方案 — 真实交易成本模型

**问题：** 当前回测未包含交易成本，导致回测收益被系统性高估。

真实交易包含以下成本：
```
理论盈利：+5%
手续费：  -0.04%
滑点：    -0.1% ~ -0.5%（Meme 币更严重）
资金费率：-0.01%/h（持仓期间累计）
流动性冲击：-0.05% ~ -0.3%（大额订单）
────────────────
实际盈利：+1% 甚至亏损 ❌
```

#### 🆕 9.5.1 新增 fee_model（手续费模型）

根据 Binance VIP 等级和 Maker/Taker 身份动态计算：

```python
def estimate_fees(symbol, qty, price, side, is_maker):
    # 默认 VIP0：Maker 0.02%, Taker 0.04%
    maker_fee = 0.0002
    taker_fee = 0.0004
    
    fee_rate = maker_fee if is_maker else taker_fee
    notional = qty * price
    return notional * fee_rate
```

#### 🆕 9.5.2 新增 slippage_model（滑点模型）

根据代币流动性模拟滑点：

```python
def estimate_slippage(symbol, qty, price):
    # 蓝筹（BTC/ETH）：滑点 0.05%
    # 基本面（UNI/AAVE）：滑点 0.1%
    # 叙事（ORDI/ID）：滑点 0.3%
    # Meme：滑点 0.5%
    liquidity_tiers = {
        "蓝筹": 0.0005,
        "基本面": 0.001,
        "叙事/庄股": 0.003,
        "Meme/超高风险": 0.005,
    }
    category = get_category(symbol)
    slippage_rate = liquidity_tiers.get(category, 0.002)
    return qty * price * slippage_rate
```

#### 🆕 9.5.3 新增 funding_model（资金费率模型）

持仓期间实时扣减累计资金费率：

```python
def estimate_funding_cost(symbol, entry_time, hold_hours):
    # 资金费率每 8 小时结算一次
    # 回测时需读取历史资金费率数据
    funding_rate_history = get_funding_rate(symbol, entry_time, hold_hours)
    total_cost = 0
    for rate in funding_rate_history:
        total_cost += position_value * rate * hold_duration
    return total_cost
```

#### 🆕 9.5.4 最终回测收益必须是 Net Profit

```python
net_profit = gross_profit - fees - slippage - funding_costs
```

**重要原则：** 所有回测报告只展示 Net Profit，Gross Profit 仅作为中间统计字段。

### 🆕 9.6 改进方案 — 修复回测幸存者偏差

**问题：** 当前回测仅使用当前仍活跃的交易对进行分析，导致严重的幸存者偏差：

```
已退市币种   ❌ 被排除
归零币种     ❌ 被排除
低流动性币种 ❌ 被排除
───────────────────────
回测收益被系统性高估 ✅（因为亏光的币不在样本中）
```

#### 🆕 9.6.1 新增 symbol_snapshots 表

每日记录每个交易对的状态快照：

```sql
CREATE TABLE symbol_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT,           -- TRADING / BREAK / HALT
    quote_volume REAL,     -- 24h 成交量
    price_change_24h REAL,
    active BOOLEAN DEFAULT 1,
    UNIQUE(date, symbol)
);
```

#### 🆕 9.6.2 回测时读取历史快照

```python
def compute_backtest_v2(df_scores, df_prices, as_of_date):
    """按历史时间点回测，只使用当时存在的交易对"""
    conn = get_conn()
    # 读取 as_of_date 当时的活跃币列表
    active = conn.execute(
        "SELECT symbol FROM symbol_snapshots WHERE date = ? AND active = 1",
        (as_of_date.date(),)
    ).fetchall()
    active_set = {r["symbol"] for r in active}
    conn.close()
    
    # 只回测当时活跃的币
    df = df_scores[df_scores["symbol"].isin(active_set)]
    # 继续原有回测逻辑...
```

**预期效果：** 2025年5月的回测只使用当时存在的币种，
避免因幸存者偏差高估历史收益。

---

## 10. 🔧 改进优先级与开发建议

### 10.1 P0 — 立即修复

| 序号 | 改进项 | 说明 | 预期效果 |
|------|--------|------|---------|
| 1 | 持仓接口改 `positionRisk` | 修复零仓位误判、Hedge Mode、mark_price 除零 | 持仓数据准确 |
| 2 | 交易记录改 Income API 权威源 | 停止自己估算 PnL | 历史交易/胜率/PnL 全部准确 |
| 3 | 取消 Meme/庄股权重补偿 | 统一评分标准 | 减少垃圾币入场 |
| 4 | 增加连续 2 轮评分确认 | 减少噪音信号 | 提升开仓质量 |
| 5 | 增加时间止损（12h） | 避免死拿垃圾单 | 改善资金效率 |

### 10.2 P1 — 下一阶段

| 序号 | 改进项 | 说明 |
|------|--------|------|
| 6 | 因子归因分析（`factor_performance` 表） | 自动发现哪些因子在亏钱 |
| 7 | EV 评价体系 | 从纯胜率改为期望收益驱动 |
| 8 | 因子自动调权（IC-based） | 根据实际预测力动态调整权重 |
| 9 | 市场环境因子（BTC Regime） | 增加整体市场方向判断 |

### 10.3 P2 — 核心升级

| 序号 | 改进项 | 说明 |
|------|--------|------|
| 10 | Alpha Score 替代当前评分 | 从"评价状态"改为"预测未来收益" |
| 11 | 完整交易级回测 | 模拟止损/止盈/减仓全过程 |
| 12 | 自学习闭环 | 因子权重、阈值、止损参数全自动化 |
| 13 | Expectancy 驱动 Auto-Tune | 以 EV/Sharpe/Profit Factor 为优化目标 |

### 10.4 投产比排序（推荐顺序）

建议按以下顺序交付：

```
① 修复交易记录和持仓数据  ← 快速修复，解决核心数据问题
② 做因子归因分析          ← 一个月内可出结果
③ 取消 Meme 加权          ← 一行代码
④ 引入 EV                 ← 改造回测核心逻辑
⑤ Alpha Score 重构        ← 全评分体系升级
```

这五项通常能解决 **70% 以上"高分开仓却大亏"的问题**。

---

## 11. 部署与监控

### 11.1 Supervisor 管理

配置文件：`supervisord.conf`

| 服务 | 命令 | 端口 | 日志 |
|------|------|------|------|
| pipeline | `python3 pipeline/main.py` | — | `/tmp/alphadog_pipeline.log` |
| engine | `python3 engine/run.py` | — | `/tmp/alphadog_engine.log` |
| trading | `python3 trader/runner.py` | — | `/tmp/alphadog_trading.log` |
| api | `uvicorn api.main:app --port 8000` | :8000 | `/tmp/alphadog_api.log` |
| frontend | `npx vite --port 3000` | :3000 | `/tmp/alphadog_frontend.log` |

所有服务通过 Supervisor Unix Socket 管理：

```bash
supervisorctl -s unix:///tmp/alphadog_supervisor.sock status
supervisorctl -s unix:///tmp/alphadog_supervisor.sock restart all
```

### 11.2 监控指标

- **余额变化**：`wallet_balance` 趋势
- **持仓数量**：当前持仓数 / 最大持仓数
- **对账差异**：trades 表 vs Income API 差值
- **评分分布**：各等级评分数量
- **最近错误**：各进程 stderr 日志

Pro 进程管理

配置参考 `supervisord.conf`。

---

## 12. 🆕 自动复盘系统

### 12.1 现状问题

当前系统已具备基础统计能力（胜率、收益率、因子表现），
但仍然缺乏**真正的自动复盘能力**——系统不懂"为什么亏钱"和"什么在赚钱"。

### 12.2 设计目标

每天自动生成一份 `review_report`，内容包括：

1. 最佳交易 TOP 10（按 EV 排序）
2. 最差交易 TOP 10（按亏损额排序）
3. 最大亏损原因分析（归因到具体因子）
4. 最佳盈利因子（哪些因子最近有效）
5. 当前市场状态（牛/熊/横盘/恐慌）
6. 本周策略建议（调整建议）

### 12.3 自动分析示例

**场景 1：最近 50 笔亏损交易分析**

```
分析结果：最近 50 笔亏损交易中
  - 68% 发生于 Funding Rate > 0.05%
  - 52% 发生于 RSI > 70
  - 41% 同时触发两个条件

建议：
  ✓ 当 Funding Rate > 0.05% 时，仓位减半
  ✓ 降低资金费率因子的评分权重 50%
```

**场景 2：最近 30 笔盈利交易分析**

```
分析结果：最近 30 笔盈利交易中
  - 74% 同时满足：OI增长 + 吸筹增强 + Funding低位
  - 平均 EV = +6.8%
  - 平均持仓时间 = 8.3h

建议：
  ✓ 提高 OI/吸筹/Funding 组合因子的权重 20%
  ✓ 优先选择满足此组合条件的开仓信号
```

### 12.4 核心实现逻辑

#### 🆕 12.4.1 data_collection 模块

```python
def build_review_report(conn, days_back=7):
    report = {
        "generated_at": datetime.now().isoformat(),
        "period_days": days_back,
        "top_trades": analyze_top_trades(conn, days_back, top_n=10),
        "worst_trades": analyze_worst_trades(conn, days_back, top_n=10),
        "loss_attribution": attribute_losses(conn, days_back),
        "win_attribution": attribute_wins(conn, days_back),
        "best_factor": find_best_factor(conn, days_back),
        "market_regime": get_market_regime(conn),
        "recommendations": generate_recommendations(conn, days_back),
    }
    return report
```

#### 🆕 12.4.2 loss_attribution 模块

每个亏损交易关联当时评分时的特征快照，统计各因子分桶下的亏损集中度：

```python
def attribute_losses(conn, days):
    losses = conn.execute("""
        SELECT t.symbol, t.pnl, s.raw_features
        FROM trades t
        JOIN alpha_scores s ON s.symbol = t.symbol
            AND s.time = (SELECT MAX(time) FROM alpha_scores 
                          WHERE symbol = t.symbol AND time <= t.entry_time)
        WHERE t.pnl < 0 AND t.exit_time > datetime('now', ?)
        LIMIT 100
    """, (f'-{days} days',)).fetchall()
    return aggregate_by_factor(losses)
```

**输出示例：**
```
{
  "funding_rate_high": {
    "count": 34, "total_loss": -125.6, "avg_loss": -3.69,
    "pct_of_all_losses": 0.68
  },
  "rsi_overbought": {
    "count": 26, "total_loss": -89.3, "avg_loss": -3.43,
    "pct_of_all_losses": 0.52
  }
}
```

#### 🆕 12.4.3 recommendation 模块

```python
def generate_recommendations(conn, days):
    recs = []
    # 检查亏损归因
    for factor, stats in attribute_losses(conn, days).items():
        if stats["pct_of_all_losses"] > 0.3:
            recs.append({
                "action": "reduce_weight",
                "target": factor,
                "severity": "high",
                "reason": f"{factor} 占亏损 {stats['pct_of_all_losses']:.0%}",
            })
    # 检查盈利归因
    for factor, stats in attribute_wins(conn, days).items():
        if stats["pct_of_all_wins"] > 0.3:
            recs.append({
                "action": "increase_weight",
                "target": factor,
                "severity": "medium",
                "reason": f"{factor} 占盈利 {stats['pct_of_all_wins']:.0%}",
            })
    return recs
```

### 12.5 报告格式示例

```json
{
  "generated_at": "2026-06-26T08:00:00Z",
  "summary": {
    "total_trades": 47,
    "win_rate": 55.3,
    "total_pnl": 234.50,
    "current_drawdown": -3.2,
    "market_regime": "Bull Market"
  },
  "top_trades": [
    {"symbol": "SOLUSDT", "pnl": 45.20, "ev": 12.3, "reason": "吸筹+趋势"},
    {"symbol": "LINKUSDT", "pnl": 32.10, "ev": 9.8, "reason": "OI增长+Funding低"}
  ],
  "worst_trades": [
    {"symbol": "DOGEUSDT", "pnl": -28.50, "ev": -5.2, "reason": "Funding>0.1%"},
    {"symbol": "PEPEUSDT", "pnl": -22.30, "ev": -4.8, "reason": "RSI>80+追高"}
  ],
  "recommendations": [
    {
      "action": "reduce_weight",
      "target": "funding_rate",
      "severity": "high",
      "reason": "68%亏损发生于Funding>0.05%"
    },
    {
      "action": "increase_weight",
      "target": "oi_chip_funding_combo",
      "severity": "medium",
      "reason": "74%盈利满足三重条件"
    }
  ]
}
```

### 12.6 预期效果

系统具备**自动研究员**能力，而不仅仅是统计工具：

- 每天自动发现"什么在亏钱，什么在赚钱"
- 自动生成可操作的建议（提权/降权/加条件）
- 结合因子归因分析（§9.2），形成完整的"**分析 → 归因 → 建议 → 调整**"闭环
---

## V3.0 升级（2026-06-27）

### §5.11 Expected Value（EV）+ Risk Reward（R:R）双重开仓过滤（最高优先级）

**背景**：当前系统的开仓逻辑主要依赖 Alpha Score、连续两轮评分确认、市场状态和硬过滤条件，但仍然缺少一个最关键的判断——这笔交易值不值得做。一个高评分信号并不意味着具有足够高的收益空间，例如某个币种虽然评分达到 80 分，但距离前方压力位仅剩 2%，而止损距离却达到 6%，这种交易即使胜率较高，长期期望收益依然可能为负。

**实现**：
1. 系统应首先计算预期收益空间（最近压力位、历史高点或模型预测目标价）以及风险空间（ATR 止损或结构止损），计算 Reward/Risk 比例
2. 仅允许 R:R≥2 或 EV>0 的交易进入执行流程
3. 未来引入 Alpha Score 模型后，可以直接采用公式：EV = P(win) × AvgWin − P(loss) × AvgLoss
4. 只有 EV 为正且达到设定阈值时才允许开仓
5. 从根本上避免大量高评分但盈亏比极差的交易

---

### §5.12 交易冷却机制（Trade Cooldown）

**背景**：目前系统止损后，如果评分再次满足条件，会立即重新开仓，这在震荡行情中极易造成连续止损。例如某个币种连续横盘震荡，系统可能在几个小时内连续止损三四次，大量消耗手续费和资金。

**实现**：
1. 当同一币种发生止损退出后，系统应进入一定时间的冷却状态，例如 6~24 小时内禁止再次交易该币种
2. 如果连续两次止损，则自动延长冷却时间甚至暂停交易一天
3. 同时可增加每日最大亏损次数限制，当某币种或整个账户当天累计止损达到设定次数后，自动停止继续开仓
4. 避免连续犯错

---

### §5.13 Breakout Confirmation（突破确认）

**背景**：目前评分达到标准即可直接买入，但很多高分实际上发生在突破失败之前。

**实现**：
1. 增加突破确认模块，将交易从"预测突破"改为"确认突破"
2. 要求价格突破最近 20 根 K 线高点，同时成交量放大至过去 20 根均量的 1.5 倍以上
3. 再配合 Alpha Score 达标才允许开仓
4. 如果只是评分很高但始终没有突破关键位置，则继续等待
5. 这样能够过滤大量假突破和诱多行情，大幅降低追高买在顶部的问题

---

### §4.7 Entry Alpha 与 Hold Alpha 分离

**背景**：目前系统使用同一个 Alpha Score 同时决定是否开仓以及是否继续持仓，这实际上属于两个完全不同的问题。一个币种可能已经上涨 30%，此时已经不适合买入，但对于已经持有的人来说却依然应该继续持有。

**实现**：
1. 拆分为两个独立模型：Entry Alpha（是否值得开仓）和 Hold Alpha（是否值得继续持有）
2. Entry Alpha 主要关注未来收益空间、突破质量、市场环境等因素
3. Hold Alpha 更关注趋势是否结束、资金是否流出、动量是否衰减等因素
4. 避免"不能买"和"必须卖"混为一谈，大幅提升持仓管理质量

---

### §5.3 Score Decay（评分衰减机制）

**背景**：目前平仓主要依赖评分低于 30 分、方向反转、筹码派发等多个条件组合判断，规则较为僵硬。

**实现**：
1. 每次开仓时记录 Entry Score，例如 82 分
2. 随后持续计算当前评分与开仓评分之间的差值
3. 当 Alpha Score 持续下降时，说明这笔交易的优势正在逐渐消失
4. 可以设定例如衰减 20 分减仓 25%，衰减 30 分减仓 50%，衰减 40 分全部退出
5. 相比固定低于 30 分才退出，这种方式更加符合 Alpha 信号逐渐失效的实际过程
6. 也更容易与未来 ML 模型结合

---

### §5.4 ATR 动态止盈替代固定百分比止盈

**背景**：目前 TP1、TP2 使用固定盈利比例（12%、20%），不同币种波动率差异极大，这种固定百分比实际上并不合理。

**实现**：
1. 统一采用 ATR 动态止盈
2. TP1 为 2×ATR、TP2 为 4×ATR、TP3 为 6×ATR
3. BTC 和 Meme 币虽然价格波动不同，但 ATR 已经包含了市场波动信息
4. 所有币种都可以采用统一逻辑
5. 移动止盈也改为 ATR 回撤，例如盈利达到 TP3 后，当价格回撤超过 2×ATR 自动平仓
6. 使整个止盈体系完全自适应市场波动

---

### §5.14 Trade Quality Engine（交易质量评分）

**背景**：目前 Alpha Score 只评价币种本身，而没有评价整笔交易的质量。

**实现**：
1. 新增 Trade Quality Engine，对每一笔交易进行综合打分
2. 综合 Alpha Score、Risk Reward、市场状态、流动性、组合相关性、成交量质量等因素，形成最终 Trade Quality Score
3. 只有当交易质量达到 80 分以上时才允许使用标准仓位
4. 60~80 分采用半仓
5. 低于 60 分直接放弃交易
6. 即使 Alpha Score 很高，但如果市场环境不好或盈亏比不佳，依然能够降低风险

---

### §5.15 真实订单簿（Order Book）过滤

**背景**：目前所有决策均基于 K 线数据，但实际成交质量与订单簿关系密切。

**实现**：
1. 增加 Binance Depth 数据分析
2. 计算买卖盘深度比例、大额挂单位置、盘口失衡程度等指标
3. 当盘口卖压明显大于买盘、或者上方存在大量压单时，即使 Alpha Score 很高，也可以降低评分甚至禁止开仓
4. 相反，当盘口出现明显买盘支撑时，可以适当提高交易质量评分
5. 这部分数据对于短中周期策略提升非常明显

---

### §9.7 升级回测为事件驱动回测（Event Driven Backtest）

**背景**：当前回测主要基于 K 线 OHLC 数据判断止盈止损，但无法确定同一根 K 线中究竟是先止盈还是先止损，因此收益通常会被高估。

**实现**：
1. 将回测升级为事件驱动模型
2. 在评分产生后模拟真实订单执行过程，包括下单、成交、滑点、止损、止盈、部分减仓、移动止盈、时间止损等完整流程
3. 甚至可以采用 1 分钟数据重放（Replay）提高准确性
4. 只有交易路径完全模拟真实执行，回测结果才具有参考价值

---

### §9.8 Walk Forward Validation 与 Out-of-Sample Testing

**背景**：目前系统虽然具有自动调参功能，但仍然属于全样本优化，很容易产生过拟合。

**实现**：
1. 增加 Walk Forward 回测
2. 每次仅使用历史一段数据训练模型，再使用未来一段数据进行测试
3. 然后不断向前滚动
4. 保留最终一部分完全未参与训练的数据作为 Out-of-Sample 测试集
5. 任何参数调整都不能接触这部分数据
6. 只有最终测试集依然保持稳定盈利，才能认为模型具有真正的泛化能力

---

### §9.9 Monte Carlo 风险分析

**背景**：传统回测只会得到一条资金曲线，但实际交易顺序可能完全不同。

**实现**：
1. 增加 Monte Carlo Simulation
2. 将历史交易随机重排数千次
3. 重新计算资金曲线、最大回撤、连续亏损次数等指标
4. 从而得到策略最坏情况下可能面临的风险
5. 例如虽然历史收益率达到 80%，但 Monte Carlo 可能发现最坏情况下最大回撤达到 45%
6. 这对于仓位管理和风险控制具有极高参考价值

---

### §5.16 Exit Optimizer（自动退出优化器）

**背景**：建议新增 Exit Optimizer，对每一笔已完成交易进行自动复盘。

**实现**：
1. 系统不仅记录实际退出收益，还应模拟"如果晚退出 1 小时"、"如果继续持有到 TP3"、"如果提前止盈"等多种���出��式
2. 对比各种退出方案的收益差异
3. 统计系统究竟是经常"卖飞"，还是经常"拿太久"
4. 经过几百甚至几千笔交易后，系统便能够自动发现当前平仓规则存在的问题
5. 并反向优化止盈、止损和移动止盈参数
6. 实现真正的数据驱动交易优化

