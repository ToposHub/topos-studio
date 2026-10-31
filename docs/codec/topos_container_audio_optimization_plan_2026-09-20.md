# Topos 容器音轨优化计划表（v1.1 音频轨后续 → 规范 v1.4/v1.5）

- 日期：2026-09-20
- 状态：**待实施**（里程碑 M-B0..M-B9；实施记录占位见 §7）
- 前置：`topos_container_audio_v1_1_plan_2026-09-19.md` M-A0..M-A7 已全部交付
  （提交 `0750ce4a`..`f858cf72`），复查修正 `9e39998b`（delivery_capability v1.1
  语义收尾）。当前规范文件版本 **v1.3**（v1.1=tpcB、v1.2=R5、v1.3=音频轨——
  版本号沿用文件级递增，"容器 v1.1 音频轨"为特性命名）。
- 定位：v1.1 音频轨在**计划表范围内**已验收完备（PCM 档无损、采样精确、往返
  逐字节相等）；本计划处理的是**范围内刻意留下的边界**与**文件离开本软件
  之后**的场景。按影响排序：AAC 同步精度 > 跨播放器互操作 > 健壮性 >
  再导入回放 > 采样率扩展 > 工程收尾。
- 关联文档：
  - `native/topos_codec/docs/ADR-C051-container-v1.1-audio-track.md`（v1.1 边界）
  - `native/topos_codec/docs/ADR-C008*`（回放音频取 master——M-B3 修订对象）
  - `native/topos_codec/docs/ADR-C050*`（GOP 分段并行——M-B5 决策对象）
  - `native/topos_codec/docs/container_spec_v1.md`（现 v1.3；本计划升 v1.4）
  - `src/shared/export/topos_encoder.py`（AAC 应用层编码，:1366 `_aac_encode_pcm`）

---

## 0. 背景与三个边界（现状证据，实施时免重复调研）

### 0.1 边界一：AAC 编码器延迟（priming）未裁剪——交付档同步偏差

AAC-LC 编码器固有启动延迟（priming）：编码器第一个包承载的是历史依赖的
静音前缀，解码后音频内容整体后移。我们目前**原样存包**、无 edit list，
外部播放器解码 AAC 档时音频晚约 2048 采样（48k 下 ≈42.7ms），恰在人眼
可察觉口型同步阈值（约 ±45ms）边缘。**PCM 档完全不受影响**（采样精确）。

| 事实 | 位置/证据 |
|---|---|
| 容器内无 elst（规范非目标，"pts 无空洞约束"） | `container_spec_v1.md:262`（§10 elst 不写）、`mov.c:522`（mux pts 单调约束） |
| 我们的 e2e 靠 2048 采样硬编码对齐通过——问题存在的直接证据 | `tests/media/test_topos_audio_container.py:374-377`（`dec = got[2048:...]` + 互相关 >0.95） |
| 实测①：帧 pts 顺序喂入 → 首包 pts = **-1024**（编码器自报 priming=1024） | 本机 PyAV 17.0.1 探测（2026-09-20），`CodecContext.create('aac','w')` 48k stereo |
| 实测②：容器 roundtrip 解码后实测对齐 **2048** 采样 | v1.1 e2e 上述硬编码值（互相关峰实测） |
| 两者相差 1024 → 解码侧管线延迟参与端到端偏移；elst 该写的值须**校准**而非照抄编码器自报 | 实测①②对照（M-B1 任务 1 解决） |
| PyAV 17 无 `CodecContext.initial_padding`；首包 `iter_sidedata()` 无 skip_samples；`delay=0` | 同上探测——priming 获取只能走 pts 偏移或端到端校准 |
| 行业标准做法：音频 trak 写最小 `edts/elst`（media_time = priming），ffmpeg movenc 即如此 | FFmpeg `libavformat/movenc.c`（参考实现） |
| **priming 随编码器后端而异**：AudioToolbox `aac_at` 写 MOV 时 elst media_time=**2112**（2×1024+64），而我们实测 native aac 为 2048——换编码器后端必须重新校准（ProRes 对标调研，2026-09-20） | [ffmpeg-users 2017](https://ffmpeg.org/pipermail/ffmpeg-user/)（aac_at elst=2112 实例） |
| 部分播放器（老 Android、部分硬解）**忽略 elst** → AAC 同步抽检必须进互操作矩阵，且 M-B1 落地后需复跑 | ffmpeg Trac 社区已知问题（同上调研） |

### 0.2 边界二：音轨"只写"——回放/再导入无声

| 事实 | 位置 |
|---|---|
| 媒体层 `read_audio_samples` 已实现但**全仓零消费方** | `src/shared/media/topos_source.py`（M-A5 实现）；唯一引用是基类预留注释 `src/shared/media/source.py:75` |
| 音频回放走 PyAV 直开文件路径，不经 source 抽象 | `src/features/edit/core/audio_playback.py:844` `AudioDecoder.open(path)`（`av.open` 容器 → 音频流解码） |
| ffmpeg/PyAV 的 mov 通用 demux 可直接读我们的 lpcm/mp4a 双轨 | v1.1 M-A1/M-A4 已用 ffprobe/ffmpeg 验证 |
| 回放音频取 master 的边界（再导入场景未定义） | ADR-C008 |

即：用户把导出的 topos 文件**重新导入时间线 → 静音**。`AudioDecoder` 直开
路径意味着可能只需少量接线（M-B3 调研先行确认）。

### 0.3 边界三：GOP 分段并行（ADR-C050）与音频互斥

| 事实 | 位置 |
|---|---|
| `add_audio_stream` 显式拒绝分段并行模式 | `src/shared/export/topos_encoder.py:1308`（`_gop_seg` 拒绝；开关 :360/:543/:924） |
| 根因：段 mux 相互独立，音频样本无处安放 | ADR-C050 段化设计 |

### 0.4 次级遗留（小项，分散在各里程碑内消化）

| 事实 | 位置 | 消化里程碑 |
|---|---|---|
| 采样率白名单仅 44100/48000（中间片场景 96k 不罕见） | `mov.c:250-251`（audio_cfg_validate :230） | M-B4 |
| **采样率下拉含 96k 且不按容器过滤**——选 topos+96k 在导出启动时被 C 层 fail-fast 拒绝（UX 缺口；仅 codec 下拉做了同步收敛，采样率下拉没有） | `video_export_settings_panel.py:1180-1184` vs `mov.c:250` | M-B4 |
| **32bit 整数"悬空"**：容器可存可读（白名单含 32）但导出路径无 `pcm_s32` 映射——无写入口的能力 | `mov.c:263`（白名单 16/24/32）vs `topos_encoder.py` `_AUDIO_CODEC_MAP`（仅 s16/s24/aac） | M-B4 |
| **float32 全链路缺失**：音频母带本为浮点渲染，导出时被 `clip[-1,1]+rint` 量化为 int——超 0dBFS 余量与亚 LSB 精度丢弃；C 层 formatFlags 固定有符号整数（'twos'）语义 | `topos_encoder.py:1447-1450`（量化点）；`build_audio_stsd`（formatFlags=0xE） | M-B4 |
| 位深无独立选择器（隐含于 codec 名）——"维持现状"需要显式决策记录，避免反复讨论 | 面板 Audio 区（:1171-1200） | M-B4（记录决策） |
| 44.1k 路径测试覆盖薄（绝大多数用例用 48k） | v1.1 测试集 | M-B4 |
| 无音频 golden 样本（仅纯视频 golden 字节冻结） | `native/topos_codec/tests/conformance/` | M-B6 |
| fuzz 未覆盖音频原子（fuzz_mov 无音频种子与不变量） | `native/topos_codec/tests/fuzz/fuzz_mov.c` | M-B2 |
| `esds` 直挂 stsd（无 `wave` 包裹）仅经 ffprobe/ffmpeg（macOS）验证过 | `mov.c` build_audio_stsd | M-B0 |
| FFmpeg libtopos 官方补丁路线的音频侧未回归 | `packaging/ffmpeg/`（白皮书路线图） | M-B7 |

### 0.5 ProRes 对标调研（2026-09-20 联网调研，借鉴结论）

对标行业中间片标准 Apple ProRes 的音轨设计，结论分四档：

| ProRes 设计 | topos 现状 | 处置 |
|---|---|---|
| PCM 无压缩为主体（24-bit 标准），采样率**全家族** 44.1/48/88.2/96/176.4/192 kHz（Apple 官方资产指南） | 44.1/48（M-B4 原计划 +96k） | **并入 M-B4**：白名单一步收全家族——印证"PCM 为主、AAC 仅交付档"定位正确 |
| **多音轨架构**：FCP "Roles as Multitrack QuickTime" 按角色（对白/音乐/音效）每角色一条离散轨（A1/A2/A3/A4…），每轨独立 mono/stereo；广播交付 8 声道单轨/8 mono 轨常见，有 16 声道案例 | 单音轨 2/6/8 | **新增 M-B8**（stems 角色轨）：demux 侧已遍历分发天然可扩；mux 单轨状态机与导出 UI 角色映射是工作量所在 |
| **tmcd 时间码轨**：ProRes/FCP 普遍携带起始时码轨；MP4 不支持 tmcd（重封装即丢）——MOV 中间片容器的独有优势 | 无时间码概念 | **新增 M-B9**：对"回 Resolve/Premiere 继续剪辑"的中间片，TC 对位是刚需 |
| elst 同步：aac_at 实证 2112 vs 我们实测 native 2048（编码器后端差异）；部分播放器忽略 elst | M-B1 校准设计 | **补强 M-B1 证据**（已并入 §0.1）+ M-B1 验收增"interop 矩阵 AAC 同步复跑" |
| ProRes RAW 教训：音频可见性依赖 NLE 生态（部分 Premiere/Resolve 不装 RAW 扩展看不到音轨） | — | 策略印证：M-B0 互操作先行 + M-B7 补丁侧回归的排序正确（不改） |
| chan 原子声明布局；社区常见 8-mono↔5.1 标签错配痛点 | v1.1 已实现 | 已对齐；M-B0 矩阵须含"Resolve/Premiere 正确解读 5.1/7.1 标签"项（已在任务 3 检查项内） |

来源：[Apple Video and Audio Asset Guide](https://help.apple.com)、[Apple Developer: 'chan' atom](https://developer.apple.com)、[ffmpeg-users: aac_at elst=2112](https://ffmpeg.org/pipermail/ffmpeg-user/)、[FFmpeg Trac: tmcd vs MP4](https://trac.ffmpeg.org)、[Apple Support: Export roles in FCP](https://support.apple.com)、[Larry Jordan: FCP multi-channel audio](https://larryjordan.com)、[Video StackExchange: 8-mono 5.1 标签重写](https://video.stackexchange.com)、[16 声道 ProRes 实践](https://malcontentcomics.com)。

---

## 1. 目标与非目标

### 目标（按用户影响排序）

1. **AAC 交付档 A/V 同步对齐行业标准**（elst/priming，M-B1）——端到端
   残差 ≤ 1 采样。
2. **跨播放器互操作验证**（M-B0 先行）——QuickTime/VLC/Resolve/Premiere/
   Windows 至少一份通过矩阵报告，拒绝项有对策。
3. **demux 音频路径系统化 fuzz**（M-B2）——畸形输入必拒、无崩溃/挂起。
4. **再导入回放有声**（M-B3）——导出→导入→播放闭环。
5. **采样率与位深扩展**（M-B4）：96kHz + **float32（pcm_f32le）** +
   pcm_s32le 补齐——float32 是视频"中间片纯度"哲学（HDR float16/32 内部
   处理、色彩元数据完整携带）的音频对齐物：母带浮点直通、零量化、
   往返逐位相等；顺带修复采样率下拉不按容器收敛的 UX 缺口。
6. **分段并行+音频路线决策**（M-B5 ADR；实现可另立计划）。
7. **工程收尾**：音频 golden 冻结 + 三平台 CI 门禁（M-B6）、FFmpeg 补丁侧
   双轨回归（M-B7）。
8. **多音轨 / stems 角色轨**（M-B8，ProRes 对标最大差距）：按时间线角色
   （对白/音乐/音效）导出多条离散音轨，每轨独立格式与声道——专业中间片
   与交付物的分水岭能力。
9. **tmcd 时间码轨**（M-B9，ProRes 对标）：容器携带起始时码，支撑
   "回 Resolve/Premiere 按 TC 对位"的编辑往返（MP4 容器做不到，
   MOV 中间片的独有优势）。

### 非目标（本计划不做）

- 多语言轨标记（语言标签/tagging——多音轨 stems 本期落地（M-B8），
  语言元数据等生态有需求再加）。
- 音频编辑能力（增益/混音/淡入淡出——时间线层职责，非容器层）。
- 渲染缓存 / 素材代理携带音轨（ADR-C008 语义不变；仅 M-B3 修订
  "再导入文件即 master"这一条边界）。
- **视频轨 elst**（§3 "pts 无空洞约束"不动；elst 仅音频 trak 最小子集）。
- 无损压缩音频（flac/alac——PCM 已覆盖无损档位）。
- AAC profile 扩展（HE-AAC/AAC-LD 等）——固定 AAC-LC（交付档足够，
  低延迟场景另议）。
- 独立位深选择器——位深由 codec 名承载（pcm_s16le / pcm_s24le /
  pcm_s32le / pcm_f32le），不设冗余下拉（决策记录于 M-B4 任务 5）。

---

## 2. 核心设计：音频 trak 最小 elst（priming 对齐）

### 2.1 原子布局（mux 新增写入，demux 新增解析）

```
trak（音频轨，第二轨）
├─ tkhd …（不变）
├─ edts                        ← 新增（仅 priming > 0 时写；lpcm 恒不写）
│  └─ elst（version 0，flags 0，entry_count=1）
│     ├─ segment_duration  u32  —— movie timescale（=1000，mov.c:1146）下
│     │                            的轨时长（与 mdhd 时长换算一致）
│     ├─ media_time        i32  —— 音频 media timescale（=采样率，mov.c:1014）
│     │                            下的 priming 采样数（>0）
│     └─ media_rate        s16.16 = 0x0001_0000（1.0 倍速）
└─ mdia …（不变）
```

### 2.2 priming 的取值（关键：校准而非照抄）

编码器自报 priming（首包 pts = -1024）与端到端实测偏移（2048）不一致，
差额来自解码侧管线延迟。**elst 的 media_time 必须取"使解码器输出对齐"的
端到端值**，由校准测试钉死：

1. **机制获取**：`_aac_encode_pcm` 给帧喂顺序 pts（0, 1024, 2048, …），
   首包 `pts` 即 `-priming_enc`（已实测验证可行）。
2. **校准测试**（新增 C 之外的 Python 单测）：编码正弦 → mux（带候选
   media_time=X）→ ffmpeg 解码 → 与源互相关求最优 lag L(X)；遍历
   X ∈ {priming_enc, 2×priming_enc, 0}，取 |L(X)| ≤ 1 采样的 X 为准。
3. **钉死**：X 作为命名常量进代码 + 校准单测断言（锁定 PyAV 版本行为）；
   若未来 X 漂移（PyAV/ffmpeg 升级），测试先红——此时更新常量并记录。
4. ADR-C052 记录校准结论与差额根因（编码器自报 vs 解码侧延迟）。

### 2.3 API 变更（零 ABI 破坏，沿用 v1.1 手法）

```c
/* mux：finish 前调用，至多一次；仅 mp4a 声明下合法（lpcm 拒绝）；
   samples 须 > 0 且 < 已写入音频总采样数 */
int32_t tc_mux_set_audio_priming(topos_mux* m, uint32_t samples);

/* demux：topos_audio_track_info.reserved[0] → priming_samples
   （0 = 无 elst / 旧文件；字段语义在头文件注释冻结） */
```

- demux 解析规则（fail-fast，与既有风格一致）：仅音频 trak 接受 edts/elst；
  约束 entry_count=1、media_time > 0、media_rate == 0x00010000、
  media_time < 轨总采样数——违反任一 → `TC_ERR_MALFORMED`。
  **视频 trak 出现 elst 仍拒绝**（输入域不变）。
- `tc_movie_read_audio` 语义不变（仍返回原始存储字节）；对齐在应用层
  按 `priming_samples` 跳过——容器不替应用做时间轴裁剪。

### 2.4 兼容语义

- **纯视频文件零字节变化**（edts 只出现在音频 trak；conformance golden
  字节级不变，同 v1.1 手法钉死）。
- 旧音频文件（v1.3 写出）：无 edts → `priming_samples = 0`，读行为不变。
- 绑定层 fail-fast：新符号 `tc_mux_set_audio_priming` 直接要求新 dylib
  （无 hasattr 探测，与 v1.1 四符号同一纪律）。

### 2.5 规范升版 v1.4（与 M-B4 的采样率/位深扩展合并一次升版）

- §10 解冻"edit list"条目中**音频 trak 最小子集**（视频 trak elst 仍非
  目标、§3 pts 无空洞约束不动）。
- 附录 A 增补 edts/elst 布局表与校准规则引用。
- §5 支持矩阵：采样率 **44100/48000/88200/96000/176400/192000 全家族**
  （对齐 Apple 交付规范，ProRes 对标 §0.5）；位深 16/24/32(int)/**32(float)**
  （formatFlags 语义表见 §2.6）。
- 多音轨（M-B8）与 tmcd（M-B9）为更大的容器结构变更，各自落地时升版：
  **先落者取 v1.5、后者 v1.6**（两里程碑相互独立，实施顺序见 §6）。

### 2.6 float32 lpcm（pcm_f32le）声明与 formatFlags 语义（随 v1.4）

中间片纯度的音频对齐物：母带浮点直通、零量化、往返逐位相等。

- **声明槽位**：`topos_movie_config.reserved[8]` 现音频占 `[0..4]`、
  注释"reserved[5..7] 保持 0"——本设计启用 `reserved[5]` 为**样本格式
  标志**（位掩码：bit0 = float32；0 = 整数，旧调用方零值语义与旧文件
  读取行为逐字节不变）。C 校验：float 仅在 lpcm + bits==32 下合法。
- **formatFlags**（lpcm 扩展，`build_audio_stsd`）：整数维持 0xE
  （signed|BE|packed）；float 写 0xB（float|BE|packed，去 signed）——
  QT/CoreAudio 标准形态（kAudioFormatFlagIsFloat=0x1），ffprobe 识别为
  `pcm_f32be`。
- **demux 暴露**：formatFlags float 位 → `topos_audio_track_info` 增
  `sample_format`（经 info 的 reserved 槽暴露，0=int/1=float）。
- **读路径**：`'>f4'` 大端直读、零转换；float 数组原样上行（不 clip、
  不重标定——值域可能超 ±1.0，这是特性而非错误）。
- **写路径**：`pcm_f32le`（别名 `pcm_f32`）→ float32 → '>f4' 字节序
  直存，**无 clip 无 rint**；NaN/Inf 显式 fail-fast 拒绝（防写出损坏
  文件，与"音频渲染失败 = 导出失败"语义一致）。`pcm_s32le` 同批补齐
  （clip+rint→i32，消除"容器可存可读却无写入口"的悬空能力）。
- **e2e 特性**：float 链路**无任何有损步骤**（渲染 float → 文件 →
  ffmpeg 解码 float），e2e 可断言与母带**逐位相等**（仅 BE↔LE 字节序
  换序）——比 int 档更强的回归断言。

### 2.7 tmcd 时间码轨设计（随规范 v1.5，M-B9；专业中间片必备）

ProRes/FCP 标配能力；MP4 容器不支持 tmcd（重封装即丢）——这是 MOV
族中间片对"回剪辑软件按 TC 对位"的独有优势。**字段语义以 QT 规范为准，
字节布局以 `ffmpeg -write_tmcd` 产物逐字节对拍为准**（oracle 定字节、
规范定语义）。

**原子布局**（mux 新增写入，demux 新增解析）：

```
trak（轨 id = 视频/音频轨之后；tmcd 恒为末轨——与 M-B8 多音轨的
│        轨序约定在此钉死：video=1、audio=2..N+1、tmcd=N+2）
├─ tkhd（flags 含 enabled/in-movie；无几何、无音量）
├─ mdia
│  ├─ mdhd（timescale 与帧率/视频轨的关系按 ffmpeg 产物钉死；单样本）
│  ├─ hdlr（'tmcd'）
│  └─ minf
│     ├─ gmhd（通用媒体头：gmin + tcmi 时间码显示元数据；ffmpeg 是否
│     │        写以对拍为准，可省——播放器不依赖）
│     └─ stbl
│        ├─ stsd → 'tmcd' 条目：
│        │   ├─ 保留 6B + data_ref_index 2B
│        │   ├─ flags       u32 —— bit0=drop frame、bit2=负时码允许、
│        │   │                bit3=24h 回绕
│        │   ├─ time_scale  u32（标称 fps：23.976→24、29.97→30…）
│        │   ├─ frame_duration u32（每帧的 media 时间单位数）
│        │   └─ frames_per_second u8 + 保留
│        ├─ stts（单条目，duration = 整片时长）
│        ├─ stsc / stsz（恒 1 样本 × 4B）/ stco
│        └─ 样本数据：i32 帧计数（相对时码原点，BE；负值需 flags bit2）
```

**API**（独立 setter，不占 reserved 槽位——时码与音频声明正交）：

```c
int32_t tc_mux_set_timecode(topos_mux* m, uint32_t hh, uint32_t mm,
                            uint32_t ss, uint32_t ff, uint32_t fps,
                            uint8_t drop_frame);   /* finish 前，至多一次 */

typedef struct topos_timecode_info {
    /* struct_size / abi_version 惯例 */
    uint32_t start_frame_count;   /* 时码原点起算的帧计数 */
    uint32_t fps;                 /* 标称 fps */
    uint8_t  drop_frame;          /* DF 标志 */
    uint8_t  flags;               /* tmcd flags 原样 */
    int32_t  hh, mm, ss, ff;      /* 起始时码（C 侧换算完成）*/
} topos_timecode_info;
int32_t tc_movie_timecode(const topos_movie* mv, topos_timecode_info* out);
```

**校验（fail-fast，与既有风格一致）**：hh<24、mm/ss<60、ff<标称 fps；
fps 白名单 {24, 25, 30, 48, 50, 60}（分数帧率映射：23.976→24 NDF、
29.97→30、59.94→60，DF 可选）；**DF 仅标称 30/60 合法**（其它帧率开 DF
→ INVALID_ARGUMENT）；负时码本期不支持（flags bit2 恒 0，写侧拒绝）；
无视频轨声明时码 → 拒绝（tmcd 依附视频轨时长）。

**逐帧时码推导**：容器只存单样本（4B，O(1) 无逐帧表）；TC(n) = start
+ n 帧推进，DF 规则 = "每分钟跳 frame 0/1、每 10 分钟整不跳"。推导
helper 放绑定层（`ToposMovieFile.frame_timecode(index)`），配 DF 边界
单测表（如 29.97 DF：`00:00:59;29 → 00:01:00;02`）。

**兼容**：无时码文件零字节变化（tmcd trak 整体缺席）；`next_track_id`
联动递增；v1.1/v1.3/v1.4 旧文件读取行为不变。

---

## 3. 里程碑分解（依赖顺序，每步可独立验收）

### M-B0 跨播放器互操作验证（先行，1 天）

放在 M-B1 之前：若某播放器拒绝我们现有 `lpcm` 扩展/`chan` 标签/无 `wave`
的 `esds` 写法，对策（如 esds 加 wave 包裹）要并入 M-B1 一起改，避免
规范二次升版。

- 任务：
  1. 生成产物矩阵：`lpcm` 16/24/32bit × 2.0/5.1/7.1 × 44.1k/48k ×
     标准/faststart + `mp4a` 2.0/5.1（48k）≈ 20 个样本（复用
     `test_mov_audio --dump` 产物生成器）。
  2. 播放器矩阵（按本机可用性执行，不可用记 N/A 不阻塞）：
     - 脚本层：ffprobe/ffmpeg（本机）+ VLC `--intf dummy` 转码、
       mpv `--ao=null` 播放日志（均可 CI 化）。
     - 手动清单：QuickTime Player（AVFoundation）、IINA、DaVinci Resolve
       导入、Adobe Premiere 导入、Windows Media Foundation（Movies & TV，
       如有 Windows 环境）。
  3. 检查项：流被识别（codec/声道/采样率/位深）、lpcm 解码逐位等价
     （经 ffmpeg 重解码 MD5 对拍）、时长一致、无崩溃。
- 验收：`docs/codec/topos_audio_interop_report_2026-*.md` 报告入库——
  每个组合一行结论；**每个"拒绝/异常"项必须有对策条目**（并入 M-B1 或
  另记 known-issue）。

### M-B1 elst/priming 落地（2-3 天，核心）

- 任务：
  1. **priming 校准**（§2.2 步骤 2）：确定 X 并在 ADR-C052 记录根因
     （编码器自报 1024 vs 实测 2048 的差额来源）。
  2. ADR-C052 定稿（决策：elst 最小子集、校准值、lpcm 恒零、视频轨仍禁）。
  3. C 层：mux 写 `edts/elst`（buf_atom 风格）+ `tc_mux_set_audio_priming`；
     demux 解析与 `priming_samples` 暴露；畸形 elst 拒绝路径。
  4. 绑定：`ToposMuxFile.set_audio_priming` /
     `ToposAudioTrackInfo.priming_samples`（fail-fast 新符号）。
  5. 编码器：`_aac_encode_pcm` 帧喂顺序 pts → 首包 pts 提取
     `priming_enc` → `set_audio_priming(X)`；PCM 恒不调用。
  6. 测试改造：`test_topos_audio_container.py` AAC 用例去掉 2048 硬编码
     → 改用 `info.priming_samples` 对齐并断言 |最优 lag − priming| ≤ 1；
     e2e（`test_topos_export_audio.py`）同步。
- 验收：
  - C 单测：elst roundtrip（priming 读回相等）、越界/零值/lpcm 调用被拒、
    畸形 elst（entry_count≠1、media_time≥总采样、media_rate≠1.0、视频轨
    带 elst）→ TC_ERR_MALFORMED；
  - ffprobe 断言音频流 `start_time ≈ priming/采样率`；
  - **端到端零手动对齐**：AAC 导出 → ffmpeg 解码 → 互相关最优 lag 与
    priming 差 ≤ 1 采样；
  - **M-B0 矩阵 AAC 组合复跑同步项**（elst 落地前后各一遍：前测=无 elst
    基线，哪些播放器自行补偿；后测=elst 生效验证——已知部分播放器忽略
    elst，§0.1 证据）；
  - 纯视频 conformance golden 字节不变；
  - 规范 v1.4 + 附录 A + ADR-C052 + ADR-INDEX 注册。
- 提交拆分建议：`feat(codec): elst mux/demux` → `feat(codec): 绑定+编码器
  接线` → `docs(codec): spec v1.4 + ADR-C052`。

### M-B2 音频 fuzz 集成（1 天）

- 任务：
  1. `seed_corpus` 增加带音频 mov 种子（M-B0 产物精选 3-5 份：lpcm/mp4a/
     faststart/畸形修复前样本）。
  2. `fuzz_mov.c` 不变量扩展：open 成功且有音轨 → `audio_info` 自洽
     （sample_count ≥ 1；chunk_count 与 stsc/stsz/stco 互相一致）；
     `tc_movie_read_audio` 探测（probe）不越界。
  3. 长跑：`-gen` 确定性回放 + 生成语料 N 万级（沿用现有 corpus_replay
     driver 用法）；崩溃/挂起/未定义返回码归零。
- 验收：长跑报告数字回填本计划 §7；发现的问题逐个修复（畸形必
  `TC_ERR_MALFORMED`，不得静默成功）。

### M-B3 再导入回放（1-2 天）

- 任务：
  1. **调研**（半天）：`AudioDecoder.open(path)` 的调用链——播放栈拿到
     topos .mov 时传的是源路径还是经 source 抽象？三种结论对应三种工作量：
     (a) 已直通 PyAV → 仅验证 + probe 元数据接线 + 文档；
     (b) 路径可达但断言/分支挡住 topos → 开分支（PyAV mov 通用 demux
     可直接读双轨，v1.1 已证）；
     (c) 需要桥接 → `read_audio_samples` 适配层。
  2. ADR-C008 边界修订：**再导入的导出文件即 master**——回放读它的音轨
     不违反"回放音频取 master"；渲染缓存/代理仍纯视频（不变）。
  3. AAC 再导入依赖 M-B1 的 priming 跳过（PyAV demux 会自动应用 elst——
     ffmpeg mov demuxer 行为，校准测试顺带断言）。
- 验收：导出（PCM 2.0 + AAC 5.1）→ 再导入 → `AudioDecoder` 能开且解码
  非静音；lpcm 与源逐位相等、AAC 容差（互相关 >0.9）；时间线插入播放
  smoke 测试；ADR-C008 修订段 + 白皮书同步。

### M-B4 采样率与位深扩展：全家族采样率 + float32 + 采样率 UI 收敛（1 天）

- 任务：
  1. C 层：采样率白名单一步收**全家族** 44100/48000/88200/96000/176400/
     192000（`mov.c:250`，对齐 Apple 交付规范，ProRes 对标 §0.5）；
     `reserved[5]` 样本格式标志 + lpcm formatFlags float 位（写 0xB /
     读回，§2.6）；`audio_cfg_validate` 增 float 校验（仅 lpcm+32bit）；
     `topos_audio_track_info` 暴露 `sample_format`。
  2. 绑定：`movie_config(..., audio_sample_format=...)`；
     `ToposAudioTrackInfo.sample_format`；读路径 `'>f4'` 直读。
  3. 编码器：`_AUDIO_CODEC_MAP` 增 `pcm_f32le/pcm_f32`（float 直通、
     NaN/Inf fail-fast）与 `pcm_s32le`（clip+rint→i32，消除悬空能力）。
  4. UI：topos 音频 codec 列表增 `pcm_f32le`（tooltip 文案补"母带级
     浮点直通"档位说明：s16 草稿 / s24 标准 / f32 母带）；采样率下拉
     增 88.2/176.4/192 kHz 档；**采样率下拉按容器收敛**（与 codec 下拉
     同机制 `_sync_audio_codec_choices_for_codec` 一并处理采样率——
     本里程碑落地后 topos 支持全家族，收敛机制为未来容器能力差异
     兜底，并消除"可选但导出必败"状态）。
  5. 测试与规范：44.1k 补全档覆盖；`topos_profiles.py` 同步；规范
     v1.4 §5 支持矩阵 + 附录 A formatFlags 语义表；**决策记录**：位深
     由 codec 名承载（不设独立选择器）、32bit 整数选择暴露（而非从
     白名单收敛移除）。
- 验收：
  - float32：C mux→demux 逐字节 roundtrip + `sample_format` 读回；
    **e2e 逐位相等**（母带 float → 导出 → ffmpeg 解码 → 与母带完全
    一致，零量化，仅 BE↔LE 换序）；ffprobe 报 `pcm_f32be/48000/2ch`；
    NaN/Inf 母带 → 导出 fail-fast 报错且不产出文件；
  - 采样率：96k/192k lpcm 2.0/5.1 roundtrip + ffprobe 读回；44.1k
    全档 roundtrip；88.2/176.4 白名单+ffprobe 抽验；
  - UI：topos 档下采样率下拉与实际能力一致（不再出现导出期才拒绝）；
  - 旧值回归全绿；纯视频 golden 字节不变（reserved[5] 只在音频声明
    时非零）。

### M-B5 分段并行 + 音频 ADR（决策 0.5 天；实现 2-3 天可另立计划）

- 任务：三方案对比并决策（ADR-C053）：
  - **方案 A**：native 追加轨工具——读已 finish 文件的 moov，追加音频
    trak + 音频 mdat 分片 + 重写 moov（我们自产文件布局已知，可行性高；
    工作量最大，但通用价值最高——未来任何"后配音频"场景复用）。
  - **方案 B**：段外独立 pass——分段并行只管视频，音频渲染/编码独立
    完成，结束后走方案 A 的追加工具合并（= B 依赖 A 的核心件）。
  - **方案 C**：维持互斥 + 文档化（零工作量，用户二选一）。
  - 初步倾向：A 的"moov 重写 + 追加"核心件 + B 的编排（导出期不占额外
    峰值内存）；但 ADR 阶段以工作量实测为准。
- 验收：ADR-C053 定稿（含工作量估计与实施计划指针）；实现是否本期做
  由 ADR 结论决定（默认另立计划）。

### M-B6 音频 golden + CI 门禁（1 天）

- 任务：
  1. **严格排在 M-B1 之后**（elst 落定再冻结，避免 golden 空转）：
     冻结 lpcm 2.0 + mp4a 5.1 两份字节级 golden（含 elst），复用
     `golden_mov_v1.bin` 的冻结机制与四配置门禁。
  2. CI：核对三平台 CI 现跑范围（查 `.github/workflows` 或等价物），
     把 native ctest（含新增音频用例）、`tests/media` 音频三件套、
     `tests/edit` 导出音频用例纳入必绿门禁。
- 验收：golden 字节冻结生效（写坏即测试红）；CI 配置合入且三平台
  （Linux/macOS universal/Windows）至少一轮绿。

### M-B7 FFmpeg libtopos 补丁侧双轨回归（0.5 天）

- 任务：`packaging/ffmpeg`（白皮书规划的官方补丁仓）：验证 libtopos
  解码器 + mov 通用 demux 组合能读双轨文件（`ffplay` 播放带音轨 topos
  mov / `ffmpeg -c:v libtopos -i x.mov -map 0:a -f wav -` 解码）；
  补丁仓 README 增音频轨说明。
- 验收：补丁仓脚本一键回归绿；文档更新。lpcm/esds 在 ffmpeg 侧走通用
  mov demux 路径（无 libtopos 依赖），预期零补丁改动——如实测不符，
  问题记录回 M-B0 报告。

### M-B8 多音轨 / stems 角色轨（2-3 天，ProRes 对标最大差距，规范 v1.5）

FCP 的标准中间片形态："Roles as Multitrack QuickTime"——按角色（对白/
音乐/音效）每角色一条离散轨（A1/A2/A3/A4…），每轨独立 mono/stereo；
广播交付 8 声道/8 mono 常见（§0.5 调研）。这是"中间片把 stems 带在
文件里往返剪辑软件"的核心能力。

- 任务：
  1. ADR-C054 定稿：API 形态与容器结构。
     - **声明形态**：reserved 槽位不足以声明 N 轨（仅剩 [6..7]）且
       ProRes 实践每轨格式独立（mono/stereo/f32 混合）→ 弃"槽位扩容"
       改**逐轨声明 API**：`tc_mux_add_audio_track(format…) → track
       handle`，`tc_mux_add_audio(track, data, size, num_samples)`；
       v1.1 单轨族 API（`tc_mux_add_audio` 等）保留为"轨 0"糖衣，
       语义不变（向后兼容视图）。
     - **轨命名**：QuickTime track name（trak/udta/©nam）承载角色名
       （"Dialogue"/"Music"/"Effects"），Resolve/Premiere 据此显示。
     - mux 状态机数组化：`a_*` 单轨字段 → 轨数组（trak 写入循环、
       mdhd timescale 各轨独立、mvhd duration = max、stss 不涉音频）。
     - 轨数上限（建议 ≤16，QT 惯例）与 fail-fast 校验。
  2. demux：M-A2 已按 handler 遍历分发（天然多轨）——扩展 API：
     `tc_movie_audio_track_count` + `tc_movie_audio_info(track_idx)` +
     `tc_movie_read_audio(track_idx, …)` + 轨名读出；**旧单轨 API 映射
     第一条音轨**（v1.1 文件行为逐字节不变）。
  3. 绑定 + 媒体层：`ToposAudioTrackInfo` 列表化；probe 元数据
     `audio_tracks: [AudioTrackInfo…]`（旧 `has_audio/channels/…` 字段
     保持为首轨视图，兼容既有消费方）。
  4. 导出接线：时间线音轨/角色 → 输出轨映射 UI（每轨独立 codec/声道/
     格式）；`TimelineExporter` 音频渲染按角色分组渲染多份母带（复用
     `_render_audio_master`，按轨过滤）。
  5. 测试 + 规范 v1.5 + 白皮书 + ADR-C054。
- 验收：
  - C：N 轨（含混合格式：f32 立体声 + s16 mono + AAC 5.1）mux→demux
    逐字节 roundtrip、轨名读写一致；v1.1 单轨文件经新 API 读出 = 旧 API
    视图；轨数超限/半声明 fail-fast；
  - ffprobe 多音轨识别（轨数/各自格式/声道）；**Resolve/Premiere 导入
    显示多轨且轨名正确**（手动清单，M-B0 矩阵扩展）；
  - e2e：双角色（对白+音乐）时间线导出 → 每轨独立解码与各角色母带
    逐位/容差对拍；单轨导出路径回归不变；
  - 互操作：多轨产物进 M-B0 矩阵复跑（重点：各播放器轨序/轨名呈现）。

### M-B9 tmcd 时间码轨（1-2 天，规范 v1.5 先落者，专业中间片必备）

ProRes/FCP 文件普遍携带 tmcd 轨承载起始时码；MP4 容器不支持 tmcd
（重封装即丢）——MOV 中间片对"回剪辑软件按 TC 对位"的独有优势（§0.5）。
属容器层功能（非音频，但同属"编辑往返友好性"）；设计定稿见 **§2.7**
（原子布局 / API / 校验规则 / DF 推导语义）。

- 任务：
  1. 调研（半天）：时间线/序列元数据是否已有起始 TC 与项目帧率语义
     （尤其 23.976 的内部表示）；`ffmpeg -write_tmcd` 产物逐字节 dump
     作字节级 oracle（tkhd flags、mdhd timescale、gmhd 有无、stts
     duration、样本 4B 全对齐，差异逐项记录）。
  2. ADR-C055 定稿：§2.7 设计转正式决策——字段语义以 QT 规范为准、
     字节布局以 ffmpeg oracle 为准；轨序约定（tmcd 恒末轨）与 M-B8
     的轨 id 分配规则在此钉死。
  3. C 层：mux `tc_mux_set_timecode` + tmcd trak 写入（buf_atom 风格，
     含 next_track_id 联动）；demux `tc_movie_timecode`（解析 tmcd
     stsd flags/time_scale/帧计数；无 tmcd → TC_ERR_STATE）；
     畸形 tmcd（样本≠4B、fps 越界、DF 与 fps 不匹配、stts 多条目）
     → TC_ERR_MALFORMED。
  4. 绑定/媒体层：`ToposMovieFile.timecode()` +
     `frame_timecode(index)` 逐帧推导 helper（DF 跳帧规则实现 + 边界
     单测表）；probe 元数据 `timecode_start / timecode_fps /
     timecode_drop_frame`（`VideoMetadata` 扩展）。
  5. 导出接线：时间线起始 TC（有则写，无则不写——零字节变化原则
     维持）。可选任务（记录决策即可）：素材代理转写保留源文件起始
     TC——代理与母片 TC 一致方便编辑对位（tmcd 非音频轨，不触碰
     ADR-C008 的代理纯视频边界）。
  6. 测试 + 规范升版（附录 A 增 tmcd 布局表；v1.5/v1.6 归属见 §2.5）
     + ADR-C055 + ADR-INDEX + 白皮书"专业中间片能力"段落。
- 验收：
  - C：`tc_mux_set_timecode` → 写入 → `tc_movie_timecode` roundtrip
    （覆盖 fps×DF 组合矩阵：24/25/30/48/50/60 × NDF、30/60 × DF）；
    校验拒绝（hh≥24、ff≥fps、DF 配 24/25fps、无视频轨）；无 TC 文件
    字节不变；
  - 字节级对拍：与 ffmpeg `-write_tmcd` 产物在 tkhd/mdhd/stsd 语义
    字段逐项一致（gmhd/udta 类可选原子允许差异，差异表入 ADR）；
  - ffprobe `format.tags.timecode` 读回一致；**mov→mov copy 保留、
    mov→mp4 丢失**（语义对照断言，证明 tmcd 为 MOV 族独有）；
  - DF 换算正确性：29.97 DF 边界用例（`00:00:59;29 → 00:01:00;02`
    跳帧规则单测表；每 10 分钟整点不跳）；
  - Resolve/Premiere 手动清单：素材起始 TC 对位正确（M-B0 矩阵扩展）；
  - e2e：带起始 TC 的时间线导出 → 再导入 → TC 读回一致；任意帧号
    `frame_timecode(n)` 推导与 (起始 TC + n/fps) 一致。

---

## 4. 测试与回归总表

| 层 | 文件 | 关键断言 |
|---|---|---|
| C 单测 | `native/topos_codec/tests/unit/test_mov_audio.c`（扩展） | elst roundtrip；priming 校验拒绝；畸形 elst → MALFORMED；96k/44.1k roundtrip；f32 formatFlags 写读一致 + sample_format 读回 |
| 位深/采样率扩展 | `test_mov_audio.c` + `test_topos_audio_container.py`（扩展） | f32/s32 逐字节 roundtrip；**f32 e2e 逐位相等**（零量化）；NaN/Inf fail-fast；96k/44.1k 全档 |
| C fuzz | `tests/fuzz/fuzz_mov.c`（扩展） | 音频种子长跑；audio_info 自洽不变量；无崩溃/挂起 |
| C golden | `tests/conformance/`（M-B6 新增） | lpcm/mp4a 双 golden 字节冻结；纯视频 golden 不变（M-B1 起每轮钉） |
| 绑定 | `tests/media/test_topos_audio_container.py`（改造） | priming 读回；**去 2048 硬编码**，info 对齐断言 ≤1 采样 |
| 媒体/回放 | `tests/media/test_topos_source_audio.py` + 新回放用例 | 再导入 probe；AudioDecoder 非静音；lpcm 逐位/AAC 容差 |
| e2e | `tests/media/test_topos_export_audio.py`（改造） | AAC 导出零手动对齐；ffprobe start_time；96k 档 |
| 互操作 | `docs/codec/topos_audio_interop_report_*.md`（M-B0） | 播放器×产物矩阵；拒绝项有对策 |
| 多音轨/stems | `test_mov_audio.c` + `test_topos_audio_container.py` + 新导出用例（扩展） | N 轨混合格式逐轨 roundtrip；轨名读写；v1.1 单轨兼容视图；Resolve/Premiere 多轨显示（手动） |
| tmcd 时间码 | 同上扩展 | tmcd 写读 roundtrip；fps/DF 标志；mov→mov 保留 / mov→mp4 丢失对照；ffprobe 读回 |
| 回归 | `tests/media -k topos` / `tests/edit` 导出音频 / ctest | 全绿；纯视频解码结果不变 |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| priming 校准值 X 跨 PyAV/ffmpeg 版本漂移 | X 为命名常量 + 校准单测钉死（升级先红后改）；elst 值本身随文件携带，旧文件不受影响 |
| 某主流播放器拒 lpcm 扩展/无 wave 的 esds（M-B0 发现） | 对策并入 M-B1 一次改完（如 esds 加 `wave` 包裹、chan 标签换 side 变体）；规范 v1.4 内吸收，避免二次升版 |
| elst 语义与各播放器 skip 行为不一致（同 X 不同残差） | M-B0 矩阵含 AAC 同步项（拍手对齐/互相关抽检）；以 ffmpeg 家族 + QuickTime 双基准，残差 >1 采样记 known-issue 并在 ADR-C052 列播放器差异表 |
| fuzz 种子覆盖不足（字节级变异打不中音频表结构） | 种子用自产 writer 产物（真实布局）+ 手工畸形样本；必要时对 moov 区做定向变异（现有 driver 支持确定性回放） |
| M-B3 调研发现播放栈深层耦合（远超接线量） | 调研结论先行记录，实现量超 2 天则拆独立计划，本计划仅收 ADR-C008 修订与结论 |
| golden 冻结后 elst/规范又要改 | M-B6 严格排在 M-B1（及 M-B0 对策吸收）之后 |
| moov 追加重写（M-B5 方案 A）破坏 faststart 语义 | ADR 阶段专门列 faststart 兼容小节；自有 writer 布局已知，co64/32 位偏移分档处理 |
| float 母带含 NaN/Inf（上游渲染异常混入） | 写路径 fail-fast 拒绝并给诊断——导出失败而非写出损坏文件（与"音频渲染失败 = 导出失败"语义一致）；e2e 用例含此负路径 |
| f32 直通后 >0dBFS 样本进入下游（播放器削波） | 值域超 ±1.0 是中间片特性（HDR 对齐物）；tooltip 文案明示"母带档"；交付链路仍走 AAC/int 档（文档与 UI 定位一致） |
| 位深选项增多（s16/s24/s32/f32/aac）造成选择负担 | tooltip 三档定位文案（草稿/标准/母带）；s32 仅为整数工作流保留，排在 f32 之后 |
| M-B8 mux 单轨状态机数组化重构引入回归（v1.1 字段 → 轨数组） | 分两步走：先纯重构（单轨行为逐字节不变，golden+全量回归钉死）再叠加多轨；旧单轨 API 保留为轨 0 糖衣，v1.1 文件读取兼容视图有专项测试 |
| M-B8 各播放器轨序/轨名呈现不一致（ProRes 生态同样有轨标签痛点） | 轨序=声明序（确定性）；轨名走 QT 标准 ©nam；M-B0 矩阵扩展手动清单覆盖 Resolve/Premiere |
| M-B9 tmcd 语义错（drop-frame/负片标志/fps 与时间线不符） | ffmpeg `-write_tmcd` 产物作 oracle 对拍；DF 只在 29.97/59.94 合法（校验拒绝其它帧率开 DF）；Resolve 手动对位清单 |

---

## 6. 建议实施顺序与工作量

```
M-B0（1d）→ M-B1（2-3d）→ ┬ M-B2（1d）┬ → M-B3（1-2d）→ M-B6（1d）→ M-B5 ADR（0.5d）→ M-B7（0.5d）→ M-B8（2-3d）→ M-B9（1-2d）
                          ├ M-B4（1d）┘
                          └ M-B9（1-2d，可提前：独立于音频里程碑，仅依赖 M-B1 的 moov 写入机就绪）
```

- M-B0 必须先行（互操作结论影响 M-B1 的原子写法）。
- M-B2/M-B4 可与彼此并行（都不动 elst 语义；M-B4 的规范升版与 M-B1 合并，
  故 M-B4 提交顺序上排在 M-B1 后）。
- M-B6 必须排在 M-B1 之后（golden 冻结时机）。
- **M-B9（tmcd）为专业中间片必备且完全独立**（视频/容器层，不碰音频
  mux 状态机）——如需尽早交付时间码能力，可在 M-B1 后提前实施
  （规范版本归属按"先落者取 v1.5"规则，见 §2.5）。
- **M-B8 排在 M-B1..M-B7 全部之后**：多轨是 mux 状态机数组化重构
  （两步走：纯重构 → 叠加多轨，见 §5 风险），早做会放大后续每个里程碑
  的回归面；elst/priming 在多轨下逐轨生效，先定单轨语义再扩轨更稳；
  tmcd 恒末轨的轨序约定已与 M-B8 钉死（§2.7），先后落地互不阻塞。
- 总计约 **11.5-15 个工作日**（M-B5 实现若立项另加 2-3 天）。
- 纪律沿用 v1.1：每里程碑独立提交（`feat(codec)/feat(media)/feat(edit)/
  docs(codec)/test(codec)`），验收数字回填 §7；M-A 系列的全部 fail-fast
  与"纯视频解码结果不变"约束继续有效。

---

## 7. 实施记录（占位，随里程碑回填）

| 里程碑 | 提交 | 验收结果 |
|---|---|---|
| M-B0 互操作 | `41eb47ea` | **✅ 完成**——两处 Critical 缺陷修复（esds 规范长度+同级链；mp4a wave 包裹+chan+0xFFFE），三硬引擎 40/40 全绿（ffprobe/ffmpeg 逐位/AVFoundation）；known-issue：VLC 3.0 'lpcm' 家族不支持（K1）、CoreAudio AudioFile API 多轨 MOV 浅支持（K2）；报告 `topos_audio_interop_report_2026-09-20.md` |
| M-B1 elst/priming | `bf9b5e94` | **✅ 完成**——校准推翻计划期假设：FFT 精测端到端延迟恰=1024（=priming_enc），elst=1024 残差 0（norm-xc 0.9996）；2048 系弱相关窗口法伪影（ADR-C052 §1）。C elst roundtrip/API 校验/6 组畸形拒绝；skip_samples==media_time 精确断言；互操作 AAC 组带 elst 复跑 18 PASS/0 FAIL；规范 v1.4（A.2 修正 + A.5）+ ADR-C052 |
| M-B2 fuzz | `f07356d7` | **✅ 完成**——fuzz_mov 音频不变量（audio_info 自洽/priming 有界/读取探测/越界拒绝/faststart 元数据一致）+ 5 个音频种子入库（gen_audio_seeds.py 确定性生成）；修复 status_is_defined 缺 TC_OK（CI 语料从未走通成功分支，首个合法文件即暴露）；种子回放 ctest fuzz_mov_audio_seeds；长跑 -gen 50000 零崩溃/挂起/未定义（0.1s） |
| M-B3 再导入回放 | `92467b48` | **✅ 完成**——调研结论 (a)：AudioDecoder.open 纯 PyAV 直开路径无挡板，probe 元数据 v1.1 已接线，零接线工作量；新 `tests/media/test_topos_reimport_playback.py`（生产编码器导出→AudioDecoder 回放：lpcm 16bit 域逐位相等、AAC 5.1 非静音+互相关>0.9 零手动对齐/decode_next_samples 连续解码 smoke）2/2 绿；ADR-C008 C-62 修订（再导入文件即 master，代理仍纯视频）+ 白皮书同步 |
| M-B4 96kHz | `3cb4aa0d` | **✅ 完成**——采样率全家族 {44.1/48/88.2/96/176.4/192}kHz（V0/V1 16.16 上限 65535Hz → >65535 走 SoundDescriptionV2（sampleRate f64，movenc 同构），ffprobe 读回正确）；float32：reserved[5] 格式标志 + formatFlags 0xB + **f32 恒走 V2**（ffmpeg/VLC 不读 V0/V1 float 位，实测识别为 pcm_s32be）+ '>f4' 直读直存（NaN/Inf fail-fast，e2e 逐位相等）；pcm_s32le 补齐悬空能力；绑定 movie_config(audio_sample_format) + ToposAudioTrackInfo.sample_format；UI codec 四档+采样率全家族下拉按容器收敛+tooltip 三档定位；profiles 能力面同步；C 88/88、media topos 1201、edit 1331 全绿 |
| M-B5 分段并行 ADR | docs(codec) ADR-C053（随 M-B7 批次提交） | **✅ 完成（决策定稿）**——采纳方案 B（段外独立 pass + native moov 重写追加轨核心件），实现另立计划；过渡期维持 fail-fast + 文案指向 ADR；验收锚点钉死 |
| M-B6 golden + CI | `8a5ae34c` | **✅ 完成**——golden_mov_audio_v1.bin 冻结（5 用例：lpcm s16/f32-V2/s24-88.2k-V2/mp4a+elst/mp4a 5.1+elst faststart，摘要含 sample_format/priming）；conformance_mov_audio 入 ctest；CI codec-matrix 补音频四件套 |
| M-B7 FFmpeg 补丁侧 | `447ac1e2` | **✅ 完成**——实测确认音轨走通用 mov demux（stock ffmpeg 双轨识别+lpcm 逐位，零补丁改动，M-B0 矩阵互证）；`packaging/ffmpeg/audio_regression.sh` 一键回归（含补丁构建组合验证位）；README 增音轨回归段；附带发现：回退 ASC `12 10` 实为 44.1k 立体声（ffprobe 按 ASC 报 44100 正确，v1.1 注释小误差记录在案） |
| M-B8 多音轨/stems | `2b3afc66`/`22e99f92`/`5b3f2e4f` + 导出侧批次 | **✅ 完成（容器/绑定/探测/导出侧全量）**——两步走执行：第一步纯重构（mux a_* 字段数组化，golden 字节冻结钉死逐字节不变）；第二步多轨叠加（逐轨声明 API + mono 布局解冻 + 轨名 ©nam + demux ≤16 轨分派 + 旧 API 恒轨 0 视图 + faststart 逐轨重定位）；三轨混合格式（f32 立体声 + s16 mono + mp4a 5.1）逐字节 roundtrip、ffprobe 识别 3 轨/格式/轨名；probe audio_tracks 逐轨元组（旧字段=首轨视图）；规范升 v1.6（附录 A.7）+ ADR-C054。**导出侧（ADR-C054 §4 已实施）**：`TimelineExportConfig.audio_stems`（每轨 stem_group/轨名/codec/声道/布局）→ 交付作业 `AudioConfig.stems`（StemTrackConfig 快照，旧 schema 兼容）→ 编码器 `add_audio_track_stream`/`write_audio_track_samples`（轨 0=主混音兼容视图不变）；`_render_audio_master(only_track_ids/channels_override)` 同链按角色过滤（loudness 仅主混音）；Deliver 面板 Stems 开关 + 逐组 codec/声道映射（topos 档门控）；空组声明前剔除、不支持编码器 all-or-nothing 降级单轨；e2e：双角色时间线 → master==Σ(stems) 对拍 + 逐轨逐位一致 + ffprobe 轨名，fail-fast 四态 + 单轨回归（test_topos_stems_export 8 例） |
| M-B9 tmcd 时间码 | `b2569ca5` | **✅ 完成**——C 写读全链（mux 恒末轨 + tref + 4B 计数样本；demux fail-fast 形态校验 + tc_movie_timecode DF 逆换算）；三处 oracle 实证修正：stsd entry 36B（dri 后保留 u32，缺它 ffmpeg demux 元数据错乱）、NTSC DF 网格 ts=标称×1000（非 ×1001）、**60DF 跳 4 帧**（SMPTE 12M，oracle 00:01:00;04→3600；30DF 跳 2；与 media 层 Timecode 同规，计划期"每分钟跳 2"通式被修正）；修复 5 处实施缺陷（stsc 缺描述符索引、dinf dref 双 close、demux flags/fps 偏移、faststart b.len 释放后回读 + co64 循环 moov 追加）；绑定 timecode/frame_timecode/format + probe 元数据（timecode/fps/drop_frame）+ 导出接线（序列 start_timecode → encoder，有则写/无则不写，域外降级）；ffprobe stream tag 逐字符一致 + mov→mov 保留/mov→mp4 丢失语义对照（movenc 重封装 K2/K3/K4 注记入 ADR）；C 89/89、绑定 17 例、媒体+导出 134 例全绿；规范升 v1.5（附录 A.6）+ ADR-C055（差异表 K1-K5）+ 白皮书 |
| **复查 2026-09-20**（本表全量核验） | 见下方明细 | **发现并修复 12 项隐藏缺陷**——计划声明主体成立（提交/ADR/golden/测试齐备，C 89/89、媒体/导出测试全绿），但存在声明外的隐藏 bug：①s24 lpcm 写路径 F 连续 view 崩溃（默认音频档导出必败，从未被 e2e 解码校验）；②pcm_s32le 量化分支缺失（档位可选但导出必败）；③短 AAC（≤1024 采样）priming ≥ 总采样被 C 拒→导出必败 + flush 期首包 pts 漏提取；④`tc_mux_free` 仅释放轨 0（多轨 mux 内存泄漏，stems 每导出必触发）；⑤tmcd demux 缺 stco/stts/stsz 静默通过（违背规范 A.6 fail-fast）+ co64 不识别 + 写侧 >4GB stco 截断；⑥media 层 Timecode DF 逆换算块首分钟按丢帧分钟计（:59 尾部 2/4 帧 ValueError，UI 回退错误 NDF）；⑦静音角色轨 stems：声明/渲染过滤口径不一致→finish 必败（常见操作触发）；⑧互操作矩阵/回归脚本/fuzz 种子三处 `0.5·sin→int` 全零截断（对拍断言空转）；⑨fuzz 多轨/tmcd API 零不变量 + 变异 fuzzer 语料错配（喂位流语料）；⑩绑定 add_audio_track 返回索引 off-by-one；⑪preflight 缺 pcm_f32le + UI docstring 回落说明失实；⑫CI 音频门禁缺口（stems/多轨/tmcd 测试未纳入、Python 接线路径不触发）。全部修复并配回归测试（s24/s32 逐位 e2e、短 AAC、DF 边界表、静音组 stems、tmcd 缺表拒绝/co64 读取、fuzz 不变量+种子重生成） |

---

## 修订记录

- 2026-09-20 v5：全量复查——M-B0..M-B9 声明逐项核验（代码/测试/文档
  对照），发现 12 项隐藏缺陷并全部修复（明细见 §7 复查行）：量化路径
  s24 崩溃与 s32 缺失、短 AAC priming 边界、mux 多轨泄漏、tmcd 读侧
  fail-fast 缺口与写侧 co64、media 层 DF 边界、静音组 stems 导出失败、
  三处脚本全零截断、fuzz 多轨/tmcd 覆盖缺口（含语料错配）、绑定索引
  off-by-one、preflight/UI 能力面漂移、CI 门禁路径与测试清单补齐；
  规范版本标签滞留 v1.3 修正为 v1.6、README 陈旧非目标清理、
  profiles/spec 声道面补 mono。
- 2026-09-20 v4：tmcd（M-B9）由清单级加深为设计级——新增 **§2.7**
  完整设计（原子布局树含 stsd 'tmcd' 字段语义与 4B 帧计数样本、
  `tc_mux_set_timecode`/`topos_timecode_info`/`tc_movie_timecode` API
  签名、fps×DF 校验矩阵、逐帧 TC 推导与 DF 跳帧规则、tmcd 恒末轨的
  轨序约定）；M-B9 任务/验收扩充（ffmpeg 字节级对拍差异表、
  fps×DF roundtrip 矩阵、DF 边界单测表 00:00:59;29→00:01:00;02、
  代理保留源 TC 可选任务、`frame_timecode` 逐帧推导 e2e）；
  定位升级为"专业中间片必备"；排期允许 M-B1 后提前实施
  （独立于音频里程碑，规范先落者取 v1.5）。
- 2026-09-20 v3：并入 ProRes 对标调研结论（§0.5 新增，含来源链接）——
  新增 M-B8 多音轨/stems 角色轨（2-3d，逐轨声明 API + 轨名 ©nam +
  mux 数组化两步走）与 M-B9 tmcd 时间码轨（1-2d，ffmpeg -write_tmcd
  作 oracle）；M-B4 采样率扩为 Apple 全家族（44.1→192k）；M-B1 补
  编码器后端 priming 差异证据（aac_at 2112 vs native 2048，§0.1）并
  增"elst 落地后互操作 AAC 同步复跑"验收；多音轨从非目标移入目标；
  总计 8-10d → 11.5-15d；规范版本：v1.4（elst+采样率/位深）、
  v1.5（多音轨+tmcd）。
- 2026-09-20 v2：并入导出音频设置盘点新发现（§0.4 增 4 行证据）——
  M-B4 由"96kHz"扩为"采样率与位深扩展"（+float32/pcm_s32le/采样率 UI
  收敛，工作量 0.5d→1d，总计 8-10d）；新增 §2.6 float32 声明与
  formatFlags 语义设计（reserved[5] 样本格式标志、float 直通、e2e 逐位
  相等断言）；非目标补 AAC profile 扩展与独立位深选择器两项决策。
- 2026-09-20 v1：初版。基于 v1.1 交付复查（`9e39998b`）的三个边界与
  实测证据（PyAV 17.0.1 首包 pts=-1024 / 容器 roundtrip 实测 2048）成文；
  核心设计为音频 trak 最小 elst + priming 校准。
