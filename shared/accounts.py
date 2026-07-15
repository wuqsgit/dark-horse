from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet

from shared.db import get_conn, init_db
from trader.config import EXCHANGE_CONFIG, TRADING_CONFIG


ROOT = Path(__file__).resolve().parents[1]
LOCAL_KEY_PATH = ROOT / ".account_secret.key"


def _fernet() -> Fernet:
    configured = os.getenv("ACCOUNT_SECRET_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except Exception:
            key = base64.urlsafe_b64encode(hashlib.sha256(configured.encode("utf-8")).digest())
            return Fernet(key)
    if not LOCAL_KEY_PATH.exists():
        LOCAL_KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(LOCAL_KEY_PATH.read_bytes().strip())


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(str(value or "").encode("utf-8")).decode("ascii") if value else ""


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(str(value or "").encode("ascii")).decode("utf-8") if value else ""


def ensure_default_account() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM trading_accounts ORDER BY is_default DESC, id LIMIT 1").fetchone()
        if row:
            return int(row["id"])
    except Exception:
        pass
    finally:
        conn.close()

    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM trading_accounts ORDER BY id LIMIT 1").fetchone()
        if row:
            return int(row["id"])
        cfg = EXCHANGE_CONFIG
        cursor = conn.execute(
            """INSERT INTO trading_accounts
               (name, environment, api_key_encrypted, api_secret_encrypted,
                initial_capital, initial_capital_time, max_positions,
                normal_trading_enabled, alpha_trading_enabled, auto_trading_enabled, enabled)
               VALUES ('默认账户', ?, ?, ?, ?, datetime('now'), ?, 1, 1, 1, 1)""",
            (
                "testnet" if cfg.get("testnet") else "prod",
                encrypt_secret(cfg.get("api_key") or ""),
                encrypt_secret(cfg.get("api_secret") or ""),
                float(TRADING_CONFIG.get("total_capital", 5000)),
                int(TRADING_CONFIG.get("max_positions", 5)),
            ),
        )
        account_id = int(cursor.lastrowid)
        conn.execute("UPDATE trading_accounts SET is_default=1 WHERE id=?", (account_id,))
        conn.commit()
        return account_id
    finally:
        conn.close()


def list_accounts(include_secrets: bool = False, enabled_only: bool = False) -> list[dict]:
    ensure_default_account()
    conn = get_conn()
    try:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM trading_accounts {where} ORDER BY is_default DESC, id")]
    finally:
        conn.close()
    for row in rows:
        key = decrypt_secret(row.pop("api_key_encrypted", ""))
        secret = decrypt_secret(row.pop("api_secret_encrypted", ""))
        row["api_key_masked"] = f"{key[:4]}****{key[-4:]}" if len(key) >= 8 else ("configured" if key else "")
        row["has_secret"] = bool(secret)
        if include_secrets:
            row["api_key"] = key
            row["api_secret"] = secret
    return rows


def get_account(account_id: int, include_secrets: bool = False) -> dict | None:
    return next((a for a in list_accounts(include_secrets=include_secrets) if int(a["id"]) == int(account_id)), None)


def account_open_position_count(account_id: int) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt
               FROM account_position_history
               WHERE account_id=? AND ABS(COALESCE(quantity, 0)) > 0""",
            (int(account_id),),
        ).fetchone()
        return int(row["cnt"] if row else 0)
    finally:
        conn.close()


def delete_account(account_id: int) -> None:
    ensure_default_account()
    account_id = int(account_id)
    conn = get_conn()
    try:
        current = conn.execute("SELECT * FROM trading_accounts WHERE id=?", (account_id,)).fetchone()
        if not current:
            raise ValueError("账户不存在")
        total = int(conn.execute("SELECT COUNT(*) FROM trading_accounts").fetchone()[0] or 0)
        if total <= 1:
            raise ValueError("至少保留1个交易账户")
        open_count = int(conn.execute(
            """SELECT COUNT(*) FROM account_position_history
               WHERE account_id=? AND ABS(COALESCE(quantity, 0)) > 0""",
            (account_id,),
        ).fetchone()[0] or 0)
        if open_count > 0:
            raise ValueError(f"账户还有持仓，无法删除（{open_count}个）")
        conn.execute("DELETE FROM trading_accounts WHERE id=?", (account_id,))
        if int(current["is_default"] or 0):
            row = conn.execute("SELECT id FROM trading_accounts ORDER BY id LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE trading_accounts SET is_default=1 WHERE id=?", (int(row["id"]),))
        conn.commit()
    finally:
        conn.close()


def save_account(payload: dict, account_id: int | None = None) -> dict:
    ensure_default_account()
    conn = get_conn()
    try:
        if account_id is None:
            if conn.execute("SELECT COUNT(*) FROM trading_accounts").fetchone()[0] >= 5:
                raise ValueError("最多只能配置5个交易账户")
            cursor = conn.execute(
                """INSERT INTO trading_accounts
                   (name, environment, api_key_encrypted, api_secret_encrypted,
                    initial_capital, initial_capital_time, max_positions,
                    max_capital_usage_pct, risk_per_trade_pct,
                    normal_trading_enabled, alpha_trading_enabled,
                    auto_trading_enabled, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(payload.get("name") or "新账户"), str(payload.get("environment") or "testnet"),
                    encrypt_secret(payload.get("api_key") or ""), encrypt_secret(payload.get("api_secret") or ""),
                    float(payload.get("initial_capital") or 0), payload.get("initial_capital_time") or None,
                    max(1, min(5, int(payload.get("max_positions") or 5))),
                    float(payload.get("max_capital_usage_pct") or 0.40), float(payload.get("risk_per_trade_pct") or 0.015),
                    int(bool(payload.get("normal_trading_enabled", True))), int(bool(payload.get("alpha_trading_enabled", True))),
                    int(bool(payload.get("auto_trading_enabled", False))), int(bool(payload.get("enabled", True))),
                ),
            )
            account_id = int(cursor.lastrowid)
        else:
            current = conn.execute("SELECT * FROM trading_accounts WHERE id=?", (account_id,)).fetchone()
            if not current:
                raise ValueError("账户不存在")
            key = encrypt_secret(payload["api_key"]) if payload.get("api_key") else current["api_key_encrypted"]
            secret = encrypt_secret(payload["api_secret"]) if payload.get("api_secret") else current["api_secret_encrypted"]
            conn.execute(
                """UPDATE trading_accounts SET name=?, environment=?, api_key_encrypted=?, api_secret_encrypted=?,
                   initial_capital=?, initial_capital_time=?, max_positions=?, max_capital_usage_pct=?, risk_per_trade_pct=?,
                   normal_trading_enabled=?, alpha_trading_enabled=?, auto_trading_enabled=?, enabled=?, updated_at=datetime('now')
                   WHERE id=?""",
                (
                    payload.get("name", current["name"]), payload.get("environment", current["environment"]), key, secret,
                    float(payload.get("initial_capital", current["initial_capital"])), payload.get("initial_capital_time", current["initial_capital_time"]),
                    max(1, min(5, int(payload.get("max_positions", current["max_positions"])))),
                    float(payload.get("max_capital_usage_pct", current["max_capital_usage_pct"])),
                    float(payload.get("risk_per_trade_pct", current["risk_per_trade_pct"])),
                    int(bool(payload.get("normal_trading_enabled", current["normal_trading_enabled"]))),
                    int(bool(payload.get("alpha_trading_enabled", current["alpha_trading_enabled"]))),
                    int(bool(payload.get("auto_trading_enabled", current["auto_trading_enabled"]))),
                    int(bool(payload.get("enabled", current["enabled"]))), int(account_id),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_account(int(account_id))


def account_exchange_config(account: dict) -> dict:
    return {
        "api_key": account.get("api_key") or "",
        "api_secret": account.get("api_secret") or "",
        "testnet": account.get("environment") == "testnet",
    }
