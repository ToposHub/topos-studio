"""
Light3D Node — 3D 灯光数据源节点（P2 单灯光重构）。

P2 重构要点：
    - 一节点一盏灯（不再是 4 槽位方向光），符合 NUKE 节点工作流。
    - 支持 4 种灯光类型：directional（平行光）/ point（点光源）/ spot（聚光灯）
      / ambient（环境光）。
    - 图标锚点 icon_pos 用于 directional/ambient 无空间位置时的图标定位。

旧场景兼容（v6→v7 拆分前的 4 槽位参数）：
    from_dict 检测旧参数（light_dir_*/light0_intensity），迁移为单盏 directional
    平行光，保持旧场景默认外观（白光 + 原 dir）。

架构（双轨制）：
    节点产出 LightPayload（单灯光描述 + 可选 IBL），通过 _emit_scene_payload
    写入求值器旁路字典。Scene3D 聚合器收集多个 LightPayload 后合并为 shader
    需要的定长数组（最多 4 盏直接光 + ambient 常量）。
"""

from typing import Any, Dict, Optional

from .base_node import Node, Port, TextureResult, ParamDef


# ── 默认参数（提取为函数，便于和 parameters dict 对齐）──

def _make_default_param_defs():
    """构造 PARAM_DEFS（单灯光参数 + IBL 环境）。"""
    defs = [
        # ── 灯光类型 ──
        ParamDef("light_type", "option", "directional",
                 labels=["directional", "point", "spot", "ambient"],
                 group="灯光 / Light"),
        # ── 颜色 + 强度（所有类型共用）──
        ParamDef("light_color_r", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="灯光 / Light"),
        ParamDef("light_color_g", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="灯光 / Light"),
        ParamDef("light_color_b", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="灯光 / Light"),
        ParamDef("light_intensity", "float", 2.0, min_val=0.0, max_val=50.0, step=0.1,
                 group="灯光 / Light"),
        # ── 方向（directional / spot 主轴）──
        ParamDef("light_dir_x", "float", -0.6, min_val=-1.0, max_val=1.0, step=0.02,
                 group="方向 / Direction"),
        ParamDef("light_dir_y", "float", 0.7, min_val=-1.0, max_val=1.0, step=0.02,
                 group="方向 / Direction"),
        ParamDef("light_dir_z", "float", 0.4, min_val=-1.0, max_val=1.0, step=0.02,
                 group="方向 / Direction"),
        # ── 位置（point / spot）──
        ParamDef("light_pos_x", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="位置 / Position"),
        ParamDef("light_pos_y", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="位置 / Position"),
        ParamDef("light_pos_z", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="位置 / Position"),
        # ── 影响范围（point / spot 距离衰减半径）──
        ParamDef("light_range", "float", 10.0, min_val=0.0, max_val=1000.0, step=0.5,
                 group="衰减 / Attenuation"),
        ParamDef("light_decay_mode", "option", "inverse_square",
                 labels=["none", "linear", "inverse_square"],
                 group="衰减 / Attenuation"),
        # ── 聚光锥角（spot）──
        ParamDef("light_inner_cone", "float", 30.0, min_val=0.0, max_val=90.0, step=1.0,
                 group="锥角 / Cone (spot)"),
        ParamDef("light_outer_cone", "float", 45.0, min_val=0.0, max_val=90.0, step=1.0,
                 group="锥角 / Cone (spot)"),
        # ── 图标锚点（directional / ambient 无空间位置，用此锚点画视口图标）──
        ParamDef("icon_pos_x", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="图标锚点 / Icon Anchor"),
        ParamDef("icon_pos_y", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="图标锚点 / Icon Anchor"),
        ParamDef("icon_pos_z", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="图标锚点 / Icon Anchor"),
        # P1-26：shadow-map 仅按需分配；quality 控制方向光 shadow-catcher 精度。
        ParamDef("light_shadow_enable", "bool", True,
                 group="阴影 / Shadows"),
        ParamDef("light_shadow_quality", "option", "medium",
                 labels=["low", "medium", "high", "ultra"],
                 group="阴影 / Shadows"),
        ParamDef("light_shadow_bias", "float", 0.002, min_val=0.0, max_val=0.1,
                 step=0.0005, group="阴影 / Shadows"),
        ParamDef("light_shadow_softness", "float", 0.2, min_val=0.0, max_val=1.0,
                 step=0.05, group="阴影 / Shadows"),
        ParamDef("light_unit", "option", "m",
                 labels=["m", "cm", "mm", "ft", "in"],
                 group="单位 / Units"),
        ParamDef("ies_profile", "file", "", group="光度学 / Photometric"),
        ParamDef("ies_enabled", "bool", False, group="光度学 / Photometric"),
        # ── HDRI 环境（IBL）──
        ParamDef("hdri_path", "file", "", group="环境 / HDRI"),
        ParamDef("use_ibl", "bool", True, group="环境 / HDRI"),
        ParamDef("env_intensity", "float", 1.0, min_val=0.0, max_val=10.0, step=0.05,
                 group="环境 / HDRI"),
    ]
    return defs


def _make_default_parameters():
    """构造 parameters 默认值（与 PARAM_DEFS 默认值对齐）。"""
    return {
        "light_type": "directional",
        "light_color_r": 1.0, "light_color_g": 1.0, "light_color_b": 1.0,
        "light_intensity": 2.0,
        "light_dir_x": -0.6, "light_dir_y": 0.7, "light_dir_z": 0.4,
        "light_pos_x": 0.0, "light_pos_y": 0.0, "light_pos_z": 0.0,
        "light_range": 10.0,
        "light_decay_mode": "inverse_square",
        "light_inner_cone": 30.0, "light_outer_cone": 45.0,
        "icon_pos_x": 0.0, "icon_pos_y": 0.0, "icon_pos_z": 0.0,
        "light_shadow_enable": True,
        "light_shadow_quality": "medium",
        "light_shadow_bias": 0.002,
        "light_shadow_softness": 0.2,
        "light_unit": "m",
        "ies_profile": "",
        "ies_enabled": False,
        "hdri_path": "",
        "use_ibl": True,
        "env_intensity": 1.0,
    }


def _legacy_float(value: Any, default: float) -> float:
    """Coerce a persisted legacy value, falling back on garbage.

    Legacy slot/main-light keys have no ParamDef, so the load-time
    sanitization in ``Node.from_dict`` does not cover them; a hand-edited
    v6 project must not crash the load with ValueError/TypeError.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# 旧参数 → 新参数的迁移映射（v6 4 槽位 / legacy 主灯 → P2 单灯光）
_LEGACY_PARAMS = frozenset({
    "light0_dir_x", "light0_dir_y", "light0_dir_z",
    "light0_color_r", "light0_color_g", "light0_color_b", "light0_intensity",
    "light1_dir_x", "light1_dir_y", "light1_dir_z",
    "light1_color_r", "light1_color_g", "light1_color_b", "light1_intensity",
    "light2_dir_x", "light2_dir_y", "light2_dir_z",
    "light2_color_r", "light2_color_g", "light2_color_b", "light2_intensity",
    "light3_dir_x", "light3_dir_y", "light3_dir_z",
    "light3_color_r", "light3_color_g", "light3_color_b", "light3_intensity",
})


class Light3DNode(Node):
    """3D 灯光源节点（P2 单灯光重构）：一节点一盏灯，支持 4 种灯光类型。

    灯光类型：
        - directional：平行光（无位置，用 dir 方向，无衰减）。
        - point：点光源（有 pos，按 range 距离衰减）。
        - spot：聚光灯（有 pos + dir + 锥角 inner/outer cone）。
        - ambient：环境光（全局常量色，加到 ambient 项，不进直接光数组）。
    可选 HDRI/IBL 环境光：本节点启用 use_ibl 时烘焙 IBL 三件套。
    """

    node_type = "light3d"
    node_title = "Light"
    i18n_title_key = "nodes.light3d.title"
    header_color = "#b8861d"  # 暖金，灯光类

    SEARCH_ALIASES = ["light3d", "light", "灯光", "3d灯光", "ibl", "hdri",
                      "directional", "point", "spot", "ambient",
                      "平行光", "点光源", "聚光灯", "环境光"]
    DESCRIPTION = "3D 灯光节点：支持平行光/点光源/聚光灯/环境光四种类型 + HDRI 环境（IBL）"
    TAGS = ["3d", "source", "light"]

    PARAM_DEFS = _make_default_param_defs()

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("display", "output", "scene"),
        ]
        self.parameters = _make_default_parameters()
        # IBL 烘焙器（按 hdri_path+mtime 内部缓存 cube map）
        self._hdri_baker = None
        self._ies_cache_key = None
        self._ies_samples = (1.0, 1.0, 1.0, 1.0)

    @classmethod
    def from_dict(cls, data):
        """反序列化 + 旧参数迁移（v6 4 槽位 → P2 单灯光）。

        旧场景文件含 light_dir_*/light0_intensity（legacy 主灯）或 light{i}_*
        （4 槽位），迁移为单盏 directional：取 legacy 主灯或 slot0 作为灯参数。

        AUD-6913: 迁移检测必须基于磁盘上的**原始 loaded 参数**
        （``data["parameters"]``）——``super().from_dict`` 先按
        ``_make_default_parameters()`` 构造节点再合并覆盖，
        ``node.parameters`` 因此恒含 ``light_type``/``light_pos_x`` 等新键，
        旧检测（基于合并后的 parameters）使 ``"light_type" not in params``
        恒为 False，整个迁移分支不可达，v6 场景灯光静默回落默认值。
        """
        node = super().from_dict(data)
        params = node.parameters
        loaded = data.get("parameters", {})
        if not isinstance(loaded, dict):
            loaded = {}
        # 检测是否有旧参数（只看 loaded，不看合并后的默认值）
        has_legacy = (any(p in loaded for p in _LEGACY_PARAMS)
                      or "light_intensity" in loaded)
        if has_legacy and "light_type" not in loaded and not _has_new_light_params(loaded):
            # 迁移：取 legacy 主灯（light_dir_* + light_intensity）或 slot0
            if "light_intensity" in loaded:
                # legacy 主灯存在
                params.setdefault("light_type", "directional")
                params["light_intensity"] = _legacy_float(
                    loaded.get("light_intensity", 2.0), 2.0)
                if "light_dir_x" in loaded:
                    params["light_dir_x"] = _legacy_float(
                        loaded.get("light_dir_x", -0.6), -0.6)
                    params["light_dir_y"] = _legacy_float(
                        loaded.get("light_dir_y", 0.7), 0.7)
                    params["light_dir_z"] = _legacy_float(
                        loaded.get("light_dir_z", 0.4), 0.4)
            elif "light0_intensity" in loaded:
                # slot0 存在（intensity>0 用 slot0，否则用默认）
                li0 = _legacy_float(loaded.get("light0_intensity", 0.0), 0.0)
                if li0 > 0.0:
                    params.setdefault("light_type", "directional")
                    params["light_intensity"] = li0
                    params["light_dir_x"] = _legacy_float(
                        loaded.get("light0_dir_x", -0.6), -0.6)
                    params["light_dir_y"] = _legacy_float(
                        loaded.get("light0_dir_y", 0.7), 0.7)
                    params["light_dir_z"] = _legacy_float(
                        loaded.get("light0_dir_z", 0.4), 0.4)
                    params["light_color_r"] = _legacy_float(
                        loaded.get("light0_color_r", 1.0), 1.0)
                    params["light_color_g"] = _legacy_float(
                        loaded.get("light0_color_g", 1.0), 1.0)
                    params["light_color_b"] = _legacy_float(
                        loaded.get("light0_color_b", 1.0), 1.0)
            # 清除旧槽位参数（避免 parameters 字典污染）
            for legacy_key in list(_LEGACY_PARAMS):
                params.pop(legacy_key, None)
            # 确保新参数有默认值
            defaults = _make_default_parameters()
            for k, v in defaults.items():
                params.setdefault(k, v)
        return node

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """按 light_type 组装单灯光，emit LightPayload 到旁路。

        参数按类型生效：
            - directional：用 dir 方向，无位置/衰减。
            - point：用 pos 位置，按 range 距离衰减。
            - spot：用 pos + dir + inner/outer cone 锥角。
            - ambient：作为全局环境色，Scene3D 加到 ambient 项。
        IBL 烘焙逻辑保留：use_ibl 或 hdri_path 非空时烘焙三件套。
        """
        from ..render3d import math_utils as m3d
        from ..render3d.scene_payload import (
            LightPayload, LIGHT_TYPE_DIRECTIONAL, LIGHT_TYPE_POINT,
            LIGHT_TYPE_SPOT, LIGHT_TYPE_AMBIENT,
        )

        # 解包 GL context（source 节点收到 CompositeEvalContext）
        from ..core.eval_context import CompositeEvalContext
        gl_ctx = ctx
        if isinstance(ctx, CompositeEvalContext):
            gl_ctx = ctx.gl

        # ── 读参数 ──
        light_type = str(self.parameters.get("light_type", "directional"))
        if light_type not in (LIGHT_TYPE_DIRECTIONAL, LIGHT_TYPE_POINT,
                              LIGHT_TYPE_SPOT, LIGHT_TYPE_AMBIENT):
            light_type = LIGHT_TYPE_DIRECTIONAL

        ldx = float(self.parameters.get("light_dir_x", -0.6))
        ldy = float(self.parameters.get("light_dir_y", 0.7))
        ldz = float(self.parameters.get("light_dir_z", 0.4))
        # 归一化方向（directional/spot 用）
        dir_norm = m3d.normalize(m3d.vec3(ldx, ldy, ldz))

        lpx = float(self.parameters.get("light_pos_x", 0.0))
        lpy = float(self.parameters.get("light_pos_y", 0.0))
        lpz = float(self.parameters.get("light_pos_z", 0.0))

        lcr = max(0.0, float(self.parameters.get("light_color_r", 1.0)))
        lcg = max(0.0, float(self.parameters.get("light_color_g", 1.0)))
        lcb = max(0.0, float(self.parameters.get("light_color_b", 1.0)))
        intensity = max(0.0, float(self.parameters.get("light_intensity", 2.0)))

        from ..render3d.camera_model import world_unit_scale
        light_unit = str(self.parameters.get("light_unit", "m"))
        unit_scale = world_unit_scale(light_unit)
        light_range = max(0.0, float(self.parameters.get("light_range", 10.0))) * unit_scale
        lpx *= unit_scale
        lpy *= unit_scale
        lpz *= unit_scale
        inner_cone = max(0.0, min(90.0, float(self.parameters.get("light_inner_cone", 30.0))))
        outer_cone = max(inner_cone, min(90.0, float(self.parameters.get("light_outer_cone", 45.0))))

        decay_mode = str(self.parameters.get("light_decay_mode", "inverse_square"))
        if decay_mode not in {"none", "linear", "inverse_square"}:
            decay_mode = "inverse_square"
        shadow_quality = str(self.parameters.get("light_shadow_quality", "medium"))
        if shadow_quality not in {"low", "medium", "high", "ultra"}:
            shadow_quality = "medium"
        shadow_enabled = bool(self.parameters.get("light_shadow_enable", True))
        shadow_bias = max(0.0, min(0.1, float(
            self.parameters.get("light_shadow_bias", 0.002))))
        shadow_softness = max(0.0, min(1.0, float(
            self.parameters.get("light_shadow_softness", 0.2))))

        ies_profile_path = str(self.parameters.get("ies_profile", "") or "")
        ies_enabled = bool(self.parameters.get("ies_enabled", False)) and bool(
            ies_profile_path)
        ies_samples = (1.0, 1.0, 1.0, 1.0)
        if ies_enabled:
            try:
                import os
                from ..render3d.ies_profile import load_ies_profile
                key = (os.path.abspath(ies_profile_path),
                       os.path.getmtime(ies_profile_path))
                if key != self._ies_cache_key:
                    self._ies_samples = load_ies_profile(ies_profile_path).samples(4)
                    self._ies_cache_key = key
                ies_samples = tuple(self._ies_samples)
            except (OSError, TypeError, ValueError):
                # A missing/broken profile is a non-fatal light diagnostic.
                ies_enabled = False
                self._ies_cache_key = None

        ipx = float(self.parameters.get("icon_pos_x", 0.0)) * unit_scale
        ipy = float(self.parameters.get("icon_pos_y", 0.0)) * unit_scale
        ipz = float(self.parameters.get("icon_pos_z", 0.0)) * unit_scale

        # ── IBL 环境（原样保留 P1 的烘焙逻辑）──
        hdri_path = str(self.parameters.get("hdri_path", "") or "")
        use_ibl = bool(self.parameters.get("use_ibl", True))
        env_intensity = max(0.0, float(self.parameters.get("env_intensity", 1.0)))

        irradiance_cube = prefilter_cube = brdf_lut = None
        if use_ibl or hdri_path:
            try:
                from ..render3d.hdri import HDRIBaker
                if self._hdri_baker is None:
                    self._hdri_baker = HDRIBaker()
                if not use_ibl or not hdri_path:
                    irradiance_cube, prefilter_cube, brdf_lut = self._hdri_baker.bake(gl_ctx, "")
                else:
                    irradiance_cube, prefilter_cube, brdf_lut = self._hdri_baker.bake(gl_ctx, hdri_path)
            except Exception as exc:
                # bake 失败不中断渲染；降级为无 IBL
                import logging
                logging.getLogger(__name__).warning(
                    "Light3D IBL bake 失败，降级为无环境光: %s (path=%s)", exc, hdri_path)
                try:
                    irradiance_cube, prefilter_cube, brdf_lut = self._hdri_baker.bake(gl_ctx, "")
                except Exception:
                    pass

        # ── 组装单灯光 payload ──
        # ambient 类型：强度仍记录，Scene3D 加到 ambient 项（不进直接光数组）
        payload = LightPayload(
            light_type=light_type,
            dir=(float(dir_norm[0]), float(dir_norm[1]), float(dir_norm[2])),
            pos=(lpx, lpy, lpz),
            color=(lcr, lcg, lcb),
            intensity=intensity,
            range=light_range,
            inner_cone=inner_cone,
            outer_cone=outer_cone,
            decay_mode=decay_mode,
            shadow_enabled=shadow_enabled,
            shadow_quality=shadow_quality,
            shadow_bias=shadow_bias,
            shadow_softness=shadow_softness,
            ies_profile_path=ies_profile_path,
            ies_enabled=ies_enabled,
            ies_samples=ies_samples,
            icon_pos=(ipx, ipy, ipz),
            irradiance_cube=irradiance_cube,
            prefilter_cube=prefilter_cube,
            brdf_lut=brdf_lut,
            env_intensity=env_intensity,
            use_ibl=use_ibl,
        )
        self._emit_scene_payload(ctx, "display", payload)
        return {}

    def cleanup(self) -> None:
        """释放 IBL 烘焙器持有的 cube map 资源。

        HDRIBaker 内部按 path+mtime 缓存 cube 对象；节点销毁时释放。
        """
        if self._hdri_baker is not None:
            try:
                self._hdri_baker.release()
            except Exception:
                pass
            self._hdri_baker = None


def _has_new_light_params(params: dict) -> bool:
    """检查 parameters 是否已含 P2 新参数（light_type 等）。"""
    return "light_type" in params and "light_pos_x" in params
