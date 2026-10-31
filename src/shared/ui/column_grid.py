"""Column-count zoom model for icon-mode item views.

A zoom slider picks the COLUMN COUNT (default 1..8) instead of a pixel
size: card width derives from the live viewport width, so cards always tile
the full panel width — no dead strip on either side, and no "one huge card
+ blank half panel" dead zone at the top of the range (every slider
position is a visibly different packing).

Qt pitfalls this module encapsulates (each one was hit for real while
building the MG asset library — keep them fixed here):

1. Icon-mode pitch equals the gridSize width; ``setSpacing`` adds nothing
   between columns once a gridSize is set, so gaps must live INSIDE the
   cell (the icon is inset by ``gap`` and centred).
2. The flow wraps a column whose right edge lands exactly on the viewport
   edge — an exact division (e.g. 471/3) drops the last card of each row.
   Two px of slack prevent that.
3. An on-demand vertical scrollbar changes the viewport width whenever it
   toggles, which both leaves a blank strip and sets up
   resize→retile→resize oscillation.  Always-on keeps the width constant.
4. An event filter sees the view's Resize BEFORE QAbstractScrollArea has
   resized its viewport child, so re-tiling must be deferred one event-loop
   turn; the viewport itself must be watched too (scrollbar geometry
   changes resize it without touching the view).
5. Stylesheet font rules silently override programmatic ``setFont``, and
   item sizeHints derived from the iconSize stick at the size that was
   current when the item was created — re-pin them on every apply.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PyQt6.QtWidgets import QAbstractItemView, QSlider

_SETTINGS_ORG = "ToposColor"
_SETTINGS_APP = "ColumnGrids"


class ColumnGridController(QObject):
    """Drive one icon-mode view with a column-count zoom model.

    Wire a slider (optional) and an apply hook, then call :meth:`retile` is
    unnecessary — the controller reacts to view/viewport resizes itself.
    ``on_columns_applied(icon, cell)`` fires whenever the applied icon size
    actually changes (panel consumers use it to re-request thumbnails at
    the new size or sync a delegate's ``thumb_size``).
    """

    def __init__(self, view: QAbstractItemView, *,
                 slider: Optional[QSlider] = None,
                 settings_key: Optional[str] = None,
                 columns_default: int = 4,
                 columns_min: int = 1,
                 columns_max: int = 8,
                 gap: int = 6,
                 aspect: float = 1.0,
                 extra_height: int = 0,
                 pin_item_hints: bool = True,
                 on_columns_applied: Optional[Callable[[QSize, QSize], None]] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent if parent is not None else view)
        # Keep the Python wrapper reachable from the view's wrapper: the Qt
        # parent/child link only preserves the C++ side, and a Python-side
        # reference keeps the instance attributes alive for as long as the
        # view itself is alive.
        view._column_grid_controller = self
        self._view = view
        self._slider = slider
        self._settings_key = settings_key
        self._columns_min = max(1, int(columns_min))
        self._columns_max = max(self._columns_min, int(columns_max))
        self._default = max(self._columns_min,
                            min(self._columns_max, int(columns_default)))
        self._gap = max(0, int(gap))
        self._aspect = float(aspect) if aspect > 0 else 1.0
        self._extra_height = max(0, int(extra_height))
        self._pin_item_hints = bool(pin_item_hints)
        self._applied = on_columns_applied
        self._active = True
        self._columns = self._default
        self._last_icon = QSize()

        # Pitfall 3: a stable viewport width is what makes flush-right
        # tiling deterministic; the horizontal bar is never needed (the
        # 2px slack keeps every row inside the width) and a flooring
        # remainder must not spawn a phantom one.
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Pitfall 4: deferred re-tile, parented to the view so a pending
        # retile can never fire into a destroyed view.
        self._retile_timer = QTimer(self)
        self._retile_timer.setSingleShot(True)
        self._retile_timer.setInterval(0)
        self._retile_timer.timeout.connect(
            lambda: self.apply_columns(self._columns))
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)

        # AUD-38001: 已持久化列数标记 —— apply_columns 的两个高频触发源
        # （0ms retile 定时器在窗口拖拽缩放期间每次 Resize 重放、
        # slider.valueChanged）此前每次执行都无条件同步写 QSettings。
        # 窗口缩放风暴中列数不变，据此把写盘门控为"列数真正变化时
        # 恰好一次"；滑块拖动的写入次数则以列数范围宽度为上界。
        self._persisted_columns: Optional[int] = None

        if slider is not None:
            slider.setRange(self._columns_min, self._columns_max)
            slider.setValue(self._stored_columns())
            slider.valueChanged.connect(self.apply_columns)

        self._columns = self._stored_columns()
        # AUD-38001: 记住"已持久化"的列数 —— 初始装载用存储值回放，
        # 不触发一次冗余写盘（resize 风暴同理：columns 不变不写）。
        self._persisted_columns = self._columns
        self.apply_columns(self._columns)

    # -- public API ---------------------------------------------------------

    @property
    def columns(self) -> int:
        return self._columns

    def attach_slider(self, slider: QSlider) -> None:
        """Wire a slider created after the controller (e.g. a footer slider)."""
        self._slider = slider
        slider.setRange(self._columns_min, self._columns_max)
        slider.setValue(self._columns)
        slider.setEnabled(self._active)
        slider.valueChanged.connect(self.apply_columns)

    def set_active(self, active: bool) -> None:
        """Suspend/resume for views that switch to a non-icon mode.

        A suspended controller ignores slider/resize events (e.g. the
        Library panel's list mode clears the gridSize itself); resuming
        re-applies the current column count.
        """
        self._active = bool(active)
        if self._slider is not None:
            self._slider.setEnabled(self._active)
        if self._active:
            self.schedule_retile()

    def schedule_retile(self) -> None:
        """Re-apply the current columns on the next event-loop turn."""
        if not self._retile_timer.isActive():
            self._retile_timer.start()

    def pin_items(self) -> None:
        """Pin every item's sizeHint to the current cell (item-based views).

        Freshly added items otherwise carry the hint from whatever
        iconSize was current at creation time.
        """
        cell = self._view.gridSize()
        if cell.isValid():
            self._pin_all_item_hints(cell)

    # -- internals ----------------------------------------------------------

    def eventFilter(self, obj: Any, event: Any) -> bool:
        try:
            if (self._active and event.type() == QEvent.Type.Resize
                    and obj in (self._view, self._view.viewport())):
                self.schedule_retile()
        except AttributeError:
            # Teardown race: the owning panel's Python references are gone
            # while the C++ view/controller are still mid-destruction and a
            # resize event reaches this filter through an empty resurrected
            # wrapper (no instance attributes).  Nothing sensible is left
            # to re-tile — swallow and drop out of the filter chain.
            return False
        return super().eventFilter(obj, event)

    def _stored_columns(self) -> int:
        if self._settings_key is None:
            return self._default
        from PyQt6.QtCore import QSettings
        stored = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
            self._settings_key, self._default)
        try:
            columns = int(stored)
        except (TypeError, ValueError):
            return self._default
        return max(self._columns_min, min(self._columns_max, columns))

    def apply_columns(self, columns: int) -> None:
        """Tile ``columns`` cards across the exact viewport width."""
        self._columns = max(self._columns_min,
                            min(self._columns_max, int(columns)))
        if not self._active:
            return
        # A not-yet-shown view still reports a stub viewport; keep cells
        # sane until the first real resize re-applies with live width.
        width = max(self._columns * 40, self._view.viewport().width())
        # Pitfall 2: 2px slack so an exact division doesn't wrap the last
        # column.
        cell_w = max(24 + self._gap, (width - 2) // self._columns)
        # Pitfall 1: the gap lives inside the pitch.
        icon_w = cell_w - self._gap
        icon = QSize(icon_w, round(icon_w * self._aspect))
        cell = QSize(cell_w, icon.height() + self._extra_height + self._gap)
        self._view.setIconSize(icon)
        self._view.setGridSize(cell)
        if self._pin_item_hints:
            self._pin_all_item_hints(cell)
        self._view.doItemsLayout()
        # AUD-38001: 写盘门控 —— 仅当列数与上次持久化值不同时同步写一次。
        # resize 风暴重放同一列数（不触发任何写），窗口拖拽缩放期间的
        # 高频磁盘写入由此消除；既有 round-trip 语义保持不变。
        if (self._settings_key is not None
                and self._columns != self._persisted_columns):
            from PyQt6.QtCore import QSettings
            QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(
                self._settings_key, self._columns)
            self._persisted_columns = self._columns
        if icon != self._last_icon:
            self._last_icon = icon
            if self._applied is not None:
                self._applied(icon, cell)

    def _pin_all_item_hints(self, cell: QSize) -> None:
        item_at = getattr(self._view, "item", None)
        if item_at is None:
            return  # model/delegate views derive hints themselves
        for row in range(self._view.count()):
            item = item_at(row)
            if item is not None:
                item.setSizeHint(cell)


__all__ = ["ColumnGridController"]
