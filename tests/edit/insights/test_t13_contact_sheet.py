"""T13 · 联系表与镜头故事板测试（offscreen 渲染）。"""

from __future__ import annotations

import os

import pytest

from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.contact_sheet import (
    THUMB_H,
    THUMB_W,
    build_contact_cells,
    render_contact_sheet_png,
    write_contact_sheet_html,
)
from src.features.edit.insights.host_protocol import (
    THUMB_SOURCE,
    ThumbnailRequest,
    ThumbnailResult,
)


pytestmark = [pytest.mark.edit_ui]


class _PngHost:
    """返回真实小 PNG 的假缩略图宿主（offscreen QImage 生成）。"""

    def __init__(self) -> None:
        self.calls: list[ThumbnailRequest] = []

    def request_thumbnail(self, request: ThumbnailRequest) -> ThumbnailResult:
        from PyQt6.QtGui import QImage

        self.calls.append(request)
        image = QImage(64, 36, QImage.Format.Format_RGB32)
        image.fill(0xFF3366AA)
        from PyQt6.QtCore import QBuffer, QIODevice

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return ThumbnailResult(
            ok=True, quality=THUMB_SOURCE, media_type="png",
            data=bytes(buffer.data()), fingerprint=f"{request.asset_id}@{request.source_frame}",
        )


@pytest.fixture(scope="module")
def snapshot():
    st = syn.build_synthetic_state()
    return capture_readonly_state(st.state, capture_id="cap-t13", captured_at=0.0)


def test_cells_no_host_all_placeholder_with_reason(snapshot):
    cells = build_contact_cells(snapshot, syn.SEQ_MAIN, host=None)
    # S1 视频轨 4 个镜头
    assert len(cells) == 4
    for cell in cells:
        assert cell.thumb_quality == "placeholder"
        assert cell.capture_id == snapshot.capture_id
        assert cell.capture_revision == 3  # 序列 revision 标注在每格
        # 占位原因分两类：可求代表帧但宿主未接线 / 映射未验收
        if cell.source_frame is not None:
            assert "not wired" in cell.thumb_reason
        else:
            assert "M04" in cell.thumb_reason


def test_unknown_mapping_placeholder_and_identity_source_frame(snapshot):
    host = _PngHost()
    cells = build_contact_cells(snapshot, syn.SEQ_MAIN, host=host)
    by_id = {c.clip_id: c for c in cells}
    # 恒速映射：代表帧=源中点（手算）
    a1 = by_id[syn.CLIP_A1]
    assert a1.source_frame == 24 + 120 // 2 == 84
    assert a1.thumb_quality == THUMB_SOURCE and a1.thumb_png
    b1 = by_id[syn.CLIP_B1]  # 2 倍速（constant）也可求代表帧
    assert b1.source_frame == 0 + 240 // 2 == 120
    # 冻结（unknown）→ 占位 + M04 原因，不给代表帧
    b2 = by_id[syn.CLIP_B2]
    assert b2.thumb_quality == "placeholder"
    assert b2.source_frame is None
    assert "M04" in b2.thumb_reason
    # 未加载序列 → 空
    assert build_contact_cells(snapshot, syn.SEQ_UNLOADED, host=host) == ()


def test_dialogue_and_marker_attached(snapshot):
    """对白/marker 取与镜头区间重叠者（手算）：
    A1 [0,120) ∩ CUE_ZH [10,70) → 你好；M_TURN(100)∈A1；
    A2 [120,200) ∩ CUE_QUOTE [80,150) → 引号文本；B1 [200,320) 含章节。"""
    host = _PngHost()
    cells = build_contact_cells(snapshot, syn.SEQ_MAIN, host=host)
    by_id = {c.clip_id: c for c in cells}
    assert by_id[syn.CLIP_A1].dialogue == syn.CUE_TEXT_ZH
    assert by_id[syn.CLIP_A1].marker == "转折点"
    assert by_id[syn.CLIP_A2].dialogue == syn.CUE_TEXT_QUOTE
    assert by_id[syn.CLIP_B1].marker == "Chapter 2"
    assert by_id[syn.CLIP_B2].dialogue == ""  # [400,460) 无字幕


def test_render_png_and_html(snapshot, tmp_path):
    host = _PngHost()
    cells = build_contact_cells(snapshot, syn.SEQ_MAIN, host=host)
    png = str(tmp_path / "sheet.png")
    layout = render_contact_sheet_png(cells, png, columns=3)
    assert os.path.exists(png) and os.path.getsize(png) > 0
    assert layout["rows"] == 2 and layout["columns"] == 3  # 4 格 → 2 行
    # PNG 可回读，尺寸符合布局（表头 36 + 2 行）
    from PyQt6.QtGui import QImage

    image = QImage(png)
    assert not image.isNull()
    expected_w = 3 * (THUMB_W + 16) + 8 * 4
    assert image.width() == expected_w
    # 文本不截字：换行结果覆盖完整 caption（每格 wrapped 行数 ≤4 但 caption
    # 已按字宽拆行；此处断言无空 caption 且未出现"省略号截断"实现）
    assert all(any(line.strip() for line in lines) for lines in layout["wrapped_lines"])

    html_path = write_contact_sheet_html(cells, str(tmp_path / "sheet"))
    html_text = open(html_path, encoding="utf-8").read()
    assert "<img src=" in html_text
    assert "NOT final color/composite" in html_text      # 源缩略图声明
    assert "<script" not in html_text.lower()
    # 相对链接的图片文件真实存在
    assert os.path.exists(os.path.join(os.path.dirname(html_path), "contact_000_gp-clip-a1.png"))


def test_placeholder_cells_render_reason_in_png(snapshot, tmp_path):
    cells = build_contact_cells(snapshot, syn.SEQ_MAIN, host=None)
    png = str(tmp_path / "placeholder.png")
    render_contact_sheet_png(cells, png, columns=2)
    assert os.path.getsize(png) > 0
    html_path = write_contact_sheet_html(cells, str(tmp_path / "ph"))
    html_text = open(html_path, encoding="utf-8").read()
    assert "placeholder" in html_text and "M04" in html_text  # 原因可见


def test_render_empty_cells_rejected():
    with pytest.raises(ValueError):
        render_contact_sheet_png((), "/tmp/never.png")
