"""交易配置"""
import os
from pathlib import Path


def _load_local_env():
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()

EXCHANGE_CONFIG = {
    "api_key": os.getenv("BINANCE_API_KEY") or os.getenv("TESTNET_API_KEY", ""),
    "api_secret": os.getenv("BINANCE_API_SECRET") or os.getenv("TESTNET_API_SECRET", ""),
    "testnet": os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes", "on"),
}

TRADING_CONFIG = {
    # ── 资金管理 ──
    "total_capital": 5000,
    "position_size_pct": 0.20,          # 每仓占总资金 20%
    "position_multiplier": 1.0,        # 仓位倍数（基于评分动态调整）
    "risk_per_trade_pct": 0.015,        # 每仓风险预算 1.5%
    "max_positions": 3,

    # ── ATR 参数 ──
    "atr_multiplier_stop": 2.0,         # 止损 = ATR × 2.0
    "atr_multiplier_take_profit": 4.5,  # 止盈 = ATR × 4.5

    # ── 评分阈值（统一标准） ──
    "min_score": 60,                    # 🔧 统一开仓门槛从 50→60
    "consecutive_scans_required": 2,    # 🔧 连续 2 轮评分确认
    "max_signal_age_minutes": 15,       # V5: only trade fresh scan signals

    # ── 时间止损（alpha-prd.md §5.4.3） ──
    "time_stop_hours": 12,              # 🔧 持仓超 12h 检查
    "time_stop_min_return": 0.02,       # 🔧 12h 内浮盈 < 2% 则平仓

    # V3.0 Score Decay 评分衰减机制
    "score_decay_exit_full": 40,         # 评分衰减超过40分则全平
    "score_decay_exit_half": 30,         # 评分衰减超过30分则减半
    "score_decay_exit_qtr": 20,          # 评分衰减超过20分则减1/4

    # ── 相关性过滤 ──
    "correlation_groups": [
        ["BTC", "ETH", "SOL"],
        ["DOGE", "SHIB", "PEPE", "WIF"],
        ["LINK", "ATOM", "DOT", "NEAR"],
        ["XRP", "ADA", "TRX"],
    ],

    # ── 分批止盈 ──
    "tp1_pct": 0.50,                    # TP1 平 50%
    "tp2_pct": 0.50,                    # TP2 平剩余 50%（全部平完）
    "tp1_target_pct": 0.05,             # TP1 止盈 5%
    "tp2_target_pct": 0.10,             # TP2 止盈 10%
    "trailing_stop_atr_multiplier": 1.5,  # 移动止盈 = 最高点 - ATR×1.5

    # ── 硬止损 ──
    "hard_stop_pct": 0.05,             # 浮亏 5% 强制平仓（上限5%，ATR×1.5取小）

    # ── 调度 ──
    "rebalance_interval_min": 5,
    "leverage_max": 3,
    "spread_limits": {
        "prod": {
            "default": 0.0030,
            "accumulation": 0.0035,
            "breakout": 0.0035,
            "pullback": 0.0040,
            "momentum": 0.0050,
            "short_breakdown": 0.0040,
            "weak_short": 0.0040,
            "hard_max": 0.0060,
        },
        "testnet": {
            "default": 0.0050,
            "accumulation": 0.0045,
            "breakout": 0.0050,
            "pullback": 0.0055,
            "momentum": 0.0080,
            "short_breakdown": 0.0055,
            "weak_short": 0.0055,
            "hard_max": 0.0100,
        },
    },
    "alpha_trading": {
        "enabled": True,
        "testnet_only": True,
        "allow_short": True,
        "max_account_exposure": 0.30,
        "max_positions": 3,
        "max_normal_reviews_per_loop": 2,
        "min_score": 68,
        "signal_ttl_minutes": 45,
        "volume_price_ttl_minutes": 20,
        "normal_score_ttl_minutes": 15,
        "probe_max_position_pct": 0.30,
        "candidate_max_position_pct": 0.50,
        "cooldown_minutes": 30,
        "max_spread_pct": 0.008,
        "blocked_profiles": ["high_risk_watch"],
        "allowed_entry_levels": ["probe", "candidate"],
    },
}

HARD_FILTERS = {
    "min_volume_usdt": 1_000_000,
    "max_volatility_level": "正常",  # V3.1: 允许正常波动
    "disallowed_price_positions": ["overbought"],  # 仅阻止overbought
    "max_funding_rate": 0.001,
}

# ── Portfolio Risk Engine (alpha-prd.md §5.9) ──
PORTFOLIO_RISK = {
    "max_total_exposure_pct": 0.80,     # 总仓位不超过80%资金
    "max_single_exposure_pct": 0.30,    # 单币不超过30%资金
    "max_category_exposure_pct": 0.50,   # 同类(蓝/基本面/叙事/Meme)不超过50%
    "max_daily_loss_pct": 0.15,        # 日亏损超过15%停止开仓
    "max_consecutive_losses": 3,        # 连续3笔亏损停止开仓
}
