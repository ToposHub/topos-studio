# Topos 容器音轨互操作报告（M-B0）

- 日期：2026-09-20
- 关联：优化计划 `topos_container_audio_optimization_plan_2026-09-20.md` §3 M-B0
- 矩阵脚本：`native/topos_codec/tests/interop/audio_interop_matrix.py`（可 CI 复跑）
- 产物：40 个（lpcm 16/24/32bit × 2.0/5.1/7.1 × 44.1k/48k × standard/faststart = 36；
  mp4a 2.0/5.1 @48k × standard/faststart = 4），音频 0.4s 确定性多频正弦母带

## 1. 结论（TL;DR）

**修复两处写侧缺陷后，三个硬引擎 40/40 全绿**（ffprobe 识别 / ffmpeg 逐位解码 /
AVFoundation 打开导出）。发现并修复：

| # | 发现 | 严重性 | 处置 |
|---|---|---|---|
| F1 | **esds 描述符编码错误**：长度字段写成固定 u32 BE（规范要求 ISO 14496-1 可变长 7-bit 续位），且 DSI/SL 被嵌进 DecoderConfigDescriptor 内部（`dcd_len=13+5+dsi+6` 错误算术）而非兄弟节点。ffmpeg/VLC 顺序扫描 tags 不看长度故能容错；**CoreAudio 严格解析读出长度=0**（0 packets），AVFoundation 整文件拒开 | Critical | **已修复**（本报告同批提交）：`buf_desc_len4` 规范长度 + 同级链结构 |
| F2 | **mp4a 缺 QuickTime wave 包裹**：AVFoundation/QuickTime 电影引擎要求 `wave{frma('mp4a'); 'mp4a'12B 空原子; esds; 8B null}` 规范形态（ffmpeg movenc 同构），直挂 esds 在 MOV 模式下拒开文件 | Critical | **已修复**（同批）：wave 包裹 + frma + chan 声道声明 + compressionID=0xFFFE（V1 惯例） |

known-issue（记录不阻塞）：

| # | 现象 | 影响面 | 对策 |
|---|---|---|---|
| K1 | **VLC 3.0.21 不支持 'lpcm' fourcc 家族**（"unknown raw audio sample size"；不解析 TN2120 扩展），36/36 lpcm 组合失败；mp4a 组合 4/4 通过 | 仅 VLC；AVFoundation/CoreAudio/ffmpeg/NLE 均正常 | 记录 known-issue，不改容器形态（'lpcm' 为 QuickTime 原生规范形态，AVFoundation 全绿）；16-bit 分发场景可经 ffmpeg 转码。VLC 4 的 mov 支持重写，届时复测 |
| K2 | **CoreAudio AudioFile C API 对多轨 MOV 支持浅**：含视频 trak 的 mp4a 组合 `ExtAudioFileRead -66567`、lpcm 组合字节数误读（"audio bytes: 40"）；纯音频 MOV 正常 | 工具链认知（afinfo/afconvert ≠ QuickTime Player 引擎），非产物缺陷 | 报告中降级为软引擎；QuickTime 引擎以 AVFoundation（avconvert）为准 |

## 2. 引擎与方法

| 引擎 | 版本/来源 | 角色 | 判定方式 |
|---|---|---|---|
| ffprobe/ffmpeg | 8.1（Homebrew） | 硬：流识别 + 解码 | lpcm 解码逐位 md5 对拍参考 BE→LE 字节（含声道序验证）；AAC 互相关 ≥0.9 |
| **avconvert** | macOS 15 系统自带（AVFoundation） | 硬：QuickTime/AVFoundation 同源电影引擎 | 能打开并导出音频（PresetAppleM4A）；>2ch 产物走 preset 立体声下混（记 PASS + 备注） |
| afinfo/afconvert | macOS 15 系统自带（CoreAudio AudioFile API） | 软：记录 K2 限制 | 打开/解码；多轨 MOV 失败记 SOFT-FAIL |
| VLC | 3.0.21 Vetinari | 软：独立 mov demuxer/lpcm 解码器 | sout 转码 wav + 互相关 |
| mpv | 未安装 | N/A | 不阻塞 |

互相关参考：确定性多频正弦（逐声道差半音，可辨识声道错序）。

## 3. 矩阵结果（修复后最终态）

汇总：**ffprobe 40/40 ✅ · ffmpeg 40/40 ✅（lpcm 全组合逐位相等）· AVFoundation 40/40 ✅ ·
CoreAudio API 2✅/38⚠️（K2）· VLC 4✅/36❌（K1，全部 lpcm 组合）· mpv N/A**

图例：✅ PASS · ❌ FAIL · ⚠️ SOFT-FAIL（K1/K2 已解释）· N/A 未安装

| 产物 | 采样率 | 声道 | ffprobe | ffmpeg | AVFound | CA-API | VLC | mpv |
|---|---|---|---|---|---|---|---|---|
| lpcm_16b_20_44100 | 44100 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_20_44100_fs | 44100 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_20_44100 | 44100 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_20_44100_fs | 44100 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_20_44100 | 44100 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_20_44100_fs | 44100 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_51_44100 | 44100 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_51_44100_fs | 44100 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_51_44100 | 44100 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_51_44100_fs | 44100 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_51_44100 | 44100 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_51_44100_fs | 44100 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_71_44100 | 44100 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_71_44100_fs | 44100 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_71_44100 | 44100 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_71_44100_fs | 44100 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_71_44100 | 44100 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_71_44100_fs | 44100 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_20_48000 | 48000 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_20_48000_fs | 48000 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_20_48000 | 48000 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_20_48000_fs | 48000 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_20_48000 | 48000 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_20_48000_fs | 48000 | 2ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_51_48000 | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_51_48000_fs | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_51_48000 | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_51_48000_fs | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_51_48000 | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_51_48000_fs | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_71_48000 | 48000 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_16b_71_48000_fs | 48000 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_71_48000 | 48000 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_24b_71_48000_fs | 48000 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_71_48000 | 48000 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| lpcm_32b_71_48000_fs | 48000 | 8ch | ✅ | ✅ | ✅ | ⚠️ | ❌ | N/A |
| mp4a_20_48000 | 48000 | 2ch | ✅ | ✅ | ✅ | ✅ | ✅ | N/A |
| mp4a_20_48000_fs | 48000 | 2ch | ✅ | ✅ | ✅ | ✅ | ✅ | N/A |
| mp4a_51_48000 | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ✅ | N/A |
| mp4a_51_48000_fs | 48000 | 6ch | ✅ | ✅ | ✅ | ⚠️ | ✅ | N/A |

## 4. 修复的字节级细节（写侧，mov.c）

### F1 esds 描述符（修复前 → 修复后）

```
修复前（错误）：                       修复后（规范，与 ffmpeg movenc 同构）：
03 00000025 ← 长度 u32BE（读作 0）     03 80 80 80 25 ← 7-bit 续位可变长
  04 0000001d ← DCD 嵌套 DSI/SL         04 80 80 80 0d ← DCD 恒 13B 叶子
    ...13B 字段 + DSI + SL                40 15 ... 13B
  05 00000005 119056e500              05 80 80 80 05 119056e500 ← DSI 兄弟
  06 00000001 02                      06 80 80 80 01 02 ← SL 兄弟
```

### F2 mp4a 声样描述（修复前 → 修复后）

```
修复前：mp4a(v1){ base36; V1 16B; esds; }              ← AVFoundation 拒开
修复后：mp4a(v1, compressionID=0xFFFE){ base36; V1 16B; ← 全引擎通过
          wave{ frma('mp4a'); 'mp4a'12B 空原子; esds; 8B null };
          chan(layout tag MPEG_5_1_A / 7_1_A / STEREO) }
```

兼容性说明：esds/wave/chan 变更仅影响 **mp4a（AAC）档**的 stsd 声样描述；
lpcm 档声样未动；样本数据、样本表（stts/stsc/stsz/stco）、moov 布局均不变。
纯视频 golden 与既有 v1.1/v1.3 音频文件的读取行为不受影响（读侧同步支持
wave 包裹形态并按规范长度严格解析；直挂 esds 旧形态仍容忍）。

## 5. 手动清单（GUI 播放器，本机可执行项）

| 播放器 | 引擎 | 状态 | 检查要点 |
|---|---|---|---|
| QuickTime Player | AVFoundation | ✅ 已由 avconvert 覆盖（同引擎）；GUI 双击抽验建议保留 | 声画同步、5.1/7.1 声像 |
| DaVinci Resolve | 自有 + QuickTime | ⏳ 待手动：导入 `/tmp/mb0_full2/` 任一产物 | 音轨出现、波形、声道标签 5.1/7.1 正确解读（§0.5 chan 检查项） |
| Adobe Premiere Pro 2025 | 自有 | ⏳ 待手动：同上 | 同上 |
| IINA | mpv 内核 | ⏸ mpv 未装 CLI；IINA 可手动抽验 | 音频是否出声（预期 lpcm 组合同 K1） |
| Windows Media Foundation | MF | ⏳ 需 Windows 环境 | lpcm/mp4a 双轨识别 |

## 6. 对 M-B1 的输入

1. F1/F2 已在本批修复并全量回归（C 87 项、tests/media topos 1195 项、
   tests/edit 1331 项、纯视频 interop oracle 全绿）——M-B1 elst 落地时
   rebase 在此基线上。
2. AAC 互操作同步复跑（M-B1 验收项）直接复用本脚本：
   `python audio_interop_matrix.py --work <dir>`（约 3 分钟）。
3. 手动清单第 5 节待用户执行后回填本报告。
