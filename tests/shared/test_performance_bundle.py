"""P0-10: 脱敏性能报告单元测试。"""

import json

import pytest

from src.shared.diagnostics.performance_bundle import (
    collect_performance_bundle,
    default_bundle_filename,
    sanitize_value,
)
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)


class TestSanitizeValue:
    def test_redacts_sensitive_keys(self):
        data = {
            "asset_path": "/Users/bob/Movies/secret.mov",
            "source_path": "/Users/bob/raw.mov",
            "frame_index": 12,
        }
        out = sanitize_value(data)
        assert out["asset_path"] == "<redacted>"
        assert out["source_path"] == "<redacted>"
        assert out["frame_index"] == 12

    def test_masks_absolute_paths_in_strings(self):
        out = sanitize_value({"note": "open /Users/alice/x/y.mov failed"})
        assert "/Users/alice" not in out["note"]
        assert "<path>" in out["note"]

    def test_windows_paths_masked(self):
        # 真实 Windows 路径在内存中是单反斜杠（JSON 转义只发生在序列化时）。
        out = sanitize_value({"msg": "cannot read C:\\Users\\jeff\\clip.mov"})
        assert "jeff" not in out["msg"]
        assert "<path>" in out["msg"]

    def test_nested_structures(self):
        data = {"a": [{"file": "/Users/x.mov", "ok": 1}], "b": {"path": "/home/z"}}
        out = sanitize_value(data)
        assert out["a"][0]["file"] == "<redacted>"
        assert out["b"]["path"] == "<redacted>"

    def test_depth_guard(self):
        deep = current = {}
        for _ in range(30):
            current["child"] = {}
            current = current["child"]
        assert sanitize_value(deep) is not None


class TestCollectBundle:
    def test_bundle_collects_and_sanitizes(self, tmp_path):
        collector = FrameTraceCollector()
        collector.start_session(page="test")
        collector.record(EditPlaybackFrameTrace(
            frame_index=1,
            asset_path="/Users/bob/private.mov",
            fallback_reason="gpu_render_failed",
        ))
        bundle = collect_performance_bundle(trace_collector=collector)
        assert bundle["bundle"] == "topos_performance"
        assert bundle["$schema_version"] == 1
        text = json.dumps(bundle)
        assert "/Users/bob" not in text
        assert "<redacted>" in text
        assert "gpu_render_failed" in text

    def test_no_trace_collector_ok(self):
        bundle = collect_performance_bundle()
        assert "sections" in bundle

    def test_erroring_producer_isolated(self):
        class Bad:
            def get_summary(self):
                raise RuntimeError("boom")

        bundle = collect_performance_bundle(trace_collector=Bad())
        assert "error" in bundle["sections"]["frame_trace_summary"]

    def test_export_writes_file(self, tmp_path):
        out = tmp_path / "bundle.json"
        path = collect_performance_bundle()
        out.write_text(json.dumps(path, default=str))
        assert out.exists()
        assert default_bundle_filename().startswith("topos_performance_")

    def test_export_function(self, tmp_path):
        from src.shared.diagnostics.performance_bundle import (
            export_performance_bundle,
        )

        target = tmp_path / "sub" / "report.json"
        result = export_performance_bundle(str(target))
        assert result == str(target)
        data = json.loads(target.read_text())
        assert data["bundle"] == "topos_performance"
