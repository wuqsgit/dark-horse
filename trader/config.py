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

_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes", "on")

EXCHANGE_CONFIG = {
    "api_key": os.getenv("TESTNET_API_KEY" if _TESTNET else "BINANCE_API_KEY", ""),
    "api_secret": os.getenv("TESTNET_API_SECRET" if _TESTNET else "BINANCE_API_SECRET", ""),
    "testnet": _TESTNET,
}

TRADING_CONFIG = {
    # ── 资金管理 ──
    "total_capital": 5000,
    "position_size_pct": 0.20,          # 每仓占总资金 20%
    "position_multiplier": 1.0,        # 仓位倍数（基于评分动态调整）
    "risk_per_trade_pct": 0.015,        # 每仓风险预算 1.5%
    "max_positions": 5,

    # ── ATR 参数 ──
    "atr_multiplier_stop": 2.0,         # 止损 = ATR × 2.0
    "atr_multiplier_take_profit": 4.5,  # 止盈 = ATR × 4.5

    # ── 评分阈值（统一标准） ──
    "min_score": 60,                    # 🔧 统一开仓门槛从 50→60
    "consecutive_scans_required": 2,    # 🔧 连续 2 轮评分确认
    "max_signal_age_minutes": 45,       # V5: keep fresh enough without skipping normal scan cadence

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
    "hard_stop_pct": 0.12,             # 浮亏 12% 强制平仓

    # ── 调度 ──
    "soft_exit_profit_pct": 2.0,
    "soft_exit_max_loss_pct": 3.5,
    "rebalance_interval_min": 5,
    "leverage_max": 8,
    "position_sizing": {
        "core_bluechip": {
            "leverage_min": 4,
            "leverage_base": 6,
            "leverage_max": 8,
            "atr_leverage_steps": [[0.008, 8], [0.015, 7], [0.025, 6], [0.040, 5]],
            "atr_stop_multiplier": 2.0,
            "min_stop_pct": 0.025,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.06,
            "confirmed_margin_pct": 0.12,
            "strong_margin_pct": 0.18,
            "max_margin_pct": 0.20,
            "risk_per_trade_pct": 0.025,
            "min_effective_margin_pct": 0.05,
            "min_effective_stop_pct": 0.035
        },
        "large_cap": {
            "leverage_min": 3,
            "leverage_base": 4,
            "leverage_max": 5,
            "atr_leverage_steps": [[0.010, 5], [0.020, 4], [0.040, 3]],
            "atr_stop_multiplier": 2.5,
            "min_stop_pct": 0.035,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.04,
            "confirmed_margin_pct": 0.08,
            "strong_margin_pct": 0.10,
            "max_margin_pct": 0.12,
            "risk_per_trade_pct": 0.020,
            "min_effective_margin_pct": 0.035,
            "min_effective_stop_pct": 0.040
        },
        "fundamental": {
            "leverage_min": 2,
            "leverage_base": 3,
            "leverage_max": 4,
            "atr_leverage_steps": [[0.012, 4], [0.025, 3], [0.050, 2]],
            "atr_stop_multiplier": 2.5,
            "min_stop_pct": 0.035,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.03,
            "confirmed_margin_pct": 0.06,
            "strong_margin_pct": 0.08,
            "max_margin_pct": 0.10,
            "risk_per_trade_pct": 0.018,
            "min_effective_margin_pct": 0.025,
            "min_effective_stop_pct": 0.045
        },
        "narrative": {
            "leverage_min": 2,
            "leverage_base": 2,
            "leverage_max": 3,
            "atr_leverage_steps": [[0.015, 3], [0.040, 2]],
            "atr_stop_multiplier": 2.5,
            "min_stop_pct": 0.035,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.025,
            "confirmed_margin_pct": 0.05,
            "strong_margin_pct": 0.06,
            "max_margin_pct": 0.08,
            "risk_per_trade_pct": 0.015,
            "min_effective_margin_pct": 0.020,
            "min_effective_stop_pct": 0.050
        },
        "meme": {
            "leverage_min": 1,
            "leverage_base": 1,
            "leverage_max": 2,
            "atr_leverage_steps": [[0.020, 2]],
            "atr_stop_multiplier": 3.5,
            "min_stop_pct": 0.070,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 2.0,
            "probe_margin_pct": 0.015,
            "confirmed_margin_pct": 0.03,
            "strong_margin_pct": 0.035,
            "max_margin_pct": 0.05,
            "risk_per_trade_pct": 0.010,
            "min_effective_margin_pct": 0.010,
            "min_effective_stop_pct": 0.055
        },
        "alpha": {
            "leverage_min": 2,
            "leverage_base": 2,
            "leverage_max": 3,
            "atr_leverage_steps": [[0.015, 3], [0.050, 2]],
            "atr_stop_multiplier": 3.0,
            "min_stop_pct": 0.050,
            "hard_stop_pct": 0.10,
            "trailing_atr_multiplier": 2.0,
            "probe_margin_pct": 0.02,
            "confirmed_margin_pct": 0.05,
            "strong_margin_pct": 0.06,
            "max_margin_pct": 0.07,
            "risk_per_trade_pct": 0.012,
            "min_effective_margin_pct": 0.018,
            "min_effective_stop_pct": 0.050
        },
    },
    "spread_limits": {
        "prod": {
            "default": 0.0030,
            "bluechip_trend": 0.0025,
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
            "bluechip_trend": 0.0050,
            "accumulation": 0.0045,
            "breakout": 0.0050,
            "pullback": 0.0055,
            "momentum": 0.0080,
            "short_breakdown": 0.0055,
            "weak_short": 0.0055,
            "hard_max": 0.0100,
        },
    },
    "bluechip_trend": {
        "enabled": True,
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "max_positions": 1,
        "probe_size_factor": 0.25,
        "confirmed_size_factor": 0.40,
        "min_score": 55,
        "min_entry_alpha": 45,
        "min_relative_strength": 45,
        "min_return_24h": 0.005,
        "min_ema20_50_ratio": 1.001,
        "min_support_score": 55,
        "min_depth_score": 20,
        "min_big_order_score": 25,
        "max_funding_rate": 0.001,
        "max_rsi": 82,
        "max_price_position_value": 0.95,
        "confirmed_score": 60,
        "confirmed_entry_alpha": 50,
        "confirmed_relative_strength": 58,
        "confirmed_trend_score": 68,
        "hard_stop_pct": 0.12,
        "time_stop_hours": 6,
        "time_stop_min_return": 0.008,
        "tp1_target_pct": 0.035,
        "tp2_target_pct": 0.070,
        "tp1_pct": 0.50,
        "tp2_pct": 0.30,
        "exit_min_entry_alpha": 35,
    },
    "alpha_trading": {
        "enabled": True,
        "testnet_only": False,
        "allow_short": False,
        "max_account_exposure": 0.30,
        "max_positions": 3,
        "max_normal_reviews_per_loop": 2,
        "min_score": 68,
        "signal_ttl_minutes": 75,
        "volume_price_ttl_minutes": 45,
        "normal_score_ttl_minutes": 45,
        "probe_max_position_pct": 0.30,
        "candidate_max_position_pct": 0.50,
        "cooldown_minutes": 30,
        "max_spread_pct": 0.008,
        "position_probe_timeout_hours": 1.0,
        "position_probe_min_progress_pct": 3.0,
        "position_min_trend_score": 50,
        "position_soft_exit_profit_pct": 2.0,
        "position_profit_protect_close_pct": 0.25,
        "position_hard_stop_pct": 0.10,
        "post_close_cooldown_minutes": 45,
        "loss_cooldown_minutes": 120,
        "stop_cooldown_minutes": 180,
        "blocked_profiles": ["high_risk_watch"],
        "allowed_entry_levels": ["probe", "candidate"],
    },
    "roll_trading": {
        "enabled": True,
        "max_layers": 2,
        "size_factors": [0.50, 0.25],
        "min_profit_pct": 5.0,
        "min_profit_r": 1.0,
        "cooldown_minutes": 60,
        "max_giveback_pct": 35.0,
        "max_15m_return_pct": 4.0,
        "max_1h_return_pct": 8.0,
        "max_spread_pct": 0.0012,
        "alpha_size_factor": 0.50,
        "spread_degraded_size_factor": 0.50,
        "lock_profit_pct": 0.30,
        "allowed_normal_keywords": ["trend", "breakout", "pullback", "momentum", "趋势", "突破", "回踩", "动量"],
        "allowed_alpha_states": ["breakout_pullback", "accumulation_volume"],
        "blocked_alpha_profiles": ["high_risk_watch"],
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
