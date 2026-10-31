"""
Unit tests for Video Pipeline Decision (V-P1-1).

Tests for:
- VideoPipelineMode enum
- VideoPipelineEffectiveMode enum
- VideoPipelineFallbackReason enum
- VideoPipelineDecision dataclass
- resolve_video_pipeline_mode() function
- fallback_to_v2_or_fail() function (V-P0-3)
- is_fatal_fallback_reason() function
- is_professional_codec() / is_high_bitdepth_format() helpers (V-P0-5)

V-P1-1: Tests import real implementation from src.shared.video.pipeline_decision
instead of duplicating logic in test file.
"""

import pytest
from typing import Optional, Any

# V-P1-1: Import from real module
from src.shared.video.pipeline_decision import (
    VideoPipelineMode,
    VideoPipelineEffectiveMode,
    VideoPipelineDecision,
    FallbackDecision,
    resolve_video_pipeline_mode,
    fallback_to_v2_or_fail,
    is_fatal_fallback_reason,
    should_allow_fallback_for_reason,
    is_professional_codec,
    is_high_bitdepth_format,
    should_prevent_fallback_for_codec_format,
    FATAL_FALLBACK_REASONS,
    NON_FATAL_REASONS,
)

from src.shared.video.fallback_types import VideoPipelineFallbackReason


# =============================================================================
# Mock ConfigManager
# =============================================================================

class MockConfigManager:
    """Mock config manager for testing."""
    def __init__(self, mode='auto', allow_fallback=True):
        self._mode = mode
        self._allow_fallback = allow_fallback

    def get(self, key):
        if key == 'video_pipeline_mode':
            return self._mode
        elif key == 'video_pipeline_allow_v2_fallback':
            return self._allow_fallback
        return None


# =============================================================================
# Tests for VideoPipelineMode
# =============================================================================

class TestVideoPipelineMode:
    """Tests for VideoPipelineMode enum."""

    def test_mode_values(self):
        """Verify mode enum values."""
        assert VideoPipelineMode.AUTO.value == "auto"
        assert VideoPipelineMode.V3.value == "v3"
        assert VideoPipelineMode.V2.value == "v2"

    def test_mode_from_string(self):
        """Verify creating mode from string."""
        assert VideoPipelineMode("auto") == VideoPipelineMode.AUTO
        assert VideoPipelineMode("v3") == VideoPipelineMode.V3
        assert VideoPipelineMode("v2") == VideoPipelineMode.V2


# =============================================================================
# Tests for VideoPipelineEffectiveMode
# =============================================================================

class TestVideoPipelineEffectiveMode:
    """Tests for VideoPipelineEffectiveMode enum."""

    def test_effective_mode_values(self):
        """Verify effective mode enum values."""
        assert VideoPipelineEffectiveMode.V3.value == "v3"
        assert VideoPipelineEffectiveMode.V2.value == "v2"


# =============================================================================
# Tests for VideoPipelineFallbackReason
# =============================================================================

class TestVideoPipelineFallbackReason:
    """Tests for VideoPipelineFallbackReason enum."""

    def test_fallback_reasons(self):
        """Verify fallback reason enum exists."""
        assert VideoPipelineFallbackReason.NONE is not None
        assert VideoPipelineFallbackReason.USER_FORCED_V2 is not None
        assert VideoPipelineFallbackReason.V3_INIT_FAILED is not None
        assert VideoPipelineFallbackReason.FIRST_FRAME_TIMEOUT is not None


# =============================================================================
# Tests for VideoPipelineDecision
# =============================================================================

class TestVideoPipelineDecision:
    """Tests for VideoPipelineDecision dataclass."""

    def test_decision_creation(self):
        """Verify decision object creation."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=True,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test message",
        )
        assert decision.requested_mode == VideoPipelineMode.AUTO
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.allow_fallback == True
        assert decision.is_v3_active() == True

    def test_decision_str_no_fallback(self):
        """Verify __str__ with no fallback."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=True,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test message",
        )
        str_repr = str(decision)
        assert "v3" in str_repr
        assert "auto" in str_repr
        assert "fallback" not in str_repr.lower()

    def test_decision_str_with_fallback(self):
        """Verify __str__ with fallback."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V2,
            allow_fallback=True,
            reason="fallback",
            fallback_reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            message="Test message",
        )
        str_repr = str(decision)
        assert "v2" in str_repr
        assert "fallback" in str_repr.lower()
        assert "V3_INIT_FAILED" in str_repr

    def test_to_log_dict(self):
        """Verify to_log_dict() method."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=True,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test message",
        )
        log_dict = decision.to_log_dict()
        assert log_dict['requested_mode'] == 'auto'
        assert log_dict['effective_mode'] == 'v3'
        assert log_dict['allow_fallback'] == True
        assert log_dict['reason'] == 'default_auto'


# =============================================================================
# Tests for resolve_video_pipeline_mode()
# =============================================================================

class TestResolveVideoPipelineMode:
    """Tests for resolve_video_pipeline_mode() function."""

    def test_auto_mode_defaults_to_v3(self):
        """Auto mode should default to v3."""
        config = MockConfigManager('auto', True)
        decision = resolve_video_pipeline_mode(config)
        assert decision.requested_mode == VideoPipelineMode.AUTO
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.allow_fallback == True
        assert decision.reason == "default_auto"

    def test_v3_mode_forces_v3(self):
        """v3 mode should force v3."""
        config = MockConfigManager('v3', False)
        decision = resolve_video_pipeline_mode(config)
        assert decision.requested_mode == VideoPipelineMode.V3
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.allow_fallback == False
        assert decision.reason == "user_forced_v3"

    def test_v2_mode_resolves_to_v3(self):
        """v2 GStreamer 链路已移除：v2 请求应映射为 v3。"""
        config = MockConfigManager('v2', True)
        decision = resolve_video_pipeline_mode(config)
        assert decision.requested_mode == VideoPipelineMode.V2
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.reason == "v2_removed_resolved_to_v3"
        assert decision.fallback_reason == VideoPipelineFallbackReason.USER_FORCED_V2

    def test_auto_with_fatal_fallback_reason(self):
        """Auto mode with fatal fallback reason should return v2."""
        config = MockConfigManager('auto', True)
        decision = resolve_video_pipeline_mode(
            config,
            force_reason=VideoPipelineFallbackReason.V3_INIT_FAILED
        )
        # v2 已移除：fatal 原因只记录，不再回退，保持 v3
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.fallback_reason == VideoPipelineFallbackReason.V3_INIT_FAILED
        assert decision.reason == "fallback"

    def test_auto_with_non_fatal_fallback_reason(self):
        """V-P0-5: Auto mode with non-fatal reason should stay v3."""
        config = MockConfigManager('auto', True)
        decision = resolve_video_pipeline_mode(
            config,
            force_reason=VideoPipelineFallbackReason.FIRST_FRAME_TIMEOUT
        )
        # FIRST_FRAME_TIMEOUT is non-fatal, should stay v3
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.reason == "non_fatal_issue"

    def test_v3_with_fallback_reason_but_disabled(self):
        """v3 mode with fallback reason but fallback disabled should stay v3."""
        config = MockConfigManager('v3', False)
        decision = resolve_video_pipeline_mode(
            config,
            force_reason=VideoPipelineFallbackReason.V3_INIT_FAILED
        )
        # Should still be v3 because user disabled fallback
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.fallback_reason == VideoPipelineFallbackReason.NONE

    def test_auto_without_allow_fallback(self):
        """Auto mode but fallback disabled should not fallback."""
        config = MockConfigManager('auto', False)
        decision = resolve_video_pipeline_mode(
            config,
            force_reason=VideoPipelineFallbackReason.V3_INIT_FAILED
        )
        # Should not fallback because allow_fallback is False
        assert decision.effective_mode == VideoPipelineEffectiveMode.V3
        assert decision.fallback_reason == VideoPipelineFallbackReason.NONE

    def test_invalid_mode_defaults_to_auto(self):
        """Invalid mode string should default to auto."""
        config = MockConfigManager('invalid', True)
        decision = resolve_video_pipeline_mode(config)
        assert decision.requested_mode == VideoPipelineMode.AUTO


# =============================================================================
# Tests for fallback_to_v2_or_fail() (V-P0-3)
# =============================================================================

class TestFallbackToV2OrFail:
    """Tests for fallback_to_v2_or_fail() function."""

    def test_fatal_reason_allows_fallback(self):
        """V-P0-3: Fatal reason with auto mode should allow fallback."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=True,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test",
        )
        result = fallback_to_v2_or_fail(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            path="/path/to/video.mp4",
            decision=decision,
        )
        # v2 GStreamer 链路已移除：即使 fatal 也不回退
        assert result.should_fallback == False
        assert result.is_fatal == True

    def test_non_fatal_reason_stays_v3(self):
        """V-P0-5: Non-fatal reason should stay v3."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=True,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test",
        )
        result = fallback_to_v2_or_fail(
            reason=VideoPipelineFallbackReason.FIRST_FRAME_TIMEOUT,
            path="/path/to/video.mp4",
            decision=decision,
        )
        # FIRST_FRAME_TIMEOUT is non-fatal, should not fallback
        assert result.should_fallback == False
        assert result.is_fatal == False

    def test_forced_v3_no_fallback(self):
        """V-P0-3: Forced v3 should not allow fallback even for fatal reason."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.V3,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=False,
            reason="user_forced_v3",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test",
        )
        result = fallback_to_v2_or_fail(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            path="/path/to/video.mp4",
            decision=decision,
        )
        # Forced v3 should not fallback
        assert result.should_fallback == False
        assert result.user_forced_v3 == True

    def test_auto_with_fallback_disabled(self):
        """V-P0-3: Auto mode but fallback disabled should not fallback."""
        decision = VideoPipelineDecision(
            requested_mode=VideoPipelineMode.AUTO,
            effective_mode=VideoPipelineEffectiveMode.V3,
            allow_fallback=False,
            reason="default_auto",
            fallback_reason=VideoPipelineFallbackReason.NONE,
            message="Test",
        )
        result = fallback_to_v2_or_fail(
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            path="/path/to/video.mp4",
            decision=decision,
        )
        assert result.should_fallback == False
        assert result.allow_fallback == False


# =============================================================================
# Tests for is_fatal_fallback_reason() (V-P0-5)
# =============================================================================

class TestIsFatalFallbackReason:
    """Tests for is_fatal_fallback_reason() function."""

    def test_fatal_reasons(self):
        """V-P0-5: Verify fatal reasons."""
        assert is_fatal_fallback_reason(VideoPipelineFallbackReason.V3_INIT_FAILED)
        assert is_fatal_fallback_reason(VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED)
        assert is_fatal_fallback_reason(VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3)
        assert is_fatal_fallback_reason(VideoPipelineFallbackReason.SOURCE_OPEN_FAILED)

    def test_non_fatal_reasons(self):
        """V-P0-5: Verify non-fatal reasons."""
        assert not is_fatal_fallback_reason(VideoPipelineFallbackReason.NONE)
        assert not is_fatal_fallback_reason(VideoPipelineFallbackReason.FIRST_FRAME_TIMEOUT)
        assert not is_fatal_fallback_reason(VideoPipelineFallbackReason.FIRST_FRAME_UNAVAILABLE)
        assert not is_fatal_fallback_reason(VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED)


# =============================================================================
# Tests for ProRes/High Bitdepth Helpers (V-P0-5)
# =============================================================================

class TestProfessionalCodecHelpers:
    """Tests for is_professional_codec() helper."""

    def test_prores_is_professional(self):
        """ProRes should be recognized as professional."""
        assert is_professional_codec('prores')
        assert is_professional_codec('prores_4444')
        assert is_professional_codec('prores422')

    def test_dnxhd_is_professional(self):
        """DNxHD/DNxHR should be recognized as professional."""
        assert is_professional_codec('dnxhd')
        assert is_professional_codec('dnxhr')

    def test_h264_is_not_professional(self):
        """H.264 should not be recognized as professional."""
        assert not is_professional_codec('h264')
        assert not is_professional_codec('hevc')

    def test_none_codec(self):
        """None codec should return False."""
        assert not is_professional_codec(None)


class TestHighBitdepthFormatHelpers:
    """Tests for is_high_bitdepth_format() helper."""

    def test_10le_is_high_bitdepth(self):
        """10-bit formats should be recognized as high bitdepth."""
        assert is_high_bitdepth_format('yuv422p10le')
        assert is_high_bitdepth_format('yuv444p10le')
        assert is_high_bitdepth_format('gbrp10le')

    def test_12le_is_high_bitdepth(self):
        """12-bit formats should be recognized as high bitdepth."""
        assert is_high_bitdepth_format('yuv422p12le')
        assert is_high_bitdepth_format('gbrp12le')

    def test_8bit_is_not_high_bitdepth(self):
        """8-bit formats should not be recognized as high bitdepth."""
        assert not is_high_bitdepth_format('yuv420p')
        assert not is_high_bitdepth_format('yuv422p')

    def test_none_format(self):
        """None format should return False."""
        assert not is_high_bitdepth_format(None)


class TestShouldPreventFallbackForCodecFormat:
    """Tests for should_prevent_fallback_for_codec_format() function."""

    def test_prores_prevents_fallback(self):
        """V-P0-5: ProRes should prevent fallback."""
        assert should_prevent_fallback_for_codec_format('prores', 'yuv422p10le')
        assert should_prevent_fallback_for_codec_format('dnxhd', None)

    def test_high_bitdepth_prevents_fallback(self):
        """V-P0-5: High bitdepth should prevent fallback."""
        assert should_prevent_fallback_for_codec_format('h264', 'yuv422p10le')
        assert should_prevent_fallback_for_codec_format(None, 'gbrp12le')

    def test_normal_codec_format_allows_fallback(self):
        """V-P0-5: Normal codec/format should not prevent fallback."""
        assert not should_prevent_fallback_for_codec_format('h264', 'yuv420p')
        assert not should_prevent_fallback_for_codec_format(None, None)


# =============================================================================
# Tests for FallbackDecision (V-P0-3)
# =============================================================================

class TestFallbackDecision:
    """Tests for FallbackDecision dataclass."""

    def test_fallback_decision_creation(self):
        """Verify FallbackDecision object creation."""
        result = FallbackDecision(
            should_fallback=True,
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            is_fatal=True,
            message="Fallback allowed",
        )
        assert result.should_fallback == True
        assert result.reason == VideoPipelineFallbackReason.V3_INIT_FAILED
        assert result.is_fatal == True

    def test_fallback_decision_str(self):
        """Verify __str__ method."""
        result = FallbackDecision(
            should_fallback=True,
            reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
            is_fatal=True,
            message="Fallback allowed",
        )
        str_repr = str(result)
        assert "fallback" in str_repr.lower()
        assert "v2" in str_repr.lower()


# =============================================================================
# Tests for Config Migration
# =============================================================================

class TestConfigMigration:
    """Tests for config migration logic (in config_manager.py)."""

    def test_migration_v2_to_v2(self):
        """v2 config should migrate to mode=v2."""
        # This test would need to import ConfigManager with cv2 dependency
        # Placeholder for integration test
        pass

    def test_migration_v3_enabled_to_v3(self):
        """v3 + enabled should migrate to mode=v3."""
        pass

    def test_migration_v3_disabled_to_auto(self):
        """v3 + disabled (inconsistent) should migrate to mode=auto."""
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])