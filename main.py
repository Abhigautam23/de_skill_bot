import os
import asyncio
import html
import json
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)

from quiz_engine import (
    generate_question, format_question_message, format_snippet_block,
    format_code_block, evaluate_coding_answer,
    question_queue,
    DE_TOPICS, ACCOUNTING_TOPICS, topic_to_track,
    record_mcq_position, get_recent_mcq_positions,
)
from database import (
    save_attempt, save_scheduled_send, mark_answered,
    mark_reminded, get_weak_topics, get_streak,
    get_blocked_concept_keys, get_due_review_concepts,
)
from behaviour import (
    build_behaviour_report,
    check_skipped_behaviour, build_skip_reminder,
)
from scheduler import setup_scheduler

load_dotenv()
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USER_ID = int(os.getenv("TELEGRAM_USER_ID"))

# send_id -> {question, sent_at}
pending_questions: dict = {}
# The send_id of the most recent CODING question still awaiting a free-text answer.
pending_coding_send_id: Optional[str] = None

_app = None

# ---------------------------------------------------------------------
# User preferences (mode + scheduled-push rotation counter)
# ---------------------------------------------------------------------

PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_prefs.json")
DEFAULT_PREFS: dict = {"mode": "de_focus", "schedule_counter": 0}

MODE_LABELS = {
    "de_focus": "DE Focus (80/20)",
    "accounting_focus": "Accounting Focus (50/50)",
    "accounting_only": "Accounting Only",
    "de_only": "DE Only",
}


def load_prefs() -> dict:
    if not os.path.exists(PREFS_PATH):
        return dict(DEFAULT_PREFS)
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_PREFS)
        merged.update(data or {})
        if merged.get("mode") not in MODE_LABELS:
            merged["mode"] = DEFAULT_PREFS["mode"]
        return merged
    except Exception as exc:
        logging.warning("load_prefs failed (%s) — falling back to defaults", exc)
        return dict(DEFAULT_PREFS)


def save_prefs(prefs: dict) -> None:
    try:
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception as exc:
        logging.warning("save_prefs failed: %s", exc)


def get_mode() -> str:
    return load_prefs().get("mode", DEFAULT_PREFS["mode"])


def set_mode(mode: str) -> None:
    prefs = load_prefs()
    prefs["mode"] = mode
    save_prefs(prefs)


def pick_track_for_quiz(mode: Optional[str] = None) -> str:
    """Used for on-demand /quiz, /next and the queue's track_provider.
    Weighted random per mode."""
    mode = mode or get_mode()
    if mode == "de_only":
        return "DE"
    if mode == "accounting_only":
        return "Accounting"
    if mode == "accounting_focus":
        return "Accounting" if random.random() < 0.50 else "DE"
    # de_focus default — 80/20.
    return "Accounting" if random.random() < 0.20 else "DE"


def pick_track_for_scheduled() -> str:
    """Deterministic rotation for scheduled cron pushes.

    - de_only / accounting_only: that track every time.
    - accounting_focus: alternate DE / Accounting.
    - de_focus: 4 DE then 1 Accounting (5-cycle).

    Increments the persisted `schedule_counter` so the rotation survives restarts.
    """
    prefs = load_prefs()
    mode = prefs.get("mode", "de_focus")
    if mode == "de_only":
        return "DE"
    if mode == "accounting_only":
        return "Accounting"
    counter = int(prefs.get("schedule_counter", 0) or 0)
    if mode == "accounting_focus":
        track = "DE" if counter % 2 == 0 else "Accounting"
    else:  # de_focus
        track = "Accounting" if counter % 5 == 4 else "DE"
    prefs["schedule_counter"] = counter + 1
    save_prefs(prefs)
    return track


def best_topic_for_track(track: str) -> tuple:
    """Pick a (topic, difficulty) pair for a scheduled push within `track`."""
    weak = get_weak_topics(track=track) or []
    if weak:
        topic, accuracy, _ = weak[0]
        if accuracy < 0.4:
            difficulty = "Beginner"
        elif accuracy < 0.65:
            difficulty = "Intermediate"
        else:
            difficulty = "Advanced"
        return topic, difficulty
    pool = ACCOUNTING_TOPICS if track == "Accounting" else DE_TOPICS
    return random.choice(pool), "Intermediate"

# ---------------------------------------------------------------------
# In-memory session state
# ---------------------------------------------------------------------

def _new_session_state() -> dict:
    return {
        "active": False,
        "started_at": None,
        "topics": [],
        "concept_keys": [],
        "correct": 0,
        "wrong": 0,
        "current_streak": 0,
        "max_streak": 0,
    }


session: dict = _new_session_state()


def _ensure_session_active() -> None:
    if not session["active"]:
        session.update(_new_session_state())
        session["active"] = True
        session["started_at"] = datetime.now(timezone.utc)


def _record_session_attempt(topic: str, concept_key: Optional[str], is_correct: bool) -> None:
    _ensure_session_active()
    if topic:
        session["topics"].append(topic)
    if concept_key:
        session["concept_keys"].append(concept_key)
    if is_correct:
        session["correct"] += 1
        session["current_streak"] += 1
        if session["current_streak"] > session["max_streak"]:
            session["max_streak"] = session["current_streak"]
    else:
        session["wrong"] += 1
        session["current_streak"] = 0


def _build_session_summary() -> str:
    if not session["active"]:
        return "🏁 <b>No active session.</b> Send /quiz to start one."
    total = session["correct"] + session["wrong"]
    accuracy = round((session["correct"] / total) * 100) if total else 0
    topics = ", ".join(sorted(set(session["topics"]))) or "(none)"
    overall_streak = get_streak()
    return (
        "🏁 <b>Session Summary</b>\n\n"
        f"📚 Topics covered: {html.escape(topics)}\n"
        f"📝 Questions: {total}\n"
        f"✅ Correct: {session['correct']}    ❌ Wrong: {session['wrong']}\n"
        f"🎯 Accuracy: {accuracy}%\n"
        f"🔥 Best streak this session: {session['max_streak']}\n"
        f"📅 Overall daily streak: {overall_streak}\n\n"
        f"<i>Send /quiz when you want another one.</i>"
    )


def _reset_session() -> None:
    session.update(_new_session_state())


# Wire queue providers now that DB and prefs helpers are in scope.
question_queue.blocked_provider = get_blocked_concept_keys
question_queue.track_provider = lambda: pick_track_for_quiz(get_mode())


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def is_authorised(update: Update) -> bool:
    return update.effective_user.id == USER_ID


def _next_end_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➡️ Next Question", callback_data="next"),
        InlineKeyboardButton("⏹ End Session", callback_data="end"),
    ]])


def _mcq_keyboard(send_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data=f"ans_{send_id}_0"),
        InlineKeyboardButton("B", callback_data=f"ans_{send_id}_1"),
        InlineKeyboardButton("C", callback_data=f"ans_{send_id}_2"),
        InlineKeyboardButton("D", callback_data=f"ans_{send_id}_3"),
    ]])


# ---------------------------------------------------------------------
# Sending a question
# ---------------------------------------------------------------------

async def send_scheduled_quiz(
    context=None,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    from_queue: bool = False,
    track: Optional[str] = None,
):
    """Send one quiz question to the configured user.

    Caller patterns:
    - /quiz, /next, "Next Question" button -> from_queue=True, topic=None.
      Track is picked weighted by the current mode.
    - /topic pick -> topic=<topic>, from_queue=False. Track derived from topic.
    - Scheduled cron push -> no args (from_queue=False, topic=None). Track is
      picked deterministically by the rotation counter.
    """
    global pending_coding_send_id
    app = context.application if context else _app

    is_scheduled_push = (not from_queue and topic is None)

    try:
        review_prefix = ""
        q: Optional[dict] = None

        # Resolve the desired track BEFORE choosing the path.
        if topic:
            track = track or topic_to_track(topic)
        elif from_queue:
            track = track or pick_track_for_quiz(get_mode())
        else:
            # Scheduled cron push.
            track = track or pick_track_for_scheduled()

        if topic is None:
            # Both /quiz and scheduled push check the "due for re-review" queue first.
            due = await asyncio.to_thread(get_due_review_concepts)
            # Prefer a due-concept that matches the desired track to honour the mode.
            matched = next((d for d in due if topic_to_track(d["topic"]) == track), None)
            review = matched or (due[0] if due else None)
            if review:
                track = topic_to_track(review["topic"])
                blocked = await asyncio.to_thread(get_blocked_concept_keys)
                blocked.discard(review["concept_key"])
                q = await asyncio.to_thread(
                    generate_question,
                    review["topic"],
                    "Intermediate",
                    None,
                    blocked,
                    get_recent_mcq_positions(),
                    review["concept_key"],
                    None,
                    track,
                )
                review_prefix = "📅 <b>7 days ago you got this wrong. Can you get it now?</b>\n\n"

        if q is None:
            if topic:
                blocked = await asyncio.to_thread(get_blocked_concept_keys)
                q = await asyncio.to_thread(
                    generate_question,
                    topic,
                    difficulty or "Intermediate",
                    None,
                    blocked,
                    get_recent_mcq_positions(),
                    None,
                    None,
                    track,
                )
            elif from_queue:
                q = await question_queue.pop(desired_track=track)
            else:
                # Scheduled cron push — pick weak topic within the chosen track.
                topic, difficulty = best_topic_for_track(track)
                blocked = await asyncio.to_thread(get_blocked_concept_keys)
                q = await asyncio.to_thread(
                    generate_question,
                    topic,
                    difficulty or "Intermediate",
                    None,
                    blocked,
                    get_recent_mcq_positions(),
                    None,
                    None,
                    track,
                )

        # Rebalance MCQ correct-answer position if needed (B-window rule).
        from quiz_engine import _rebalance_mcq_position
        q = _rebalance_mcq_position(q)

        send_id = save_scheduled_send(q["topic"], q["difficulty"])
        send_id_str = str(send_id)
        pending_questions[send_id_str] = {
            "question": q,
            "sent_at": datetime.now(timezone.utc),
        }

        msg_body = format_question_message(q, send_id)
        msg = review_prefix + msg_body

        if q.get("type") == "coding":
            pending_coding_send_id = send_id_str
            await app.bot.send_message(
                chat_id=USER_ID,
                text=msg,
                parse_mode="HTML",
            )
        else:
            await app.bot.send_message(
                chat_id=USER_ID,
                text=msg,
                parse_mode="HTML",
                reply_markup=_mcq_keyboard(send_id),
            )
            # Track the position we ARE going to assign as correct (final, post-rebalance).
            try:
                record_mcq_position(int(q.get("correct", 0)))
            except Exception:
                pass

        _ensure_session_active()
    except Exception as e:
        logging.exception("Error sending quiz")
        if app is not None:
            await app.bot.send_message(
                chat_id=USER_ID,
                text=f"⚠️ Failed to generate question: {str(e)}",
            )


# ---------------------------------------------------------------------
# Answer handlers
# ---------------------------------------------------------------------

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MCQ answer button handler."""
    if not is_authorised(update):
        return

    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")  # ans_{send_id}_{choice_idx}
    send_id = parts[1]
    choice_idx = int(parts[2])

    if send_id not in pending_questions:
        await query.edit_message_text("⏱ This question has expired. Send /quiz for a new one.")
        return

    pending = pending_questions.pop(send_id)
    q = pending["question"]
    sent_at = pending["sent_at"]

    response_time = int((datetime.now(timezone.utc) - sent_at).total_seconds())
    is_correct = choice_idx == q["correct"]
    letters = ["A", "B", "C", "D"]

    mark_answered(int(send_id))
    save_attempt(
        topic=q["topic"],
        difficulty=q["difficulty"],
        question=q["question"],
        correct=is_correct,
        response_time_seconds=response_time,
        sent_at=sent_at.isoformat(),
        concept_key=q.get("concept_key"),
        track=q.get("track") or topic_to_track(q.get("topic", "")),
    )
    _record_session_attempt(q.get("topic", ""), q.get("concept_key"), is_correct)

    result_emoji = "✅" if is_correct else "❌"
    correct_letter = letters[q["correct"]]
    verdict = "Correct!" if is_correct else "Wrong."
    correct_option = html.escape(str(q["options"][q["correct"]]))
    explanation = html.escape(str(q.get("explanation", "")))
    tip = html.escape(str(q.get("tip", "")))
    snippet_block = format_snippet_block(q.get("snippet", ""), q.get("topic", ""))

    parts_out = [
        f"{result_emoji} <b>{verdict}</b>",
        f"<b>Answer: {correct_letter}. {correct_option}</b>",
        f"💡 {explanation}",
        f"🎯 <b>Interview tip:</b> {tip}",
    ]
    if snippet_block:
        parts_out.append(f"🧪 <b>Try this:</b>\n{snippet_block}")
    parts_out.append(f"⏱ Answered in {response_time}s")

    response = "\n\n".join(parts_out)
    await query.edit_message_text(
        response,
        parse_mode="HTML",
        reply_markup=_next_end_keyboard(),
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Free-text handler — used to receive coding-question answers."""
    if not is_authorised(update):
        return
    if not update.message or not update.message.text:
        return

    global pending_coding_send_id
    text = update.message.text.strip()

    if pending_coding_send_id is None or pending_coding_send_id not in pending_questions:
        await update.message.reply_text(
            "ℹ️ No coding question is open right now. Send /quiz to start one."
        )
        return

    send_id = pending_coding_send_id
    pending = pending_questions.pop(send_id)
    pending_coding_send_id = None

    q = pending["question"]
    sent_at = pending["sent_at"]
    response_time = int((datetime.now(timezone.utc) - sent_at).total_seconds())

    await update.message.reply_text("🧠 Evaluating your answer…")

    try:
        evaluation = await asyncio.to_thread(evaluate_coding_answer, q, text)
    except Exception as exc:
        logging.exception("Coding eval failed")
        # Re-arm the question so the user can retry, but don't block them either.
        pending_questions[send_id] = pending
        pending_coding_send_id = send_id
        await update.message.reply_text(
            f"⚠️ Couldn't evaluate that answer: {exc}\nReply again to retry, or /next to skip.",
            reply_markup=_next_end_keyboard(),
        )
        return

    is_correct = evaluation["logic_correct"]
    mark_answered(int(send_id))
    save_attempt(
        topic=q["topic"],
        difficulty=q["difficulty"],
        question=q["question"],
        correct=is_correct,
        response_time_seconds=response_time,
        sent_at=sent_at.isoformat(),
        concept_key=q.get("concept_key"),
        track=q.get("track") or topic_to_track(q.get("topic", "")),
    )
    _record_session_attempt(q.get("topic", ""), q.get("concept_key"), is_correct)

    logic_line = (
        "✅ <b>Logic:</b> Correct" if is_correct else "❌ <b>Logic:</b> Incorrect"
    )
    user_output = html.escape(evaluation["user_output"]) if evaluation["user_output"] else "(no output)"
    feedback = html.escape(evaluation["feedback"])
    tip = html.escape(evaluation["tip"] or q.get("tip", ""))

    canonical_block = format_code_block(
        q.get("correct_solution") or q.get("snippet", ""),
        q.get("language", ""),
    )
    expected = q.get("expected_output", "")
    expected_block = f"<pre>{html.escape(str(expected))}</pre>" if expected else ""

    parts_out = [
        logic_line,
        f"📊 <b>Your Output:</b>\n<pre>{user_output}</pre>",
        f"💡 <b>Feedback:</b> {feedback}",
        f"🎯 <b>Interview Tip:</b> {tip}",
    ]
    if expected_block:
        parts_out.append(f"📐 <b>Expected output:</b>\n{expected_block}")
    if canonical_block:
        parts_out.append(f"✓ <b>Canonical solution:</b>\n{canonical_block}")
    parts_out.append(f"⏱ Answered in {response_time}s")

    response = "\n\n".join(parts_out)
    # Telegram message hard-limit safety — trim if needed.
    if len(response) > 4000:
        response = response[:3990] + "\n…(trimmed)"

    await update.message.reply_text(
        response,
        parse_mode="HTML",
        reply_markup=_next_end_keyboard(),
    )


# ---------------------------------------------------------------------
# Next / End session buttons
# ---------------------------------------------------------------------

async def handle_session_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    query = update.callback_query
    await query.answer()

    if query.data == "next":
        await send_scheduled_quiz(context, from_queue=True)
    elif query.data == "end":
        summary = _build_session_summary()
        _reset_session()
        await query.message.reply_text(summary, parse_mode="HTML")


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    _reset_session()
    label = MODE_LABELS.get(get_mode(), MODE_LABELS["de_focus"])
    await update.message.reply_text(
        "👋 *DE & Accounting Quiz Bot is live.*\n\n"
        f"📚 Current mode: *{label}*\n\n"
        "Commands:\n"
        "/quiz — get a question now\n"
        "/next — fetch the next question\n"
        "/mode — switch DE / Accounting mix\n"
        "/topic — pick a specific topic\n"
        "/report — see your weak spots\n"
        "/streak — see your current streak\n"
        "/schedule — view or change quiz times\n"
        "/stats — full performance breakdown\n\n"
        "After each answer you'll see ➡️ Next Question and ⏹ End Session buttons. "
        "Coding questions: just reply with your SQL/SuiteQL/Python as a normal text message.",
        parse_mode="Markdown",
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    current = get_mode()
    label = MODE_LABELS.get(current, MODE_LABELS["de_focus"])
    keyboard = [
        [InlineKeyboardButton("🔢 DE Focus (80/20)", callback_data="mode_de_focus")],
        [InlineKeyboardButton("📊 Accounting Focus (50/50)", callback_data="mode_accounting_focus")],
        [InlineKeyboardButton("📒 Accounting Only", callback_data="mode_accounting_only")],
        [InlineKeyboardButton("💻 DE Only", callback_data="mode_de_only")],
    ]
    await update.message.reply_text(
        f"📚 Current mode: {label}\nPick a mode:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_mode_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    query = update.callback_query
    await query.answer()
    new_mode = query.data.replace("mode_", "")
    if new_mode not in MODE_LABELS:
        await query.edit_message_text("⚠️ Unknown mode.")
        return
    set_mode(new_mode)
    await query.edit_message_text(
        f"✅ Mode set to: <b>{html.escape(MODE_LABELS[new_mode])}</b>\n\n"
        "Send /quiz to try it out.",
        parse_mode="HTML",
    )


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    await send_scheduled_quiz(context, from_queue=True)


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same as tapping the ➡️ Next Question button."""
    if not is_authorised(update):
        return
    await send_scheduled_quiz(context, from_queue=True)


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return

    keyboard: list = []

    keyboard.append([InlineKeyboardButton("━ Data Engineering ━", callback_data="noop")])
    row: list = []
    for topic in DE_TOPICS:
        row.append(InlineKeyboardButton(topic, callback_data=f"topicde|{topic}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("━ Accounting & Finance ━", callback_data="noop")])
    row = []
    for topic in ACCOUNTING_TOPICS:
        row.append(InlineKeyboardButton(topic, callback_data=f"topicac|{topic}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "Pick a topic:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_topic_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("topicde|"):
        track = "DE"
        topic = data[len("topicde|"):]
    elif data.startswith("topicac|"):
        track = "Accounting"
        topic = data[len("topicac|"):]
    else:
        await query.edit_message_text("⚠️ Unknown topic selection.")
        return
    await query.edit_message_text(
        f"⚡ Generating <b>{html.escape(topic)}</b> question…", parse_mode="HTML"
    )
    await send_scheduled_quiz(context, topic=topic, track=track)


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Acknowledge clicks on the section-header buttons in /topic."""
    if not is_authorised(update):
        return
    await update.callback_query.answer()


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    report = build_behaviour_report()
    await update.message.reply_text(report, parse_mode="Markdown")


async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    streak = get_streak()
    if streak == 0:
        msg = "❌ No streak yet today. Answer a question to start one."
    elif streak == 1:
        msg = "🔥 1 day streak. Keep it going tomorrow."
    else:
        msg = f"🔥 {streak} day streak. Don't break it."
    await update.message.reply_text(msg)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    weak = get_weak_topics()
    if not weak:
        await update.message.reply_text("No stats yet. Answer some questions first.")
        return

    msg = "*📊 Full Performance Breakdown*\n\n"
    bars = ["🟥", "🟨", "🟩"]
    for topic, acc, total in weak:
        bar = bars[0] if acc < 0.5 else bars[1] if acc < 0.7 else bars[2]
        msg += f"{bar} *{topic}*: {round(acc*100)}% ({total} attempts)\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    t1 = os.getenv("QUIZ_TIME_1", "10:00")
    t2 = os.getenv("QUIZ_TIME_2", "13:30")
    t3 = os.getenv("QUIZ_TIME_3", "18:00")
    await update.message.reply_text(
        f"⏰ *Current Schedule (UK time)*\n\n"
        f"1️⃣ {t1}\n2️⃣ {t2}\n3️⃣ {t3}\n\n"
        f"To change times, update your `.env` file and restart the bot.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------

async def check_reminders():
    unanswered = check_skipped_behaviour()
    for item in unanswered:
        reminder = build_skip_reminder(item["topic"], item["difficulty"])
        await _app.bot.send_message(chat_id=USER_ID, text=reminder, parse_mode="Markdown")
        mark_reminded(item["id"])


async def send_weekly_report():
    report = build_behaviour_report()
    await _app.bot.send_message(chat_id=USER_ID, text=report, parse_mode="Markdown")


async def _post_init(app: Application) -> None:
    """Once the event loop is running, kick off the initial queue prefill in the background."""
    logging.info("Pre-generating initial question queue (target=%d)…", question_queue.target_size)
    asyncio.create_task(question_queue.topup(question_queue.target_size))


def main():
    global _app
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    _app = app

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(CommandHandler("topic", topic_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("streak", streak_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CallbackQueryHandler(handle_answer, pattern="^ans_"))
    app.add_handler(CallbackQueryHandler(handle_topic_pick, pattern=r"^topic(de|ac)\|"))
    app.add_handler(CallbackQueryHandler(handle_mode_pick, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(handle_session_button, pattern="^(next|end)$"))
    app.add_handler(CallbackQueryHandler(handle_noop, pattern="^noop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    scheduler = setup_scheduler(app, send_scheduled_quiz, check_reminders, send_weekly_report)
    scheduler.start()

    logging.info("Bot started. Scheduler running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
