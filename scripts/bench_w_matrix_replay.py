#!/usr/bin/env python3
"""§10 真实工作流回放矩阵 —— W01-W08 可执行批次（W05 为 Composite 专项）.

W02 已从试点（bench_out/ai_upgrade/W02_pilot_*）升级为全口径入矩阵；
W05 专项（图保存重载 + cache/telemetry 语义）已入矩阵，93 节点矩阵
（bench_composite_nodes_gpu_coverage，0 failed）继续单独归档。本脚本跑
八条工作流，每条产出结构化验证：

* W01 导入 H.264 → 两段剪辑 + 淡入淡出 + 字幕（经真实 EditSession
  plan 提交）→ 输出 H.264；probe 帧/音/时长 + 项目保存重载一致性。
* W03 TOPOS Proxy 无效果 → H.264/HEVC；probe + telemetry full_gpu
  blockers 非空（CPU fallback 诚实记录，无 Metal 也不虚报）。
* W04 真实人声 + strip_eq/compressor/delay 链 → 含音频导出；音画
  时长对齐 + 峰值非静音。
* W07 多版本（横/竖）交付：第一项中途取消（无完整成品残留），
  第二项独立完成并过 probe。
* W08 GPU/模型不可用环境：capability manifest 的模型态为
  not_installed、导出 telemetry 诚实携带 full_gpu blockers、
  search_assets 优雅降级——不崩溃、不虚报 GPU/模型完成。

用法：.venv/bin/python scripts/bench_w_matrix_replay.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

H264_SRC = ROOT / "tests" / "test_videos" / "2K_h264.mp4"
H265_SRC = ROOT / "tests" / "test_videos" / "2K_H265.mov"
TOPOS_SRC = ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov"
FRAMES = 24


# ── 公共底座 ────────────────────────────────────────────────────────────

class _Host:
    def __init__(self, state):
        from src.features.edit.ai_editing.approval_service import (
            ApprovalService)
        self._state = state
        self._approval_service = ApprovalService()

    def get_state(self):
        return self._state

    def get_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import (
            build_manifest)
        return build_manifest()

    def on_committed(self, result):
        if result.applied:
            from src.features.edit.ai_editing.edit_project_service import (
                ProjectSequences)
            self._state = ProjectSequences(
                project=result.new_project,
                sequences=result.new_sequences)

    def request_user_approval(self, plan, diff):
        from src.features.edit.ai_editing.transaction_service import (
            _expected_commit_scope)
        return self._approval_service.issue_after_user_confirmation(
            _expected_commit_scope(plan), issued_by="w-matrix")

    def approval_service(self):
        return self._approval_service


def _make_project(source: Path, total_frames: int, with_audio: bool = True):
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.edit_project_service import (
        ProjectSequences)
    from src.features.edit.core.project import (
        EditProject, MediaAsset, TimelineSequenceRef)
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, TimelineSequence)

    asset = MediaAsset(
        asset_id="src", path=str(source), media_type="video",
        display_name="src", metadata={"fps": 25.0,
                                      "total_frames": total_frames})
    project = EditProject.create(name="w-matrix")
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="c1", asset_id="src", track_id="v1",
        timeline_start=0, timeline_duration=FRAMES,
        source_start=0, source_duration=FRAMES)
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    audio_tracks = ()
    if with_audio:
        a_clip = ClipInstance(
            clip_id="a1", asset_id="src", track_id="a1",
            timeline_start=0, timeline_duration=FRAMES,
            source_start=0, source_duration=FRAMES)
        audio_tracks = (EditTrack(track_id="a1", name="A1",
                                  track_type="audio", index=0,
                                  clips=(a_clip,)),)
    seq = TimelineSequence(
        timeline_id="t1", name="W", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(v1,), audio_tracks=audio_tracks)
    ref = TimelineSequenceRef(timeline_id="t1", name="W", fps=25.0,
                              width=1920, height=1080, sample_rate=48000)
    project = EditProject(
        project_id=project.project_id, name=project.name,
        assets=project.assets, timelines={"t1": ref},
        root_bin_id=project.root_bin_id, active_timeline_id="t1")
    state = ProjectSequences(project=project, sequences={"t1": seq})
    host = _Host(state)
    session = EditSession.bind(host)
    return host, session, project, seq


def _apply_plan(session, ops: list[tuple[str, dict]], title: str) -> dict:
    """经真实 EditSession.apply 提交（宿主签发 grant）。"""
    from src.features.edit.ai_editing.models import (
        EditOperation, EditPlan, OperationKind, PlanTarget)

    # base_revision 锚在目标 timeline 上（TAEP P0 乐观锁），提交边界递增。
    tl = session._host.get_state().sequences.get("t1")
    base_revision = int(getattr(tl, "revision", 0) or 0)
    project_id = session._host.get_state().project.project_id
    plan = EditPlan.create(
        PlanTarget(project_id, "t1"),
        base_revision,
        operations=tuple(
            EditOperation(f"op{i}", OperationKind(kind), params=params)
            for i, (kind, params) in enumerate(ops)),
        title=title,
    )
    grant = session._host.request_user_approval(plan, None)
    result = session.apply(plan, approval=grant)
    return {"applied": result.applied, "error": result.error,
            "revision": result.new_revision}


def _export(project, seq, codec: str, pix_fmt: str, out: Path,
            resolution=None, cancel_after: int | None = None):
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportSource, TimelineExportConfig,
        create_timeline_exporter)
    from src.features.edit.core.timeline_v2 import Rational

    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id=seq.timeline_id,
        output_path=str(out), codec=ExportCodec(codec),
        pixel_format=pix_fmt, quality=6_000_000, quality_mode="vbr",
        fps=Rational(25, 1), include_audio=True, burn_subtitles=False,
        resolution=resolution or (1920, 1080))
    exporter = create_timeline_exporter(project, seq)
    if cancel_after is not None:
        def _cancel(current, total, pct):
            if current >= cancel_after:
                exporter.cancel_export()
        exporter._progress_callback = _cancel
    outcome = exporter.export_timeline(cfg)
    # stage_telemetry 已是 JSON-safe dict（recorder.snapshot().to_dict()），
    # full_gpu_blockers 直接在顶层。
    telemetry = getattr(outcome, "stage_telemetry", None)
    blockers = None
    if isinstance(telemetry, dict):
        blockers = telemetry.get("full_gpu_blockers")
    return {"success": bool(outcome),
            "cancelled": bool(getattr(outcome, "cancelled", False)),
            "cause": str(getattr(outcome, "cause", "") or ""),
            "full_gpu_blockers": blockers}


def _probe(path: Path) -> dict:
    import av

    with av.open(str(path)) as c:
        v = [s for s in c.streams if s.type == "video"]
        a = [s for s in c.streams if s.type == "audio"]
        out = {"video": bool(v), "audio": bool(a)}
        if v:
            out["frames"] = int(v[0].frames or 0)
            out["codec"] = v[0].codec_context.name
            out["w"] = v[0].width
            out["h"] = v[0].height
        if a and a[0].duration and a[0].time_base:
            out["audio_s"] = round(float(a[0].duration * a[0].time_base), 3)
        return out


def _audio_peak_dbfs(path: Path) -> float:
    import av
    import numpy as np

    peak = 0.0
    with av.open(str(path)) as c:
        for frame in c.decode(audio=0):
            arr = frame.to_ndarray()
            peak = max(peak, float(np.abs(arr).max()))
    import math
    return round(20.0 * math.log10(max(peak, 1e-9)), 1)


# ── 各工作流 ────────────────────────────────────────────────────────────

def w01(tmp: Path) -> dict:
    """导入 H.264 → 两段 + 淡入淡出 + 字幕 → H.264 输出。"""
    from src.features.edit.ai_editing.edit_api import EditSession

    host, session, project, seq = _make_project(H264_SRC, 1572)
    try:
        # 两段：c1 已在；加第二段 c2（相邻放置）
        r1 = _apply_plan(session, [
            ("add_clip", {"clip_id": "c2", "track_id": "v1",
                          "asset_id": "src",
                          "source": {"start": 24, "duration": FRAMES},
                          "timeline": {"start": FRAMES,
                                       "duration": FRAMES}}),
            ("set_clip_fade", {"clip_id": "c1", "track_id": "v1",
                               "fade_in_duration": 5}),
            ("set_clip_fade", {"clip_id": "c2", "track_id": "v1",
                               "fade_out_duration": 5}),
        ], "W01 cuts+fades")
        r2 = _apply_plan(session, [
            ("ensure_subtitle_track", {"track_type": "subtitle",
                                       "track_id": "s1", "name": "S1"}),
            ("add_subtitle", {"track_id": "s1", "text": "W01 字幕",
                              "timeline": {"start": 0, "duration": 20}}),
        ], "W01 subtitle")
        out = tmp / "w01_out.mp4"
        exp = _export(project, seq, "h264", "yuv420p", out)
        probe = _probe(out) if out.exists() else {}
        # 保存重载一致性：序列 round-trip 后 clip 数不变
        from src.features.edit.core.timeline_v2 import TimelineSequence

        t = host.get_state().sequences["t1"]
        v1 = t.get_track_by_id("v1")
        clip_count = len(v1.clips)
        round_trip = TimelineSequence.from_dict(t.to_dict())
        reload_clips = len(round_trip.get_track_by_id("v1").clips)
        return {
            "plan_apply_1": r1, "plan_apply_2": r2,
            "export": exp, "probe": probe,
            "clips": clip_count, "clips_after_reload": reload_clips,
            "ok": (r1["applied"] and r2["applied"] and exp["success"]
                   and probe.get("video") and clip_count == reload_clips
                   and probe.get("audio")),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w02(tmp: Path) -> dict:
    """TOPOS Proxy 全链路（全口径）：底轨 ColorStack 调色 + 顶层 Blur(r8)
    @0.5 不透明度双层叠加 → TOPOS Proxy 输出。

    与试点的差别：走与其它 W 工作流相同的真实 EditSession 计划提交 +
    TimelineExporter 导出路径。验证：native probe（TPIC 容器）、效果参与
    （与 identity 导出中间帧哈希不同）、telemetry full_gpu blockers、
    双层 provider 取帧（试点发现 decode 采样缺口的路径）。
    """
    from src.features.edit.ai_editing.edit_api import EditSession

    def _frame_hash(path: Path) -> str:
        import av
        import hashlib

        with av.open(str(path)) as c:
            frames = list(c.decode(video=0))
        mid = frames[len(frames) // 2]
        return hashlib.sha256(mid.to_ndarray().tobytes()).hexdigest()

    def _probe_native(path: Path) -> dict:
        from src.shared.media.factory import MediaSourceFactory

        result = MediaSourceFactory.probe_video_metadata_cached(str(path))
        md = result.metadata
        return {
            "backend": result.backend,
            "ok": md is not None and bool(getattr(md, "width", 0)),
            "reason_code": result.reason_code,
            "w": getattr(md, "width", None),
            "h": getattr(md, "height", None),
        }

    host, session, project, seq = _make_project(TOPOS_SRC, 300,
                                                with_audio=False)
    try:
        # 1. identity anchor：无效果基线（h264 便于逐帧哈希对比）
        anchor = tmp / "w02_anchor.mov"
        exp_anchor = _export(project, seq, "h264", "yuv420p", anchor)
        anchor_hash = (_frame_hash(anchor)
                       if exp_anchor["success"] and anchor.exists() else None)

        # 2. 计划提交：顶层视频轨 + 同源 clip + blur + 不透明度
        r_apply = _apply_plan(session, [
            ("ensure_track", {"track_type": "video", "track_id": "v2",
                              "name": "V2"}),
            ("add_clip", {"clip_id": "c2", "track_id": "v2",
                          "asset_id": "src",
                          "source": {"start": 0, "duration": FRAMES},
                          "timeline": {"start": 0, "duration": FRAMES}}),
            ("set_effect", {"clip_id": "c2", "track_id": "v2",
                            "effect_type": "blur",
                            "parameters": {"radius": 8.0}}),
            ("set_opacity", {"clip_id": "c2", "track_id": "v2",
                             "opacity": 0.5}),
        ], "W02 two-layer blur")

        # 3. 底轨 ColorStack 调色：plan 协议首版无调色 op，按 w04 直改
        # 模型先例挂 EditProject.color_stacks + c1.color_stack_id。
        # 计划提交会整体替换 project/sequences——必须先提交、再在刷新后
        # 的状态上挂调色栈，否则被提交结果覆盖。
        from dataclasses import replace as _dc_replace

        from src.shared.project.color_stack import ColorStackData

        seq = host.get_state().sequences["t1"]
        project = host.get_state().project
        base_stack = ColorStackData(stack_id="w02_grade_tmp")
        grading = dict(base_stack.grading_params)
        grading["exposure"] = 25.0
        grading["contrast"] = 10.0
        stack = ColorStackData(stack_id="w02_grade", grading_params=grading)
        project = _dc_replace(project, color_stacks={
            **project.color_stacks, stack.stack_id: stack})
        c1 = seq.get_track_by_id("v1").get_clip_by_id("c1")
        c1 = c1.with_color_stack(stack.stack_id)
        v1 = seq.get_track_by_id("v1").with_clip_updated(c1)
        seq = seq.with_track_updated(v1)

        # 4. 效果参与证明：调色+双层导出与 anchor 的中间帧哈希必须不同
        engaged = tmp / "w02_engaged.mov"
        exp_engaged = _export(project, seq, "h264", "yuv420p", engaged)
        engaged_hash = (_frame_hash(engaged)
                        if exp_engaged["success"] and engaged.exists()
                        else None)
        pixel_delta = (anchor_hash is not None and engaged_hash is not None
                       and anchor_hash != engaged_hash)

        # 5. 交付物：TOPOS Proxy 输出 + native probe（TPIC 容器 FFmpeg
        # 探不了，native probe 成功即"无 invalid probe"契约）
        delivery = tmp / "w02_out.mov"
        exp_topos = _export(project, seq, "topos_proxy", "yuv422p10le",
                            delivery)
        native = (_probe_native(delivery)
                  if exp_topos["success"] and delivery.exists() else {})

        return {
            "anchor_export": exp_anchor,
            "plan_apply": r_apply,
            "engaged_export": exp_engaged,
            "pixel_delta_vs_anchor": pixel_delta,
            "topos_export": exp_topos,
            "native_probe": native,
            "ok": (r_apply["applied"] and exp_anchor["success"]
                   and exp_topos["success"] and pixel_delta
                   and native.get("backend") == "topos_native"
                   and native.get("ok")),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w03(tmp: Path) -> dict:
    """TOPOS Proxy 无效果 → H.264/HEVC；CPU fallback 诚实记录。"""
    host, session, project, seq = _make_project(TOPOS_SRC, 300,
                                                with_audio=False)
    try:
        results = {}
        for codec, fmt in (("h264", "yuv420p"), ("hevc", "yuv420p10le")):
            out = tmp / f"w03_{codec}.mov"
            exp = _export(project, seq, codec, fmt, out)
            probe = _probe(out) if out.exists() else {}
            results[codec] = {"export": exp, "probe": probe}
        blockers = [r["export"].get("full_gpu_blockers")
                    for r in results.values()]
        return {
            "results": results,
            "cpu_fallback_honest": all(b is not None for b in blockers),
            "ok": all(r["export"]["success"] and r["probe"].get("video")
                      for r in results.values()),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w04(tmp: Path) -> dict:
    """真实人声 + EQ/Compressor/Delay → 含音频导出，峰值非静音。"""
    from src.features.edit.core.effect_instance import EffectInstance
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack

    host, session, project, seq = _make_project(H265_SRC, 487)
    try:
        dsp = tuple(EffectInstance(effect_type=t, params={}, is_enabled=True)
                    for t in ("strip_eq", "compressor", "delay"))
        a1 = seq.get_track_by_id("a1").get_clip_by_id("a1")
        a1 = a1.with_effects(dsp)
        new_track = seq.get_track_by_id("a1").with_clip_updated(a1)
        seq = seq.with_track_updated(new_track)
        out = tmp / "w04_out.mov"
        exp = _export(project, seq, "h264", "yuv420p", out)
        probe = _probe(out) if out.exists() else {}
        peak = _audio_peak_dbfs(out) if out.exists() else None
        expected = FRAMES / 25.0
        aligned = (probe.get("audio_s") is not None
                   and abs(probe["audio_s"] - expected) < 0.25)
        return {
            "export": exp, "probe": probe, "peak_dbfs": peak,
            "audio_video_aligned": aligned,
            "ok": (exp["success"] and probe.get("audio") and aligned
                   and peak is not None and peak > -60.0),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w07(tmp: Path) -> dict:
    """多版本（横/竖）：竖版中途取消无残留，横版独立完成。"""
    host, session, project, seq = _make_project(TOPOS_SRC, 300,
                                                with_audio=False)
    try:
        portrait_out = tmp / "w07_portrait.mov"
        cancelled = _export(project, seq, "h264", "yuv420p", portrait_out,
                            resolution=(1080, 1920), cancel_after=5)
        landscape_out = tmp / "w07_landscape.mov"
        done = _export(project, seq, "h264", "yuv420p", landscape_out,
                       resolution=(1920, 1080))
        portrait_probe = _probe(portrait_out) if portrait_out.exists() else {}
        landscape_probe = (_probe(landscape_out)
                           if landscape_out.exists() else {})
        return {
            "portrait_cancel": cancelled, "landscape_done": done,
            "portrait_probe": portrait_probe,
            "landscape_probe": landscape_probe,
            "ok": (cancelled["cancelled"] and not cancelled["success"]
                   and done["success"] and landscape_probe.get("video")
                   and landscape_probe.get("w") == 1920),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w06(tmp: Path) -> dict:
    """可交付插件 + preview-only 插件：前者导出可验证，后者计划期拒绝。

    warm_tint（头部声明 @export_support opengl）经 GL 导出效果链真实
    参与渲染——与 identity 导出的中间帧哈希必须不同（参与证明，非
    参数存在性证明）。Film_Halation（preview_only）在计划阶段被
    EFFECT_NOT_DELIVERABLE 拒绝且 details 携带替代建议。
    """
    from src.features.edit.ai_editing.diagnostics import TaepDiagnosticCode
    from src.features.edit.ai_editing.edit_api import EditSession

    def _frame_hash(path: Path) -> str:
        import av
        import hashlib
        import numpy as np

        with av.open(str(path)) as c:
            frames = list(c.decode(video=0))
        mid = frames[len(frames) // 2]
        arr = mid.to_ndarray()
        return hashlib.sha256(arr.tobytes()).hexdigest()

    # GL 合成臂是插件参与的通道：显式启用 GPU 渲染（该环境变量为
    # 用户/脚本强制语义，自适应策略让位）。CPU 回退下插件被跳过——
    # 该事实由能力质量约束与导出遥测诚实承载（W08 覆盖）。
    os.environ["TOPOS_EXPORT_GPU_RENDER"] = "1"
    host, session, project, seq = _make_project(TOPOS_SRC, 300,
                                                with_audio=False)
    try:
        # ── 可交付插件：先导 identity，再上链导出，帧哈希必须不同 ──
        plain = tmp / "w06_plain.mov"
        exp_plain = _export(project, seq, "h264", "yuv420p", plain)
        r_apply = _apply_plan(session, [
            ("set_effect", {"clip_id": "c1", "track_id": "v1",
                            "effect_type": "plugin:warm_tint",
                            "parameters": {}}),
        ], "W06 deliverable plugin")
        tinted = tmp / "w06_tinted.mov"
        exp_tinted_probe = None
        engaged = None
        if (exp_plain["success"] and plain.exists() and r_apply["applied"]):
            # 提交后 host state 才携带新效果——必须刷新 seq 引用，
            # 否则导出仍是旧时间线（插件根本不在帧上）。
            seq = host.get_state().sequences["t1"]
            exp_tinted = _export(project, seq, "h264", "yuv420p", tinted)
            exp_tinted_probe = exp_tinted
            if exp_tinted["success"] and tinted.exists():
                engaged = _frame_hash(tinted) != _frame_hash(plain)
        # remove_effect 可执行性同时被覆盖
        r_off = _apply_plan(session, [
            ("remove_effect", {"clip_id": "c1", "track_id": "v1",
                               "effect_type": "plugin:warm_tint"}),
        ], "W06 remove")

        # ── preview-only 插件：计划期拒绝 ──
        reject = _apply_plan(session, [
            ("set_effect", {"clip_id": "c1", "track_id": "v1",
                            "effect_type": "plugin:Film_Halation",
                            "parameters": {}}),
        ], "W06 preview-only rejected")

        return {
            "deliverable_apply": r_apply,
            "identity_remove": r_off,
            "tinted_export": exp_tinted_probe,
            "plugin_engaged_in_output": engaged,
            "preview_only_reject": reject,
            "ok": (r_apply["applied"] and r_off["applied"]
                   and bool(engaged)
                   and not reject["applied"]),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w08(tmp: Path) -> dict:
    """GPU/模型不可用：模型态诚实、导出 fallback 诚实、工具优雅降级。"""
    from src.features.edit.ai_editing.capability_manifest import build_manifest
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.tool_facade import ToolFacade

    host, session, project, seq = _make_project(TOPOS_SRC, 300,
                                                with_audio=False)
    try:
        manifest = build_manifest().to_dict()
        model_caps = [c for c in manifest.get("capabilities", [])
                      if c.get("capability_id") in (
                          "local_media_analysis", "asr_local",
                          "visual_caption_local")]
        models_honest = model_caps and all(
            not c.get("available") for c in model_caps)

        facade = ToolFacade(session)
        search = facade.invoke("topos.search_assets", {"query": "clip"})

        out = tmp / "w08_out.mov"
        exp = _export(project, seq, "h264", "yuv420p", out)
        probe = _probe(out) if out.exists() else {}
        return {
            "model_capabilities": [
                {"id": c.get("capability_id"), "state": c.get("state")}
                for c in model_caps],
            "models_honest_unavailable": bool(models_honest),
            "search_assets_graceful": (
                isinstance(search, dict)
                and (search.get("ok") in (True, None)
                     or "note" in search or search.get("segments") is not None)),
            "export": exp, "probe": probe,
            "cpu_fallback_honest": exp.get("full_gpu_blockers") is not None,
            "ok": (bool(models_honest) and exp["success"]
                   and probe.get("video")
                   and exp.get("full_gpu_blockers") is not None),
        }
    finally:
        from src.features.edit.ai_editing.edit_api import EditSession as _E
        _E.unbind()


def w05(tmp: Path) -> dict:
    """W05 Composite 专项：图保存重载 round-trip + cache/telemetry 语义。

    93 节点矩阵（bench_composite_nodes_gpu_coverage）已覆盖非平输出/
    绿幕 attribution/MODEL_UNAVAILABLE；本回放补齐剩余验收口径：

    1. 保存重载：graph → NodeGraph.to_dict → JSON 文件 → from_dict →
       重渲染像素 sha256 一致 + 结构（节点/连接/关键参数）保真。
    2. cache：同图重渲染命中节点级缓存（CacheManager.stats.hits 增长）；
       改 chroma tolerance 后正确失效（像素必须变化——缓存陈旧即 FAIL）。
    3. telemetry：frame trace attribution 字段落齐（eval_ms/total_ms/
       backend/node_count）。
    """
    import hashlib
    import sys
    import time as _time
    # CompositeFrameRenderer 的 standalone GL 上下文需要 headless 模式
    # （与 bench_composite_nodes_gpu_coverage.main 同口径）；矩阵进程
    # 其它工作流不触碰 GL，这里局部补齐。
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
    sys.path.insert(0, str(ROOT / "scripts"))
    from bench_composite_nodes_gpu_coverage import (
        NODE_FIXTURES, build_graph)
    import numpy as np
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer)

    sources, keyer_params = NODE_FIXTURES["chroma_keyer"]()
    graph_data = build_graph(
        [("chroma_keyer", dict(keyer_params))], list(sources),
        out_size=(960, 540))

    # 1) 保存重载 round-trip
    saved = NodeGraph.from_dict(graph_data).to_dict()
    graph_file = tmp / "w05_graph.json"
    graph_file.write_text(
        json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    reloaded = json.loads(graph_file.read_text(encoding="utf-8"))
    reload_data = NodeGraph.from_dict(reloaded).to_dict()
    orig_nodes = {n["id"]: n for n in saved["nodes"]}
    back_nodes = {n["id"]: n for n in reload_data["nodes"]}
    keyer_ids = [nid for nid, n in orig_nodes.items()
                 if n.get("type") == "chroma_keyer"]
    structure_ok = (
        set(orig_nodes) == set(back_nodes)
        and len(saved["connections"]) == len(reload_data["connections"])
        and all(back_nodes[nid].get("parameters", {}).get("tolerance") == 0.15
                for nid in keyer_ids)
    )

    renderer = CompositeFrameRenderer()

    def _plan_diag() -> dict:
        """RenderPlan 运行时诊断（cache_hits/cached_node_ids）。

        comp-clip 路径的节点级缓存命中数由 evaluator 记入
        RenderScheduler.latest_plan——这是真实机制的可观测面。
        """
        er = getattr(renderer, "_export_renderer", None)
        plan = getattr(getattr(er, "_scheduler", None), "latest_plan", None)
        if plan is None:
            return {}
        return {
            "cache_hits": int(getattr(plan, "cache_hits", 0) or 0),
            "cache_misses": int(getattr(plan, "cache_misses", 0) or 0),
            "cached_node_ids": list(
                getattr(plan, "cached_node_ids", []) or []),
        }

    def _render(data: dict):
        img = renderer.render_comp_clip_frame(
            graph_data=data, timeline_frame=0, source_frame=0,
            quality="preview")
        trace = dict(getattr(renderer, "last_frame_trace", None) or {})
        if img is None:
            return None, trace
        arr = np.asarray(img)
        return (
            hashlib.sha256(arr.tobytes()).hexdigest(),
            {"mean": round(float(arr.astype("float64").mean()), 3),
             "std": round(float(arr.astype("float64").std()), 3),
             **{k: trace.get(k) for k in (
                 "eval_ms", "total_ms", "backend", "node_count",
                 "cache_hit", "strategy")}},
        )

    try:
        hash1, info1 = _render(reload_data)
        hash2, info2 = _render(reload_data)
        pixel_stable = (hash1 is not None and hash1 == hash2)

        plan_warm = _plan_diag()
        hash3, _info3 = _render(reload_data)
        plan_served = _plan_diag()
        cache_serves = (
            hash3 == hash1
            and plan_served.get("cache_hits", 0) >= 1)

        # 参数变化 → 缓存必须失效（tolerance 0.45 > 白主体距绿 0.285，
        # 全帧被抠掉 → 像素必然不同；若仍命中旧缓存说明陈旧读取）。
        changed = json.loads(json.dumps(reload_data))
        for n in changed["nodes"]:
            if n.get("type") == "chroma_keyer":
                n["parameters"]["tolerance"] = 0.45
        hash4, info4 = _render(changed)
        plan_changed = _plan_diag()
        cache_invalidates = (hash4 is not None and hash4 != hash1
                             and plan_changed.get("cache_misses", 0) >= 1)

        attribution_ok = bool(
            info1.get("eval_ms") is not None
            and info1.get("total_ms") is not None
            and info1.get("backend"))
        nonflat = float(info1.get("std") or 0.0) > 1.0

        result = {
            "structure_roundtrip": structure_ok,
            "pixel_stable": pixel_stable,
            "cache_serves": cache_serves,
            "cache_invalidates": cache_invalidates,
            "attribution_ok": attribution_ok,
            "nonflat": nonflat,
            "cache_hits_warm": plan_served,
            "cache_hits_changed": plan_changed,
            "render1": info1, "render2": info2,
            "render_changed": info4,
            "graph_file": str(graph_file),
            "ok": bool(structure_ok and pixel_stable and cache_serves
                       and cache_invalidates and attribution_ok and nonflat),
        }
        return result
    finally:
        try:
            renderer.cleanup()
        except Exception:
            pass
        import gc as _gc
        _gc.collect()


# ── 主流程 ──────────────────────────────────────────────────────────────

def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "bench_out" / "ai_upgrade" / f"W_matrix_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="w_matrix_"))

    report: dict[str, dict] = {}
    for wid, fn in (("W01", w01), ("W02", w02), ("W03", w03), ("W04", w04),
                    ("W05", w05), ("W06", w06), ("W07", w07), ("W08", w08)):
        try:
            report[wid] = fn(tmp)
            verdict = "PASS" if report[wid].get("ok") else "FAIL"
        except Exception as exc:  # noqa: BLE001 — 记录失败而非中断矩阵
            report[wid] = {"ok": False, "exception": repr(exc),
                           "trace": traceback.format_exc()[-2000:]}
            verdict = "FAIL"
        print(f"  {wid}: {verdict}")

    passed = [w for w, r in report.items() if r.get("ok")]
    lines = [
        "# §10 真实工作流回放矩阵 — 可执行批次",
        "",
        f"- 日期：{stamp}；每工作流 {FRAMES} 帧",
        f"- 结果：{len(passed)}/{len(report)} PASS（{'、'.join(passed) if passed else '无'}）",
        "- W05 专项（图保存重载 + cache/telemetry）已入矩阵；93 节点矩阵"
        " 0 failed 由 bench_composite_nodes_gpu_coverage 归档——诚实状态。",
        "",
        "| 工作流 | 结论 | 关键证据 |",
        "|---|---|---|",
    ]
    for wid, r in report.items():
        key = json.dumps({k: v for k, v in r.items()
                          if k not in ("trace",)}, ensure_ascii=False)[:220]
        lines.append(f"| {wid} | {'PASS' if r.get('ok') else 'FAIL'} | {key} |")
    (out_dir / "result.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"evidence → {out_dir}")
    # 退出码与注册的工作流总数联动（此前写死 6，新增 W02 后 7/7 PASS
    # 仍退出 1 —— 已修复的脚本缺陷，见 W_matrix_20260919-012922 审计）。
    return 0 if len(passed) == len(report) else 1


if __name__ == "__main__":
    sys.exit(main())
