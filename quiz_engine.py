import os
import asyncio
import html
import json
import logging
import random
from collections import Counter, deque
from typing import Callable, Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DE_TOPICS = [
    "SQL",
    "Python",
    "ETL/ELT",
    "dbt",
    "OLAP",
    "Data Modelling",
    "PySpark",
    "Data Warehousing",
    "Databricks",
]

ACCOUNTING_TOPICS = [
    "Double Entry Bookkeeping",
    "Chart of Accounts",
    "General Ledger",
    "Trial Balance",
    "Financial Statements",
    "Accounts Payable / Receivable",
    "NetSuite Specific",
    "Revenue Recognition",
    "Bank Reconciliation",
    "Management Accounts",
]

# Backwards-compat alias — main.py used to import TOPICS for /topic.
TOPICS = DE_TOPICS

# Accounting topics that allow coding-style questions (SuiteQL).
ACCOUNTING_CODING_TOPICS = {"NetSuite Specific"}


def topic_to_track(topic: str) -> str:
    return "Accounting" if topic in ACCOUNTING_TOPICS else "DE"


# Telegram <pre><code class="language-XYZ"> highlighter hint per topic.
LANG_BY_TOPIC = {
    # DE
    "SQL": "sql",
    "Python": "python",
    "ETL/ELT": "python",
    "dbt": "sql",
    "OLAP": "sql",
    "Data Modelling": "sql",
    "PySpark": "python",
    "Data Warehousing": "sql",
    "Databricks": "sql",
    # Accounting
    "Double Entry Bookkeeping": "",
    "Chart of Accounts": "",
    "General Ledger": "",
    "Trial Balance": "",
    "Financial Statements": "",
    "Accounts Payable / Receivable": "",
    "NetSuite Specific": "sql",
    "Revenue Recognition": "",
    "Bank Reconciliation": "",
    "Management Accounts": "",
}

# 40% MCQ, 60% coding (as per spec).
MCQ_RATIO = 0.40

# Roll-up of the most recent 4 MCQ correct-answer indices that we've sent.
# Used to enforce the "B never appears more than once in any 4-window" rule
# and to hint the model.
recent_correct_positions: deque = deque(maxlen=4)


def get_recent_mcq_positions() -> list:
    return list(recent_correct_positions)


def record_mcq_position(idx: int) -> None:
    recent_correct_positions.append(int(idx))


SYSTEM_PROMPT_DE = """You are a senior data engineering interviewer at a top fintech (Wise, Monzo, Checkout.com level).
Generate ONE quiz question for a data engineer preparing for interviews.

You will be told whether to generate an MCQ or a CODING question. Respect that exactly.

================ FINTECH TABLES (use for coding questions) ================
transactions(id, customer_id, amount, currency, status, created_at)
customers(id, email, country, created_at, tier)
payments(id, transaction_id, method, processed_at, fee)

============================ MCQ — JSON shape ============================
Respond with ONLY this JSON, nothing else:
{
  "type": "mcq",
  "topic": "the topic name",
  "difficulty": "Beginner | Intermediate | Advanced",
  "concept_key": "snake_case_short_concept_id",
  "question": "scenario-based question text",
  "options": ["A_text", "B_text", "C_text", "D_text"],
  "correct": 0,
  "explanation": "why the answer is correct (2-3 sentences)",
  "tip": "one practical interview tip",
  "snippet": "short runnable code (max 15 lines) illustrating THIS question's concept"
}

MCQ rules:
- options has EXACTLY 4 items.
- correct is the integer index 0..3 of the correct option.
- Distribute the correct answer across A(0), B(1), C(2), D(3). NEVER place the correct
  answer at B more than once in any window of 4 consecutive MCQs. Use the
  "recent_correct_positions" hint provided below to balance.
- Make questions scenario-based and practical, not just definitions.

========================== CODING — JSON shape ===========================
Respond with ONLY this JSON, nothing else:
{
  "type": "coding",
  "topic": "the topic name",
  "difficulty": "Beginner | Intermediate | Advanced",
  "concept_key": "snake_case_short_concept_id",
  "language": "sql" | "python",
  "question": "the problem statement — be precise about what to return",
  "sample_data": "monospace-style 5-8 row preview from the relevant fintech table(s) — the candidate must be able to reason about it. Use pipes and aligned columns.",
  "correct_solution": "the canonical solution code — runnable, max 25 lines",
  "expected_output": "what the canonical solution returns when run on sample_data — show as compact rows / values",
  "explanation": "why this approach works (2-3 sentences)",
  "tip": "one practical interview tip",
  "snippet": "same as correct_solution OR a tighter highlight (max 15 lines)"
}

Coding rules:
- The candidate writes raw SQL or raw Python — NO multiple choice.
- Always include 5–8 sample rows in `sample_data` from the fintech tables above
  (transactions / customers / payments). Realistic values (GBP/EUR/USD, fintech-y
  emails, statuses like 'success'/'pending'/'failed').
- `expected_output` MUST be the actual result of running `correct_solution`
  against `sample_data` — keep it short (no more than ~8 rows).
- For PySpark / Python questions, language = "python".
- For SQL / dbt / OLAP / Data Modelling / Data Warehousing, language = "sql".

============================== concept_key ===============================
- Short snake_case id of the CORE concept the question tests. Keep it tight.
- Examples: "window_functions_lag", "dbt_incremental_delete_insert",
  "python_generator_pattern", "scd_type_2_merge", "spark_broadcast_join",
  "star_schema_grain".
- Two questions on the same concept MUST share the same concept_key.

============================ Snippet rules ===============================
- Max 15 lines, plain code only — DO NOT wrap in markdown fences.
- SQL / dbt / OLAP / Data Modelling / Data Warehousing -> SQL.
- Python / ETL/ELT -> Python.   PySpark -> Python (PySpark).
- Use realistic fintech-flavoured names (transactions, payments, ledger, etc.).
- Inside JSON, escape newlines as \\n.

============================== Topic guide ===============================
- SQL: window functions, CTEs, query optimisation, indexes
- Python: pandas, generators, decorators, pipeline patterns
- ETL/ELT: idempotency, incremental loads, CDC, error handling
- dbt: ref(), sources, tests, macros, incremental strategies, snapshots
- OLAP: star schema, fact/dimension tables, aggregations
- Data Modelling: Kimball, Data Vault, SCD types, normalisation
- PySpark: partitioning, transformations, optimisation, DataFrames
- Data Warehousing: Snowflake, BigQuery, Fabric, clustering, partitioning
- Databricks: Unity Catalog, Delta Lake, Auto Loader, DLT pipelines, Photon engine,
  cluster configuration, medallion architecture, MERGE INTO syntax,
  Change Data Feed, Z-ordering, liquid clustering"""


SYSTEM_PROMPT_ACCOUNTING = """You are a senior accountant / financial controller mentoring a candidate preparing for accounting and NetSuite admin interviews.
Generate ONE quiz question.

You will be told whether to generate an MCQ or a CODING question. Respect that exactly.

============== NETSUITE TABLES (use only for NetSuite Specific coding) ==============
transaction(id, type, trandate, status, entity, postingperiod, memo, currency)
transactionline(transaction, account, debit, credit, memo, department, location, foreignamount)
account(id, acctnumber, acctname, accttype, parent, fullname)
customer(id, companyname, email, subsidiary)
vendor(id, companyname, terms)
accountingperiod(id, periodname, startdate, enddate, closed)
subsidiary(id, name, country, currency)

============================ MCQ — JSON shape ============================
Respond with ONLY this JSON, nothing else:
{
  "type": "mcq",
  "topic": "the topic name",
  "difficulty": "Beginner | Intermediate | Advanced",
  "concept_key": "snake_case_short_concept_id",
  "question": "scenario-based question text — present a realistic accounting situation with concrete numbers / dates",
  "options": ["A_text", "B_text", "C_text", "D_text"],
  "correct": 0,
  "explanation": "why the answer is correct (2-3 sentences)",
  "tip": "one practical interview tip",
  "snippet": "short illustrative example — e.g. journal-entry T-account, SuiteQL fragment, or a worked-example calculation. Max 15 lines, plain text."
}

MCQ rules — VERY IMPORTANT:
- Always SCENARIO-BASED, never plain definitions.
  Bad:  "What is double-entry bookkeeping?"
  Good: "A client's trial balance shows debits exceeding credits by £500. Which of the following is the most likely cause?"
- Use concrete numbers (£/$/€), realistic counterparties (vendor names, customer names), and clear question wording.
- options has EXACTLY 4 items.
- correct is the integer index 0..3.
- Distribute the correct answer across A/B/C/D — NEVER place it at B more than once
  in any window of 4 consecutive MCQs. Use the recent_correct_positions hint provided.

========== CODING — only for "NetSuite Specific" topic (SuiteQL) ==========
Respond with ONLY this JSON, nothing else:
{
  "type": "coding",
  "topic": "NetSuite Specific",
  "difficulty": "Beginner | Intermediate | Advanced",
  "concept_key": "snake_case_short_concept_id",
  "language": "sql",
  "question": "Write a SuiteQL query that ... <precise spec>",
  "sample_data": "monospace-style 5-8 row preview from the relevant NetSuite table(s) — pipes and aligned columns.",
  "correct_solution": "the canonical SuiteQL — runnable, max 25 lines",
  "expected_output": "compact rows / values produced by the canonical solution against sample_data",
  "explanation": "why this approach works (2-3 sentences)",
  "tip": "one practical NetSuite interview tip",
  "snippet": "same as correct_solution OR a tighter highlight (max 15 lines)"
}

Coding rules:
- Coding questions are ONLY allowed when topic == "NetSuite Specific".
  For every other accounting topic, ALWAYS return an MCQ — never coding.
- Use realistic NetSuite table/column names from the schema above.
- `expected_output` must reflect what the canonical SuiteQL would actually return.
- Focus areas: trial balance extracts, transaction-line aggregations, joins to
  account/customer/vendor, period filtering, custom-segment / department slicing.

============================== concept_key ===============================
- Short snake_case id of the CORE concept being tested.
- Examples: "trial_balance_transposition_error", "asc606_performance_obligation",
  "bank_rec_outstanding_cheque", "suiteql_tb_by_period",
  "ap_three_way_match", "scd_journal_accrual_reversal".
- Two questions on the same concept MUST share the same concept_key.

============================== Topic guide ===============================
- Double Entry Bookkeeping: debit/credit rules, journal entry structure, T-accounts.
- Chart of Accounts: account types (Asset/Liability/Equity/Income/Expense), numbering, hierarchy.
- General Ledger: GL postings, period close, reconciliation between GL and sub-ledgers.
- Trial Balance: balanced totals, transposition / omission / compensating / commission errors,
  adjusted vs unadjusted TB.
- Financial Statements: P&L vs Balance Sheet vs Cash Flow articulation, retained earnings
  movement, indirect-method cash flow.
- Accounts Payable / Receivable: 3-way match, ageing buckets, dunning, AR factoring,
  reconciliation to GL.
- NetSuite Specific: SuiteQL syntax, saved searches (criteria/results/highlighting),
  record types, custom segments, transaction line vs header, posting period status.
- Revenue Recognition: ASC 606 5-step, performance obligations, deferred revenue mechanics,
  contract modifications, accruals.
- Bank Reconciliation: outstanding cheques, deposits in transit, NSF, bank vs book balance.
- Management Accounts: variance analysis (price/volume/mix), budget vs actual, contribution
  margin, marginal vs absorption costing.

============================ Snippet rules ===============================
- Max 15 lines, plain text — DO NOT wrap in markdown fences.
- For NetSuite Specific snippets, use SuiteQL.
- For pure-accounting topics, snippets should be journal entries / T-accounts /
  short worked calculations as plain text, NOT code.
- Inside JSON, escape newlines as \\n."""


EVAL_SYSTEM_PROMPT = """You are a strict senior data engineer evaluating a junior's code answer to an interview question.

Respond ONLY in this exact JSON format, nothing else:
{
  "logic_correct": true,
  "user_output": "compact representation of what the candidate's code would return when run on the sample data (max 8 lines)",
  "feedback": "specific, direct feedback on what is right or wrong (2-3 sentences)",
  "tip": "one practical interview tip relevant to this answer (1 sentence)"
}

Rules:
- logic_correct = true ONLY if the candidate's code, run against the sample data,
  would produce the same set of rows / value(s) as the canonical solution.
  Order doesn't matter unless the question explicitly requires ordering.
- Trivial syntax slips that wouldn't actually run (e.g. missing semicolons in SQL,
  obvious typos that any reviewer would mentally fix) → still mark TRUE if logic is correct,
  call them out in feedback.
- If the candidate's approach is technically valid but different from the canonical
  one, still mark TRUE if it produces the right output."""


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


def _parse_json_response(text: str) -> dict:
    text = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def _rebalance_mcq_position(q: dict) -> dict:
    """Enforce the rule: in any window of 4 consecutive MCQs the correct
    position B (index 1) must NOT appear more than once.

    We have the last `recent_correct_positions` (up to 4). For the NEW
    question, the relevant window is the last 3 sent positions + this one.
    If putting the correct answer at B would exceed 1 B in that window,
    swap option B with the least-used position.
    """
    if q.get("type") != "mcq":
        return q
    options = q.get("options")
    if not options or len(options) < 4 or "correct" not in q:
        return q

    cur = int(q["correct"])
    last_three = list(recent_correct_positions)[-3:]
    if cur == 1 and last_three.count(1) >= 1:
        counts = Counter(last_three)
        candidates = [i for i in range(len(options)) if i != 1]
        candidates.sort(key=lambda i: counts.get(i, 0))
        new_idx = candidates[0]
        opts = list(options)
        opts[cur], opts[new_idx] = opts[new_idx], opts[cur]
        q["options"] = opts
        q["correct"] = new_idx
        logger.info(
            "Rebalanced MCQ correct position from B(1) -> %s (recent=%s)",
            new_idx,
            last_three,
        )
    return q


def _decide_qtype(track: str, topic: str, force_type: Optional[str] = None) -> str:
    """Pick mcq vs coding for a given (track, topic).

    - DE: 40% MCQ, 60% coding (existing rule for all DE topics).
    - Accounting: only "NetSuite Specific" allows coding (40% MCQ / 60% coding,
      where coding is SuiteQL). All other accounting topics are 100% MCQ.
    """
    if force_type:
        return force_type
    if track == "Accounting":
        if topic in ACCOUNTING_CODING_TOPICS:
            return "mcq" if random.random() < MCQ_RATIO else "coding"
        return "mcq"
    return "mcq" if random.random() < MCQ_RATIO else "coding"


def _build_user_prompt(
    *,
    track: str,
    qtype: str,
    topic: str,
    difficulty: str,
    blocked_concepts: Optional[set] = None,
    recent_positions: Optional[list] = None,
    concept_focus: Optional[str] = None,
) -> str:
    blocked_concepts = blocked_concepts or set()
    recent_positions = recent_positions or []

    blocked_hint = ""
    if blocked_concepts:
        # Cap to a reasonable number to avoid blowing context.
        sample = list(blocked_concepts)[:60]
        blocked_hint = (
            "\n\nDO NOT use any of these recently-covered concept_keys "
            "(pick something different):\n" + ", ".join(sorted(sample))
        )

    positions_hint = ""
    if qtype == "mcq" and recent_positions:
        readable = [["A", "B", "C", "D"][p] if 0 <= p <= 3 else "?" for p in recent_positions]
        positions_hint = (
            "\n\nrecent_correct_positions (most recent last): "
            + ", ".join(readable)
            + ". Distribute the correct answer to a DIFFERENT position from these where natural — "
              "and never place it at B more than once in any window of 4."
        )

    concept_hint = ""
    if concept_focus:
        concept_hint = (
            f"\n\nThis question MUST test the concept_key: \"{concept_focus}\". "
            "Use that exact concept_key in your JSON output."
        )

    flavour = (
        "Make it practical and fintech-relevant."
        if track == "DE"
        else "Make it scenario-based with realistic numbers and counterparties."
    )
    return (
        f"Generate a {qtype.upper()} question. "
        f"Track: {track}. Topic: {topic}. Difficulty: {difficulty}. "
        f"{flavour}"
        f"{concept_hint}{blocked_hint}{positions_hint}"
    )


def generate_question(
    topic: Optional[str] = None,
    difficulty: str = "Intermediate",
    avoid_topic: Optional[str] = None,
    blocked_concepts: Optional[set] = None,
    recent_positions: Optional[list] = None,
    concept_focus: Optional[str] = None,
    force_type: Optional[str] = None,
    track: str = "DE",
) -> dict:
    """Synchronously call Anthropic and return a validated question dict.

    - `track` => "DE" or "Accounting". Picks the system prompt and topic pool.
    - `topic` None => random topic from the track's pool, different from
      `avoid_topic` where possible.
    - `blocked_concepts` => set of concept_keys to avoid; retry up to 3 times.
    - `recent_positions` => last few correct-answer indices for MCQs (hint).
    - `concept_focus` => force a specific concept_key (review path).
    - `force_type` => "mcq" or "coding" to override the type heuristic.
    """
    pool = ACCOUNTING_TOPICS if track == "Accounting" else DE_TOPICS
    if not topic:
        choices = [t for t in pool if t != avoid_topic] or pool
        topic = random.choice(choices)

    qtype = _decide_qtype(track, topic, force_type)
    blocked_concepts = blocked_concepts or set()
    recent_positions = recent_positions if recent_positions is not None else get_recent_mcq_positions()
    system_prompt = SYSTEM_PROMPT_ACCOUNTING if track == "Accounting" else SYSTEM_PROMPT_DE

    last_q: Optional[dict] = None
    for attempt in range(3):
        prompt = _build_user_prompt(
            track=track,
            qtype=qtype,
            topic=topic,
            difficulty=difficulty,
            blocked_concepts=blocked_concepts,
            recent_positions=recent_positions,
            concept_focus=concept_focus,
        )
        try:
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=2000,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            q = _parse_json_response(text)
        except Exception as exc:
            logger.warning("generate_question attempt %s failed: %s", attempt, exc)
            continue

        q.setdefault("topic", topic)
        q.setdefault("difficulty", difficulty)
        q["type"] = q.get("type") or qtype
        q["track"] = track
        q["snippet"] = _clean_snippet(q.get("snippet", ""))
        if q.get("type") == "coding":
            q["correct_solution"] = _clean_snippet(q.get("correct_solution", "")) or q["snippet"]
        last_q = q

        ck = q.get("concept_key")
        if not ck:
            logger.warning("generate_question: missing concept_key, retrying (attempt %s)", attempt)
            continue
        if ck in blocked_concepts and not concept_focus:
            logger.info("generate_question: blocked concept_key %r, retrying", ck)
            continue

        return q

    # Fall back to whatever the last attempt produced (may still be useful).
    if last_q is None:
        raise RuntimeError("generate_question: all 3 attempts failed")
    return last_q


def evaluate_coding_answer(question: dict, user_code: str) -> dict:
    """Send the candidate's free-text code answer to Claude for evaluation.

    Returns a dict: { logic_correct: bool, user_output: str, feedback: str, tip: str }.
    """
    user_prompt = (
        f"QUESTION:\n{question.get('question', '')}\n\n"
        f"LANGUAGE: {question.get('language', 'sql')}\n\n"
        f"SAMPLE DATA:\n{question.get('sample_data', '')}\n\n"
        f"CANONICAL SOLUTION:\n{question.get('correct_solution', '')}\n\n"
        f"EXPECTED OUTPUT (from canonical solution):\n{question.get('expected_output', '')}\n\n"
        f"CANDIDATE CODE:\n{user_code}\n"
    )
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=900,
        system=EVAL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = message.content[0].text.strip()
    parsed = _parse_json_response(text)
    return {
        "logic_correct": bool(parsed.get("logic_correct", False)),
        "user_output": str(parsed.get("user_output", "")).strip(),
        "feedback": str(parsed.get("feedback", "")).strip(),
        "tip": str(parsed.get("tip", "")).strip(),
    }


def format_question_message(q: dict, send_id) -> str:
    qtype = q.get("type", "mcq")
    track = q.get("track") or topic_to_track(q.get("topic", ""))
    track_label = "Accounting Quiz" if track == "Accounting" else "DE Quiz"
    topic = html.escape(str(q["topic"]))
    difficulty = html.escape(str(q["difficulty"]))
    question = html.escape(str(q["question"]))

    if qtype == "coding":
        lang = str(q.get("language", "sql")).lower()
        lang_label = lang.upper()
        sample_block = ""
        sample = q.get("sample_data", "")
        if sample:
            sample_block = f"<pre>{html.escape(str(sample))}</pre>\n\n"
        msg = (
            f"💻 <b>{track_label} — {topic}</b> <code>{difficulty}</code> <code>{lang_label}</code>\n\n"
            f"{question}\n\n"
            f"{sample_block}"
            f"<i>Reply with your {lang_label} code as a plain text message — no buttons.</i>\n"
            f"<i>ID: {send_id}</i>"
        )
        return msg

    icon = "📒" if track == "Accounting" else "📊"
    letters = ["A", "B", "C", "D"]
    options_text = "\n".join(
        f"{letters[i]}. {html.escape(str(opt))}" for i, opt in enumerate(q["options"])
    )
    msg = (
        f"{icon} <b>{track_label} — {topic}</b> <code>{difficulty}</code>\n\n"
        f"{question}\n\n"
        f"{options_text}\n\n"
        f"<i>Tap A, B, C, or D below.</i>\n"
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


def format_code_block(code: str, language: str) -> str:
    if not code:
        return ""
    safe = html.escape(code)
    lang = (language or "").lower()
    if lang in {"sql", "python"}:
        return f'<pre><code class="language-{lang}">{safe}</code></pre>'
    return f"<pre>{safe}</pre>"


class QuestionQueue:
    """In-memory async queue of pre-generated questions with topic-diversity
    and concept-key dedupe.

    - On startup, `topup(target_size)` is fired to fill the queue.
    - When a question is popped and the queue drops to <= min_size, a
      background refill of `refill_amount` questions is triggered.
    - Diversity: at generation time we avoid the just-added topic, and at
      pop time we prefer the first item whose topic differs from the
      just-served one.
    - Dedupe: blocked concept_keys are pulled from `blocked_provider`
      (injected by main.py) and stale items are dropped at pop time.
    """

    def __init__(
        self,
        target_size: int = 5,
        min_size: int = 2,
        refill_amount: int = 3,
        blocked_provider: Optional[Callable[[], set]] = None,
        track_provider: Optional[Callable[[], str]] = None,
    ):
        self.target_size = target_size
        self.min_size = min_size
        self.refill_amount = refill_amount
        self.blocked_provider = blocked_provider or (lambda: set())
        self.track_provider = track_provider or (lambda: "DE")
        self._queue: list = []
        self._last_added_topic: Optional[str] = None
        self._last_served_topic: Optional[str] = None
        self._lock = asyncio.Lock()
        self._topping_up = False

    def __len__(self) -> int:
        return len(self._queue)

    def _safe_blocked(self) -> set:
        try:
            return set(self.blocked_provider() or set())
        except Exception as exc:
            logger.warning("blocked_provider raised: %s", exc)
            return set()

    def _safe_track(self) -> str:
        try:
            t = self.track_provider() or "DE"
            return "Accounting" if t == "Accounting" else "DE"
        except Exception as exc:
            logger.warning("track_provider raised: %s", exc)
            return "DE"

    async def topup(self, n: Optional[int] = None) -> None:
        """Generate up to `n` questions and append them, avoiding consecutive
        same topics and concept-keys we've recently covered. The track for
        each generated item is sampled fresh from `track_provider`, so the
        queue mix follows the user's current mode."""
        n = n if n is not None else self.refill_amount
        for _ in range(n):
            blocked = self._safe_blocked()
            track = self._safe_track()
            try:
                q = await asyncio.to_thread(
                    generate_question,
                    None,
                    "Intermediate",
                    self._last_added_topic,
                    blocked,
                    get_recent_mcq_positions(),
                    None,
                    None,
                    track,
                )
            except Exception as exc:
                logger.warning("Queue topup: failed to generate question: %s", exc)
                continue
            self._queue.append(q)
            self._last_added_topic = q.get("topic")
            logger.info(
                "Queue topup: added track=%s topic=%s type=%s concept=%s (size=%d)",
                q.get("track"),
                q.get("topic"),
                q.get("type"),
                q.get("concept_key"),
                len(self._queue),
            )

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

    async def pop(self, desired_track: Optional[str] = None) -> dict:
        """Return one question matching `desired_track` if specified.

        - Drops queue items whose concept_key is now blocked.
        - Prefers a queue item whose track matches `desired_track` AND whose
          topic differs from the just-served one.
        - Falls back to ANY item with the desired track.
        - If nothing in the queue matches, generates one inline using the
          desired track (or `track_provider()` if None).
        """
        async with self._lock:
            blocked = self._safe_blocked()
            if blocked:
                before = len(self._queue)
                self._queue = [
                    item for item in self._queue
                    if item.get("concept_key") not in blocked
                ]
                if len(self._queue) != before:
                    logger.info(
                        "Queue: dropped %d stale-concept items (size=%d)",
                        before - len(self._queue),
                        len(self._queue),
                    )

            picked: Optional[dict] = None
            if self._queue and desired_track:
                # 1st pass: matching track AND different topic from last served.
                for i, item in enumerate(self._queue):
                    if (
                        item.get("track") == desired_track
                        and item.get("topic") != self._last_served_topic
                    ):
                        picked = self._queue.pop(i)
                        break
                # 2nd pass: any item with matching track.
                if picked is None:
                    for i, item in enumerate(self._queue):
                        if item.get("track") == desired_track:
                            picked = self._queue.pop(i)
                            break
            elif self._queue:
                # No track preference — preserve old behaviour.
                pick_idx = 0
                if self._last_served_topic:
                    for i, item in enumerate(self._queue):
                        if item.get("topic") != self._last_served_topic:
                            pick_idx = i
                            break
                picked = self._queue.pop(pick_idx)

            if picked is None:
                track = desired_track or self._safe_track()
                logger.info(
                    "Queue: no match for desired_track=%s (size=%d) -> generating inline",
                    desired_track,
                    len(self._queue),
                )
                picked = await asyncio.to_thread(
                    generate_question,
                    None,
                    "Intermediate",
                    self._last_served_topic,
                    blocked,
                    get_recent_mcq_positions(),
                    None,
                    None,
                    track,
                )

            self._last_served_topic = picked.get("topic")
            self._maybe_trigger_refill()
            return picked


question_queue = QuestionQueue()
