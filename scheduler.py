import os
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from dotenv import load_dotenv

load_dotenv()

UK_TZ = pytz.timezone("Europe/London")

def parse_time(time_str):
    h, m = time_str.split(":")
    return int(h), int(m)

def setup_scheduler(app, send_quiz_func, check_reminders_func, send_report_func):
    scheduler = AsyncIOScheduler(timezone=UK_TZ)

    t1 = os.getenv("QUIZ_TIME_1", "10:00")
    t2 = os.getenv("QUIZ_TIME_2", "13:30")
    t3 = os.getenv("QUIZ_TIME_3", "18:00")

    for time_str in [t1, t2, t3]:
        h, m = parse_time(time_str)
        scheduler.add_job(
            send_quiz_func,
            CronTrigger(hour=h, minute=m, timezone=UK_TZ),
            id=f"quiz_{h}_{m}"
        )

    scheduler.add_job(
        check_reminders_func,
        CronTrigger(minute=0, hour="*/2", timezone=UK_TZ),
        id="reminder_check"
    )

    scheduler.add_job(
        send_report_func,
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=UK_TZ),
        id="weekly_report"
    )

    return scheduler
