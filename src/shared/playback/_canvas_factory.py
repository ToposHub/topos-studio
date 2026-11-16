"""播放器画布工厂：优先 GPU YUV 画布，失败回退普通 QWidget 画布。"""
from __future__ import annotations

import logging

from .base_player import _VideoCanvas

logger = logging.getLogger(__name__)


def make_canvas(parent):
    """返回 ``GLVideoCanvas``；导入或创建失败时返回 ``_VideoCanvas``。"""
    try:
        from .gl_video_canvas import GLVideoCanvas
        return GLVideoCanvas(parent)
    except Exception:
        logger.warning('[PLAYBACK] GPU canvas unavailable, using RGB canvas',
                       exc_info=True)
        return _VideoCanvas(parent)
