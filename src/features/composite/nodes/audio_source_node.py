"""Media reference for an audio-only composition layer."""

from typing import Any

from .base_node import Node, ParamDef


class AudioSourceNode(Node):
    node_type = "audio_source"
    node_title = "Audio Source"
    supports_layer_root = True
    PARAM_DEFS = [
        ParamDef("file_path", "file", ""),
        ParamDef("asset_id", "str", "", hidden=True),
        ParamDef("source_in", "int", 0),
        ParamDef("source_out", "int", -1),
    ]

    def __init__(self, node_id: str | None = None) -> None:
        super().__init__(node_id)
        self.parameters = {"file_path": "", "asset_id": "", "source_in": 0, "source_out": -1}

    def _do_evaluate(self, *args: Any, **kwargs: Any) -> None:
        return None
