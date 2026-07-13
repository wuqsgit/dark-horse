"""AlphaDog Scoring Engine v2 — Alpha Score

核心改进（对应 alpha-prd.md §4.4）：
1. Alpha Score 替代状态评分：P(return_12h>5%) × 0.4 + P(return_24h>10%) × 0.3 - P(max_dd>8%) × 0.3
2. EV 评价体系：胜率 × 平均盈 - 败率 × 平均亏
3. 趋势阶段识别：accumulation / breakout / trend / euphoria / distribution
4. 市场环境因子
5. 取消 Meme/Narrative 权重补偿
"""
import json, logging, os, uuid
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import pandas as pd

from shared.db import insert_training_samples, get_conn
from shared.market_phase import detect_market_phase
from engine.structure_metrics import compute_structure_metrics
from engine.factor_engine import compute_score_layers

class ScoringEngine:
    def __init__(self, weights_path=None):
        self.scan_id = uuid.uuid4().hex[:12]
        self.weights_path = weights_path or os.path.join(os.path.dirname(__file__), "factor_weights.json")
        self._load_weights()

    def _load_weights(self):
        d = {"version": 2,
             "alpha_formula": {"w_return_12h": 0.40, "w_return_24h": 0.30, "w_max_drawdown": 0.30},
             "custom_factors": {},
             "sub_weights": {"vol_quality": 0.20, "chip": 0.25, "absorption": 0.15,
                            "position": 0.10, "rsi": 0.12, "atr": 0.08, "vol_ratio": 0.10},
             "category_weights": {"technical": 0.50, "futures": 0.25, "onchain": 0.15,
                                  "heat": 0.10, "market_regime": 0.15}}
        try:
            with open(self.weights_path) as f: loaded = json.load(f)
            self.sub_w = {**d["sub_weights"], **(loaded.get("sub_weights") or {})}
            self.cat_w = {**d["category_weights"], **(loaded.get("category_weights") or {})}
            self.custom_factors = loaded.get("custom_factors") or {}
            self.alpha_formula = loaded.get("alpha_formula") or d["alpha_formula"]
        except:
            self.sub_w = d["sub_weights"]; self.cat_w = d["category_weights"]
            self.custom_factors = {}; self.alpha_formula = d["alpha_formula"]

    def _get_custom_score(self, tech, factor_name, config):
        src = config.get("source", "")
        raw = tech.get(src, config.get("default", 50))
        if raw is None: return config.get("default", 50)
        m = config.get("mapping", "thresholds")
        if m == "direct": return max(0, min(100, float(raw)))
        if m == "formula": return max(0, min(100, config.get("a",0)*float(raw)+config.get("b",50)))
        th = config.get("thresholds", [])
        if not th: return config.get("default", 50)
        for t in sorted(th, key=lambda x: x.get("max", 999)):
            if float(raw) <= t.get("max", 999): return t.get("score", 50)
        return th[-1].get("score", 50)

    def _compute_custom_factors(self, tech):
        r = {}
        for n, c in self.custom_factors.items():
            w = c.get("weight", 0)
            if w > 0: r[n] = {"score": self._get_custom_score(tech, n, c), "weight": w}
        return r

    def _percentile_map(self, values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        items = sorted(values.items(), key=lambda x: x[1])
        n = len(items)
        if n == 1:
            return {items[0][0]: 50.0}
        return {symbol: round(i / (n - 1) * 100, 1) for i, (symbol, _) in enumerate(items)}

    def _compute_market_strengths(self, results: list[dict]) -> dict[str, float]:
        values = {}
        for r in results:
            tech = (r.get("raw_features") or {}).get("technical", {})
            fut = (r.get("raw_features") or {}).get("futures", {})
            ret_6h = float(tech.get("return_6h") or 0)
            ret_24h = float(tech.get("return_24h") or 0)
            volume_change = float(tech.get("volume_change_pct") or 0)
            oi_change = float(fut.get("oi_change_pct") or 0)
            vol_quality = (float(tech.get("vol_quality_score") or 50) - 50) / 100
            values[r["symbol"]] = (
                ret_6h * 0.45
                + ret_24h * 0.35
                + max(-1.0, min(1.5, volume_change)) * 0.08
                + max(-0.5, min(0.5, oi_change)) * 0.07
                + vol_quality * 0.05
            )
        return self._percentile_map(values)

    def score_all(self, df_1h, df_15m, df_6h, df_24h, df_futures, df_onchain):
        now = datetime.now(tz=timezone.utc)
        results = []
        for sym in df_1h["symbol"].unique():
            try:
                c1h = df_1h[df_1h["symbol"]==sym].sort_values("time")
                c15m = df_15m[df_15m["symbol"]==sym].sort_values("time") if not df_15m.empty else pd.DataFrame()
                c6h = df_6h[df_6h["symbol"]==sym].sort_values("time") if not df_6h.empty else pd.DataFrame()
                c24h = df_24h[df_24h["symbol"]==sym].sort_values("time") if not df_24h.empty else pd.DataFrame()
                fut = df_futures[df_futures["symbol"]==sym].sort_values("time") if not df_futures.empty else pd.DataFrame()
                tech = self._compute_technical(c1h, c15m, c6h, c24h)
                fut_feat = self._compute_futures(fut)
                onc_feat = self._compute_onchain(df_onchain, sym)
                depth_feat = self._compute_depth_factor(sym)
                phase = self._detect_market_phase(tech, fut_feat)
                market_phase = detect_market_phase(sym, tech, fut_feat, {})
                alpha = self._compute_alpha_score(tech, fut_feat, onc_feat, phase)
                ev = self._estimate_ev(tech, fut_feat)
                score_layers = compute_score_layers(sym, tech, fut_feat, onc_feat, depth_feat, {})
                # V4.0: 融合深度因子到 alpha score
                alpha = self._apply_depth_to_alpha(alpha, depth_feat)
                raw = {"alpha": alpha, "phase": phase, "ev": ev,
                       "technical": {k: round(v,4) if isinstance(v,float) else v for k,v in tech.items() if v is not None},
                       "futures": {k: round(v,6) if isinstance(v,float) else v for k,v in fut_feat.items() if v is not None},
                       "onchain": {k: round(v,2) if isinstance(v,float) else v for k,v in onc_feat.items() if v is not None},
                       "depth": {k: round(v,4) if isinstance(v,float) else v for k,v in depth_feat.items() if v is not None},
                       "market_phase": market_phase,
                       "score_layers": score_layers}
                results.append({
                    "time": now, "symbol": sym,
                    "composite_score": round(score_layers.get("display_score", alpha["score"]), 2),
                    "composite_summary": alpha["grade"],
                    "risk_label": self._get_risk_labels(tech, fut_feat, phase),
                    "chip_phase": tech.get("chip_phase", "未知"),
                    "trend_state": phase["phase"],
                    "trend_direction": tech.get("trend_direction", "横盘"),
                    "volatility_level": tech.get("volatility_level", "正常"),
                    "price_position": tech.get("price_position", "中位"),
                    "relative_strength": 50.0,
                    "market_price": tech.get("current_price", 0),
                    # V3.0 Entry/Hold Alpha 分离
                    "entry_alpha": round(self._compute_entry_alpha(tech, fut_feat, phase), 1),
                    "hold_alpha": round(self._compute_hold_alpha(tech, fut_feat, phase), 1),
                    "raw_features": raw,
                    "scan_id": self.scan_id})
            except: continue

        # === V5: 绝对评分 + 历史胜率 (不再百分位归一化) ===
        # 1. 获取历史胜率
        market_strengths = self._compute_market_strengths(results)
        for r in results:
            strength = market_strengths.get(r["symbol"], 50.0)
            r["relative_strength"] = strength
            r["raw_features"]["market_strength"] = {
                "score": strength,
                "basis": "cross_sectional_return_volume_oi",
            }

        hist_perf = self._get_all_historical_performance()
        
        # 2. 计算绝对评分 + 历史胜率加成
        for r in results:
            sym = r["symbol"]
            perf = hist_perf.get(sym, {
                "total": 0, "win_rate": 50.0, "total_pnl": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 1.0,
                "expectancy": 0.0,
            })
            
            # 保存原始评分
            raw_score = r.get("composite_score", 50)
            r["composite_score_raw"] = raw_score
            
            # V5: 历史胜率加成（胜率>50%加分，<50%不扣分）
            # 原始评分50分=50%胜率对应0加成
            # 限制：胜率0-50%不扣分，只在>50%时加分
            hist_bonus = 0.0
            
            # 新综合评分 = 原始评分 + 历史胜率加成
            hist_adjust = 0.0
            if perf["total"] >= 5:
                if perf["expectancy"] > 0 and perf["total_pnl"] > 0 and perf["profit_factor"] > 1.15:
                    hist_adjust += min(6.0, 2.0 + (perf["profit_factor"] - 1.0) * 3.0)
                if perf["expectancy"] < 0 or perf["total_pnl"] < 0:
                    hist_adjust -= min(18.0, 4.0 + abs(perf["expectancy"]) * 4.0)
                if perf["profit_factor"] < 0.8:
                    hist_adjust -= min(6.0, (0.8 - perf["profit_factor"]) * 8.0)

            new_score = raw_score + hist_adjust
            
            # 限制在 0-100 范围
            r["composite_score"] = round(max(0, min(100, new_score)), 1)
            r["historical_win_rate"] = round(perf["win_rate"], 1)
            r["historical_expectancy"] = round(perf["expectancy"], 4)
            r["historical_profit_factor"] = round(perf["profit_factor"], 3)
            r["historical_total_pnl"] = round(perf["total_pnl"], 4)
            r["raw_features"]["historical_performance"] = {
                **perf,
                "score_adjust": round(hist_adjust, 2),
            }
            rf = r.get("raw_features") or {}
            tech = rf.get("technical") or {}
            fut_feat = rf.get("futures") or {}
            onc_feat = rf.get("onchain") or {}
            depth_feat = rf.get("depth") or {}
            score_layers = compute_score_layers(sym, tech, fut_feat, onc_feat, depth_feat, perf)
            r["raw_features"]["score_layers"] = score_layers
            r["composite_score"] = round(max(0, min(100, score_layers.get("display_score", new_score) + hist_adjust * 0.25)), 1)
            r["composite_summary"] = self._grade_from_score(r["composite_score"])
        
        # 3. 按新评分排序
        results.sort(key=lambda x: -x["composite_score"])
        
        # Keep the full scan universe.  Diversification belongs in the live
        # candidate selector; truncating here makes the monitor look blind.

        # ── 🆕 写 training_samples 快照 ──
        self._write_training_samples(results)

        return results

    def _detect_market_regime_text(self, tech):
        """返回市场状态文本（用于 training_samples 的 market_regime 字段）"""
        td = tech.get("trend_direction", "横盘")
        vl = tech.get("volatility_level", "正常")
        pc24 = tech.get("price_change_24h", 0)
        vq = tech.get("vol_quality_score", 50)
        if td == "向下" and vl in ("偏高", "极高"):
            return "Panic Market"
        if td == "向下":
            return "Bear Market"
        if td == "向上" and vq > 60 and pc24 > 0.02:
            return "Bull Market"
        return "Sideways Market"

    def _write_training_samples(self, results):
        """将本次评分全部特征快照写入 training_samples 表"""
        try:
            rows = []
            now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for r in results:
                rf = r.get("raw_features", {})
                tech = rf.get("technical", {})
                market_regime = self._detect_market_regime_text(tech)
                rows.append((
                    self.scan_id,
                    r["symbol"],
                    now_str,
                    json.dumps(rf, ensure_ascii=False),
                    r["composite_score"],
                    market_regime,
                ))
            if rows:
                insert_training_samples(rows)
                logger = logging.getLogger("engine")
                logger.info(f"[training-samples] Wrote {len(rows)} samples (scan={self.scan_id[:8]}...)")
        except Exception as e:
            logger = logging.getLogger("engine")
            logger.warning(f"[training-samples] Write failed: {e}")

    def _compute_depth_factor(self, symbol):
        """V4.0 订单簿深度因子

        Returns:
            dict: {
                depth_ratio: float,          # 买盘/卖盘比例（前20档）
                depth_ratio_score: float,    # 0-100评分
                big_order_density: float,    # 大单总量/日成交额（相对化）
                big_order_score: float,      # 0-100评分
                robot_signature: bool,       # 是否检测到等量重复挂单（2410规律）
                imbalance_ratio: float,      # 原始失衡比例
            }
        """
        try:
            from shared.db import fetch_orderbook_depth, fetch_24h_quote_volume

            depth_rows = fetch_orderbook_depth(symbol, hours=6)
            if not depth_rows:
                return {"depth_ratio": 1.0, "depth_ratio_score": 50, "big_order_density": 0,
                        "big_order_score": 50, "robot_signature": False, "imbalance_ratio": 1.0}

            latest = depth_rows[0]
            bid_depth = float(latest["bid_depth"] or 0)
            ask_depth = float(latest["ask_depth"] or 0)
            imbalance = float(latest["imbalance_ratio"] or 1.0)
            top_bid = float(latest["top_bid_qty"] or 0)
            top_ask = float(latest["top_ask_qty"] or 0)

            depth_ratio = bid_depth / ask_depth if ask_depth > 0 else 1.0

            if depth_ratio >= 2.5: depth_ratio_score = 90
            elif depth_ratio >= 2.0: depth_ratio_score = 80
            elif depth_ratio >= 1.5: depth_ratio_score = 70
            elif depth_ratio >= 1.2: depth_ratio_score = 60
            elif depth_ratio >= 0.8: depth_ratio_score = 50
            elif depth_ratio >= 0.5: depth_ratio_score = 35
            elif depth_ratio >= 0.3: depth_ratio_score = 20
            else: depth_ratio_score = 10

            quote_vol_24h = fetch_24h_quote_volume(symbol)
            big_order_density = 0.0
            if quote_vol_24h > 0:
                max_qty = max(top_bid, top_ask)
                big_order_density = max_qty / quote_vol_24h

            if big_order_density >= 0.02: big_order_score = 85
            elif big_order_density >= 0.01: big_order_score = 70
            elif big_order_density >= 0.005: big_order_score = 55
            elif big_order_density >= 0.001: big_order_score = 45
            else: big_order_score = 40

            robot_signature = False
            if len(depth_rows) >= 2:
                prev = dict(depth_rows[1])
                prev_max = max(float(prev.get("top_bid_qty", 0) or 0),
                               float(prev.get("top_ask_qty", 0) or 0))
                if prev_max > 0 and max(top_bid, top_ask) / prev_max > 3:
                    robot_signature = True

            return {
                "depth_ratio": round(depth_ratio, 3),
                "depth_ratio_score": depth_ratio_score,
                "big_order_density": round(big_order_density, 5),
                "big_order_score": big_order_score,
                "robot_signature": robot_signature,
                "imbalance_ratio": round(imbalance, 3),
            }
        except Exception as e:
            lg = logging.getLogger("engine")
            lg.warning(f"depth_factor error {symbol}: {e}")
            return {"depth_ratio": 1.0, "depth_ratio_score": 50, "big_order_density": 0,
                    "big_order_score": 50, "robot_signature": False, "imbalance_ratio": 1.0}

    def _apply_depth_to_alpha(self, alpha, depth_feat):
        """V4.0: 将深度因子融入 alpha score

        深度比权重10%，大单密度权重10%（从chip和absorption调整）
        """
        depth_ratio_score = depth_feat.get("depth_ratio_score", 50)
        big_order_score = depth_feat.get("big_order_score", 50)
        robot_sig = depth_feat.get("robot_signature", False)

        # 深度比权重10%：调整alpha score
        depth_adjust = (depth_ratio_score - 50) * 0.10
        new_score = alpha["score"] + depth_adjust

        # 大单密度权重10%：从chip因子中抽取一部分
        # 原有chip权重25%，这里再从alpha总分中分10%
        big_order_adjust = (big_order_score - 50) * 0.05
        new_score = new_score + big_order_adjust

        # robot_signature检测到时，降低该币种权重（可能是虚假深度）
        if robot_sig:
            new_score -= 5

        new_score = max(5, min(95, new_score))

        return {
            **alpha,
            "score": round(new_score, 1),
            "depth_ratio": depth_feat.get("depth_ratio"),
            "big_order_density": depth_feat.get("big_order_density"),
            "robot_signature": robot_sig,
        }

    # ==================== Market Regime ====================

    def get_market_regime(self, results):
        """
        改进：从全局（BTC/USDT）判定市场状态，而非个币级别。
        🆕 4.6 Market Regime Engine
        四个状态: Bull / Bear / Sideways / Panic
        """
        # 查找 BTC 的 raw_features 中的 technical 数据
        btc_row = next((r for r in results if r["symbol"] in ("BTCUSDT", "BTC")), None)
        if not btc_row:
            return {"state": "Sideways Market", "confidence": "低", "modifier": 0}

        rf = btc_row.get("raw_features", {})
        if isinstance(rf, str):
            import json; rf = json.loads(rf)
        tech = rf.get("technical", {})

        td = tech.get("trend_direction", "横盘")
        vl = tech.get("volatility_level", "正常")
        pc24 = tech.get("price_change_24h", 0)
        vq = tech.get("vol_quality_score", 50)
        vol_r = tech.get("volume_change_pct", 0)

        # Panic: BTC暴跌 + 放量
        if td == "向下" and pc24 < -0.08 and vol_r > 1.0:
            return {"state": "Panic Market", "confidence": "高", "modifier": -25, "long_banned": True}

        # Bear: BTC向下趋势
        if td == "向下":
            modifier = -15
            if vl in ("偏高", "极高"):
                modifier = -20
            return {"state": "Bear Market", "confidence": "中", "modifier": modifier, "long_banned": False}

        # Bull: BTC向上趋势 + 量健康
        if td == "向上" and vq > 60 and pc24 > 0.02:
            return {"state": "Bull Market", "confidence": "中", "modifier": 0, "long_banned": False}

        # Sideways
        return {"state": "Sideways Market", "confidence": "低", "modifier": 0, "long_banned": False}

    # === V5: 历史胜率查询 ===
    def _get_all_historical_win_rates(self) -> dict:
        """从 trades 表查询各币种历史胜率"""
        from shared.db import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT symbol,
                       COUNT(*) as total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM trades
                WHERE created_at > datetime('now', '-7 days')
                GROUP BY symbol
            """).fetchall()
            
            win_rates = {}
            for r in rows:
                if r["total"] and r["total"] >= 1:  # 至少1笔交易即可
                    win_rates[r["symbol"]] = r["wins"] / r["total"] * 100
                else:
                    # 无交易历史的币种，默认50%胜率
                    win_rates[r["symbol"]] = 50.0
            
            return win_rates
        finally:
            conn.close()

    # === V5: 分散化过滤 ===
    def _get_all_historical_performance(self) -> dict:
        """Return per-symbol expectancy metrics from recent real/system trades."""
        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT symbol,
                       COUNT(*) AS total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_win,
                       ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)) AS gross_loss,
                       AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,
                       ABS(AVG(CASE WHEN pnl < 0 THEN pnl END)) AS avg_loss,
                       SUM(pnl) AS total_pnl
                FROM trades
                WHERE created_at > datetime('now', '-7 days')
                  AND source IN ('system', 'income_auto')
                GROUP BY symbol
            """).fetchall()

            perf = {}
            for r in rows:
                total = int(r["total"] or 0)
                wins = int(r["wins"] or 0)
                gross_win = float(r["gross_win"] or 0)
                gross_loss = float(r["gross_loss"] or 0)
                total_pnl = float(r["total_pnl"] or 0)
                avg_win = float(r["avg_win"] or 0)
                avg_loss = float(r["avg_loss"] or 0)
                win_rate = (wins / total * 100) if total else 50.0
                profit_factor = gross_win / gross_loss if gross_loss > 0 else (3.0 if gross_win > 0 else 1.0)
                expectancy = total_pnl / total if total else 0.0
                perf[r["symbol"]] = {
                    "total": total,
                    "win_rate": round(win_rate, 2),
                    "total_pnl": round(total_pnl, 6),
                    "avg_win": round(avg_win, 6),
                    "avg_loss": round(avg_loss, 6),
                    "profit_factor": round(profit_factor, 4),
                    "expectancy": round(expectancy, 6),
                }
            return perf
        finally:
            conn.close()

    def _apply_diversity_filter(self, results, max_per_category=3):
        """按类别分散化，每类最多选max_per_category个"""
        # 类别映射（简化版）
        CATEGORY_MAP = {
            "BTC": "蓝筹", "ETH": "蓝筹", "BNB": "蓝筹", "LTC": "蓝筹",
            "SOL": "叙事", "AVAX": "叙事", "ADA": "叙事", "LINK": "叙事",
            "DOGE": "Meme", "SHIB": "Meme",
        }
        
        categories = {}
        for r in results:
            sym = r["symbol"].replace("USDT", "").replace("USDT", "")
            cat = CATEGORY_MAP.get(sym, "其他")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(r)
        
        # 每类选最优，最多选max_per_category
        filtered = []
        for cat, items in categories.items():
            filtered.extend(items[:max_per_category])
        
        # 如果过滤后太少，补充未分类的高分币
        if len(filtered) < 10:
            uncategorized = [r for r in results if r not in filtered]
            filtered.extend(uncategorized[:10-len(filtered)])
        
        return sorted(filtered, key=lambda x: -x["composite_score"])

    def _compute_alpha_score(self, tech, fut, onchain, phase):
        # === 🆕 优先用真实 6h/24h 收益率 ===
        ret_6h = tech.get("return_6h")
        ret_24h = tech.get("return_24h")
        
        # 用真实收益计算概率
        p12 = 0.30  # 默认
        p24 = 0.30
        if ret_24h is not None:
            if ret_24h > 0.10: p24 = 0.70
            elif ret_24h > 0.05: p24 = 0.55
            elif ret_24h > 0: p24 = 0.40
            elif ret_24h > -0.05: p24 = 0.25
            else: p24 = 0.15
        if ret_6h is not None:
            if ret_6h > 0.05: p12 = 0.70
            elif ret_6h > 0.02: p12 = 0.55
            elif ret_6h > 0: p12 = 0.40
            elif ret_6h > -0.03: p12 = 0.25
            else: p12 = 0.15
        
        pdd = self._est_drawdown_prob(tech, fut, phase)
        rg = self._compute_market_regime(tech)
        pb = {"accumulation":5,"breakout":8,"trend":3,"euphoria":-15,"distribution":-20}.get(phase["phase"],0)
        w = self.alpha_formula
        s = (w["w_return_12h"]*p12 + w["w_return_24h"]*p24 - w["w_max_drawdown"]*pdd)*100 + pb + rg
        s = max(5, min(95, s))
        return {"score":round(s,1),"grade":self._grade_from_score(s),
                "p_return_12h":round(p12,3),"p_return_24h":round(p24,3),
                "p_drawdown":round(pdd,3),"phase_bonus":pb,"regime_adjustment":rg,
                "actual_return_6h":round(ret_6h,4) if ret_6h else None,
                "actual_return_24h":round(ret_24h,4) if ret_24h else None}

    def _detect_market_phase(self, tech, fut):
        ema = tech.get("ema20_slope",0); rsi = tech.get("rsi_14",50)
        fr = fut.get("funding_rate",0); vq = tech.get("vol_quality_score",50)
        vl = tech.get("volatility_level","正常"); ch = tech.get("chip_phase","中性震荡")
        pp = tech.get("price_position","中位"); ab = tech.get("absorption_score", tech.get("abs_score",50))
        if rsi > 75 and fr > 0.0005 and pp in ("高位","偏高"):
            return {"phase":"euphoria","confidence":"高","reason":f"RSI{rsi:.0f}"}
        if ch == "疑似出货" or (ab < 35 and vl in ("偏高","极高")):
            return {"phase":"distribution","confidence":"高","reason":"派发"}
        if tech.get("ema20_50_ratio",1) > 1.02 and vq > 70:
            return {"phase":"breakout","confidence":"中","reason":"突破"}
        if abs(ema) < 0.5 and vl in ("偏低","极低") and pp in ("低位","偏低"):
            return {"phase":"accumulation","confidence":"中","reason":"低波"}
        if ema > 0.5 and pp in ("中位",) and ch not in ("疑似出货",):
            return {"phase":"trend","confidence":"中","reason":"EMA上行"}
        return {"phase":"neutral","confidence":"低","reason":"无明显特征"}

    def _compute_market_regime(self, tech):
        s = 0.0
        if tech.get("trend_direction") == "向上": s += 5
        elif tech.get("trend_direction") == "向下": s -= 8
        vl = tech.get("volatility_level","正常")
        if vl == "极低": s += 3
        elif vl in ("偏高","极高"): s -= 8
        return s
    def _est_return_prob(self, tech, fut, onchain, hours, target):
        p = 0.30
        if tech.get("trend_direction") == "向上": p += 0.10
        elif tech.get("trend_direction") == "向下": p -= 0.10
        ch = tech.get("chip_phase", "中性震荡")
        if ch in ("吸筹拉盘", "温和吸筹"): p += 0.10
        elif ch == "疑似出货": p -= 0.15
        p += (tech.get("vol_quality_score", 50) - 50) / 500
        pp = tech.get("price_position", "中位")
        if pp == "低位": p += 0.08
        elif pp == "高位": p -= 0.08
        fr = fut.get("funding_rate", 0)
        if fr < -0.0005: p += 0.05
        elif fr > 0.001: p -= 0.10
        if fut.get("oi_score", 50) > 60: p += 0.03
        if onchain.get("has_data", False):
            p += (onchain.get("flow_score", 50) - 50) / 500
        atr_r = tech.get("atr_ratio", 0.02)
        if atr_r * ((hours / 24.0) ** 0.5) > target * 0.3: p += 0.03
        if hours == 24 and target == 0.10: p *= 0.85
        return max(0.05, min(0.95, p))

    def _est_drawdown_prob(self, tech, fut, phase):
        p = 0.15
        vl = tech.get("volatility_level", "正常")
        if vl == "极高": p += 0.25
        elif vl == "偏高": p += 0.15
        if tech.get("trend_direction") == "向下": p += 0.15
        if phase["phase"] in ("euphoria", "distribution"): p += 0.20
        if fut.get("funding_rate", 0) > 0.001: p += 0.10
        if tech.get("price_position") in ("高位",): p += 0.10
        if tech.get("atr_ratio", 0) > 0.04: p += 0.15
        return max(0.05, min(0.85, p))

    def _estimate_ev(self, tech, fut):
        p = 0.30 + (tech.get("chip_score", 50) - 50) / 200
        p += (tech.get("absorption_score", tech.get("abs_score", 50)) - 50) / 200
        p = max(0, min(1, p))
        ev = p * 0.04 - (1 - p) * 0.03
        return {"ev": round(ev * 100, 2), "p_win": round(p, 2), "avg_win": 4.0, "avg_loss": 3.0}

    def _compute_entry_alpha(self, tech, fut, phase):
        """V3.0 Entry Alpha - 关注是否值得开仓
        重点：收益空间、突破质量、市场环境
        """
        s = 50.0
        
        # 1. 筹码阶段 - 吸筹/突破是好信号
        ch = tech.get("chip_phase", "中性震荡")
        if ch in ("吸筹拉盘", "温和吸筹"): s += 15
        elif ch == "疑似出货": s -= 20
        
        # 2. 市场阶段 - breakout最好, trend次之
        ph = phase.get("phase", "neutral")
        if ph == "breakout": s += 10
        elif ph == "trend": s += 5
        elif ph in ("euphoria", "distribution"): s -= 15
        
        # 3. 趋势方向 - 向上趋势值得开仓
        td = tech.get("trend_direction", "横盘")
        if td == "向上": s += 10
        elif td == "向下": s -= 15
        
        # 4. 价格位置 - 低位更有空间
        pp = tech.get("price_position", "中位")
        if pp == "低位": s += 8
        elif pp == "偏高": s -= 5
        elif pp == "高位": s -= 10
        
        # 5. 资金费率 - 负费率是加分项
        fr = fut.get("funding_rate", 0)
        if fr < -0.0005: s += 8
        elif fr > 0.001: s -= 10
        
        # 6. OI增长 - 资金流入是好信号
        if fut.get("oi_score", 50) > 60: s += 5
        
        # 7. 波动率 - 适中最好，太高不适合开仓
        vl = tech.get("volatility_level", "正常")
        if vl in ("偏高", "极高"): s -= 10
        elif vl == "偏低": s += 3
        
        return max(5, min(95, s))

    def _compute_hold_alpha(self, tech, fut, phase):
        """V3.0 Hold Alpha - 关注是否值得继续持有
        重点：趋势是否结束、资金是否流出、动量是否衰减
        """
        s = 50.0
        
        # 1. 趋势方向 - 趋势还在就继续持有
        td = tech.get("trend_direction", "横盘")
        if td == "向上": s += 15
        elif td == "向下": s -= 20
        
        # 2. 筹码阶段 - 派发就要考虑走
        ch = tech.get("chip_phase", "中性震荡")
        if ch in ("疑似出货",): s -= 25
        elif ch == "吸筹拉盘": s += 10
        
        # 3. 市场阶段 - euphoria要小心
        ph = phase.get("phase", "neutral")
        if ph == "euphoria": s -= 15
        elif ph == "distribution": s -= 20
        elif ph == "trend": s += 10
        
        # 4. 资金费率 - 变正要小心
        fr = fut.get("funding_rate", 0)
        if fr > 0.001: s -= 15
        elif fr < -0.0005: s += 5
        
        # 5. 波动率 - 极高波动要考虑退出
        vl = tech.get("volatility_level", "正常")
        if vl == "极高": s -= 15
        elif vl == "偏高": s -= 8
        
        # 6. 价格位置 - 高位要小心
        pp = tech.get("price_position", "中位")
        if pp == "高位": s -= 15
        elif pp == "偏低": s += 5
        
        # 7. 成交量质量 - 放量是好信号
        vq = tech.get("vol_quality_score", 50)
        if vq > 70: s += 8
        elif vq < 30: s -= 8
        
        return max(5, min(95, s))

    def _grade_from_score(self, s):
        if s >= 80: return "S1"
        if s >= 70: return "S2"
        if s >= 60: return "A1"
        if s >= 55: return "A2"
        if s >= 45: return "B"
        if s >= 30: return "C"
        return "D"

    # ==================== V3.3 Breakout Score ====================

    
    def _compute_breakout_score(self, df_1h, df_15m, df_6h=None, df_futures=None):
        """V3.3 突破确认综合评分 (0-100)
        综合：价格突破 + 成交量 + OI + 订单簿 + 资金费率 + CVD + ATR + 多周期
        """
        s = 50.0  # 基础分
        details = {}
        
        if df_1h is None or df_1h.empty:
            return {"score": 0, "details": {}, "reason": "数据不足"}
        
        # 1. 价格突破 - 收盘价突破最近N根K线高点
        try:
            n = 10
            if len(df_1h) >= n:
                recent_high = df_1h["high"].tail(n).max()
                current_close = df_1h["close"].iloc[-1]
                if current_close > recent_high:
                    s += 15
                    details["price_breakout"] = "突破"
                else:
                    s -= 10
                    details["price_breakout"] = "未突破"
            else:
                details["price_breakout"] = "数据不足"
        except Exception as e:
            details["price_breakout"] = f"error:{e}"
        
        # 2. 成交量确认 - 高于均量1.5倍
        try:
            avg_vol = df_1h["volume"].tail(20).mean()
            current_vol = df_1h["volume"].iloc[-1]
            if avg_vol > 0 and current_vol > avg_vol * 1.5:
                s += 12
                details["volume_confirm"] = f"放量{current_vol/avg_vol:.1f}x"
            elif avg_vol > 0 and current_vol > avg_vol * 1.2:
                s += 5
                details["volume_confirm"] = f"温和放量{current_vol/avg_vol:.1f}x"
            else:
                s -= 8
                details["volume_confirm"] = "缩量"
        except Exception as e:
            details["volume_confirm"] = f"error:{e}"
        
        # 3. Open Interest 增加 - 新增资金入场
        try:
            if df_futures is not None and "open_interest" in df_futures.columns:
                oi_now = df_futures["open_interest"].iloc[-1]
                oi_prev = df_futures["open_interest"].iloc[-5] if len(df_futures) >= 5 else oi_now
                if oi_prev > 0 and (oi_now - oi_prev) / oi_prev > 0.02:
                    s += 10
                    details["oi_increase"] = "资金流入"
                elif oi_now < oi_prev:
                    s -= 10
                    details["oi_increase"] = "资金流出"
                else:
                    details["oi_increase"] = "持平"
            else:
                details["oi_increase"] = "无数据"
        except Exception as e:
            details["oi_increase"] = f"error:{e}"
        
        # 4. 资金费率 - 避免过度拥挤
        try:
            if df_futures is not None and "funding_rate" in df_futures.columns:
                fr = df_futures["funding_rate"].iloc[-1]
                if fr < 0:
                    s += 8  # 负费率是加分
                elif fr > 0.001:
                    s -= 10  # 高费率风险
                details["funding_rate"] = f"{fr*100:.3f}%"
            else:
                details["funding_rate"] = "无数据"
        except Exception as e:
            details["funding_rate"] = f"error:{e}"
        
        # 5. ATR波动率扩张 - 确认突破动能
        try:
            atr_now = df_1h["atr"].iloc[-1] if "atr" in df_1h.columns else None
            if atr_now is not None:
                atr_prev = df_1h["atr"].iloc[-5] if len(df_1h) >= 5 else atr_now
                if atr_prev > 0 and atr_now > atr_prev * 1.2:
                    s += 8
                    details["atr_expand"] = "扩张"
                elif atr_now < atr_prev * 0.8:
                    s -= 5
                    details["atr_expand"] = "收缩"
                else:
                    details["atr_expand"] = "稳定"
        except Exception as e:
            details["atr_expand"] = f"error:{e}"
        
        # 6. 多周期趋势一致 (15m + 1h + 6h)
        try:
            trend_score = 0
            for df, name in [(df_15m, "15m"), (df_1h, "1h")]:
                if df is not None and not df.empty and len(df) >= 10:
                    ma5 = df["close"].tail(5).mean()
                    ma20 = df["close"].tail(20).mean()
                    if ma5 > ma20: trend_score += 1
                    elif ma5 < ma20: trend_score -= 1
            if trend_score >= 1:
                s += 10
                details["multi_tf"] = "多周期向上"
            elif trend_score <= -1:
                s -= 10
                details["multi_tf"] = "多周期向下"
            else:
                details["multi_tf"] = "震荡"
        except Exception as e:
            details["multi_tf"] = f"error:{e}"
        
        # 7. 订单簿失衡 (模拟 - 需要真实数据)
        # 暂时用成交量分布代替
        try:
            up_vol = df_1h[df_1h["close"] > df_1h["open"]]["volume"].sum()
            down_vol = df_1h[df_1h["close"] < df_1h["open"]]["volume"].sum()
            if up_vol > down_vol * 1.3:
                s += 8
                details["orderbook"] = "买盘强"
            elif down_vol > up_vol * 1.3:
                s -= 8
                details["orderbook"] = "卖盘强"
            else:
                details["orderbook"] = "平衡"
        except Exception as e:
            details["orderbook"] = f"error:{e}"
        
        # 8. CVD (累积成交量 delta) 同步上升
        try:
            df_1h["close"]
            df_1h["open"]
            cvd = (df_1h["close"] - df_1h["open"]).apply(lambda x: x > 0)
            up_cvd = cvd[cvd == True].sum()
            if up_cvd > len(df_1h) * 0.6:
                s += 5
                details["cvd"] = "真实买盘"
            elif up_cvd < len(df_1h) * 0.4:
                s -= 5
                details["cvd"] = "真实卖盘"
            else:
                details["cvd"] = "中性"
        except Exception as e:
            details["cvd"] = f"error:{e}"
        
        score = max(0, min(100, s))
        
        return {
            "score": round(score, 1),
            "details": details,
            "reason": "通过" if score >= 60 else "未通过"
        }

    # ==================== V3.7 Trade Quality Engine ====================

    
    def _compute_trade_quality(self, alpha_score, ev_score, rr_ratio, breakout_score, 
                                market_regime, liquidity_score, ob_score, oi_score, funding_score, 
                                correlation_score=50):
        """V3.7 Trade Quality Engine - 综合多因素生成0-100分
        综合：Alpha Score + EV + R:R + Breakout + 市场环境 + 流动性 + 订单簿 + OI + 资金费率 + 相关性
        """
        s = 50.0
        details = {}
        
        # 1. Alpha Score (权重25%)
        if alpha_score >= 80: s += 12.5
        elif alpha_score >= 70: s += 8
        elif alpha_score >= 60: s += 4
        elif alpha_score < 50: s -= 8
        details["alpha"] = alpha_score
        
        # 2. Expected Value (权重20%)
        if ev_score >= 0: s += 10
        elif ev_score >= -0.5: s += 5
        else: s -= 10
        details["ev"] = ev_score
        
        # 3. Risk Reward Ratio (权重15%)
        if rr_ratio >= 2.0: s += 7.5
        elif rr_ratio >= 1.5: s += 5
        elif rr_ratio >= 1.0: s += 2
        elif rr_ratio < 0.5: s -= 7.5
        details["rr"] = rr_ratio
        
        # 4. Breakout Score (权重15%)
        if breakout_score >= 70: s += 7.5
        elif breakout_score >= 60: s += 4
        elif breakout_score < 40: s -= 7.5
        details["breakout"] = breakout_score
        
        # 5. Market Regime (权重5%)
        if market_regime in ("breakout", "trend"): s += 2.5
        elif market_regime in ("distribution", "euphoria"): s -= 2.5
        details["regime"] = market_regime
        
        # 6. Liquidity (权重5%)
        if liquidity_score >= 70: s += 2.5
        elif liquidity_score < 30: s -= 2.5
        details["liquidity"] = liquidity_score
        
        # 7. Order Book (权重5%)
        if ob_score >= 60: s += 2.5
        elif ob_score < 40: s -= 2.5
        details["ob"] = ob_score
        
        # 8. Open Interest (权重5%)
        if oi_score >= 60: s += 2.5
        elif oi_score < 40: s -= 2.5
        details["oi"] = oi_score
        
        # 9. Funding Rate (权重5%)
        if funding_score >= 60: s += 2.5
        elif funding_score < 40: s -= 2.5
        details["funding"] = funding_score
        
        # 10. Correlation (权重-5%) - 负权重，相关性高则减分
        if correlation_score >= 70: s -= 2.5
        elif correlation_score < 30: s += 1
        details["correlation"] = correlation_score
        
        score = max(0, min(100, s))
        
        # 决策建议
        if score >= 70:
            decision = "正常仓位"
        elif score >= 50:
            decision = "降低仓位"
        else:
            decision = "放弃交易"
        
        return {
            "score": round(score, 1),
            "details": details,
            "decision": decision
        }

    # ==================== Technical ====================

    def _compute_technical(self, df_1h, df_15m, df_6h=None, df_24h=None):
        r = {}
        if df_1h.empty:
            return r
        c = df_1h["close"].values
        h = df_1h["high"].values
        lo = df_1h["low"].values
        v = df_1h["volume"].values
        o = df_1h["open"].values
        n = len(c)
        r["current_price"] = float(c[-1]) if n > 0 else 0

        # === 🆕 真实 6h/24h 收益率 ===
        r["return_6h"] = None
        r["return_24h"] = None
        if df_6h is not None and not df_6h.empty and len(df_6h) >= 2:
            c6 = df_6h["close"].values
            if len(c6) >= 2:
                r["return_6h"] = (c6[-1] - c6[0]) / c6[0] if c6[0] != 0 else 0
        if df_24h is not None and not df_24h.empty and len(df_24h) >= 2:
            c24 = df_24h["close"].values
            if len(c24) >= 2:
                r["return_24h"] = (c24[-1] - c24[0]) / c24[0] if c24[0] != 0 else 0

        if n >= 14:
            tr = np.maximum(h[1:]-lo[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(lo[1:]-c[:-1])))
            atr = float(np.mean(tr[-14:]))
            r["atr"] = atr; r["atr_ratio"] = atr / c[-1] if c[-1] != 0 else 0
        else:
            r["atr"] = r["atr_ratio"] = 0

        if n >= 24:
            vol = float(np.std(np.diff(np.log(c[-25:])))) * 100
        else:
            vol = 0
        if vol < 1.5: r["volatility_level"], r["volatility_score"] = "极低", 80
        elif vol < 3: r["volatility_level"], r["volatility_score"] = "偏低", 65
        elif vol < 5: r["volatility_level"], r["volatility_score"] = "正常", 50
        elif vol < 8: r["volatility_level"], r["volatility_score"] = "偏高", 30
        else: r["volatility_level"], r["volatility_score"] = "极高", 15

        if n >= 50:
            ema20 = self._ema(c, 20); ema50 = self._ema(c, 50)
            r["ema20"] = float(ema20[-1])
            r["ema20_slope"] = float((ema20[-1]-ema20[-5])/ema20[-5]*100) if len(ema20) >= 5 else 0
            r["ema20_50_ratio"] = float(ema20[-1]/ema50[-1]) if ema50[-1] > 0 else 1.0
            if ema20[-1] > ema50[-1]*1.02: r["trend_direction"], r["trend_score"] = "向上", 70
            elif ema20[-1] < ema50[-1]*0.98: r["trend_direction"], r["trend_score"] = "向下", 30
            else: r["trend_direction"], r["trend_score"] = "横盘", 50
        else:
            r["ema20"] = float(c[-1]) if n else 0
            r["ema20_slope"] = 0; r["ema20_50_ratio"] = 1.0
            r["trend_direction"], r["trend_score"] = "横盘", 50

        if n >= 48:
            rh = float(np.max(h[-48:])); rl = float(np.min(lo[-48:]))
            r["range_width_pct"] = (rh - rl) / rl if rl > 0 else 0
            rv = float(np.mean(v[-24:]))
            ov = float(np.mean(v[-48:-24])) if n >= 48 else rv
            vc = (rv - ov) / ov if ov > 0 else 0
            r["volume_change_pct"] = vc
            if r["range_width_pct"] < 0.15 and abs(vc) < 0.3:
                r["volatility_quality"], r["vol_quality_score"], r["trend_state"] = "有效压缩", 85, "低波蓄力"
            elif r["range_width_pct"] < 0.25 and abs(vc) < 0.5:
                r["volatility_quality"], r["vol_quality_score"], r["trend_state"] = "普通波动", 55, "窄幅震荡"
            else:
                r["volatility_quality"], r["vol_quality_score"], r["trend_state"] = "失控波动", 20, "剧烈波动"
        else:
            r["volatility_quality"], r["vol_quality_score"], r["trend_state"] = "普通波动", 50, "震荡换手"

        if n >= 24:
            rr = (c[-24:]/o[-24:])-1
            uv = float(np.sum(v[-24:][rr > 0])); dv = float(np.sum(v[-24:][rr <= 0]))
            vr = uv / dv if dv > 0 else 2.0
            r["up_down_vol_ratio"] = vr
            pc24 = (c[-1]-c[-25])/c[-25] if n >= 25 else 0
            r["price_change_24h"] = pc24
            if n >= 48:
                rp = min(24, n//2)
                oa = float(np.mean(v[-2*rp:-rp])) if n >= 2*rp else float(np.mean(v))
                ra = float(np.mean(v[-rp:]))
                if pc24 < -0.03 and ra > oa*1.3: r["absorption_quality"], r["abs_score"] = "放量接盘", 80
                elif pc24 < -0.03 and ra < oa*0.7: r["absorption_quality"], r["abs_score"] = "缩量下跌", 20
                elif abs(pc24) < 0.02 and ra > oa*1.2: r["absorption_quality"], r["abs_score"] = "横盘温量(吸筹)", 75
                else: r["absorption_quality"], r["abs_score"] = "正常", 50
            else:
                r["absorption_quality"], r["abs_score"] = "正常", 50
            if pc24 > 0.05 and vr > 1.3: r["chip_phase"], r["chip_score"] = "吸筹拉盘", 80
            elif pc24 > 0.03 and vr > 1.1: r["chip_phase"], r["chip_score"] = "温和吸筹", 65
            elif pc24 < -0.03 and vr < 0.7: r["chip_phase"], r["chip_score"] = "疑似出货", 20
            elif vr > 1.5 and abs(pc24) < 0.03: r["chip_phase"], r["chip_score"] = "高换手震荡", 40
            else: r["chip_phase"], r["chip_score"] = "中性震荡", 50
        else:
            r["chip_phase"], r["chip_score"] = "中性震荡", 50
            r["up_down_vol_ratio"] = 1.0; r["price_change_24h"] = 0

        if n >= 48:
            ph = float(np.max(h[-48:])); pl = float(np.min(lo[-48:]))
            pos = (c[-1]-pl)/(ph-pl) if ph != pl else 0.5
            r["price_position_value"] = pos
            if pos < 0.3: r["price_position"], r["position_score"] = "低位", 80
            elif pos < 0.5: r["price_position"], r["position_score"] = "偏低", 65
            elif pos < 0.7: r["price_position"], r["position_score"] = "中位", 50
            elif pos < 0.85: r["price_position"], r["position_score"] = "偏高", 35
            else: r["price_position"], r["position_score"] = "高位", 20
        else:
            r["price_position"], r["position_score"] = "中位", 50

        if n >= 15:
            g = np.diff(c); ls = -np.diff(c)
            g[g < 0] = 0; ls[ls < 0] = 0
            ag = float(np.mean(g[-14:])) if len(g) >= 14 else 0.5
            al = float(np.mean(ls[-14:])) if len(ls) >= 14 else 0.5
            rsi = 100 - 100/(1+ag/al) if al > 0 else 100
            r["rsi_14"] = rsi
            if rsi < 30: r["rsi_score"] = 80
            elif rsi < 40: r["rsi_score"] = 65
            elif rsi < 60: r["rsi_score"] = 50
            elif rsi < 70: r["rsi_score"] = 30
            else: r["rsi_score"] = 15
        else:
            r["rsi_14"], r["rsi_score"] = 50, 50

        atr_r = r.get("atr_ratio", 0)
        if atr_r > 0:
            if atr_r < 0.01: r["atr_normalized_score"] = 80
            elif atr_r < 0.02: r["atr_normalized_score"] = 65
            elif atr_r < 0.035: r["atr_normalized_score"] = 50
            elif atr_r < 0.06: r["atr_normalized_score"] = 35
            else: r["atr_normalized_score"] = 20
        else: r["atr_normalized_score"] = 50

        # === 🆕 用真实 6h/24h 数据计算 vol_ratio ===
        ret_6h = r.get("return_6h")
        ret_24h = r.get("return_24h")
        if ret_6h is not None and ret_24h is not None and ret_24h != 0:
            r["vol_ratio_6_24"] = abs(ret_6h / ret_24h) if ret_24h != 0 else 1.0
        else:
            # Fallback to 1h 模拟
            if n >= 25:
                r6 = np.diff(np.log(c[-7:])) if n >= 7 else None
                r24 = np.diff(np.log(c[-25:])) if n >= 25 else None
                if r6 is not None and r24 is not None and len(r6) > 1 and len(r24) > 1:
                    r["vol_ratio_6_24"] = float(np.std(r6))/float(np.std(r24)) if float(np.std(r24)) > 0 else 1.0
                else: r["vol_ratio_6_24"] = 1.0
            else: r["vol_ratio_6_24"] = 1.0

        vr = r.get("vol_ratio_6_24", 1.0)
        if vr < 0.6: r["vol_ratio_score"] = 75
        elif vr < 0.85: r["vol_ratio_score"] = 60
        elif vr < 1.2: r["vol_ratio_score"] = 50
        elif vr < 1.8: r["vol_ratio_score"] = 35
        else: r["vol_ratio_score"] = 20

        if n >= 24:
            rl2 = np.minimum(lo[-24:], c[-24:])
            bc = sum(1 for i in range(1, len(rl2)) if rl2[i] > rl2[i-1]*0.995)
            r["support_score"] = min(100, bc*5+20)
        else: r["support_score"] = 50

        structure = compute_structure_metrics(df_1h, df_15m)
        for key, value in structure.items():
            if value is not None:
                r[key] = value
        if "absorption_score" not in r and "abs_score" in r:
            r["absorption_score"] = r["abs_score"]
        if "abs_score" not in r and "absorption_score" in r:
            r["abs_score"] = r["absorption_score"]

        return r

    def _compute_futures(self, df):
        r = {}
        if df.empty: return r
        lt = df.iloc[-1]
        r["funding_rate"] = float(lt["funding_rate"])
        r["open_interest"] = float(lt["open_interest"])
        r["mark_price"] = float(lt["mark_price"])
        fr = float(lt["funding_rate"])
        if fr < -0.001: r["funding_state"], r["funding_score"] = "极端负费率(空头拥挤)", 70
        elif fr < -0.0005: r["funding_state"], r["funding_score"] = "负费率(空头偏多)", 60
        elif fr > 0.001: r["funding_state"], r["funding_score"] = "极端正费率(多头拥挤)", 30
        elif fr > 0.0005: r["funding_state"], r["funding_score"] = "正费率(多头偏多)", 45
        else: r["funding_state"], r["funding_score"] = "费率正常", 55
        if len(df) >= 3:
            oi = df["open_interest"].values
            oc = (oi[-1]-oi[0])/oi[0] if oi[0] > 0 else 0
            r["oi_change_pct"] = oc
            if oc > 0.1: r["oi_state"], r["oi_score"] = "OI暴增", 15
            elif oc > 0.03: r["oi_state"], r["oi_score"] = "OI温和增长", 65
            elif oc < -0.05: r["oi_state"], r["oi_score"] = "OI大幅减少", 45
            else: r["oi_state"], r["oi_score"] = "OI稳定", 55
        else:
            r["oi_change_pct"] = 0; r["oi_state"], r["oi_score"] = "OI无数据", 50
        return r

    def _compute_onchain(self, df, symbol):
        r = {"has_data": False}
        if df.empty: return r
        sd = df[df["symbol"].str.contains(symbol, case=False, na=False)]
        if sd.empty: return r
        lt = sd.iloc[-1]
        r["has_data"] = True
        r["cex_net_flow_usd"] = float(lt.get("cex_net_flow_usd", 0) or 0)
        r["cex_net_flow_14d_usd"] = float(lt.get("cex_net_flow_14d_usd", 0) or 0)
        r["cex_net_outflow_ratio"] = float(lt.get("cex_net_outflow_ratio", 0) or 0)
        nf = float(lt.get("cex_net_flow_usd", 0) or 0)
        if nf < 0: r["flow_state"], r["flow_score"] = "流入交易所(可能卖出)", 25
        elif nf > 100000: r["flow_state"], r["flow_score"] = "大幅流出交易所(利好)", 80
        elif nf > 10000: r["flow_state"], r["flow_score"] = "温和流出交易所", 65
        else: r["flow_state"], r["flow_score"] = "净流向中性", 50
        n14 = float(lt.get("cex_net_flow_14d_usd", 0) or 0)
        if n14 > 500000: r["flow_14d_score"] = 80
        elif n14 < -500000: r["flow_14d_score"] = 25
        else: r["flow_14d_score"] = 50
        return r

    def _get_risk_labels(self, tech, fut, phase):
        lbl = []
        if tech.get("volatility_level") in ("极高","偏高"): lbl.append("高波动风险")
        if tech.get("chip_phase") == "疑似出货": lbl.append("出货风险")
        if fut.get("funding_score",50) <= 30: lbl.append("杠杆拥挤风险")
        if phase["phase"] == "euphoria": lbl.append("情绪过热追高风险")
        return "/".join(lbl) if lbl else "正常"

    # ==================== Backtest ====================

    def compute_backtest(self, df_scores, df_prices):
        from bisect import bisect_left
        results = []
        price_index = {}
        for sym in df_prices["symbol"].unique():
            sub = df_prices[df_prices["symbol"]==sym].sort_values("time_bucket")
            times = pd.to_datetime(sub["time_bucket"], errors="coerce", utc=True).dt.tz_convert(None)
            closes = pd.to_numeric(sub["close"], errors="coerce")
            valid = times.notna() & closes.notna()
            price_index[sym] = (times[valid].dt.to_pydatetime().tolist(), closes[valid].astype(float).tolist())
        for sym in df_scores["symbol"].unique():
            ss = df_scores[df_scores["symbol"]==sym].sort_values("time")
            ts, cs = price_index.get(sym, (None, None))
            if ts is None or len(ts) == 0: continue
            for _, row in ss.iterrows():
                gt = row["time"]
                if not isinstance(gt, (pd.Timestamp, datetime)): gt = pd.to_datetime(gt, errors="coerce", utc=True)
                if pd.isna(gt):
                    continue
                grade = row.get("composite_summary","B")
                gs = row.get("composite_score",50)
                pa = float(row.get("market_price",0) or 0)
                if pa == 0: continue
                if isinstance(gt, pd.Timestamp):
                    gt2 = gt.tz_convert(None).to_pydatetime() if gt.tz is not None else gt.to_pydatetime()
                elif getattr(gt, "tzinfo", None) is not None:
                    gt2 = gt.astimezone(timezone.utc).replace(tzinfo=None)
                else:
                    gt2 = gt
                idx = bisect_left(ts, gt2)
                rets = {}
                for lb, h in [("return_6h",6),("return_12h",12),("return_24h",24),("return_48h",48)]:
                    tt = gt2 + pd.Timedelta(hours=h)
                    j = bisect_left(ts, tt, lo=idx)
                    rets[lb] = (float(cs[j])-pa)/pa if j < len(ts) else None
                ei = bisect_left(ts, gt2+pd.Timedelta(hours=48), lo=idx)
                mdd = 0
                if ei > idx:
                    wc = cs[idx:ei]
                    if len(wc) > 0: mdd = min(0, (float(min(wc))-pa)/pa)
                results.append({
                    "symbol":sym,"grade":grade,"grade_score":gs,
                    "grade_time":gt,"price_at_grade":pa,
                    "return_6h":rets.get("return_6h"),"return_12h":rets.get("return_12h"),
                    "return_24h":rets.get("return_24h"),"return_48h":rets.get("return_48h"),
                    "max_drawdown":mdd,
                    "win_12h":rets.get("return_12h",0)>0 if rets.get("return_12h") is not None else None,
                    "win_24h":rets.get("return_24h",0)>0 if rets.get("return_24h") is not None else None,
                })
        return results

    @staticmethod
    def _ema(values, period):
        if len(values) < period: return values
        r = np.zeros_like(values)
        r[:period] = values[:period]
        m = 2 / (period + 1)
        for i in range(period, len(values)):
            r[i] = (values[i] - r[i-1]) * m + r[i-1]
        return r
