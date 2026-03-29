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
