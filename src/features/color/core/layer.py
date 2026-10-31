"""调色图层数据模型（Layer / MaskShape / MaskGroup）。

职责：定义非破坏性调色的核心节点状态——``Layer`` 封装一条调色链的
全部参数（色轮/曲线/HSL/混色器/warper/效果/LUT/软剪裁/限定器/
插件栈/AI 蒙版/烘焙缓存/关键帧轨道）与序列化（to_dict/from_dict，
含 assets_dir 二进制外存）；``MaskShape``/``MaskGroup`` 描述 Power
Window 形状与可嵌套群组；模块级 helper ``build_curves_lut_from_data``
/ ``build_hsl_curve_lut_from_data`` 从曲线控制点重建渲染用 LUT。

线程边界：纯数据类、无锁。实例在 UI 线程创建与编辑；渲染线程
（AsyncRenderer）与导出 worker 以只读方式读取参数与 ``to_dict``
快照。``is_dirty`` / ``mask_dirty`` 是 UI→渲染的失效标志（渲染侧
消费后复位），MaskShape 内部渲染缓存归光栅化方所有。并发修改同一
Layer 不受保护，编辑必须串行于 UI 线程。
"""
import copy
import hashlib
import logging
import os
import numpy as np
import cv2
import uuid
from typing import List, Tuple, Union, Optional, Dict, Any, Iterable

try:
    from scipy.interpolate import PchipInterpolator
    HAS_SCIPY = True
except ModuleNotFoundError:
    HAS_SCIPY = False

logger = logging.getLogger(__name__)

class MaskShape:
    """Power Window 蒙版形状（circle/rect/curve/gradient/bitmap/brush）。

    几何使用 0.0-1.0 归一化坐标（分辨率无关）；``operation`` 决定与
    已有蒙版的布尔组合（add/subtract/intersect）。类常量 CIRCLE/RECT/
    CURVE/GRADIENT/BITMAP/BRUSH 为形状类型，OP_ADD/OP_SUBTRACT/
    OP_INTERSECT 为布尔运算。

    内部维护两级光栅化缓存（``_render_cache`` 完整结果 / ``_shape_cache``
    未旋转形状），键由 ``_get_render_cache_key`` / ``_get_shape_cache_key``
    计算，归渲染方管理。
    """

    CIRCLE = "circle"
    RECT = "rect"
    CURVE = "curve"
    GRADIENT = "gradient"
    BITMAP = "bitmap"
    BRUSH = "brush"  # New: Brush tool type

    # Boolean Operations
    OP_ADD = "add"
    OP_SUBTRACT = "subtract"
    OP_INTERSECT = "intersect"

    def __init__(self, shape_type: str) -> None:
        """
        按形状类型创建默认蒙版。

        Args:
            shape_type: MaskShape.CIRCLE/RECT/CURVE/GRADIENT/BITMAP/BRUSH
                之一；curve 默认 feather=0，其余默认 50（0-100 标度）。
        """
        self.id = str(uuid.uuid4())
        self.type = shape_type
        self.name = None # Optional custom name
        self.visible = True
        self.invert = False
        self.locked = False  # P1-COLOR-09: 锁定后画布禁止拖动/改形
        self.operation = self.OP_ADD
        self.feather = 0 if shape_type == self.CURVE else 50  # 0-100 scale
        self.opacity = 1.0 # 0.0-1.0
        self.blur = 0.0    # 0-100 scale
        self.clean_black = 0.0 # 0-20 scale
        self.clean_white = 0.0 # 0-20 scale
        
        # Bitmap Data (for AI Masks)
        self.bitmap_data = None
        
        # Brush Strokes Data (for Brush tool)
        # Each stroke: {"points": [(x, y, pressure), ...], "size": 50, "feather": 0, "opacity": 1.0, "flow": 1.0}
        self.brush_strokes = []
        self.brush_size = 50      # Default brush size (pixels)
        self.brush_feather = 0    # Default brush feather (0-100)
        self.brush_opacity = 1.0  # Default brush opacity (0.0-1.0)
        self.brush_flow = 1.0     # Default brush flow (0.0-1.0)

        # Geometry (Normalized 0.0-1.0 to be resolution independent)
        self.center = [0.5, 0.5]
        self.size = [0.3, 0.3] # [width/radius, height]
        self.rotation = 0.0
        # Points: list of dicts for curves: [{"pos": (x,y), "c1": (x,y), "c2": (x,y), "type": "smooth/corner"}]
        self.points = [] 
        self.grad_points = [[0.4, 0.5], [0.6, 0.5]] # Start/End for gradient
        self.softness = 0.2 # For gradient

        # Render cache (avoid re-rasterizing unchanged shapes)
        self._render_cache = None
        self._render_cache_key = None
        self._shape_cache = None      # Cache for un-rotated shape (without rotation)
        self._shape_cache_key = None

    def _bitmap_fingerprint(self) -> tuple:
        """位图内容指纹（形状 + dtype + 紧凑摘要）。

        AUD-0700：缓存键原先用 ``id(bitmap_data)``——对象被 GC 后地址
        复用、或数组被原位修改时 id/shape 都不变，会命中陈旧蒙版缓存。
        内容摘要对两种情况都稳定失效；仅在蒙版重栅格化（mask_dirty）
        路径计算，代价远低于其替代的 resize/羽化光栅化本身。
        """
        data = self.bitmap_data
        if data is None:
            return ()
        contiguous = np.ascontiguousarray(data)
        digest = hashlib.blake2b(contiguous.tobytes(), digest_size=16).digest()
        return (contiguous.shape, contiguous.dtype.str, digest)

    def _get_render_cache_key(self, target_w: int, target_h: int, img_w: int) -> tuple:
        """计算完整渲染缓存键，包含所有参数（含旋转）。"""
        base = (
            target_w, target_h, img_w,
            self.type, self.feather, self.opacity,
            self.blur, self.clean_black, self.clean_white,
            self.invert, self.rotation, self.softness,
            tuple(self.center), tuple(self.size),
        )
        if self.type == MaskShape.GRADIENT:
            base += (tuple(tuple(p) for p in self.grad_points),)
        elif self.type == MaskShape.CURVE and self.points:
            base += (tuple(
                (tuple(p.get('pos', (0, 0))), tuple(p.get('c1', (0, 0))), tuple(p.get('c2', (0, 0))),
                 p.get('fi'), p.get('fo'))
                for p in self.points
            ),)
        elif self.type == MaskShape.BITMAP and self.bitmap_data is not None:
            base += (self._bitmap_fingerprint(),)
        elif self.type == MaskShape.BRUSH:
            base += (len(self.brush_strokes),
                     sum(len(s.get('points', [])) for s in self.brush_strokes),
                     self.brush_size, self.brush_feather,
                     self.brush_opacity, self.brush_flow)
        return base

    def _get_shape_cache_key(self, target_w: int, target_h: int, img_w: int) -> tuple:
        """计算形状缓存键（不含旋转和位置），用于移动/旋转优化。

        旋转变化时无需重新生成形状，只需对缓存的未旋转纹理做 warpAffine。
        位置变化时同样只需 warpAffine 平移，无需重建 QPainter 纹理。
        circle/rect 的 center 不参与缓存键，移动时只做快速平移。
        """
        # circle/rect: 排除 center，移动时通过 warpAffine 平移
        center_for_cache = (0.5, 0.5) if self.type in ('circle', 'rect') else tuple(self.center)
        base = (
            target_w, target_h, img_w,
            self.type, self.feather,
            self.blur, self.clean_black, self.clean_white,
            center_for_cache, tuple(self.size),
        )
        if self.type == MaskShape.CURVE and self.points:
            base += (tuple(
                (tuple(p.get('pos', (0, 0))), tuple(p.get('c1', (0, 0))), tuple(p.get('c2', (0, 0))),
                 p.get('fi'), p.get('fo'))
                for p in self.points
            ),)
        elif self.type == MaskShape.BITMAP and self.bitmap_data is not None:
            base += (self._bitmap_fingerprint(),)
        elif self.type == MaskShape.BRUSH:
            base += (len(self.brush_strokes),
                     sum(len(s.get('points', [])) for s in self.brush_strokes),
                     self.brush_size, self.brush_feather,
                     self.brush_opacity, self.brush_flow)
        return base

    @property
    def has_per_point_feather(self) -> bool:
        """Curve 是否有逐点羽化数据"""
        return self.type == 'curve' and any(
            p.get('fi') is not None or p.get('fo') is not None
            for p in self.points
        )

    def to_dict(self, assets_dir: str = None) -> dict:
        """
        Serialize the MaskShape to a dictionary.
        
        Args:
            assets_dir: Optional directory path to save bitmap data as PNG files.
                        If provided and shape is BITMAP type, saves bitmap_data to file.
        
        Returns:
            Dictionary containing the shape's data.
        """
        data = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "visible": self.visible,
            "invert": self.invert,
            "locked": self.locked,
            "operation": self.operation,
            "feather": self.feather,
            "opacity": self.opacity,
            "blur": self.blur,
            "clean_black": self.clean_black,
            "clean_white": self.clean_white,
            "center": self.center,
            "size": self.size,
            "rotation": self.rotation,
            "points": self.points,
            "grad_points": self.grad_points,
            "softness": self.softness
        }
        
        # Brush-specific data
        if self.type == MaskShape.BRUSH:
            data["brush_strokes"] = self.brush_strokes
            data["brush_size"] = self.brush_size
            data["brush_feather"] = self.brush_feather
            data["brush_opacity"] = self.brush_opacity
            data["brush_flow"] = self.brush_flow
        
        # Bitmap data: save to file if assets_dir is provided
        if self.type == self.BITMAP and self.bitmap_data is not None:
            if assets_dir and os.path.isdir(assets_dir):
                filename = f"{self.id}.png"
                filepath = os.path.join(assets_dir, filename)
                # Convert float32 [0,1] to uint8 [0,255] for saving
                bitmap_uint8 = (self.bitmap_data * 255).astype(np.uint8)
                # Use imencode + tofile to handle Unicode paths on Windows
                success, encoded_img = cv2.imencode('.png', bitmap_uint8)
                if success:
                    encoded_img.tofile(filepath)
                data["bitmap_file"] = filename
            # If no assets_dir, bitmap_data is not persisted (will be lost)
        
        return data
    
    @classmethod
    def from_dict(cls, data: dict, assets_dir: str = None) -> 'MaskShape':
        """
        Deserialize a MaskShape from a dictionary.
        
        Args:
            data: Dictionary containing the shape's data.
            assets_dir: Optional directory path to load bitmap data from PNG files.
                        If provided and shape is BITMAP type, loads bitmap_data from file.
        
        Returns:
            A MaskShape instance.

        Raises:
            ValueError: AUD-0701——外部工程数据缺少有效 ``type`` 字段或
                本体不是 dict。调用方（Layer.from_dict）按单项隔离捕获。
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"MaskShape 反序列化需要 dict，得到 {type(data).__name__}")
        shape_type = data.get("type")
        if not isinstance(shape_type, str) or not shape_type:
            raise ValueError(
                f"MaskShape 数据缺少有效 type 字段: {data.get('type')!r}")
        shape = cls(shape_type)
        shape.id = data.get("id", str(uuid.uuid4()))
        shape.name = data.get("name")
        shape.visible = data.get("visible", True)
        shape.invert = data.get("invert", False)
        shape.locked = data.get("locked", False)
        shape.operation = data.get("operation", cls.OP_ADD)
        shape.feather = data.get("feather", 50)
        shape.opacity = data.get("opacity", 1.0)
        shape.blur = data.get("blur", 0.0)
        shape.clean_black = data.get("clean_black", 0.0)
        shape.clean_white = data.get("clean_white", 0.0)
        shape.center = data.get("center", [0.5, 0.5])
        shape.size = data.get("size", [0.3, 0.3])
        shape.rotation = data.get("rotation", 0.0)
        shape.points = data.get("points", [])
        shape.grad_points = data.get("grad_points", [[0.4, 0.5], [0.6, 0.5]])
        shape.softness = data.get("softness", 0.2)
        
        # Load brush-specific data
        if shape.type == cls.BRUSH:
            shape.brush_strokes = data.get("brush_strokes", [])
            shape.brush_size = data.get("brush_size", 50)
            shape.brush_feather = data.get("brush_feather", 0)
            shape.brush_opacity = data.get("brush_opacity", 1.0)
            shape.brush_flow = data.get("brush_flow", 1.0)
        
        # Load bitmap data from file if assets_dir is provided
        if shape.type == cls.BITMAP and data.get("bitmap_file"):
            if assets_dir and os.path.isdir(assets_dir):
                filepath = os.path.join(assets_dir, data["bitmap_file"])
                if os.path.exists(filepath):
                    # Use np.fromfile + imdecode to handle Unicode paths on Windows
                    img_data = np.fromfile(filepath, dtype=np.uint8)
                    img = cv2.imdecode(img_data, cv2.IMREAD_GRAYSCALE)
                    if img is not None:
                        # Convert uint8 [0,255] to float32 [0,1]
                        shape.bitmap_data = (img / 255.0).astype(np.float32)
        
        return shape


class MaskGroup:
    """蒙版群组，支持嵌套子遮罩或子群组，可对整个组执行反转或布尔运算。
    
    Attributes:
        id: 唯一标识符
        name: 群组名称
        visible: 可见性
        invert: 群组级反转
        operation: 群组级布尔运算 (add/subtract/intersect)
        opacity: 群组级透明度 (0.0-1.0)
        children: 子元素列表 (可包含 MaskShape 或 MaskGroup)
        expanded: UI 展开状态
    """
    
    # 类型标识
    TYPE = "group"
    
    # 布尔运算常量 (与 MaskShape 共享)
    OP_ADD = "add"
    OP_SUBTRACT = "subtract"
    OP_INTERSECT = "intersect"
    
    def __init__(self, name: str = "Group") -> None:
        """创建空群组（子元素随后经 add_child 加入）。"""
        self.id: str = str(uuid.uuid4())
        self.name: str = name
        self.visible: bool = True
        self.invert: bool = False
        self.locked: bool = False  # P1-COLOR-09: 组级锁定（子形状画布编辑一并禁止）
        self.operation: str = self.OP_ADD
        self.opacity: float = 1.0
        self.children: List[Union['MaskShape', 'MaskGroup']] = []
        self.expanded: bool = True  # UI 展开状态
    
    def add_child(self, child: Union['MaskShape', 'MaskGroup']) -> None:
        """添加子元素到群组末尾。"""
        self.children.append(child)
    
    def remove_child(self, child_id: str) -> bool:
        """移除指定 ID 的子元素，返回是否成功。"""
        for i, child in enumerate(self.children):
            if child.id == child_id:
                self.children.pop(i)
                return True
        return False
    
    def get_child_by_id(self, child_id: str) -> Optional[Union['MaskShape', 'MaskGroup']]:
        """根据 ID 查找子元素 (递归)。"""
        for child in self.children:
            if child.id == child_id:
                return child
            if isinstance(child, MaskGroup):
                result = child.get_child_by_id(child_id)
                if result:
                    return result
        return None
    
    def to_dict(self, assets_dir: str = None) -> Dict[str, Any]:
        """序列化群组到字典 (递归序列化 children)。"""
        data = {
            "id": self.id,
            "type": self.TYPE,
            "name": self.name,
            "visible": self.visible,
            "invert": self.invert,
            "locked": self.locked,
            "operation": self.operation,
            "opacity": self.opacity,
            "expanded": self.expanded,
            "children": []
        }
        
        for child in self.children:
            if isinstance(child, MaskGroup):
                data["children"].append(child.to_dict(assets_dir=assets_dir))
            elif isinstance(child, MaskShape):
                data["children"].append(child.to_dict(assets_dir=assets_dir))
        
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], assets_dir: str = None) -> 'MaskGroup':
        """从字典反序列化群组 (递归反序列化 children)。

        AUD-0701：单个坏 child 只跳过并告警，不中断整个工程加载。
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"MaskGroup 反序列化需要 dict，得到 {type(data).__name__}")
        group = cls(data.get("name", "Group"))
        group.id = data.get("id", str(uuid.uuid4()))
        group.visible = data.get("visible", True)
        group.invert = data.get("invert", False)
        group.locked = data.get("locked", False)
        group.operation = data.get("operation", cls.OP_ADD)
        group.opacity = data.get("opacity", 1.0)
        group.expanded = data.get("expanded", True)

        # 递归反序列化子元素（逐项隔离坏数据）
        children_data = data.get("children", [])
        if isinstance(children_data, (list, tuple)):
            for child_data in children_data:
                try:
                    if not isinstance(child_data, dict):
                        raise ValueError(
                            f"child 需要 dict，得到 {type(child_data).__name__}")
                    child_type = child_data.get("type")
                    if child_type == cls.TYPE:
                        # 递归创建子群组
                        child = MaskGroup.from_dict(child_data, assets_dir=assets_dir)
                    else:
                        # 创建 MaskShape
                        child = MaskShape.from_dict(child_data, assets_dir=assets_dir)
                    group.children.append(child)
                except (ValueError, KeyError, TypeError) as e:
                    logger.warning(
                        "MaskGroup.from_dict: 跳过无法反序列化的子蒙版 (%s)", e)

        return group


# AUD-0704: 限定器（HSL Keyer）默认参数的唯一权威定义。``Layer.__init__``
# 与 ``reset_params`` 共用，保证"重置调色"与新建图层得到完全一致的
# qualifier 状态（旧版 reset 只复位 enabled，抠像区间残留）。
_DEFAULT_QUALIFIER_PARAMS: Dict[str, Any] = {
    "enabled": False,
    "hue_enabled": True,
    "hue_center": 0.5,
    "hue_width": 1.0,
    "hue_soft": 0.1,
    "sat_enabled": True,
    "sat_low": 0.0,
    "sat_high": 1.0,
    "sat_soft_low": 0.0,
    "sat_soft_high": 0.0,
    "lum_enabled": True,
    "lum_low": 0.0,
    "lum_high": 1.0,
    "lum_soft_low": 0.0,
    "lum_soft_high": 0.0,
    "blur": 0.0,
    "clean_black": 0.0,
    "clean_white": 0.0,
    "denoise": 0.0,
    "invert": False
}


class Layer:
    """调色图层：非破坏性调色链的一个节点（状态 + 序列化）。

    持有一组调色参数域（grading_params、curves、HSL 曲线、mixer、
    warper、effects、LUT、soft clip、qualifier、插件效果栈）、蒙版
    集合（mask_shapes，含群组）、AI matte、AI 烘焙缓存状态与调色
    关键帧轨道（grade_keyframes）。渲染结果不存于本类——
    ``is_dirty``/``mask_dirty`` 通知 AsyncRenderer 失效重算，
    ``cached_mask``/``cached_result`` 为渲染侧缓存槽。

    类常量 ``BLEND_MODES``：与 UI 一致的混合模式显示名列表。
    """

    # Blend Mode Definitions
    BLEND_MODES = [
        "Normal", "Multiply", "Screen", "Overlay", "Soft Light", "Hard Light",
        "Color Dodge", "Color Burn", "Darken", "Lighten", "Difference", "Exclusion",
        "Hue", "Saturation", "Color", "Luminosity", "Addition", "Subtract", "Divide"
    ]

    def __init__(self, name: str = "Layer"):
        """
        创建默认图层（全部调色参数为零值/禁用、无蒙版、标记脏）。

        Args:
            name: 图层显示名。
        """
        self.id = str(uuid.uuid4())
        self.name = name
        self.visible = True
        self.opacity = 1.0
        self.blend_mode = "Normal" # Display name from BLEND_MODES
        
        # Masking
        self.mask_shapes = []
        
        # Color Grading Params (Default values)
        # 注意：参数名与测试和 UI 保持一致
        self.grading_params = {
            "lift": (0, 0, 0),    # (h, s, b)
            "gamma": (0, 0, 0),
            "gain": (0, 0, 0),
            "exposure": 0,
            "temp": 0,            # 兼容旧名
            "temperature": 0,     # 标准名（与测试一致）
            "tint": 0,
            "contrast": 0,
            "shadow": 0,          # 兼容旧名
            "shadows": 0,         # 标准名（与测试一致）
            "highlights": 0,
            "saturation": 0,
            "vibrance": 0,
            "mid_detail": 0,
            "sharpness": 0,
            "hue_shift": 0,
            # 色调区间饱和度（-100..100，缺省 0 = 无效果）。
            # 与移动端 mega.frag tonal_saturation_adjust_g 对齐。
            "lift_saturation": 0,
            "gamma_saturation": 0,
            "gain_saturation": 0
        }
        
        self.curves_lut = None # np.ndarray (256, 1, 3) or (256, 1)
        self.curves_data = {} # { channel: [(x, y), ...] }
        
        # HSL Curve (6 curves: LumSat, HueHue, HueSat, HueLum, SatSat, SatLum)
        self.hsl_curve_lut = None # np.ndarray (6, 256) float32
        self.hsl_curve_data = {} # { 'LumSat': [(x, y), ...], ... }
        
        self.mixer_params = {} # { 'red': [h, s, l], ... }
        self.warper_params = None # np.ndarray grid
        self.effects_params = {
            'enabled': False,
            'mode': 0, 
            'sharpen': 0, 
            'texture': 0, 
            'halo': 0, 
            'radius': 0.0, 
            'iterations': 1
        }
        self.active_lut_path = None
        self.lut_intensity = 1.0
        
        # Soft Clip (Filmic Tone Mapping)
        self.soft_clip_params = {
            "enabled": False,
            "high": 1.0,
            "white_point": 1.1,
            "shoulder_strength": 1.0,
            "low": 0.0,
            "black_point": -0.1,
            "toe_strength": 1.0,
            "exposure": 0.0,
            "contrast": 0.0
        }

        # Qualifier (HSL Keyer)
        # AUD-0704: 与 reset_params 共用同一份默认值（模块级常量）
        self.qualifier_params = dict(_DEFAULT_QUALIFIER_PARAMS)
        
        # Render Caches
        self.cached_mask = None
        self.cached_result = None
        self.is_dirty = True # Needs re-render
        self.mask_dirty = True 

        # AI Matte (BiRefNet / SAM)
        self.ai_matte = None # numpy array (float32, 0.0-1.0)
        self.ai_matte_operation = "add" # add, subtract, intersect
        self.ai_matte_enabled = True

        # Plugin Support (Topos DCTL) - Effect Stack
        self.plugins = []  # List of plugin instances: [{"instance_id": str, "plugin_id": str, "params": dict, "enabled": bool}]

        # AI Baking (栅格化) - Cache heavy AI computations to EXR
        self.is_baked = False  # Whether this layer has been baked to a cached EXR file
        self.baked_file_name = None  # Cached EXR filename (e.g., "baked_layer_uuid.exr")
        self.baked_texture_id = None  # GPU texture ID for the baked result (managed by AsyncRenderer)

        # P1-COLOR-03: 调色参数关键帧轨道 {param_name: GradeKeyframeTrack}
        self.grade_keyframes: Dict[str, Any] = {}

    def reset_params(self) -> None:
        """把图层整体重置回 ``__init__`` 的默认状态。

        清空全部调色参数、曲线/LUT、mixer/warper/effects/LUT/软剪裁/
        限定器参数、蒙版形状、AI matte、烘焙缓存与关键帧轨道，并把
        is_dirty/mask_dirty 置 True。关键帧轨道一并清空，否则下一次
        seek 会经 apply_grade_keyframes 把已重置的调色值"复活"。
        """
        self.grading_params = {
            "lift": (0, 0, 0),
            "gamma": (0, 0, 0),
            "gain": (0, 0, 0),
            "exposure": 0,
            "temp": 0,            # 兼容旧名
            "temperature": 0,     # 标准名
            "tint": 0,
            "contrast": 0,
            "shadow": 0,          # 兼容旧名
            "shadows": 0,         # 标准名
            "highlights": 0,
            "saturation": 0,
            "vibrance": 0,
            "mid_detail": 0,
            "sharpness": 0,
            "hue_shift": 0,
            # 色调区间饱和度（-100..100，缺省 0 = 无效果）。
            # 与移动端 mega.frag tonal_saturation_adjust_g 对齐。
            "lift_saturation": 0,
            "gamma_saturation": 0,
            "gain_saturation": 0
        }
        # P1-COLOR-03: 重置连关键帧轨道一起清空，否则下一次 seek 会经
        # apply_grade_keyframes 把已重置的调色值"复活"。
        self.grade_keyframes = {}
        self.curves_lut = None
        self.curves_data = {}
        self.hsl_curve_lut = None
        self.hsl_curve_data = {}
        self.mixer_params = {}
        self.warper_params = None
        # 插件实例一并清空：v1 栈回退/reset 后残留上一状态会导致插件
        # 效果"复活"（结构键参与合成 shader 缓存，残留即静默生效）
        self.plugins = []
        self.effects_params = {
            'enabled': False,
            'mode': 0, 
            'sharpen': 0, 
            'texture': 0, 
            'halo': 0, 
            'radius': 0.0, 
            'iterations': 1
        }
        self.active_lut_path = None
        self.lut_intensity = 1.0
        self.soft_clip_params = {
            "enabled": False,
            "high": 1.0,
            "white_point": 1.1,
            "shoulder_strength": 1.0,
            "low": 0.0,
            "black_point": -0.1,
            "toe_strength": 1.0,
            "exposure": 0.0,
            "contrast": 0.0
        }
        # AUD-0704: qualifier 整组按默认重建（旧版只复位 enabled，
        # 抠像区间 hue_center/sat_low 等残留，下次启用即带旧值）
        self.qualifier_params = dict(_DEFAULT_QUALIFIER_PARAMS)

        # Clear all mask shapes
        self.mask_shapes.clear()
        
        # Clear AI matte data
        self.ai_matte = None
        self.ai_matte_enabled = True
        

        # Clear baked cache state
        self.is_baked = False
        self.baked_file_name = None
        self.baked_texture_id = None
        self.is_dirty = True
        self.mask_dirty = True

    def add_shape(self, shape_type: str) -> MaskShape:
        """新建一个 MaskShape 追加到蒙版列表（置脏）。

        Args:
            shape_type: MaskShape 类型常量之一。

        Returns:
            创建的 MaskShape 实例（含新分配的 uuid）。
        """
        shape = MaskShape(shape_type)
        self.mask_shapes.append(shape)
        self.is_dirty = True
        self.mask_dirty = True
        return shape
    
    def add_group(self, name: str = "Group") -> MaskGroup:
        """添加一个新的蒙版群组。
        
        Args:
            name: 群组名称
            
        Returns:
            创建的 MaskGroup 实例
        """
        group = MaskGroup(name)
        self.mask_shapes.append(group)
        self.is_dirty = True
        self.mask_dirty = True
        return group

    def remove_shape(self, shape_id: str) -> None:
        """按 ID 移除遮罩元素（递归穿透群组，置脏）。

        Args:
            shape_id: MaskShape 或 MaskGroup 的 id。
        """
        # 递归移除遮罩元素 (支持群组)
        self._remove_item_recursive(self.mask_shapes, shape_id)
        self.is_dirty = True
        self.mask_dirty = True
    
    def _remove_item_recursive(self, items: List, target_id: str) -> bool:
        """递归移除指定 ID 的遮罩元素。"""
        for i, item in enumerate(items):
            if item.id == target_id:
                items.pop(i)
                return True
            if isinstance(item, MaskGroup):
                if self._remove_item_recursive(item.children, target_id):
                    return True
        return False
    
    def get_mask_item_by_id(self, item_id: str) -> Optional[Union[MaskShape, MaskGroup]]:
        """根据 ID 递归查找遮罩元素 (支持群组)。"""
        for item in self.mask_shapes:
            if item.id == item_id:
                return item
            if isinstance(item, MaskGroup):
                result = item.get_child_by_id(item_id)
                if result:
                    return result
        return None

    def copy_params_from(self, source_layer: 'Layer') -> None:
        """从源图层复制所有调色参数到当前图层。
        
        复制的参数包括：
        - 基础调色参数 (grading_params)
        - 曲线数据 (curves_data, curves_lut)
        - HSL 曲线数据 (hsl_curve_data, hsl_curve_lut)
        - RGB Mixer 参数 (mixer_params)
        - 变形参数 (warper_params)
        - 效果参数 (effects_params)
        - LUT 设置 (active_lut_path, lut_intensity)
        - 软剪裁参数 (soft_clip_params)
        - 抠像参数 (qualifier_params)

        不复制的内容（计算密集型或无意义）：
        - 蒙版形状 (mask_shapes) - 包括 Remove BG、Smart Brush、Brush 等
        - AI Matte 数据 (ai_matte)
        - 插件效果栈 (plugins)
        - 胶片颗粒 (film_grain_params) - engine 级全局效果，不属于图层
          （AUD-0704：旧文档把其列入复制清单属误导，已删除死代码分支）

        Args:
            source_layer: 源图层，从中复制参数
        """
        import copy
        
        # 复制基础调色参数
        self.grading_params = copy.deepcopy(source_layer.grading_params)
        
        # 复制曲线数据
        self.curves_data = copy.deepcopy(source_layer.curves_data)
        if source_layer.curves_lut is not None:
            self.curves_lut = source_layer.curves_lut.copy()
        else:
            self.curves_lut = None
        
        # 复制 HSL 曲线数据
        self.hsl_curve_data = copy.deepcopy(source_layer.hsl_curve_data)
        if source_layer.hsl_curve_lut is not None:
            self.hsl_curve_lut = source_layer.hsl_curve_lut.copy()
        else:
            self.hsl_curve_lut = None
        
        # 复制 RGB Mixer 参数
        self.mixer_params = copy.deepcopy(source_layer.mixer_params)
        
        # 复制变形参数
        if source_layer.warper_params is not None:
            self.warper_params = source_layer.warper_params.copy()
        else:
            self.warper_params = None
        
        # 复制效果参数
        self.effects_params = copy.deepcopy(source_layer.effects_params)
        
        # 复制 LUT 设置
        self.active_lut_path = source_layer.active_lut_path
        self.lut_intensity = source_layer.lut_intensity
        
        # 复制软剪裁参数
        self.soft_clip_params = copy.deepcopy(source_layer.soft_clip_params)
        
        # 复制抠像参数
        self.qualifier_params = copy.deepcopy(source_layer.qualifier_params)

        # 复制插件效果栈
        if hasattr(source_layer, 'plugins') and source_layer.plugins:
            self.plugins = copy.deepcopy(source_layer.plugins)

        # AUD-0704: 已删除 film_grain_params 分支——该属性从不在 Layer 上
        # 初始化（胶片颗粒是 engine 级全局效果，见 ImageEngine.406），
        # hasattr 恒 False，属于永不执行的死代码。

        # P1-COLOR-03: 复制调色关键帧轨道（Paste Grade 含动画）
        if getattr(source_layer, 'grade_keyframes', None):
            from src.features.color.core.grade_keyframes import GradeKeyframeTrack
            self.grade_keyframes = {
                name: (copy.deepcopy(track) if isinstance(track, GradeKeyframeTrack)
                       else GradeKeyframeTrack.from_dict(track))
                for name, track in source_layer.grade_keyframes.items()
            }

        # 复制图层属性
        if hasattr(source_layer, 'blend_mode'):
            self.blend_mode = source_layer.blend_mode
        if hasattr(source_layer, 'opacity'):
            self.opacity = source_layer.opacity

        # 标记需要重新渲染
        self.is_dirty = True
        self.mask_dirty = True

    def apply_grade_patch(self, patch: dict, categories: Iterable[str]) -> None:
        """按类别白名单应用一个 grade 补丁（P1-COLOR-16 Paste Attributes）。

        只修改 ``categories`` 中类别对应的字段（映射见
        ``grade_clipboard.GRADE_CATEGORY_KEYS``），未列出的类别保持原值。
        字段解析与 ``from_dict`` 同路径（tuple 重建、曲线 LUT 重算、
        warper ndarray 恢复、plugins 旧格式迁移）。

        Args:
            patch: ``to_dict`` 产物的类别子集（通常来自 GradeClipboard）。
            categories: 要应用的类别键集合。
        """
        from src.features.color.core.grade_clipboard import GRADE_CATEGORY_KEYS

        wanted = set()
        for cat in categories:
            wanted.update(GRADE_CATEGORY_KEYS.get(cat, ()))
        if not wanted:
            return

        if "grading_params" in wanted and "grading_params" in patch:
            for k, v in patch["grading_params"].items():
                if k in ("lift", "gamma", "gain") and isinstance(v, list):
                    self.grading_params[k] = tuple(v)
                else:
                    self.grading_params[k] = v

        if "mixer_params" in wanted and "mixer_params" in patch:
            self.mixer_params = copy.deepcopy(patch["mixer_params"])

        if "effects_params" in wanted and "effects_params" in patch:
            self.effects_params = copy.deepcopy(patch["effects_params"])

        if "active_lut_path" in wanted and "active_lut_path" in patch:
            self.active_lut_path = patch["active_lut_path"]
            self.lut_intensity = patch.get("lut_intensity", 1.0)

        if "qualifier_params" in wanted and "qualifier_params" in patch:
            self.qualifier_params.update(copy.deepcopy(patch["qualifier_params"]))

        if "soft_clip_params" in wanted and "soft_clip_params" in patch:
            self.soft_clip_params.update(copy.deepcopy(patch["soft_clip_params"]))

        if "curves_data" in wanted and "curves_data" in patch:
            self.curves_data = copy.deepcopy(patch["curves_data"])
            self.curves_lut = build_curves_lut_from_data(self.curves_data)

        if "hsl_curve_data" in wanted and "hsl_curve_data" in patch:
            self.hsl_curve_data = copy.deepcopy(patch["hsl_curve_data"])
            self.hsl_curve_lut = build_hsl_curve_lut_from_data(self.hsl_curve_data)

        if "warper_params" in wanted and "warper_params" in patch:
            if patch["warper_params"] is not None:
                self.warper_params = np.array(patch["warper_params"], dtype=np.float32)
            else:
                self.warper_params = None

        if "mask_shapes" in wanted and "mask_shapes" in patch:
            self.mask_shapes = [
                (MaskGroup.from_dict(d) if d.get("type") == MaskGroup.TYPE
                 else MaskShape.from_dict(d))
                for d in patch["mask_shapes"]
            ]

        if "ai_matte_file" in wanted:
            self.ai_matte_operation = patch.get("ai_matte_operation", "add")
            self.ai_matte_enabled = patch.get("ai_matte_enabled", True)
            # ai_matte 二进制本体不经剪贴板跨 clip 携带（与 to_dict/assets_dir
            # 契约一致）；文件名保留，加载由水合路径按 assets_dir 解析。
            if patch.get("ai_matte_file"):
                self.ai_matte_file = patch["ai_matte_file"]

        if "plugins" in wanted and "plugins" in patch:
            plugins = copy.deepcopy(patch["plugins"])
            for plugin in plugins:
                if "type" not in plugin:
                    plugin["type"] = "dctl"
            self.plugins = plugins

        # 标记需要重新渲染
        self.is_dirty = True
        self.mask_dirty = True

    def has_grading_effects(self) -> bool:
        """检查图层是否有调色效果。

        只在以下工具有调整时才显示图标：
        - 色轮 (Lift, Gamma, Gain, Exposure, Temp, Tint, Contrast, Shadow, Saturation, Vibrance, Mid Detail, Hue Shift)
        - 曲线 (RGB Curves)
        - 柔化裁切 (Soft Clip)
        - 混色器 (RGB Mixer)
        - 色彩扭曲器 (Warper)

        Returns:
            bool: 是否有调色效果
        """
        # 检查色轮参数（默认值都是 0 或 (0,0,0)）
        for key in ["lift", "gamma", "gain"]:
            val = self.grading_params.get(key, (0, 0, 0))
            if val != (0, 0, 0):
                return True
        for key in ["exposure", "temp", "tint", "contrast", "shadow", "highlights", "saturation", "vibrance", "mid_detail", "sharpness", "hue_shift"]:
            if self.grading_params.get(key, 0) != 0:
                return True

        # 检查曲线数据 - 只有当有实际的曲线数据时才认为有调色
        # RGB 曲线默认值是 [(0, 255), (255, 0)]（像素坐标，从左上到右下的直线）
        has_non_default_curves = False
        if self.curves_data and len(self.curves_data) > 0:
            for channel, points in self.curves_data.items():
                if points and len(points) > 0:
                    # 默认曲线是两个点：(0, 255) 和 (255, 0)
                    if len(points) > 2:
                        has_non_default_curves = True
                        break
                    if len(points) == 2:
                        # 检查是否是默认的直线（允许浮点误差）
                        p0, p1 = points[0], points[1]
                        if not (abs(p0[0] - 0) < 1 and abs(p0[1] - 255) < 1 and
                               abs(p1[0] - 255) < 1 and abs(p1[1] - 0) < 1):
                            has_non_default_curves = True
                            break
            if has_non_default_curves:
                return True

        # 检查 curves_lut 是否是非默认值（线性 LUT）
        # 默认线性 LUT: [0/255, 1/255, ..., 255/255]
        if self.curves_lut is not None:
            lut = self.curves_lut
            # curves_lut 形状为 (256, 1, 3) BGR 或 (256, 1)
            if lut.shape[0] == 256:
                # 检查是否是线性 LUT（允许浮点误差）
                is_linear = True
                for i in range(256):
                    expected = i / 255.0
                    # 检查所有通道
                    if lut.ndim == 3:
                        for c in range(lut.shape[2]):
                            if abs(lut[i, 0, c] - expected) > 0.001:
                                is_linear = False
                                break
                    else:
                        if abs(lut[i, 0] - expected) > 0.001:
                            is_linear = False
                            break
                    if not is_linear:
                        break
                if not is_linear:
                    return True

        # 检查 RGB Mixer 参数 - 默认是空字典，有内容才认为有调色
        if self.mixer_params and len(self.mixer_params) > 0:
            for channel, values in self.mixer_params.items():
                if values and len(values) >= 3:
                    # 所有通道的默认值都是 (0, 0, 0)
                    if tuple(values[:3]) != (0, 0, 0) and list(values[:3]) != [0, 0, 0]:
                        return True

        # 检查色彩扭曲器参数
        if self.warper_params is not None:
            return True

        # 检查柔化裁切参数
        if self.soft_clip_params.get("enabled", False):
            return True
        # 检查其他柔化裁切参数是否被修改（排除默认值）
        soft_clip_defaults = {
            "high": 1.0, "white_point": 1.1, "shoulder_strength": 1.0,
            "low": 0.0, "black_point": -0.1, "toe_strength": 1.0,
            "exposure": 0.0, "contrast": 0.0
        }
        for key, default_val in soft_clip_defaults.items():
            if self.soft_clip_params.get(key, default_val) != default_val:
                return True

        return False

    # Plugin Effect Stack Methods
    def add_plugin(self, plugin_id: str, default_params: Optional[dict] = None) -> str:
        """Add a new plugin instance to the effect stack.

        Args:
            plugin_id: The plugin identifier (filename without extension)
            default_params: Optional default parameter values

        Returns:
            The generated instance_id for the new plugin instance
        """
        instance_id = str(uuid.uuid4())
        # Determine plugin type from PluginManager
        from src.features.color.core.plugin_manager import get_plugin_manager
        plugin_data = get_plugin_manager().get_plugin(plugin_id)
        plugin_type = plugin_data.get("type", "dctl") if plugin_data else "dctl"

        self.plugins.append({
            "instance_id": instance_id,
            "plugin_id": plugin_id,
            "params": default_params or {},
            "enabled": True,
            "type": plugin_type
        })
        self.is_dirty = True
        return instance_id

    def remove_plugin(self, instance_id: str) -> None:
        """Remove a plugin instance from the effect stack.
        
        Args:
            instance_id: The instance_id of the plugin to remove
        """
        self.plugins = [p for p in self.plugins if p["instance_id"] != instance_id]
        self.is_dirty = True

    def get_plugin_instance(self, instance_id: str) -> Optional[dict]:
        """Get a plugin instance by its instance_id.
        
        Args:
            instance_id: The instance_id to search for
            
        Returns:
            The plugin dictionary or None if not found
        """
        for plugin in self.plugins:
            if plugin["instance_id"] == instance_id:
                return plugin
        return None

    def set_plugin_enabled(self, instance_id: str, enabled: bool) -> None:
        """Set the enabled state of a plugin instance.

        Args:
            instance_id: The instance_id of the plugin
            enabled: Boolean enabled state
        """
        plugin = self.get_plugin_instance(instance_id)
        if plugin:
            plugin["enabled"] = enabled
            self.is_dirty = True

    def get_ai_plugins(self) -> List[dict]:
        """Return list of enabled AI plugin instances."""
        return [p for p in self.plugins if p.get("type") == "ai" and p.get("enabled", True)]

    def get_dctl_plugins(self) -> List[dict]:
        """Return list of enabled DCTL plugin instances."""
        return [p for p in self.plugins if p.get("type") == "dctl" and p.get("enabled", True)]

    def to_dict(self, assets_dir: str = None) -> dict:
        """
        Serialize the Layer to a dictionary.
        
        Args:
            assets_dir: Optional directory path to save binary data (bitmaps, AI mattes) as PNG files.
                        If provided, saves mask shape bitmaps and AI matte to files.
        
        Returns:
            Dictionary containing the layer's data.
        """
        data = {
            "id": self.id,
            "name": self.name,
            "visible": self.visible,
            "opacity": self.opacity,
            "blend_mode": self.blend_mode,
            "grading_params": self.grading_params,
            "mixer_params": self.mixer_params,
            "effects_params": self.effects_params,
            "active_lut_path": self.active_lut_path,
            "lut_intensity": self.lut_intensity,
            "soft_clip_params": self.soft_clip_params,
            "qualifier_params": self.qualifier_params,
            "mask_shapes": [s.to_dict(assets_dir=assets_dir) for s in self.mask_shapes],
            "ai_matte_operation": self.ai_matte_operation,
            "ai_matte_enabled": self.ai_matte_enabled
        }
        
        # We only save curves_data (control points).
        # curves_lut is a high-precision generated cache and should be recalculated on load.
        if self.curves_data:
            data["curves_data"] = self.curves_data

        # HSL Curve data (control points)
        if self.hsl_curve_data:
            data["hsl_curve_data"] = self.hsl_curve_data

        if self.warper_params is not None:
            # Convert numpy array to list
            if hasattr(self.warper_params, "tolist"):
                data["warper_params"] = self.warper_params.tolist()
            else:
                data["warper_params"] = self.warper_params # Assuming already list/dict if not ndarray
        
        # Plugin Effect Stack
        data["plugins"] = self.plugins

        # P1-COLOR-03: 调色关键帧轨道（仅在有轨道时序列化）
        if self.grade_keyframes:
            data["grade_keyframes"] = {
                name: track.to_dict()
                for name, track in self.grade_keyframes.items()
            }

        # AI Matte: save to file if assets_dir is provided
        if self.ai_matte is not None and assets_dir and os.path.isdir(assets_dir):
            filename = f"ai_matte_{self.id}.png"
            filepath = os.path.join(assets_dir, filename)
            # Convert float32 [0,1] to uint8 [0,255] for saving
            ai_matte_uint8 = (self.ai_matte * 255).astype(np.uint8)
            # Use imencode + tofile to handle Unicode paths on Windows
            success, encoded_img = cv2.imencode('.png', ai_matte_uint8)
            if success:
                encoded_img.tofile(filepath)
            data["ai_matte_file"] = filename

        # AI Baking: save baked_file_name for project persistence
        if self.is_baked and self.baked_file_name:
            data["is_baked"] = True
            data["baked_file_name"] = self.baked_file_name

        return data

    @classmethod
    def from_dict(cls, data: dict, assets_dir: str = None) -> 'Layer':
        """
        Deserialize a Layer from a dictionary.
        
        Args:
            data: Dictionary containing the layer's data.
            assets_dir: Optional directory path to load binary data (bitmaps, AI mattes) from PNG files.
                        If provided, loads mask shape bitmaps and AI matte from files.
        
        Returns:
            A Layer instance.
        """
        layer = cls(data.get("name", "Layer"))
        layer.id = data.get("id", str(uuid.uuid4()))
        layer.visible = data.get("visible", True)
        layer.opacity = data.get("opacity", 1.0)
        # Handle case sensitivity from previous versions if any
        # AUD-0701：blend_mode 可能被外部改成 None/数字——旧实现直接
        # bm.lower() 抛 AttributeError（无诊断信息的裸崩溃）。保持"层
        # 级结构损坏即抛错"的既有契约（上层按项隔离：color_stack_hydrator
        # 逐条捕获丢弃，AUD-24200），但升级为带明确信息的 ValueError。
        bm = data.get("blend_mode", "Normal")
        if not isinstance(bm, str):
            raise ValueError(
                f"Layer 反序列化：blend_mode 必须是 str，得到 "
                f"{type(bm).__name__} ({bm!r})")
        layer.blend_mode = bm.title() if bm.lower() != "normal" else "Normal"
        if layer.blend_mode not in cls.BLEND_MODES:
            layer.blend_mode = "Normal"
            
        # Load Grading
        if "grading_params" in data:
            # Ensure tuples are converted back from lists (JSON stores tuples as lists)
            # We merge with default params to ensure all keys exist
            gp = data["grading_params"]
            if not isinstance(gp, dict):
                logger.warning(
                    "Layer.from_dict: grading_params 结构非法（%s），忽略",
                    type(gp).__name__)
                gp = {}
            for k, v in gp.items():
                if k in ["lift", "gamma", "gain"] and isinstance(v, list):
                    layer.grading_params[k] = tuple(v)
                else:
                    layer.grading_params[k] = v
            
        layer.mixer_params = data.get("mixer_params", {})
        layer.effects_params = data.get("effects_params", {
            'enabled': False,
            'mode': 0, 'sharpen': 0, 'texture': 0, 'halo': 0, 'radius': 0.0, 'iterations': 1
        })
        layer.active_lut_path = data.get("active_lut_path")
        layer.lut_intensity = data.get("lut_intensity", 1.0)
        
        # Qualifier
        if "qualifier_params" in data:
            layer.qualifier_params.update(data["qualifier_params"])
            
        # Soft Clip
        if "soft_clip_params" in data:
            layer.soft_clip_params.update(data["soft_clip_params"])

        # Curves
        # AUD-0701：curves_data 是外部数据，容器/控制点结构非法时局部
        # 降级（不构建 LUT），不让整个 from_dict 崩溃。
        if "curves_data" in data:
            curves_data = data["curves_data"]
            if isinstance(curves_data, dict):
                layer.curves_data = curves_data
                layer.curves_lut = build_curves_lut_from_data(curves_data)
            else:
                logger.warning(
                    "Layer.from_dict: curves_data 结构非法（%s），忽略曲线",
                    type(curves_data).__name__)
        elif "curves_lut" in data:
            layer.curves_lut = np.array(data["curves_lut"], dtype=np.float32)
            if np.max(layer.curves_lut) > 1.1:
                layer.curves_lut /= 255.0

        # HSL Curve
        if "hsl_curve_data" in data:
            hsl_curve_data = data["hsl_curve_data"]
            if isinstance(hsl_curve_data, dict):
                layer.hsl_curve_data = hsl_curve_data
                layer.hsl_curve_lut = build_hsl_curve_lut_from_data(hsl_curve_data)
            else:
                logger.warning(
                    "Layer.from_dict: hsl_curve_data 结构非法（%s），忽略 HSL 曲线",
                    type(hsl_curve_data).__name__)

        # Warper
        if "warper_params" in data:
            layer.warper_params = np.array(data["warper_params"], dtype=np.float32)
            
        # Masks - support both MaskShape and MaskGroup (递归反序列化)
        # AUD-0701：单项蒙版结构非法只跳过并告警（局部降级），不再让
        # 整个工程在加载路径抛未捕获异常。
        if "mask_shapes" in data:
            shapes_data = data["mask_shapes"]
            if isinstance(shapes_data, (list, tuple)):
                for item_data in shapes_data:
                    try:
                        if not isinstance(item_data, dict):
                            raise ValueError(
                                f"mask_shapes 项需要 dict，得到 {type(item_data).__name__}")
                        item_type = item_data.get("type")
                        if item_type == MaskGroup.TYPE:
                            layer.mask_shapes.append(
                                MaskGroup.from_dict(item_data, assets_dir=assets_dir))
                        else:
                            layer.mask_shapes.append(
                                MaskShape.from_dict(item_data, assets_dir=assets_dir))
                    except (ValueError, KeyError, TypeError) as e:
                        logger.warning(
                            "Layer.from_dict: 跳过无法反序列化的蒙版 (%s)", e)
            else:
                logger.warning(
                    "Layer.from_dict: mask_shapes 结构非法（%s），忽略蒙版",
                    type(shapes_data).__name__)
            
        # AI Matte Params
        layer.ai_matte_operation = data.get("ai_matte_operation", "add")
        layer.ai_matte_enabled = data.get("ai_matte_enabled", True)
        
        # AI Matte: load from file if assets_dir is provided
        if data.get("ai_matte_file") and assets_dir and os.path.isdir(assets_dir):
            filepath = os.path.join(assets_dir, data["ai_matte_file"])
            if os.path.exists(filepath):
                # Use np.fromfile + imdecode to handle Unicode paths on Windows
                img_data = np.fromfile(filepath, dtype=np.uint8)
                img = cv2.imdecode(img_data, cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    # Convert uint8 [0,255] to float32 [0,1]
                    layer.ai_matte = (img / 255.0).astype(np.float32)

        # Plugin Effect Stack
        layer.plugins = data.get("plugins", [])

        # P1-COLOR-03: 调色关键帧轨道
        kf_data = data.get("grade_keyframes") or {}
        if kf_data:
            from src.features.color.core.grade_keyframes import GradeKeyframeTrack
            layer.grade_keyframes = {
                str(name): GradeKeyframeTrack.from_dict(track_data)
                for name, track_data in kf_data.items()
            }

        # Backward Compatibility: Add type field to old plugin instances
        for plugin in layer.plugins:
            if "type" not in plugin:
                plugin["type"] = "dctl"
        
        # Backward Compatibility: Migrate old single-plugin format to new stack format
        if not layer.plugins and data.get("plugin_id"):
            layer.plugins.append({
                "instance_id": str(uuid.uuid4()),
                "plugin_id": data.get("plugin_id"),
                "params": data.get("plugin_params", {}),
                "enabled": True
            })

        # AI Baking: load baked state from project data
        # Note: baked_texture_id will be set by AsyncRenderer when loading the EXR file
        layer.is_baked = data.get("is_baked", False)
        layer.baked_file_name = data.get("baked_file_name", None)

        return layer


# ============================================================
# Helper functions to rebuild LUTs from curve control points
# ============================================================

def _sanitize_control_points(pts: Any) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """清洗外部曲线控制点（AUD-0701/0702）。

    - 只接受 list/tuple/ndarray 容器；逐点要求可索引且前两个分量可转
      float，坏点丢弃（p 不是二元组时不再抛 TypeError）；
    - 按 x 升序排序并去掉重复 x（保留首个）——PchipInterpolator 要求
      xs 严格递增，重复 x 在装有 scipy 的机器上抛 ValueError、无
      scipy 时 np.interp 容忍，行为依赖运行环境；规整后两条插值路径
      输入一致。

    Returns:
        (xs, ys) float32 数组；有效点不足 2 个（含去重后）返回 None，
        调用方按"该通道无曲线"处理。
    """
    if not isinstance(pts, (list, tuple, np.ndarray)):
        return None
    clean: List[Tuple[float, float]] = []
    for p in pts:
        if isinstance(p, (list, tuple, np.ndarray)) and len(p) >= 2:
            try:
                clean.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    if len(clean) < 2:
        return None
    clean.sort(key=lambda t: t[0])
    xs_list: List[float] = []
    ys_list: List[float] = []
    for x, y in clean:
        if xs_list and x == xs_list[-1]:
            continue  # AUD-0702：重复 x 去重，保证严格递增
        xs_list.append(x)
        ys_list.append(y)
    if len(xs_list) < 2:
        return None
    return (np.array(xs_list, dtype=np.float32),
            np.array(ys_list, dtype=np.float32))


def build_curves_lut_from_data(curves_data: dict) -> np.ndarray:
    """Build a (256, 1, 3) LUT from curves control points data.

    Args:
        curves_data: Dictionary with keys 'M', 'R', 'G', 'B' containing
                     lists of (x, y) control points in 0-255 range, in the
                     CurvesWidget convention **including its inverted Y
                     axis** (UI screen coordinates): y=0 is the top
                     (maximum output, white), y=255 the bottom (black);
                     the identity curve is ``[(0, 255), (255, 0)]``.

    Returns:
        LUT array of shape (256, 1, 3) with values normalized to 0.0-1.0,
        derived as ``(255.0 - y) / 255.0`` exactly like
        ``CurvesWidget._compute_lut`` (src/shared/ui/widgets/curves_widget.py)
        and ``scripting.api._interpolate_curve_255`` (AUD-25900).
    """
    lut = np.zeros((256, 1, 3), dtype=np.float32)
    channels = [('M', 0), ('R', 0), ('G', 1), ('B', 2)]

    if not isinstance(curves_data, dict):
        return lut

    for ch_name, lut_ch in channels:
        sanitized = _sanitize_control_points(curves_data.get(ch_name))
        if sanitized is None:
            continue
        xs, ys = sanitized

        if len(xs) == 2:
            slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
            # AUD-R2-001: y 是 UI 倒置坐标，先翻转再归一化（与
            # CurvesWidget._compute_lut / _interpolate_curve_255 一致），
            # 旧实现 y/255 会把工程恢复的曲线整体反相。
            result = (
                255.0 - (ys[0] + slope * (np.arange(256, dtype=np.float32) - xs[0]))
            ) / 255.0
        elif HAS_SCIPY:
            pchip = PchipInterpolator(xs, ys)
            xi = np.arange(256, dtype=np.float32)
            result = (255.0 - pchip(xi)) / 255.0
        else:
            xi = np.arange(256, dtype=np.float32)
            result = (255.0 - np.interp(xi, xs, ys)) / 255.0

        result = np.clip(result, 0.0, 1.0)

        if ch_name == 'M':
            lut[:, 0, 0] = result
            lut[:, 0, 1] = result
            lut[:, 0, 2] = result
        else:
            lut[:, 0, lut_ch] = result

    return lut


def build_hsl_curve_lut_from_data(hsl_curve_data: dict) -> np.ndarray:
    """Build a (6, 256) HSL curve LUT from control points data.
    
    Args:
        hsl_curve_data: Dictionary with keys 'LumSat', 'HueHue', 'HueSat', 
                        'HueLum', 'SatSat', 'SatLum' containing lists of 
                        (x, y) control points in 0-255 range.
    
    Returns:
        LUT array of shape (6, 256) float32
        Index order: LumSat=0, HueHue=1, HueSat=2, HueLum=3, SatSat=4, SatLum=5
        - Multiplier types: values in 0.0-2.0 range (default 1.0)
        - Shift type (HueHue): values in -0.5 to +0.5 range (default 0.0)
    """
    mode_order = ['LumSat', 'HueHue', 'HueSat', 'HueLum', 'SatSat', 'SatLum']
    lut = np.zeros((6, 256), dtype=np.float32)

    if not isinstance(hsl_curve_data, dict):
        # AUD-0701：容器非法时按"全部默认曲线"处理（乘子 1.0 / HueHue 0）
        hsl_curve_data = {}

    for i, mode in enumerate(mode_order):
        sanitized = _sanitize_control_points(hsl_curve_data.get(mode))
        if sanitized is not None:
            xs, ys = sanitized
            if len(xs) == 2:
                slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
                curve_y = ys[0] + slope * (np.arange(256, dtype=np.float32) - xs[0])
            elif HAS_SCIPY:
                pchip = PchipInterpolator(xs, ys)
                curve_y = pchip(np.arange(256, dtype=np.float32))
            else:
                curve_y = np.interp(np.arange(256, dtype=np.float32), xs, ys)

            if mode == 'HueHue':
                lut[i] = (127.5 - curve_y) / 255.0
            else:
                lut[i] = (255.0 - curve_y) / 127.5
        else:
            if mode == 'HueHue':
                lut[i] = 0.0
            else:
                lut[i] = 1.0

    return lut
