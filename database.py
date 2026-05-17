# =====================================================================
# MIGRATION — run this once manually in the Supabase SQL editor:
#
#     ALTER TABLE quiz_attempts ADD COLUMN IF NOT EXISTS concept_key TEXT;
#     ALTER TABLE quiz_attempts ADD COLUMN IF NOT EXISTS track TEXT DEFAULT 'DE';
#
# - concept_key: per-question concept identifier (used for the 7-day no-repeat
#   and 30-day no-repeat-correct dedupe rules).
# - track: 'DE' or 'Accounting'. DEFAULT 'DE' backfills existing rows.
# =====================================================================

import os
from datetime import datetime, timezone, timedelta

from supabase import create_client
from dotenv import load_dotenv

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
        answered_at TIMESTAMPTZ,
        concept_key TEXT,
        track TEXT DEFAULT 'DE'
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


def save_attempt(
    topic,
    difficulty,
    question,
    correct,
    response_time_seconds=None,
    sent_at=None,
    concept_key=None,
    track="DE",
):
    supabase.table("quiz_attempts").insert({
        "topic": topic,
        "difficulty": difficulty,
        "question": question,
        "correct": correct,
        "response_time_seconds": response_time_seconds,
        "sent_at": sent_at or datetime.now(timezone.utc).isoformat(),
        "answered_at": datetime.now(timezone.utc).isoformat(),
        "concept_key": concept_key,
        "track": track,
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


def get_weak_topics(track=None):
    """Return [(topic, accuracy, total_attempts), ...] sorted weakest-first.

    Pass `track="DE"` or `track="Accounting"` to scope to a single learning
    track. Default (no arg) preserves existing behaviour and returns ALL topics
    pooled together — used by behaviour.py / weekly report.
    """
    query = supabase.table("quiz_attempts").select("topic, correct, track")
    if track:
        query = query.eq("track", track)
    result = query.execute()
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
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = supabase.table("quiz_attempts").select("*").gte("sent_at", since).execute()
    return result.data


def get_unanswered_recent():
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    result = (
        supabase.table("scheduled_sends")
        .select("*")
        .gte("sent_at", since)
        .eq("answered", False)
        .eq("reminded", False)
        .execute()
    )
    return result.data


def get_streak():
    from datetime import date
    result = (
        supabase.table("quiz_attempts")
        .select("answered_at")
        .order("answered_at", desc=True)
        .execute()
    )
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


# ---------------------------------------------------------------------
# concept_key dedupe helpers
# ---------------------------------------------------------------------

def get_blocked_concept_keys() -> set:
    """Concepts we MUST NOT ask right now.

    A concept_key is blocked if either:
      - it appears in quiz_attempts within the last 7 days (any outcome), OR
      - it was answered correctly within the last 30 days.
    """
    now = datetime.now(timezone.utc)
    since_7d = (now - timedelta(days=7)).isoformat()
    since_30d = (now - timedelta(days=30)).isoformat()

    blocked: set = set()

    r1 = (
        supabase.table("quiz_attempts")
        .select("concept_key")
        .gte("sent_at", since_7d)
        .execute()
    )
    for row in r1.data or []:
        ck = row.get("concept_key")
        if ck:
            blocked.add(ck)

    r2 = (
        supabase.table("quiz_attempts")
        .select("concept_key")
        .gte("sent_at", since_30d)
        .eq("correct", True)
        .execute()
    )
    for row in r2.data or []:
        ck = row.get("concept_key")
        if ck:
            blocked.add(ck)

    return blocked


def get_due_review_concepts() -> list:
    """Concepts that were answered WRONG ~7+ days ago and have not been
    correctly answered since — used to surface the "📅 7 days ago you got
    this wrong" re-ask flow.

    Returns a list of {concept_key, topic, question} dicts, most recent first.
    """
    now = datetime.now(timezone.utc)
    cutoff_90d = (now - timedelta(days=90)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    result = (
        supabase.table("quiz_attempts")
        .select("concept_key, topic, question, correct, sent_at")
        .gte("sent_at", cutoff_90d)
        .order("sent_at", desc=True)
        .execute()
    )

    seen: set = set()
    due: list = []
    for row in result.data or []:
        ck = row.get("concept_key")
        if not ck or ck in seen:
            continue
        seen.add(ck)
        # Only re-ask if the most-recent attempt for this concept was wrong
        # AND that attempt is at least 7 days old.
        if not row.get("correct") and row.get("sent_at") and row["sent_at"] <= cutoff_7d:
            due.append({
                "concept_key": ck,
                "topic": row["topic"],
                "question": row["question"],
            })
    return due
