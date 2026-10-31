"""TAEP Phase 9 — assistant_events 与 provider_profiles 逻辑测试。

UI 子组件的状态/事件对象是纯数据，可在无 Qt 环境测试（TAEP §11.9 UI-0）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.assistant_events import (
    AssistantState,
    AssistantStateSnapshot,
    AssistantRequest,
    ContextSummary,
    VariantCard,
)
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
    ProfileRegistry,
)


# ================================================================
# AssistantState 状态机
# ================================================================


def test_state_busy_and_awaiting_user_flags():
    assert AssistantState.PLANNING.is_busy is True
    assert AssistantState.IDLE.is_busy is False
    assert AssistantState.AWAITING_VARIANT_APPROVAL.is_awaiting_user is True
    assert AssistantState.PLANNING.is_awaiting_user is False


def test_assistant_request_immutable_with_id():
    req = AssistantRequest(request_id="r1", text="做一个宣传片")
    assert req.request_id == "r1"
    with pytest.raises(ValueError):
        AssistantRequest(request_id="", text="x")


def test_assistant_request_keeps_explicit_motion_graphic_target():
    req = AssistantRequest(
        request_id="r-target",
        text="修改这个 Clip 的网页",
        target_asset_ids=("teaching_animation:scene-a",),
    )
    assert req.target_asset_ids == ("teaching_animation:scene-a",)


def test_state_snapshot_defaults():
    snap = AssistantStateSnapshot()
    assert snap.state == AssistantState.IDLE
    assert snap.variants == ()
    assert snap.error_message is None


def test_variant_card_immutable():
    v = VariantCard(variant_id="v1", label="A 口播优先", duration_frames=750)
    assert v.duration_frames == 750
    assert v.status == "proposed"


# ================================================================
# AiProviderProfile（非 secret）
# ================================================================


def test_profile_round_trip_no_secret():
    p = AiProviderProfile(
        profile_id="local-qwen", display_name="本地模型",
        endpoint="http://127.0.0.1:8000", model_id="qwen-edit",
        credential_ref="keychain:topos:local-qwen",  # 只是引用，不含 secret
        data_policy=DataPolicy(execution="local", allow_media_upload=False),
    )
    d = p.to_dict()
    # dict 里只有 credential_ref，绝无 secret/api_key
    s = str(d)
    assert "api_key" not in s.lower()
    assert "secret" not in s.lower()
    assert d["credential_ref"] == "keychain:topos:local-qwen"
    # round-trip
    p2 = AiProviderProfile.from_dict(d)
    assert p2.profile_id == "local-qwen"
    assert p2.credential_ref == p.credential_ref


def test_profile_allows_media_upload_requires_both():
    # modality 含 media_upload + policy 允许 -> True
    p1 = AiProviderProfile(
        profile_id="cloud-1", display_name="Cloud",
        modalities=("text", "tool_call", "media_upload"),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
    )
    assert p1.allows_media_upload is True
    # policy 允许但 modality 不含 -> False
    p2 = AiProviderProfile(
        profile_id="cloud-2", display_name="Cloud2",
        modalities=("text",),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
    )
    assert p2.allows_media_upload is False


def test_profile_registry_active():
    reg = ProfileRegistry()
    p = AiProviderProfile(profile_id="p1", display_name="P1")
    reg.add(p)
    assert reg.active_profile is None
    reg.set_active("p1")
    assert reg.active_profile is p
    # remove active clears
    reg.remove("p1")
    assert reg.active_profile is None


def test_profile_registry_set_active_unknown_raises():
    reg = ProfileRegistry()
    with pytest.raises(KeyError):
        reg.set_active("nonexistent")


# ================================================================
# CredentialStore（fake，测试用）
# ================================================================


def test_fake_credential_store_round_trip():
    store = FakeCredentialStore()
    store.set_secret("ref1", "sk-secret-value")
    assert store.get_secret("ref1") == "sk-secret-value"
    store.delete_secret("ref1")
    assert store.get_secret("ref1") is None
