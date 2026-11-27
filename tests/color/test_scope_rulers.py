"""Readability and measurement alignment of scope chrome, without OpenGL."""
from collections.abc import Callable

import pytest
from PyQt6.QtGui import QFont, QImage, QPainter

from src.shared.ui.widgets.gpu_scope_widget import (
    _scope_ruler_font, draw_histogram_ruler, draw_waveform_ruler,
)


def _paint(width: int, height: int, draw: Callable) -> tuple[QImage, list]:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        font = QFont('Arial')
        font.setPixelSize(24)
        painter.setFont(font)
        labels = draw(painter)
        assert painter.font().pixelSize() == 24, 'Annotation must restore its painter state'
    finally:
        painter.end()
    return image, labels


def test_waveform_ruler_font_and_right_aligned_value_positions(qapp) -> None:
    assert _scope_ruler_font().pixelSize() == 12
    assert _scope_ruler_font().weight() == QFont.Weight.Medium
    image, labels = _paint(250, 300, lambda painter: draw_waveform_ruler(painter, 300, 46))
    assert len(labels) == 9
    by_code = {int(label): (rect, y) for label, rect, y in labels}
    for code, (rect, y) in by_code.items():
        assert rect.right() == 38  # Eight logical pixels before the plot axis.
        assert rect.top() >= 0 and rect.bottom() <= 300
        assert y == round((1.0 - (code / 1023.0 + 0.1) / 1.3) * 300)
        assert image.pixelColor(46, y).getRgb() == (190, 160, 70, 255)
    assert by_code[1023][1] > 0
    assert by_code[0][1] < 299  # The signal keeps both HDR headroom margins.


@pytest.mark.parametrize(('height', 'mode', 'transfer'), [
    (60, 'code10', 'srgb'), (100, 'code10', 'srgb'),
    (80, 'code10_video', 'srgb'), (100, 'nits', 'pq'), (100, 'nits', 'hlg'),
    (100, 'nits', 'srgb'), (100, 'percent', 'srgb'),
])
def test_short_waveform_labels_do_not_overlap(qapp, height: int, mode: str, transfer: str) -> None:
    _, labels = _paint(250, height, lambda painter: draw_waveform_ruler(painter, height, 46, mode, transfer))
    assert labels
    for (_, first, _), (_, second, _) in zip(labels, labels[1:]):
        assert first.bottom() + 2 <= second.top()
    for label, rect, _ in labels:
        assert rect.top() >= 0 and rect.bottom() <= height
        assert 'nits' not in label  # Keep the selected unit's numerical labels inside the margin.


@pytest.mark.parametrize(('mode', 'transfer', 'endpoints'), [
    ('percent', 'srgb', ('100%', '0%')),
    ('code10_video', 'srgb', ('940', '64')),
    ('nits', 'srgb', ('100', '0')),
    ('nits', 'pq', ('10k', '0')),
    ('nits', 'hlg', ('1000', '0')),
])
def test_grid_and_labels_share_the_selected_scale(qapp, mode, transfer, endpoints) -> None:
    image, labels = _paint(250, 320, lambda painter: draw_waveform_ruler(
        painter, 320, 46, mode, transfer, plot_width=250))
    assert (labels[0][0], labels[-1][0]) == endpoints
    assert {y for y in range(320) if image.pixelColor(180, y).alpha()} == {y for _, _, y in labels}


def test_hiding_excursions_puts_full_range_at_plot_edges(qapp) -> None:
    _, labels = _paint(250, 300, lambda painter: draw_waveform_ruler(
        painter, 300, 46, plot_width=250, show_excursions=False))
    assert (labels[0][0], labels[0][2]) == ('1023', 0)
    assert (labels[-1][0], labels[-1][2]) == ('0', 299)


@pytest.mark.parametrize(('width', 'codes'), [
    (250, ['0', '256', '512', '768', '1023']),
    (500, ['0', '128', '256', '384', '512', '640', '768', '896', '1023']),
])
def test_histogram_labels_fit_and_follow_plot_ticks(qapp, width: int, codes: list[str]) -> None:
    image, labels = _paint(width, 50, lambda painter: draw_histogram_ruler(painter, width))
    assert [label for label, _, _ in labels] == codes
    for label, rect, x in labels:
        assert 0 <= rect.left() and rect.right() <= width
        assert rect.top() >= 0 and rect.bottom() < 22
        assert x == pytest.approx(20 + (width - 30) * int(label) / 1023)
        if label not in ('0', '1023'):
            assert rect.center().x() == pytest.approx(x)
        assert image.pixelColor(round(x), 22).getRgb() == (190, 160, 70, 255)
    for (_, first, _), (_, second, _) in zip(labels, labels[1:]):
        assert first.right() + 8 <= second.left()
    assert image.pixelColor(20, 29).alpha() == 255
    assert image.pixelColor(width - 10, 29).alpha() == 255
    assert all(image.pixelColor(x, 30).alpha() == 0 for x in range(width))
