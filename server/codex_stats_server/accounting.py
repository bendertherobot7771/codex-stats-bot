"""Replay shared account snapshots, never charge the same quota twice."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def sample(quota: dict, boundary: float | None) -> tuple | None:
    used, captured, reset = (number(quota.get(key)) for key in ("used_percent", "captured_at", "resets_at"))
    if used is None or not 0 <= used <= 100 or not reset or captured is None:
        return None
    if quota.get("window_minutes") != 10080:
        return None
    if boundary is not None:
        if abs(captured - boundary) > 30:
            return None
    return captured, int(reset), used, quota.get("source") == "oauth_usage_endpoint"


def union_duration(intervals: list[tuple[float, float]]) -> float:
    end, total = float("-inf"), 0.0
    for left, right in sorted(intervals):
        total += max(0.0, right - max(left, end))
        end = max(end, right)
    return total


def split_percent(amount: float, weights: dict[str, float]) -> dict[str, float]:
    """Largest remainder in hundredths: displayed PC shares also conserve the total."""
    units = round(amount * 100)
    total = sum(weights.values())
    exact = {key: units * weight / total for key, weight in weights.items()}
    shares = {key: int(value) for key, value in exact.items()}
    order = sorted(weights, key=lambda key: (-(exact[key] - shares[key]), key))
    for key in order[:units - sum(shares.values())]:
        shares[key] += 1
    return {key: value / 100 for key, value in shares.items()}


def ledger(tasks: list[dict], events: list[dict]) -> dict:
    samples = defaultdict(dict)
    metadata, boundaries, credits, activity = {}, defaultdict(dict), {}, {}
    for event in events:
        account, tid = str(event.get("account_fingerprint", "unknown")), str(event.get("task_id"))
        kind = event.get("event_type")
        start, finish = number(event.get("started_at")), number(event.get("finished_at"))
        observed = number(event.get("sent_at")) or number((event.get("quota") or {}).get("captured_at")) or start or 0
        if kind == "task_started":
            observed = start or observed
        if kind != "quota_snapshot":
            activity[tid] = max(activity.get(tid, 0), observed)
        at = start if kind == "task_started" else finish if kind == "task_completed" else None
        candidates = [(event.get("start_quota") or {}, start, "start"),
                      (event.get("quota") or {}, at, "end" if kind == "task_completed" else "current")]
        for quota, boundary, label in candidates:
            captured = number(quota.get("captured_at")) or 0
            if quota.get("reset_credits_count") is not None and captured > credits.get(account, (float("-inf"), {}))[0]:
                credits[account] = (captured, {key: quota.get(key) for key in ("reset_credits_count", "reset_credits_expire_at")})
            old = metadata.get(account, {})
            rank = (quota.get("source") == "oauth_usage_endpoint", captured)
            old_rank = (old.get("source") == "oauth_usage_endpoint", number(old.get("captured_at")) or 0)
            if rank > old_rank:
                metadata[account] = dict(quota)
            parsed = sample(quota, boundary)
            if parsed is None:
                continue
            stamp, reset, used, direct = parsed
            point = (reset, used, direct)
            # Prefer direct API over log samples, then the greatest simultaneous observation.
            previous = samples[account].get(stamp)
            if previous is None or (direct, used) > (previous[2], previous[1]):
                samples[account][stamp] = point
            if label in ("start", "end"):
                boundaries[tid][label] = (stamp, reset, used)
    entries, windows, current = [], {}, {}
    for account, points in samples.items():
        has_direct = any(point[2] for point in points.values())
        ordered = [(stamp, point) for stamp, point in sorted(points.items()) if point[2] or not has_direct]
        previous, epoch, spent, pending = None, None, 0.0, None
        for right, (reset, used, direct) in ordered:
            changed = previous is None or reset != previous[1]
            if not changed and direct and used < previous[2]:
                if pending is None:
                    pending = (right, reset, used)
                    continue
                # Two fresh lower readings confirm an unscheduled reset. A single
                # lagging response from another PC must not reset everybody's counter.
                stamp, _, baseline = pending
                windows[current[account]]["ended_at"] = stamp
                epoch = (account, stamp)
                windows[epoch] = {"account": account, "reset_at": reset, "started_at": stamp,
                                  "ended_at": None, "baseline": min(baseline, used)}
                current[account], spent = epoch, 0.0
                previous = (stamp, reset, min(baseline, used))
                pending = None
            else:
                pending = None
            if changed:
                epoch = (account, right)
                windows[epoch] = {"account": account, "reset_at": reset, "started_at": right,
                                  "ended_at": None, "baseline": used}
                if previous is not None:
                    windows[current[account]]["ended_at"] = right
                current[account], spent = epoch, 0.0
                previous = (right, reset, used)
                continue
            left, _, high = previous
            delta = round(min(max(0.0, used - high), max(0.0, 100.0 - spent)), 2)
            if delta > 0 and right > left:
                candidates, machine_intervals = [], defaultdict(list)
                for task in tasks:
                    if str(task.get("account_fingerprint")) != account:
                        continue
                    begin = max(left, float(task["started_at"]))
                    finish_sample = boundaries.get(str(task["task_id"]), {}).get("end")
                    task_end = (max(float(task["finished_at"]), finish_sample[0] if finish_sample else 0)
                                if task.get("finished_at") is not None
                                else activity.get(str(task["task_id"]), float(task["started_at"])) + 120)
                    end = min(right, task_end)
                    if end > begin:
                        candidates.append((task, begin, end))
                        machine_intervals[str(task["machine_id"])].append((begin, end))
                covered = union_duration([(a, b) for _, a, b in candidates])
                entry = {"epoch": epoch, "account": account, "reset_at": reset, "at": right,
                         "percent": delta, "machines": {}, "tasks": {},
                         "estimated": len(machine_intervals) > 1, "unallocated": 0.0}
                if covered < right - left - 0.001 or not candidates:
                    entry["unallocated"] = delta
                else:
                    weights = {m: union_duration(spans) for m, spans in machine_intervals.items()}
                    for machine, share in split_percent(delta, weights).items():
                        entry["machines"][machine] = share
                        matching = [(t, b - a) for t, a, b in candidates if str(t["machine_id"]) == machine]
                        entry["tasks"].update(split_percent(share, {str(t["task_id"]): d for t, d in matching}))
                spent = round(spent + delta, 2)
                entries.append(entry)
            previous = (right, reset, max(high, used))
    for account, (_, info) in credits.items():
        metadata.setdefault(account, {}).update(info)
    return {"entries": entries, "windows": windows, "current": current,
            "metadata": metadata, "boundaries": dict(boundaries), "activity": activity}


def payload(tasks: list[dict], entries: list[dict]) -> dict:
    rows = {}
    for task in tasks:
        machine = str(task["machine_id"])
        row = rows.setdefault(machine, {"machine_id": machine, "machine_name": task["machine_name"],
                                      "user_name": task["user_name"], "tasks": 0, "weekly_percent": 0.0,
                                      "estimated_tasks": 0, "unallocated_tasks": 0, "total_tokens": 0})
        row["tasks"] += 1
        row["total_tokens"] += int(task.get("total_tokens") or 0)  # Legacy API compatibility only.
    for entry in entries:
        for machine, amount in entry["machines"].items():
            if machine in rows:
                rows[machine]["weekly_percent"] += amount
                rows[machine]["estimated_tasks"] += int(entry["estimated"])
    for row in rows.values():
        row["weekly_percent"] = round(row["weekly_percent"], 2)
    return {"rows": sorted(rows.values(), key=lambda row: -row["weekly_percent"]), "tasks": len(tasks),
            "observed_weekly_percent": round(sum(e["percent"] for e in entries), 2),
            "unallocated_percent": round(sum(e["unallocated"] for e in entries), 2),
            "total_tokens": sum(row["total_tokens"] for row in rows.values())}
