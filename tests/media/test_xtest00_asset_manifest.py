"""X-TEST-00 冻结工具的聚焦单测。

只测纯逻辑与合成小样本，不触碰 tests/test_videos 真实素材：
1. 指纹确定性（同内容同 hash；内容变化 hash 变化）；
2. PyAV 探测合成 MOV（正路径）与 native 探测 NOT_APPLICABLE；
3. 探测冲突检测逐字段报告 reason_code；
4. canonical digest 排除易变字段（generated_at/absolute_path/dirty count）；
5. TOPOS_CODEC_LIB 缺失时给出 TOPOS_CODEC_LIB_NOT_FOUND，不静默。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.freeze_ai_test_asset_manifest import (
    canonical_digest,
    detect_probe_conflicts,
    enumerate_assets,
    native_topos_probe,
    probe_asset,
    pyav_probe,
    sha256_fingerprint,
    _topos_codec_info,
)


av = pytest.importorskip("av")  # noqa: N816 — PyAV 缺失时整组跳过


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _write_synthetic_mov(path: Path, frames: int = 4, width: int = 64,
                         height: int = 48, fps: int = 25) -> None:
    """用 PyAV 编码一个极小的 mpeg4 MOV 合成样本（不依赖 tests/test_videos）。"""
    container = av.open(str(path), "w")
    stream = container.add_stream("mpeg4", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    for i in range(frames):
        frame = av.VideoFrame(width, height, format="yuv420p")
        frame.planes[0].update(bytes(
            (i * 7 + x) % 256 for x in range(width * height)))
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()


@pytest.fixture()
def synthetic_mov(tmp_path: Path) -> Path:
    p = tmp_path / "synthetic.mov"
    _write_synthetic_mov(p)
    return p


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello topos" * 1000)
        for mode in ("sha256_full", "sha256_head1mb_tail1mb+size"):
            assert (sha256_fingerprint(f, mode)
                    == sha256_fingerprint(f, mode))

    def test_content_change_changes_full_digest(self, tmp_path: Path) -> None:
        f = tmp_path / "b.bin"
        f.write_bytes(b"A" * 4096)
        d_full = sha256_fingerprint(f, "sha256_full")
        f.write_bytes(b"B" * 4096)
        assert sha256_fingerprint(f, "sha256_full") != d_full

    def test_unknown_mode_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "c.bin"
        f.write_bytes(b"x")
        with pytest.raises(ValueError):
            sha256_fingerprint(f, "sha256_bogus")


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

class TestProbes:
    def test_pyav_probe_synthetic_mov(self, synthetic_mov: Path) -> None:
        result = pyav_probe(synthetic_mov)
        assert result["status"] == "AVAILABLE", result
        video = result["video"]
        assert video["width"] == 64
        assert video["height"] == 48
        assert video["pix_fmt"] == "yuv420p"
        assert video["bit_depth"] == 8
        assert video["fps"] == [25, 1]
        assert video["frame_count"] == 4
        assert result["audio_streams"] == []

    def test_native_probe_not_applicable_for_non_topos(
            self, synthetic_mov: Path) -> None:
        result = native_topos_probe(synthetic_mov)
        assert result["status"] == "NOT_APPLICABLE"
        assert result["reason_code"] is None

    def test_no_video_stream_reason_code(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.mov"
        f.write_bytes(b"\x00" * 64)
        result = pyav_probe(f)
        assert result["status"] == "FAILED"
        assert result["reason_code"] in ("PYAV_OPEN_FAILED", "NO_VIDEO_STREAM")

    def test_topos_project_package_not_media(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import zipfile

        pkg = tmp_path / "project.topos"
        with zipfile.ZipFile(pkg, "w") as zf:
            zf.writestr("project.json", "{}")
        # probe_asset 需要 REPO_ROOT 相对路径：临时用 monkeypatch 改 REPO_ROOT
        import tools.freeze_ai_test_asset_manifest as mod

        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        entry = probe_asset(pkg, "sha256_full")
        assert entry["probe_status"] == "NOT_MEDIA"
        assert entry["probe_reason_codes"] == ["TOPOS_PROJECT_PACKAGE_DETECTED"]
        assert entry["probe_backends"] == []
        assert entry["content_hash"]["digest"] == sha256_fingerprint(pkg, "sha256_full")


class TestConflictDetection:
    def test_conflict_reported_per_field(self) -> None:
        probes = [
            {"backend": "pyav", "status": "AVAILABLE",
             "video": {"width": 1920, "height": 1080, "fps": [25, 1],
                       "fps_float": 25.0, "bit_depth": 10, "frame_count": 96}},
            {"backend": "topos_native", "status": "AVAILABLE",
             "video": {"width": 1920, "height": 1081, "fps": [25, 1],
                       "fps_float": 25.0, "bit_depth": 10, "frame_count": 95}},
        ]
        conflicts = detect_probe_conflicts(probes)
        fields = {c["field"] for c in conflicts}
        assert fields == {"height", "frame_count"}
        assert all(c["reason_code"] == "PROBE_METADATA_CONFLICT"
                   for c in conflicts)

    def test_no_conflict_when_consistent(self) -> None:
        probes = [
            {"backend": "pyav", "status": "AVAILABLE",
             "video": {"width": 64, "height": 48, "fps_float": 25.0,
                       "bit_depth": 8, "frame_count": 4}},
            {"backend": "topos_native", "status": "NOT_APPLICABLE",
             "video": None},
        ]
        assert detect_probe_conflicts(probes) == []

    def test_unavailable_backend_not_compared(self) -> None:
        probes = [
            {"backend": "pyav", "status": "AVAILABLE",
             "video": {"width": 64, "height": 48, "fps_float": 25.0,
                       "bit_depth": 8, "frame_count": 4}},
            {"backend": "topos_native", "status": "UNAVAILABLE",
             "reason_code": "TOPOS_CODEC_LIB_NOT_FOUND",
             "video": {"width": 1, "height": 1, "fps_float": 1.0,
                       "bit_depth": 8, "frame_count": 1}},
        ]
        assert detect_probe_conflicts(probes) == []


# ---------------------------------------------------------------------------
# canonical digest
# ---------------------------------------------------------------------------

class TestCanonicalDigest:
    @staticmethod
    def _manifest(abs_path: str, dirty: int, digest: str) -> dict:
        return {
            "$schema": "xtest00_asset_manifest@1",
            "generated_at": "2026-09-17T10:00:00+0800",
            "environment": {"git": {"revision": "abc",
                                    "dirty_file_count": dirty}},
            "assets": [{
                "file": "tests/test_videos/a.mov",
                "absolute_path": abs_path,
                "content_hash": {"mode": "sha256_full", "digest": digest},
                "probe_status": "PASS",
                "video": {"width": 64, "height": 48},
            }],
        }

    def test_volatile_fields_excluded(self) -> None:
        m1 = self._manifest("/Users/alice/repo/a.mov", 3, "d" * 64)
        m2 = self._manifest("/Users/bob/work/a.mov", 17, "d" * 64)
        assert canonical_digest(m1) == canonical_digest(m2)

    def test_content_change_changes_digest(self) -> None:
        m1 = self._manifest("/repo/a.mov", 0, "d" * 64)
        m2 = self._manifest("/repo/a.mov", 0, "e" * 64)
        assert canonical_digest(m1) != canonical_digest(m2)


# ---------------------------------------------------------------------------
# 枚举与 TOPOS 库 reason_code
# ---------------------------------------------------------------------------

class TestEnumerationAndCodec:
    def test_enumerate_skips_non_media(self, tmp_path: Path) -> None:
        (tmp_path / "a.mov").write_bytes(b"x")
        (tmp_path / "b.json").write_text("{}")
        (tmp_path / ".DS_Store").write_bytes(b"x")
        sub = tmp_path / ".cache"
        sub.mkdir()
        (sub / "c.mov").write_bytes(b"x")
        found = [p.name for p in enumerate_assets(tmp_path)]
        assert found == ["a.mov"]

    def test_missing_topos_lib_reason_code(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_CODEC_LIB", str(tmp_path / "nope.dylib"))
        info = _topos_codec_info()
        assert info["status"] == "UNAVAILABLE"
        assert info["reason_code"] == "TOPOS_CODEC_LIB_NOT_FOUND"
