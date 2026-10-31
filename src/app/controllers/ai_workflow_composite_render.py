"""AI Workflow §9.5「合成渲染后导入」的宿主侧执行器（plan §9.5 完整实现）。

职责划分（线程红线）：

- :func:`make_exposed_input_resolver` —— **主线程**。在 Run 触发时把当前
  composite 图解析为纯 dict 快照（显式 ``composite_graph_id`` 优先，否则取
  Composite 页活动图），填进 ``invocation.inputs["composite_graph"]``（替换
  compiler 的 exposed_inputs 占位）。读取 ``composite_page`` / ``EditProject``
  涉及 Qt，只能在这个阶段做。
- :func:`make_composite_render_provider` —— **worker 线程**（editorial 路径）。
  只消费上一步产出的 dict 快照：headless ``CompositeRenderOutput`` /
  ``CompositeFrameRenderer``（自建 GL context，专为 worker 线程设计）渲染到
  项目缓存目录，产物注册进 ``GenerationBin``（生成素材箱候选）。

红线（与 editorial / TTS 流程同构）：

- 渲染候选先入生成素材箱；**不导入媒体池、不写时间线**——导入是用户在生成
  素材箱里的显式动作（``import_ai_generated_asset`` 链路）。
- 无可用 composite 图 / 渲染失败 / 被取消 → 返回 ``None``，editorial 层诚实
  降级（``COMPOSITE_RENDER_NO_OUTPUT``），不伪造产物。
- provenance 记录图内容 hash（不记绝对路径之外的信息；prompt hash 位填图 hash）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_LOG = logging.getLogger(__name__)

#: composite 渲染候选的缓存目录（与 generated_media 缓存同源）。
_RENDER_CACHE_SUBDIR = "composite_renders"

#: AUD-2214: composite_renders 缓存上限（总大小 / 文件数），超出后按 mtime
#: 从最旧开始淘汰（0 表示对应维度不限制）。每次渲染 uuid 新名落盘且从不
#: 清理，长会话/4K 下缓存可增长到 GB 级。
_RENDER_CACHE_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_RENDER_CACHE_MAX_FILES = 64
#: 淘汰保护窗口：1 小时内新写的产物可能是生成素材箱里待预览/导入的候选
#: （``preview_handle`` 指向本目录），不参与淘汰。
_RENDER_CACHE_MIN_AGE_S = 3600.0


def _prune_render_cache(out_dir: Path) -> None:
    """AUD-2214: composite_renders 缓存按大小/数量 LRU 淘汰（worker 线程执行）。

    只触碰 ``composite-*`` 前缀产物，按 mtime 从旧到新删除，直到总大小与
    数量都回落到上限内；保护窗口内的文件不删。任何失败只记 debug，绝不
    影响渲染主流程。
    """
    try:
        entries: list[tuple[float, int, Path]] = []
        for path in out_dir.glob("composite-*"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                entries.append((stat.st_mtime, stat.st_size, path))
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    count = len(entries)
    if total <= _RENDER_CACHE_MAX_TOTAL_BYTES and count <= _RENDER_CACHE_MAX_FILES:
        return
    now = time.time()
    prunable = sorted(
        entry for entry in entries if now - entry[0] > _RENDER_CACHE_MIN_AGE_S
    )
    for _mtime, size, path in prunable:
        if total <= _RENDER_CACHE_MAX_TOTAL_BYTES and count <= _RENDER_CACHE_MAX_FILES:
            break
        try:
            path.unlink()
        except OSError:
            _LOG.debug("composite render cache prune failed for %s",
                       path, exc_info=True)
            continue
        total -= size
        count -= 1
    if total > _RENDER_CACHE_MAX_TOTAL_BYTES or count > _RENDER_CACHE_MAX_FILES:
        _LOG.debug(
            "composite render cache still over budget after prune "
            "(%d bytes / %d files; young artifacts protected)",
            total, count,
        )


@dataclass
class _CompositeRenderContext:
    """渲染上下文引用快照（AUD-2211，主线程 resolver 阶段捕获）。

    只在主线程填充；worker 线程的 render provider 只读本结构携带的引用，
    不再活读 main_window 的可变状态属性。
    """

    edit_project: Any = None
    timeline_sequences: Any = None
    config_manager: Any = None


def _make_bin_insert_marshaler(main_window: Any) -> Any:
    """构造 GenerationBin 写入投递器（AUD-2212，必须在 GUI 线程调用）。

    ``GenerationBin`` 是无锁 dict；worker 线程直接 ``add_candidates`` 与 GUI
    线程对 ``all_assets/by_state`` 的遍历刷新并发时，CPython dict 迭代中的
    resize 会抛 "dictionary changed size during iteration"。GenerationBin
    （generation_bin.py）不在本模块的修改边界内、无法自行加锁，因此按审查
    建议的替代方案：worker 经本 QObject 的信号 emit 写入，Qt 自动以 queued
    connection 切回 marshaler 所属（GUI）线程执行，与读侧天然串行。时序
    安全：入箱信号在 provider 返回前发出，早于 run 终态事件与任何用户交互，
    GUI 消费候选时写入必然已生效。非 Qt 宿主（headless 测试）返回 None，
    调用方回退同步写入（此时不存在并发读侧）。
    """
    try:
        from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal
    except ImportError:
        return None
    if QCoreApplication.instance() is None or not isinstance(main_window, QObject):
        return None

    class _BinInsertMarshaler(QObject):
        insert_requested = pyqtSignal(object)  # payload: (gen_bin, asset)

        def __init__(self, parent: Any) -> None:
            super().__init__(parent)
            # AutoConnection：worker 线程 emit → marshaler 所属（GUI）线程槽。
            self.insert_requested.connect(self._insert)

        def _insert(self, payload: Any) -> None:
            gen_bin, asset = payload
            try:
                gen_bin.add_candidates((asset,))
            except Exception:  # noqa: BLE001 — 入箱失败只记日志，不崩 GUI
                _LOG.debug("queued generation-bin insertion failed", exc_info=True)

    try:
        return _BinInsertMarshaler(main_window)
    except (RuntimeError, TypeError):
        _LOG.debug("generation-bin marshaler creation failed", exc_info=True)
        return None


# ---------------------------------------------------------------------- #
# 主线程：composite 图快照解析（exposed input 填充）
# ---------------------------------------------------------------------- #


def resolve_composite_graph_snapshot(
    main_window: Any, explicit_composite_id: str = "",
) -> dict[str, Any] | None:
    """解析要渲染的 composite 图（主线程专用；返回纯 dict 快照）。

    优先级：显式 ``composite_graph_id``（``EditProject.compositions``）→
    Composite 页活动图（``composite_page.get_composite_data()``）。
    """
    project = getattr(main_window, "_current_edit_project", None)
    if explicit_composite_id:
        if project is None:
            return None
        compositions = getattr(project, "compositions", None) or {}
        data = compositions.get(explicit_composite_id)
        if data is None:
            _LOG.warning(
                "composite graph %r not found in project compositions",
                explicit_composite_id,
            )
            return None
        if hasattr(data, "to_dict"):
            return dict(data.to_dict())
        if isinstance(data, dict):
            return dict(data)
        return None

    page = getattr(main_window, "composite_page", None)
    getter = getattr(page, "get_composite_data", None)
    if not callable(getter):
        return None
    try:
        graph_data = getter()
    except Exception:  # noqa: BLE001 — 图读取失败 → 诚实降级
        _LOG.warning("failed to read active composite graph", exc_info=True)
        return None
    if isinstance(graph_data, dict) and graph_data.get("nodes"):
        return dict(graph_data)
    return None


def make_exposed_input_resolver(main_window: Any) -> Callable[[Any, Any], None]:
    """构造 exposed input 填充器（主线程，Run 前）。

    对声明了 ``composite_graph`` exposed input 的图，把解析出的 dict 快照 +
    内容 hash 写进 ``invocation.inputs["composite_graph"]``，供 worker 线程的
    render provider 消费。解析失败时移除占位，让 provider 走降级。
    """

    def resolver(invocation: Any, graph: Any) -> None:
        inputs = getattr(invocation, "inputs", None)
        if not isinstance(inputs, dict):
            return
        exposed = tuple(getattr(graph, "exposed_inputs", ()) or ())
        has_composite = any(name == "composite_graph" for name, _n, _p in exposed)
        if not has_composite:
            return
        explicit_id = str(inputs.get("composite_graph_id", "") or "").strip()
        snapshot = resolve_composite_graph_snapshot(main_window, explicit_id)
        if snapshot is None:
            inputs.pop("composite_graph", None)
            return
        inputs["composite_graph"] = {
            "_kind": "composite_graph_snapshot",
            "graph": snapshot,
            "content_hash": composite_graph_hash(snapshot),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            # AUD-2211：渲染所需的工程/时间线/配置引用在**主线程**此阶段捕获。
            # worker 线程只消费本快照携带的引用，不再活读 main_window 的
            # _current_edit_project / _current_timeline_sequences——渲染期间主
            # 线程切换/关闭工程不再把 worker 的输入换成新引用或 None
            # （模块 docstring 声明的"worker 只消费快照"红线）。引用随快照
            # 一起存活（Python 引用计数保证旧工程对象不被中途回收）。
            "render_context": _CompositeRenderContext(
                edit_project=getattr(main_window, "_current_edit_project", None),
                timeline_sequences=getattr(
                    main_window, "_current_timeline_sequences", None,
                ),
                config_manager=getattr(main_window, "config_manager", None),
            ),
        }

    return resolver


def composite_graph_hash(graph_data: dict[str, Any]) -> str:
    """composite 图 dict 的 canonical hash（GRAPH_STALE 校验 / provenance 用）。"""
    payload = json.dumps(graph_data, sort_keys=True, ensure_ascii=False,
                         default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------- #
# worker 线程：headless 渲染 + 生成素材箱注册
# ---------------------------------------------------------------------- #


def make_composite_render_provider(main_window: Any) -> Callable[..., Any]:
    """构造 plan §9.5 的渲染 provider（worker 线程，editorial 路径注入）。

    签名 ``(invocation, cancel_requested) -> SkillArtifact | None``。返回的
    artifact 只携带 GenerationBin 候选引用（``payload_ref``）；
    ``media_asset_id`` 留空——未导入媒体池前不可被任何 plan 放置。
    """
    # AUD-2212：GUI 线程（wiring 阶段）构造入箱投递器；provider 在 worker
    # 线程经信号 emit 写入，Qt 自动 queued 切回 GUI 线程执行。
    bin_marshaler = _make_bin_insert_marshaler(main_window)

    def provider(invocation: Any, cancel_requested: Any = None) -> Any:
        # AUD-2213：provider 整体异常防护。契约是"渲染失败/被取消 → 返回
        # None，editorial 层诚实降级（COMPOSITE_RENDER_NO_OUTPUT）"——
        # inputs["composite_graph"] 来自工程/AI 可控的图数据，任何解析/渲染
        # 异常都必须被收敛为 None，而不是逃逸到 harness 调用点。
        try:
            return _run_composite_render_provider(
                main_window, invocation, cancel_requested, bin_marshaler,
            )
        except Exception:  # noqa: BLE001 — 诚实降级：log + None，不伪造产物
            _LOG.warning("composite render provider failed", exc_info=True)
            return None

    return provider


def _run_composite_render_provider(
    main_window: Any,
    invocation: Any,
    cancel_requested: Any = None,
    bin_marshaler: Any = None,
) -> Any:
    """provider 主体（由 :func:`make_composite_render_provider` 的包装调用）。"""
    from src.features.ai_harness.editorial_skills import SkillArtifact

    inputs = getattr(invocation, "inputs", {}) or {}
    composite_input = inputs.get("composite_graph")
    graph_data = None
    render_context: _CompositeRenderContext | None = None
    if isinstance(composite_input, dict):
        raw = composite_input.get("graph")
        if isinstance(raw, dict):
            graph_data = raw
        context = composite_input.get("render_context")
        if isinstance(context, _CompositeRenderContext):
            render_context = context
    if graph_data is None:
        _LOG.info("composite render: no graph snapshot on invocation, degrade")
        return None
    if render_context is None:
        # AUD-2211：快照缺渲染上下文 = resolver 未运行（旧 invocation 形态）。
        # 不回退到活读 main_window——那正是本模块 docstring 禁止的红线；
        # 按无图降级处理，让调用方重新走 resolver 路径。
        _LOG.warning(
            "composite render: graph snapshot has no render context, degrade"
        )
        return None

    is_cancelled: Callable[[], bool] = (
        (lambda: bool(cancel_requested())) if callable(cancel_requested)
        else (lambda: False)
    )
    if is_cancelled():
        return None

    # AUD-2211：只消费 resolver 在主线程捕获的引用快照。
    edit_project = render_context.edit_project
    timeline_sequences = render_context.timeline_sequences
    config_manager = render_context.config_manager
    output_path, modality, duration_seconds = _render_headless(
        graph_data, edit_project, timeline_sequences, config_manager,
        is_cancelled,
    )
    if output_path is None:
        return None

    asset_id = _register_generation_candidate(
        main_window, output_path, modality, graph_data, invocation,
        duration_seconds, bin_marshaler=bin_marshaler,
    )
    if asset_id is None:
        return None
    label = "视频" if modality == "video" else "图片"
    return SkillArtifact(
        artifact_id=f"generated_media:{asset_id}",
        artifact_type="media_generation_ref",
        summary=f"合成渲染候选（{label}）已入生成素材箱，待预览/导入",
        trust="unverified",
        payload_ref=asset_id,
        media_asset_id="",
    )


def _render_headless(
    graph_data: dict[str, Any],
    edit_project: Any,
    timeline_sequences: Any,
    config_manager: Any,
    is_cancelled: Callable[[], bool],
) -> tuple[str | None, str, float | None]:
    """headless 渲染到缓存目录。返回 (output_path, modality, duration_seconds)。

    失败/取消返回 ``(None, modality, None)``——调用方诚实降级。
    """
    from src.features.composite.render.composite_render_output import (
        CompositeMode,
        CompositeRenderOutput,
        detect_composite_mode,
        resolve_video_range,
    )

    mode = detect_composite_mode(graph_data)
    comp_clip = _resolve_comp_clip(edit_project, graph_data)
    width, height = _resolve_output_resolution(graph_data)
    out_dir = _render_cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    # AUD-2214: 渲染新产物前先淘汰过期缓存（worker 线程，不占 UI 线程）。
    _prune_render_cache(out_dir)
    render_output = CompositeRenderOutput(config_manager)

    if mode == CompositeMode.VIDEO:
        start, end, fps = resolve_video_range(graph_data, comp_clip)
        if end < start or fps <= 0:
            _LOG.warning("composite render: invalid video range %s..%s@%s",
                         start, end, fps)
            return None, "video", None
        output_path = str(out_dir / f"composite-{uuid.uuid4().hex[:10]}.mp4")
        result = render_output.render_video(
            graph_data, output_path, width, height, fps, start, end,
            edit_project=edit_project,
            timeline_sequences=timeline_sequences,
            comp_clip=comp_clip,
            is_cancelled=is_cancelled,
        )
        if is_cancelled():
            return None, "video", None
        if not getattr(result, "success", False):
            _LOG.warning("composite render failed: %s",
                         getattr(result, "error", "unknown"))
            return None, "video", None
        return output_path, "video", (end - start + 1) / float(fps)

    frame = _render_single_frame(
        render_output, graph_data, edit_project, timeline_sequences,
    )
    if frame is None or is_cancelled():
        return None, "image", None
    output_path = str(out_dir / f"composite-{uuid.uuid4().hex[:10]}.png")
    if not _write_png(frame, output_path, config_manager):
        return None, "image", None
    return output_path, "image", None


def _render_single_frame(
    render_output: Any,
    graph_data: dict[str, Any],
    edit_project: Any,
    timeline_sequences: Any,
) -> Any:
    """IMAGE 模式：CompositeFrameRenderer 渲染单帧（自建 GL context）。"""
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer,
    )

    renderer = CompositeFrameRenderer()
    try:
        return renderer.render_comp_clip_frame(
            graph_data,
            edit_project=edit_project,
            timeline_sequences=timeline_sequences,
            quality="export",
        )
    finally:
        # AUD-2210：cleanup() 释放 async PBO、纹理池与内部 export renderer 的
        # GL 资源（worker 线程 GL context + 资源池不再依赖 GC 释放）。finally
        # 保证渲染异常路径同样释放；cleanup 自身失败只记日志，不掩盖渲染结果。
        try:
            renderer.cleanup()
        except Exception:  # noqa: BLE001 — 清理失败不得把成功渲染变成异常
            _LOG.debug("composite frame renderer cleanup failed", exc_info=True)


def _write_png(
    frame_bgr_float: Any, output_path: str, config_manager: Any = None,
) -> bool:
    """scene-linear BGR float32 [0,1] → sRGB 编码 → 8bit PNG。

    AUD-2209：readback（quality="export"）是 scene-linear，直接乘 255 量化
    会让产物系统性偏暗。复用主导出路径同一编码函数
    ``composite_render_output._encode_linear_to_srgb``（ColorTransformProvider，
    尊重 OCIO 配置；provider 不可用回退内置 sRGB OETF），与 FileOutput 的
    png/jpg 导出及视频路径同源。超出 [0,1] 的高光经 OETF 后 >1，量化时
    clip 到显示白——SDR 8bit PNG 无 HDR 余量，与主导出行为一致
    （composite_render_output._convert_for_image_ext 同款 clip）。
    """
    import cv2
    import numpy as np
    from src.features.composite.render.composite_render_output import (
        _encode_linear_to_srgb,
    )

    frame = np.asarray(frame_bgr_float)
    if frame.dtype != np.float32:
        frame = frame.astype(np.float32)
    encoded = _encode_linear_to_srgb(frame, config_manager=config_manager)
    array = np.clip(encoded, 0.0, 1.0) * 255.0 + 0.5
    return bool(cv2.imwrite(output_path, array.astype(np.uint8)))


def _register_generation_candidate(
    main_window: Any,
    output_path: str,
    modality: str,
    graph_data: dict[str, Any],
    invocation: Any,
    duration_seconds: float | None,
    *,
    bin_marshaler: Any = None,
) -> str | None:
    """渲染产物注册进 GenerationBin；返回 asset_id（bin 不可用时返回 None）。"""
    from src.features.edit.ai_editing.generation_models import (
        GeneratedAsset,
        GenerationModality,
        GenerationProvenance,
    )

    gen_bin = _generation_bin(main_window)
    if gen_bin is None:
        _LOG.warning("composite render: GenerationBin unavailable; "
                     "candidate not registered (output kept at %s)", output_path)
        return None
    asset_id = f"gen-comp-{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    asset = GeneratedAsset(
        asset_id=asset_id,
        request_id=str(getattr(invocation, "invocation_id", "") or asset_id),
        modality=GenerationModality(modality),
        provenance=GenerationProvenance(
            provider_id="topos-composite-render",
            model_id="composite-graph",
            model_version="1",
            requested_at=now_iso,
            input_prompt_hash=composite_graph_hash(graph_data),
            reference_asset_ids=tuple(
                getattr(getattr(invocation, "scope", None), "selected_asset_ids", ())
                or (),
            ),
            parent_skill_id=str(getattr(invocation, "skill_id", "") or ""),
        ),
        preview_handle=str(output_path),
        duration_seconds=duration_seconds,
        created_at=now_iso,
    )
    if bin_marshaler is not None:
        # AUD-2212：worker 线程不直接写无锁 bin——信号 emit，Qt queued 切回
        # GUI 线程执行（时序见 _make_bin_insert_marshaler 的说明）。
        bin_marshaler.insert_requested.emit((gen_bin, asset))
    else:
        # 非 Qt 宿主（headless）：同步写入（无并发读侧，安全）。
        gen_bin.add_candidates((asset,))
    return asset_id


def _generation_bin(main_window: Any) -> Any:
    bridge = getattr(main_window, "_ai_host_bridge", None)
    service = getattr(bridge, "_generation_service", None)
    return getattr(service, "generation_bin", None)


def _render_cache_dir() -> Path:
    from src.features.edit.ai_editing.model_pack_manager import default_cache_dir

    return default_cache_dir() / _RENDER_CACHE_SUBDIR


def _resolve_comp_clip(edit_project: Any, graph_data: dict[str, Any]) -> Any:
    comp_clip_id = graph_data.get("comp_clip_id")
    if not comp_clip_id or edit_project is None:
        return None
    comp_clips = getattr(edit_project, "comp_clips", None) or {}
    clip = comp_clips.get(comp_clip_id)
    if isinstance(clip, dict):
        return clip
    return clip if clip is not None else None


def _resolve_output_resolution(graph_data: dict[str, Any]) -> tuple[int, int]:
    """Output 节点的自定义分辨率；否则 1920x1080（与 composite_controller 一致）。"""
    for node in graph_data.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "output":
            continue
        params = node.get("parameters") or {}
        if params.get("format_mode") in ("custom", "project"):
            # AUD-2213：width/height 是工程/AI 可控输入，非法值（非数字字符
            # 串等）不得让 ValueError 逃出 provider——回退默认分辨率。
            try:
                w = int(params.get("width", 1920) or 1920)
                h = int(params.get("height", 1080) or 1080)
            except (TypeError, ValueError):
                _LOG.warning(
                    "composite render: invalid output resolution %r x %r, "
                    "falling back to 1920x1080",
                    params.get("width"), params.get("height"),
                )
                return 1920, 1080
            return w, h
    return 1920, 1080


__all__ = [
    "composite_graph_hash",
    "make_composite_render_provider",
    "make_exposed_input_resolver",
    "resolve_composite_graph_snapshot",
]
