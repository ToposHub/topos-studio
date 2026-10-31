"""Unit tests for the VideoToolbox hardware-decode capability probe.

The live VT probe is environment-dependent (and can even be flaky on
hackintosh driver stacks under decoder-instance contention), so these tests
monkeypatch the session-level probe and verify the caching / policy / retry
logic around it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from src.shared.media.macos_vt import hardware_capability as hc


_darwin_only_w01 = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS 专属逻辑（VT/Metal）；W01 复跑确认在 Windows 为测试缺平台门"
)

@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    hc.clear_hardware_capability_cache()
    monkeypatch.delenv("TOPOS_PLAYBACK_VT_POLICY", raising=False)
    monkeypatch.delenv("TOPOS_VT_HW_PROBE", raising=False)
    monkeypatch.setenv("TOPOS_VT_HW_CACHE_DIR", str(tmp_path))
    # Opt in to the disk cache for these tests (production default).
    monkeypatch.setenv("TOPOS_VT_HW_CACHE", "1")
    yield
    hc.clear_hardware_capability_cache()


def _patch_session_probe(monkeypatch, results: list):
    """Patch VtSession.probe_hardware_support to pop canned results."""
    from src.shared.media.macos_vt.vt_session import VtSession

    calls = []

    def fake(self, width, height, codec='h264', extradata=None, codec_tag=''):
        calls.append((codec, extradata, codec_tag))
        return results.pop(0) if results else results[-1] if results else None

    monkeypatch.setattr(VtSession, "probe_hardware_support", fake)
    return calls


def _patch_demuxer(monkeypatch):
    from src.shared.media.macos_vt import packet_demux

    class _Info:
        codec_name = 'hevc'
        width, height = 3840, 2160
        bit_depth = 10
        extradata = b'\x00' * 8
        codec_tag = 'hvc1'

    class _Demuxer:
        def open(self, path):
            return _Info()

        def close(self):
            pass

    monkeypatch.setattr(packet_demux, "PacketDemuxer", _Demuxer)


def test_policy_skips_probe_for_8bit_h26x(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("8-bit H.264/H.265 must not consult the hw probe")

    monkeypatch.setattr(hc, "vt_hardware_decode_available", _fail)
    assert hc.playback_should_prefer_ffmpeg('hevc', 'yuv420p') is False
    assert hc.playback_should_prefer_ffmpeg('h264', 'nv12') is False


def test_policy_ignores_non_h26x_codecs(monkeypatch):
    monkeypatch.setattr(
        hc, "vt_hardware_decode_available",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prores must not probe")),
    )
    assert hc.playback_should_prefer_ffmpeg('prores_ks', 'yuv422p10le') is False


def test_policy_env_overrides(monkeypatch):
    monkeypatch.setenv("TOPOS_PLAYBACK_VT_POLICY", "force_ffmpeg")
    assert hc.playback_should_prefer_ffmpeg('hevc', 'yuv420p') is True
    monkeypatch.setenv("TOPOS_PLAYBACK_VT_POLICY", "force_vt")
    assert hc.playback_should_prefer_ffmpeg('hevc', 'yuv420p10le') is False


def test_policy_true_when_vt_would_be_software(monkeypatch):
    monkeypatch.setattr(hc, "vt_hardware_decode_available", lambda *a, **k: False)
    assert hc.playback_should_prefer_ffmpeg('hevc', 'yuv420p10le', '/x.mov') is True


def test_policy_false_when_hardware_available(monkeypatch):
    monkeypatch.setattr(hc, "vt_hardware_decode_available", lambda *a, **k: True)
    assert hc.playback_should_prefer_ffmpeg('hevc', 'yuv420p10le', '/x.mov') is False


def test_policy_unknown_verdict_keeps_vt(monkeypatch):
    monkeypatch.setattr(hc, "vt_hardware_decode_available", lambda *a, **k: None)
    assert hc.playback_should_prefer_ffmpeg('hevc', 'yuv420p10le', '/x.mov') is False


@_darwin_only_w01
def test_probe_retries_transient_not_supported_now(monkeypatch):
    _patch_demuxer(monkeypatch)
    calls = _patch_session_probe(monkeypatch, [False, True])
    assert hc.probe_vt_hardware_decode_for_file('/x.mov') is True
    assert len(calls) == 2


@_darwin_only_w01
def test_probe_keeps_definitive_false(monkeypatch):
    _patch_demuxer(monkeypatch)
    calls = _patch_session_probe(monkeypatch, [False, False])
    assert hc.probe_vt_hardware_decode_for_file('/x.mov') is False
    assert len(calls) == 2


@_darwin_only_w01
def test_probe_verdict_cached_across_calls(monkeypatch):
    _patch_demuxer(monkeypatch)
    calls = _patch_session_probe(monkeypatch, [True])
    assert hc.vt_hardware_decode_available('/x.mov', 'hevc', 10, 3840, 2160) is True
    assert hc.vt_hardware_decode_available('/x.mov', 'hevc', 10, 3840, 2160) is True
    assert len(calls) == 1


@_darwin_only_w01
def test_probe_verdict_persisted_to_disk(monkeypatch):
    _patch_demuxer(monkeypatch)
    calls = _patch_session_probe(monkeypatch, [False, False])

    hc.clear_hardware_capability_cache()
    assert hc.vt_hardware_decode_available('/x.mov', 'hevc', 10, 3840, 2160) is False

    # Simulate a process restart: memory cleared, disk cache intact.
    hc.clear_hardware_capability_cache()
    assert hc.vt_hardware_decode_available('/x.mov', 'hevc', 10, 3840, 2160) is False
    assert len(calls) == 2  # no re-probe after restart

    cache_file = Path(os.environ['TOPOS_VT_HW_CACHE_DIR']) / 'macos_vt_hw_decode.json'
    payload = json.loads(cache_file.read_text())
    assert payload['schema_version'] == hc._HW_CACHE_SCHEMA_VERSION
    assert list(payload['entries'].values()) == [False]


def test_machine_fingerprint_in_cache_key(monkeypatch):
    key = hc._hw_cache_key('hevc', 10, 3840, 2160)
    assert key['codec'] == 'hevc'
    assert key['bit_depth'] == 10
    assert key['size_bucket'] == 'uhd'
    assert 'cpu' in key['machine'] and key['machine']['platform']


def test_size_buckets():
    assert hc._size_bucket(1920, 1080) == 'hd'
    assert hc._size_bucket(3840, 2160) == 'uhd'
    assert hc._size_bucket(7680, 4320) == 'large'
