import os
import sys
import uuid
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import subprocess

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

from storage import (
    load_tasks, save_tasks, delete_task, update_task,
    get_tasks_for_date, mark_done_for_date, mark_missed_for_date,
    is_done_for_date, is_missed_for_date,
    record_stat, get_week_stats,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kiev")

# ─── ConversationHandler стани ─────────────────────────────────────────────
STEP_TEXT = 1
STEP_DATE = 2
STEP_TIME = 3
STEP_REPEAT = 4

STEP_REMOVE_PICK = 10

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
WEEKDAY_NAMES_FULL = ["понеділок", "вівторок", "середу", "четвер", "п'ятницю", "суботу", "неділю"]


# ─── Guard ──────────────────────────────────────────────────────────────────

def only_me(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != MY_CHAT_ID:
            if update.message:
                await update.message.reply_text("Доступ заборонено.")
            return
        return await func(update, context)
    return wrapper


# ─── Helpers ────────────────────────────────────────────────────────────────

def today_str() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def now_dt() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def format_task_line(task: dict, date_str: str, index: int) -> str:
    if is_done_for_date(task, date_str):
        status = "✅"
    elif is_missed_for_date(task, date_str):
        status = "❌"
    else:
        status = "⚪️"

    time_part = f" 🕐 {task['due_time']}" if task.get("due_time") else ""
    repeat_part = " 🔁" if task.get("repeat", {}).get("enabled") else ""
    return f"{status} {index}. {task['text']}{time_part}{repeat_part}"


def build_date_keyboard() -> InlineKeyboardMarkup:
    """Кнопки на 30 днів вперед, по 7 в рядку."""
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    buttons = []
    row = []
    for i in range(30):
        d = today + timedelta(days=i)
        label = f"{d.day} {WEEKDAY_NAMES[d.weekday()]}"
        row.append(InlineKeyboardButton(label, callback_data=f"date:{d.isoformat()}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def build_time_keyboard() -> InlineKeyboardMarkup:
    hours = [5, 6, 7, 8, 9, 10, 11, 12,  13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    buttons = []
    row = []
    for h in hours:
        row.append(InlineKeyboardButton(f"{h:02d}:00", callback_data=f"time:{h:02d}:00"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⏭ Пропустити", callback_data="time:skip")])
    return InlineKeyboardMarkup(buttons)


def build_repeat_keyboard(selected: list[int]) -> InlineKeyboardMarkup:
    row = []
    for i, name in enumerate(WEEKDAY_NAMES):
        mark = "✅" if i in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{name}", callback_data=f"wd:{i}"))
    buttons = [row[:4], row[4:]]
    buttons.append([
        InlineKeyboardButton("✅  Готово", callback_data="wd:done"),
        InlineKeyboardButton("❌ Без повтору", callback_data="wd:skip"),
    ])
    return InlineKeyboardMarkup(buttons)



# ─── /plan ──────────────────────────────────────────────────────────────────

@only_me
async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Сьогодні", callback_data="plan:today")],
        [InlineKeyboardButton("📋 Весь список", callback_data="plan:all")],
        [InlineKeyboardButton("🗓 Обрати дату", callback_data="plan:pick_date")]
    ])

    await update.message.reply_text(
        "📋 Обери режим перегляду плану:",
        reply_markup=keyboard
    )

async def plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # ─── СЬОГОДНІ ─────────────────────
    if data == "plan:today":
        date_str = today_str()
        tasks = get_tasks_for_date(date_str)

        if not tasks:
            await query.edit_message_text("📭 На сьогодні задач немає")
            return

        text = "📋 Сьогоднішній план:\n\n"
        text += "\n".join([
            format_task_line(t, date_str, i+1)
            for i, t in enumerate(tasks)
        ])

        await query.edit_message_text(text)
        return

    # ─── ВСІ ЗАДАЧІ ───────────────────
    if data == "plan:all":
        tasks = load_tasks()

        if not tasks:
            await query.edit_message_text("📭 Список порожній")
            return

        # ─── групування по датах ───
        grouped = {}

        for t in tasks:
            date_key = t.get("due_date", "—")
            grouped.setdefault(date_key, []).append(t)

        # ─── сортуємо дати ───
        sorted_dates = sorted(grouped.keys())

        text = "📋 Календар задач:\n\n"

        for date_str in sorted_dates:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                label = f"{dt.day:02d}.{dt.month:02d}"
            except:
                label = date_str

            text += f"📅 {label}\n"

            for i, task in enumerate(grouped[date_str], 1):
                text += "   " + format_task_line(task, date_str, i) + "\n"

            text += "\n"

        await query.edit_message_text(text)
        return

    # ─── ВИБІР ДАТИ ───────────────────
    if data == "plan:pick_date":
        await query.edit_message_text(
            "🗓 Обери дату:",
            reply_markup=build_date_keyboard()
        )
        return

    # ─── КОНКРЕТНА ДАТА ───────────────
    if data.startswith("date:"):
        date_str = data.split(":", 1)[1]
        tasks = get_tasks_for_date(date_str)

        if not tasks:
            await query.edit_message_text(f"📭 Нема задач на {date_str}")
            return

        text = f"📋 План на {date_str}\n\n"
        text += "\n".join([
            format_task_line(t, date_str, i+1)
            for i, t in enumerate(tasks)
        ])

        await query.edit_message_text(text)
        return

# ─── /add (ConversationHandler) ─────────────────────────────────────────────

@only_me
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📝 Напиши текст задачі:\n_(або /cancel щоб скасувати)_",
        parse_mode="Markdown",
    )
    return STEP_TEXT


async def step_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["text"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 Вибери дату:",
        reply_markup=build_date_keyboard(),
    )
    return STEP_DATE


async def step_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_val = query.data.split(":", 1)[1]
    context.user_data["due_date"] = date_val
    await query.edit_message_text(
        f"📅 Дата: *{date_val}*\n\n🕐 Вибери час (необов'язково):",
        parse_mode="Markdown",
        reply_markup=build_time_keyboard(),
    )
    return STEP_TIME


async def step_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = query.data.split(":", 1)[1]

    if val == "skip":
        context.user_data["due_time"] = None
    else:
        context.user_data["due_time"] = val

    context.user_data["repeat_weekdays"] = []
    await query.edit_message_text(
        "🔁 Повторювати щотижня? Вибери дні або пропусти:",
        reply_markup=build_repeat_keyboard([]),
    )
    return STEP_REPEAT


async def step_repeat_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тоггл днів тижня для повтору."""
    query = update.callback_query
    await query.answer()
    val = query.data.split(":", 1)[1]

    if val == "skip":
        await _finish_add(query, context, repeat_enabled=False)
        return ConversationHandler.END

    if val == "done":
        await _finish_add(query, context, repeat_enabled=True)
        return ConversationHandler.END

    wd = int(val)
    selected = context.user_data.setdefault("repeat_weekdays", [])
    if wd in selected:
        selected.remove(wd)
    else:
        selected.append(wd)

    await query.edit_message_text(
        "🔁 Вибери дні повтору:",
        reply_markup=build_repeat_keyboard(selected),
    )
    return STEP_REPEAT


async def _finish_add(query, context, repeat_enabled: bool):
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    until = (today + timedelta(days=30)).isoformat()

    task = {
        "id": str(uuid.uuid4()),
        "text": context.user_data["text"],
        "due_date": context.user_data["due_date"],
        "due_time": context.user_data.get("due_time"),
        "done_dates": [],
        "missed_dates": [],
        "added": datetime.now(tz).isoformat(),
        "active": True,
        "repeat": {
            "enabled": repeat_enabled,
            "weekdays": sorted(context.user_data.get("repeat_weekdays", [])) if repeat_enabled else [],
            "until": until if repeat_enabled else None,
        },
    }

    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)

    time_str = f" о {task['due_time']}" if task["due_time"] else ""
    repeat_str = ""
    if repeat_enabled and task["repeat"]["weekdays"]:
        days = ", ".join(WEEKDAY_NAMES[d] for d in task["repeat"]["weekdays"])
        repeat_str = f"\n🔁 Повтор: {days} (до {until})"

    await query.edit_message_text(
        f"✅ Додано: *{task['text']}*\n"
        f"📅 {task['due_date']}{time_str}{repeat_str}",
        parse_mode="Markdown",
    )


# ─── /done з кнопками ───────────────────────────────────────────────────────

@only_me
async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_date = today_str()
    tasks = get_tasks_for_date(target_date)

    if not tasks:
        await update.message.reply_text("На сьогодні задач немає.")
        return

    # Якщо передали числа через пробіл: /done або відповідь "3 5 1"
    if context.args:
        await _done_by_numbers(update, context, tasks, target_date, context.args)
        return

    # Інакше — показуємо inline-кнопки
    await update.message.reply_text(
        "✅ *Вибери виконані задачі:*",
        parse_mode="Markdown",
        reply_markup=build_done_keyboard(tasks, target_date),
    )


async def _done_by_numbers(update, context, tasks, target_date, args):
    """Відмічає кілька задач за номерами: /done 1 3 5"""
    results = []
    for arg in args:
        try:
            idx = int(arg) - 1
            task = tasks[idx]
            if is_done_for_date(task, target_date):
                results.append(f"⚠️ #{idx+1} вже виконана")
            else:
                mark_done_for_date(task["id"], target_date)
                record_stat(target_date, done=1)
                results.append(f"✅ #{idx+1} {task['text']}")
        except (ValueError, IndexError):
            results.append(f"❌ #{arg} — невірний номер")

    await update.message.reply_text("\n".join(results), parse_mode="Markdown")


def build_done_keyboard(tasks: list, date_str: str) -> InlineKeyboardMarkup:
    """Кнопка на кожну невиконану задачу."""
    buttons = []
    for i, t in enumerate(tasks, 1):
        if is_done_for_date(t, date_str):
            # Вже виконана — показуємо сірою із галочкою, але некликабельна
            label = f"✅ {i}. {t['text']}"
            cb = f"done_noop:{t['id']}"
        else:
            label = f"⬜ {i}. {t['text']}"
            cb = f"done_toggle:{t['id']}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])

    buttons.append([InlineKeyboardButton("🔄 Оновити список", callback_data="done_refresh")])
    return InlineKeyboardMarkup(buttons)


async def callback_done_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Натискання на задачу → відмічає виконаною і оновлює кнопки."""
    query = update.callback_query
    await query.answer()

    task_id = query.data.split(":", 1)[1]
    target_date = today_str()

    mark_done_for_date(task_id, target_date)
    record_stat(target_date, done=1)

    # Оновлюємо список кнопок
    tasks = get_tasks_for_date(target_date)
    done_count = sum(1 for t in tasks if is_done_for_date(t, target_date))

    await query.edit_message_text(
        f"✅ *Виконані задачі ({done_count}/{len(tasks)}):*",
        parse_mode="Markdown",
        reply_markup=build_done_keyboard(tasks, target_date),
    )


async def callback_done_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Оновлено!")
    target_date = today_str()
    tasks = get_tasks_for_date(target_date)
    done_count = sum(1 for t in tasks if is_done_for_date(t, target_date))

    await query.edit_message_text(
        f"✅ *Виконані задачі ({done_count}/{len(tasks)}):*",
        parse_mode="Markdown",
        reply_markup=build_done_keyboard(tasks, target_date),
    )


async def callback_done_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клік на вже виконану задачу — просто показує підказку."""
    query = update.callback_query
    await query.answer("Вже виконана ✅")

# ─── /remove з кнопками ─────────────────────────────────────────────────────

@only_me
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = load_tasks()
    if not tasks:
        await update.message.reply_text("Список порожній.")
        return

    # Якщо передали числа: /remove 2 4
    if context.args:
        await _remove_by_numbers(update, context, tasks, context.args)
        return

    await update.message.reply_text(
        "🗑 *Вибери задачу для видалення:*",
        parse_mode="Markdown",
        reply_markup=build_remove_keyboard(tasks),
    )


async def _remove_by_numbers(update, context, tasks, args):
    """Видаляє кілька задач за номерами: /remove 1 3"""
    # Сортуємо в зворотньому порядку щоб індекси не зсувались
    indices = sorted(set(int(a) - 1 for a in args if a.isdigit()), reverse=True)
    results = []
    for idx in indices:
        try:
            removed = delete_task(tasks[idx]["id"])
            results.append(f"🗑 Видалено: *{removed['text']}*")
        except IndexError:
            results.append(f"❌ #{idx+1} — невірний номер")

    await update.message.reply_text("\n".join(results), parse_mode="Markdown")


def build_remove_keyboard(tasks: list) -> InlineKeyboardMarkup:
    buttons = []
    for i, t in enumerate(tasks, 1):
        date_str = t.get("due_date", "—")
        label = f"🗑 {i}. {t['text']} ({date_str})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"remove_task:{t['id']}")])

    buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="remove_cancel")])
    return InlineKeyboardMarkup(buttons)


async def callback_remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    task_id = query.data.split(":", 1)[1]
    removed = delete_task(task_id)

    if not removed:
        await query.edit_message_text("⚠️ Задачу вже видалено.")
        return

    # Показуємо оновлений список — якщо ще є задачі
    tasks = load_tasks()
    if tasks:
        await query.edit_message_text(
            f"🗑 Видалено: *{removed['text']}*\n\nВидалити ще?",
            parse_mode="Markdown",
            reply_markup=build_remove_keyboard(tasks),
        )
    else:
        await query.edit_message_text(
            f"🗑 Видалено: *{removed['text']}*\n\n_Список порожній._",
            parse_mode="Markdown",
        )


async def callback_remove_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Скасовано")
    await query.edit_message_text("❌ Видалення скасовано.")

# ─── /stats ─────────────────────────────────────────────────────────────────

@only_me
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    week_dates = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    stats = get_week_stats(week_dates)

    lines = ["📊 *Статистика за 7 днів:*\n"]
    total_done = total_missed = 0
    for d in week_dates:
        s = stats[d]
        done, missed = s["done"], s["missed"]
        total_done += done
        total_missed += missed
        bar = "▓" * done + "░" * missed
        dt = datetime.strptime(d, "%Y-%m-%d")
        label = f"{dt.day:02d}.{dt.month:02d} {WEEKDAY_NAMES[dt.weekday()]}"
        lines.append(f"`{label}` {bar or '—'} ✅{done} ❌{missed}")

    total = total_done + total_missed
    pct = int(total_done / total * 100) if total else 0
    lines.append(f"\nВиконано: *{total_done}/{total}* ({pct}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── /restart ─────────────────────────────────────────────────────────────────
@only_me
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Перезапускаю бота...")
    script = os.path.abspath(__file__)
    subprocess.Popen([sys.executable, script])
    os._exit(0)
    await update.message.reply_text("🧹 Стан бота оновлено.")

# ─── /clear ─────────────────────────────────────────────────────────────────

@only_me
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_tasks([])
    await update.message.reply_text("🗑 Список очищено повністю.")


# ─── /cancel ────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Скасовано.")
    return ConversationHandler.END


# ─── SCHEDULER JOBS ─────────────────────────────────────────────────────────

async def job_deactivate_missed(context: ContextTypes.DEFAULT_TYPE):
    """О 00:00 — деактивує прострочені звичайні задачі, записує missed в статистику."""
    tz = ZoneInfo(TIMEZONE)
    yesterday = (datetime.now(tz).date() - timedelta(days=1)).isoformat()

    tasks = load_tasks()
    changed = False
    for t in tasks:
        if not t.get("active", True):
            continue
        repeat = t.get("repeat", {})
        if repeat.get("enabled"):
            # Для повторюваних — перевіряємо чи вчора був їхній день
            try:
                yd = datetime.strptime(yesterday, "%Y-%m-%d").date()
            except ValueError:
                continue
            if yd.weekday() in repeat.get("weekdays", []):
                if yesterday not in t.get("done_dates", []) and yesterday not in t.get("missed_dates", []):
                    t.setdefault("missed_dates", []).append(yesterday)
                    record_stat(yesterday, missed=1)
                    changed = True
        else:
            # Звичайна задача — якщо due_date == yesterday і не виконана
            if t.get("due_date") == yesterday:
                if yesterday not in t.get("done_dates", []):
                    t.setdefault("missed_dates", []).append(yesterday)
                    record_stat(yesterday, missed=1)
                t["active"] = False
                changed = True

    if changed:
        save_tasks(tasks)
    logger.info("job_deactivate_missed done for %s", yesterday)


async def job_morning_plan(context: ContextTypes.DEFAULT_TYPE):
    """О 09:00 — надсилає план на сьогодні."""
    today = today_str()
    tasks = get_tasks_for_date(today)
    dt = datetime.strptime(today, "%Y-%m-%d")
    date_label = f"{dt.day}.{dt.month:02d}, {WEEKDAY_NAMES_FULL[dt.weekday()]}"

    if not tasks:
        await context.bot.send_message(
            chat_id=MY_CHAT_ID,
            text=f"☀️ Доброго ранку! На {date_label} задач немає. Додай /add",
        )
        return

    lines = [f"☀️ *Доброго ранку! План на {date_label}:*\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(format_task_line(t, today, i))
    lines.append(f"\n_Всього: {len(tasks)}_")

    await context.bot.send_message(
        chat_id=MY_CHAT_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


async def job_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Кожну хвилину — перевіряє задачі з due_time і надсилає нагадування за 5 хв."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    remind_time = (now + timedelta(minutes=5)).strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    tasks = get_tasks_for_date(today)
    for t in tasks:
        if t.get("due_time") == remind_time and not is_done_for_date(t, today):
            await context.bot.send_message(
                chat_id=MY_CHAT_ID,
                text=f"⏰ Нагадування! Через 5 хв: *{t['text']}* о {t['due_time']}",
                parse_mode="Markdown",
            )


async def job_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Щонеділі о 20:00 — звіт за тиждень."""
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    week_dates = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    stats = get_week_stats(week_dates)

    total_done = total_missed = 0
    lines = ["📊 *Тижневий звіт:*\n"]
    missed_tasks = []

    for d in week_dates:
        s = stats[d]
        done, missed = s["done"], s["missed"]
        total_done += done
        total_missed += missed
        bar = "▓" * done + "░" * missed
        dt = datetime.strptime(d, "%Y-%m-%d")
        label = f"{dt.day:02d}.{dt.month:02d} {WEEKDAY_NAMES[dt.weekday()]}"
        lines.append(f"`{label}` {bar or '—'} ✅{done} ❌{missed}")

    # Знаходимо пропущені задачі за тиждень
    all_tasks = load_tasks()
    for t in all_tasks:
        for d in week_dates:
            if d in t.get("missed_dates", []):
                missed_tasks.append(f"• {t['text']} ({d})")

    total = total_done + total_missed
    pct = int(total_done / total * 100) if total else 0
    lines.append(f"\n*Виконано: {total_done}/{total} ({pct}%)*")

    if missed_tasks:
        lines.append("\n❌ *Пропущені задачі:*")
        lines.extend(missed_tasks[:10])

    await context.bot.send_message(
        chat_id=MY_CHAT_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# ─── INIT ───────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    tz = ZoneInfo(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(job_morning_plan, "cron", hour=9, minute=0, args=[application])
    scheduler.add_job(job_deactivate_missed, "cron", hour=0, minute=0, args=[application])
    scheduler.add_job(job_reminders, "cron", minute="*", args=[application])
    scheduler.add_job(job_weekly_report, "cron", day_of_week="sun", hour=20, minute=0, args=[application])

    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # ConversationHandler для /add (без змін)
    conv_add = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            STEP_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_text)],
            STEP_DATE: [CallbackQueryHandler(step_date, pattern=r"^date:")],
            STEP_TIME: [CallbackQueryHandler(step_time, pattern=r"^time:")],
            STEP_REPEAT: [CallbackQueryHandler(step_repeat_toggle, pattern=r"^wd:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(conv_add)
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("restart", cmd_restart))

    # ✅ Нові callback-хендлери
    app.add_handler(CallbackQueryHandler(plan_callback, pattern=r"^plan|^date:"))
    app.add_handler(CallbackQueryHandler(callback_done_toggle, pattern=r"^done_toggle:"))
    app.add_handler(CallbackQueryHandler(callback_done_noop, pattern=r"^done_noop:"))
    app.add_handler(CallbackQueryHandler(callback_done_refresh, pattern=r"^done_refresh$"))
    app.add_handler(CallbackQueryHandler(callback_remove_task, pattern=r"^remove_task:"))
    app.add_handler(CallbackQueryHandler(callback_remove_cancel, pattern=r"^remove_cancel$"))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()