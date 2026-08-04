# AI-Driven Alpha Entry Strategy V2 Design

## 1. 决策摘要

Alpha 开仓策略重构为“AI 主导策略判断、确定性系统掌管风险和执行”的混合架构。

- AI 负责识别吸筹、趋势回踩、洗盘收复、点火、突破接受和假突破概率。
- Alpha Strategy Worker 负责维护跨周期、有记忆的市场状态机。
- Trader 只消费状态机生成的交易意图，不再在交易循环中重新拼装 Alpha 形态。
- 账户风险、同类币限制、最大敞口、盘口滑点、止损、订单幂等和最终下单继续由确定性代码负责。
- 第一版继续使用 XGBoost，不使用大语言模型直接判断 K 线，也不让 AI 服务持有交易所密钥。
- 旧 `evaluate_alpha_volume_price()` 在迁移期保留为兼容适配器，稳定后删除。

目标不是放宽追涨，而是更早发现类似 AKE 的候选，在小风险试仓后，随着市场确认逐级增加仓位，同时过滤 ARC 类量价失效和 BANK 类异常波动。

## 2. 当前链路与主要问题

现有调用链：

```text
Alpha Pipeline（10 分钟）
  → 拉取 Alpha / Futures K 线和盘口
  → alpha_candles_* / futures_candles_*

Alpha Engine（5 分钟）
  → 计算当前快照分数
  → evaluate_alpha_volume_price()
  → alpha_scan_scores

Trader（5 分钟）
  → 读取最新 Alpha 扫描
  → 再次 evaluate_alpha_volume_price()
  → 突破、市场阶段、盘口、组合风险过滤
  → 生成 open action

AI Entry Quality
  → 只审核已经生成的 open action
  → allow / probe / reject / resize
```

主要问题：

1. Alpha 决策是无记忆的单次快照，无法表达“先观察、再点火、后确认”的过程。
2. AI 介入点位于开仓动作生成之后，无法提前发现 AKE 式吸筹。
3. 6 小时量能阈值把发现、触发和追涨风险混为一个条件。
4. 短线涨幅超过阈值后固定冷却，无法转换成“等待回踩确认”。
5. 当前突破确认只看最近几根 K 线，不知道候选之前处于何种形态。
6. AI 当前标签是未来 24 小时先到 `+1R` 还是 `-1R`，不能单独识别大级别延续和短时假突破。
7. 缺失特征当前会补成 `0`，模型无法区分“真实为零”和“数据缺失”。
8. Alpha 采集、评分和交易循环时间不对齐，可能重复消费同一根 K 线或使用未收盘 K 线。
9. 当前 OI 变化按数组位置近似小时，不能保证真实的 4 小时、24 小时时间跨度。
10. 测试网和主网市场数据缺少明确的数据源标识，禁止将不同环境样本混入同一个模型。

## 3. 目标和非目标

### 3.1 目标

1. 在 AKE 第一次点火前进入观察状态。
2. 捕捉趋势中继和洗盘收复，而不是只捕捉 6 小时巨量。
3. 对长上影、突破回落和量价失效给出明确的假突破概率。
4. 支持 `观察 → 试仓 → 确认加仓 → 回踩加仓`。
5. 所有状态迁移可回放、可解释、可恢复、可审计。
6. AI 故障时不影响已有仓位管理。
7. 多账户消费同一个市场信号，但独立执行账户风险。
8. 新旧策略可并行 Shadow 对比并可一键回滚。

### 3.2 非目标

- 第一版不使用 Transformer、视觉模型或大语言模型读取图表。
- 第一版不让 AI 自行决定杠杆上限、最大账户敞口或取消硬止损。
- 第一版不开放 Alpha 做空。
- 第一版不以单一 AKE 样本直接拟合生产阈值。
- 第一版不替换现有普通币开仓策略。
- 第一版不删除现有仓位盈利加仓和退出管理。

## 4. 总体架构

```text
┌────────────────────────────────────────────────────────────┐
│ Market Data                                                │
│ Alpha Spot + Binance Futures + OI + Funding + Orderbook    │
└─────────────────────────────┬──────────────────────────────┘
                              │ closed candle only
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Alpha Feature Builder V3                                   │
│ 结构 / 量能 / K线质量 / 跨市场 / 衍生品 / 流动性 / 市场上下文 │
└─────────────────────────────┬──────────────────────────────┘
                              │ immutable snapshot
                              ▼
┌────────────────────────────────────────────────────────────┐
│ AI Strategy Service                                        │
│ setup model / trigger model / fakeout model                │
│ p_setup / p_followthrough / p_fakeout / expected_r         │
└─────────────────────────────┬──────────────────────────────┘
                              │ recommendation
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Alpha Strategy State Machine                               │
│ IDLE → WATCH → ARMED → PROBE_READY → CONFIRMED → RETEST    │
│                    ↘ WAIT_RETEST / FAILED / EXPIRED         │
└─────────────────────────────┬──────────────────────────────┘
                              │ append-only signal event
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Trader Account Risk Adapter                                │
│ 持仓/同类币/账户敞口/流动性/风险预算/信号消费幂等              │
└─────────────────────────────┬──────────────────────────────┘
                              │ bounded trade intent
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Existing Execution Engine                                  │
│ open / roll_add / close / stop / order reconciliation      │
└────────────────────────────────────────────────────────────┘
```

## 5. 运行时组件

### 5.1 Market Data Collector

拆分当前统一的 10 分钟采集：

- Universe refresh：每 10 分钟。
- 15 分钟 K 线滚动窗口：每 60 秒检查一次。
- 1 小时、6 小时、24 小时 K 线：每 10 分钟。
- OI、Funding、Mark Price：每 5 分钟。
- 盘口快照：观察池外每 10 分钟，观察池内每 1 分钟。

第一版仍可使用 REST。Worker 通过 `last_closed_bar_time` 去重，只在出现新的已收盘 15 分钟 K 线时计算策略状态。

后续可切换 WebSocket，但 WebSocket 只改变数据到达方式，不改变状态机接口。

### 5.2 Alpha Feature Builder V3

新增纯函数式特征构建器：

```python
build_alpha_feature_snapshot(
    alpha_symbol: str,
    futures_symbol: str,
    cutoff_time: datetime,
    market_env: str,
) -> AlphaFeatureSnapshot
```

约束：

- 只允许读取 `cutoff_time` 之前已经收盘的数据。
- 相同 `symbol + cutoff_time + feature_schema_version` 必须得到相同结果。
- 不读取账户、持仓或未来 K 线。
- 每个特征同时带有 `present` 状态，缺失值不再伪装成零。
- 每个快照记录数据源、最新 K 线时间和特征版本。

### 5.3 AI Strategy Service

复用现有 8010 服务，新增 Alpha Strategy V2 API 和独立模型组。

AI 只返回预测和建议，不写 `alphadog.db`，不接收交易所密钥。

### 5.4 Alpha Strategy Worker

新增独立 Worker，建议暂放在 `alpha_engine/strategy_worker.py`。

职责：

1. 读取最新已收盘 K 线。
2. 生成不可变特征快照。
3. 调用 AI Strategy Service。
4. 执行确定性状态迁移。
5. 原子更新当前状态。
6. 追加状态事件。
7. 不直接下单。

运行频率为每 60 秒，但只在新闭合 K 线到来时推进主要状态。

### 5.5 Trader

Trader 不再重复计算 Alpha 量价形态，只读取最新可执行事件：

- `PROBE_READY`：无仓位账户可申请试仓。
- `CONFIRMED`：已有 Alpha 试仓账户可申请第一次加仓；无仓位账户可申请确认仓。
- `RETEST_READY`：已有对应仓位可申请后续加仓。
- `INVALIDATED`：试仓可触发结构失效退出评估。

每个账户独立执行风险检查并记录事件消费结果。

## 6. 状态机设计

### 6.1 状态定义

| 状态 | 含义 | 是否允许产生交易事件 |
|---|---|---|
| `IDLE` | 没有有效形态 | 否 |
| `WATCH_ACCUMULATION` | 高换手、窄幅承接 | 否 |
| `WATCH_CONTINUATION` | 上升趋势中的缩量回踩 | 否 |
| `WATCH_RECLAIM` | 深洗盘后的潜在收复 | 否 |
| `ARMED` | 形态成熟，等待点火 | 否 |
| `PROBE_READY` | 点火成立，可申请试仓 | 是 |
| `WAIT_RETEST` | 价格过热，不追，等待首次回踩 | 否 |
| `ACCEPTANCE_PENDING` | 已点火，等待突破接受 | 否 |
| `CONFIRMED` | 突破被接受，可申请确认仓/加仓 | 是 |
| `RETEST_READY` | 第一次缩量回踩成功，可申请继续加仓 | 是 |
| `FAILED` | 形态或突破失效 | 可触发试仓退出评估 |
| `COOLDOWN` | 失败后的冷却期 | 否 |
| `EXPIRED` | 观察或触发超时 | 否 |

### 6.2 核心迁移

```text
IDLE
  ├─ accumulation setup → WATCH_ACCUMULATION
  ├─ continuation setup → WATCH_CONTINUATION
  └─ reclaim setup      → WATCH_RECLAIM

WATCH_*
  ├─ setup strengthened → ARMED
  ├─ invalidated        → FAILED
  └─ ttl exceeded       → EXPIRED

ARMED
  ├─ normal ignition    → PROBE_READY
  ├─ overheated ignition→ WAIT_RETEST
  ├─ invalidated        → FAILED
  └─ ttl exceeded       → EXPIRED

PROBE_READY
  ├─ event emitted      → ACCEPTANCE_PENDING
  ├─ breakout rejected  → FAILED
  └─ ttl exceeded       → EXPIRED

WAIT_RETEST
  ├─ first retest holds → CONFIRMED
  ├─ falls into base    → FAILED
  └─ ttl exceeded       → EXPIRED

ACCEPTANCE_PENDING
  ├─ hold confirmed     → CONFIRMED
  ├─ falls into base    → FAILED
  └─ ttl exceeded       → EXPIRED

CONFIRMED
  ├─ first pullback hold→ RETEST_READY
  ├─ structure breaks   → FAILED
  └─ trend ages out     → EXPIRED

FAILED / EXPIRED
  └─ reason-based delay → COOLDOWN → IDLE
```

### 6.3 状态迁移优先级

同一轮只能迁移一次，优先级固定：

1. 数据硬错误
2. 结构失效
3. TTL 过期
4. 突破确认
5. 点火
6. 观察升级
7. 保持原状态

这样可防止同一根 K 线先产生开仓事件、后又被判定失败。

## 7. 三类 Setup

以下数值是第一轮回放起始参数，不是直接上线参数。

### 7.1 Accumulation Setup

用于捕捉 AKE 第一次点火前的形态。

确定性候选条件：

- `range_2h_pct <= max(4%, 1.5 * atr_15m_pct)`
- 最近 3 根 15 分钟成交额斜率向上，或 `quote_volume_ratio_1h >= 1.5`
- 价格没有同步创新低
- `absorption_score >= 60`
- 点差和深度通过观察级数据门槛

核心特征：

```text
absorption_score =
    volume_activity_score
    × price_stability_score
    × low_hold_score
```

高成交、低位移只代表“有人承接”，不能决定方向，因此只能进入观察状态。

### 7.2 Continuation Setup

用于捕捉 AKE 后续台阶式主升。

确定性候选条件：

- 1 小时 EMA20 斜率向上
- 1 小时收盘位于 EMA20 上方，或刚刚重新收复
- 距离 24 小时高点回撤约 5%–18%
- 最近 2 小时成交量相对之前区间不扩张，初始阈值 `<= 1.10`
- 最近 6 根 1 小时 K 线至少 3 次低点抬高，或者主要结构低点未破

### 7.3 Reclaim Setup

用于捕捉深洗盘后的快速收复。

确定性候选条件：

- 从 24 小时高点回撤约 8%–25%
- 出现放量止跌或长下影
- 收盘重新站回关键平台、EMA20 或破位前价格
- 收复 K 线成交量初始阈值 `>= 1.8x`

该类型风险最高，默认只能产生试仓，不允许直接确认仓。

## 8. 点火、过热和突破接受

### 8.1 Normal Ignition

初始条件：

- 已处于 `ARMED`
- 已收盘 15 分钟涨幅约 2.5%–8%
- 15 分钟成交额相对前 24 根中位数 `>= 1.8x`
- 收盘突破观察区上沿或重新收复关键位
- `close_location >= 0.65`
- `upper_wick_ratio <= 0.35`
- `price_efficiency_score >= 60`
- AI `p_followthrough >= 0.65`
- AI `p_fakeout <= 0.35`

满足后进入 `PROBE_READY`。

### 8.2 Overheated Ignition

当出现以下任一条件：

- `ret_15m > 8%`
- `ret_1h > 15%`
- `ret_6h > 30%`
- 单根 K 线区间超过短周期 ATR 的异常倍数

不再固定冷却，也不允许新开满仓：

- 已经有吸筹试探仓：保留仓位，进入 `WAIT_RETEST`。
- 没有仓位：只进入 `WAIT_RETEST`，不追涨。
- 回踩成功后可以进入 `CONFIRMED`。
- 跌回原平台则进入 `FAILED`。

### 8.3 Breakout Acceptance

点火后的 1–2 根已收盘 15 分钟 K 线检查：

- 收盘仍位于突破位上方。
- 最低价未有效跌穿结构失效位。
- 回踩量能小于点火量能。
- 没有连续长上影。
- AI `p_followthrough >= 0.70`
- AI `p_fakeout <= 0.25`

确认后进入 `CONFIRMED` 并产生确认事件。

### 8.4 First Retest

确认后首次回踩：

- 回撤不超过点火段的 38%–61.8%，具体使用 ATR 和结构共同约束。
- 成交量收缩。
- 收盘重新站回突破位或 EMA20。
- 未出现 OI 坍塌、放量下跌和盘口流动性崩坏。

满足后进入 `RETEST_READY`。

## 9. AI 模型设计

### 9.1 第一版模型

第一版使用三个二分类 XGBoost 模型：

| Model Key | 输入时点 | 输出 |
|---|---|---|
| `alpha_setup_v1` | 观察候选形成时 | `p_setup_success` |
| `alpha_trigger_v1` | 点火 K 线收盘时 | `p_followthrough` |
| `alpha_fakeout_v1` | 点火或确认 K 线收盘时 | `p_fakeout` |

不在第一版引入序列神经网络，原因是：

- 当前有效标签数量有限。
- 表格特征更容易审计。
- XGBoost 对缺失值和非线性阈值较友好。
- 训练、部署、回滚可复用当前 AI 服务。

### 9.2 AI 输出契约

```json
{
  "request_id": "AKEUSDT:2026-07-15T04:30:00Z:trigger:v3",
  "status": "ready",
  "stage": "trigger",
  "model_versions": {
    "followthrough": "alpha_trigger_v1_20260728T020000Z",
    "fakeout": "alpha_fakeout_v1_20260728T020000Z"
  },
  "p_setup_success": 0.74,
  "p_followthrough": 0.78,
  "p_fakeout": 0.19,
  "expected_r": 0.46,
  "recommended_action": "probe",
  "max_position_factor": 0.30,
  "reasons": [
    "volume contraction before breakout",
    "higher-low structure",
    "breakout close accepted"
  ],
  "feature_schema_version": 3
}
```

`recommended_action` 仅是建议。状态迁移器必须再次检查硬条件。

### 9.3 AI 不可控制的内容

AI 不得控制：

- 交易所 API 请求。
- 最大杠杆。
- 最大账户敞口。
- 同类型币限制。
- 单笔最大风险。
- 数据是否可交易的硬判断。
- 订单 client id。
- 是否绕过止损。
- 是否忽略已有仓位或未完成订单。

## 10. Feature Schema V3

### 10.1 价格结构

- `ret_15m`, `ret_30m`, `ret_1h`, `ret_2h`, `ret_4h`, `ret_6h`, `ret_24h`
- `range_2h_pct`, `range_6h_pct`, `range_24h_pct`
- `atr_15m_pct`, `atr_1h_pct`
- `compression_2h_vs_24h`
- `ema20_distance_15m`, `ema20_slope_15m`
- `ema20_50_ratio_1h`, `ema20_slope_1h`
- `higher_lows_8x15m`, `higher_lows_6x1h`
- `higher_highs_8x15m`, `higher_highs_6x1h`
- `distance_from_high_2h`, `distance_from_high_6h`, `distance_from_high_24h`
- `breakout_distance_pct`
- `closes_above_breakout_level`

### 10.2 K 线质量

- `body_return_pct`
- `true_range_pct`
- `close_location`
- `upper_wick_ratio`
- `lower_wick_ratio`
- `body_to_range_ratio`
- `gap_from_previous_close`
- `retest_depth_pct`
- `breakout_hold_bars`

### 10.3 量能和成交效率

- `quote_volume_ratio_15m`
- `quote_volume_ratio_1h`
- `quote_volume_ratio_6h`
- `quote_volume_zscore_15m`
- `quote_volume_slope_3bars`
- `pre_breakout_volume_contraction`
- `trade_count_ratio`
- `average_trade_size_ratio`
- `taker_buy_quote_ratio`
- `taker_buy_ratio_slope`
- `price_efficiency_score`
- `absorption_score`

建议定义：

```text
normalized_price_move = abs(ret_15m) / max(atr_15m_pct, epsilon)
normalized_volume = max(quote_volume_ratio_15m, epsilon)
price_efficiency = normalized_price_move / log1p(normalized_volume)
```

`absorption_score` 在成交活跃但价格区间受控、低点不继续下移时升高。

### 10.4 衍生品

- `oi_change_15m`, `oi_change_1h`, `oi_change_4h`, `oi_change_24h`
- `price_oi_quadrant`
- `funding_rate`
- `funding_zscore`
- `mark_index_basis`
- `liquidation_pressure`（数据可用时）

OI 必须按时间戳查找最近样本，不允许按数组下标模拟小时。

### 10.5 双市场同步

- `alpha_spot_return_15m`
- `futures_return_15m`
- `spot_futures_return_diff`
- `spot_volume_ratio_15m`
- `futures_volume_ratio_15m`
- `volume_sync_score`
- `spot_leads_futures`
- `futures_leads_spot`

### 10.6 流动性

- `spread_pct_current`
- `spread_pct_median_15m`
- `spread_pct_p95_1h`
- `bid_depth_usdt`
- `ask_depth_usdt`
- `depth_imbalance`
- `depth_imbalance_stability`
- `estimated_slippage_probe`
- `estimated_slippage_confirmed`

深度必须按 `price × quantity` 转换为 USDT，而不是只累计币数量。

### 10.7 市场上下文

- `btc_ret_1h`, `btc_ret_6h`
- `market_breadth_1h`
- `market_breadth_6h`
- `alpha_universe_median_return`
- `category_relative_strength`
- `listing_age_hours`
- `market_phase_code`
- `setup_type_code`

## 11. 数据管线变更

### 11.1 K 线字段

现有 K 线表增加：

```sql
ALTER TABLE futures_candles_15m ADD COLUMN taker_buy_quote_vol REAL;
ALTER TABLE futures_candles_15m ADD COLUMN source_env TEXT;
ALTER TABLE futures_candles_15m ADD COLUMN is_closed INTEGER DEFAULT 1;
```

其他需要训练的周期同步增加 `source_env`。Alpha K 线在接口提供字段时增加主动买入成交额。

### 11.2 环境隔离

每条市场数据、特征快照、训练样本和模型都必须标记：

```text
source_env = mainnet | testnet
```

规则：

- Testnet 执行只消费 Testnet 状态。
- Mainnet 执行只消费 Mainnet 状态。
- Testnet 和 Mainnet 样本不得训练同一个模型。
- 模型 key 实际带环境，例如 `alpha_trigger_v1_mainnet`。

### 11.3 已收盘 K 线

状态机只能以闭合 K 线推进：

```text
bar_open_time + interval <= evaluation_time
```

数据库读取必须显式排除当前正在形成的 K 线。

## 12. 标签与训练

### 12.1 Counterfactual 样本

不能只采集最终开仓的币，否则产生选择偏差。

每根已收盘 15 分钟 K 线，对以下对象保存样本：

- 全部 Alpha Top N 中满足最低数据质量的币。
- 所有 `WATCH_*`、`ARMED`、`PROBE_READY` 和 `WAIT_RETEST` 状态。
- 已持仓 Alpha 币。

样本唯一键：

```text
market_env + model_key + symbol + stage + candle_close_time + feature_schema_version
```

### 12.2 Setup 标签

观察状态以未来 8 小时为窗口：

```text
positive:
  先达到 +2R，且达到前没有触发 -1R

negative:
  先达到 -1R，或者 8 小时内没有达到 +2R
```

同时保存：

- `mfe_2h_r`, `mfe_4h_r`, `mfe_8h_r`
- `mae_30m_r`, `mae_2h_r`, `mae_8h_r`
- `time_to_1r`, `time_to_2r`

### 12.3 Trigger 标签

点火状态以未来 4 小时为窗口：

```text
followthrough_positive:
  先达到 +2R，且 4 小时收盘收益为正

fakeout_positive:
  60 分钟内重新收回突破位下方
  或者先达到 -0.75R
```

同一根 K 线同时触及上下阈值时按不利方向优先。

### 12.4 Acceptance 标签

突破接受以未来 2–4 小时为窗口：

```text
positive:
  突破位保持，且未来最大有利波动达到 +1.5R

negative:
  重新跌回平台并达到 -0.75R
```

### 12.5 实际成交标签

Counterfactual 标签用于学习形态，真实交易结果用于校准：

- 实际成交价。
- 实际滑点。
- 实际手续费和资金费。
- 实际最大有利/不利波动。
- 最终退出原因和实现 R。

模型发布必须同时报告理论标签和真实成交子集结果。

### 12.6 训练切分

- 严格按时间顺序切分。
- 同一段行情的相邻样本不得横跨训练集和验证集。
- 建议按自然日或 24 小时 purge gap 分块。
- AKE 一类连续主升样本需要降权，避免一段行情贡献大量重复正样本。
- 按 setup type、波动分位、币种和上市年龄分别报告指标。

### 12.7 模型发布门槛

初始门槛：

- 每个模型至少 1,000 个合格标签。
- 验证集至少 300 个样本。
- 正负类别均存在。
- PR-AUC 高于基线。
- Top 20% 预测组的净期望 R 高于未过滤基线。
- Brier Score 和校准曲线不明显恶化。
- 假突破模型在验证集的召回率达到配置门槛。
- 任一特征重要性占比不得超过 60%。
- 关键子组不能出现明显灾难性退化。

发布采用 Champion / Challenger：

- 当前生产模型为 Champion。
- 新模型先运行 Shadow。
- 达标后只影响仓位缩放。
- 再达标后才能影响 allow / probe / reject。
- 可随时回滚到上一 Champion。

## 13. AI API

### 13.1 评估

```http
POST /v2/alpha-strategy/evaluate
```

请求：

```json
{
  "request_id": "AKEUSDT:2026-07-15T04:30:00Z:trigger:v3",
  "market_env": "testnet",
  "alpha_symbol": "ALPHA_...USDT",
  "futures_symbol": "AKEUSDT",
  "stage": "trigger",
  "setup_type": "accumulation",
  "candle_close_time": "2026-07-15T04:30:00Z",
  "feature_schema_version": 3,
  "feature_quality": {
    "coverage": 0.94,
    "missing": ["liquidation_pressure"]
  },
  "features": {}
}
```

响应使用第 9.2 节定义。

### 13.2 批量观察

```http
POST /v2/alpha-strategy/observe
```

用于写入全市场 Counterfactual 样本，不影响交易。

### 13.3 状态

```http
GET /v2/alpha-strategy/status
```

返回：

- 各环境模型状态。
- 模型版本。
- 样本数和标签数。
- 特征覆盖率。
- 最近训练、评估和错误时间。
- Drift 指标。

### 13.4 训练和标签

```http
POST /v2/alpha-strategy/outcomes/label
POST /v2/alpha-strategy/models/train
```

## 14. 数据库设计

### 14.1 不可变特征快照

```sql
CREATE TABLE alpha_feature_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market_env TEXT NOT NULL,
    alpha_symbol TEXT,
    futures_symbol TEXT NOT NULL,
    candle_close_time TEXT NOT NULL,
    feature_schema_version INTEGER NOT NULL,
    data_quality_status TEXT NOT NULL,
    data_quality_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        market_env,
        futures_symbol,
        candle_close_time,
        feature_schema_version
    )
);
```

### 14.2 当前市场状态

```sql
CREATE TABLE alpha_signal_states (
    market_env TEXT NOT NULL,
    futures_symbol TEXT NOT NULL,
    alpha_symbol TEXT,
    state TEXT NOT NULL,
    setup_type TEXT,
    setup_id TEXT,
    state_version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    last_candle_close_time TEXT,
    snapshot_id TEXT,
    reference_price REAL,
    base_low REAL,
    base_high REAL,
    breakout_level REAL,
    invalidation_price REAL,
    p_setup_success REAL,
    p_followthrough REAL,
    p_fakeout REAL,
    expected_r REAL,
    model_versions_json TEXT,
    reason_codes_json TEXT,
    metrics_json TEXT,
    PRIMARY KEY (market_env, futures_symbol)
);
```

### 14.3 状态事件

```sql
CREATE TABLE alpha_signal_events (
    event_id TEXT PRIMARY KEY,
    market_env TEXT NOT NULL,
    futures_symbol TEXT NOT NULL,
    alpha_symbol TEXT,
    setup_id TEXT,
    from_state TEXT,
    to_state TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    action_type TEXT,
    event_time TEXT NOT NULL,
    candle_close_time TEXT NOT NULL,
    snapshot_id TEXT,
    reference_price REAL,
    invalidation_price REAL,
    max_position_factor REAL,
    expires_at TEXT,
    reason_codes_json TEXT NOT NULL,
    ai_decision_json TEXT,
    created_at TEXT NOT NULL
);
```

`action_type` 可为：

```text
NONE
PROBE_LONG
CONFIRM_LONG
RETEST_ADD
INVALIDATE_PROBE
```

### 14.4 多账户事件消费

```sql
CREATE TABLE alpha_signal_consumptions (
    account_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    rejection_reason TEXT,
    client_order_id TEXT,
    position_id TEXT,
    quantity REAL,
    order_id TEXT,
    consumed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, event_id, action_type)
);
```

状态：

```text
PENDING
RISK_REJECTED
PLANNED
SUBMITTED
PARTIALLY_FILLED
FILLED
FAILED
EXPIRED
```

### 14.5 迁移兼容

保留：

- `alpha_scan_scores`：全市场发现排序和前端展示。
- `alpha_trade_candidates`：旧链路审计，迁移期继续写。
- `alpha_cooldowns`：账户无关的策略冷却可逐步迁移到状态机。
- `position_roll_events`：继续记录确认和回踩加仓。

## 15. 并发、幂等和恢复

### 15.1 状态更新

使用乐观锁：

```sql
UPDATE alpha_signal_states
SET state = ?, state_version = state_version + 1, ...
WHERE market_env = ?
  AND futures_symbol = ?
  AND state_version = ?;
```

更新成功后，在同一事务内插入对应事件。

### 15.2 事件 ID

```text
event_id =
sha256(
  market_env
  + futures_symbol
  + setup_id
  + to_state
  + candle_close_time
  + state_version
)
```

同一根 K 线重复计算不会产生第二个交易事件。

### 15.3 Client Order ID

```text
client_order_id =
DH-A2-{account_id}-{event_id_short}-{action_type}
```

Trader 超时后必须先按 `client_order_id` 查询订单，再决定是否重试。

### 15.4 服务重启

Strategy Worker 启动时：

1. 读取所有非终止状态。
2. 检查最后处理的闭合 K 线。
3. 从下一根 K 线顺序补算。
4. 已存在的 `event_id` 不重复写。
5. 不根据内存状态猜测订单结果。

Trader 启动时：

1. 查询 `PENDING / PLANNED / SUBMITTED / PARTIALLY_FILLED` 消费记录。
2. 与交易所订单和真实持仓对账。
3. 只在对账完成后继续消费新事件。

## 16. 硬风控执行顺序

AI 事件进入 Trader 后按固定顺序检查：

1. `market_env` 与账户一致。
2. 事件未过期。
3. 事件尚未被该账户消费。
4. 市场数据健康且最新。
5. Futures 合约可交易。
6. 没有冲突的挂单。
7. 当前仓位与事件动作兼容。
8. 同类型币仓位限制。
9. Alpha 最大仓位数。
10. 账户总敞口和单币敞口。
11. 连续亏损和日内损失限制。
12. 实时盘口点差、深度和预估滑点。
13. 按结构失效位计算风险预算。
14. 数量、最小名义价值和精度校验。
15. 生成幂等订单并执行。

任一失败只记录 `RISK_REJECTED`，不得要求 AI 再次解释或绕过。

## 17. 仓位构建

以“计划最大 Alpha 仓位”为基准：

| 阶段 | 默认使用比例 | 说明 |
|---|---:|---|
| Accumulation 提前试探 | 0%–15% | 仅高质量、低滑点形态允许 |
| `PROBE_READY` | 20%–30% | 点火成立 |
| `CONFIRMED` | 再增加 30%–40% | 突破被接受 |
| `RETEST_READY` | 再增加 20%–30% | 第一次缩量回踩成功 |

总仓位仍受以下更小值约束：

```text
最终仓位 =
min(
  AI max_position_factor,
  setup stage cap,
  symbol risk cap,
  category cap,
  account exposure cap,
  risk-budget size,
  liquidity size
)
```

加仓继续复用 `roll_add` 执行和 `position_roll_events`，但新增：

- `signal_event_id`
- `setup_id`
- `alpha_stage`
- `ai_model_versions`

## 18. 配置

新增 `alpha_strategy_v2`：

```python
"alpha_strategy_v2": {
    "enabled": False,
    "mode": "shadow",
    "market_env": "testnet",
    "worker_interval_seconds": 60,
    "closed_bar_delay_seconds": 5,
    "feature_schema_version": 3,
    "setup_watch_threshold": 0.55,
    "setup_arm_threshold": 0.62,
    "trigger_followthrough_threshold": 0.65,
    "trigger_fakeout_max": 0.35,
    "acceptance_followthrough_threshold": 0.70,
    "acceptance_fakeout_max": 0.25,
    "watch_ttl_hours": 12,
    "armed_ttl_hours": 4,
    "acceptance_ttl_bars": 2,
    "wait_retest_ttl_hours": 4,
    "probe_stage_cap": 0.30,
    "confirmed_stage_cap": 0.70,
    "retest_stage_cap": 1.00,
    "ai_timeout_ms": 300,
    "ai_failure_mode": "hold_state",
}
```

模式：

- `off`：不运行。
- `shadow`：计算并记录，不产生真实交易事件。
- `signal`：产生事件，但 Trader 只记录不执行。
- `testnet_live`：测试网真实执行。
- `mainnet_canary`：主网受限账户和仓位。
- `mainnet_live`：主网正式运行。

## 19. 降级策略

### 19.1 AI 超时

- `IDLE / WATCH / ARMED`：保持原状态，不升级。
- `PROBE_READY / ACCEPTANCE_PENDING`：不产生新仓位事件。
- 已有仓位：继续由现有止损、退出和加仓风控管理。
- 连续超时达到阈值后状态页显示错误。

### 19.2 模型未就绪

- Shadow 模式继续收集样本。
- 不允许旧的单阈值逻辑自动替代 AI V2 产生新仓事件。
- 旧 Alpha 策略是否继续交易由独立 feature flag 决定。

### 19.3 特征缺失

- 硬关键字段缺失：不评估、不迁移。
- 可选字段缺失：传递缺失掩码，由模型处理。
- 特征覆盖率低于配置门槛：保持状态。

## 20. 回放系统

新增：

```text
backtest/alpha_strategy_v2/
├── replay.py
├── event_clock.py
├── feature_source.py
├── simulated_ai.py
├── execution_simulator.py
├── metrics.py
└── reports.py
```

要求：

- 使用事件时间推进，禁止读取未来数据。
- 完全复用生产 Feature Builder 和 State Machine。
- 可选择固定模型输出或真实模型输出。
- 支持手续费、资金费和滑点。
- 支持部分成交和事件过期。
- 每次回放产出逐状态审计日志。

核心指标：

- `large_move_recall`：未来达到 +2R/+3R 的行情被发现比例。
- `watch_precision`：观察状态最终转化率。
- `probe_precision`：试仓后先到 +2R 的比例。
- `fakeout_rate`：点火后快速跌回平台比例。
- `capture_ratio`：捕获完整上涨段的比例。
- `entry_delay_bars`：相对理想触发点延迟。
- `mfe_r`, `mae_r`, `realized_r`。
- 扣除成本后的期望 R。
- 最大回撤和连续亏损。
- 按 setup type、币种、类别和市场阶段拆分。

## 21. AKE / ARC / BANK 验收场景

### 21.1 AKE

期望：

- 7 月 15 日点火前进入 `WATCH_ACCUMULATION`。
- 成交增加但价格横住时不直接开满仓。
- 12:45 巨幅点火时，如果没有提前试仓则进入 `WAIT_RETEST`，不追满仓。
- 7 月 24 日和 27 日的缩量整理、重新放量阶段进入 `WATCH_CONTINUATION → ARMED → PROBE_READY`。
- 趋势确认后允许分批加仓。

### 21.2 ARC

期望：

- 巨量但价格没有有效位移时可进入观察，但不得直接试仓。
- 上影线过长、突破位无法保持时 `p_fakeout` 升高。
- 跌回平台后进入 `FAILED`。

### 21.3 BANK

期望：

- 按实际风险类别参与同类币限制。
- 异常点差、深度不足或环境数据不一致时被硬风控拦截。
- 若属于洗盘收复，必须走 `WATCH_RECLAIM`，默认只允许试仓。

## 22. 监控与前端

Alpha 页面新增：

- 当前状态和持续时间。
- Setup 类型。
- `p_setup_success / p_followthrough / p_fakeout`。
- 突破位、失效位和当前距离。
- 最新状态迁移原因。
- 模型版本和特征覆盖率。
- Shadow 建议与真实动作对比。
- 每个账户的事件消费结果。

运行状态新增：

- Strategy Worker 心跳。
- 最新已处理 K 线。
- 待处理状态数。
- AI 超时率。
- 状态迁移计数。
- 事件重复抑制计数。
- 各模型 Drift 状态。

告警：

- Worker 超过 3 分钟无心跳。
- 闭合 K 线延迟超过 5 分钟。
- AI 超时率超过 10%。
- 特征覆盖率突然下降。
- 同一事件出现重复订单请求。
- 模型输入分布显著漂移。

## 23. 代码落点

新增：

```text
alpha_engine/strategy/
├── __init__.py
├── models.py
├── feature_builder.py
├── setup_rules.py
├── trigger_rules.py
├── state_machine.py
├── repository.py
├── worker.py
└── config.py

ai_service/
├── alpha_features_v3.py
├── alpha_labels.py
└── alpha_strategy_service.py

backtest/alpha_strategy_v2/
└── ...
```

修改：

- `alpha_pipeline/collector.py`
  - 环境化数据源、主动买入成交额、闭合 K 线、OI 时间序列和深度 USDT。
- `alpha_engine/run.py`
  - 启动 Strategy Worker，迁移期继续旧评分。
- `ai_service/main.py`
  - 增加 V2 Strategy API。
- `ai_service/storage.py`
  - 阶段样本、模型组和多时间窗标签。
- `trader/execution.py`
  - Alpha 开仓改为消费 Signal Event。
- `trader/runner.py`
  - 在每个账户循环内读取未消费事件。
- `shared/db.py`
  - 新表、事务、CAS 更新和事件消费。
- `api/main.py`
  - 状态、事件和模型结果查询。
- `frontend`
  - 状态机和 AI 概率展示。

迁移期：

```text
evaluate_alpha_volume_price()
  → LegacyAlphaEntryAdapter
```

新策略稳定后删除 Trader 中对该函数的重复调用。

## 24. 实施阶段

### Phase 0：数据正确性

- 增加环境标识。
- 严格过滤闭合 K 线。
- 修复 OI 时间窗口。
- 增加 taker buy 和深度 USDT 字段。
- 建立完整性测试。

未完成 Phase 0 不训练模型。

### Phase 1：回放和特征

- 实现 Feature Schema V3。
- 建立无未来数据的回放时钟。
- 用 AKE、ARC、BANK 和全市场历史生成 Counterfactual 样本。
- 先运行规则状态机，不接 AI。

### Phase 2：AI Shadow

- 训练 setup / trigger / fakeout 模型。
- 状态机同时记录规则结果和 AI 结果。
- 不产生真实交易事件。
- 评估阈值、覆盖率和漂移。

### Phase 3：Signal 模式

- 产生 `PROBE_READY / CONFIRMED / RETEST_READY` 事件。
- Trader 只记录本应执行的动作。
- 与旧策略进行逐信号对照。

### Phase 4：Testnet Live

- 只开放试仓。
- 达到验收门槛后开放确认加仓。
- 最后开放回踩加仓。

### Phase 5：Mainnet Canary

- 单独账户。
- 极小仓位。
- AI 只能缩小仓位，不能放大规则风险预算。
- 稳定后再逐步提高权限。

## 25. 上线验收门槛

进入 Testnet Live 前：

- 状态机回放无未来数据泄漏。
- 同一根 K 线重复执行不产生重复事件。
- 服务重启后事件和订单能够正确恢复。
- AKE 关键阶段能够进入预期状态。
- ARC 典型假突破不能进入确认仓。
- AI 服务中断不影响已有仓位管理。
- 所有硬风控测试保持通过。

进入 Mainnet Canary 前：

- Testnet 连续运行至少一个完整评估周期。
- Shadow/Signal/Testnet 三个阶段结果可追溯。
- 模型验证期净期望 R 高于基线。
- 假突破率低于旧策略。
- 最大回撤不高于配置门槛。
- 无重复订单、跨环境数据或状态恢复事故。

## 26. 最终安全边界

AI 可以决定：

- 哪个币值得观察。
- 当前属于哪种形态。
- 点火后延续和假突破概率。
- 建议试仓、确认或等待。
- 建议最大仓位因子。

AI 不能决定：

- 绕过数据完整性。
- 绕过账户和组合风险。
- 绕过同类型币限制。
- 突破最大杠杆和单笔风险。
- 使用过期信号下单。
- 重复消费同一事件。
- 取消硬止损。
- 直接调用交易所。

这使 AI 拥有策略意义上的开仓权，但不拥有破坏账户安全边界的权限。
