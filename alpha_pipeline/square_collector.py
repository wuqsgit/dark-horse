"""Optional Binance Square-compatible feed collector.

Binance does not publish a stable read API for Square. The collector therefore
uses a configurable read-only JSON feed and degrades to disabled when no feed
URL is configured.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from shared.db import (
    fetch_active_alpha_symbols,
    fetch_alpha_square_posts,
    insert_alpha_square_sentiment_snapshot,
    upsert_alpha_square_posts,
)


BEARISH_TERMS = {
    "看空", "下跌", "砸盘", "瀑布", "做空", "卖出", "跌破", "归零",
    "bearish", "dump", "short", "sell", "breakdown", "rug",
}
BULLISH_TERMS = {
    "看多", "上涨", "突破", "拉升", "做多", "买入", "反弹",
    "bullish", "pump", "long", "buy", "breakout", "rebound",
}
SUBSTANTIVE_RISK_TERMS = {
    "被盗", "漏洞", "攻击", "下架", "跑路", "冻结", "暂停提现", "增发",
    "hack", "exploit", "delist", "rug pull", "stolen", "suspend withdrawal",
    "contract vulnerability",
}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _published_at(value) -> str | None:
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            raw = float(value)
            if raw > 10_000_000_000:
                raw /= 1000
            parsed = datetime.fromtimestamp(raw, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def classify_square_text(content: str) -> tuple[str, float, bool]:
    text = str(content or "").strip().lower()
    bearish = sum(term in text for term in BEARISH_TERMS)
    bullish = sum(term in text for term in BULLISH_TERMS)
    substantive_risk = any(term in text for term in SUBSTANTIVE_RISK_TERMS)
    if bearish == bullish:
        return "neutral", 0.0, substantive_risk
    total = max(1, bearish + bullish)
    sentiment = "bearish" if bearish > bullish else "bullish"
    return sentiment, abs(bearish - bullish) / total, substantive_risk


def summarize_square_posts(
    posts: list[dict],
    *,
    base_asset: str,
    now: datetime | None = None,
    window_minutes: int = 30,
) -> tuple:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_cutoff = now - timedelta(minutes=window_minutes)
    baseline_cutoff = now - timedelta(hours=24)
    current = []
    baseline = []
    seen = set()
    for post in posts:
        published = datetime.fromisoformat(
            str(post["published_at"]).replace("Z", "+00:00")
        )
        content_key = hashlib.sha256(
            " ".join(str(post.get("content") or "").lower().split()).encode()
        ).hexdigest()
        dedupe_key = (str(post.get("author_id") or ""), content_key)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if published >= current_cutoff:
            current.append(post)
        elif published >= baseline_cutoff:
            baseline.append(post)

    directional = [
        post for post in current
        if post.get("sentiment") in {"bearish", "bullish"}
    ]
    baseline_directional = [
        post for post in baseline
        if post.get("sentiment") in {"bearish", "bullish"}
    ]
    bearish_ratio = (
        sum(post.get("sentiment") == "bearish" for post in directional)
        / len(directional)
        if directional else 0.0
    )
    baseline_ratio = (
        sum(post.get("sentiment") == "bearish" for post in baseline_directional)
        / len(baseline_directional)
        if baseline_directional else bearish_ratio
    )
    author_weights = Counter()
    for post in directional:
        engagement = max(0.0, _number(post.get("engagement")))
        author_weights[str(post.get("author_id") or "unknown")] += (
            1.0 + math.log1p(engagement)
        )
    total_weight = sum(author_weights.values())
    top3_share = (
        sum(sorted(author_weights.values(), reverse=True)[:3]) / total_weight
        if total_weight else 1.0
    )
    snapshot_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    metrics = {
        "time": snapshot_time,
        "base_asset": str(base_asset).upper(),
        "window_minutes": int(window_minutes),
        "effective_post_count": len(directional),
        "unique_authors": len(author_weights),
        "bearish_ratio": bearish_ratio,
        "baseline_bearish_ratio_24h": baseline_ratio,
        "top3_author_share": top3_share,
        "substantive_risk_count": sum(
            bool(post.get("substantive_risk")) for post in current
        ),
    }
    return (
        metrics["time"],
        metrics["base_asset"],
        metrics["window_minutes"],
        metrics["effective_post_count"],
        metrics["unique_authors"],
        metrics["bearish_ratio"],
        metrics["baseline_bearish_ratio_24h"],
        metrics["top3_author_share"],
        metrics["substantive_risk_count"],
        json.dumps(metrics, ensure_ascii=False),
    )


class BinanceSquareCollector:
    def __init__(self, feed_url: str | None = None):
        self.feed_url = str(
            feed_url or os.getenv("ALPHA_SQUARE_FEED_URL") or ""
        ).strip()
        self.client = httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "DarkHorse/1.0"},
        )

    @property
    def enabled(self) -> bool:
        return bool(self.feed_url)

    async def close(self):
        await self.client.aclose()

    @staticmethod
    def _items(payload) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", payload)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "list", "posts", "content"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def _normalize(self, item: dict, base_asset: str) -> tuple | None:
        content = str(
            item.get("content")
            or item.get("text")
            or item.get("title")
            or ""
        ).strip()
        published_at = _published_at(
            item.get("published_at")
            or item.get("publishTime")
            or item.get("createTime")
            or item.get("timestamp")
        )
        if not content or not published_at:
            return None
        post_id = str(
            item.get("post_id")
            or item.get("id")
            or item.get("contentId")
            or hashlib.sha256(
                f"{base_asset}:{published_at}:{content}".encode()
            ).hexdigest()
        )
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        author_id = str(
            item.get("author_id")
            or author.get("id")
            or item.get("userId")
            or "unknown"
        )
        author_name = str(
            item.get("author_name")
            or author.get("name")
            or item.get("nickname")
            or ""
        )
        sentiment, confidence, risk = classify_square_text(content)
        engagement = sum(
            _number(item.get(key))
            for key in (
                "like_count", "likes", "comment_count", "comments",
                "share_count", "shares",
            )
        )
        source_url = str(item.get("url") or item.get("source_url") or "")
        return (
            post_id,
            str(base_asset).upper(),
            published_at,
            author_id,
            author_name,
            content,
            sentiment,
            confidence,
            int(risk),
            engagement,
            source_url,
            json.dumps(item, ensure_ascii=False),
        )

    async def collect_once(self, *, limit: int = 200) -> dict:
        if not self.enabled:
            return {"enabled": False, "symbols": 0, "posts": 0}
        symbols = [dict(row) for row in fetch_active_alpha_symbols(limit=limit)]
        post_count = 0
        snapshot_count = 0
        errors = []
        for symbol in symbols:
            base_asset = str(symbol.get("base_asset") or "").upper()
            if not base_asset:
                continue
            try:
                response = await self.client.get(
                    self.feed_url,
                    params={"keyword": base_asset, "limit": 100},
                )
                response.raise_for_status()
                rows = [
                    normalized
                    for item in self._items(response.json())
                    if (normalized := self._normalize(item, base_asset))
                ]
                post_count += upsert_alpha_square_posts(rows)
                stored = fetch_alpha_square_posts(base_asset, hours=24)
                snapshot = summarize_square_posts(
                    stored,
                    base_asset=base_asset,
                )
                insert_alpha_square_sentiment_snapshot(snapshot)
                snapshot_count += 1
            except Exception as exc:
                errors.append({"base_asset": base_asset, "error": str(exc)})
        return {
            "enabled": True,
            "symbols": len(symbols),
            "posts": post_count,
            "snapshots": snapshot_count,
            "errors": errors,
        }
