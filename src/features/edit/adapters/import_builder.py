"""ImportResult → EditProject 落地构建器（P1-16 生产接线）。

把 FCPXML/OTIO adapter 产出的候选 ``ImportResult`` 转成真实的 EditProject：

- 素材：文件存在的经 ``EditProject.import_asset`` 落池（路径去重）；文件
  缺失的生成离线素材（``is_offline=True``，metadata 记录 ``external_ref``
  与原始 URL，供后续 relink）；
- clip 重映射：外部引用 id（FCPXML ``<asset id>`` / OTIO ``target_url``）→
  工程内真实 ``asset_id``；时间范围、效果等参数原样保留；
- 时间线：完整 ``TimelineSequence`` 返回给调用方注册（运行时住在
  MainWindow），同时 upsert 轻量 ``TimelineSequenceRef``（fps Rational →
  float）并把新时间线设为 active。

纯 core 实现（无 Qt），可独立测试。适配器产出的 ``InterchangeReport``
原样透传，由 UI 层负责 preview/确认（TAEP §9.2）。
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from urllib.parse import unquote, urlparse

from src.features.edit.adapters.interchange_types import ImportResult
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    TimelineSequenceRef,
)

_WINDOWS_DRIVE_RE = re.compile(r"^/[A-Za-z]:/")


def url_to_local_path(url: str) -> str:
    """file:// URL 或裸路径 → 本地路径；其他协议原样返回。

    W07 Windows：返回经 ``os.path.normpath`` 的**原生分隔符**路径
    （file:///C:/x.mp4 → 原生分隔符路径）——与工程其余路径存储一致，
    get_asset_by_path 的字符串去重比较才不会因斜杠风格错配漏判。
    """
    if not url:
        return ""
    if url.startswith("file://"):
        parsed = urlparse(url)
        path = unquote(parsed.path) if parsed.path else url
        # Windows 盘符：file:///C:/x.mp4 → urlparse 给 /C:/x.mp4
        if _WINDOWS_DRIVE_RE.match(path):
            path = path[1:]
        return os.path.normpath(path)
    return url


@dataclass(frozen=True)
class InterchangeImportSummary:
    """导入落地结果摘要（供 UI 报告）。"""

    imported_asset_ids: tuple[str, ...] = ()
    reused_asset_ids: tuple[str, ...] = ()
    offline_assets: tuple[tuple[str, str], ...] = ()  # (asset_id, path/ref)
    sequence_ids: tuple[str, ...] = ()
    unmapped_clip_ids: tuple[str, ...] = ()

    @property
    def asset_count(self) -> int:
        return (
            len(self.imported_asset_ids)
            + len(self.reused_asset_ids)
            + len(self.offline_assets)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported_asset_ids": list(self.imported_asset_ids),
            "reused_asset_ids": list(self.reused_asset_ids),
            "offline_assets": [
                {"asset_id": aid, "path": p} for aid, p in self.offline_assets
            ],
            "sequence_ids": list(self.sequence_ids),
            "unmapped_clip_ids": list(self.unmapped_clip_ids),
        }


def _normalized_asset_descs(result: ImportResult) -> list[dict[str, Any]]:
    """规范 asset 描述：补 external_id；OTIO 无 desc 时从 clips 推导。"""
    descs: list[dict[str, Any]] = []
    for raw in result.assets:
        desc = dict(raw)
        ext = str(desc.get("asset_id") or "")
        if ext.startswith("imp-"):
            ext = ext[len("imp-"):]
        desc["external_id"] = ext
        descs.append(desc)
    if descs:
        return descs

    # OTIO import 不产 asset 描述：clip.asset_id 即 target_url。
    seen: dict[str, dict[str, Any]] = {}
    for seq in result.sequences:
        for track in (*seq.video_tracks, *seq.audio_tracks):
            media_type = "video" if getattr(track, "track_type", "video") == "video" else "audio"
            for clip in track.clips:
                ref = str(clip.asset_id)
                if ref in seen:
                    continue
                local = url_to_local_path(ref)
                seen[ref] = {
                    "asset_id": ref,
                    "external_id": ref,
                    "name": os.path.basename(local) or ref,
                    "media_type": media_type,
                    "path": ref,
                }
    return list(seen.values())


def _add_offline_asset(project: EditProject, asset: MediaAsset) -> EditProject:
    """加入离线素材（镜像 import_asset 的 root bin 逻辑）。"""
    new_assets = dict(project.assets)
    new_assets[asset.asset_id] = asset

    new_bins = dict(project.bins)
    root_bin = project.bins.get(project.root_bin_id)
    if root_bin is not None:
        new_bins[project.root_bin_id] = root_bin.with_asset_added(asset.asset_id)

    return replace(
        project,
        assets=new_assets,
        bins=new_bins,
        modified_at=time.time(),
    )


def collect_asset_rows(result: ImportResult) -> list[tuple[str, str, bool]]:
    """导入预览用的素材行：(名称, 本地路径, 文件是否存在)。

    Thread affinity (AUD-12613)：对每个素材做 ``os.path.exists`` 阻塞
    磁盘探测（网络卷/离线盘上可达秒级）。生产入口在 UI 槽内同步调用
    本函数（src/app/controllers/file_controller.py）；大型导入应改由
    worker 线程调用后再把行数据投递回 UI 线程。
    """
    rows: list[tuple[str, str, bool]] = []
    for desc in _normalized_asset_descs(result):
        local = url_to_local_path(str(desc.get("path") or ""))
        name = str(desc.get("name") or os.path.basename(local) or desc.get("external_id") or "?")
        rows.append((name, local or str(desc.get("path") or ""), bool(local) and os.path.exists(local)))
    return rows


def build_project_from_import_result(
    base: EditProject,
    result: ImportResult,
    *,
    probe_metadata: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[EditProject, dict[str, Any], InterchangeImportSummary]:
    """把适配器 ImportResult 落地为工程内容。

    Args:
        base: 宿主工程（素材/时间线会合并进去，路径去重）
        result: 适配器产出的候选导入结果
        probe_metadata: 在线素材元数据探测函数（默认用 core 的
            ``probe_media_metadata``；测试可注入轻量 stub）

    Returns:
        (新 EditProject, {timeline_id: 完整 TimelineSequence}, 摘要)

    Thread affinity (AUD-12613)：对每个在线素材执行 ``os.path.exists`` +
    ``probe_metadata`` 逐素材阻塞探测（ffprobe/解码器探测可达百毫秒级/
    素材）。生产入口在 UI 槽内同步调用本函数
    （src/app/controllers/file_controller.py）；素材量大时应由 worker
    线程调用并把结果经信号/队列投递回 UI 线程。
    """
    from src.features.edit.core.project import probe_media_metadata

    probe = probe_metadata or probe_media_metadata
    project = base

    imported: list[str] = []
    reused: list[str] = []
    offline: list[tuple[str, str]] = []
    # 外部引用键（FCPXML asset id / OTIO target_url / 各种路径形式）→ 工程内 asset_id
    remap: dict[str, str] = {}

    for desc in _normalized_asset_descs(result):
        external_id = str(desc.get("external_id") or "")
        raw_path = str(desc.get("path") or "")
        local = url_to_local_path(raw_path)
        display_name = (
            str(desc.get("name") or "")
            or os.path.basename(local)
            or external_id
            or "Imported Media"
        )
        media_type = str(desc.get("media_type") or "video")

        topos_id: str
        if local and os.path.exists(local):
            existed = project.get_asset_by_path(local) is not None
            try:
                metadata = probe(local)
            except Exception as exc:
                # AUD-12614：单个损坏素材的探测失败只降级该素材（空元数据
                # 继续导入，缺失字段由下游惰性补全），不中断整批导入。
                logger.warning(
                    "素材元数据探测失败，降级为空元数据继续导入: %s (%s)",
                    local, exc)
                metadata = {}
            project = project.import_asset(
                path=local,
                media_type=media_type,
                metadata=metadata,
                display_name=display_name,
            )
            asset = project.get_asset_by_path(local)
            topos_id = asset.asset_id if asset is not None else ""
            (reused if existed else imported).append(topos_id)
        else:
            # 离线素材同样按路径去重：重复导入同一文件不产生重复离线项
            offline_path = (
                local
                or raw_path
                or f"topos://interchange/{external_id or uuid.uuid4().hex[:8]}"
            )
            existing_offline = project.get_asset_by_path(offline_path)
            if existing_offline is not None:
                topos_id = existing_offline.asset_id
                reused.append(topos_id)
            else:
                topos_id = str(uuid.uuid4())
                asset = MediaAsset(
                    asset_id=topos_id,
                    path=offline_path,
                    media_type=media_type,
                    display_name=display_name,
                    metadata={
                        "source_kind": "interchange",
                        "external_ref": external_id,
                        "original_url": raw_path,
                    },
                    proxy_status="none",
                    created_at=time.time(),
                    is_offline=True,
                )
                project = _add_offline_asset(project, asset)
                offline.append((topos_id, offline_path))

        # 首个命中优先注册，避免多个 desc 的键互相覆盖
        for key in (
            external_id,
            raw_path,
            local,
            os.path.basename(local) if local else "",
        ):
            if key and key not in remap:
                remap[key] = topos_id

    # clip 重映射 + 重建 sequence / ref
    sequences_out: dict[str, TimelineSequence] = {}
    unmapped: list[str] = []

    def _remap_track(track: EditTrack) -> EditTrack:
        new_clips = []
        for clip in track.clips:
            new_id = remap.get(clip.asset_id) or remap.get(
                url_to_local_path(str(clip.asset_id))
            )
            if new_id:
                new_clips.append(replace(clip, asset_id=new_id))
            else:
                unmapped.append(clip.clip_id)
                new_clips.append(clip)
        return replace(track, clips=tuple(new_clips))

    for seq in result.sequences:
        new_seq = seq._rebuild(
            video_tracks=tuple(_remap_track(t) for t in seq.video_tracks),
            audio_tracks=tuple(_remap_track(t) for t in seq.audio_tracks),
        )
        timeline_id = new_seq.timeline_id
        if timeline_id in project.timelines or timeline_id in sequences_out:
            timeline_id = f"{timeline_id}-{uuid.uuid4().hex[:6]}"
            new_seq = replace(new_seq, timeline_id=timeline_id)
        sequences_out[timeline_id] = new_seq

        ref = TimelineSequenceRef(
            timeline_id=timeline_id,
            name=new_seq.name or "Imported Timeline",
            fps=float(new_seq.fps),
            width=new_seq.width,
            height=new_seq.height,
            sample_rate=new_seq.sample_rate,
            format_settings=dict(new_seq.format_settings),
            created_at=time.time(),
        )
        new_timelines = dict(project.timelines)
        new_timelines[timeline_id] = ref
        project = replace(
            project,
            timelines=new_timelines,
            active_timeline_id=timeline_id,
            modified_at=time.time(),
        )

    summary = InterchangeImportSummary(
        imported_asset_ids=tuple(imported),
        reused_asset_ids=tuple(reused),
        offline_assets=tuple(offline),
        sequence_ids=tuple(sequences_out.keys()),
        unmapped_clip_ids=tuple(unmapped),
    )
    return project, sequences_out, summary
