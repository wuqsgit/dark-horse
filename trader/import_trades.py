import sys, os
print("⛔ 此文件已禁用 — 手动导入会污染 trades 表。")
print("   数据应由 system 回调 + income_auto 补录提供。")
sys.exit(1)

import sys, os, time, json, urllib.request, ssl, hashlib, hmac, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from shared.db import get_conn
from datetime import datetime, timezone
from collections import defaultdict

API_KEY = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_API_SECRET"]
BASE = "https://testnet.binancefuture.com"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def signed_get(path, params):
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    q = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode("utf-8"), q.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{q}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": API_KEY})
    with urllib.request.urlopen(req, context=ctx) as r:
        return json.loads(r.read())

all_inc = []
for _ in range(40):
    data = signed_get("/fapi/v1/income", {"limit": 500, "startTime": int(time.time()*1000) - 95*86400_000})
    if not data: break
    all_inc.extend(data)
    if len(data) < 500: break
    time.sleep(0.15)

trade_inc = [i for i in all_inc if i["incomeType"] == "REALIZED_PNL"]
groups = defaultdict(list)
for i in trade_inc:
    sym = i.get("symbol", "")
    if not sym: continue
    groups[sym].append(i)
for sym in groups:
    groups[sym].sort(key=lambda x: x["time"])

merged = []
for sym, items in groups.items():
    cur = {"symbol": sym, "times": [items[0]["time"]], "pnl": float(items[0]["income"])}
    for it in items[1:]:
        if it["time"] - cur["times"][-1] < 7200_000:
            cur["times"].append(it["time"])
            cur["pnl"] += float(it["income"])
        else:
            merged.append(cur)
            cur = {"symbol": sym, "times": [it["time"]], "pnl": float(it["income"])}
    merged.append(cur)

conn = get_conn()
existing = set()
for r in conn.execute("SELECT symbol, exit_time, ROUND(pnl,2) FROM trades").fetchall():
    existing.add(f"{r[0]}|{r[1]}|{r[2]}")

inserted = 0
for t in merged:
    sym = t["symbol"]
    pnl = round(t["pnl"], 2)
    avg_ms = sum(t["times"]) // len(t["times"])
    exit_dt = datetime.fromtimestamp(avg_ms/1000, tz=timezone.utc)
    exit_ts = exit_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    dedup = f"{sym.replace('USDT','')}|{exit_ts}|{pnl}"
    if dedup in existing: continue
    try:
        kdata = signed_get("/fapi/v1/klines", {"symbol": sym, "interval": "1h", "limit": 4})
        ks = [float(k[4]) for k in kdata[-4:]]
        exit_p = round(ks[-1], 6) if ks else 0
        entry_p = round((ks[0] + ks[-1]) / 2, 6) if ks else 0
    except:
        entry_p = 0; exit_p = 0
    side = "LONG" if pnl > 0 else "SHORT"
    pnl_pct = round(pnl / (entry_p or 1) * 100, 2) if entry_p > 0 else 0
    qty = round(abs(pnl) / (abs(entry_p - exit_p) or 0.01), 3)
    try:
        conn.execute("""INSERT INTO trades(symbol, side, quantity, entry_price, exit_price, pnl, pnl_pct, exit_reason, entry_time, exit_time, grade_at_entry, score_at_entry) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sym.replace("USDT",""), side, max(qty, 0.001), entry_p, exit_p, pnl, pnl_pct, "historical_import", exit_ts, exit_ts, "", ""))
        inserted += 1
    except: pass

conn.commit()
total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
print(f"OK: +{inserted} trades, total={total}")
conn.close()
