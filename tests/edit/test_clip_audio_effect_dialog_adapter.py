"""
Tests for the clip-level audio effect dialog adapter (P-clip-FX).

The adapter reuses the 8 mixer dialogs for clip-level effects, translating
between the dialog's strong-typed *Params dataclass and the clip's plain
params dict. These tests cover:
  - dialog registry construction (8 entries, correct metadata)
  - open_dialog creates a cached dialog and feeds dict → *Params
  - the committed signal round-trips *Params → dict via clip_effect_params_committed
  - non-mixer effect_types return None (no dialog)
"""

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.clip_audio_effect_dialog_adapter import (
    ClipAudioEffectDialogManager,
    _ensure_dialog_registry,
)
from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
from src.features.edit.core.timeline_v2 import ReverbParams


class TestDialogRegistry:
    """The lazy dialog registry covers all 8 mixer effect_types."""

    def test_registry_has_eight_entries(self):
        reg = _ensure_dialog_registry()
        assert set(reg.keys()) == set(AUDIO_MIXER_EFFECT_TYPES)
        assert len(reg) == 8

    def test_registry_metadata_shape(self):
        reg = _ensure_dialog_registry()
        for effect_type, (dialog_cls, params_cls, committed_attr, has_iid) in reg.items():
            assert dialog_cls is not None
            assert params_cls is not None
            # committed signal attribute name follows <type>_committed convention
            assert "_committed" in committed_attr
            # only StripEQDialog lacks instance_id (capsule-style ctor)
            assert isinstance(has_iid, bool)


class TestOpenDialog:
    """open_dialog builds a cached dialog and wires the committed signal."""

    def test_open_returns_dialog(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        params = ReverbParams.default().to_dict()
        dialog = mgr.open_dialog(
            effect_type="reverb",
            clip_id="clip_1",
            effect_id="fx_1",
            params_dict=params,
            clip_name="My Clip",
        )
        assert dialog is not None
        assert dialog.isVisible()
        dialog.close()

    def test_open_caches_dialog(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        params = {"mix": 30.0, "decay": 2.0, "pre_delay_ms": 0.0,
                  "damping_hz": 5000.0, "enabled": True}
        d1 = mgr.open_dialog("reverb", "clip_2", "fx_2", params)
        d2 = mgr.open_dialog("reverb", "clip_2", "fx_2", params)
        # 同一 (clip_id, effect_id) 复用同一窗口
        assert d1 is d2
        d1.close()

    def test_non_mixer_returns_none(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        dialog = mgr.open_dialog(
            effect_type="blur",  # 视频效果，无 dialog
            clip_id="clip_3", effect_id="fx_3",
            params_dict={"radius": 5.0},
        )
        assert dialog is None


class TestCommittedRoundTrip:
    """The committed signal round-trips *Params → dict via the adapter."""

    def test_committed_emits_dict(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        received: list = []
        mgr.clip_effect_params_committed.connect(
            lambda cid, eid, p: received.append((cid, eid, p))
        )

        params = ReverbParams(mix=50.0, decay=3.0).to_dict()
        dialog = mgr.open_dialog("reverb", "clip_4", "fx_4", params)

        # 模拟 dialog emit committed（传 *Params dataclass）
        new_params = ReverbParams(mix=80.0, decay=4.0)
        dialog.reverb_committed.emit("clip_4", new_params)

        assert len(received) == 1
        cid, eid, emitted_dict = received[0]
        assert cid == "clip_4"
        assert eid == "fx_4"
        # dict 与 *Params.to_dict() 一致
        assert emitted_dict["mix"] == 80.0
        assert emitted_dict["decay"] == 4.0
        assert emitted_dict["enabled"] is True
        dialog.close()

    def test_committed_none_params_ignored(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        received: list = []
        mgr.clip_effect_params_committed.connect(
            lambda cid, eid, p: received.append((cid, eid, p))
        )
        params = ReverbParams.default().to_dict()
        dialog = mgr.open_dialog("reverb", "clip_5", "fx_5", params)
        # None params → 不 emit
        dialog.reverb_committed.emit("clip_5", None)
        assert received == []
        dialog.close()


class TestParamsDictConversion:
    """dict → *Params.from_dict feeds the dialog initial state correctly."""

    def test_partial_dict_uses_defaults(self, qapp):
        """from_dict tolerates missing fields (uses *Params defaults)."""
        mgr = ClipAudioEffectDialogManager()
        # 只给 mix，其它字段缺失 → from_dict 用默认值兜底
        partial = {"mix": 60.0}
        dialog = mgr.open_dialog("reverb", "clip_6", "fx_6", partial)
        assert dialog is not None
        # dialog 内部 _current 是完整 ReverbParams
        assert dialog._current.mix == 60.0
        assert dialog._current.decay == 2.0  # 默认值
        dialog.close()
