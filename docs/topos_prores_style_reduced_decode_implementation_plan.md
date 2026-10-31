# Topos 类 ProRes 降分辨率解码完整实施计划

**文档状态：** 可执行设计与实施跟踪表（2026-09-08）

**适用范围：** `native/topos_codec`、Topos MOV、`.toos`、Python binding、`ToposMediaSource`、播放调度与 GPU 上传链

**审计基线：** `fix-ui` 分支，已包含 `777266b4`、`ef61a107`、`375b5825` 交付的固定比例 reduced decode 入口

**目标：** 让 4K、6K、8K、12K、16K Topos 素材在预览时只读取并解码与约 2K 输出有关的数据，而不是完整解码后缩放；尽量不增加文件体积，不明显降低编码吞吐。

> 本文的“类 ProRes”仅指产品效果：原生低分辨率解码、低延迟、可连续播放；不声明复制 Apple 未公开实现，也不以 API 输出为 2K 代替真实性能证据。

---

## 1. 结论与推荐路线

可以实现，但不能只靠当前的“丢弃高频 DCT 系数”完成全部目标。

当前 Topos 是逐 8×8 块、逐 slice 的变长 `(run, level)` 码流。现有 `/2、/3、/4、/8` 路径已经不创建完整源平面，并且会跳过未保留系数的写入、反量化和逆变换；但它仍须读取每个源块并顺序消费全部变长码字。源分辨率越大，DC 数量、slice 数和熵解析量仍按源像素数增长。仅做系数裁剪，12K 不可能稳定达到“接近 2K 解码负载”。

推荐分两层推进：

1. **V7-A：可跳过 coefficient-band 码流。** 把低频和高频变成有独立长度、独立校验的字节段，先真正跳过高频熵码字。它不复制代理图，文件头开销很小，编码端复用已有 DCT/量化/token 遍。
2. **V7-B：2K scalable base + enhancement residual。** 对 6K 以上素材写一个最长边不超过 2048 的基础层；全分辨率不是再写一份原图，而是写“由基础层预测后的增强残差”。预览只读基础层，完整解码读基础层和残差。只有这一步才能让 8K/12K/16K 的解码工作量真正受 2K 上限约束。

V7-A 先落地并独立验收；V7-B 只有在体积和编码速度门禁通过后才成为默认。两者都失败时保留当前 V1–V6 解码和现有 reduced fallback，不破坏旧素材。

---

## 2. 当前代码审计

| 项目 | 当前状态 | 代码证据 | 与目标的差距 |
| --- | --- | --- | --- |
| 分辨率上限 | 帧头和公开配置支持到 16384×16384 | `frame_header.h::TC_MAX_CODED_DIM`、`topos_codec.h::topos_frame_config` | 1K–16K 可编码不等于 8K/12K 可按 2K 成本解码 |
| 固定比例 API | 已有 full、1/2、1/3、1/4、1/8 | `tc_decode_scale`、`tc_frame_decode_reduced` | context、surface、播放器尚未完整接入 reduced 模式 |
| 低频重建 | 按比例限制扫描序系数，并使用 sampled inverse | `coefficient_limit`、`color_store.h`、SIMD transform | 仍解析全部高频变长码字 |
| 熵语法 | 每块 DC + `(run, level)`×N + EOB，整 slice 单一 payload | `color_scan.h`、`slice_codec.c` | 高低频没有独立 byte range，无法 O(1) 跳过高频 |
| 并行 | slice 常驻线程池、无状态/context 批量解码 | `tpool.c`、`tc_frame_decode_batch*`、`ToposDecoder` | 播放默认 read-ahead 关闭；reduced context batch API 不完整 |
| 输出内存 | 可直写 caller-owned CPU 或映射的 host-visible GPU 缓冲 | `topos_decode_surface` | codec 不创建 Metal/D3D/Vulkan 纹理，也不提交同步命令 |
| 播放器接线 | legacy 流使用 native reduced request；V7-B MOV 预览通过 `tc_movie_packet_base()`/`_batch()` 只取 base range；非固定目标仍走 AUTO_2K/旧流兼容语义 | `src/shared/media/topos_source.py`、`mov.c` | V7-B 的单帧/批量 base I/O 已落地；V7-A band reader 和最终 E2E 门禁仍待完成 |
| 现有实测 | 4K→2K p50 约改善 22.6%；6K→2K 约 0.9% | `docs/image/reports/reduced_decode_4k_6k_to_2k_2026-09-08.md` | 6K 结果已证明只优化逆变换不足以达成目标 |

### 2.1 必须区分的三种路径

| 路径 | 是否解完整源平面 | 是否解全部熵码字 | 工作量是否接近 2K | 定位 |
| --- | ---: | ---: | ---: | --- |
| 完整解码后缩放 | 是 | 是 | 否 | 必须从预览链移除 |
| 当前 reduced coefficient decode | 否 | 是 | 否，仍随源块数增长 | 旧流兼容 fallback |
| V7-A band skip | 否 | 否，高频段直接 seek/skip | 部分；仍须处理每个源块的基础符号 | 4K/6K 的低风险加速层 |
| V7-B 2K scalable base | 否 | 否，增强层完全不读 | 是，预览主要工作量由 base 尺寸决定 | 8K/12K/16K 产品目标 |

---

## 3. 产品语义和分辨率选择

### 3.1 公开模式

保留现有固定比例，并新增自动预算模式：

| 模式 | 输出语义 | 典型用途 |
| --- | --- | --- |
| `FULL` | 完整分辨率和完整质量 | 导出、像素检查、暂停后的高质量刷新 |
| `HALF` | `ceil(source/2)` | 4K→2K |
| `THIRD` | `ceil(source/3)` | 6K→2K 专用快路 |
| `QUARTER` | `ceil(source/4)` | 8K→2K |
| `EIGHTH` | `ceil(source/8)` | 16K→2K；12K→约 1.5K 后由 GPU 显示缩放到 2K |
| `AUTO_2K` | 保持宽高比，最长边不超过 2048；优先 V7 base | 时间线播放、素材浏览、拖动和快速定位 |

固定比例集合里没有 1/6，因此“12K 精确得到 2K”不能由现有枚举表达。V7-B 的 base 直接记录任意合法目标几何，可精确写入 2048 宽的基础层；旧流则选 1/8 输出约 1.5K，再在 GPU 做显示插值。不得为旧流谎报成原生 2K 解码。

### 3.2 播放质量切换

```text
开始播放 / scrub
    └─ AUTO_2K（只读 base 或最低可用 band）
暂停 150–250 ms
    └─ 后台请求 FULL，高质量帧完成后原子替换
导出 / 取样 / scopes 精确模式
    └─ FULL，禁止静默使用 reduced 帧
```

`DecodedFrame.extra` 必须暴露实际路径、实际输出尺寸、base/band 版本和是否存在未校验的跳过段，UI/HUD 不得把 fallback 当作 V7 native reduced。

---

## 4. V7-A：可跳过 coefficient-band 码流设计

### 4.1 频带划分

为兼容现有视觉结果，第一版沿用当前扫描序保留界限，不在同一批次重新调参：

| 段 | 扫描位置 | 累计用途 |
| --- | --- | --- |
| B0 | 0..4 | 1/8 |
| B1 | 5..10 | 1/4 |
| B2 | 11..16 | 1/3 |
| B3 | 17..24 | 1/2 |
| B4 | 25..63 | full |

后续可依据真实素材质量报告调整界限，但任何已发布 `band_layout_version` 永不改变含义。

### 4.2 码流布局

建议使用新 TPIC major（暂定 `version_major=7`，最终由 ADR 冻结），保留 53 字节公共帧头，在其后增加有界的 layer/slice directory：

```text
TPIC v7 frame header
TPLD frame directory
  layer descriptors[]
  slice descriptors[]
payload region
  base/slice0/B0 ... B4
  base/slice1/B0 ... B4
  enhancement layers ...
```

每个 coefficient band 具有独立 `offset + size + crc32`。读取目录时一次验证所有范围不溢出、不重叠、不越过 sample；reduced decode 只校验并读取需要的段。被跳过段的 CRC 留到 full decode 验证，输出诊断中记录 `unchecked_segment_count`。

### 4.3 段内符号语法

不能简单把当前每块码流复制五份，否则每个 band 都增加 EOB，文件会明显膨胀。V7-A 参考语法采用：

- B0：每块 DC 差分 + 低频 AC 数量/位置/level；DC 预测仍严格闭合于 slice；
- B1–B4：先编码“有非零系数的 block index 增量”，只为 active block 写 band-local 位置和 level；
- active block 和系数位置都使用有界 delta/VLC，段尾只有 slice 级终止，不为所有空块重复写 EOB；
- 编码器在现有 `fill_color_band_tokens` 的量化/token 遍中一次完成分桶，不重复 DCT，不重复量化；
- full decoder 合并 B0–B4 后进入现有反量化/IDCT，必须与同一量化系数集合的参考重建逐像素一致。

先实现清晰的 scalar 参考语法，再决定是否复用 canonical VLC 码书；在体积报告完成前不得冻结 V7 表。

---

## 5. V7-B：2K scalable base + enhancement residual

### 5.1 为什么需要基础层

V7-A 即使跳过 B1–B4，仍要为源图的每个 8×8 块解 B0。12K 约有 2K 的 36 倍像素，单凭 B0 不会变成 2K 级负载。V7-B 把预览空间分辨率本身写成独立基础层：

```text
源帧 S
  ├─ 固定滤波下采样 → base P（最长边 ≤ 2048）→ 编码为可独立解码层
  ├─ 编码器本地解码 base → 重建 P_hat
  └─ 固定滤波上采样 P_hat → 预测 S' → residual R = S - S' → 编码为增强层

预览：decode(base) → P_hat
完整：decode(base) → P_hat → upsample → decode(R) → add/clip
```

P 不是额外的旁路代理文件，而是完整重建所需的预测基础；增强层不再重复编码 P 已解释的低频信息。
残差必须由“已量化并重建的 `P_hat`”生成，不能直接使用下采样原值 P，
否则编码器和解码器的预测基准不同，会产生不可控漂移。

### 5.2 基础层策略

- 源最长边 ≤2048：不写 scalable base，直接使用普通单层帧；
- 4K：默认先用 V7-A，只有 A/B 证明 base 模式编码回退和体积均达标才启用；
- 6K/8K/12K/16K：默认候选 base，base 保持宽高比且最长边为 2048；
- odd dimension、4:2:2 chroma、4:4:4、GBR、10/12-bit、Alpha 都由同一几何函数推导；
- 下采样/上采样滤波器必须固定、整数化、跨平台确定性；首版使用可 SIMD 的 separable 6-tap 或经报告确认的 box/lanczos-lite，不调用平台私有缩放器；
- enhancement residual 使用有界 signed 变换输入和独立量化参数；完整解码不得把残差钳位到无符号后再相加；
- Alpha lossless 模式的 full 重建必须 bit-exact；预览 Alpha 可用 base，但不得改变 straight/premultiplied 语义。

### 5.3 自适应写入策略

不能承诺所有噪声素材都同时“零体积增加、零编码开销”。编码器必须按 profile 明确取舍：

| 策略 | 行为 | 使用场景 |
| --- | --- | --- |
| `band-only` | 只写 V7-A，不写空间 base | 2K/4K、极高噪声、编码速度优先 |
| `scalable-fast` | 单次生成 base+residual，不双重试编 | 6K 以上默认候选 |
| `scalable-size-guarded` | token 位数 dry-run 后在 scalable 与 band-only 中择小 | 离线母版/图片；允许少量编码开销 |

容器/轨级 capability 必须声明是否每帧允许布局切换。若允许，播放器先看 frame directory；不得通过试解猜测。

---

## 6. 完整实施任务表

### RD0：冻结基线和修复正确性

| ID | 任务 | 具体改动 | 主要文件 | 验收/提交 |
| --- | --- | --- | --- | --- |
| RD0-01 | 冻结基准 corpus | 保存 2K/4K/6K/8K/12K/16K 的自然、纹理、噪声、渐变、文字、HDR、Alpha 素材及 SHA | `tests/fixtures/topos_reduced/`、`docs/image/` | corpus manifest 可重复生成；`test(codec): freeze reduced-decode corpus` |
| RD0-02 | 收口当前 scaled 正确性 | 覆盖 sparse/batch/SIMD 开关组合、odd dimensions、422/444/GBR/Alpha，先消灭越界和路径差异 | `color_store.h`、`color_scan.h`、`test_codec.c` | ASan/UBSan + scalar/SIMD 逐位一致；单独提交 |
| RD0-03 | 分项 telemetry | 增加 packet bytes read、segments parsed/skipped、entropy symbols、blocks、IDCT samples、upload bytes、p50/p95/p99 | `codec.h/c`、binding、benchmark schema | 每个 reduced 模式能证明“少读/少解/少算”；`perf(codec): instrument reduced decode` |
| RD0-04 | 统一 benchmark runner | 同进程同 packet 对比 full、post-scale、legacy reduced、V7 band、V7 base；冷热缓存分开 | `scripts/bench_topos_reduced_decode.py` | JSON schema 校验通过，报告含硬件/线程/构建参数 |

### RD1：API、自动比例和播放接线

| ID | 任务 | 具体改动 | 主要文件 | 验收/提交 |
| --- | --- | --- | --- | --- |
| RD1-01 | 请求结构 | 新增 `topos_decode_request`：mode、target box、quality、memory type、flags；避免继续扩散位置参数 | `include/topos_codec.h` | ABI size/version 检查、旧 API 保留；`feat(codec): add reduced decode request ABI` |
| RD1-02 | context reduced API | 增加 prepare/decode/batch/surface 的 reduced 版本，查询几何与实际输出一致 | `decoder_ctx.c`、`codec.c` | 单帧与 batch 像素一致，单 context 不可重入语义不变 |
| RD1-03 | `AUTO_2K` 选择器 | 根据源几何和 stream capability 选择 V7 base 或旧流 1/2、1/3、1/4、1/8 fallback | codec + binding | 4K/6K/8K/12K/16K 映射表单测；实际模式可查询 |
| RD1-04 | Python binding | 暴露 request、reduced context batch、surface 和 telemetry | `src/shared/codec/topos_binding.py` | ctypes 结构镜像、生命周期和错误路径测试通过 |
| RD1-05 | 视频播放接线 | `ToposMediaSource(target_size)` 直接申请 reduced 几何和平面；删除该路径的 full decode + NumPy box 缩放 | `src/shared/media/topos_source.py` | target_size 播放 native bytes/symbols 降低，legacy fallback 有诊断 |
| RD1-06 | 图片/序列接线 | `.toos` 和图片序列共用 `AUTO_2K`，缓存键包含 bitstream/layout/scale | image binding、sequence、file explorer | 缩略图、灯箱、序列预取无全分辨率中间平面 |

### RD2：V7-A band 码流参考实现

| ID | 任务 | 具体改动 | 主要文件 | 验收/提交 |
| --- | --- | --- | --- | --- |
| RD2-01 | ADR 与 spec | 冻结 major、目录边界、band layout、CRC、错误码、旧 reader 拒绝语义 | 新 `ADR-C0xx`、`bitstream_spec_v7.md` | offset/size/CRC/能力位无歧义；`docs(codec): specify v7 reduced bitstream` |
| RD2-02 | 安全目录解析 | checked arithmetic 验证目录、层和段不重叠且都在 sample 内；未知 critical flag 拒绝 | `frame_header.c`、`packet.c`、新 `layer_directory.c` | 截断/伪造/重叠 fuzz 不越界、不大分配 |
| RD2-03 | 单遍 token 分桶 | 在已有 DCT/量化/token 遍把 AC 按 B0–B4 分桶；不重复 DCT/quant | `codec.c`、新 `band_tokens.c` | scalar 参考系数与旧路径逐值一致；编码热点报告 |
| RD2-04 | 标量 band encoder | 实现 DC/base 和 active-block enhancement 语法、精确位数估计、CRC | entropy/bitstream | 同输入同配置 packet byte-exact；1/2/4/8/16 线程编码一致 |
| RD2-05 | 标量 band decoder | 只打开请求所需段并合并系数；full 打开全部段 | entropy/codec | full 重建与参考量化系数逐像素一致；跳过段字节计数准确 |
| RD2-06 | 体积决策门 | 对 corpus 比较 V2 VLC 与 V7-A；分析头开销、EOB 消除、active-block map | benchmark/report | median ≤+1%，p95 ≤+3%；否则修改语法，不进入默认 writer |

### RD3：V7-A 热路径、SIMD 和批量调度

| ID | 任务 | 具体改动 | 主要文件 | 验收/提交 |
| --- | --- | --- | --- | --- |
| RD3-01 | band 熵热路径 | 两级 LUT、批量 refill、active block delta 融合解码；只对已请求段初始化 reader | `entropy/` | 与 scalar 逐截断位差分一致；高频段 symbol count 为 0 |
| RD3-02 | 低频 sampled IDCT | 为 B0、B0+B1、B0..B2、B0..B3 固定生成 scalar/AVX2/NEON kernel | `simd/dispatch.*`、transform | 所有 CPU dispatch 与 scalar bit-exact；非法 CPU flag 回退安全 |
| RD3-03 | 1/3 专用路径 | 6K→2K 采用固定相位表和连续写回，消除通用除法/坐标映射 | `color_store.h`、SIMD | exact divisible 与 odd-size 两套 golden；比通用 sampled path 快 |
| RD3-04 | 跨帧任务图 | I/O read → directory parse → segment CRC/entropy → IDCT/store 分层批次；按 frame/slice 共享线程预算 | `tpool.c`、`decoder_ctx.c`、播放器 | 8/16 线程无嵌套过度调度；取消和错误只影响规定帧 |
| RD3-05 | 稳态零分配 | base/band scratch、DC 行、输出平面和 packet arena grow-only 复用 | decoder context | 几何稳定连续 1000 帧热路径零 heap alloc |

### RD4：V7-B scalable base + residual

| ID | 任务 | 具体改动 | 主要文件 | 验收/提交 |
| --- | --- | --- | --- | --- |
| RD4-01 | 固定采样规范 | 定义整数下采样/上采样、边界延拓、422 chroma phase、rounding、10/12-bit 和 Alpha 规则 | spec + reference C | x86/ARM/不同线程数逐像素一致 |
| RD4-02 | base 编码 | 从输入平面直接生成 ≤2K base，复用 V7-A slice/band encoder | codec encoder | 6K/8K/12K/16K base 几何正确；无完整尺寸临时副本 |
| RD4-03 | residual 编码 | 新 signed residual forward/quant/token 路径；base 预测与 full decoder 同一函数 | transform/codec | base+residual 重建在 profile 误差预算内；Alpha lossless bit-exact |
| RD4-04 | base-only decode | 目录定位后只 pread/mmap base byte ranges，不触碰 enhancement payload | movie/image reader | 读字节、CRC、symbol、IDCT、RSS 均受 2K 上限控制 |
| RD4-05 | full decode | base → 固定上采样 → residual add/clip；支持逐 slice concealment | codec + tests | full 输出 deterministic；坏 base/enhancement 的 conceal 规则冻结 |
| RD4-06 | sized encoder 适配 | base 与 residual 共享目标字节预算，避免各层独立 QP 搜索导致编码爆炸 | `tc_frame_encode_sized` | 目标大小误差不劣于当前门禁；probe 次数和 p95 有上限 |
| RD4-07 | 策略选择 | 实现 band-only/scalable-fast/size-guarded，能力写入 frame/track | encoder、MOV tpcC | reader 无需试解；混合布局 seek 和顺播正确 |
| RD4-08 | 体积/编码速度门 | 与 V2 同质量对比总包大小和 encode p50/p95 | report | median 体积 ≤+1%、p95 ≤+3%；encode p50 ≤+5%、p95 ≤+8% |

### 6.1 当前实施状态（2026-09-08）

已完成并已分别提交：RD0 基线/telemetry/benchmark、RD1 request/context/AUTO_2K
与播放器接线、RD2 目录与 scalar V7-A 参考链、RD3 的熵/低频/1/3/跨帧/零分配
优化，以及 RD4-01～RD4-05、RD4-07 的参考实现。RD4-04 本次新增的落地面是：
MOV reader 通过 `tc_movie_packet_base()` 对 V7-B 做 header/directory/base 的
范围读取，播放器 preview 已切到该接口；V1–V7-A 保留完整 sample 兼容回退。

RD4-06 与 RD4-08 仍未宣称完成。当前 V7-B writer 是显式 opt-in reference
writer；同质量联合体积、编码 p50/p95 和最终质量预算未通过前，不进入默认 writer。
跨平台 surface/E2E、RD6 长稳矩阵和 RD7 rollout 仍是后续实施项。

### RD5：GPU 缓冲、渲染和播放策略

| ID | 任务 | 具体改动 | 主要文件 | 验收/提交 |
| --- | --- | --- | --- | --- |
| RD5-01 | surface 所有权协议 | 明确 acquire/map/decode/unmap/fence/release；codec 只写映射地址，平台层负责资源和同步 | C ABI、`frame_path.py` | 无 use-after-free、覆盖在飞帧或隐式全帧 copy |
| RD5-02 | macOS 路径 | 优先 IOSurface/共享缓冲；OpenGL 路径可先用持久映射 PBO ring 再 texture upload | platform adapter | 无 Python/numpy 中间复制；Instruments 可见 upload 字节符合 2K |
| RD5-03 | Windows/Linux 路径 | D3D11 staging/shared 或 persistent PBO/dmabuf 按能力 gate；不可映射时明确回退 | platform adapter | 不把 staging copy 宣称为 zero-copy；路径在 diagnostics 可见 |
| RD5-04 | planar GPU 显示 | reduced YUV/GBR 继续走 R16UI planar shader，色彩、范围、chroma siting 不变 | renderer/yuv upload | full/reduced 色彩参考误差在同一门限 |
| RD5-05 | 动态质量策略 | 播放/scrub 用 AUTO_2K，暂停后异步 FULL 替换；队列积压时丢旧请求而非阻塞 UI | playback queue/source | 10 分钟播放无队列无限增长；seek 后旧 generation 不上屏 |
| RD5-06 | 批量预取策略 | 依据帧时限和内存自适应 2–4 帧，不把整批成本压到首帧 | `ToposMediaSource`、decode queue | 首帧 p95 不因 batch 恶化；缓存命中和 dropped frame 可观测 |

### RD6：正确性、安全、质量和性能验收

| ID | 测试类别 | 必测矩阵 | 通过条件 |
| --- | --- | --- | --- |
| RD6-01 | 几何 | 1..16384、非 8 对齐、奇数宽高、超窄/超高、2K/4K/6K/8K/12K/16K | 不越界；plane geometry、stride、chroma rounding 正确 |
| RD6-02 | 格式 | YUV422/444、GBR、10/12-bit、无 Alpha/A16/near-lossless、full/limited、PQ/HLG | metadata 不漂移；full 路径满足现有 golden/误差门禁 |
| RD6-03 | 比例 | 1/2、1/3、1/4、1/8、AUTO_2K、暂停 FULL 替换 | 实际输出和诊断一致；不发生 full decode 后 CPU resize |
| RD6-04 | 码流安全 | 每个目录字段、段长度、CRC、截断位、非法 VLC、段重叠、超大计数 | ASan/UBSan/fuzz 无 crash/OOB/不受控分配；错误码稳定 |
| RD6-05 | SIMD/线程 | scalar/AVX2/NEON × 1/2/4/8/16 threads × single/batch/surface | full bit-exact；reduced 与 scalar reference bit-exact |
| RD6-06 | 预览质量 | 自然/纹理/细线/文字/渐变/噪声/HDR/Alpha，逐帧和运动序列 | 对 full decode 后高质量缩小给 PSNR/SSIM/ΔE；默认目标 SSIM ≥0.98，明显 alias/ringing/闪烁为失败 |
| RD6-07 | 性能 | 冷/热缓存、codec-only、I/O、upload、E2E，4K/6K/8K/12K/16K | 见第 7 节硬门禁 |
| RD6-08 | 长稳 | 顺播、反向、seek、scrub、暂停切 FULL、坏帧、取消，至少 1000 帧/10 分钟 | RSS 有界、无线程/资源泄漏、无旧帧覆盖新帧 |

### RD7：版本、迁移和发布

| ID | 任务 | 具体改动 | 验收/提交 |
| --- | --- | --- | --- |
| RD7-01 | capability query | 暴露 V7 band、scalable base、AUTO_2K、surface backend 和最大尺寸 | writer/reader/player 对能力判断一致 |
| RD7-02 | 旧流兼容 | V1–V6 永久可读；旧流 AUTO_2K 使用 legacy reduced 并明确诊断 | 旧 golden 零回归；不自动重写用户文件 |
| RD7-03 | writer rollout | `experimental` → opt-in → 新建高分辨率素材默认；每阶段保留开关 | 任一门禁失败可回退 V2 writer，不影响 reader |
| RD7-04 | MOV/图片同步 | tpcC/TPIM capability、frame major、目录版本和 sample entry 一致 | 不一致文件明确拒绝，不靠首帧猜测 |
| RD7-05 | SDK/CLI | inspect 输出层/段/比例/字节；CLI 可指定 full/ratio/auto2k；文档给 fallback | C/Python/CLI 示例和 ABI smoke 通过 |
| RD7-06 | 发布报告 | 保存环境、命令、素材 hash、体积、编码、解码、质量和已知边界 | 未达到 8K/12K 门禁前不得宣传“任意分辨率 2K 负载” |

---

## 7. 不可放宽的验收门禁

### 7.1 “真正降分辨率解码”门禁

V7 reduced 路径必须同时满足：

1. 不分配完整源分辨率输出或中间像素平面；
2. 未请求的 coefficient band 不初始化 entropy reader、不解 symbol、不做 CRC；
3. V7-B base-only 不读取 enhancement payload；冷缓存实际 I/O 字节可测；
4. 只对实际输出需要的系数/样本执行反量化和逆变换；
5. `ToposMediaSource` 不再用 NumPy 对完整帧后缩放；
6. GPU 上传字节按 reduced 平面几何计算；
7. telemetry 能给出 `bytes_read / bytes_skipped / symbols_decoded / idct_samples / output_bytes`。

少一项都只能称为“目标尺寸输出”，不能称为“2K 成本解码”。

### 7.2 性能门禁

以同一参考机器、release/O3、同一 2K 输出、同 profile、同素材和线程预算比较：

| 指标 | 发布目标 | 最低可接受 |
| --- | ---: | ---: |
| 4K/6K/8K/12K/16K `AUTO_2K` codec p95 | ≤ 2K full p95 的 1.25× | ≤1.50× |
| 60 fps 播放 codec+upload p95 | ≤16.7 ms | 若硬件档只承诺 30 fps，则 ≤33.3 ms |
| 10 分钟顺播掉帧率 | <0.1% | <1% 且原因可诊断 |
| base-only 峰值输出/工作集 | ≤2K full 的 1.25× | ≤1.50× |
| 源分辨率从 8K 增至 16K 的 base-only entropy symbols | ≤1.10× | ≤1.25× |

V7-A band-only 单独报告，但不能用它替代 V7-B 的分辨率无关门禁。

### 7.3 文件体积和编码速度门禁

- 同质量相对当前 V2 VLC：总文件 median 不增加超过 1%，p95 不增加超过 3%；
- 纯目录/对齐开销必须低于帧包的 0.15%；
- `scalable-fast` 编码 p50 回退不超过 5%，p95 不超过 8%；
- 不允许为获得 reduced decode 而静默降低 full 质量；
- 若高噪声/Alpha 素材超门限，自动或显式使用 `band-only`，并记录布局；
- 在门禁通过前，V7 writer 只能 opt-in，V1–V6 reader 和 V2 writer 不删除。

---

## 8. 实施顺序、提交和停止条件

### 8.1 推荐顺序

```text
RD0 基线/正确性
  → RD1 reduced API + 播放器真正接线
  → RD2 V7-A scalar 码流
  → RD3 V7-A SIMD/并行
  → V7-A 体积与 4K/6K 性能评审
  → RD4 V7-B scalable base/residual
  → RD5 GPU/播放策略
  → RD6 全矩阵验收
  → RD7 发布
```

### 8.2 重要提交粒度

| 完成项 | 建议提交 |
| --- | --- |
| 当前 reduced 正确性闭环 | `fix(codec): harden scaled reduced reconstruction` |
| telemetry 与 benchmark | `perf(codec): instrument reduced decode workload` |
| context/request API | `feat(codec): add auto2k reduced decode requests` |
| 播放器接入 | `perf(media): route Topos previews through native reduced decode` |
| V7 spec/directory | `feat(codec): add v7 coefficient band directory` |
| V7 scalar band codec | `feat(codec): add skippable coefficient bands` |
| SIMD/1/3/batch | `perf(codec): accelerate reduced band reconstruction` |
| scalable base/residual | `feat(codec): add 2k scalable base layer` |
| GPU surface adapter | `perf(media): decode Topos preview into mapped GPU surfaces` |
| 发布与文档 | `release(codec): publish v7 reduced decode capability` |

每次重要提交必须同时包含对应测试；性能提交还必须保存前后 JSON 和 Markdown 报告。不得把未相关的工作区修改一起暂存。

### 8.3 停止/回退条件

| 风险 | 停止条件 | 回退 |
| --- | --- | --- |
| band 语法膨胀 | corpus p95 >+3% 或噪声类持续 >+5% | 调整 active-block 语法；仍失败则 V7-A 不做默认 writer |
| scalable residual 体积差 | 同质量 p95 >+3% | 高噪声帧/轨使用 band-only |
| 编码变慢 | p95 回退 >8% 且热点不是可消除实现问题 | 仅 ≥8K 或离线 profile 启用 base；保留 fast band-only |
| 预览质量差 | 细线/文字/HDR 出现明显 alias、闪烁，或质量门限失败 | 提高 base 质量/改滤波；禁止以扩大系数数量掩盖语法问题 |
| 12K 仍随源尺寸增长 | base-only symbols/bytes/时间明显随 8K→16K 增长 | 不发布“2K 负载”声明，继续定位 I/O、目录和误走 enhancement |
| GPU 路径不是真直写 | 仍有 full-size numpy/RGB 中间复制 | 标记 CPU upload fallback，不宣传 zero-copy |

---

## 9. 完成定义

只有以下条件全部满足，Topos 才可以宣称具备“类 ProRes 的原生降分辨率播放”：

1. 4K、6K、8K、12K、16K 都能通过 `AUTO_2K` 在不完整解码源平面的情况下播放；
2. 1/2、1/3、1/4、1/8 均有固定、可测试、可诊断的 native 路径；
3. V7-A 对未请求高频段实现 byte-level skip，而不仅是解码后丢弃；
4. V7-B 对 8K/12K/16K 只读取和解码 ≤2K base，增强层不被触碰；
5. 播放器、图片序列、素材浏览器和 C/Python API 使用同一比例和几何语义；
6. AVX2/NEON、单帧/批量、CPU/映射 GPU surface 与 scalar reference 一致；
7. 文件体积、编码速度、预览质量和长时播放通过第 7 节门禁；
8. 旧文件继续可读，新 reader 对未知 V7 能力明确失败，源文件不被自动重写；
9. 发布报告保存真实素材、硬件、命令、构建、线程、冷热缓存和 p50/p95/p99；
10. 任意宣传数字都能由仓库内 benchmark 和报告复现。
