# Audio Mixer UI（2026-11-16）

真实 PyQt6 控件与全局主题生成的独立审阅图。表计为静态示例读数，不播放音频，
不修改用户工程。轨道颜色由真实 AddTrackCommand 分配。

- `mixer.png`：默认 88px、四轨加 Master，包含空机架、超出三行的机架及末轨后的留白。
- `mixer_compact.png`：70px 紧凑模式，保留永久加号和统一分区边界。
- `mixer_faders.png`：隐藏 FX 机架，窄面板横向滚动，Master 保持固定。

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/preview_audio_mixer.py --output artifacts/audio_mixer_sections/mixer.png
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/preview_audio_mixer.py --output artifacts/audio_mixer_sections/mixer_compact.png --compact --size 360x460
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/preview_audio_mixer.py --output artifacts/audio_mixer_sections/mixer_faders.png --hide-inserts --size 320x460
```

回归覆盖全局主题下 DPR 1/2 的分隔线连接、四类右键菜单入口、机架快捷键和命令信号、
推子/Master 对齐，以及轨道颜色的新增分配、保存、重排和跨视图撤销重做。
