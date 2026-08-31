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

_DEFAULT_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes", "on")

TRADING_CONFIG = {
    # ── 资金管理 ──
    "total_capital": 5000,
    "position_size_pct": 0.20,          # 每仓占总资金 20%
    "position_multiplier": 1.0,        # 仓位倍数（基于评分动态调整）
    "risk_per_trade_pct": 0.0075,       # 每仓最大初始风险 0.75%（1R）
    "max_positions": 5,

    # ── ATR 参数 ──
    "atr_multiplier_stop": 2.0,         # 止损 = ATR × 2.0
    "atr_multiplier_take_profit": 4.5,  # 止盈 = ATR × 4.5

    # ── 评分阈值（统一标准） ──
    "min_score": 60,                    # 🔧 统一开仓门槛从 50→60
    "consecutive_scans_required": 2,    # 🔧 连续 2 轮评分确认
    "max_signal_age_minutes": 75,       # Allow for collection/scoring jitter without accepting old signals

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

    # ── 兜底止损 ──
    # 正常止损由开仓时的结构 1R 决定；该阈值只处理历史仓位/保护单异常。
    "emergency_margin_stop_pct": 0.20,

    # ── 调度 ──
    "soft_exit_profit_pct": 2.0,
    "soft_exit_max_loss_pct": 3.5,
    "normal_soft_exit": {
        "cooldown_minutes": 60,
        "strong_trend_close_pct": 0.20,
        "weak_trend_close_pct": 0.25,
    },
    "rebalance_interval_min": 5,
    "leverage_max": 8,
    "dynamic_leverage": {
        "target_margin_loss_pct": 0.20,
        "atr_stop_multiplier": 2.0,
        "min_stop_pct": 0.025,
        "max_stop_pct": 0.10,
        "min_leverage": 2,
        "max_leverage": 8,
        "symbol_caps": {
            "BTCUSDT": 8,
            "ETHUSDT": 6,
            "SOLUSDT": 5,
            "LINKUSDT": 5,
            "AAVEUSDT": 5,
        },
    },
    "position_sizing": {
        "core_bluechip": {
            "leverage_max": 8,
            "atr_stop_multiplier": 2.0,
            "min_stop_pct": 0.025,
            "max_stop_pct": 0.050,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.075,
            "confirmed_margin_pct": 0.10,
            "strong_margin_pct": 0.15,
            "max_margin_pct": 0.15,
            "risk_per_trade_pct": 0.0075,
            "strong_risk_per_trade_pct": 0.0200,
            "min_effective_margin_pct": 0.05,
            "min_effective_stop_pct": 0.035
        },
        "large_cap": {
            "leverage_max": 5,
            "atr_stop_multiplier": 2.5,
            "min_stop_pct": 0.035,
            "max_stop_pct": 0.060,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.075,
            "confirmed_margin_pct": 0.10,
            "strong_margin_pct": 0.15,
            "max_margin_pct": 0.15,
            "risk_per_trade_pct": 0.0075,
            "min_effective_margin_pct": 0.035,
            "min_effective_stop_pct": 0.040
        },
        "fundamental": {
            "leverage_max": 4,
            "atr_stop_multiplier": 2.5,
            "min_stop_pct": 0.035,
            "max_stop_pct": 0.065,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.075,
            "confirmed_margin_pct": 0.10,
            "strong_margin_pct": 0.15,
            "max_margin_pct": 0.15,
            "risk_per_trade_pct": 0.0075,
            "min_effective_margin_pct": 0.025,
            "min_effective_stop_pct": 0.045
        },
        "narrative": {
            "leverage_max": 3,
            "atr_stop_multiplier": 2.5,
            "min_stop_pct": 0.035,
            "max_stop_pct": 0.075,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 1.5,
            "probe_margin_pct": 0.075,
            "confirmed_margin_pct": 0.10,
            "strong_margin_pct": 0.15,
            "max_margin_pct": 0.15,
            "risk_per_trade_pct": 0.00675,
            "min_effective_margin_pct": 0.020,
            "min_effective_stop_pct": 0.050
        },
        "meme": {
            "leverage_max": 3,
            "atr_stop_multiplier": 3.5,
            "min_stop_pct": 0.070,
            "max_stop_pct": 0.100,
            "hard_stop_pct": 0.12,
            "trailing_atr_multiplier": 2.0,
            "probe_margin_pct": 0.075,
            "confirmed_margin_pct": 0.10,
            "strong_margin_pct": 0.15,
            "max_margin_pct": 0.15,
            "risk_per_trade_pct": 0.00525,
            "min_effective_margin_pct": 0.010,
            "min_effective_stop_pct": 0.055
        },
        "alpha": {
            "leverage_max": 3,
            "atr_stop_multiplier": 3.0,
            "min_stop_pct": 0.050,
            "max_stop_pct": 0.080,
            "hard_stop_pct": 0.10,
            "trailing_atr_multiplier": 2.0,
            "probe_margin_pct": 0.075,
            "confirmed_margin_pct": 0.10,
            "strong_margin_pct": 0.15,
            "max_margin_pct": 0.15,
            "risk_per_trade_pct": 0.0075,
            "strong_risk_per_trade_pct": 0.0200,
            "probe_risk_per_trade_pct": 0.0045,
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
        "aggregate_confirmed_score": 75,
        "aggregate_confirmed_base_score": 60,
        "aggregate_confirmed_relative_strength": 70,
        "aggregate_confirmed_return_24h": 0.005,
        "aggregate_confirmed_oi_change": 0.0,
        "aggregate_confirmed_support_score": 55,
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
        "min_score": 78,
        "full_position_score": 80,
        "entry_confirmation": {
            "min_trend_score": 72,
            "min_alpha_volume_6h": 1.8,
            "min_futures_volume_6h": 1.5,
            "min_oi_4h": 0.0,
            "min_oi_24h": -0.01,
            "oi_waiver_floor_4h": -0.005,
            "waiver_alpha_volume_6h": 3.0,
            "waiver_futures_volume_6h": 2.0,
            "require_15m_breakout_confirmation": True,
        },
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
        "position_hard_stop_pct": 0.10,  # 当前仓位保证金亏损 10% 强制平仓
        "emergency_margin_stop_pct": 0.20,
        "trend_management": {
            "enabled": True,
            # Only fully closed 15m bars can trigger these rules.
            "weak_lookback_candles": 3,
            "weak_close_pct": 0.30,
            "weak_cooldown_minutes": 30,
            "structure_lookback_candles": 3,
        },
        "profit_lock": {
            "enabled": True,
            # All thresholds below are margin ROI percentages. They are kept
            # separate from the unlevered price-return/R-multiple model.
            "arm_peak_roi": 6.0,
            "arm_lock_roi": 0.5,
            "stage1_peak_roi": 10.0,
            "stage1_close_pct": 0.25,
            "stage1_lock_roi": 2.0,
            "stage2_peak_roi": 15.0,
            "stage2_close_pct": 0.20,
            "stage2_lock_roi": 6.0,
            "runner_peak_roi": 25.0,
            "runner_lock_ratio": 0.50,
            "runner_giveback_ratio": 0.35,
            # A profitable partial exit creates a trade-level budget. The
            # remaining position may give back at most this share of it.
            "realized_profit_giveback_ratio": 0.35,
            "realized_profit_min_usdt": 0.50,
            "realized_profit_min_margin_ratio": 0.01,
            # Spike-and-stall protection uses only fully closed 15m candles.
            "stall_peak_roi": 8.0,
            "stall_close_pct": 0.30,
            "stall_lock_ratio": 0.40,
            "stall_lookback_candles": 3,
            "stall_rearm_atr": 1.0,
            "stall_max_trend_score": 60,
            "explosive_runner": {
                "enabled": True,
                "min_score": 88.0,
                "grace_minutes": 60,
                "stage1_close_pct": 0.20,
                "min_remaining_fraction": 0.40,
                "pre_runner_giveback_ratio": 0.60,
                "mature_giveback_ratio": 0.50,
                "atr_multiplier": 2.50,
                "reentry_min_score": 88.0,
                "reentry_position_factor": 0.50,
                "reentry_used_cooldown_minutes": 180,
            },
        },
        "post_close_cooldown_minutes": 45,
        "loss_cooldown_minutes": 120,
        "stop_cooldown_minutes": 180,
        "blocked_profiles": ["high_risk_watch"],
        "allowed_entry_levels": ["probe", "candidate"],
    },
    "alpha_strategy_v2": {
        "enabled": os.getenv("ALPHA_STRATEGY_V2_ENABLED", "false").lower()
        in ("1", "true", "yes", "on"),
        "mode": os.getenv("ALPHA_STRATEGY_V2_MODE", "shadow").strip().lower(),
        "market_env": os.getenv(
            "ALPHA_FUTURES_MARKET_ENV",
            "mainnet",
        ).strip().lower(),
        "worker_interval_seconds": 60,
        "closed_bar_delay_seconds": 5,
        "feature_schema_version": 4,
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
        "trigger_pending_bars": 2,
        "early_probe_stage_cap": 0.15,
        "recovery_max_bars": 96,
        "probe_stage_cap": 0.30,
        "sentiment_reversal_stage_cap": 0.50,
        "confirmed_stage_cap": 0.70,
        "retest_stage_cap": 1.00,
        "mainnet_canary_factor": 0.25,
        "max_alpha_positions": 2,
        "signal_ttl_minutes": 90,
        "legacy_alpha_entry_enabled": os.getenv(
            "ALPHA_LEGACY_ENTRY_ENABLED",
            "false",
        ).lower() in ("1", "true", "yes", "on"),
        "ai_timeout_ms": 300,
        "ai_failure_mode": "hold_state",
        "testnet_live_rule_fallback": os.getenv(
            "ALPHA_V2_TESTNET_RULE_FALLBACK",
            "true",
        ).lower() in ("1", "true", "yes", "on"),
    },
    "roll_trading": {
        "enabled": True,
        "max_layers": 3,
        "short_max_layers": 2,
        "trigger_r": 1.5,
        # Every profitable position can pyramid as R expands. A healthy
        # pullback/recovery remains an alternative trigger for later layers.
        "layer_trigger_r": [1.5, 2.5, 3.5],
        "add_initial_qty_pct": 0.25,
        "layer_add_initial_qty_pct": [0.25, 0.20, 0.15],
        "short_layer_add_initial_qty_pct": [0.50, 0.35],
        "max_total_qty_multiple": 1.10,
        "short_max_total_qty_multiple": 1.50,
        "repeat_pullback_atr": 0.75,
        "repeat_recover_to_peak_atr": 0.25,
        "repeat_min_r": 1.0,
        "min_minutes_between_layers": 20,
        "break_even_buffer_pct": 0.0015,
        "trailing_atr_multiplier": 2.0,
        "min_remaining_margin": 5.0,
        "min_notional_multiplier": 1.5,
        "explosive_trigger_r": 0.75,
        "explosive_min_score": 88.0,
        "explosive_add_initial_qty_pct": 0.50,
        "explosive_max_total_qty_multiple": 2.0,
    },
    "short_trading": {
        "enabled": True,
        "min_entry_alpha": 65,
        "full_position_alpha": 72,
        "probe_position_factor": 0.50,
        "negative_funding_reduce_at": -0.001,
        "negative_funding_block_at": -0.003,
        "max_short_positions": 2,
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
    "max_positions_per_category": 1,    # 每个风险类别最多持有1个币
    "max_category_exposure_pct": 0.50,   # 同类(蓝/基本面/叙事/Meme)不超过50%
    "max_daily_loss_pct": 0.15,        # 日亏损超过15%停止开仓
    "max_consecutive_losses": 3,        # 连续3笔亏损停止开仓
}
