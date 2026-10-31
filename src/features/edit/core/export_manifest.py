"""Export Manifest —— 导出可追溯清单（P1-18）。

背景（计划表 P1-18）：导出已有 preflight 与响度报告，但每次交付缺少
一份能回答"使用了什么源、什么版本、哪些地方降级"的归档清单。
跨机器收集、外部交付和重开验收都没有可审计依据。

本模块为每次导出生成 manifest（schema ``topos.export_manifest.v1``）：

- 工程与时间线 revision（可复现性的锚点）；
- 全部源素材的路径/大小/修改时间/SHA-256（缺失素材如实标注）；
- 导出配置快照（codec/分辨率/帧率/音频/字幕烧录/渲染模式）；
- 能力注册表快照（P1-17）——交付时刻各能力的真实状态；
- 交付 QC 报告（P1-19）与 preflight 阻断/降级记录；
- 响度报告（若启用）与输出文件完整性（大小/SHA-256）。

设计约束：
- 纯模型 + 标准库，无 Qt/GPU；对 Mock/鸭子类型工程防御（getattr 全兜底）；
- manifest 写失败绝不影响导出结果（调用方负责捕获降级）；
- JSON 原子落盘（临时文件 + os.replace）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EXPORT_MANIFEST_SCHEMA = "topos.export_manifest.v1"
MANIFEST_SUFFIX = ".manifest.json"


@dataclass(frozen=True)
class AssetRecord:
    """单个源素材的可追溯记录。"""

    asset_id: str
    display_name: str
    path: str
    status: str  # ok / missing / unresolved / hash_failed
    file_size: Optional[int] = None
    mtime: Optional[float] = None
    sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "display_name": self.display_name,
            "path": self.path,
            "status": self.status,
            "file_size": self.file_size,
            "mtime": self.mtime,
            "sha256": self.sha256,
        }


def manifest_path_for(output_path: str) -> str:
    """导出文件对应的 manifest 路径（<output>.manifest.json）。"""
    return f"{output_path}{MANIFEST_SUFFIX}"


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _iter_clip_asset_ids(sequence: Any):
    seen: set = set()
    for track in getattr(sequence, "all_tracks", ()):
        for clip in getattr(track, "clips", ()):
            asset_id = getattr(clip, "asset_id", "")
            if asset_id and asset_id not in seen:
                seen.add(asset_id)
                yield asset_id


def collect_asset_records(
    sequence: Any,
    project: Optional[Any],
    *,
    hash_files: bool = True,
) -> Tuple[Tuple[AssetRecord, ...], Tuple[str, ...]]:
    """收集时间线引用的全部素材记录，缺失项如实标注。

    ``hash_files=False`` 只读取文件元数据并留下延后警告，供导出完成
    通知前快速落盘 metadata manifest；正常审计默认仍计算完整 SHA-256。

    Returns:
        (records, warnings) —— warnings 供导出日志与 manifest 同时保留。
    """
    records: List[AssetRecord] = []
    warnings: List[str] = []
    lookup = getattr(project, "get_asset_by_id", None)
    for asset_id in _iter_clip_asset_ids(sequence):
        asset = lookup(asset_id) if callable(lookup) else None
        if asset is None:
            records.append(AssetRecord(
                asset_id=asset_id,
                display_name=asset_id,
                path="",
                status="unresolved",
            ))
            warnings.append(f"素材 {asset_id} 未在工程中解析到，无法追溯")
            continue
        raw_path = getattr(asset, "path", "")
        path = raw_path if isinstance(raw_path, str) else ""
        name = getattr(asset, "display_name", "") or asset_id
        if not isinstance(name, str):
            name = asset_id
        if not path or not os.path.exists(path):
            records.append(AssetRecord(
                asset_id=asset_id,
                display_name=name,
                path=path,
                status="missing",
            ))
            warnings.append(f"素材「{name}」源文件不存在：{path or '(空路径)'}")
            continue
        try:
            stat = os.stat(path)
            digest = _sha256_file(path) if hash_files else None
        except OSError as exc:
            records.append(AssetRecord(
                asset_id=asset_id, display_name=name, path=path,
                status="hash_failed", mtime=None,
            ))
            warnings.append(f"素材「{name}」哈希失败：{exc}")
            continue
        records.append(AssetRecord(
            asset_id=asset_id,
            display_name=name,
            path=path,
            status="ok",
            file_size=stat.st_size,
            mtime=stat.st_mtime,
            sha256=digest,
        ))
        if not hash_files:
            warnings.append(f"素材「{name}」SHA-256 延后在后台生成")
    return tuple(records), tuple(warnings)


def _timeline_snapshot(sequence: Any) -> Dict[str, Any]:
    fps = getattr(sequence, "fps", None)
    fps_num = int(getattr(fps, "num", 0) or 0)
    fps_den = int(getattr(fps, "den", 1) or 1)
    return {
        "timeline_id": getattr(sequence, "timeline_id", ""),
        "name": getattr(sequence, "name", ""),
        "revision": int(getattr(sequence, "revision", 0) or 0),
        "fps": {"num": fps_num, "den": fps_den, "float": fps_num / fps_den if fps_den else 0.0},
        "width": int(getattr(sequence, "width", 0) or 0),
        "height": int(getattr(sequence, "height", 0) or 0),
        "sample_rate": int(getattr(sequence, "sample_rate", 0) or 0),
        "duration_frames": int(getattr(sequence, "duration_frames", 0) or 0),
        "range_start": getattr(sequence, "range_start", None),
        "range_end": getattr(sequence, "range_end", None),
    }


def _project_snapshot(project: Any) -> Dict[str, Any]:
    return {
        "project_id": getattr(project, "project_id", ""),
        "name": getattr(project, "name", ""),
        "revision": int(getattr(project, "revision", 0) or 0),
        "modified_at": getattr(project, "modified_at", None),
    }


def _jsonable(value: Any) -> Any:
    """枚举等非原生类型降级为字符串，保证 manifest 可 JSON 序列化。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _config_snapshot(config: Any) -> Dict[str, Any]:
    keys = (
        "codec", "resolution", "fps", "include_audio", "burn_subtitles",
        "render_mode", "range_type", "audio_codec", "audio_sample_rate",
        "audio_channels", "bitrate", "crf", "pixel_format",
        "loudness_target_lufs",
        # T-P1-70 步骤 4：AI 导出溯源（None 时省略，非 AI 发起不出现）。
        "ai_plan_id", "ai_job_id",
    )
    snapshot: Dict[str, Any] = {}
    for key in keys:
        value = getattr(config, key, None)
        if value is not None:
            snapshot[key] = _jsonable(value)
    return snapshot


def _output_snapshot(output_path: Optional[str]) -> Dict[str, Any]:
    if not output_path:
        return {"path": output_path or "", "present": False}
    if os.path.isdir(output_path):
        # R16（第七轮审计）：目录式图像序列交付——单文件哈希在这里只会
        # 报 "Is a directory"。列帧清单/逐帧哈希/首尾帧号（FPS 在 config
        # snapshot），缺帧/取消的导出不会走到成功 manifest。
        import re as _re

        frame_re = _re.compile(r"^(.+)_(\d+)\.(png|tif|tiff)$", _re.IGNORECASE)
        entries = []
        for name in sorted(os.listdir(output_path)):
            m = frame_re.match(name)
            if not m:
                continue
            path = os.path.join(output_path, name)
            try:
                entries.append({
                    "name": name,
                    "size": os.stat(path).st_size,
                    "sha256": _sha256_file(path),
                    "frame": int(m.group(2)),
                })
            except OSError as exc:
                logger.warning("Manifest frame hash failed: %s", exc)
        numbers = [e["frame"] for e in entries]
        return {
            "path": output_path,
            "present": True,
            "sequence": {
                "frame_files": len(entries),
                "first_frame": min(numbers) if numbers else None,
                "last_frame": max(numbers) if numbers else None,
                "frames": entries,
            },
        }
    # F1.3（基线缺陷修复）：present 必须反映输出文件存在性——不存在时
    # 返回 False（此前恒 True 且只带 hash_error，"缺失输出被记为在场"）。
    if not os.path.exists(output_path):
        return {"path": output_path, "present": False}
    try:
        stat = os.stat(output_path)
        digest = _sha256_file(output_path)
    except OSError as exc:
        # 文件存在但 stat/hash 失败（并发删除/权限）：在场但不可校验。
        logger.warning("Manifest output hash failed: %s", exc)
        return {"path": output_path, "present": True, "hash_error": str(exc)}
    return {
        "path": output_path,
        "present": True,
        "size": stat.st_size,
        "sha256": digest,
    }


def probe_output_media(output_path: Optional[str]) -> Dict[str, Any]:
    """T-P1-70 步骤 3：统一输出 probe（manifest 内置交付验收依据）。

    容器/codec/profile/pix_fmt/分辨率/帧率/帧数/时长/音频流/色彩元数据/
    首尾帧可解码。帧数为**全量解码计数**（诚实口径，manifest 在后台
    daemon 线程写入，不阻塞交付完成）；probe 失败如实返回
    ``probe_failed``，不伪造字段。
    """
    if not output_path or not os.path.exists(output_path):
        return {"present": False}
    probe: Dict[str, Any] = {"present": True}
    try:
        import av

        with av.open(str(output_path)) as container:
            probe["container"] = str(
                getattr(container, "format", None) and
                container.format.name or "")
            vstream = next(iter(container.streams.video), None)
            if vstream is not None:
                cc = vstream.codec_context
                probe["video"] = {
                    "codec": cc.name,
                    "profile": str(cc.profile or ""),
                    "pix_fmt": str(cc.pix_fmt),
                    "width": int(cc.width or 0),
                    "height": int(cc.height or 0),
                }
            probe["audio_streams"] = []
            for stream in container.streams.audio:
                cc = stream.codec_context
                probe["audio_streams"].append({
                    "codec": cc.name,
                    "channels": int(cc.channels or 0),
                    "sample_rate": int(cc.sample_rate or 0),
                })
            frame_count = 0
            first_ok = False
            last_ok = False
            for packet in container.demux():
                for frame in packet.decode():
                    if isinstance(frame, av.AudioFrame):
                        continue
                    frame_count += 1
                    if frame_count == 1:
                        first_ok = True
                    last_ok = True
            probe["frame_count"] = frame_count
            probe["first_frame_decodes"] = first_ok
            probe["last_frame_decodes"] = last_ok
            if vstream is not None and vstream.average_rate is not None:
                probe["video"]["fps"] = float(
                    vstream.average_rate) or None
            duration = float(getattr(container, "duration", 0) or 0)
            if duration > 0:
                probe["duration_seconds"] = round(
                    duration / 1_000_000.0, 3)
    except Exception as exc:  # noqa: BLE001 — probe 失败必须如实可读
        probe["probe_failed"] = f"{type(exc).__name__}: {exc}"
    return probe


def _collect_degradations(
    config: Optional[Any],
    sequence: Any,
    stage_telemetry: Optional[Dict[str, Any]],
    preflight_result: Optional[Any],
    output_probe: Optional[Dict[str, Any]],
    effect_export_skips: Optional[Tuple[str, ...]],
) -> List[Dict[str, Any]]:
    """T-P1-70 步骤 5：所有降级/回退/省略进入结果，不只写日志。"""
    entries: List[Dict[str, Any]] = []

    if stage_telemetry:
        for blocker in stage_telemetry.get("full_gpu_blockers") or []:
            entries.append({"code": "gpu_blocker", "detail": str(blocker)})
        root_cause = stage_telemetry.get("fallback_root_cause")
        if root_cause:
            entries.append({
                "code": "fallback_root_cause", "detail": str(root_cause)})

    if preflight_result is not None:
        for msg in getattr(preflight_result, "degraded_messages", ()) or ():
            entries.append({"code": "preflight_degraded", "detail": str(msg)})

    for effect_type in effect_export_skips or ():
        entries.append({
            "code": "effect_export_skipped",
            "detail": (f"效果 {effect_type} 无交付实现，导出帧未应用"
                       "（预览可能仍可见）"),
        })

    if (config is not None and getattr(config, "include_audio", False)
            and output_probe and output_probe.get("present")
            and "audio_streams" in output_probe
            and not output_probe["audio_streams"]):
        has_audio_clips = any(
            getattr(track, "clips", ())
            for track in getattr(sequence, "all_tracks", ())
            if getattr(track, "track_type", "") == "audio")
        if has_audio_clips:
            entries.append({
                "code": "audio_omitted",
                "detail": "时间线含音频片段但交付文件无音频流",
            })
    return entries


def build_export_manifest(
    project: Any,
    sequence: Any,
    config: Optional[Any] = None,
    *,
    qc_report: Optional[Any] = None,
    preflight_result: Optional[Any] = None,
    loudness_report: Optional[Any] = None,
    capabilities: Optional[Any] = None,
    output_path: Optional[str] = None,
    hash_assets: bool = True,
    stage_telemetry: Optional[Dict[str, Any]] = None,
    effect_export_skips: Optional[Tuple[str, ...]] = None,
    probe_media: bool = True,
) -> Dict[str, Any]:
    """构建导出 manifest（dict，可直接 JSON 序列化）。

    Args:
        capabilities: CapabilityRegistry（P1-17）；None 时现场探测一次。
        qc_report / preflight_result / loudness_report: 传入对应报告对象
            （有 ``to_dict`` 即可）；None 的段整体省略。
        output_path: 输出文件路径（存在则记录大小与 SHA-256）。
        hash_assets: 是否在本次调用中同步扫描源素材 SHA-256。正常审计
            保持 True；导出完成通知前可设 False，先落 metadata manifest，
            再由后台任务原子替换为完整哈希版本。
        stage_telemetry: T-P1-70 步骤 4——统一阶段 telemetry 快照
            （``ExportTelemetryRecorder.snapshot().to_dict()``），含
            full_gpu 判定、blockers、fallback 根因与阶段统计。
        effect_export_skips: T-P1-70 步骤 5——导出中被跳过的效果类型
            （如无 CPU 实现的插件），逐项进入 degradations。
        probe_media: 是否对输出文件做统一 media probe（步骤 3）。后台
            完整哈希版本保持 True；快速 metadata sidecar 可设 False。
    """
    if capabilities is None:
        try:
            from src.features.edit.core.capability_registry import (
                build_capability_registry,
            )
            capabilities = build_capability_registry()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Capability snapshot unavailable for manifest: %s", exc)
            capabilities = None

    records, warnings = collect_asset_records(
        sequence, project, hash_files=bool(hash_assets)
    )

    # P1-13: 使用位置索引——缺失素材报告可回答"都在哪些时间线被用到"
    try:
        from src.features.edit.core.asset_usage_index import build_asset_usage_index
        usage_index = build_asset_usage_index([sequence], project)
        manifest_asset_usage = usage_index.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Asset usage index unavailable for manifest: %s", exc)
        manifest_asset_usage = None

    manifest: Dict[str, Any] = {
        "schema": EXPORT_MANIFEST_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": _project_snapshot(project),
        "timeline": _timeline_snapshot(sequence),
        "assets": [r.to_dict() for r in records],
        "asset_warnings": list(warnings),
    }
    if config is not None:
        manifest["config"] = _config_snapshot(config)
    if manifest_asset_usage is not None:
        manifest["asset_usage"] = manifest_asset_usage
    if capabilities is not None and hasattr(capabilities, "to_dict"):
        manifest["capabilities"] = capabilities.to_dict()
    if qc_report is not None and hasattr(qc_report, "to_dict"):
        manifest["delivery_qc"] = qc_report.to_dict()
    if preflight_result is not None:
        manifest["preflight"] = {
            "ok": bool(getattr(preflight_result, "ok", False)),
            "blocking": list(getattr(preflight_result, "blocking_messages", []) or []),
            "degraded": list(getattr(preflight_result, "degraded_messages", []) or []),
            "blockers": [item.to_dict() for item in
                         (getattr(preflight_result, "blockers", ()) or ())],
        }
    if loudness_report is not None and hasattr(loudness_report, "to_dict"):
        manifest["loudness"] = loudness_report.to_dict()
    manifest["output"] = _output_snapshot(output_path)
    # T-P1-70 步骤 3/4/5：统一 probe、GPU trail、降级清单入 manifest。
    output_probe = (
        probe_output_media(output_path)
        if probe_media and manifest["output"].get("present") else None)
    if output_probe is not None:
        manifest["output_probe"] = output_probe
    if stage_telemetry is not None:
        manifest["stage_telemetry"] = stage_telemetry
    degradations = _collect_degradations(
        config, sequence, stage_telemetry, preflight_result,
        output_probe, effect_export_skips)
    if degradations:
        manifest["degradations"] = degradations
    return manifest


def write_manifest_file(path: str, manifest: Dict[str, Any]) -> str:
    """manifest 原子落盘（临时文件 + os.replace），返回路径。"""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return path
