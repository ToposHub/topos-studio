"""F9.3 · 多版本交付编译器——VersionDerivationPlan → N 个 ExportJob。

现状（调研）：`VersionDerivationPlan`/`LinkedVersion` 仅有数据模型
（editorial_advanced.py:305-321），无生成器/执行器接入 DELIVER 队列。
本模块补齐编译器（**复用既有 ExportQueue 串行执行**——不新建队列）：

- 每个 LinkedVersion → 一个 ``ExportJob``（config = 基线导出配置 +
  版本差异覆写：分辨率/画幅/字幕策略），``linked_version_id`` 字段
  做分组展示与恢复关联；
- **命名冲突先检查**：同 output_path 已在队列 → 拒绝（不覆盖）；
- **素材依赖先检查**：基线配置引用的时间线存在且素材非离线（离线
  → BLOCKING 拒绝清单，不产半套 job）；
- **冻结工程版本**：每 job config 记录 ``source_project_revision`` +
  ``source_snapshot_hash``（F8.1 快照哈希）——恢复/重试验证源版本，
  **不用旧版本缓存续渲新版本**；
- PCM 级音频解码校验（probe 目前显式跳过 AudioFrame——调研发现的
  缺口）：`count_audio_frames` 统计解码音频帧/采样数入 manifest。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.features.edit.ai_editing.editorial_advanced import (
    VersionDerivationPlan,
)

#: 画幅 → 输出分辨率（可扩展）
ASPECT_RESOLUTIONS: Dict[str, Tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "2.39:1": (1920, 804),
}


@dataclass(frozen=True)
class CompiledVersionJob:
    """编译产物：一个待入队的 ExportJob 配置（含版本溯源）。"""

    linked_version_id: str
    name: str
    config: Dict[str, Any]
    output_path: str


@dataclass(frozen=True)
class VersionCompileResult:
    """编译结果：jobs + 拒绝清单（不产半套）。"""

    jobs: Tuple[CompiledVersionJob, ...]
    refusals: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusals


#: 字幕策略 → job 侧 SubtitleMode（N06：旧编译器写 ``burn_subtitles``
#: bool，job 的 ``from_flat_config`` 读 ``subtitle_mode``（默认 off）——
#: 两键从未对齐，编译出的多版本字幕策略从不生效）。
SUBTITLE_STRATEGY_MODES: Dict[str, str] = {
    "burn_in": "burn",
    "burn": "burn",
    "soft": "sidecar",
    "sidecar": "sidecar",
    "none": "off",
    "off": "off",
}


def compile_version_jobs(
    plan: VersionDerivationPlan,
    base_config: Dict[str, Any],
    *,
    source_project_revision: int = 0,
    source_snapshot_hash: Optional[str] = None,
    timeline_exists: bool = True,
    all_assets_online: bool = True,
    existing_output_paths: Optional[List[str]] = None,
    resolve_track_id=None,
) -> VersionCompileResult:
    """把派生计划编译为 N 个 ExportJob 配置。

    Args:
        base_config: 基线 TimelineExportConfig（dict 形态——与
            ExportJob.config 同型）。
        timeline_exists / all_assets_online: 依赖预检（False → 整体
            拒绝并给原因，不产半套 job）。
        existing_output_paths: 队列中已有的输出路径（命名冲突检查）。
        resolve_track_id: ``language -> subtitle_track_id | None``（N06：
            语言→字幕轨身份解析，由批次入口从时间线字幕轨元数据注入；
            解析失败且策略非 off → 拒绝该版本，不盲选默认轨）。

    Returns:
        VersionCompileResult：ok=False 时 refusals 给逐项原因。
    """
    refusals: List[str] = []
    if not plan.versions:
        return VersionCompileResult((), ("派生计划无版本条目",))
    if not timeline_exists:
        return VersionCompileResult((), ("基线时间线不存在——拒绝编译",))
    if not all_assets_online:
        # M10（S03）：离线是整体拒绝的硬门——此前只追加理由仍继续编译，
        # 可能返回部分 jobs（半套交付）。与 timeline_exists 同级 fail-fast。
        return VersionCompileResult(
            (), ("存在离线素材——先重连再编译多版本",))
    existing = set(existing_output_paths or [])

    jobs: List[CompiledVersionJob] = []
    for version in plan.versions:
        resolution = ASPECT_RESOLUTIONS.get(version.canvas_aspect)
        if resolution is None:
            refusals.append(
                f"版本 {version.version_id}：未知画幅 {version.canvas_aspect}"
                "（支持：16:9/9:16/1:1/4:5/2.39:1）")
            continue
        width, height = resolution
        config = dict(base_config)
        # M10（S03）：画幅写进 resolution——VideoExportJob.from_flat_config
        # 读的是 resolution 元组键（此前只写 width/height，真实 job 仍取
        # 基线 3840×2160，竖屏派生从不生效）；width/height 同步写保持
        # 旧消费者兼容。
        config["resolution"] = [width, height]
        config["width"] = width
        config["height"] = height
        # N06：字幕模式走 job 侧真实消费键 ``subtitle_mode``
        # （SubtitleMode.OFF/BURN/SIDECAR）；``burn_subtitles`` bool
        # 保留兼容旧消费者。语言→轨身份经注入解析器落
        # ``subtitle_track_id``；策略非 off 而语言无对应轨 → 拒绝
        # （不静默导出无字幕版本）。
        mode = SUBTITLE_STRATEGY_MODES.get(version.subtitle_strategy)
        if mode is None:
            refusals.append(
                f"版本 {version.version_id}：未知字幕策略 "
                f"{version.subtitle_strategy}"
                f"（支持：{sorted(SUBTITLE_STRATEGY_MODES)}）")
            continue
        track_id = None
        if mode != "off" and resolve_track_id is not None:
            # 解析器由批次入口注入（生产路径总是提供）；解析失败 →
            # 拒绝该版本（不盲选默认轨）。纯函数调用方不传解析器时
            # 保持轨身份缺失（消费侧按 language 呈现，不冒充已解析）。
            track_id = resolve_track_id(version.language)
            if not track_id:
                refusals.append(
                    f"版本 {version.version_id}：语言 {version.language}"
                    " 无对应字幕轨——拒绝（不盲选默认轨）")
                continue
        config["subtitle_mode"] = mode
        config["burn_subtitles"] = mode == "burn"
        config["subtitle_language"] = version.language
        if track_id:
            config["subtitle_track_id"] = track_id
        # 版本溯源（恢复/重试验证源版本——不用旧缓存续新版本）
        config["source_project_revision"] = int(source_project_revision)
        if source_snapshot_hash:
            config["source_snapshot_hash"] = source_snapshot_hash
        config["linked_version_id"] = version.version_id
        config["canvas_aspect"] = version.canvas_aspect
        output_path = str(config.get("output_path", "") or "")
        if not output_path:
            refusals.append(
                f"版本 {version.version_id}：基线配置缺 output_path")
            continue
        # 每版本独立输出（同目录按版本 id 去重命名）
        if "{version}" in output_path:
            output_path = output_path.replace(
                "{version}", version.version_id)
        else:
            base, dot, ext = output_path.rpartition(".")
            output_path = f"{base}_{version.version_id}.{ext}" if dot \
                else f"{output_path}_{version.version_id}"
        if output_path in existing:
            refusals.append(
                f"版本 {version.version_id}：输出路径冲突 {output_path}"
                "——已在该队列（不覆盖成功输出）")
            continue
        existing.add(output_path)
        config["output_path"] = output_path
        jobs.append(CompiledVersionJob(
            linked_version_id=version.version_id,
            name=(f"{version.canvas_aspect} · {version.language}"
                  f" · {version.subtitle_strategy}"),
            config=config,
            output_path=output_path))
    if refusals and not jobs:
        return VersionCompileResult((), tuple(refusals))
    return VersionCompileResult(tuple(jobs), tuple(refusals))


def count_audio_frames(path: str) -> Optional[Dict[str, int]]:
    """F9.3 · PCM 级音频解码校验（probe 缺口）。

    全量解码音频流统计帧数/采样数——无声（0 帧）或解码失败如实返回
    （None=打开失败；0 帧入 manifest 供 QC 判"声明有音轨但无音频帧"）。
    """
    try:
        import av

        with av.open(str(path)) as container:
            streams = container.streams.audio
            if not streams:
                return {"audio_streams": 0, "audio_frames": 0,
                        "audio_samples": 0}
            stream = streams[0]
            frames = 0
            samples = 0
            for packet in container.demux(stream):
                for frame in packet.decode():
                    frames += 1
                    samples += frame.samples
            return {"audio_streams": len(streams),
                    "audio_frames": frames, "audio_samples": samples}
    except Exception:  # noqa: BLE001 — 打开/解码失败如实 None
        return None


__all__ = [
    "ASPECT_RESOLUTIONS",
    "SUBTITLE_STRATEGY_MODES",
    "CompiledVersionJob",
    "VersionCompileResult",
    "compile_version_jobs",
    "count_audio_frames",
]
