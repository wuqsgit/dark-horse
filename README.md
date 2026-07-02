# Dark Horse 🐎

一套会自己扫币、打分、回测、复盘、实盘执行的加密货币交易系统。

它不是“点一下就财务自由”的神灯，也不是“今晚梭哈明早游艇”的玄学按钮。Dark Horse 更像一个很勤快的交易助理：白天扫盘，晚上复盘，看到机会先问三遍“盘口靠谱吗？风险够不够？别追高行不行？”然后才考虑动手。

> 当前系统默认面向 Binance Futures Testnet。真金白银上场前，请先让它在测试网里多跑几圈，别让钱包替策略交学费。

## 它能干啥？✨

### 1. 自动扫币：不再只盯几个老熟人

系统会持续扫描币种池，给每个币打分、排序、分类：

- 普通合约扫描：适合偏稳的主流/热门合约。
- Alpha 扫描：专门盯 Binance Alpha 里可能冒头的新币。
- 按币种特征分类：趋势、突破、回踩、低位蓄力、过热风险等。

一句话：它会努力在一堆“看起来都很热闹”的币里，挑出真正可能有戏的家伙。

### 2. Alpha 量价策略：先看量价，再谈理想

Alpha 模块现在走独立逻辑，不再被普通交易评分硬复核。

它重点看：

- 15m / 1h / 6h 涨跌结构
- 6h 成交量放大
- 距离 24h 高点的回撤
- 盘口价差 spread
- 买卖盘深度和承接
- 是否过热、是否追高、是否只适合观察

只有 Alpha 自己的量价评分过线，才会进入实盘执行检查。换句话说：Alpha 不再拿普通合约模板当尺子硬量自己，终于不用穿不合脚的鞋了。

### 3. 实盘执行：会动手，但不是手痒就动手

实盘 runner 会自动处理：

- 开仓
- 平仓
- 部分止盈
- 移动止盈
- Alpha 独立开仓
- 普通交易和 Alpha 交易开关
- 当前持仓同步
- 历史交易聚合
- Binance Testnet 接口校验

页面里也能看到“系统刚才为什么没动手”。这块非常重要，因为交易系统最有价值的能力之一，不是瞎冲，而是会说：

“等等，这个 spread 有点宽，先别上头。”

### 4. 滚仓机制：盈利仓才有资格加戏

系统支持滚仓 V1，但规则很保守：

- 必须盈利达到触发线
- 必须已经 TP1 锁过一部分利润
- 最多滚 2 层
- 第 1 层加当前仓位 50%
- 第 2 层加当前仓位 25%
- Alpha 仓位滚仓再打折
- 新增风险不能超过允许回吐的浮盈
- 回撤过大、短线过热、盘口不行，直接不滚

滚仓的核心不是“越涨越上头”，而是“用已经赚到的钱，谨慎地多吃一口趋势”。优雅一点，别像饿了三天。

### 5. 回测和复盘：不是摆设，是给系统长脑子的

系统内置回测、复盘、因子分析和策略学习闭环：

- 回测概览
- 最近信号表现
- 因子表现分析
- 复盘问题列表
- 有效做法沉淀
- 候选策略影子验证
- 策略学习规则生效/拒绝

目标是让系统从历史样本里学到：哪些评分有用，哪些条件是错觉，哪些开仓像“看起来很努力，其实很危险”。

## 页面模块 🖥️

前端是一个 Vite + React 控制台，主要页面包括：

- 扫描列表：普通交易候选币和评分明细。
- Alpha 扫描：Alpha 币种、量价状态、执行原因。
- 回测概览：回测数据、信号表现、因子和复盘。
- 实盘交易：账户统计、当前持仓、历史交易、交易开关、系统未动手原因。

整体风格偏“黑客驾驶舱”，但希望你用它的时候像驾驶员，不像赌场门口排队的人。

## 项目结构 🧭

```text
dark-horse/
├── api/                 # FastAPI 后端接口
├── frontend/            # React + Vite 前端
├── trader/              # 实盘 runner、执行、风控、交易所适配
├── engine/              # 普通评分、回测、因子分析
├── pipeline/            # 普通行情采集
├── alpha_pipeline/      # Alpha 行情、盘口、K线采集
├── alpha_engine/        # Alpha 评分和量价策略
├── shared/              # SQLite 数据层和公共逻辑
├── configs/             # 策略配置
├── strategies/          # 币种分类和策略模板
└── alphadog.db          # 本地 SQLite 数据库，不建议提交
```

## 快速启动 🚀

### 1. 准备环境

复制环境变量模板：

```bash
cp .env.example .env
```

填入 Binance Testnet API Key：

```env
BINANCE_TESTNET=true
TESTNET_API_KEY=your_testnet_key
TESTNET_API_SECRET=your_testnet_secret
```

### 2. 启动后端 API

```bash
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 3000
```

打开：

```text
http://127.0.0.1:3000/
```

### 4. 启动普通扫描和评分

```bash
python pipeline/main.py
python engine/run.py
```

### 5. 启动 Alpha 扫描

建议用模块方式启动，Windows 下尤其重要：

```bash
python -m alpha_pipeline.main
python -m alpha_engine.run
```

### 6. 启动实盘 runner

```bash
python -m trader.runner
```

然后去页面里的“实盘交易”打开或关闭：

- 普通交易
- Alpha 交易

开关实时生效；关闭某类交易时，如果已有对应仓位，系统会尝试平掉。

## 常用接口 🔌

```text
GET  /api/scan/latest
GET  /api/scan/by_symbol/{symbol}
GET  /api/alpha/scan/latest
GET  /api/alpha/scan/by_symbol/{alpha_symbol}
GET  /api/backtest/summary
GET  /api/backtest/review
GET  /api/backtest/factor_analysis
GET  /api/trading/status
GET  /api/trading/controls
POST /api/trading/controls
```

## 当前策略原则 🧠

Dark Horse 的原则很朴素：

- 不追短线暴拉。
- 不碰盘口太宽的币。
- Alpha 重点看量价，不拿普通评分硬套。
- 开仓前看实时 Binance 盘口。
- 盈利仓先保护，再考虑滚仓。
- 回测和复盘持续喂给策略学习。
- 宁可错过，也别把测试网跑成情绪过山车。

## 风险提示 ⚠️

这不是投资建议。

这不是稳赚系统。

这也不是“打开 README 就开始赚钱”的神秘卷轴。

加密货币波动极大，合约交易尤其容易把人教育得很立体。请务必：

- 先跑 Testnet。
- 小仓验证。
- 看日志。
- 看复盘。
- 看历史交易。
- 不要把配置文件里的 `testnet=true` 改成 `false` 以后还假装自己很冷静。

## 开发者备注 🛠️

本项目使用本地 SQLite 数据库，适合快速迭代和测试。生产化前建议重点处理：

- 数据库迁移管理
- 任务进程守护
- API 鉴权
- 日志归档
- 参数版本管理
- 回测和实盘数据隔离
- 风控熔断
- 密钥管理

## 一句话总结 🐎

Dark Horse 是一个会扫币、会复盘、会开关交易、会盯盘口、会用量价挑 Alpha、还会在盈利时谨慎滚仓的交易系统。

它不保证抓住每一匹黑马，但至少会尽量避免把斑马刷成黑马。 😄
