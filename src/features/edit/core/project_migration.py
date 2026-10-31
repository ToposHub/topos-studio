"""P2-13 工程内容 schema 版本与迁移管线。

历史问题：编辑内容的兼容性修复散落在各 from_dict（如 Blur 的
``directional``→``motion``、音频轨 strip_eq→effect_chain），旧工程打开时
既无版本标记也无迁移记录——schema 演进只靠"碰巧能读"。

本模块建立集中契约：

- 包 manifest 携带 ``edit_content_schema_version``（保存时写当前版本；
  缺省 = 1，即"无版本标记的历史工程"）；
- ``migrate_project_payload`` 按版本链逐步迁移 full_data 载荷，返回
  迁移日志（每步一条，含 id 与说明）；
- ``preview_project_migrations`` 提供 dry-run 预检（不修改数据）；
- 较新版本工程（文件版本 > 当前）不盲迁移：记录告警条目，按尽力兼容
  打开（与专业 NLE 的前向兼容惯例一致）。

新增序列化字段的流程：bump ``EDIT_CONTENT_SCHEMA_VERSION`` 并注册一个
``ContentMigration``；from_dict 侧的容错仍保留（双保险），但兼容语义的
单一真相在迁移链里。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "EDIT_CONTENT_SCHEMA_VERSION",
    "EDIT_CONTENT_VERSION_KEY",
    "ContentMigration",
    "CONTENT_MIGRATIONS",
    "migrate_project_payload",
    "preview_project_migrations",
]

# 当前编辑内容 schema 版本（v1 = 无版本标记的历史工程）。
# 单一真相在共享层 manifest（共享层不能反向依赖 feature 模块）。
from src.shared.project.manifest import CURRENT_EDIT_CONTENT_SCHEMA_VERSION  # noqa: E402

EDIT_CONTENT_SCHEMA_VERSION = CURRENT_EDIT_CONTENT_SCHEMA_VERSION

# manifest / 载荷中的版本字段名。
EDIT_CONTENT_VERSION_KEY = "edit_content_schema_version"


@dataclass(frozen=True)
class ContentMigration:
    """一步内容迁移：把载荷从 from_version 升到 to_version。

    ``migrate`` 接收/返回整个 full_data 风格的 dict（assets / timelines /
    timeline_sequences / graphs / ...），必须幂等（重复应用无副作用）。
    """

    from_version: int
    to_version: int
    migration_id: str
    description: str
    migrate: Callable[[dict], dict]


# ------------------------------------------------------------------
# 迁移实现
# ------------------------------------------------------------------


def _ensure_assets_metadata(data: dict) -> dict:
    """v1→v2：素材 metadata 统一为 dict（缺失/None → {}）。

    历史工程/外部工具生成的 media_pool.json 可能缺 metadata 键；后续
    字段读取（多机位组、嵌套 ref、布局）假定 dict 存在。
    """
    assets = data.get("assets")
    if not isinstance(assets, dict):
        return data
    for asset in assets.values():
        if isinstance(asset, dict):
            meta = asset.get("metadata")
            if meta is None:
                asset["metadata"] = {}
            elif not isinstance(meta, dict):
                asset["metadata"] = {"_legacy_metadata": str(meta)}
    return data


def _rewrite_blur_directional(data: dict) -> dict:
    """v2→v3：Blur 的 legacy ``directional`` 类型统一为 ``motion``。

    此前该映射散落在 BlurNode/evaluator 的加载路径（静默）；集中到迁移
    链后旧工程打开即可审计。载荷任意深度递归替换，幂等。
    """

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("blur_type") == "directional":
                node["blur_type"] = "motion"
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(data)
    return data


def _ensure_sampling_phase(data: dict) -> dict:
    """v3→v4: old integer source windows retain a zero sampling phase.

    Existing clip/source windows and unknown payloads stay unchanged; only
    absent defaults are filled. The lost phase of an already-edited v3 clip
    cannot be inferred. New window edits preserve it explicitly from v4 on.
    """
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if {"asset_id", "source_duration", "timeline_duration", "source_start"} <= node.keys():
                node.setdefault("source_frame_phase_num", 0)
                node.setdefault("source_frame_phase_den", 1)
                speed = node.get("speed")
                if isinstance(speed, dict):
                    speed.setdefault("curve_time_offset", None)
            for child in node.values():
                walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
    walk(data)
    return data


CONTENT_MIGRATIONS: tuple[ContentMigration, ...] = (
    ContentMigration(
        from_version=1,
        to_version=2,
        migration_id="assets_metadata_dict",
        description="素材 metadata 统一为 dict（缺失/None → {}）",
        migrate=_ensure_assets_metadata,
    ),
    ContentMigration(
        from_version=2,
        to_version=3,
        migration_id="blur_directional_to_motion",
        description="Blur legacy 类型 directional → motion（集中化原加载期静默映射）",
        migrate=_rewrite_blur_directional,
    ),
    ContentMigration(
        from_version=3,
        to_version=4,
        migration_id="clip_sampling_phase",
        description="片段保留有理数源帧相位及变速曲线时间原点；旧窗口缺省零相位",
        migrate=_ensure_sampling_phase,
    ),
)


# ------------------------------------------------------------------
# 管线
# ------------------------------------------------------------------


def _resolve_version(data: dict) -> int:
    version = data.get(EDIT_CONTENT_VERSION_KEY, 1)
    try:
        return int(version)
    except (TypeError, ValueError):
        return 1


def _applicable_steps(from_version: int) -> list[ContentMigration]:
    """按版本链排序的待应用迁移（from_version 之后的每步一次）。"""
    steps: list[ContentMigration] = []
    version = from_version
    for _ in range(len(CONTENT_MIGRATIONS) + 1):
        if version >= EDIT_CONTENT_SCHEMA_VERSION:
            break
        step = next(
            (m for m in CONTENT_MIGRATIONS if m.from_version == version), None)
        if step is None:
            # 链断裂：注册表缺该版本的迁移步骤，保守停止。
            logger.warning(
                "content migration chain gap at v%d; stopping", version)
            break
        steps.append(step)
        version = step.to_version
    return steps


def preview_project_migrations(data: dict) -> list[str]:
    """dry-run：报告将应用的迁移（不修改数据）。

    较新版本工程返回告警条目（不迁移）。
    """
    version = _resolve_version(data)
    if version > EDIT_CONTENT_SCHEMA_VERSION:
        return [
            f"工程由较新版本创建（内容 schema v{version}，当前 "
            f"v{EDIT_CONTENT_SCHEMA_VERSION}）；按尽力兼容打开，未知字段可能丢失"
        ]
    return [
        f"v{m.from_version}→v{m.to_version} [{m.migration_id}] {m.description}"
        for m in _applicable_steps(version)
    ]


def migrate_project_payload(data: dict) -> tuple[dict, list[str]]:
    """按版本链迁移载荷，返回 (新载荷, 迁移日志)。

    - 无版本标记（v1）的旧工程走完整链；
    - 已是当前版本 → 原样返回、日志为空；
    - 较新版本 → 不迁移，日志含前向兼容告警；
    - 单步迁移抛异常时记录失败并继续（迁移失败不阻断打开）。
    """
    if not isinstance(data, dict):
        return data, []

    version = _resolve_version(data)
    if version > EDIT_CONTENT_SCHEMA_VERSION:
        return data, preview_project_migrations(data)

    steps = _applicable_steps(version)
    if not steps and data.get(EDIT_CONTENT_VERSION_KEY) == EDIT_CONTENT_SCHEMA_VERSION:
        return data, []  # 已是当前版本：零开销原样返回

    log: list[str] = []
    migrated = data
    reached = version
    for step in steps:
        try:
            migrated = step.migrate(migrated)
            reached = step.to_version
            log.append(
                f"v{step.from_version}→v{step.to_version} "
                f"[{step.migration_id}] {step.description}")
        except Exception as exc:  # noqa: BLE001 — 单步失败不阻断打开
            log.append(
                f"v{step.from_version}→v{step.to_version} "
                f"[{step.migration_id}] 迁移失败：{exc}")
            break

    if isinstance(migrated, dict):
        # 如实记录实际到达的版本（链断/失败时不谎报当前版本，
        # 下次打开可重试剩余步骤）。
        migrated = dict(migrated)
        migrated[EDIT_CONTENT_VERSION_KEY] = reached
    return migrated, log
