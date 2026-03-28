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
        document_registry._entries.clear()
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
