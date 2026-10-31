"""DESK-01A · .topos 包契约冻结规范（手机⇄桌面交接的唯一事实来源）。

背景
----
Topos Edit 移动版与桌面版需要交换**单个自包含 ``.topos`` 包**并双向继续
精剪。此前包结构的"规范"散落在三处且互相矛盾：

- ``schema_v1_spec.py``：v1 时代的手写 JSON Schema，manifest 声明
  ``additionalProperties: False``、project_mode 枚举仍含已废弃的
  ``single_asset``——与生产 serializer 实际写出的字段早已不一致；
- ``manifest.py`` / ``topos_project.py`` / 各 feature serializer：真实
  字段集合，但没有可被另一端（Dart/移动）引用的声明式描述；
- 各测试的隐式预期。

本模块把契约**集中冻结**为一份声明式、可哈希的规范：

1. **版本常量**：包 schema / 编辑内容 schema / 资源索引 schema 及各自
   的最小可读版本。单一数值真值仍在 ``manifest.py``（避免双处漂移），
   这里只 re-export 并补充资源协议版本。
2. **字段 schema**：每个包内 JSON 条目的结构描述（JSON Schema 子集：
   type/required/properties/enum/items）。未知键**一律放行**——前向
   兼容（DESK-04A 未知字段保留）是契约的一部分，故本规范永不使用
   ``additionalProperties: false``。
3. **结构校验器**：``validate_payload`` 用自带的最小实现做结构校验
   （生产 venv 不含 jsonschema，``schema_v1_spec.validate_json`` 在缺库
   时直接 fail-closed，不能作为交接契约的运行依赖）。移动端（Dart）
   只需实现同一子集即可得到相同判定。
4. **规范哈希**：``spec_digest`` 对"身份块 + 全部字段 schema"的规范
   JSON（键排序、无空白）取 SHA-256。双端各自计算并比对哈希，即可
   确认引用的是**同一份**字段契约——不依赖口头约定"我们都用 v3"。

版本冻结（2026-10-25，DESK-01A）
--------------------------------

- 包 schema：**v3**（``TOPOS_SCHEMA_VERSION``）
- 编辑内容 schema：**v4**（``CURRENT_EDIT_CONTENT_SCHEMA_VERSION``，
  迁移链见 ``src/features/edit/core/project_migration.py``）
- 资源索引 schema：**v1**（本模块新冻结；``resources/resources.json``
  的落地实现见 DESK-01B ``resource_resolver.py``）
- 最小可读：包 schema ≥1（v1/v2 打开时自动升版，见
  ``ToposManifest.from_dict``）；编辑内容 schema ≥1（v1 = 无版本标记
  的历史工程，加载侧走完整迁移链）
- 更高版本（未来写入者）：**允许按尽力兼容打开，但保存时绝不降级**
  （``ToposProjectIO.save`` 已有 never-downgrade 守卫；DESK-01D 增加
  显式能力预检）。本模块的 ``classify_package_versions`` 供加载入口
  做该判定。

契约变更纪律
------------
任何字段层面的增删改必须：① bump 对应 schema 版本并注册迁移（编辑
内容走 ``CONTENT_MIGRATIONS`` 链；资源索引走自身 minor 演进）；② 同步
更新本模块字段 schema；③ 更新 ``tests/project/test_package_contract.py``
中钉住的 ``spec_digest`` 金丝雀值与黄金包夹具。规范哈希变化 = 契约变化，
评审时以哈希 diff 为准。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.shared.project.manifest import (
    TOPOS_SCHEMA_VERSION,
    CURRENT_EDIT_CONTENT_SCHEMA_VERSION,
)

__all__ = [
    "PACKAGE_SCHEMA_VERSION",
    "EDIT_CONTENT_SCHEMA_VERSION",
    "RESOURCE_INDEX_SCHEMA_VERSION",
    "MIN_READ_PACKAGE_SCHEMA_VERSION",
    "MIN_READ_EDIT_CONTENT_SCHEMA_VERSION",
    "MIN_READ_RESOURCE_INDEX_SCHEMA_VERSION",
    "RESOURCE_URI_SCHEME",
    "PACKAGE_FIELD_SCHEMAS",
    "SPEC_IDENTITY",
    "validate_payload",
    "spec_digest",
    "classify_package_versions",
    "is_valid_resource_uri",
    "canonical_json",
]


# ================================================================
# 版本冻结（单一数值真值在 manifest.py；此处为契约面 re-export）
# ================================================================

#: 包传输 schema 版本（manifest.json 的 schema_version）。
PACKAGE_SCHEMA_VERSION: int = TOPOS_SCHEMA_VERSION

#: 编辑内容载荷 schema 版本（manifest 的 edit_content_schema_version）。
EDIT_CONTENT_SCHEMA_VERSION: int = CURRENT_EDIT_CONTENT_SCHEMA_VERSION

#: 资源索引（resources/resources.json）schema 版本。
#: v1 随 DESK-01A 冻结、DESK-01B 落地：内容寻址（SHA-256）+ 逻辑 URI。
RESOURCE_INDEX_SCHEMA_VERSION: int = 1

#: 各维度的最小可读版本：低于该值拒绝打开（而非静默误读）。
MIN_READ_PACKAGE_SCHEMA_VERSION: int = 1
MIN_READ_EDIT_CONTENT_SCHEMA_VERSION: int = 1
MIN_READ_RESOURCE_INDEX_SCHEMA_VERSION: int = 1

#: 逻辑资源 URI 方案。两种合法形式：
#:   topos-res://<resource_id>          按资源 ID 引用（包内稳定标识）
#:   topos-res://sha256-<64 位小写 hex> 按内容身份引用（跨包去重/校验）
RESOURCE_URI_SCHEME: str = "topos-res"


def is_valid_resource_uri(uri: str) -> bool:
    """校验逻辑资源 URI 的文法（仅文法，不查存在性）。"""
    if not isinstance(uri, str) or not uri.startswith(f"{RESOURCE_URI_SCHEME}://"):
        return False
    body = uri[len(RESOURCE_URI_SCHEME) + 3:]
    if not body:
        return False
    if body.startswith("sha256-"):
        hex_part = body[len("sha256-"):]
        return len(hex_part) == 64 and all(c in "0123456789abcdef" for c in hex_part)
    # resource_id：非空、不含路径分隔符与空白（包内标识，不承载路径语义）
    return all(c not in "/\\\t\r\n " for c in body)


# ================================================================
# 字段 schema（JSON Schema 子集；未知键一律放行）
# ================================================================
# 子集说明：type / required / properties / items / enum。
#   - "integer" 匹配 int（不含 bool）；"number" 匹配 int/float（不含 bool）
#   - "string" 匹配 str；"boolean" 匹配 bool；"null" 匹配 None
#   - 未声明的属性不校验（前向兼容：未来写入者的新键必须能穿过旧读取者）
#   - enum 值做精确成员判定

_ASSET_MEDIA_TYPES = {
    "image", "video", "audio", "subtitle", "image_sequence",
    "comp_clip", "nested_sequence", "adjustment", "solid", "motion_graphic",
}

_PROJECT_MODES = ["standalone_image", "edit_project", "composite_shot", "mixed_project"]

MANIFEST_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["format", "schema_version", "project_id", "project_mode"],
    "properties": {
        "format": {"type": "string", "enum": ["topos_project_archive"]},
        "schema_version": {"type": "integer"},
        "created_by": {"type": "string"},
        "project_id": {"type": "string"},
        "project_mode": {"type": "string", "enum": _PROJECT_MODES},
        "features": {"type": "array", "items": {"type": "string"}},
        "primary_asset_id": {"type": ["string", "null"]},
        "active_timeline_id": {"type": ["string", "null"]},
        "created_at": {"type": "number"},
        "modified_at": {"type": "number"},
        "last_saved_by": {"type": "string"},
        "required_features": {"type": "array", "items": {"type": "string"}},
        "optional_features": {"type": "array", "items": {"type": "string"}},
        "present_modules": {"type": "array", "items": {"type": "string"}},
        "edit_content_schema_version": {"type": "integer"},
    },
}

PROJECT_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project_id", "name", "project_mode"],
    "properties": {
        "project_id": {"type": "string"},
        "name": {"type": "string"},
        "project_mode": {"type": "string", "enum": _PROJECT_MODES},
        "assets": {
            "type": "object",
            "value_schema": {
                "type": "object",
                "required": ["asset_id", "path", "media_type", "display_name"],
                "properties": {
                    "asset_id": {"type": "string"},
                    "path": {"type": "string"},
                    "path_relative": {"type": "string"},
                    "path_original": {"type": "string"},
                    "media_type": {"type": "string", "enum": sorted(_ASSET_MEDIA_TYPES)},
                    "display_name": {"type": "string"},
                    "is_offline": {"type": "boolean"},
                },
            },
        },
        "bins": {
            "type": "object",
            "value_schema": {
                "type": "object",
                "required": ["bin_id", "name"],
                "properties": {
                    "bin_id": {"type": "string"},
                    "name": {"type": "string"},
                    "parent_id": {"type": ["string", "null"]},
                    "asset_count": {"type": "integer"},
                },
            },
        },
        "timelines": {
            "type": "object",
            "value_schema": {
                "type": "object",
                "required": ["timeline_id", "name"],
                "properties": {
                    "timeline_id": {"type": "string"},
                    "name": {"type": "string"},
                    "fps": {"type": "number"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
            },
        },
        "color_stacks": {
            "type": "object",
            "value_schema": {
                "type": "object",
                "required": ["stack_id"],
                "properties": {
                    "stack_id": {"type": "string"},
                    "asset_id": {"type": ["string", "null"]},
                    "name": {"type": "string"},
                    "scope": {"type": "string"},
                },
            },
        },
        "compositions": {
            "type": "object",
            "value_schema": {
                "type": "object",
                "required": ["composition_id"],
                "properties": {
                    "composition_id": {"type": "string"},
                    "name": {"type": "string"},
                    "source_kind": {"type": "string"},
                    "asset_id": {"type": ["string", "null"]},
                    "timeline_id": {"type": ["string", "null"]},
                    "clip_id": {"type": ["string", "null"]},
                },
            },
        },
        "deliver_presets": {
            "type": "object",
            "value_schema": {
                "type": "object",
                "required": ["preset_id", "name"],
                "properties": {
                    "preset_id": {"type": "string"},
                    "name": {"type": "string"},
                    "format_type": {"type": "string"},
                },
            },
        },
        "ui_state": {"type": "object"},
        "settings": {"type": "object"},
        "primary_asset_id": {"type": ["string", "null"]},
        "primary_color_stack_id": {"type": ["string", "null"]},
        "active_timeline_id": {"type": ["string", "null"]},
        "active_composition_id": {"type": ["string", "null"]},
        "created_at": {"type": "number"},
        "modified_at": {"type": "number"},
        "revision": {"type": "integer"},
    },
}

MEDIA_POOL_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "value_schema": {
        "type": "object",
        "required": ["asset_id", "path", "media_type", "display_name"],
        "properties": {
            "asset_id": {"type": "string"},
            "path": {"type": "string"},
            "path_relative": {"type": "string"},
            "path_original": {"type": "string"},
            "media_type": {"type": "string", "enum": sorted(_ASSET_MEDIA_TYPES)},
            "display_name": {"type": "string"},
            "metadata": {"type": "object"},
            "thumbnail_key": {"type": ["string", "null"]},
            "proxy_path": {"type": ["string", "null"]},
            "proxy_status": {"type": "string"},
            "is_offline": {"type": "boolean"},
            "color_metadata": {"type": ["object", "null"]},
            "created_at": {"type": "number"},
            "reel": {"type": "string"},
            "scene": {"type": "string"},
            "shot": {"type": "string"},
            "take": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "rating": {"type": "integer"},
            "flag": {"type": "string"},
            "color_label": {"type": "string"},
            "comment": {"type": "string"},
        },
    },
}

BINS_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "value_schema": {
        "type": "object",
        "required": ["bin_id", "name"],
        "properties": {
            "bin_id": {"type": "string"},
            "name": {"type": "string"},
            "parent_id": {"type": ["string", "null"]},
            "asset_ids": {"type": "array", "items": {"type": "string"}},
            "timeline_ids": {"type": "array", "items": {"type": "string"}},
        },
    },
}

EDIT_TIMELINES_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "value_schema": {
        "type": "object",
        "required": ["timeline_id", "name"],
        "properties": {
            "timeline_id": {"type": "string"},
            "name": {"type": "string"},
            "fps": {"type": "number"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "sample_rate": {"type": "integer"},
            "format_settings": {"type": "object"},
            "created_at": {"type": "number"},
        },
    },
}

#: edit/timeline_sequences.json —— TimelineSequence.to_dict。
#: fps 为有理数字符串 ``"<num>/<den>"``（Rational.from_string 互逆），
#: 这是跨端时间精度的根：**不允许**退化为 float 或毫秒。
TIMELINE_SEQUENCES_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "value_schema": {
        "type": "object",
        "required": [
            "timeline_id", "name", "fps", "width", "height",
            "sample_rate",
        ],
        "properties": {
            "timeline_id": {"type": "string"},
            "name": {"type": "string"},
            "fps": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "sample_rate": {"type": "integer"},
            "format_settings": {"type": "object"},
            "video_tracks": {"type": "array", "items": {"type": "object"}},
            "audio_tracks": {"type": "array", "items": {"type": "object"}},
            "subtitle_tracks": {"type": "array", "items": {"type": "object"}},
            "markers": {"type": "array", "items": {"type": "object"}},
            "revision": {"type": "integer"},
        },
    },
}

COLOR_STACKS_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "value_schema": {
        "type": "object",
        "required": ["stack_id"],
        "properties": {
            "stack_id": {"type": "string"},
            "asset_id": {"type": ["string", "null"]},
            "name": {"type": "string"},
        },
    },
}

#: resources/resources.json —— DESK-01B 资源索引（随本 ADR 冻结为 v1）。
#: 内容寻址：每个资源按 SHA-256 登记；逻辑 URI 见 ``is_valid_resource_uri``。
#: ``archive_path`` 必须位于 ``resources/`` 前缀下（实现侧校验，路径穿越
#: 拒绝）；``roles`` 描述资源在工程中的用途（source_media/font/lut/
#: texture/mask/generated_audio/generated_video/thumbnail_proxy/other）。
RESOURCE_INDEX_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "resources"],
    "properties": {
        "schema_version": {"type": "integer", "enum": [RESOURCE_INDEX_SCHEMA_VERSION]},
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "resource_id", "kind", "archive_path", "size_bytes", "sha256",
                ],
                "properties": {
                    "resource_id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "video", "audio", "image", "font", "lut",
                            "texture", "mask", "generated", "other",
                        ],
                    },
                    "archive_path": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                    "sha256": {"type": "string"},
                    "original_filename": {"type": "string"},
                    "mime": {"type": "string"},
                    "roles": {"type": "array", "items": {"type": "string"}},
                    "asset_id": {"type": ["string", "null"]},
                    "metadata": {"type": "object"},
                },
            },
        },
    },
}

CACHE_MANIFEST_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "baked_files": {"type": "object"},
        "ai_result_files": {"type": "object"},
        "thumbnail_files": {"type": "object"},
        "proxy_files": {"type": "object"},
        "total_size_bytes": {"type": "integer"},
        "created_at": {"type": "number"},
    },
}

#: 条目名 → 字段 schema。条目名即包内归档路径（与
#: ``payload_cache.KNOWN_JSON_FILES`` 的键一致，便于按包遍历校验）。
PACKAGE_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "manifest.json": MANIFEST_FIELD_SCHEMA,
    "project.json": PROJECT_FIELD_SCHEMA,
    "edit/media_pool.json": MEDIA_POOL_FIELD_SCHEMA,
    "edit/bins.json": BINS_FIELD_SCHEMA,
    "edit/timelines.json": EDIT_TIMELINES_FIELD_SCHEMA,
    "edit/timeline_sequences.json": TIMELINE_SEQUENCES_FIELD_SCHEMA,
    "color/color_stacks.json": COLOR_STACKS_FIELD_SCHEMA,
    "resources/resources.json": RESOURCE_INDEX_FIELD_SCHEMA,
    "cache_manifest.json": CACHE_MANIFEST_FIELD_SCHEMA,
}


# ================================================================
# 规范 JSON 与哈希
# ================================================================

def canonical_json(data: Any) -> str:
    """规范 JSON 文本：键排序、无空白、UTF-8、ensure_ascii=False。

    双端哈希比对的前提是字节级一致的序列化；Dart 侧按同规则实现
    （jsonEncode 前手动排序键、去除填充空白）。
    """
    return json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )


#: 规范身份块：参与哈希的全部"契约是什么"内容。
#: 注意：这里**不含**黄金样例本体（黄金包由 tests 负责生成与钉住），
#: 也不含各条目内 JSON 的实际数据——只冻结结构与版本。
SPEC_IDENTITY: dict[str, Any] = {
    "spec_id": "topos.package-spec",
    "spec_revision": 1,
    "package_schema_version": PACKAGE_SCHEMA_VERSION,
    "edit_content_schema_version": EDIT_CONTENT_SCHEMA_VERSION,
    "resource_index_schema_version": RESOURCE_INDEX_SCHEMA_VERSION,
    "min_read": {
        "package_schema_version": MIN_READ_PACKAGE_SCHEMA_VERSION,
        "edit_content_schema_version": MIN_READ_EDIT_CONTENT_SCHEMA_VERSION,
        "resource_index_schema_version": MIN_READ_RESOURCE_INDEX_SCHEMA_VERSION,
    },
    "resource_uri_scheme": RESOURCE_URI_SCHEME,
    "field_schemas": PACKAGE_FIELD_SCHEMAS,
}


def spec_digest() -> str:
    """规范哈希：对 SPEC_IDENTITY 规范 JSON 取 SHA-256（hex，64 位）。

    双端各自计算并与对端比对，即确认字段契约一致。修改任何字段
    schema / 版本 / URI 文法都会改变该值——测试
    （tests/project/test_package_contract.py）钉住该值作为契约金丝雀。
    """
    return hashlib.sha256(canonical_json(SPEC_IDENTITY).encode("utf-8")).hexdigest()


# ================================================================
# 最小结构校验器（不依赖 jsonschema）
# ================================================================

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _check_type(value: Any, expected: str | list[str]) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    return any(_TYPE_CHECKS[n](value) for n in names if n in _TYPE_CHECKS)


def _validate(node: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _check_type(node, expected_type):
        errors.append(
            f"{path}: 期望 type={expected_type}，实际 "
            f"{type(node).__name__}"
        )
        return

    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path}: 值 {node!r} 不在枚举 {schema['enum']!r} 内")

    if isinstance(node, dict):
        for key in schema.get("required", ()):
            if key not in node:
                errors.append(f"{path}: 缺少必填键 {key!r}")
        props = schema.get("properties", {})
        value_schema = schema.get("value_schema")
        for key, child in node.items():
            child_path = f"{path}.{key}"
            if key in props:
                _validate(child, props[key], child_path, errors)
            elif value_schema is not None:
                _validate(child, value_schema, child_path, errors)
            # 其余键：未知键放行（前向兼容）
    elif isinstance(node, list) and "items" in schema:
        for i, item in enumerate(node):
            _validate(item, schema["items"], f"{path}[{i}]", errors)


def validate_payload(entry_name: str, data: Any) -> list[str]:
    """按冻结字段 schema 校验一个包内 JSON 条目。

    Args:
        entry_name: 包内条目路径（如 ``"manifest.json"``、
            ``"edit/timeline_sequences.json"``）。
        data: 解析后的 JSON 数据。

    Returns:
        错误列表（空 = 通过）。未知条目名返回单条错误——冻结的条目
        集合之外的内容不由本规范判定（payload_cache 原样保留）。
    """
    schema = PACKAGE_FIELD_SCHEMAS.get(entry_name)
    if schema is None:
        return [f"{entry_name}: 不在冻结的包条目集合内"]
    errors: list[str] = []
    _validate(data, schema, entry_name, errors)
    return errors


# ================================================================
# 版本分级（DESK-01D 加载预检使用）
# ================================================================

@dataclass(frozen=True)
class PackageVersionStatus:
    """一个维度（包/内容/资源索引）的版本判定结果。"""

    dimension: str
    file_version: int
    current_version: int
    min_read: int
    #: "current"  = 当前版本，完整读写
    #: "legacy"   = 旧于当前但不低于最小可读：迁移链升级后读写
    #: "future"   = 高于当前：尽力兼容打开，保存不降级（never-downgrade）
    #: "unreadable" = 低于最小可读：拒绝打开（受控失败）
    status: str

    @property
    def is_writable_without_downgrade(self) -> bool:
        return self.status in ("current", "legacy")


def classify_package_versions(
    manifest_data: dict[str, Any] | None,
    *,
    resource_index_version: int | None = None,
) -> dict[str, PackageVersionStatus]:
    """对包 manifest 声明的各版本维度做分级判定。

    Args:
        manifest_data: 包内 manifest.json 解析结果（None 视为损坏包，
            由调用方先行拒绝）。
        resource_index_version: resources/resources.json 的
            schema_version（无资源索引的旧包传 None = 不适用，
            该维度报告为 legacy-0：加载后按需建立索引）。

    Returns:
        {dimension: status}，dimension ∈ package/edit_content/resource_index。
    """
    result: dict[str, PackageVersionStatus] = {}

    def _classify(
        dimension: str, file_version: int, current: int, min_read: int,
    ) -> PackageVersionStatus:
        if file_version < min_read:
            status = "unreadable"
        elif file_version > current:
            status = "future"
        elif file_version == current:
            status = "current"
        else:
            status = "legacy"
        return PackageVersionStatus(
            dimension=dimension,
            file_version=file_version,
            current_version=current,
            min_read=min_read,
            status=status,
        )

    data = manifest_data or {}
    package_version = data.get("schema_version", 1)
    if not isinstance(package_version, int) or isinstance(package_version, bool):
        package_version = 1
    result["package"] = _classify(
        "package", package_version, PACKAGE_SCHEMA_VERSION,
        MIN_READ_PACKAGE_SCHEMA_VERSION,
    )

    edit_version = data.get("edit_content_schema_version", 1)
    if not isinstance(edit_version, int) or isinstance(edit_version, bool):
        edit_version = 1
    result["edit_content"] = _classify(
        "edit_content", edit_version, EDIT_CONTENT_SCHEMA_VERSION,
        MIN_READ_EDIT_CONTENT_SCHEMA_VERSION,
    )

    if resource_index_version is None:
        # 旧包无资源索引：不是错误——资源解析层按需回退外部路径。
        result["resource_index"] = PackageVersionStatus(
            dimension="resource_index",
            file_version=0,
            current_version=RESOURCE_INDEX_SCHEMA_VERSION,
            min_read=MIN_READ_RESOURCE_INDEX_SCHEMA_VERSION,
            status="legacy",
        )
    else:
        result["resource_index"] = _classify(
            "resource_index", resource_index_version,
            RESOURCE_INDEX_SCHEMA_VERSION, MIN_READ_RESOURCE_INDEX_SCHEMA_VERSION,
        )

    return result
