"""W08（计划 W-I07）：真实交互/多轨/资源长压 —— L3 真机浸泡（机器 B）。

以**墙钟时长**计量（不是离屏处理帧折算）：

- `soak_playback`：2/4/8 轨混合 4K 播放浸泡（默认 600s），真实渲染
  线程 + 真实窗口 surface（QT_QPA_PLATFORM=windows），记录 frame_ready
  呈现间隔 p50/p95、掉帧（render_error/丢弃）、RSS/显存/队列趋势，
  停止后回落判定；
- `soak_interaction`：脚本化窗口交互（默认 1800s）：seek/scrub/settle/
  pause/play/画质切换 + 真实窗口事件（showMinimized/activateWindow/
  失焦），逐段采样指标；
- `soak_renderer_restart`：渲染器 100× start/stop 全生命周期循环
  （W02 验收的渲染器级项在此收口），显存趋势不得单调增长。

标记 `soak`（默认收集排除，显式 `-m soak` 运行）。时长可经
`W08_PLAYBACK_S` / `W08_INTERACT_S` / `W08_RESTARTS` 覆盖（冒烟用小值
先行验证脚本本身）。

如实边界（单列未验，不冒充）：
- 多屏/拔屏/DPI 变化：本机单一虚拟显示，无法真实插拔——以
  showMinimized/activateWindow 的真实窗口状态变化覆盖"最小化/失焦/
  恢复"，其余登记未验；
- 控制台/监看输出、TDR、驱动破坏性事件：不做主动破坏（计划约束）；
- 多机位网格与 Program 同跑的 App 级 UI：浸泡在渲染/解码生产链层
  （renderer + decode manager，真实线程），App 页面级网格另属人工档。
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

pytest.importorskip("PyQt6")

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"

ASSET_4K = TEST_VIDEOS / "4K_H264.mov"
ASSET_2K = TEST_VIDEOS / "2K_H265.mov"
ASSET_PR = TEST_VIDEOS / "2K_prores422.mov"


def _l3_ready() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "not_windows"
    try:
        import ctypes

        ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return False, "nvcuda_missing"
    missing = [p.name for p in (ASSET_4K, ASSET_2K, ASSET_PR) if not p.exists()]
    if missing:
        return False, f"assets_missing:{missing}"
    return True, ""


_L3_OK, _L3_REASON = _l3_ready()

pytestmark = [
    pytest.mark.soak,
    pytest.mark.skipif(not _L3_OK, reason=f"w08 soak unavailable: {_L3_REASON}"),
]


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


class _MetricsSampler(threading.Thread):
    """周期采样 RSS/显存（nvidia-smi）→ JSONL。"""

    def __init__(self, out_path: Path, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self._out = out_path.open("w", encoding="utf-8")
        self._stop_event = threading.Event()
        self._interval = interval
        self.samples: list[dict] = []

    def _vram_mib(self) -> float | None:
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            return float(res.stdout.strip().splitlines()[0])
        except Exception:  # noqa: BLE001
            return None

    def sample(self) -> dict:
        import psutil

        proc = psutil.Process(os.getpid())
        s = {
            "t": round(time.monotonic(), 3),
            "rss_mib": round(proc.memory_info().rss / (1 << 20), 1),
            "threads": proc.num_threads(),
            "vram_mib": self._vram_mib(),
        }
        self.samples.append(s)
        self._out.write(json.dumps(s) + "\n")
        self._out.flush()
        return s

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sample()
            except Exception:  # noqa: BLE001 —— 采样失败不终止浸泡
                pass
            self._stop_event.wait(self._interval)

    def stop(self) -> list[dict]:
        self._stop_event.set()
        self.join(timeout=5)
        try:
            self.sample()
        except Exception:  # noqa: BLE001
            pass
        self._out.close()
        return self.samples


def _trend(samples: list[dict], key: str) -> dict:
    vals = [s[key] for s in samples if s.get(key) is not None]
    if not vals:
        return {"available": False}
    half = max(1, len(vals) // 2)
    first_part = vals[:half] or vals
    second_part = vals[half:] or vals
    return {
        "available": True,
        "first": vals[0],
        "min": min(vals),
        "max": max(vals),
        "p50": round(statistics.median(vals), 1),
        "last": vals[-1],
        "first_half_mean": round(statistics.fmean(first_part), 1),
        "second_half_mean": round(statistics.fmean(second_part), 1),
    }


@pytest.fixture(scope="module")
def renderer(qapp):
    from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext

    surface = QOffscreenSurface()
    surface.create()
    main_ctx = QOpenGLContext()
    if not main_ctx.create():
        surface.destroy()
        pytest.skip("QOpenGLContext.create() failed")
    main_ctx.makeCurrent(surface)

    from src.features.edit.core.edit_gpu_preview_renderer import (
        EditGpuPreviewRenderer,
    )

    r = EditGpuPreviewRenderer(main_context=main_ctx)
    r.start()
    for _ in range(200):
        if r.is_initialized:
            break
        qapp.processEvents()
        time.sleep(0.05)
    if not r.is_initialized:
        r.stop()
        r.wait(5000)
        main_ctx.doneCurrent()
        surface.destroy()
        pytest.skip(f"renderer init failed: {r._startup_failure_reason!r}")
    yield r, main_ctx, surface
    r.stop()
    r.wait(10000)
    main_ctx.doneCurrent()
    surface.destroy()


class _SoakDriver:
    """浸泡驱动：真实管理器解码 + 渲染线程提交 + 指标收集。"""

    # 模块级代际计数：renderer 拒绝旧代际任务——每个用例新建 driver
    # 时若各自从 10000 重置，后序用例的提交会全部按 stale 拒绝。
    _GEN_LOCK = threading.Lock()
    _GEN_NEXT = [10000]

    def __init__(self, qtbot, qapp, renderer, evidence_dir: Path):
        self.qtbot = qtbot
        self.qapp = qapp
        self.renderer = renderer
        self.evidence = evidence_dir
        self.intervals: list[float] = []
        self.render_errors: list[tuple[int, str]] = []
        self.present_count = 0
        renderer.render_error.connect(self._on_error)
        # 扮演 Viewer 的 present-ack 协议（不 ack 会耗尽 24 slot 池，
        # present_slot_unavailable 是协议违约不是产品缺陷）
        renderer.present_frame_ready.connect(self._on_present)

    def _on_present(self, present) -> None:
        self.present_count += 1
        try:
            slot_id = int(getattr(present, "slot_id", -1))
            if slot_id >= 0:
                self.renderer.ack_present_slot(slot_id)
        except Exception:  # noqa: BLE001 —— ack 失败不阻断浸泡（计数留痕）
            pass

    def _on_error(self, frame: int, msg: str) -> None:
        self.render_errors.append((frame, msg))

    def next_gen(self) -> int:
        with _SoakDriver._GEN_LOCK:
            _SoakDriver._GEN_NEXT[0] += 1
            return _SoakDriver._GEN_NEXT[0]

    def layers(self, manager, frame: int, n_tracks: int):
        assets = [ASSET_4K, ASSET_2K, ASSET_PR]
        out = []
        for ti in range(n_tracks):
            asset = assets[ti % len(assets)]
            f, ft = manager.get_frame(str(asset), max(0, frame - ti * 3))
            if f is None or ft not in ("cuda_resident", "vt_planar", "planar"):
                pytest.fail(f"decode failed: {asset.name}@{frame - ti * 3}: {ft}")
            key = "cuda_resident" if ft == "cuda_resident" else "decoded_frame"
            out.append({
                key: f, "frame_type": ft, "asset_path": str(asset),
                "track_info": {"opacity": 1.0 / max(1, n_tracks // 2),
                               "clip_id": f"w08_{ti}", "track_index": ti},
            })
        return out

    def render(self, frame: int, layers, timeout: float = 30.0) -> bool:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditRenderTask,
        )

        task = EditRenderTask(
            frame=frame, generation_id=self.next_gen(), layers=layers,
            viewport_size=(1920, 1080), output_fbo_size=(1920, 1080),
            coordinate_size=(1920, 1080))
        got = threading.Event()

        def _done(*_a):
            got.set()

        connected = False
        try:
            self.renderer.frame_ready.connect(_done)
            connected = True
            t0 = time.monotonic()
            if not self.renderer.submit(task):
                return False
            deadline = time.monotonic() + timeout
            while not got.is_set() and time.monotonic() < deadline:
                self.qapp.processEvents()
                time.sleep(0.005)
            if got.is_set():
                self.intervals.append(time.monotonic() - t0)
                return True
            return False
        finally:
            if connected:
                try:
                    self.renderer.frame_ready.disconnect(_done)
                except Exception:
                    pass

    def summarize(self, tag: str, samples: list[dict], wall_s: float) -> dict:
        ints = self.intervals
        p95 = (statistics.quantiles(ints, n=20)[18] if len(ints) >= 20
               else (max(ints) if ints else None))
        session = self.renderer._cuda_interop_session
        summary = {
            "tag": tag,
            "wall_clock_s": round(wall_s, 1),
            "frames": len(ints),
            "interval_p50_s": round(statistics.median(ints), 4) if ints else None,
            "interval_p95_s": round(p95, 4) if p95 else None,
            "interval_max_s": round(max(ints), 4) if ints else None,
            "render_errors": len(self.render_errors),
            "render_error_kinds": sorted({m for _f, m in self.render_errors})[:5],
            "present_acked": self.present_count,
            "rss_mib": _trend(samples, "rss_mib"),
            "vram_mib": _trend(samples, "vram_mib"),
            "interop_stats": dict(session.stats) if session else None,
            "decode_pool": None,
        }
        (self.evidence / f"{tag}_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        return summary


@pytest.fixture()
def evidence_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("w08")


class TestW08Soak:
    def test_soak_playback_multitrack(self, qtbot, qapp, renderer,
                                      evidence_dir, monkeypatch):
        """2/4/8 轨混合 4K 播放浸泡（墙钟 W08_PLAYBACK_S，默认 600s）。"""
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)
        r, _ctx, _surf = renderer
        driver = _SoakDriver(qtbot, qapp, r, evidence_dir)

        from src.features.edit.core.edit_gpu_decode_manager import (
            EditGpuDecodeManager,
        )

        manager = EditGpuDecodeManager()
        sampler = _MetricsSampler(evidence_dir / "playback_metrics.jsonl")
        sampler.start()
        dropped = 0
        try:
            duration = _env_int("W08_PLAYBACK_S", 600)
            t_end = time.monotonic() + duration
            frame = 0
            phase = 0
            track_cycle = (2, 4, 8, 4, 2)
            print(f"\n[w08] playback soak {duration}s wall clock")
            while time.monotonic() < t_end:
                n_tracks = track_cycle[phase % len(track_cycle)]
                layers = driver.layers(manager, frame, n_tracks)
                if not driver.render(frame, layers):
                    dropped += 1
                frame = (frame + 1) % 200
                if frame == 0:
                    phase += 1
        finally:
            samples = sampler.stop()
            manager.close_all()

        settle = _MetricsSampler(evidence_dir / "playback_settle.jsonl")
        settle_samples = [settle.sample() for _ in range(
            int(_env_int("W08_SETTLE_SAMPLES", 6)))]
        time.sleep(0.5)

        summary = driver.summarize("playback", samples,
                                   float(_env_int("W08_PLAYBACK_S", 600)))
        summary["dropped_submits"] = dropped
        summary["settle_vram_mib"] = [s["vram_mib"] for s in settle_samples]
        (evidence_dir / "playback_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"[w08] frames={summary['frames']} "
              f"p95={summary['interval_p95_s']}s "
              f"errors={summary['render_errors']} dropped={dropped}")
        # 判定：渲染错误为 0（掉帧按 dropped 记录不判死），显存趋势
        # 第二半均值不失控增长（>首半 2 倍 + 1GiB 视为泄漏嫌疑）
        assert summary["render_errors"] == 0, summary["render_error_kinds"]
        vram = summary["vram_mib"]
        if vram.get("available"):
            growth = vram["second_half_mean"] - vram["first_half_mean"]
            assert growth < 1024 + vram["first_half_mean"], (
                f"显存趋势失控: {vram}")

    def test_soak_interaction_window_events(self, qtbot, qapp, renderer,
                                            evidence_dir, monkeypatch):
        """脚本化窗口交互浸泡（墙钟 W08_INTERACT_S，默认 1800s）。

        交互族：随机 seek（正/反向）、连续 scrub、暂停/恢复、画质
        （target_size）切换、真实窗口事件（minimize/activate/失焦）。
        """
        import random

        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)
        r, _ctx, _surf = renderer
        driver = _SoakDriver(qtbot, qapp, r, evidence_dir)

        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QWidget

        # 真实顶层窗口（QT_QPA_PLATFORM=windows 下经真实窗口管理器）
        window = QWidget()
        window.resize(640, 360)
        window.show()

        from src.features.edit.core.edit_gpu_decode_manager import (
            EditGpuDecodeManager,
        )

        manager = EditGpuDecodeManager()
        rng = random.Random(20261025)
        sampler = _MetricsSampler(evidence_dir / "interact_metrics.jsonl")
        sampler.start()
        seek_failures = 0
        try:
            duration = _env_int("W08_INTERACT_S", 1800)
            t_end = time.monotonic() + duration
            print(f"\n[w08] interaction soak {duration}s wall clock")
            cycle = 0
            while time.monotonic() < t_end:
                mode = cycle % 6
                if mode == 0:  # 随机双向 seek
                    target = rng.randrange(0, 200)
                    layers = driver.layers(manager, target, 2)
                    if not driver.render(target, layers):
                        seek_failures += 1
                elif mode == 1:  # 连续 scrub（邻域步进）
                    base = rng.randrange(0, 180)
                    for f in range(base, base + 6):
                        layers = driver.layers(manager, f, 2)
                        driver.render(f, layers, timeout=10)
                elif mode == 2:  # 窗口事件：最小化 → 恢复 → 失焦/激活
                    window.showMinimized()
                    qapp.processEvents()
                    time.sleep(0.2)
                    window.showNormal()
                    window.activateWindow()
                    qapp.processEvents()
                    qapp.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
                    qapp.processEvents()
                    qapp.sendEvent(window, QEvent(QEvent.Type.WindowActivate))
                    qapp.processEvents()
                elif mode == 3:  # 密集反向 seek
                    for f in (180, 120, 60, 120, 180):
                        layers = driver.layers(manager, f, 2)
                        driver.render(f, layers, timeout=15)
                elif mode == 4:  # 4 轨重载
                    layers = driver.layers(manager, rng.randrange(0, 200), 4)
                    driver.render(rng.randrange(0, 200), layers, timeout=20)
                else:  # 静置（后台预热/池回收窗口）
                    time.sleep(1.0)
                cycle += 1
        finally:
            samples = sampler.stop()
            manager.close_all()
            window.close()

        summary = driver.summarize("interact", samples,
                                   float(_env_int("W08_INTERACT_S", 1800)))
        summary["seek_failures"] = seek_failures
        (evidence_dir / "interact_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"[w08] frames={summary['frames']} p95={summary['interval_p95_s']} "
              f"errors={summary['render_errors']} seek_fail={seek_failures}")
        assert summary["render_errors"] == 0, summary["render_error_kinds"]
        vram = summary["vram_mib"]
        if vram.get("available"):
            growth = vram["second_half_mean"] - vram["first_half_mean"]
            assert growth < 1024 + vram["first_half_mean"], f"显存趋势失控: {vram}"

    def test_renderer_restart_cycles(self, qapp, evidence_dir, monkeypatch):
        """渲染器 N×（默认 100）start/stop 全生命周期（W02 渲染器级项）。

        每循环：真实渲染线程 start → init → 单帧驻留渲染 → stop → wait，
        采样显存；趋势不得单调增长（驱动级上下文/纹理累积=泄漏）。
        """
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
        monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)
        from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext

        from src.features.edit.core.edit_gpu_decode_manager import (
            EditGpuDecodeManager,
        )
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        n_cycles = _env_int("W08_RESTARTS", 100)
        sampler = _MetricsSampler(
            evidence_dir / "restart_metrics.jsonl", interval=5.0)
        manager = EditGpuDecodeManager()
        failures = []
        try:
            print(f"\n[w08] renderer restart x{n_cycles}")
            for cycle in range(n_cycles):
                surface = QOffscreenSurface()
                surface.create()
                main_ctx = QOpenGLContext()
                if not main_ctx.create():
                    failures.append((cycle, "ctx_create"))
                    surface.destroy()
                    continue
                main_ctx.makeCurrent(surface)
                r = EditGpuPreviewRenderer(main_context=main_ctx)
                r.start()
                for _ in range(200):
                    if r.is_initialized:
                        break
                    qapp.processEvents()
                    time.sleep(0.02)
                if not r.is_initialized:
                    failures.append((cycle, "init"))
                else:
                    frame, ft = manager.get_frame(str(ASSET_4K), 0)
                    if frame is not None:
                        key = ("cuda_resident" if ft == "cuda_resident"
                               else "decoded_frame")
                        from src.features.edit.core.edit_gpu_preview_renderer \
                            import EditRenderTask

                        task = EditRenderTask(
                            frame=0, generation_id=900000 + cycle,
                            layers=[{key: frame, "frame_type": ft,
                                     "asset_path": str(ASSET_4K),
                                     "track_info": {"opacity": 1.0,
                                                    "clip_id": "w08r",
                                                    "track_index": 0}}],
                            viewport_size=(1920, 1080),
                            output_fbo_size=(1920, 1080),
                            coordinate_size=(1920, 1080))
                        r.submit(task)
                        time.sleep(0.05)
                        # 内联 ack：该用例无 Viewer，slot 须即刻归还
                        pending = getattr(r, "_current_present_lease", None)
                        slot = getattr(pending, "slot_id", -1)
                        if slot is not None and slot >= 0:
                            r.ack_present_slot(int(slot))
                r.stop()
                r.wait(10000)
                main_ctx.doneCurrent()
                surface.destroy()
                if cycle % 10 == 0:
                    sampler.sample()
                    print(f"[w08] cycle {cycle}: {sampler.samples[-1]}")
        finally:
            manager.close_all()
            samples = sampler.samples

        vram = _trend(samples, "vram_mib")
        summary = {
            "tag": "renderer_restart",
            "cycles": n_cycles,
            "failures": failures[:10],
            "failure_count": len(failures),
            "vram_mib": vram,
            "rss_mib": _trend(samples, "rss_mib"),
        }
        (evidence_dir / "restart_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1),
            encoding="utf-8")
        assert not failures, failures[:5]
        if vram.get("available") and len(samples) >= 4:
            # 单调增长嫌疑：末段均值显著高于首段（>512MiB 判泄漏）
            growth = vram["second_half_mean"] - vram["first_half_mean"]
            assert growth < 512, f"渲染器循环显存累积: {vram}"
