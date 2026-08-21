from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True)
class Allocation:
    task_id: str
    weekly_percent: float | None
    confidence: str
    overlap_size: int


def allocate_weekly_percent(tasks: Iterable[dict[str, Any]]) -> dict[str, Allocation]:
    """Allocate each observed account-window delta once across overlapping tasks."""
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    allocations: dict[str, Allocation] = {}

    for task in tasks:
        if task.get("status") != "completed" or task.get("finished_at") is None:
            continue
        reset = int(task.get("start_reset_at") or task.get("end_reset_at") or 0)
        grouped[(str(task.get("account_fingerprint") or "unknown"), reset)].append(task)

    for window_tasks in grouped.values():
        for component in _overlap_components(window_tasks):
            allocations.update(_allocate_component(component))
    return allocations


def aggregate_by_user_machine(
    tasks: Iterable[dict[str, Any]], allocations: dict[str, Allocation]
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for task in tasks:
        if task.get("status") != "completed":
            continue
        key = (
            str(task.get("user_name") or "unknown"),
            str(task.get("machine_id") or "unknown"),
            str(task.get("machine_name") or "unknown"),
        )
        row = rows.setdefault(
            key,
            {
                "user_name": key[0],
                "machine_id": key[1],
                "machine_name": key[2],
                "tasks": 0,
                "total_tokens": 0,
                "weekly_percent": 0.0,
                "estimated_tasks": 0,
                "unallocated_tasks": 0,
            },
        )
        row["tasks"] += 1
        row["total_tokens"] += int(task.get("total_tokens") or 0)
        allocation = allocations.get(str(task["task_id"]))
        if not allocation or allocation.weekly_percent is None:
            row["unallocated_tasks"] += 1
        else:
            row["weekly_percent"] += allocation.weekly_percent
            if allocation.confidence != "direct":
                row["estimated_tasks"] += 1
    return sorted(rows.values(), key=lambda row: (-row["weekly_percent"], -row["total_tokens"]))


def _overlap_components(tasks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(tasks, key=lambda task: (float(task["started_at"]), str(task["task_id"])))
    components: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_end = float("-inf")
    for task in ordered:
        started = float(task["started_at"])
        finished = float(task["finished_at"])
        if current and started > current_end:
            components.append(current)
            current = []
            current_end = float("-inf")
        current.append(task)
        current_end = max(current_end, finished)
    if current:
        components.append(current)
    return components


def _allocate_component(component: list[dict[str, Any]]) -> dict[str, Allocation]:
    start_resets = {task.get("start_reset_at") for task in component if task.get("start_reset_at") is not None}
    end_resets = {task.get("end_reset_at") for task in component if task.get("end_reset_at") is not None}
    reset_mismatch = len(start_resets | end_resets) > 1
    start_values = [float(task["start_used_percent"]) for task in component if task.get("start_used_percent") is not None]
    end_values = [float(task["end_used_percent"]) for task in component if task.get("end_used_percent") is not None]

    if reset_mismatch or not start_values or not end_values:
        reason = "reset_crossed" if reset_mismatch else "token_only"
        return {
            str(task["task_id"]): Allocation(str(task["task_id"]), None, reason, len(component))
            for task in component
        }

    observed_delta = max(0.0, max(end_values) - min(start_values))
    weights = [max(0, int(task.get("total_tokens") or 0)) for task in component]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1] * len(component)
        weight_sum = len(component)

    confidence = "direct" if len(component) == 1 else "allocated_overlap"
    return {
        str(task["task_id"]): Allocation(
            task_id=str(task["task_id"]),
            weekly_percent=observed_delta * weight / weight_sum,
            confidence=confidence,
            overlap_size=len(component),
        )
        for task, weight in zip(component, weights, strict=True)
    }

