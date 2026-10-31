#!/usr/bin/env python3
"""Drive the REAL app composite page: video_frame + constant + merge → output.

Builds the minimal composition the user described (footage + constant merged
into output), starts comp-clip playback and measures:

  - viewer update rate at the composite_frame_ready boundary
  - playhead progression (sampled from page._composite_current_frame)
  - stall events: viewer frozen while playhead advances (render/decode
    starvation) and playhead itself frozen while playback is "playing"
    (UI-thread stall)
  - submitted vs completed render requests (cancellation starvation)

A real app window appears during the run. stdout captured by the caller.

Usage:
    .venv/bin/python tests/benchmark/drive_real_app_composite_playback.py \\
        [video_path] [seconds]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Same config isolation main.py applies for source checkouts.
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"

VIDEO = Path(sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT / "tests/test_videos/2K_H265.mov").resolve()
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
COMP_FPS = 24.0
COMP_DURATION_FRAMES = int(COMP_FPS * (SECONDS + 15))  # headroom so one-shot never ends early


def log(msg: str) -> None:
    print(f"[DRIVER] {msg}", flush=True)


def install_leak_trace() -> None:
    """Trace GpuDecodedFrame leaks back to their creation stack."""
    import traceback
    from src.shared.media.gpu_frame import GpuDecodedFrame

    orig_post = GpuDecodedFrame.__post_init__
    orig_del = GpuDecodedFrame.__del__
    birth: dict[int, str] = {}

    def traced_post(self):
        orig_post(self)
        if len(birth) < 4000:
            birth[id(self)] = "".join(traceback.format_stack(limit=8)[:-1])

    def traced_del(self):
        rel = getattr(self, "release", None)
        if rel is not None and not rel.is_released:
            tb = birth.pop(id(self), "<pre-trace>")
            print(f"[LEAK-TRACE] frame={self.frame_index} "
                  f"zero_copy={self.zero_copy} path={self.decode_path}\n{tb}",
                  flush=True)
        else:
            birth.pop(id(self), None)
        orig_del(self)

    GpuDecodedFrame.__post_init__ = traced_post
    GpuDecodedFrame.__del__ = traced_del


def install_pending_trace() -> None:
    """Log _vt_pending slot transitions to catch same-key overwrite leaks."""
    from src.features.composite.nodes.video_frame_node import VideoFrameNode

    orig = VideoFrameNode._decode_and_store

    def traced(self, frame_num, render_mode, generation, *, notify, scrub=False):
        result = orig(self, frame_num, render_mode, generation,
                      notify=notify, scrub=scrub)
        pending = self._vt_pending
        gpu = getattr(pending, "gpu_frame", None) if pending is not None else None
        print(f"[VT-STORE] req={frame_num} result={result} "
              f"pending_key={pending[0] if pending else None} "
              f"pending_fidx={getattr(gpu, 'frame_index', None)} "
              f"gen={generation}/{self._media_generation} "
              f"cur={self._current_frame}", flush=True)
        return result

    VideoFrameNode._decode_and_store = traced

    orig_df = VideoFrameNode._decode_frame

    def traced_df(self, frame_number, render_mode="preview", *,
                 background=False, scrub=None):
        td0 = time.perf_counter()
        result = orig_df(self, frame_number, render_mode,
                         background=background, scrub=scrub)
        cls = type(result).__name__
        print(f"[VT-DEC] req={frame_number} → {cls} "
              f"{(time.perf_counter()-td0)*1000:.1f}ms", flush=True)
        return result

    VideoFrameNode._decode_frame = traced_df

    from src.features.composite.core import video_context as vc_mod
    orig_csf = vc_mod.VideoContext._compute_source_frame

    def traced_csf(self, timeline_frame, fps, source_in, source_out,
                   time_offset, speed, loop, hold, total_frames):
        r = orig_csf(self, timeline_frame, fps, source_in, source_out,
                     time_offset, speed, loop, hold, total_frames)
        if r != timeline_frame:
            print(f"[MAP] tl={timeline_frame} fps={fps} in={source_in} "
                  f"out={source_out} off={time_offset} speed={speed} "
                  f"loop={loop} hold={hold} total={total_frames} → {r}",
                  flush=True)
        return r

    vc_mod.VideoContext._compute_source_frame = traced_csf

    orig_uf = VideoFrameNode.upload_frame

    def traced_uf(self, ctx, frame_number, render_mode="preview"):
        vt_gate = (not self._vt_gpu_disabled and self._use_service
                   and self._clip_id is not None
                   and self._video_context is not None)
        taken = self._vt_pending[0] if self._vt_pending is not None else None
        r = orig_uf(self, ctx, frame_number, render_mode)
        print(f"[VT-UP] req={frame_number} gate={vt_gate} "
              f"disabled={self._vt_gpu_disabled} pending={taken} "
              f"cur={self._current_frame} ok={r} "
              f"adv={self._requests_advancing()} "
              f"cache_n={len(self._frame_cache)}", flush=True)
        return r

    VideoFrameNode.upload_frame = traced_uf


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog

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

    # Headless guard: startup restore can pop a modal dialog (WelcomeDialog)
    # whose exec() blocks the first processEvents() call.
    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError:
        pass

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
    for _ in range(90):
        app.processEvents()
    # Let session restore / ProjectManagerDialog settle so it cannot race the
    # project we create below.
    settle_end = time.time() + 4.0
    while time.time() < settle_end:
        app.processEvents()
        time.sleep(0.02)
    log(f"booted page={mw.current_page} video={VIDEO.name} seconds={SECONDS}")

    # import media (creates the project)
    if os.environ.get("TOPOS_LEAK_TRACE"):
        install_leak_trace()
    if os.environ.get("TOPOS_VT_TRACE"):
        install_pending_trace()
    mw.media_pool_panel._import_files([str(VIDEO)])
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and project.get_asset_by_path(str(VIDEO)) is not None:
            break
        time.sleep(0.02)
    assert project is not None, "project not created"
    asset = project.get_asset_by_path(str(VIDEO))
    log(f"asset imported fps={asset.metadata.get('fps')} frames={asset.metadata.get('total_frames')}")

    MODE = os.environ.get("TOPOS_PLAYBACK_MODE", "page")  # page|timeline|edit|color
    NEEDS_COMP = MODE in ("page", "timeline")
    blur_radius = float(os.environ.get("TOPOS_BLUR_RADIUS", "0"))
    if NEEDS_COMP:
        # empty comp clip → Composite page
        new_project, comp_clip_id, comp_asset_id, graph_id = project.create_empty_comp_clip(
            name="PB Test", width=1920, height=1080, fps=COMP_FPS,
            duration_frames=COMP_DURATION_FRAMES,
            background_color=(0.0, 0.0, 0.0, 1.0))
        mw._current_edit_project = new_project
        for attempt in range(3):
            mw._switch_to_composite_page(graph_id)
            deadline = time.time() + 10
            while time.time() < deadline:
                app.processEvents()
                if getattr(mw, "_current_composite_id", None) == graph_id:
                    break
                time.sleep(0.05)
            if getattr(mw, "_current_composite_id", None) == graph_id:
                break
            log(f"switch attempt {attempt} failed "
                f"(composite_id={getattr(mw, '_current_composite_id', None)}), retrying")
        for _ in range(30):
            app.processEvents()

        page = mw.composite_page
        assert page is not None, "composite page missing"
        deadline = time.time() + 30
        while time.time() < deadline and getattr(page, "_renderer", None) is None:
            app.processEvents()
            time.sleep(0.02)
        renderer = page._renderer
        assert renderer is not None, "composite renderer not initialized"

        # wait until OUR graph is the one loaded (session restore can swap graphs)
        deadline = time.time() + 15
        while time.time() < deadline:
            graph = page._node_graph
            outs = graph.get_nodes_by_type("output") if graph is not None else []
            if outs and getattr(mw, "_current_composite_id", None) == graph_id:
                break
            app.processEvents()
            time.sleep(0.05)
        else:
            raise AssertionError("our comp graph never loaded "
                                 f"(current_composite_id={getattr(mw, '_current_composite_id', None)})")

        # ── build the user's graph: video_frame (+blur) + constant + merge → output ──
        actions = page._node_editor._actions
        consts = graph.get_nodes_by_type("constant")
        if consts:
            const_node = consts[0]
        else:
            const_node = actions.add_node("constant", -1200, 500)
            assert const_node is not None, "constant creation failed"
        out_node = graph.get_nodes_by_type("output")[0]
        video_node = actions.add_node("video_frame", -1100, 300,
                                      parameters={"file_path": str(VIDEO)})
        merge_node = actions.add_node("merge", -400, 300)
        assert video_node is not None and merge_node is not None, "node creation failed"
        actions.disconnect(const_node.id, "result", out_node.id, "image")

        if blur_radius > 0:
            blur_node = actions.add_node("blur", -750, 300,
                                         parameters={"radius_x": blur_radius,
                                                     "radius_y": blur_radius})
            ok1 = actions.connect(video_node.id, "image", blur_node.id, "image")
            ok1b = actions.connect(blur_node.id, "result", merge_node.id, "A")
            log(f"blur node added radius={blur_radius}: "
                f"video→blur={ok1} blur→merge.A={ok1b}")
        else:
            ok1 = actions.connect(video_node.id, "image", merge_node.id, "A")
        ok2 = actions.connect(const_node.id, "result", merge_node.id, "B")
        ok3 = actions.connect(merge_node.id, "result", out_node.id, "image")
        log(f"graph wired: src→merge.A={ok1} constant→merge.B={ok2} merge→output={ok3}")

        # let the first evaluation + video open settle (render thread opens the
        # clip synchronously on first evaluate)
        emissions: list[tuple[float, int, int]] = []
        page.composite_frame_ready.connect(
            lambda rid, tex, w, h: emissions.append((time.perf_counter(), rid, tex)))
        deadline = time.time() + 25
        while time.time() < deadline and not emissions:
            app.processEvents()
            time.sleep(0.01)
        log(f"first frame after {len(emissions)} emissions; "
            f"current_frame={page._composite_current_frame} total={page.comp_clip_total_frames()}")

        # instrument submissions
        submits = {"n": 0}
        orig_submit = renderer.submit

        def counted_submit(cmd, data=None):
            if data and data.get("request") is not None:
                submits["n"] += 1
            return orig_submit(cmd, data)

        renderer.submit = counted_submit

        if os.environ.get("TOPOS_UI_TRACE"):
            from src.features.composite.render import composite_renderer as cr_mod
            orig_handle = cr_mod.CompositeRenderer._handle_evaluate_graph
            handle_costs: list[float] = []

            def traced_handle(self, data):
                th0 = time.perf_counter()
                r = orig_handle(self, data)
                handle_costs.append((time.perf_counter() - th0) * 1000)
                return r

            cr_mod.CompositeRenderer._handle_evaluate_graph = traced_handle
            page._driver_handle_costs = handle_costs

            # phase breakdown inside the render thread
            orig_eval_snap = cr_mod.CompositeRenderer._evaluate_snapshot
            orig_render_out = cr_mod.CompositeRenderer._render_to_output
            phase_costs: dict[str, list[float]] = {"eval_snap": [], "render_out": []}

            def traced_snap(self, **kw):
                tp0 = time.perf_counter()
                r = orig_eval_snap(self, **kw)
                phase_costs["eval_snap"].append((time.perf_counter() - tp0) * 1000)
                return r

            def traced_out(self, result, request_id=0):
                tp0 = time.perf_counter()
                r = orig_render_out(self, result, request_id)
                phase_costs["render_out"].append((time.perf_counter() - tp0) * 1000)
                return r

            cr_mod.CompositeRenderer._evaluate_snapshot = traced_snap
            cr_mod.CompositeRenderer._render_to_output = traced_out
            page._driver_phase_costs = phase_costs

            from src.features.composite.evaluators.blur_evaluator import BlurEvaluatorMixin
            orig_blur = BlurEvaluatorMixin._apply_blur

            def traced_blur(self, input_tex, params, w, h):
                tb0 = time.perf_counter()
                r = orig_blur(self, input_tex, params, w, h)
                phase_costs.setdefault("blur", []).append(
                    (time.perf_counter() - tb0) * 1000)
                return r

            BlurEvaluatorMixin._apply_blur = traced_blur

            # fine-grained helpers used inside blur path
            def wrap_method(cls, method_name, tag):
                orig_m = getattr(cls, method_name, None)
                if orig_m is None:
                    return

                def wrapped(*a, **kw):
                    tw0 = time.perf_counter()
                    r = orig_m(*a, **kw)
                    phase_costs.setdefault(tag, []).append(
                        (time.perf_counter() - tw0) * 1000)
                    return r
                setattr(cls, method_name, wrapped)

            pool = getattr(renderer, "_resource_pool", None) if NEEDS_COMP else None
            from src.features.composite.render.gpu_resource_pool import GPUResourcePool
            wrap_method(GPUResourcePool, "acquire_texture", "pool.acquire")
            wrap_method(GPUResourcePool, "release_texture", "pool.release")
            wrap_method(GPUResourcePool, "_acquire_texture", "pool._acquire")
            wrap_method(GPUResourcePool, "insert_fence", "pool.fence")
            wrap_method(GPUResourcePool, "get_fbo", "pool.get_fbo")

            from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
            wrap_method(BaseEvaluatorMixin, "_bind_external_texture", "bind_ext")
            wrap_method(BaseEvaluatorMixin, "_render_quad", "render_quad")
            wrap_method(BaseEvaluatorMixin, "_get_texture_by_id", "get_tex_by_id")

            t0 = time.perf_counter()
            orig_setf = type(page).set_composite_current_frame
            setf_calls: list[tuple[float, float]] = []

            def traced_setf(self, frame):
                ts0 = time.perf_counter()
                orig_setf(self, frame)
                setf_calls.append((ts0 - t0, (time.perf_counter() - ts0) * 1000))
                return None

            type(page).set_composite_current_frame = traced_setf
            page._driver_setf_calls = setf_calls

            orig_ready = type(page)._on_frame_ready
            ready_costs: list[float] = []

            def traced_ready(self, request_id, tex_id, width, height):
                tr0 = time.perf_counter()
                orig_ready(self, request_id, tex_id, width, height)
                ready_costs.append((time.perf_counter() - tr0) * 1000)

            type(page)._on_frame_ready = traced_ready
            page._driver_ready_costs = ready_costs

    # ── play and measure ──
    ui_ticks: list[tuple[float, float]] = []   # (t, eval_cost)
    if os.environ.get("TOPOS_UI_TRACE"):
        orig_tick = type(page)._on_comp_playback_tick
        orig_eval = type(page)._do_evaluate_graph

        def traced_tick(self):
            ui_ticks.append((time.perf_counter() - t0, -1.0))
            orig_tick(self)

        def traced_eval(self):
            te0 = time.perf_counter()
            orig_eval(self)
            ui_ticks.append((time.perf_counter() - t0,
                             (time.perf_counter() - te0) * 1000))

        type(page)._on_comp_playback_tick = traced_tick
        type(page)._do_evaluate_graph = traced_eval

    tl_emissions: list[tuple[float, int]] = []
    tl_comp_costs: list[float] = []

    if MODE in ("edit", "color"):
        # ── edit: plain footage clip + blur effect on the Edit timeline.
        #    color: same clip sent to the Color page + BlurSharpen optical blur.
        #    与合成页共用同一素材、同等模糊强度，做三页管线对比。
        mw.switch_to_page("Edit")
        for _ in range(60):
            app.processEvents()

        tv = mw.timeline_panel.timeline_view
        video_asset = project.get_asset_by_path(str(VIDEO))
        assert video_asset is not None, "video asset missing"
        tv._ensure_first_asset_timeline_settings(video_asset)
        for _ in range(20):
            app.processEvents()
        timeline = mw.edit_page._current_timeline or tv._create_default_timeline(video_asset)
        ok = tv._add_asset_as_clip(video_asset, timeline.video_tracks[0], 0)
        deadline = time.time() + 20
        while time.time() < deadline:
            app.processEvents()
            if mw.timeline_preview_renderer is not None:
                break
            time.sleep(0.02)
        # _add_asset_as_clip 写的是 timeline_view 自己的 _timeline；与会话
        # 恢复出来的 edit_page._current_timeline 可能不是同一对象。
        src = getattr(tv, "_timeline", None) or timeline
        if src is not None:
            timeline = src
        clips = timeline.video_tracks[0].clips if timeline.video_tracks else []
        if not clips:
            # 兜底：等 UI 命令异步落地
            for _ in range(50):
                app.processEvents()
                clips = timeline.video_tracks[0].clips if timeline.video_tracks else []
                if clips:
                    break
                time.sleep(0.02)
        assert clips, "clip not added"
        clip = clips[-1]
        # edit_page/_current_timeline 可能还是会话恢复的旧对象；特效添加等
        # controller 入口都读它——统一引用，保证三处（view/page/controller）
        # 看到同一条时间线。
        try:
            mw.edit_page._current_timeline = timeline
        except Exception:
            pass
        log(f"{MODE} mode: clip added ok={ok} start={clip.timeline_start}")
        for _ in range(30):
            app.processEvents()

        if MODE == "edit":
            # controller 入口在会话恢复环境下静默失败，直接走命令 API
            from src.features.edit.commands_facade import get_command_classes
            _cmds = get_command_classes()
            AddEffectCommand = _cmds["AddEffectCommand"]
            cmd = AddEffectCommand.create(
                track_id=clip.track_id, clip_id=clip.clip_id,
                effect_type="blur")
            new_timeline = cmd.apply(timeline)
            timeline = new_timeline or timeline
            try:
                mw.edit_page._current_timeline = timeline
            except Exception:
                pass
            mw._sync_timeline_to_views(timeline)
            for _ in range(10):
                app.processEvents()
            clip2 = next(c for c in timeline.video_tracks[0].clips
                         if c.clip_id == clip.clip_id)
            effs = [e for e in clip2.effects if e.effect_type == "blur"]
            assert effs, f"blur effect not attached ({clip2.effects})"
            eff = effs[-1]
            mw._on_edit_effect_param_changed(clip.clip_id, eff.effect_id,
                                             "radius_x", blur_radius)
            mw._on_edit_effect_param_changed(clip.clip_id, eff.effect_id,
                                             "radius_y", blur_radius)
            for _ in range(20):
                app.processEvents()
            log(f"edit mode: blur effect radius={blur_radius} added")
            tl_renderer = mw.timeline_preview_renderer
            tl_renderer.frame_ready.connect(
                lambda frame, result: tl_emissions.append(
                    (time.perf_counter() - t0, int(frame))))
            controller = mw.timeline_panel.get_playback_controller()
            t0 = time.perf_counter()
            controller.play()
            samples = []
            while time.perf_counter() - t0 < SECONDS:
                app.processEvents()
                try:
                    head = int(controller.current_frame)
                except Exception:
                    head = 0
                samples.append((time.perf_counter() - t0, head,
                                bool(controller.is_playing)))
                time.sleep(0.004)
            still_playing = bool(controller.is_playing)
            try:
                controller.pause()
            except Exception:
                pass
            total_s = time.perf_counter() - t0
        else:
            mw.open_timeline_clip_in_color(
                timeline_id=timeline.timeline_id, clip_id=clip.clip_id,
                timeline_frame=0)
            for _ in range(60):
                app.processEvents()
            if blur_radius > 0:
                blur_params = {'mode': 0, 'sharpen': 0.0, 'texture': 0.0,
                               'halo': 0.0, 'radius': 0.0, 'iterations': 1,
                               'blur_enabled': True, 'blur_mode': 1,
                               'blur_intensity': 0.5, 'blur_detail': 0.0,
                               'blur_radius': 6.0, 'blur_bokeh': 0.0,
                               'blur_angle': 0.0}
                mw.on_blur_sharpen_changed(blur_params)
                log(f"color mode: optical blur radius=6 enabled")
            for _ in range(20):
                app.processEvents()
            vc = mw.video_controller
            assert vc is not None, "video controller missing"
            vc.frame_changed.connect(
                lambda f: tl_emissions.append(
                    (time.perf_counter() - t0, int(f))))
            log("color mode: playing")
            t0 = time.perf_counter()
            mw.on_video_play_toggled()
            samples = []
            while time.perf_counter() - t0 < SECONDS:
                app.processEvents()
                try:
                    head = int(vc.current_frame)
                except Exception:
                    head = -1
                samples.append((time.perf_counter() - t0, head, True))
                time.sleep(0.004)
            still_playing = True
            try:
                mw.video_controller.stop_playback()
            except Exception:
                pass
            total_s = time.perf_counter() - t0

    elif MODE == "timeline":
        # ── timeline-backed: put the comp clip asset on the Edit timeline,
        #    play through the Edit transport, measure the preview renderer.
        from src.features.edit.core.timeline_preview_renderer import (
            PreviewRenderWorker)

        # ensure phase_costs exists even without TOPOS_UI_TRACE
        if "phase_costs" not in dir():
            phase_costs = getattr(page, "_driver_phase_costs", None) or {}
            page._driver_phase_costs = phase_costs

        orig_comp_frame = PreviewRenderWorker._render_comp_clip_frame

        def traced_comp_frame(self, layer):
            tc0 = time.perf_counter()
            r = orig_comp_frame(self, layer)
            tl_comp_costs.append((time.perf_counter() - tc0) * 1000)
            return r

        PreviewRenderWorker._render_comp_clip_frame = traced_comp_frame

        # 回路事件流：请求时点 + comp 渲染开始/结束
        t0 = time.perf_counter()
        loop_events: list[tuple[float, str, int]] = []
        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer as TPR)
        orig_req = TPR.request_render

        def traced_request(self, frame):
            loop_events.append((time.perf_counter() - t0, "req", int(frame)))
            return orig_req(self, frame)

        TPR.request_render = traced_request

        def traced_comp_frame2(self, layer):
            loop_events.append(
                (time.perf_counter() - t0, "start", int(layer.source_frame)))
            tc0 = time.perf_counter()
            r = orig_comp_frame(self, layer)
            loop_events.append(
                (time.perf_counter() - t0, "end", int(layer.source_frame)))
            tl_comp_costs.append((time.perf_counter() - tc0) * 1000)
            return r

        # 替换先前的 traced_comp_frame，让事件也记录在内
        PreviewRenderWorker._render_comp_clip_frame = traced_comp_frame2
        page._driver_loop_events = loop_events

        def wrap2(cls, name, tag):
            orig = getattr(cls, name, None)
            if orig is None:
                return

            def wrapped(self, *a, **kw):
                tw0 = time.perf_counter()
                r = orig(self, *a, **kw)
                phase_costs.setdefault(tag, []).append(
                    (time.perf_counter() - tw0) * 1000)
                return r
            setattr(cls, name, wrapped)

        wrap2(PreviewRenderWorker, "_composite_layers", "tl.cpu_composite")

        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer as CFR_cls)
        wrap2(CFR_cls, "_build_snapshot", "tl.build_snapshot")
        wrap2(CFR_cls, "render_comp_clip_frame", "tl.cfr_body")
        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer as CER_cls)
        wrap2(CER_cls, "_evaluate_snapshot", "tl.eval_snapshot")
        wrap2(CER_cls, "_render_full_frame", "tl.full_frame")
        VFN = __import__(
            "src.features.composite.nodes.video_frame_node",
            fromlist=["VideoFrameNode"]).VideoFrameNode
        orig_dec = VFN._decode_frame

        def traced_decode(self, frame_number, render_mode="preview", **kw):
            td0 = time.perf_counter()
            r = orig_dec(self, frame_number, render_mode, **kw)
            ms = (time.perf_counter() - td0) * 1000
            phase_costs.setdefault("tl.decode", []).append(ms)
            loop_events.append((time.perf_counter() - t0, "dec",
                                int(frame_number) * 10 + (id(self) % 1000 < 500 and 0 or 1) if False else int(frame_number)))
            dec_ids = getattr(self, "_drv_id", None) or f"{id(self)%10000}"
            self._drv_id = dec_ids
            print(f"[DEC] frame={frame_number} node={dec_ids} "
                  f"clip={getattr(self, '_clip_id', None)} "
                  f"ms={ms:.1f}", flush=True)
            return r

        VFN._decode_frame = traced_decode
        wrap2(PreviewRenderWorker, "_render_frame", "tl.render_frame")
        wrap2(PreviewRenderWorker, "_try_gpu_render", "tl.try_gpu")

        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer as CFrameRenderer)
        orig_cfr = CFrameRenderer.render_comp_clip_frame

        def traced_cfr(self, *a, **kw):
            tc0 = time.perf_counter()
            r = orig_cfr(self, *a, **kw)
            phase_costs.setdefault("tl.cfr_total", []).append(
                (time.perf_counter() - tc0) * 1000)
            return r

        CFrameRenderer.render_comp_clip_frame = traced_cfr

        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer as CExportRenderer)
        orig_readback = CExportRenderer._readback

        def traced_readback(self, result):
            tc0 = time.perf_counter()
            r = orig_readback(self, result)
            phase_costs.setdefault("tl.readback", []).append(
                (time.perf_counter() - tc0) * 1000)
            return r

        CExportRenderer._readback = traced_readback

        mw.switch_to_page("Edit")
        for _ in range(60):
            app.processEvents()

        comp_asset = new_project.get_asset_by_id(comp_asset_id)
        assert comp_asset is not None, "comp clip asset missing"
        tv = mw.timeline_panel.timeline_view
        tv._ensure_first_asset_timeline_settings(comp_asset)
        for _ in range(20):
            app.processEvents()
        timeline = mw.edit_page._current_timeline or tv._create_default_timeline(comp_asset)
        tv._add_asset_as_clip(comp_asset, timeline.video_tracks[0], 0)
        deadline = time.time() + 20
        while time.time() < deadline:
            app.processEvents()
            if mw.timeline_preview_renderer is not None:
                break
            time.sleep(0.02)
        tl_renderer = mw.timeline_preview_renderer
        assert tl_renderer is not None, "timeline preview renderer missing"
        tl_renderer.frame_ready.connect(
            lambda frame, result: tl_emissions.append(
                (time.perf_counter() - t0, int(frame))))

        controller = mw.timeline_panel.get_playback_controller()
        if os.environ.get("TOPOS_UI_TRACE"):
            import traceback as _tb
            ctl_cls = type(controller)

            def traced_pause(self, *a, **kw):
                print("[PAUSE-CALL]\n" + "".join(
                    _tb.format_stack(limit=8)[:-1]), flush=True)
                return ctl_cls.pause(self, *a, **kw)

            def traced_stop(self, *a, **kw):
                print("[STOP-CALL]\n" + "".join(
                    _tb.format_stack(limit=8)[:-1]), flush=True)
                return ctl_cls.stop(self, *a, **kw)

            ctl_cls.pause = traced_pause
            ctl_cls.stop = traced_stop
        try:
            clips = timeline.video_tracks[0].clips
            log(f"timeline clips={len(clips)} "
                + " ".join(f"[{c.timeline_start}-{c.timeline_end}]" for c in clips))
            log(f"timeline duration={getattr(timeline, 'duration_frames', '?')} "
                f"fps={getattr(timeline, 'fps', '?')}")
        except Exception as e:
            log(f"timeline info failed: {e}")
        log("timeline mode: playing via Edit transport")
        t0 = time.perf_counter()
        controller.play()
        samples: list[tuple[float, int, bool]] = []
        frozen_reported = False
        while time.perf_counter() - t0 < SECONDS:
            app.processEvents()
            try:
                head = int(controller.current_frame)
            except Exception:
                head = int(getattr(timeline, "current_frame", 0) or 0)
            samples.append((time.perf_counter() - t0, head,
                            bool(controller.is_playing)))
            # 冻结诊断：播放头连续 3 秒不动时快照内部状态
            if not frozen_reported and len(samples) > 500:
                recent = [f for _, f, _ in samples[-500:]]
                if max(recent) - min(recent) <= 1 and recent[-1] == recent[0]:
                    frozen_reported = True
                    r = tl_renderer
                    eng = getattr(r, "_realtime_engine", None)
                    print(f"\n[FREEZE] playhead stuck at {recent[0]}", flush=True)
                    try:
                        print(f"  controller.state={controller.state} "
                              f"is_playing={controller.is_playing}", flush=True)
                        print(f"  renderer: in_flight={r._render_in_flight} "
                              f"pending={getattr(r, '_pending_frame', '?')} "
                              f"queued={getattr(r, '_queued_frame', '?')} "
                              f"last_emitted={r._last_emitted_frame}", flush=True)
                        if eng is not None:
                            print(f"  engine: state={eng._state} "
                                  f"mode={eng._mode} "
                                  f"consec_miss={eng._consecutive_misses}",
                                  flush=True)
                            budget = getattr(eng, "_budget", None)
                            if budget is not None:
                                print(f"  budget: pressure={budget.is_back_pressured} "
                                      f"in_flight={budget._in_flight_start is not None}",
                                      flush=True)
                    except Exception as e:
                        print(f"  [FREEZE-DIAG error: {e}]", flush=True)
            time.sleep(0.004)
        still_playing = bool(controller.is_playing)
        try:
            controller.pause()
        except Exception:
            pass
        total_s = time.perf_counter() - t0
    else:
        page.comp_seek(0)
        t0 = time.perf_counter()
        emissions.clear()
        submits["n"] = 0
        page.comp_play()
        samples: list[tuple[float, int, bool]] = []
        while time.perf_counter() - t0 < SECONDS:
            app.processEvents()
            samples.append((time.perf_counter() - t0,
                            page._composite_current_frame,
                            bool(getattr(page, "_comp_playing", False))))
            time.sleep(0.004)

        still_playing = bool(getattr(page, "_comp_playing", False))
        page.comp_pause()
        total_s = time.perf_counter() - t0

    # ── analysis ──
    print("\n===== RESULTS =====", flush=True)
    active = [(t, f, p) for t, f, p in samples]
    n = len(active)
    print(f"mode={MODE} duration={total_s:.1f}s still_playing_at_end={still_playing}")

    if MODE in ("timeline", "edit", "color"):
        distinct = len({f for _, f in tl_emissions})
        print(f"preview frames: {len(tl_emissions)} "
              f"rate={len(tl_emissions) / max(total_s, 1e-6):.1f}/s "
              f"distinct={distinct}")
        frames = [f for _, f, _ in active]
        print(f"timeline playhead: start={frames[0]} end={frames[-1]} "
              f"expected≈{int(COMP_FPS * total_s)}")
        if tl_comp_costs:
            cs = sorted(tl_comp_costs)
            print(f"comp-frame render: n={len(cs)} "
                  f"p50={cs[len(cs)//2]:.1f}ms p95={cs[int(len(cs)*0.95)]:.1f}ms "
                  f"max={cs[-1]:.1f}ms sum={sum(cs)/1000:.1f}s")
        loop_events = (getattr(page, "_driver_loop_events", None)
                       if NEEDS_COMP else None) or []
        if len(loop_events) > 10:
            starts = [(t, f) for t, k, f in loop_events if k == "start"]
            ends = [(t, f) for t, k, f in loop_events if k == "end"]
            reqs = [(t, f) for t, k, f in loop_events if k == "req"]

            def _p(arr):
                if not arr:
                    return "n/a"
                arr2 = sorted(arr)
                return (f"p50={arr2[len(arr2)//2]:.1f}ms "
                        f"p95={arr2[int(len(arr2)*0.95)]:.1f}ms")

            gaps_render = [(starts[i][0] - ends[i-1][0]) * 1000
                           for i in range(1, min(len(starts), len(ends)))]
            gaps_emit = [(tl_emissions[i][0] - ends[i][0]) * 1000
                         for i in range(min(len(tl_emissions), len(ends)))]
            gaps_req = []
            for i in range(1, min(len(reqs), len(starts))):
                for j in range(len(reqs) - 1, -1, -1):
                    if reqs[j][1] == starts[i][1] and reqs[j][0] <= starts[i][0]:
                        gaps_req.append((starts[i][0] - reqs[j][0]) * 1000)
                        break
            inter = [(starts[i][0] - starts[i-1][0]) * 1000
                     for i in range(1, len(starts))]
            print(f"  loop: req->render-start {_p(gaps_req)}")
            print(f"  loop: render-end->next-start {_p(gaps_render)}")
            print(f"  loop: render-end->emit {_p(gaps_emit)}")
            print(f"  loop: render interval {_p(inter)} "
                  f"({len(starts)/total_s:.1f}/s)")
            decs = [(t, f) for t, k, f in loop_events if k == "dec"]
            if decs:
                print(f"  decode seq (last 24): "
                      + " ".join(f"{f}@{t:.2f}" for t, f in decs[-24:]))
                # 每个渲染帧的解码次数
                from collections import Counter
                per_frame = Counter()
                for _, f in decs:
                    per_frame[f] += 1
                multi = {f: c for f, c in per_frame.items() if c > 1}
                print(f"  decode per-frame: total={len(decs)} "
                      f"frames={len(per_frame)} multi={len(multi)} "
                      f"sample={dict(list(multi.items())[:8])}")
        tl_phases = (getattr(page, "_driver_phase_costs", None)
                     if NEEDS_COMP else None) or {}
        for name in sorted(tl_phases):
            costs = tl_phases.get(name) or []
            if costs:
                cs = sorted(costs)
                print(f"  phase {name}: n={len(cs)} p50={cs[len(cs)//2]:.1f}ms "
                      f"p95={cs[int(len(cs)*0.95)]:.1f}ms")
        em_times = [t for t, _ in tl_emissions]
        if len(em_times) > 1:
            gaps = [(em_times[i] - em_times[i-1], em_times[i-1])
                    for i in range(1, len(em_times))]
            worst = max(gaps)
            print(f"preview max gap: {worst[0]:.2f}s @t={worst[1]:.1f}s")
            print(f"preview gaps > 1s: {len([g for g in gaps if g[0] > 1.0])}")
        for s in range(0, int(total_s), 5):
            em = sum(1 for t, _ in tl_emissions if s <= t < s + 5)
            head = next((f for t, f, _ in active if t >= s), -1)
            print(f"  {s:3d}-{s+5:<3d}s frames={em/5:.1f}/s playhead≈{head}")
        raise SystemExit(0)

    print(f"viewer emissions: {len(emissions)}  "
          f"rate={len(emissions) / max(total_s, 1e-6):.1f}/s")
    print(f"render submits:   {submits['n']}  "
          f"(completed/submit = {len(emissions) / max(submits['n'], 1):.0%})")

    # playhead progression
    frames = [f for _, f, _ in active]
    print(f"playhead: start={frames[0]} end={frames[-1]} "
          f"expected≈{int(COMP_FPS * total_s)}")

    # stall A: viewer frozen while playhead advances
    em_times = [t for t, _, _ in emissions]
    gaps = [(em_times[i] - em_times[i - 1], em_times[i - 1] - t0)
            for i in range(1, len(em_times))]
    if gaps:
        worst = max(gaps)
        print(f"viewer max gap: {worst[0]:.2f}s @t={worst[1]:.1f}s")
        big = [g for g in gaps if g[0] > 1.0]
        print(f"viewer gaps > 1s: {len(big)} " +
              str([f"{g[0]:.1f}s@{g[1]:.1f}" for g in big[:10]]))
    else:
        print("viewer max gap: NO EMISSIONS AT ALL")

    # stall B: playhead itself frozen while playing
    stalls: list[tuple[float, float, int]] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and frames[j + 1] == frames[i]:
            j += 1
        if j - i >= 2 and active[i][0] - t0 > 2.0:  # ignore initial open
            dur = active[j][0] - active[i][0]
            if dur > 1.0:
                stalls.append((active[i][0] - t0, dur, frames[i]))
        i = j + 1
    print(f"playhead stalls > 1s (while playing): {len(stalls)} " +
          str([f"@{s[0]:.1f}s for {s[1]:.1f}s (frame {s[2]})" for s in stalls[:10]]))

    # per-second buckets
    handle_costs = (getattr(page, "_driver_handle_costs", None)
                    if NEEDS_COMP else None)
    if handle_costs:
        cs = sorted(handle_costs)
        print(f"\nrender-thread evaluate: n={len(cs)} "
              f"({len(cs)/total_s:.1f}/s) p50={cs[len(cs)//2]:.1f}ms "
              f"p95={cs[int(len(cs)*0.95)]:.1f}ms max={cs[-1]:.1f}ms "
              f"sum={sum(cs)/1000:.1f}s")
    phase_costs = getattr(page, "_driver_phase_costs", None)
    if phase_costs:
        for name, costs in phase_costs.items():
            if not costs:
                continue
            cs = sorted(costs)
            print(f"  phase {name}: n={len(cs)} p50={cs[len(cs)//2]:.2f}ms "
                  f"p95={cs[int(len(cs)*0.95)]:.2f}ms max={cs[-1]:.1f}ms "
                  f"sum={sum(cs)/1000:.2f}s")
    pool = getattr(renderer, "_resource_pool", None) if NEEDS_COMP else None
    if pool is not None:
        pooled = sum(len(v) for v in getattr(pool, "_pooled_textures", {}).values())
        print(f"\nresource pool: created={pool._total_created} "
              f"reused={pool._total_reused} "
              f"active={len(pool._active_textures)} pooled={pooled} "
              f"mem={pool.memory_usage_bytes()/1048576:.0f}MB")
    setf_calls = getattr(page, "_driver_setf_calls", None)
    if setf_calls:
        costs = sorted(c for _, c in setf_calls)
        print(f"set_composite_current_frame: n={len(setf_calls)} "
              f"({len(setf_calls)/total_s:.1f}/s) p50={costs[len(costs)//2]:.2f}ms "
              f"p95={costs[int(len(costs)*0.95)]:.2f}ms max={costs[-1]:.1f}ms")
    if ui_ticks:
        ticks = [t for t, c in ui_ticks if c < 0]
        evals = [(t, c) for t, c in ui_ticks if c >= 0]
        print(f"\nUI ticks: {len(ticks)} ({len(ticks) / total_s:.1f}/s); "
              f"evals: {len(evals)} ({len(evals) / total_s:.1f}/s)")
        if evals:
            costs = sorted(c for _, c in evals)
            print(f"eval cost ms: p50={costs[len(costs)//2]:.1f} "
                  f"p95={costs[int(len(costs)*0.95)]:.1f} "
                  f"max={costs[-1]:.1f}")
        for s in range(0, int(total_s), 5):
            tks = sum(1 for t in ticks if s <= t < s + 5)
            evs = [c for t, c in evals if s <= t < s + 5]
            avg_ev = sum(evs) / len(evs) if evs else 0
            print(f"  {s:3d}-{s+5:<3d}s ticks={tks/5:.1f}/s "
                  f"evals={len(evs)/5:.1f}/s avg_cost={avg_ev:.1f}ms")

    print("\nper-second: t | playhead | emissions")
    sec_frames: dict[int, int] = {}
    for t, f, _ in active:
        s = int(t)
        sec_frames[s] = f
    sec_em: dict[int, int] = {}
    for t, _, _ in emissions:
        sec_em[int(t - t0)] = sec_em.get(int(t - t0), 0) + 1
    for s in sorted(set(sec_frames) | set(sec_em)):
        print(f"  {s:3d}s | frame {sec_frames.get(s, -1):5d} | {sec_em.get(s, 0)} emissions")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
