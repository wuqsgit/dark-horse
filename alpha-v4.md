# AlphaDog V4.0 完整规格文档

> 版本：4.0  
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
| 最小评分 | 60 |
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
│   ├── execution.py   # 执行引擎(决策+下单) V4.0
│   ├── exchange.py    # Binance API封装
│   ├── risk.py       # 风控模块 V4.0
│   ├── config.py    # 配置常量 V4.0
│   ├── models.py   # 数据模型
│   └── cooldown_manager.py # 冷却管理
├── strategies/
│   └── token_profiles.json # 代币分类+阈值
├── api/
│   └── main.py        # FastAPI应用 V4.0
├── shared/
│   └── db.py         # 数据库操作
├── frontend/         # Vite+React前端
├── supervisord.conf  # Supervisor配置
└── alphadog.db      # SQLite数据库
```

---

## 3 数据管道 Pipeline

### 3.1 Binance数据采集器

**文件**：pipeline/binance_http.py  
**类**：BinanceHTTPCollector

使用 httpx.AsyncClient 直连币安API，并发限制 asyncio.Semaphore(5)，批量 asyncio.gather 每批10个。

#### 采集功能

| 功能 | API 端点 | 说明 |
|------|---------|------|
| 获取交易对列表 | /fapi/v1/ticker/24hr | 获取所有USDT交易对，按24h成交量排序取前200 |
| 1h K线 | /api/v3/klines?interval=1h&limit=48 | 最近48根（约2天） |
| 15m K线 | /api/v3/klines?interval=15m&limit=48 | 最近48根（约12小时） |
| 期货数据 | /fapi/v1/premiumIndex + /fapi/v1/openInterest | 资金费率+持仓量 |
| 更新活跃币列表 | upsert_symbol(pair) | 写入symbols表 |

### 3.2 Dune链上采集器

**文件**：pipeline/dune_collector.py  
**类**：DuneCollector

每30min采集，存入 onchain_flows 表。

**采集字段**：
- cex_net_flow_usd：24h净流量(USD)
- cex_net_flow_14d_usd：14天净流量(USD)
- cex_net_outflow_ratio：流出比���

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

#### 4.2.3 评分等级

| 等级 | 评分 | 含义 |
|------|------|------|
| S1 | ≥80 | 极强，优先开仓 |
| S2 | 70-80 | 强信号 |
| A1 | 60-70 | 中等偏强 |
| A2 | 55-60 | 中等 |
| B | 45-55 | 中性 |
| C | 30-45 | 弱，不开仓 |
| D | <30 | 极弱，平仓 |

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
    "raw_features": {...},
    "scan_id": "scan_20260629_0800"
}
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
4. engine.decide()       → 四象限决策
5. engine.execute()     → 执行操作
6. sleep(300)
```

### 5.2 执行引擎 V4.0 - 四象限退出逻辑

**文件**：trader/execution.py  
**类**：ExecutionEngine

#### 四象限逻辑（V4.0核心改动）

```
盈利 ≥ 0:
├── 趋势完好(uptrend) + 吸筹(accumulation/reaccumulation)
│   ├── 盈利≥5% → TP1平50%
│   ├── 盈利≥10% → TP2平剩余50%
│   └── 否则 → 持有
└── 趋势破坏 → 止盈离场

盈利 < 0:
├── 触及5%止损 → 硬止损全平
└── 未触及止损 → 持有（信任系统）
    └── 检查移动止盈(ATR×1.5回撤) → 全平
```

#### 删除的旧逻辑

- 弱信号5项检查
- 弱减50%/25%
- 分批止盈状态机（tp1_done/tp2_done/trailing_active）
- 高位拒绝

### 5.3 持仓跟踪 V4.0

```python
# 简化的持仓跟踪
_pos_tracker = {
    "BTCUSDT": {
        "highest_price": 65200.0,   # 持仓期间最高价
        "entry_price": 65000.0,     # 入场价
    }
}
```

### 5.4 冷却机制

**文件**：trader/cooldown_manager.py

| 场景 | 冷却时间 |
|------|---------|
| 单次��损后 | 6小时 |
| 连续2次止损 | 24小时 |
| 开仓后 | 30分钟 |

**检查函数**：
```python
is_in_cooldown(symbol) → (bool, reason, seconds_remaining)
record_stop(symbol, pnl) → 记录止损+冷却
record_profit(symbol) → 重置冷却
```

---

## 6 风控系统 V4.0

### 6.1 仓位计算

**文件**：trader/risk.py  
**函数**：calculate_position()

```python
def calculate_position(exchange, symbol, price, balance, score=None):
    # V4.0: 固定仓位20% + 固定3x杠杆
    margin = balance * 0.20
    leverage = 3  # 固定3x
    
    # ATR from 4H klines, 14周期
    atr = exchange.get_atr(symbol)
    
    # V4.0: 止损 = min(ATR×1.5, 5%上限) - 取较小值
    stop_distance_atr = atr * 1.5
    stop_distance_5pct = price * 0.05
    stop_distance = min(stop_distance_atr, stop_distance_5pct)
    
    # 止盈距离
    tp1_distance = atr * 2
    tp2_distance = atr * 4
    
    position_value = margin * leverage
    quantity = position_value / price
    
    return {
        "quantity": round(quantity, 3),
        "stop_loss": round(stop_distance, 8),
        "take_profit": round(tp1_distance, 8),
        "atr_value": atr,
        "leverage": leverage,
    }
```

### 6.2 分批止盈 V4.0

**文件**：trader/risk.py  
**函数**：calc_tp_levels()

```python
def calc_tp_levels(entry_price, side, atr_value):
    # V4.0: 简化版
    # TP1: 盈利5%，平50%
    tp1 = entry_price + entry_price * 0.05 if side == "LONG" else entry_price - entry_price * 0.05
    # TP2: 盈利10%，平剩余50%
    tp2 = entry_price + entry_price * 0.10 if side == "LONG" else entry_price - entry_price * 0.10
    
    return {
        "tp1_price": round(tp1, 8),
        "tp2_price": round(tp2, 8),
        "tp1_qty_pct": 0.50,   # 平50%
        "tp2_qty_pct": 0.50,   # 平剩余50%
        "trail_trigger_atr": 1.5,  # 回撤ATR×1.5触发全平
    }
```

### 6.3 移动止盈 V4.0

**文件**：trader/risk.py  
**函数**：calc_trailing_stop()

```python
def calc_trailing_stop(current_price, highest_price, atr_value, trail_trigger_atr=1.5):
    """V4.0: 从最高点回撤 ATR×1.5 价格绝对值触发全平
    
    Args:
        current_price: 当前价格
        highest_price: 持仓期间最高价格
        atr_value: ATR值
        trail_trigger_atr: ATR倍数，默认1.5
    
    Returns: True if should close
    
    Example:
        最高价: 65,000
        ATR: 800
        ATR×1.5: 1,200
        触发价: 65,000 - 1,200 = 63,800
    """
    if highest_price <= 0:
        return False
    drawdown_price = highest_price - current_price
    trigger_price = atr_value * trail_trigger_atr
    return drawdown_price >= trigger_price
```

### 6.4 硬过滤 V4.0

**文件**：trader/risk.py  
**函数**：meets_hard_filters()

**V4.0改动**：删除"价格高位"硬过滤，仅保留overbought拒绝

| 检查项 | 规则 |
|--------|------|
| 评分 | ≥min_score |
| 价格位置 | ≠overbought |
| 波动率 | ≤max_volatility_level |
| 成交量 | ≥min_volume_usdt |
| 资金费率 | ≤max_funding_rate |

---

## 7 配置常量 V4.0

### 7.1 交易参数

**文件**：trader/config.py

```python
TRADING_CONFIG = {
    # ── 资金管理 ──
    "total_capital": 5000,
    "position_size_pct": 0.20,          # 每仓20%
    "risk_per_trade_pct": 0.015,        # 每仓风险预算1.5%（V4.0改）
    "max_positions": 3,
    
    # ── V4.0 核心改动 ──
    "leverage_max": 3,                 # 固定3x（V4.0改）
    
    # ── 评分阈值 ──
    "min_score": 60,
    "consecutive_scores_required": 2,
    
    # ── V4.0 分批止盈 ──
    "tp1_pct": 0.50,                  # V4.0: 平50%
    "tp2_pct": 0.50,                  # V4.0: 平剩余50%
    "tp1_target_pct": 0.05,            # V4.0: 5%
    "tp2_target_pct": 0.10,           # V4.0: 10%
    "trailing_stop_atr_multiplier": 1.5, # V4.0: ATR×1.5
    
    # ── V4.0 止损 ──
    "hard_stop_pct": 0.05,             # V4.0: 5%
    
    # ── 调度 ──
    "rebalance_interval_min": 5,
}
```

### 7.2 硬过滤

```python
HARD_FILTERS = {
    "min_volume_usdt": 1_000_000,
    "max_volatility_level": "正常",
    "disallowed_price_positions": ["overbought"],  # V4.0: 仅overbought
    "max_funding_rate": 0.001,
}
```

---

## 8 API层 V4.0

### 8.1 FastAPI端点

**文件**：api/main.py

| 端点 | 方法 | 描述 |
|------|------|------|
| /api/trading/status | GET | 完整状态 |
| /api/trading/positions | GET | 持仓列表 |
| /api/trading/balance | GET | 余额 |
| /api/trading/trades | GET | **每币种最新1笔**（V4.0改） |
| /api/trading/history | GET | **每币种最新1笔**（V4.0改） |
| /api/trading/closed | GET | 已平仓统计 |
| /api/trading/performance | GET | 历史性能 |
| /api/trading/scores | GET | 最新评分 |
| /api/trading/score_history | GET | 评分历史 |
| /api/trading/backtest | GET | 回测结果 |
| /api/trading/factor-analysis | GET | 因子分析 |

### 8.2 V4.0 API改动

**每币种只展示最新1笔**：

```python
# V4.0: 每个币种只取最新一笔
recent_trades = conn.execute(
    """SELECT t.* FROM trades t
    INNER JOIN (
        SELECT symbol, MAX(created_at) as max_created
        FROM trades
        WHERE exit_reason NOT IN ('historical_import','历史补录(手动平仓)')
        GROUP BY symbol
    ) tm ON t.symbol = tm.symbol AND t.created_at = tm.max_created
    ORDER BY t.created_at DESC LIMIT 100"""
).fetchall()
```

---

## 9 数据库

### 9.1 表结构

**数据库**：alphadog.db（WAL模式）

| 表名 | 用途 |
|------|------|
| symbols | 活跃交易对 |
| candles_1h | 1h K线 |
| candles_15m | 15m K线 |
| futures_data | 期货数据 |
| onchain_flows | 链上资金流 |
| alpha_scores | 评分结果 |
| trades | 交易记录 |
| backtest_results | 回测结果 |
| backtest_review | 回测复盘 |
| positions_history | 持仓快照 |
| factor_analysis | 因子分析 |
| trade_cooldown | 冷却追踪（V3.0+） |

### 9.2 trade_cooldown表

```sql
CREATE TABLE trade_cooldown (
    symbol TEXT PRIMARY KEY,
    last_stop_time TEXT,
    stop_count_24h INTEGER DEFAULT 0,
    consecutive_stops INTEGER DEFAULT 0,
    cooldown_until TEXT,
    reason TEXT,
    updated_at TEXT
);
```

---

## 10 部署

### 10.1 Supervisor配置

**文件**：supervisord.conf

```
[program:pipeline]
command=python3 pipeline/main.py

[program:engine]
command=python3 engine/run.py

[program:trading]
command=python3 trader/runner.py

[program:api]
command=uvicorn api.main:app --port 8000

[program:frontend]
command=npx vite --port 3000
```

---

## 11 V4.0 改动对照表

| 项目 | V3.0旧值 | V4.0新值 |
|------|----------|----------|
| **止损** | -12% | 5%（ATR×1.5取小） |
| **风险预算** | 5% | 1.5% |
| **杠杆** | 动态 | 固定3x |
| **TP1触发** | 12% | 5% |
| **TP1减仓** | 25% | 50% |
| **TP2触发** | 20% | 10% |
| **TP2减仓** | 25% | 50%（剩余） |
| **移动止盈** | 回撤12% | ATR×1.5价格回撤 |
| **高位拒绝** | 拒绝 | 删除 |
| **退出逻辑** | 5种条件 | 四象限简化 |
| **API显示** | 多笔 | 每币1笔 |

---

## 12 盈亏比改进效果

| 指标 | V3.0 | V4.0（预期） |
|------|------|------------|
| 单笔最大亏损 | 12% ($600) | 5% ($250) |
| 盈亏比 | 0.4:1 | 2.5:1 |
| 所需胜率 | 71%才能保本 | 29%即可保本 |
| 7日预期收益 | -62% | -5% ~ +15% |
| 最大回撤 | 无保护 | 单日不超过5% |

---

*文档完成：2026-06-29*