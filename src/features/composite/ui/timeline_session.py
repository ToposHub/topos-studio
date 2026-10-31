# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Timeline UI session-state + ColumnModel (P8, T67/T75/T80).

Pure-data module capturing the timeline's *session* preferences — the UI
state the plan (§12) says should persist across saves but never influence
the render result. Two distinct concerns live here:

1. :class:`TimelineSessionState` — what the user has personalised about the
   timeline: which rows are expanded, the active filter, the zoom level,
   the time unit, the AE-nav toggle, the snap toggle. The plan (§12)
   explicitly says current selection, mouse position and drag state are
   NOT saved; this class captures only the durable prefs.
2. :class:`ColumnModel` — the left-panel column config (show/hide/reorder/
   width) the plan (§16.4 / T75 / T80) wants configurable. Backed by the
   fixed ColumnSpec table from batch 7's ``timeline_layout``, but mutable
   per session and serialisable.

Design rules:

* GUI-free. Serialises to/from plain JSON-safe dicts so it can be embedded
  in a project file's UI section (or a sidecar prefs file) without pulling
  in QWidget.
* Forward-compatible. Unknown keys on load are preserved (not dropped) so a
  newer file opens in an older build without losing prefs. Schema version
  field gates migration.
* Render-data separation. The class never reads the node graph; it's pure
  preference state. Expanded-node IDs are stored as a set, applied by the
  host when it rebuilds the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import timeline_layout


TIMELINE_SESSION_VERSION = 1


@dataclass
class ColumnConfig:
    """One column's runtime config (visibility + width + order).

    ``order`` is the column's position in the visible sequence (0-based);
    hidden columns keep their order so re-showing restores the original
    position. ``width`` is the user's chosen pixel width; 0 = use the spec
    default.
    """

    name: str
    visible: bool = True
    width: int = 0       # 0 = use ColumnSpec.width
    order: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "visible": self.visible,
                "width": int(self.width), "order": int(self.order)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ColumnConfig":
        return cls(
            name=str(data.get("name", "")),
            visible=bool(data.get("visible", True)),
            width=int(data.get("width", 0) or 0),
            order=int(data.get("order", 0) or 0),
        )


@dataclass
class ColumnModel:
    """Left-panel column configuration (T75/T80, plan §16.4).

    The base set of columns comes from :data:`timeline_layout.LEFT_COLUMNS`
    so the model can't invent columns the painter doesn't know how to draw.
    Users can:

    * toggle visibility (``set_visible``),
    * drag to reorder (``move_column``),
    * drag the right edge to resize (``set_width``),
    * restore defaults (``reset``).

    The model guarantees: the ``name`` column is always visible (you can't
    hide the parameter name) and no column shrinks below its
    ``ColumnSpec.min_width``.
    """

    configs: List[ColumnConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.configs:
            self.reset()

    # ── defaults ──
    def reset(self) -> None:
        """Restore the canonical column order/visibility/width from the spec."""
        self.configs = [
            ColumnConfig(name=spec.name,
                         visible=spec.visible_by_default,
                         width=0,  # 0 = use spec default
                         order=i)
            for i, spec in enumerate(timeline_layout.LEFT_COLUMNS)
        ]

    # ── read ──
    def visible_columns(self) -> List[Tuple[int, "timeline_layout.ColumnSpec",
                                            ColumnConfig]]:
        """Return (order, ColumnSpec, ColumnConfig) for each visible column,
        sorted by the user's order."""
        spec_by_name = {s.name: s for s in timeline_layout.LEFT_COLUMNS}
        out = []
        for cfg in self.configs:
            if not cfg.visible:
                continue
            spec = spec_by_name.get(cfg.name)
            if spec is None:
                continue
            out.append((cfg.order, spec, cfg))
        out.sort(key=lambda t: t[0])
        return out

    def column_widths(self, total_width: int) -> Dict[str, int]:
        """Resolve every visible column's pixel width for a given total.

        User widths override spec defaults; the ``name`` column absorbs the
        remainder after fixed columns take their (user-or-default, floored
        at spec min) widths.
        """
        spec_by_name = {s.name: s for s in timeline_layout.LEFT_COLUMNS}
        widths: Dict[str, int] = {}
        fixed_total = 0
        for cfg in self.configs:
            spec = spec_by_name.get(cfg.name)
            if spec is None or not cfg.visible or spec.name == "name":
                continue
            w = cfg.width if cfg.width > 0 else spec.width
            w = max(spec.min_width, w)
            widths[cfg.name] = w
            fixed_total += w
        name_spec = spec_by_name.get("name")
        if name_spec is not None:
            remainder = max(name_spec.min_width, total_width - fixed_total)
            widths["name"] = remainder
        return widths

    def is_visible(self, name: str) -> bool:
        cfg = self._find(name)
        return bool(cfg.visible) if cfg else False

    def width_for(self, name: str) -> int:
        cfg = self._find(name)
        return int(cfg.width) if cfg else 0

    # ── write ──
    def set_visible(self, name: str, visible: bool) -> bool:
        """Toggle a column's visibility. Returns False (no change) if the
        caller tries to hide the mandatory 'name' column."""
        if name == "name" and not visible:
            return False  # name column is always visible
        cfg = self._find(name)
        if cfg is None:
            return False
        if cfg.visible == visible:
            return False
        cfg.visible = visible
        return True

    def set_width(self, name: str, width: int) -> bool:
        spec_by_name = {s.name: s for s in timeline_layout.LEFT_COLUMNS}
        spec = spec_by_name.get(name)
        cfg = self._find(name)
        if spec is None or cfg is None:
            return False
        # Never shrink below the spec's minimum; never go negative.
        cfg.width = max(spec.min_width, max(0, int(width)))
        return True

    def move_column(self, name: str, new_order: int) -> bool:
        """Reorder a column. Other columns shift to make room. The 'name'
        column can be reordered but the host typically keeps it as the
        flexible-width column."""
        cfg = self._find(name)
        if cfg is None:
            return False
        new_order = max(0, int(new_order))
        # Build the order list, remove the moved column, reinsert.
        ordered = sorted(self.configs, key=lambda c: c.order)
        ordered = [c for c in ordered if c.name != name]
        ordered.insert(min(new_order, len(ordered)), cfg)
        for i, c in enumerate(ordered):
            c.order = i
        return True

    def _find(self, name: str) -> Optional[ColumnConfig]:
        for c in self.configs:
            if c.name == name:
                return c
        return None

    # ── serialise ──
    def to_dict(self) -> Dict[str, Any]:
        return {"columns": [c.to_dict() for c in self.configs]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ColumnModel":
        model = cls()
        model.reset()
        incoming = data.get("columns", []) if isinstance(data, Mapping) else []
        # Only accept configs whose name is in the spec; drop unknown names
        # silently (forward-compat: a newer file may have columns this build
        # doesn't paint).
        spec_names = {s.name for s in timeline_layout.LEFT_COLUMNS}
        known = {c.name: c for c in model.configs}
        for raw in incoming:
            try:
                cfg = ColumnConfig.from_dict(raw)
            except Exception:
                continue
            if cfg.name not in spec_names:
                continue
            base = known.get(cfg.name)
            if base is not None:
                base.visible = cfg.visible
                base.width = cfg.width
                base.order = cfg.order
        # Re-normalise order so there are no gaps after merging.
        model.configs.sort(key=lambda c: c.order)
        for i, c in enumerate(model.configs):
            c.order = i
        return model


@dataclass
class TimelineSessionState:
    """User-personalised timeline UI state (plan §12 / T67).

    Render-data separation: nothing here influences the render. The host
    applies ``expanded_node_ids`` and ``selected_param_row`` to the model
    on load; everything else is pure preference.
    """

    # Per-node expansion so reopening a comp keeps the user's tree state.
    expanded_node_ids: Set[str] = field(default_factory=set)
    # Active filter (matches KeyframesModel.set_filter vocabulary).
    filter_mode: str = "all"
    # Horizontal zoom + scroll so the user's framing survives a reload.
    pixels_per_frame: float = 4.0
    horizontal_scroll: float = 0.0
    # Time display unit.
    display_mode: str = "time"
    # AE-nav toggle (batch 8a) — saved so a user who prefers legacy K=add
    # doesn't have to re-toggle every session.
    ae_nav_enabled: bool = True
    snap_enabled: bool = True
    # Show/hide the expression-status column / expression rows.
    show_expression_rows: bool = True
    # Optional: the primary selected param row (node_id, param_name). Saved
    # as a convenience so reopening lands on the same param; None is fine.
    selected_param_row: Optional[Tuple[str, str]] = None
    # Column configuration.
    columns: ColumnModel = field(default_factory=ColumnModel)
    # Schema version — gates future migration.
    schema_version: int = TIMELINE_SESSION_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "expanded_node_ids": sorted(self.expanded_node_ids),
            "filter_mode": self.filter_mode,
            "pixels_per_frame": float(self.pixels_per_frame),
            "horizontal_scroll": float(self.horizontal_scroll),
            "display_mode": self.display_mode,
            "ae_nav_enabled": bool(self.ae_nav_enabled),
            "snap_enabled": bool(self.snap_enabled),
            "show_expression_rows": bool(self.show_expression_rows),
            "selected_param_row": (
                list(self.selected_param_row)
                if self.selected_param_row else None),
            "columns": self.columns.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimelineSessionState":
        if not isinstance(data, Mapping):
            return cls()
        state = cls()
        # schema_version — preserve the file's value for migration decisions
        # but clamp to the current version after applying defaults.
        # AUD-11102: the value comes from external project files; a null or
        # non-numeric value must fall back to the current version instead of
        # raising and aborting the whole session-state load (every other
        # field here already degrades gracefully).
        try:
            state.schema_version = int(data.get("schema_version",
                                                TIMELINE_SESSION_VERSION))
        except (TypeError, ValueError):
            state.schema_version = TIMELINE_SESSION_VERSION
        # Expanded nodes.
        ids = data.get("expanded_node_ids", [])
        if isinstance(ids, list):
            state.expanded_node_ids = {str(i) for i in ids if i}
        # Filter mode — validate against the known vocabulary.
        fm = str(data.get("filter_mode", "all"))
        state.filter_mode = fm if fm in ("all", "animated", "modified",
                                         "expression", "selected") else "all"
        # Numeric prefs — clamp to sane bounds.
        try:
            ppf = float(data.get("pixels_per_frame", 4.0))
            # FrameMapper clamps to [0.05, 256]; mirror that here.
            state.pixels_per_frame = max(0.05, min(256.0, ppf))
        except (TypeError, ValueError):
            state.pixels_per_frame = 4.0
        try:
            state.horizontal_scroll = float(data.get("horizontal_scroll", 0.0))
        except (TypeError, ValueError):
            state.horizontal_scroll = 0.0
        # Display mode.
        dm = str(data.get("display_mode", "time"))
        state.display_mode = dm if dm in ("time", "frame", "seconds",
                                          "timecode") else "time"
        # Boolean prefs.
        state.ae_nav_enabled = bool(data.get("ae_nav_enabled", True))
        state.snap_enabled = bool(data.get("snap_enabled", True))
        state.show_expression_rows = bool(data.get("show_expression_rows",
                                                   True))
        # Selected param row.
        spr = data.get("selected_param_row")
        if isinstance(spr, list) and len(spr) == 2:
            state.selected_param_row = (str(spr[0]), str(spr[1]))
        # Columns.
        cols = data.get("columns")
        if isinstance(cols, Mapping):
            state.columns = ColumnModel.from_dict(cols)
        return state


def migrate_session_state(data: Mapping[str, Any]) -> "TimelineSessionState":
    """Apply any version-to-version migrations, then build the state.

    Today there's only version 1, so this is a thin wrapper that forwards to
    from_dict. Future versions add migration steps here (e.g. renaming a
    deprecated key) so callers don't branch on schema_version themselves.
    The plan (§12 migration rules 1-6) governs keyframe/expression
    migration in the graph layer; this function only handles UI prefs.
    """
    return TimelineSessionState.from_dict(data)


__all__ = [
    "TIMELINE_SESSION_VERSION",
    "ColumnConfig", "ColumnModel",
    "TimelineSessionState", "migrate_session_state",
]
