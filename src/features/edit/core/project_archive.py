"""P1-13 工程收集 / 归档（Collect / Archive）。

将工程引用的媒体文件收集到目标目录并逐文件校验 SHA-256，产出一份
路径已重写（指向归档内副本）的新 EditProject——保存该工程后即可在
另一台机器打开（计划表 P1-13 验收）。

缺失素材不做静默跳过：报告中列出素材及其全部时间线使用位置
（"缺失素材报告包含所有使用位置"）。

设计约束：
- 纯核心模块，不依赖 Qt：工程包的写出由调用方（UI / 测试）用
  EditProjectIO.save_project 落盘，本模块只负责媒体收集、校验与
  路径重写。
- 虚拟素材（comp_clip/nested/solid 等）没有宿主文件，不参与收集。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from src.features.edit.core.asset_usage_index import build_asset_usage_index
from src.features.edit.core.project import EditProject, batch_relink_assets

logger = logging.getLogger(__name__)

ARCHIVE_REPORT_SCHEMA = "topos.project_archive.v1"
#: W10：哈希算法版本随 schema 固化（未来算法升级 = schema bump +
#: 该字段变更，读端按字段分发而不是猜）。
ARCHIVE_HASH_ALGO = "sha256"

#: 收集状态
STATUS_COPIED = "copied"
STATUS_MISSING = "missing"
STATUS_HASH_MISMATCH = "hash_mismatch"
STATUS_COPY_FAILED = "copy_failed"

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")

#: W10：Windows 保留设备名（不带扩展名比较；CON.mov/CON.txt 同样非法）
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _sha256_of(path: str, chunk: int = 1 << 20) -> str:
    """计算聚合 SHA-256（AUD-17201）。

    文件 → 内容哈希；目录 → 按（排序后相对路径 + 文件内容）顺序聚合
    的哈希：任何文件的增删/改名/内容变化都会改变结果。目录素材
    （image_sequence）不再以 ""=="" 伪装成校验通过。

    W10 跨机契约：相对路径统一以 ``/`` 进入哈希——同一目录内容在
    Windows（``os.sep='\\'``）与 macOS/Linux 上必须得到相同哈希，
    分隔符差异不得算成内容变化（Mac→Windows→Mac 往返对拍依赖此点）。
    """
    h = hashlib.sha256()
    if os.path.isdir(path):
        for root, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for filename in sorted(filenames):
                file_path = os.path.join(root, filename)
                rel = os.path.relpath(file_path, path).replace(os.sep, "/")
                h.update(rel.encode("utf-8", errors="surrogateescape") + b"\0")
                with open(file_path, "rb") as f:
                    while True:
                        block = f.read(chunk)
                        if not block:
                            break
                        h.update(block)
        return h.hexdigest()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _sanitize_name(name: str, fallback: str) -> str:
    cleaned = _UNSAFE_FILENAME.sub("_", name).strip("._") or fallback
    cleaned = cleaned[:120]
    # W10：Windows 保留设备名带任何扩展名仍然非法（CON.mov → 设备），
    # 按词干匹配前缀化（_CON.mov 合法且可读）；NTFS/FAT/APFS 均接受。
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    # 尾部点/空格在 Windows 被静默剥离（创建名≠请求名，校验路径错位）
    return cleaned.rstrip(". ")


@dataclass(frozen=True)
class ArchiveFileRecord:
    """单个素材的收集结果。"""

    asset_id: str
    display_name: str
    source_path: str
    archived_path: str  # 相对归档根目录；缺失/失败时为空串
    status: str
    sha256: str = ""
    file_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "display_name": self.display_name,
            "source_path": self.source_path,
            "archived_path": self.archived_path,
            "status": self.status,
            "sha256": self.sha256,
            "file_size": self.file_size,
        }


@dataclass
class ArchiveReport:
    """收集报告：文件清单 + 缺失素材使用位置 + 校验结果。"""

    archive_root: str
    records: tuple[ArchiveFileRecord, ...] = ()
    # 缺失素材 → 全部使用位置（人类可读字符串，来自 AssetUsageIndex）
    missing_usage: dict[str, tuple[str, ...]] = field(default_factory=dict)
    skipped_unused: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # M20.2b：字体/插件/模型身份清单（dependencies.json 内容快照）
    identity_manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def copied(self) -> tuple[ArchiveFileRecord, ...]:
        return tuple(r for r in self.records if r.status == STATUS_COPIED)

    @property
    def missing(self) -> tuple[ArchiveFileRecord, ...]:
        return tuple(r for r in self.records if r.status == STATUS_MISSING)

    @property
    def failures(self) -> tuple[ArchiveFileRecord, ...]:
        return tuple(
            r for r in self.records
            if r.status in (STATUS_HASH_MISMATCH, STATUS_COPY_FAILED)
        )

    @property
    def passed(self) -> bool:
        """收集成功 = 没有校验失败（缺失素材允许，见 missing_usage）。"""
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ARCHIVE_REPORT_SCHEMA,
            # W10：哈希算法版本固化（读端按字段分发；分隔符见下）
            "hash_algo": ARCHIVE_HASH_ALGO,
            "archive_root": self.archive_root,
            "records": [r.to_dict() for r in self.records],
            "missing_usage": {
                asset_id: list(locs) for asset_id, locs in self.missing_usage.items()
            },
            "skipped_unused": list(self.skipped_unused),
            "warnings": list(self.warnings),
            # M20.2b：身份清单（字体/插件/模型）随归档写出
            # dependencies.json 时的内容快照（空 dict = 未生成）。
            "identity_manifest": dict(self.identity_manifest),
        }


#: R02（媒体池复查）：单文件复制前的最低预留空间（哈希读缓存/元数据余量）
_MIN_FREE_SLACK_BYTES = 1 << 20


def _preexisting_names(dir_path: str) -> set[str]:
    """R02：目标子目录既有条目名（大小写不敏感键）。

    这些名字进入唯一名空间后，本轮收集的所有目标名都会避开它们——
    目标目录已有文件（含 LUT/manifest 之外的同名媒体）绝不被覆盖，
    冲突策略 = 追加序号改名，既有文件字节保持不变。
    """
    try:
        return {name.casefold() for name in os.listdir(dir_path)}
    except OSError:
        return set()


def _free_bytes(path: str) -> Optional[int]:
    """R02：持续空间检查——目标所在卷剩余字节（探测失败返回 None）。"""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _source_size_bytes(path: str) -> int:
    """源文件/目录树的磁盘字节数（目录素材递归求和；失败返回 0）。"""
    if os.path.isdir(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for filename in files:
                try:
                    total += os.path.getsize(os.path.join(root, filename))
                except OSError:
                    continue
        return total
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_partial(dest_path: str, src: str) -> None:
    """R02：复制失败只清理本任务刚创建的半成品。

    目标名经唯一名空间分配（不与既有文件重名），因此 dest_path 处的
    内容只可能是本轮写入的半成品；再用 realpath 双保险确认不是源
    自身（源/目标相同或别名时绝不误删源文件）。
    """
    try:
        if os.path.realpath(dest_path) == os.path.realpath(src):
            return
        if os.path.isdir(dest_path) and not os.path.islink(dest_path):
            shutil.rmtree(dest_path)
        else:
            os.remove(dest_path)
    except OSError:
        logger.debug("archive partial cleanup failed: %s", dest_path,
                     exc_info=True)


def lut_dependency_paths(project: Any) -> set[str]:
    """M20.2 LUT 闭包扫描（调色栈 + 合成图 LUT 节点）。

    从 :func:`collect_project` 抽出共用：归档收集与 Media Management
    的空间估算（R04）消费同一份依赖路径，避免两处近似清单漂移。
    """
    lut_paths: set[str] = set()
    try:
        for stack in (getattr(project, "color_stacks", None) or {}).values():
            lp = getattr(stack, "lut_path", None)
            if lp:
                lut_paths.add(str(lp))
            for layer in (getattr(stack, "layers", None) or ()):
                if isinstance(layer, dict):
                    llp = layer.get("active_lut_path") or layer.get("lut_path")
                    if llp:
                        lut_paths.add(str(llp))
        # N11 余项：Composite 图的 LUT 闭包——compositions[].nodes[]
        # （NodeEngine 序列化 dict）中 lut 节点 parameters["lut_path"]
        # 引用的外部 .cube/.3dl 随归档（旧实现只遍历调色栈，合成图
        # LUT 被静默丢弃=恢复断链）。
        for comp in (getattr(project, "compositions", None) or {}).values():
            for node in (getattr(comp, "nodes", None) or ()):
                if not isinstance(node, dict):
                    continue
                params = node.get("parameters") or {}
                nlp = params.get("lut_path")
                if nlp:
                    lut_paths.add(str(nlp))
    except Exception:  # noqa: BLE001 — 收集失败不阻断媒体归档
        logger.exception("LUT dependency collection failed")
    return lut_paths


def _unique_dest_name(base: str, used: set[str]) -> str:
    """W10：目标名唯一性按大小写不敏感键判定。

    Windows（NTFS/FAT）与 macOS（APFS 默认）文件名大小写不敏感——
    "A.mov" 与 "a.mov" 是同一文件。旧的精确匹配去重在这类系统上放行
    两个"唯一"名，第二次 copy 静默覆盖第一次的归档副本（跨素材错连，
    首个素材的 archived_path 指向他人内容）。统一用 casefold 键；
    对大小写敏感文件系统只是保守改名，无正确性影响。
    """
    def _key(name: str) -> str:
        return name.casefold()

    if _key(base) not in {_key(u) for u in used}:
        used.add(base)
        return base
    stem, ext = os.path.splitext(base)
    counter = 2
    while _key(f"{stem}_{counter}{ext}") in {_key(u) for u in used}:
        counter += 1
    final = f"{stem}_{counter}{ext}"
    used.add(final)
    return final


# ---------------------------------------------------------------------------
# M20.2b · 字体/插件/模型身份闭包（归档只记身份，不可复制项不静默换）
# ---------------------------------------------------------------------------

#: 常见系统字体目录（best-effort 解析字体文件；解析不到只记身份）
_FONT_DIRS = (
    "/Library/Fonts",
    "/System/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "C:/Windows/Fonts",
    "/usr/share/fonts",
    os.path.expanduser("~/.fonts"),
)


def collect_font_families(
    project: Any, sequences: Iterable[Any] = (),
) -> list[str]:
    """收集工程使用的字体族名（字幕片段 font_family，去重保序）。"""
    seen: dict[str, None] = {}
    for sequence in sequences or ():
        for track in getattr(sequence, "subtitle_tracks", ()) or ():
            for clip in getattr(track, "subtitle_clips", ()) or ():
                family = (getattr(clip, "font_family", "") or "").strip()
                if family:
                    seen.setdefault(family, None)
    return list(seen.keys())


def _resolve_font_file(family: str) -> Optional[str]:
    """best-effort 在常见系统字体目录解析字体文件（找不到返回 None）。

    只按文件名主干大小写不敏感匹配（专业字体排版/变体解析不在归档
    范围）；解析结果仅用于记录身份哈希，归档不分发字体文件（许可）。
    """
    wanted = family.strip().lower()
    if not wanted:
        return None
    for font_dir in _FONT_DIRS:
        if not os.path.isdir(font_dir):
            continue
        try:
            for root, _dirs, files in os.walk(font_dir):
                for filename in files:
                    stem, _ext = os.path.splitext(filename)
                    if stem.lower() == wanted:
                        return os.path.join(root, filename)
        except OSError:
            continue
    return None


def build_identity_manifest(
    project: Any,
    sequences: Iterable[Any] = (),
) -> dict[str, Any]:
    """构建归档身份清单（字体/插件/模型——不可复制项保留身份与诊断）。

    - 字体：族名 + 本机解析结果（文件/sha256）；解析不到 → status
      unresolved + 明示"目标机需自备"（字体文件不随包分发，许可）；
    - 插件：复用 P1-COLOR-23 依赖报告的插件段（plugin_id/version/
      content_hash/source_uri；未注册 → missing）；
    - AI 模型：AIModelManager 注册表缓存状态（manager 不可用时空表
      并留 note——不冒充无模型依赖）。
    """
    from src.features.color.core.project_dependencies import (
        KIND_PLUGIN,
        build_dependency_report,
    )

    fonts: list[dict[str, Any]] = []
    for family in collect_font_families(project, sequences):
        entry: dict[str, Any] = {
            "family": family,
            "status": "unresolved",
            "path": None,
            "sha256": None,
            "detail": "本机未解析到字体文件——目标机需自备（字体不随归档分发）",
        }
        resolved = _resolve_font_file(family)
        if resolved:
            entry.update({
                "status": "resolved",
                "path": resolved,
                "sha256": _sha256_of(resolved) or None,
                "detail": "本机已解析（仅记身份，不随归档分发）",
            })
        fonts.append(entry)

    plugins: list[dict[str, Any]] = []
    plugin_report = None
    try:
        plugin_report = build_dependency_report(project, plugin_manager=None)
    except Exception:  # noqa: BLE001 — 插件段失败不阻断归档（留空表）
        logger.exception("plugin identity collection failed")
    if plugin_report is not None:
        plugins = [e.to_dict() for e in plugin_report.by_kind(KIND_PLUGIN)]

    ai_models: list[dict[str, Any]] = []
    models_note = ""
    try:
        from src.shared.ai_models.model_manager import get_model_manager
        model_report = build_dependency_report(
            project, plugin_manager=False,
            model_manager=get_model_manager())
        from src.features.color.core.project_dependencies import KIND_AI_MODEL
        ai_models = [e.to_dict()
                     for e in model_report.by_kind(KIND_AI_MODEL)]
    except Exception:  # noqa: BLE001 — 模型段失败明示不冒充
        models_note = "模型管理器不可用——AI 模型身份未收集（不冒充无依赖）"
        logger.debug("ai model identity collection unavailable",
                     exc_info=True)

    return {
        "schema": "topos.archive_dependencies.v1",
        "hash_algorithm": "sha256",
        "fonts": fonts,
        "plugins": plugins,
        "ai_models": ai_models,
        "notes": [models_note] if models_note else [],
    }



def _rewrite_paths_in_jsonlike(node: Any, mapping: dict[str, str]) -> Any:
    """N11 · 递归改写 payload 中的路径字符串（纯 JSON 形态）。

    字符串精确匹配 mapping 键才替换（不做子串猜测——避免把含路径
    前缀的其他值误改）；dict/list 逐层重建，无命中返回原对象引用
    （调用方据此判断是否需要写回）。
    """
    if isinstance(node, str):
        return mapping.get(node, node)
    if isinstance(node, dict):
        changed = False
        out: dict[str, Any] = {}
        for key, value in node.items():
            new_value = _rewrite_paths_in_jsonlike(value, mapping)
            changed = changed or new_value is not value
            out[key] = new_value
        return out if changed else node
    if isinstance(node, list):
        changed = False
        out_list: list[Any] = []
        for item in node:
            new_item = _rewrite_paths_in_jsonlike(item, mapping)
            changed = changed or new_item is not item
            out_list.append(new_item)
        return out_list if changed else node
    return node


def collect_project(
    project: EditProject,
    *,
    target_dir: str,
    sequences: Iterable[Any] = (),
    include_unused: bool = True,
    media_dir: str = "media",
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancelled_fn: Optional[Callable[[], bool]] = None,
) -> tuple[EditProject, ArchiveReport]:
    """收集工程媒体到 target_dir，返回（路径重写后的工程, 收集报告）。

    Args:
        project: 当前工程
        target_dir: 归档根目录（自动创建；媒体落在其 media/ 子目录）
        sequences: 全部时间线序列，用于缺失素材的使用位置报告与
            include_unused=False 时的"被使用素材"判定
        include_unused: True 收集全部素材；False 只收集时间线引用的素材
            （Resolve 风格 Collect，粒度为整文件；按入出点截取的
            Consolidate/Transcode 属后续迭代）
        progress_callback: 可选进度回调 ``callback(processed, total)``，
            每处理完一个素材调用一次（AUD-17203；长阻塞 I/O 的进度钩子）
        cancelled_fn: 可选取消检查 ``cancelled_fn() -> bool``，每个素材
            处理前轮询；返回 True 时立即停止收集并返回"已收集部分 +
            带 warning 的报告"（AUD-17203；取消钩子）

    Returns:
        (路径重写后的工程, 收集报告)。取消时报告的 warnings 含
        "cancelled" 条目，records/relink 只覆盖已完成部分。
    """
    sequences = list(sequences)
    usage_index = build_asset_usage_index(sequences, project) if sequences else None
    used_ids = usage_index.used_asset_ids() if usage_index is not None else frozenset()

    media_root = os.path.join(target_dir, media_dir)
    os.makedirs(media_root, exist_ok=True)

    # R02（媒体池复查）：目标目录预检——既有文件名进入唯一名空间，
    # 本轮所有目标名避开它们（冲突 = 追加序号改名），既有文件字节
    # 保持不变；dependencies.json 同一所有权策略（已有即保留）。
    used_names: set[str] = _preexisting_names(media_root)
    manifest_path = os.path.join(target_dir, "dependencies.json")
    manifest_preexisting = os.path.exists(manifest_path)
    records: list[ArchiveFileRecord] = []
    missing_usage: dict[str, tuple[str, ...]] = {}
    skipped_unused: list[str] = []
    warnings: list[str] = []
    relink_map: dict[str, str] = {}
    _archived_media_old_paths: dict[str, str] = {}  # N11 快照重写反查

    # 先物化待收集清单：进度回调需要稳定总数，也让取消粒度收敛在素材级
    candidates: list[tuple[str, Any]] = []
    for asset_id, asset in project.assets.items():
        if asset.is_virtual:
            continue  # 无宿主文件的虚拟素材
        if not include_unused and asset_id not in used_ids:
            skipped_unused.append(asset_id)
            continue
        candidates.append((asset_id, asset))

    # M20.2：LUT 外部依赖收集（归档闭包）——调色栈 lut_path 与
    # composite LUT 节点引用的 .cube/.3dl 文件随包带上（缺失明示，
    # 不静默换 identity）。M20.2c：工程恢复侧路径重写进包内 luts/ 目录
    # （lut_relink_map 供返回工程栈载荷改写）。
    lut_records: list[ArchiveFileRecord] = []
    lut_relink_map: dict[str, str] = {}
    lut_root = os.path.join(target_dir, "luts")
    # R02：既有 LUT 目录预检进唯一名空间（同名冲突改名，不覆盖）。
    if os.path.isdir(lut_root):
        used_names |= _preexisting_names(lut_root)
    lut_paths = lut_dependency_paths(project)
    if lut_paths:
        os.makedirs(lut_root, exist_ok=True)
        for lut_path in sorted(lut_paths):
            if not os.path.exists(lut_path):
                warnings.append(f"LUT 缺失（不随归档）：{lut_path}")
                continue
            base = _sanitize_name(
                os.path.basename(lut_path), "lut")
            dest = _unique_dest_name(base, used_names)
            src_file = os.path.join(lut_root, dest)
            # R04：持续空间检查（LUT 与媒体同一策略）。
            free = _free_bytes(lut_root)
            if free is not None and free < _source_size_bytes(lut_path) + _MIN_FREE_SLACK_BYTES:
                warnings.append(f"目标空间不足，LUT 未收集：{lut_path}")
                continue
            try:
                import shutil as _shutil
                _shutil.copy2(lut_path, src_file)
                # N11：LUT 记录对齐媒体契约——archived_path 相对归档根
                # （"/" 分隔可移植，不再用原机绝对路径）+ sha256/大小
                # （分发文件逐个 hash 校验与媒体同口径）。
                lut_hash = _sha256_of(src_file)
                lut_size = os.path.getsize(src_file)
                lut_records.append(ArchiveFileRecord(
                    asset_id=f"lut:{os.path.basename(lut_path)}",
                    display_name=os.path.basename(lut_path),
                    source_path=lut_path,
                    archived_path=os.path.join(
                        "luts", dest).replace(os.sep, "/"),
                    status="copied",
                    sha256=lut_hash,
                    file_size=lut_size,
                ))
                lut_relink_map[lut_path] = src_file
            except OSError as exc:
                warnings.append(f"LUT 收集失败：{lut_path}（{exc}）")
    total = len(candidates)
    for index, (asset_id, asset) in enumerate(candidates, start=1):
        if cancelled_fn is not None and cancelled_fn():
            warnings.append(
                f"Collect cancelled by caller after {index - 1}/{total} assets"
            )
            logger.info(
                "Collect project cancelled by caller at %d/%d assets",
                index - 1, total,
            )
            break

        try:
            src = asset.path
            if not os.path.exists(src):
                locations = usage_index.locations_for(asset_id) if usage_index else ()
                missing_usage[asset_id] = tuple(loc.format() for loc in locations)
                records.append(ArchiveFileRecord(
                    asset_id=asset_id,
                    display_name=asset.display_name,
                    source_path=src,
                    archived_path="",
                    status=STATUS_MISSING,
                ))
                continue

            ext = os.path.splitext(src)[1]
            base = _sanitize_name(
                os.path.basename(asset.display_name or src),
                fallback=f"asset_{asset_id[:8]}",
            )
            if not os.path.splitext(base)[1] and ext:
                base = base + ext
            dest_name = _unique_dest_name(base, used_names)
            dest_path = os.path.join(media_root, dest_name)

            # R04（媒体池复查）：持续空间检查——预检只是启动时点的保守
            # 估算，长任务中途磁盘被填满时在这里按文件失败（截断半成品
            # 即时清理），不再误报收集成功。
            src_size = _source_size_bytes(src)
            free = _free_bytes(media_root)
            if (src_size > 0 and free is not None
                    and free < src_size + _MIN_FREE_SLACK_BYTES):
                warnings.append(
                    f"目标空间不足（需约 {src_size} 字节），未复制：{src}")
                records.append(ArchiveFileRecord(
                    asset_id=asset_id,
                    display_name=asset.display_name,
                    source_path=src,
                    archived_path="",
                    status=STATUS_COPY_FAILED,
                ))
                continue

            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dest_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dest_path)
            except OSError as exc:
                # R02：失败只删除本任务刚创建的半成品（目标名不与既有
                # 文件重名；realpath 双保险防源/目标别名误删源）。
                _remove_partial(dest_path, src)
                logger.warning("Archive copy failed for %s: %s", src, exc)
                records.append(ArchiveFileRecord(
                    asset_id=asset_id,
                    display_name=asset.display_name,
                    source_path=src,
                    archived_path="",
                    status=STATUS_COPY_FAILED,
                ))
                continue

            # AUD-17202：哈希/大小计算与复制同入 per-file 容错。源文件在
            # exists() 检查后被删除/加锁时，降级为失败记录并继续收集，
            # 保证报告始终产出，不再中断成"无报告的半成品归档"。
            try:
                src_hash = _sha256_of(src)
                dst_hash = _sha256_of(dest_path)
                dest_size = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
            except OSError as exc:
                logger.warning("Archive verify failed for %s: %s", src, exc)
                warnings.append(
                    f"SHA-256 verify failed after copy: {src} -> {dest_path}"
                )
                records.append(ArchiveFileRecord(
                    asset_id=asset_id,
                    display_name=asset.display_name,
                    source_path=src,
                    archived_path="",
                    status=STATUS_COPY_FAILED,
                ))
                continue

            status = STATUS_COPIED if src_hash == dst_hash else STATUS_HASH_MISMATCH
            if status == STATUS_HASH_MISMATCH:
                warnings.append(f"SHA-256 mismatch after copy: {src} -> {dest_path}")

            records.append(ArchiveFileRecord(
                asset_id=asset_id,
                display_name=asset.display_name,
                source_path=src,
                # W10：可移植索引统一 "/" 分隔（Mac↔Windows 报告与
                # 证据比对不得把分隔符差异算成内容变化；relink 用
                # dest_path 的平台原生绝对路径，与此互不影响）
                archived_path=os.path.join(media_dir, dest_name).replace(
                    os.sep, "/"),
                status=status,
                sha256=src_hash,
                file_size=dest_size,
            ))
            relink_map[asset_id] = dest_path
            # N11：媒体旧路径留档（快照 payload 重写的反查源）
            _archived_media_old_paths[asset_id] = str(src)
        finally:
            if progress_callback is not None:
                progress_callback(index, total)

    archived_project = batch_relink_assets(project, relink_map) if relink_map else project

    # M20.2c：LUT 路径重写进工程载荷——归档包自洽（恢复侧不再依赖
    # 原机 LUT 路径；缺失/收集失败的 LUT 保持原路径，警告已在案）。
    # 栈级 lut_path 与 layer 级 active_lut_path/lut_path 都改写。
    if lut_relink_map:
        from dataclasses import replace as _dc_replace
        new_stacks: dict[str, Any] = {}
        stacks_changed = False
        for sid, stack in (getattr(archived_project, "color_stacks",
                                    None) or {}).items():
            new_stack = stack
            kwargs: dict[str, Any] = {}
            stack_lut = str(getattr(stack, "lut_path", None) or "")
            if stack_lut and stack_lut in lut_relink_map:
                kwargs["lut_path"] = lut_relink_map[stack_lut]
            layers = getattr(stack, "layers", None) or ()
            new_layers = None
            for i, layer in enumerate(layers):
                if not isinstance(layer, dict):
                    continue
                key = ("active_lut_path" if layer.get("active_lut_path")
                       else "lut_path" if layer.get("lut_path") else None)
                lp = layer.get(key) if key else None
                if lp and str(lp) in lut_relink_map:
                    if new_layers is None:
                        new_layers = [
                            dict(d) if isinstance(d, dict) else d
                            for d in layers]
                    new_layers[i][key] = lut_relink_map[str(lp)]
            if new_layers is not None:
                kwargs["layers"] = tuple(new_layers)
            if kwargs:
                try:
                    new_stack = _dc_replace(stack, **kwargs)
                    stacks_changed = True
                except TypeError:
                    # 非 dataclass 栈形态（测试替身/宿主自定义）：浅拷贝改写
                    try:
                        import copy as _copy
                        clone = _copy.copy(stack)
                        for k, v in kwargs.items():
                            setattr(clone, k, v)
                        new_stack = clone
                        stacks_changed = True
                    except Exception:  # noqa: BLE001 — 改写失败保持原栈
                        new_stack = stack
            new_stacks[sid] = new_stack
        if stacks_changed:
            try:
                archived_project = _dc_replace(
                    archived_project, color_stacks=new_stacks)
            except TypeError:
                logger.warning("archive: LUT path rewrite skipped "
                               "(project color_stacks not replaceable)")

    # N11 尾项：**保留快照 payload 内路径重写**——快照 full_data 携带
    # 快照时刻的完整资产表（原机绝对路径），归档搬家后恢复快照会指向
    # 不存在的源。对每个快照 payload 递归字符串替换：媒体 relink_map
    # （旧路径→包内 media/…）+ LUT lut_relink_map（→包内 luts/…）。
    # payload 为纯 dict/list JSON 形态，深度遍历按字符串精确前缀替换；
    # 改写失败保持原 payload（警告在案，不阻断归档）。
    combined_relink: dict[str, str] = dict(lut_relink_map)
    for _asset_id, _new_abs in relink_map.items():
        # relink_map: asset_id -> 绝对新路径；媒体旧路径从原资产表反查
        old_path = _archived_media_old_paths.get(str(_asset_id))
        if old_path:
            combined_relink[old_path] = str(_new_abs)
    if combined_relink and getattr(archived_project,
                                   "project_snapshots", None):
        new_table: dict[str, Any] = {}
        table_changed = False
        for snap_name, entry in (
                archived_project.project_snapshots or {}).items():
            if not isinstance(entry, dict):
                new_table[snap_name] = entry
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                new_table[snap_name] = entry
                continue
            new_payload = _rewrite_paths_in_jsonlike(
                payload, combined_relink)
            if new_payload is not payload:
                new_entry = dict(entry)
                new_entry["payload"] = new_payload
                new_table[snap_name] = new_entry
                table_changed = True
            else:
                new_table[snap_name] = entry
        if table_changed:
            try:
                archived_project = archived_project.with_project_snapshots(
                    new_table)
            except Exception:  # noqa: BLE001 — 快照表不可写回：保序警告
                logger.warning("archive: snapshot payload path rewrite "
                               "skipped (project snapshots not replaceable)")

    # M20.2：LUT 依赖记录并入归档报告（copied 集合）
    records.extend(lut_records)

    # M20.2b：字体/插件/模型身份闭包——dependencies.json 随归档写出
    # （不可复制项保留身份/版本与准确缺失诊断；字体不随包分发，许可）。
    # R02：目标已有 dependencies.json 时保留原文件（不覆盖）。
    identity_manifest: dict[str, Any] = {}
    try:
        import json as _json
        identity_manifest = build_identity_manifest(project, sequences)
        if manifest_preexisting:
            warnings.append(
                "目标目录已有 dependencies.json，保留原文件（不覆盖）")
        else:
            with open(manifest_path, "w", encoding="utf-8") as fh:
                _json.dump(identity_manifest, fh, ensure_ascii=False, indent=2)
        for font in identity_manifest.get("fonts", ()):
            if font.get("status") != "resolved":
                warnings.append(
                    f"字体未在本机解析（目标机需自备，不随归档分发）："
                    f"{font.get('family')}")
        for plugin in identity_manifest.get("plugins", ()):
            if plugin.get("status") != "ok":
                warnings.append(
                    f"插件缺失（归档只记身份）：{plugin.get('key')}"
                    f"——{plugin.get('detail', '')}")
        warnings.extend(identity_manifest.get("notes", ()))
    except Exception:  # noqa: BLE001 — 身份清单失败不阻断媒体归档
        logger.exception("identity manifest collection failed")

    report = ArchiveReport(
        archive_root=target_dir,
        records=tuple(records),
        missing_usage=missing_usage,
        skipped_unused=tuple(skipped_unused),
        warnings=tuple(warnings),
        identity_manifest=identity_manifest,
    )
    logger.info(
        "Collect project -> %s: %d copied, %d missing, %d failed",
        target_dir, len(report.copied), len(report.missing), len(report.failures),
    )
    return archived_project, report
