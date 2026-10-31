"""
Video Pro 播放引擎宿主。

统一管理三套播放引擎并实现与 MainWindow 的接缝协议：
- can_play(path) / play(path)     —— 播放路由（main.start_player 逐引擎询问）
- resize(w, h)                    —— 跟随主窗口尺寸
- snapshot() -> bool              —— 快照（返回是否已处理）
- shutdown()                      —— 主窗口关闭时停解码线程

路由规则：.topos → ToposPlayer（原生解码）；ProRes/DNxHR 等 mezzanine →
LibavPlayer（PyAV 多线程解码）；其余视频 → UltimatePlayer（Qt Multimedia）。
跨引擎互斥：播放一个时停掉并隐藏其他。
"""
from __future__ import annotations

import os

TOPOS_EXT = '.topos'


def is_topos_content(path: str) -> bool:
    """按内容判定：.topos 扩展名，或容器里的 TPIC 码流。

    官方样本 2K_topos.mov 等是 .mov 壳的 Topos 文件——按扩展名路由
    会把它们错送 QMediaPlayer（解不了 TPIC）→ 触发回退系统播放器。
    """
    if path.lower().endswith(TOPOS_EXT):
        return True
    try:
        from src.shared.media.topos_source import is_topos_movie
        return is_topos_movie(path)
    except Exception:
        return False


def decide_route(path: str) -> str:
    """[ARCH-3] 唯一的播放路由决策（独立模式 VideoProEngine 与嵌入模式
    FileOpsMixin.play_video_in_panel 共用，消除双实现复制漂移）。

    返回 'topos' | 'libav' | 'qt'：
    - topos：.topos / TPIC 码流 → ToposPlayer（原生解码）
    - libav：ProRes/DNxHR 等 mezzanine → LibavPlayer（PyAV 多线程解码）
    - qt：  其余视频 → UltimatePlayer（Qt Multimedia）
    """
    if is_topos_content(path):
        return 'topos'
    from .libav_player import is_mezzanine
    return 'libav' if is_mezzanine(path) else 'qt'


def stop_other_players(target, players) -> None:
    """跨引擎互斥：停掉并隐藏 players 中除 target 外的所有播放器。"""
    for p in players:
        if p is not None and p is not target:
            p.stop()
            p.hide()


class VideoProEngine:
    def __init__(self, window):
        self.window = window
        from .libav_player import LibavPlayer
        from .player_qt import UltimatePlayer
        from .topos_player import ToposPlayer

        self.qt_player = UltimatePlayer(window)
        self.qt_player.hide()
        self.topos_player = ToposPlayer(window)
        self.topos_player.hide()
        self.libav_player = LibavPlayer(window)
        self.libav_player.hide()

        # 上/下一个文件（原 main.py 的接线原样迁入）
        nav_prev = lambda: window.trigger_js("navigateFile(-1)")
        nav_next = lambda: window.trigger_js("navigateFile(1)")
        self.qt_player.request_prev.connect(nav_prev)
        self.qt_player.request_next.connect(nav_next)
        self.topos_player.request_prev.connect(nav_prev)
        self.topos_player.request_next.connect(nav_next)
        self.libav_player.request_prev.connect(nav_prev)
        self.libav_player.request_next.connect(nav_next)
        # 解码失败 → 前端可见 toast（不静默转交系统播放器）
        self.topos_player.play_failed.connect(self._toast)
        self.libav_player.play_failed.connect(self._toast)

    # ---- MainWindow 接缝协议 ----

    def can_play(self, path: str) -> bool:
        return path.lower().endswith((TOPOS_EXT, '.mp4', '.mov', '.m4v', '.avi',
                                      '.mkv', '.webm', '.flv', '.wmv', '.3gp',
                                      '.mxf', '.ts', '.mts', '.m2ts', '.ogv',
                                      '.qt', '.divx', '.f4v'))

    def play(self, path: str) -> None:
        # [ARCH-3] 路由决策走模块级 decide_route（与嵌入模式共用）
        target = {
            'topos': self.topos_player,
            'libav': self.libav_player,
            'qt': self.qt_player,
        }[decide_route(path)]
        stop_other_players(target, (self.topos_player, self.libav_player, self.qt_player))
        target.setGeometry(0, 0, self.window.width(), self.window.height())
        target.play_video(path)

    def resize(self, w: int, h: int) -> None:
        for p in (self.qt_player, self.topos_player, self.libav_player):
            if not p.isHidden():
                p.setGeometry(0, 0, w, h)

    def snapshot(self) -> bool:
        for p in (self.topos_player, self.libav_player, self.qt_player):
            if p.isVisible():
                p.snapshot()
                return True
        return False

    def shutdown(self) -> None:
        self.topos_player.stop_decoder()
        self.libav_player.stop_decoder()
        self.qt_player.stop()

    # ---- 内部 ----

    def _toast(self, msg: str) -> None:
        safe = str(msg).replace("'", '').replace('"', '')
        self.window.trigger_js(f"if (window.showToast) window.showToast('{safe}', 'error');")
