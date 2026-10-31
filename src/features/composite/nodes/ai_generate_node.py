"""Placeholder AI image generation node."""

from typing import Optional
from .base_node import ParamDef, Port
from .placeholder_node import PlaceholderNode


class AIGenerateNode(PlaceholderNode):
    """AI generation placeholder."""

    node_type = "ai_generate"
    node_title = "AI Generate"
    header_color = "#9B59B6"

    PARAM_DEFS = [
        ParamDef("prompt", "str", ""),
        ParamDef("negative_prompt", "str", ""),
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=1.0),
        ParamDef("model", "str", "default"),
        ParamDef("output_width", "int", 1024, min_val=1, max_val=8192, step=1),
        ParamDef("output_height", "int", 1024, min_val=1, max_val=8192, step=1),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        # 输入端口不可省：快照按端口名校验连接，缺 inputs 会让上游连接
        # 被丢弃 → P0-3 占位直通拿不到输入 → 图尾输出 None（矩阵中唯一
        # 裸 None 行的根因）。同族 ai_inpaint/ai_expand 等均有此端口。
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self._init_port_revisions()

