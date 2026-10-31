# ADR-I020：外部软件生态适配路线

## 状态

O8 当前交付：OIIO 适配器路径修复、无 SDK 语法门禁，以及可运行的 FFmpeg
外部进程 bridge 往返（2026-09-08）。OIIO 真 SDK/宿主往返和 FFmpeg 原生
demuxer/encoder 仍待对应 SDK/宿主环境。

## 已实现

- `plugins/oiio_topos/topos_imageio.cpp` 提供 OIIO `ImageInput`/`ImageOutput`；
  只使用公开 Topos C ABI，不复制 TPIC、色彩或 Alpha 逻辑；
- v1 OIIO 路径支持 scanline RGB(A) 的 GBR/444 读取与 GBR 写入，10/12-bit
  通过显式 uint16 容器换算；4:2:2 Preview 因色度半宽而明确拒读；
- `plugins/oiio_topos/syntax_check.sh` 使用 OIIO 2.x stub 头做持续
  `-fsyntax-only -Wall -Wextra -Wpedantic` 检查；
- CMake `TOPOS_BUILD_OIIO_PLUGIN=ON` 现在指向仓库真实插件路径。
- `scripts/topos_ffmpeg_bridge.py` 提供 GBR 4:4:4 10-bit 无 alpha 的
  `PNG/EXR ↔ FFmpeg rawvideo ↔ Topos C ABI ↔ .toos` 单帧往返；测试固定走
  外部 `ffmpeg`/`ffprobe` 进程，Topos 编解码仍只有一套 C ABI。

## 未宣称能力

OIIO 插件不会自动让 Blender、Maya、Nuke、Photoshop、After Effects、Resolve
或 Premiere 支持 `.toos`。这些宿主需要按各自 SDK、ABI 和授权单独交付；当前
仓库没有把它们标成 supported。FFmpeg bridge 也不等于原生 FFmpeg codec：
它只覆盖明确的 GBR10 单帧交换；当前 Topos MOV 仍以自有 demux/mux 和
Python/native 路径为准，不能因为容器是 MOV 就宣称 ffmpeg codec 原生支持。

## 验收顺序

1. 无 SDK：运行 `./plugins/oiio_topos/syntax_check.sh`；
2. FFmpeg bridge：运行 `python3 -m pytest -q tests/media/test_topos_ffmpeg_bridge.py`，
   记录 FFmpeg/ffprobe 版本和 GBR10 往返结果；
3. 有 OIIO SDK：用 `TOPOS_BUILD_OIIO_PLUGIN=ON` 构建插件，记录 OIIO 版本、OS、
   架构和 plugin search path；
4. 用 `oiiotool --info`、`.toos → EXR`、`EXR → .toos` 做 GBR/Alpha/metadata
   往返，并对 golden 做像素/位深比较；
5. FFmpeg 原生插件、Resolve、Premiere、Nuke 只有在真实宿主完成导入/导出/重开/颜色和
   Alpha 检查后才进入 capability matrix。
