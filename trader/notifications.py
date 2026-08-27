import logging
import threading
import time

import httpx

from shared.db import get_trading_kv_value


logger = logging.getLogger("trader.notifications")

FEISHU_EXPLOSIVE_WEBHOOK_KEY = "feishu_explosive_webhook"


def _execution_label(status):
    return {
        "ok": "已成功开仓",
        "blocked": "开仓被硬风控阻止",
        "error": "开仓失败",
    }.get(str(status or "").lower(), "已生成开仓计划")


def _message_text(result: dict, copy_number: int, copies: int) -> str:
    side = "做多" if result.get("position_side") == "LONG" else "做空"
    error = result.get("error") or result.get("data_error")
    lines = [
        f"【爆发行情提醒 {copy_number}/{copies}】",
        f"币种：{result.get('symbol') or '-'}",
        f"方向：{side}",
        f"状态：{_execution_label(result.get('status'))}",
        f"价格：{float(result.get('entry_price') or 0):.8g}",
        f"数量：{float(result.get('quantity') or 0):.8g}",
        f"Alpha评分：{float(result.get('score') or result.get('alpha_score') or 0):.2f}",
    ]
    if error:
        lines.append(f"原因：{error}")
    lines.append("类型：explosive_breakout")
    return "\n".join(lines)


class ExplosiveFeishuNotifier:
    def __init__(
        self,
        webhook=None,
        *,
        copies=3,
        timeout_seconds=2.0,
        retry_attempts=3,
        post=None,
        sleep=None,
        thread_factory=None,
    ):
        self.webhook = str(webhook or "").strip()
        self.copies = max(1, int(copies))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.retry_attempts = max(1, int(retry_attempts))
        self.post = post or httpx.post
        self.sleep = sleep or time.sleep
        self.thread_factory = thread_factory or threading.Thread
        self._seen = set()
        self._lock = threading.Lock()

    def notify(self, result: dict) -> bool:
        if not self.webhook or result.get("event_type") != "explosive_breakout":
            return False
        setup_id = str(
            result.get("setup_id")
            or f"{result.get('symbol')}:{result.get('position_side')}:{result.get('run_id')}"
        )
        with self._lock:
            if setup_id in self._seen:
                return False
            self._seen.add(setup_id)
        thread = self.thread_factory(
            target=self._deliver,
            args=(dict(result),),
            daemon=True,
        )
        thread.start()
        return True

    def _deliver(self, result: dict):
        for copy_number in range(1, self.copies + 1):
            delivered = False
            for attempt in range(1, self.retry_attempts + 1):
                try:
                    response = self.post(
                        self.webhook,
                        json={
                            "msg_type": "text",
                            "content": {
                                "text": _message_text(result, copy_number, self.copies),
                            },
                        },
                        timeout=self.timeout_seconds,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if int(payload.get("code", 0)) != 0:
                        raise RuntimeError(payload.get("msg") or f"Feishu code={payload.get('code')}")
                    delivered = True
                    break
                except Exception as exc:
                    logger.warning(
                        "Feishu explosive alert failed symbol=%s copy=%s attempt=%s: %s",
                        result.get("symbol"),
                        copy_number,
                        attempt,
                        exc,
                    )
                    if attempt < self.retry_attempts:
                        self.sleep(0.5)
            if not delivered:
                logger.error(
                    "Feishu explosive alert exhausted retries symbol=%s copy=%s",
                    result.get("symbol"),
                    copy_number,
                )
            if copy_number < self.copies:
                self.sleep(1.0)


_notifier = None
_notifier_webhook = None
_notifier_lock = threading.Lock()


def _configured_notifier():
    global _notifier, _notifier_webhook
    webhook = str(get_trading_kv_value(FEISHU_EXPLOSIVE_WEBHOOK_KEY, "") or "").strip()
    if not webhook:
        return None
    with _notifier_lock:
        if _notifier is None or webhook != _notifier_webhook:
            _notifier = ExplosiveFeishuNotifier(webhook)
            _notifier_webhook = webhook
        return _notifier


def notify_explosive_results(results: list) -> int:
    try:
        notifier = _configured_notifier()
    except Exception as exc:
        logger.warning("Unable to load explosive alert webhook from KV: %s", exc)
        return 0
    if notifier is None:
        return 0
    queued = 0
    for result in results or []:
        try:
            queued += int(notifier.notify(result))
        except Exception as exc:
            logger.warning("Unable to queue explosive alert for %s: %s", result.get("symbol"), exc)
    return queued
