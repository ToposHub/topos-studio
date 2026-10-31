"""wd11-g58：批 D（AUD-17000~AUD-17232，edit 工程对象/归档/代理域）修复回归。

覆盖条目：
- AUD-17000 / AUD-17001：placeholder_frame 元数据尺寸安全提取 + 静态帧缓存
- AUD-17003：playback_debug 环境变量解析容错
- AUD-17101：remove_timeline 清理 bin 的 timeline_ids 引用
- AUD-17102：remove_bin 子 bin 上移一级，parent_id 不悬挂
- AUD-17103：import_subclip 回退分支新建的 root bin 不被覆盖丢弃
- AUD-17104：check_media_availability 与 is_virtual 语义对齐（含 placeholder）
- AUD-17105：reset_clip_composite 返回注解如实标注双形状
- AUD-17106：duplicate_asset 继承素材日志字段与 is_offline
- AUD-17107：_probe_audio_info 探测异常时容器仍被关闭
- AUD-17201：目录类素材（image_sequence）参与 SHA-256 校验
- AUD-17202：哈希/大小计算失败降级为失败记录，报告始终产出
- AUD-17203：collect_project 进度/取消钩子
- AUD-17231：PyAV 取消路径清理半成品代理
- AUD-17232：CLI 兜底超时回收子进程并清理部分输出
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from typing import Any

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core import playback_debug as playback_debug_module
from features.edit.core import project_archive as project_archive_module
from features.edit.core import proxy_generator as proxy_generator_module
from features.edit.core.placeholder_frame import (
    _metadata_dimension,
    _render_placeholder_frame_cached,
    _STRIPE_PERIOD,
    _SMPTE_COLORS,
    render_placeholder_frame,
    render_placeholder_frame_for_asset,
)
from features.edit.core.project import (
    EditProject,
    MediaAsset,
    ProjectBin,
    _probe_audio_info,
    check_media_availability,
)
from features.edit.core.project_archive import (
    STATUS_COPIED,
    STATUS_COPY_FAILED,
    STATUS_HASH_MISMATCH,
    collect_project,
)


def _asset(asset_id: str, path: str, **overrides: Any) -> MediaAsset:
    params: dict[str, Any] = dict(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=os.path.basename(path) or asset_id,
        metadata={},
    )
    params.update(overrides)
    return MediaAsset(**params)


class _FakeAsset:
    """placeholder_frame 的鸭子类型素材（避免构造完整工程）。"""

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata


# ================================================================
# AUD-17000 / AUD-17001：placeholder_frame
# ================================================================


class TestPlaceholderFrameMetadataSafety:
    """AUD-17000：畸形外部元数据不再在渲染路径抛 ValueError/TypeError。"""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (1920, 1920),
            (1920.0, 1920),
            (" 1920 ", 1920),
            ("1920.5", 1920),
            ("1920px", 0),
            ("", 0),
            (None, 0),
            (True, 0),
            ({"w": 1}, 0),
            ([1920], 0),
            (float("nan"), 0),
            (float("inf"), 0),
        ],
    )
    def test_metadata_dimension_safe(self, value: Any, expected: int) -> None:
        assert _metadata_dimension(value) == expected

    def test_bad_metadata_falls_back_to_passed_size(self) -> None:
        asset = _FakeAsset({"width": "1920px", "height": {"h": 1080}})
        frame = render_placeholder_frame_for_asset(asset, 320, 180)
        assert frame.shape == (180, 320, 3)
        assert frame.dtype == np.uint8

    def test_numeric_string_metadata_used(self) -> None:
        asset = _FakeAsset({"width": " 640 ", "height": 360})
        frame = render_placeholder_frame_for_asset(asset, 320, 180)
        assert frame.shape == (360, 640, 3)


class TestPlaceholderFrameCache:
    """AUD-17001：确定性静态帧缓存 + 低临时分配实现。"""

    def test_cached_frame_shared_and_copy_distributed(self) -> None:
        canonical = _render_placeholder_frame_cached(64, 36)
        assert _render_placeholder_frame_cached(64, 36) is canonical

        f1 = render_placeholder_frame(64, 36)
        f2 = render_placeholder_frame(64, 36)
        assert f1 is not f2  # 调用方拿到独立副本，可安全修改
        assert np.array_equal(f1, f2)
        f1[0, 0] = 0
        assert not np.array_equal(f1, f2)
        # 缓存本体未被调用方修改污染
        assert np.array_equal(_render_placeholder_frame_cached(64, 36), f2)

    def test_bit_identical_to_legacy_algorithm(self) -> None:
        """新实现（按行平铺掩码 + 双调色行选取）与旧算法逐位一致。"""
        palette = np.array(_SMPTE_COLORS, dtype=np.float32)[:, ::-1]

        def legacy(width: int, height: int) -> np.ndarray:
            n = len(_SMPTE_COLORS)
            columns = np.arange(width)
            indices = (columns * n) // max(width, 1)
            row = palette[indices]
            frame = np.broadcast_to(row[None, :, :], (height, width, 3)).copy()
            xx, yy = np.meshgrid(np.arange(width), np.arange(height))
            stripe_mask = (((xx + yy) // _STRIPE_PERIOD) % 2 == 0)[..., None]
            frame = np.where(stripe_mask, frame * 0.65, frame)
            return np.clip(frame * 255.0, 0, 255).astype(np.uint8)

        for width, height in ((2, 2), (64, 36), (97, 53), (127, 257), (320, 180)):
            assert np.array_equal(legacy(width, height), render_placeholder_frame(width, height))


# ================================================================
# AUD-17003：playback_debug 环境变量容错
# ================================================================


class TestPlaybackDebugEnvParsing:
    def test_parse_float_env_fallbacks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for bad in ("", "4O", "abc", None):
            monkeypatch.setattr(
                playback_debug_module.os, "environ",
                {} if bad is None else {"TOPOS_EDIT_PLAYBACK_SLOW_MS": bad},
            )
            assert playback_debug_module._parse_float_env(
                "TOPOS_EDIT_PLAYBACK_SLOW_MS", 40.0
            ) == 40.0

    def test_parse_float_env_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_EDIT_PLAYBACK_SLOW_MS", "12.5")
        assert playback_debug_module._parse_float_env(
            "TOPOS_EDIT_PLAYBACK_SLOW_MS", 40.0
        ) == 12.5

    def test_bad_env_does_not_break_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_EDIT_PLAYBACK_SLOW_MS", "4O")
        try:
            reloaded = importlib.reload(playback_debug_module)
            assert reloaded._SLOW_THRESHOLD_MS == 40.0
        finally:
            monkeypatch.delenv("TOPOS_EDIT_PLAYBACK_SLOW_MS", raising=False)
            importlib.reload(playback_debug_module)


# ================================================================
# AUD-17101 / AUD-17102：remove_timeline / remove_bin 引用一致性
# ================================================================


class TestRemoveTimelineCleansBinRefs:
    def test_remove_timeline_clears_bin_timeline_ids(self) -> None:
        project = EditProject.create("t")
        project = project.create_bin("Bin A")
        project = project.create_timeline("TL")
        timeline_id = project.active_timeline_id
        assert timeline_id is not None
        bin_id = next(b for b in project.bins if b != project.root_bin_id)
        project = project.move_timeline_to_bin(timeline_id, bin_id)
        assert timeline_id in project.bins[bin_id].timeline_ids

        removed = project.remove_timeline(timeline_id)

        assert timeline_id not in removed.timelines
        # AUD-17101：bin 引用同步清理，无悬挂 ID
        assert all(
            timeline_id not in b.timeline_ids for b in removed.bins.values()
        )

    def test_remove_active_timeline_still_clears_active(self) -> None:
        project = EditProject.create("t")
        project = project.create_timeline("TL")
        timeline_id = project.active_timeline_id
        assert timeline_id is not None
        removed = project.remove_timeline(timeline_id)
        assert removed.active_timeline_id is None


class TestRemoveBinReparentsChildren:
    def test_children_move_up_one_level(self) -> None:
        project = EditProject.create("t")
        project = project.create_bin("Outer", parent_id=project.root_bin_id)
        outer_id = next(b for b in project.bins if b != project.root_bin_id)
        project = project.create_bin("Inner", parent_id=outer_id)
        inner_id = next(
            b for b in project.bins if b not in (project.root_bin_id, outer_id)
        )
        assert project.bins[inner_id].parent_id == outer_id

        removed = project.remove_bin(outer_id)

        assert outer_id not in removed.bins
        # AUD-17102：子 bin 上移一级（挂到被删 bin 的父级），不悬挂
        assert removed.bins[inner_id].parent_id == project.root_bin_id

    def test_top_level_removed_children_become_top_level(self) -> None:
        project = EditProject.create("t")
        project = project.create_bin("Orphaned Parent")  # parent_id=None
        parent_id = next(b for b in project.bins if b != project.root_bin_id)
        project = project.create_bin("Child", parent_id=parent_id)
        child_id = next(
            b for b in project.bins if b not in (project.root_bin_id, parent_id)
        )

        removed = project.remove_bin(parent_id)

        assert child_id in removed.bins
        assert removed.bins[child_id].parent_id is None


# ================================================================
# AUD-17103：import_subclip root bin 回退
# ================================================================


class TestImportSubclipRootBinFallback:
    def _subclip(self, asset_id: str = "sc1") -> MediaAsset:
        return _asset(
            asset_id,
            "/media/source.mov",
            metadata={"is_subclip": True},
        )

    def test_fallback_root_bin_kept_and_populated(self) -> None:
        # 无 bins、无 root_bin_id 的旧/损坏 schema 工程
        project = EditProject(project_id="p", name="broken", assets={})

        result = project.import_subclip(self._subclip())

        assert result.root_bin_id is not None
        root_id = result.root_bin_id
        assert root_id in result.bins, "AUD-17103：新建 root bin 不得被覆盖丢弃"
        root_bin = result.bins[root_id]
        assert isinstance(root_bin, ProjectBin)
        assert "sc1" in root_bin.asset_ids
        assert "sc1" in result.assets

    def test_existing_root_bin_used(self) -> None:
        project = EditProject.create("t")
        root_id = project.root_bin_id
        result = project.import_subclip(self._subclip())
        assert result.root_bin_id == root_id
        assert "sc1" in result.bins[root_id].asset_ids


# ================================================================
# AUD-17104：check_media_availability 虚拟素材语义
# ================================================================


class TestCheckMediaAvailability:
    def test_virtual_and_placeholder_assets_available(self, tmp_path) -> None:
        real = tmp_path / "real.mov"
        real.write_bytes(b"data")

        project = EditProject(
            project_id="p", name="t",
            assets={
                "real": _asset("real", str(real)),
                "solid": _asset("solid", "", media_type="solid"),
                "nested": _asset("nested", "", media_type="nested_sequence"),
                "mg": _asset("mg", "topos://motion_graphic/scene_x",
                             media_type="motion_graphic"),
                "comp": _asset("comp", "", media_type="comp_clip"),
                "ph": _asset("ph", "", media_type="placeholder",
                             metadata={"placeholder": True}),
                "gone": _asset("gone", str(tmp_path / "gone.mov")),
            },
            root_bin_id="root",
        )

        availability = check_media_availability(project)

        # AUD-17104：与 is_virtual 语义对齐，placeholder 同样豁免
        for asset_id in ("real", "solid", "nested", "mg", "comp", "ph"):
            assert availability[asset_id] is True, asset_id
        assert availability["gone"] is False


# ================================================================
# AUD-17105：reset_clip_composite 返回契约
# ================================================================


class TestResetClipCompositeContract:
    def test_annotation_declares_union_shape(self) -> None:
        annotation = EditProject.reset_clip_composite.__annotations__.get("return", "")
        assert "tuple" in annotation and "EditProject" in annotation, (
            "AUD-17105：注解必须如实覆盖（EditProject, TimelineSequence) 元组形状"
        )

    def test_early_returns_self(self) -> None:
        project = EditProject.create("t")
        assert project.reset_clip_composite("tl", "clip", timeline=None) is project


# ================================================================
# AUD-17106：duplicate_asset 日志字段继承
# ================================================================


class TestDuplicateAssetLogFields:
    def test_log_fields_and_offline_inherited(self) -> None:
        src = _asset(
            "src1", "/media/a.mov",
            keywords=("interview", "b-roll"),
            rating=4,
            flag="reject",
            color_label="blue",
            comment="handheld take",
            is_offline=True,
            reel="R1", scene="SC2", shot="SH3", take="T4",
        )
        project = EditProject(project_id="p", name="t", assets={"src1": src},
                              root_bin_id="root")

        new_project, new_id = project.duplicate_asset("src1")

        copied = new_project.assets[new_id]
        # AUD-17106：素材日志字段与离线状态随副本继承
        assert copied.keywords == ("interview", "b-roll")
        assert copied.rating == 4
        assert copied.flag == "reject"
        assert copied.color_label == "blue"
        assert copied.comment == "handheld take"
        assert copied.is_offline is True
        assert copied.reel == "R1" and copied.scene == "SC2"
        assert copied.shot == "SH3" and copied.take == "T4"
        # proxy 状态仍重置（保留既有语义）
        assert copied.proxy_status == "none"


# ================================================================
# AUD-17107：_probe_audio_info 容器关闭
# ================================================================


class TestProbeAudioInfoClosesContainer:
    def test_container_closed_on_mid_probe_exception(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        av = pytest.importorskip("av")

        class _ExplodingStreams:
            def __iter__(self):
                raise RuntimeError("corrupt stream table")

        class _FakeContainer:
            def __init__(self) -> None:
                self.streams = _ExplodingStreams()
                self.closed = False

            def close(self) -> None:
                self.closed = True

        fake = _FakeContainer()
        monkeypatch.setattr(av, "open", lambda *_a, **_k: fake)

        result = _probe_audio_info("/media/corrupt.mp3")

        assert result is None
        # AUD-17107：探测中途异常也保证容器（文件句柄）被关闭
        assert fake.closed is True


# ================================================================
# AUD-17201 / AUD-17202 / AUD-17203：project_archive
# ================================================================


def _dir_asset(asset_id: str, path: str) -> MediaAsset:
    return _asset(asset_id, path, media_type="image_sequence")


class TestArchiveDirectoryHash:
    def test_directory_asset_hashed_and_copied(self, tmp_path) -> None:
        src_dir = tmp_path / "seq"
        src_dir.mkdir()
        (src_dir / "frame_0001.png").write_bytes(b"aaa")
        (src_dir / "frame_0002.png").write_bytes(b"bbb")

        project = EditProject(
            project_id="p", name="t",
            assets={"d1": _dir_asset("d1", str(src_dir))},
            root_bin_id="root",
        )
        archived, report = collect_project(project, target_dir=str(tmp_path / "arc"))

        record = report.records[0]
        # AUD-17201：目录素材参与校验，sha256 非空且状态 copied
        assert record.status == STATUS_COPIED
        assert record.sha256 != ""
        assert report.passed

    def test_corrupt_directory_copy_flagged(self, tmp_path, monkeypatch) -> None:
        src_dir = tmp_path / "seq"
        src_dir.mkdir()
        (src_dir / "a.png").write_bytes(b"aaa")

        project = EditProject(
            project_id="p", name="t",
            assets={"d1": _dir_asset("d1", str(src_dir))},
            root_bin_id="root",
        )

        real_copytree = shutil.copytree

        def _tampering_copytree(src: str, dst: str, **kwargs: Any) -> None:
            real_copytree(src, dst, **kwargs)
            with open(os.path.join(dst, "a.png"), "ab") as f:
                f.write(b"CORRUPTED")  # 模拟复制损坏/不完整

        monkeypatch.setattr(shutil, "copytree", _tampering_copytree)

        _archived, report = collect_project(project, target_dir=str(tmp_path / "arc"))

        record = report.records[0]
        # AUD-17201：目录内容差异被哈希捕获，不再 ""=="" 恒真
        assert record.status == STATUS_HASH_MISMATCH
        assert not report.passed

    def test_directory_hash_deterministic_and_sensitive(self, tmp_path) -> None:
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        (a / "1.bin").write_bytes(b"x")
        (b / "1.bin").write_bytes(b"x")
        (b / "2.bin").write_bytes(b"y")

        h1 = project_archive_module._sha256_of(str(a))
        h2 = project_archive_module._sha256_of(str(b))
        c = tmp_path / "c"; c.mkdir()
        (c / "1.bin").write_bytes(b"x")
        assert h1 == project_archive_module._sha256_of(str(c))
        assert h1 != h2


class TestArchivePerFileVerifyTolerance:
    def test_hash_failure_degrades_to_copy_failed_record(
        self, tmp_path, monkeypatch,
    ) -> None:
        (tmp_path / "a.mov").write_bytes(b"aaa")
        (tmp_path / "b.mov").write_bytes(b"bbb")
        project = EditProject(
            project_id="p", name="t",
            assets={
                "a1": _asset("a1", str(tmp_path / "a.mov")),
                "b1": _asset("b1", str(tmp_path / "b.mov")),
            },
            root_bin_id="root",
        )

        real_sha = project_archive_module._sha256_of

        def _flaky_sha(path: str, chunk: int = 1 << 20) -> str:
            if path.endswith("a.mov"):
                raise OSError("file vanished during hashing")
            return real_sha(path, chunk)

        monkeypatch.setattr(project_archive_module, "_sha256_of", _flaky_sha)

        # AUD-17202：单个文件哈希 OSError 不再冲出 collect_project
        archived, report = collect_project(project, target_dir=str(tmp_path / "arc"))

        statuses = {r.asset_id: r.status for r in report.records}
        assert statuses["a1"] == STATUS_COPY_FAILED
        assert statuses["b1"] == STATUS_COPIED
        assert not report.passed
        # 完好素材仍被重写进归档
        assert any("arc" in a.path for a in archived.assets.values())


class TestArchiveProgressAndCancel:
    def test_progress_callback_reports_counts(self, tmp_path) -> None:
        for name in ("a.mov", "b.mov", "c.mov"):
            (tmp_path / name).write_bytes(name.encode())
        project = EditProject(
            project_id="p", name="t",
            assets={n[:-4]: _asset(n[:-4], str(tmp_path / n))
                    for n in ("a.mov", "b.mov", "c.mov")},
            root_bin_id="root",
        )

        seen: list[tuple[int, int]] = []
        _archived, report = collect_project(
            project, target_dir=str(tmp_path / "arc"),
            progress_callback=lambda done, total: seen.append((done, total)),
        )

        assert seen == [(1, 3), (2, 3), (3, 3)]
        assert len(report.copied) == 3

    def test_cancel_returns_partial_report_with_warning(self, tmp_path) -> None:
        for name in ("a.mov", "b.mov"):
            (tmp_path / name).write_bytes(name.encode())
        project = EditProject(
            project_id="p", name="t",
            assets={
                "a1": _asset("a1", str(tmp_path / "a.mov")),
                "b1": _asset("b1", str(tmp_path / "b.mov")),
            },
            root_bin_id="root",
        )

        processed = {"n": 0}

        def _cancel_after_first() -> bool:
            return processed["n"] >= 1

        def _progress(done: int, total: int) -> None:
            processed["n"] = done

        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arc"),
            progress_callback=_progress,
            cancelled_fn=_cancel_after_first,
        )

        # AUD-17203：取消即停，报告带 warning，只收集已完成部分
        assert len(report.copied) == 1
        assert any("cancelled" in w for w in report.warnings)
        copied_id = report.copied[0].asset_id
        assert copied_id in archived.assets
        assert str(tmp_path / "arc") in archived.assets[copied_id].path
        assert "b1" in archived.assets  # 未处理素材保持原路径


# ================================================================
# AUD-17231 / AUD-17232：proxy_generator 清理路径
# ================================================================


def _make_test_video(path: str, frames: int = 6) -> None:
    av = pytest.importorskip("av")
    container = av.open(path, "w")
    try:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width, stream.height = 64, 48
        stream.pix_fmt = "yuv420p"
        for i in range(frames):
            frame = av.VideoFrame(64, 48, "rgb24")
            array = frame.to_ndarray()
            array[:] = (i * 40) % 255
            for packet in stream.encode(frame.from_ndarray(array, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


class TestPyAVCancelCleansPartialProxy:
    def test_cancelled_generation_removes_partial_file(self, tmp_path) -> None:
        pytest.importorskip("av")
        source = str(tmp_path / "source.mp4")
        _make_test_video(source)
        proxy_path = str(tmp_path / "source_proxy.mp4")

        result = proxy_generator_module._generate_video_proxy_pyav(
            asset=_asset("a1", source),
            proxy_path=proxy_path,
            config=proxy_generator_module.DEFAULT_PROXY_CONFIG,
            cancelled_fn=lambda: True,  # 首个 packet 即取消
        )

        assert result is None
        # AUD-17231：取消路径不残留截断的代理文件
        assert not os.path.exists(proxy_path)


class _FakeCliProcess:
    """subprocess.Popen 替身：先超时，kill 后可回收。"""

    def __init__(self, fail_mode: str) -> None:
        self._fail_mode = fail_mode
        self.returncode: int | None = None
        self.killed = False
        self.communicate_calls: list[Any] = []

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls.append(timeout)
        if self._fail_mode == "timeout" and not self.killed:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        self.returncode = 0 if self._fail_mode == "timeout" else 1
        return (b"", b"ffmpeg error output")

    def kill(self) -> None:
        self.killed = True


class TestCliFallbackCleanup:
    def _prepare(self, tmp_path) -> tuple[MediaAsset, str]:
        source = str(tmp_path / "source.mp4")
        with open(source, "wb") as f:
            f.write(b"fake video")
        proxy_path = str(tmp_path / "source_proxy.mp4")
        with open(proxy_path, "wb") as f:
            f.write(b"partial output")  # 模拟超时前的部分写入
        return _asset("a1", source), proxy_path

    def test_timeout_reaps_process_and_removes_partial(
        self, tmp_path, monkeypatch,
    ) -> None:
        asset, proxy_path = self._prepare(tmp_path)

        created: dict[str, _FakeCliProcess] = {}

        def _fake_popen(cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            proc = _FakeCliProcess("timeout")
            created["p"] = proc
            return proc

        monkeypatch.setattr(proxy_generator_module, "find_ffmpeg",
                            lambda: "/fake/ffmpeg")
        monkeypatch.setattr(subprocess, "Popen", _fake_popen)

        result = proxy_generator_module._generate_video_proxy_cli(
            asset, proxy_path, proxy_generator_module.DEFAULT_PROXY_CONFIG,
        )

        proc = created["p"]
        assert result is None
        # AUD-17232：kill 后 communicate 回收子进程
        assert proc.killed is True
        assert len(proc.communicate_calls) == 2
        assert proc.communicate_calls[1] == 5
        # AUD-17232：部分输出被清理
        assert not os.path.exists(proxy_path)

    def test_nonzero_returncode_removes_partial(
        self, tmp_path, monkeypatch,
    ) -> None:
        asset, proxy_path = self._prepare(tmp_path)

        monkeypatch.setattr(proxy_generator_module, "find_ffmpeg",
                            lambda: "/fake/ffmpeg")
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda cmd, **kw: _FakeCliProcess("returncode"),
        )

        result = proxy_generator_module._generate_video_proxy_cli(
            asset, proxy_path, proxy_generator_module.DEFAULT_PROXY_CONFIG,
        )

        assert result is None
        assert not os.path.exists(proxy_path)
