"""HostHelpSources —— 把 MainWindow 持有的 5 个 registry 适配成 HelpSources Protocol。

plan §10.3 P1.4 帮助装配的"最后一公里"：MainWindow 启动时已构造好
ShortcutManager / FeatureFlags / FeatureRegistry，诊断码与能力 manifest
是模块级常量/工厂。本类只做"形状适配"——把 host 已有的 registry 翻译成
:class:`HelpSources` Protocol 要求的 dict 形状，供 ``HelpIndex.build`` 消费。

设计要点（红线）：
- **纯数据适配，不跑模型**：5 个 ``list_*`` 方法都是枚举遍历/属性读取。
- **单源失败不阻断**：每个方法独立 try/except，异常或缺失返回 None，
  :class:`HelpIndex` 据此跳过该源（不崩）。
- **不编造**：``label`` / ``suggested_action`` 只从已有的 MESSAGE_TEMPLATES /
  ``_DIAGNOSTIC_EXPLANATIONS`` 取，找不到用空串。
- **不引入 moderngl / composite.core**：本模块只 import 编辑域/共享层。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: feature_id → 中文标签（FeatureFlags 无 label 字段，补一张映射表）。
#: 缺失的 feature_id 用 feature_id 本身作 label（不编造，可读即可）。
_FEATURE_LABELS: dict[str, str] = {
    "library": "媒体库",
    "color": "调色",
    "edit": "剪辑",
    "composite": "合成",
    "deliver": "交付",
    "ai_assistant": "AI 助手",
    "media_analysis": "媒体分析",
    "cloud_analysis": "云端分析",
    "generation": "生成",
    "mcp_server": "MCP 服务",
    "ai_workflow": "AI 工作流",
}


class HostHelpSources:
    """把 MainWindow 持有的 5 个 registry 适配成 HelpSources Protocol。

    host 启动时构造（``_setup_ai_editing``），传给
    :func:`build_help_assistant`。任一 registry 缺失/异常时该源返回 None
    （:class:`HelpIndex` 据此跳过，不崩）。
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    # ------------------------------------------------------------------ #
    # 诊断码：TaepDiagnosticCode 枚举 + MESSAGE_TEMPLATES
    # ------------------------------------------------------------------ #

    def list_diagnostics(self) -> list[dict[str, Any]] | None:
        """遍历 TaepDiagnosticCode，返回 ``{code, label, message_template, suggested_action}``。

        ``label`` 取自 MESSAGE_TEMPLATES zh 文案（无独立 label 字段，复用模板首句）；
        ``suggested_action`` 取自 ``_DIAGNOSTIC_EXPLANATIONS[code].remediation[0]``（7 码有），
        其余用空串（不编造修复路径）。
        """
        try:
            from src.features.edit.ai_editing.diagnostics import (
                MESSAGE_TEMPLATES,
                TaepDiagnosticCode,
            )
        except Exception:  # noqa: BLE001
            logger.debug("diagnostics import failed", exc_info=True)
            return None

        # _DIAGNOSTIC_EXPLANATIONS 是 help_index 的模块私有表，直接读 remediation。
        try:
            from src.features.edit.ai_editing.help_index import _DIAGNOSTIC_EXPLANATIONS
        except Exception:  # noqa: BLE001
            _DIAGNOSTIC_EXPLANATIONS = {}  # type: ignore[assignment]

        out: list[dict[str, Any]] = []
        try:
            for code_enum in TaepDiagnosticCode:
                code = code_enum.value
                templates = MESSAGE_TEMPLATES.get(code_enum, {})
                zh = templates.get("zh", "")
                # label 复用 zh 模板（剥掉 {var} 占位符后是可读文案）。
                label = _strip_placeholders(zh) or code
                explanation = _DIAGNOSTIC_EXPLANATIONS.get(code, {})
                remediation = explanation.get("remediation", ())
                suggested = remediation[0] if remediation else ""
                out.append({
                    "code": code,
                    "label": label,
                    "message_template": zh,
                    "suggested_action": suggested,
                })
        except Exception:  # noqa: BLE001
            logger.debug("list_diagnostics iterate failed", exc_info=True)
            return None
        return out or None

    # ------------------------------------------------------------------ #
    # 快捷键：ShortcutManager.get_all_actions()
    # ------------------------------------------------------------------ #

    def list_shortcuts(self) -> list[dict[str, Any]] | None:
        """从 host.shortcut_manager 取全部快捷键，返回 ``{id, label, category, shortcut, feature_id}``。

        ``shortcut`` 用 ``manager.get_shortcut(id)`` 取当前活动键（含用户覆盖）。
        host 无 shortcut_manager 时返回 None。
        """
        manager = getattr(self._host, "shortcut_manager", None)
        if manager is None:
            return None
        try:
            actions = manager.get_all_actions()
        except Exception:  # noqa: BLE001
            logger.debug("list_shortcuts get_all_actions failed", exc_info=True)
            return None
        out: list[dict[str, Any]] = []
        try:
            for a in actions:
                aid = getattr(a, "id", "")
                if not aid:
                    continue
                # get_shortcut 可能抛异常（如未注册），逐个兜底。
                try:
                    shortcut = manager.get_shortcut(aid)
                except Exception:  # noqa: BLE001
                    shortcut = getattr(a, "default_shortcut", "")
                out.append({
                    "id": aid,
                    "label": getattr(a, "label", aid),
                    "category": getattr(a, "category", ""),
                    "shortcut": shortcut or getattr(a, "default_shortcut", ""),
                    "feature_id": getattr(a, "feature_id", None),
                })
        except Exception:  # noqa: BLE001
            logger.debug("list_shortcuts iterate failed", exc_info=True)
            return None
        return out or None

    # ------------------------------------------------------------------ #
    # 能力：capability_manifest.build_manifest()
    # ------------------------------------------------------------------ #

    def list_capabilities(self) -> dict[str, Any] | None:
        """调 capability_manifest.build_manifest()，返回 ``{operations, effects}``。

        effects 每项转成 ``{"effect_type": ...}`` dict（EffectCapability 是 dataclass，
        :meth:`HelpIndex._add_capabilities` 期望 dict 形状）。
        """
        try:
            from src.features.edit.ai_editing.capability_manifest import build_manifest
        except Exception:  # noqa: BLE001
            logger.debug("capability_manifest import failed", exc_info=True)
            return None
        try:
            manifest = build_manifest()
        except Exception:  # noqa: BLE001
            logger.debug("build_manifest failed", exc_info=True)
            return None
        try:
            effects = []
            for fx in getattr(manifest, "effects", ()) or ():
                et = getattr(fx, "effect_type", None)
                if et:
                    effects.append({"effect_type": et})
            return {
                "operations": list(getattr(manifest, "operations", ()) or ()),
                "effects": effects,
            }
        except Exception:  # noqa: BLE001
            logger.debug("capabilities shape failed", exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # 特性：FeatureFlags
    # ------------------------------------------------------------------ #

    def list_features(self) -> list[dict[str, Any]] | None:
        """遍历 feature_flags，返回 ``{feature_id, enabled, label}``。

        feature_flags 无统一枚举 keys，用 :data:`DEFAULT_FEATURE_FLAGS` 作权威列表。
        host 无 feature_flags 时返回 None。
        """
        flags = getattr(self._host, "feature_flags", None)
        if flags is None:
            return None
        try:
            from src.shared.feature_flags import DEFAULT_FEATURE_FLAGS
        except Exception:  # noqa: BLE001
            logger.debug("feature_flags import failed", exc_info=True)
            return None
        out: list[dict[str, Any]] = []
        try:
            for fid in DEFAULT_FEATURE_FLAGS.keys():
                try:
                    enabled = bool(flags.is_enabled(fid))
                except Exception:  # noqa: BLE001
                    enabled = False
                out.append({
                    "feature_id": fid,
                    "enabled": enabled,
                    "label": _FEATURE_LABELS.get(fid, fid),
                })
        except Exception:  # noqa: BLE001
            logger.debug("list_features iterate failed", exc_info=True)
            return None
        return out or None

    # ------------------------------------------------------------------ #
    # 面板：FeatureRegistry.get_all_pages()
    # ------------------------------------------------------------------ #

    def list_panels(self) -> list[dict[str, Any]] | None:
        """从 feature_registry.get_all_pages() 取面板。

        返回 ``{panel_id, title, category, page_ids, feature_id, enabled}``。
        PageDescriptor 只有 page_id/title/shortcut，category/feature_id/enabled
        从 FeatureModule + feature_flags 推导（缺则降级单值/None/True）。
        """
        registry = getattr(self._host, "feature_registry", None)
        if registry is None:
            return None
        try:
            pages = registry.get_all_pages()
        except Exception:  # noqa: BLE001
            logger.debug("list_panels get_all_pages failed", exc_info=True)
            return None
        # feature_flags 用于推导 enabled（缺则全 True）。
        flags = getattr(self._host, "feature_flags", None)
        out: list[dict[str, Any]] = []
        try:
            for page in pages:
                pid = getattr(page, "page_id", "")
                if not pid:
                    continue
                title = getattr(page, "title", pid)
                # category / feature_id 从同名 module 推导（page_id 形如 "edit.timeline"，
                # 取首段作 feature_id 候选；module 的 feature_id 是权威）。
                first_seg = pid.split(".", 1)[0]
                feature_id: str | None = None
                module = None
                try:
                    module = registry.get_module(first_seg)
                except Exception:  # noqa: BLE001
                    module = None
                if module is not None:
                    feature_id = getattr(module, "feature_id", first_seg)
                    category = _FEATURE_LABELS.get(feature_id or "", first_seg.title())
                else:
                    feature_id = first_seg
                    category = first_seg.title()
                # enabled：feature_id 在 flags 中查（None/查不到 → True）。
                enabled = True
                if flags is not None and feature_id is not None:
                    try:
                        enabled = bool(flags.is_enabled(feature_id))
                    except Exception:  # noqa: BLE001
                        enabled = True
                out.append({
                    "panel_id": pid,
                    "title": title,
                    "category": category,
                    "page_ids": (pid,),
                    "feature_id": feature_id,
                    "enabled": enabled,
                })
        except Exception:  # noqa: BLE001
            logger.debug("list_panels iterate failed", exc_info=True)
            return None
        return out or None


# ---------------------------------------------------------------------- #
# 内部工具
# ---------------------------------------------------------------------- #


def _strip_placeholders(text: str) -> str:
    """剥掉模板里的 ``{var}`` 占位符（如 '素材 {path} 离线' → '素材 离线'）。

    简单实现：把 ``{...}`` 整段替换为空，再 squeeze 空格。只用于生成可读 label。
    """
    if not text:
        return ""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            if depth > 0:
                depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    cleaned = "".join(out)
    # squeeze 连续空格
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned.strip()


__all__ = ["HostHelpSources"]
