#!/usr/bin/env bash
set -Eeuo pipefail

# Export only trading_accounts from the local SQLite database, transfer the
# encrypted rows and their Fernet key over SSH, then import and restart DarkHorse.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
DEFAULT_REMOTE_HOST="root@47.116.79.90"
DEFAULT_REMOTE_DIR="/var/www/dark-horse/dark-horse"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: $0 [ssh-host] [remote-app-dir]"
  echo "Default: $0 $DEFAULT_REMOTE_HOST $DEFAULT_REMOTE_DIR"
  exit 0
fi

REMOTE_HOST="${1:-${DARK_HORSE_REMOTE_HOST:-$DEFAULT_REMOTE_HOST}}"
REMOTE_DIR="${2:-${DARK_HORSE_REMOTE_DIR:-$DEFAULT_REMOTE_DIR}}"
LOCAL_DB="${DARK_HORSE_LOCAL_DB:-$ROOT_DIR/alphadog.db}"
LOCAL_KEY="${DARK_HORSE_LOCAL_ACCOUNT_KEY:-$ROOT_DIR/.account_secret.key}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

for command_name in ssh scp "$PYTHON_BIN"; do
  command -v "$command_name" >/dev/null 2>&1 || fail "missing command: $command_name"
done

[[ -f "$LOCAL_DB" ]] || fail "local database not found: $LOCAL_DB"
[[ -s "$LOCAL_KEY" ]] || fail "local account key not found: $LOCAL_KEY"
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "unsafe remote directory: $REMOTE_DIR"

# When ACCOUNT_SECRET_KEY is configured, the local key file is not necessarily
# the key that encrypted the rows. Refuse instead of uploading undecryptable data.
if [[ -n "${ACCOUNT_SECRET_KEY:-}" ]]; then
  fail "local ACCOUNT_SECRET_KEY is set; unset it before using key-file sync"
fi
if [[ -f "$ROOT_DIR/.env" ]] && grep -Eq '^[[:space:]]*ACCOUNT_SECRET_KEY=[[:space:]]*[^[:space:]#]+' "$ROOT_DIR/.env"; then
  fail "local .env contains ACCOUNT_SECRET_KEY; use the same value on the server or clear it before key-file sync"
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/darkhorse-account-sync.XXXXXX")"
chmod 700 "$TEMP_DIR"
SQL_FILE="$TEMP_DIR/trading_accounts.sql"
ID_FILE="$TEMP_DIR/trading_account_ids.txt"
KEY_FILE="$TEMP_DIR/account_secret.key"
REMOTE_TEMP="/tmp/darkhorse-account-sync-$(id -u)-$$"
REMOTE_CREATED=0

cleanup() {
  rm -rf "$TEMP_DIR"
  if [[ "$REMOTE_CREATED" == "1" ]]; then
    ssh -o BatchMode=yes "$REMOTE_HOST" "rm -rf '$REMOTE_TEMP'" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "Exporting trading_accounts from $LOCAL_DB ..."
"$PYTHON_BIN" - "$LOCAL_DB" "$SQL_FILE" "$ID_FILE" <<'PY'
import os
import sqlite3
import sys

db_path, output_path, id_path = sys.argv[1:4]
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
try:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(trading_accounts)")]
    if not columns:
        raise SystemExit("trading_accounts table does not exist")
    rows = conn.execute("SELECT * FROM trading_accounts ORDER BY id").fetchall()
    if not rows:
        raise SystemExit("trading_accounts table is empty")
    if sum(1 for row in rows if bool(row["is_default"])) != 1:
        raise SystemExit("trading_accounts must contain exactly one default account")

    def identifier(value):
        return '"' + str(value).replace('"', '""') + '"'

    def literal(value):
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            return "X'" + value.hex() + "'"
        if isinstance(value, (int, float)):
            return repr(value)
        return "'" + str(value).replace("'", "''") + "'"

    quoted_columns = ", ".join(identifier(column) for column in columns)
    update_columns = [column for column in columns if column != "id"]
    updates = ", ".join(
        f"{identifier(column)}=excluded.{identifier(column)}"
        for column in update_columns
    )

    with open(output_path, "w", encoding="utf-8") as output:
        for row in rows:
            values = ", ".join(literal(row[column]) for column in columns)
            output.write(
                f"INSERT INTO trading_accounts ({quoted_columns}) VALUES ({values}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates};\n"
            )
    os.chmod(output_path, 0o600)
    with open(id_path, "w", encoding="ascii") as output:
        output.write("\n".join(str(int(row["id"])) for row in rows) + "\n")
    os.chmod(id_path, 0o600)
    configured = sum(
        1 for row in rows
        if bool(row["api_key_encrypted"]) and bool(row["api_secret_encrypted"])
    )
    print(f"Exported {len(rows)} account(s); {configured} contain encrypted AK/SK.")
finally:
    conn.close()
PY

cp "$LOCAL_KEY" "$KEY_FILE"
chmod 600 "$KEY_FILE"

if [[ "${DARK_HORSE_SYNC_DRY_RUN:-0}" == "1" ]]; then
  echo "Dry run complete; no SSH connection or server change was made."
  exit 0
fi

echo "Uploading encrypted account bundle to $REMOTE_HOST ..."
ssh "$REMOTE_HOST" "mkdir -m 700 '$REMOTE_TEMP'"
REMOTE_CREATED=1
scp -q "$SQL_FILE" "$ID_FILE" "$KEY_FILE" "$REMOTE_HOST:$REMOTE_TEMP/"

echo "Importing accounts into $REMOTE_DIR ..."
ssh "$REMOTE_HOST" "bash -s -- '$REMOTE_DIR' '$REMOTE_TEMP'" <<'REMOTE_SCRIPT'
set -Eeuo pipefail

APP_DIR="$1"
SYNC_DIR="$2"
DB_PATH="$APP_DIR/alphadog.db"
NEW_KEY="$SYNC_DIR/account_secret.key"
SQL_FILE="$SYNC_DIR/trading_accounts.sql"
ID_FILE="$SYNC_DIR/trading_account_ids.txt"
CURRENT_KEY="$APP_DIR/.account_secret.key"
ORIGINAL_KEY="$SYNC_DIR/original_account_secret.key"
KEY_INSTALLED=0
IMPORT_COMMITTED=0
HAD_ORIGINAL_KEY=0

cleanup_remote() {
  result=$?
  if [[ "$result" != "0" && "$KEY_INSTALLED" == "1" && "$IMPORT_COMMITTED" == "0" ]]; then
    if [[ "$HAD_ORIGINAL_KEY" == "1" ]]; then
      install -m 600 "$ORIGINAL_KEY" "$CURRENT_KEY"
    else
      rm -f "$CURRENT_KEY"
    fi
  fi
  rm -rf "$SYNC_DIR"
  exit "$result"
}
trap cleanup_remote EXIT

[[ -d "$APP_DIR" ]] || { echo "ERROR: remote app directory not found: $APP_DIR" >&2; exit 1; }
[[ -f "$DB_PATH" ]] || { echo "ERROR: remote database not found: $DB_PATH" >&2; exit 1; }
[[ -s "$NEW_KEY" && -s "$SQL_FILE" && -s "$ID_FILE" ]] || { echo "ERROR: account bundle is incomplete" >&2; exit 1; }

cd "$APP_DIR"

if [[ -n "${ACCOUNT_SECRET_KEY:-}" ]]; then
  echo "ERROR: remote shell has ACCOUNT_SECRET_KEY set; it would override .account_secret.key" >&2
  exit 1
fi
if [[ -f .env ]] && grep -Eq '^[[:space:]]*ACCOUNT_SECRET_KEY=[[:space:]]*[^[:space:]#]+' .env; then
  echo "ERROR: remote .env contains ACCOUNT_SECRET_KEY; clear it before key-file sync" >&2
  exit 1
fi

if [[ -x .venv/bin/python ]]; then
  APP_PYTHON="$APP_DIR/.venv/bin/python"
elif command -v python3.11 >/dev/null 2>&1; then
  APP_PYTHON="$(command -v python3.11)"
else
  APP_PYTHON="$(command -v python3)"
fi

# All existing remote account IDs must be included in the local export. Rows
# encrypted with the old remote key cannot safely remain after key replacement.
"$APP_PYTHON" - "$DB_PATH" "$ID_FILE" <<'PY'
import sqlite3
import sys

db_path, id_path = sys.argv[1:3]
with open(id_path, "r", encoding="ascii") as source:
    imported_ids = {int(value.strip()) for value in source if value.strip()}
conn = sqlite3.connect(db_path)
try:
    remote_ids = {int(row[0]) for row in conn.execute("SELECT id FROM trading_accounts")}
finally:
    conn.close()
extra_ids = sorted(remote_ids - imported_ids)
if extra_ids:
    raise SystemExit(
        "remote contains account IDs absent from the local export: "
        + ", ".join(map(str, extra_ids))
    )
print(f"Remote account preflight passed: {len(remote_ids)} existing account(s).")
PY

if [[ -f "$CURRENT_KEY" ]]; then
  cp -p "$CURRENT_KEY" "$ORIGINAL_KEY"
  HAD_ORIGINAL_KEY=1
fi
install -m 600 "$NEW_KEY" "$CURRENT_KEY"
KEY_INSTALLED=1

"$APP_PYTHON" - "$DB_PATH" "$SQL_FILE" <<'PY'
import sqlite3
import sys

db_path, sql_path = sys.argv[1:3]
with open(sql_path, "r", encoding="utf-8") as source:
    statements = source.read()

conn = sqlite3.connect(db_path, timeout=30)
try:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        "BEGIN IMMEDIATE;\n"
        "UPDATE trading_accounts SET is_default=0;\n"
        + statements
        + "\nCOMMIT;"
    )
except Exception:
    if conn.in_transaction:
        conn.rollback()
    raise
finally:
    conn.close()
PY
IMPORT_COMMITTED=1

"$APP_PYTHON" - <<'PY'
from shared.accounts import list_accounts

accounts = list_accounts(include_secrets=True)
if not accounts:
    raise SystemExit("no trading accounts found after import")
for account in accounts:
    if account.get("enabled") and (not account.get("api_key") or not account.get("api_secret")):
        raise SystemExit(f"enabled account {account['id']} cannot decrypt AK/SK")
print(
    "Imported accounts:",
    [
        {
            "id": row["id"],
            "name": row["name"],
            "environment": row["environment"],
            "default": bool(row.get("is_default")),
            "has_ak": bool(row.get("api_key")),
            "has_sk": bool(row.get("api_secret")),
        }
        for row in accounts
    ],
)
PY

echo "Restarting DarkHorse ..."
./start.sh
echo "trading_accounts sync completed successfully."
REMOTE_SCRIPT

REMOTE_CREATED=0
echo "Done. trading_accounts were imported and the server was restarted."
