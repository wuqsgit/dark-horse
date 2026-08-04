from __future__ import annotations


def replay_metrics(
    rows: list[dict],
    *,
    round_trip_cost_r: float = 0.03,
) -> dict:
    labels = [row for row in rows if row.get("label")]
    events = [row for row in rows if row.get("action_type") not in (None, "NONE")]
    watched = [row for row in rows if str(row.get("to_state", "")).startswith("WATCH")]
    probes = [row for row in events if row.get("action_type") == "PROBE_LONG"]
    successes = sum(int((row.get("label") or {}).get("followthrough") or 0) for row in probes)
    fakeouts = sum(int((row.get("label") or {}).get("fakeout") or 0) for row in probes)
    large_moves = [
        row for row in labels
        if float((row.get("label") or {}).get("mfe_r") or 0) >= 2
    ]
    captured_ids = {
        row.get("snapshot_id")
        for row in events
        if float((row.get("label") or {}).get("mfe_r") or 0) >= 2
    }
    watched_successes = sum(
        int((row.get("label") or {}).get("setup_success") or 0)
        for row in watched
    )
    realized = []
    for row in events:
        label = row.get("label") or {}
        if int(label.get("followthrough") or 0):
            gross = min(2.0, float(label.get("mfe_r") or 0))
        else:
            gross = max(-1.0, float(label.get("mae_r") or 0))
        realized.append(gross - max(0.0, float(round_trip_cost_r)))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    consecutive_losses = 0
    max_consecutive_losses = 0
    for value in realized:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        consecutive_losses = consecutive_losses + 1 if value < 0 else 0
        max_consecutive_losses = max(
            max_consecutive_losses,
            consecutive_losses,
        )
    return {
        "bars": len(rows),
        "watch_count": len(watched),
        "action_count": len(events),
        "probe_count": len(probes),
        "probe_precision": round(successes / len(probes), 4) if probes else 0.0,
        "watch_precision": (
            round(watched_successes / len(watched), 4)
            if watched
            else 0.0
        ),
        "fakeout_rate": round(fakeouts / len(probes), 4) if probes else 0.0,
        "large_move_count": len(large_moves),
        "large_move_recall": (
            round(len(captured_ids) / len(large_moves), 4)
            if large_moves
            else 0.0
        ),
        "net_expected_r": (
            round(sum(realized) / len(realized), 6)
            if realized
            else 0.0
        ),
        "max_drawdown_r": round(max_drawdown, 6),
        "max_consecutive_losses": max_consecutive_losses,
    }
