"""B0 capability truthfulness and release-integrity contracts."""

from __future__ import annotations

import hashlib

import pytest

from src.features.edit.ai_editing.capability_status import (
    CapabilityState,
    build_capability_matrix,
)
from src.features.edit.ai_editing.model_pack_manager import (
    ModelPackLicense,
    ModelPackManager,
    ModelPackSpec,
)
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile

pytestmark = pytest.mark.edit_core


def _profile() -> AiProviderProfile:
    return AiProviderProfile(
        profile_id="cloud",
        display_name="Cloud",
        provider_kind="openai",
        endpoint="https://example.invalid",
        model_id="model",
        credential_ref="topos:cloud",
    )


def test_demo_provider_is_not_reported_as_available() -> None:
    from src.features.edit.ai_editing.generation_provider import (
        DeterministicGenerationProvider,
    )

    statuses = build_capability_matrix(
        profile=_profile(),
        credential_available=True,
        generation_providers=(DeterministicGenerationProvider(),),
    )
    video = next(s for s in statuses if s.capability_id == "generation_video")
    assert video.state == CapabilityState.DEMO
    assert video.available is False
    assert video.next_step


def test_cloud_profile_reports_offline_without_network_call() -> None:
    statuses = build_capability_matrix(
        profile=_profile(), credential_available=True, offline=True,
    )
    text_agent = next(s for s in statuses if s.capability_id == "deepseek_text_agent")
    assert text_agent.state == CapabilityState.OFFLINE
    assert text_agent.retryable is True


def test_configured_cloud_profile_is_unverified_until_connection_test_passes() -> None:
    profile = _profile()
    initial = build_capability_matrix(profile=profile, credential_available=True)
    text_agent = next(s for s in initial if s.capability_id == "deepseek_text_agent")
    assert text_agent.state == CapabilityState.UNVERIFIED
    assert text_agent.available is False
    assert "测试连接" in text_agent.next_step

    verified = build_capability_matrix(
        profile=profile,
        credential_available=True,
        connection_results={profile.profile_id: True},
    )
    text_agent = next(s for s in verified if s.capability_id == "deepseek_text_agent")
    assert text_agent.state == CapabilityState.AVAILABLE


def test_failed_connection_is_visible_not_available() -> None:
    profile = _profile()
    statuses = build_capability_matrix(
        profile=profile,
        credential_available=True,
        connection_results={profile.profile_id: "connection refused"},
    )
    text_agent = next(s for s in statuses if s.capability_id == "deepseek_text_agent")
    assert text_agent.state == CapabilityState.FAILED
    assert text_agent.available is False


def test_strict_host_rejects_release_required_hash(tmp_path) -> None:
    manager = ModelPackManager(cache_dir=tmp_path, strict_verification=True)
    pack_dir = tmp_path / "qwen3_asr"
    pack_dir.mkdir()
    assert manager.mark_installed("qwen3_asr", pack_dir) is False
    status = manager.get_status("qwen3_asr")
    assert status.installed is True
    assert status.verified is False
    assert status.reason


def test_missing_release_manifest_has_actionable_capability_status(tmp_path) -> None:
    manager = ModelPackManager(cache_dir=tmp_path, strict_verification=True)
    statuses = build_capability_matrix(model_pack_manager=manager)
    asr = next(status for status in statuses if status.capability_id == "asr_local")
    assert asr.state == CapabilityState.NOT_INSTALLED
    assert "发布清单" in asr.reason
    assert "发布清单" in asr.next_step


def test_real_release_hash_can_be_applied_and_verified(tmp_path) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "model.bin").write_bytes(b"weights")
    digest = hashlib.sha256(b"model.bin\0weights").hexdigest()
    spec = ModelPackSpec(
        pack_id="test_pack", capability="transcript", version="1",
        size_bytes=7, sha256=digest, languages=("en",), requires_gpu=False,
        min_vram_mb=0,
        license=ModelPackLicense("MIT", True, True, False),
    )
    manager = ModelPackManager(
        cache_dir=tmp_path / "cache", specs=(spec,), strict_verification=True,
    )
    assert manager.mark_installed("test_pack", pack_dir) is True
    assert manager.get_status("test_pack").usable is True
