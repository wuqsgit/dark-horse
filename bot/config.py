"""
AlphaDog Crypto Bot - 配置文件
"""
import os

# === 交易所配置 ===
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TESTNET_API_KEY = os.getenv("TESTNET_API_KEY", "")
TESTNET_API_SECRET = os.getenv("TESTNET_API_SECRET", "")

# === 模式配置 ===
TEST_MODE = True  # True = 测试网, False = 实盘
SYMBOL = "BTCUSDT"  # 交易对
INTERVAL = "1m"  # K线周期

# === 风控参数 ===
MAX_POSITION_PCT = 0.10  # 最大仓位占总资金比例
MAX_SINGLE_LOSS_PCT = 0.02  # 单笔最大亏损比例
MAX_DAILY_LOSS_PCT = 0.05  # 每日最大亏损比例
STOP_LOSS_PCT = 0.015  # 止损比例 (1.5%)
TAKE_PROFIT_PCT = 0.03  # 止盈比例 (3%)

# === 信号参数 ===
SIGNAL_THRESHOLD = 75  # 入场信号分数阈值
MIN_VOLUME_24H = 1e8  # 24h最小成交量

# === 技术参数 ===
SCORE_WEIGHTS = {
    "fundamental": 0.30,
    "technical": 0.30,
    "sentiment": 0.20,
    "risk": 0.20,
}

# === 日志配置 ===
LOG_DIR = "./logs"
LOG_LEVEL = "INFO"