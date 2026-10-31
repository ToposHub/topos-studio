"""TM-04: the first executable templates (structured, no HTML dependency).

Each template is a deterministic ``(params, context) -> TeachingSceneDocument``
function that uses the Auto Layout resolver + design tokens.  Templates produce
STRUCTURE (elements + layout); motion is applied separately via the motion
commands so a template stays a clean, parameterized recipe (TM-01/TM-03).

Templates declared here: ``title_card``, ``section_divider``, ``steps``,
``lower_third``.  The registry also publishes the S2 brand-data building blocks.
"""

from __future__ import annotations

from typing import Any, Mapping

from .design_tokens import DesignTokenSet, resolve_style
from .executable_template import (
    ExecutableTemplate, ExecutableTemplateRegistry, ParamSpec, TemplateBuildContext,
)
from .layout import AutoLayout, LayoutChild, LayoutDirection, Rect, SizingMode, resolve_grid, resolve_stack
from .model import SCENE_SCHEMA_VERSION, SceneElement, SceneTransform, TeachingSceneDocument
from .template_registry import TemplateCapabilities


def _element(element_id: str, type_: str, rect: Rect, style: Mapping[str, Any],
             content: Any = "", z_index: int = 0) -> SceneElement:
    return SceneElement(
        element_id=element_id, type=type_, z_index=z_index, visible=True,
        transform=SceneTransform(
            x=rect.x, y=rect.y, width=max(1.0, rect.width), height=max(1.0, rect.height),
        ),
        style=dict(style), content=content,
    )


def _tokens(context: TemplateBuildContext) -> DesignTokenSet:
    return context.tokens


# ---------------------------------------------------------------------------
# title_card
# ---------------------------------------------------------------------------

def _build_title_card(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = tokens.resolve_value(params.get("accent_color", "$palette.primary"))
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.display"}, tokens)
    subtitle_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.heading"}, tokens)
    accent_style = resolve_style({"fill": accent}, tokens)
    inner_width = float(context.canvas_width) - 2.0 * (tokens.safe_area[0] + 40.0)
    children = [
        LayoutChild("accent_bar", sizing=SizingMode.HUG,
                    preferred_width=inner_width, preferred_height=8.0),
        LayoutChild("title", preferred_width=inner_width, preferred_height=80.0),
        LayoutChild("subtitle", preferred_width=inner_width, preferred_height=40.0),
    ]
    layout = AutoLayout(
        direction=LayoutDirection.VERTICAL, padding=(40.0, 40.0, 40.0, 40.0),
        gap=16.0, align="start", safe_area=tuple(tokens.safe_area),
    )
    rects = resolve_stack(children, context.canvas, layout)
    elements = [
        _element("accent_bar", "shape", rects["accent_bar"], accent_style, {"kind": "rect"}),
        _element("title", "text", rects["title"], title_style, str(params.get("title", "Title"))),
        _element("subtitle", "text", rects["subtitle"], subtitle_style, str(params.get("subtitle", ""))),
    ]
    return TeachingSceneDocument(
        scene_id=f"tpl_title_{params.get('title', 'x')[:8]}".replace(" ", "_"),
        schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Title Card")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den,
        duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


TITLE_CARD = ExecutableTemplate(
    template_id="title_card", version=1, display_name="Title Card",
    description="Large title with subtitle and an accent bar; vertical stack.",
    params_schema=(
        ParamSpec("title", "string", default="Title", required=True, description="Main title text."),
        ParamSpec("subtitle", "string", default="", description="Optional subtitle."),
        ParamSpec("accent_color", "color", default="$palette.primary", description="Accent bar colour (token or hex)."),
    ),
    build=_build_title_card,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "subtitle", "accent_color"),
        animation_properties=("opacity", "scale_x", "y"),
    ),
)


# ---------------------------------------------------------------------------
# section_divider
# ---------------------------------------------------------------------------

def _build_section_divider(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    index = str(params.get("index", "1"))
    label = str(params.get("label", "Section"))
    big_style = resolve_style({"color": tokens.resolve_value("$palette.primary"), "font_size": "$type_scale.display"}, tokens)
    label_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.heading"}, tokens)
    sl, st, sr, sb = tokens.safe_area
    content_w = max(0.0, context.canvas_width - (sl + sr) - 80.0)  # minus H padding (40+40)
    label_w = max(120.0, content_w - 200.0 - 40.0)                 # minus index width + gap
    children = [
        LayoutChild("index", preferred_width=200.0, preferred_height=120.0),
        LayoutChild("label", preferred_width=label_w, preferred_height=120.0),
    ]
    layout = AutoLayout(direction=LayoutDirection.HORIZONTAL, padding=(40.0, 40.0, 40.0, 40.0),
                        gap=40.0, align="center", safe_area=tuple(tokens.safe_area))
    rects = resolve_stack(children, context.canvas, layout)
    elements = [
        _element("index", "text", rects["index"], big_style, index),
        _element("label", "text", rects["label"], label_style, label),
    ]
    return TeachingSceneDocument(
        scene_id=f"tpl_section_{index}",
        schema_version=SCENE_SCHEMA_VERSION, name=f"Section {index}: {label}",
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den,
        duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


SECTION_DIVIDER = ExecutableTemplate(
    template_id="section_divider", version=1, display_name="Section Divider",
    description="Large index number beside a section label.",
    params_schema=(
        ParamSpec("index", "string", default="1", description="Section index/number."),
        ParamSpec("label", "string", default="Section", required=True, description="Section label."),
    ),
    build=_build_section_divider,
    capabilities=TemplateCapabilities(
        preview=True, export=True, editable_fields=("index", "label"),
        animation_properties=("opacity", "x"),
    ),
)


# ---------------------------------------------------------------------------
# steps (the S1-style four-step template)
# ---------------------------------------------------------------------------

def _build_steps(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    raw_steps = params.get("steps") or "Step one, Step two, Step three"
    if isinstance(raw_steps, str):
        parsed = [s.strip() for s in raw_steps.split(",") if s.strip()]
    elif isinstance(raw_steps, (list, tuple)):
        parsed = [str(s).strip() for s in raw_steps if str(s).strip()]
    else:
        raise ValueError("steps must be a comma-separated string or a list")
    if not parsed:
        raise ValueError("steps must contain at least one non-empty label")
    accent = tokens.resolve_value(params.get("accent_color", "$palette.primary"))
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    card_style = resolve_style({"fill": tokens.resolve_value("$palette.muted")}, tokens)
    card_style = {**card_style, "opacity": 0.15}
    label_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    number_style = resolve_style({"color": accent, "font_size": "$type_scale.heading"}, tokens)

    safe = tuple(tokens.safe_area)
    title_rect = Rect(x=context.canvas.x + safe[0] + 40.0, y=context.canvas.y + safe[1] + 40.0,
                      width=context.canvas_width - 2.0 * (safe[0] + 40.0), height=80.0)
    grid_canvas = Rect(
        x=context.canvas.x + safe[0] + 40.0, y=title_rect.y + title_rect.height + 24.0,
        width=context.canvas_width - 2.0 * (safe[0] + 40.0),
        height=context.canvas_height - (title_rect.y + title_rect.height + 24.0) - safe[3] - 40.0,
    )
    # AUD-27311: the advertised ``columns`` ParamSpec ("0 = auto") was never
    # read — honour it, falling back to the auto cap of 4 when unset/invalid.
    try:
        requested_columns = int(params.get("columns") or 0)
    except (TypeError, ValueError):
        requested_columns = 0
    columns = requested_columns if requested_columns > 0 else min(len(parsed), 4)
    columns = max(1, min(columns, len(parsed)))
    card_children = [LayoutChild(f"card_{i + 1}", preferred_width=1.0, preferred_height=1.0) for i in range(len(parsed))]
    card_rects = resolve_grid(card_children, grid_canvas, columns=columns, gap=24.0)

    elements = [_element("title", "text", title_rect, title_style, str(params.get("title", "Steps")))]
    for index, step in enumerate(parsed, start=1):
        cid = f"card_{index}"
        rect = card_rects[cid]
        elements.append(_element(cid, "shape", rect, card_style, {"kind": "rect"}, z_index=1))
        elements.append(_element(f"{cid}_number", "text",
                                 Rect(x=rect.x + 16.0, y=rect.y + 12.0, width=rect.width - 32.0, height=40.0),
                                 number_style, str(index), z_index=2))
        elements.append(_element(f"{cid}_label", "text",
                                 Rect(x=rect.x + 16.0, y=rect.y + 56.0, width=rect.width - 32.0, height=rect.height - 68.0),
                                 label_style, str(step), z_index=2))
    return TeachingSceneDocument(
        scene_id="tpl_steps",
        schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Steps")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den,
        duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


STEPS = ExecutableTemplate(
    template_id="steps", version=1, display_name="Steps",
    description="A title row plus N step cards in a responsive grid (S1 style).",
    params_schema=(
        ParamSpec("title", "string", default="Steps", description="Optional header title."),
        ParamSpec("steps", "string", default="Step one, Step two, Step three",
                  description="Comma-separated step labels."),
        ParamSpec("accent_color", "color", default="$palette.primary", description="Number accent colour."),
        ParamSpec("columns", "integer", default=0, description="Max columns (0 = auto)."),
    ),
    build=_build_steps,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "steps", "accent_color"),
        animation_properties=("opacity", "scale_x", "y"),
    ),
)


# ---------------------------------------------------------------------------
# lower_third
# ---------------------------------------------------------------------------

def _build_lower_third(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = tokens.resolve_value(params.get("accent_color", "$palette.primary"))
    name_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.heading"}, tokens)
    role_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.body"}, tokens)
    bar_style = resolve_style({"fill": accent}, tokens)
    safe = tuple(tokens.safe_area)
    bar = Rect(x=safe[0] + 40.0, y=context.canvas_height - safe[3] - 140.0,
               width=8.0, height=100.0)
    name_rect = Rect(x=bar.x + 24.0, y=bar.y, width=context.canvas_width - bar.x - 100.0, height=56.0)
    role_rect = Rect(x=bar.x + 24.0, y=bar.y + 56.0, width=context.canvas_width - bar.x - 100.0, height=40.0)
    elements = [
        _element("accent_bar", "shape", bar, bar_style, {"kind": "rect"}),
        _element("name", "text", name_rect, name_style, str(params.get("name", "Name"))),
        _element("role", "text", role_rect, role_style, str(params.get("role", ""))),
    ]
    return TeachingSceneDocument(
        scene_id="tpl_lower_third",
        schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("name", "Lower Third")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den,
        duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


LOWER_THIRD = ExecutableTemplate(
    template_id="lower_third", version=1, display_name="Lower Third",
    description="Speaker name + role with an accent bar at the lower third.",
    params_schema=(
        ParamSpec("name", "string", default="Speaker", required=True),
        ParamSpec("role", "string", default="", description="Optional role/organisation."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_lower_third,
    capabilities=TemplateCapabilities(
        preview=True, export=True, editable_fields=("name", "role", "accent_color"),
        animation_properties=("opacity", "x"),
    ),
)


# ---------------------------------------------------------------------------
# Shared helpers for the remaining templates (TM-04 full first batch of 15)
# ---------------------------------------------------------------------------

def _split_list(value: Any, *, sep: str = ",") -> list[str]:
    """Parse a comma-separated string OR a list into trimmed non-empty items."""
    if isinstance(value, str):
        return [s.strip() for s in value.split(sep) if s.strip()]
    if isinstance(value, (list, tuple)):
        return [str(s).strip() for s in value if str(s).strip()]
    raise ValueError("expected a comma-separated string or a list")


def _accent(params: Mapping[str, Any], tokens: DesignTokenSet) -> Any:
    return tokens.resolve_value(params.get("accent_color", "$palette.primary"))


def _safe_inset(tokens: DesignTokenSet, margin: float = 40.0) -> tuple[float, float, float, float]:
    """Token safe-area + an authoring margin; returns (l, t, r, b) insets."""
    sl, st, sr, sb = tokens.safe_area
    return (sl + margin, st + margin, sr + margin, sb + margin)


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def _attribution(params: Mapping[str, Any]) -> str:
    author = str(params.get("author", "")).strip()
    role = str(params.get("role", "")).strip()
    if author and role:
        return f"{author} \u00b7 {role}"
    return author or role


def _split_events(value: Any) -> list[tuple[str, str]]:
    """Parse "year:label" pairs (falls back to ordinal index for bare labels)."""
    parts = _split_list(value)
    events: list[tuple[str, str]] = []
    for index, part in enumerate(parts):
        if ":" in part:
            year, _, label = part.partition(":")
            events.append((year.strip(), label.strip()))
        else:
            events.append((str(index + 1), part))
    return events


# ---------------------------------------------------------------------------
# process_flow
# ---------------------------------------------------------------------------

def _build_process_flow(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    nodes = _split_list(params.get("nodes", "Start, Middle, End"))
    if len(nodes) < 2:
        raise ValueError("process_flow needs at least 2 nodes")
    vertical = str(params.get("direction", "horizontal")) == "vertical"
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    node_style = {**resolve_style({"fill": tokens.resolve_value("$palette.muted")}, tokens), "opacity": 0.18}
    label_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    arrow_style = resolve_style({"fill": accent}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st,
                      context.canvas_width - sl - sr, 70.0)
    flow = Rect(context.canvas.x + sl, title_rect.y + title_rect.height + 24.0,
                context.canvas_width - sl - sr,
                context.canvas_height - (title_rect.y + title_rect.height + 24.0) - sb)
    n = len(nodes)
    gap = 48.0
    elements = [_element("title", "text", title_rect, title_style, str(params.get("title", "Process")))]
    if vertical:
        cell_h = max(40.0, (flow.height - gap * (n - 1)) / n)
        cursor_y = flow.y
        for index, label in enumerate(nodes, start=1):
            rect = Rect(flow.x, cursor_y, flow.width, cell_h)
            elements.append(_element(f"node_{index}", "shape", rect, node_style, {"kind": "rect"}, z_index=1))
            elements.append(_element(f"node_{index}_label", "text",
                                     Rect(rect.x + 16.0, rect.y + rect.height / 2.0 - 16.0, rect.width - 32.0, 32.0),
                                     label_style, str(label), z_index=2))
            cursor_y += cell_h
            if index < n:
                elements.append(_element(f"arrow_{index}", "shape",
                                         Rect(flow.x + flow.width / 2.0 - 4.0, cursor_y + 8.0, 8.0, gap - 16.0),
                                         arrow_style, {"kind": "rect"}))
                cursor_y += gap
    else:
        cell_w = max(60.0, (flow.width - gap * (n - 1)) / n)
        cursor_x = flow.x
        for index, label in enumerate(nodes, start=1):
            rect = Rect(cursor_x, flow.y, cell_w, flow.height)
            elements.append(_element(f"node_{index}", "shape", rect, node_style, {"kind": "rect"}, z_index=1))
            elements.append(_element(f"node_{index}_label", "text",
                                     Rect(rect.x + 12.0, rect.y + rect.height / 2.0 - 16.0, rect.width - 24.0, 32.0),
                                     label_style, str(label), z_index=2))
            cursor_x += cell_w
            if index < n:
                elements.append(_element(f"arrow_{index}", "shape",
                                         Rect(cursor_x + 8.0, flow.y + flow.height / 2.0 - 4.0, gap - 16.0, 8.0),
                                         arrow_style, {"kind": "rect"}))
                cursor_x += gap
    return TeachingSceneDocument(
        scene_id="tpl_process_flow", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Process Flow")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


PROCESS_FLOW = ExecutableTemplate(
    template_id="process_flow", version=1, display_name="Process Flow",
    description="A chain of nodes connected by arrows (horizontal or vertical).",
    params_schema=(
        ParamSpec("title", "string", default="Process", description="Optional header title."),
        ParamSpec("nodes", "string", default="Start, Middle, End",
                  description="Comma-separated node labels (>= 2)."),
        ParamSpec("direction", "string", default="horizontal", enum=("horizontal", "vertical"),
                  description="Flow direction."),
        ParamSpec("accent_color", "color", default="$palette.primary", description="Arrow accent colour."),
    ),
    build=_build_process_flow,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "nodes", "direction", "accent_color"),
        animation_properties=("opacity", "x", "y"),
    ),
)


# ---------------------------------------------------------------------------
# data_chart (bar chart)
# ---------------------------------------------------------------------------

def _parse_numbers(value: Any) -> list[float]:
    if isinstance(value, str):
        out: list[float] = []
        for tok in value.split(","):
            tok = tok.strip()
            if tok:
                out.append(float(tok))
        return out
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    raise ValueError("expected a comma-separated string or a list of numbers")


def _build_data_chart(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    values = _parse_numbers(params.get("values", "3, 5, 2, 7, 4"))
    if not values:
        raise ValueError("data_chart needs at least one value")
    labels = _split_list(params.get("labels", ""))
    max_value = float(params.get("max_value", 0) or 0)
    if max_value <= 0:
        max_value = max(values)
    if max_value <= 0:
        max_value = 1.0
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    bar_style = resolve_style({"fill": accent}, tokens)
    axis_style = resolve_style({"fill": tokens.resolve_value("$palette.muted")}, tokens)
    label_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.body"}, tokens)
    value_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st,
                      context.canvas_width - sl - sr, 70.0)
    chart = Rect(context.canvas.x + sl, title_rect.y + title_rect.height + 16.0,
                 context.canvas_width - sl - sr,
                 context.canvas_height - (title_rect.y + title_rect.height + 16.0) - sb - 40.0)
    baseline_y = chart.y + chart.height
    n = len(values)
    bar_gap = 24.0
    bar_w = max(8.0, (chart.width - bar_gap * (n - 1)) / n)
    elements = [
        _element("title", "text", title_rect, title_style, str(params.get("title", "Chart"))),
        _element("axis", "shape", Rect(chart.x, baseline_y, chart.width, 2.0), axis_style, {"kind": "rect"}),
    ]
    cursor_x = chart.x
    for index, v in enumerate(values, start=1):
        h = max(1.0, (v / max_value) * chart.height)
        bar_rect = Rect(cursor_x, baseline_y - h, bar_w, h)
        elements.append(_element(f"bar_{index}", "shape", bar_rect, bar_style, {"kind": "rect"}, z_index=1))
        elements.append(_element(f"bar_{index}_value", "text",
                                 Rect(cursor_x, baseline_y - h - 28.0, bar_w, 24.0),
                                 value_style, _fmt_num(v), z_index=2))
        if index - 1 < len(labels):
            elements.append(_element(f"bar_{index}_label", "text",
                                     Rect(cursor_x, baseline_y + 4.0, bar_w, 32.0),
                                     label_style, str(labels[index - 1]), z_index=2))
        cursor_x += bar_w + bar_gap
    return TeachingSceneDocument(
        scene_id="tpl_data_chart", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Data Chart")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


DATA_CHART = ExecutableTemplate(
    template_id="data_chart", version=1, display_name="Bar Chart",
    description="A titled bar chart with value labels and optional category labels.",
    params_schema=(
        ParamSpec("title", "string", default="Chart"),
        ParamSpec("values", "string", default="3, 5, 2, 7, 4",
                  description="Comma-separated bar values (numbers)."),
        ParamSpec("labels", "string", default="", description="Comma-separated category labels."),
        ParamSpec("max_value", "number", default=0, description="Y maximum (0 = auto from data)."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_data_chart,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "values", "labels", "max_value", "accent_color"),
        animation_properties=("scale_y", "opacity"),
    ),
)


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def _build_comparison(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    left_items = _split_list(params.get("left_items", ""))
    right_items = _split_list(params.get("right_items", ""))
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    head_style = resolve_style({"color": accent, "font_size": "$type_scale.heading"}, tokens)
    item_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    vs_style = resolve_style({"color": tokens.resolve_value("$palette.muted"),
                              "font_size": "$type_scale.heading"}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st,
                      context.canvas_width - sl - sr, 70.0)
    body = Rect(context.canvas.x + sl, title_rect.y + title_rect.height + 16.0,
                context.canvas_width - sl - sr,
                context.canvas_height - (title_rect.y + title_rect.height + 16.0) - sb)
    col_gap = 80.0
    col_w = max(120.0, (body.width - col_gap) / 2.0)
    left_col = Rect(body.x, body.y, col_w, body.height)
    right_col = Rect(body.x + col_w + col_gap, body.y, col_w, body.height)
    elements = [
        _element("title", "text", title_rect, title_style, str(params.get("title", "Compare"))),
        _element("left_head", "text", Rect(left_col.x, left_col.y, left_col.width, 50.0), head_style,
                 str(params.get("left_label", "A"))),
        _element("right_head", "text", Rect(right_col.x, right_col.y, right_col.width, 50.0), head_style,
                 str(params.get("right_label", "B"))),
        _element("vs", "text",
                 Rect(left_col.x + col_w, body.y + body.height / 2.0 - 24.0, col_gap, 48.0),
                 vs_style, "VS"),
    ]
    row_h = 40.0
    for index, item in enumerate(left_items, start=1):
        elements.append(_element(f"left_{index}", "text",
                                 Rect(left_col.x, left_col.y + 64.0 + (index - 1) * row_h, left_col.width, row_h),
                                 item_style, str(item)))
    for index, item in enumerate(right_items, start=1):
        elements.append(_element(f"right_{index}", "text",
                                 Rect(right_col.x, right_col.y + 64.0 + (index - 1) * row_h, right_col.width, row_h),
                                 item_style, str(item)))
    return TeachingSceneDocument(
        scene_id="tpl_comparison", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Comparison")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


COMPARISON = ExecutableTemplate(
    template_id="comparison", version=1, display_name="Comparison",
    description="Two labelled columns of bullet items with a VS marker.",
    params_schema=(
        ParamSpec("title", "string", default="Compare"),
        ParamSpec("left_label", "string", default="Before"),
        ParamSpec("right_label", "string", default="After"),
        ParamSpec("left_items", "string", default="", description="Comma-separated left items."),
        ParamSpec("right_items", "string", default="", description="Comma-separated right items."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_comparison,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "left_label", "right_label", "left_items", "right_items", "accent_color"),
        animation_properties=("opacity", "x"),
    ),
)


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------

def _build_quote(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    mark_style = resolve_style({"color": accent, "font_size": "$type_scale.display"}, tokens)
    quote_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.heading"}, tokens)
    author_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.body"}, tokens)
    sl, st, sr, sb = _safe_inset(tokens, margin=80.0)
    inner_w = context.canvas_width - sl - sr
    mark = Rect(context.canvas.x + sl, context.canvas.y + st, inner_w, 90.0)
    quote_rect = Rect(context.canvas.x + sl, mark.y + mark.height - 20.0, inner_w, 200.0)
    author_rect = Rect(context.canvas.x + sl, quote_rect.y + quote_rect.height + 16.0, inner_w, 40.0)
    elements = [
        _element("mark", "text", mark, mark_style, "\u201c"),
        _element("quote", "text", quote_rect, quote_style, str(params.get("quote", "\u2026"))),
        _element("author", "text", author_rect, author_style, _attribution(params)),
    ]
    return TeachingSceneDocument(
        scene_id="tpl_quote", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("author") or "Quote"),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


QUOTE = ExecutableTemplate(
    template_id="quote", version=1, display_name="Quote",
    description="A large pull quote with an attribution line.",
    params_schema=(
        ParamSpec("quote", "string", default="\u2026", required=True, description="The quote text."),
        ParamSpec("author", "string", default="", description="Attribution name."),
        ParamSpec("role", "string", default="", description="Optional role / organisation."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_quote,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("quote", "author", "role", "accent_color"),
        animation_properties=("opacity", "y"),
    ),
)


# ---------------------------------------------------------------------------
# formula_proof
# ---------------------------------------------------------------------------

def _build_formula_proof(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    steps = _split_list(params.get("steps", ""))
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    formula_style = resolve_style({"color": accent, "font_size": "$type_scale.display"}, tokens)
    step_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    num_style = resolve_style({"color": tokens.resolve_value("$palette.muted"),
                               "font_size": "$type_scale.body"}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    inner_w = context.canvas_width - sl - sr
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st, inner_w, 64.0)
    formula_rect = Rect(context.canvas.x + sl, title_rect.y + title_rect.height + 16.0, inner_w, 140.0)
    elements = [
        _element("title", "text", title_rect, title_style, str(params.get("title", "Proof"))),
        _element("formula", "text", formula_rect, formula_style, str(params.get("formula", "E = mc^2"))),
    ]
    step_y = formula_rect.y + formula_rect.height + 24.0
    row_h = 44.0
    for index, step in enumerate(steps, start=1):
        elements.append(_element(f"step_{index}_num", "text",
                                 Rect(context.canvas.x + sl, step_y, 48.0, row_h),
                                 num_style, f"{index}."))
        elements.append(_element(f"step_{index}", "text",
                                 Rect(context.canvas.x + sl + 56.0, step_y, inner_w - 56.0, row_h),
                                 step_style, str(step)))
        step_y += row_h
    return TeachingSceneDocument(
        scene_id="tpl_formula_proof", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Formula")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


FORMULA_PROOF = ExecutableTemplate(
    template_id="formula_proof", version=1, display_name="Formula / Proof",
    description="A displayed formula followed by numbered derivation steps.",
    params_schema=(
        ParamSpec("title", "string", default="Proof"),
        ParamSpec("formula", "string", default="E = mc^2", required=True, description="The displayed formula."),
        ParamSpec("steps", "string", default="", description="Comma-separated derivation steps."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_formula_proof,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "formula", "steps", "accent_color"),
        animation_properties=("opacity", "y"),
    ),
)


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------

def _build_timeline(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    events = _split_events(params.get("events", "2020:A, 2021:B, 2022:C"))
    if not events:
        raise ValueError("timeline needs at least one event")
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    year_style = resolve_style({"color": accent, "font_size": "$type_scale.heading"}, tokens)
    label_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    axis_style = resolve_style({"fill": tokens.resolve_value("$palette.muted")}, tokens)
    dot_style = resolve_style({"fill": accent}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    inner_w = context.canvas_width - sl - sr
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st, inner_w, 64.0)
    axis_y = context.canvas_height / 2.0
    elements = [
        _element("title", "text", title_rect, title_style, str(params.get("title", "Timeline"))),
        _element("axis", "shape", Rect(context.canvas.x + sl, axis_y - 1.5, inner_w, 3.0),
                 axis_style, {"kind": "rect"}),
    ]
    n = len(events)
    if n == 1:
        xs = [context.canvas.x + sl + inner_w / 2.0]
    else:
        step = inner_w / (n - 1)
        xs = [context.canvas.x + sl + i * step for i in range(n)]
    for index, (year, label) in enumerate(events, start=1):
        cx = xs[index - 1]
        elements.append(_element(f"event_{index}_dot", "shape",
                                 Rect(cx - 8.0, axis_y - 8.0, 16.0, 16.0),
                                 dot_style, {"kind": "rect"}))
        above = (index % 2 == 1)
        if above:
            year_rect = Rect(cx - 80.0, axis_y - 90.0, 160.0, 36.0)
            label_rect = Rect(cx - 80.0, axis_y - 54.0, 160.0, 30.0)
        else:
            year_rect = Rect(cx - 80.0, axis_y + 54.0, 160.0, 36.0)
            label_rect = Rect(cx - 80.0, axis_y + 18.0, 160.0, 30.0)
        elements.append(_element(f"event_{index}_year", "text", year_rect, year_style, str(year)))
        elements.append(_element(f"event_{index}_label", "text", label_rect, label_style, str(label)))
    return TeachingSceneDocument(
        scene_id="tpl_timeline", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Timeline")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


TIMELINE = ExecutableTemplate(
    template_id="timeline", version=1, display_name="Timeline",
    description="A horizontal axis with alternating above/below dated events.",
    params_schema=(
        ParamSpec("title", "string", default="Timeline"),
        ParamSpec("events", "string", default="2020:A, 2021:B, 2022:C",
                  description='Comma-separated "year:label" pairs.'),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_timeline,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "events", "accent_color"),
        animation_properties=("opacity", "x"),
    ),
)


# ---------------------------------------------------------------------------
# checklist
# ---------------------------------------------------------------------------

def _build_checklist(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    items = _split_list(params.get("items", "First, Second, Third"))
    if not items:
        raise ValueError("checklist needs at least one item")
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.title"}, tokens)
    box_style = resolve_style({"fill": accent}, tokens)
    check_style = resolve_style({"fill": tokens.resolve_value("$palette.surface")}, tokens)
    item_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    inner_w = context.canvas_width - sl - sr
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st, inner_w, 64.0)
    row_h = 48.0
    gap = 12.0
    elements = [_element("title", "text", title_rect, title_style, str(params.get("title", "Checklist")))]
    cursor_y = title_rect.y + title_rect.height + 16.0
    for index, item in enumerate(items, start=1):
        box = Rect(context.canvas.x + sl, cursor_y, 28.0, 28.0)
        elements.append(_element(f"item_{index}_box", "shape", box, box_style, {"kind": "rect"}, z_index=1))
        elements.append(_element(f"item_{index}_check", "shape",
                                 Rect(box.x + 7.0, box.y + 13.0, 14.0, 4.0),
                                 check_style, {"kind": "rect"}, z_index=2))
        elements.append(_element(f"item_{index}", "text",
                                 Rect(box.x + 40.0, cursor_y, inner_w - 40.0, 32.0),
                                 item_style, str(item)))
        cursor_y += row_h + gap
    return TeachingSceneDocument(
        scene_id="tpl_checklist", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Checklist")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


CHECKLIST = ExecutableTemplate(
    template_id="checklist", version=1, display_name="Checklist",
    description="A titled list of checked rows.",
    params_schema=(
        ParamSpec("title", "string", default="Checklist"),
        ParamSpec("items", "string", default="First, Second, Third",
                  description="Comma-separated list items."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_checklist,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "items", "accent_color"),
        animation_properties=("opacity", "y"),
    ),
)


# ---------------------------------------------------------------------------
# tutorial_callout
# ---------------------------------------------------------------------------

def _build_tutorial_callout(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    title_style = resolve_style({"color": accent, "font_size": "$type_scale.heading"}, tokens)
    body_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    border_style = resolve_style({"fill": accent}, tokens)
    bg_style = {**resolve_style({"fill": tokens.resolve_value("$palette.muted")}, tokens), "opacity": 0.15}
    pointer = str(params.get("pointer_direction", "left"))
    sl, st, sr, sb = _safe_inset(tokens)
    inner_w = context.canvas_width - sl - sr
    box_h = min(320.0, context.canvas_height - st - sb)
    box = Rect(context.canvas.x + sl, context.canvas_height / 2.0 - box_h / 2.0, inner_w, box_h)
    if pointer == "right":
        bar = Rect(box.x + box.width - 6.0, box.y, 6.0, box.height)
    elif pointer == "top":
        bar = Rect(box.x, box.y, box.width, 6.0)
    elif pointer == "bottom":
        bar = Rect(box.x, box.y + box.height - 6.0, box.width, 6.0)
    else:
        bar = Rect(box.x, box.y, 6.0, box.height)
    pad = 28.0
    title_rect = Rect(box.x + pad, box.y + pad, box.width - 2.0 * pad, 44.0)
    body_rect = Rect(box.x + pad, title_rect.y + title_rect.height + 12.0,
                     box.width - 2.0 * pad, box.height - 2.0 * pad - 44.0 - 12.0)
    elements = [
        _element("bg", "shape", box, bg_style, {"kind": "rect"}, z_index=0),
        _element("border", "shape", bar, border_style, {"kind": "rect"}, z_index=1),
        _element("title", "text", title_rect, title_style, str(params.get("title", "Note"))),
        _element("body", "text", body_rect, body_style, str(params.get("body", ""))),
    ]
    return TeachingSceneDocument(
        scene_id="tpl_callout", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Callout")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


TUTORIAL_CALLOUT = ExecutableTemplate(
    template_id="tutorial_callout", version=1, display_name="Tutorial Callout",
    description="A highlighted note box with an accent border on a chosen edge.",
    params_schema=(
        ParamSpec("title", "string", default="Note"),
        ParamSpec("body", "string", default="", description="Callout body text."),
        ParamSpec("pointer_direction", "string", default="left",
                  enum=("left", "right", "top", "bottom"),
                  description="Which edge carries the accent bar."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_tutorial_callout,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "body", "pointer_direction", "accent_color"),
        animation_properties=("opacity", "scale"),
    ),
)


# ---------------------------------------------------------------------------
# social_card
# ---------------------------------------------------------------------------

def _build_social_card(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    headline_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.display"}, tokens)
    handle_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.body"}, tokens)
    tag_label_style = resolve_style({"color": tokens.resolve_value("$palette.surface"),
                                     "font_size": "$type_scale.body"}, tokens)
    pill_style = resolve_style({"fill": accent}, tokens)
    sl, st, sr, sb = _safe_inset(tokens, margin=48.0)
    inner_w = context.canvas_width - sl - sr
    inner_h = context.canvas_height - st - sb
    headline_rect = Rect(context.canvas.x + sl, context.canvas.y + st + inner_h * 0.28,
                         inner_w, inner_h * 0.34)
    handle_rect = Rect(context.canvas.x + sl, context.canvas.y + context.canvas_height - sb - 36.0,
                       inner_w * 0.6, 28.0)
    tag_text = str(params.get("tag", "#tag"))
    tag_w = max(80.0, len(tag_text) * 14.0 + 32.0)
    pill = Rect(context.canvas.x + context.canvas_width - sr - tag_w, context.canvas.y + st, tag_w, 36.0)
    tag_rect = Rect(pill.x + 12.0, pill.y + 6.0, pill.width - 24.0, 24.0)
    elements = [
        _element("headline", "text", headline_rect, headline_style, str(params.get("headline", "Headline"))),
        _element("handle", "text", handle_rect, handle_style, str(params.get("handle", "@topos"))),
        _element("tag_pill", "shape", pill, pill_style, {"kind": "rect"}, z_index=1),
        _element("tag", "text", tag_rect, tag_label_style, tag_text, z_index=2),
    ]
    return TeachingSceneDocument(
        scene_id="tpl_social_card", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("headline", "Social Card")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


SOCIAL_CARD = ExecutableTemplate(
    template_id="social_card", version=1, display_name="Social Card",
    description="A square social card: big headline, handle, and an accent tag pill.",
    params_schema=(
        ParamSpec("headline", "string", default="Headline", required=True),
        ParamSpec("handle", "string", default="@topos", description="Author / channel handle."),
        ParamSpec("tag", "string", default="#tag", description="Short tag shown in the accent pill."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_social_card,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("headline", "handle", "tag", "accent_color"),
        animation_properties=("opacity", "scale"),
    ),
)


# ---------------------------------------------------------------------------
# product_feature
# ---------------------------------------------------------------------------

def _build_product_feature(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    features = _split_list(params.get("features", "Fast, Reliable, Open"))
    if not features:
        raise ValueError("product_feature needs at least one feature")
    title_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.display"}, tokens)
    sub_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.heading"}, tokens)
    bullet_style = resolve_style({"fill": accent}, tokens)
    feat_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.body"}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    inner_w = context.canvas_width - sl - sr
    title_rect = Rect(context.canvas.x + sl, context.canvas.y + st, inner_w, 90.0)
    sub_rect = Rect(context.canvas.x + sl, title_rect.y + title_rect.height + 8.0, inner_w, 50.0)
    elements = [
        _element("title", "text", title_rect, title_style, str(params.get("title", "Product"))),
        _element("subtitle", "text", sub_rect, sub_style, str(params.get("subtitle", ""))),
    ]
    cursor_y = sub_rect.y + sub_rect.height + 28.0
    row_h = 44.0
    for index, feat in enumerate(features, start=1):
        bullet = Rect(context.canvas.x + sl, cursor_y + 14.0, 12.0, 12.0)
        elements.append(_element(f"feature_{index}_bullet", "shape", bullet, bullet_style, {"kind": "rect"}))
        elements.append(_element(f"feature_{index}", "text",
                                 Rect(bullet.x + 24.0, cursor_y, inner_w - 24.0, row_h),
                                 feat_style, str(feat)))
        cursor_y += row_h
    return TeachingSceneDocument(
        scene_id="tpl_product_feature", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("title", "Product Feature")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


PRODUCT_FEATURE = ExecutableTemplate(
    template_id="product_feature", version=1, display_name="Product Feature",
    description="Title + subtitle + a bulleted list of product features.",
    params_schema=(
        ParamSpec("title", "string", default="Product", required=True),
        ParamSpec("subtitle", "string", default="", description="Optional supporting subtitle."),
        ParamSpec("features", "string", default="Fast, Reliable, Open",
                  description="Comma-separated feature bullets."),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_product_feature,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("title", "subtitle", "features", "accent_color"),
        animation_properties=("opacity", "x"),
    ),
)


# ---------------------------------------------------------------------------
# cta (call to action)
# ---------------------------------------------------------------------------

def _build_cta(params: Mapping[str, Any], context: TemplateBuildContext) -> TeachingSceneDocument:
    tokens = _tokens(context)
    accent = _accent(params, tokens)
    head_style = resolve_style({"color": "$palette.surface_fg", "font_size": "$type_scale.display"}, tokens)
    sub_style = resolve_style({"color": "$palette.muted", "font_size": "$type_scale.heading"}, tokens)
    btn_label_style = resolve_style({"color": tokens.resolve_value("$palette.surface"),
                                     "font_size": "$type_scale.body"}, tokens)
    btn_style = resolve_style({"fill": accent}, tokens)
    sl, st, sr, sb = _safe_inset(tokens)
    inner_w = context.canvas_width - sl - sr
    inner_h = context.canvas_height - st - sb
    head_rect = Rect(context.canvas.x + sl, context.canvas.y + st + inner_h * 0.22,
                     inner_w, inner_h * 0.30)
    sub_rect = Rect(context.canvas.x + sl, head_rect.y + head_rect.height + 12.0,
                    inner_w, inner_h * 0.16)
    btn_label = str(params.get("button_label", "Get Started"))
    btn_w = max(160.0, len(btn_label) * 18.0 + 64.0)
    btn_h = 64.0
    btn_x = context.canvas.x + sl + (inner_w - btn_w) / 2.0
    btn_y = sub_rect.y + sub_rect.height + 32.0
    btn = Rect(btn_x, btn_y, btn_w, btn_h)
    elements = [
        _element("headline", "text", head_rect, head_style, str(params.get("headline", "Get started today"))),
        _element("subtext", "text", sub_rect, sub_style, str(params.get("subtext", ""))),
        _element("button", "shape", btn, btn_style, {"kind": "rect"}, z_index=1),
        _element("button_label", "text",
                 Rect(btn.x, btn.y + btn_h / 2.0 - 16.0, btn_w, 32.0),
                 btn_label_style, btn_label, z_index=2),
    ]
    return TeachingSceneDocument(
        scene_id="tpl_cta", schema_version=SCENE_SCHEMA_VERSION, name=str(params.get("headline", "Call to Action")),
        width=context.canvas_width, height=context.canvas_height,
        fps_num=context.fps_num, fps_den=context.fps_den, duration_frames=context.duration_frames,
        background={"color": tokens.resolve_value("$palette.surface")},
        elements=tuple(elements), animations=(),
    )


CTA = ExecutableTemplate(
    template_id="cta", version=1, display_name="Call to Action",
    description="A centred headline + subtext + a brand-accent button.",
    params_schema=(
        ParamSpec("headline", "string", default="Get started today", required=True),
        ParamSpec("subtext", "string", default="", description="Optional supporting line."),
        ParamSpec("button_label", "string", default="Get Started"),
        ParamSpec("accent_color", "color", default="$palette.primary"),
    ),
    build=_build_cta,
    capabilities=TemplateCapabilities(
        preview=True, export=True,
        editable_fields=("headline", "subtext", "button_label", "accent_color"),
        animation_properties=("opacity", "scale"),
    ),
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: All builtin templates, in the order the plan (TM-04) lists them.
BUILTIN_TEMPLATES: tuple[ExecutableTemplate, ...] = (
    TITLE_CARD, LOWER_THIRD, SECTION_DIVIDER, STEPS, PROCESS_FLOW, DATA_CHART,
    COMPARISON, QUOTE, FORMULA_PROOF, TIMELINE, CHECKLIST, TUTORIAL_CALLOUT,
    SOCIAL_CARD, PRODUCT_FEATURE, CTA,
)


def create_builtin_template_registry() -> ExecutableTemplateRegistry:
    """Registry seeded with the full first batch of 15 executable templates (TM-04)."""
    registry = ExecutableTemplateRegistry()
    for template in BUILTIN_TEMPLATES:
        registry.register(template)
    return registry


__all__ = [
    "BUILTIN_TEMPLATES",
    "CHECKLIST", "COMPARISON", "CTA", "DATA_CHART", "FORMULA_PROOF",
    "LOWER_THIRD", "PROCESS_FLOW", "PRODUCT_FEATURE", "QUOTE",
    "SECTION_DIVIDER", "SOCIAL_CARD", "STEPS", "TIMELINE", "TITLE_CARD",
    "TUTORIAL_CALLOUT",
    "create_builtin_template_registry",
]
