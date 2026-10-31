"""
AssetDeck Pro Video Player Plugin
视频播放插件——收编全部视频播放代码：
- UltimatePlayer（Qt Multimedia，常规格式）
- ToposPlayer（libtopos_codec 原生解码，.topos）
- Topos 缩略图 / 元数据 provider
"""
import os
import sys
from pathlib import Path

# 插件包内运行时确保仓库共享层（topos_binding）可导入
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# file_explorer 根（utils / thumb_engine）——以独立方式加载插件时兜底
_FE_ROOT = str(Path(__file__).resolve().parents[2])
if _FE_ROOT not in sys.path:
    sys.path.insert(0, _FE_ROOT)

NAME = 'Video Pro'
VERSION = '2.0.0'
AUTHOR = 'AssetDeck Team'
DESCRIPTION = '视频播放引擎：Qt Multimedia 常规格式 + Topos 原生解码（含缩略图/元数据）'


def setup(context):
    """插件初始化入口。

    Qt 部件延后到主窗口就绪（插件加载早于 QApplication/MainWindow）；
    缩略图/元数据 provider 无 Qt 依赖，装载期即注册。
    """
    import utils
    import thumb_engine
    from . import topos_media

    utils.register_thumbnail_provider(['.topos'], topos_media.generate_topos_thumbnail)
    utils.register_metadata_provider(['.topos'], topos_media.get_topos_metadata)
    # [BUG-P3-54] thumb_engine.EXT_VIDEO 已含 '.topos'，冗余 add 已移除

    def on_window_ready(window):
        from .player_host import VideoProEngine
        window.attach_play_engine(VideoProEngine(window))

    # [ARCH-3] 该钩子仅独立模式会 fire（main.MainWindow.__init__ 末尾）；
    # 嵌入模式宿主有自己的浮层播放管理（FileOpsMixin.play_video_in_panel），
    # 与独立模式共用 player_host.decide_route 的同一路由决策。
    context.register_hook('mainwindow_ready', on_window_ready)
