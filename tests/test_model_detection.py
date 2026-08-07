"""Model selection: which loaded LM Studio model becomes primary vs fast.

Pure-function tests — no HTTP, no LM Studio, no mocking required.
"""

import app.core.config as config
from app.core.config import _select_models


def test_select__12b_recognized_as_primary():
    primary, fast = _select_models(["google/gemma-4-12b", "google/gemma-4-e4b"])
    assert primary == "google/gemma-4-12b"
    assert fast == "google/gemma-4-e4b"


def test_select__heavy_and_fast_split():
    primary, fast = _select_models(["google/gemma-4-26b-a4b", "google/gemma-4-e4b"])
    assert primary == "google/gemma-4-26b-a4b"
    assert fast == "google/gemma-4-e4b"


def test_select__single_model_serves_both_roles():
    primary, fast = _select_models(["google/gemma-4-12b"])
    assert primary == fast == "google/gemma-4-12b"


def test_select__qwen_only__used_as_last_resort():
    primary, fast = _select_models(["qwen/qwen-3.6-14b"])
    assert primary == "qwen/qwen-3.6-14b"


def test_select__gemma_beats_qwen():
    primary, _ = _select_models(["qwen/qwen-3.6-14b", "google/gemma-4-12b"])
    assert primary == "google/gemma-4-12b"


def test_select__nothing_loaded():
    assert _select_models([]) == (None, None)


def test_resolve__pinned_primary__survives_detection(monkeypatch):
    """An explicitly pinned GEMMA_PRIMARY_MODEL is never clobbered by startup detection.

    The unpinned fast slot still follows whatever LM Studio reports.
    """
    monkeypatch.setattr(config, "GEMMA_PRIMARY_MODEL", "openai/mistral/pinned-by-user")
    monkeypatch.setattr(config, "_PRIMARY_PINNED", True)
    monkeypatch.setattr(config, "_FAST_PINNED", False)

    primary, fast = config._resolve_models("google/gemma-4-12b", "google/gemma-4-e4b")

    assert primary == "openai/mistral/pinned-by-user"
    assert fast == "openai/google/gemma-4-e4b"
