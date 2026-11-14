# tests/test_memory_integration.py
"""The memory schemas, and the time ranges the constraint bridge reads out of a memory."""

from app.services.memory.constraint_bridge import parse_time_range
from app.schemas.memory import MemoryRecord, ExtractedMemory, MemoryExtractionResponse


class TestMemorySchemas:
    def test_memory_record_from_dict(self):
        record = MemoryRecord(
            id="m1", user_id="u1", memory_type="fact",
            content="CS student", confidence=0.8,
        )
        assert record.memory_type == "fact"
        assert record.confidence == 0.8
        assert record.stability == 1.0  # default

    def test_extracted_memory_validates_type(self):
        mem = ExtractedMemory(type="preference", content="Likes mornings")
        assert mem.type == "preference"

    def test_extraction_response_schema(self):
        resp = MemoryExtractionResponse(memories=[
            ExtractedMemory(type="fact", content="CS student"),
            ExtractedMemory(type="goal", content="Finish DSA by April"),
        ])
        assert len(resp.memories) == 2


class TestTimeRangeParsing:
    """Ranges come back in HORIZON minutes (0 = DAY_START_HOUR = 08:00), the
    frame TimeSlot and OR-Tools use — not wall-clock minutes from midnight."""

    def test_24h_format(self):
        result = parse_time_range("No tasks between 14:00 and 15:00")
        assert result == (360, 420)  # 14:00-15:00 is 6-7h after 08:00

    def test_12h_format(self):
        result = parse_time_range("Meeting from 2 PM to 3 PM")
        assert result == (360, 420)

    def test_after_pattern(self):
        result = parse_time_range("No work after 6 PM")
        assert result == (600, 1440)  # 18:00 to the end of the 08:00-anchored day

    def test_before_pattern(self):
        result = parse_time_range("No tasks before 10 AM")
        assert result == (0, 120)  # day start to 10:00, not an 8AM-6PM dead zone

    def test_no_time_returns_none(self):
        result = parse_time_range("User prefers quiet study")
        assert result is None
