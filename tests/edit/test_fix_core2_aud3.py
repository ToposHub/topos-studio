"""批 A Critical 修复回归测试（AUD-17100 / AUD-17600 / AUD-18400）。

AUD-17100：update_comp_clip 不得原地修改与旧工程快照共享的合成图节点
dict——undo 历史中的旧快照必须保持更新前的值。

AUD-17600：磁盘耗尽安全终止路径（_on_audio_ready → _finalize_take）
不得在持有非重入 _buffer_lock 时执行——同线程二次加锁会死锁挂死。

AUD-18400：CPU 效果处理器（sharpen/denoise/edge_detect）必须按帧
dtype 满刻度归一（uint8/255、uint16/65535、float 不缩放）——旧的
硬编码 /255 往返使高位深导出帧整帧饱和/退化 uint8。
"""

from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject
from src.features.edit.core.recording_manager import (
    _ChunkedPcmBuffer,
    RecordingConfig,
    RecordingManager,
    _DISK_FAIL_BYTES,
    _DISK_WARN_BYTES,
)
from src.features.edit.core.timeline_export import (
    _cpu_denoise,
    _cpu_edge_detect,
    _cpu_sharpen,
)


# ================================================================
# AUD-17100 — update_comp_clip 快照隔离
# ================================================================


def _find_node(graph_data: dict, node_type: str) -> dict:
    for node in graph_data.get("nodes", []):
        if node.get("type") == node_type:
            return node
    raise AssertionError(f"graph has no '{node_type}' node")


class TestUpdateCompClipSnapshotIsolation:
    """update_comp_clip 必须逐节点 copy-on-write，旧快照零突变。"""

    def test_old_snapshot_graph_not_mutated(self):
        project = EditProject.create("AUD-17100")
        project, comp_clip_id, _, graph_id = project.create_empty_comp_clip(
            width=1920, height=1080,
            background_color=(0.1, 0.2, 0.3, 1.0),
        )
        old_graph = project.compositions[graph_id]
        old_constant = _find_node(old_graph, "constant")
        old_output = _find_node(old_graph, "output")
        old_constant_params = old_constant["parameters"]

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id,
            width=3840,
            height=2160,
            background_color=(0.9, 0.1, 0.1, 1.0),
        )

        # 旧工程（undo 栈中的快照）保持更新前的值
        assert old_graph["width"] == 1920
        assert old_graph["height"] == 1080
        assert old_constant_params["width"] == 1920
        assert old_constant_params["height"] == 1080
        assert old_constant_params["color_r"] == 0.1
        assert old_constant_params["color_g"] == 0.2
        assert old_constant_params["color_b"] == 0.3
        assert old_output["parameters"].get("width") == 1920
        assert old_output["parameters"].get("height") == 1080

        # 新工程拿到更新后的值（行为与修复前一致）
        new_graph = new_project.compositions[graph_id]
        new_constant = _find_node(new_graph, "constant")
        new_output = _find_node(new_graph, "output")
        assert new_graph["width"] == 3840
        assert new_constant["parameters"]["width"] == 3840
        assert new_constant["parameters"]["height"] == 2160
        assert new_constant["parameters"]["color_r"] == 0.9
        assert new_output["parameters"]["width"] == 3840
        assert new_output["parameters"]["format_mode"] == "custom"

    def test_modified_node_dicts_are_not_shared_objects(self):
        """被修改的节点必须重建为新 dict，而不是原地改写共享对象。"""
        project = EditProject.create("AUD-17100 identity")
        project, comp_clip_id, _, graph_id = project.create_empty_comp_clip(
            width=640, height=480,
        )
        old_nodes = project.compositions[graph_id]["nodes"]
        old_node_ids = {id(node) for node in old_nodes}

        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id, width=1280, height=720,
        )
        new_nodes = new_project.compositions[graph_id]["nodes"]

        # constant/output 均被修改 → 必须是新对象
        for node in new_nodes:
            if node.get("type") in ("constant", "output"):
                assert id(node) not in old_node_ids, (
                    f"{node.get('type')} node dict is still the object "
                    "shared with the old project snapshot"
                )
        # 旧快照的 nodes 列表对象本身也未被改写（仍是原列表内容）
        assert all(
            node.get("parameters", {}).get("width", 640) == 640
            for node in old_nodes
        )

    def test_name_only_update_keeps_resolution(self):
        """只改名不改分辨率时，节点分辨率参数不被破坏。"""
        project = EditProject.create("AUD-17100 name")
        project, comp_clip_id, _, graph_id = project.create_empty_comp_clip(
            width=1920, height=1080,
        )
        new_project = project.update_comp_clip(
            comp_clip_id=comp_clip_id, name="Renamed Comp",
        )
        new_graph = new_project.compositions[graph_id]
        assert new_graph["name"] == "Renamed Comp"
        constant = _find_node(new_graph, "constant")
        output = _find_node(new_graph, "output")
        assert constant["parameters"]["width"] == 1920
        assert output["parameters"]["width"] == 1920


# ================================================================
# AUD-17600 — 磁盘耗尽路径不得持锁 finalize（死锁回归）
# ================================================================


class _FakeQByteArray(bytes):
    """够用的 QByteArray 替身：_on_audio_ready 只用 size() 与 bytes()。"""

    def size(self) -> int:  # type: ignore[override]
        return len(self)


class _FakeIoDevice:
    """够用的 QIODevice 替身：只实现 readAll。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readAll(self) -> _FakeQByteArray:
        return _FakeQByteArray(self._payload)


class _FakeUsage:
    def __init__(self, free: int) -> None:
        self.free = free


def _make_recording_manager(tmp_path: Path) -> RecordingManager:
    cfg = RecordingConfig(
        output_dir=str(tmp_path), scene_name="AUD17600", pre_roll_seconds=0,
    )
    return RecordingManager(cfg)


def _seed_small_buffer(mgr: RecordingManager, tmp_path: Path) -> None:
    """替换为小内存上限的缓冲，使下一次 append 触发溢写预检。

    保留 start_recording 注册的磁盘预警回调（入队语义），
    与真实录音路径的缓冲构造一致。
    """
    mgr._pcm_buffer = _ChunkedPcmBuffer(
        channels=1, spool_dir=str(tmp_path), memory_limit_bytes=1024,
        on_disk_warning=mgr._pending_disk_warnings.append,
    )
    mgr._pcm_buffer.append(np.zeros((1, 256), dtype=np.float32))


class TestDiskExhaustionNoDeadlock:
    """磁盘耗尽 → 安全终止路径必须在锁外 finalize。"""

    def test_finalize_take_runs_outside_buffer_lock(self, tmp_path, monkeypatch):
        """DiskSpaceExhausted → 线程能完成（不死锁）且保留已录数据。"""
        mgr = _make_recording_manager(tmp_path)
        errors: list[str] = []
        mgr.on_error = errors.append
        mgr.start_recording()
        _seed_small_buffer(mgr, tmp_path)

        # 剩余空间低于 _DISK_FAIL_BYTES → append 时抛 DiskSpaceExhausted
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda path: _FakeUsage(_DISK_FAIL_BYTES // 2),
        )
        mgr._io_device = _FakeIoDevice(
            np.zeros((1, 4096), dtype=np.float32).tobytes())

        # 旧实现在持有 _buffer_lock 时调用 _finalize_take（再次加锁）
        # → 同线程死锁；用带超时的线程执行，回归时 is_alive() 为 True。
        worker = threading.Thread(target=mgr._on_audio_ready)
        worker.start()
        worker.join(timeout=15.0)
        assert not worker.is_alive(), (
            "_on_audio_ready deadlocked: disk-exhaustion path still "
            "finalizes while holding the non-reentrant _buffer_lock"
        )

        # on_error 已上报（磁盘耗尽消息），take 以 completed_partial 保留
        assert any("安全终止" in msg or "磁盘" in msg for msg in errors)
        assert mgr._current_take is None
        assert len(mgr.takes) == 1
        assert mgr.takes[0].status == "completed_partial"
        assert Path(mgr.takes[0].file_path).exists()

    def test_disk_warning_error_callback_not_invoked_under_lock(
            self, tmp_path, monkeypatch):
        """磁盘预警经 on_error 转发时不得持有 _buffer_lock（外部回调禁持锁）。"""
        mgr = _make_recording_manager(tmp_path)
        lock_during_callback: list[bool] = []
        errors: list[str] = []

        def probing_on_error(msg: str) -> None:
            acquired = mgr._buffer_lock.acquire(blocking=False)
            if acquired:
                mgr._buffer_lock.release()
            lock_during_callback.append(not acquired)
            errors.append(msg)

        mgr.on_error = probing_on_error
        mgr.start_recording()
        _seed_small_buffer(mgr, tmp_path)

        # warn~fail 区间 → 只预警不终止
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda path: _FakeUsage((_DISK_WARN_BYTES + _DISK_FAIL_BYTES) // 2),
        )
        mgr._io_device = _FakeIoDevice(
            np.zeros((1, 4096), dtype=np.float32).tobytes())

        worker = threading.Thread(target=mgr._on_audio_ready)
        worker.start()
        worker.join(timeout=15.0)
        assert not worker.is_alive(), "warning path deadlocked under _buffer_lock"

        # 预警被转发，且回调执行时锁是可获取的（未被持有）
        assert errors, "disk warning was queued but never drained to on_error"
        assert lock_during_callback and not any(lock_during_callback)
        # 预警不终止录音
        assert mgr.state == "recording"
        assert mgr._current_take is not None


# ================================================================
# AUD-18400 — CPU 效果处理器按 dtype 分流
# ================================================================


class TestCpuEffectHighBitDepth:
    """sharpen/denoise/edge_detect 的 uint16/float 路径。"""

    def test_sharpen_uint16_midgray_not_saturated(self):
        """uint16 中灰经 sharpen 后保持中灰（旧实现整帧饱和为白）。"""
        frame = np.full((8, 8, 3), 32768, dtype=np.uint16)
        out = _cpu_sharpen(frame, {"amount": 1.0, "radius": 1.0})
        assert out.dtype == np.uint16
        # 平坦帧 diff=0 → 输出应回到原码值（±1 量化误差）
        np.testing.assert_allclose(
            out.astype(np.float64), 32768.0, atol=1)

    def test_sharpen_uint8_path_unchanged(self):
        """uint8 路径保持现状：中灰 128 → 128，dtype uint8。"""
        frame = np.full((8, 8, 3), 128, dtype=np.uint8)
        out = _cpu_sharpen(frame, {"amount": 1.0, "radius": 1.0})
        assert out.dtype == np.uint8
        np.testing.assert_array_equal(out, np.full_like(frame, 128))

    def test_sharpen_float_input_not_rescaled(self):
        """float 输入不额外缩放：[0,1] 平坦帧原样返回，dtype 保留。"""
        frame = np.full((6, 6, 3), 0.5, dtype=np.float32)
        out = _cpu_sharpen(frame, {"amount": 1.0, "radius": 1.0})
        assert out.dtype == np.float32
        np.testing.assert_allclose(out, 0.5, atol=1e-6)

    def test_denoise_uint16_midgray_not_saturated(self):
        """uint16 中灰经 denoise 后保持中灰（旧实现输出全白）。"""
        frame = np.full((8, 8, 3), 32768, dtype=np.uint16)
        out = _cpu_denoise(frame, {"strength": 0.6, "radius": 2})
        assert out.dtype == np.uint16
        np.testing.assert_allclose(
            out.astype(np.float64), 32768.0, atol=1)

    def test_denoise_uint16_alpha_handled_in_frame_dtype(self):
        """BGRA uint16：luma-only 模式 alpha 原样保留；全通道模式 alpha 参与。"""
        rng = np.random.default_rng(2026)
        frame = rng.integers(0, 65536, (9, 11, 4), dtype=np.uint16)
        luma_only = _cpu_denoise(frame, {
            "strength": 0.7, "radius": 2, "luminance_only": True,
        })
        assert luma_only.dtype == np.uint16
        np.testing.assert_array_equal(luma_only[:, :, 3], frame[:, :, 3])

        full = _cpu_denoise(frame, {
            "strength": 0.7, "radius": 2,
        })
        assert full.dtype == np.uint16
        assert not np.array_equal(full[:, :, 3], frame[:, :, 3])

    def test_edge_detect_uint16_edge_in_full_scale(self):
        """uint16 硬边缘检出为满刻度 65535（而非 uint8 的 255）。"""
        frame = np.zeros((8, 8, 3), dtype=np.uint16)
        frame[:, 4:] = 65535
        out = _cpu_edge_detect(frame, {
            "method": "sobel", "threshold": 0.1, "channel": "luminance",
        })
        assert out.dtype == np.uint16
        assert np.all(out[:, 3:5] == 65535)
        assert np.all(out[:, :2] == 0)
        assert np.all(out[:, 6:] == 0)

    def test_edge_detect_uint16_subtle_gradient_not_clipped(self):
        """uint16 微小梯度（/255 会溢出 >1）按满刻度保留，不被钳到 255。"""
        frame = np.zeros((6, 6, 3), dtype=np.uint16)
        frame[:, 3:] = 30500
        frame[:, :3] = 30000
        out = _cpu_edge_detect(frame, {
            "method": "sobel", "output_mode": "gradient",
            "channel": "luminance",
        })
        assert out.dtype == np.uint16
        # 归一化梯度 = 500/65535 → 边缘列约 500 码值；旧实现 clip(1.96)→255
        edge_value = int(out[:, 2:4].max())
        assert 400 <= edge_value <= 600, edge_value
        assert np.all(out[:, 0:1] == 0)

    def test_edge_detect_uint8_path_unchanged(self):
        """uint8 硬边缘检出仍为 255（既有行为）。"""
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        frame[:, 4:] = 255
        out = _cpu_edge_detect(frame, {
            "method": "sobel", "threshold": 0.1, "channel": "luminance",
        })
        assert out.dtype == np.uint8
        assert np.all(out[:, 3:5] == 255)
        assert np.all(out[:, 6:] == 0)

    def test_effect_chain_keeps_uint16_dtype_end_to_end(self):
        """_apply_clip_effects_cpu 链式调用后 uint16 帧不退化为 uint8。"""
        from unittest.mock import MagicMock

        from src.features.edit.core.timeline_export import TimelineExporter

        exporter = TimelineExporter.__new__(TimelineExporter)
        eff = MagicMock()
        eff.effect_type = "sharpen"
        eff.is_enabled = True
        eff.order = 0
        params = {"amount": 1.0, "radius": 1.0}
        eff.params = params
        eff.get_param_at_frame = lambda name, frame: params.get(name)
        clip = MagicMock()
        clip.effects = (eff,)
        layer = MagicMock()
        layer.clip = clip

        frame = np.full((8, 8, 3), 32768, dtype=np.uint16)
        out = exporter._apply_clip_effects_cpu(frame, layer, 0)
        assert out.dtype == np.uint16
        np.testing.assert_allclose(
            out.astype(np.float64), 32768.0, atol=1)
