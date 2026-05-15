import os
import asyncio
import html
import json
import logging
import random
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

TOPICS = ["SQL", "Python", "ETL/ELT", "dbt", "OLAP", "Data Modelling", "PySpark", "Data Warehousing"]

# Telegram <pre><code class="language-XYZ"> highlighter hint per topic.
LANG_BY_TOPIC = {
    "SQL": "sql",
    "Python": "python",
    "ETL/ELT": "python",
    "dbt": "sql",
    "OLAP": "sql",
    "Data Modelling": "sql",
    "PySpark": "python",
    "Data Warehousing": "sql",
}

SYSTEM_PROMPT = """You are a senior data engineering interviewer at a top fintech (Wise, Monzo, Checkout.com level).
Generate one quiz question for a data engineer preparing for interviews.

Respond ONLY in this exact JSON format, nothing else:
{
  "topic": "the topic name",
  "difficulty": "the difficulty level",
  "question": "the question text",
  "options": ["A", "B", "C", "D"],
  "correct": 0,
  "explanation": "why this answer is correct (2-3 sentences)",
  "tip": "one practical interview tip on this topic",
  "snippet": "short, runnable code (max 15 lines) demonstrating the concept of THIS specific question"
}

Rules:
- options has exactly 4 items
- correct is the index 0-3 of the correct option
- Make questions scenario-based and practical, not just definitions
- SQL: window functions, CTEs, query optimisation, indexes
- Python: pandas, generators, decorators, pipeline patterns
- ETL/ELT: idempotency, incremental loads, CDC, error handling
- dbt: ref(), sources, tests, macros, incremental strategies, snapshots
- OLAP: star schema, fact/dimension tables, aggregations, MDX concepts
- Data Modelling: Kimball, Data Vault, SCD types, normalisation
- PySpark: partitioning, transformations, optimisation, DataFrames
- Data Warehousing: Snowflake, BigQuery, Fabric, clustering, partitioning

Snippet rules:
- Max 15 lines, practical and memorable, must illustrate THIS question's concept
- Use plain code only — DO NOT wrap the snippet in markdown fences (no ``` lines)
- Pick the right language per topic:
  * SQL / dbt / OLAP / Data Modelling / Data Warehousing -> SQL
  * Python / ETL/ELT -> Python
  * PySpark -> Python (PySpark)
- Use realistic fintech-flavoured names (transactions, payments, ledger, etc.) where relevant
- The snippet must be valid JSON-encoded inside the response (escape newlines as \\n inside the JSON string)"""


def _clean_snippet(s: str) -> str:
    """Strip stray markdown fences / extra whitespace and clip to 15 lines."""
    if not s:
        return ""
    s = s.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1 :] if first_newline != -1 else ""
    if s.endswith("```"):
        last_newline = s.rfind("\n")
        s = s[:last_newline] if last_newline != -1 else ""
    s = s.strip("\n")
    lines = s.splitlines()
    if len(lines) > 15:
        lines = lines[:15]
    return "\n".join(lines)


def generate_question(topic: Optional[str] = None, difficulty: str = "Intermediate", avoid_topic: Optional[str] = None) -> dict:
    """Synchronously call Anthropic and return a validated question dict.

    If `topic` is None, a random topic is chosen — different from `avoid_topic` if possible.
    Always called via asyncio.to_thread() so it doesn't block the event loop.
    """
    if not topic:
        choices = [t for t in TOPICS if t != avoid_topic] or TOPICS
        topic = random.choice(choices)

    prompt = f"Generate a {difficulty} level question on: {topic}. Make it practical and fintech-relevant."

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    q = json.loads(text)

    q.setdefault("topic", topic)
    q.setdefault("difficulty", difficulty)
    q["snippet"] = _clean_snippet(q.get("snippet", ""))
    return q


def format_question_message(q: dict, send_id) -> str:
    letters = ["A", "B", "C", "D"]
    topic = html.escape(str(q["topic"]))
    difficulty = html.escape(str(q["difficulty"]))
    question = html.escape(str(q["question"]))
    options_text = "\n".join(
        f"{letters[i]}. {html.escape(str(opt))}" for i, opt in enumerate(q["options"])
    )

    msg = (
        f"📊 <b>DE Quiz — {topic}</b> <code>{difficulty}</code>\n\n"
        f"{question}\n\n"
        f"{options_text}\n\n"
        f"<i>Reply with A, B, C, or D</i>\n"
        f"<i>ID: {send_id}</i>"
    )
    return msg


def format_snippet_block(snippet: str, topic: str) -> str:
    """Return a Telegram HTML <pre><code> block for the snippet, or empty string if none."""
    if not snippet:
        return ""
    lang = LANG_BY_TOPIC.get(topic, "")
    safe = html.escape(snippet)
    if lang:
        return f'<pre><code class="language-{lang}">{safe}</code></pre>'
    return f"<pre>{safe}</pre>"


class QuestionQueue:
    """In-memory async queue of pre-generated questions with topic-diversity.

    - On startup, `topup(target_size)` is fired to fill the queue.
    - When a question is popped and the queue drops to <= min_size, a background
      refill of `refill_amount` questions is triggered.
    - Diversity: at generation time we avoid the just-added topic, and at
      pop time we prefer the first item whose topic differs from the just-served one.
    """

    def __init__(self, target_size: int = 5, min_size: int = 2, refill_amount: int = 3):
        self.target_size = target_size
        self.min_size = min_size
        self.refill_amount = refill_amount
        self._queue: list[dict] = []
        self._last_added_topic: Optional[str] = None
        self._last_served_topic: Optional[str] = None
        self._lock = asyncio.Lock()
        self._topping_up = False

    def __len__(self) -> int:
        return len(self._queue)

    async def topup(self, n: Optional[int] = None) -> None:
        """Generate up to `n` questions and append them, avoiding consecutive same topics."""
        n = n if n is not None else self.refill_amount
        for _ in range(n):
            try:
                q = await asyncio.to_thread(
                    generate_question, None, "Intermediate", self._last_added_topic
                )
            except Exception as exc:
                logger.warning("Queue topup: failed to generate question: %s", exc)
                continue
            self._queue.append(q)
            self._last_added_topic = q.get("topic")
            logger.info("Queue topup: added %s (size=%d)", q.get("topic"), len(self._queue))

    def _maybe_trigger_refill(self) -> None:
        if self._topping_up:
            return
        if len(self._queue) > self.min_size:
            return

        async def _runner():
            self._topping_up = True
            try:
                await self.topup(self.refill_amount)
            finally:
                self._topping_up = False

        asyncio.create_task(_runner())

    async def pop(self) -> dict:
        """Return one question. Generates one inline if the queue is empty."""
        async with self._lock:
            if not self._queue:
                logger.info("Queue empty on pop -> generating one inline")
                q = await asyncio.to_thread(
                    generate_question, None, "Intermediate", self._last_served_topic
                )
            else:
                pick_idx = 0
                if self._last_served_topic:
                    for i, item in enumerate(self._queue):
                        if item.get("topic") != self._last_served_topic:
                            pick_idx = i
                            break
                q = self._queue.pop(pick_idx)

            self._last_served_topic = q.get("topic")
            self._maybe_trigger_refill()
            return q


question_queue = QuestionQueue()
