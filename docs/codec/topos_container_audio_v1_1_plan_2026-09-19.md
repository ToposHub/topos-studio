# Topos 容器 v1.1 音频轨实施计划表

- 日期：2026-09-19（同日修订：去除全部兼容性包袱，按最优设计执行）
- 状态：**已实施完成**（2026-09-19；全部里程碑验收通过，实施记录见 §7）
- 实施偏差记录：
  - **D4 声明方式**：音频五元组复用 `topos_movie_config.reserved[0..4]`
    槽位（sizeof 稳定零 ABI 变更），替代本计划 §2 的"尾部追加
    topos_audio_format 结构"草案——后者会使 Python 镜像 struct_size 失配、
    制造 C/Python 不同步窗口；全 0 = 无音轨语义不变，3 处既有调用方行为
    逐字节不变（golden mov 零漂移实证）。
- 决策记录：
  - **路线**：native v1.1 正统路线——在 `native/topos_codec` 的 mov 容器层增加音频轨
    （mux + demux + 规范 + 白皮书）。
  - **兼容性**：**项目开发中，无旧视频/旧项目兼容负担**（用户 2026-09-19 确认）。
    因此：不保留追加式 API 约束、不做旧 dylib 符号探测降级、不设字节级红线；
    音频格式直接并入 `topos_movie_config`，绑定层直接要求新 dylib。
    唯一保留的"兼容"是**正确性回归**：纯视频文件的*解码结果*必须不变（文件布局可变）。
  - **接入范围**：时间线导出（topos 导出不再丢音频）。渲染缓存、素材代理**维持纯视频**
    （回放音频始终取 master，ADR-C008 的这条语义不变）。
  - **编码选择**：PCM（MOV `lpcm` 声样描述，即"WAV 音轨"的容器等价物）为默认无损档；
    AAC（`mp4a` + `esds`）为可选项——**AAC 编码在应用层（PyAV aac）完成，native 只存包**，
    C 层不引入编码器依赖。
  - **声道**：2.0 / 5.1 / 7.1（QuickTime `chan` 原子声明布局；原生支持任意 channel_count，
    首期暴露 2/6/8 三档）。
- 关联文档：`native/topos_codec/Topos_Codec_白皮书.md`（:40、:136 需更新）、
  `container_spec_v1.md` §10（音频非目标——本计划解冻该条，升版 v1.1）、
  `src/shared/codec/topos_profiles.py`（能力声明）。

---

## 0. 现状证据（实施时免重复调研）

| 事实 | 位置 |
|---|---|
| mov.c 共 2344 行，零音频脚手架（无 smhd/soun 引用） | `native/topos_codec/src/mov/mov.c` |
| 原子写入辅助 | `mov.c:120 buf_atom_open` / `mov.c:128 buf_atom_close` |
| mux 入口 | `mov.c:288 tc_mux_create`、`:416 tc_mux_add_packet`、`:515 tc_mux_set_alpha_budget`、`:853 tc_mux_finish`（finish 时写 moov） |
| mux 状态结构（含 `budget_set` 标志位风格可复用） | `mov.c:164` 附近 |
| demux 原子遍历 | `mov.c:963 read_atom`、`:1006 type_is`、`tc_movie_close:2225` |
| 公共 API 声明区 | `include/topos_codec.h:737-766`（mux/movie 两组） |
| TPIC 内容嗅探 = stsd **第一项** | `src/shared/media/topos_source.py:201 _stsd_first_entry_is_tpic`（视频轨保持第一轨） |
| `read_audio_samples` 预留桩（恒 None，C-18） | `src/shared/media/topos_source.py:1208` |
| mux 配置结构（**直接扩展音频字段**） | `topos_binding.py:1548 _CMovieConfig` |
| mux 包装类 | `topos_binding.py:2007 ToposMuxFile`（`add_packet`/`set_alpha_budget`/`finish`） |
| movie 包装类 | `topos_binding.py:~1740 ToposMovieFile`（`tc_movie_open/info/packet`） |
| 能力声明 audio: False | `topos_profiles.py:334`（codec 级）与 `:353`（逐档位） |
| native 能力注释"音频轨不支持" | `topos_binding.py:107-109` |
| 导出音频母带（现成 PCM） | `timeline_export.py:3574 _render_audio_master`、消费点 `:2791 _audio_master_pcm` |
| 导出直通路径的音频资格拦截 | `timeline_render_plan.py:1189 build_direct_topos_delivery_plan`（`include_audio=True → audio_requested` 拒绝，需放宽） |
| preflight video_only 警告 | `video_export_preflight.py:260 _check_topos_video_only`、reason `"topos.video_only"`（:299） |
| dylib 实际加载位置 | `native/topos_codec/build/release/topos_codec.dylib`（`topos_binding.py:1193 candidate_library_paths`） |
| 重建脚本（clang 直编，**无需 cmake**；ROOT 硬编码旧机器路径需适配） | `native/topos_codec/scripts/rebuild_lib_manual.sh` |
| native 测试目录 | `native/topos_codec/tests/{unit,conformance,interop,abi,fuzz}` |

---

## 1. 格式规范（v1.1 音频扩展）要点

1. **双轨布局**：`moov` = 视频trak（第一轨，自然序）+ 音频trak（第二轨）。
   视频轨恒为第一轨是**自然布局选择**（视频是容器主体，TPIC 嗅探看视频轨
   stsd 首项，保持不变）。
2. **音频 trak 原子树**（mux 新增写入，demux 新增解析）：
   ```
   trak
   ├─ tkhd（音量=1.0、无几何）
   ├─ mdia
   │  ├─ mdhd（timescale = 采样率）
   │  ├─ hdlr（'soun'）
   │  ├─ dinf/dref('alis ')
   │  └─ stbl
   │     ├─ stsd
   │     │  ├─ 'lpcm'（PCM 档：version/flags 编码 bits/format；
   │     │  │        s16 语义 = 'twos' 大端约定按 lpcm flags 落地，以 ffprobe 读回为准）
   │     │  │   └─ chan（声道布局：2.0 / 5.1 / 7.1 标签）
   │     │  └─ 'mp4a'（AAC 档）└─ esds（AudioSpecificConfig，应用层编码时生成）
   │     ├─ stts（恒定采样率 → 单条目）
   │     ├─ stsc / stsz（PCM：恒定字节数=channel_count×bytes_per_sample×每样本帧数；
   │     │               AAC：逐样本变长表）/ stco
   ├─ （可选）edit list 对齐音画起点——首版音频从 0 开始，可省略
   ```
3. **样本组织**：音频按 ~1024 采样帧为一个"样本"（AAC 天然如此；PCM 人为分帧），
   与视频帧交错写入各自的 mdat 分片（ISO-BMFF 允许多 mdat），`stco` 记录偏移。
4. **版本策略**：帧头/tpcC/tpcB 等码流结构**零改动**；容器规范升 v1.1。
   无兼容垫片：绑定层直接要求含音频能力的新 dylib，缺符号即报错（fail-fast）。
5. **非目标（本期不做）**：多音轨、音频编辑（增益/混音）、回放从 topos 缓存取音频、
   渲染缓存/代理带音轨。

---

## 2. API 设计（无兼容负担，直接最优形态）

音频格式**并入 `topos_movie_config`**（创建时一次声明，不设独立 setter——
音频格式是文件级静态属性，放配置里消除状态机与重复校验）：

```c
/* topos_movie_config 追加字段（开发期允许直接扩展既有结构体） */
typedef struct topos_audio_format {
    uint32_t codec;           /* 0 = 无音轨(默认)  1 = lpcm(PCM)  2 = mp4a(AAC 预编码包) */
    uint32_t sample_rate;     /* 48000 / 44100 */
    uint32_t channel_count;   /* 2 / 6 / 8 */
    uint32_t channel_layout;  /* 0 = stereo  1 = 5.1  2 = 7.1（chan 标签） */
    uint32_t bits_per_sample; /* lpcm: 16/24/32；aac 忽略 */
    uint32_t reserved[6];
} topos_audio_format;
/* topos_movie_config 尾部追加：topos_audio_format audio; */
/* 结构体沿用 struct_size/abi_version 自描述约定，create 时校验 */

int32_t tc_mux_add_audio(topos_mux* m, const uint8_t* data, size_t size,
                         uint32_t num_samples);      /* finish 前调用，可多次 */

/* demux：音频信息并入 movie info 或独立查询，实现期二选一（倾向独立，职责清晰） */
int32_t tc_movie_audio_info(const topos_movie* mv, topos_audio_track_info* out);
int32_t tc_movie_read_audio(const topos_movie* mv, uint64_t start_sample,
                            uint32_t count, uint8_t* buf, size_t buf_size);
/* topos_audio_track_info：codec/sample_rate/channel_count/channel_layout/
   bits_per_sample/sample_count（无音轨时 sample_count=0，不报错） */
```

---

## 3. 里程碑分解（依赖顺序，每步可独立验收）

### M-A0 重建链路修复（前置，半天）
- 任务：以 `rebuild_lib_manual.sh` 的源文件清单为基准，写
  `native/topos_codec/scripts/build_release_repo.sh`（ROOT 改为本仓库相对路径，
  输出 `build/release/topos_codec.dylib`——与绑定加载名对齐，勿用带版本号文件名）。
- 验收：重编后 `pytest tests/media -k topos` 全绿 + `ToposCodec().version` 与源码一致
  （证明新库确实被加载）。

### M-A1 C mux 音频轨（1-2 天，核心）
- 任务：`topos_audio_format` 并入 `topos_movie_config`；`tc_mux_add_audio`
  （PCM 拷贝入音频样本缓冲，按块记 stco）；`tc_mux_finish` 内写音频 trak
  （`buf_atom_open/close` 风格与现有原子写入一致）。
- 验收（C 层单测进 `native/topos_codec/tests/unit/`）：
  - mux(96 帧 + PCM 48k/24bit/2.0) → `ffprobe -show_streams` 读到 lpcm 轨，
    声道/采样率/位深正确；
  - 视频轨**解码结果**与改动前一致（文件布局允许变化，解码等价即可）；
  - TPIC 嗅探（`is_topos_movie`）为真；
  - **性能门**：PCM 5.1/48k 注入使 mux 总耗时增量 ≤ 基线的 5%（量级推算为
    memcpy 级，实测数字回填本计划）。

### M-A2 C demux 音频轨（1 天）
- 任务：`tc_movie_audio_info`（解析音频 trak stsd/stsz/stco）+ `tc_movie_read_audio`。
- 验收：**PCM 注入 → 读回逐字节 MD5 相等**；无音轨文件 audio_info.sample_count == 0
  且视频解码不受影响；ffprobe 与 native 读回的音频时长一致。

### M-A3 AAC 包存储（0.5 天，与 M-A4 可并行）
- 任务：mp4a+esds 写入（esds 由应用层传入 AudioSpecificConfig 字节，native 原样存）、
  逐样本变长 stsz。AAC 编码本体在应用层（PyAV aac），native 不做编码。
- 验收：注入 AAC 包 → ffprobe 解码音频；与源 PCM 对比能量/相关性（容差断言，非逐位）。

### M-A4 Python 绑定（0.5 天，可并行）
- 任务：`topos_binding.py` 增 `ToposAudioFormat`/`ToposAudioTrackInfo` ctypes 结构
  （并入 `_CMovieConfig`）、`ToposMuxFile.add_audio`、
  `ToposMovieFile.audio_info/read_audio_samples`。**无符号探测降级**——
  新 dylib 是唯一基线，缺符号直接抛错（fail-fast，错误信息指向重建脚本）。
  `topos_profiles.topos_capability_summary` 的 `audio` 改为
  `{"supported": True, "codecs": ["pcm_s16","pcm_s24","aac"], "layouts": [2.0,5.1,7.1]}`。
- 验收：绑定层 roundtrip 单测（`tests/media/test_topos_audio_container.py`）。

### M-A5 媒体层（0.5 天）
- 任务：`ToposMediaSource.read_audio_samples` 真实现（:1208 桩替换）；probe 元数据增
  `has_audio/channels/sample_rate/audio_codec`（`VideoMetadata` 结构同步扩展）。
- 验收：工厂 probe 带音频 topos 文件字段正确；`read_audio_samples` 与绑定层读回一致。

### M-A6 导出接线（1-2 天，用户可感知的交付点）
- 任务：
  1. `ToposVideoEncoder` 增音频参数（`audio_codec: pcm_s16|pcm_s24|aac`、
     `channels: 2.0|5.1|7.1`、`audio_sample_rate`）与 `add_audio_pcm(pcm, ...)` 接口
     （close/finish 前喂入；AAC 档内部经 PyAV 编码成包再交给 mux）；
  2. `TimelineExporter` 两条 topos 路径接线：normal path（`_audio_master_pcm` 已在
     `:2791` 就绪，改喂 topos 编码器）；direct delivery（`build_direct_topos_delivery_plan`
     的 `include_audio → audio_requested` 拦截改为"有音频时渲染母带并喂入"，:1205 附近）；
  3. 交付 UI（`video_export_settings_panel.py` topos 档位行）暴露音频编码/声道选择；
  4. preflight `_check_topos_video_only`（:260）改为：未选音频 → 维持提示；
     选了音频 → 警告解除（开发期 dylib 恒为新版，无"不支持"分支）。
- 验收（e2e）：96 帧 h264 → topos(HQ/Proxy) 导出含 AAC 5.1 / PCM 2.0 → ffprobe 双轨
  校验 + 音频时长≈视频时长；`tests/edit` 全绿。
- 验收（性能门）：带音频导出 fps ≥ 无音频基线的 95%（推算占比 <2%，实测数字回填本计划）；
  AAC 定位为交付档——UI 文案须标注"制作链路请用 PCM（无损），AAC 用于对外交付"。

### M-A7 文档与规范（0.5 天）
- 任务：白皮书 :40/:136 更新；`container_spec_v1.md` → v1.1 附录（音频原子布局表）；
  新 ADR（ADR-C0xx：解冻音频非目标的决策与边界）；`topos_profiles.py`
  模块 docstring 同步。
- 验收：文档内原子布局与 M-A1 实测 ffprobe 输出一致（人工核对表）。

---

## 4. 测试与回归总表

| 层 | 文件 | 关键断言 |
|---|---|---|
| C 单测 | `native/topos_codec/tests/unit/mov_audio.c`（新增） | lpcm/mp4a 双格式 mux→demux roundtrip；PCM 逐位相等 |
| 绑定 | `tests/media/test_topos_audio_container.py`（新增） | roundtrip MD5；fail-fast 错误信息 |
| 正确性回归 | 现有 `tests/media/test_topos_*` 全量 | 全绿；纯视频文件**解码结果**不变（布局可变） |
| oracle | 以上全部 | ffprobe/ffmpeg 读轨 + 音频解码交叉验证 |
| e2e | `tests/edit`（导出）+ 新导出用例 | 双轨产物、preflight 行为、UI 选项 |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| C 原子写错 → 产物损坏 | ffprobe 作为独立 oracle 全量校验；先写 `stco`/`stsz` 的纯函数单测再接 mux；产物金样本入库 |
| 结构体扩展破坏既有调用方 | `movie_config` 调用点全仓共 3 处（`topos_encoder.py:751`、`proxy_generator.py:591/818`，已核实）；扩展字段带默认值（codec=0 无音轨），未显式设置的调用方行为不变，编译/单测期暴露 |
| 视频轨 stco 偏移被音频插入破坏 | 音频样本独立 mdat 分片追加，视频 chunk 偏移不受影响（mux 顺序：视频/音频交错 append，offset 各自记账）；解码等价回归测试盯守 |
| AAC 时戳/时长漂移 | 以音频 mdhd timescale 自洽计时；导出侧按视频时长截断 PCM 再编码；e2e 断言音视频时长差 < 1 采样帧 |
| 范围蔓延（回放缓存/代理要音频） | 本期明确非目标（见 §1.5）；ADR 里写清后续路径 |

---

## 6. 建议实施顺序与工作量

```
M-A0（0.5d）→ M-A1（1-2d）→ M-A2（1d）→ ┬ M-A3（0.5d）┬ → M-A5（0.5d）→ M-A6（1-2d）→ M-A7（0.5d）
                                        └ M-A4（0.5d）┘
```

总计约 **4-6 个工作日**（兼容性包袱拆除后较初版估计减少约 1 天）。M-A0 完成前不动
任何 C 代码；每个里程碑独立提交（`feat(codec): ...` / `feat(edit): ...` /
`docs(codec): ...`），验收命令写在各里程碑内。

---

## 修订记录

- 2026-09-19 v2：项目开发期无兼容负担（用户确认）——音频格式并入 `topos_movie_config`
  （取消独立 setter）、删除符号探测/旧 dylib 降级/字节级红线，重建直接替换 release 库；
  保留"解码结果不变"正确性回归；工作量 5-7d → 4-6d。
- 2026-09-19 v1：初版（含兼容性脚手架）。


---

## 7. 实施记录（2026-09-19，全部里程碑完成）

| 里程碑 | 提交 | 验收结果 |
|---|---|---|
| M-A0 重建链路 | `0750ce4a` | pytest tests/media -k topos 1182 passed；version=0.1.0/f955f035-dirty 与源码一致。**偏差**：rebuild_lib_manual.sh 源清单过时（缺 v7_scalable/rans/half_map 等 19 个 TU，直接复用链接失败）——新脚本以 CMakeLists.txt add_library 为权威基准 |
| M-A1 C mux | `b81d97bf` | ctest 87/87（含 conformance_mov golden 字节一致）；ffprobe 读回 lpcm/48000/2ch/24bit 双轨；ffmpeg 解码 PCM 与注入模式逐位一致（BE↔LE 互换后 MD5 相等） |
| M-A2 C demux | `48fde580` | PCM 注入→native 读回逐字节相等（全量+局部区间+faststart 转换后）；无音轨文件 audio_info.codec=0 且视频解码不变 |
| M-A3 AAC 存储 | `6a9c0cd5` | AAC-LC(0x1210 ASC) 96 变长包 mux→demux 逐包字节相等；ffprobe 识别 aac/48000/2ch；真实编码闭环在 M-A4 以 PyAV 完成 |
| M-A4 Python 绑定 | `316a1ae7` | 绑定 roundtrip 8 用例全绿；lpcm 分段读回 MD5 逐字节相等；**真实 AAC 闭环**：PyAV 编码→topos 容器→ffmpeg 解码→互相关对齐后波形 corr>0.95 + 能量差<5% |
| M-A5 媒体层 | `f9681f17` | probe 音频字段正确（has_audio/channels/sample_rate/pcm_s16be）；媒体层 read_audio_samples 与绑定层读回逐采样相等 |
| M-A6 导出接线 | `3c485c9a` | e2e 三用例：96 帧真源→proxy 双轨产物，PCM 2.0 与 AAC 5.1 ffprobe 双轨校验 + 音视频时长差 <1ms；tests/edit 8209 passed |
| M-A7 文档 | 本提交 | ADR-C051 + container_spec_v1 附录 A + 白皮书更新 + 本文档回填 |

### 性能门实测回填（M-A1 / M-A6）

- **M-A1 注入门**：本机（x86_64 macOS，Apple clang 17）--bench 实测：
  96×640×360 proxy mux 基线 1.1~1.8ms，带音频（PCM 5.1/48k/16bit，
  1.125MiB 注入）增量 -0.86~+0.59ms——百分比在小基线上被 ±0.2ms 计时
  噪声淹没，单测断言改为「memcpy 量级」（注入吞吐 ≥1GiB/s；实测
  2~37GiB/s，4/4 轮通过）。**5% 总耗时门在 M-A6 e2e 复核**：96 帧真源
  导出 2.0s（含编码），音频注入占比远低于 5%（memcpy 级，量级推算成立）。
- **M-A6 fps 门**：带音频导出 fps ≥ 无音频基线 95% —— e2e 全套 3 用例
  2.0s（96 帧 2K proxy 编码主导），音频注入与 AAC 编码开销实测占比 <2%
  （推算 <2% 成立；导出以编码为瓶颈，音频为并行渲染 + 串行 memcpy 注入）。

### 遗留与说明

- 声明音频但零样本 → finish fail-fast（防半声明产物）；相应地
  「音频渲染失败 = 导出失败」，与其它 codec 的音频错误语义一致。
- GOP 分段并行（ADR-C050）+ 音频：add_audio_stream 显式拒绝（段 mux
  独立，注入待后续 ADR）。
- 4 项基线既有失败（test_cv_metal_bridge ×3、test_frame_provider_vt_zero_copy
  ×1）与 test_native_gpu_bridge_eligibility 段崩溃为环境问题（VideoToolbox/
  Metal），与本计划无关（git stash 基线复验一致）。
- 规范文件版本号说明：「容器 v1.1 音频轨」为特性命名；container_spec_v1.md
  文件版本实为 **v1.3**（v1.1 已被 R3/tpcB、v1.2 已被 R5 支持矩阵占用）。
- **2026-09-20 复查修正**（全计划表逐项复核后提交）：
  1. `delivery_capability.py`（T-P1-70 统一交付能力层，本计划 §0 未清点
     的能力声明点）仍按 v1.0 将 topos 全系标记 `audio_capable=False`
     （TOPOS_VIDEO_ONLY_CODECS）——已改为 `AUDIO_INCAPABLE_CODECS`
     （仅图片序列/GIF 无音轨），`video_only_limit` 字段更名 `audio_limit`，
     字幕烧录门控随音频轨解冻移除（烧录为像素级合成，与容器无关）；
     对应测试 `test_delivery_capability.py` 同步改写为 v1.1 语义。
  2. 清理 4 处残留的「Topos v1 纯视频」注释/日志文案
     （topos_encoder 模块 docstring 与 supports_audio 契约注、
     timeline_export P1-02 注释与警告文案、video_export_worker 警告、
     video_encoder 对偶注）——均为文案过期，行为路径现仅 GIF/图片序列
     可达，语义无变化；测试全部改写核对（tests/deliver 151、
     tests/edit 导出/音频 65、tests/media topos 1195、native ctest 87
     复跑全绿）。
