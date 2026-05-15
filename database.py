import os
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def setup_tables():
    """Run this once to create tables via Supabase SQL editor"""
    sql = """
    CREATE TABLE IF NOT EXISTS quiz_attempts (
        id SERIAL PRIMARY KEY,
        topic TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        question TEXT NOT NULL,
        correct BOOLEAN NOT NULL,
        response_time_seconds INTEGER,
        sent_at TIMESTAMPTZ DEFAULT NOW(),
        answered_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS scheduled_sends (
        id SERIAL PRIMARY KEY,
        sent_at TIMESTAMPTZ DEFAULT NOW(),
        topic TEXT,
        difficulty TEXT,
        answered BOOLEAN DEFAULT FALSE,
        reminded BOOLEAN DEFAULT FALSE
    );

    CREATE TABLE IF NOT EXISTS user_behaviour (
        id SERIAL PRIMARY KEY,
        date DATE DEFAULT CURRENT_DATE,
        questions_sent INTEGER DEFAULT 0,
        questions_answered INTEGER DEFAULT 0,
        streak_days INTEGER DEFAULT 0,
        best_topic TEXT,
        worst_topic TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    print("Copy this SQL and run it in your Supabase SQL editor:")
    print(sql)

def save_attempt(topic, difficulty, question, correct, response_time_seconds=None, sent_at=None):
    supabase.table("quiz_attempts").insert({
        "topic": topic,
        "difficulty": difficulty,
        "question": question,
        "correct": correct,
        "response_time_seconds": response_time_seconds,
        "sent_at": sent_at or datetime.now(timezone.utc).isoformat(),
        "answered_at": datetime.now(timezone.utc).isoformat()
    }).execute()

def save_scheduled_send(topic, difficulty):
    result = supabase.table("scheduled_sends").insert({
        "topic": topic,
        "difficulty": difficulty,
        "sent_at": datetime.now(timezone.utc).isoformat()
    }).execute()
    return result.data[0]["id"]

def mark_answered(send_id):
    supabase.table("scheduled_sends").update({
        "answered": True
    }).eq("id", send_id).execute()

def mark_reminded(send_id):
    supabase.table("scheduled_sends").update({
        "reminded": True
    }).eq("id", send_id).execute()

def get_weak_topics():
    result = supabase.table("quiz_attempts").select("topic, correct").execute()
    if not result.data:
        return None

    topic_stats = {}
    for row in result.data:
        t = row["topic"]
        if t not in topic_stats:
            topic_stats[t] = {"correct": 0, "total": 0}
        topic_stats[t]["total"] += 1
        if row["correct"]:
            topic_stats[t]["correct"] += 1

    ranked = sorted(
        [(t, s["correct"] / s["total"], s["total"]) for t, s in topic_stats.items() if s["total"] >= 2],
        key=lambda x: x[1]
    )
    return ranked

def get_recent_performance(days=7):
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = supabase.table("quiz_attempts").select("*").gte("sent_at", since).execute()
    return result.data

def get_unanswered_recent():
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    result = supabase.table("scheduled_sends").select("*").gte("sent_at", since).eq("answered", False).eq("reminded", False).execute()
    return result.data

def get_streak():
    from datetime import timedelta, date
    result = supabase.table("quiz_attempts").select("answered_at").order("answered_at", desc=True).execute()
    if not result.data:
        return 0

    dates = set()
    for row in result.data:
        if row["answered_at"]:
            d = datetime.fromisoformat(row["answered_at"].replace("Z", "+00:00")).date()
            dates.add(d)

    streak = 0
    check_date = date.today()
    while check_date in dates:
        streak += 1
        check_date -= timedelta(days=1)
    return streak
