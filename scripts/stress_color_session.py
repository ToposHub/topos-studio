"""N09：调色会话压力/性能录制（scopes/s00_s01_record 协议延伸）。

按计划 N09/R12 的压力矩阵驱动**真实应用会话**（原生平台默认；offscreen
可显式指定）：

- 持续播放（素材循环）与随机 seek 交替；
- 调参拖动（grade 手势经图命令路由，B06.2/N03 通道）；
- 示波器 9 模式轮换 + 浮动窗口开关/resize；
- 页面切换（Color ↔ Edit）；
- 全程仪表：帧发布间隔 p50/p95/p99、调参命令往返 p95、RSS 内存趋势、
  示波器分段诊断（texture_not_shared/fbo_incomplete/blit_failed 为
  硬门槛）、渲染统计快照。

输出 JSON 报告（--output 目录）+ 预算对照表（README 由调用方按报告
数值撰写，不虚报）。

用法：
    .venv/bin/python scripts/stress_color_session.py tests/test_videos/测试素材01.mov \
        --minutes 30 --output docs/validation/color_resources/n09_stress
    # 冒烟：--minutes 2 --platform offscreen
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import resource
import statistics
import sys
import tempfile
import time

try:
    import psutil
except ImportError:  # pragma: no cover — 原生环境缺 psutil 时退化峰值口径
    psutil = None
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 预算表（N09 协议 2026-11 版；超预算 = failed，如实报告）。
#: 渲染帧距（interframe）受机器/素材影响大，作为**上报指标**不设门；
#: 验收门 = 节奏完整性（播放时钟 p95）+ 渲染流持续性（发布比，允许
#: 丢帧但不得停滞）+ 交互延迟 + 无持续泄漏。
BUDGETS = {
    "playback_clock_p95_ms": 60.0,        # 25fps 素材的节奏完整性
    "render_freshness_fps_min": 0.5,      # 稳态播放相位渲染不得停滞
    "param_command_p95_ms": 120.0,        # 一次 grade 手势命令往返
    "seek_to_frame_p95_ms": 400.0,
    "rss_growth_mb_per_10min": 250.0,     # 稳态泄漏判定（需 ≥5min 稳态窗）
}

_SCOPE_MODES = ("waveform", "parade", "vectorscope", "histogram", "cie",
                "falsecolor", "split", "quad", "all")


def _percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, int(round(p / 100.0 * (len(ordered) - 1))))
        return ordered[idx]
    return {"count": len(ordered),
            "p50": round(pct(50), 2), "p95": round(pct(95), 2),
            "p99": round(pct(99), 2),
            "max": round(ordered[-1], 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "docs/validation/color_resources/n09_stress")
    parser.add_argument("--minutes", type=float, default=30.0,
                        help="总压力时长（分钟）；30 = 协议全量")
    parser.add_argument("--platform", default="",
                        help="QT_QPA_PLATFORM（空 = 原生；冒烟用 offscreen）")
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--behavior-seconds", type=float, default=45.0,
                        help="每个压力行为的时长（冒烟可缩短以覆盖全行为）")
    args = parser.parse_args()

    source = args.media.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform
    config_dir = Path(tempfile.mkdtemp(prefix="topos-stress-"))
    (config_dir / "config.json").write_text(json.dumps({
        "locale": "en_US", "work_mode": "video", "current_page": "Color",
        "show_welcome_on_startup": False, "auto_load_last_project": False,
        "check_updates_on_startup": False, "auto_save_interval": 0,
    }))
    os.environ["TOPOS_CONFIG_PATH"] = str(config_dir)
    os.environ["TOPOS_PRODUCT_MODE"] = "studio"
    os.environ["TOPOS_TEST_MODE"] = "1"
    os.environ.setdefault("TOPOS_SCOPE_DIAG", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    import src.main as app_main  # noqa: F401 — QApplication 初始化副作用
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtCore import QObject, QTimer
    from PyQt6.QtWidgets import QApplication
    from src.app.main_window import MainWindow

    # 原生 GL：与 src.main.main() 同构的格式前置（4.5 core + 上下文共享；
    # offscreen 软件路径不敏感，原生缺此设置 moderngl 探测 version_code=0）
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    rng = random.Random(args.seed)
    total_seconds = args.minutes * 60.0

    class StressDriver(QObject):
        def __init__(self, host: Any) -> None:
            super().__init__(host)
            self.host = host
            self.phase = "start"
            self.started = time.monotonic()
            self.behavior = "playback"
            self.behavior_started = self.started
            self.behavior_rotator = iter(())  # 惰性初始化
            self.frame_times: list[float] = []
            self._playback_intervals: list[float] = []  # 稳态播放相位渲染帧
            self.clock_times: list[float] = []   # 播放控制器节拍（frame_changed）
            self.param_latencies: list[float] = []
            self.seek_latencies: list[float] = []
            self.rss_samples: list[tuple[float, float]] = []
            self._behavior_windows: list[tuple[float, float, str]] = []
            self.mode_switches = 0
            self.page_switches = 0
            self.float_toggles = 0
            self.seeks = 0
            self.param_drags = 0
            self.errors: list[str] = []
            self.total_frames = 0
            self.fps = 25.0
            self.report: dict[str, Any] = {
                "media": str(source.relative_to(ROOT)
                             if source.is_relative_to(ROOT) else source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "validation": "n09_stress_session",
                "minutes": args.minutes,
                "behavior_seconds": args.behavior_seconds,
                "platform": os.environ.get("QT_QPA_PLATFORM") or "native",
                "seed": args.seed,
            }
            self.timer = QTimer(self)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self.tick)
            self.timer.start()
            # 帧发布间隔仪表（渲染完成）+ 播放节拍（控制器时钟）
            self.host.engine.frame_ready.connect(self._on_frame)
            try:
                controller = self.host.timeline_panel.get_playback_controller()
                controller.frame_changed.connect(self._on_clock)
            except Exception:  # noqa: BLE001 — 控制器缺席时只测渲染
                pass

        # ── 仪表 ──
        def _on_frame(self, *_a) -> None:
            now = time.monotonic()
            self.frame_times.append(now)
            # 只在播放相位记「待测间隔」（seek/暂停的洞不算播放帧距）
            if (self.phase == "stress" and self.behavior == "playback"
                    and now - self.started > 60.0):
                self._playback_intervals.append(now)
                if len(self._playback_intervals) > 20000:
                    del self._playback_intervals[:10000]

        def _on_clock(self, _frame: int) -> None:
            now = time.monotonic()
            self.clock_times.append(now)
            if len(self.clock_times) > 20000:
                del self.clock_times[:10000]

        def _in_playback_phase(self, t: float) -> bool:
            # 近似：clock_times 未带相位标签——用行为区间记录判断
            for t0, t1, behavior in self._behavior_windows:
                if t0 <= t <= t1:
                    return behavior == "playback"
            return False

        def _clock_samples(self) -> list[float]:
            return [(b - a) * 1000.0 for a, b in zip(self.clock_times,
                                                     self.clock_times[1:])
                    if 0.005 < (b - a) < 2.0]

        def _interframe_samples(self) -> list[float]:
            return [(b - a) * 1000.0 for a, b in zip(self.frame_times,
                                                     self.frame_times[1:])
                    if (b - a) < 2.0]  # 剔除暂停/切相位的空洞

        def _rss_mb(self) -> float:
            # 当前 RSS（psutil；退化口径 = ru_maxrss 峰值，单调不可回退）
            if psutil is not None:
                try:
                    return psutil.Process().memory_info().rss / 1_048_576.0
                except Exception:  # noqa: BLE001
                    pass
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
                1_048_576.0 if sys.platform == "darwin" else 1024.0)

        def _grade_drag(self) -> None:
            """一次调参手势（图命令路由 + legacy 同值预览）。"""
            started = time.perf_counter()
            try:
                value = 0.4 + 0.3 * rng.random()
                self.host.on_grading_changed(
                    value, 0.0, 0.0, value * 0.5, -0.05, 0.02,
                    0.1 * rng.random(), 0.0, 0.0, 0.0, 0.0, 0.0)
                self.param_drags += 1
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"grade_drag: {exc}")
            self.param_latencies.append(
                (time.perf_counter() - started) * 1000.0)

        def _do_seek(self) -> None:
            frame = rng.randrange(0, max(1, self.total_frames))
            started = time.perf_counter()
            try:
                self.host.edit_page.seek_to_frame(frame)
                self.seeks += 1
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"seek: {exc}")
            self.seek_latencies.append(
                (time.perf_counter() - started) * 1000.0)

        def _playback_state(self) -> Any:
            try:
                return self.host.timeline_panel.get_playback_controller()
            except Exception:  # noqa: BLE001
                return None

        # ── 相位机 ──
        def tick(self) -> None:
            try:
                now = time.monotonic()
                elapsed = now - self.started
                self.rss_samples.append((elapsed, self._rss_mb()))
                if elapsed > total_seconds:
                    self.finish(True)
                    return
                if self.phase == "start":
                    self.host._auto_create_edit_project(name="N09 Stress")
                    self.host.media_pool_panel._import_files([str(source)])
                    self.phase = "import"
                    return
                if self.phase == "import":
                    project = self.host._current_edit_project
                    asset = project.get_asset_by_path(str(source))
                    if asset is None:
                        if elapsed > 120:
                            raise TimeoutError("媒体导入超时")
                        return
                    self.total_frames = int(asset.total_frames)
                    self.fps = float(asset.fps)
                    tv = self.host.timeline_panel.timeline_view
                    if not tv._ensure_first_asset_timeline_settings(asset):
                        raise RuntimeError("无法创建时间线")
                    timeline = self.host.edit_page._current_timeline
                    if not tv._add_asset_as_clip(asset,
                                                 timeline.video_tracks[0], 0):
                        raise RuntimeError("无法插入片段")
                    self.phase = "timeline"
                    return
                if self.phase == "timeline":
                    timeline = self.host.edit_page._current_timeline
                    clip = timeline.video_tracks[0].clips[0]
                    self.host.open_timeline_clip_in_color(
                        timeline.timeline_id, clip.clip_id, 0)
                    self.phase = "warmup"
                    self.behavior_started = now
                    return
                if self.phase == "warmup":
                    # 两个 Corrector + 选中（图命令调参目标）
                    panel = self.host.color_node_panel
                    snapshot = panel.snapshot()
                    if not any(n.kind.value == "corrector"
                               for n in snapshot.nodes.values()):
                        panel.view.apply_edit("add_serial",
                                              panel.view.state.add_serial)
                        return
                    first = next(n.id for n in panel.snapshot().nodes.values()
                                 if n.kind.value == "corrector")
                    panel.view.state.graph.active_id = first
                    panel.view._selection_changed()
                    controller = self._playback_state()
                    if controller is not None:
                        controller.play()
                    self.phase = "stress"
                    self.behavior_rotator = self._rotate_behavior()
                    self.behavior = next(self.behavior_rotator)
                    self.behavior_started = now
                    return
                if self.phase != "stress":
                    return
                # ── 压力行为轮换 ──
                if now - self.behavior_started > args.behavior_seconds:
                    self._behavior_windows.append(
                        (self.behavior_started, now, self.behavior))
                    self.behavior = next(self.behavior_rotator)
                    self.behavior_started = now
                self._drive_behavior(now)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"tick: {exc!r}")
                self.finish(False, str(exc))

        def _rotate_behavior(self):
            order = ("playback", "seek", "param", "scope", "page", "float",
                     "playback")
            while True:
                for behavior in order:
                    yield behavior

        def _drive_behavior(self, now: float) -> None:
            within = now - self.behavior_started
            controller = self._playback_state()
            if self.behavior == "playback":
                if controller is not None and not controller.is_playing:
                    controller.play()
                if int(within * 2) % 5 == 0:
                    self._grade_drag()  # 播放中拖参（S07 场景）
            elif self.behavior == "seek":
                if controller is not None and controller.is_playing:
                    controller.pause()
                if within % 1.2 < 0.12:
                    self._do_seek()
            elif self.behavior == "param":
                if controller is not None and controller.is_playing:
                    controller.pause()
                if within % 0.4 < 0.1:
                    self._grade_drag()
            elif self.behavior == "scope":
                mode = _SCOPE_MODES[self.mode_switches % len(_SCOPE_MODES)]
                self.host.scope_panel.set_mode(mode)
                self.host._on_scope_demand_changed()
                self.mode_switches += 1
                time.sleep(0.05)  # 让 demand/执行推进一个节拍
            elif self.behavior == "page":
                target = "Edit" if self.page_switches % 2 == 0 else "Color"
                try:
                    self.host.switch_to_page(target)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"page: {exc}")
                self.page_switches += 1
                time.sleep(0.2)
            elif self.behavior == "float":
                try:
                    panel = self.host.scope_panel
                    if self.float_toggles % 2 == 0:
                        panel.open_floating()
                        window = getattr(panel, "_floating_window", None)
                        if window is not None:
                            window.resize(960 + (self.float_toggles % 3) * 120,
                                          640)
                    else:
                        close = getattr(panel, "_floating_window", None)
                        if close is not None:
                            close.close()
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"float: {exc}")
                self.float_toggles += 1
                time.sleep(0.3)

        # ── 收束 ──
        def finish(self, success: bool, error: str = "") -> None:
            self.timer.stop()
            # 收束在途行为窗口（publish_ratio 的相位判定用）
            self._behavior_windows.append(
                (self.behavior_started, time.monotonic(), self.behavior))
            controller = self._playback_state()
            try:
                if controller is not None and controller.is_playing:
                    controller.pause()
            except Exception:  # noqa: BLE001
                pass
            from src.features.color.core.scope_diagnostics import (
                scope_diagnostics,
            )
            diag = scope_diagnostics.snapshot()
            events = diag.get("events", {})
            widget_failures = {reason: events.get("widget/" + reason, {})
                               for reason in ("texture_not_shared",
                                              "fbo_incomplete",
                                              "blit_failed")}
            hard_fail = any(v.get("count", 0)
                            for v in widget_failures.values())
            elapsed = time.monotonic() - self.started
            # RSS 斜率（MB/10min）：跳过前 60s 预热（着色器/解码缓存）
            rss_slope = 0.0
            steady = [(t, r) for t, r in self.rss_samples if t >= 60.0]
            if len(steady) >= 2:
                (t0, r0), (t1, r1) = steady[0], steady[-1]
                span_min = max((t1 - t0) / 60.0, 1e-6)
                rss_slope = (r1 - r0) / span_min * 10.0
            # p95 口径：稳态（>60s）+ 播放相位的渲染帧间隔
            playback_phase = [(b - a) * 1000.0
                              for a, b in zip(self._playback_intervals,
                                              self._playback_intervals[1:])
                              if 0.005 < (b - a) < 2.0]
            interframe = _percentiles(playback_phase)
            clock = _percentiles(self._clock_samples())
            params = _percentiles(self.param_latencies)
            seeks = _percentiles(self.seek_latencies)
            stats: dict[str, Any] = {}
            try:
                stats = self.host.engine.get_render_stats() \
                    if hasattr(self.host.engine, "get_render_stats") else {}
            except Exception:  # noqa: BLE001
                pass
            # 稳态播放相位的渲染新鲜度（fps）：停滞 = 过期输出风险。
            # 切页会暂停播放时钟——以实际发生渲染的窗口时长归一。
            playback_window_span = sum(
                t1 - t0 for t0, t1, behavior in self._behavior_windows
                if behavior == "playback" and t0 - self.started > 60.0)
            render_fps = (len(playback_phase) / playback_window_span
                          if playback_window_span > 0 else 0.0)
            steady_span = sum(t1 - t0 for t0, t1, _b
                              in self._behavior_windows)
            rss_gate_applicable = steady_span >= 300.0
            budgets_hit = {
                "playback_clock_p95_ms":
                    clock.get("p95", 0.0)
                    <= BUDGETS["playback_clock_p95_ms"],
                "render_freshness_fps_min":
                    render_fps >= BUDGETS["render_freshness_fps_min"],
                "param_command_p95_ms":
                    params.get("p95", 0.0) <= BUDGETS["param_command_p95_ms"],
                "seek_to_frame_p95_ms":
                    seeks.get("p95", 0.0) <= BUDGETS["seek_to_frame_p95_ms"],
                "rss_growth_mb_per_10min":
                    (rss_slope <= BUDGETS["rss_growth_mb_per_10min"]
                     if rss_gate_applicable else None),  # 窗口不足=不判
            }
            passed = (success and not error and not hard_fail
                      and all(v is not False for v in budgets_hit.values())
                      and not self.errors)
            self.report.update({
                "status": "passed" if passed else "failed",
                "error": error,
                "hard_gate_widget_failures": widget_failures,
                "budgets": BUDGETS,
                "budgets_hit": budgets_hit,
                "metrics": {
                    "playback_interframe_ms_report_only": interframe,
                    "render_freshness_fps": round(render_fps, 3),
                "steady_playback_window_seconds": round(
                    playback_window_span, 1),
                "steady_playback_render_frames": len(playback_phase),
                    "playback_clock_ms": clock,
                    "param_command_ms": params,
                    "seek_ms": seeks,
                    "rss_mb": {
                        "first": round(self.rss_samples[0][1], 1)
                        if self.rss_samples else None,
                        "peak": round(max((r for _, r in
                                           self.rss_samples), default=0.0), 1),
                        "growth_mb_per_10min": round(rss_slope, 1),
                    },
                    "counts": {
                        "frames": len(self.frame_times),
                        "playback_clock_ticks": len(self.clock_times),
                        "is_playing_at_end": bool(
                            self._playback_state() is not None
                            and self._playback_state().is_playing),
                        "param_drags": self.param_drags,
                        "seeks": self.seeks,
                        "scope_mode_switches": self.mode_switches,
                        "page_switches": self.page_switches,
                        "float_toggles": self.float_toggles,
                    },
                },
                "scope_diagnostics_events": {
                    key: value for key, value in events.items()
                    if isinstance(value, dict) and value.get("count")},
                "render_stats_keys": sorted(stats)[:24] if stats else [],
                "behavior_windows": [
                    [round(t0 - self.started, 1), round(t1 - self.started, 1),
                     behavior]
                    for t0, t1, behavior in self._behavior_windows],
                "errors": self.errors[:20],
                "elapsed_seconds": round(elapsed, 1),
            })
            (output / "stress_report.json").write_text(
                json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(self.report, ensure_ascii=False), flush=True)
            app = QApplication.instance()
            app.exit(0 if passed else 1)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    driver = StressDriver(window)
    code = app.exec()
    # 峰值 RSS 补记（进程级）
    return code


if __name__ == "__main__":
    raise SystemExit(main())
