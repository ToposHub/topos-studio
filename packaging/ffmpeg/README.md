# FFmpeg Topos 集成（libtopos wrapper）

让 FFmpeg 生态原生支持 Topos Video Codec（TPIC / Topos MOV）的解封装与解码。

## 后续项登记（2026-09-21，产品化计划 E11）

以下产品档已在本仓落地，wrapper 尚未承接（当前 bd>12 / GBR / CFA 输入即
`AV_PIX_FMT_NONE` 拒绝）——承接时按位流规范自描述透传，不做第二实现：

- **Topos 422 LP**（v1.8 帧间档，载体 V7-R3/em 8；解码需 GOP context 语义）
- **float(bd16) 图片档**（.toos HALF 域，bd=16 整数管线，输出格式待定）
- **TRAW / Topos RAW**（pf=3 CFA 4 相位平面；wrapper 输出相位平面、
  debayer 留给应用层）

## 这是什么

- `patches/0001-libtopos-decoder.patch`：对 FFmpeg 上游的补丁集（按 `n8.0.1` 锚点编写），内容：
  1. `libavcodec/libtoposdec.c` —— 新增解码器 `libtopos`：调用 `libtopos_codec` 的无状态
     纯函数接口 `tc_frame_decode`（spec §11.1），两段式（几何查询 → 直写 AVFrame 平面），
     输出右对齐 uint16 的 planar 10/12-bit YUV（4:2:2 / 4:4:4），色彩元数据
     （range/primaries/transfer/matrix/chroma siting/SAR，H.273 码值）逐项透传，
     坏 slice 按 §9 conceal 交付（AV_LOG_WARNING）；
  2. `codec_id.h` / `allcodecs.c` / `Makefile` / `configure` —— 注册
     `AV_CODEC_ID_LIBTOPOS` 与 `--enable-libtopos`（check_lib 方式探测，无 pkg-config 依赖）；
  3. `codec_desc.c` —— `AVCodecDescriptor` 条目。**必须插在表中按 id 升序的正确位置**
     （紧随 `AV_CODEC_ID_SMPTE_436M_ANC` 之后）：`avcodec_descriptor_get` 是 bsearch，
     位置错误会在 `ff_decode_preinit` 里 NULL 解引用（codec_descriptor->props）直接段错误；
  4. `libavformat/isom_tags.c` —— MOV 四字符 `TPIC` 映射到 `AV_CODEC_ID_LIBTOPOS`
     （Topos MOV 是标准 QuickTime 容器，解封装天然复用 `mov.c`）。
- `build_ffmpeg_topos.sh`：一键可复现构建（下载源码 → 补丁 → 前缀组装 → configure → make）。

## 音轨回归（v1.4，M-B7）

Topos 音轨（lpcm/mp4a）走 FFmpeg **通用 mov demux 路径，无 libtopos 依赖**
——预期零补丁改动（M-B0/M-B7 实测：stock ffmpeg 8.1 双轨识别 + lpcm 逐位
解码全绿）。一键回归：

```bash
bash packaging/ffmpeg/audio_regression.sh [补丁ffmpeg前缀]
```

- 生成确定性双轨产物（lpcm s16 + mp4a+elst）→ ffprobe 识别 + lpcm md5
  逐位对拍；
- 补丁构建存在时（`build_ffmpeg_topos.sh` 产物）追加 `-c:v libtopos`
  组合验证（视频走 libtopos、音频走通用 demux 的共存读回）；
- 注意：mp4a fixture 为合成包（非真实 AAC 码流），demux 路径用 `-c:a
  copy` 验证；真实码流解码由 `native/topos_codec/tests/interop/
  audio_interop_matrix.py` 互操作矩阵覆盖。

## 使用

```bash
# 依赖（macOS）
brew install nasm pkg-config

# 构建（产物在 /tmp/ffmpeg-topos/bin）
bash packaging/ffmpeg/build_ffmpeg_topos.sh

# 验证
/tmp/ffmpeg-topos/bin/ffmpeg -decoders | grep topos
/tmp/ffmpeg-topos/bin/ffprobe  xxx.topos
/tmp/ffmpeg-topos/bin/ffmpeg  -i xxx.topos -c:v prores_ks out.mov   # 转交付格式
```

## 设计说明

- **解码正确性（已验证）**：`tc_frame_decode` 是无状态纯函数，输出样本右对齐 uint16
  （10-bit 值域 0–1023），与 FFmpeg `AV_PIX_FMT_YUV422P10LE` 的内存布局逐位一致——
  wrapper 不做任何像素变换。实测：同文件经 `ffmpeg` 与官方绑定层
  （`ToposMovieFile` + `ToposDecoder`）解码，48 帧 yuv422p10le raw md5 逐位相同；
  ffprobe 元数据（bt709 / tv range / 25fps）与容器一致。
- **性能（实测 1080p）**：`ffmpeg -f null` 解码 108 fps（9.3 ms/帧）；并行度由
  `libtopos_codec` 内部 slice 线程池承担；wrapper 保持无状态，后续要吃满多核可加
  `AV_CODEC_CAP_FRAME_THREADS`。`-c:v prores_ks` 转码交付链已烟测通过。
- **范围**：当前仅解码器；编码器包装（`libtoposenc.c`，YUV422P10LE → TPIC packet）
  为二期。
- **许可**：FFmpeg 侧为 LGPL 构建，`libtopos_codec` 为动态链接（私有库）——自用无障碍；
  **对外分发前需过一遍法务**（LGPL 传播条款 + 私有库的版权归属）。

## 升级上游

补丁锚点随 FFmpeg 大版本可能漂移（codec_id 枚举尾部、configure 行号、`isom_tags.c`）。
升级流程：换 tag 下载 → `patch -p1` 看冲突 → 修锚点 → 更新本 README 的锚点版本。

## 已知非目标（与 container_spec_v1 §10 一致）

field order——`mov.c` 对其缺省行为安全。

注：音频轨与音频 trak 的 edit list（elst/priming）自容器 v1.1/v1.4 起为
正式支持项（见 `audio_regression.sh` 音轨回归段与 container_spec 附录 A），
不再是补丁侧非目标。
