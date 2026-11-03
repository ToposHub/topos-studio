"""R01: authoritative read-only projection of effect resources for Color UI.

权威来源与投影契约（docs/color_resources_backend_implementation_plan.md R01）：

- **PluginManager**（``.tglsl`` DCTL / ``.tai`` AI 插件注册表）与
  **EffectCatalog**（Composite 内建效果统一目录）是唯一权威。本 presenter
  只做投影，**自身绝不扫描目录、不编译 shader、不加载模型**；磁盘扫描
  由应用启动路径（menu_toolbar auto_scan / EffectCatalog 懒构建）或
  :meth:`EffectsResourceCatalog.warm_background` 的后台线程完成。
- 资源 ID 规则：插件条目 id = ``plugin_id``（文件名去扩展名，跨会话稳定，
  与 legacy Layer ``plugins`` 载荷一致，收藏/草稿身份不漂移）；内建效果
  id = ``effect:<effect_type>``。**同名不同内容**（两个来源共用 plugin_id
  但 content hash 不同）确定性消歧为 ``<plugin_id>@<hash8>``；同内容多路
  径去重为单条目。分类/名称翻译只改显示字段，永不参与 ID。
- 能力状态（fail-visible）：DCTL 缺 shader、AI 模型未安装、内建效果尚未
  接入 Color 节点后端 → ``available=False`` + 机器可读 ``disabled_reason``
  码；离线 bake 能力（AI 插件）以 ``bake_capable`` 标注，不冒充实时。
- 线程模型：投影持有 RLock，可在后台线程 refresh；``catalog_changed``
  信号由 Qt 自动跨线程排队到接收方线程。
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

# DCTL 条目在 Color 图/legacy Layer 中的 id 就是 plugin_id（不带前缀）；
# 内建 Composite 效果走独立前缀，避免与插件文件名碰撞。
_BUILTIN_ID_PREFIX = "effect:"

#: 参数 ParamDef.ptype → 浏览器控件 kind（effects_browser_panel 契约）。
_PARAMDEF_KIND_MAP = {
    "float": "slider",
    "int": "int",
    "bool": "checkbox",
    "option": "select",
    "color": "color",
}


def _plugin_schema_hash(params: Sequence[Mapping[str, Any]]) -> str:
    """参数 schema 指纹（canonical JSON，与 plugin_identity 同风格）。

    dict 形态的插件参数（PluginManager 解析产物）没有 ParamDef 属性面，
    这里按相同规则（排序键 + ensure_ascii + default=str）计算，检测
    增删参/改名/改域的 schema 漂移。未知参数类型原样保留参与哈希。
    """
    canonical = []
    for param in params:
        if not isinstance(param, Mapping):
            continue
        canonical.append({
            "var_name": param.get("var_name", ""),
            "type": param.get("type", ""),
            "default": param.get("default"),
            "min": param.get("min"),
            "max": param.get("max"),
            "options": list(param.get("options") or []),
        })
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _paramdef_to_param(pdef: Any) -> dict[str, Any]:
    """EffectSpec 的 ParamDef → 浏览器参数契约（slider/checkbox/select/color）。

    保持 PluginManager 的取值约定：checkbox default 为 0/1，select default
    为选项下标，slider 带 min/max。未知 ptype 原样透传（浏览器显示
    "Parameter UI unavailable"，不静默丢参数）。
    """
    name = getattr(pdef, "name", "") or ""
    ptype = str(getattr(pdef, "ptype", "") or "")
    default = getattr(pdef, "default", None)
    param: dict[str, Any] = {
        "type": _PARAMDEF_KIND_MAP.get(ptype, ptype),
        "var_name": name,
        "label": name,
        "default": default,
    }
    kind = param["type"]
    if kind in ("slider", "int", "float"):
        low = getattr(pdef, "min_val", None)
        high = getattr(pdef, "max_val", None)
        param["min"] = float(low) if low is not None else 0.0
        param["max"] = float(high) if high is not None else 1.0
    elif kind == "select":
        labels = [str(x) for x in (getattr(pdef, "labels", None) or [])]
        param["options"] = labels
        try:
            param["default"] = labels.index(default)
        except (ValueError, TypeError):
            param["default"] = 0
    elif kind == "checkbox":
        param["default"] = 1 if default else 0
    elif kind == "color":
        rgb = default if isinstance(default, (list, tuple)) else (1.0, 1.0, 1.0)
        param["default"] = [float(v) for v in tuple(rgb)[:3]]
    return param


def _default_ai_model_probe(model_path: str) -> bool:
    """默认 AI 模型探测：相对路径按仓库根解析，绝对路径直接判定。"""
    if not model_path:
        return False
    path = Path(model_path).expanduser()
    if not path.is_absolute():
        root = Path(__file__).resolve().parents[3]
        path = root / path
    return path.is_file()


class EffectsResourceCatalog(QObject):
    """向 Color UI 投影不可变效果资源条目（正式 catalog presenter）。"""

    catalog_changed = pyqtSignal()

    def __init__(self, *, plugin_manager: Any = None,
                 effect_catalog: Any = None,
                 ai_model_probe: Callable[[str], bool] | None = None,
                 include_builtins: bool = True) -> None:
        super().__init__()
        self._plugin_manager = plugin_manager
        self._effect_catalog = effect_catalog
        self._ai_model_probe = ai_model_probe or _default_ai_model_probe
        self._include_builtins = bool(include_builtins)
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._built = False

    # ── 权威引用（懒解析，避免模块导入期代价） ─────────────────────
    def _pm(self) -> Any:
        if self._plugin_manager is None:
            from src.features.color.core.plugin_manager import get_plugin_manager
            self._plugin_manager = get_plugin_manager()
        return self._plugin_manager

    def _ec(self) -> Any:
        if self._effect_catalog is None:
            from src.features.shared.effects.effect_catalog import get_effect_catalog
            self._effect_catalog = get_effect_catalog()
        return self._effect_catalog

    def _ec_if_loaded(self) -> Any:
        """effect_catalog 已被（其他消费者/后台预热）导入时才返回实例。

        effect_catalog 顶层导入会拉起 Composite 节点链（实测 ~0.8s）。
        GUI 线程的首次 entries() 不得支付这笔导入费——未预热时内建条目
        暂不投影，等 warm_background 完成后经 catalog_changed 重投影。
        """
        import sys
        if self._effect_catalog is not None:
            return self._effect_catalog
        if "src.features.shared.effects.effect_catalog" not in sys.modules:
            return None
        return self._ec()

    # ── 投影 ───────────────────────────────────────────────────────
    def refresh(self) -> None:
        """从内存权威重建投影；不扫描目录、不编译、不加载模型。

        AI 模型可用性探测（stat 级别）在这里执行——生产装配中 refresh
        由 :meth:`warm_background` 的后台线程驱动，GUI 线程只读缓存。
        """
        entries = self._project()
        with self._lock:
            self._entries = entries
            self._built = True
        self.catalog_changed.emit()

    def entries(self) -> list[dict[str, Any]]:
        """当前投影（深拷贝，调用方可安全修改）。首次调用懒投影一次。"""
        with self._lock:
            if not self._built:
                self._entries = self._project()
                self._built = True
            return deepcopy(self._entries)

    def warm_background(self) -> threading.Thread:
        """后台预热：触发 EffectCatalog 冷构建（唯一磁盘扫描步骤）后 refresh。

        生产装配在 GUI 线程之外调用；线程为 daemon，不阻塞退出。
        """
        thread = threading.Thread(target=self._warm, name="effects-catalog-warm",
                                   daemon=True)
        thread.start()
        return thread

    def _warm(self) -> None:
        try:
            if self._include_builtins:
                # EffectCatalog 懒构建会扫描插件目录并解析全部 .tglsl；
                # 这一步留在后台线程（R01: 扫描不占 UI 线程）。
                self._ec().get_all_specs()
            self.refresh()
        except Exception:  # noqa: BLE001 — 预热失败不得拖垮启动
            import logging
            logging.getLogger(__name__).exception(
                "Effects catalog background warm-up failed")

    # ── 内部投影实现 ───────────────────────────────────────────────
    def _project(self) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        projected.extend(self._project_plugins())
        if self._include_builtins:
            projected.extend(self._project_builtins())
        return self._dedupe(projected)

    def _project_plugins(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        plugins = self._pm().get_all_plugins()
        items = sorted(plugins.items()) if isinstance(plugins, Mapping) else \
            [(str(p.get("plugin_id") or ""), p) for p in plugins
             if isinstance(p, Mapping)]
        for _key, metadata in items:
            if not isinstance(metadata, Mapping):
                continue
            plugin_id = str(metadata.get("plugin_id") or _key)
            params = [dict(p) for p in (metadata.get("params") or [])
                      if isinstance(p, Mapping)]
            plugin_type = str(metadata.get("type") or "dctl")
            shader = metadata.get("shader_code")
            content_hash = str(metadata.get("content_hash") or "")
            if plugin_type == "ai":
                model_path = str(metadata.get("model_path") or "")
                available = self._ai_model_probe(model_path)
                reason = "" if available else "ai_model_missing"
                category = str(metadata.get("category") or "AI")
                realtime, bake = False, True
            else:
                available = bool(shader)
                reason = "" if available else "plugin_shader_missing"
                category = str(metadata.get("category") or "plugin")
                realtime, bake = True, False
            entries.append({
                "id": plugin_id,
                "kind": "effect",
                "name": str(metadata.get("name") or plugin_id),
                "category": category,
                "description": str(metadata.get("description") or ""),
                "author": str(metadata.get("author") or ""),
                "version": str(metadata.get("version") or ""),
                "content_hash": content_hash,
                "schema_hash": _plugin_schema_hash(params),
                "source_uri": str(metadata.get("source_uri") or ""),
                "source": plugin_type,
                "available": available,
                "disabled_reason": reason,
                "realtime": realtime,
                "bake_capable": bake,
                "parameters": params,
            })
        return entries

    def _project_builtins(self) -> list[dict[str, Any]]:
        catalog = self._ec_if_loaded()
        # 只读已构建的目录：未冷构建时不强制触发扫描/导入（由
        # warm_background 或 Edit 页首个消费者完成），GUI 线程零磁盘 IO。
        if catalog is None or not getattr(catalog, "is_built", lambda: False)():
            return []
        entries: list[dict[str, Any]] = []
        for spec in catalog.get_all_specs():
            effect_type = str(getattr(spec, "effect_type", "") or "")
            if not effect_type or effect_type.startswith("plugin:"):
                continue  # DCTL 插件已由 PluginManager 投影（id 契约不同）
            params = [_paramdef_to_param(p) for p in (getattr(spec, "param_defs", ()) or ())]
            entries.append({
                "id": _BUILTIN_ID_PREFIX + effect_type,
                "kind": "effect",
                "name": str(getattr(spec, "name", "") or effect_type),
                "category": str(getattr(spec, "category", "") or "filter"),
                "description": str(getattr(spec, "description", "") or ""),
                "author": "Topos",
                "version": "",
                "content_hash": str(getattr(spec, "plugin_content_hash", "") or ""),
                "schema_hash": str(getattr(spec, "plugin_parameter_schema_hash", "") or ""),
                "source_uri": "",
                "source": "builtin",
                # 内建 Composite 效果尚未接入 Color 节点图执行（R06 接线），
                # 如实禁用而非冒充可用。
                "available": False,
                "disabled_reason": "color_backend_not_wired",
                "realtime": True,
                "bake_capable": False,
                "parameters": params,
            })
        return entries

    @staticmethod
    def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同内容去重；同 plugin_id 不同内容确定性消歧（id@hash8）。"""
        by_content: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in entries:
            key = (entry["id"], entry.get("content_hash") or entry.get("schema_hash") or "")
            by_content.setdefault(key, entry)
        ambiguous: dict[str, int] = {}
        for entry_id, *_ in by_content:
            ambiguous[entry_id] = ambiguous.get(entry_id, 0) + 1
        result: list[dict[str, Any]] = []
        for (entry_id, _), entry in by_content.items():
            if ambiguous[entry_id] > 1:
                fingerprint = entry.get("content_hash") or entry.get("schema_hash") or ""
                entry = dict(entry, id=f"{entry_id}@{fingerprint[:8]}")
            result.append(entry)
        result.sort(key=lambda e: (e.get("category", ""), e.get("name", "")))
        return result
