"""
Camera3D Node — 3D 相机数据源节点（阶段七）。

把原本揉在 Scene3D 里的相机参数（cam_fov/cam_dist/cam_orbit_yaw/pitch/
target_*）独立成节点，输出 CameraPayload 到 Scene3D 聚合器的 camera_in。

P1 相机模式扩展：支持三种相机模式（camera_mode 参数）：
    - orbit：轨道相机（默认），绕 target 旋转（dist/yaw/pitch）。
    - free：自由相机，用 cam_eye_* 精确摆位，看向 target。
    - ortho：正交投影（top/right/front 视图用），无透视收缩。
另支持可调 near/far 裁剪面、cam_roll 横滚、cam_ortho_scale 正交半高。

架构（双轨制）：
    - 节点产出 ScenePayload（proj/view/eye/target/pitch），通过 _emit_scene_payload
      写入求值器旁路字典，不进标准缓存轨道
    - 仍输出一个 "display" 端口（scene 类型）供节点图连线；占位 TextureResult
      由 Scene3D 聚合时替换为真实渲染图
"""

from typing import Any, Dict, Optional

from .base_node import Node, Port, TextureResult, ParamDef


class Camera3DNode(Node):
    """3D 相机源节点：orbit/free/ortho 三模式，产出 proj/view/eye/target 喂给 Scene3D。"""

    node_type = "camera3d"
    node_title = "Camera"
    i18n_title_key = "nodes.camera3d.title"
    header_color = "#1d6fb8"  # 蓝，相机类

    SEARCH_ALIASES = ["camera3d", "camera", "相机", "3d相机", "view",
                      "orbit", "ortho", "orthographic", "free"]
    DESCRIPTION = "3D 相机节点：orbit/free/ortho 三模式（FOV/裁剪面/横滚/正交），输出给 Scene3D"
    TAGS = ["3d", "source", "camera"]

    PARAM_DEFS = [
        # ── 相机模式（P1 扩展：轨道 / 自由 / 正交）──
        # orbit：绕 target 旋转（yaw/pitch/dist），开箱即用 + Alt 导航回写。
        # free：自由位置/朝向（cam_eye_* + cam_target_*），适合精确摆位。
        # ortho：正交投影（top/right/front 视图用），无透视收缩。
        ParamDef("camera_mode", "option", "orbit",
                 labels=["orbit", "free", "ortho"], group="相机模式 / Camera"),
        # ── 透视相机（orbit / free 共用 FOV；ortho 模式下 fov 无效）──
        ParamDef("cam_fov", "float", 50.0, min_val=1.0, max_val=170.0, step=0.5,
                 group="相机模式 / Camera"),
        # P3 专业摄影机：焦距 + 传感器（与 FOV 联动换算，二选一调节）
        ParamDef("cam_focal_length", "float", 38.6, min_val=2.0, max_val=500.0, step=0.1,
                 group="相机模式 / Camera"),
        ParamDef("cam_sensor_size", "float", 36.0, min_val=1.0, max_val=100.0, step=0.1,
                 group="相机模式 / Camera"),
        # P1-26：显式 filmback。cam_sensor_size 保留为旧场景兼容的高度值。
        ParamDef("cam_filmback", "option", "custom",
                 labels=["custom", "full_frame", "super35", "aps_c",
                         "micro_four_thirds", "16mm"],
                 group="Filmback / Filmback"),
        ParamDef("cam_sensor_width", "float", 36.0, min_val=1.0, max_val=100.0,
                 step=0.1, group="Filmback / Filmback"),
        ParamDef("cam_gate_fit", "option", "vertical",
                 labels=["vertical", "horizontal", "fill", "overscan"],
                 group="Filmback / Filmback"),
        ParamDef("cam_match_mode", "option", "preserve_fov",
                 labels=["preserve_fov", "preserve_focal_length"],
                 group="Filmback / Filmback"),
        ParamDef("cam_unit", "option", "m",
                 labels=["m", "cm", "mm", "ft", "in"],
                 group="单位 / Units"),
        # ── 裁剪面（P1 扩展：原硬编码 0.1/1000.0，现可调）──
        ParamDef("cam_near", "float", 0.1, min_val=0.001, max_val=100.0, step=0.1,
                 group="裁剪面 / Clipping"),
        ParamDef("cam_far", "float", 1000.0, min_val=10.0, max_val=100000.0, step=10.0,
                 group="裁剪面 / Clipping"),
        # ── 镜头偏移（lens shift，平移视锥中心不产生透视变形）──
        ParamDef("cam_lens_shift_x", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01,
                 group="相机模式 / Camera"),
        ParamDef("cam_lens_shift_y", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01,
                 group="相机模式 / Camera"),
        # ── 曝光（EV，渲染输出线性 RGB 的乘数，便于在 3D 阶段控制明度）──
        ParamDef("cam_exposure", "float", 0.0, min_val=-5.0, max_val=5.0, step=0.1,
                 group="相机模式 / Camera"),
        # ── 轨道相机参数（camera_mode=orbit 时生效）──
        ParamDef("cam_dist", "float", 5.0, min_val=0.1, max_val=1000.0, step=0.1,
                 group="轨道 / Orbit"),
        ParamDef("cam_orbit_yaw", "float", 30.0, min_val=-360.0, max_val=360.0, step=1.0,
                 group="轨道 / Orbit"),
        ParamDef("cam_orbit_pitch", "float", 20.0, min_val=-89.0, max_val=89.0, step=1.0,
                 group="轨道 / Orbit"),
        # cam_target_* 是轨道/自由相机的注视中心，Alt+中键平移会更新它（真正的 pan）
        ParamDef("cam_target_x", "float", 0.0, min_val=-1000.0, max_val=1000.0, step=0.1,
                 group="注视点 / Target"),
        ParamDef("cam_target_y", "float", 0.0, min_val=-1000.0, max_val=1000.0, step=0.1,
                 group="注视点 / Target"),
        ParamDef("cam_target_z", "float", 0.0, min_val=-1000.0, max_val=1000.0, step=0.1,
                 group="注视点 / Target"),
        # ── 自由相机位置（camera_mode=free 时生效；orbit 模式忽略）──
        ParamDef("cam_eye_x", "float", 4.0, min_val=-1000.0, max_val=1000.0, step=0.1,
                 group="自由位置 / Eye (free)"),
        ParamDef("cam_eye_y", "float", 3.0, min_val=-1000.0, max_val=1000.0, step=0.1,
                 group="自由位置 / Eye (free)"),
        ParamDef("cam_eye_z", "float", 5.0, min_val=-1000.0, max_val=1000.0, step=0.1,
                 group="自由位置 / Eye (free)"),
        # ── 相机横滚（绕前向轴旋转，影响 up 向量；所有模式共用）──
        ParamDef("cam_roll", "float", 0.0, min_val=-180.0, max_val=180.0, step=1.0,
                 group="相机模式 / Camera"),
        # ── 正交视图半高（camera_mode=ortho 时生效；世界单位）──
        ParamDef("cam_ortho_scale", "float", 5.0, min_val=0.1, max_val=1000.0, step=0.1,
                 group="相机模式 / Camera"),
        # ── 景深 DOF（P3 新增；需要 Scene3D 后处理支持）──
        ParamDef("cam_dof_enable", "bool", False, group="景深 / Depth of Field"),
        ParamDef("cam_focus_distance", "float", 5.0, min_val=0.1, max_val=1000.0, step=0.1,
                 group="景深 / Depth of Field"),
        ParamDef("cam_fstop", "float", 2.8, min_val=0.5, max_val=64.0, step=0.1,
                 group="景深 / Depth of Field"),
        # ── 安全框（视口 UI overlay，不进渲染输出）──
        ParamDef("cam_show_safe_areas", "bool", False, group="显示 / Display"),
        # ── 画幅（算 aspect 用；Scene3D 也会反向同步）──
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1,
                 group="画幅 / Format"),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1,
                 group="画幅 / Format"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("display", "output", "scene"),
        ]
        self.parameters = {
            "camera_mode": "orbit",
            "cam_fov": 50.0,
            "cam_focal_length": 38.6,
            "cam_sensor_size": 36.0,
            "cam_filmback": "custom",
            "cam_sensor_width": 36.0,
            "cam_gate_fit": "vertical",
            "cam_match_mode": "preserve_fov",
            "cam_unit": "m",
            "cam_near": 0.1,
            "cam_far": 1000.0,
            "cam_lens_shift_x": 0.0,
            "cam_lens_shift_y": 0.0,
            "cam_exposure": 0.0,
            "cam_dist": 5.0,
            "cam_orbit_yaw": 30.0,
            "cam_orbit_pitch": 20.0,
            "cam_target_x": 0.0,
            "cam_target_y": 0.0,
            "cam_target_z": 0.0,
            "cam_eye_x": 4.0,
            "cam_eye_y": 3.0,
            "cam_eye_z": 5.0,
            "cam_roll": 0.0,
            "cam_ortho_scale": 5.0,
            "cam_dof_enable": False,
            "cam_focus_distance": 5.0,
            "cam_fstop": 2.8,
            "cam_show_safe_areas": False,
            "width": 1920,
            "height": 1080,
        }
        self._syncing_lens_params = False

    def set_parameter(self, key: str, value) -> None:
        """Set a camera parameter and keep filmback optics consistently linked."""
        super().set_parameter(key, value)
        if self._syncing_lens_params:
            return
        if key == "cam_filmback":
            try:
                from ..render3d.camera_model import filmback_spec
                spec = filmback_spec(
                    str(self.parameters.get("cam_filmback", "custom")),
                    custom_width_mm=float(self.parameters.get("cam_sensor_width", 36.0)),
                    custom_height_mm=float(self.parameters.get("cam_sensor_size", 36.0)),
                )
                self._syncing_lens_params = True
                super().set_parameter("cam_sensor_width", spec.width_mm)
                super().set_parameter("cam_sensor_size", spec.height_mm)
            except (TypeError, ValueError):
                pass
            finally:
                self._syncing_lens_params = False
        elif key in {"cam_sensor_size", "cam_sensor_width"}:
            # Manual gate edits are no longer a named preset.
            self._syncing_lens_params = True
            super().set_parameter("cam_filmback", "custom")
            self._syncing_lens_params = False

        if key not in {"cam_fov", "cam_focal_length", "cam_sensor_size",
                       "cam_sensor_width", "cam_filmback", "cam_gate_fit",
                       "width", "height"}:
            return
        try:
            from ..render3d.camera_model import (
                filmback_spec, focal_from_fov_and_filmback,
                fov_from_focal_and_filmback)
            self._syncing_lens_params = True
            aspect = max(1.0, float(self.parameters.get("width", 1920))) / max(
                1.0, float(self.parameters.get("height", 1080)))
            spec = filmback_spec(
                str(self.parameters.get("cam_filmback", "custom")),
                custom_width_mm=float(self.parameters.get("cam_sensor_width", 36.0)),
                custom_height_mm=float(self.parameters.get("cam_sensor_size", 36.0)),
            )
            gate_fit = str(self.parameters.get("cam_gate_fit", "vertical"))
            if key == "cam_fov":
                fov = max(1.0, min(170.0, float(self.parameters.get("cam_fov", 50.0))))
                super().set_parameter(
                    "cam_focal_length",
                    focal_from_fov_and_filmback(fov, spec, aspect, gate_fit))
            elif key in {"cam_focal_length", "cam_sensor_size", "cam_sensor_width",
                         "cam_filmback", "cam_gate_fit", "width", "height"}:
                focal = max(0.1, float(self.parameters.get("cam_focal_length", 38.6)))
                match_mode = str(self.parameters.get("cam_match_mode", "preserve_fov"))
                if key == "cam_filmback" and match_mode == "preserve_fov":
                    # The preset change should retain the current framing.
                    fov = max(1.0, min(170.0,
                                      float(self.parameters.get("cam_fov", 50.0))))
                    super().set_parameter(
                        "cam_focal_length",
                        focal_from_fov_and_filmback(fov, spec, aspect, gate_fit))
                else:
                    super().set_parameter(
                        "cam_fov",
                        fov_from_focal_and_filmback(focal, spec, aspect, gate_fit))
        except Exception:
            pass
        finally:
            self._syncing_lens_params = False

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """计算相机投影/视图矩阵，emit CameraPayload 到旁路。

        P1 相机模式扩展，按 ``camera_mode`` 分三路：
            - orbit：绕 target 旋转（dist/yaw/pitch），开箱即用。
            - free：自由位置（cam_eye_*）看向 target，精确摆位。
            - ortho：正交投影（top/right/front 视图用），无透视收缩。

        三路共用 target/roll/near/far/aspect。pitch 由 eye-target 反算后写回
        payload（驱动 Scene3D grid 地平线渐隐）。roll 绕前向轴旋转 up 向量。
        """
        from ..render3d import math_utils as m3d
        from ..render3d.scene_payload import CameraPayload

        # 读参数
        camera_mode = str(self.parameters.get("camera_mode", "orbit"))
        # AUD-64015: clamp to the ParamDef range [1, 170] — the same clamp
        # the set_parameter lens linkage applies.  math_utils.perspective
        # guards aspect/near/far but not fov: fov of 0/360 makes
        # 1/tan(fov/2) → inf and poisons the whole projection matrix
        # (Scene3D renders garbage with no raised error).  from_dict /
        # out-of-range keyframes can write such values.
        cam_fov = min(170.0, max(1.0,
                                 float(self.parameters.get("cam_fov", 50.0))))
        cam_focal = float(self.parameters.get("cam_focal_length", 38.6))
        cam_sensor = float(self.parameters.get("cam_sensor_size", 36.0))
        cam_filmback_name = str(self.parameters.get("cam_filmback", "custom"))
        cam_sensor_width = float(self.parameters.get("cam_sensor_width", 36.0))
        cam_gate_fit = str(self.parameters.get("cam_gate_fit", "vertical"))
        cam_match_mode = str(self.parameters.get("cam_match_mode", "preserve_fov"))
        cam_unit = str(self.parameters.get("cam_unit", "m"))
        from ..render3d.camera_model import filmback_spec, world_unit_scale
        filmback = filmback_spec(
            cam_filmback_name, custom_width_mm=cam_sensor_width,
            custom_height_mm=cam_sensor)
        cam_sensor = filmback.height_mm
        unit_scale = world_unit_scale(cam_unit)
        near = max(0.001, float(self.parameters.get("cam_near", 0.1)) * unit_scale)
        far = max(near + 1.0, float(self.parameters.get("cam_far", 1000.0)) * unit_scale)
        cam_lens_shift_x = float(self.parameters.get("cam_lens_shift_x", 0.0))
        cam_lens_shift_y = float(self.parameters.get("cam_lens_shift_y", 0.0))
        cam_exposure = float(self.parameters.get("cam_exposure", 0.0))
        cam_dist = max(0.1, float(self.parameters.get("cam_dist", 5.0)) * unit_scale)
        cam_yaw = float(self.parameters.get("cam_orbit_yaw", 30.0))
        cam_orbit_pitch = float(self.parameters.get("cam_orbit_pitch", 20.0))
        cam_tx = float(self.parameters.get("cam_target_x", 0.0)) * unit_scale
        cam_ty = float(self.parameters.get("cam_target_y", 0.0)) * unit_scale
        cam_tz = float(self.parameters.get("cam_target_z", 0.0)) * unit_scale
        cam_roll = float(self.parameters.get("cam_roll", 0.0))
        cam_ortho_scale = max(0.1, float(self.parameters.get("cam_ortho_scale", 5.0))) * unit_scale
        # P3 景深参数
        cam_dof_enable = bool(self.parameters.get("cam_dof_enable", False))
        cam_focus_distance = max(0.1, float(self.parameters.get("cam_focus_distance", 5.0))) * unit_scale
        cam_fstop = max(0.5, float(self.parameters.get("cam_fstop", 2.8)))
        cam_show_safe_areas = bool(self.parameters.get("cam_show_safe_areas", False))
        w = max(1, int(self.parameters.get("width", 1920)))
        h = max(1, int(self.parameters.get("height", 1080)))
        aspect = w / h if h > 0 else 1.0
        cam_target = m3d.vec3(cam_tx, cam_ty, cam_tz)

        # ── 投影矩阵：ortho 模式用正交，其余用透视（含 lens shift）──
        if camera_mode == "ortho":
            proj = m3d.ortho(cam_ortho_scale, aspect, near, far)
        else:
            # P3：支持镜头偏移（lens shift），shift 非零时用 perspective_with_shift
            if abs(cam_lens_shift_x) > 1e-4 or abs(cam_lens_shift_y) > 1e-4:
                proj = m3d.perspective_with_shift(
                    cam_fov, aspect, near, far, cam_lens_shift_x, cam_lens_shift_y)
            else:
                proj = m3d.perspective(cam_fov, aspect, near, far)

        # ── 视图矩阵：按模式决定 eye ──
        if camera_mode == "free":
            # 自由相机：直接用 cam_eye_* 位置
            eye = m3d.vec3(
                float(self.parameters.get("cam_eye_x", 4.0)) * unit_scale,
                float(self.parameters.get("cam_eye_y", 3.0)) * unit_scale,
                float(self.parameters.get("cam_eye_z", 5.0)) * unit_scale,
            )
            pitch = m3d.pitch_from_eye_target(eye, cam_target)
        else:
            # orbit / ortho：orbit 参数推 eye（ortho 也用轨道位置控制视角朝向）
            eye = m3d.orbit_to_eye(cam_dist, cam_yaw, cam_orbit_pitch, target=cam_target)
            pitch = cam_orbit_pitch

        # roll：绕前向轴旋转 up 向量（非零时构造旋转 up）
        if abs(cam_roll) > 1e-3:
            # 前向 = target - eye；绕此轴旋转世界 up（0,1,0）roll 度
            forward = cam_target - eye
            up = m3d.rotate_around_axis(m3d.vec3(0.0, 1.0, 0.0), forward, cam_roll)
        else:
            up = m3d.vec3(0.0, 1.0, 0.0)
        view = m3d.look_at(eye, cam_target, up)

        payload = CameraPayload(
            proj=proj,
            view=view,
            eye=tuple(float(x) for x in eye),
            target=(cam_tx, cam_ty, cam_tz),
            near=near,
            far=far,
            fov=cam_fov,
            pitch=pitch,
            camera_mode=camera_mode,
            exposure=cam_exposure,
            dof_enable=cam_dof_enable,
            focus_distance=cam_focus_distance,
            fstop=cam_fstop,
            focal_length=cam_focal,
            sensor_size=cam_sensor,
            sensor_width=filmback.width_mm,
            filmback=filmback.name,
            gate_fit=cam_gate_fit,
            match_mode=cam_match_mode,
            unit_mode=cam_unit,
            unit_scale=unit_scale,
            lens_shift_x=cam_lens_shift_x,
            lens_shift_y=cam_lens_shift_y,
            show_safe_areas=cam_show_safe_areas,
        )
        self._emit_scene_payload(ctx, "display", payload)
        return {}

    def cleanup(self) -> None:
        """Camera3D 无 GL 资源（纯 numpy），无需清理。"""
        pass
