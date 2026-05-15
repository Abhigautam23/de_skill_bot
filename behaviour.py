from database import get_weak_topics, get_recent_performance, get_streak, get_unanswered_recent
from datetime import datetime, timezone

def get_best_topic_for_next_quiz():
    weak = get_weak_topics()
    if not weak:
        return None, "Intermediate"

    worst_topic, accuracy, total = weak[0]

    if accuracy < 0.4:
        difficulty = "Beginner"
    elif accuracy < 0.65:
        difficulty = "Intermediate"
    else:
        difficulty = "Advanced"

    return worst_topic, difficulty

def build_behaviour_report():
    recent = get_recent_performance(days=7)
    weak = get_weak_topics()
    streak = get_streak()

    if not recent:
        return "📭 No data yet. Answer some questions to see your behaviour report."

    total = len(recent)
    correct = sum(1 for r in recent if r["correct"])
    accuracy = round((correct / total) * 100) if total > 0 else 0

    report = f"""📈 *Your Weekly Behaviour Report*

🔥 Streak: {streak} day{"s" if streak != 1 else ""}
📝 Questions this week: {total}
✅ Accuracy: {accuracy}%

"""
    if weak:
        report += "*Topic Breakdown:*\n"
        for topic, acc, count in weak[:5]:
            bar = "🟥" if acc < 0.5 else "🟨" if acc < 0.7 else "🟩"
            report += f"{bar} {topic}: {round(acc*100)}% ({count} attempts)\n"

        worst = weak[0]
        report += f"\n⚠️ *Weakest area: {worst[0]}* ({round(worst[1]*100)}% accuracy)\n"
        report += "Your next scheduled quiz will focus on this topic."

    if streak == 0:
        report += "\n❌ *You haven't answered anything today. Don't break the habit.*"
    elif streak >= 3:
        report += f"\n💪 *{streak} day streak — keep it going.*"

    return report

def check_skipped_behaviour():
    unanswered = get_unanswered_recent()
    return unanswered

def build_skip_reminder(topic, difficulty):
    messages = [
        f"⏰ You haven't answered your *{topic}* question yet. Takes 30 seconds.",
        f"👀 Still waiting on your *{topic}* answer. Don't let it slide.",
        f"🎯 Quick reminder — your *{topic}* quiz is still open. Answer it now.",
    ]
    import random
    return random.choice(messages)
