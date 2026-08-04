# Alpha Strategy V2 运行手册

## 安全默认值

新策略默认关闭，代码部署和数据库迁移不会触发新订单：

```dotenv
ALPHA_STRATEGY_V2_ENABLED=false
ALPHA_STRATEGY_V2_MODE=shadow
ALPHA_LEGACY_ENTRY_ENABLED=true
AI_EXECUTION_MODE=shadow
```

运行模式依次为：

```text
off → shadow → signal → testnet_live → mainnet_canary → mainnet_live
```

- `shadow`：生成特征、AI 样本和状态，不产生 Trader 可执行事件。
- `signal`：产生信号事件，各账户只记录 `SIGNAL_ONLY`。
- `testnet_live`：只允许测试网账户消费。
- `mainnet_canary`：主网小仓位消费，额外乘以 Canary 仓位因子。
- `mainnet_live`：主网正式消费。

`testnet_live` 必须搭配 `ALPHA_FUTURES_MARKET_ENV=testnet`；两个主网模式必须搭配 `mainnet`。配置不一致时 Worker 会拒绝启动。

## 部署后检查

```bash
./start.sh
curl -s http://127.0.0.1:8010/v2/alpha-strategy/status
curl -s http://127.0.0.1:8000/api/alpha-strategy/status
```

前端进入“Alpha 策略”，检查：

- Worker 心跳和最新闭合 15 分钟 K 线；
- Feature Ready 数、样本数和标注数；
- 当前状态、概率、突破位和失效位；
- 事件消费结果；
- Champion/Challenger、Drift 和告警。

采集频率：

- 1 分钟：新闭合 15 分钟 K 线检查、观察池盘口；
- 5 分钟：OI、Funding、Mark Price；
- 10 分钟：Universe 和全周期行情。
- Alpha/Futures 策略行情默认保留 90 天，可用
  `ALPHA_STRATEGY_RETENTION_DAYS` 调整；普通扫描行情仍按短周期保留。

## 历史回放

首次部署先补齐历史 K 线和 OI/Funding；历史盘口无法补造，会以缺失特征处理：

```bash
python3 tools/backfill_alpha_strategy_history.py AKEUSDT \
  --market-env mainnet --days 90
python3 tools/backfill_alpha_strategy_history.py ARCUSDT \
  --market-env mainnet --days 90
python3 tools/backfill_alpha_strategy_history.py BANKUSDT \
  --market-env mainnet --days 90
```

```bash
python3 tools/run_alpha_strategy_replay.py AKEUSDT \
  --market-env mainnet \
  --start 2026-07-15T00:00:00Z \
  --end 2026-07-28T00:00:00Z \
  --output /tmp/ake-alpha-v2.json
```

ARC、BANK 使用同一命令替换合约名。回放采用事件时钟，生产 Feature Builder 和 State Machine 只读取当前时点之前的数据；未来 K 线只用于标签。

## 标签、训练和模型切换

手动生成 Counterfactual 标签并同步真实成交结果：

```bash
curl -s -X POST http://127.0.0.1:8010/v2/alpha-strategy/outcomes/label \
  -H 'Content-Type: application/json' \
  -d '{"market_env":"mainnet","limit":1000}'
```

训练三个模型：

```bash
curl -s -X POST http://127.0.0.1:8010/v2/alpha-strategy/models/train \
  -H 'Content-Type: application/json' \
  -d '{"market_env":"mainnet","stage":"setup","target":"setup_success"}'

curl -s -X POST http://127.0.0.1:8010/v2/alpha-strategy/models/train \
  -H 'Content-Type: application/json' \
  -d '{"market_env":"mainnet","stage":"trigger","target":"followthrough"}'

curl -s -X POST http://127.0.0.1:8010/v2/alpha-strategy/models/train \
  -H 'Content-Type: application/json' \
  -d '{"market_env":"mainnet","stage":"trigger","target":"fakeout"}'
```

模型至少需要 1,000 个合格标签和 300 个验证样本。时间切分、24 小时 purge gap、PR-AUC、Brier、选中组净期望、假突破召回率和单特征支配检查不通过时不会发布。

新模型先成为 `challenger`。人工确认后晋升：

```bash
curl -s -X POST http://127.0.0.1:8010/v2/alpha-strategy/models/promote \
  -H 'Content-Type: application/json' \
  -d '{"version":"MODEL_VERSION"}'
```

回滚：

```bash
curl -s -X POST http://127.0.0.1:8010/v2/alpha-strategy/models/rollback \
  -H 'Content-Type: application/json' \
  -d '{"model_key":"alpha_trigger_v1_mainnet","target":"followthrough"}'
```

## 分阶段启用

1. 部署后开启 `ALPHA_STRATEGY_V2_ENABLED=true`，保持 `shadow`。
2. 样本和模型门槛通过后改为 `signal`，对照旧策略。
3. 在测试网使用 `testnet_live`，先验证试仓、确认加仓、回踩加仓和失效退出。
4. 测试网完整评估周期无重复订单、跨环境或恢复事故后，才切 `mainnet_canary`。
5. Canary 通过后再考虑 `mainnet_live`，并将 `ALPHA_LEGACY_ENTRY_ENABLED=false` 关闭旧 Alpha 开仓链路。

任何阶段都不应通过关闭硬止损、同类币限制或账户风险检查来“提高命中率”。
