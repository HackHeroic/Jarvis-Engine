"""What kind of work a task is, so its workspace can fit it. Pure: no I/O, no model.

A task is read from its words: the title first, then (when the title says nothing) its
topic keywords and its goal's id, a slug of the goal ("write_my_thesis"). Cue words are
matched as whole words or phrases, never inside another word.

The verbs say what the work is ("write", "solve", "submit"), so they are read before the
nouns ("README", "problems", "fee"), which decide only when no verb does. When a title
names several kinds of work, the substantive work wins over the errand around it:
"Write and submit the lab report" is writing. A writing verb about code ("write a
script") is building, unless the title also names a written piece.
"""

import re
from enum import Enum


class TaskKind(str, Enum):
    """The kinds of work a workspace is shaped for."""

    PRACTICE = "practice"  # solve problems
    STUDY = "study"  # revise, review, read, flashcards
    WRITE = "write"  # a report, README, essay, documentation
    BUILD = "build"  # implement, code a project or assignment
    ADMIN = "admin"  # submit, email, register, pay


def _cues(words: str) -> frozenset[str]:
    return frozenset(w.strip() for w in words.split(",") if w.strip())


# What the work is, said as a verb; highest priority first.
_VERBS: tuple[tuple[TaskKind, frozenset[str]], ...] = (
    (TaskKind.WRITE, _cues("write, writing, draft, drafting, rewrite, redraft, proofread")),
    (TaskKind.BUILD, _cues(
        "implement, implementing, build, building, code, code up, develop, debug, refactor,"
        " deploy, prototype"
    )),
    (TaskKind.PRACTICE, _cues("practice, practise, practicing, practising, solve, solving, drill")),
    (TaskKind.STUDY, _cues(
        "revise, review, study, read, reread, learn, memorise, memorize, recap, summarise,"
        " summarize, understand, go through, watch"
    )),
    (TaskKind.ADMIN, _cues(
        "submit, send, email, mail, register for, enrol, enroll, pay, apply for, upload,"
        " renew, fill in, fill out"
    )),
)

# What the work is about, said as a noun; read only when no verb says it.
_NOUNS: tuple[tuple[TaskKind, frozenset[str]], ...] = (
    (TaskKind.WRITE, _cues(
        "readme, report, reports, essay, essays, documentation, thesis, dissertation,"
        " cover letter, blog post, proposal, literature review, statement of purpose,"
        " resume, cv, memo"
    )),
    (TaskKind.BUILD, _cues(
        "simulator, simulation, prototype, app, website, api, script, parser, compiler,"
        " unit tests, frontend, backend"
    )),
    (TaskKind.PRACTICE, _cues(
        "problems, problem set, exercise, exercises, mock, mock interview, mock test,"
        " past paper, past papers, dpp, pyq, pyqs, leetcode, codeforces, worksheet, quiz"
    )),
    (TaskKind.ADMIN, _cues("fee, fees, registration, application form, deadline extension")),
    (TaskKind.STUDY, _cues("flashcards, flash cards, notes, lecture, lectures, revision")),
)

_WRITTEN_PIECES = dict(_NOUNS)[TaskKind.WRITE]
_CODE_NOUNS = dict(_NOUNS)[TaskKind.BUILD] | _cues("code, program, function, tests")

# Words that say the topic is programming, where LeetCode and Codeforces have problems.
_PROGRAMMING_CUES = _cues(
    "dsa, algorithm, algorithms, data structure, data structures, leetcode, codeforces,"
    " hackerrank, competitive programming, coding, programming, dp, dynamic programming,"
    " knapsack, recursion, backtracking, greedy, graph, graphs, bfs, dfs, dijkstra, tree,"
    " trees, binary tree, bst, heap, heaps, linked list, linked lists, array, arrays,"
    " hashing, hash map, sorting, binary search, two pointers, sliding window,"
    " bit manipulation, python, java, c++, cpp, javascript, typescript, golang, sql"
)


def _words(text: str) -> str:
    """``text`` lower-cased with every run of other characters (but + and #) as one space,
    padded with a space each side, so a cue is found as `` cue ``."""
    return f" {re.sub(r'[^a-z0-9+#]+', ' ', text.lower()).strip()} "


def _has_any(words: str, cues: frozenset[str]) -> bool:
    return any(f" {cue} " in words for cue in cues)


def _first_kind(words: str, table: tuple[tuple[TaskKind, frozenset[str]], ...]) -> TaskKind | None:
    return next((kind for kind, cues in table if _has_any(words, cues)), None)


def _kind_of(words: str) -> TaskKind | None:
    kind = _first_kind(words, _VERBS)
    if kind is TaskKind.WRITE and _has_any(words, _CODE_NOUNS) and not _has_any(words, _WRITTEN_PIECES):
        return TaskKind.BUILD  # "write a script": code, not prose
    return kind or _first_kind(words, _NOUNS)


def _context(topic_keywords: list[str], goal_id: str | None) -> str:
    return _words(" ".join([*topic_keywords, (goal_id or "").replace("_", " ")]))


def classify_task(title: str, topic_keywords: list[str], goal_id: str | None) -> TaskKind:
    """The kind of work the task is: from its title's cues, else from its keywords and
    goal id, else STUDY."""
    return (
        _kind_of(_words(title or ""))
        or _kind_of(_context(topic_keywords or [], goal_id))
        or TaskKind.STUDY
    )


def is_programming_topic(title: str, topic_keywords: list[str], goal_id: str | None) -> bool:
    """Whether the task is about programming (data structures, algorithms, a language)."""
    words = _words(title or "") + _context(topic_keywords or [], goal_id)
    return _has_any(words, _PROGRAMMING_CUES)
