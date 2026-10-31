"""
图标管理模块 - 使用 QtAwesome 提供统一的图标接口
Icon Manager Module - Provides unified icon interface using QtAwesome

所有图标都来自 Font Awesome，离线可用，无需网络连接。
All icons are from Font Awesome, works offline, no network required.
"""

import os
import logging

logger = logging.getLogger(__name__)
from typing import TYPE_CHECKING, Optional
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QIcon, QPixmap, QPainter
from PyQt6.QtSvg import QSvgRenderer
import qtawesome as qta

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from PyQt6.QtWidgets import QToolButton

# 图标颜色常量
class IconColors:
    """图标颜色常量"""
    DEFAULT = "#aaaaaa"      # 默认灰色
    WHITE = "#ffffff"        # 白色
    ORANGE = "#ff8800"       # 橙色 (主题色)
    RED = "#ff4444"          # 红色 (删除/危险)
    GREEN = "#88ff88"        # 绿色 (成功/添加)
    BLUE = "#88ccff"         # 蓝色 (信息)
    YELLOW = "#ffcc00"       # 黄色 (警告)
    CYAN = "#88ffff"         # 青色 (AI功能)


def get_icon(name: str, color: Optional[str] = None, scale: float = 1.0) -> QIcon:
    """获取 QtAwesome 图标
    
    Args:
        name: 图标名称 (如 'fa5s.eye', 'fa5s.trash')
        color: 图标颜色 (十六进制颜色字符串)
        scale: 图标缩放比例
    
    Returns:
        QIcon 对象
    """
    if color is None:
        color = IconColors.DEFAULT
    
    try:
        return qta.icon(name, color=color, scale=scale)
    except Exception as e:
        logger.error(f"Failed to load icon '{name}': {e}")
        return QIcon()


# ========== 常用图标快捷函数 ==========

def icon_reset(color: str = IconColors.ORANGE) -> QIcon:
    """重置图标"""
    return get_icon('fa5s.undo', color=color)

def icon_add(color: str = IconColors.WHITE) -> QIcon:
    """添加图标"""
    return get_icon('fa5s.plus', color=color)

def icon_delete(color: str = IconColors.RED) -> QIcon:
    """删除图标"""
    return get_icon('fa5s.trash', color=color)

def icon_up(color: str = IconColors.DEFAULT) -> QIcon:
    """向上图标"""
    return get_icon('fa5s.chevron-up', color=color)

def icon_down(color: str = IconColors.DEFAULT) -> QIcon:
    """向下图标"""
    return get_icon('fa5s.chevron-down', color=color)

def icon_visible(color: str = IconColors.ORANGE) -> QIcon:
    """可见图标 (眼睛睁开)"""
    return get_icon('fa5s.eye', color=color)

def icon_hidden(color: str = "#444444") -> QIcon:
    """隐藏图标 (眼睛闭合)"""
    return get_icon('fa5s.eye-slash', color=color)

def icon_mask(color: str = IconColors.DEFAULT) -> QIcon:
    """蒙版图标 - 从 SVG 文件加载（按 color 着色）"""
    # 获取 SVG 文件路径
    svg_path = os.path.join(
        os.path.dirname(__file__),
        '..', 'assets', 'icons', 'buttons', 'mask.svg'
    )

    # 标准化路径
    svg_path = os.path.normpath(svg_path)

    if os.path.exists(svg_path):
        try:
            renderer = QSvgRenderer(svg_path)
            if renderer.isValid():
                pixmap = QPixmap(64, 64)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                renderer.render(painter)
                painter.end()
                # AUD-32003: SVG 成功路径此前忽略 color 参数，按文件原色
                # 渲染——蒙版激活态的橙色高亮永不生效。这里以 SourceIn
                # 叠加纯色层：保留 SVG 的 alpha 形状，颜色统一为请求色。
                painter = QPainter(pixmap)
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceIn
                )
                painter.fillRect(pixmap.rect(), QColor(color))
                painter.end()
                return QIcon(pixmap)
        except Exception as e:
            logger.error(f"Failed to load mask SVG '{svg_path}': {e}")

    # 如果 SVG 加载失败，回退到 qtawesome
    return get_icon('fa5s.mask', color=color)

def icon_fx(color: str = IconColors.DEFAULT) -> QIcon:
    """特效图标"""
    return get_icon('fa5s.magic', color=color)

def icon_circle(color: str = IconColors.WHITE) -> QIcon:
    """圆形图标"""
    return get_icon('fa5s.circle', color=color)

def icon_rect(color: str = IconColors.WHITE) -> QIcon:
    """矩形图标"""
    return get_icon('fa5s.square', color=color)

def icon_pen(color: str = IconColors.WHITE) -> QIcon:
    """钢笔图标"""
    return get_icon('fa5s.pen', color=color)

def icon_brush(color: str = IconColors.WHITE) -> QIcon:
    """画笔图标"""
    return get_icon('fa5s.paint-brush', color=color)

def icon_eraser(color: str = IconColors.DEFAULT) -> QIcon:
    """橡皮擦图标"""
    return get_icon('fa5s.eraser', color=color)

def icon_cut(color: str = IconColors.GREEN) -> QIcon:
    """剪裁/移除图标 - 用于 Remove BG 功能"""
    return get_icon('fa5s.cut', color=color)

def icon_smart(color: str = IconColors.CYAN) -> QIcon:
    """智能/魔棒图标 - 用于 Smart Brush 功能"""
    return get_icon('fa5s.magic', color=color)

def icon_folder(color: str = IconColors.DEFAULT) -> QIcon:
    """文件夹图标"""
    return get_icon('fa5s.folder', color=color)

def icon_folder_open(color: str = IconColors.DEFAULT) -> QIcon:
    """打开的文件夹图标"""
    return get_icon('fa5s.folder-open', color=color)

def icon_ai(color: str = IconColors.CYAN) -> QIcon:
    """AI 图标"""
    return get_icon('fa5s.robot', color=color)

def icon_brain(color: str = IconColors.CYAN) -> QIcon:
    """大脑/AI 图标"""
    return get_icon('fa5s.brain', color=color)

def icon_invert(color: str = IconColors.WHITE) -> QIcon:
    """反转图标"""
    return get_icon('fa5s.adjust', color=color)

def icon_operation_add(color: str = IconColors.WHITE) -> QIcon:
    """布尔运算 - 添加"""
    return get_icon('fa5s.plus', color=color)

def icon_operation_subtract(color: str = IconColors.WHITE) -> QIcon:
    """布尔运算 - 减去"""
    return get_icon('fa5s.minus', color=color)

def icon_operation_intersect(color: str = IconColors.WHITE) -> QIcon:
    """布尔运算 - 交集"""
    # AUD-32002: 'fa5s.object-intersect' 不在 FA5 free charmap 中
    # （qtawesome 对未知字形抛异常后只得到空图标），改用实际存在的
    # object-group 字形作为交集按钮的近似图形。
    return get_icon('fa5s.object-group', color=color)

def icon_gradient(color: str = IconColors.WHITE) -> QIcon:
    """渐变图标"""
    return get_icon('ei.tint', color=color)

def icon_crop(color: str = IconColors.DEFAULT) -> QIcon:
    """裁剪图标"""
    return get_icon('fa5s.crop-alt', color=color)

def icon_refresh(color: str = IconColors.ORANGE) -> QIcon:
    """刷新/重载图标"""
    return get_icon('fa5s.sync-alt', color=color)

def icon_save(color: str = IconColors.DEFAULT) -> QIcon:
    """保存图标"""
    return get_icon('fa5s.save', color=color)

def icon_open(color: str = IconColors.DEFAULT) -> QIcon:
    """打开文件图标"""
    return get_icon('fa5s.folder-open', color=color)

def icon_export(color: str = IconColors.DEFAULT) -> QIcon:
    """导出图标"""
    return get_icon('fa5s.file-export', color=color)

def icon_import(color: str = IconColors.DEFAULT) -> QIcon:
    """导入图标"""
    return get_icon('fa5s.file-import', color=color)

def icon_settings(color: str = IconColors.DEFAULT) -> QIcon:
    """设置图标"""
    return get_icon('fa5s.cog', color=color)

def icon_home(color: str = IconColors.DEFAULT) -> QIcon:
    """项目管理/主页图标"""
    return get_icon('fa5s.home', color=color)

def icon_search(color: str = IconColors.DEFAULT) -> QIcon:
    """搜索图标"""
    return get_icon('fa5s.search', color=color)

def icon_zoom_in(color: str = IconColors.DEFAULT) -> QIcon:
    """放大图标"""
    return get_icon('fa5s.search-plus', color=color)

def icon_zoom_out(color: str = IconColors.DEFAULT) -> QIcon:
    """缩小图标"""
    return get_icon('fa5s.search-minus', color=color)

def icon_grid(color: str = IconColors.DEFAULT) -> QIcon:
    """网格视图图标"""
    return get_icon('fa5s.th', color=color)

def icon_list(color: str = IconColors.DEFAULT) -> QIcon:
    """列表视图图标"""
    return get_icon('fa5s.list', color=color)

def icon_color_picker(color: str = IconColors.DEFAULT) -> QIcon:
    """颜色选择器图标"""
    return get_icon('fa5s.eye-dropper', color=color)

def icon_layer(color: str = IconColors.DEFAULT) -> QIcon:
    """图层图标"""
    return get_icon('fa5s.layer-group', color=color)

def icon_image(color: str = IconColors.DEFAULT) -> QIcon:
    """图片图标"""
    return get_icon('fa5s.image', color=color)

def icon_info(color: str = IconColors.DEFAULT) -> QIcon:
    """信息图标"""
    return get_icon('fa5s.info-circle', color=color)

def icon_warning(color: str = IconColors.YELLOW) -> QIcon:
    """警告图标"""
    return get_icon('fa5s.exclamation-triangle', color=color)

def icon_error(color: str = IconColors.RED) -> QIcon:
    """错误图标"""
    return get_icon('fa5s.times-circle', color=color)

def icon_check(color: str = IconColors.GREEN) -> QIcon:
    """成功/勾选图标"""
    return get_icon('fa5s.check', color=color)

def icon_close(color: str = IconColors.DEFAULT) -> QIcon:
    """关闭图标"""
    return get_icon('fa5s.times', color=color)

def icon_new_folder(color: str = IconColors.DEFAULT) -> QIcon:
    """新建文件夹图标"""
    return get_icon('fa5s.folder-plus', color=color)

def icon_arrow_up(color: str = IconColors.DEFAULT) -> QIcon:
    """向上箭头图标"""
    return get_icon('fa5s.arrow-up', color=color)

def icon_arrow_down(color: str = IconColors.DEFAULT) -> QIcon:
    """向下箭头图标"""
    return get_icon('fa5s.arrow-down', color=color)

def icon_arrow_left(color: str = IconColors.DEFAULT) -> QIcon:
    """向左箭头图标"""
    return get_icon('fa5s.arrow-left', color=color)

def icon_arrow_right(color: str = IconColors.DEFAULT) -> QIcon:
    """向右箭头图标"""
    return get_icon('fa5s.arrow-right', color=color)

def icon_copy(color: str = IconColors.DEFAULT) -> QIcon:
    """复制图标"""
    return get_icon('fa5s.copy', color=color)

def icon_paste(color: str = IconColors.DEFAULT) -> QIcon:
    """粘贴图标"""
    return get_icon('fa5s.paste', color=color)

def icon_duplicate(color: str = IconColors.DEFAULT) -> QIcon:
    """复制/重复图标"""
    return get_icon('fa5s.clone', color=color)

def icon_grading(color: str = IconColors.DEFAULT) -> QIcon:
    """调色图标"""
    return get_icon('fa5s.sliders-h', color=color)


def icon_favorite(color: str = IconColors.ORANGE) -> QIcon:
    """收藏图标（实心星星）"""
    return get_icon('fa5s.star', color=color)


def icon_favorite_outline(color: str = IconColors.DEFAULT) -> QIcon:
    """收藏图标（空心星星）"""
    # AUD-32001: 空心星取 FA5 regular 集（fa5 前缀），与实心
    # 'fa5s.star' 区分，收藏两态视觉可辨。
    return get_icon('fa5.star', color=color)


# ========== 剪辑相关图标 ==========

def icon_play(color: str = IconColors.DEFAULT) -> QIcon:
    """播放图标"""
    return get_icon('fa5s.play', color=color)

def icon_pause(color: str = IconColors.DEFAULT) -> QIcon:
    """暂停图标"""
    return get_icon('fa5s.pause', color=color)

def icon_stop(color: str = IconColors.DEFAULT) -> QIcon:
    """停止图标"""
    return get_icon('fa5s.stop', color=color)

def icon_step_back(color: str = IconColors.DEFAULT) -> QIcon:
    """后退一帧图标"""
    return get_icon('fa5s.step-backward', color=color)

def icon_step_forward(color: str = IconColors.DEFAULT) -> QIcon:
    """前进一帧图标"""
    return get_icon('fa5s.step-forward', color=color)

def icon_jump_start(color: str = IconColors.DEFAULT) -> QIcon:
    """跳到开头图标"""
    return get_icon('fa5s.fast-backward', color=color)

def icon_jump_end(color: str = IconColors.DEFAULT) -> QIcon:
    """跳到结尾图标"""
    return get_icon('fa5s.fast-forward', color=color)

def icon_loop(color: str = IconColors.DEFAULT) -> QIcon:
    """循环图标"""
    return get_icon('fa5s.redo-alt', color=color)

def icon_select_tool(color: str = IconColors.DEFAULT) -> QIcon:
    """选择工具图标"""
    return get_icon('fa5s.mouse-pointer', color=color)

def icon_blade_tool(color: str = IconColors.DEFAULT) -> QIcon:
    """刀片工具图标"""
    return get_icon('fa5s.cut', color=color)

def icon_trim_tool(color: str = IconColors.DEFAULT) -> QIcon:
    """修剪工具图标"""
    return get_icon('fa5s.compress-arrows-alt', color=color)

def icon_hand_tool(color: str = IconColors.DEFAULT) -> QIcon:
    """抓手工具图标"""
    return get_icon('fa5s.hand-paper', color=color)

def icon_zoom_tool(color: str = IconColors.DEFAULT) -> QIcon:
    """缩放工具图标"""
    return get_icon('fa5s.search-plus', color=color)

def icon_snap(color: str = IconColors.DEFAULT) -> QIcon:
    """吸附图标"""
    return get_icon('fa5s.magnet', color=color)

def icon_link(color: str = IconColors.DEFAULT) -> QIcon:
    """链接图标"""
    return get_icon('fa5s.link', color=color)

def icon_unlink(color: str = IconColors.DEFAULT) -> QIcon:
    """取消链接图标"""
    return get_icon('fa5s.unlink', color=color)

def icon_marker(color: str = IconColors.DEFAULT) -> QIcon:
    """标记图标"""
    return get_icon('fa5s.map-marker-alt', color=color)

def icon_insert_edit(color: str = IconColors.DEFAULT) -> QIcon:
    """插入编辑图标"""
    return get_icon('fa5s.level-down-alt', color=color)

def icon_overwrite_edit(color: str = IconColors.DEFAULT) -> QIcon:
    """覆盖编辑图标"""
    return get_icon('fa5s.exchange-alt', color=color)

def icon_append_edit(color: str = IconColors.DEFAULT) -> QIcon:
    """追加编辑图标"""
    return get_icon('fa5s.plus-square', color=color)

def icon_ripple(color: str = IconColors.DEFAULT) -> QIcon:
    """波纹编辑图标"""
    return get_icon('fa5s.wave-square', color=color)

def icon_roll(color: str = IconColors.DEFAULT) -> QIcon:
    """滚动编辑图标"""
    return get_icon('fa5s.arrows-alt-h', color=color)

def icon_slip(color: str = IconColors.DEFAULT) -> QIcon:
    """滑动编辑图标"""
    return get_icon('fa5s.sign-in-alt', color=color)

def icon_slide(color: str = IconColors.DEFAULT) -> QIcon:
    """滑移编辑图标"""
    return get_icon('fa5s.arrows-alt', color=color)

def icon_video_track(color: str = IconColors.DEFAULT) -> QIcon:
    """视频轨道图标"""
    return get_icon('fa5s.film', color=color)

def icon_audio_track(color: str = IconColors.DEFAULT) -> QIcon:
    """音频轨道图标"""
    return get_icon('fa5s.volume-up', color=color)

def icon_mute(color: str = IconColors.DEFAULT) -> QIcon:
    """静音图标"""
    return get_icon('fa5s.volume-mute', color=color)

def icon_solo(color: str = IconColors.DEFAULT) -> QIcon:
    """独奏图标"""
    return get_icon('fa5s.headphones', color=color)

def icon_lock_track(color: str = IconColors.DEFAULT) -> QIcon:
    """锁定轨道图标"""
    return get_icon('fa5s.lock', color=color)

def icon_unlock_track(color: str = IconColors.DEFAULT) -> QIcon:
    """解锁轨道图标"""
    return get_icon('fa5s.unlock', color=color)


# ========== 3D 工具图标 ==========

def icon_3d_select(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 选择工具图标"""
    return get_icon('fa5s.mouse-pointer', color=color)

def icon_3d_move(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 移动工具图标"""
    return get_icon('fa5s.arrows-alt', color=color)

def icon_3d_rotate(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 旋转工具图标"""
    return get_icon('fa5s.sync-alt', color=color)

def icon_3d_scale(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 缩放工具图标"""
    return get_icon('fa5s.expand-arrows-alt', color=color)

def icon_3d_model(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 模型(Object)模式图标"""
    return get_icon('fa5s.cube', color=color)

def icon_3d_point(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 点模式图标"""
    return get_icon('fa5s.circle', color=color)

def icon_3d_edge(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 边模式图标"""
    return get_icon('fa5s.grip-lines', color=color)

def icon_3d_polygon(color: str = IconColors.DEFAULT) -> QIcon:
    """3D 面模式图标"""
    return get_icon('fa5s.draw-polygon', color=color)


# ========== 图标名称映射表 (用于动态查找) ==========

ICON_MAP = {
    # 基础操作
    'reset': 'fa5s.undo',
    'add': 'fa5s.plus',
    'delete': 'fa5s.trash',
    'refresh': 'fa5s.sync-alt',
    'save': 'fa5s.save',
    'open': 'fa5s.folder-open',
    'export': 'fa5s.file-export',
    'import': 'fa5s.file-import',
    'settings': 'fa5s.cog',
    'close': 'fa5s.times',
    'check': 'fa5s.check',
    'favorite': 'fa5s.star',
    'favorite_outline': 'fa5.star',
    
    # 导航
    'up': 'fa5s.chevron-up',
    'down': 'fa5s.chevron-down',
    'left': 'fa5s.chevron-left',
    'right': 'fa5s.chevron-right',
    'arrow_up': 'fa5s.arrow-up',
    'arrow_down': 'fa5s.arrow-down',
    'arrow_left': 'fa5s.arrow-left',
    'arrow_right': 'fa5s.arrow-right',
    'copy': 'fa5s.copy',
    'paste': 'fa5s.paste',
    'duplicate': 'fa5s.clone',
    
    # 可见性
    'visible': 'fa5s.eye',
    'hidden': 'fa5s.eye-slash',
    
    # 图层与蒙版
    'mask': 'fa5s.mask',
    'fx': 'fa5s.magic',
    'layer': 'fa5s.layer-group',
    
    # 形状
    'circle': 'fa5s.circle',
    'rect': 'fa5s.square',
    'pen': 'fa5s.pen',
    'brush': 'fa5s.paint-brush',
    'eraser': 'fa5s.eraser',
    'gradient': 'fa5s.sliders-h',
    
    # 文件夹
    'folder': 'fa5s.folder',
    'folder_open': 'fa5s.folder-open',
    'new_folder': 'fa5s.folder-plus',
    
    # AI
    'ai': 'fa5s.robot',
    'brain': 'fa5s.brain',
    
    # 视图
    'grid': 'fa5s.th',
    'list': 'fa5s.list',
    'zoom_in': 'fa5s.search-plus',
    'zoom_out': 'fa5s.search-minus',
    
    # 其他
    'crop': 'fa5s.crop-alt',
    'color_picker': 'fa5s.eye-dropper',
    'image': 'fa5s.image',
    'info': 'fa5s.info-circle',
    'warning': 'fa5s.exclamation-triangle',
    'error': 'fa5s.times-circle',
    'invert': 'fa5s.adjust',
    
    # 布尔运算
    'op_add': 'fa5s.plus',
    'op_subtract': 'fa5s.minus',
    'op_intersect': 'fa5s.object-group',

    # 剪辑相关
    'play': 'fa5s.play',
    'pause': 'fa5s.pause',
    'stop': 'fa5s.stop',
    'step_back': 'fa5s.step-backward',
    'step_forward': 'fa5s.step-forward',
    'jump_start': 'fa5s.fast-backward',
    'jump_end': 'fa5s.fast-forward',
    'loop': 'fa5s.redo-alt',
    'select_tool': 'fa5s.mouse-pointer',
    'blade_tool': 'fa5s.cut',
    'trim_tool': 'fa5s.compress-arrows-alt',
    'hand_tool': 'fa5s.hand-paper',
    'snap': 'fa5s.magnet',
    'link': 'fa5s.link',
    'unlink': 'fa5s.unlink',
    'marker': 'fa5s.map-marker-alt',
    'insert_edit': 'fa5s.level-down-alt',
    'overwrite_edit': 'fa5s.exchange-alt',
    'append_edit': 'fa5s.plus-square',
    'ripple': 'fa5s.wave-square',
    'roll': 'fa5s.arrows-alt-h',
    'slip': 'fa5s.sign-in-alt',
    'slide': 'fa5s.arrows-alt',
    'video_track': 'fa5s.film',
    'audio_track': 'fa5s.volume-up',
    'mute': 'fa5s.volume-mute',
    'solo': 'fa5s.headphones',
    'lock_track': 'fa5s.lock',
    'unlock_track': 'fa5s.unlock',
}


def get_icon_by_name(key: str, color: Optional[str] = None) -> QIcon:
    """通过键名获取图标

    Args:
        key: 图标键名 (如 'reset', 'add', 'delete')
        color: 图标颜色

    Returns:
        QIcon 对象
    """
    icon_name = ICON_MAP.get(key)
    if icon_name:
        return get_icon(icon_name, color)
    else:
        logger.info(f"Unknown icon key: {key}")
        return QIcon()


# ========== 绑定/锁图标统一入口 ==========
# 全项目统一的「绑定/锁」图标配色：
#   锁定(linked)  = 橙 #ff8800（主题色，与 ⛓ 旧样式的 :checked 态一致）
#   解锁(unlinked) = 灰 #777777（暗色主题下柔和不抢眼）
# 历史：edit 页 Lock scale 曾用橙-白；composite ⛓ 用橙-灰；此处统一为橙-灰。
LINK_ICON_COLOR_ON = IconColors.ORANGE   # "#ff8800"
LINK_ICON_COLOR_OFF = "#777777"          # 灰


def _link_icon(linked: bool) -> QIcon:
    """绑定/锁图标：锁定=关锁(橙) / 解锁=开锁(灰)。"""
    return get_icon('fa5s.lock' if linked else 'fa5s.lock-open',
                    LINK_ICON_COLOR_ON if linked else LINK_ICON_COLOR_OFF)


def make_link_button(initial_linked: bool = True, tooltip: str = "Link") -> "QToolButton":
    """创建统一的「绑定/锁」切换按钮。

    锁定时显示关锁图标(fa5s.lock, 橙)，解锁时显示开锁图标(fa5s.lock-open, 灰)。
    可勾选；状态切换时自动更换图标，调用方只需连接 ``toggled``/``clicked``
    处理业务联动逻辑（如 scale_x → scale_y 同步）。

    Args:
        initial_linked: 初始是否锁定（默认 True，与 ⛓ 旧默认一致）。
        tooltip: 鼠标悬停提示。

    Returns:
        配置好的 QToolButton（20×20，无边框透明，hover 高亮）。
    """
    # 局部导入避免与 style_sheet 潜在的循环依赖。
    from PyQt6.QtWidgets import QToolButton
    from src.shared.ui.style_sheet import TOOLBAR_FLAT_BTN_STYLE

    btn = QToolButton()
    btn.setFixedSize(20, 20)
    btn.setIconSize(QSize(12, 12))
    btn.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
    btn.setCheckable(True)
    btn.setChecked(initial_linked)
    btn.setToolTip(tooltip)
    btn.setIcon(_link_icon(initial_linked))
    # Dynamic property so tests/queries can identify link buttons without
    # depending on the icon glyph or text (which changed from ⛓ to a lock
    # icon — see make_link_button's introduction).
    btn.setProperty("link_button", True)

    def _on_toggled(checked: bool) -> None:
        btn.setIcon(_link_icon(checked))

    btn.toggled.connect(_on_toggled)
    return btn
