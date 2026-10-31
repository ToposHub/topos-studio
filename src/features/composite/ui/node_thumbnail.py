"""
节点缩略图系统。

为选中/查看的节点生成低分辨率预览图，显示在节点 UI 上或 properties panel 中。

架构：
- NodeThumbnailManager: 管理缩略图缓存和更新调度
- ThumbnailWidget: 显示缩略图的 QLabel 子类（支持缩放、通道切换）
- 集成点：CompositePage 获取渲染数据 → 传递给 ThumbnailManager

线程安全：
- 缩略图数据在 UI 端生成（从 numpy array 转 QImage）
- 不跨线程访问 GPU
"""

import logging
from typing import TYPE_CHECKING, Optional, Dict, Tuple
from PyQt6.QtCore import QObject, QTimer, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QPen, QFont
from PyQt6.QtWidgets import QLabel
import numpy as np

logger = logging.getLogger(__name__)


def linear_rgb_to_display(
    rgb: np.ndarray,
    *,
    transfer: int = 1,
    exposure: float = 0.0,
    display_primaries: int = 0,
    tonemap: int = 0,
    target_peak: float = 100.0,
    viewer_gamma: float = 1.0,
) -> np.ndarray:
    """Apply the Composite Viewer display transform to linear RGB data.

    Renderer readbacks are scene-linear BGR floats.  The main viewer applies
    this output-device transform in its display shader; thumbnails must use
    the same transform before converting to 8-bit pixels or they appear too
    dark and oversaturated (for example, a linearized pink becomes red).

    ``rgb`` is expected to be RGB channel order and the returned array is
    clipped to the displayable 0..1 range.
    """
    encoded = np.asarray(rgb, dtype=np.float32)[..., :3].copy()
    encoded *= float(2.0 ** float(exposure))

    if tonemap == 1:
        peak = max(1.0, float(target_peak))
        normalized = encoded / peak
        encoded = normalized / (1.0 + normalized)
    elif tonemap == 2:
        peak = max(1.0, float(target_peak))
        x = encoded / peak
        encoded = np.clip(
            (x * (2.51 * x + 0.03)) /
            (x * (2.43 * x + 0.59) + 0.14),
            0.0,
            1.0,
        )

    # Match the BT.709-centric gamut conversion in composite_viewport.py.
    if display_primaries == 1:
        matrix = np.array(
            [
                [0.6274040, 0.3292820, 0.0433136],
                [0.0690970, 0.9195400, 0.0113612],
                [0.0163916, 0.0880132, 0.8955950],
            ],
            dtype=np.float32,
        )
        encoded = encoded @ matrix.T
    elif display_primaries == 2:
        matrix = np.array(
            [
                [0.8224620, 0.1775380, 0.0000000],
                [0.0331946, 0.9668054, 0.0000000],
                [0.0170827, 0.0723974, 0.9105199],
            ],
            dtype=np.float32,
        )
        encoded = encoded @ matrix.T

    if transfer == 0:
        pass
    elif transfer == 2:
        lo = encoded * 4.5
        hi = 1.099 * np.power(np.maximum(encoded, 0.0), 0.45) - 0.099
        encoded = np.where(encoded <= 0.018, lo, hi)
    elif transfer == 3:
        # ST 2084, matching the viewer shader's linear-nits convention.
        n = np.clip(encoded / 10000.0, 0.0, 1.0)
        v = np.power(n, 0.1593017578125)
        encoded = np.power(
            (0.8359375 + 18.8515625 * v) /
            (1.0 + 18.6875 * v),
            78.84375,
        )
    elif transfer == 4:
        # ARIB STD-B67 HLG OETF, matching the viewer shader.
        lo = np.sqrt(np.maximum(3.0 * encoded, 0.0))
        hi = 0.17883277 * np.log(
            np.maximum(12.0 * encoded - 0.28466892, 1.0e-10)
        ) + 0.55991073
        encoded = np.where(encoded <= (1.0 / 12.0), lo, hi)
    elif transfer == 1:
        # sRGB OETF, matching the viewer shader.
        lo = encoded * 12.92
        hi = 1.055 * np.power(np.maximum(encoded, 0.0), 1.0 / 2.4) - 0.055
        encoded = np.where(encoded <= 0.0031308, lo, hi)
    else:
        # Unknown display modes should retain the application's SDR default.
        lo = encoded * 12.92
        hi = 1.055 * np.power(np.maximum(encoded, 0.0), 1.0 / 2.4) - 0.055
        encoded = np.where(encoded <= 0.0031308, lo, hi)

    if float(viewer_gamma) != 1.0:
        encoded = np.power(
            np.maximum(encoded, 0.0),
            1.0 / max(0.1, float(viewer_gamma)),
        )

    return np.clip(encoded, 0.0, 1.0)

if TYPE_CHECKING:
    from src.features.composite.core.node_engine import NodeGraph


class ThumbnailWidget(QLabel):
    """显示节点输出预览的缩略图控件。

    支持：
    - 固定尺寸（200x200 用于 Properties Panel，64x64 用于 Node Item）
    - 通道显示模式（RGBA、R、G、B、A、Luma）
    - 空状态提示
    - 错误状态显示
    """

    # 通道模式
    CHANNEL_RGBA = 0
    CHANNEL_R = 1
    CHANNEL_G = 2
    CHANNEL_B = 3
    CHANNEL_A = 4
    CHANNEL_LUMA = 5

    # 通道模式名称
    CHANNEL_NAMES = ["RGBA", "R", "G", "B", "A", "Luma"]

    def __init__(self, size: Tuple[int, int] = (200, 200), parent=None) -> None:
        super().__init__(parent)
        self._size = QSize(size[0], size[1])
        self._channel_mode = self.CHANNEL_RGBA
        self._has_image = False
        self._node_id: Optional[str] = None
        self._node_title: str = ""
        self._display_settings = {
            "transfer": 1,
            "exposure": 0.0,
            "display_primaries": 0,
            "tonemap": 0,
            "target_peak": 100.0,
            "viewer_gamma": 1.0,
        }

        self.setFixedSize(self._size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                background-color: #1a1a1a;
                border: 1px solid #444;
                border-radius: 4px;
            }
        """)
        self._draw_placeholder()

    def set_node_info(self, node_id: Optional[str], node_title: str = "") -> None:
        """设置当前关联的节点信息。"""
        self._node_id = node_id
        self._node_title = node_title
        if node_id is None:
            self._draw_placeholder()

    def set_channel_mode(self, mode: int) -> None:
        """设置通道显示模式。"""
        if 0 <= mode <= 5:
            self._channel_mode = mode

    def set_display_settings(self, **settings) -> None:
        """Set the display transform used for linear thumbnail data."""
        for key in self._display_settings:
            if key in settings:
                self._display_settings[key] = settings[key]

    def update_from_numpy(self, data: Optional[np.ndarray]) -> None:
        """从 numpy 数组更新缩略图。

        Args:
            data: BGR float32 数组 (H, W, 3) 或 BGRA (H, W, 4)
                  值范围 0.0-1.0 或更高（HDR）
        """
        if data is None or data.size == 0:
            self._has_image = False
            self._draw_placeholder()
            return

        try:
            # 处理 HDR 数据（clamp 到 0-1）
            if data.dtype == np.float32 or data.dtype == np.float64:
                # 应用通道模式
                processed = self._apply_channel_mode(data)
                # Channel-mode output is still BGR linear data.  Reorder to
                # RGB before applying the same ODT as the main viewer.
                processed = processed[:, :, [2, 1, 0]]
                processed = linear_rgb_to_display(
                    processed,
                    **self._display_settings,
                )
                # 转换为 8-bit
                rgb8 = (processed * 255.0).astype(np.uint8)
            else:
                rgb8 = data.astype(np.uint8)

            # 确保是 RGB 格式
            if rgb8.ndim == 2:
                # 单通道 → RGB
                rgb8 = np.stack([rgb8, rgb8, rgb8], axis=-1)
            elif rgb8.shape[-1] == 4:
                # BGRA → RGB (drop alpha for display)
                rgb8 = rgb8[:, :, :3]

            # 创建 QImage
            h, w = rgb8.shape[:2]
            # Float data was converted to RGB above. Integer input is already
            # treated as display-ready RGB for backwards compatibility.
            if data.dtype != np.float32 and data.dtype != np.float64:
                rgb8 = rgb8[:, :, [2, 1, 0]]  # BGR → RGB
            bytes_per_line = 3 * w
            qimg = QImage(rgb8.data.tobytes(), w, h, bytes_per_line, QImage.Format.Format_RGB888)

            if qimg.isNull():
                self._draw_error("Failed to create image")
                return

            # 缩放为固定尺寸（保持比例）
            scaled = qimg.scaled(
                self._size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )

            self.setPixmap(QPixmap.fromImage(scaled))
            self._has_image = True
        except Exception as e:
            self._draw_error(str(e))

    def _apply_channel_mode(self, data: np.ndarray) -> np.ndarray:
        """应用通道显示模式。

        Args:
            data: BGR float32 数组

        Returns:
            处理后的 RGB float32 数组
        """
        if data.ndim < 3 or data.shape[-1] < 3:
            return data

        mode = self._channel_mode

        if mode == self.CHANNEL_RGBA:
            # 显示 RGB（忽略 alpha）
            return data[:, :, :3]

        elif mode == self.CHANNEL_R:
            # 红色通道 → 灰度可视化（用红色显示）
            r = data[:, :, 2]  # BGR 中 R 在 index 2
            return np.stack([r, np.zeros_like(r), np.zeros_like(r)], axis=-1)

        elif mode == self.CHANNEL_G:
            # 绿色通道
            g = data[:, :, 1]
            return np.stack([np.zeros_like(g), g, np.zeros_like(g)], axis=-1)

        elif mode == self.CHANNEL_B:
            # 蓝色通道
            b = data[:, :, 0]
            return np.stack([np.zeros_like(b), np.zeros_like(b), b], axis=-1)

        elif mode == self.CHANNEL_A:
            # Alpha 通道 → 灰度可视化
            if data.shape[-1] >= 4:
                a = data[:, :, 3]
            else:
                # 没有 alpha，显示为全白
                a = np.ones_like(data[:, :, 0])
            return np.stack([a, a, a], axis=-1)

        elif mode == self.CHANNEL_LUMA:
            # 亮度 = 0.2126*R + 0.7152*G + 0.0722*B (BT.709)
            r = data[:, :, 2]
            g = data[:, :, 1]
            b = data[:, :, 0]
            luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
            return np.stack([luma, luma, luma], axis=-1)

        return data[:, :, :3]

    def _draw_placeholder(self) -> None:
        """绘制空状态占位符。"""
        pixmap = QPixmap(self._size)
        pixmap.fill(QColor(26, 26, 26))

        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.drawRect(0, 0, self._size.width() - 1, self._size.height() - 1)

        # 绘制提示文字
        painter.setPen(QColor(120, 120, 120))
        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)

        if self._node_id:
            text = f"Preview for {self._node_title}\n(No data yet)"
        else:
            text = "Select a node\nto preview"

        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

        self.setPixmap(pixmap)
        self._has_image = False

    def _draw_error(self, error_msg: str) -> None:
        """绘制错误状态。"""
        pixmap = QPixmap(self._size)
        pixmap.fill(QColor(26, 26, 26))

        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor(255, 60, 60), 1))
        painter.drawRect(0, 0, self._size.width() - 1, self._size.height() - 1)

        painter.setPen(QColor(255, 80, 80))
        font = QFont()
        font.setPixelSize(11)
        painter.setFont(font)

        text = f"Error:\n{error_msg[:40]}"
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

        self.setPixmap(pixmap)
        self._has_image = False


class NodeThumbnailManager(QObject):
    """节点缩略图管理器。

    管理：
    - 缩略图缓存（node_id → QImage）
    - 待更新队列（避免频繁渲染）
    - 定时批量更新（减少渲染开销）

    集成方式：
    1. CompositePage 持有 ThumbnailManager 实例
    2. CompositePage 在节点选中时调用 request_thumbnail()
    3. CompositeRenderer 通过信号传递渲染数据（可选：异步 readback）
    4. ThumbnailManager 缓存并更新 ThumbnailWidget
    """

    # 信号：缩略图已更新
    thumbnail_updated = pyqtSignal(str, QImage)  # node_id, thumbnail

    # 配置
    THUMBNAIL_SIZE_LARGE = (200, 200)  # Properties Panel 大预览
    THUMBNAIL_SIZE_SMALL = (64, 64)    # Node Item 小缩略图
    UPDATE_INTERVAL = 500              # 更新间隔（ms）
    MAX_THUMBNAILS = 10                # 最大缓存数量

    def __init__(self, graph: "NodeGraph", parent=None) -> None:
        super().__init__(parent)
        self._graph = graph
        self._thumbnails: Dict[str, QImage] = {}  # node_id -> QImage
        self._thumbnail_data: Dict[str, Optional[np.ndarray]] = {}  # node_id -> numpy data
        self._pending_updates: set = set()  # 待更新的节点 ID
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._process_updates)
        self._update_timer.setInterval(self.UPDATE_INTERVAL)
        self._update_timer.setSingleShot(True)

        # 当前选中的节点
        self._selected_node_id: Optional[str] = None

        # 渲染数据来源（由 CompositePage 设置）
        self._render_data_callback: Optional[callable] = None
        self._display_settings = {
            "transfer": 1,
            "exposure": 0.0,
            "display_primaries": 0,
            "tonemap": 0,
            "target_peak": 100.0,
            "viewer_gamma": 1.0,
        }

    def set_display_settings(self, **settings) -> None:
        """Synchronize thumbnail display settings with the main viewer."""
        updated = dict(self._display_settings)
        for key in updated:
            if key in settings:
                updated[key] = settings[key]
        if updated != self._display_settings:
            self._display_settings = updated
            # Cached QImages are already encoded with the previous ODT.
            self.clear_cache()

    def set_render_data_callback(self, callback: callable) -> None:
        """设置获取渲染数据的回调函数。

        Args:
            callback: 函数签名 (node_id: str) -> Optional[np.ndarray]
                      返回 BGR float32 数组
        """
        self._render_data_callback = callback

    def set_selected_node(self, node_id: Optional[str]) -> None:
        """设置当前选中的节点（触发预览请求）。"""
        self._selected_node_id = node_id
        if node_id:
            self.request_thumbnail(node_id)

    def request_thumbnail(self, node_id: str) -> Optional[QImage]:
        """请求节点缩略图。

        优先返回缓存的缩略图，同时安排异步更新。
        """
        # 优先返回缓存
        cached = self._thumbnails.get(node_id)
        if cached and not cached.isNull():
            return cached

        # 加入待更新队列
        self._pending_updates.add(node_id)
        self._schedule_update()

        return None

    def update_thumbnail_from_data(self, node_id: str, data: Optional[np.ndarray],
                                    size: Tuple[int, int] = None) -> None:
        """从 numpy 数组更新节点缩略图。

        Args:
            node_id: 节点 ID
            data: BGR float32 数组（来自渲染器 readback）
            size: 缩略图尺寸，默认 THUMBNAIL_SIZE_LARGE
        """
        if size is None:
            size = self.THUMBNAIL_SIZE_LARGE

        if data is None or data.size == 0:
            # 清除缓存
            self._thumbnails.pop(node_id, None)
            self._thumbnail_data.pop(node_id, None)
            return

        try:
            # 处理 HDR 数据
            processed = data[:, :, :3]  # 只取线性 RGB；ODT 负责最终裁切
            # BGR → RGB
            rgb = processed[:, :, [2, 1, 0]]
            rgb = linear_rgb_to_display(rgb, **self._display_settings)
            # 转换为 8-bit
            rgb8 = (rgb * 255.0).astype(np.uint8)

            h, w = rgb8.shape[:2]
            bytes_per_line = 3 * w
            qimg = QImage(rgb8.data.tobytes(), w, h, bytes_per_line, QImage.Format.Format_RGB888)

            if qimg.isNull():
                return

            # 缩放为指定尺寸
            scaled = qimg.scaled(
                QSize(size[0], size[1]),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )

            # 缓存
            self._thumbnails[node_id] = scaled
            self._thumbnail_data[node_id] = data.copy()

            # 限制缓存数量（LRU：删除最旧的）
            if len(self._thumbnails) > self.MAX_THUMBNAILS:
                # 删除第一个（非选中节点的）
                for key in list(self._thumbnails.keys()):
                    if key != self._selected_node_id:
                        self._thumbnails.pop(key)
                        self._thumbnail_data.pop(key)
                        break

            # 发射信号
            self.thumbnail_updated.emit(node_id, scaled)

        except Exception as e:
            logger.warning(f"Error updating thumbnail: {e}")

    def get_thumbnail(self, node_id: str) -> Optional[QImage]:
        """获取缓存的缩略图。"""
        return self._thumbnails.get(node_id)

    def get_thumbnail_data(self, node_id: str) -> Optional[np.ndarray]:
        """获取原始缩略图数据（numpy array）。"""
        return self._thumbnail_data.get(node_id)

    def clear_cache(self) -> None:
        """清除所有缓存。"""
        self._thumbnails.clear()
        self._thumbnail_data.clear()
        self._pending_updates.clear()

    def _schedule_update(self) -> None:
        """安排下一次批量更新。"""
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _process_updates(self) -> None:
        """处理待更新的缩略图请求。

        尝试通过 render_data_callback 获取数据。
        """
        if not self._render_data_callback:
            return

        # 只处理选中节点（减少不必要的渲染）
        if self._selected_node_id and self._selected_node_id in self._pending_updates:
            data = self._render_data_callback(self._selected_node_id)
            if data is not None:
                self.update_thumbnail_from_data(self._selected_node_id, data)

        self._pending_updates.clear()
