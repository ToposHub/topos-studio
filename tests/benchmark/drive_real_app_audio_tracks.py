#!/usr/bin/env python3
"""Drive the REAL app Edit timeline: video + N audio tracks, measure playback.

Scenarios:
  - audio-only: N WAV clips on A1..AN
  - A+V: video on V1 + N WAVs on A1..AN (audio must not drag the video)

Measures:
  - video presented fps (timeline_preview_renderer.frame_ready distinct)
  - audio smoothness: AudioRingBuffer stats (underruns/overflow/filled)
    + audio-clock progression rate vs wall time
  - process CPU% / per-core utilization (psutil)
  - audio_thread trace event counts (output_wait/write_zero) via env trace

Usage:
    .venv/bin/python tests/benchmark/drive_real_app_audio_tracks.py \\
        [video.mp4] wav1 [wav2 ...] [seconds]

First arg with video extension = video asset (V1, establishes timeline fps);
remaining non-numeric args = WAV assets, one per audio track.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv"}

# TOPOS_AUDIO_FX=heavy：给每条音频轨挂满 FX 链（EQ/压缩/混响/延迟/门/限幅），
# 用于验证效果器向量化后多轨实时混音余量。
FX_HEAVY = os.environ.get("TOPOS_AUDIO_FX") == "heavy"


def _heavy_fx_chain():
    from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
    entries = [
        ("strip_eq", {"enabled": True, "mode": "15-band", "gain": 0.0,
                      "low_cut_enable": True, "low_cut": 80.0,
                      "high_cut_enable": False, "high_cut": 18000.0,
                      "bands": [2.0, 1.0, 0.0, -1.0, 0.0, 0.0, 1.0, 0.0,
                                0.0, 1.0, 0.0, -1.0, 0.0, 1.0, 2.0]}),
        ("compressor", {"enabled": True, "threshold_db": -24.0, "ratio": 4.0,
                        "attack_ms": 3.0, "release_ms": 250.0, "knee_db": 30.0,
                        "makeup_gain": 0.0}),
        ("reverb", {"enabled": True, "mix": 30.0, "decay": 2.0,
                    "pre_delay_ms": 20.0, "damping_hz": 5000.0}),
        ("delay", {"enabled": True, "time_ms": 300.0, "feedback": 40.0,
                   "mix": 30.0, "ping_pong": True, "low_cut_hz": 100.0}),
        ("noise_gate", {"enabled": True, "threshold_db": -50.0,
                        "attack_ms": 1.0, "release_ms": 100.0}),
        ("limiter", {"enabled": True, "threshold_db": -3.0, "release_ms": 100.0}),
    ]
    return EffectChainParams(effects=tuple(
        TrackEffectInstance(effect_type=t, params=p, enabled=True, order=i)
        for i, (t, p) in enumerate(entries)
    ))


def _track_kwargs() -> dict:
    """新建音频轨时的 FX 附加参数（TOPOS_AUDIO_FX=heavy 时生效）。"""
    return {"effect_chain": _heavy_fx_chain()} if FX_HEAVY else {}


# TOPOS_AUDIO_CLIP_FX=heavy：把重 FX 挂到每个音频 clip 的 effects 上
# （Effects 面板把音频效果器拖到片段上的路径，走 set_clip_effects →
# AudioMixer._clip_effect_chains，与轨道级 FX 是不同容器同一 DSP）。
CLIP_FX_HEAVY = os.environ.get("TOPOS_AUDIO_CLIP_FX") == "heavy"


def _heavy_clip_effects():
    from src.features.edit.core.effect_instance import EffectInstance
    fx = _heavy_fx_chain().effects
    return tuple(
        EffectInstance(
            effect_type=e.effect_type,
            params=dict(e.params),
            is_enabled=True,
            order=e.order,
        )
        for e in fx
    )


def _attach_clip_fx(timeline, track, heavy_effects):
    """给该轨上所有 clip 挂重 FX（返回更新后的 timeline）。"""
    for clip in list(track.clips):
        timeline = timeline.with_clip_updated(clip.with_effects(heavy_effects))
    return timeline


# TOPOS_AUDIO_TRANSITIONS=<seconds>：每轨放两个相邻 clip 并在切口挂音频转场
# （curve 由 TOPOS_AUDIO_TRANS_CURVE 指定，默认 constant_power）——
# 走 track.transitions → set_audio_transitions → apply_crossfade_block 实时路径。
TRANS_S = float(os.environ.get("TOPOS_AUDIO_TRANSITIONS", "0") or 0)
TRANS_CURVE = os.environ.get("TOPOS_AUDIO_TRANS_CURVE", "constant_power")


def log(msg: str) -> None:
    print(f"[DRIVER] {msg}", flush=True)


def parse_args() -> tuple[Path | None, list[Path], float]:
    args = sys.argv[1:]
    video: Path | None = None
    wavs: list[Path] = []
    seconds = 25.0
    for a in args:
        if a.replace(".", "", 1).isdigit():
            seconds = float(a)
            continue
        p = Path(a).resolve()
        if p.suffix.lower() in VIDEO_EXTS and video is None:
            video = p
        else:
            wavs.append(p)
    if not wavs:
        wav_dir = REPO_ROOT / "tests/test_audio/wav"
        wavs = sorted(wav_dir.glob("*.wav"))[:1]
    return video, wavs, seconds


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog

    VIDEO, WAVS, SECONDS = parse_args()
    STEADY_SKIP_S = 3.0

    os.environ["QSG_RHI_BACKEND"] = "opengl"
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setRedBufferSize(8); fmt.setGreenBufferSize(8); fmt.setBlueBufferSize(8); fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24); fmt.setStencilBufferSize(8)
    if sys.platform != "win32":
        fmt.setSamples(4)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    if os.environ.get("TOPOS_PAINT_TRACE"):
        from tests.benchmark.drive_real_app_edit_tracks import install_paint_trace
        install_paint_trace()

    from PyQt6.QtWidgets import QProgressDialog

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                # 进度对话框（导入进度）不能 reject——会取消导入，
                # 让后续 clip 添加静默失败。
                if isinstance(w, QProgressDialog):
                    continue
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    # 消融：TOPOS_DSP_SWAP=old 把 DSP 效果器换回优化前实现（A/B 基准用）。
    # 必须在 src 各模块导入后、播放器构建前重绑三个消费者命名空间。
    if os.environ.get("TOPOS_DSP_SWAP") == "old":
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "old_dsp_swap", os.environ.get("TOPOS_OLD_DSP", "/tmp/old_audio_dsp.py"))
        _old = _ilu.module_from_spec(_spec)
        sys.modules["old_dsp_swap"] = _old
        _spec.loader.exec_module(_old)
        import src.features.edit.core.audio_dsp as _dsp
        import src.features.edit.core.audio_playback as _apb
        import src.features.edit.core.audio_graph_renderer as _agr
        for _mod in (_dsp, _apb, _agr):
            for _name in ("StripEQFilter", "EffectChain", "MasterLimiter",
                          "apply_crossfade_block", "apply_clip_fade"):
                if hasattr(_old, _name):
                    setattr(_mod, _name, getattr(_old, _name))
        log("DSP swapped to OLD implementation (A/B ablation)")

    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow

    config_manager = ConfigManager()
    feature_flags = load_feature_flags(config_manager)
    I18N.load_locale("zh_CN")
    registry = FeatureRegistry()
    registry.initialize(AppContext(config_manager=config_manager, feature_flags=feature_flags, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    if feature_flags.is_enabled("composite"):
        from src.features.composite.module import CompositeModule
        m = CompositeModule(); registry.register_module(m); m.discover_and_register_plugins()
    if feature_flags.is_enabled("edit"):
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry, test_mode=False)
    mw.show()

    if os.environ.get("TOPOS_NO_WAVEFORM"):
        # 消融实验：完全禁用波形提取，定位后台 CPU 风暴来源
        from src.features.edit.core.audio_waveform import WaveformExtractor
        WaveformExtractor.instance().request_waveform = (
            lambda *a, **k: None)
        log("waveform extraction DISABLED (ablation)")

    for _ in range(90):
        app.processEvents()
    settle_end = time.time() + 4.0
    while time.time() < settle_end:
        app.processEvents()
        time.sleep(0.02)
    log(f"booted page={mw.current_page} video={VIDEO.name if VIDEO else None} "
        f"audio_tracks={len(WAVS)} seconds={SECONDS}")

    # ── import media ──
    imports = ([str(VIDEO)] if VIDEO else []) + [str(w) for w in WAVS]
    mw.media_pool_panel._import_files(imports)
    deadline = time.time() + 90
    project = None
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and all(
            project.get_asset_by_path(str(p)) is not None
            for p in ([VIDEO] if VIDEO else []) + WAVS
        ):
            break
        time.sleep(0.02)
    assert project is not None, "project not created"
    assets = [project.get_asset_by_path(str(p)) for p in ([VIDEO] if VIDEO else []) + WAVS]
    assert all(assets), "some assets missing after import"
    wav_probe = assets[1] if VIDEO is not None else assets[0]
    meta = dict(getattr(wav_probe, "metadata", {}) or {})
    log(f"WAV metadata: duration_seconds={meta.get('duration_seconds')} "
        f"total_frames={meta.get('total_frames')} fps={meta.get('fps')} "
        f"type={meta.get('type')} sample_rate={meta.get('sample_rate')} "
        f"channels={meta.get('channels')} keys={sorted(k for k in meta.keys())[:14]}")

    # ── build timeline ──
    mw.switch_to_page("Edit")
    for _ in range(60):
        app.processEvents()

    tv = mw.timeline_panel.timeline_view
    first_asset = assets[0]
    tv._ensure_first_asset_timeline_settings(first_asset)
    for _ in range(20):
        app.processEvents()
    timeline = mw.edit_page._current_timeline or tv._create_default_timeline(first_asset)

    if VIDEO is not None:
        ok_v = tv._add_asset_as_clip(first_asset, timeline.video_tracks[0], 0)
        deadline = time.time() + 20
        while time.time() < deadline:
            app.processEvents()
            if mw.timeline_preview_renderer is not None:
                break
            time.sleep(0.02)
        timeline = getattr(tv, "_timeline", None) or timeline
        log(f"V1 video clip ok={ok_v}")
    else:
        ok_v = None

    # audio clips: skip A1 when the video asset inserted its linked audio
    # there; place each WAV on the next audio track.
    from src.features.edit.core.timeline_v2 import EditTrack
    wav_assets = assets[1:] if VIDEO is not None else assets
    skip_a1 = VIDEO is not None and any(
        t.clips for t in timeline.audio_tracks[:1])
    start_index = 1 if skip_a1 else 0
    if skip_a1:
        log("A1 occupied by video's linked audio; WAVs start at A2")
    audio_index = 0
    for audio_index, asset in enumerate(wav_assets):
        if audio_index == 0 and not skip_a1:
            a1 = timeline.audio_tracks[0] if timeline.audio_tracks else None
            if a1 is None:
                timeline = timeline.with_track_added(EditTrack(
                    track_id=str(uuid.uuid4()), name="A1", track_type="audio",
                    index=0, clips=(), transitions=(), is_enabled=True,
                    is_locked=False, is_muted=False, is_solo=False,
                    **_track_kwargs()))
                mw._sync_timeline_to_views(timeline)
                if getattr(tv, "_timeline", None) is not None:
                    tv._timeline = timeline
                a1 = timeline.audio_tracks[0]
            elif FX_HEAVY:
                # 已存在的 A1（空时间线默认轨）通过 frozen replace 挂 FX
                import dataclasses
                a1 = dataclasses.replace(a1, effect_chain=_heavy_fx_chain())
                timeline = timeline.with_track_updated(a1)
                mw._sync_timeline_to_views(timeline)
                if getattr(tv, "_timeline", None) is not None:
                    tv._timeline = timeline
                a1 = timeline.audio_tracks[0]
            ok_a = tv._add_asset_as_clip(asset, a1, 0)
            cur_track = a1
        else:
            name = f"A{audio_index + 1 + start_index}"
            tl2 = timeline.with_track_added(EditTrack(
                track_id=str(uuid.uuid4()), name=name, track_type="audio",
                index=audio_index + start_index, clips=(), transitions=(),
                is_enabled=True, is_locked=False, is_muted=False,
                is_solo=False, **_track_kwargs()))
            timeline = tl2
            try:
                mw.edit_page._current_timeline = timeline
            except Exception:
                pass
            mw._sync_timeline_to_views(timeline)
            if getattr(tv, "_timeline", None) is not None:
                tv._timeline = timeline
            for _ in range(10):
                app.processEvents()
            track_obj = next(
                (t for t in timeline.audio_tracks if t.name == name), None)
            assert track_obj is not None, f"{name} not created"
            ok_a = tv._add_asset_as_clip(asset, track_obj, 0)
            cur_track = track_obj
        timeline = getattr(tv, "_timeline", None) or timeline
        log(f"audio clip {audio_index + 1}: ok={ok_a}")
        for _ in range(10):
            app.processEvents()

        # 转场场景：同一素材紧接在第一个 clip 之后，形成相邻切口
        if TRANS_S > 0 and ok_a:
            timeline = getattr(tv, "_timeline", None) or timeline
            t_now = timeline.get_track_by_id(cur_track.track_id)
            if t_now is not None and t_now.clips:
                first = sorted(t_now.clips, key=lambda c: c.timeline_start)[0]
                ok2 = tv._add_asset_as_clip(asset, t_now, int(first.timeline_end))
                log(f"transition pair clip on {t_now.name}: ok={ok2}")
                for _ in range(10):
                    app.processEvents()
                # 放置同步更新 tv._timeline；回取快照，否则下一轮迭代/循环尾的
                # _sync_timeline_to_views 会用旧快照把 pair clip 覆盖掉。
                timeline = getattr(tv, "_timeline", None) or timeline

    try:
        mw.edit_page._current_timeline = timeline
    except Exception:
        pass
    mw._sync_timeline_to_views(timeline)
    for _ in range(40):
        app.processEvents()

    n_audio = len([t for t in timeline.audio_tracks if t.clips])
    tl_fps = float(timeline.fps) if hasattr(timeline.fps, "num") else float(timeline.fps)
    log(f"timeline ready: fps={tl_fps} audio_tracks={len(timeline.audio_tracks)} "
        f"tracks_with_clips={n_audio} video_clip={ok_v}")

    # 音频转场场景：每轨前两个 clip 的切口挂 audio:<curve> 转场
    if TRANS_S > 0:
        from src.features.edit.core.timeline_v2 import TransitionInstance
        timeline = getattr(tv, "_timeline", None) or timeline
        dur_frames = max(1, int(round(TRANS_S * tl_fps)))
        n_tr = 0
        if os.environ.get("TOPOS_DEBUG_FX"):
            log("transition-attach clip counts: " + str(
                [(t.name, len(t.clips)) for t in timeline.audio_tracks]))
        for t in list(timeline.audio_tracks):
            clips_sorted = sorted(t.clips, key=lambda c: c.timeline_start)
            if len(clips_sorted) < 2:
                continue
            a, b = clips_sorted[0], clips_sorted[1]
            tr = TransitionInstance(
                transition_type=f"audio:{TRANS_CURVE}",
                track_id=t.track_id,
                left_clip_id=a.clip_id,
                right_clip_id=b.clip_id,
                center_frame=int(a.timeline_end),
                duration=dur_frames,
                alignment="centered",
            )
            timeline = timeline.with_track_updated(t.with_transition_added(tr))
            globals()["_trans_cut_frame"] = int(a.timeline_end)
            n_tr += 1
        try:
            mw.edit_page._current_timeline = timeline
        except Exception:
            pass
        mw._sync_timeline_to_views(timeline)
        if getattr(tv, "_timeline", None) is not None:
            tv._timeline = timeline
        for _ in range(20):
            app.processEvents()
        # 转场经注册循环（_sync_track_audio_params）进入引擎，需重跑同步
        try:
            mw.edit_page._sync_audio_tracks_from_timeline()
            for _ in range(20):
                app.processEvents()
        except Exception as e:
            log(f"transition re-sync failed: {e}")
        log(f"audio transitions attached: {n_tr} × {TRANS_CURVE} {TRANS_S}s "
            f"(duration={dur_frames}f)")

    # Effects 面板路径消融：把重 FX 挂到每个音频 clip 的 effects 上
    if CLIP_FX_HEAVY:
        timeline = getattr(tv, "_timeline", None) or timeline
        heavy_fx = _heavy_clip_effects()
        for t in list(timeline.audio_tracks):
            if t.clips:
                timeline = _attach_clip_fx(timeline, t, heavy_fx)
        try:
            mw.edit_page._current_timeline = timeline
        except Exception:
            pass
        mw._sync_timeline_to_views(timeline)
        if getattr(tv, "_timeline", None) is not None:
            tv._timeline = timeline
        for _ in range(40):
            app.processEvents()
        n_fx = sum(len(t.clips) for t in timeline.audio_tracks)
        log(f"clip-level heavy FX attached: clips={n_fx} "
            f"fx_per_clip={len(heavy_fx)}")
        # 注册发生在 clip 添加时（早于挂 FX）——重跑一遍同步，
        # 等价于真实 app 里 Effects 面板挂效果后的 controller 即时同步。
        try:
            mw.edit_page._current_timeline = timeline
            mw.edit_page._sync_audio_tracks_from_timeline()
            for _ in range(20):
                app.processEvents()
            log("audio re-sync after clip FX attach done")
        except Exception as e:
            log(f"audio re-sync failed: {e}")

    # ── measure ──
    import psutil
    proc = psutil.Process()

    tl_renderer = mw.timeline_preview_renderer
    emissions: list[tuple[float, int]] = []
    if tl_renderer is not None:
        t0 = time.perf_counter()
        tl_renderer.frame_ready.connect(
            lambda frame, result: emissions.append((time.perf_counter() - t0, int(frame))))

    controller = mw.timeline_panel.get_playback_controller()
    audio_mgr = getattr(mw.edit_page, "_audio_manager", None)

    if os.environ.get("TOPOS_AUDIO_LOOP_TRACE") and audio_mgr is not None:
        import traceback
        _loop_stats: dict[str, list] = {"gap_ms": [], "cycle_ms": [], "chunks": []}
        _loop_last = {"t": None}

        def _install_loop_trace() -> None:
            thread = getattr(audio_mgr, "_playback_thread", None)
            if thread is None:
                return
            orig_cycle = type(thread)._play_cycle

            def traced_cycle(self):
                t0 = time.perf_counter()
                last = _loop_last["t"]
                if last is not None:
                    _loop_stats["gap_ms"].append((t0 - last) * 1000)
                _loop_last["t"] = t0
                try:
                    return orig_cycle(self)
                finally:
                    _loop_stats["cycle_ms"].append((time.perf_counter() - t0) * 1000)
            type(thread)._play_cycle = traced_cycle
            log("audio loop trace installed")

        QTimer.singleShot(1500, _install_loop_trace)
        globals()["_loop_stats"] = _loop_stats

        _mix_stats: list = []

        def _install_mix_trace() -> None:
            # 先捕获 PyAV resampler 失败的真实异常（默认被吞进 debug 日志）
            import src.features.edit.core.audio_playback as _apb
            _orig_resample = _apb._resample_audio
            _err_printed = {"n": 0}

            def traced_resample(samples, source_rate, target_rate, instance_id=0, flush=False):
                try:
                    return _orig_resample(samples, source_rate, target_rate,
                                          instance_id=instance_id, flush=flush)
                except Exception as e:
                    if _err_printed["n"] < 2:
                        _err_printed["n"] += 1
                        import traceback
                        print(f"[RESAMPLE-RAISE] {e}\n"
                              + traceback.format_exc(limit=4), flush=True)
                    raise

            _apb._resample_audio = traced_resample
            # 拦截内部 debug 日志，暴露 PyAV→scipy 回退原因
            import logging as _logging
            class _CaptureHandler(_logging.Handler):
                def emit(self, record):
                    msg = record.getMessage()
                    if "resampler failed" in msg and _err_printed["n"] < 3:
                        _err_printed["n"] += 1
                        print(f"[RESAMPLE-FALLBACK] {record.name}: {msg}", flush=True)
            _apb.logger.addHandler(_CaptureHandler())
            _apb.logger.setLevel(_logging.DEBUG)

            thread = getattr(audio_mgr, "_playback_thread", None)
            mixer = getattr(thread, "_mixer", None) if thread else None
            if mixer is None:
                return
            orig_mix = type(mixer).mix_samples
            prof = {"obj": None}

            def traced_mix(self, *a, **kw):
                t0 = time.perf_counter()
                try:
                    return orig_mix(self, *a, **kw)
                finally:
                    _mix_stats.append((time.perf_counter() - t0) * 1000)
                    if prof["obj"] is not None and len(_mix_stats) == 200:
                        prof["obj"].disable()
                        import io as _io
                        import pstats as _pstats
                        s = _io.StringIO()
                        _pstats.Stats(prof["obj"], stream=s).sort_stats(
                            'tottime').print_stats(14)
                        print("[MIX-PROFILE @200]\n" + s.getvalue()[:2400],
                              flush=True)
                        prof["obj"] = None

            import cProfile
            type(mixer).mix_samples = traced_mix
            prof["obj"] = cProfile.Profile()
            prof["obj"].enable()
            log("mix_samples trace installed")

        QTimer.singleShot(1600, _install_mix_trace)
        globals()["_mix_stats"] = _mix_stats

    ring_samples: list[tuple[float, dict]] = []
    audio_time_samples: list[tuple[float, float]] = []

    # let audio manager sync from the new timeline
    for _ in range(60):
        app.processEvents()
        time.sleep(0.01)

    t0 = time.perf_counter()
    proc.cpu_percent(None)  # prime
    import psutil as _ps
    _ps.cpu_percent(None, percpu=True)

    # Per-thread CPU sampler: aggregate thread CPU time by thread name.
    import threading as _th
    _thread_cpu: dict[str, list[float]] = {}

    def _sample_thread_cpu() -> None:
        try:
            names = {t.ident: t.name for t in _th.enumerate()}
            for ti in proc.threads():
                key = names.get(ti.id, f"native-{ti.id}")
                _thread_cpu.setdefault(key, []).append(
                    ti.user_time + ti.system_time)
        except Exception as e:
            _thread_cpu.setdefault("_sample_error", []).append(0.0)
            globals()["__thread_sample_err"] = repr(e)

    # 转场场景：起点定到切口前 2s，确保播放窗覆盖整个转场跨度
    _cut = globals().get("_trans_cut_frame")
    if _cut is not None:
        try:
            controller.seek(max(0, int(_cut - 2.0 * tl_fps)))
            for _ in range(20):
                app.processEvents()
            log(f"playback seeked to transition cut (frame {_cut - 2.0 * tl_fps:.0f})")
        except Exception as e:
            log(f"seek to transition failed: {e}")

    controller.play()

    # 调试探针：确认 clip/track FX 真正进入了实时引擎
    if os.environ.get("TOPOS_DEBUG_FX") and audio_mgr is not None:
        def _probe_fx_state() -> None:
            try:
                mixer = getattr(getattr(audio_mgr, "_playback_thread", None), "_mixer", None)
                if mixer is None:
                    log("fx-probe: no mixer yet")
                    return
                clips = [(k, len(c._effects)) for k, c in mixer._clip_effect_chains.items()]
                tracks = [(k, len(c._effects)) for k, c in mixer._effect_chains.items()]
                trans = {k: len(v) for k, v in mixer._audio_transitions.items()}
                log(f"fx-probe: clip_chains={clips} track_chains={tracks} "
                    f"audio_transitions={trans}")
            except Exception as e:
                log(f"fx-probe failed: {e}")
        QTimer.singleShot(3000, _probe_fx_state)
    samples: list[tuple[float, int, bool]] = []
    cpu_samples: list[float] = []
    percpu_samples: list[list[float]] = []
    while time.perf_counter() - t0 < SECONDS:
        app.processEvents()
        now = time.perf_counter() - t0
        try:
            head = int(controller.current_frame)
        except Exception:
            head = 0
        samples.append((now, head, bool(controller.is_playing)))
        if audio_mgr is not None:
            try:
                ring_samples.append((now, audio_mgr.ring_stats()))
            except Exception:
                pass
            try:
                audio_time_samples.append((now, float(audio_mgr.get_audio_time())))
            except Exception:
                pass
        cpu_samples.append(proc.cpu_percent(None) if not os.environ.get("TOPOS_NO_PSUTIL") else 0.0)
        percpu_samples.append(
            _ps.cpu_percent(None, percpu=True) if not os.environ.get("TOPOS_NO_PSUTIL")
            else [0.0])
        _sample_thread_cpu()
        # 与视频基准驱动对齐（4ms）：processEvents 间隔直接决定 UI 事件
        # 批处理粒度，20ms 会拖慢呈现链，污染测量。
        time.sleep(0.004)

    still_playing = bool(controller.is_playing)
    try:
        controller.pause()
    except Exception:
        pass
    total_s = time.perf_counter() - t0

    # 可视化回归：暂停后截取主窗口，人工核对播放头/波形/表头渲染。
    if os.environ.get("TOPOS_SCREENSHOT"):
        try:
            app.processEvents()
            pix = mw.grab()
            out = os.environ["TOPOS_SCREENSHOT"]
            pix.save(out)
            log(f"screenshot saved: {out}")
        except Exception as e:
            log(f"screenshot failed: {e}")

    # ── analysis ──
    print("\n===== RESULTS =====", flush=True)
    if os.environ.get("TOPOS_PAINT_TRACE"):
        from tests.benchmark.drive_real_app_edit_tracks import _paint_stats
        if _paint_stats:
            print("UI paint costs (ms):", flush=True)
            for label, costs in sorted(_paint_stats.items()):
                if label.startswith("TimelineView.paint_rect"):
                    vs = sorted(costs)
                    print(f"  {label:<28} n={len(vs):>5} p50={vs[len(vs)//2]:>6} "
                          f"p95={vs[int(len(vs)*0.95)]:>6} max={vs[-1]:>6}", flush=True)
                    continue
                cs = sorted(costs)
                print(f"  {label:<28} n={len(cs):>5} p50={cs[len(cs)//2]:>6.2f} "
                      f"p95={cs[int(len(cs)*0.95)]:>7.2f} max={cs[-1]:>8.2f} "
                      f"sum={sum(cs)/1000:.2f}s", flush=True)
    print(f"video={VIDEO.name if VIDEO else 'none'} audio_tracks={n_audio} "
          f"timeline_fps={tl_fps} duration={total_s:.1f}s still_playing={still_playing}", flush=True)

    frames = [f for _, f, _ in samples]
    if frames:
        advanced = frames[-1] - frames[0]
        print(f"playhead: start={frames[0]} end={frames[-1]} advanced={advanced} "
              f"expected≈{int(tl_fps * total_s)}", flush=True)
        print(f"EFFECTIVE_FPS={advanced / max(total_s, 1e-6):.2f} "
              f"({advanced / max(tl_fps * total_s, 1e-6) * 100:.0f}% realtime)", flush=True)

    if emissions:
        em_times = [t for t, _ in emissions]
        distinct = len({f for _, f in emissions})
        gaps = [(em_times[i] - em_times[i - 1]) * 1000 for i in range(1, len(em_times))]
        gs = sorted(gaps) if gaps else [0]
        print(f"video presented: distinct={distinct} rate={distinct / max(total_s, 1e-6):.1f}/s "
              f"interval p50={gs[len(gs)//2]:.1f}ms p95={gs[int(len(gs)*0.95)]:.1f}ms "
              f"(budget {1000/tl_fps:.1f})", flush=True)
    else:
        print("video presented: (no video track)", flush=True)

    if ring_samples:
        first_rs, last_rs = ring_samples[0][1], ring_samples[-1][1]
        underruns = (last_rs.get('underruns', 0) or 0)
        overflows = (last_rs.get('overflows', 0) or 0)
        filled_vals = [rs.get('filled', 0) or 0 for _, rs in ring_samples[1:]]
        if filled_vals:
            fs = sorted(filled_vals)
            print(f"AUDIO_RING: underruns={underruns} overflows={overflows} "
                  f"filled p50={fs[len(fs)//2]} p10={fs[int(len(fs)*0.1)]} "
                  f"capacity={last_rs.get('capacity', '?')}", flush=True)
        else:
            print(f"AUDIO_RING: {last_rs}", flush=True)

    if len(audio_time_samples) > 10:
        (t1, a1), (t2, a2) = audio_time_samples[0], audio_time_samples[-1]
        wall = t2 - t1
        audio_advanced = a2 - a1
        print(f"AUDIO_CLOCK: advanced={audio_advanced:.2f}s over {wall:.2f}s wall "
              f"rate={audio_advanced / max(wall, 1e-6):.3f}x realtime", flush=True)
        # 平滑度：音频时钟步进分布（理想 = 匀速推进）
        steps = [(audio_time_samples[i][1] - audio_time_samples[i-1][1])
                 / max(audio_time_samples[i][0] - audio_time_samples[i-1][0], 1e-6)
                 for i in range(1, len(audio_time_samples))]
        st = sorted(steps)
        stalled = sum(1 for s in steps if s < 0.5)
        print(f"AUDIO_SMOOTHNESS: rate p10={st[int(len(st)*0.1)]:.3f} p50={st[len(st)//2]:.3f} "
              f"p90={st[int(len(st)*0.9)]:.3f} stalled_windows(<0.5x)={stalled}/{len(steps)}", flush=True)

    if cpu_samples:
        cs = sorted(cpu_samples)
        print(f"PROCESS_CPU: p50={cs[len(cs)//2]:.0f}% max={cs[-1]:.0f}% "
              f"(of {os.cpu_count()} cores)", flush=True)
    if percpu_samples:
        n_cores = len(percpu_samples[0])
        busy_cores = sum(
            1 for i in range(n_cores)
            if sum(pc[i] for pc in percpu_samples) / max(len(percpu_samples), 1) > 70)
        avg_core = sum(
            sum(pc) for pc in percpu_samples) / max(len(percpu_samples), 1) / n_cores
        print(f"CORES: avg_util={avg_core:.0f}% cores_busy(>70%)={busy_cores}/{n_cores}", flush=True)

    if _thread_cpu:
        deltas = {
            name: max(ts) - min(ts)
            for name, ts in _thread_cpu.items() if len(ts) > 1
        }
        top = sorted(deltas.items(), key=lambda kv: -kv[1])[:12]
        print("THREAD_CPU_SECONDS (top):", flush=True)
        for name, sec in top:
            print(f"  {name[:52]:<54} {sec:6.2f}s", flush=True)

    loop_stats = globals().get("_loop_stats")
    if loop_stats and loop_stats["gap_ms"]:
        def _pct(arr, p):
            s = sorted(arr)
            return s[min(len(s) - 1, int(len(s) * p))]
        gaps = loop_stats["gap_ms"]
        cycles = loop_stats["cycle_ms"]
        long_gaps = sum(1 for g in gaps if g > 60)
        print(f"AUDIO_LOOP: n={len(gaps)} gap p50={_pct(gaps,0.5):.1f}ms "
              f"p90={_pct(gaps,0.9):.1f}ms p99={_pct(gaps,0.99):.1f}ms "
              f"max={max(gaps):.0f}ms long(>60ms)={long_gaps}", flush=True)
        print(f"AUDIO_LOOP cycle_ms: p50={_pct(cycles,0.5):.2f} p90={_pct(cycles,0.9):.2f} "
              f"p99={_pct(cycles,0.99):.2f} max={max(cycles):.1f}", flush=True)
        # gap 减 cycle ≈ 线程等待（轮询 + GIL 等待）
        waits = [g - c for g, c in zip(gaps, cycles) if g > c]
        if waits:
            print(f"AUDIO_LOOP wait_ms(gap-cycle): p50={_pct(waits,0.5):.1f} "
                  f"p90={_pct(waits,0.9):.1f} p99={_pct(waits,0.99):.1f} max={max(waits):.0f}", flush=True)

    mix_stats = globals().get("_mix_stats")
    if mix_stats:
        ms = sorted(mix_stats)
        print(f"MIX_SAMPLES: n={len(ms)} p50={ms[len(ms)//2]:.2f}ms "
              f"p90={ms[int(len(ms)*0.9)]:.2f}ms p99={ms[int(len(ms)*0.99)]:.2f}ms "
              f"max={ms[-1]:.1f}ms", flush=True)

    print("===== END =====", flush=True)

    try:
        controller.pause()
    except Exception:
        pass
    for _ in range(10):
        app.processEvents()
    mw.close()
    for _ in range(10):
        app.processEvents()
    if os.environ.get("TOPOS_NATURAL_EXIT"):
        # 自然退出验证：不绕过解释器 finalization（退出崩溃回归检测用）。
        print("[DRIVER] natural exit (no os._exit)", flush=True)
        app.quit()
        return 0
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
