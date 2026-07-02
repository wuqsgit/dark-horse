ALPHA_PROFILES = {
    "early_discovery": {
        "label": "早期发现型",
        "weights": {
            "discovery_score": 0.35,
            "liquidity_score": 0.25,
            "risk_score": 0.20,
            "momentum_score": 0.10,
            "tradeability_score": 0.10,
        },
        "thresholds": {
            "alpha_score": 68,
            "discovery_score": 70,
            "liquidity_score": 55,
            "risk_score": 60,
            "spread_pct": 0.8,
            "volume_growth_6h": 1.5,
            "abs_percent_change_24h": 30,
        },
    },
    "momentum_continuation": {
        "label": "动量延续型",
        "weights": {
            "momentum_score": 0.35,
            "liquidity_score": 0.25,
            "risk_score": 0.20,
            "discovery_score": 0.10,
            "tradeability_score": 0.10,
        },
        "thresholds": {
            "alpha_score": 72,
            "momentum_score": 65,
            "liquidity_score": 60,
            "risk_score": 55,
            "ret_1h_gt": 0,
            "ret_6h_gt": 0,
            "spread_pct": 0.7,
            "percent_change_24h_max": 45,
        },
    },
    "futures_mapped": {
        "label": "合约映射型",
        "weights": {
            "tradeability_score": 0.25,
            "liquidity_score": 0.25,
            "risk_score": 0.20,
            "momentum_score": 0.20,
            "discovery_score": 0.10,
        },
        "thresholds": {
            "alpha_score": 70,
            "liquidity_score": 60,
            "risk_score": 55,
            "spread_pct": 0.8,
        },
    },
    "high_risk_watch": {
        "label": "高风险观察型",
        "weights": {
            "risk_score": 0.40,
            "liquidity_score": 0.25,
            "tradeability_score": 0.15,
            "momentum_score": 0.10,
            "discovery_score": 0.10,
        },
        "thresholds": {
            "spread_pct_gt": 1.2,
            "risk_score_lt": 50,
            "liquidity_score_lt": 45,
            "abs_percent_change_24h_gt": 60,
            "range_24h_pct_gt": 80,
        },
    },
    "neutral_watch": {
        "label": "中性观察型",
        "weights": {
            "discovery_score": 0.20,
            "momentum_score": 0.20,
            "liquidity_score": 0.25,
            "risk_score": 0.25,
            "tradeability_score": 0.10,
        },
        "thresholds": {
            "alpha_score": 70,
            "liquidity_score": 60,
            "risk_score": 60,
            "spread_pct": 0.8,
        },
    },
}


PROFILE_LABELS = {name: cfg["label"] for name, cfg in ALPHA_PROFILES.items()}
ENTRY_LABELS = {
    "block": "禁止开仓",
    "observe": "观察",
    "probe": "小仓试探",
    "candidate": "Alpha 候选",
}


def weighted_alpha_score(scores, profile):
    weights = ALPHA_PROFILES.get(profile, ALPHA_PROFILES["neutral_watch"])["weights"]
    return sum(float(scores.get(k) or 0) * w for k, w in weights.items())


def classify_alpha_profile(scores, features):
    spread = float((features.get("depth") or {}).get("spread_pct") or 0)
    ret = features.get("returns") or {}
    volume = features.get("volume") or {}
    risk = features.get("risk") or {}
    pct24 = abs(float(ret.get("pct_24h") or 0))
    range24 = float(risk.get("range_24h_pct") or 0)
    volume_growth = float(volume.get("volume_growth_6h") or 1)
    tradeability = features.get("tradeability")

    if (
        spread > 1.2
        or float(scores.get("risk_score") or 0) < 50
        or float(scores.get("liquidity_score") or 0) < 45
        or pct24 > 60
        or range24 > 80
    ):
        return "high_risk_watch"

    if tradeability == "alpha_futures_mapped" and features.get("futures_symbol"):
        return "futures_mapped"

    if (
        volume_growth >= 1.5
        and pct24 <= 25
        and float(scores.get("risk_score") or 0) >= 60
        and float(scores.get("liquidity_score") or 0) >= 55
    ):
        return "early_discovery"

    if (
        float(ret.get("ret_1h") or 0) > 0
        and float(ret.get("ret_6h") or 0) > 0
        and volume_growth >= 1.0
        and float(ret.get("pct_24h") or 0) <= 40
        and float(scores.get("risk_score") or 0) >= 55
    ):
        return "momentum_continuation"

    return "neutral_watch"


def evaluate_alpha_entry(profile, alpha_score, scores, features):
    cfg = ALPHA_PROFILES.get(profile, ALPHA_PROFILES["neutral_watch"])
    thresholds = cfg["thresholds"]
    ret = features.get("returns") or {}
    volume = features.get("volume") or {}
    depth = features.get("depth") or {}
    risk = features.get("risk") or {}
    tradeability = features.get("tradeability")
    futures_symbol = features.get("futures_symbol")
    spread = float(depth.get("spread_pct") or 0)
    block_reasons = []

    def require(condition, reason):
        if not condition:
            block_reasons.append(reason)

    if profile == "high_risk_watch":
        if spread > thresholds["spread_pct_gt"]:
            block_reasons.append("盘口价差过宽")
        if float(scores.get("risk_score") or 0) < thresholds["risk_score_lt"]:
            block_reasons.append("风险分不足")
        if float(scores.get("liquidity_score") or 0) < thresholds["liquidity_score_lt"]:
            block_reasons.append("流动性不足")
        if abs(float(ret.get("pct_24h") or 0)) > thresholds["abs_percent_change_24h_gt"]:
            block_reasons.append("24h 涨跌幅过大")
        if float(risk.get("range_24h_pct") or 0) > thresholds["range_24h_pct_gt"]:
            block_reasons.append("24h 波动区间过大")
        return {
            "entry_level": "block",
            "suggested_position_pct": 0,
            "block_reasons": block_reasons or ["高风险观察模板命中"],
            "decision": "暂不开仓：Alpha 风险模板命中，只观察不交易。",
            "thresholds": thresholds,
        }

    if profile == "early_discovery":
        require(alpha_score >= thresholds["alpha_score"], "Alpha 分还没到早期发现线")
        require(float(scores.get("discovery_score") or 0) >= thresholds["discovery_score"], "发现分不足")
        require(float(scores.get("liquidity_score") or 0) >= thresholds["liquidity_score"], "流动性不足")
        require(float(scores.get("risk_score") or 0) >= thresholds["risk_score"], "风险分不足")
        require(spread <= thresholds["spread_pct"], "盘口价差偏大")
        require(float(volume.get("volume_growth_6h") or 1) >= thresholds["volume_growth_6h"], "成交额还没放大")
        require(abs(float(ret.get("pct_24h") or 0)) <= thresholds["abs_percent_change_24h"], "价格已经过热")
        return _entry_result(
            block_reasons,
            "probe",
            0.25,
            "早期发现型：基础条件成立，可小仓试探；不强制等突破前高。",
            "观察：早期发现条件还不完整。",
            thresholds,
        )

    if profile == "momentum_continuation":
        require(alpha_score >= thresholds["alpha_score"], "Alpha 分还没到动量延续线")
        require(float(scores.get("momentum_score") or 0) >= thresholds["momentum_score"], "动量分不足")
        require(float(scores.get("liquidity_score") or 0) >= thresholds["liquidity_score"], "流动性不足")
        require(float(scores.get("risk_score") or 0) >= thresholds["risk_score"], "风险分不足")
        require(float(ret.get("ret_1h") or 0) > thresholds["ret_1h_gt"], "1h 动量未转强")
        require(float(ret.get("ret_6h") or 0) > thresholds["ret_6h_gt"], "6h 动量未转强")
        require(spread <= thresholds["spread_pct"], "盘口价差偏大")
        require(float(ret.get("pct_24h") or 0) <= thresholds["percent_change_24h_max"], "24h 涨幅偏高")
        level = "candidate" if alpha_score >= 78 and float(scores.get("risk_score") or 0) >= 65 else "probe"
        pct = 0.40 if level == "candidate" else 0.30
        return _entry_result(
            block_reasons,
            level,
            pct,
            "动量延续型：趋势和流动性可用，可进入 Alpha 候选。",
            "观察：动量延续条件还不完整。",
            thresholds,
        )

    if profile == "futures_mapped":
        require(tradeability == "alpha_futures_mapped", "未映射 Binance Futures")
        require(bool(futures_symbol), "没有可用 futures_symbol")
        require(alpha_score >= thresholds["alpha_score"], "Alpha 分还没到合约映射候选线")
        require(float(scores.get("liquidity_score") or 0) >= thresholds["liquidity_score"], "流动性不足")
        require(float(scores.get("risk_score") or 0) >= thresholds["risk_score"], "风险分不足")
        require(spread <= thresholds["spread_pct"], "Alpha 盘口价差偏大")
        return _entry_result(
            block_reasons,
            "candidate",
            0.30,
            "合约映射型：可进入 Alpha 小仓候选，下单前仍需 Binance Futures 盘口和账户风控确认。",
            "观察：已映射合约，但还缺少 Alpha 候选条件。",
            thresholds,
        )

    require(alpha_score >= thresholds["alpha_score"], "Alpha 分还没到观察候选线")
    require(float(scores.get("liquidity_score") or 0) >= thresholds["liquidity_score"], "流动性不足")
    require(float(scores.get("risk_score") or 0) >= thresholds["risk_score"], "风险分不足")
    require(spread <= thresholds["spread_pct"], "盘口价差偏大")
    return _entry_result(
        block_reasons,
        "observe",
        0,
        "中性观察型：暂只观察，不进入实盘候选。",
        "观察：Alpha 条件还不完整。",
        thresholds,
    )


def _entry_result(block_reasons, pass_level, position_pct, pass_text, fail_text, thresholds):
    if block_reasons:
        return {
            "entry_level": "observe",
            "suggested_position_pct": 0,
            "block_reasons": block_reasons,
            "decision": fail_text + " 缺口：" + "；".join(block_reasons[:3]),
            "thresholds": thresholds,
        }
    return {
        "entry_level": pass_level,
        "suggested_position_pct": position_pct,
        "block_reasons": [],
        "decision": pass_text,
        "thresholds": thresholds,
    }
