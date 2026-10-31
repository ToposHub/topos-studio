# TOPOS 首轮本地先导测量证据

这些 JSON 保存逐帧或逐相位原始数字、输入/产物 SHA、实际加载库 SHA 和命令；大型中间 YUV/MOV 文件不入库。此处不是发布报告：10-bit 配对源来自有损 ProRes XQ 解码，仅 2K 的前 30 帧，参照编码器是 FFmpeg `prores_ks`；RAW/half 是 128×128 合成探针。具体局限和待办见 `../../TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md` 第 7 节。

## 复现 10-bit 422 同源输入

在项目根目录运行；素材 `tests/test_videos/2K_prores4444XQ.mov` 的 SHA-256 必须是 `0880187374eeb2c61deaa64cb350398da4db88a31fd97e13f7a9992394565516`。

```bash
.venv/bin/python tools/extract_topos_planar_reference.py \
  --source-video tests/test_videos/2K_prores4444XQ.mov \
  --out bench_out/topos_paired_q01_real_source/source_422p10le_30f.yuv \
  --manifest bench_out/topos_paired_q01_real_source/source_422p10le_30f.json \
  --pix-fmt yuv422p10le --frames 30
shasum -a 256 bench_out/topos_paired_q01_real_source/source_422p10le_30f.yuv
```

期望原始平面 SHA-256：`506862a3897c379bd8609b3df92fe850796e9f647355cda3b1151daf9c2c3e2a`。再分别运行：

```bash
for tier in proxy lt standard hq; do
  .venv/bin/python tools/measure_topos_paired_quality.py \
    --source-raw bench_out/topos_paired_q01_real_source/source_422p10le_30f.yuv \
    --source-origin decoded-lossy-source \
    --source-parent tests/test_videos/2K_prores4444XQ.mov \
    --tier "$tier" --width 1920 --height 1080 --frames 30 --fps 25 \
    --threads 4 --match-prores-bitrate \
    --out-dir "bench_out/topos_paired_q01_real_source/${tier}-match"
done
```

该工具拒绝已有输出目录内同名结果，重测时指定新的目录。JSON 中的 wall-clock 时间和库 SHA 只适用于当时构建；重新编译后数值可变化，应记录新构建指纹。`rate_matched_2pct=true` 是该次产物的实际视频载荷匹配，不含 MOV 容器/音频。

## 12-bit 4444/XQ 参照链审计样本

`prores_ks` 不接受原生 12-bit 444 输入，不能用于此项直比。本机 VideoToolbox 编码器接受 `p416le`；测量工具会先把 12-bit planar 显式转换到 p416，再把整个中间件转回 12-bit，对完整 SHA 做逐字节一致性校验，最后确认 ProRes 解码是 `yuva444p12le`。本机需 `--allow-sw` 才能建立编码会话，所以硬件加速状态**未确认**。

先提取 2K、前 30 帧源：

```bash
.venv/bin/python tools/extract_topos_planar_reference.py \
  --source-video tests/test_videos/2K_prores4444XQ.mov \
  --out bench_out/topos_paired_q01_real_source/source_444p12le_30f.yuv \
  --manifest bench_out/topos_paired_q01_real_source/source_444p12le_30f.json \
  --pix-fmt yuv444p12le --frames 30
for tier in 4444 4444xq; do
  .venv/bin/python tools/measure_topos_paired_quality.py \
    --source-raw bench_out/topos_paired_q01_real_source/source_444p12le_30f.yuv \
    --source-origin decoded-lossy-source \
    --source-parent tests/test_videos/2K_prores4444XQ.mov \
    --tier "$tier" --width 1920 --height 1080 --frames 30 --fps 25 \
    --threads 4 --prores-encoder prores_videotoolbox --allow-sw \
    --match-prores-bitrate \
    --out-dir "bench_out/topos_paired_q01_real_source/${tier}-vt-12bit-match"
done
```

期望原始 12-bit 平面 SHA-256：`39b74b0580c8f083f33fc8a8e39bf4404344581ae7969b942520745d4b9c9489`。**这两份报告状态是 `reference_chain_audit_required_no_release_verdict`。** 虽然输入 p416 转换逐字节往返一致且视频载荷匹配，参照解码的 Y PSNR 仅约 37 dB；不能据这些巨大差值声称 TOPOS 画质胜出。

已用 `tools/verify_prores_vt_decode.py` 将 XQ 产物前 3 帧交给 AVFoundation 独立解码为 16-bit bi-planar 高 12 位，并与 FFmpeg 的 `yuva444p12le` 前三平面比较。Y/U/V 的平均码值差约 0.315/0.180/0.193，最大差为 3/2/2；结果存 `independent_decode_4444xq_3f.json`。这只排除了“大偏差单独来自 FFmpeg 解码器”的假设；编码器的 p416 色彩范围、质量选项与独立无损多场景素材仍须核查。

补做 `tools/probe_prores_vt_12bit_ramp.py --out-dir bench_out/topos_vt_12bit_ramp_probe` 的 128×128、2 帧平滑灰阶探针，`vt_12bit_ramp.json` 中 Y 最大偏差 1 个 12-bit 码值，U/V 完全一致。这表明本机 p416→ProRes→12-bit 平面的简单灰阶映射没有系统性量程偏移；它不解释复杂源的约 37 dB，也不验证颜色/高频/质量选项。

## RAW/half 原生域探针

本目录带有原始小样本：`raw12.raw`、`raw16.raw` 和 `float16-finite.raw`。它们按 `tools/measure_topos_native_domains.py` 的输入契约存放四相位 CFA 或三通道 half 位模式；运行时工具会核对长度并记录完整 SHA。具体命令：

```bash
for kind in raw12 raw16; do
  .venv/bin/python tools/measure_topos_native_domains.py \
    --kind "$kind" --source "docs/codec/evidence/TOPOS_2026-09-27_local_pilot/$kind.raw" \
    --width 128 --height 128 --out-dir "bench_out/topos_native_domain_rerun/$kind"
done
.venv/bin/python tools/measure_topos_native_domains.py \
  --kind float16 \
  --source docs/codec/evidence/TOPOS_2026-09-27_local_pilot/float16-finite.raw \
  --width 128 --height 128 --out-dir bench_out/topos_native_domain_rerun/float16
```

`float-*` 是有损档，发现 Inf/NaN 类别改变后，产品编码入口和 CLI 现会在写盘前拒绝这些特殊值。本目录仅保存有限值正向探针；负向测试见 `tests/native/test_topos_float_tier_nonfinite.py` 与 `tests/media/test_topos_half_float.py`。显式 HALF/固定 QP 路径不属于这四个有损预设。
浮点探针源含一个负零；更新后的 `native_domains_float16-finite.json` 还记录源零变非零及负零变非零：ultra 将它变正零，high/medium/low 将它变非零，故四档不承诺负零 bit-exact。

## 4K XQ 性能分段先导基线

`performance_4k_xq_thread_sweep_5rounds.json` 保留了 1/8/16 线程各 5 轮、交错顺序的原始 `bench_encode_stages.py` JSON 与每帧耗时。每次命令如下，线程按报告的 `order` 运行：

```bash
.venv/bin/python tools/bench_encode_stages.py \
  --input tests/test_videos/4K_prores4444XQ.mov \
  --frames 16 --warmup 4 --threads 16 --profile 4444xq --allow-dirty
```

本次工作树因新工具和文档编辑而标脏，报告状态明确为 `dirty_worktree_pilot_not_release_performance`。计时范围从预载 planar 帧进入 `ToposVideoEncoder` 到 mux，排除源解码、GPU 和应用调度。进程 RSS 包含素材预载，不等同应用导出峰值。此数据不构成“4K25 实时”或与 ProRes 速度优劣的发布证据。

`performance_4k_xq_thread_refine_3rounds.json` 是后续对 8/12/16 线程各三轮交错顺序的补测。使用单独的 `native/topos_codec/build/perf_current` Release 库，内嵌提交 `1d30609f`，通过 `TOPOS_CODEC_LIB` 指向该库；测量参数与上方相同，仅改变 `--threads`，顺序依次为 8/12/16、16/8/12、12/16/8。每次完整报告的路径和库/机器指纹见 JSON。该补测同样排除源解码与应用调度，并以 `--allow-dirty` 记录为本机先导值。

## 三种新增 RAW 实片的读取与 CFA 探针

`raw_media_access_2026-09-28.json` 绑定三份文件的完整 SHA-256，并分别记录了 FFmpeg/Resolve 的实测读取结果及 TOPOS 产品媒体工厂的当前结果。BRAW 与 R3D 在 Resolve 20.2.1.6 的临时项目里成功导入并各导出首帧 RGB 图片；临时项目随即删除，原项目恢复。图片未入库，避免把显影后的 8-bit RGB 误当原始 CFA。BRAW/R3D 目前仍不能经 TOPOS 自身的 PyAV 媒体工厂直接解码。

ProRes RAW HQ 首帧由 FFmpeg 解出 `bayer_rggb16le`，以偶数坐标 `(3072, 1136)` 裁取 2048×2048，再按 RGGB 四相位顺序写为原生 RAW16 输入。可复现的核心步骤：

```bash
mkdir -p bench_out/topos_raw_real_cfa_probe
ffmpeg -hide_banner -v error \
  -i tests/test_videos/H034C0035_240410_P05R39.MOV \
  -map 0:v:0 -frames:v 1 -pix_fmt bayer_rggb16le -f rawvideo -y \
  bench_out/topos_raw_real_cfa_probe/proresraw_frame0.rggb16le
.venv/bin/python - <<'PY'
import numpy as np
from pathlib import Path
folder = Path('bench_out/topos_raw_real_cfa_probe')
frame = np.memmap(folder / 'proresraw_frame0.rggb16le', dtype='<u2',
                  mode='r', shape=(4320, 8192))
crop = frame[1136:3184, 3072:5120]
with (folder / 'proresraw_center_2048_raw16.raw').open('wb') as target:
    for phase in (crop[0::2, 0::2], crop[0::2, 1::2],
                  crop[1::2, 0::2], crop[1::2, 1::2]):
        target.write(np.ascontiguousarray(phase).tobytes())
PY
.venv/bin/python tools/measure_topos_native_domains.py \
  --kind raw16 \
  --source bench_out/topos_raw_real_cfa_probe/proresraw_center_2048_raw16.raw \
  --width 2048 --height 2048 \
  --out-dir bench_out/topos_raw_real_cfa_probe/raw16_six_tiers
```

期望四相位输入 SHA-256：`03961f031d9181f797af95de4e655d0ce62d6550e076ccacfcf303d7a770719f`；六档输出统计保存在 `native_domains_proresraw_decoded_raw16_crop.json`。这是**已压缩 ProRes RAW 的解码值**，只验证 TOPOS 对真实拍摄内容的再编码表现，既非相机无损真值，也非三种 RAW 文件之间的同源画质对比。
