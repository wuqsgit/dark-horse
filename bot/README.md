# AlphaDog Crypto Bot

基于 AlphaDog 评分体系的自动交易机器人

## 结构

```
bot/
├── config.py       # 配置文件
├── trader.py       # 交易所接口 (Binance)
├── risk_manager.py # 风控模块
├── strategy.py    # 策略引擎
└── main.py        # 主程序
```

## 快速开始

### 1. 配置 API

Binance AK/SK 请在系统“实盘 → 账户管理”中配置。程序会从 `trading_accounts` 表读取并解密，不再读取 AK/SK 环境变量。

获取测试网API: https://testnet.binance.vision/

### 2. 修改配置

账户的测试网/实盘环境也在“账户管理”中选择。
- `SYMBOL = "BTCUSDT"` # 交易对
- 设置风控参数

### 3. 运行

```bash
# 立即执行一次交易检查
python3 main.py once

# 查看状态
python3 main.py status

# 持续运行
python3 main.py start
```

## 风控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| MAX_POSITION_PCT | 10% | 最大仓位占比 |
| MAX_DAILY_LOSS_PCT | 5% | 每日最大亏损 |
| STOP_LOSS_PCT | 1.5% | 止损线 |
| TAKE_PROFIT_PCT | 3% | 止盈线 |
| SIGNAL_THRESHOLD | 75 | 入场信号分数 |

## 模拟运行

当前 `TEST_MODE = True`，只会打印信号，不会实际下单。

切换到实盘:
```python
# config.py
TEST_MODE = False
```

## ⚠️ 风险警告

- 加密货币交易风险极高
- 可能损失全部资金
- 建议先用测试网跑至少1个月
- 实盘资金不要超过可承受亏损的范围
