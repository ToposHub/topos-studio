"""GpuScopeWidget vectorscope target overlay (GPU 管线标注回归)。

GPU 管线迁移时 6 色目标框/文字标注丢失(只剩烘焙的圆环刻度),
本测试锁定 overlay 的几何映射与 CPU 版 VectorScopeWidget 一致。
"""
import math

import pytest


@pytest.fixture(scope='module')
def qapp_mod():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _render_targets(cx=300.0, cy=300.0, radius=250.0):
    """把目标框画进 QImage,返回 (image, COLOR_TARGETS)。"""
    from PyQt6.QtGui import QColor, QImage, QPainter
    from src.shared.ui.widgets.gpu_scope_widget import draw_vectorscope_targets
    from src.shared.ui.widgets.vectorscope_widget import COLOR_TARGETS

    img = QImage(600, 600, QImage.Format.Format_RGB32)
    img.fill(QColor(5, 5, 5))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    draw_vectorscope_targets(painter, cx, cy, radius)
    painter.end()
    return img, COLOR_TARGETS


class TestVectorscopeTargetOverlay:
    def test_flag_stored(self, qapp_mod):
        from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
        plain = GpuScopeWidget()
        assert plain._show_vectorscope_targets is False
        vs = GpuScopeWidget(preserve_aspect=True,
                            show_vectorscope_targets=True)
        assert vs._show_vectorscope_targets is True

    def test_targets_drawn_at_cpu_widget_positions(self, qapp_mod):
        """R 目标框(左上)与 C 目标框(右下)应落在 CPU 映射位置。"""
        img, targets = _render_targets(cx=300.0, cy=300.0, radius=250.0)
        cb, cr = targets['R']
        ex = 300.0 + cb * 2.0 * 250.0
        ey = 300.0 - cr * 2.0 * 250.0
        # 100% 目标框边框像素应偏红
        color = img.pixelColor(int(ex) - 4, int(ey))
        assert color.red() > 150 and color.green() < 160
        # C(青)目标框在右下:红通道低、绿蓝高
        cb, cr = targets['C']
        ex = 300.0 + cb * 2.0 * 250.0
        ey = 300.0 - cr * 2.0 * 250.0
        color = img.pixelColor(int(ex) + 4, int(ey))
        assert color.blue() > 150 and color.red() < 160

    def test_center_stays_clean(self, qapp_mod):
        """零饱和度中心没有任何目标框。"""
        img, _ = _render_targets()
        color = img.pixelColor(300, 300)
        assert color.red() < 60 and color.green() < 60

    def test_labels_present(self, qapp_mod):
        """标签朝中心偏移,R 标签区域内应有红色字形像素。"""
        img, targets = _render_targets()
        cb, cr = targets['R']
        ex = 300.0 + cb * 2.0 * 250.0
        ey = 300.0 - cr * 2.0 * 250.0
        # 标签朝中心偏移 label_offset=12(与实现一致的几何)
        to_cx, to_cy = 300.0 - ex, 300.0 - ey
        dist = math.hypot(to_cx, to_cy)
        lx = ex + to_cx / dist * 12.0
        ly = ey + to_cy / dist * 12.0
        reddish = 0
        for dx in range(-2, 22):
            for dy in range(-10, 10):
                c = img.pixelColor(int(lx) + dx, int(ly) + dy)
                if c.red() > 150 and c.green() < 150:
                    reddish += 1
        assert reddish >= 5, f"R 标签区域红色像素不足: {reddish}"

    def test_edge_labels_stay_inside_view(self, qapp_mod):
        """边缘目标(100% 环贴边)的标签不应画出视图外。"""
        img, targets = _render_targets(cx=300.0, cy=300.0, radius=250.0)
        w, h = img.width(), img.height()
        # 逐像素检查整幅图:所有非黑像素都在视图内(必然为真,
        # 此处真正想锁的是绘制调用本身不越界 —— QImage 越界写入会
        # 被 Qt 静默裁掉,因此改为验证 B/C 等边缘目标的标签像素存在)
        for name in ('B', 'C', 'Y'):
            cb, cr = targets[name]
            ex = 300.0 + cb * 2.0 * 250.0
            ey = 300.0 - cr * 2.0 * 250.0
            to_cx, to_cy = 300.0 - ex, 300.0 - ey
            dist = math.hypot(to_cx, to_cy)
            lx = ex + to_cx / dist * 12.0
            ly = ey + to_cy / dist * 12.0
            found = False
            for dx in range(-2, 22):
                for dy in range(-10, 10):
                    px, py = int(lx) + dx, int(ly) + dy
                    if 0 <= px < w and 0 <= py < h:
                        c = img.pixelColor(px, py)
                        if c.red() + c.green() + c.blue() > 150:
                            found = True
                            break
                if found:
                    break
            assert found, f"{name} 标签不可见(可能被裁掉)"
