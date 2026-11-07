"""Display data for the Color Keyframes panel; no grade or project values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.shared.i18n.i18n import I18N

KeyKind = Literal["static", "dynamic"]
Scope = Literal["all", "color", "sizing", "matte"]
EditKind = Literal["add", "convert", "delete", "move", "dynamics", "lock",
                   "auto", "enable", "paste", "copy"]
KeyRef = tuple[str, int]


def tr(key: str, fallback: str) -> str:
    return I18N.tr(f"color_keyframes.{key}", fallback)


@dataclass(frozen=True, slots=True)
class Marker:
    frame: int
    kind: KeyKind = "static"
    start: float = 1.0
    end: float = 1.0


@dataclass(frozen=True, slots=True)
class Track:
    id: str
    label: str
    scope: Scope = "all"
    children: tuple[Track, ...] = ()
    markers: tuple[Marker, ...] = ()
    locked: bool = False
    auto: bool = False
    available: bool = True
    enabled: bool = True
    row_controls: bool = True
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class Snapshot:
    context: str = ""
    frame_count: int = 0
    timecode_fps: int = 24
    playhead: int = 0
    corrector: str = ""
    tracks: tuple[Track, ...] = ()
    edit_notice: str = ""


@dataclass(frozen=True, slots=True)
class EditRequest:
    context: str
    action: EditKind
    targets: tuple[KeyRef, ...]
    kind: KeyKind = "static"
    delta: int = 0
    start: float = 1.0
    end: float = 1.0
    enabled: bool = False
    # Source identities are carried separately for paste; the host owns values.
    sources: tuple[KeyRef, ...] = ()
    source_context: str = ""


def demo_snapshot() -> Snapshot:
    """Invented clip used to exercise gestures without opening any media."""
    domains = (
        ("linear_window", "Linear Win"), ("circle_window", "Circ Win"),
        ("polygon_window", "Polygon Win"), ("powercurve", "PowerCurve"),
        ("gradient_window", "Gradient Win"), ("correction", "Color Corrector"),
        ("qualifier", "Qualifier"), ("isolation", "Misc Isolation"),
        ("defocus", "Defocus"), ("nr", "NR"), ("effects", "Open FX"),
        ("format", "Node Format"),
    )

    def corrector(index: int) -> Track:
        prefix = f"corrector-{index}"
        return Track(prefix, tr("corrector", "Corrector {index}").format(index=index), "color", tuple(
            Track(f"{prefix}/{key}", tr(key, label), "color", markers=(
                (Marker(0), Marker(58, "dynamic"), Marker(128, "dynamic"))
                if index == 1 and key == "correction" else
                (Marker(0), Marker(86)) if index == 2 and key == "qualifier" else
                () if key == "effects" else (Marker(0),)
            )) for key, label in domains
        ))

    sizing = Track("sizing", tr("sizing", "Sizing"), "sizing", tuple(
        Track(f"sizing/{key}", tr(key, label), "sizing", markers=(
            (Marker(0), Marker(104, "dynamic")) if key == "input" else (Marker(0),)
        )) for key, label in (("input", "Input Sizing"), ("convergence", "Convergence"),
                             ("float_window", "Float Window"), ("auto_align", "Auto Align"))
    ))
    return Snapshot("demo", 168, 24, 32, "corrector-1", (corrector(1), corrector(2), sizing))
