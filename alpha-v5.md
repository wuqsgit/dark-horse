# AlphaDog V5.0 完整规格文档

> 版本：5.0  
> 更新：2026-06-30  
> 环境：Binance 永续合约测试网 (testnet.binancefuture.com)

---

## 1 系统概览

### 1.1 定位

AlphaDog V5 是基于**绝对评分 + 历史胜率**的 Binance 永续合约**全自动**交易系统，通过多因子综合评分选币，配合候选选择器实现分散化持仓，自动执行开平仓。

### 1.2 核心改进 (V4 → V5)

| 问题 | V4 方案 | V5 方案 |
|------|---------|---------|
| 评分百分位归一化 | 所有币都在40-100分区间 | 绝对评分 + 历史胜率加成 |
| 持仓集中3个币 | 只选最高评分 | 候选选择器按类别分散 |
| 频繁弱退出 | 5项弱信号检查 | 移除，只保留5%止损/10%止盈 |
| 冷却机制弱 | 开仓后30min冷却 | 4小时强制持有 |

### 1.3 核心数据流

```
Binance Spot API ─→ Pipeline(每10min) ─→ SQLite ─→ Engine(每5min评分) ─→ Trader(每5min执行)
                                            ↑                              ↑
                                  Dune API(每30min链上数据)          Binance Futures API
```

### 1.4 核心参数

| 参数 | 值 |
|------|-----|
| 测试网 | testnet.binancefuture.com |
| 初始资金 | $5,000 USDT |
| 最大持仓 | 3 |
| 评分阈值 | 20 (V5降低) |
| 评分间隔 | 5分钟 |
| 交易循环 | 5分钟 |
| 开仓冷却 | 4小时 |
| 止损 | 5%固定 |
| 止盈 | 10%固定 |

---

## 2 系统架构

### 2.1 进程组成

| 进程 | 入口文件 | 职责 | 调度 |
|------|----------|------|------|
| pipeline | pipeline/main.py | 数据采集 | 10min(现货)/30min(链上) |
| engine | engine/run.py | 评分+回测 | 5min评分 / 1h回测 |
| trading | trader/runner.py | 交易执行 | 5min循环 |
| api | api/main.py | REST API | 持续(:8000) |

### 2.2 文件结构

```
alphadog/
├── pipeline/
│   ├── main.py           # 入口，APScheduler调度
│   ├── binance_http.py    # 币安HTTP采集器
│   └── dune_collector.py # Dune链上数据采集
├── engine/
│   ├── run.py           # 评分+回测调度
│   ├── scoring.py       # 评分引擎核心 V5.0 (重构)
│   └── factor_weights.json # 因子权重配置
├── trader/
│   ├── runner.py       # 交易主循环
│   ├── execution.py   # 执行引擎 V5.0 (简化版)
│   ├── selection.py # 候选选择器 V5.0 (新增)
│   ├── exchange.py # Binance API封装
│   ├── risk.py    # 风控模块
│   ├── config.py   # 配置常量
│   └── cooldown_manager.py # 冷却管理
├── strategies/
│   └── token_profiles.json # 代币分类+阈值
├── api/
│   └── main.py      # FastAPI应用
├── shared/
│   └── db.py       # 数据库操作
├── alphadog.db    # SQLite数据库
└── alpha-v5.md    # 本文档
```

---

## 3 评分引擎 Engine (V5)

### 3.1 评分逻辑

**文件**：engine/scoring.py  
**类**：ScoringEngine

#### V5 核心改进

```python
# V5: 绝对评分 + 历史胜率加成
def score_all(self, df_1h, df_15m, df_6h, df_24h, df_futures, df_onchain):
    results = []
    for sym in df_1h["symbol"].unique():
        # 1. 技术面绝对评分 (0-100)
        tech_score = self._compute_tech_absolute(df_1h, df_15m)
        
        # 2. 盈利概率预测
        win_prob = self._predict_win_probability(tech_score, df_futures)
        
        # 3. 历史胜率（从trades表查询）
        hist_win_rate = self._get_historical_win_rate(sym)
        
        # 4. 预期值 EV = win% × avg_win - loss% × avg_loss
        ev = win_prob * 0.05 - (1 - win_prob) * 0.05
        
        # 5. 综合评分 = 原始评分 + 历史胜率加成
        # 历史胜率>50%时加分，<50%不扣分
        hist_bonus = max(0, (hist_win_rate - 50)) * 0.5
        composite = tech_score + hist_bonus
        
        results.append({
            "symbol": sym,
            "composite_score": composite,
            "composite_score_raw": tech_score,  # 保存原始评分
            "historical_win_rate": hist_win_rate,
            "relative_strength": hist_win_rate,  # 复用字段存历史胜率
        })
    
    # V5: 分散化过滤
    results = self._apply_diversity_filter(results)
    
    return results
```

### 3.2 历史胜率查询

```python
def _get_historical_win_rates(self) -> dict:
    """从 trades 表查询各币种历史胜率"""
    rows = conn.execute("""
        SELECT symbol,
               COUNT(*) as total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
        FROM trades
        WHERE created_at > datetime('now', '-7 days')
        GROUP BY symbol
    """).fetchall()
    
    # 至少1笔交易即可计入
    win_rates = {r["symbol"]: r["wins"] / r["total"] * 100 for r in rows if r["total"] >= 1}
    return win_rates
```

### 3.3 分散化过滤

```python
def _apply_diversity_filter(self, results, max_per_category=3):
    """按类别分散化，每类最多选max_per_category个"""
    CATEGORY_MAP = {
        "BTC": "蓝筹", "ETH": "蓝筹", "BNB": "蓝筹", "LTC": "蓝筹",
        "SOL": "基本面", "AVAX": "基本面", "ADA": "基本面", "LINK": "基本面",
        "DOGE": "Meme", "SHIB": "Meme",
    }
    
    categories = {}
    for r in results:
        sym = r["symbol"].replace("USDT", "")
        cat = CATEGORY_MAP.get(sym, "其他")
        categories.setdefault(cat, []).append(r)
    
    # 每类选最优
    filtered = []
    for cat, items in categories.items():
        filtered.extend(items[:max_per_category])
    
    # 补充未分类的高分币
    if len(filtered) < 10:
        filtered.extend([r for r in results if r not in filtered][:10-len(filtered)])
    
    return sorted(filtered, key=lambda x: -x["composite_score"])
```

### 3.4 评分字段

| 字段 | 说明 |
|------|------|
| composite_score | V5综合评分（原始+历史胜率加成） |
| composite_score_raw | 原始技术评分 |
| historical_win_rate | 历史胜率(0-100%) |
| relative_strength | 历史胜率（复用字段） |
| chip_phase | 筹码阶段 |
| trend_state | 趋势状态 |
| trend_direction | 趋势方向 |

---

## 4 候选选择器 Selection (V5)

### 4.1 功能

**文件**：trader/selection.py  
**类**：CandidateSelector

实现真正分散化选币：

1. **黑名单过滤** - 24h内止损的币禁止开仓
2. **类别分散** - 每类最多1个持仓
3. **4小时冷却** - 开仓后4小时内不能开仓

### 4.2 类别配置

```python
CATEGORY_LIMITS = {
    "蓝筹": 1,      # BTC/ETH 最多1个
    "基本面": 1,     # 主流叙事币 最多1个
    "Meme": 1,      # Meme币 最多1个
    "其他": 1,       # 其他 最多1个
}

CATEGORY_MAP = {
    "BTC": "蓝筹", "ETH": "蓝筹", "BNB": "蓝筹", "LTC": "蓝筹",
    "SOL": "基本面", "AVAX": "基本面", "ADA": "基本面", "LINK": "基本面",
    "DOT": "基本面", "MATIC": "基本面", "NEAR": "基本面", "ARB": "基本面",
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme",
}
```

### 4.3 选币流程

```python
def select_candidates(self, scored_symbols, current_positions, max_positions=3):
    # 1. 排除已有持仓
    available = [s for s in scored_symbols if s["symbol"] not in pos_symbols]
    
    # 2. 排除黑名单
    available = [s for s in available if s["symbol"] not in self.blacklist]
    
    # 3. 按类别分组
    categories = group_by_category(available)
    
    # 4. 每类选最优
    selected = []
    for cat, symbols in categories.items():
        limit = CATEGORY_LIMITS.get(cat, 1)
        selected.extend(sorted(symbols, key=lambda x: -x["score"])[:limit])
    
    # 5. 补充至max_positions
    if len(selected) < max_positions:
        selected.extend(remaining[:max_positions-len(selected)])
    
    return selected[:max_positions]
```

---

## 5 执行引擎 Execution (V5)

### 5.1 简化决策逻辑

**文件**：trader/execution.py  
**类**：ExecutionEngine

#### V5 核心改进

| V4 | V5 |
|----|-----|
| 5项弱信号检查 | 移除 |
| 趋势破坏退出 | 移除 |
| 移动止盈 | 移除 |
| 分批止盈(5%/10%) | 保留 |
| 弱退出减仓 | 移除 |

#### V5 退出逻辑

```python
def decide(self, top_symbols, positions):
    actions = []
    
    # === 1. 处理持仓 ===
    for pos in positions:
        pnl_pct = pos["unrealized_pnl"] / pos["margin"] * 100
        
        if pnl_pct <= -5:
            # 硬止损（5%）
            actions.append({
                "action": "close",
                "symbol": pos["symbol"],
                "reason": f"硬止损{pnl_pct:.1f}%"
            })
        elif pnl_pct >= 10:
            # 止盈（10%）
            actions.append({
                "action": "close", 
                "symbol": pos["symbol"],
                "reason": f"止盈{pnl_pct:.1f}%"
            })
        # V5: 不再有弱退出，信任系统持有
    
    # === 2. 开新仓 ===
    if len(positions) < 3:
        selector = CandidateSelector()
        candidates = selector.select_candidates(top_symbols, positions)
        
        for cand in candidates:
            if len(actions) >= 3:
                break
            actions.append(self._build_open_action(cand))
    
    return actions
```

### 5.2 开仓参数

```python
def _build_open_action(self, cand):
    return {
        "action": "open",
        "symbol": cand["symbol"],
        "leverage": 3,           # 固定3x
        "stop_loss_pct": 0.05,   # 固定5%止损
        "take_profit_pct": 0.10,  # 固定10%止盈
    }
```

---

## 6 冷却机制 Cooldown (V5)

### 6.1 配置

**文件**：trader/cooldown_manager.py

| 场景 | V4 | V5 |
|------|-----|-----|
| 开仓后冷却 | 30min | 4小时 |
| 单次止损 | 6小时 | 12小时 |
| 连续2次止损 | 24小时 | 24小时 |
| 每日止损上限 | 3次 | 3次 |

### 6.2 黑名单逻辑

```python
def _load_blacklist(self) -> set:
    """加载黑名单 - 24h内止损的币"""
    rows = conn.execute("""
        SELECT symbol FROM trade_cooldown 
        WHERE cooldown_until > datetime('now', 'utc')
    """).fetchall()
    return {r["symbol"] for r in rows}
```

---

## 7 数据库 Schema

### 7.1 核心表

| 表名 | 说明 |
|------|------|
| alpha_scores | 评分结果 |
| trades | 交易记录 |
| trade_cooldown | 冷却记录 |
| klines_1h | 1小时K线 |
| klines_15m | 15分钟K线 |
| futures | 期货数据 |
| onchain | 链上数据 |
| training_samples | 训练样本 |

### 7.2 关键查询

```sql
-- 最新评分 Top 10
SELECT symbol, composite_score, historical_win_rate
FROM alpha_scores 
WHERE time = (SELECT MAX(time) FROM alpha_scores)
ORDER BY composite_score DESC 
LIMIT 10;

-- 历史胜率
SELECT symbol,
       COUNT(*) as total,
       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
FROM trades
WHERE created_at > datetime('now', '-7 days')
GROUP BY symbol;

-- 黑名单
SELECT symbol FROM trade_cooldown 
WHERE cooldown_until > datetime('now', 'utc');
```

---

## 8 部署与运维

### 8.1 启动命令

```bash
# 启动 Pipeline
cd alphadog && python3 pipeline/main.py > logs/pipeline.log 2>&1 &

# 启动 Engine  
cd alphadog && python3 engine/run.py > logs/engine.log 2>&1 &

# 启动 Trader
cd alphadog && python3 trader/runner.py > logs/trader.log 2>&1 &
```

### 8.2 监控

```bash
# 检查评分更新
sqlite3 alphadog.db "SELECT MAX(time) FROM alpha_scores;"

# 检查交易记录
sqlite3 alphadog.db "SELECT symbol, COUNT(*) FROM trades GROUP BY symbol;"

# 检查进程
ps aux | grep -E "(engine|pipeline|runner)"
```

### 8.3 日志

| 日志文件 | 内容 |
|---------|------|
| logs/pipeline.log | 数据采集 |
| logs/engine.log | 评分引擎 |
| logs/trader.log | 交易执行 |

---

## 9 版本历史

| 版本 | 日期 | 改进 |
|------|------|------|
| V4.0 | 2026-06-29 | 初始版本 |
| V5.0 | 2026-06-30 | 评分重构+候选选择器+简化执行 |

---

## 10 附录

### 10.1 评分分布示例 (V5)

```
FETUSDT: 32.0 (历史胜率75%)
JTOUSDT: 29.3 (历史胜率75.7%)
AAVEUSDT: 27.1 (历史胜率55.2%)
ADAUSDT: 24.5 (历史胜率50.0%)
AVAXUSDT: 24.5 (历史胜率0.0%)
```

### 10.2 预期效果

| 指标 | V4 (现状) | V5 (预期) |
|------|----------|----------|
| 持仓分散度 | 3个币循环 | 每类1个，最多3类 |
| 日均交易次数 | 20+ | ≤3 |
| 平均持仓时间 | 5分钟 | 4小时以上 |
| 弱退出比例 | 90% | 0% |