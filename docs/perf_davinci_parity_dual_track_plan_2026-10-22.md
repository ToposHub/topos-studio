# 达芬奇性能对齐 · 多机并行实施计划（2026-10-22）

> 目标：**性能对齐**（非功能对齐）——4K 多格式实时播放零感掉帧、导出吞吐翻倍级提升、
> 全平台硬解零拷贝直通。本计划由 2026-10-22 双 Explore 审计 + 硬件/基准证据盘点驱动，
> 所有任务落到文件级，可直接按表开工。
>
> **执行文档**（每台机器拿自己那份即可开工，本文件为总纲）：
> - A 线（Mac，i9-9900K + RX 6900 XT）：`docs/perf_plan_A_mac_2026-10-22.md`
> - B 线（Windows RTX 4090）+ C 农场（RX 6800）：`docs/perf_plan_B_windows_2026-10-22.md`

---

## 0. 三条铁律（每个任务包开工前重读一遍）

1. **先测量，后动手**：任何优化合并前必须有 bench_out A/B 锚点对照，无锚点不合并。
2. **零拷贝优先于算力**：本项目已有的基准证明，"用了硬件"不等于"变快"——
   Windows d3d11va 硬解因每帧 GPU→CPU 下载比纯 CPU 慢 3-10×（4K H264：486fps vs 46.9fps）。
   每条数据路径的**拷贝次数**是第一优化对象。
3. **绞杀者模式，不做大重写**：Python 保留为应用层；只把"帧图执行引擎"逐模块下沉 native
   （复用 libtopos_codec 的 ctypes 绑定模式）；`.tglsl` 插件体系、Qt UI、3.6 万测试不动。

---

## 1. 机器角色分配（已按实测证据指定）

| 角色 | 机器 | 已知配置 | 证据 | 承担线路 |
|---|---|---|---|---|
| **A 线（开发主力）** | 本机 Mac | i9-9900K / RX 6900 XT 16GB / Metal 3 / macOS | 当前会话 | macOS·Metal·播放主线（归因→免费午餐→帧泵下沉→Metal 预览收口） |
| **B 线（开发主力）** | Windows #1 | i9-13900K / RTX 4090 / Win11 / Py3.13 | `bench_hw_decode_results_nvidia_rtx4090.json`（2026-09-12） | Windows·NV 零拷贝·D3D 后端线 |
| **C 农场（无开发冲突）** | Windows #2 及其余闲置机 | RX 6800 / 16C24T / Win11（`BENCH_HW_DECODE_WINDOWS.md` 2026-08-29）等 | 基准文档 | 夜间全量测试门禁 + 72 素材基准矩阵 + soak 长跑 |

**为什么只用两条开发线**：核心热点目录（realtime_playback、shared/gpu、codec）高度共享，
第三条开发线只会制造合并冲突与协调成本；多余机器的价值在"跑"不在"写"——全量 3.6 万测试、
844 处条件 skip 审计、52GB 素材矩阵基准、内存 soak，这些恰恰是当前 CI（只跑子集）欠的债。
若有多余 **Apple Silicon** Mac，优先派做 Metal 验证机（A 线是 Intel/AMD，代表不了大多数 Mac 用户）。

---

## 2. 现有资产盘点（计划的地基，别重复建设）

| 资产 | 位置 | 状态 |
|---|---|---|
| GLSL↔MSL 翻译 + 对拍门禁 | `src/features/color/core/shader_nodes_msl.py`（ADR 0004，SDR ≤1/255 / HDR ≤2e-3 rel） | 11 snippet 全收口，**机制可复制到 HLSL/D3D** |
| Metal compute 导出渲染器 | `src/features/edit/core/timeline_metal_export_renderer.py` + `src/shared/gpu/metal.py` | 已上线（仅导出侧，预览仍是 GL） |
| 自研 C 编解码器 | `native/topos_codec`（RANS 熵编码/slice 解码/MOV 封装），ctypes 绑定 `src/shared/codec/topos_binding.py` | 帧泵下沉的技术模板 |
| 实时播放引擎 | `src/features/edit/core/realtime_playback/` 共 6870 行（scheduler 787 / ring_buffer / gpu_frame_pool / telemetry / budget） | 下沉主候选，有遥测可对拍 |
| 硬解管理 | `edit_gpu_decode_manager.py`、`decode_manager_source.py`、Windows 侧 `WindowsHwPlanarSource` | **Mac 的 H264/H265/ProRes 已默认走 VT 零拷贝**（`macos_vt/source.py:135`，CPU 拷贝 0 次）；瓶颈在 Windows（每帧同步下载）与 Mac 的回退/监看链 |
| 休眠资产（已建成未接线） | `GpuFramePool`（realtime_playback/gpu_frame_pool.py，生产未调用）；`AVSyncController`（纯逻辑完成，engine 未接）；`TOPOS_METAL_VIDEO_PREVIEW` 开关 | 各设"激活或移除"决策任务（计划表 A A1.4/A4.1） |
| A/B 基准体系 | `bench_out/`（anchor_registry / ab4k_baseline / dev）、`scripts/bench_*.py` 约 15 个 | 直接扩展，勿另起炉灶 |
| 测试素材矩阵 | `tests/test_videos` 72 文件 / 52GB：ProRes 全家族、DNxHR 10/12bit、H264/H265、topos_422(HQ) | 基准与验收的标准素材库 |
| 测试兜底 | 1855 文件 / 3.6 万用例 / 32 marker（pytest.ini 实数）/ 844 条件 skip | 每次 native 化的保险网 |
| venv | `.venv/bin/python` 3.12.13：PyQt6 6.10.2 / moderngl 5.12.0 / av 17.0.1 / numpy 1.26.4 | 完好（注：Windows 机是 Py3.13+PyAV18.1，**版本漂移，Phase 0 统一**） |

---

## 3. 双线协作规范（防冲突，先定规矩再开工）

- **分支**：A 线 `perf/a-playback`、B 线 `perf/b-zerocopy`，均自 `main` 拉出；每 Phase 末经
  「bench 锚点 + 受影响测试子集全绿 + /review 门禁」后由 A 线统一合回 main。
- **目录所有权**（写代码前查表）：
  - A 线独占：`src/features/edit/core/realtime_playback/`、`src/features/color/`、`src/shared/gpu/`、`benchmarks/`
  - B 线独占：`scripts/bench_hw_*`、`scripts/probe_hw_*`、`native/topos_codec`（Windows 构建侧）、未来 `src/shared/gpu/d3d/`
  - 共享只读：`tests/`、`docs/`、`tests/test_videos`
  - 真需跨区改动的，先在 docs 提交 1 页 ADR 再动。
- **每日交接物**：每线当日 bench_out 锚点 JSON + 一段进度记录（沿用项目既有进度文档惯例），
  写入 `docs/perf_progress_<线>_<日期>.md`，另一线开工前必读。
- **版本纪律**：以 `constraints.txt` 收口两机 Python/PyAV 版本（Mac 3.12+av17 vs Win 3.13+av18 必须二选一）。

---

## 4. 阶段计划表

> 工时按"每线每天 1 个会话（约 4-6h agent 工作量）"估算；里程碑以**门禁通过**为验收，不以日期为验收。

### Phase 0 · 归因与基建（第 1 周，A+B+C 三机同时）

| # | 任务 | 机器 | 落点 | 验收门禁 |
|---|---|---|---|---|
| 0.1 | 帧预算分解 harness：扩展 `realtime_playback/telemetry.py`，把每帧拆为 解码/上传/GPU 合成/回读/Python 编排/上屏 六段计时，落盘 JSON | A | telemetry.py + 新 `scripts/bench_frame_budget.py` | 2K+4K 各 3 种编码（H264/ProRes422HQ/topos_422HQ）跑出六段毫秒表 |
| 0.2 | Windows 环境对齐：统一 Python/PyAV 到 constraints 版本，复跑 `bench_hw_decode_windows.py` 重锚 RTX 4090 | B | constraints.txt、scripts | 新锚点入库 `bench_out/anchor_registry` |
| 0.3 | 全量测试首跑 + skip 审计：3.6 万用例全量 + 844 条件 skip 逐条标注"本机可跑/真不可跑" | C | `tests/`、产出审计表 | 全量结果报告 + 可激活 skip 清单 |
| 0.4 | 《帧预算归因报告》：六段耗时排名 + 拷贝路径清单（哪些路径有 GPU→CPU 下载） | A | `docs/perf_frame_budget_report_2026-10.md` | 报告评审通过，产出 Phase 1-2 任务包修订版 |

### Phase 1 · Python 免费午餐 + 零拷贝 PoC（第 2-3 周）

| # | 任务 | 机器 | 落点 | 验收门禁 |
|---|---|---|---|---|
| 1.1 | 播放期 GC 治理：`gc.freeze()` 启动期冻结 + 播放中禁 gen2，接入 realtime_playback engine | A | `realtime_playback/engine.py`、`src/main.py` | 长播放（10min）GC 停顿计数归零，掉帧率对照锚点下降 |
| 1.2 | QImage 拷贝削减：审计 `program_viewer.py`（48 处引用）等 8 个热点文件，逐个消除格式转换/中转拷贝 | A | `src/features/edit/ui/program_viewer.py` 等 | 每处改动有 micro-bench 前后对照 |
| 1.3 | 信号风暴治理：播放热路径信号合批（用现有状态锁模式） | A | realtime_playback → UI 的信号链 | 单帧 Python 编排毫秒数（0.1 的指标）下降 ≥30% |
| 1.4 | **零拷贝 PoC**：NVDEC → D3D11 纹理**不下载**，直接进着色管线（D3D11GLInterop / CUDA-GL），测吞吐上限 | B | 新 `scripts/poc_nvdec_zerocopy.py` | 4K H264 端到端 fps ≥ 软解基线（486fps）×1.5 |
| 1.5 | 零拷贝 PoC（Mac 侧）：VideoToolbox → CVPixelBuffer(IOSurface) → GL/Metal 纹理直通 | A | 新 `scripts/poc_videotoolbox_zerocopy.py` | 同上对照 Mac 软解基线 |

### Phase 2 · 硬解零拷贝落地（第 3-5 周，双线并行）

| # | 任务 | 机器 | 落点 | 验收门禁 |
|---|---|---|---|---|
| 2.1 | Windows 生产链路接 PoC 成果：`WindowsHwPlanarSource` 增加零拷贝模式（feature flag 默认关） | B | `src/shared/video/`、`edit_gpu_decode_manager.py` | 72 素材中全部 H264/H265 硬解路径无每帧下载 |
| 2.2 | Mac VideoToolbox 生产链路同样接入（feature flag） | A | 同上 Mac 侧 | ProRes 走 VT（macOS 13+ 支持-ProRes 硬解时启用），H264/H265 必通 |
| 2.3 | 素材×平台覆盖矩阵：72 素材在 A/B/C 三机跑「解码方式/首帧延迟/持续 fps/显存峰值」全表 | C | 新 `scripts/bench_decode_matrix.py` | 三机全表入库 anchor_registry，缺口列清单 |
| 2.4 | topos_codec GPU 侧解码路径设计（ADR）：slice 解码结果直接驻留显存的接口草案 | A+B 各出一版 | `docs/adr/`（新目录如无） | ADR 评审通过，进 Phase 3 实现 |

### Phase 3 · 帧泵 native 下沉（第 5-9 周，A 主导，B 做 RHI 决策）

| # | 任务 | 机器 | 落点 | 验收门禁 |
|---|---|---|---|---|
| 3.1 | 下沉 `timeline_decode_scheduler.py`（787 行）→ C：ctypes 绑定（沿用 topos_binding 模式），Python 侧留策略接口 | A | `native/topos_playback/`（新）、`realtime_playback/` | 与 Python 版逐帧位对拍（复用 telemetry）≥10 万帧零分歧 |
| 3.2 | 下沉 `frame_ring_buffer.py` + `gpu_frame_pool.py`（含锁与水位策略） | A | 同上 | 播放主循环 Python CPU 占比 <15%（0.1 指标口径） |
| 3.3 | 薄 RHI ADR：D3D11 vs Vulkan 二选一（结论倾向 D3D11：与 d3d11va 零拷贝同 API、无 MoltenVK 负担；GLSL→HLSL 翻译复用 ADR 0004 门禁机制） | B | `docs/adr/0005-rhi.md` | 决策评审通过 |
| 3.4 | D3D11 后端 PoC：单 effect（grading）D3D11 compute 与 GL 同负载对拍 | B | `src/shared/gpu/d3d/`（新） | SDR ≤1/255 对拍门禁通过（复用 G2 机制） |
| 3.5 | 夜间对拍长跑：native vs Python 帧泵 10 小时 soak + 内存水位 | C | 现有 CI real-media 机 | 无帧分歧、无内存增长趋势 |

### Phase 4 · 预览 Metal 收口 + Windows 后端落地（第 9-14 周）

| # | 任务 | 机器 | 落点 | 验收门禁 |
|---|---|---|---|---|
| 4.1 | `gl_viewport.py` 双路径开关（GL/Metal），实时预览走 Metal（复用 MSL 翻译+G2 门禁） | A | `src/features/color/ui/gl_viewport.py`、`canvas.py` | 实时路径对拍门禁通过，默认切 Metal 后 bench 锚点全面不劣化 |
| 4.2 | Windows D3D11 后端落地：MSL→HLSL 翻译层 + composition snippet 分批移植 | B | `src/shared/gpu/d3d/`、`composition_uniforms.py` manifest 扩展 | 11 snippet 全过 SDR ≤1/255 门禁 |
| 4.3 | DeckLink 输出零拷贝：PBO 回读 → IOSurface/DXGI 直通 | A(Mac)/B(Win) | `file_controller.py` PBO 路径、`monitor_output.py` | 监视输出路径拷贝次数减半以上 |

### Phase 5 · 零拷贝纪律审计 + 长尾（第 14 周起，持续）

- 全链路拷贝审计：对每个数据路径标注"CPU/GPU 驻留 + 拷贝次数"，建立 architecture 元测试守卫（仿 `tests/architecture`）防退化。
- 启动时间、工程打开延迟、媒体池缩略图管线、导出编码器喂帧纪律。
- 显存水位治理：gpu_frame_pool 上限策略 + 压力测试。

---

## 5. KPI 口径（Phase 0 定精确基线，下表为目标方向）

| 指标 | 口径 | 目标方向 |
|---|---|---|
| 播放掉帧率 | telemetry 统计，4K ProRes422HQ 时间线 10min | 基线 → <0.05% |
| 单帧 Python 编排耗时 | 0.1 harness 第 6 段 | Phase 3 后 <15% 占比 |
| 4K H264 解码端到端 | Win 侧 fps | 零拷贝后 ≥ 软解 486fps 的 1.5×（即 >700fps） |
| 导出 ms/frame | 现有 cp_export bench | 相对基线持续锚点化，每 Phase 不劣化 |
| 首帧延迟 | 0.3 矩阵表 | 硬件路径 < 软件路径 |
| 全量测试 | C 农场夜跑 | 绿 + skip 审计后可跑用例 100% 激活 |

---

## 6. Token 消耗编排（怎么烧最高效）

每个任务包统一走四段流水线，禁止跳段：

```
Explore(归因/读上下文, 可多 agent 并行)
  → general-purpose(实现, 按目录所有权分派)
    → /review 门禁(Critical 必修)
      → bench 锚点合并(无锚点不合)
```

- **量级预估**（单任务包）：归因 0.3-1M / 实现 0.5-2M / 审查 0.2-0.5M；一个 Phase 全程 5-15M。
  全计划约 40-80M tokens，与"大量预算"匹配。
- **并行度**：每机一个会话同时挂 2-3 个 agent（如 A 线：QImage 审计 + GC 治理 + Metal 预研互不冲突）。
- **C 农场闲时任务**：全量测试、soak、素材矩阵用闲时/定时任务夜跑，token 花在"读结果写报告"，不花在等待。
- **禁止事项**：无归因数据的盲写重写；一次性生成大 C++ 文件不求对拍；跳过 review 直接合并。
- **自适应**：每 Phase 末用最新归因报告重算下一 Phase 任务包（计划是活文档，本文件随 Phase 演进追加修订记录）。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| native 帧泵行为回归 | feature flag 默认关；逐帧位对拍门禁；3.6 万测试兜底 |
| 双线合并冲突 | 目录所有权表（§3）；共享区先 ADR |
| Windows 双机（4090/6800）结果打架 | 锚点必须带机器指纹；矩阵表分列不平均 |
| 零拷贝驱动兼容性（interop 边界情况） | 每平台保留下载回退路径 + 运行时探测 |
| 黑苹果 Metal 长期风险 | Metal 3 目前可用；Apple Silicon 验证机纳入后覆盖主流 Mac |

## 8. 明日即可开工清单（按序）

1. A 机：任务包 0.1（帧预算 harness）——今天就能派 agent。
2. B 机：任务包 0.2（版本对齐 + 重锚 4090）。
3. C 机：任务包 0.3（全量测试首跑 + skip 审计）。
4. 三机各自拉分支：`perf/a-playback`、`perf/b-zerocopy`、农场用 main 只读。

---

### 修订记录
- 2026-10-22：初版（依据：双 Explore 审计、SPDisplays/venv 实测、bench_hw_decode 两份 Windows 实测数据）。
- 2026-10-22：拆出 A/B 两份机器专属执行计划表（perf_plan_A_mac / perf_plan_B_windows），总纲保留三机协作规范、KPI 与 token 编排全量规则；§4 任务表以两份执行表为准。
- 2026-10-22：四路函数级测绘后两份执行表升级 v2（任务全部落到 文件:行号 级）。同步修正总纲：marker 实为 32 非 51；Mac VT 零拷贝已是默认播放路径；新增休眠资产行（GpuFramePool/AVSyncController/TOPOS_METAL_VIDEO_PREVIEW）。关键改判：原"QImage 48 处削减"不在 GPU 热路径，重定向到监看回读链（详见计划表 A §0）。
