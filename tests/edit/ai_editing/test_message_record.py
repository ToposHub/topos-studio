"""MessageRecord tests (plan §A5 §5.1-5.2 / §10.1).

Validates the upgrade from the 500-char ``MessageSummary`` to a full,
redacted, status-aware record that can still read legacy summary data.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentMessage,
    AgentMessageStatus,
    ContextUsage,
)
from src.features.edit.ai_editing.message_record import (
    MESSAGE_CONTENT_LIMIT,
    MESSAGE_RECORD_VERSION,
    MessageRecord,
    MessageRecordStatus,
    truncate_for_storage,
)
from src.features.edit.ai_editing.session_models import MessageSummary


# ---------------------------------------------------------------------- #
# Creation + validation
# ---------------------------------------------------------------------- #


class TestCreation:
    def test_defaults(self):
        r = MessageRecord()
        assert r.message_id
        assert r.role == "user"
        assert r.status == MessageRecordStatus.COMPLETED
        assert r.content_complete is True

    def test_invalid_role_normalised(self):
        assert MessageRecord(role="robot").role == "user"

    def test_unknown_status_normalised(self):
        assert MessageRecord(status="weird").status == MessageRecordStatus.COMPLETED

    def test_empty_id_rejected(self):
        with pytest.raises(ValueError):
            MessageRecord(message_id="")


# ---------------------------------------------------------------------- #
# Serialization
# ---------------------------------------------------------------------- #


class TestSerialization:
    def test_roundtrip(self):
        r = MessageRecord(
            message_id="m1",
            role="assistant",
            content="hello",
            run_id="r1",
            artifact_refs=("a1", "a2"),
            usage={"actual_prompt": 10},
        )
        restored = MessageRecord.from_dict(r.to_dict())
        assert restored.content == "hello"
        assert restored.artifact_refs == ("a1", "a2")
        assert restored.usage == {"actual_prompt": 10}
        assert restored.content_complete is True

    def test_schema_version_stamped(self):
        d = MessageRecord().to_dict()
        assert d["schema_version"] == MESSAGE_RECORD_VERSION


# ---------------------------------------------------------------------- #
# Legacy migration
# ---------------------------------------------------------------------- #


class TestLegacyMigration:
    def test_legacy_text_field_becomes_content(self):
        # Old MessageSummary persisted shape: {text: ...}, no status/usage.
        legacy = {
            "message_id": "old1",
            "role": "user",
            "text": "legacy snippet",
            "run_id": "r0",
        }
        r = MessageRecord.from_dict(legacy)
        assert r.content == "legacy snippet"
        assert r.content_complete is False  # flagged as possibly incomplete
        assert r.status == MessageRecordStatus.COMPLETED

    def test_from_summary_flags_incomplete(self):
        summary = MessageSummary(
            message_id="s1",
            role="user",
            text="summary text",
            run_id="r1",
            artifact_id="art1",
            decision_id="d1",
        )
        r = MessageRecord.from_summary(summary)
        assert r.content == "summary text"
        assert r.content_complete is False
        assert r.artifact_refs == ("art1",)
        assert r.decision_id == "d1"

    def test_from_summary_without_artifact(self):
        summary = MessageSummary(message_id="s2", text="no artifact")
        r = MessageRecord.from_summary(summary)
        assert r.artifact_refs == ()


# ---------------------------------------------------------------------- #
# Runtime conversion
# ---------------------------------------------------------------------- #


class TestFromAgentMessage:
    def test_converts_full_body_and_usage(self):
        am = AgentMessage(
            message_id="am1",
            role="assistant",
            content="runtime reply",
            run_id="r1",
            status=AgentMessageStatus.COMPLETED,
            usage=ContextUsage(actual_prompt=50, actual_completion=20),
            artifact_refs=("a1", "a2"),
        )
        r = MessageRecord.from_agent_message(am)
        assert r.content == "runtime reply"
        assert r.content_complete is True
        assert r.usage is not None
        assert r.usage["actual_prompt"] == 50
        assert r.artifact_refs == ("a1", "a2")

    def test_no_usage_stays_none(self):
        am = AgentMessage(message_id="am2", role="assistant", content="hi")
        r = MessageRecord.from_agent_message(am)
        assert r.usage is None


# ---------------------------------------------------------------------- #
# Truncation
# ---------------------------------------------------------------------- #


class TestTruncation:
    def test_short_content_not_truncated(self):
        body, truncated = truncate_for_storage("short")
        assert not truncated
        assert body == "short"

    def test_oversized_truncated_with_marker(self):
        big = "x" * (MESSAGE_CONTENT_LIMIT + 100)
        body, truncated = truncate_for_storage(big)
        assert truncated
        assert len(body) > MESSAGE_CONTENT_LIMIT  # marker appended
        assert "截断" in body

    def test_to_dict_truncates_oversized(self):
        big = "y" * (MESSAGE_CONTENT_LIMIT + 50)
        r = MessageRecord(content=big)
        d = r.to_dict()
        assert len(d["content"]) <= MESSAGE_CONTENT_LIMIT + 100
        assert "截断" in d["content"]


# ---------------------------------------------------------------------- #
# Redaction
# ---------------------------------------------------------------------- #


class TestRedaction:
    def test_redacted_flag_set_when_sanitised(self):
        # sanitize_for_storage redacts known secret patterns; if it changes the
        # body, redacted must be True.  Using a clearly-redactable pattern:
        # the exact behaviour depends on sanitize_for_storage, so we only
        # assert the flag mechanism via truncation here.
        big = "z" * (MESSAGE_CONTENT_LIMIT + 10)
        d = MessageRecord(content=big).to_dict()
        # truncation counts as redaction
        assert d["redacted"] is True

    def test_clean_body_not_flagged(self):
        d = MessageRecord(content="plain clean text").to_dict()
        # no truncation, no sanitisation change -> not redacted
        assert d["redacted"] is False
