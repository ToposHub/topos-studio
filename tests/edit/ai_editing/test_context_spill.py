"""Context spill metadata remains bounded and does not persist raw prompts."""

from __future__ import annotations

from src.features.edit.ai_editing.context_spill import ContextSpillStore
from src.features.edit.ai_editing.session_models import CompressionCheckpoint


def test_spill_store_deduplicates_and_restores_metadata_only() -> None:
    store = ContextSpillStore()
    first = store.record(
        scope="session-1",
        kind="chat_history",
        items=[{"role": "user", "content": "api_key=abcdefghijklmnop"}],
        token_count=42,
        preview="历史摘要",
    )
    assert store.record(
        scope="session-1",
        kind="chat_history",
        items=[{"role": "user", "content": "api_key=abcdefghijklmnop"}],
        token_count=42,
        preview="历史摘要",
    ) == first
    snapshot = store.export_all()
    assert snapshot is not None
    assert "api_key=abcdefghijklmnop" not in str(snapshot)

    restored = ContextSpillStore()
    assert restored.import_all(snapshot) == 1
    assert restored.get(first.spill_id) is not None


def test_compression_checkpoint_keeps_optional_spill_id_compatible() -> None:
    checkpoint = CompressionCheckpoint(
        checkpoint_id="cp-1", message_count_before=10,
        message_count_after=3, spill_id="spill-1",
    )
    restored = CompressionCheckpoint.from_dict(checkpoint.to_dict())
    assert restored.spill_id == "spill-1"
