"""P0-COL-03：AI 插件输出通道契约与消费路径回归。

修复前的缺陷链：
1. `ImageEngine._on_ai_plugin_finished` 把 RGB 推理结果转成 BGR 后，
   同一个 BGR 数组既进 CPU 缓存（磁盘契约，正确）又提交 GPU 上传
   （声明 RGB 契约）——`_do_upload_ai_plugin_result` 原样写纹理，
   AI 结果红蓝互换；
2. 项目加载恢复路径 `_restore_ai_plugin_results` 从磁盘读 BGR 后同样
   原样上传，互换复现；
3. 预览/全分辨率合成不消费 AI 纹理（见 async_renderer/image_engine
   的 pass_input 绑定点），AI 像素不进入最终画面。

本文件锁定通道契约：**磁盘 BGR / GPU RGB**，以及 AI 纹理解析 helper
的语义。渲染循环级 preview/full/export parity E2E 依赖 GL 测试分层
（P2-TEST-01），先以 seam 级确定性测试守卫边界。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.layer import Layer


# 纯红探针：AI 引擎输出端是 RGB（ai_plugin_engine 输出已做 BGR→RGB）
def _pure_red_rgb() -> np.ndarray:
    img = np.zeros((8, 8, 3), dtype=np.float32)
    img[..., 0] = 1.0
    return img


def test_ai_finished_uploads_rgb_and_caches_bgr(image_engine):
    """推理完成：GPU 上传必须收到 RGB，CPU 缓存/磁盘侧保持 BGR。"""
    from unittest.mock import MagicMock

    from src.shared.video.render_commands import RenderCommand

    result_rgb = _pure_red_rgb()
    layer = Layer("L")
    image_engine.layers = [layer]
    mock_renderer = MagicMock()
    image_engine.renderer = mock_renderer

    image_engine._on_ai_plugin_finished(layer.id, "inst-1", result_rgb)

    submitted = [
        call.args[1] for call in mock_renderer.submit.call_args_list
        if len(call.args) >= 2
        and call.args[0] == RenderCommand.UPLOAD_AI_PLUGIN_RESULT
    ]
    assert submitted, "必须提交 UPLOAD_AI_PLUGIN_RESULT 命令"
    uploaded = submitted[-1]['image_array']
    # 纯红探针：上传数组 R 通道为 1（RGB 契约）；若回归为 BGR 则 R=0
    assert uploaded[0, 0, 0] == pytest.approx(1.0), (
        "GPU 上传必须是 RGB（R=1）；检测到 BGR（通道互换回归）"
    )
    cached = image_engine.ai_plugin_result_cache[(layer.id, "inst-1")]
    assert cached[0, 0, 2] == pytest.approx(1.0), (
        "CPU 缓存/磁盘契约是 BGR（B=1）；缓存侧被误改为 RGB"
    )


def test_restore_uploads_rgb_from_bgr_disk(image_engine):
    """项目加载恢复：磁盘 BGR → 上传 RGB（转换发生在调用点）。"""
    from unittest.mock import MagicMock, patch

    from src.app.controllers.layer_controller import LayerOpsMixin
    from src.shared.io.file_io import FileIO
    from src.shared.video.render_commands import RenderCommand

    disk_bgr = np.zeros((8, 8, 3), dtype=np.float32)
    disk_bgr[..., 2] = 1.0  # 磁盘 BGR 纯红

    layer = Layer("L")
    layer.plugins = [{
        "instance_id": "inst-1", "plugin_id": "AI_Upscale_4X",
        "type": "ai", "enabled": True, "ai_result_file": "x.exr",
    }]
    image_engine.layers = [layer]
    mock_renderer = MagicMock()
    image_engine.renderer = mock_renderer

    controller = LayerOpsMixin.__new__(LayerOpsMixin)
    controller.engine = image_engine

    # AUD-2909：EXR 加载移到线程池，结果经 queued 信号回主线程写缓存并
    # 提交上传。patch 必须覆盖事件泵送阶段（worker 异步读盘发生在
    # _restore_ai_plugin_results 返回之后）；超时 5s 防挂起。
    import time as _time

    from PyQt6.QtCore import QCoreApplication

    with patch.object(FileIO, "load_ai_plugin_result",
                      return_value=disk_bgr.copy()):
        controller._restore_ai_plugin_results()
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if image_engine.ai_plugin_result_cache.get((layer.id, "inst-1")) \
                    is not None and mock_renderer.submit.called:
                break
            _time.sleep(0.01)
    QCoreApplication.processEvents()
    assert getattr(controller, "_ai_plugin_restore_worker", None) is None, (
        "恢复 worker 未在超时内完成（queued 信号未送达）")

    submitted = [
        call.args[1] for call in mock_renderer.submit.call_args_list
        if len(call.args) >= 2
        and call.args[0] == RenderCommand.UPLOAD_AI_PLUGIN_RESULT
    ]
    assert submitted, "恢复路径必须提交 UPLOAD_AI_PLUGIN_RESULT"
    uploaded = submitted[-1]['image_array']
    assert uploaded[0, 0, 0] == pytest.approx(1.0), (
        "恢复上传必须是 RGB（R=1）；磁盘 BGR 被原样上传（通道互换回归）"
    )
    cached = image_engine.ai_plugin_result_cache[(layer.id, "inst-1")]
    assert cached[0, 0, 2] == pytest.approx(1.0), "CPU 缓存必须保持磁盘 BGR"


def test_get_layer_ai_texture_resolution(image_engine):
    """AI 纹理解析：最后一个启用的 AI 插件优先；禁用/无 renderer 返回 None。"""
    from unittest.mock import MagicMock

    layer = Layer("L")
    layer.plugins = [
        {"instance_id": "a", "type": "ai", "enabled": True},
        {"instance_id": "b", "type": "ai", "enabled": False},
        {"instance_id": "c", "type": "ai", "enabled": True},
        {"instance_id": "d", "type": "dctl", "enabled": True},
    ]
    tex_a, tex_c = object(), object()
    mock_renderer = MagicMock()
    mock_renderer.ai_plugin_textures = {
        (layer.id, "a"): tex_a,
        (layer.id, "c"): tex_c,
    }
    image_engine.renderer = mock_renderer

    assert image_engine._get_layer_ai_texture(layer) is tex_c, (
        "应取最后一个启用的 AI 插件纹理"
    )

    layer.plugins[2]["enabled"] = False
    assert image_engine._get_layer_ai_texture(layer) is tex_a

    layer.plugins[0]["enabled"] = False
    assert image_engine._get_layer_ai_texture(layer) is None

    image_engine.renderer = None
    layer.plugins[0]["enabled"] = True
    assert image_engine._get_layer_ai_texture(layer) is None
