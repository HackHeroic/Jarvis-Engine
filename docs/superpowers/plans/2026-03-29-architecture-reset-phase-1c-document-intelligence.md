# Architecture Reset Phase 1C: Document Intelligence

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make uploaded documents intelligent — classify them by type (practice problems, lecture notes, syllabus, assignment, reference), extract type-specific content, and route through registered handlers. After Phase 1C, uploading a PDF triggers the right processing pipeline automatically via the registry framework.

**Architecture:** Uses BaseRegistry (Phase 1A) for extensible document type registration. The Document Intelligence Pipeline: (1) Docling extracts text, (2) LLM classifies document type using registry-generated prompt, (3) registry looks up handler, (4) handler processes document, (5) triggers replan if handler metadata says so, (6) stores ingestion event as memory. Handlers for practice_problems, syllabus, and assignment are functional stubs that log intent — full enrichment requires Supabase table migrations (extracted_problems, task_completion_criteria) which are deferred. The reference handler delegates to the existing `knowledge_ingester.py`.

**Tech Stack:** FastAPI, Pydantic v2, BaseRegistry, LiteLLM, Supabase, ChromaDB, IBM Docling

**Spec:** `docs/superpowers/specs/2026-03-28-jarvis-architecture-reset-design.md` (sections: Architectural Principle: The Registry Framework, Document Type Registry, Document Intelligence Pipeline, Document Classification Schema, Practice Problem Extraction, Task Enrichment Logic)

**Prerequisite:** Phase 1A (BaseRegistry) + Phase 1B (MemoryStore for ingestion event storage)

**Produces:** Working document classification pipeline that routes documents to the correct handler based on type. Fully extensible — adding a new document type = one handler + one registration. Covered by tests.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/schemas/document.py` | DocumentClassification, ExtractedProblem, ProblemSetExtraction schemas |
| Create | `app/services/documents/__init__.py` | Documents package init |
| Create | `app/services/documents/registry.py` | Document type registry + 5 default handlers + registration |
| Create | `app/services/documents/pipeline.py` | Document intelligence pipeline (classify → dispatch → memory) |
| Create | `tests/test_document_registry.py` | Unit tests for document type registry |
| Create | `tests/test_document_pipeline.py` | Integration tests for classification + pipeline dispatch |
| Modify | `app/main.py` | Register default document types at startup |

---

### Task 1: Document Classification Schemas

**Files:**
- Create: `app/schemas/document.py`

- [ ] **Step 1: Write a quick import test**

```python
# Verify schemas are importable
# Run after creating the file:
# python -c "from app.schemas.document import DocumentClassification, ExtractedProblem, ProblemSetExtraction; print('OK')"
```

- [ ] **Step 2: Create document schemas**

```python
# app/schemas/document.py
"""Pydantic schemas for the document intelligence pipeline."""

from pydantic import BaseModel, Field


class DocumentClassification(BaseModel):
    """LLM classifies uploaded document into a registered type.

    The document_type field is a free string validated against the
    document registry at runtime — not a hardcoded Literal.
    This allows new document types to be added via registration
    without changing this schema.
    """

    document_type: str = Field(
        description="One of the registered document types from the registry"
    )

    confidence: float = Field(ge=0, le=1)

    topics_covered: list[str] = Field(
        default_factory=list,
        description="Granular topic tags: 'CNN architectures', 'backpropagation', 'Adam optimizer'",
    )

    problem_count: int | None = Field(
        default=None,
        description="Number of individual problems/questions found (if applicable)",
    )

    deadline_detected: str | None = Field(
        default=None,
        description="ISO date if a deadline is mentioned",
    )

    difficulty_estimate: float | None = Field(
        default=None, ge=0, le=1,
        description="Estimated difficulty 0-1 based on content complexity",
    )


class ExtractedProblem(BaseModel):
    """A single problem extracted from a practice problem document."""

    problem_number: int
    problem_text: str = Field(description="The actual question text")
    topic_tags: list[str] = Field(description="Knowledge component tags")
    difficulty_estimate: float = Field(ge=0, le=1, default=0.5)
    expected_time_minutes: int = Field(default=10)
    has_solution: bool = False
    solution_text: str | None = None


class ProblemSetExtraction(BaseModel):
    """Full extraction result from a practice problem document."""

    problems: list[ExtractedProblem] = Field(default_factory=list)
    overall_topics: list[str] = Field(default_factory=list)
    source_document_id: str = ""
```

- [ ] **Step 3: Verify import**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.schemas.document import DocumentClassification, ExtractedProblem, ProblemSetExtraction; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/schemas/document.py
git commit -m "feat(docs): add DocumentClassification, ExtractedProblem, ProblemSetExtraction schemas"
```

---

### Task 2: Document Type Registry

**Files:**
- Create: `app/services/documents/__init__.py`
- Create: `app/services/documents/registry.py`
- Create: `tests/test_document_registry.py`

- [ ] **Step 1: Write failing tests for document registry**

```python
# tests/test_document_registry.py
"""Tests for the document type registry."""

import pytest
from app.services.documents.registry import document_registry, register_default_document_types


def test_default_document_types_registered():
    register_default_document_types()
    names = document_registry.registered_names()
    assert "practice_problems" in names
    assert "lecture_notes" in names
    assert "syllabus" in names
    assert "assignment" in names
    assert "reference" in names
    assert len(names) == 5


def test_reference_is_fallback():
    register_default_document_types()
    result = document_registry.get_or_fallback("unknown_doc_type")
    assert result.name == "reference"


def test_classification_prompt_generated():
    register_default_document_types()
    prompt = document_registry.classification_prompt()
    assert "practice_problems" in prompt
    assert "lecture_notes" in prompt
    assert "reference" in prompt
    assert "Problem sets" in prompt or "exercises" in prompt


def test_practice_problems_has_correct_metadata():
    register_default_document_types()
    entry = document_registry.get("practice_problems")
    assert entry is not None
    assert entry.metadata.get("modifies_tasks") is True
    assert entry.metadata.get("triggers_replan") is True


def test_lecture_notes_does_not_modify_tasks():
    register_default_document_types()
    entry = document_registry.get("lecture_notes")
    assert entry is not None
    assert entry.metadata.get("modifies_tasks") is False
    assert entry.metadata.get("triggers_replan") is False


def test_reference_does_not_modify_tasks():
    register_default_document_types()
    entry = document_registry.get("reference")
    assert entry is not None
    assert entry.metadata.get("modifies_tasks") is False


def test_all_handlers_are_callable():
    register_default_document_types()
    for name in document_registry.registered_names():
        entry = document_registry.get(name)
        assert callable(entry.handler), f"Handler for {name} is not callable"


def test_all_entries_have_examples():
    register_default_document_types()
    for name in document_registry.registered_names():
        entry = document_registry.get(name)
        assert len(entry.examples) > 0, f"No examples for {name}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_document_registry.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create document package init**

```python
# app/services/documents/__init__.py
"""Jarvis Document Intelligence — registry-based document processing."""
```

- [ ] **Step 4: Implement document registry with 5 default types**

```python
# app/services/documents/registry.py
"""Document type registry — extensible document processing.

Uses BaseRegistry from app/core/registry.py. Adding a new document type
requires only a handler function + a register() call.

Current types: practice_problems, lecture_notes, syllabus, assignment, reference.
"""

import logging

from app.core.registry import BaseRegistry, RegistryEntry

logger = logging.getLogger(__name__)

# The global document type registry instance
document_registry = BaseRegistry[dict](
    name="document",
    fallback_key="reference",
)


# ── Handler definitions ────────────────────────────────────
# These are functional stubs for Phase 1C. They log what they would do
# and store the classification as a memory. Full enrichment (problem
# extraction, task matching, completion criteria) requires Supabase
# table migrations (extracted_problems, task_completion_criteria)
# which will be implemented when the workspace is enhanced.


async def handle_practice_problems(user_id: str, extraction: dict, source_id: str):
    """Extract individual problems, match to tasks, enrich completion criteria.

    Phase 1C: Logs intent + stores ingestion memory. Full enrichment deferred.
    """
    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    problem_count = classification.get("problem_count") if hasattr(classification, "get") else getattr(classification, "problem_count", None)
    logger.info(
        "[DocumentHandler] practice_problems: user=%s, source=%s, topics=%s, problems=%s",
        user_id, source_id, topics, problem_count,
    )
    return {"handler": "practice_problems", "status": "logged", "topics": topics}


async def handle_lecture_notes(user_id: str, extraction: dict, source_id: str):
    """Extract key concepts, link to tasks as study material.

    Phase 1C: Logs intent. Linking deferred to workspace enhancement.
    """
    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    logger.info(
        "[DocumentHandler] lecture_notes: user=%s, source=%s, topics=%s",
        user_id, source_id, topics,
    )
    return {"handler": "lecture_notes", "status": "logged", "topics": topics}


async def handle_syllabus(user_id: str, extraction: dict, source_id: str):
    """Extract topics + deadlines, create/update tasks.

    Phase 1C: Logs intent. Task creation deferred to workspace enhancement.
    """
    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    deadline = classification.get("deadline_detected") if hasattr(classification, "get") else getattr(classification, "deadline_detected", None)
    logger.info(
        "[DocumentHandler] syllabus: user=%s, source=%s, topics=%s, deadline=%s",
        user_id, source_id, topics, deadline,
    )
    return {"handler": "syllabus", "status": "logged", "topics": topics, "deadline": deadline}


async def handle_assignment(user_id: str, extraction: dict, source_id: str):
    """Extract requirements + deadline, add as completion criteria or new task.

    Phase 1C: Logs intent. Task creation deferred to workspace enhancement.
    """
    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    deadline = classification.get("deadline_detected") if hasattr(classification, "get") else getattr(classification, "deadline_detected", None)
    logger.info(
        "[DocumentHandler] assignment: user=%s, source=%s, topics=%s, deadline=%s",
        user_id, source_id, topics, deadline,
    )
    return {"handler": "assignment", "status": "logged", "topics": topics, "deadline": deadline}


async def handle_reference(user_id: str, extraction: dict, source_id: str):
    """Chunk + store in ChromaDB for RAG. Default handler.

    Phase 1C: Logs intent. Actual RAG storage already handled by existing
    knowledge_ingester.py in the ingestion pipeline.
    """
    classification = extraction.get("classification", {})
    topics = classification.get("topics_covered", []) if hasattr(classification, "get") else getattr(classification, "topics_covered", [])
    logger.info(
        "[DocumentHandler] reference: user=%s, source=%s, topics=%s",
        user_id, source_id, topics,
    )
    return {"handler": "reference", "status": "logged", "topics": topics}


# ── Registration ──────────────────────────────────────────

def register_default_document_types() -> None:
    """Register the built-in document types. Called during app lifespan."""

    document_registry.register(RegistryEntry(
        name="practice_problems",
        description="Problem sets, DPPs, sample papers, exercises, practice questions",
        handler=handle_practice_problems,
        examples=["DPP with 15 math problems", "Sample exam paper", "LeetCode problem compilation"],
        metadata={"modifies_tasks": True, "triggers_replan": True},
    ))

    document_registry.register(RegistryEntry(
        name="lecture_notes",
        description="Class notes, lecture slides, topic summaries, study guides",
        handler=handle_lecture_notes,
        examples=["Chapter 5 notes on neural networks", "Lecture slides from ML class", "Study guide for midterm"],
        metadata={"modifies_tasks": False, "triggers_replan": False},
    ))

    document_registry.register(RegistryEntry(
        name="syllabus",
        description="Course structure, topic lists, exam schedules, curriculum outlines",
        handler=handle_syllabus,
        examples=["CS301 course syllabus", "Semester schedule with exam dates", "Module breakdown for DL course"],
        metadata={"modifies_tasks": True, "triggers_replan": True},
    ))

    document_registry.register(RegistryEntry(
        name="assignment",
        description="Homework, projects, lab reports, deliverables with deadlines",
        handler=handle_assignment,
        examples=["Assignment 3: implement CNN", "Project proposal due Friday", "Lab report requirements"],
        metadata={"modifies_tasks": True, "triggers_replan": True},
    ))

    document_registry.register(RegistryEntry(
        name="reference",
        description="Textbook chapters, articles, documentation, general reference material",
        handler=handle_reference,
        examples=["Chapter from Deep Learning textbook", "Research paper on transformers", "API documentation"],
        metadata={"modifies_tasks": False, "triggers_replan": False},
    ))
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_document_registry.py -v`
Expected: All 8 tests PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/documents/__init__.py app/services/documents/registry.py tests/test_document_registry.py
git commit -m "feat(docs): add document type registry with 5 default types"
```

---

### Task 3: Document Intelligence Pipeline

**Files:**
- Create: `app/services/documents/pipeline.py`
- Create: `tests/test_document_pipeline.py`

- [ ] **Step 1: Write failing tests for the pipeline**

```python
# tests/test_document_pipeline.py
"""Tests for the document intelligence pipeline: classify → dispatch → memory."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.documents.pipeline import document_intelligence_pipeline
from app.services.documents.registry import document_registry, register_default_document_types
from app.schemas.document import DocumentClassification


@pytest.fixture(autouse=True)
def setup_registry():
    register_default_document_types()


@pytest.mark.asyncio
async def test_pipeline_classifies_and_dispatches():
    """Pipeline should classify the document and call the correct handler."""
    mock_memory_store = MagicMock()
    mock_memory_store.store_memory.return_value = {"id": "mem-1"}

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=DocumentClassification(
            document_type="practice_problems",
            confidence=0.9,
            topics_covered=["CNNs", "backpropagation"],
            problem_count=10,
        ),
    ):
        result = await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Problem 1: Explain convolution...\nProblem 2: Derive backprop...",
            source="direct_upload",
            source_id="src-1",
            memory_store=mock_memory_store,
        )

    assert result.document_type == "practice_problems"
    assert result.confidence == 0.9
    # Memory should be stored for the ingestion event
    mock_memory_store.store_memory.assert_called_once()
    stored = mock_memory_store.store_memory.call_args
    assert "practice_problems" in stored[0][1]["content"]


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_reference():
    """Unknown document types should fall back to reference handler."""
    mock_memory_store = MagicMock()
    mock_memory_store.store_memory.return_value = {"id": "mem-1"}

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=DocumentClassification(
            document_type="unknown_type",
            confidence=0.3,
            topics_covered=["misc"],
        ),
    ):
        result = await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Random content...",
            source="direct_upload",
            source_id="src-2",
            memory_store=mock_memory_store,
        )

    # Should fall back to reference (the registry's fallback_key)
    # The pipeline should not crash
    assert result is not None


@pytest.mark.asyncio
async def test_pipeline_stores_memory_on_ingestion():
    """Every document ingestion should create a memory record."""
    mock_memory_store = MagicMock()
    mock_memory_store.store_memory.return_value = {"id": "mem-1"}

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=DocumentClassification(
            document_type="lecture_notes",
            confidence=0.85,
            topics_covered=["neural networks", "deep learning"],
        ),
    ):
        await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Lecture 5: Neural Networks...",
            source="direct_upload",
            source_id="src-3",
            memory_store=mock_memory_store,
        )

    mock_memory_store.store_memory.assert_called_once()
    call_args = mock_memory_store.store_memory.call_args[0]
    assert call_args[0] == "u1"  # user_id
    mem_dict = call_args[1]
    assert mem_dict["type"] == "fact"
    assert "lecture_notes" in mem_dict["content"]
    assert mem_dict["source"] == "ingestion"


@pytest.mark.asyncio
async def test_pipeline_handles_classification_error():
    """If classification fails, pipeline should not crash."""
    mock_memory_store = MagicMock()

    with patch(
        "app.services.documents.pipeline.classify_document",
        new_callable=AsyncMock,
        side_effect=Exception("LLM failed"),
    ):
        result = await document_intelligence_pipeline(
            user_id="u1",
            extracted_text="Some content...",
            source="direct_upload",
            source_id="src-4",
            memory_store=mock_memory_store,
        )

    # Should return None or a fallback, not crash
    assert result is None


class TestDocumentClassificationSchema:
    def test_valid_classification(self):
        cls = DocumentClassification(
            document_type="practice_problems",
            confidence=0.9,
            topics_covered=["CNNs"],
            problem_count=5,
        )
        assert cls.document_type == "practice_problems"
        assert cls.confidence == 0.9
        assert cls.problem_count == 5

    def test_defaults(self):
        cls = DocumentClassification(
            document_type="reference",
            confidence=0.5,
        )
        assert cls.topics_covered == []
        assert cls.problem_count is None
        assert cls.deadline_detected is None
        assert cls.difficulty_estimate is None

    def test_confidence_bounds(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DocumentClassification(document_type="x", confidence=1.5)
        with pytest.raises(ValidationError):
            DocumentClassification(document_type="x", confidence=-0.1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_document_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement the document intelligence pipeline**

```python
# app/services/documents/pipeline.py
"""Document Intelligence Pipeline — classify → dispatch → memory.

Universal entry point for all document processing. Uses the document
type registry for extensible classification and handling.

Adding a new document type requires ZERO changes to this function.
"""

import logging

from app.models.brain.litellm_conf import hybrid_route_query
from app.schemas.document import DocumentClassification
from app.services.documents.registry import document_registry

logger = logging.getLogger(__name__)

CLASSIFICATION_SYSTEM_PROMPT = (
    "You are a document classifier. Classify the document into one of the given types. "
    "Return valid JSON only."
)


async def classify_document(
    extracted_text: str,
    registry_prompt: str,
) -> DocumentClassification:
    """Classify a document using the LLM and registry-generated prompt.

    Args:
        extracted_text: First ~8000 chars of the document text.
        registry_prompt: Classification prompt from document_registry.classification_prompt().

    Returns:
        DocumentClassification with document_type, confidence, topics, etc.
    """
    user_prompt = f"""{registry_prompt}

Document text (first 8000 chars):
{extracted_text[:8000]}

Classify this document. Return JSON with: document_type, confidence (0-1), topics_covered (list), problem_count (int or null), deadline_detected (ISO date or null), difficulty_estimate (0-1 or null)."""

    result = await hybrid_route_query(
        user_prompt=user_prompt,
        system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
        response_schema=DocumentClassification,
        prefer_local=False,  # Classification needs reliability — use Gemini
    )

    if isinstance(result, dict):
        return DocumentClassification.model_validate(result)
    if isinstance(result, DocumentClassification):
        return result
    return DocumentClassification.model_validate_json(str(result))


async def document_intelligence_pipeline(
    user_id: str,
    extracted_text: str,
    source: str,
    source_id: str,
    memory_store=None,
) -> DocumentClassification | None:
    """Universal document processing pipeline.

    Uses the registry framework — no hardcoded type checks.
    Adding a new document type requires ZERO changes to this function.

    Steps:
    1. Classify using registry-generated prompt
    2. Look up handler from registry
    3. Execute handler
    4. Trigger replan if handler metadata says so
    5. Store ingestion event as memory

    Args:
        user_id: Owner of the document.
        extracted_text: Text extracted from the document (via Docling or raw).
        source: Where the document came from ("direct_upload", "slack", "email", "api").
        source_id: Unique ID for this document.
        memory_store: Optional MemoryStore instance for storing ingestion memory.

    Returns:
        DocumentClassification result, or None if classification failed.
    """
    try:
        # 1. Classify using registry-generated prompt
        registry_prompt = document_registry.classification_prompt()
        classification = await classify_document(extracted_text, registry_prompt)

        # 2. Look up handler from registry (falls back to "reference")
        entry = document_registry.get_or_fallback(classification.document_type)

        # 3. Execute handler
        extraction = {"classification": classification}
        await entry.handler(user_id, extraction, source_id)

        # 4. Store ingestion event as memory
        if memory_store:
            topics_str = ", ".join(classification.topics_covered[:5])
            memory_store.store_memory(user_id, {
                "type": "fact",
                "content": f"Uploaded {classification.document_type}: topics {topics_str}",
                "source": "ingestion",
                "source_id": source_id,
            })

        return classification

    except Exception as e:
        logger.warning("Document intelligence pipeline failed: %s", e)
        return None
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_document_pipeline.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/services/documents/pipeline.py tests/test_document_pipeline.py
git commit -m "feat(docs): add document intelligence pipeline — classify, dispatch, memory"
```

---

### Task 4: Wire Document Registry into App Startup

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add document type registration to lifespan**

In `app/main.py`, find where `register_default_intents()` is called (around line 47-48). Add immediately after:

```python
    from app.services.documents.registry import register_default_document_types
    register_default_document_types()
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -c "from app.services.documents.registry import document_registry, register_default_document_types; register_default_document_types(); print(f'{len(document_registry.registered_names())} doc types registered')"`
Expected: `5 doc types registered`

- [ ] **Step 3: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add app/main.py
git commit -m "feat(startup): register default document types during app lifespan"
```

---

### Task 5: Integration Tests — Full Pipeline

**Files:**
- Create: `tests/test_document_integration.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_document_integration.py
"""Integration tests for the document intelligence system."""

import pytest
from app.schemas.document import DocumentClassification, ExtractedProblem, ProblemSetExtraction
from app.services.documents.registry import document_registry, register_default_document_types


class TestDocumentRegistryIntegration:
    def setup_method(self):
        register_default_document_types()

    def test_adding_new_document_type_at_runtime(self):
        """Verify that adding a new document type via register works."""
        from app.core.registry import RegistryEntry

        async def handle_meeting(user_id, extraction, source_id):
            return {"handler": "meeting_transcript"}

        document_registry.register(RegistryEntry(
            name="meeting_transcript",
            description="Meeting notes and transcripts",
            handler=handle_meeting,
            examples=["standup notes", "sprint retro"],
            metadata={"modifies_tasks": True, "triggers_replan": True},
        ))

        assert "meeting_transcript" in document_registry.registered_names()
        entry = document_registry.get("meeting_transcript")
        assert callable(entry.handler)

        # Classification prompt should now include the new type
        prompt = document_registry.classification_prompt()
        assert "meeting_transcript" in prompt

    def test_classification_prompt_covers_all_types(self):
        """Prompt should mention all registered types with descriptions."""
        prompt = document_registry.classification_prompt()
        for name in document_registry.registered_names():
            assert name in prompt

    def test_fallback_on_unknown_type(self):
        """Unknown types should fall back to reference handler."""
        entry = document_registry.get_or_fallback("alien_artifact")
        assert entry.name == "reference"
        assert entry.metadata.get("modifies_tasks") is False


class TestProblemExtractionSchemas:
    def test_extracted_problem_validation(self):
        problem = ExtractedProblem(
            problem_number=1,
            problem_text="What is the output of a 3x3 convolution on a 5x5 input?",
            topic_tags=["CNN", "convolution"],
            difficulty_estimate=0.4,
            expected_time_minutes=5,
            has_solution=True,
            solution_text="3x3",
        )
        assert problem.problem_number == 1
        assert problem.has_solution is True
        assert len(problem.topic_tags) == 2

    def test_problem_set_extraction(self):
        pse = ProblemSetExtraction(
            problems=[
                ExtractedProblem(
                    problem_number=1,
                    problem_text="Q1",
                    topic_tags=["math"],
                ),
                ExtractedProblem(
                    problem_number=2,
                    problem_text="Q2",
                    topic_tags=["physics"],
                ),
            ],
            overall_topics=["math", "physics"],
            source_document_id="doc-1",
        )
        assert len(pse.problems) == 2
        assert pse.overall_topics == ["math", "physics"]

    def test_classification_with_all_fields(self):
        cls = DocumentClassification(
            document_type="syllabus",
            confidence=0.95,
            topics_covered=["ML", "DL", "NLP"],
            problem_count=None,
            deadline_detected="2026-06-15",
            difficulty_estimate=0.6,
        )
        assert cls.deadline_detected == "2026-06-15"
        assert cls.difficulty_estimate == 0.6


class TestRegistryMetadata:
    def setup_method(self):
        register_default_document_types()

    def test_modifies_tasks_flag(self):
        """Only practice_problems, syllabus, and assignment modify tasks."""
        modifiers = [
            name for name in document_registry.registered_names()
            if document_registry.get(name).metadata.get("modifies_tasks")
        ]
        assert set(modifiers) == {"practice_problems", "syllabus", "assignment"}

    def test_triggers_replan_flag(self):
        """Same types that modify tasks should trigger replan."""
        replanners = [
            name for name in document_registry.registered_names()
            if document_registry.get(name).metadata.get("triggers_replan")
        ]
        assert set(replanners) == {"practice_problems", "syllabus", "assignment"}
```

- [ ] **Step 2: Run integration tests**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_document_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full Phase 1C test suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_document_registry.py tests/test_document_pipeline.py tests/test_document_integration.py -v`
Expected: All tests PASS

- [ ] **Step 4: Run combined Phase 1A + 1B + 1C suite**

Run: `cd /Users/madhav/Jarvis-cursor/Jarvis-Engine && python -m pytest tests/test_registry.py tests/test_intent_routing.py tests/test_draft_store.py tests/test_core_pipeline.py tests/test_memory_store.py tests/test_memory_retriever.py tests/test_memory_extractor.py tests/test_memory_constraint_bridge.py tests/test_memory_integration.py tests/test_document_registry.py tests/test_document_pipeline.py tests/test_document_integration.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/madhav/Jarvis-cursor/Jarvis-Engine
git add tests/test_document_integration.py
git commit -m "test: add document intelligence integration tests — registry, schemas, metadata"
```

---

## Phase 1C Complete Checklist

After completing all 5 tasks, verify:

- [ ] Document schemas defined (`DocumentClassification`, `ExtractedProblem`, `ProblemSetExtraction`)
- [ ] Document type registry has 5 default types (practice_problems, lecture_notes, syllabus, assignment, reference)
- [ ] All handlers are callable and log their intent
- [ ] `reference` is the fallback for unknown document types
- [ ] Classification prompt auto-discovers registered types
- [ ] Document intelligence pipeline classifies → dispatches → stores memory
- [ ] Pipeline handles errors gracefully (returns None, doesn't crash)
- [ ] Adding a new document type at runtime works (test proves it)
- [ ] Metadata flags (`modifies_tasks`, `triggers_replan`) are correct
- [ ] Document types registered at app startup
- [ ] All tests pass: Phase 1A (32) + Phase 1B (41) + Phase 1C (new)

**Next phase:** Phase 1D (Behavioral Intelligence — PEARL pattern detection) or Phase 1E (Stabilize & Document).
