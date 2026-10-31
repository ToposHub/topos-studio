"""批 A Critical 修复回归测试（AUD-16309 / AUD-16500 / AUD-16710）。

- AUD-16309：Consolidate 分段输出文件名去重——同名素材/归档目录已有
  同名文件时追加递增后缀，不再让 ffmpeg ``-y``/shutil 静默覆盖已有媒体
  内容（归档工程指向错误素材画面、sha256 与最终文件不匹配）。
- AUD-16500：close_source/close_all/invalidate_all_sources 三条关闭路径
  接入 P2-09 租约协议——先排水等待在飞租约归零，超时的推迟到租约归还
  时补关闭，绝不解码中途关闭 VT/FFmpeg 会话（C 层 use-after-free）。
- AUD-16710：load_project 反序列化链异常边界——损坏/手改 .topos 包的
  畸形载荷（缺键/类型不符）转为受控加载失败（返回 None + 完整异常日志），
  不再让异常穿出到 Qt 菜单槽使应用崩溃。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

import src.features.edit.core.consolidate as consolidate_module
import src.features.edit.core.edit_gpu_decode_manager as decode_manager_module
from src.features.edit.core.consolidate import (
    MODE_TRANSCODE,
    STATUS_COPIED_IMAGE,
    STATUS_CONSOLIDATED,
    _unique_out_name,
    consolidate_project,
)
from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from tests.edit.test_edit_gpu_decode_manager import _CloseTrackingSource

pytestmark = pytest.mark.edit_core


# ─────────────────────────────────────────────────────────────────────────────
# AUD-16309：分段输出文件名去重
# ─────────────────────────────────────────────────────────────────────────────

class TestAud16309UniqueOutName:
    def test_unique_out_name_in_run_and_on_disk(self, tmp_path: Path) -> None:
        media_root = str(tmp_path)
        used: set[str] = set()

        # 首个名字直接登记
        assert _unique_out_name("a_c1.mov", media_root, used) == "a_c1.mov"
        # 同批同名素材（第二张卡的 DSC_0001）：追加递增后缀
        assert _unique_out_name("a_c1.mov", media_root, used) == "a_c1_2.mov"
        assert _unique_out_name("a_c1.mov", media_root, used) == "a_c1_3.mov"
        # 不同分段序号不冲突
        assert _unique_out_name("a_c2.mov", media_root, used) == "a_c2.mov"

        # 归档目录已有同名文件（对同一 target_dir 重复整理）：同样不覆盖
        (tmp_path / "b_c1.mov").write_bytes(b"old-content")
        assert _unique_out_name("b_c1.mov", media_root, set()) == "b_c1_2.mov"

    def test_same_display_name_images_not_overwritten(self, tmp_path: Path) -> None:
        """两个同名图片素材：分段文件互不覆盖，sha256 与内容一一对应。"""
        src_dir = tmp_path / "in"
        src_dir.mkdir()
        f1 = src_dir / "cardA_still.png"
        f1.write_bytes(b"AAA")
        f2 = src_dir / "cardB_still.png"
        f2.write_bytes(b"BBB")

        a1 = MediaAsset(
            asset_id="img-1", path=str(f1), media_type="image",
            display_name="still.png", metadata={"width": 1920, "height": 1080},
        )
        a2 = MediaAsset(
            asset_id="img-2", path=str(f2), media_type="image",
            display_name="still.png", metadata={"width": 1920, "height": 1080},
        )
        project = _project_with_assets([a1, a2])
        seq = _sequence_using_assets(("img-1", 0), ("img-2", 10))

        out = tmp_path / "out"
        new_project, _new_seqs, report = consolidate_project(
            project, sequences=[seq], target_dir=str(out), handles_frames=0,
        )

        assert report.passed, report.warnings
        copied = [r for r in report.records if r.status == STATUS_COPIED_IMAGE]
        assert len(copied) == 2
        # AUD-16309 核心：同名素材不再共用同一个输出文件
        archived = {r.archived_path for r in copied}
        assert len(archived) == 2

        # 每个素材的分段内容/哈希与其源一一对应（修复前第二个素材会
        # 覆盖第一个的文件，img-1 的记录指向 BBB 内容）
        by_asset = {r.asset_id: r for r in copied}
        for asset_id, content in (("img-1", b"AAA"), ("img-2", b"BBB")):
            rec = by_asset[asset_id]
            seg_path = out / rec.archived_path
            data = seg_path.read_bytes()
            assert data == content
            assert rec.sha256 == hashlib.sha256(data).hexdigest()
            # 归档工程中的分段素材指向正确媒体
            new_asset = new_project.assets[rec.new_asset_id]
            assert Path(new_asset.path).read_bytes() == content

    def test_same_display_name_video_segments_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """视频分段路径（_transcode_segment）同样去重。

        _run_ffmpeg 用桩替换（无需真实 ffmpeg）：每个分段写一个按文件名
        区分的占位内容，即可断言第二个同名素材不再覆盖第一个的分段。
        """
        def _fake_run_ffmpeg(args, timeout=None) -> None:  # type: ignore[no-untyped-def]
            dst = args[-1]
            Path(dst).write_bytes(f"segment:{os.path.basename(dst)}".encode())

        monkeypatch.setattr(consolidate_module, "_run_ffmpeg", _fake_run_ffmpeg)

        src1 = tmp_path / "takeA.mov"
        src1.write_bytes(b"take-a")
        src2 = tmp_path / "takeB.mov"
        src2.write_bytes(b"take-b")
        v1 = _fake_video_asset("vid-1", str(src1), "take.mov")
        v2 = _fake_video_asset("vid-2", str(src2), "take.mov")
        project = _project_with_assets([v1, v2])
        seq = _sequence_using_assets(("vid-1", 0), ("vid-2", 10))

        out = tmp_path / "out"
        _new_project, _new_seqs, report = consolidate_project(
            project, sequences=[seq], target_dir=str(out),
            mode=MODE_TRANSCODE, handles_frames=0,
        )

        assert report.passed, report.warnings
        segs = [r for r in report.records if r.status == STATUS_CONSOLIDATED]
        assert len(segs) == 2
        assert len({r.archived_path for r in segs}) == 2  # 去重生效
        for rec in segs:
            seg_path = out / rec.archived_path
            data = seg_path.read_bytes()
            assert data == f"segment:{seg_path.name}".encode()
            assert rec.sha256 == hashlib.sha256(data).hexdigest()

    def test_rerun_into_same_target_keeps_previous_files(
        self, tmp_path: Path,
    ) -> None:
        """对同一归档目录重复整理：不覆盖上一轮产物（磁盘去重）。"""
        src_dir = tmp_path / "in"
        src_dir.mkdir()
        f1 = src_dir / "still.png"
        f1.write_bytes(b"first-run")

        asset = MediaAsset(
            asset_id="img-1", path=str(f1), media_type="image",
            display_name="still.png", metadata={"width": 1920, "height": 1080},
        )
        project = _project_with_assets([asset])
        seq = _sequence_using_assets(("img-1", 0))
        out = tmp_path / "out"

        consolidate_project(
            project, sequences=[seq], target_dir=str(out), handles_frames=0,
        )
        first = out / "media" / "still_c1.png"
        assert first.read_bytes() == b"first-run"

        # 第二轮整理（同名素材，内容不同）：不覆盖第一轮文件
        f1.write_bytes(b"second-run")
        _np, _ns, report = consolidate_project(
            project, sequences=[seq], target_dir=str(out), handles_frames=0,
        )
        assert report.passed, report.warnings
        assert first.read_bytes() == b"first-run"
        rec = report.records[0]
        assert rec.archived_path != "media/still_c1.png"
        assert (out / rec.archived_path).read_bytes() == b"second-run"


# ─────────────────────────────────────────────────────────────────────────────
# AUD-16500：关闭路径接入 P2-09 租约协议
# ─────────────────────────────────────────────────────────────────────────────

class _BlockingPlanarSource(_CloseTrackingSource):
    """read_next_frame 阻塞到放行事件，模拟正在 C 层解码的长耗时帧。"""

    def __init__(self) -> None:
        super().__init__(current_frame=0)
        self.decode_started = threading.Event()
        self.release_decode = threading.Event()

    def read_next_frame(self) -> _FakeDecodedFrame:  # type: ignore[override]
        self.decode_started.set()
        self.release_decode.wait(timeout=10.0)
        return super().read_next_frame()


def _decode_manager(
    created: list,
    factory=None,
) -> EditGpuDecodeManager:
    """伪造 planar source 工厂的 manager（decode 路径与真实一致）。"""
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]

    def _create_source(asset_path: str, target_size, **kwargs):  # type: ignore[no-untyped-def]
        source = factory() if factory is not None else _CloseTrackingSource(current_frame=0)
        created.append(source)
        return source, "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]
    return manager


def _source_key(manager: EditGpuDecodeManager, asset: str, stream_key: str):
    spec = manager._resolve_preview_decode_source(asset, (640, 360))
    return manager._make_source_key(manager._normalize_stream_key(asset, stream_key), spec)


class TestAud16500ClosePathsRespectLease:

    def test_close_all_closes_idle_sources_immediately(self) -> None:
        """无在飞租约：行为不变（同步关闭）。"""
        created: list = []
        manager = _decode_manager(created)
        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")

        manager.close_all()

        assert created[0].closed
        assert not manager._sources
        assert not manager._source_refs
        assert not manager._deferred_close_sources

    def test_close_all_defers_leased_source_until_release(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(decode_manager_module, "_CLOSE_LEASE_DRAIN_TIMEOUT", 0.05)
        created: list = []
        manager = _decode_manager(created)
        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        key = _source_key(manager, "/a.mov", "clip-a")

        # 模拟解码线程持租约正在 C 层使用该会话
        with manager._lock:
            manager._lease_source(key)

        manager.close_all()

        # AUD-16500：绝不在解码中途 close（use-after-free 窗口）
        assert not created[0].closed
        assert key not in manager._sources
        assert key not in manager._source_cache
        assert key in manager._deferred_close_sources

        # 最后一个租约归还 → 锁外补关闭
        manager._release_source(key)
        assert created[0].closed
        assert key not in manager._deferred_close_sources
        assert key not in manager._source_refs

    def test_invalidate_all_defers_leased_source_until_release(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(decode_manager_module, "_CLOSE_LEASE_DRAIN_TIMEOUT", 0.05)
        created: list = []
        manager = _decode_manager(created)
        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        key = _source_key(manager, "/a.mov", "clip-a")

        with manager._lock:
            manager._lease_source(key)

        manager.invalidate_all_sources()

        assert not created[0].closed
        assert key in manager._deferred_close_sources

        manager._release_source(key)
        assert created[0].closed
        assert not manager._deferred_close_sources

    def test_close_source_defers_leased_source_until_release(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(decode_manager_module, "_CLOSE_LEASE_DRAIN_TIMEOUT", 0.05)
        created: list = []
        manager = _decode_manager(created)
        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        key = _source_key(manager, "/a.mov", "clip-a")

        with manager._lock:
            manager._lease_source(key)

        manager.close_source("/a.mov")

        assert not created[0].closed
        assert key in manager._deferred_close_sources
        # 素材级缓存仍被清理（下次访问重新创建会话）
        assert key not in manager._sources
        assert key not in manager._source_cache

        manager._release_source(key)
        assert created[0].closed
        assert not manager._deferred_close_sources

    def test_close_all_during_in_flight_decode_does_not_close_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """端到端：解码线程阻塞在源内时 close_all 推迟关闭，归还后补关。"""
        monkeypatch.setattr(decode_manager_module, "_CLOSE_LEASE_DRAIN_TIMEOUT", 0.05)
        created: list = []
        source = _BlockingPlanarSource()

        def _factory() -> _BlockingPlanarSource:
            return source

        manager = _decode_manager(created, factory=_factory)
        key = _source_key(manager, "/a.mov", "clip-a")

        decode_done: list = []

        def _decode() -> None:
            frame, frame_type = manager.get_frame(
                "/a.mov", 0, target_size=(640, 360), stream_key="clip-a",
            )
            decode_done.append((frame, frame_type))

        worker = threading.Thread(target=_decode, name="TestDecodeWorker")
        worker.start()
        assert source.decode_started.wait(timeout=5.0)

        # 解码在飞（持租约）：close_all 不得关闭正在使用的会话
        manager.close_all()
        assert not source.closed
        assert key in manager._deferred_close_sources

        source.release_decode.set()
        worker.join(timeout=5.0)
        assert not worker.is_alive()
        assert decode_done and decode_done[0][0] is not None
        # 解码线程 finally 归还租约 → 推迟单元在锁外补关闭
        assert source.closed
        assert not manager._deferred_close_sources

    def test_stale_deferred_unit_swept_on_next_close_all(self) -> None:
        """上一轮推迟、租约已归零的单元由下一次 close_all 收走。"""
        manager = EditGpuDecodeManager()
        key = ("clip", "/a.mov", "/a.mov", (640, 360), "auto")
        stale = _CloseTrackingSource(current_frame=0)
        manager._deferred_close_sources[key] = [stale]

        manager.close_all()

        assert stale.closed
        assert not manager._deferred_close_sources
        assert not manager._source_refs


# ─────────────────────────────────────────────────────────────────────────────
# AUD-16710：load_project 反序列化异常边界
# ─────────────────────────────────────────────────────────────────────────────

def _read_zip_member(pkg: Path, member: str) -> bytes:
    with zipfile.ZipFile(pkg) as zf:
        return zf.read(member)


def _rewrite_zip_member(pkg: Path, member: str, payload: dict) -> None:
    """手改 .topos 包：替换/注入一个 JSON 成员（保留其余成员不变）。"""
    with zipfile.ZipFile(pkg) as zf:
        items = {info.filename: zf.read(info.filename) for info in zf.infolist()}
    items[member] = json.dumps(payload).encode("utf-8")
    tmp = pkg.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in items.items():
            zf.writestr(name, data)
    os.replace(tmp, pkg)


class TestAud16710LoadProjectDeserializeBoundary:

    @staticmethod
    def _save_minimal_project(tmp_path: Path) -> Path:
        asset = MediaAsset(
            asset_id="a-1", path=str(tmp_path / "media.mp4"),
            media_type="video", display_name="media.mp4", metadata={},
        )
        root = ProjectBin(
            bin_id="root", name="Root", parent_id=None, asset_ids=("a-1",),
        )
        project = EditProject(
            project_id="p1", name="BoundaryTest",
            assets={"a-1": asset}, bins={"root": root}, root_bin_id="root",
        )
        io = EditProjectIO()
        out = tmp_path / "pkg.topos"
        assert io.save_project(str(out), project)
        return out

    def test_valid_package_still_loads(self, tmp_path: Path, qapp) -> None:
        pkg = self._save_minimal_project(tmp_path)
        loaded = EditProjectIO().load_project(str(pkg))
        assert loaded is not None
        assert loaded[0].name == "BoundaryTest"

    def test_corrupt_asset_rating_returns_none_not_crash(
        self, tmp_path: Path, qapp,
    ) -> None:
        """手改 rating 为字符串：int() ValueError 转为受控失败。"""
        pkg = self._save_minimal_project(tmp_path)
        media = json.loads(_read_zip_member(pkg, "edit/media_pool.json"))
        media[next(iter(media))]["rating"] = "not-a-number"
        _rewrite_zip_member(pkg, "edit/media_pool.json", media)

        assert EditProjectIO().load_project(str(pkg)) is None

    def test_corrupt_timeline_marker_returns_none_not_crash(
        self, tmp_path: Path, qapp,
    ) -> None:
        """手改 marker 缺 frame：TimelineMarker.from_dict KeyError 转为受控失败。"""
        corrupt_seq = {
            "seq-1": {
                "timeline_id": "seq-1",
                "name": "Main",
                "markers": [{"marker_id": "m1"}],  # 缺 "frame" → data["frame"] KeyError
            },
        }
        # 佐证：该载荷确实在反序列化链上抛 KeyError（而非被上游过滤）
        with pytest.raises(KeyError):
            TimelineSequence.from_dict(corrupt_seq["seq-1"])

        pkg = self._save_minimal_project(tmp_path)
        _rewrite_zip_member(pkg, "edit/timeline_sequences.json", corrupt_seq)

        assert EditProjectIO().load_project(str(pkg)) is None

    def test_corrupt_ui_state_shape_returns_none_not_crash(
        self, tmp_path: Path, qapp,
    ) -> None:
        """手改 ui_state 为 JSON 数组：AttributeError 同样被边界拦截。"""
        pkg = self._save_minimal_project(tmp_path)
        # 顶层不是对象：UIState.from_dict(list).get → AttributeError
        _rewrite_zip_member(pkg, "ui/workspace_state.json", [1, 2, 3])

        assert EditProjectIO().load_project(str(pkg)) is None


# ─────────────────────────────────────────────────────────────────────────────
# 测试辅助（与 test_consolidate.py 同构）
# ─────────────────────────────────────────────────────────────────────────────

def _fake_video_asset(asset_id: str, path: str, display_name: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=path, media_type="video",
        display_name=display_name,
        metadata={"fps": 24.0, "width": 1920, "height": 1080,
                  "total_frames": 240, "duration": 10.0},
    )


def _project_with_assets(assets: list[MediaAsset]) -> EditProject:
    root = ProjectBin(
        bin_id="root", name="Root", parent_id=None,
        asset_ids=tuple(a.asset_id for a in assets),
    )
    return EditProject(
        project_id="p1", name="Aud3Test",
        assets={a.asset_id: a for a in assets},
        bins={"root": root}, root_bin_id="root",
    )


def _sequence_using_assets(*asset_starts: tuple[str, int]) -> TimelineSequence:
    """每个素材一个 clip，时间线位置错开（互不重叠）。"""
    clips = []
    pos = 0
    for asset_id, start in asset_starts:
        clips.append(ClipInstance(
            clip_id=f"clip-{asset_id}-{start}",
            asset_id=asset_id,
            track_id="v1",
            timeline_start=pos,
            timeline_duration=24,
            source_start=start,
            source_duration=24,
        ))
        pos += 24 + 10
    track = EditTrack(track_id="v1", name="V1", track_type="video", clips=tuple(clips))
    return TimelineSequence(
        timeline_id="seq-1", name="Main", fps=Rational(24, 1),
        video_tracks=(track,),
    )
