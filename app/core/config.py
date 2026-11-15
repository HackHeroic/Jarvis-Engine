"""Hardware settings (MPS/MLX), DB secrets, model auto-detection."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root; try multiple paths for robustness (reload/subprocess edge cases)
_project_root = Path(__file__).resolve().parent.parent.parent
_env_candidates = [
    _project_root / ".env",
    Path.cwd() / ".env",
]
for _env_path in _env_candidates:
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
        break

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

# LLM Routing (Level 9 - LiteLLM Hybrid Router)
# LM Studio: http://127.0.0.1:1234 — both models hit the same server; model name selects which
LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:1234/v1")

# Gemma 4 model defaults. At startup, detect_loaded_models() probes LM Studio and
# rewrites PRIMARY+FAST to whichever model is actually loaded (Gemma preferred, qwen
# as last resort). Single-model setups work transparently. Setting either env var
# pins that slot — detection will not overwrite it.
LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_PRIMARY_MODEL: str = os.getenv("GEMMA_PRIMARY_MODEL", "openai/google/gemma-4-26b-a4b")
GEMMA_FAST_MODEL: str = os.getenv("GEMMA_FAST_MODEL", "openai/google/gemma-4-e4b")
# An explicitly pinned model name outranks startup detection (design D7): the user
# chose it deliberately, so detect_loaded_models() must leave that slot alone.
# A non-empty value is the pin — captured here, the single env-var reader. An empty
# env var (GEMMA_PRIMARY_MODEL=) reads as unset, so detection still fills the slot.
_PRIMARY_PINNED: bool = bool(os.getenv("GEMMA_PRIMARY_MODEL"))
_FAST_PINNED: bool = bool(os.getenv("GEMMA_FAST_MODEL"))
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
_raw_gemini = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
GEMINI_MODEL: str = _raw_gemini if _raw_gemini.startswith("gemini/") else f"gemini/{_raw_gemini}"
GEMINI_PRIMARY: bool = os.getenv("GEMINI_PRIMARY", "false").lower() == "true"

# Claude cloud tier (app/infrastructure/llm/claude.py), tried before Gemini whenever a
# credential is set. An API key wins over an auth token when both are set.
# CLAUDE_PRIMARY skips the local model on every call, as GEMINI_PRIMARY does.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_AUTH_TOKEN: str = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
CLAUDE_PRIMARY: bool = os.getenv("CLAUDE_PRIMARY", "false").lower() == "true"
# The one place the Claude model id is named.
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
# Thinking effort per model role. Effort is the main lever on Claude's token spend:
# low for classification and short replies, medium for decomposition and research.
CLAUDE_EFFORT_BY_ROLE: dict[str, str] = {"fast": "low", "primary": "medium", "cloud": "medium"}

# Seconds before a language-model call is abandoned. Applied twice on purpose: as the
# transport's own timeout, and as a hard ceiling around the provider call. For a
# stream the ceiling bounds each wait for the next token, not the whole reply.
LLM_TIMEOUT_SECONDS: float = 60
LLM_STREAM_TIMEOUT_SECONDS: float = 120

# SLM for Semantic Router (Level 2 - fast intent detection)
# Uses Gemma fast model when available; falls back to primary at startup probe.
SLM_ROUTER_MODEL: str = os.getenv("SLM_ROUTER_MODEL", GEMMA_FAST_MODEL)
SLM_ROUTER_URL: str | None = os.getenv("SLM_ROUTER_URL")  # Optional; if unset, uses LOCAL_LLM_URL


# Capability-class markers, not pinned model ids — local model churn is ~quarterly
# (Gemma 4 12B beats the older 27B-class at ~8 GB), so match by size class instead.
_HEAVY_MARKERS = ("27b", "26b", "22b", "14b", "12b")
_FAST_MARKERS = ("e4b", "e2b", "-4b", "1b", "3b")


def _select_models(ids: list[str]) -> tuple[str | None, str | None]:
    """Pick (primary, fast) from loaded model ids.

    Gemma family preferred; non-Gemma (qwen) accepted as last resort.
    A lone model serves both roles.
    """
    def _pick(pool: list[str]) -> tuple[str | None, str | None]:
        heavy = next((i for i in pool if any(m in i for m in _HEAVY_MARKERS)), None)
        fast = next((i for i in pool if any(m in i for m in _FAST_MARKERS)), None)
        if heavy and fast:
            return heavy, fast
        if heavy:
            return heavy, heavy
        if fast:
            return fast, fast
        return (pool[0], pool[0]) if pool else (None, None)

    gemmas = [i for i in ids if "gemma" in i.lower()]
    if gemmas:
        return _pick(gemmas)
    others = [i for i in ids if "qwen" in i.lower()]
    return _pick(others)


def _normalize_model_id(model_id: str) -> str:
    """LM Studio returns plain ids (e.g. "google/gemma-4-26b-a4b"); LiteLLM needs "openai/"."""
    return model_id if model_id.startswith("openai/") else f"openai/{model_id}"


def _resolve_models(primary: str, fast: str) -> tuple[str, str]:
    """Apply env pins over the detected (primary, fast) ids.

    Precedence: env pin > detection. Each slot is pinned independently, so pinning
    only the primary still lets detection choose the fast model. Pinned values are
    normalized too — users pin the id LM Studio displays ("google/gemma-4-12b"),
    which LiteLLM cannot route without the "openai/" prefix.
    """
    return (
        _normalize_model_id(GEMMA_PRIMARY_MODEL if _PRIMARY_PINNED else primary),
        _normalize_model_id(GEMMA_FAST_MODEL if _FAST_PINNED else fast),
    )


def detect_loaded_models() -> dict:
    """Probe LM Studio /v1/models and pick the best loaded model for primary + fast.

    Called at app startup. Mutates module globals GEMMA_PRIMARY_MODEL / GEMMA_FAST_MODEL
    / SLM_ROUTER_MODEL / LOCAL_LLM_MODEL to whatever model(s) are actually loaded.

    Selection is delegated to _select_models (pure, testable) and env pins are applied
    by _resolve_models — a slot pinned via env keeps its value. If only one model is
    loaded, both roles point at it. If LM Studio is unreachable, leave defaults.
    """
    global GEMMA_PRIMARY_MODEL, GEMMA_FAST_MODEL, SLM_ROUTER_MODEL, LOCAL_LLM_MODEL
    try:
        import httpx
        url = LOCAL_LLM_URL.rstrip("/") + "/models"
        with httpx.Client(timeout=2.0) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json().get("data", [])
        ids = [m.get("id", "").lower() for m in data]
        primary, fast_model = _select_models(ids)
        if primary is None:
            return {"loaded": ids, "warning": "No usable model loaded in LM Studio"}
        GEMMA_PRIMARY_MODEL, GEMMA_FAST_MODEL = _resolve_models(primary, fast_model)
        SLM_ROUTER_MODEL = GEMMA_FAST_MODEL
        LOCAL_LLM_MODEL = GEMMA_PRIMARY_MODEL

        print(f"[Model Detect] primary={primary} fast={fast_model} (from {len(ids)} loaded)")
        if _PRIMARY_PINNED or _FAST_PINNED:
            print(
                f"[Model Detect] env pins override detection → "
                f"primary={GEMMA_PRIMARY_MODEL} fast={GEMMA_FAST_MODEL}"
            )

        return {
            "loaded": ids,
            "primary": GEMMA_PRIMARY_MODEL,
            "fast": GEMMA_FAST_MODEL,
            "single_model": primary == fast_model,
        }
    except Exception as e:
        return {"loaded": [], "error": str(e), "warning": "LM Studio unreachable; using defaults"}

# LiteLLM observability
LITELLM_VERBOSE: bool = os.getenv("LITELLM_VERBOSE", "false").lower() in ("1", "true", "yes")

# LangGraph checkpointer — one SQLite file, one thread per (user, session).
# Local-first: this is conversation working memory, never leaves the machine.
CHECKPOINT_DB_PATH: str = os.getenv("JARVIS_CHECKPOINT_DB", "data/checkpoints.sqlite")
# A daily sweep keeps this many newest checkpoints per thread (and sub-graph namespace)
# and deletes the rest: a turn resumes from the newest, and nothing reads the history.
CHECKPOINT_KEEP_PER_THREAD: int = 20

# Horizon for scheduling (minutes)
DEFAULT_HORIZON_MINUTES: int = 2880  # 48 hours
MAX_HORIZON_MINUTES: int = 43200  # 30 days (per PDF: month-long planning)
DAY_START_HOUR: int = 8  # Planning day starts at 8 AM local (habit translator convention)
# From this local hour on, a new plan starts on the next local day at DAY_START_HOUR:
# a plan made at 22:30 puts nothing that night.
EVENING_CUTOFF_HOUR: int = 22
# The zone a user's day runs in when their profile names none (IANA name). Minute 0
# of every plan is DAY_START_HOUR in the user's zone, never in UTC.
DEFAULT_TIMEZONE: str = os.getenv("JARVIS_DEFAULT_TIMEZONE", "Asia/Kolkata")

# The slot sweep (TaskService.sweep_passed_slots): a pending task becomes "missed" once
# its slot ended this many minutes ago with no outcome recorded, so a Complete clicked
# just after the slot still counts. The sweep looks back this many days of slots.
SLOT_GRACE_MINUTES: int = 30
SLOT_SWEEP_LOOKBACK_DAYS: int = 7

# Anticipation (app/services/anticipation): the in-process scheduler the lifespan starts.
# Every ANTICIPATION_TICK_SECONDS it runs each subsystem's sweeps that are due, then each
# user's proactive triggers. JARVIS_ANTICIPATION=false keeps it from starting.
ANTICIPATION_ENABLED: bool = os.getenv("JARVIS_ANTICIPATION", "true").lower() != "false"
ANTICIPATION_TICK_SECONDS: int = 60
# A manual run (POST /anticipation/run) always re-evaluates its trigger; a second manual
# run of the same trigger for the same user within this many seconds gets the first
# one's outcome instead, so repeated clicks cannot each spend a model call.
ANTICIPATION_MANUAL_COOLDOWN_SECONDS: int = 10
# The morning brief is due from the user's wake time for this many hours, on their clock.
MORNING_BRIEF_WINDOW_HOURS: int = 6

# The focus profile (app/services/energy.py) and the solver's energy term. Outcomes of
# the last FOCUS_WINDOW_DAYS count for the local hour of their slot; each hour's score
# is smoothed toward the heuristic prior with FOCUS_PRIOR_STRENGTH pseudo-outcomes. The
# profile is "learned" from MIN_FOCUS_OBSERVATIONS outcomes, and only a learned profile
# steers the solver: each task not due today leans toward high-focus hours with weight
# ENERGY_OBJECTIVE_WEIGHT x difficulty. JARVIS_ENERGY_OBJECTIVE=false turns the term off.
ENERGY_OBJECTIVE_ENABLED: bool = os.getenv("JARVIS_ENERGY_OBJECTIVE", "true").lower() != "false"
# Scaled to the solver's makespan weight (15 per minute): 15 x 60, so one focus-penalty
# point on the hardest task is worth an hour of plan length. Enough to carry a hard task
# from the morning to the evening peak of a day the plan already uses (a TMT cost of up
# to ~2,000); not enough to stretch the plan for it. Weights of 750-1,200 behave alike;
# from ~2,000 easy tasks drift into the peak too, and each day idles until the evening.
ENERGY_OBJECTIVE_WEIGHT: int = 900
FOCUS_WINDOW_DAYS: int = 28
FOCUS_PRIOR_STRENGTH: float = 3.0
MIN_FOCUS_OBSERVATIONS: int = 10

# CP-SAT wall-clock cap. An uncapped solve has no upper bound, and it sits on
# the request path — on the streaming plan flow that is an unbounded stall with
# every queued SSE frame stuck behind it. Capped, CP-SAT returns the best
# schedule it found (FEASIBLE) or UNKNOWN, which the caller already treats as
# INFEASIBLE: widen the horizon, cut scope. 10s is far above a normal solve
# (a 30-day horizon lands in well under a second) and short enough that a user
# watching the plan form is not left waiting. Raise it for pathological plans
# via the env var.
SOLVER_MAX_TIME_SECONDS: float = float(os.getenv("JARVIS_SOLVER_MAX_TIME_SECONDS", "10"))
# Inside that cap, the search stops once no better schedule has turned up for this many
# seconds. The best schedule usually arrives in a fraction of a second; the rest of the
# search goes to proving nothing beats it. The wait starts at the first schedule found,
# so a search that has found none yet still runs to the cap. It is wall-clock time, so
# a busy machine finds improvements more slowly: under heavy load (load average ~30), a
# 12-task, 3-day plan stopped short of its best in 8 of 20 solves at 0.75 s and 3 of 20
# at 1.0 s (each by at most 0.03%). 1.0 s costs ~0.25 s per solve over 0.75 s. 0 (or
# None) turns the idle stop off: the search then runs to a proof or the cap.
SOLVER_IDLE_STOP_SECONDS: float | None = float(
    os.getenv("JARVIS_SOLVER_IDLE_STOP_SECONDS", "1.0")
)

# Adaptive pacing (research-based; used when user_override not set)
PACING_SUSTAINABLE_MIN_PER_DAY: int = 90  # slack >= 10
PACING_MODERATE_MIN_PER_DAY: int = 120  # slack >= 5
PACING_STANDARD_MIN_PER_DAY: int = 180  # slack >= 3
PACING_MAX_DEEP_WORK_PER_DAY: int = 240  # upper bound 4h
PACING_COGNITIVE_LOAD_HIGH: float = 0.8  # factor when intrinsic_load >= 0.8
PACING_COGNITIVE_LOAD_MED: float = 0.9  # factor when intrinsic_load >= 0.7

# ChromaDB (L4 Vector DB) - Cloud or local
CHROMA_API_KEY: str | None = os.getenv("CHROMA_API_KEY")
CHROMA_TENANT: str | None = os.getenv("CHROMA_TENANT")
CHROMA_DATABASE: str = os.getenv("CHROMA_DATABASE", "Jarvis-Vector-Db")

# Document search (RAG) relevance cut-off, as cosine distance (1 - cosine
# similarity) under the collection's default embedder, all-MiniLM-L6-v2. Its
# unrelated sentence pairs sit around 0.1-0.3 similarity and on-topic ones at
# 0.4 and up, so 0.6 (similarity >= 0.4) keeps on-topic chunks and drops the
# noise floor. A miss only means the reply has no document context; a weak
# match pasted into the prompt misleads the model — so err strict.
RAG_MAX_COSINE_DISTANCE: float = float(os.getenv("JARVIS_RAG_MAX_COSINE_DISTANCE", "0.6"))

# A task's workspace shows material from a document NOT linked to the task only when a
# chunk is at least this close to the task (its title and topic keywords), as cosine
# similarity under the same embedder; the search cut-off is 1 minus this. Stricter than
# document search above (similarity >= 0.4): the workspace presents the excerpt as
# material for the task and builds its quiz on it, so no material beats loose material.
# A starting value, not yet measured on real documents: tune it on the demo data.
WORKSPACE_MATERIAL_MIN_SIMILARITY: float = float(
    os.getenv("JARVIS_WORKSPACE_MATERIAL_MIN_SIMILARITY", "0.45")
)

# Memory (MemoryService), as cosine similarity under the memory embedder. A fact said or
# noticed at least MEMORY_SUPPRESS_SIMILARITY close to one the user forgot or dismissed is
# not stored again; one at least MEMORY_DEDUPE_SIMILARITY close to an active or faded
# memory of the same type reinforces it instead of adding a copy. Below
# MEMORY_RETENTION_FLOOR strength a memory leaves recall, and the daily decay sweep marks
# it faded (a constraint excepted).
MEMORY_SUPPRESS_SIMILARITY: float = 0.85
MEMORY_DEDUPE_SIMILARITY: float = 0.85
MEMORY_RETENTION_FLOOR: float = 0.1
# Recall's token budgets (MemoryService.recall), estimated at 3.5 characters a token
# (CHARS_PER_TOKEN_ESTIMATE, low on purpose so newer tokenizers stay within them): the
# always-on profile (constraints, preferences, goals) and the memories relevant to the
# message, which count only from MEMORY_RECALL_MIN_SIMILARITY up. However many memories
# a user has, the prompt carries at most the two budgets' worth.
MEMORY_PROFILE_TOKENS: int = 200
MEMORY_RECALL_TOKENS: int = 300
MEMORY_RECALL_MIN_SIMILARITY: float = 0.3

# The largest chat turn the stream accepts (ChatStreamRequest): a message of at most
# MAX_CHAT_PROMPT_CHARS characters, and an upload that decodes to at most
# MAX_CHAT_UPLOAD_BYTES. A bigger request is a 422 before any work starts.
MAX_CHAT_PROMPT_CHARS: int = 8_000
MAX_CHAT_UPLOAD_BYTES: int = 10 * 1024 * 1024

# Conversations (ConversationMemoryService). The model sees the newest
# HISTORY_MAX_MESSAGES messages after the conversation's summary_through (its verbatim
# window), each cut to HISTORY_MAX_CHARS_PER_MESSAGE characters. Once more than
# COMPACT_AFTER_MESSAGES are past it, one FAST call folds all but the newest
# COMPACT_KEEP_RECENT into the summary; so between compactions the window only grows
# (8 to 18 messages), and the prefix it sends changes only when a summary moves it. A
# conversation idle for SESSION_IDLE_MINUTES gets the rest summarised too, by the
# session-summary sweep, for "since we last spoke"; its newest COMPACT_KEEP_RECENT
# messages stay in the window, word for word, for when the user comes back.
HISTORY_MAX_MESSAGES: int = 24
HISTORY_MAX_CHARS_PER_MESSAGE: int = 1500
COMPACT_AFTER_MESSAGES: int = 16
COMPACT_KEEP_RECENT: int = 8
SESSION_IDLE_MINUTES: int = 30
# One summary call reads the oldest unsummarized messages first, at most
# SUMMARY_PAGE_MESSAGES of them, and sends as many as fit SUMMARY_INPUT_MAX_CHARS of
# transcript. A backlog (a chat older than summaries, a long outage) is worked through
# a page per run, so every message eventually reaches a summary.
SUMMARY_PAGE_MESSAGES: int = 40
SUMMARY_INPUT_MAX_CHARS: int = 24_000
# After a summary call fails (or is refused), that conversation's next try waits this
# long: the turns stay as they were, and no turn pays for a call that keeps failing.
SUMMARY_RETRY_MINUTES: int = 15
# The sweep reads each user's recently active sessions from storage once every
# SESSION_SUMMARY_RESCAN_HOURS (sessions active in the last SESSION_SUMMARY_LOOKBACK_DAYS),
# to pick up conversations from before a restart; between those reads it knows from the
# turns this process served, and a tick with nothing due reads nothing.
SESSION_SUMMARY_RESCAN_HOURS: int = 24
SESSION_SUMMARY_LOOKBACK_DAYS: int = 7
# The context note's token budgets for the conversation summary (<earlier>) and "since
# we last spoke" (<since>), estimated like the memory budgets. A summary sentence that
# does not fit is left out, never cut mid-sentence.
CONVERSATION_SUMMARY_TOKENS: int = 450
SINCE_LAST_SPOKE_TOKENS: int = 250
# A summary sentence is never shown, stored or fed to the next summary when it, or any of
# its clauses, is at least this close (cosine similarity, memory embedder) to a memory
# the user forgot or dismissed. Calibrated on all-MiniLM-L6-v2 with clause splitting
# (10 forgotten facts, each repeated inside a run-on summary sentence, against 25
# summary sentences): the weakest true match scored 0.497, the next 0.719. Of the 240
# non-matching pairs, 5 scored 0.45 or more (top 0.497, a sister visiting against a
# girlfriend moving away) and none 0.50. Dropping an innocent sentence costs little and
# showing a forgotten fact breaks a promise, so the cut sits below the weakest true
# match. Unsplit, the same run-on sentences scored as low as 0.42.
SUMMARY_SUPPRESS_SIMILARITY: float = 0.45

# Uploaded files (DocumentFileStore): kept in this private Supabase Storage bucket, each
# at {user_id}/{source_id}. The bucket is created on first use, limited to the largest
# upload ingestion accepts (MAX_CHAT_UPLOAD_BYTES).
DOCUMENT_BUCKET: str = "jarvis-documents"
# A document is linked to a task when one of its phrases (a topic, or its file name's
# subject) is at least this close to the task's title (cosine similarity, the memory
# embedder all-MiniLM-L6-v2). Measured on 4 documents (an ML syllabus of ten topics, DBMS
# notes, OS notes, an academic transcript) against 48 task titles: 192 pairs, 20 of them
# a document that is material for the task. The weakest true match but one scored 0.557
# ("ML quiz prep: SVMs" against "Support vector machines"), the strongest unrelated pair
# but one 0.476 ("Decide which trees to plant in the garden" against "Decision trees"),
# so the cut sits between them. Two pairs fall on the wrong side of any single cut: "Train
# a small neural net in PyTorch" (0.478, missed) and "Data structures: binary trees"
# against "Decision trees" (0.610, linked). All the topics joined into one string, the
# old way, scored at most 0.528 for a true match: none reached the old cut of 0.65.
DOCUMENT_LINK_SIMILARITY: float = 0.52


def is_feature_enabled(flag: str) -> bool:
    """Check if a feature flag is enabled. Runtime check via env vars.

    Convention: JARVIS_{FLAG_NAME} env var. Default: enabled ("1").
    Set to "0" to disable.
    """
    return os.environ.get(f"JARVIS_{flag}", "1") == "1"
