"""T14 · 本地只读审片资料包测试。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.codec import parse_snapshot
from src.features.edit.insights.review_package import build_review_package


pytestmark = [pytest.mark.edit_ui]  # 含联系表 offscreen 渲染


@pytest.fixture(scope="module")
def snapshot():
    st = syn.build_synthetic_state()
    return capture_readonly_state(st.state, capture_id="cap-t14", captured_at=0.0)


def test_package_contents_and_manifest(snapshot, tmp_path):
    result = build_review_package(snapshot, tmp_path, make_zip=True)
    assert result.ok, result.error
    pkg = result.directory
    names = {p.name for p in Path(pkg).iterdir()}
    assert {"index.html", "report.csv", "report.json", "report.html",
            "stats.json", "stats.html", "snapshot.json", "manifest.json",
            "contact_sheet"} <= names

    manifest = json.loads((result.manifest and Path(pkg, "manifest.json").read_text(encoding="utf-8")))
    assert manifest["capture_id"] == snapshot.capture_id
    assert manifest["report_schema_version"] == "insights-report/1"
    assert manifest["snapshot_schema_version"] == "1"
    assert manifest["sequence_revisions"] == snapshot.sequence_revision_map
    assert manifest["payload_hash"] == snapshot.payload_hash
    assert manifest["load_states"][syn.SEQ_UNLOADED] == "unloaded"
    assert manifest["read_only"] is True
    # 缺图降级如实标注（无宿主 → 全占位）
    assert manifest["contact_sheet"]["degraded"] is True
    assert result.degraded is True
    # 文件清单的 sha256 与实际文件一致（抽查）
    entry = next(f for f in manifest["files"] if f["path"] == "report.csv")
    import hashlib

    actual = hashlib.sha256(
        (Path(pkg) / "report.csv").read_bytes()
    ).hexdigest()
    assert entry["sha256"] == actual
    # ZIP 内容 = 目录内容
    with zipfile.ZipFile(result.zip_path) as zf:
        assert "manifest.json" in zf.namelist()
        assert "snapshot.json" in zf.namelist()


def test_package_snapshot_roundtrip_via_codec(snapshot, tmp_path):
    """包内 snapshot.json 用 T01 codec 完整往返：同次捕获恒等。"""
    result = build_review_package(snapshot, tmp_path)
    assert result.ok
    text = (Path(result.directory) / "snapshot.json").read_text(encoding="utf-8")
    parsed = parse_snapshot(text)
    assert parsed.ok, parsed.error
    assert parsed.snapshot == snapshot


def test_package_from_same_capture_immune_to_host_edits(tmp_path):
    """生成用同一冻结快照；捕获后宿主继续编辑不影响包内容。"""
    st = syn.build_synthetic_state()
    snap = capture_readonly_state(st.state, capture_id="cap-fixed", captured_at=0.0)
    st.project.assets["late"] = st.project.assets[syn.ASSET_A]  # 宿主侧写入
    result = build_review_package(snap, tmp_path)
    assert result.ok
    manifest = json.loads((Path(result.directory) / "manifest.json").read_text(encoding="utf-8"))
    assert len([f for f in manifest["files"]]) > 0
    parsed = parse_snapshot((Path(result.directory) / "snapshot.json").read_text(encoding="utf-8"))
    assert parsed.ok and parsed.snapshot == snap  # 包内容仍属同次捕获


def test_failure_keeps_old_package_and_cleans_staging(snapshot, tmp_path, monkeypatch):
    import pathlib

    first = build_review_package(snapshot, tmp_path)
    assert first.ok
    old_manifest = Path(first.directory, "manifest.json").read_text(encoding="utf-8")

    # 第二次：用同 capture_id 撞既有目录 + 强制中途失败 → 旧包原样、无 staging 残留
    import src.features.edit.insights.review_package as rp

    def boom(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(rp, "write_csv", boom)
    second = build_review_package(snapshot, tmp_path)
    assert not second.ok and "write failure" in second.error
    assert Path(first.directory, "manifest.json").read_text(encoding="utf-8") == old_manifest
    leftovers = [p.name for p in Path(tmp_path).iterdir() if p.name.startswith(".staging")]
    assert leftovers == []


def test_existing_package_not_overwritten(snapshot, tmp_path):
    import pathlib

    first = build_review_package(snapshot, tmp_path)
    assert first.ok
    # 同 capture_id 再打包：目标目录已存在 → 拒绝覆盖
    again = build_review_package(snapshot, tmp_path)
    assert not again.ok and "already exists" in again.error
    assert Path(first.directory, "manifest.json").exists()


def test_chinese_output_path(snapshot, tmp_path):
    cn_dir = tmp_path / "中文目录" / "审片资料"
    result = build_review_package(snapshot, cn_dir)
    assert result.ok, result.error
    pkg_dir = cn_dir / Path(result.directory).name
    assert (pkg_dir / "index.html").exists()
    html_text = (pkg_dir / "index.html").read_text(encoding="utf-8")
    assert "<script" not in html_text.lower()
    # HTML 只链接包内相对资源
    assert 'href="report.html"' in html_text


def test_stats_scope_notes_in_package(snapshot, tmp_path):
    import pathlib

    result = build_review_package(snapshot, tmp_path)
    stats = json.loads(Path(result.directory, "stats.json").read_text(encoding="utf-8"))
    assert stats[syn.SEQ_MAIN]["clip_count"] == 5
    assert "direct clip references only" in " ".join(stats[syn.SEQ_MAIN]["scope_notes"])
