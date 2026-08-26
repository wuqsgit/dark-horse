import json
import logging
import os
import tempfile
from pathlib import Path


logger = logging.getLogger(__name__)

_SUMMARY_FIELDS = {
    "initial_capital",
    "equity",
    "total_pnl",
    "unrealized_pnl",
    "position_count",
}
_ACCOUNT_FIELDS = {
    "account_id",
    "account_name",
    "environment",
    "status",
    "stale",
    "error",
    "initial_capital",
    "net_capital_adjustments",
    "wallet_balance",
    "equity",
    "available_balance",
    "unrealized_pnl",
    "total_pnl",
    "return_pct",
    "max_positions",
    "position_count",
    "normal_trading_enabled",
    "alpha_trading_enabled",
    "auto_trading_enabled",
}


def sanitize_account_status_snapshot(payload: dict) -> dict:
    """Keep only the current-position and portfolio status contract."""
    if not isinstance(payload, dict):
        return {"accounts": [], "summary": {}}

    accounts = []
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        sanitized = {key: account[key] for key in _ACCOUNT_FIELDS if key in account}
        sanitized["positions"] = [
            dict(position)
            for position in account.get("positions") or []
            if isinstance(position, dict)
        ]
        accounts.append(sanitized)

    summary = payload.get("summary")
    sanitized_payload = {
        "accounts": accounts,
        "summary": {
            key: summary[key]
            for key in _SUMMARY_FIELDS
            if isinstance(summary, dict) and key in summary
        },
    }
    if "environment_status" in payload:
        sanitized_payload["environment_status"] = payload["environment_status"]
    return sanitized_payload


def load_account_snapshot(path: str | Path) -> dict | None:
    snapshot_path = Path(path)
    try:
        with snapshot_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load account status snapshot from %s: %s", snapshot_path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("Ignoring non-object account status snapshot at %s", snapshot_path)
        return None
    return payload


def save_account_snapshot(path: str | Path, payload: dict) -> None:
    snapshot_path = Path(path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=snapshot_path.parent,
            prefix=f"{snapshot_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, snapshot_path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
