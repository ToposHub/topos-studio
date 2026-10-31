"""Native Tangent Hub integration primitives."""

from .hub_client import TangentHubClient
from .protocol import (
    FromHubCommand,
    PanelInfo,
    PanelType,
    ToHubCommand,
    TangentCommand,
    decode_message,
    encode_all_change,
    encode_application_definition,
    encode_parameter_value,
    frame_message,
)
from .settings import (
    CONTROL_SURFACE_AUTO,
    CONTROL_SURFACE_CONFIG_KEY,
    CONTROL_SURFACE_DISABLED,
    CONTROL_SURFACE_ELEMENT,
    CONTROL_SURFACE_RIPPLE,
    DEFAULT_CONTROL_SURFACE,
    normalize_control_surface,
)

__all__ = [
    "FromHubCommand",
    "PanelInfo",
    "PanelType",
    "TangentCommand",
    "TangentHubClient",
    "ToHubCommand",
    "CONTROL_SURFACE_AUTO",
    "CONTROL_SURFACE_CONFIG_KEY",
    "CONTROL_SURFACE_DISABLED",
    "CONTROL_SURFACE_ELEMENT",
    "CONTROL_SURFACE_RIPPLE",
    "DEFAULT_CONTROL_SURFACE",
    "decode_message",
    "encode_all_change",
    "encode_application_definition",
    "encode_parameter_value",
    "frame_message",
    "normalize_control_surface",
]
