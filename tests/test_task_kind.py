"""What kind of work a task is, read from its words alone (no model call)."""

import typing

import pytest

from app.schemas.workspace import TaskWorkspace
from app.services.analytical.task_kind import TaskKind, classify_task, is_programming_topic


def test_classify_task__write_the_readme__write():
    assert classify_task("Course project: write the README", [], None) is TaskKind.WRITE


def test_classify_task__dp_practice_knapsack__practice():
    assert classify_task("DP practice: knapsack and its variants", [], None) is TaskKind.PRACTICE


def test_classify_task__submit_the_quiz_answers__admin():
    assert classify_task("Submit the quiz answers", [], None) is TaskKind.ADMIN


def test_classify_task__page_replacement_simulator__build():
    assert classify_task("Page replacement simulator", ["operating systems"], None) is TaskKind.BUILD


def test_classify_task__unknown_wording__study():
    assert classify_task("Operating systems, unit 2", [], None) is TaskKind.STUDY


@pytest.mark.parametrize("title, kind", [
    ("Write and submit the lab report", TaskKind.WRITE),  # the writing is the work
    ("Revise DBMS normalisation", TaskKind.STUDY),
    ("Make flashcards for the cell cycle", TaskKind.STUDY),
    ("Draft the essay introduction", TaskKind.WRITE),
    ("Write a Python script to parse the logs", TaskKind.BUILD),  # code, not prose
    ("Implement the LRU cache", TaskKind.BUILD),
    ("Solve 5 graph problems", TaskKind.PRACTICE),
    ("Mock interview: system design", TaskKind.PRACTICE),
    ("Pay the exam fee", TaskKind.ADMIN),
    ("Register for the hackathon", TaskKind.ADMIN),
    ("Email the professor about the extension", TaskKind.ADMIN),
    ("", TaskKind.STUDY),
])
def test_classify_task__common_titles__their_kind(title, kind):
    assert classify_task(title, [], None) is kind


def test_classify_task__no_cue_in_the_title__keywords_and_goal_decide():
    assert classify_task("Chapter 2", [], "write_my_thesis") is TaskKind.WRITE
    assert classify_task("Knapsack variants", ["practice problems"], None) is TaskKind.PRACTICE


def test_classify_task__cue_in_the_title__outranks_the_keywords():
    assert classify_task("Revise chapter 2", ["report"], "write_my_thesis") is TaskKind.STUDY


def test_classify_task__word_inside_another_word__not_a_cue():
    assert classify_task("Repayment plans in microfinance", [], None) is TaskKind.STUDY  # not "pay"


def test_is_programming_topic__dsa_task__true():
    assert is_programming_topic("DP practice: knapsack and its variants", [], None)
    assert is_programming_topic("Practice problems", ["dynamic programming"], None)
    assert is_programming_topic("Mock interview prep", [], "crack_dsa_interviews")


def test_is_programming_topic__chemistry_practice__false():
    assert not is_programming_topic("Practice organic chemistry reaction mechanisms", [], None)


def test_task_kind__values__are_the_workspace_schemas_choices():
    choices = typing.get_args(TaskWorkspace.model_fields["task_kind"].annotation)

    assert sorted(choices) == sorted(kind.value for kind in TaskKind)
    assert TaskWorkspace.model_fields["task_kind"].default == TaskKind.STUDY.value
