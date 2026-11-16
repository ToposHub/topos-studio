"""GPU 预览帧：YUV 平面 + H.273 色彩元数据。

播放器解码线程产出 :class:`YuvFrame`，GUI 线程的
:class:`~src.shared.playback.gl_video_canvas.GLVideoCanvas` 直接上传
Y/U/V 平面到 GPU，由 shader 完成 YUV→RGB、limited/full range 展开和
BT.601/709/2020 矩阵转换。这样 10/12-bit 源不会在 CPU 端先被压成
8-bit RGB，预览链只剩一次 GPU 采样。

平面约定
--------
- 数组 dtype 与源码值一致：8-bit 为 ``uint8``；10/12/16-bit 为
  ``uint16``（未左移，仍是 0..(2^depth-1) 的码值）。
- ``subsampling`` 为 ``'420'`` / ``'422'`` / ``'444'``；U/V 的宽高必须
  与上传器的约定一致（420: 宽高各半；422: 宽半、高同；444: 同尺寸）。
- ``matrix`` / ``color_range`` / ``chroma_siting`` 使用 H.273 原值；
  ``color_range`` 0=limited，1=full。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class YuvFrame:
    """一帧可上传 GPU 的 YUV 平面。

    ``y`` / ``u`` / ``v`` 必须是本帧独立持有的数组（不能引用解码器
    复用的平面缓冲），否则环形缓冲中的旧帧会被下一次解码覆盖。
    """

    y: Any
    u: Any
    v: Any
    bit_depth: int = 8
    subsampling: str = '420'
    matrix: int = 1          # H.273 matrix_coefficients，默认 BT.709
    color_range: int = 0     # 0=limited, 1=full
    chroma_siting: int = 1   # H.273 chroma_sample_loc_type
    pixel_format: str = ''   # '422' / '444' / '420' / 'gbr'
    source_width: int = 0
    source_height: int = 0

    @property
    def width(self) -> int:
        return int(self.y.shape[1])

    @property
    def height(self) -> int:
        return int(self.y.shape[0])

    def is_valid(self) -> bool:
        """平面尺寸是否符合 subsampling 约定。"""
        yh, yw = self.y.shape[:2]
        uh, uw = self.u.shape[:2]
        vh, vw = self.v.shape[:2]
        if (uh, uw) != (vh, vw):
            return False
        if self.subsampling == '420':
            return uh == (yh + 1) // 2 and uw == (yw + 1) // 2
        if self.subsampling == '422':
            return uh == yh and uw == (yw + 1) // 2
        if self.subsampling == '444':
            return (uh, uw) == (yh, yw)
        return False
