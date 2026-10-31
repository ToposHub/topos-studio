import sys
import json
import re
import struct
import os
import platform
from datetime import datetime
from pathlib import Path

from src.shared.i18n.i18n import I18N

# 配置文件名（色板/历史记录等用户偏好数据）
CONFIG_FILE_NAME = "color_picker_swatches.json"


def _get_user_config_dir() -> Path:
    """返回项目统一的用户配置目录（跨平台）。

    与 config_manager.ConfigManager.get_default_config_path() 保持一致：
      - macOS:  ~/Library/Application Support/Topos/
      - Windows: %APPDATA%/Topos/
      - Linux:   $XDG_CONFIG_HOME/topos/ 或 ~/.config/topos/

    这样色彩色板数据和 config.json 放在同一个目录下，便于用户备份和迁移。
    """
    override = os.environ.get("TOPOS_CONFIG_PATH")
    if override:
        return Path(override).parent

    system_name = platform.system()
    app_name = "Topos"
    if system_name == "Darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    elif system_name == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / app_name if appdata else Path.home() / "AppData" / "Roaming" / app_name
    else:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        return Path(xdg_config_home) / "topos" if xdg_config_home else Path.home() / ".config" / "topos"


# 配置文件路径：与项目主配置 (config.json) 同目录
CONFIG_FILE_PATH = _get_user_config_dir() / CONFIG_FILE_NAME

def load_config():
    """加载配置文件，如果不存在则返回None"""
    try:
        if CONFIG_FILE_PATH.exists():
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载配置文件失败: {e}")
    return None

def save_config(swatch_data, last_color="#FF0000", picker_custom_swatches=None, color_history=None):
    """保存配置文件"""
    try:
        CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        config = {
            "name": "Color Inspector User Config",
            "version": "2.0",
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_color": last_color,
            "swatches": swatch_data,
            "picker_custom_swatches": picker_custom_swatches or ["#333333"] * 20,
            "color_history": color_history or []
        }
        
        with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"配置已保存到: {CONFIG_FILE_PATH}")
    except Exception as e:
        print(f"保存配置文件失败: {e}")


# ============================================================
# --- 外部数据结构校验（AUD-32403 / AUD-32407） ---
# ============================================================
def _is_valid_hex_color(value: object) -> bool:
    """颜色值必须是本模块写入的 #RRGGBB hex 字符串。"""
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None


def _validate_swatch_rows(rows: object) -> list | None:
    """校验色板数据结构（来源：导入的 JSON / 用户配置文件，均为外部数据）。

    Returns:
        结构合法时返回归一化的 ``[{"name": str, "colors": [hex, ...],
        "locked": bool}]`` 列表；任何一项不合法返回 ``None``——调用方据此
        拒绝导入（保留原数据与 UI 一致）或回退默认色板，绝不让畸形数据
        进入 ``_create_swatch_rows``（缺键会 KeyError、UI 半构建）。
    """
    if not isinstance(rows, list) or not rows:
        return None
    if len(rows) > 64:  # 防御性上限（默认 8 行，留足余量）
        return None
    normalized: list = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        name = row.get("name")
        colors = row.get("colors")
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(colors, list) or len(colors) > 128:
            return None
        if not all(_is_valid_hex_color(c) for c in colors):
            return None
        normalized.append({
            "name": name,
            "colors": list(colors),
            "locked": bool(row.get("locked", False)),
        })
    return normalized


from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QPushButton, QGridLayout, QFrame, QScrollArea,
                             QFileDialog, QMessageBox, QDialog, QSizePolicy,
                             QMenu, QColorDialog, QStackedWidget, QComboBox)
from PyQt6.QtGui import (QPainter, QColor, QLinearGradient, QPen,
                         QCursor, QPixmap, QFont, QAction, QDrag, QImage)
from PyQt6.QtCore import (
    Qt, QPoint, pyqtSignal, QRectF, QRect, QMimeData, QTimer,
)
from PyQt6.QtSvg import QSvgRenderer

from .color_conversion import (
    linear_to_srgb as _linear_to_srgb,
    srgb_to_linear as _srgb_to_linear,
)

# ============================================================
# --- WCAG 对比度计算模块 ---
# ============================================================
def srgb_to_linear(c):
    """sRGB 值转换为线性空间"""
    return _srgb_to_linear(c)

def get_relative_luminance(r, g, b):
    """计算 WCAG 相对亮度"""
    rs, gs, bs = srgb_to_linear(r/255.0), srgb_to_linear(g/255.0), srgb_to_linear(b/255.0)
    return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs

def get_contrast_ratio(color1, color2):
    """计算两色对比度比率"""
    l1 = get_relative_luminance(color1.red(), color1.green(), color1.blue())
    l2 = get_relative_luminance(color2.red(), color2.green(), color2.blue())
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)

def get_wcag_level(contrast_ratio):
    """判断 WCAG 等级"""
    if contrast_ratio >= 7.0:
        return "AAA", "#22C55E"
    elif contrast_ratio >= 4.5:
        return "AA", "#3B82F6"
    elif contrast_ratio >= 3.0:
        return "AA Large", "#F59E0B"
    else:
        return "Fail", "#EF4444"

def find_minimum_l_for_contrast(base_color, target_ratio=4.5, is_text_dark=True):
    """自动调整亮度以满足对比度要求"""
    r, g, b = base_color.red(), base_color.green(), base_color.blue()
    L, a, b_lab = rgb_to_lab(r, g, b)
    
    if is_text_dark:
        bg_color = QColor(255, 255, 255)
    else:
        bg_color = QColor(0, 0, 0)
    
    for delta in range(-50, 51):
        new_L = L + delta
        if new_L < 0 or new_L > 100:
            continue
        new_r, new_g, new_b = lab_to_rgb(new_L, a, b_lab)
        new_color = QColor(new_r, new_g, new_b)
        ratio = get_contrast_ratio(new_color, bg_color)
        if ratio >= target_ratio:
            return new_color, ratio, new_L
    
    return None, 0, 0

# ============================================================
# --- 色彩空间管理模块 (CMS) ---
# ============================================================

# 色彩空间定义
COLOR_SPACES = {
    "sRGB": {"name": "sRGB", "gamma": 2.2, "white": "D65"},
    "Display P3": {"name": "Display P3", "gamma": 2.2, "white": "D65"},
    "Rec.709": {"name": "Rec.709", "gamma": 2.4, "white": "D65"},
    "ACEScg": {"name": "ACEScg", "gamma": 1.0, "white": "D60"},
}

# sRGB to XYZ 矩阵 (D65)
SRGB_TO_XYZ = [
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041]
]

# XYZ to sRGB 矩阵 (D65)
XYZ_TO_SRGB = [
    [3.2404542, -1.5371385, -0.4985314],
    [-0.9692660, 1.8760108, 0.0415560],
    [0.0556434, -0.2040259, 1.0572252]
]

# Display P3 to XYZ 矩阵 (D65)
P3_TO_XYZ = [
    [0.4865709, 0.2656677, 0.1982174],
    [0.2289746, 0.6917385, 0.0792871],
    [0.0000000, 0.0451134, 1.0439442]
]

# XYZ to Display P3 矩阵 (D65)
XYZ_TO_P3 = [
    [2.4934969, -0.9313836, -0.4027107],
    [-0.8294889, 1.7626641, 0.0236246],
    [0.0358456, -0.0761723, 0.9568845]
]

# ACEScg (AP1) to XYZ 矩阵（ACES ~D60 白点；ampas 发布值——每行之和
# 即 D60 白点 XYZ ≈ (0.9526, 1.0, 1.0088)，列即 AP1 基色 xy：
# R(0.713,0.293) G(0.165,0.830) B(0.128,0.044)。AUD-32406：此前的常量
# 行和为 (1,1,1)、列也不匹配 AP1 基色，属未发表来源的错误矩阵，且为
# 死代码；启用前先更正为标准值，否则 Bradford 适配在错误白点上展开。）
ACESCG_TO_XYZ = [
    [0.6624541811, 0.1340042065, 0.1561876870],
    [0.2722287168, 0.6740817658, 0.0536895174],
    [-0.0055746495, 0.0040607335, 1.0103391003],
]

# ============================================================
# --- 色彩空间矩阵辅助（AUD-32406） ---
# Rec.709 / ACEScg 此前在 rgb_to_xyz_linear 中静默回退 sRGB 矩阵 +
# sRGB 解码（ACESCG_TO_XYZ 成死代码），溢色检查对这两个空间给出错误
# 结论。Rec.709 与 sRGB 共享 BT.709 基色但 OETF 不同；ACEScg 是 AP1/D60
# 场景线性，须先经 Bradford 把白点适配到 D65 才能进入本模块的 D65
# XYZ→sRGB/P3 逆矩阵。
# ============================================================
def _mat_mul(a: list, b: list) -> list:
    """3x3 矩阵乘法。"""
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def _mat_vec(m: list, v: list) -> list:
    """3x3 矩阵 × 3 向量。"""
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def _mat_inv(m: list) -> list:
    """3x3 矩阵求逆（伴随矩阵法）。"""
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if det == 0:
        raise ValueError("singular color matrix")
    inv_det = 1.0 / det
    return [
        [(e * i - f * h) * inv_det, (c * h - b * i) * inv_det, (b * f - c * e) * inv_det],
        [(f * g - d * i) * inv_det, (a * i - c * g) * inv_det, (c * d - a * f) * inv_det],
        [(d * h - e * g) * inv_det, (b * g - a * h) * inv_det, (a * e - b * d) * inv_det],
    ]


# Bradford 色适应矩阵与参考白（Y=1；ACES 白点 ~D60，sRGB/P3 白点 D65）
_BRADFORD = [
    [0.8951000, 0.2664000, -0.1614000],
    [-0.7502000, 1.7135000, 0.0367000],
    [0.0389000, -0.0685000, 1.0296000],
]
_BRADFORD_INV = _mat_inv(_BRADFORD)
_D60_XYZ = [0.32168 / 0.33767, 1.0, (1.0 - 0.32168 - 0.33767) / 0.33767]
_D65_XYZ = [0.3127 / 0.3290, 1.0, (1.0 - 0.3127 - 0.3290) / 0.3290]


def _bradford_adapt(src_white: list, dst_white: list, matrix: list) -> list:
    """把 RGB→XYZ 矩阵的白点从 src_white 适配到 dst_white（Bradford）。"""
    src_cone = _mat_vec(_BRADFORD, src_white)
    dst_cone = _mat_vec(_BRADFORD, dst_white)
    scale = [
        [dst_cone[0] / src_cone[0], 0.0, 0.0],
        [0.0, dst_cone[1] / src_cone[1], 0.0],
        [0.0, 0.0, dst_cone[2] / src_cone[2]],
    ]
    return _mat_mul(_BRADFORD_INV, _mat_mul(scale, _mat_mul(_BRADFORD, matrix)))


# ACEScg(AP1, D60) → XYZ(D65)；及其逆（供 xyz_linear_to_rgb 使用）
ACESCG_TO_XYZ_D65 = _bradford_adapt(_D60_XYZ, _D65_XYZ, ACESCG_TO_XYZ)
XYZ_D65_TO_ACESCG = _mat_inv(ACESCG_TO_XYZ_D65)


def _bt709_to_linear(c: float) -> float:
    """BT.709 OETF 逆（项目 Rec.709 契约：与 color_transform_provider 的
    ``_bt709_to_linear_np`` / OCIO rec709 空间一致，见
    tests/color/test_ocio_config_load.py；不用 BT.1886 纯 2.4 幂）。

    线性段 ``c / 4.5``（c <= 0.081），否则 ``((c + 0.099) / 1.099)^(1/0.45)``。
    """
    c = max(0.0, min(1.0, c))
    if c <= 0.081:
        return c / 4.5
    return ((c + 0.099) / 1.099) ** (1.0 / 0.45)

def matrix_multiply(matrix, vector):
    """矩阵乘向量"""
    result = []
    for row in matrix:
        val = sum(row[i] * vector[i] for i in range(3))
        result.append(val)
    return result

def linearize_srgb(c):
    """将 sRGB 值转换为线性空间"""
    return _srgb_to_linear(c)

def delinearize_srgb(c):
    """将线性空间值转换为 sRGB"""
    return _linear_to_srgb(c)

def rgb_to_xyz_linear(r, g, b, color_space="sRGB"):
    """RGB 转换到 XYZ (线性空间)。

    AUD-32406：Rec.709 用 BT.709 OETF 逆解码（基色矩阵与 sRGB 相同），
    ACEScg 为场景线性（不做 OETF 解码）且使用 Bradford D60→D65 适配后的
    AP1→XYZ 矩阵；此前这两个空间静默按 sRGB 矩阵 + sRGB 曲线计算。
    """
    if color_space == "ACEScg":
        rgb_linear = [r / 255.0, g / 255.0, b / 255.0]
        matrix = ACESCG_TO_XYZ_D65
    elif color_space == "Rec.709":
        rgb_linear = [
            _bt709_to_linear(r / 255.0),
            _bt709_to_linear(g / 255.0),
            _bt709_to_linear(b / 255.0),
        ]
        matrix = SRGB_TO_XYZ  # BT.709 与 sRGB 共享基色矩阵
    else:
        # sRGB / Display P3：sRGB 分段 EOTF 逆
        rgb_linear = [linearize_srgb(r/255.0), linearize_srgb(g/255.0), linearize_srgb(b/255.0)]
        matrix = P3_TO_XYZ if color_space == "Display P3" else SRGB_TO_XYZ

    return matrix_multiply(matrix, rgb_linear)

def xyz_linear_to_rgb(x, y, z, target_space="sRGB"):
    """XYZ 转换到目标色彩空间的 RGB"""
    if target_space == "sRGB":
        matrix = XYZ_TO_SRGB
    elif target_space == "Display P3":
        matrix = XYZ_TO_P3
    elif target_space == "ACEScg":
        # AUD-32406：输入 XYZ 为 D65 基准，先经适配后的 AP1 逆矩阵
        matrix = XYZ_D65_TO_ACESCG
    else:
        matrix = XYZ_TO_SRGB
    
    rgb_linear = matrix_multiply(matrix, [x, y, z])
    rgb = [max(0, min(255, int(delinearize_srgb(c) * 255))) for c in rgb_linear]
    return rgb[0], rgb[1], rgb[2]

def convert_color_space(r, g, b, source_space="sRGB", target_space="sRGB"):
    """在不同色彩空间之间转换颜色"""
    if source_space == target_space:
        return r, g, b
    
    xyz = rgb_to_xyz_linear(r, g, b, source_space)
    return xyz_linear_to_rgb(xyz[0], xyz[1], xyz[2], target_space)

def check_out_of_gamut(r, g, b, source_space="Display P3", target_space="sRGB"):
    """检查颜色是否超出目标色彩空间的色域"""
    if source_space == target_space:
        return False
    
    xyz = rgb_to_xyz_linear(r, g, b, source_space)
    
    if target_space == "sRGB":
        matrix = XYZ_TO_SRGB
    elif target_space == "Display P3":
        matrix = XYZ_TO_P3
    else:
        return False
    
    rgb_linear = matrix_multiply(matrix, xyz)
    
    for c in rgb_linear:
        if c < -0.001 or c > 1.001:
            return True
    return False

# --- 核心工具：RGB 转 Lab ---
def rgb_to_lab(r, g, b):
    def pivot(n):
        return n / 12.92 if n <= 0.04045 else ((n + 0.055) / 1.055) ** 2.4
    r, g, b = pivot(r/255), pivot(g/255), pivot(b/255)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    def f(t):
        return t ** (1/3) if t > 0.008856 else (7.787 * t) + (16 / 116)
    x, y, z = x / 0.95047, y / 1.00000, z / 1.08883
    L = max(0, 116 * f(y) - 16)
    a = (f(x) - f(y)) * 500
    b_val = (f(y) - f(z)) * 200
    return int(L), int(a), int(b_val)

# --- 核心工具：Lab 转 RGB ---
def lab_to_rgb(L, a, b):
    """Lab颜色空间转RGB"""
    fy = (L + 16) / 116
    fx = a / 500 + fy
    fz = fy - b / 200
    
    def f_inv(t):
        return t ** 3 if t > 0.206893 else (t - 16/116) / 7.787
    
    x = 0.95047 * f_inv(fx)
    y = 1.00000 * f_inv(fy)
    z = 1.08883 * f_inv(fz)
    
    r = x * 3.2406 - y * 1.5372 - z * 0.4986
    g = -x * 0.9689 + y * 1.8758 + z * 0.0415
    b_val = x * 0.0557 - y * 0.2040 + z * 1.0570
    
    def gamma_inv(n):
        return n * 12.92 if n <= 0.0031308 else 1.055 * (n ** (1/2.4)) - 0.055
    
    r = max(0, min(255, int(gamma_inv(r) * 255)))
    g = max(0, min(255, int(gamma_inv(g) * 255)))
    b_val = max(0, min(255, int(gamma_inv(b_val) * 255)))
    
    return r, g, b_val

# --- SVG图标定义 ---
_DROPPER_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path fill-rule="evenodd" d="M16.098 2.598a3.75 3.75 0 1 1 3.622 6.275l-1.72.46V12a.75.75 0 0 1-.22.53l-.75.75a.75.75 0 0 1-1.06 0l-.97-.97-7.94 7.94a2.56 2.56 0 0 1-1.81.75 1.06 1.06 0 0 0-.75.31l-.97.97a.75.75 0 0 1-1.06 0l-.75-.75a.75.75 0 0 1 0-1.06l.97-.97a1.06 1.06 0 0 0 .31-.75c0-.68.27-1.33.75-1.81L11.69 9l-.97-.97a.75.75 0 0 1 0-1.06l.75-.75A.75.75 0 0 1 12 6h2.666l.461-1.72c.165-.617.49-1.2.971-1.682Zm-3.348 7.463L4.81 18a1.06 1.06 0 0 0-.31.75c0 .318-.06.63-.172.922a2.56 2.56 0 0 1 .922-.172c.281 0 .551-.112.75-.31l7.94-7.94-1.19-1.19Z" clip-rule="evenodd" />
</svg>'''

_GEAR_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path fill-rule="evenodd" d="M11.078 2.25c-.917 0-1.699.663-1.85 1.567L9.05 4.889c-.02.12-.115.26-.297.348a7.493 7.493 0 0 0-.986.57c-.166.115-.334.126-.45.083L6.3 5.508a1.875 1.875 0 0 0-2.282.895l-.922 1.597a1.875 1.875 0 0 0 .632 2.532l1.242.816c.094.063.167.19.174.368.012.29.012.59.012.89 0 .3 0 .6-.012.89-.007.178-.08.305-.174.368l-1.242.816a1.875 1.875 0 0 0-.632 2.532l.922 1.597a1.875 1.875 0 0 0 2.282.895l1.015-.382c.117-.044.285-.032.45.083.312.21.644.4.986.57.182.088.277.228.297.348l.178 1.072c.151.904.933 1.567 1.85 1.567h1.844c.916 0 1.699-.663 1.85-1.567l.178-1.072c.02-.12.114-.26.297-.348.342-.17.674-.36.986-.57.165-.115.333-.127.45-.083l1.015.382a1.875 1.875 0 0 0 2.282-.895l.922-1.597a1.875 1.875 0 0 0-.632-2.532l-1.242-.816c-.094-.063-.167-.19-.174-.368a12.37 12.37 0 0 1 0-1.78c.007-.178.08-.305.174-.368l1.242-.816a1.875 1.875 0 0 0 .632-2.532l-.922-1.597a1.875 1.875 0 0 0-2.282-.895l-1.015.382c-.117.044-.285.032-.45-.083a7.493 7.493 0 0 0-.986-.57c-.183-.088-.277-.228-.297-.348l-.178-1.072a1.875 1.875 0 0 0-1.85-1.567h-1.844ZM12 15.75a3.75 3.75 0 1 0 0-7.5 3.75 3.75 0 0 0 0 7.5Z" clip-rule="evenodd" />
</svg>'''

# 色板图标 SVG
_SWATCH_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M4 5a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v14a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V5zm3 2a1 1 0 0 0-1 1v3a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1H7zm6 0a1 1 0 0 0-1 1v3a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-3zm-6 5a1 1 0 0 0-1 1v3a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1H7zm6 0a1 1 0 0 0-1 1v3a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-3z"/>
</svg>'''

# 拖拽手柄 SVG
_GRIP_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <circle cx="9" cy="6" r="1.5"/>
  <circle cx="15" cy="6" r="1.5"/>
  <circle cx="9" cy="12" r="1.5"/>
  <circle cx="15" cy="12" r="1.5"/>
  <circle cx="9" cy="18" r="1.5"/>
  <circle cx="15" cy="18" r="1.5"/>
</svg>'''

def _svg_with_fill(svg_data: bytes, color: str) -> bytes:
    return svg_data.replace(b'fill="currentColor"', f'fill="{color}"'.encode())

def create_dropper_cursor():
    """创建吸管光标"""
    cursor_size = 32
    draw_size = 24
    
    pixmap = QPixmap(cursor_size, cursor_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    
    # 吸管尖端在底部中央，所以需要让尖端对准热点(0, hotspot_y)
    # SVG viewBox 是 24x24，吸管尖端大约在 (12, 23)
    # 我们需要让尖端位于 pixmap 的左上角附近
    rect = QRectF(0, 0, draw_size, draw_size)
    
    # 绘制阴影
    shadow_renderer = QSvgRenderer(_svg_with_fill(_DROPPER_SVG, "#000000"))
    p.setOpacity(0.3)
    shadow_renderer.render(p, rect.translated(1, 1))
    
    # 绘制白色吸管
    white_renderer = QSvgRenderer(_svg_with_fill(_DROPPER_SVG, "#FFFFFF"))
    p.setOpacity(1.0)
    white_renderer.render(p, rect)

    p.end()
    
    # 热点在吸管尖端位置 (SVG中大约在 y=23/24 的位置)
    hotspot_y = int((23.0 / 24.0) * draw_size)
    return QCursor(pixmap, 12, hotspot_y)

# --- 自定义图标按钮 ---
class IconButton(QPushButton):
    def __init__(self, icon_size=24, parent=None):
        super().__init__(parent)
        self._icon_render_size = icon_size 
        self._svg_data = None

    def set_svg_data(self, svg_text):
        """存储SVG字符串并触发重绘"""
        self._svg_data = svg_text
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        
        if not self._svg_data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        draw_size = min(self.width(), self.height()) * 0.55
        
        rect = QRectF(
            (self.width() - draw_size) / 2,
            (self.height() - draw_size) / 2,
            draw_size,
            draw_size
        )

        shadow_renderer = QSvgRenderer(self._svg_data.replace(b'fill="currentColor"', b'fill="#000000"'))
        painter.setOpacity(0.3)
        shadow_renderer.render(painter, rect.translated(1.5, 1.5))
        
        white_renderer = QSvgRenderer(self._svg_data)
        painter.setOpacity(1.0)
        white_renderer.render(painter, rect)

# --- 全局吸管监听器 ---
class EyeDropperOverlay(QWidget):
    """全屏透明覆盖层，用于屏幕级取色。

    工作原理：启动时抓取整个屏幕截图，然后显示一个覆盖整个屏幕的透明窗口。
    点击时从截图中读取像素颜色。

    注意：grabWindow 在 macOS 上可能需要屏幕录制权限（macOS 10.15+）。
    如果截图返回全黑导致取色异常，请检查系统设置中的屏幕录制权限。
    """
    colorPicked = pyqtSignal(QColor)
    # 取消信号：用户按 Esc 取消取色
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.cursor_shape = create_dropper_cursor()
        # BUG-A4-06: 预置为 None——start() 失败（无屏幕/空截图）时该成员
        # 不存在，mousePressEvent 访问会 AttributeError；预置 + 判空保证
        # 覆盖层任何交互入口都可防御。
        self.full_screen_img: QImage | None = None
        # AUD-32401: grabWindow 截图按物理像素存储，而 globalPosition()
        # 返回逻辑坐标——记录截图来源屏幕的逻辑原点，采样时做
        # devicePixelRatio 换算（Retina 上 dpr=2，逻辑 (x,y) 对应物理
        # (2x,2y)），否则取色点系统性偏向点击位置左上。
        self._screen_origin = QPoint(0, 0)

    def start(self):
        """启动吸管模式：截图 → 显示全屏覆盖层 → 等待点击。

        启动失败（无屏幕或截图为空）时只发 cancelled 并确保覆盖层
        不可见，绝不让半初始化的覆盖层留在屏幕上接收点击。
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            self.full_screen_img = None
            self.hide()
            self.cancelled.emit()
            return

        img = screen.grabWindow(0).toImage()
        if img.isNull():
            self.full_screen_img = None
            self.hide()
            self.cancelled.emit()
            return
        self.full_screen_img = img
        self._screen_origin = screen.geometry().topLeft()

        self.setGeometry(screen.geometry())
        self.setCursor(self.cursor_shape)
        self.show()
        self.raise_()
        self.activateWindow()

    def mousePressEvent(self, event):
        # BUG-A4-06: start() 失败的覆盖层不应崩溃——按取消处理。
        if self.full_screen_img is None:
            self.close()
            self.cancelled.emit()
            return
        pos = event.globalPosition().toPoint()
        # AUD-32401: 逻辑全局坐标 → 屏幕内逻辑坐标 → 截图像素坐标。
        # 截图按物理像素存储，须乘 devicePixelRatio；吸管光标的 hotspot
        # 已精确定位在吸管尖端（create_dropper_cursor），移除旧的 -8 像素
        # 采样偏移 hack。
        dpr = self.full_screen_img.devicePixelRatio()
        if dpr <= 0.0:
            dpr = 1.0
        local = pos - self._screen_origin
        # 边界保护：避免坐标越界
        px = max(0, min(self.full_screen_img.width() - 1, int(local.x() * dpr)))
        py = max(0, min(self.full_screen_img.height() - 1, int(local.y() * dpr)))
        color = QColor(self.full_screen_img.pixel(px, py))
        # AUD-32402: 取色完成立刻释放全屏截图（4K 屏约 33MB QImage）。
        self.full_screen_img = None
        self.close()
        self.colorPicked.emit(color)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.full_screen_img = None  # AUD-32402: 取消也释放截图
            self.close()
            self.cancelled.emit()

# --- 色彩方块组件 ---
class ColorSquare(QWidget):
    colorChanged = pyqtSignal(int, int)
    def __init__(self):
        super().__init__()
        self.setFixedSize(250, 250)
        self.hue, self.sat, self.val = 0, 255, 255

    def paintEvent(self, event):
        p = QPainter(self)
        grad_h = QLinearGradient(0, 0, self.width(), 0)
        grad_h.setColorAt(0, Qt.GlobalColor.white)
        grad_h.setColorAt(1, QColor.fromHsv(self.hue, 255, 255))
        p.fillRect(self.rect(), grad_h)
        grad_v = QLinearGradient(0, 0, 0, self.height())
        grad_v.setColorAt(0, QColor(0, 0, 0, 0))
        grad_v.setColorAt(1, QColor(0, 0, 0, 255))
        p.fillRect(self.rect(), grad_v)
        p.setPen(QPen(Qt.GlobalColor.white, 2))
        p.drawEllipse(QPoint(int(self.sat/255*250), int((255-self.val)/255*250)), 6, 6)

    def mouseMoveEvent(self, event):
        self.sat = max(0, min(255, int(event.pos().x() / 250 * 255)))
        self.val = max(0, min(255, int(255 - (event.pos().y() / 250 * 255))))
        self.colorChanged.emit(self.sat, self.val)
        self.update()
    def mousePressEvent(self, event): self.mouseMoveEvent(event)

# --- 色相滑块组件 ---
class HueSlider(QWidget):
    hueChanged = pyqtSignal(int)
    def __init__(self):
        super().__init__()
        self.setFixedSize(20, 250)
        self.hue = 0
    def paintEvent(self, event):
        p = QPainter(self)
        for y in range(self.height()):
            p.setPen(QColor.fromHsv(int(y/self.height()*359), 255, 255))
            p.drawLine(0, y, self.width(), y)
        p.setPen(QPen(Qt.GlobalColor.white, 3))
        p.drawLine(0, int(self.hue/359*self.height()), self.width(), int(self.hue/359*self.height()))
    def mouseMoveEvent(self, event):
        self.hue = max(0, min(359, int(event.pos().y() / self.height() * 359)))
        self.hueChanged.emit(self.hue)
        self.update()
    def mousePressEvent(self, event): self.mouseMoveEvent(event)

# --- 透明度滑块（水平方向） ---
class AlphaSlider(QWidget):
    alphaChanged = pyqtSignal(int)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(113, 16)
        self.alpha = 255
        self._color = QColor(255, 0, 0)
    def set_color(self, color):
        self._color = QColor(color)
        self.update()
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # 绘制棋盘格背景表示透明
        tile = 4
        for ty in range(0, h, tile):
            for tx in range(0, w, tile):
                c = 200 if (tx // tile + ty // tile) % 2 == 0 else 255
                p.fillRect(tx, ty, tile, tile, QColor(c, c, c))
        # 绘制透明度渐变
        grad = QLinearGradient(0, 0, w, 0)
        c = QColor(self._color)
        c.setAlpha(0)
        grad.setColorAt(0.0, c)
        c.setAlpha(255)
        grad.setColorAt(1.0, c)
        p.fillRect(0, 0, w, h, grad)
        # 绘制指示线
        indicator_x = int(self.alpha / 255 * (w - 1))
        p.setPen(QPen(Qt.GlobalColor.white, 2))
        p.drawLine(indicator_x, 0, indicator_x, h)
        p.setPen(QPen(Qt.GlobalColor.black, 1))
        p.drawRect(0, 0, w - 1, h - 1)
    def mouseMoveEvent(self, event):
        x = max(0, min(event.pos().x(), self.width() - 1))
        self.alpha = int(x / (self.width() - 1) * 255)
        self.alphaChanged.emit(self.alpha)
        self.update()
    def mousePressEvent(self, event): self.mouseMoveEvent(event)

# --- 支持滚轮调整的输入框 ---
class SpinLineEdit(QLineEdit):
    valueChanged = pyqtSignal(int)
    
    def __init__(self, min_val=0, max_val=255, parent=None):
        super().__init__(parent)
        self.min_val = min_val
        self.max_val = max_val
        self._value = min_val
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hover = False
        self._original_style = ""
        self.editingFinished.connect(self._on_editing_finished)
        self.returnPressed.connect(self._on_editing_finished)
        self._dragging = False
        self._drag_start_x = 0
        self._drag_start_value = 0
        self.setMouseTracking(True)
        
    def _on_editing_finished(self):
        try:
            val = int(self.text())
            val = max(self.min_val, min(self.max_val, val))
            if val != self._value:
                self._value = val
                self.valueChanged.emit(val)
            else:
                self.setText(str(self._value))
        except ValueError:
            self.setText(str(self._value))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._dragging = True
            self._drag_start_x = int(event.position().x())
            self._drag_start_value = self._value
            self.grabMouse()
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dx = int(event.position().x()) - self._drag_start_x
            step = self._drag_sensitivity(event.modifiers())
            new_val = self._drag_start_value + int(dx / step)
            new_val = max(self.min_val, min(self.max_val, new_val))
            if new_val != self._value:
                self._value = new_val
                self.setText(str(new_val))
                self.valueChanged.emit(new_val)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.MouseButton.MiddleButton:
            self._dragging = False
            self.releaseMouse()
            self.setCursor(Qt.CursorShape.IBeamCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    @staticmethod
    def _drag_sensitivity(modifiers):
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            return 0.2
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            return 0.1
        return 1

    def enterEvent(self, event):
        self._hover = True
        self._original_style = self.styleSheet()
        self.setStyleSheet("background-color: #2A2A2A; border: 1px solid #0078D4; border-radius: 4px;")
        super().enterEvent(event)
        
    def leaveEvent(self, event):
        self._hover = False
        self.setStyleSheet(self._original_style)
        super().leaveEvent(event)
        
    def wheelEvent(self, event):
        if not self._hover:
            event.ignore()
            return
            
        delta = 1 if event.angleDelta().y() > 0 else -1
        
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            step = 5
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            step = 10
        else:
            step = 1
            
        new_val = self._value + delta * step
        new_val = max(self.min_val, min(self.max_val, new_val))
        
        if new_val != self._value:
            self._value = new_val
            self.setText(str(new_val))
            self.valueChanged.emit(new_val)
        event.accept()
    
    def setValue(self, val):
        self._value = max(self.min_val, min(self.max_val, val))
        self.setText(str(self._value))
        
    def value(self):
        return self._value

# --- 可拖拽预览色块 ---
class DraggablePreview(QFrame):
    """可拖拽的颜色预览框，支持将颜色拖拽到色块预设中"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.color = "#FF0000"
        self.setAcceptDrops(True)
        
    def set_color(self, color):
        """设置颜色，支持 QColor 对象（含透明度）和字符串"""
        self.color = color
        if isinstance(color, QColor):
            if color.alpha() < 255:
                css = f"background-color: rgba({color.red()},{color.green()},{color.blue()},{color.alpha()/255:.3f}); border-radius: 6px; border: 1px solid #444;"
            else:
                css = f"background-color: {color.name()}; border-radius: 6px; border: 1px solid #444;"
        else:
            css = f"background-color: {color}; border-radius: 6px; border: 1px solid #444;"
        self.setStyleSheet(css)
        
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)
        
    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if not hasattr(self, '_drag_start_pos') or not self._drag_start_pos:
            return
            
        distance = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
        if distance < 10:
            return
            
        # 开始拖拽颜色
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(self.color.name() if isinstance(self.color, QColor) else str(self.color))
        mime_data.setData("application/x-color-preview", b"1")
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

# --- 对比度检查器组件 ---
class ContrastChecker(QFrame):
    """WCAG 对比度检查器组件"""
    
    fixRequested = pyqtSignal(QColor)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.foreground_color = QColor(0, 0, 0)
        self.background_color = QColor(255, 255, 255)
        self.setup_ui()
        
    def setup_ui(self):
        self.setStyleSheet("background-color: #1A1A1A; border-radius: 6px; border: 1px solid #333;")
        self.setFixedHeight(85)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        
        # 标题行
        title_layout = QHBoxLayout()
        title_label = QLabel(I18N.tr("color_picker.contrast_check", "对比度检查 (WCAG)"))
        title_label.setStyleSheet("font-size: 10px; color: #888; font-weight: bold;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        
        # 切换背景色按钮
        self.btn_toggle_bg = QPushButton(I18N.tr("color_picker.white_bg", "白底"))
        self.btn_toggle_bg.setFixedSize(45, 18)
        self.btn_toggle_bg.setStyleSheet("""
            QPushButton { font-size: 9px; background: #333; border: 1px solid #444; border-radius: 3px; color: #FFF; }
            QPushButton:hover { background: #444; }
        """)
        self.btn_toggle_bg.clicked.connect(self._toggle_background)
        title_layout.addWidget(self.btn_toggle_bg)
        layout.addLayout(title_layout)
        
        # 对比度数值和等级显示
        contrast_layout = QHBoxLayout()
        contrast_layout.setSpacing(8)
        
        # 对比度数值
        self.ratio_label = QLabel("21.0:1")
        self.ratio_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #FFF;")
        contrast_layout.addWidget(self.ratio_label)
        
        # 等级徽章
        self.badge_label = QLabel(I18N.tr("color_picker.wcag_aaa", "AAA"))
        self.badge_label.setFixedSize(50, 22)
        self.badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge_label.setStyleSheet("font-size: 10px; font-weight: bold; border-radius: 4px;")
        contrast_layout.addWidget(self.badge_label)
        
        contrast_layout.addStretch()
        layout.addLayout(contrast_layout)
        
        # 文字预览
        preview_layout = QHBoxLayout()
        self.preview_label = QLabel(I18N.tr("color_picker.text_preview", "Aa 文字预览"))
        self.preview_label.setFixedHeight(22)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("font-size: 12px; border-radius: 4px;")
        preview_layout.addWidget(self.preview_label)
        
        # 自动修复按钮
        self.btn_fix = QPushButton(I18N.tr("color_picker.fix", "修复"))
        self.btn_fix.setFixedSize(40, 22)
        self.btn_fix.setStyleSheet("""
            QPushButton { font-size: 9px; background: #0078D4; border: none; border-radius: 4px; color: #FFF; }
            QPushButton:hover { background: #1083D8; }
            QPushButton:disabled { background: #444; color: #666; }
        """)
        self.btn_fix.setToolTip(I18N.tr(
            "color_picker.fix_tooltip", "自动调整亮度以满足 AA 标准 (4.5:1)"))
        self.btn_fix.clicked.connect(self._on_fix_clicked)
        preview_layout.addWidget(self.btn_fix)
        
        layout.addLayout(preview_layout)
        self._update_display()
        
    def set_foreground_color(self, color):
        """设置前景色"""
        self.foreground_color = color
        self._update_display()
        
    def _toggle_background(self):
        """切换背景色"""
        if self.background_color == QColor(255, 255, 255):
            self.background_color = QColor(0, 0, 0)
            self.btn_toggle_bg.setText(I18N.tr("color_picker.black_bg", "黑底"))
        else:
            self.background_color = QColor(255, 255, 255)
            self.btn_toggle_bg.setText(I18N.tr("color_picker.white_bg", "白底"))
        self._update_display()
        
    def _update_display(self):
        """更新显示"""
        ratio = get_contrast_ratio(self.foreground_color, self.background_color)
        level, color = get_wcag_level(ratio)
        
        self.ratio_label.setText(f"{ratio:.1f}:1")
        self.badge_label.setText(level)
        self.badge_label.setStyleSheet(f"font-size: 10px; font-weight: bold; border-radius: 4px; background: {color}; color: #FFF;")
        
        # 更新文字预览
        fg_hex = self.foreground_color.name().upper()
        bg_hex = self.background_color.name().upper()
        self.preview_label.setStyleSheet(f"font-size: 12px; border-radius: 4px; background: {bg_hex}; color: {fg_hex}; padding: 2px;")
        
        # 更新修复按钮状态
        self.btn_fix.setEnabled(ratio < 4.5)
        
    def _on_fix_clicked(self):
        """点击修复按钮"""
        is_dark_bg = self.background_color == QColor(0, 0, 0)
        fixed_color, ratio, new_L = find_minimum_l_for_contrast(
            self.foreground_color, 
            target_ratio=4.5,
            is_text_dark=not is_dark_bg
        )
        if fixed_color:
            self.fixRequested.emit(fixed_color)

# --- 代码格式导出面板 ---
class CodeExportPanel(QFrame):
    """代码格式导出面板，支持一键复制多种格式"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_color = QColor("#FF0000")
        self.setup_ui()
        
    def setup_ui(self):
        self.setStyleSheet("background-color: #1A1A1A; border-radius: 6px; border: 1px solid #333;")
        self.setFixedHeight(120)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)
        
        # 标题
        title_label = QLabel(I18N.tr("color_picker.code_export", "代码格式导出"))
        title_label.setStyleSheet("font-size: 10px; color: #888; font-weight: bold;")
        layout.addWidget(title_label)
        
        # 格式按钮行
        format_btn_layout = QHBoxLayout()
        format_btn_layout.setSpacing(2)
        
        formats = [
            ("CSS", "css"),
            ("Sass", "sass"),
            ("Swift", "swift"),
            ("Kotlin", "kotlin"),
            ("JSON", "json"),
        ]
        
        self.format_buttons = {}
        for name, key in formats:
            btn = QPushButton(name)
            btn.setFixedSize(50, 18)
            btn.setStyleSheet("""
                QPushButton { font-size: 9px; background: #333; border: 1px solid #444; border-radius: 3px; color: #FFF; }
                QPushButton:hover { background: #444; }
                QPushButton:checked { background: #0078D4; border: 1px solid #0078D4; }
            """)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, k=key: self._on_format_clicked(k))
            self.format_buttons[key] = btn
            format_btn_layout.addWidget(btn)
        
        self.format_buttons["css"].setChecked(True)
        format_btn_layout.addStretch()
        layout.addLayout(format_btn_layout)
        
        # 代码显示区域
        self.code_display = QLineEdit()
        self.code_display.setStyleSheet("""
            QLineEdit { background: #0D0D0D; border: 1px solid #222; border-radius: 4px; 
                       font-family: Menlo, Monaco, monospace; font-size: 10px; color: #FFF; padding: 4px; }
        """)
        self.code_display.setReadOnly(True)
        layout.addWidget(self.code_display)
        
        # 复制按钮
        copy_btn = QPushButton(I18N.tr("color_picker.copy", "复制"))
        copy_btn.setFixedHeight(18)
        copy_btn.setStyleSheet("""
            QPushButton { font-size: 9px; background: #0078D4; border: none; border-radius: 4px; color: #FFF; }
            QPushButton:hover { background: #1083D8; }
        """)
        copy_btn.clicked.connect(self._copy_code)
        layout.addWidget(copy_btn)
        
        self.current_format = "css"
        self._update_code()
        
    def set_color(self, color):
        """设置当前颜色"""
        self.current_color = color
        self._update_code()
        
    def _on_format_clicked(self, format_key):
        """切换格式"""
        for key, btn in self.format_buttons.items():
            btn.setChecked(key == format_key)
        self.current_format = format_key
        self._update_code()
        
    def _generate_code(self, format_key):
        """生成对应格式的代码"""
        hex_val = self.current_color.name().upper()
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        
        if format_key == "css":
            return f"--primary-color: {hex_val};"
        elif format_key == "sass":
            return f"$primary-color: {hex_val};"
        elif format_key == "swift":
            return f"UIColor(red: {r}/255, green: {g}/255, blue: {b}/255, alpha: 1.0)"
        elif format_key == "kotlin":
            return f"Color(0x{hex_val[1:]})"
        elif format_key == "json":
            return '{"color": "' + hex_val + '", "rgb": {"r": ' + str(r) + ', "g": ' + str(g) + ', "b": ' + str(b) + '}}'
        return hex_val
        
    def _update_code(self):
        """更新代码显示"""
        code = self._generate_code(self.current_format)
        self.code_display.setText(code)
        
    def _copy_code(self):
        """复制代码"""
        QApplication.clipboard().setText(self.code_display.text())

# --- macOS 简约风格输入对话框 ---
class MinimalInputDialog(QDialog):
    """macOS 简约风格的输入对话框"""
    
    def __init__(self, title, prompt, default_text="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(280, 160)
        self.setStyleSheet("""
            QDialog { 
                background-color: #2C2C2C; 
                border: 1px solid #3A3A3A;
                border-radius: 12px; 
            }
        """)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        
        # 标题
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #FFF; background: transparent; border: none;")
        layout.addWidget(title_label)
        
        # 提示文字
        if prompt:
            prompt_label = QLabel(prompt)
            prompt_label.setStyleSheet("font-size: 12px; color: #888; background: transparent; border: none;")
            layout.addWidget(prompt_label)
        
        # 输入框
        self.input_edit = QLineEdit()
        self.input_edit.setText(default_text)
        self.input_edit.setFixedHeight(32)
        self.input_edit.setStyleSheet("""
            QLineEdit { 
                background-color: #1A1A1A; 
                border: 1px solid #3A3A3A; 
                border-radius: 6px; 
                padding: 6px 10px;
                font-size: 13px; 
                color: #FFF;
            }
            QLineEdit:focus { border: 1px solid #0078D4; }
        """)
        self.input_edit.selectAll()
        layout.addWidget(self.input_edit)
        
        layout.addSpacing(8)
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()
        
        cancel_btn = QPushButton(I18N.tr("color_picker.cancel", "取消"))
        cancel_btn.setFixedSize(70, 28)
        cancel_btn.setStyleSheet("""
            QPushButton { 
                background-color: #3A3A3A; 
                border: none; 
                border-radius: 6px; 
                font-size: 13px;
                color: #AAA;
            }
            QPushButton:hover { background-color: #4A4A4A; color: #FFF; }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        confirm_btn = QPushButton(I18N.tr("color_picker.ok", "确定"))
        confirm_btn.setFixedSize(70, 28)
        confirm_btn.setStyleSheet("""
            QPushButton { 
                background-color: #0078D4; 
                border: none; 
                border-radius: 6px; 
                font-size: 13px;
                color: #FFF;
            }
            QPushButton:hover { background-color: #1083D8; }
        """)
        confirm_btn.clicked.connect(self.accept)
        btn_layout.addWidget(confirm_btn)
        
        layout.addLayout(btn_layout)
        
        self.input_edit.returnPressed.connect(self.accept)
        self.input_edit.setFocus()
        
    def get_text(self):
        return self.input_edit.text().strip()
        
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

# --- macOS 简约风格确认对话框 ---
class MinimalConfirmDialog(QDialog):
    """macOS 简约风格的确认对话框"""
    
    def __init__(self, title, message, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(300, 170)
        self.setStyleSheet("""
            QDialog { 
                background-color: #2C2C2C; 
                border: 1px solid #3A3A3A;
                border-radius: 12px; 
            }
        """)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        
        # 标题
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #FFF; background: transparent; border: none;")
        title_label.setFixedHeight(22)
        layout.addWidget(title_label)
        
        # 消息
        msg_label = QLabel(message)
        msg_label.setStyleSheet("font-size: 12px; color: #888; background: transparent; border: none; line-height: 1.4;")
        msg_label.setWordWrap(True)
        msg_label.setFixedHeight(50)
        layout.addWidget(msg_label)
        
        layout.addSpacing(12)
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()
        
        cancel_btn = QPushButton(I18N.tr("color_picker.cancel", "取消"))
        cancel_btn.setFixedSize(70, 28)
        cancel_btn.setStyleSheet("""
            QPushButton { 
                background-color: #3A3A3A; 
                border: none; 
                border-radius: 6px; 
                font-size: 13px;
                color: #AAA;
            }
            QPushButton:hover { background-color: #4A4A4A; color: #FFF; }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        confirm_btn = QPushButton(I18N.tr("color_picker.ok", "确定"))
        confirm_btn.setFixedSize(70, 28)
        confirm_btn.setStyleSheet("""
            QPushButton { 
                background-color: #EF4444; 
                border: none; 
                border-radius: 6px; 
                font-size: 13px;
                color: #FFF;
            }
            QPushButton:hover { background-color: #DC2626; }
        """)
        confirm_btn.clicked.connect(self.accept)
        btn_layout.addWidget(confirm_btn)
        
        layout.addLayout(btn_layout)
        
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

# --- 历史记录组件（2排布局）---
class ColorHistory(QWidget):
    """颜色历史记录组件，显示最近12次取色"""
    
    colorSelected = pyqtSignal(QColor)
    
    MAX_HISTORY = 12
    ROWS = 1
    COLS = 12
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = []
        self.setup_ui()
        
    def setup_ui(self):
        self.setStyleSheet("background-color: transparent;")
        self.setFixedSize(228, 22)
        
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        
        self.history_buttons = []
        for row in range(self.ROWS):
            for col in range(self.COLS):
                btn = QPushButton()
                btn.setFixedSize(18, 18)
                btn.setStyleSheet("background: #333; border-radius: 2px;")
                btn.setToolTip(I18N.tr("color_picker.history_swatch", "历史色块"))
                idx = row * self.COLS + col
                btn.clicked.connect(lambda _, i=idx: self._on_history_clicked(i))
                self.history_buttons.append(btn)
                layout.addWidget(btn, row, col)
        
    def add_color(self, color):
        """添加颜色到历史记录"""
        hex_val = color.name().upper()
        
        if hex_val in self.history:
            self.history.remove(hex_val)
        
        self.history.insert(0, hex_val)
        
        if len(self.history) > self.MAX_HISTORY:
            self.history.pop()
        
        self._update_display()
        
    def _update_display(self):
        """更新历史色块显示"""
        for i, btn in enumerate(self.history_buttons):
            if i < len(self.history):
                color = self.history[i]
                btn.setStyleSheet(f"background: {color}; border-radius: 2px;")
                btn.setToolTip(color)
            else:
                btn.setStyleSheet("background: #333; border-radius: 2px;")
                btn.setToolTip(I18N.tr("color_picker.empty", "空"))
                
    def _on_history_clicked(self, idx):
        """点击历史色块"""
        if idx < len(self.history):
            color = QColor(self.history[idx])
            self.colorSelected.emit(color)

# --- 设置对话框（整合所有新功能）---
class SettingsDialog(QDialog):
    """设置对话框，包含对比度检查、代码导出、色彩空间、历史记录"""
    
    colorApplied = pyqtSignal(QColor)
    resetRequested = pyqtSignal()
    
    def __init__(self, current_color, parent=None):
        super().__init__(parent)
        self.current_color = current_color
        self.setWindowTitle(I18N.tr(
            "color_picker.settings_title", "Color Inspector - 设置面板"))
        self.setFixedSize(420, 430)
        self.setStyleSheet("""
            QDialog { background-color: #252525; }
            QLabel { color: #EEE; }
        """)
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        
        # 1. 色彩空间选择器
        space_group = QFrame()
        space_group.setStyleSheet("background: #1A1A1A; border-radius: 6px; border: 1px solid #333;")
        space_layout = QHBoxLayout(space_group)
        space_layout.setContentsMargins(8, 6, 8, 6)
        
        space_label = QLabel(I18N.tr("color_picker.color_space", "色彩空间:"))
        space_label.setStyleSheet("font-size: 11px; color: #888;")
        space_layout.addWidget(space_label)
        
        self.space_combo = QComboBox()
        self.space_combo.addItems(["sRGB", "Display P3", "Rec.709", "ACEScg"])
        self.space_combo.setFixedSize(120, 22)
        self.space_combo.setStyleSheet("""
            QComboBox { background: #333; border: 1px solid #444; border-radius: 4px; font-size: 10px; color: #FFF; padding: 3px; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView { background: #333; border: 1px solid #444; selection-background-color: #0078D4; }
        """)
        self.space_combo.currentTextChanged.connect(self._check_gamut)
        space_layout.addWidget(self.space_combo)
        
        # 溢色警告
        self.gamut_warning = QLabel(I18N.tr("color_picker.gamut_warning", "⚠ 溢色警告"))
        self.gamut_warning.setStyleSheet("font-size: 11px; color: #EF4444; font-weight: bold;")
        self.gamut_warning.hide()
        space_layout.addWidget(self.gamut_warning)
        space_layout.addStretch()
        
        layout.addWidget(space_group)
        
        # 2. 对比度检查器
        self.contrast_checker = ContrastChecker()
        self.contrast_checker.set_foreground_color(self.current_color)
        self.contrast_checker.fixRequested.connect(self._on_color_fixed)
        layout.addWidget(self.contrast_checker)
        
# 3. 代码格式导出面板
        self.code_export = CodeExportPanel()
        self.code_export.set_color(self.current_color)
        layout.addWidget(self.code_export)
        
        layout.addStretch()
        
        # 恢复默认设置按钮
        reset_btn = QPushButton(I18N.tr("color_picker.reset_defaults", "恢复默认设置"))
        reset_btn.setFixedHeight(30)
        reset_btn.setStyleSheet("""
            QPushButton { font-size: 12px; background: #EF4444; border: none; border-radius: 6px; color: #FFF; }
            QPushButton:hover { background: #DC2626; }
        """)
        reset_btn.clicked.connect(self._on_reset_clicked)
        layout.addWidget(reset_btn)
        
        # 关闭按钮
        close_btn = QPushButton(I18N.tr("color_picker.close", "关闭"))
        close_btn.setFixedHeight(30)
        close_btn.setStyleSheet("""
            QPushButton { font-size: 12px; background: #555; border: none; border-radius: 6px; color: #FFF; }
            QPushButton:hover { background: #666; }
        """)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)
        
        self._check_gamut(self.space_combo.currentText())
        
    def set_color(self, color):
        """更新当前颜色"""
        self.current_color = color
        self.contrast_checker.set_foreground_color(color)
        self.code_export.set_color(color)
        self._check_gamut(self.space_combo.currentText())
        
    def _check_gamut(self, space_name):
        """检查溢色"""
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        out_of_gamut = check_out_of_gamut(r, g, b, space_name, "sRGB")
        
        if out_of_gamut and space_name != "sRGB":
            self.gamut_warning.show()
        else:
            self.gamut_warning.hide()
            
    def _on_color_fixed(self, color):
        """对比度修复后应用颜色"""
        self.current_color = color
        self.colorApplied.emit(color)
        self.set_color(color)
        
    def _on_reset_clicked(self):
        """恢复默认设置"""
        reply = QMessageBox.question(self, "确认恢复", 
            "确定要恢复所有默认设置吗？\n\n这将清除：\n- 所有色板数据\n- 自定义颜色\n- 历史记录\n\n此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.resetRequested.emit()
            self.close()

# --- 基础色块按钮 ---
class SwatchButton(QPushButton):
    """色块按钮基类"""
    
    def __init__(self, color="#333333", parent=None):
        super().__init__(parent)
        self.color = color
        self.setObjectName("Swatch")
        self.setFixedSize(18, 18)
        self.setStyleSheet(f"background-color: {color}; border-radius: 2px;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class DraggableColorButton(SwatchButton):
    """支持拖拽排序的色块按钮"""
    
    colorChanged = pyqtSignal(str)  # 颜色改变信号
    
    def __init__(self, color="#333333", parent=None):
        super().__init__(color, parent)
        self.setAcceptDrops(True)
        self._drag_start_pos = None
        
    def update_style(self):
        hex_str = self.color.name() if isinstance(self.color, QColor) else str(self.color)
        self.setStyleSheet(f"background-color: {hex_str}; border-radius: 2px;")
        self.setToolTip(hex_str)
    
    def set_color(self, color):
        self.color = color
        self.update_style()
        self.colorChanged.emit(color)
        
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)
        
    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if not self._drag_start_pos:
            return
            
        distance = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
        if distance < 10:
            return
            
        # 开始拖拽
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(self.color)
        mime_data.setData("application/x-color-button", b"1")
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.MoveAction)
        
    def dragEnterEvent(self, event):
        # 接收来自色块按钮或预览色块的拖拽
        if event.mimeData().hasFormat("application/x-color-button") or \
           event.mimeData().hasFormat("application/x-color-preview"):
            event.acceptProposedAction()
            
    def dropEvent(self, event):
        # 处理来自色块按钮的拖拽（交换颜色）
        if event.mimeData().hasFormat("application/x-color-button"):
            source = event.source()
            if source and isinstance(source, DraggableColorButton) and source != self:
                # 交换颜色
                source_color = source.color
                source.set_color(self.color)
                self.set_color(source_color)
            event.acceptProposedAction()
        
        # 处理来自预览色块的拖拽（设置颜色）
        elif event.mimeData().hasFormat("application/x-color-preview"):
            source = event.source()
            if source and isinstance(source, DraggablePreview):
                # 将预览色块的颜色设置到当前色块
                self.set_color(source.color)
            event.acceptProposedAction()

# --- 可拖拽色板行 ---
class DraggableSwatchRow(QFrame):
    """支持拖拽排序的色板行"""
    
    rowMoved = pyqtSignal(int, int)  # 行移动信号 (from_index, to_index)
    nameChanged = pyqtSignal(str)  # 名称改变信号
    
    def __init__(self, name, colors, row_index, parent=None):
        super().__init__(parent)
        self.row_name = name
        self.row_index = row_index
        self.colors = colors
        self.color_buttons = []
        self.is_locked = (name == "自定义")  # 自定义行默认锁定
        self.setAcceptDrops(True)
        self.setup_ui()
        
    def setup_ui(self):
        self.setStyleSheet("background-color: transparent;")
        self.setFixedHeight(22)  # 固定高度22px（色块17px + 上下各2.5px边距）
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)  # 水平扩展，垂直固定
        
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 1, 0, 1)  # 上下各1px边距
        main_layout.setSpacing(1)
        
        # 拖拽手柄（隐藏）
        self.grip_btn = IconButton(12)
        self.grip_btn.setFixedSize(14, 16)
        self.grip_btn.set_svg_data(_svg_with_fill(_GRIP_SVG, "#666"))
        self.grip_btn.setCursor(Qt.CursorShape.SizeVerCursor)
        self.grip_btn.setToolTip(I18N.tr("color_picker.drag_reorder", "拖拽调整顺序"))
        self.grip_btn.hide()
        main_layout.addWidget(self.grip_btn)
        
        # 分类名称标签（双击可编辑）
        self.name_label = QLabel(self.row_name)
        self.name_label.setFixedSize(55, 16)  # 五个字符宽度
        self.name_label.setStyleSheet("font-size: 11px; color: #888; font-weight: bold;")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)  # 垂直居中
        self.name_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name_label.setToolTip(I18N.tr("color_picker.dblclick_rename", "双击修改名称"))
        self.name_label.mouseDoubleClickEvent = self._on_name_double_click
        main_layout.addWidget(self.name_label)
        
        # 色块容器
        colors_layout = QHBoxLayout()
        colors_layout.setSpacing(1)
        
        for color in self.colors:
            btn = DraggableColorButton(color)
            btn.clicked.connect(lambda _, c=color, b=btn: self._on_color_clicked(b))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, b=btn: self._show_color_context_menu(b, pos))
            self.color_buttons.append(btn)
            colors_layout.addWidget(btn)
            
        main_layout.addLayout(colors_layout)
        main_layout.addStretch()
        
    def _on_name_double_click(self, event):
        """双击修改名称"""
        if self.is_locked:
            return
        dialog = MinimalInputDialog("重命名", "输入新的名称", self.row_name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_name = dialog.get_text()
            if new_name:
                self.row_name = new_name
                self.name_label.setText(self.row_name)
                self.nameChanged.emit(self.row_name)
            
    def _on_color_clicked(self, btn):
        """色块点击事件"""
        # 由父组件处理
        pass
        
    def _show_color_context_menu(self, btn, pos):
        """显示色块右键菜单"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #333; border: 1px solid #444; }
            QMenu::item { padding: 6px 20px; color: #EEE; }
            QMenu::item:selected { background-color: #0078D4; }
        """)
        
        # 编辑颜色
        action_edit = QAction(I18N.tr("color_picker.edit_color", "编辑颜色"), menu)
        action_edit.triggered.connect(lambda: self._edit_color(btn))
        menu.addAction(action_edit)
        
        # 复制颜色
        action_copy = QAction(I18N.tr("color_picker.copy_hex", "复制HEX值"), menu)
        action_copy.triggered.connect(lambda: QApplication.clipboard().setText(btn.color))
        menu.addAction(action_copy)
        
        # 清除颜色
        action_delete = QAction(I18N.tr("color_picker.clear", "清除"), menu)
        action_delete.triggered.connect(lambda: self._clear_color(btn))
        menu.addAction(action_delete)
        
        menu.exec(btn.mapToGlobal(pos))
        
    def _edit_color(self, btn):
        """编辑色块颜色"""
        current_color = QColor(btn.color)
        color = QColorDialog.getColor(current_color, self, "选择颜色")
        if color.isValid():
            btn.set_color(color.name().upper())
            
    def _clear_color(self, btn):
        """清除色块"""
        btn.set_color("#333333")
        
    def get_colors(self):
        """获取当前所有颜色"""
        return [btn.color for btn in self.color_buttons]
        
    def update_row_index(self, new_index):
        """更新行索引"""
        self.row_index = new_index

    # 拖拽相关事件
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)
        
    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if not hasattr(self, '_drag_start_pos') or not self._drag_start_pos:
            return
            
        distance = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
        if distance < 10:
            return
            
        # 开始拖拽行
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setData("application/x-swatch-row", str(self.row_index).encode())
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.MoveAction)
        
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-swatch-row"):
            event.acceptProposedAction()
            
    def dropEvent(self, event):
        if event.mimeData().hasFormat("application/x-swatch-row"):
            source_index = int(event.mimeData().data("application/x-swatch-row").data().decode())
            target_index = self.row_index
            if source_index != target_index:
                self.rowMoved.emit(source_index, target_index)
            event.acceptProposedAction()

# ============================================================
# --- 色板页面组件 ---
# ============================================================
class SwatchPage(QWidget):
    """色板页面，支持多列、拖拽排序、双击改名"""
    
    # 默认色板数据
    DEFAULT_SWATCHES = [
        {"name": "基础色板", "colors": ["#FF0000", "#FF8C00", "#FFFF00", "#00FF00", "#00FFFF", 
                   "#0000FF", "#FF00FF", "#FFFFFF", "#000000", "#808080",
                   "#E0E0E0", "#C0C0C0", "#A0A0A0", "#909090", "#707070",
                   "#606060", "#505050", "#404040", "#303030", "#202020"]},
        {"name": "纯色色板", "colors": ["#FF4D4D", "#FF824D", "#FFB84D", "#FFED4D", "#DBFF4D",
                 "#A6FF4D", "#71FF4D", "#4DFF5F", "#4DFF94", "#4DFFCA",
                 "#4DFFFF", "#4DCAFF", "#4D94FF", "#4D5FFF", "#714DFF",
                 "#A64DFF", "#DB4DFF", "#FF4DED", "#FF4DB8", "#FF4D82"]},
        {"name": "冷调色板", "colors": ["#6FB4E9", "#70D2B8", "#F9AD76", "#B596E4", "#F39FB3",
                 "#EAD67E", "#7AA3D9", "#88CAA2", "#E4AA84", "#AE92D8",
                 "#F1A2B6", "#E6D18A", "#6A9CD6", "#7EC7AA", "#DFA47A",
                 "#A48CD0", "#F0B4C4", "#E2CC82", "#89A0BF", "#89C2B0"]},
        {"name": "网页配色", "colors": ["#1A3C5E", "#2B506E", "#3D647E", "#4E788F", "#5F8C9F", 
                "#40826D", "#51937E", "#62A48F", "#73B5A0", "#84C6B1", 
                "#5B3256", "#6C4367", "#7D5478", "#8E6589", "#9F769A", 
                "#FF6B35", "#FF7C46", "#FF8D57", "#FF9E68", "#FFAF79"]},
        {"name": "极简深色", "colors": ["#121212", "#181818", "#1E1E1E", "#242424", "#2C2C2C",
                "#333333", "#3B3B3B", "#444444", "#4D4D4D", "#555555",
                "#E0E0E0", "#B0B0B0", "#888888", "#666666", "#00FF85",
                "#1E90FF", "#FF0099", "#FFD700", "#FF4500", "#7D3CFF"]},
        {"name": "马卡龙色", "colors": ["#EDF2FB", "#D7E3FC", "#CCDBFD", "#C1D3FE", "#ABC4FF",
                "#FCE1E4", "#F8C2C2", "#FADADD", "#FFD1DC", "#FFC0CB",
                "#E8D1C5", "#EDDCD2", "#FFF1E6", "#F0EFEB", "#EEDDD3",
                "#DDEFEA", "#DAEAF6", "#E8DFF5", "#F2F5FF", "#55D6C2"]},
        {"name": "高对比色", "colors": ["#8931EF", "#F2CA19", "#FF00BD", "#0057E9", "#87E911",
                "#E11845", "#00BF7D", "#B3C7F7", "#C44601", "#5BA300",
                "#FB3522", "#9826DA", "#7DF804", "#FFEF00", "#FF9832", 
                "#2B81E2", "#0BAE65", "#347EF0", "#6138DF", "#D369BB"]},
        {"name": "自定义", "colors": ["#333333"] * 20, "locked": True}
    ]
    
    colorSelected = pyqtSignal(QColor)
    colorConfirmed = pyqtSignal(QColor)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window = parent
        # 尝试加载用户配置，如果不存在则使用默认配置
        config = load_config()
        # AUD-32407：用户配置文件是外部数据——结构畸形（swatches 非列表、
        # 行缺 name/colors 键、颜色非合法 hex）时回退默认色板，而不是让
        # _create_swatch_rows 抛 KeyError 使取色器构造失败。
        swatch_data = _validate_swatch_rows(config.get("swatches")) if config else None
        if swatch_data is not None:
            self.swatch_data = swatch_data
            # 加载上次选择的颜色
            last_color = config.get("last_color", "#FF0000")
            if not _is_valid_hex_color(last_color):
                last_color = "#FF0000"
            self.current_color = QColor(last_color)
            print(f"已加载用户色板配置，上次颜色: {last_color}")
        else:
            self.swatch_data = [row.copy() for row in self.DEFAULT_SWATCHES]
            self.current_color = QColor("#FF0000")
            print("使用默认色板配置")
        self.swatch_rows = []  # 存储所有行组件
        self.setup_ui()
        
    def setup_ui(self):
        """设置色板页面UI"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(2)  # 缩小间距
        
        # 当前颜色预览区域 - 颜色输入在左侧，预览框、吸管按钮、设置按钮对齐到右侧
        preview_layout = QHBoxLayout()
        preview_layout.setSpacing(4)
        preview_layout.addSpacing(140)  # 左侧留空120像素
        
        # 颜色值输入区域（左侧）
        color_inputs_layout = QVBoxLayout()
        color_inputs_layout.setSpacing(2)
        
        # HEX输入框
        hex_layout = QHBoxLayout()
        hex_layout.setSpacing(2)
        hex_layout.addWidget(QLabel(I18N.tr("color_picker.hex_label", "HEX:")))
        self.hex_edit = QLineEdit()
        self.hex_edit.setFixedSize(70, 22)
        self.hex_edit.setStyleSheet("background-color: #1A1A1A; border: 1px solid #333; border-radius: 4px; font-size: 11px; color: #FFF;")
        self.hex_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hex_edit.setText(I18N.tr("color_picker.hex_default", "#FF0000"))
        self.hex_edit.editingFinished.connect(self._on_hex_changed)
        self.hex_edit.returnPressed.connect(self._on_hex_changed)
        hex_layout.addWidget(self.hex_edit)
        color_inputs_layout.addLayout(hex_layout)
        
        # RGB输入框
        rgb_layout = QHBoxLayout()
        rgb_layout.setSpacing(2)
        self.rgb_edits = {}
        for key in ["R", "G", "B"]:
            rgb_layout.addWidget(QLabel(key + ":"))
            edit = SpinLineEdit(0, 255)
            edit.setFixedSize(36, 22)
            edit.setStyleSheet("background-color: #1A1A1A; border: 1px solid #333; border-radius: 4px; font-size: 11px; color: #FFF;")
            edit.setValue(255 if key == "R" else 0)
            edit.valueChanged.connect(lambda val, k=key: self._on_rgb_changed(k, val))
            # AUD-32405：SpinLineEdit.valueChanged 在滚轮/中键拖拽时连续
            # 触发中间值——中间值只同步预览，离散提交（editingFinished）
            # 才广播 colorSelected（进入历史记录）。
            edit.editingFinished.connect(lambda k=key: self._on_rgb_edit_committed(k))
            self.rgb_edits[key] = edit
            rgb_layout.addWidget(edit)
        color_inputs_layout.addLayout(rgb_layout)
        
        preview_layout.addLayout(color_inputs_layout)
        
        preview_layout.addStretch()
        
        # 当前颜色预览框（可拖拽）- 右侧
        self.preview = DraggablePreview()
        self.preview.setFixedSize(50, 50)
        self.preview.setStyleSheet("background-color: #FF0000; border-radius: 6px; border: 1px solid #444;")
        self.preview.setToolTip(I18N.tr(
            "color_picker.drag_to_swatches", "拖拽颜色到色块预设中保存"))
        preview_layout.addWidget(self.preview)
        
        # 吸管按钮 - 右侧
        self.btn_pick = IconButton(24)
        self.btn_pick.setObjectName("IconBtn")
        self.btn_pick.setFixedSize(50, 50)
        self.btn_pick.set_svg_data(_svg_with_fill(_DROPPER_SVG, "#FFFFFF"))
        self.btn_pick.setToolTip(I18N.tr("color_picker.pick_screen", "屏幕取色"))
        preview_layout.addWidget(self.btn_pick)
        
        # 设置按钮（切换工具）- 右侧
        self.btn_settings = IconButton(24)
        self.btn_settings.setObjectName("IconBtn")
        self.btn_settings.setFixedSize(50, 50)
        self.btn_settings.set_svg_data(_svg_with_fill(_GEAR_SVG, "#FFFFFF"))
        self.btn_settings.setToolTip(I18N.tr(
            "color_picker.switch_tool", "切换颜色选择工具"))
        preview_layout.addWidget(self.btn_settings)
        
        main_layout.addLayout(preview_layout)
        
        # 色板网格区域（滚动区域）
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: #252525; }")
        
        self.scroll_content = QFrame()
        self.scroll_content.setStyleSheet("background-color: #252525;")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setSpacing(0)  # 行间距设为0
        self.scroll_layout.setContentsMargins(4, 0, 4, 0)  # 上下边距设为0
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)  # 内容顶部对齐
        
        # 创建色板行
        self._create_swatch_rows()
        
        scroll_area.setWidget(self.scroll_content)
        main_layout.addWidget(scroll_area)
        
        # 操作按钮区域 - 所有按钮放到一排
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        
        # 添加当前颜色按钮
        btn_add = QPushButton(I18N.tr("color_picker.add_to_custom", "添加到自定义"))
        btn_add.setStyleSheet("""
            QPushButton { padding: 4px 10px; font-size: 10px; background-color: #0078D4; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #1083D8; }
        """)
        btn_add.clicked.connect(self._add_current_to_custom)
        btn_layout.addWidget(btn_add)
        
        # 添加新行按钮
        btn_add_row = QPushButton(I18N.tr("color_picker.add_row", "添加新行"))
        btn_add_row.setStyleSheet("""
            QPushButton { padding: 4px 10px; font-size: 10px; background-color: #4CAF50; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #5CBF60; }
        """)
        btn_add_row.clicked.connect(self._add_new_row)
        btn_layout.addWidget(btn_add_row)
        
        # 重置按钮
        btn_reset = QPushButton(I18N.tr("color_picker.reset", "重置"))
        btn_reset.setStyleSheet("""
            QPushButton { padding: 4px 10px; font-size: 10px; background-color: #FF6B6B; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #FF8B8B; }
        """)
        btn_reset.clicked.connect(self._reset_to_default)
        btn_layout.addWidget(btn_reset)
        
        # 导出按钮
        btn_export = QPushButton(I18N.tr("color_picker.export", "导出"))
        btn_export.setStyleSheet("""
            QPushButton { padding: 4px 10px; font-size: 10px; background: #2E7D32; border: none; border-radius: 4px; }
            QPushButton:hover { background: #3E8D42; }
        """)
        btn_export.clicked.connect(self._export_swatches)
        btn_layout.addWidget(btn_export)
        
        # 导入按钮
        btn_import = QPushButton(I18N.tr("color_picker.import", "导入"))
        btn_import.setStyleSheet("""
            QPushButton { padding: 4px 10px; font-size: 10px; background: #5D4037; border: none; border-radius: 4px; }
            QPushButton:hover { background: #6D5047; }
        """)
        btn_import.clicked.connect(self._import_swatches)
        btn_layout.addWidget(btn_import)
        
        # ASE导出按钮
        btn_export_ase = QPushButton(I18N.tr("color_picker.ase", "ASE"))
        btn_export_ase.setStyleSheet("""
            QPushButton { padding: 4px 10px; font-size: 10px; background: #6A1B9A; border: none; border-radius: 4px; }
            QPushButton:hover { background: #7A2BAA; }
        """)
        btn_export_ase.clicked.connect(self._export_ase)
        btn_layout.addWidget(btn_export_ase)
        
        btn_layout.addStretch()
        
        # 确定按钮
        btn_confirm = QPushButton(I18N.tr("color_picker.ok", "确定"))
        btn_confirm.setStyleSheet("""
            QPushButton { padding: 8px 24px; font-size: 12px; font-weight: bold; background: #FF8C00; border: none; border-radius: 6px; }
            QPushButton:hover { background: #FFA500; }
        """)
        btn_confirm.clicked.connect(self._confirm_color)
        btn_layout.addWidget(btn_confirm)
        
        main_layout.addLayout(btn_layout)
    
    def _create_swatch_rows(self):
        """创建色板行"""
        # 清除现有行
        for row in self.swatch_rows:
            row.deleteLater()
        self.swatch_rows.clear()
        
        # 创建新行
        for i, row_data in enumerate(self.swatch_data):
            row = DraggableSwatchRow(row_data["name"], row_data["colors"], i)
            row.is_locked = row_data.get("locked", False)
            if row.is_locked:
                row.grip_btn.setEnabled(False)
                row.grip_btn.setStyleSheet("opacity: 0.3;")
            row.rowMoved.connect(self._on_row_moved)
            row.nameChanged.connect(lambda name, idx=i: self._on_name_changed(idx, name))
            
            # 连接色块点击事件
            for btn in row.color_buttons:
                btn.clicked.connect(lambda _, b=btn: self._select_color(b.color))
                
            self.swatch_rows.append(row)
            self.scroll_layout.addWidget(row)
            
    def _on_row_moved(self, from_index, to_index):
        """处理行移动"""
        # 不允许移动锁定的行
        if self.swatch_rows[from_index].is_locked or self.swatch_rows[to_index].is_locked:
            return
            
        # 重新排序数据
        moved_row = self.swatch_data.pop(from_index)
        self.swatch_data.insert(to_index, moved_row)
        
        # 重建UI
        self._create_swatch_rows()
        
    def _on_name_changed(self, index, new_name):
        """处理名称改变"""
        self.swatch_data[index]["name"] = new_name
        
    def _select_color(self, color_hex):
        """选择颜色并更新预览和输入框"""
        self.current_color = QColor(color_hex)
        self.preview.set_color(color_hex)
        
        # 更新输入框
        self.hex_edit.setText(color_hex)
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        self.rgb_edits["R"].setValue(r)
        self.rgb_edits["G"].setValue(g)
        self.rgb_edits["B"].setValue(b)
        
        self.colorSelected.emit(self.current_color)
    
    def _on_hex_changed(self):
        """HEX输入框改变时处理"""
        hex_text = self.hex_edit.text().strip()
        if len(hex_text) == 7 and hex_text.startswith("#"):
            color = QColor(hex_text)
        elif len(hex_text) == 6:
            color = QColor("#" + hex_text)
        else:
            color = QColor()
        if color.isValid():
            self.current_color = color
            self.preview.set_color(color.name().upper())  # 更新可拖拽预览框的颜色
            r, g, b = color.red(), color.green(), color.blue()
            self.rgb_edits["R"].setValue(r)
            self.rgb_edits["G"].setValue(g)
            self.rgb_edits["B"].setValue(b)
            self.colorSelected.emit(color)
    
    def _on_rgb_changed(self, key, val):
        """RGB输入框改变时处理。

        AUD-32405：滚轮/中键拖拽的连续中间值只更新当前颜色、预览与
        HEX 文本，不发 colorSelected——该信号会写入 12 格颜色历史并在
        确认时持久化，连续中间值会挤掉用户真实历史。离散提交走
        ``_on_rgb_edit_committed``。
        """
        r = self.rgb_edits["R"].value()
        g = self.rgb_edits["G"].value()
        b = self.rgb_edits["B"].value()
        self.current_color = QColor(r, g, b)
        self.preview.set_color(self.current_color.name().upper())  # 更新可拖拽预览框的颜色
        self.hex_edit.setText(self.current_color.name().upper())

    def _on_rgb_edit_committed(self, key):
        """RGB 输入离散提交（editingFinished）时广播 colorSelected。"""
        self._on_rgb_changed(key, self.rgb_edits[key].value())
        self.colorSelected.emit(self.current_color)
    
    def _confirm_color(self):
        """确定选择颜色并保存配置"""
        # 更新数据中的颜色
        for i, row in enumerate(self.swatch_rows):
            self.swatch_data[i]["colors"] = row.get_colors()
        
        # 保存配置到文件（包括当前选择的颜色）
        current_color_hex = self.current_color.name().upper()
        save_config(self.swatch_data, current_color_hex)
        
        self.colorConfirmed.emit(self.current_color)
    
    def _add_current_to_custom(self):
        """将当前颜色添加到自定义色板"""
        color_hex = self.current_color.name().upper()
        # 找到自定义行（最后一行）
        custom_row = self.swatch_rows[-1]
        # 添加到第一个空位
        for i, btn in enumerate(custom_row.color_buttons):
            if btn.color == "#333333":
                btn.set_color(color_hex)
                self.swatch_data[-1]["colors"][i] = color_hex
                return
        # 如果没有空位，插入到第一个位置并后移
        colors = custom_row.get_colors()
        for i in range(len(colors) - 1, 0, -1):
            colors[i] = colors[i-1]
        colors[0] = color_hex
        for i, btn in enumerate(custom_row.color_buttons):
            btn.set_color(colors[i])
        self.swatch_data[-1]["colors"] = colors
    
    def _add_new_row(self):
        """添加新的自定义色板行"""
        dialog = MinimalInputDialog("添加新行", "输入色板名称", "新色板", self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_name = dialog.get_text()
            if new_name:
                new_row = {
                    "name": new_name,
                    "colors": ["#333333"] * 20,
                    "locked": False
                }
                self.swatch_data.insert(len(self.swatch_data) - 1, new_row)
                self._create_swatch_rows()
    
    def _reset_to_default(self):
        """重置色板为默认值"""
        dialog = MinimalConfirmDialog("重置色板", "确定要重置所有色板为默认值吗？\n此操作不可撤销。", self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.swatch_data = [row.copy() for row in self.DEFAULT_SWATCHES]
            self._create_swatch_rows()
    
    def _export_swatches(self):
        """导出色板到JSON文件"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出色板预设",
            f"color_palette_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            "JSON Files (*.json)"
        )
        if file_path:
            # 更新数据
            for i, row in enumerate(self.swatch_rows):
                self.swatch_data[i]["colors"] = row.get_colors()
                
            swatch_data = {
                "name": "Color Inspector Palette",
                "version": "2.0",
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "swatches": self.swatch_data
            }
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(swatch_data, f, indent=2, ensure_ascii=False)
                QMessageBox.information(self, "导出成功", f"色板已保存到:\n{file_path}")
            except Exception as e:
                QMessageBox.warning(self, "导出失败", f"保存文件时出错:\n{str(e)}")
    
    def _import_swatches(self):
        """从JSON文件导入色板"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "导入色板预设", "", "JSON Files (*.json)"
        )
        if file_path:
            # AUD-32403：导入是外部数据入口——先读取并整体校验，全部合法
            # 才替换 swatch_data。此前直接整体替换后才构建 UI，畸形数据
            # （行缺 name/colors 键、颜色非法）导致 KeyError 被
            # QMessageBox 兜住时 UI 已半构建、数据已替换，且"最后一行是
            # 自定义行"的假设（_add_current_to_custom）也不再成立。
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                QMessageBox.warning(self, "导入失败", f"读取文件时出错:\n{str(e)}")
                return
            if not isinstance(data, dict):
                QMessageBox.warning(self, "导入失败", "文件格式不正确：缺少 swatches 字段")
                return
            swatch_data = _validate_swatch_rows(data.get("swatches"))
            if swatch_data is None:
                QMessageBox.warning(
                    self, "导入失败",
                    "色板数据结构不合法：\n每行需要 name（非空字符串）与 "
                    "colors（#RRGGBB 颜色列表）字段")
                return
            self.swatch_data = swatch_data
            self._create_swatch_rows()
            QMessageBox.information(self, "导入成功", "色板已成功导入")
    
    def _export_ase(self):
        """导出Adobe ASE格式色板文件"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出ASE色板",
            f"color_palette_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ase",
            "ASE Files (*.ase)"
        )
        if file_path:
            try:
                self._write_ase_file(file_path)
                QMessageBox.information(self, "导出成功", f"ASE色板已保存到:\n{file_path}")
            except Exception as e:
                QMessageBox.warning(self, "导出失败", f"保存ASE文件时出错:\n{str(e)}")
    
    def _write_ase_file(self, file_path):
        """写入ASE格式文件（Adobe Swatch Exchange）。

        AUD-32404：ASE 块结构为 ``uint16 块类型 + uint32 块长度 + 块数据``。
        此前每个颜色块缺少 4 字节块长度字段，且头部颜色计数把被跳过的
        #333333 占位色计入（声明数 > 实际块数），导出文件无法被
        Photoshop 等软件解析。现在头部计数 == 实际写入块数，每块先在
        内存组装 payload 再写 ``type + len(payload) + payload``。
        """
        exported_colors = [
            color_hex
            for row in self.swatch_rows
            for color_hex in row.get_colors()
            if color_hex != "#333333"
        ]
        with open(file_path, 'wb') as f:
            f.write(b'ASEF')
            f.write(struct.pack('>H', 1))  # version major
            f.write(struct.pack('>H', 0))  # version minor
            f.write(struct.pack('>I', len(exported_colors)))  # 实际块数

            for color_hex in exported_colors:
                name_bytes = color_hex.encode('utf-16-be')
                payload = (
                    struct.pack('>H', len(color_hex) + 1)  # 名称长度含终止符
                    + name_bytes
                    + b'\x00\x00'                          # UTF-16 null 终止符
                    + b'RGB '                              # 颜色模型
                    + struct.pack('>f', QColor(color_hex).redF())
                    + struct.pack('>f', QColor(color_hex).greenF())
                    + struct.pack('>f', QColor(color_hex).blueF())
                    + struct.pack('>H', 0)                 # 颜色类型: global
                )
                f.write(struct.pack('>H', 0x0001))          # 块类型: color entry
                f.write(struct.pack('>I', len(payload)))   # 块长度（字节数）
                f.write(payload)
    
    def set_current_color(self, color: QColor):
        """从主窗口同步当前颜色"""
        self.current_color = color
        color_hex = color.name().upper()
        self.preview.set_color(color_hex)  # 更新可拖拽预览框的颜色
        self.hex_edit.setText(color_hex)
        r, g, b = color.red(), color.green(), color.blue()
        self.rgb_edits["R"].setValue(r)
        self.rgb_edits["G"].setValue(g)
        self.rgb_edits["B"].setValue(b)

# ============================================================
# --- 拾色器页面组件 ---
# ============================================================
class PickerPage(QWidget):
    """拾色器页面，包含色彩方块、色相滑块和参数输入"""
    
    colorConfirmed = pyqtSignal(QColor)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window = parent
        self.hue, self.sat, self.val = 0, 255, 255
        self.alpha = 255
        self._updating = False
        self.setup_ui()
        
    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)
        
        # 顶部区域：色彩方块 + 色相滑块 + 右侧参数面板
        top_hbox = QHBoxLayout()
        top_hbox.setSpacing(6)
        
        # 色彩方块
        self.square = ColorSquare()
        top_hbox.addWidget(self.square)
        
        # 色相滑块
        self.slider = HueSlider()
        top_hbox.addWidget(self.slider)
        
        # 右侧参数面板
        side_container = QFrame()
        side_container.setFixedSize(180, 250)
        side_panel = QVBoxLayout(side_container)
        side_panel.setSpacing(3)
        side_panel.setContentsMargins(2, 0, 2, 0)
        
        # 预览与吸管按钮 - 右对齐布局
        head_layout = QHBoxLayout()
        head_layout.setSpacing(4)
        
        head_layout.addStretch()
        
        self.preview = DraggablePreview()
        self.preview.setFixedSize(50, 50)
        self.preview.set_color("#FF0000")
        self.preview.setToolTip(I18N.tr(
            "color_picker.drag_to_custom", "拖拽颜色到自定义色块中保存"))
        head_layout.addWidget(self.preview)
        
        self.btn_pick = IconButton(24)
        self.btn_pick.setObjectName("IconBtn")
        self.btn_pick.setFixedSize(50, 50)
        self.btn_pick.set_svg_data(_svg_with_fill(_DROPPER_SVG, "#FFFFFF"))
        head_layout.addWidget(self.btn_pick)
        
        # 设置按钮（齿轮图标）
        self.btn_settings = IconButton(24)
        self.btn_settings.setObjectName("IconBtn")
        self.btn_settings.setFixedSize(50, 50)
        self.btn_settings.set_svg_data(_svg_with_fill(_GEAR_SVG, "#FFFFFF"))
        self.btn_settings.setToolTip(I18N.tr(
            "color_picker.switch_tool", "切换颜色选择工具"))
        head_layout.addWidget(self.btn_settings)
        
        side_panel.addLayout(head_layout)
        
        # 参数输入区域
        self.edits = {}
        grid = QGridLayout()
        grid.setVerticalSpacing(1)
        grid.setHorizontalSpacing(2)
        
        edit_ranges = {
            "R": (0, 255), "G": (0, 255), "B": (0, 255),
            "H": (0, 359), "S": (0, 100), "L": (0, 100),
            "L_lab": (0, 100), "a": (-128, 127), "b": (-128, 127),
        }
        
        # RGB
        row = 0
        rgb_layout = QHBoxLayout()
        rgb_layout.setSpacing(2)
        for key in ["R", "G", "B"]:
            rgb_layout.addWidget(QLabel(key + ":"))
            min_v, max_v = edit_ranges[key]
            edit = SpinLineEdit(min_v, max_v)
            edit.setFixedSize(36, 22)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            edit.valueChanged.connect(lambda val, k=key: self.on_spin_changed(k, val))
            self.edits[key] = edit
            rgb_layout.addWidget(edit)
        grid.addLayout(rgb_layout, row, 0, 1, 2)
        row += 1
        
        # HSL
        hsl_layout = QHBoxLayout()
        hsl_layout.setSpacing(2)
        for key in ["H", "S", "L"]:
            hsl_layout.addWidget(QLabel(key + ":"))
            min_v, max_v = edit_ranges[key]
            edit = SpinLineEdit(min_v, max_v)
            edit.setFixedSize(36, 22)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            edit.valueChanged.connect(lambda val, k=key: self.on_spin_changed(k, val))
            self.edits[key] = edit
            hsl_layout.addWidget(edit)
        grid.addLayout(hsl_layout, row, 0, 1, 2)
        row += 1
        
        # Lab
        lab_layout = QHBoxLayout()
        lab_layout.setSpacing(2)
        for key in ["L_lab", "a", "b"]:
            label_text = "L:" if key == "L_lab" else key + ":"
            lab_layout.addWidget(QLabel(label_text))
            min_v, max_v = edit_ranges[key]
            edit = SpinLineEdit(min_v, max_v)
            edit.setFixedSize(36, 22)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            edit.valueChanged.connect(lambda val, k=key: self.on_spin_changed(k, val))
            self.edits[key] = edit
            lab_layout.addWidget(edit)
        grid.addLayout(lab_layout, row, 0, 1, 2)
        row += 1
        
        # HEX
        hex_layout = QHBoxLayout()
        hex_layout.setSpacing(2)
        hex_layout.addWidget(QLabel(I18N.tr("color_picker.hex_label", "HEX:")))
        hex_edit = QLineEdit()
        hex_edit.setFixedSize(80, 22)
        hex_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hex_edit.editingFinished.connect(self.on_hex_changed)
        hex_edit.returnPressed.connect(self.on_hex_changed)
        self.edits["#"] = hex_edit
        hex_layout.addWidget(hex_edit)
        hex_layout.addStretch()
        grid.addLayout(hex_layout, row, 0, 1, 2)
        
        side_panel.addLayout(grid)
        
        # 透明度控制行：标签 + 滑块 + 数值框
        alpha_row = QHBoxLayout()
        alpha_row.setSpacing(4)
        alpha_row.addWidget(QLabel(I18N.tr("color_picker.alpha_label", "A:")))
        
        self.alpha_slider = AlphaSlider()
        alpha_row.addWidget(self.alpha_slider)
        
        self.alpha_edit = SpinLineEdit(0, 255)
        self.alpha_edit.setFixedSize(40, 22)
        self.alpha_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.alpha_edit.setValue(255)
        self.alpha_edit.valueChanged.connect(self._on_alpha_changed)
        alpha_row.addWidget(self.alpha_edit)
        
        self.alpha_slider.alphaChanged.connect(self._on_alpha_slider_changed)
        side_panel.addLayout(alpha_row)
        
        # 历史记录（4排布局）
        history_label = QLabel(I18N.tr("color_picker.history", "历史"))
        history_label.setStyleSheet("font-size: 10px; color: #888; font-weight: bold;")
        side_panel.addWidget(history_label)
        
        self.color_history = ColorHistory()
        self.color_history.colorSelected.connect(self.load_qcolor)
        side_panel.addWidget(self.color_history)
        
        side_panel.addStretch()
        
        # 添加和确定按钮（并列显示，右对齐）
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        btn_layout.addStretch()
        
        self.btn_add = QPushButton(I18N.tr("color_picker.add_to_custom", "添加到自定义"))
        self.btn_add.setStyleSheet("""
            QPushButton { padding: 3px 8px; font-size: 10px; background: #4D606F; border: none; border-radius: 4px; }
            QPushButton:hover { background: #1083D8; }
        """)
        btn_layout.addWidget(self.btn_add)
        
        self.btn_confirm = QPushButton(I18N.tr("color_picker.ok", "确定"))
        self.btn_confirm.setFixedSize(80, 30)
        self.btn_confirm.setStyleSheet("""
            QPushButton { font-size: 13px; font-weight: bold; background: #FF8C00; border: none; border-radius: 6px; }
            QPushButton:hover { background: #FFA500; }
        """)
        self.btn_confirm.clicked.connect(self._confirm_color)
        btn_layout.addWidget(self.btn_confirm)
        
        side_panel.addLayout(btn_layout)
        
        top_hbox.addWidget(side_container)
        main_layout.addLayout(top_hbox)
        
        # 底部色板区域（快捷预设）- 显示前3行 + 自定义行
        swatch_container = QVBoxLayout()
        swatch_container.setSpacing(2)  # 保持原始间距
        
        # 预设颜色数据
        preset_colors = [
            ("基础", ["#FF0000", "#FF8C00", "#FFFF00", "#00FF00", "#00FFFF", 
                     "#0000FF", "#FF00FF", "#FFFFFF", "#000000", "#808080",
                     "#E0E0E0", "#C0C0C0", "#A0A0A0", "#909090", "#707070",
                     "#606060", "#505050", "#404040", "#303030", "#202020"]),
            ("纯色", ["#FF4D4D", "#FF824D", "#FFB84D", "#FFED4D", "#DBFF4D",
                     "#A6FF4D", "#71FF4D", "#4DFF5F", "#4DFF94", "#4DFFCA",
                     "#4DFFFF", "#4DCAFF", "#4D94FF", "#4D5FFF", "#714DFF",
                     "#A64DFF", "#DB4DFF", "#FF4DED", "#FF4DB8", "#FF4D82"]),
            ("冷调", ["#6FB4E9", "#70D2B8", "#F9AD76", "#B596E4", "#F39FB3",
                     "#EAD67E", "#7AA3D9", "#88CAA2", "#E4AA84", "#AE92D8",
                     "#F1A2B6", "#E6D18A", "#6A9CD6", "#7EC7AA", "#DFA47A",
                     "#A48CD0", "#F0B4C4", "#E2CC82", "#89A0BF", "#89C2B0"]),
        ]
        
        self.preset_swatches = []
        for label_text, colors in preset_colors:
            row_layout = QHBoxLayout()
            row_layout.setSpacing(1)
            
            lbl = QLabel(label_text)
            lbl.setFixedSize(55, 16)  # 五个字符宽度
            lbl.setStyleSheet("font-size: 11px; color: #888; font-weight: bold;")
            row_layout.addWidget(lbl)
            
            row_buttons = []
            for color in colors:
                btn = SwatchButton(color)
                btn.clicked.connect(lambda _, c=color: self.load_qcolor(QColor(c)))
                row_buttons.append(btn)
                row_layout.addWidget(btn)
            self.preset_swatches.append(row_buttons)
            row_layout.addStretch()
            swatch_container.addLayout(row_layout)
        
        # 自定义行（始终保留）
        custom_row = QHBoxLayout()
        custom_row.setSpacing(1)
        
        lbl_custom = QLabel(I18N.tr("color_picker.custom", "自定义"))
        lbl_custom.setFixedSize(55, 16)  # 五个字符宽度
        lbl_custom.setStyleSheet("font-size: 11px; color: #888; font-weight: bold;")
        custom_row.addWidget(lbl_custom)
        
        self.custom_swatches = []
        for i in range(20):
            btn = DraggableColorButton("#333333")
            btn.clicked.connect(lambda _, b=btn: self.load_color_from_swatch(b))
            self.custom_swatches.append(btn)
            custom_row.addWidget(btn)
        custom_row.addStretch()
        swatch_container.addLayout(custom_row)
        
        main_layout.addLayout(swatch_container)
        
        # 信号绑定
        self.square.colorChanged.connect(self.update_sv)
        self.slider.hueChanged.connect(self.update_h)
        
    def on_spin_changed(self, key, val):
        if key in ["R", "G", "B"]:
            r = self.edits["R"].value() if key != "R" else val
            g = self.edits["G"].value() if key != "G" else val
            b = self.edits["B"].value() if key != "B" else val
            self.load_qcolor(QColor(r, g, b))
        elif key in ["H", "S", "L"]:
            h = self.edits["H"].value() if key != "H" else val
            s = self.edits["S"].value() if key != "S" else val
            l = self.edits["L"].value() if key != "L" else val
            self.load_qcolor(QColor.fromHsl(h, int(s * 2.55), int(l * 2.55)))
        elif key in ["L_lab", "a", "b"]:
            L_val = self.edits["L_lab"].value() if key != "L_lab" else val
            a_val = self.edits["a"].value() if key != "a" else val
            b_val = self.edits["b"].value() if key != "b" else val
            r, g, b = lab_to_rgb(L_val, a_val, b_val)
            self.load_qcolor(QColor(r, g, b))
    
    def on_hex_changed(self):
        hex_text = self.edits["#"].text().strip()
        if len(hex_text) == 7 and hex_text.startswith("#"):
            color = QColor(hex_text)
        elif len(hex_text) == 6:
            color = QColor("#" + hex_text)
        else:
            color = QColor()
        if color.isValid():
            self.load_qcolor(color)

    def _on_alpha_slider_changed(self, alpha):
        """透明度滑块改变時触发"""
        self.alpha = alpha
        if not self.alpha_edit.hasFocus():
            self.alpha_edit.setValue(alpha)
        self._update_preview_alpha()

    def _on_alpha_changed(self, val):
        """透明度数值框改变時触发"""
        self.alpha = val
        self.alpha_slider.alpha = val
        self.alpha_slider.update()
        self._update_preview_alpha()

    def _update_preview_alpha(self):
        """更新预览色的透明度"""
        color = QColor.fromHsv(self.hue, self.sat, self.val)
        color.setAlpha(self.alpha)
        self.preview.set_color(color)

    def update_h(self, h):
        self.hue = h
        self.square.hue = h
        self.square.update()
        self.sync_all()

    def update_sv(self, s, v):
        self.sat, self.val = s, v
        self.sync_all()

    def load_qcolor(self, color):
        self.hue = color.hsvHue() if color.hsvHue() != -1 else 0
        self.sat = color.hsvSaturation()
        self.val = color.value()
        self.alpha = color.alpha()
        self.square.hue, self.square.sat, self.square.val = self.hue, self.sat, self.val
        self.slider.hue = self.hue
        self.square.update()
        self.slider.update()
        self.sync_all()

    def load_color_from_swatch(self, btn):
        hex_val = btn.color
        if hex_val and hex_val != "#333333":
            self.load_qcolor(QColor(hex_val))

    def sync_all(self):
        if self._updating:
            return
        self._updating = True
        color = QColor.fromHsv(self.hue, self.sat, self.val)
        r, g, b = color.red(), color.green(), color.blue()
        
        if not self.edits["R"].hasFocus():
            self.edits["R"].setValue(r)
        if not self.edits["G"].hasFocus():
            self.edits["G"].setValue(g)
        if not self.edits["B"].hasFocus():
            self.edits["B"].setValue(b)
        
        h = color.hslHue() if color.hslHue() != -1 else 0
        s = int(color.hslSaturationF() * 100)
        l = int(color.lightnessF() * 100)
        
        if not self.edits["H"].hasFocus():
            self.edits["H"].setValue(h)
        if not self.edits["S"].hasFocus():
            self.edits["S"].setValue(s)
        if not self.edits["L"].hasFocus():
            self.edits["L"].setValue(l)
        
        L_lab, a_val, b_val = rgb_to_lab(r, g, b)
        
        if not self.edits["L_lab"].hasFocus():
            self.edits["L_lab"].setValue(L_lab)
        if not self.edits["a"].hasFocus():
            self.edits["a"].setValue(a_val)
        if not self.edits["b"].hasFocus():
            self.edits["b"].setValue(b_val)
        
        if not self.edits["#"].hasFocus():
            self.edits["#"].setText(color.name().upper())
        
        # 同步透明度
        self.alpha_slider.set_color(color)
        self.alpha_slider.alpha = self.alpha
        self.alpha_slider.update()
        if not self.alpha_edit.hasFocus():
            self.alpha_edit.setValue(self.alpha)
        
        # 预览色带透明度
        color.setAlpha(self.alpha)
        self.preview.set_color(color)
        
        self._updating = False
    
    def _confirm_color(self):
        """确定选择颜色"""
        color = QColor.fromHsv(self.hue, self.sat, self.val)
        color.setAlpha(self.alpha)
        self.colorConfirmed.emit(color)
    
    def get_current_color(self):
        """获取当前颜色"""
        color = QColor.fromHsv(self.hue, self.sat, self.val)
        color.setAlpha(self.alpha)
        return color
    
    def add_to_custom_swatch(self):
        """添加当前颜色到自定义色板"""
        color_hex = self.edits["#"].text()
        for i in range(len(self.custom_swatches)-1, 0, -1):
            prev_color = self.custom_swatches[i-1].color
            self.custom_swatches[i].set_color(prev_color)
        self.custom_swatches[0].set_color(color_hex)

# ============================================================
# --- 主窗口 ---
# ============================================================
class CompactColorStudio(QWidget):
    # 公开信号：颜色确认（点击确认按钮时 emit）和实时变化（拖动选色时 emit）
    colorConfirmed = pyqtSignal(QColor)
    colorLiveChanged = pyqtSignal(QColor)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(I18N.tr("color_picker.title", "Color Inspector"))
        self.setFixedSize(480, 360)
        self._updating = False
        # 始终置顶：作为独立顶层窗口弹出时，点击主窗口不会把它挡到后面。
        # 这对于取色器很重要——用户需要边调色边看到主窗口的实时预览。
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self.setStyleSheet("""
            QWidget { background-color: #252525; color: #EEE; font-family: 'PingFang SC', 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif; }
            QLineEdit { background-color: #1A1A1A; border: 1px solid #333; border-radius: 4px; font-size: 11px; color: #FFF; }
            QLabel { font-size: 10px; color: #888; font-weight: bold; }
            QPushButton#IconBtn { background-color: #333; border: 1px solid #444; border-radius: 8px; padding: 0; }
            QPushButton#IconBtn:hover { background-color: #444; }
            QPushButton#Swatch { border: none; border-radius: 3px; }
            QPushButton#Swatch:hover { border: 1px solid #666; }
        """)

        self.initUI()

        # 吸管监听器
        self.dropper = EyeDropperOverlay()
        self.dropper.colorPicked.connect(self._on_color_picked)

        # 实时颜色变化：PickerPage.sync_all 是颜色变化的核心同步点，
        # 通过 monkey-patch 在每次 sync_all 后 emit colorLiveChanged。
        _orig_sync = self.picker_page.sync_all
        def _sync_with_signal():
            _orig_sync()
            self.colorLiveChanged.emit(self.picker_page.get_current_color())
        self.picker_page.sync_all = _sync_with_signal

        # 事件过滤器：只过滤自身（而非全局 QApplication），避免污染主窗口事件流。
        # 原来用 QApplication.instance().installEventFilter(self) 会在嵌入主窗口时
        # 拦截整个 app 的鼠标事件。
        self.installEventFilter(self)

    def initUI(self):
        main_vbox = QVBoxLayout(self)
        main_vbox.setContentsMargins(6, 2, 6, 2)
        main_vbox.setSpacing(2)
        
        # 页面堆叠容器
        self.page_stack = QStackedWidget()
        
        # 拾色器页面
        self.picker_page = PickerPage(self)
        self.picker_page.colorConfirmed.connect(self._confirm_and_close)
        self.picker_page.btn_pick.clicked.connect(self.start_picking)
        self.picker_page.btn_add.clicked.connect(self.picker_page.add_to_custom_swatch)
        self.picker_page.btn_settings.clicked.connect(self._show_tools_menu)
        self.page_stack.addWidget(self.picker_page)
        
        # 色板页面
        self.swatch_page = SwatchPage(self)
        self.swatch_page.colorSelected.connect(self._on_swatch_color_selected)
        self.swatch_page.colorConfirmed.connect(self._confirm_and_close)
        self.swatch_page.btn_settings.clicked.connect(self._show_tools_menu)
        self.swatch_page.btn_pick.clicked.connect(self.start_picking)
        self.page_stack.addWidget(self.swatch_page)
        
        main_vbox.addWidget(self.page_stack)
        
        # 设置当前页面为拾色器
        self.current_tool = "picker"
        self.page_stack.setCurrentWidget(self.picker_page)
        
        # 恢复上次选择的颜色到拾色器页面
        self.picker_page.load_qcolor(self.swatch_page.current_color)
        
        # 恢复PickerPage的自定义色板（AUD-32407：逐项校验外部持久化数据）
        config = load_config()
        if config and "picker_custom_swatches" in config:
            picker_custom = config["picker_custom_swatches"]
            if isinstance(picker_custom, list):
                for i, color in enumerate(picker_custom):
                    if i < len(self.picker_page.custom_swatches) and _is_valid_hex_color(color):
                        self.picker_page.custom_swatches[i].set_color(color)

        # 恢复历史记录（AUD-32407：同上逐项校验）
        if config and "color_history" in config:
            history = config["color_history"]
            if isinstance(history, list):
                for color_hex in history:
                    if _is_valid_hex_color(color_hex):
                        self.picker_page.color_history.add_color(QColor(color_hex))
        
        # 设置对话框（单例）
        self.settings_dialog = None

        # 回车键快捷键：相当于点击当前页面的"确定"按钮。
        # Return（主键盘回车）和 Enter（数字小键盘回车）都支持。
        from PyQt6.QtGui import QShortcut
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            QShortcut(key, self, activated=self._confirm_current_page)

    def _confirm_current_page(self):
        """回车快捷键：确认当前显示页面的颜色。"""
        current = self.page_stack.currentWidget()
        if current is self.picker_page:
            self.picker_page._confirm_color()
        elif current is self.swatch_page:
            self.swatch_page._confirm_color()
    
    def _show_tools_menu(self):
        """显示工具切换菜单"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #333; border: 1px solid #444; border-radius: 6px; }
            QMenu::item { padding: 8px 20px; color: #EEE; border-radius: 4px; }
            QMenu::item:selected { background-color: #0078D4; }
        """)
        
        # 拾色器选项
        action_picker = QAction(I18N.tr("color_picker.tool_picker", "拾色器"), menu)
        action_picker.triggered.connect(lambda: self._switch_tool("picker"))
        menu.addAction(action_picker)
        
        # 色板选项
        action_swatch = QAction(I18N.tr("color_picker.tool_swatch", "色板"), menu)
        action_swatch.triggered.connect(lambda: self._switch_tool("swatch"))
        menu.addAction(action_swatch)
        
        menu.addSeparator()
        
        # 设置面板选项（整合所有新功能）
        action_settings = QAction(I18N.tr("color_picker.tool_settings", "设置面板..."), menu)
        action_settings.triggered.connect(self._show_settings_dialog)
        menu.addAction(action_settings)
        
        menu.addSeparator()
        
        # 未来可扩展的选项（占位）
        action_wheel = QAction(I18N.tr("color_picker.tool_wheel", "色轮 (待开发)"), menu)
        action_wheel.setEnabled(False)
        menu.addAction(action_wheel)
        
        action_cube = QAction(I18N.tr("color_picker.tool_cube", "亮度立方体 (待开发)"), menu)
        action_cube.setEnabled(False)
        menu.addAction(action_cube)
        
        # 根据当前页面确定按钮位置
        if self.current_tool == "picker":
            btn = self.picker_page.btn_settings
        else:
            btn = self.swatch_page.btn_settings
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))
    
    def _show_settings_dialog(self):
        """显示设置面板"""
        current_color = self.picker_page.get_current_color()
        
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(current_color, self)
            self.settings_dialog.colorApplied.connect(self._on_settings_color_applied)
            self.settings_dialog.resetRequested.connect(self._on_reset_settings)
        
        self.settings_dialog.set_color(current_color)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()
    
    def _on_settings_color_applied(self, color):
        """从设置面板应用颜色"""
        self.picker_page.load_qcolor(color)
        self.swatch_page.set_current_color(color)
        self.picker_page.color_history.add_color(color)
        
    def _on_reset_settings(self):
        """恢复默认设置"""
        # 重置色板数据
        self.swatch_page.swatch_data = [row.copy() for row in SwatchPage.DEFAULT_SWATCHES]
        self.swatch_page._create_swatch_rows()
        
        # 重置PickerPage自定义色板
        for btn in self.picker_page.custom_swatches:
            btn.set_color("#333333")
        
        # 清空历史记录
        self.picker_page.color_history.history.clear()
        self.picker_page.color_history._update_display()
        
        # 重置当前颜色为红色
        default_color = QColor("#FF0000")
        self.picker_page.load_qcolor(default_color)
        self.swatch_page.set_current_color(default_color)
        
        # 删除配置文件
        try:
            if CONFIG_FILE_PATH.exists():
                CONFIG_FILE_PATH.unlink()
                print("配置文件已删除")
        except Exception as e:
            print(f"删除配置文件失败: {e}")
    
    def _switch_tool(self, tool_name):
        """切换颜色选择工具"""
        self.current_tool = tool_name
        
        if tool_name == "picker":
            self.page_stack.setCurrentWidget(self.picker_page)
        elif tool_name == "swatch":
            # 同步当前颜色到色板页面
            current_color = self.picker_page.get_current_color()
            self.swatch_page.set_current_color(current_color)
            self.page_stack.setCurrentWidget(self.swatch_page)
    
    def start_picking(self):
        """启动吸管"""
        self.dropper.start()
    
    def _on_color_picked(self, color):
        """吸管拾取颜色后处理"""
        self.picker_page.load_qcolor(color)
        self.swatch_page.set_current_color(color)
        self.picker_page.color_history.add_color(color)
        
    def _on_swatch_color_selected(self, color):
        """色板选择颜色后同步到拾色器页面"""
        self.picker_page.load_qcolor(color)
        self.picker_page.color_history.add_color(color)
    
    def eventFilter(self, obj, event):
        """事件过滤器：处理点击其他地方退出编辑状态"""
        if event.type() == event.Type.MouseButtonPress:
            # 获取当前焦点控件
            focused_widget = QApplication.focusWidget()
            
            # 如果焦点控件是输入框（SpinLineEdit 或普通 QLineEdit）
            if focused_widget and isinstance(focused_widget, QLineEdit):
                # 检查点击是否发生在输入框外部
                click_pos = event.globalPosition().toPoint()
                widget_rect = focused_widget.rect()
                widget_top_left = focused_widget.mapToGlobal(widget_rect.topLeft())
                widget_bottom_right = focused_widget.mapToGlobal(widget_rect.bottomRight())
                global_rect = QRect(widget_top_left, widget_bottom_right)
                
                # 如果点击在输入框外部，清除焦点以触发 editingFinished
                if not global_rect.contains(click_pos):
                    focused_widget.clearFocus()
        
        return super().eventFilter(obj, event)

    def _confirm_and_close(self, color):
        """确定颜色并关闭窗口"""
        self.picker_page.color_history.add_color(color)

        r, g, b = color.red(), color.green(), color.blue()
        L_lab, a_val, b_val = rgb_to_lab(r, g, b)

        # 保存PickerPage的自定义色板颜色
        picker_custom_colors = []
        for btn in self.picker_page.custom_swatches:
            picker_custom_colors.append(btn.color)

        # 保存历史记录
        history_colors = self.picker_page.color_history.history.copy()

        # 保存配置（包括色板数据、当前颜色、PickerPage自定义色板、历史记录）
        current_color_hex = color.name().upper()
        save_config(self.swatch_page.swatch_data, current_color_hex, picker_custom_colors, history_colors)

        # 向外通知确认的颜色（作为库使用时，调用方通过 colorConfirmed 信号获取结果）
        self.colorConfirmed.emit(color)

        # 在 macOS 上，如果在 QPushButton 的鼠标事件处理中立即关闭这个
        # 独立顶层窗口，剩余的鼠标释放事件可能穿透到下面的 QDialog，
        # 从而把“新建合成片段”窗口一并关闭。延迟到当前事件完成后再关窗，
        # 不影响 get_color 返回颜色，也不会改变 Project Settings 的行为。
        QTimer.singleShot(0, self.close)

    def set_current_color(self, color: QColor):
        """设置当前颜色（用于初始化）。

        调用方在弹出选择器前设置初始颜色，让用户从当前色开始调整。
        """
        self.picker_page.load_qcolor(color)

    def get_current_color(self) -> QColor:
        """获取当前选中的颜色。"""
        return self.picker_page.get_current_color()


# ============================================================
# 模块级公共 API：模态取色对话框
# ============================================================

def get_color(parent=None, initial=None, on_live=None):
    """弹出色彩选择器，返回选中颜色（确认）或 None（取消）。

    这是整个项目的统一颜色选取入口，替代直接使用 QColorDialog。

    实现注意：CompactColorStudio 内部有屏幕吸管功能（EyeDropperOverlay），
    吸管需要一个能自由接收鼠标事件的全屏透明覆盖层。如果用模态
    QDialog.exec() 包裹，模态事件循环会阻止覆盖层接收事件，导致吸管
    点击后"卡死"。因此这里用 CompactColorStudio 作为独立顶层窗口 +
    QEventLoop 等待结果，而非模态对话框。

    Args:
        parent: 父 widget。
        initial: 初始 QColor；None 则默认黑色。
        on_live: 可选回调 ``on_live(QColor)``，在用户拖动选色时实时调用，
                 用于实时预览（等价于 QColorDialog.currentColorChanged）。

    Returns:
        确认的 QColor，或 None（用户关闭窗口未确认）。
    """
    from PyQt6.QtCore import QEventLoop

    # CompactColorStudio 本身就是 QWidget，可以直接作为顶层窗口弹出。
    # 不设 parent，避免被父窗口的模态性影响。
    studio = CompactColorStudio()
    if initial is not None:
        studio.set_current_color(initial)

    # 窗口居中：优先在父窗口所在屏幕居中；无 parent 时在主屏幕居中。
    # 用 show() 后的屏幕几何来计算，确保坐标基于实际显示的屏幕。
    studio.show()
    target_screen = None
    if parent is not None:
        target_screen = QApplication.screenAt(parent.geometry().center())
    if target_screen is None:
        target_screen = QApplication.primaryScreen()
    if target_screen is not None:
        sg = target_screen.availableGeometry()
        fg = studio.frameGeometry()
        studio.move(
            sg.center().x() - fg.width() // 2,
            sg.center().y() - fg.height() // 2
        )

    # AUD-32402: 显式保持默认的 WA_DeleteOnClose=False——closeEvent 被
    # 下方 monkey-patch 包裹，若开启 delete-on-close，窗口销毁后
    # ``_orig_close(event)`` 会触碰已销毁的 C++ 对象抛 RuntimeError。
    # 选择器 widget 树（两页 UI、8 行色板共数百个 QObject）改由事件循环
    # 退出后统一 ``deleteLater()`` 释放（见函数末尾），不再每次取色泄漏
    # 一整棵树。
    studio.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

    loop = QEventLoop()
    result = {"color": None}

    def _on_confirmed(color):
        result["color"] = color
        loop.quit()

    studio.colorConfirmed.connect(_on_confirmed)

    # 处理用户直接关闭窗口（不点确认按钮）的情况：
    # 重写 closeEvent 让它退出事件循环。
    _orig_close = studio.closeEvent
    def _close_and_quit(event):
        loop.quit()
        _orig_close(event)
    studio.closeEvent = _close_and_quit

    if on_live is not None:
        studio.colorLiveChanged.connect(on_live)

    studio.raise_()
    studio.activateWindow()

    loop.exec()
    # AUD-32402: 事件循环已退出，studio 不再有任何使用者——调度销毁整棵
    # 选择器 widget 树（deleteLater 在外层应用事件循环的下一轮生效）。
    studio.deleteLater()
    return result["color"]


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("PingFang SC", 10))
    window = CompactColorStudio()
    window.show()
    sys.exit(app.exec())
