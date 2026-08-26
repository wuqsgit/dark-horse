import json
import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)


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
    temporary_path = snapshot_path.with_name(f"{snapshot_path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, snapshot_path)
