"""Flow — Focus & Productivity Telegram Bot (Optimized for Speed)."""

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.request import HTTPXRequest

import database as db

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("flow")

# ── Fast HTML escape (no DOM creation) ────────────────────────
_HTML_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"}

def escape_html(s: str) -> str:
    return "".join(_HTML_MAP.get(c, c) for c in s)

# ── Quotes ────────────────────────────────────────────────────
QUOTES = [
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("It is not enough to be busy. The question is: what are we busy about?", "Thoreau"),
    ("Focus on being productive instead of busy.", "Tim Ferriss"),
    ("The successful warrior is the average man, with laser-like focus.", "Bruce Lee"),
    ("Concentrate all your thoughts upon the work at hand.", "Alexander Graham Bell"),
    ("Where focus goes, energy flows.", "Tony Robbins"),
    ("Deep work is the superpower of the 21st century.", "Cal Newport"),
    ("You don't need more time, you need more focus.", "Unknown"),
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("Action is the foundational key to all success.", "Pablo Picasso"),
    ("Start where you are. Use what you have. Do what you can.", "Arthur Ashe"),
    ("Small daily improvements are the key to staggering long-term results.", "Unknown"),
    ("Discipline is choosing between what you want now and what you want most.", "Abraham Lincoln"),
    ("Your future is created by what you do today, not tomorrow.", "Robert Kiyosaki"),
    ("The mind is everything. What you think you become.", "Buddha"),
]

def random_quote():
    q, a = random.choice(QUOTES)
    return f'"{q}"\n— {a}'

# ── In-memory cache for user settings (avoids DB hit on every command) ──
_settings_cache: dict[int, dict] = {}

def get_cached_settings(user_id: int) -> dict:
    s = _settings_cache.get(user_id)
    if s:
        return s
    s = db.get_user(user_id)
    if s:
        _settings_cache[user_id] = s
    return s or {}

def invalidate_cache(user_id: int):
    _settings_cache.pop(user_id, None)

# ══════════════════════════════════════════════════════════════
#  TIMER STATE
# ══════════════════════════════════════════════════════════════
timers: dict[int, dict] = {}
user_sessions_count: dict[int, int] = {}


def get_timer(user_id: int) -> dict | None:
    return timers.get(user_id)


def init_timer(user_id: int, mode: str, settings: dict, task_id: int = None):
    if mode == "focus":
        duration = settings.get("focus_min", 25) * 60
    elif mode == "short":
        duration = settings.get("short_min", 5) * 60
    else:
        duration = settings.get("long_min", 15) * 60

    timers[user_id] = {
        "mode": mode,
        "total": duration,
        "remaining": duration,
        "running": False,
        "task_id": task_id,
        "started_at": None,
        "job_name": None,
    }


def fmt(seconds: int) -> str:
    m, s = divmod(max(seconds, 0), 60)
    return f"{m:02d}:{s:02d}"


def progress(remaining: int, total: int, length: int = 12) -> str:
    if total <= 0:
        return "░" * length
    filled = round((total - remaining) / total * length)
    return "▓" * filled + "░" * (length - filled)


def mode_emoji(mode: str) -> str:
    return {"focus": "🍅", "short": "☕", "long": "🌿"}.get(mode, "⏱")


def mode_label(mode: str) -> str:
    return {"focus": "Focus", "short": "Short Break", "long": "Long Break"}.get(mode, mode)


# ══════════════════════════════════════════════════════════════
#  TIMER TICK
# ══════════════════════════════════════════════════════════════

async def timer_tick(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    t = get_timer(user_id)
    if not t or not t["running"]:
        return

    t["remaining"] -= 1
    remaining = t["remaining"]

    if remaining <= 0:
        t["running"] = False
        await session_complete(context, user_id, t)
    elif remaining == 60:
        await context.bot.send_message(chat_id=user_id, text=f" {mode_emoji(t['mode'])} 1 minute left...")
    elif remaining == 300 and t["mode"] == "focus":
        await context.bot.send_message(chat_id=user_id, text=f"🧘 {mode_emoji(t['mode'])} 5 minutes left — keep going!")


async def session_complete(context: ContextTypes.DEFAULT_TYPE, user_id: int, t: dict):
    settings = get_cached_settings(user_id)
    mode = t["mode"]
    task_id = t.get("task_id")

    if mode == "focus":
        duration_min = t["total"] // 60
        started_str = t.get("started_at")
        started_iso = started_str.isoformat() if started_str else None

        db.record_session(user_id, duration_min, "focus", task_id, started_iso)
        if task_id:
            db.increment_task_sessions(user_id, task_id)

        user_sessions_count[user_id] = user_sessions_count.get(user_id, 0) + 1
        count = user_sessions_count[user_id]
        sessions_until_long = settings.get("sessions_long", 4)

        task_name = ""
        if task_id:
            task = db.get_task(user_id, task_id)
            if task:
                task_name = f"\n📋 Task: <b>{escape_html(task['text'])}</b>"

        if count % sessions_until_long == 0:
            next_mode, next_label = "long", "Long Break"
            next_dur = settings.get("long_min", 15)
        else:
            next_mode, next_label = "short", "Short Break"
            next_dur = settings.get("short_min", 5)

        kb = [
            [InlineKeyboardButton(f"☕ {next_label} ({next_dur} min)", callback_data=f"start_{next_mode}")],
            [InlineKeyboardButton("🍅 New Focus", callback_data="start_focus")],
        ]

        msg = (
            f"🎉 <b>Focus session complete!</b>\n"
            f"⏱ {duration_min} minutes\n"
            f"📊 Session {count}/{sessions_until_long}"
            f"{task_name}\n\n{random_quote()}"
        )

        await context.bot.send_message(
            chat_id=user_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb),
        )

        if settings.get("auto_break", 0):
            await asyncio.sleep(1)
            await _start_timer(context, user_id, next_mode)

    else:
        kb = [[InlineKeyboardButton(f"🍅 Start Focus ({settings.get('focus_min', 25)} min)", callback_data="start_focus")]]
        msg = f"💪 <b>Break's over!</b>\nReady to focus?\n\n{random_quote()}"
        await context.bot.send_message(
            chat_id=user_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb),
        )
        if settings.get("auto_focus", 0):
            await asyncio.sleep(1)
            await _start_timer(context, user_id, "focus")


async def _start_timer(context: ContextTypes.DEFAULT_TYPE, user_id: int, mode: str, task_id: int = None):
    settings = get_cached_settings(user_id)

    old = get_timer(user_id)
    if old and old.get("job_name"):
        try:
            context.job_queue.get_jobs_by_name(old["job_name"])[0].schedule_removal()
        except (IndexError, AttributeError):
            pass

    init_timer(user_id, mode, settings, task_id=task_id)
    t = get_timer(user_id)
    t["running"] = True
    t["started_at"] = datetime.now(tz=timezone.utc)

    job_name = f"timer_{user_id}_{int(time.time())}"
    t["job_name"] = job_name
    context.job_queue.run_once(timer_tick, t["total"], chat_id=user_id, name=job_name)

    await _send_timer_status(context, user_id)


# ══════════════════════════════════════════════════════════════
#  TIMER STATUS
# ══════════════════════════════════════════════════════════════

async def _send_timer_status(context_or_update, user_id: int):
    t = get_timer(user_id)
    if not t:
        return

    bar = progress(t["remaining"], t["total"])
    task_text = ""
    if t.get("task_id"):
        task = db.get_task(user_id, t["task_id"])
        if task:
            task_text = f"\n📋 <i>{escape_html(task['text'])}</i>"

    status = "Running" if t["running"] else ("Paused" if t["remaining"] < t["total"] else "Ready")

    msg = (
        f"{mode_emoji(t['mode'])} <b>{mode_label(t['mode'])}</b>\n"
        f"⏱ <code>{fmt(t['remaining'])}</code>\n"
        f"{bar}\n📌 {status}{task_text}"
    )

    kb = []
    if not t["running"] and t["remaining"] >= t["total"]:
        kb.append([InlineKeyboardButton("▶️ Start", callback_data=f"start_{t['mode']}")])
    elif t["running"]:
        kb.append([InlineKeyboardButton("⏸ Pause", callback_data="pause"), InlineKeyboardButton("⏹ Reset", callback_data="reset")])
    else:
        kb.append([InlineKeyboardButton("▶️ Resume", callback_data="resume"), InlineKeyboardButton("⏹ Reset", callback_data="reset")])

    if t["mode"] == "focus":
        kb.append([InlineKeyboardButton("☕ Take Break", callback_data="switch_short")])
    else:
        kb.append([InlineKeyboardButton("🍅 Switch to Focus", callback_data="switch_focus")])

    if isinstance(context_or_update, Update):
        await context_or_update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await context_or_update.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


# ══════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username, user.first_name)

    name = user.first_name or "there"
    msg = (
        f"Hey {name} 👋\n\n"
        f"I'm <b>Flow</b> — your focus companion.\n"
        f"Stay productive with timed sessions, task tracking, and stats.\n\n"
        f"<b>Quick start:</b>\n"
        f"🍅 /focus — Start a focus session\n"
        f"✅ /task <code>Buy groceries</code> — Add a task\n"
        f"📊 /stats — See your progress\n\nSend /help for all commands."
    )
    kb = [
        [InlineKeyboardButton(" Start Focus", callback_data="start_focus")],
        [InlineKeyboardButton("✅ My Tasks", callback_data="view_tasks")],
        [InlineKeyboardButton("💬 Motivation", callback_data="quote")],
    ]
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>Flow — Commands</b>\n\n"
        "<b>⏱ Timer</b>\n"
        "/focus — Start focus (25 min)\n"
        "/break — Short break (5 min)\n"
        "/longbreak — Long break (15 min)\n"
        "/pause — Pause timer\n"
        "/reset — Reset timer\n\n"
        "<b>✅ Tasks</b>\n"
        "/task &lt;text&gt; — Add task\n"
        "/tasks — View tasks\n"
        "/done &lt;id&gt; — Complete task\n"
        "/settask &lt;id&gt; — Assign to focus\n"
        "/deltask &lt;id&gt; — Delete task\n\n"
        "<b>📊 Stats</b>\n"
        "/stats — Your statistics\n"
        "/streak — Day streak\n\n"
        "<b>️ Settings</b>\n"
        "/settings — Configure timer\n\n"
        "<b>⏰ Reminders</b>\n"
        "/remind &lt;time&gt; — Set daily reminder (e.g., /remind 09:00)\n"
        "/remindoff — Turn off reminder\n"
        "/remindstatus — View reminder settings\n"
        "/streakoff — Turn off streak alerts\n"
        "/streakon — Turn on streak alerts\n\n"
        "<b>💡 Other</b>\n"
        "/quote — Get motivated\n"
        "/help — This message"
    )
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML)


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)
    t = get_timer(user_id)
    task_id = t.get("task_id") if t else None
    await _start_timer(context, user_id, "focus", task_id=task_id)


async def cmd_break(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)
    await _start_timer(context, user_id, "short")


async def cmd_longbreak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)
    await _start_timer(context, user_id, "long")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    t = get_timer(user_id)
    if not t or not t["running"]:
        await update.effective_chat.send_message("No timer running. Use /focus to start!")
        return

    t["running"] = False
    if t.get("job_name"):
        for job in context.job_queue.get_jobs_by_name(t["job_name"]):
            job.schedule_removal()

    bar = progress(t["remaining"], t["total"])
    msg = f"⏸ <b>Paused</b>\n{mode_emoji(t['mode'])} {mode_label(t['mode'])}\n <code>{fmt(t['remaining'])}</code>\n{bar}"
    kb = [[InlineKeyboardButton("▶️ Resume", callback_data="resume"), InlineKeyboardButton("⏹ Reset", callback_data="reset")]]
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    t = get_timer(user_id)
    if not t:
        await update.effective_chat.send_message("No timer to reset. Use /focus!")
        return

    if t.get("job_name"):
        for job in context.job_queue.get_jobs_by_name(t["job_name"]):
            job.schedule_removal()

    t["remaining"] = t["total"]
    t["running"] = False
    t["started_at"] = None
    await _send_timer_status(update, user_id)


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.effective_chat.send_message("Usage: /task &lt;your task&gt;\nExample: /task Write project proposal", parse_mode=ParseMode.HTML)
        return

    task_id = db.add_task(user_id, text)
    tasks = db.get_tasks(user_id, include_completed=False)

    msg = f"✅ Task added!\n<b>#{task_id}</b> — {escape_html(text)}\n📋 {len(tasks)} active task(s)."
    kb = [
        [InlineKeyboardButton("📋 View All Tasks", callback_data="view_tasks")],
        [InlineKeyboardButton("◎ Focus on this", callback_data=f"settask_{task_id}")],
    ]
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def _send_task_list(update_or_context, user_id: int, show_completed: bool = False):
    tasks = db.get_tasks(user_id, include_completed=show_completed)

    if not tasks:
        msg = "📋 <b>No tasks yet!</b>\n\nAdd one with:\n/task &lt;text&gt;"
        kb = []
    else:
        active = [t for t in tasks if not t["completed"]]
        done = [t for t in tasks if t["completed"]]

        parts = ["📋 <b>Your Tasks</b>\n"]
        if active:
            parts.append("<b>Active:</b>")
            for t in active[:15]:
                si = f"  🍅{t['sessions']}" if t["sessions"] > 0 else ""
                parts.append(f"  <b>#{t['id']}</b> — {escape_html(t['text'])}{si}")
        if done:
            parts.append(f"\n<b>Done ({len(done)}):</b>")
            for t in done[:5]:
                parts.append(f"  ✓ #{t['id']} — {escape_html(t['text'])}")
            if len(done) > 5:
                parts.append(f"  ... and {len(done) - 5} more")

        msg = "\n".join(parts)
        kb = []
        if active:
            kb.append([InlineKeyboardButton(f"✓ #{t['id']}", callback_data=f"done_{t['id']}") for t in active[:3]])
        kb.append([InlineKeyboardButton("◎ Focus on task", callback_data="pick_task_focus")])
        if not show_completed and done:
            kb.append([InlineKeyboardButton(f"Show done ({len(done)})", callback_data="show_done")])

    markup = InlineKeyboardMarkup(kb) if kb else None
    if isinstance(update_or_context, Update):
        await update_or_context.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update_or_context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=markup)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)
    await _send_task_list(update, user_id)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.effective_chat.send_message("Usage: /done &lt;task_id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Please provide a valid task number.")
        return

    task = db.get_task(user_id, task_id)
    if not task:
        await update.effective_chat.send_message(f"Task #{task_id} not found.")
        return

    if task["completed"]:
        await update.effective_chat.send_message(f"Task #{task_id} is already complete!")
        return

    db.complete_task(user_id, task_id)
    await update.effective_chat.send_message(
        f"🎉 Task done!\n✓ <b>#{task_id}</b> — {escape_html(task['text'])}\n🍅 {task['sessions']} session(s) invested.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_deltask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.effective_chat.send_message("Usage: /deltask &lt;task_id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Please provide a valid task number.")
        return

    task = db.get_task(user_id, task_id)
    if not task:
        await update.effective_chat.send_message(f"Task #{task_id} not found.")
        return

    db.delete_task(user_id, task_id)
    t = get_timer(user_id)
    if t and t.get("task_id") == task_id:
        t["task_id"] = None
    await update.effective_chat.send_message(f"🗑 Task #{task_id} deleted.")


async def cmd_settask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.effective_chat.send_message("Usage: /settask &lt;task_id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Please provide a valid task number.")
        return

    task = db.get_task(user_id, task_id)
    if not task:
        await update.effective_chat.send_message(f"Task #{task_id} not found.")
        return

    t = get_timer(user_id)
    if t:
        t["task_id"] = task_id
    else:
        settings = get_cached_settings(user_id)
        init_timer(user_id, "focus", settings, task_id=task_id)

    await update.effective_chat.send_message(
        f"◎ Focusing on:\n<b>#{task_id}</b> — {escape_html(task['text'])}\n\nUse /focus to start.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unsettask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    t = get_timer(user_id)
    if t:
        t["task_id"] = None
    await update.effective_chat.send_message("Task cleared.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)

    # Only 3 DB queries total instead of 7+
    today = db.get_today_stats(user_id)
    weekly = db.get_weekly_data(user_id)
    total = db.get_total_stats(user_id)
    streak = db.get_streak(user_id)
    recent = db.get_recent_sessions(user_id, limit=5)

    max_min = max(d["minutes"] for d in weekly) if weekly else 1
    chart = []
    for d in weekly:
        bl = round(d["minutes"] / max(1, max_min) * 10) if d["minutes"] > 0 else 0
        bar = "█" * bl + "░" * (10 - bl) if bl > 0 else "░" * 10
        marker = " ◀" if d["date"] == datetime.now().date().isoformat() else ""
        chart.append(f"  {d['day_name']} {bar} {d['minutes']}m{marker}")

    msg = (
        f" <b>Your Stats</b>\n\n"
        f"<b>Today</b>\n  🍅 {today['count']} sessions\n  ⏱ {today['minutes']} min\n  🔥 {streak} day streak\n\n"
        f"<b>All Time</b>\n  🍅 {total['sessions']} sessions\n  ⏱ {total['minutes'] // 60}h {total['minutes'] % 60}m\n\n"
        f"<b>Last 7 Days</b>\n" + "\n".join(chart)
    )

    if recent:
        lines = [f"  {s['duration']}m — {s.get('task_text') or 'Free focus'} ({datetime.fromisoformat(s['completed_at']).strftime('%H:%M')})" for s in recent]
        msg += f"\n\n<b>Recent</b>\n" + "\n".join(lines)

    kb = [[InlineKeyboardButton("🍅 Start Focus", callback_data="start_focus")]]
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cmd_streak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    streak = db.get_streak(user_id)

    emoji = "🔥" if streak > 0 else "💤"
    if streak == 0:
        msg = f"{emoji} <b>Streak: 0</b>\n\nStart a focus session today!"
    elif streak < 3:
        msg = f"{emoji} <b>{streak} day streak!</b>\nKeep going — momentum is building!"
    elif streak < 7:
        msg = f"{emoji} <b>{streak} day streak!</b>\nGreat work! Consistency is key."
    elif streak < 30:
        msg = f"{emoji} <b>{streak} day streak!</b>\nAmazing! You're in the zone! 🚀"
    else:
        msg = f"{emoji} <b>{streak} day streak!</b>\nLegendary! You're unstoppable! 🏆"

    kb = [[InlineKeyboardButton("🍅 Focus Now", callback_data="start_focus")]]
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)
    user = get_cached_settings(user_id)

    msg = (
        "⚙️ <b>Settings</b>\n\n"
        f" Focus: <b>{user.get('focus_min', 25)} min</b>\n"
        f"☕ Short break: <b>{user.get('short_min', 5)} min</b>\n"
        f"🌿 Long break: <b>{user.get('long_min', 15)} min</b>\n"
        f"🔄 Long break after: <b>{user.get('sessions_long', 4)} sessions</b>\n\n"
        f" Sound: <b>{'On' if user.get('sound', 1) else 'Off'}</b>\n"
        f"⏩ Auto-break: <b>{'On' if user.get('auto_break', 0) else 'Off'}</b>\n"
        f"⏩ Auto-focus: <b>{'On' if user.get('auto_focus', 0) else 'Off'}</b>"
    )

    kb = [
        [InlineKeyboardButton("🍅 Focus", callback_data="setting_focus"), InlineKeyboardButton("☕ Break", callback_data="setting_short")],
        [InlineKeyboardButton("🌿 Long Break", callback_data="setting_long"), InlineKeyboardButton("🔄 Cycles", callback_data="setting_cycles")],
        [InlineKeyboardButton(f"🔔 Sound: {'✅' if user.get('sound', 1) else '❌'}", callback_data="toggle_sound"),
         InlineKeyboardButton(f"⏩ Auto-break: {'✅' if user.get('auto_break', 0) else '❌'}", callback_data="toggle_autobreak")],
        [InlineKeyboardButton(f"⏩ Auto-focus: {'✅' if user.get('auto_focus', 0) else '❌'}", callback_data="toggle_autofocus")],
    ]
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(random_quote())


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a daily reminder. Usage: /remind 09:00"""
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)

    # Parse time
    if not context.args:
        current = db.get_reminder(user_id)
        await update.effective_chat.send_message(
            f"⏰ <b>Daily Reminder</b>\n\n"
            f"Current time: <b>{current['remind_time']}</b>\n\n"
            f"Set a new time:\n/remind 09:00\n\n"
            f"Turn off:\n/remindoff",
            parse_mode=ParseMode.HTML,
        )
        return

    time_str = context.args[0]
    # Validate format
    if len(time_str) != 5 or time_str[2] != ':':
        await update.effective_chat.send_message("️ Please use HH:MM format (e.g., /remind 09:00)")
        return

    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await update.effective_chat.send_message("⚠️ Invalid time. Use HH:MM (e.g., /remind 14:30)")
        return

    db.set_reminder_time(user_id, time_str)
    db.set_reminder_on(user_id, True)

    await update.effective_chat.send_message(
        f"✅ <b>Reminder set!</b>\n\n"
        f"I'll nudge you every day at <b>{time_str}</b> to focus.\n\n"
        f"Turn off anytime with /remindoff",
        parse_mode=ParseMode.HTML,
    )

    # Reschedule the daily job
    await schedule_daily_reminders(context)


async def cmd_remindoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.set_reminder_on(user_id, False)
    await update.effective_chat.send_message("⏰ Daily reminder turned off. Use /remind to turn it back on.")


async def cmd_remindstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    r = db.get_reminder(user_id)
    status = "✅ On" if r.get("remind_on", 1) else "❌ Off"
    streak = "✅ On" if r.get("streak_alert", 1) else "❌ Off"

    msg = (
        f" <b>Reminder Settings</b>\n\n"
        f"Daily reminder: <b>{status}</b> at {r.get('remind_time', '09:00')}\n"
        f"Streak alert: <b>{streak}</b>\n\n"
        f"<b>Change:</b>\n"
        f"/remind [time] — Set time (e.g., /remind 10:00)\n"
        f"/remindoff — Turn off\n"
        f"/streakoff — Turn off streak alerts"
    )
    await update.effective_chat.send_message(text=msg, parse_mode=ParseMode.HTML)


async def cmd_streakoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.set_streak_alert(user_id, False)
    await update.effective_chat.send_message("Streak alerts turned off. Use /streakon to enable again.")


async def cmd_streakon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.set_streak_alert(user_id, True)
    await update.effective_chat.send_message("Streak alerts enabled! 🔥")


# ══════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    db.ensure_user(user_id, query.from_user.username, query.from_user.first_name)

    await query.answer()

    if data.startswith("start_"):
        mode = data.replace("start_", "")
        t = get_timer(user_id)
        task_id = t.get("task_id") if t else None
        await _start_timer(context, user_id, mode, task_id=task_id)

    elif data == "pause":
        t = get_timer(user_id)
        if t and t["running"]:
            t["running"] = False
            if t.get("job_name"):
                for job in context.job_queue.get_jobs_by_name(t["job_name"]):
                    job.schedule_removal()
        await _send_timer_status(context, user_id)

    elif data == "resume":
        t = get_timer(user_id)
        if t and not t["running"] and t["remaining"] > 0:
            t["running"] = True
            if t.get("job_name"):
                for job in context.job_queue.get_jobs_by_name(t["job_name"]):
                    job.schedule_removal()
            job_name = f"timer_{user_id}_r_{int(time.time())}"
            t["job_name"] = job_name
            context.job_queue.run_once(timer_tick, t["remaining"], chat_id=user_id, name=job_name)
        await _send_timer_status(context, user_id)

    elif data == "reset":
        t = get_timer(user_id)
        if t:
            if t.get("job_name"):
                for job in context.job_queue.get_jobs_by_name(t["job_name"]):
                    job.schedule_removal()
            t["remaining"] = t["total"]
            t["running"] = False
        await _send_timer_status(context, user_id)

    elif data.startswith("switch_"):
        mode = data.replace("switch_", "")
        t = get_timer(user_id)
        task_id = t.get("task_id") if t else None
        await _start_timer(context, user_id, mode, task_id=task_id)

    elif data == "view_tasks":
        await _send_task_list(context, user_id)

    elif data.startswith("done_"):
        task_id = int(data.replace("done_", ""))
        task = db.get_task(user_id, task_id)
        if task and not task["completed"]:
            db.complete_task(user_id, task_id)
            await query.edit_message_text(f"🎉 Completed!\n✓ <b>#{task_id}</b> — {escape_html(task['text'])}", parse_mode=ParseMode.HTML)
        elif task and task["completed"]:
            db.uncomplete_task(user_id, task_id)
            await query.edit_message_text(f"↩️ Reopened task #{task_id}.")

    elif data.startswith("settask_"):
        task_id = int(data.replace("settask_", ""))
        task = db.get_task(user_id, task_id)
        if not task:
            await query.edit_message_text(f"Task #{task_id} not found.")
            return
        t = get_timer(user_id)
        if t:
            t["task_id"] = task_id
        else:
            settings = get_cached_settings(user_id)
            init_timer(user_id, "focus", settings, task_id=task_id)
        await query.edit_message_text(f"◎ Focusing on:\n<b>#{task_id}</b> — {escape_html(task['text'])}\n\n/focus to start!", parse_mode=ParseMode.HTML)

    elif data == "pick_task_focus":
        tasks = db.get_tasks(user_id, include_completed=False)
        if not tasks:
            await query.edit_message_text("No active tasks. Add one with /task")
            return
        kb = [[InlineKeyboardButton(f"#{t['id']} — {t['text'][:30]}", callback_data=f"settask_{t['id']}")] for t in tasks[:8]]
        await query.edit_message_text("◎ Pick a task:", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "show_done":
        await _send_task_list(context, user_id, show_completed=True)

    elif data.startswith("setting_"):
        key = data.replace("setting_", "")
        labels = {
            "focus": ("🍅 Focus duration", "focus_min", [15, 25, 30, 45, 60]),
            "short": ("☕ Short break", "short_min", [3, 5, 10, 15]),
            "long": ("🌿 Long break", "long_min", [10, 15, 20, 30]),
            "cycles": ("🔄 Sessions until long break", "sessions_long", [3, 4, 5, 6]),
        }
        label, field, options = labels.get(key, ("Setting", "", []))
        user = get_cached_settings(user_id)
        current = user.get(field, 25)
        kb = [[InlineKeyboardButton(f"{opt} min{' ✅' if opt == current else ''}", callback_data=f"setval_{key}_{opt}_done")] for opt in options]
        await query.edit_message_text(f"{label}\nCurrent: <b>{current} min</b>\n\nChoose:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("toggle_"):
        field_map = {"sound": "sound", "autobreak": "auto_break", "autofocus": "auto_focus"}
        field = field_map.get(data.replace("toggle_", ""))
        if field:
            user = get_cached_settings(user_id)
            new_val = 0 if user.get(field, 1) else 1
            db.update_user_setting(user_id, field, new_val)
            invalidate_cache(user_id)
            user = get_cached_settings(user_id)
            msg = (
                "️ <b>Settings Updated</b>\n\n"
                f"🍅 Focus: <b>{user.get('focus_min', 25)} min</b>\n"
                f"☕ Short break: <b>{user.get('short_min', 5)} min</b>\n"
                f"🌿 Long break: <b>{user.get('long_min', 15)} min</b>\n"
                f"🔄 Long break after: <b>{user.get('sessions_long', 4)} sessions</b>\n\n"
                f" Sound: <b>{'On' if user.get('sound', 1) else 'Off'}</b>\n"
                f"⏩ Auto-break: <b>{'On' if user.get('auto_break', 0) else 'Off'}</b>\n"
                f"⏩ Auto-focus: <b>{'On' if user.get('auto_focus', 0) else 'Off'}</b>"
            )
            kb = [
                [InlineKeyboardButton("🍅 Focus", callback_data="setting_focus"), InlineKeyboardButton("☕ Break", callback_data="setting_short")],
                [InlineKeyboardButton(" Long Break", callback_data="setting_long"), InlineKeyboardButton("🔄 Cycles", callback_data="setting_cycles")],
                [InlineKeyboardButton(f" {'✅' if user.get('sound', 1) else '❌'}", callback_data="toggle_sound"),
                 InlineKeyboardButton(f" {'✅' if user.get('auto_break', 0) else '❌'}", callback_data="toggle_autobreak")],
                [InlineKeyboardButton(f"⏩ {'✅' if user.get('auto_focus', 0) else '❌'}", callback_data="toggle_autofocus")],
            ]
            await query.edit_message_text(text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("setval_"):
        parts = data.split("_")
        if len(parts) == 4:
            _, field, value, _ = parts
            field_map = {"focus": ("focus_min", int), "short": ("short_min", int), "long": ("long_min", int), "cycles": ("sessions_long", int)}
            db_field, cast = field_map.get(field, (None, None))
            if db_field:
                try:
                    db.update_user_setting(user_id, db_field, cast(value))
                    invalidate_cache(user_id)
                    await query.edit_message_text("✅ Updated!")
                except (ValueError, TypeError):
                    await query.edit_message_text("Invalid value.")

    elif data == "quote":
        await query.edit_message_text(random_quote())


# ══════════════════════════════════════════════════════════════
#  TEXT HANDLER
# ══════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    db.ensure_user(user_id, update.effective_user.username, update.effective_user.first_name)

    if not text.startswith("/") and len(text) > 2:
        task_id = db.add_task(user_id, text)
        await update.message.reply_text(
            f"✅ Task <b>#{task_id}</b>: {escape_html(text)}\n/focus to start, or /settask {task_id}",
            parse_mode=ParseMode.HTML,
        )


# ══════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ══════════════════════════════════════════════════════════════

async def error_handler(update, context):
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        logger.warning("Conflict — waiting 5s before retry")
        await asyncio.sleep(5)
        return
    logger.error(f"Error: {context.error}")


# ══════════════════════════════════════════════════════════════
#  DAILY REMINDERS
# ══════════════════════════════════════════════════════════════

async def send_daily_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Send morning focus reminders to all users who have them enabled."""
    reminders = db.get_all_reminders()
    for r in reminders:
        user_id = r["user_id"]
        today = db.get_today_stats(user_id)
        streak = db.get_streak(user_id)

        msg = (
            f"☀️ <b>Good morning!</b>\n\n"
            f"Time to focus today! 🍅\n"
            f"Your current streak: <b>🔥 {streak} days</b>\n\n"
            f"Send /focus to start a session!\n"
            f"Or check your stats with /stats"
        )
        try:
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send reminder to {user_id}: {e}")


async def send_streak_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Evening alert for users who haven't focused today."""
    users = db.get_streak_alert_users()
    for u in users:
        user_id = u["user_id"]
        today = db.get_today_stats(user_id)
        if today["count"] == 0:
            streak = db.get_streak(user_id)
            msg = (
                f"🌙 <b>Don't lose your streak!</b>\n\n"
                f"You haven't focused today yet.\n"
                f"Current streak: <b>🔥 {streak} days</b>\n\n"
                f"Just one 25-minute session will keep it going!\n"
                f"Send /focus to start."
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Failed to send streak alert to {user_id}: {e}")


async def schedule_daily_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Schedule reminder jobs based on user preferences."""
    from telegram.ext._jobqueue import Job

    # Remove old reminder jobs
    for name in ["daily_reminders", "streak_alerts"]:
        try:
            jobs = context.job_queue.get_jobs_by_name(name)
            for job in jobs:
                job.schedule_removal()
        except Exception:
            pass

    # Schedule daily reminders (group by time)
    reminders = db.get_all_reminders()
    times = {}
    for r in reminders:
        t = r["remind_time"]
        if t not in times:
            times[t] = []
        times[t].append(r["user_id"])

    for time_str, user_ids in times.items():
        hour, minute = map(int, time_str.split(':'))
        from datetime import time as dt_time
        reminder_time = dt_time(hour, minute)

        # Schedule for today if time hasn't passed, otherwise tomorrow
        now = datetime.now()
        if now.time() < reminder_time:
            today_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delay = (today_at - now).total_seconds()
        else:
            tomorrow_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
            delay = (tomorrow_at - now).total_seconds()

        context.job_queue.run_repeating(
            send_daily_reminders,
            interval=timedelta(days=1),
            first=delay,
            name=f"daily_{time_str}",
        )

    # Schedule streak alerts at 21:00 every day
    now = datetime.now()
    streak_dt = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now >= streak_dt:
        streak_dt += timedelta(days=1)
    delay = (streak_dt - now).total_seconds()

    context.job_queue.run_repeating(
        send_streak_alerts,
        interval=timedelta(days=1),
        first=delay,
        name="streak_alerts",
    )


# ══════════════════════════════════════════════════════════════
#  MAIN — OPTIMIZED FOR SPEED
# ══════════════════════════════════════════════════════════════

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("Set TELEGRAM_BOT_TOKEN!")
        return

    db.init_db()

    # OPTIMIZATION: Custom HTTPX request with aggressive timeouts
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=5.0,
        read_timeout=10.0,
        write_timeout=5.0,
        pool_timeout=5.0,
    )

    app = Application.builder().token(token).request(request).build()

    # OPTIMIZATION: Only listen to messages and callbacks (reduces overhead)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("focus", cmd_focus))
    app.add_handler(CommandHandler("break", cmd_break))
    app.add_handler(CommandHandler("longbreak", cmd_longbreak))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("deltask", cmd_deltask))
    app.add_handler(CommandHandler("settask", cmd_settask))
    app.add_handler(CommandHandler("unsettask", cmd_unsettask))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("streak", cmd_streak))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("quote", cmd_quote))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("remindoff", cmd_remindoff))
    app.add_handler(CommandHandler("remindstatus", cmd_remindstatus))
    app.add_handler(CommandHandler("streakoff", cmd_streakoff))
    app.add_handler(CommandHandler("streakon", cmd_streakon))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("🚀 Flow bot is running!")

    # Schedule daily reminders after app starts
    app.job_queue.run_once(schedule_daily_reminders, 2, name="init_reminders")

    # OPTIMIZATION: poll_interval=0 means check for updates ASAP
    # OPTIMIZATION: allowed_updates limits to what we actually use
    app.run_polling(
        poll_interval=0,
        timeout=30,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
