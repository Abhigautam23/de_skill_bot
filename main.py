import os
import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

from quiz_engine import (
    generate_question, format_question_message, format_snippet_block,
    question_queue, TOPICS,
)
from database import (
    save_attempt, save_scheduled_send, mark_answered,
    mark_reminded, get_weak_topics, get_streak,
)
from behaviour import (
    get_best_topic_for_next_quiz, build_behaviour_report,
    check_skipped_behaviour, build_skip_reminder,
)
from scheduler import setup_scheduler

load_dotenv()
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USER_ID = int(os.getenv("TELEGRAM_USER_ID"))

AUTO_CONTINUE_DELAY_SECONDS = 5

pending_questions: dict[str, dict] = {}  # send_id -> {question, sent_at}
auto_continue_task: Optional[asyncio.Task] = None  # pending "next question in 5s" task

_app = None


def is_authorised(update: Update) -> bool:
    return update.effective_user.id == USER_ID


def _cancel_auto_continue() -> None:
    """Cancel any pending auto-continue task (e.g. when user manually requests a quiz)."""
    global auto_continue_task
    if auto_continue_task and not auto_continue_task.done():
        auto_continue_task.cancel()
    auto_continue_task = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    await update.message.reply_text(
        "👋 *DE Quiz Bot is live.*\n\n"
        "Commands:\n"
        "/quiz — get a question now\n"
        "/skip — skip the 5s wait between questions\n"
        "/topic — pick a specific topic\n"
        "/report — see your weak spots\n"
        "/streak — see your current streak\n"
        "/schedule — view or change quiz times\n"
        "/stats — full performance breakdown",
        parse_mode="Markdown",
    )


async def send_scheduled_quiz(
    context=None,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    from_queue: bool = False,
):
    """Send one quiz question to the configured user.

    - `from_queue=True`  -> pop a pre-generated mixed-topic question (used by /quiz, auto-continue).
    - `from_queue=False` -> generate fresh for `topic` (used by /topic) or for the
      weakest-topic scheduled cron pushes (preserves original behaviour).
    """
    app = context.application if context else _app

    try:
        if from_queue and not topic:
            q = await question_queue.pop()
        else:
            if not topic:
                topic, difficulty = get_best_topic_for_next_quiz()
            q = await asyncio.to_thread(
                generate_question, topic, difficulty or "Intermediate"
            )

        send_id = save_scheduled_send(q["topic"], q["difficulty"])
        pending_questions[str(send_id)] = {
            "question": q,
            "sent_at": datetime.now(timezone.utc),
        }

        msg = format_question_message(q, send_id)
        keyboard = [[
            InlineKeyboardButton("A", callback_data=f"ans_{send_id}_0"),
            InlineKeyboardButton("B", callback_data=f"ans_{send_id}_1"),
            InlineKeyboardButton("C", callback_data=f"ans_{send_id}_2"),
            InlineKeyboardButton("D", callback_data=f"ans_{send_id}_3"),
        ]]
        await app.bot.send_message(
            chat_id=USER_ID,
            text=msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logging.error(f"Error sending quiz: {e}")
        await app.bot.send_message(
            chat_id=USER_ID,
            text=f"⚠️ Failed to generate question: {str(e)}",
        )


async def _auto_continue(context, delay: int = AUTO_CONTINUE_DELAY_SECONDS):
    """Wait `delay` seconds then deliver the next queued question. Cancellable."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await send_scheduled_quiz(context, from_queue=True)


def _schedule_auto_continue(context) -> None:
    """Replace any pending auto-continue task with a fresh one."""
    global auto_continue_task
    _cancel_auto_continue()
    auto_continue_task = asyncio.create_task(_auto_continue(context))


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return

    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")  # ans_{send_id}_{choice_idx}
    send_id = parts[1]
    choice_idx = int(parts[2])

    if send_id not in pending_questions:
        await query.edit_message_text("⏱ This question has expired. Wait for the next one.")
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
    )

    result_emoji = "✅" if is_correct else "❌"
    correct_letter = letters[q["correct"]]
    verdict = "Correct!" if is_correct else "Wrong."
    correct_option = html.escape(str(q["options"][q["correct"]]))
    explanation = html.escape(str(q["explanation"]))
    tip = html.escape(str(q["tip"]))
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
    parts_out.append(
        f"<i>Next question in {AUTO_CONTINUE_DELAY_SECONDS}s… or send /skip to get it now.</i>"
    )

    response = "\n\n".join(parts_out)
    await query.edit_message_text(response, parse_mode="HTML")

    _schedule_auto_continue(context)


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    _cancel_auto_continue()
    await send_scheduled_quiz(context, from_queue=True)


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    global auto_continue_task
    if auto_continue_task and not auto_continue_task.done():
        auto_continue_task.cancel()
        auto_continue_task = None
        await send_scheduled_quiz(context, from_queue=True)
    else:
        await update.message.reply_text("Nothing to skip — send /quiz to start one.")


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorised(update):
        return
    _cancel_auto_continue()

    keyboard, row = [], []
    for topic in TOPICS:
        row.append(InlineKeyboardButton(topic, callback_data=f"topic_{topic}"))
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
    _cancel_auto_continue()
    query = update.callback_query
    await query.answer()
    topic = query.data.replace("topic_", "")
    await query.edit_message_text(
        f"⚡ Generating <b>{html.escape(topic)}</b> question…", parse_mode="HTML"
    )
    await send_scheduled_quiz(context, topic=topic)


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
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("topic", topic_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("streak", streak_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CallbackQueryHandler(handle_answer, pattern="^ans_"))
    app.add_handler(CallbackQueryHandler(handle_topic_pick, pattern="^topic_"))

    scheduler = setup_scheduler(app, send_scheduled_quiz, check_reminders, send_weekly_report)
    scheduler.start()

    logging.info("Bot started. Scheduler running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
