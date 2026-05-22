import json
import os
from datetime import datetime

TASKS_FILE = "tasks.json"
STATS_FILE = "stats.json"


# ─── TASKS ────────────────────────────────────────────────────────────────────

def load_tasks() -> list[dict]:
    if not os.path.exists(TASKS_FILE):
        return []
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_tasks(tasks: list[dict]) -> None:
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def get_task_by_id(task_id: str) -> dict | None:
    return next((t for t in load_tasks() if t["id"] == task_id), None)


def update_task(task_id: str, fields: dict) -> bool:
    """Оновлює конкретні поля задачі за id. Повертає True якщо знайдено."""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t.update(fields)
            save_tasks(tasks)
            return True
    return False


def delete_task(task_id: str) -> dict | None:
    """Видаляє задачу за id. Повертає видалену задачу або None."""
    tasks = load_tasks()
    for i, t in enumerate(tasks):
        if t["id"] == task_id:
            removed = tasks.pop(i)
            save_tasks(tasks)
            return removed
    return None


def get_tasks_for_date(date_str: str) -> list[dict]:
    """
    Повертає всі задачі для конкретної дати.
    Враховує звичайні задачі (due_date == date_str)
    і повторювані (weekday збігається і дата в межах until).
    """
    tasks = load_tasks()
    result = []
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    for t in tasks:
        if t.get("active") is False:
            continue

        repeat = t.get("repeat", {})

        if repeat.get("enabled"):
            # Перевіряємо чи дата в межах until
            until_str = repeat.get("until")
            if until_str:
                until = datetime.strptime(until_str, "%Y-%m-%d").date()
                if target > until:
                    continue
            # Перевіряємо чи weekday збігається (0=пн .. 6=нд)
            if target.weekday() in repeat.get("weekdays", []):
                result.append(t)
        else:
            if t.get("due_date") == date_str:
                result.append(t)

    return result


def mark_done_for_date(task_id: str, date_str: str) -> bool:
    """Позначає задачу як виконану для конкретної дати."""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            done_dates = t.setdefault("done_dates", [])
            if date_str not in done_dates:
                done_dates.append(date_str)
            save_tasks(tasks)
            return True
    return False


def mark_missed_for_date(task_id: str, date_str: str) -> bool:
    """Позначає задачу як пропущену для конкретної дати."""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            missed = t.setdefault("missed_dates", [])
            if date_str not in missed:
                missed.append(date_str)
            save_tasks(tasks)
            return True
    return False


def is_done_for_date(task: dict, date_str: str) -> bool:
    return date_str in task.get("done_dates", [])


def is_missed_for_date(task: dict, date_str: str) -> bool:
    return date_str in task.get("missed_dates", [])


# ─── STATS ────────────────────────────────────────────────────────────────────

def load_stats() -> dict:
    """
    Структура:
    {
      "2025-05-22": {"done": 3, "missed": 1},
      ...
    }
    """
    if not os.path.exists(STATS_FILE):
        return {}
    with open(STATS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_stats(stats: dict) -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def record_stat(date_str: str, done: int = 0, missed: int = 0) -> None:
    """Додає до статистики за дату."""
    stats = load_stats()
    day = stats.setdefault(date_str, {"done": 0, "missed": 0})
    day["done"] += done
    day["missed"] += missed
    save_stats(stats)


def get_week_stats(week_dates: list[str]) -> dict:
    """Повертає статистику за список дат."""
    stats = load_stats()
    return {d: stats.get(d, {"done": 0, "missed": 0}) for d in week_dates}