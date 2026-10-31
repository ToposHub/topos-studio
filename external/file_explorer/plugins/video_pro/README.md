# Video Pro — 视频播放插件

file_explorer 的全部视频播放能力（原 `player.py` / `topos_player.py` 已整体迁入）。

## 组成

| 模块 | 职责 |
|---|---|
| `player_qt.py` | UltimatePlayer——Qt Multimedia（自带 FFmpeg 后端），常规格式（mp4/mov/mkv/mxf…），含截图、片段截取、播放失败回退 |
| `topos_player.py` | ToposPlayer——`.topos` 原生解码播放器：libtopos_codec 绑定层直解（O(1) 索引 seek + 多线程切片解码）+ swscale 转色 + 背压节速 + 按序显示 |
| `player_host.py` | VideoProEngine——引擎宿主，实现 MainWindow 接缝协议（can_play/play/resize/snapshot/shutdown），路由 `.topos` → ToposPlayer、其余 → UltimatePlayer |
| `topos_media.py` | 缩略图（首帧 WEBP）与元数据（Codec/分辨率/帧率/位深/色彩信息/码率），无 Qt 依赖 |

## 接缝协议（core 侧的四个通用扩展点）

1. `plugin_manager.fire_event('mainwindow_ready', window=...)`——MainWindow 建好后触发；插件此时创建 Qt 部件并 `window.attach_play_engine(engine)`
2. `MainWindow.start_player` 逐引擎询问 `can_play(path)` → `play(path)`
3. `utils.register_thumbnail_provider(exts, fn)` —— 缩略图 provider
4. `utils.register_metadata_provider(exts, fn)` —— 元数据 provider

## 独立播放器路线

`topos_player.py` 仅依赖 stdlib + numpy + PyQt6 + PyAV + libtopos_codec（仓库
`native/topos_codec` 构建），不 import file_explorer 任何模块——抽出独立
桌面播放器只需一个新的入口壳。
