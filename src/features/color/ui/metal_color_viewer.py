"""阶段 4.4/4.5 —— Color Viewer 的 Metal 原生呈现端（默认关闭，显式开启）。

组成（GUI 线程 / 渲染线程边界与 Edit 域宿主相同）：

- :func:`color_metal_viewer_enabled`：**显式开启才生效**——
  ``TOPOS_COLOR_METAL=1`` 且未被统一回退开关 ``TOPOS_METAL_DISABLE``
  关闭（能力名复用 ``MetalFeature.COLOR_GRAPH_TEXTURE``，即
  ``TOPOS_METAL_DISABLE=color_graph_metal`` 一并关掉本视口）。未开启时
  ``gl_viewport.py`` 的 GL 呈现路径完全不变。
- :class:`ColorMetalViewportHost`：复用（不改）Edit 域
  ``MetalViewportHost`` 的窗口侧——本子类只在其上叠加 **EDR/HDR 显示
  端配置**：像素格式升为 RGBA16Float、
  ``wantsExtendedDynamicRangeContent``、EDR 上限与色彩空间；屏幕切换
  （``ScreenChangeInternal`` / ``DevicePixelRatioChange``）时按新屏幕的
  EDR 能力**重新配置显示端**（计划 4.4 验收：EDR 能力随屏幕变更重配）。
- :class:`EdrLayerDecision`：EDR 决策纯逻辑（可离线测试）——EDR 能力
  不足时明确回到 SDR BGRA8 + sRGB 色彩空间，不冒充。
- :class:`ColorMetalViewerPresenter`：渲染线程侧呈现端口
  （``MetalPresentPort`` + ``CAMetalLayerSink`` + ``MetalPresentPass``，
  全部共享只读复用）；消费 :class:`ColorFrameMetalPublisher` 发布的
  native ``GpuPresentFrame``，槽位 ack 经 ``on_released`` 回到发布者。
- :func:`fit_display_rect`：图幅在 drawable 内的适配矩形（缩放/平移），
  与 GL 路径同一「等比适配 + zoom/pan」语义（top-left 原点像素坐标，
  供 ``normalized_rect`` 转换）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QWidget

from src.features.edit.ui.metal_viewport_host import MetalViewportHost
from src.shared.gpu.metal.present_port import (
    CAMetalLayerSink,
    MetalPresentPass,
    MetalPresentPort,
    PresentOutcome,
    normalized_rect,
)
from src.shared.gpu.present_frame import GpuPresentFrame

logger = logging.getLogger(__name__)

#: MSL/Metal 像素格式常量（避免在本模块 import Metal——offscreen 安全）
MTL_PIXEL_FORMAT_BGRA8_UNORM = 80
MTL_PIXEL_FORMAT_RGBA16_FLOAT = 115

#: SDR 背景与 GL 视口一致（Color chrome 的 #191919）
VIEWER_BACKGROUND = (25.0 / 255.0, 25.0 / 255.0, 25.0 / 255.0, 1.0)


def color_metal_viewer_enabled() -> Tuple[bool, str]:
    """``(enabled, reason)``：Color 视口 Metal 原生呈现是否开启。

    开启 = ``TOPOS_COLOR_METAL`` 为真值 **且** 统一 kill switch 未关
    ``color_graph_metal`` 能力。``reason`` 在关闭时给出稳定原因键（诊断
    可见，不静默降级）。
    """
    from src.shared.gpu.metal.feature_switches import (
        MetalFeature,
        feature_disabled,
    )

    flag = os.environ.get("TOPOS_COLOR_METAL", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False, "opt_in_missing"
    if feature_disabled(MetalFeature.COLOR_GRAPH_TEXTURE):
        return False, "disabled_by_switch:color_graph_metal"
    return True, ""


# ── EDR 决策（纯逻辑） ───────────────────────────────────────────────────

@dataclass(frozen=True)
class EdrLayerDecision:
    """显示端层配置决策（像素格式 / EDR 开关 / 上限 / 色彩空间）。"""

    pixel_format: int
    wants_edr: bool
    max_edr: float
    colorspace: str          # extended_linear_srgb / srgb

    @property
    def is_float(self) -> bool:
        """层是否为 float 容器（HDR/EDR present 的前提）。"""
        return self.pixel_format == MTL_PIXEL_FORMAT_RGBA16_FLOAT


def decide_edr_layer(screen_edr: float, hdr_desired: bool) -> EdrLayerDecision:
    """屏幕 EDR 能力 × 是否需要 HDR → 层配置。

    ``screen_edr`` 为屏幕的
    ``maximumExtendedDynamicRangeColorComponentValue``（1.0 = 无 EDR）。
    EDR 能力不足（≤1.0）时明确回 SDR BGRA8/sRGB（呈现的内容已是
    ``hdr_sdr_tonemap`` 的 SDR 码值——展示输出描述保证）。
    """
    capability = float(screen_edr)
    if hdr_desired and capability > 1.0:
        return EdrLayerDecision(
            pixel_format=MTL_PIXEL_FORMAT_RGBA16_FLOAT,
            wants_edr=True,
            max_edr=capability,
            colorspace="extended_linear_srgb")
    return EdrLayerDecision(
        pixel_format=MTL_PIXEL_FORMAT_BGRA8_UNORM,
        wants_edr=False,
        max_edr=1.0,
        colorspace="srgb")


def fit_display_rect(
    image_size: Tuple[int, int],
    drawable_size: Tuple[int, int],
    *,
    zoom: float = 1.0,
    pan: Tuple[float, float] = (0.0, 0.0),
) -> Tuple[float, float, float, float]:
    """图像在 drawable 内的呈现矩形（top-left 原点像素，``(x,y,w,h)``）。

    等比适配（contain）后乘 ``zoom``，再按 ``pan``（归一化偏移，单位为
    适配后图幅）平移；结果不裁剪（present shader 负责背景填充）。
    """
    image_w, image_h = float(image_size[0]), float(image_size[1])
    surface_w = float(max(1, int(drawable_size[0])))
    surface_h = float(max(1, int(drawable_size[1])))
    if image_w <= 0.0 or image_h <= 0.0:
        return (0.0, 0.0, surface_w, surface_h)
    scale = min(surface_w / image_w, surface_h / image_h)
    width = image_w * scale * float(zoom)
    height = image_h * scale * float(zoom)
    x = (surface_w - width) * 0.5 + pan[0] * width
    y = (surface_h - height) * 0.5 + pan[1] * height
    return (x, y, width, height)


# ── 宿主（GUI 线程） ─────────────────────────────────────────────────────

class ColorMetalViewportHost(MetalViewportHost):
    """Color 视口宿主：Edit 宿主 + EDR/HDR 显示端配置。

    GUI 线程独占；``configure_display`` 在 attach 后与屏幕变更时调用
    （重配像素格式/EDR/色彩空间——``CAMetalLayer`` 允许在取 drawable
    之前重配；呈现端口按层的当前像素格式重建管线）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._edr_decision: Optional[EdrLayerDecision] = None

    @property
    def edr_decision(self) -> Optional[EdrLayerDecision]:
        return self._edr_decision

    def screen_edr_capability(self) -> float:
        """本 widget 所在屏幕的 EDR 能力（1.0 = 无 EDR / 非 cocoa）。"""
        try:
            import Quartz
            window = self.window().windowHandle() if self.window() else None
            screen = window.screen() if window is not None else None
            if screen is None:
                screen = Quartz.NSScreen.mainScreen()
            if screen is None:
                return 1.0
            value = getattr(
                screen, "maximumExtendedDynamicRangeColorComponentValue",
                None)
            return float(value()) if callable(value) else 1.0
        except Exception:  # noqa: BLE001 —— 能力探测失败按无 EDR
            logger.debug("EDR capability probe failed", exc_info=True)
            return 1.0

    def configure_display(self, hdr_desired: bool) -> Optional[EdrLayerDecision]:
        """按屏幕 EDR 能力配置层（attach 后调用；屏幕变更时重调）。"""
        if self._layer is None:
            return None
        decision = decide_edr_layer(self.screen_edr_capability(), hdr_desired)
        try:
            import Quartz
            self._layer.setPixelFormat_(decision.pixel_format)
            if decision.wants_edr:
                setter = getattr(
                    self._layer, "setWantsExtendedDynamicRangeContent_", None)
                if callable(setter):
                    setter(True)
                max_setter = getattr(
                    self._layer,
                    "setMaximumExtendedDynamicRangeColorComponentValue_",
                    None)
                if callable(max_setter):
                    max_setter(decision.max_edr)
                space_name = {
                    "extended_linear_srgb":
                        "kCGColorSpaceExtendedLinearSRGB",
                    "srgb": "kCGColorSpaceSRGB",
                }[decision.colorspace]
                space = Quartz.CGColorSpaceCreateWithName(
                    getattr(Quartz, space_name, None)
                    or Quartz.kCGColorSpaceSRGB)
                if space is not None:
                    self._layer.setColorspace_(space)
            else:
                setter = getattr(
                    self._layer, "setWantsExtendedDynamicRangeContent_", None)
                if callable(setter):
                    setter(False)
                space = Quartz.CGColorSpaceCreateWithName(
                    Quartz.kCGColorSpaceSRGB)
                if space is not None:
                    self._layer.setColorspace_(space)
        except Exception:  # noqa: BLE001 —— 配置失败保持 SDR，可见诊断
            logger.warning("ColorMetalViewportHost: EDR 配置失败",
                           exc_info=True)
            decision = decide_edr_layer(1.0, False)
        if self._edr_decision != decision:
            logger.info(
                "ColorMetalViewportHost: 显示端配置 %s（EDR %s, max %.2f）",
                decision.colorspace, decision.wants_edr, decision.max_edr)
        self._edr_decision = decision
        return decision

    def event(self, event: Any) -> bool:
        # 屏幕切换 / 缩放变更 = 新 surface 代次（宿主已同步）；EDR 能力
        # 属于屏幕属性，随屏幕变更**重新配置显示端**（4.4 验收）。
        try:
            if event.type() in self._SURFACE_EVENT_TYPES \
                    and self._layer is not None \
                    and self._edr_decision is not None:
                self.configure_display(self._edr_decision.wants_edr)
        except Exception:  # noqa: BLE001 —— PyQt 虚函数不得抛出
            logger.debug("ColorMetalViewportHost: 屏幕变更 EDR 重配失败",
                         exc_info=True)
        return super().event(event)


# ── 呈现端口驱动（渲染线程） ─────────────────────────────────────────────

class ColorMetalViewerPresenter:
    """把 native 帧送进 Color 视口的 CAMetalLayer（渲染线程独占）。

    生命周期：``attach_layer``（GUI 线程已建层后，渲染线程接线）→
    ``submit``（任意线程，只入队）→ ``poll``（渲染线程：取 drawable、
    编码、提交；完成帧经回调归还槽位）→ ``shutdown``。
    """

    def __init__(self, metal_ctx: Any, host: ColorMetalViewportHost) -> None:
        self._ctx = metal_ctx
        self._host = host
        self._port: Optional[MetalPresentPort] = None
        self._pixel_format = 0
        self.stats: dict = {"submits": 0, "attached": 0, "reattached": 0}

    def attach_layer(self) -> bool:
        """按宿主层的当前像素格式建呈现端口（幂等；格式变化重建）。"""
        layer = self._host.layer
        if layer is None:
            return False
        pixel_format = int(layer.pixelFormat())
        if self._port is not None and pixel_format == self._pixel_format:
            return True
        presenter = MetalPresentPass(self._ctx, pixel_format)
        self._port = MetalPresentPort(
            CAMetalLayerSink(layer, pixel_format),
            presenter=presenter,
            publisher=self._host.publisher,
        )
        self._pixel_format = pixel_format
        self.stats["attached"] += 1
        if self._port is not None and self.stats["attached"] > 1:
            self.stats["reattached"] += 1
        return True

    @property
    def port(self) -> Optional[MetalPresentPort]:
        return self._port

    def submit(
        self,
        published: Any,
        *,
        image_size: Tuple[int, int],
        zoom: float = 1.0,
        pan: Tuple[float, float] = (0.0, 0.0),
        on_presented: Any = None,
        on_released: Any = None,
    ) -> bool:
        """入队一帧（newest-wins；矩形按宿主当前 drawable 计算）。"""
        if self._port is None and not self.attach_layer():
            return False
        assert self._port is not None
        surface = self._host.publisher.current()
        rect_px = fit_display_rect(
            image_size, surface.drawable_size, zoom=zoom, pan=pan)
        self.stats["submits"] += 1
        return self._port.submit(
            published.present, rect=normalized_rect(rect_px, surface.drawable_size),
            display_rect_px=rect_px,
            on_presented=on_presented,
            on_released=on_released,
        )

    def represent(self, image_size: Tuple[int, int], *,
                  zoom: float = 1.0, pan: Tuple[float, float] = (0.0, 0.0),
                  ) -> bool:
        """缩放/平移时重显在屏帧（无新渲染；无租约）。"""
        if self._port is None:
            return False
        surface = self._host.publisher.current()
        rect_px = fit_display_rect(
            image_size, surface.drawable_size, zoom=zoom, pan=pan)
        return self._port.represent_displayed(
            normalized_rect(rect_px, surface.drawable_size))

    def poll(self) -> List[PresentOutcome]:
        if self._port is None:
            return []
        return self._port.poll()

    def shutdown(self) -> List[PresentOutcome]:
        if self._port is None:
            return []
        outcomes = self._port.shutdown()
        self._port = None
        return outcomes


__all__ = [
    "ColorMetalViewportHost",
    "ColorMetalViewerPresenter",
    "EdrLayerDecision",
    "VIEWER_BACKGROUND",
    "color_metal_viewer_enabled",
    "decide_edr_layer",
    "fit_display_rect",
]
