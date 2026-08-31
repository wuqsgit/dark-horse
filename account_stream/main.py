from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from account_stream.service import AccountSynchronizer
from shared.accounts import list_accounts
from shared.db import init_db, upsert_service_runtime_status
from shared.live_account_store import update_account_stream_state


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("account_stream")
RECONCILE_INTERVAL = float(os.getenv("ACCOUNT_STREAM_RECONCILE_SECONDS", "10"))


def _signature(account: dict) -> tuple:
    return (
        account.get("environment"),
        account.get("api_key"),
        account.get("api_secret"),
        bool(account.get("enabled")),
    )


def report_runtime_status(status: str, **fields) -> bool:
    """Keep telemetry write failures from stopping account reconciliation."""
    try:
        upsert_service_runtime_status(
            "account_stream",
            status=status,
            **fields,
        )
        return True
    except Exception as exc:
        logger.warning("account stream runtime status write failed: %s", exc)
        return False


async def run_supervisor(stop: asyncio.Event) -> None:
    tasks: dict[int, asyncio.Task] = {}
    signatures: dict[int, tuple] = {}
    child_stops: dict[int, asyncio.Event] = {}
    logger.info("account stream supervisor started")
    report_runtime_status(
        "starting",
        details={"reconcile_seconds": RECONCILE_INTERVAL},
    )
    while not stop.is_set():
        try:
            accounts = list_accounts(include_secrets=True, enabled_only=True)
            active_ids = set()
            for account in accounts:
                account_id = int(account["id"])
                active_ids.add(account_id)
                signature_value = _signature(account)
                task = tasks.get(account_id)
                if (
                    task is not None
                    and not task.done()
                    and signatures.get(account_id) == signature_value
                ):
                    continue
                if task is not None:
                    child_stops[account_id].set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                try:
                    child_stop = asyncio.Event()
                    synchronizer = AccountSynchronizer(
                        account,
                        reconcile_interval=RECONCILE_INTERVAL,
                    )
                    tasks[account_id] = asyncio.create_task(
                        synchronizer.run(child_stop)
                    )
                    child_stops[account_id] = child_stop
                    signatures[account_id] = signature_value
                except Exception as exc:
                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    update_account_stream_state(
                        account_id,
                        status="error",
                        ws_connected=0,
                        last_error_at=now,
                        last_error=f"{type(exc).__name__}: {exc}",
                    )
            for account_id in set(tasks) - active_ids:
                child_stops[account_id].set()
                tasks[account_id].cancel()
                await asyncio.gather(tasks[account_id], return_exceptions=True)
                tasks.pop(account_id, None)
                child_stops.pop(account_id, None)
                signatures.pop(account_id, None)
            report_runtime_status(
                "ok",
                details={
                    "account_count": len(active_ids),
                    "reconcile_seconds": RECONCILE_INTERVAL,
                },
            )
        except Exception as exc:
            logger.exception("account stream supervisor failed")
            report_runtime_status(
                "error",
                error_code="account_stream_supervisor_failed",
                last_error=f"{type(exc).__name__}: {exc}",
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    for child_stop in child_stops.values():
        child_stop.set()
    for task in tasks.values():
        task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)


async def main() -> None:
    init_db()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await run_supervisor(stop)


if __name__ == "__main__":
    asyncio.run(main())
