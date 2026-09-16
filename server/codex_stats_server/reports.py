from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from typing import Any

from .accounting import ledger, payload
from .database import StatsDatabase


def percent(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "%"


def duration(seconds: float) -> str:
    hours = max(0, int(seconds // 3600))
    return f"{hours // 24}д {hours % 24}ч"


def quota_suffix(quota: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    reset = quota.get("resets_at")
    text = "До сброса " + (duration(reset - now) if reset and reset > now else "нет свежих данных")
    count, expiries = quota.get("reset_credits_count"), quota.get("reset_credits_expire_at") or []
    if count:
        for stamp, amount in sorted(Counter(stamp for stamp in expiries if stamp > now).items()):
            text += f" · Сброс {amount} на {duration(stamp - now)}"
        unknown = max(0, count - len(expiries))
        if unknown:
            text += f" · Сброс {unknown} · срок неизвестен"
    return text


def context(database: StatsDatabase) -> tuple[list[dict], dict]:
    tasks, events = database.accounting_data()
    return tasks, ledger(tasks, events)


def stats_payload(database: StatsDatabase, days: int = 7) -> dict[str, Any]:
    days = max(1, min(days, 365))
    tasks, book = context(database)
    since = time.time() - days * 86400
    entries = [e for e in book["entries"] if e["epoch"] in book["current"].values()]
    result = payload(tasks, entries)
    result.update(days=days, generated_at=time.time(), active_tasks=sum(t["status"] == "active" for t in tasks))
    # Explicit history can exceed 100 across resets; current counters cannot.
    result["history_observed_percent"] = sum(e["percent"] for e in book["entries"] if e["at"] >= since)
    return result


def weekly_windows(database: StatsDatabase) -> list[dict[str, Any]]:
    tasks, book = context(database)
    windows = []
    for epoch, window in sorted(book["windows"].items(), key=lambda item: item[1]["started_at"], reverse=True):
        entries = [e for e in book["entries"] if e["epoch"] == epoch]
        # Keep every known PC visible with zero after an account-wide reset.
        selected = [t for t in tasks if t["account_fingerprint"] == window["account"]]
        result = payload(selected, entries)
        result.update(window)
        result["label"] = (datetime.fromtimestamp(window["started_at"]).strftime("%d.%m.%Y %H:%M") +
                           " → сброс " + datetime.fromtimestamp(window["reset_at"]).strftime("%d.%m.%Y %H:%M"))
        result["epoch"] = epoch
        windows.append(result)
    return windows


def status_footer(database: StatsDatabase) -> str:
    _, book = context(database)
    return "\n".join(quota_suffix(q) for q in book["metadata"].values()) or "До сброса нет данных"


def telegram_weeks(database: StatsDatabase, page: int = 1, page_size: int = 8) -> tuple[str, dict | None]:
    windows = weekly_windows(database)
    if not windows:
        return "Недельной статистики пока нет.\n" + status_footer(database), None
    page_count = max(1, (len(windows) + page_size - 1) // page_size)
    page = max(1, min(page, page_count))
    offset = (page - 1) * page_size
    lines = [f"Недельные окна Codex · страница {page}/{page_count}:"]
    buttons = []
    for index, window in enumerate(windows[offset:offset + page_size], offset + 1):
        lines.append(f"{index}. {window['label']} · {percent(window['observed_weekly_percent'])}")
        buttons.append({"text": str(index), "callback_data": f"week:{index}"})
    keyboard = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    navigation = []
    if page > 1:
        navigation.append({"text": "←", "callback_data": f"weeks:{page - 1}"})
    if page < page_count:
        navigation.append({"text": "→", "callback_data": f"weeks:{page + 1}"})
    if navigation:
        keyboard.append(navigation)
    lines.append(status_footer(database))
    return "\n".join(lines), {"inline_keyboard": keyboard}


def telegram_week(database: StatsDatabase, index: int = 1) -> str:
    windows = weekly_windows(database)
    if not windows:
        return "Недельной статистики пока нет.\n" + status_footer(database)
    if index < 1 or index > len(windows):
        return f"Неделя не найдена. Доступно: 1–{len(windows)}."
    window = windows[index - 1]
    lines = [f"Неделя {index}: {window['label']}"]
    for row in window["rows"]:
        marker = "≈" if row["estimated_tasks"] else ""
        lines.append(f"• {row['machine_name']}: {marker}{percent(row['weekly_percent'])}")
    lines.append(f"Всего: {percent(window['observed_weekly_percent'])}")
    if window["unallocated_percent"]:
        lines.append(f"Без достоверного интервала: {percent(window['unallocated_percent'])}")
    if any(row["estimated_tasks"] for row in window["rows"]):
        lines.append("≈ — расход пересечений распределён между ПК оценочно")
    lines.append(status_footer(database))
    return "\n".join(lines)


def telegram_stats(database: StatsDatabase, days: int = 7) -> str:
    return telegram_week(database)


def telegram_active(database: StatsDatabase) -> str:
    tasks = database.active_tasks()
    lines = ["Активные задания:" if tasks else "Активных заданий нет."]
    for task in tasks:
        minutes = max(0, int((time.time() - float(task["started_at"])) / 60))
        lines.append(f"• {task['machine_name']} · {minutes} мин")
    lines.append(status_footer(database))
    return "\n".join(lines)


def telegram_last(database: StatsDatabase, limit: int = 5) -> str:
    tasks = database.recent_completed(limit)
    return "\n\n".join(task_completed_message(task, database) for task in tasks) or "Завершённых заданий пока нет.\n" + status_footer(database)


def task_completed_message(task: dict[str, Any], database: StatsDatabase) -> str:
    tasks, book = context(database)
    tid, machine, account = str(task["task_id"]), str(task["machine_id"]), task["account_fingerprint"]
    bounds = book["boundaries"].get(tid, {})
    start, end = bounds.get("start"), bounds.get("end")
    quota = book["metadata"].get(account, {})
    epoch = book["current"].get(account)
    entries = [e for e in book["entries"] if e["epoch"] == epoch]
    week_total = sum(e["machines"].get(machine, 0) for e in entries)
    task_entries = [e for e in book["entries"] if tid in e["tasks"]]
    credited = sum(e["tasks"][tid] for e in task_entries if e["epoch"] == epoch)
    estimated = any(e["estimated"] for e in task_entries)
    before, after = percent(100 - start[2]) if start else "нет замера", percent(100 - end[2]) if end else "нет замера"
    change = "нет замера"
    crossed = start and end and any(start[0] < w["started_at"] <= end[0] for w in book["windows"].values() if w["account"] == account)
    if start and end:
        change = percent(end[2] - start[2]) if not crossed and end[2] >= start[2] else "сброс/смена окна"
    credit_text = ("≈" if estimated else "") + percent(credited) if start and end else "нет замера"
    lines = [f"{task['machine_name']} · {percent(week_total)} за неделю · {quota_suffix(quota)}",
             f"До задания: {before} >>> После задания: {after}",
             f"Изменение за время задания: {change} Учтено за {task['machine_name']}: {credit_text}"]
    others = sorted({t["machine_name"] for t in tasks if t["account_fingerprint"] == account
                     and str(t["machine_id"]) != machine and float(t["started_at"]) < float(task["finished_at"])
                     and (float(t["finished_at"]) if t.get("finished_at") is not None else
                          book["activity"].get(str(t["task_id"]), float(t["started_at"])) + 120) > float(task["started_at"])})
    if others:
        lines.extend([f"Во время задания также работал {', '.join(others)}.",
                      "Расход пересечения распределён между ПК оценочно."])
    return "\n".join(lines)
