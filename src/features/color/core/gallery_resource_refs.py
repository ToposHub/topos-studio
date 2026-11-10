"""受管资源引用（SR04）——库相对身份，不绑定本机绝对路径。

BR07 首版把导入安装的 LUT 引用重写为**绝对路径**（``<root>/LUTs/...``）；
整个 Presets 目录移动／换机器后 LUT 文件仍在库里，grade 却还指向旧
位置。第二轮复查（SR04）明确：受管资源的长期身份是**库相对引用**，
运行时由 resolver 换算成本机路径——

- 持久化形式：``topos-lut://LUTs/<安装名>``（安装名＝``<sha8>_<原名>``，
  内容寻址、随库迁移）；
- :func:`resolve_lut_reference` 只做**换算**不做存在性兜底——缺失要
  如实暴露给既有的"缺 LUT 告警/过滤"路径，绝不猜替代文件；
- 用户自己的绝对/外部引用不属受管范围，原样通过（不猜路径）。
"""
from __future__ import annotations

import os
from typing import Optional

#: 受管引用 scheme
MANAGED_LUT_SCHEME = "topos-lut"

#: 库内受管资源目录名（与 GalleryTransferService 的安装目录一致）
DEPENDENCY_DIR_NAME = "LUTs"

_SCHEME_PREFIX = MANAGED_LUT_SCHEME + "://"
_DIR_PREFIX = DEPENDENCY_DIR_NAME + "/"

__all__ = [
    "MANAGED_LUT_SCHEME",
    "DEPENDENCY_DIR_NAME",
    "managed_lut_reference",
    "parse_managed_lut_reference",
    "resolve_lut_reference",
]


def managed_lut_reference(install_name: str) -> str:
    """安装名（``LUTs/`` 下的文件名）→ 受管引用字符串。"""
    return f"{_SCHEME_PREFIX}{_DIR_PREFIX}{install_name}"


def parse_managed_lut_reference(ref: object) -> Optional[str]:
    """受管引用 → 安装名；非受管/非法引用返回 ``None``。"""
    if not isinstance(ref, str) or not ref.startswith(_SCHEME_PREFIX):
        return None
    rest = ref[len(_SCHEME_PREFIX):]
    if not rest.startswith(_DIR_PREFIX):
        return None
    name = rest[len(_DIR_PREFIX):]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if name in {".", ".."} or ".." in name.split("/"):
        return None
    return name


def resolve_lut_reference(ref: object, library_root: str) -> Optional[str]:
    """引用 → 本机路径。

    受管引用换算为**当前库根**下的期望路径（不检查存在性——缺失如实
    暴露）；非受管引用（用户绝对路径等）原样返回。
    """
    name = parse_managed_lut_reference(ref)
    if name is None:
        if isinstance(ref, str):
            return ref
        return None
    root = os.path.realpath(os.path.abspath(library_root))
    return os.path.join(root, DEPENDENCY_DIR_NAME, name)
