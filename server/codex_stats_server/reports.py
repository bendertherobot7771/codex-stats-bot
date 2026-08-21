from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .allocation import aggregate_by_user_machine, allocate_weekly_percent
from .database import StatsDatabase


def stats_payload(database: StatsDatabase, days: int = 7) -> dict[str, Any]:
    days = max(1, min(days, 365))
    tasks = database.tasks_since(time.time() - days * 86_400)
    allocations = allocate_weekly_percent(tasks)
    rows = aggregate_by_user_machine(tasks, allocations)
    return {
        "days": days,
        "generated_at": time.time(),
        "rows": rows,
        "active_tasks": len([task for task in tasks if task.get("status") == "active"]),
        "observed_weekly_percent": sum(row["weekly_percent"] for row in rows),
        "total_tokens": sum(row["total_tokens"] for row in rows),
    }


def telegram_stats(database: StatsDatabase, days: int = 7) -> str:
    payload = stats_payload(database, days)
    lines = [f"Статистика Codex за {days} дн."]
    if not payload["rows"]:
        lines.append("Завершённых заданий пока нет.")
    for row in payload["rows"]:
        marker = "≈" if row["estimated_tasks"] else ""
        unallocated = f", без %: {row['unallocated_tasks']}" if row["unallocated_tasks"] else ""
        lines.append(
            f"• {row['user_name']} / {row['machine_name']}: "
            f"{marker}{row['weekly_percent']:.2f}% · {_tokens(row['total_tokens'])} · "
            f"задач: {row['tasks']}{unallocated}"
        )
    lines.append(f"Наблюдаемый расход: {payload['observed_weekly_percent']:.2f}%")
    if payload["active_tasks"]:
        lines.append(f"Сейчас выполняется: {payload['active_tasks']}")
    lines.append("≈ — распределено между пересекающимися заданиями")
    return "\n".join(lines)


def telegram_active(database: StatsDatabase) -> str:
    tasks = database.active_tasks()
    if not tasks:
        return "Активных заданий нет."
    lines = ["Активные задания:"]
    now = time.time()
    for task in tasks:
        minutes = max(0, int((now - float(task["started_at"])) / 60))
        lines.append(
            f"• {task['user_name']} / {task['machine_name']} · {minutes} мин · "
            f"{_tokens(int(task['total_tokens'] or 0))}"
        )
    return "\n".join(lines)


def telegram_last(database: StatsDatabase, limit: int = 5) -> str:
    tasks = database.recent_completed(limit)
    if not tasks:
        return "Завершённых заданий пока нет."
    allocations = allocate_weekly_percent(tasks)
    lines = ["Последние задания:"]
    for task in tasks:
        allocation = allocations.get(str(task["task_id"]))
        percent = "нет замера"
        if allocation and allocation.weekly_percent is not None:
            prefix = "≈" if allocation.confidence != "direct" else ""
            percent = f"{prefix}{allocation.weekly_percent:.2f}%"
        stamp = datetime.fromtimestamp(float(task["finished_at"])).strftime("%d.%m %H:%M")
        lines.append(
            f"• {stamp} · {task['user_name']} / {task['machine_name']} · "
            f"{percent} · {_tokens(int(task['total_tokens'] or 0))}"
        )
    return "\n".join(lines)


def task_completed_message(task: dict[str, Any]) -> str:
    start = task.get("start_used_percent")
    end = task.get("end_used_percent")
    delta = None if start is None or end is None else max(0.0, float(end) - float(start))
    percent = f"{delta:.2f}% (предварительно)" if delta is not None else "нет замера"
    return (
        f"Задание завершено\n"
        f"{task['user_name']} / {task['machine_name']}\n"
        f"Токены: {_tokens(int(task['total_tokens'] or 0))}\n"
        f"Изменение недельного окна: {percent}"
    )


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} млн токенов"
    if value >= 1_000:
        return f"{value / 1_000:.1f} тыс. токенов"
    return f"{value} токенов"

