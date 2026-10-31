# 升级计划 M · Mac 主责（整体快速复核后的正确性收尾与下一阶段升级）

> 日期：2026-10-24；审查基线：`9e05556c3`；执行宿主：Darwin x86_64 / Python 3.12.13。
> 状态更新：2026-10-25 复核。正文主包勾选为 23/25，M16/M18 仍开放；勾选含软件子项收口，不代表完整用户场景验收。最新逐包结论和新增缺口见[复核与收尾计划](upgrade_audit_followup_2026-10-25.md)。下文初始问题与历次修订保留历史口径。
> 配套：[Windows 计划 W](upgrade_plan_W_windows_2026-10-24.md)。旧 A/B/F 保留历史，不覆盖其证据。

## 0. 结论、证据与两机分工

这一轮应先收尾正确性、生产接线和验收，再扩大功能。F 总表实际为 **45/53 勾选，8 项仍开**（F5.2、F6.3、F10.1–F10.6）；多个勾选只表示模型/API 子包完成，UI、正式 IO 或成片消费者仍列“后续”。这不抹去已完成工作，但不足以判定整个用户功能已验收。

初次复核已跑12个定向测试文件，**142 passed / 7.48s**，9组只读逻辑探针观察到缺口；v2额外执行4组只读计划遗漏探针，结果另存，本轮未重跑上述142项。测试通过和生产链有问题可以同时成立，原因是现测试多在helper层自验。未跑全库、真实GUI/GPU、外接监看或Windows真机；本表不是全项目无遗漏证明。

证据：[执行指纹/原命令](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/run_manifest.json>)、[142项日志](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/focused_pytest.log>)、[9组复现结果](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/logic_probe_results.json>)、[依赖冲突](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/pip_check.log>)。探针退出0只表示执行完成，`issue_present=true`表示发现缺口。

- Mac 主责：共享模型/schema/命令/Color/Edit/Composite/Deliver正确性、产品入口和Mac特有VT/监看。Windows对同一共享提交做复验，避免两边重复实现。
- Windows 主责：CUDA/GL、原生DLL、设备/平面门禁、Windows打包与真实工作流/长压。共享 renderer/帧契约先协商，由一个集成者串行修改，分小提交交接。
- P0为开工/发布门禁，P1为正确性及核心收尾，P2为效率扩展；“静态确认”“局部复现”“历史真机证据”“待验收”分别记录。硬件/SDK条件不满足可延期，不能改成通过。
- 文档中的Mac绝对链接供本机跳转；Windows按仓库根解析 [可移植源码/证据索引](validation/upgrade_2026-10-24/portable_source_index.json)，索引保留相对路径、行号和审查commit。执行命令用 `git rev-parse --show-toplevel` 取得本机根，机器绝对路径只留运行环境，不能写成工程身份。
- 每包遵循 `.clinerules`：领域命令与Undo、版本化迁移/未知payload保留、渲染线程GPU与显式释放、HDR不无理由clamp。复杂包的子项分别提交和验收，不一次重构多个系统。编号按领域分类，M18是发布门禁，不要求等待编号小于它的全部可选功能；新M19–M24按实际依赖进入。硬依赖决定启动，条件依赖只门禁对应子能力。

## 1. 本轮发现的问题（共享代码由 Mac 修复，Windows 必须复验）

| ID | 证据层级 | 触发与问题 | 当前入口 | 责任包 |
|---|---|---|---|---|
| S01 | 已复现 | 快照恢复丢素材 metadata/评级，正式 IO 往返丢快照表；真实 dict 形态的时间线比较也未覆盖。 | [project_snapshots.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/project_snapshots.py:108>) | M02 |
| S02 | 已复现 | 口头语计划用伪 clip_id 且缺 track_id；真实 compiler 报错，整片删除也不能表达词区间。 | [filler_removal.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/ai_editing/filler_removal.py:147>) | M11 |
| S03 | 已复现 | 竖屏编译只写 width/height，真实 job 仍取原 resolution；离线拒绝仍返回部分 jobs。字幕/版本校验、PCM helper 未接正式消费者。 | [version_compiler.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/deliver/core/version_compiler.py:103>) | M10 |
| S04 | 已复现（packet 替身） | 所谓 PTS 索引读 packet.dts；重排序样例使 timeline 0 映射源帧 2。真实 VFR+B 帧仍须媒体验收。 | [ffmpeg_source.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/media/ffmpeg_source.py:317>) | M04 |
| S05 | 静态调用链确认 | 外录绑定在预览仍生效，成片/Stem 遇变速或保调却跳回机内声；同一片段音源不一致。 | [timeline_export.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:4187>) | M03 |
| S06 | 局部复现＋静态确认 | TC 法用 wav_tc-video_tc，方向与消费契约相反；视频/WAV 帧部分固定除 25，24fps 样例 1.48s≠1.5s。 | [edit_controller.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py:7742>) | M03 |
| S07 | 已复现（故障注入） | 入库源 hash 读失败穿透整个批次，无法返回逐项报告；relative_to 外路径与中途取消也未闭合。 | [verified_ingest.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/verified_ingest.py:106>) | M12 |
| S08 | 已复现（生产 CPU 路径） | 实际 main→pre→trim→post，应 pre→main→trim→post；单像素非交换向量最大误差 0.0439084，四路顺序均需修正。 | [timeline_export.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:9543>) | M05 |
| S09 | 已复现＋静态确认 | 插件实际在 layer.plugins，版本清单却读根字段，输出空；缺失检查又误用内置音频 registry，并吞掉接口错误。 | [color_versions.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/color_versions.py:109>) | M07 |
| S10 | 已复现（图数据） | RIP 签名漏连线/端口，交换 Merge A/B 后不变；启用态字段需统一，stale helper 尚未接预览/导出守卫。 | [bake_service.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/composite/bake_service.py:43>) | M08 |
| S11 | 静态调用链确认 | Qualifier 动画只在 Color 引擎求值，TimelineExporter 仍只算基础 grade 白名单，不能据 helper 测试声称成片一致。 | [timeline_export.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:9098>) | M06 |
| S12 | 静态调用链确认 | EXR still 写读 API 已有，但 Gallery 设为参考仍调用只读 JPEG 的 load_still_image；float 抓帧链未闭合。 | [layer_controller.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/layer_controller.py:931>) | M09 |

额外三项：M-I01＝本机 pip check 的3处依赖冲突；M-I02＝旧4K窗口解码等待/预取锁争用待重测；M-I03＝监看第一刀后历史48.2ms仍超25fps的40ms预算。后两项依据 [perf_baseline_freeze_mac_2026-10.md](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/perf_baseline_freeze_mac_2026-10.md:135>)，只作为复测优先级，不冒充当前HEAD实测。Windows七项独立问题与修复方案完整列在W计划§1；Mac不修改其自有CUDA实现。

## 2. 可执行工作包

完成定义：真实入口→领域提交→Undo/保存重开→预览/导出→失败/取消/关闭，适用环节全部有证据。API完成可关闭子项，不能直接关闭整包。正式发布不得遗漏当前支持范围内的P1正确性项。

### M00 · 冻结真实完成状态与回归门禁

- [x] 状态：已完成（2026-10-24，HEAD `12b998754`；53 项差量表四桶分列[核验收口8/生产接线30/仅API层7/条件延期2/未开始6]、142 focused 单进程复现零漂移、新增分域子进程门禁脚本+CI 真实阻断、失败/缺口登记含宿主与责任归属）；优先级：P0；前置：—。
- 对应：O01/O02；F0/F1。
- 实施：核对 A/B/F 总表、45 个勾选及其台账，把 API 已实现、生产已接线、用户场景已验收、条件延期分列；校正文档头与旧状态冲突。保留原提交和证据，不能删历史或重写为全绿。登记现有失败 ID、宿主、依赖与责任人。
- 验收/交付：输出逐任务差量表和复现基线；本轮关键正确性/保存/导出 focused suites 设真实阻断，非零失败不能仅靠与旧基线相等放行。CI 分域/子进程避免累积 Qt/C 解码器挂起；不得把新增失败改 skip。证据：[M00 manifest](validation/upgrade_2026-10-24/M00/12b998754/20261024_195658/manifest_M00.md)、[差量表](validation/upgrade_2026-10-24/M00/12b998754/20261024_195658/f_plan_gap_table.json)、[门禁报告](validation/upgrade_2026-10-24/M00/12b998754/20261024_195658/gate_report.json)、门禁脚本 `scripts/m00_focused_gate.py`、CI `.github/workflows/focused-logic-gate.yml`。

### M01 · 隔离并修复 Mac 运行依赖组合

- [x] 状态：已完成（2026-10-24，HEAD `cc97300cb`；三组合[核心/ASR worker/TTS worker]独立环境 pip check=0、Qwen3-ASR-0.6B 真模型小样端到端 PASS、缺栈诊断三类入口验证、constraints 漂移 42 包登记移交 M18、qwen-tts↔qwen-asr 上游互斥与 nemo/pyannote 宿主限制登记）；优先级：P1；前置：M00。证据：[M01 manifest](validation/upgrade_2026-10-24/M01/cc97300cb/20261024_212800/manifest_M01.md)、`requirements-ai-worker.txt`、`requirements-ai-worker-tts.txt`、`scripts/m01_build_envs.sh`。
- 对应：M-I01。
- 实施：在独立验证环境重建核心 UI/视频环境与可选 AI worker 环境，逐项解决 pyannote-core 6.0.1→numpy>=2 对本机1.26.4、lightning2.4→packaging<25 对26.3、qwen-tts0.1.1→transformers4.57.3 对4.57.6 的冲突。核 pyproject/requirements/constraints/CI 的平台与 Python 差异；保留现有 venv 供对照，不直接原地升级。
- 验收/交付：各支持安装组合 pip check=0；核心解码/OCIO/Qt 与已启用 AI 真模型小样通过；冻结包记录实际版本/可选能力。缺可选栈时功能准确诊断，核心应用可用。

### M02 · 完整工程快照、恢复和 IO 保真

- [x] 状态：**Mac 侧已收口**（M02.1 S01 全三缺关闭 `80a38c5ac`，17 用例；M02.2 已收口 `2b12be2c9`：真实素材日志/嵌套/字幕全保真验收联测[真实 2K 素材+中文关键词评分+nested_sequence 双来源身份+子时间线+中文字幕+marker+采样级字段经 .topos 往返逐字段保留；再存再开定点稳定]，2 用例；M15 联动=快照比较面板已挂 M15.1a）；优先级：P1；前置：M00。证据：提交 `80a38c5ac`、`2b12be2c9`、`tests/edit/test_m02_snapshot_io_fidelity.py`。
- 对应：S01；F8.1/F8.2/F8.3。
- 实施：使用正式完整工程包 serializer，保存素材 metadata/评级、真实 sequence 内容、调色/组/版本、Smart Bin 与未知 payload；定义快照表持久化与非递归引用，比较使用实际 dict schema。清查显式 EditProject 构造器是否抹掉新字段，按调用点小提交改用既有 replace 语义。
- 验收/交付：带真实素材日志/嵌套/调色/字幕的工程：快照→修改→恢复→正式保存重开全部保真；新增/移除时间线可比较；恢复经命令可 Undo，过期/损坏快照拒绝，旧工程兼容。先补当前复现再补生产 IO 测试。

### M03 · 外录音同步与所有音频消费者同源

- [x] 状态：**Mac 侧已收口**（2026-10-24 M03.1 已收口：S05 成片/Stem 变速/保调绑定片段不再回退机内声[reader source_offset_seconds 与预览同口径]、S06 TC 帧率折算+方向契约修正，11 回归用例，探针 S06=False；M03.2 已收口 `3396978a3`：DF/NDF 丢帧换算[SMPTE 真跳号显示+DF 逆映射全区间恒等往返+非法码拒绝+非 NTSC 显式拒绝]，5 测试；M03.3 已收口 `0854b8304`：双源混音显式声明[mix_mode 字段+dual_source_mix_declaration 统一契约+dual 显式拒绝]，4 测试；M03.4 已收口 `0a98efcee`：同步诊断持久化[SyncDiagnosticRecord 第七表随工程 JSON/包往返，拒绝的同步也留存=审计事实]，6 测试；M03.5 已收口 `f6647430a`：跨日 TC 偏移声明[24h 环半日阈值归一+wrapped 声明+人工确认 caveat，不静默]，5 测试；波形 worker 化（UI 线程迁移——真实窗口交互验证=M18 域登记））；优先级：P1；前置：M00。证据：提交 `84189f8f9`、`tests/edit/test_m03_bound_audio_retime.py`。
- 对应：S05/S06；F2.6/F2.10。
- 实施：先统一 rational TC/BWF 时钟与 offset 正负契约，支持明确的 fps、DF/NDF、跨日；再让预览、成片、Stem 使用同一个 clip-aware 绑定/变速 resolver。未支持组合入队前明确拒绝，不能静默切回机内声。波形分析移入可取消 worker，generation/revision 防过时提交；同步诊断持久化并预览已有片段影响。
- 验收/交付：机内300Hz/外录700Hz，±offset、24/25/29.97DF、44.1/48k，1×/2×/反放/速度曲线/保调及 trim/split 对拍 PCM 频谱和起点；TC 摄影机晚10秒得到正确正offset。分析取消零提交，UI可操作；双源混音显式声明另做。

### M04 · 正确的显示 PTS、几何解释与序列资产

- [x] 状态：**Mac 侧已收口**（2026-10-24 M04.1 已收口：S04 PTS 索引改读 pts 按显示帧序排序+首帧归一[B帧重排/start_time]，2 回归用例+真实合成 VFR/CFR 对拍 15 绿，探针 S04=False；M04.2 已收口 `7bff2032f`：解释组合按真实消费者定义[rotation/SAR/field_order 覆盖域+consumer 支持矩阵 fps=applied 其余 declared_unapplied 明示]+指纹失效修复[render_cache 漏收解释覆盖→改覆盖缓存仍绿真 bug]，7 测试；M04.3 已收口 `b43b63e20`：通用序列分类接媒体池单资产[classify_import_batch 连号静帧成组（缺口诊断/巧合防护）+导入批每组单个 image_sequence 素材]，7 测试——插入链下游消费 image_sequence 既有路径；真实 VFR+B 帧长片媒体验收=需长片真实素材，M18 实机域登记）；优先级：P1；前置：M00。证据：提交 `cc97300cb`。
- 对应：S04；F5.4/F2.7。
- 实施：用显示帧身份构建 PTS index，处理 B帧重排/start_time/负或缺戳/非零源入点，不能以 demux DTS 充当显示帧序号。把通用序列分类接到媒体池单资产→Source→插入→代理→导出；旋转/SAR/隔行按真实消费者定义组合。解释覆盖变化须纳入 proxy/render cache/denoise/RIP 指纹和失效。
- 验收/交付：真实 VFR+B帧+音频长片顺序与随机 seek 首中尾、代理/原片对拍不漂移；90°/SAR/TFF/BFF几何正确；1001序列缺1007可诊断，连号照片可选单图，保存移盘后不乱帧。未支持组合显式转码/拒绝。

### M05 · 完整调色链、载荷与成员语义

- [x] 状态：**Mac 侧已收口**（M05.1/2a/2b 已收口；M05.2c 已收口 `a52925800`+`fb24f00d8`：HSL 曲线+soft-clip CPU 图像实现[advanced_grade_cpu 与 lut_exporter/shader 同源数学]接入导出+能力矩阵两表同步 video=full——**R02 尾片闭合**；M05.2d 已收口 `dbdd595a4`：3D LUT 文件 CPU 加载消费[apply_lut_file_to_color 公共接缝接入导出主栈 soft-clip 后同位、栈级/layer 级 LUT 载荷与 advanced 检测、交付 fail-fast 不静默缺效果；顺带修复 _apply_3dlut_cpu 上边界 off-by-one——1.0 输入此前查成表 [0,0,0]，与 GLSL 边缘钳制相反]，4 测试；M05.3 已收口 `7a991be18`：组成员编辑策略[split 不自动入组契约/跨时间线汇总/链段赋值视图]；M05.2e 已收口 `7abd96498`：Composite 链段消费[chain_for_stack 生产侧+_apply_topos_color 顺序 ping-pong pass——pre→主→post 经池 scratch 纹理、错误路径归还、高级段跳过同 Edit 语义、standalone GL 顺序测试 8 项]；M05.2f 已收口 `3ed10fb5e`：composite topos_color shader 对齐权威 PRIMARY_GRADE[与 Edit 预览/导出共用 GradingMath GLSL 镜像含暗部保护级，轮子 params_to_rgb 同一转换，GPU 测试与 CPU 权威对拍 contrast35×exposure20 非交换向量五路一致]——包内 GPU 数学与链消费全部收口；剩余：Windows/实机显示一致性=M18）；优先级：P1；硬依赖：M00。
- 对应：S08；R02；F4.1。拆为以下独立子任务，分别记录状态和证据：
- **M05.1 顺序**：解析单点输出 pre→main→trim→post，CPU/GPU预览和导出共用顺序与指纹。以contrast35+exposure20非交换向量消除现有0.0439084误差，不能只改测试helper。
- **M05.2 完整载荷与后端能力**：携带真实Layer/曲线/LUT/soft-clip/遮罩/插件及各链段到Color/Edit/Composite/CPU/GPU导出；逐后端能力矩阵明确支持、需bake、拒绝。只告警后丢效果不能产出成功交付。
- **M05.3 组管理与成员编辑**：Pre/Clip/Post入口、组跟踪归属、split/复制/跨时间线成员策略与领域命令分开实施；共享grade/组链与版本身份不混用。
- 验收/交付：组前曲线＋主栈插件＋微调遮罩＋组后LUT，在上述五类生产消费者逐层及最终像素对拍；空链、负值/超白、split/Undo/保存重开保持预期成员和效果。未支持后端真实预检拒绝或经用户选择bake；Windows同一夹具复验。

### M06 · 二级动画贯通帧级求值和导出

- [x] 状态：**Mac 侧已收口**（M06.1 已收口 `41974641c`；M06.2 已收口 `4d2d1333f`：numpy 版 keyer[qualifier_cpu.py,keyer.glsl 逐条移植：HSL+HDR 归一/hue 环形/sat/lum smoothstep 带/invert]接入 CPU 导出像素级应用，matte 限定整条 layer 调度，后处理段显式告警，LUT 快路径排除 qualifier，9+5 测试；M06.3 已收口 `141a640e9`：0/48/96 帧全链像素对拍[动画 qualifier 三帧导出 vs 逐帧闭式参考全对拍+非平凡性钉住]，2 测试；M06.4 已收口 `b6237b7bc`：遮罩形状组合 CPU 模型层[mask_shapes_cpu——组合/后处理语义与 GPU 管线逐条同源纯 CPU 评估；circle/rect/gradient 解析近似声明、curve/bitmap/brush 显式 unsupported 三口径报告]，9 测试；M06.5 已收口 `e512c48a7`：qualifier GPU/CPU 逐像素对拍[standalone GL 编译真实 keyer.glsl×合成 HSL+HDR 梯度图，四组参数全像素 |Δ|<2e-3+非平凡性]，GL 侧落地；Metal 对拍按 metal_compute_ab 既有评估（默认关）随 M18 实机域）；优先级：P1；前置：M05。
- 对应：S11；F4.5。
- 实施：统一 ColorStack 帧级求值快照，把 qualifier/mask 及动画结果传到 TimelineExporter 等真实消费者；不能仅复用排除 qualifier 的基础白名单。明确源帧/时间线/变速域及插件参数可动画矩阵，暂未支持后端入队前明示。
- 验收/交付：动画限定器在0/48/96帧、非零入点、随机seek、反放/2×下，Color/时间线预览/CPU与GPU成片遮罩和像素一致；保存/Undo/逐参数复制生效。饱和度/色相与点集拓扑按后续需求逐域扩展。

### M07 · 插件版本清单、共享锁和版本入口

- [x] 状态：**Mac 侧已收口**（M07.1 S09 清单 `cdf14321a`；M07.2 版本工具面板[列表/保存/激活/锁标] `ca5359b0a`+菜单接线 `b19c946b3`；M07.4 分享后自动锁已收口 `78442098f`：Use Shared Grade 成功后 shared_stack_ids 逐栈经命令栈 SetStackLockCommand[此前 lock 零生产调用]，2 测试；M07.5 版本比较已收口 `442313358`：compare_versions 逐键差异+插件差集+面板多选比较，4 测试；剩余：Mac/Windows 缺依赖一致性复验=M18）；优先级：P1；硬依赖：M00。证据：提交 `cdf14321a`、`tests/edit/test_m07_plugin_manifest.py`。
- 对应：S09；F4.2。
- 实施：遍历真实layer.plugins，复用Color插件身份/版本registry；修错误的音频registry解包，探测失败标unverified而非无缺失。版本列表/激活/比较/锁标与分享后自动锁，各作为独立入口子项，按具体构造器检查字段保留。
- 验收/交付：真实.tglsl/.tai/DCTL载荷保存版本→缺插件/升级版本→激活/正式重开，状态与参数保留且诊断明确；影响范围确认，锁定后非授权写入零提交；Mac/Windows缺依赖结果一致。

### M08 · 烘焙签名、生产协调器与发布事务

- [x] 状态：**Mac 侧已收口**（M08.1a/1b+M08.2 协调器已收口；M08.2b 已收口 `a18351cbe`：BakeProgressDialog UI[六阶段文案+帧进度+取消+失败原因呈现]；M08.2c 菜单接线已收口 `b19c946b3`：Composite/Render-in-Place 菜单入口；M08.1c 已收口 `586f53f5f`：预览/重开守卫+徽标[RenderPlan.bake_staleness preview/export 双模式审计+渲染线程签名去重信号+工具栏徽标 tooltip 逐节点 reason]，4 测试——重开路径经 plan 审计同一入口覆盖；剩余：Windows 实测文件锁/rename 失败=M18 延期门在案）；优先级：P1；硬依赖：M00。
- 对应：S10；R09；F7.3。分两个独立子包：
- **M08.1 签名与消费守卫**：规范化上游边、端口/顺序、真实disabled/enable状态、媒体身份与参数；锥外不误失效。将bake_is_stale接预览/导出/重开，重烘焙/回原图/阻断策略可见，补徽标。
- **M08.2 Bake协调器**：冻结revision/源签名→独占临时序列→逐帧实际解码/帧数/格式校验→manifest→可恢复发布→领域rewrite/Undo。复用disk_cache_store已有临时文件/replace/manifest能力，不另建平行缓存；产物完整前原图继续有效。
- 验收/交付：换Merge/Subtract端口必须stale；第N帧写失败、磁盘满、取消、窗口关闭、源图中途变化、重开恢复均不发布半套bake，不覆盖旧成功产物。图rewrite有一次Undo；临时清理只动本任务文件。Windows实测文件锁/rename失败。

### M09 · HDR still 的真实抓取、四通道加载与比较

- [x] 状态：**Mac 侧已收口**（M09.1 参考加载 EXR 优先 `5bb00a9c5`；M09.2 抓帧 float 链闭合 `7802a1c8f`——S12 全链贯通；M09.3 已收口 `2d08797a5`：四通道 alpha 声明+Lightbox[alpha_semantics 声明 sidecar 往返、straight↔premultiplied 转域族、undeclared 拒绝不猜、StillLightboxDialog 并排/差异+精度/语义徽标]，8 测试+顺带清理重复 write_still_exr 死定义；剩余：真实窗口抓 4.5/透明彩边人工验收=M18 实机域）；优先级：P1；硬依赖：M00。
- 对应：S12；F4.3。
- 实施：渲染线程float抓帧→EXR/metadata→Gallery优先高精度加载→锁定参考显示变换→wipe/difference；替换生产入口仅读JPEG的链路。分别验RGB与RGBA：四通道上传、alpha保持、straight/premultiplied声明和比较域；精度/旧JPEG降级可见。Lightbox/参考对话框复用现有模型。
- 验收/交付：真实窗口抓4.5/-0.25与透明彩边/alpha梯度，重开后设参考确实消费EXR；同帧difference近零，透明区与边缘不被错误预乘，ODT不重复。缺EXR后端给准确诊断，JPEG截图不证明HDR。

### M10 · 多版本交付接真实类型、队列与恢复

- [x] 状态：**Mac 侧已收口**（M10.1 S03 画幅/离线硬门 `1359b9836`+`9660c23c2`；M10.2 序列恢复接 VerifiedFrameManifest `3b0dd77be`；M10.3 已收口 `8661daeb3`：字幕语言字段入队消费者[SubtitleConfig.language 经 from_flat_config/快照往返] + 入队快照 revision 硬门[过期明示拒绝/零 revision 兼容]，5 测试；UI 入口=Deliver 队列面板既有+批次导出 dock 在案）；优先级：P1；硬依赖：M00。证据：提交 `1359b9836`。
- 对应：S03；F9.3/F6.4。
- 实施：编译输出进入真实VideoExportJob/schema和to_timeline_export_config，统一resolution、字幕语言/策略及源版本字段；拒绝清空整批jobs，入队/恢复消费者实际校验revision/snapshot。图像序列恢复入口接VerifiedFrameManifest。
- 验收/交付：3840×2160母版派生9:16后真实runner为1080×1920；离线/未知画幅/命名冲突零入队。暂停重启、改源版本、损坏帧不误续渲。PNG/TIFF已声明位深与straight/premultiplied RGBA、透明彩边、非零帧号逐像素回导；EXR/DPX负值/超白及标签按已声明子集对拍，AOV扩展按需求，不能用帧数正确代替像素/alpha正确。

### M11 · 文稿删除走真实命令编译与事务

- [x] 状态：**Mac 侧已收口**（2026-10-24 M11.1 已收口：S02 RangeRippleDeleteCommand 域命令[split×2+ripple 中段，按起点定位解析 id，整轨快照 undo]+plan_compiler 区间语义+build_filler_removal_plan 真实 track/clip 映射[从右到左发射保持坐标有效]，detect→build→compile→apply→undo 端到端真实 compiler 通过；M11.2 面板 `0a6211b70`+菜单 `b19c946b3`；M11.3 已收口 `c3b108852`：差异预览[勾选→真实 clip/帧区间逐 op 呈现→审批同一构建链]，3 测试；M11.4 已收口 `d17c121c6`：跨容器映射[容器内命中显式跳过带原因——此前顶层 ripple 波及整个容器]，3 测试；字幕/marker 同步联验（随真实工程素材=M18 域登记））；优先级：P1；硬依赖：M00。证据：提交 `88ad1dcfa`。
- 对应：S02；F9.1。
- 实施：从canonical snapshot映射词时间到真实track/clip/source区间，以既有split/区间删除/ripple命令生成合法计划，不用伪clip_id。接选择/差异预览/审批/执行入口，复杂容器支持范围明示。
- 验收/交付：detect→build→compile→preview diff→apply→Undo，删除仅影响目标词；链接A/V、字幕/marker同步，低置信词可排除；过时revision拒绝，取消/重试不重复剪。先验现成快照接口，再扩命名版本/外录场景。

### M12 · 安全入库、代理清理与可靠重连接线

- [x] 状态：**Mac 侧已收口**（M12.1a 入库逐项报告 `d7dc61da0`；M12.2 代理/缓存清理面板 `5e2a5d663`+目录迁移预检 `ea0bae358`+菜单接线 `b19c946b3`；M12.3 重连可靠匹配 `cac4046be`；M12.3b 已收口 `ea8f06376`：auto search 按钮同款接线[索引预过滤+命中目录三级验证+人工核验明示，AUD-20509 防护保留]，2 测试）；优先级：P1；硬依赖：M00。
- 对应：S07；R11；F6.2/F6.5/F6.6。三项独立记录状态、分别提交：
- **M12.1 入库**：源hash/copy异常纳入逐项结果，chunk取消、源/目标路径边界、独占临时名与commit前不覆盖契约；cancelled/skipped/failed/verified语义分开，取消报告不能因无failed就显示全部成功。
- **M12.2 代理与缓存**：报告/安全清理UI、活动/跨项目引用保护、目录迁移、低磁盘/配额与解释变化失效。性能独立测，复用现有manager。
- **M12.3 重连**：可靠匹配器替换basename-only对话框路径，候选置信/冲突由用户确认，变速/嵌套切点保持。
- 验收/交付：第一项成功、第二项hash读失败、第三项继续/跳过有完整报告；hash中途取消可及时退出。源卡外路径拒绝，两个任务争同一目标或commit间出现同名良好文件时不覆盖。播放中清理不删原片/活动代理；同名异内容不自动错连。

### M13 · 剪辑、标题与多机位交互收尾

- [x] 状态：**Mac 侧已收口**（M13.1–M13.6 模型层已收口：source_patch_matrix[布线矩阵/混合值/边界导航 `6928da8e5`]、keyframe_curves[↑↓导航+Bezier 插值 `966a47160`]、title_animation[roll/cue 淡变/批量样式 `cc0877c0b`+`6c801d719`]、multicam_adaptive[自适应降档策略+DPI 恢复 `96d1ab61b`]，38 测试；M13.7 已收口 `d8fc61782`：cue 动画渲染消费接线[SubtitleClip.animation 字段+apply_cue_animation（roll/淡入淡出，title_animation 模型层首个渲染消费者）]，5 测试；M13.8 已收口 `a62a3dedb`：多机位降档策略+DPI 恢复页面入口[策略徽标明示+tile 高度按档缩放+dock 恢复前置可视比判定——M13.5/6 模型层首次生产消费]，4 测试；M13.9 已收口 `e59566c2f`：布线矩阵对话框入口[channel_matrix 首个生产消费+稀疏 dst 目标宽修复——中间未映射行显式静音]，4 测试；剩余：真实窗口联测=M18 实机域）；优先级：P2；前置：M03、M04。
- 对应：F1.4/F2.1–F2.9。
- 实施：按§3逐项小包：源声道map透传与完整patch矩阵/轨头引脚；连续JKL修剪、音频预听与预后卷/左帧；关键帧前后导航和曲线插值UI；标题roll/cue动画/恢复同步/批量样式；混合值和production-info批量；多屏DPI/拔屏恢复。四路网格先真实窗口联验，再评估九路降档。
- 子任务分别登记：**M13.1**源patch/混合值；**M13.2**JKL/预后卷/左帧；**M13.3**关键帧导航/曲线；**M13.4**标题roll/样式/同步；**M13.5**多机位真实窗口与降档；**M13.6**多屏DPI/焦点。ShotMatch由M22负责，COMP跟踪/Roto由M23负责，不能算进本包泛UI验收。
- 验收/交付：各入口有可录操作脚本和保存/Undo/成片证据。文本焦点不误触；动画实际改变最终像素；标题横竖画幅/字体替代由用户明确选择；九路无法达标时显示降档状态，不承诺所有机器满帧。

### M14 · 声音精度、嵌套与 bus 语义收尾

- [x] 状态：**Mac 侧已收口**（M14.1 `020c1bd32`；M14.2a `da15525cc`、M14.2b 已收口 `ca91ff383`：crossfade 增益包络[linear/equal-power 半周期正弦]+Stem 声明语义，6 测试；M14.3a `2ba6bacd2`；M14.3b 已收口 `979c6825d`：工程采样率字段接 EditProject[JSON 序列化双向+工程包转换器 settings 往返，旧工程缺键 0=未设置→访问器解析 48000]，4 往返测试+重复 color_groups 定义清理；M14.3c 已收口 `325f97466`：采样级 UI 微调[音频属性面板 clip 级 audio/source_sample_offset 可编辑行→UpdateClipParamCommand 可 Undo，@48k 参考率语义 tooltip 声明]，3 测试；剩余：录音磁盘满/拔设备隔离注入与热切换明示=实机门 M18、Windows 句柄释放复验=M18）；优先级：P1；前置：M03、M04。
- 对应：F3.1–F3.4/F2.6。
- 实施：先统一嵌套父retime/混合fps的视频音频映射与crossfade，不单修音频；统一GROUP/TRACK Stem的bus/send贡献。再单独落地采样级UI、retime子帧对齐/crossfade、工程sample-rate字段；同步漂移矫正/素材marker按模型契约推进。实时保调曲线/transport倍率和监听设备分开立项。
- 补充子任务：**M14.1**冻结split时左右片sample offset归属、取整与源连续性；**M14.2**嵌套/retime/crossfade/Stem语义；**M14.3**采样级UI和sample-rate迁移，各自关闭。
- 验收/交付：NTSC下非整帧采样点split/trim→Undo/重开→成片，左右片总样本数连续，不重读/漏样本；PCM样本数/边界、对白听感、全静音+target、Stem与成片routing一致；不支持嵌套组合继续显式拒绝。录音磁盘满/拔设备使用隔离注入；热切换未实现时明示停录重建；Windows验证句柄释放与文件可移动。

### M15 · 审片评论、候选 take 与版本产品闭环

- [x] 状态：**Mac 侧已收口**（M15.2 `35d0e0966`：R10 关闭；M15.3 `f006503ff`：fit 契约；M15.1a 已收口 `3cdb3d251`：SnapshotComparePanel 版本比较 UI[compare_snapshots 真实链+差异条目/summary]；M15.1b 已收口 `a0e6d2d07`：自动备份调度器[revision 前进才拍/单槽滚动备份点/失败不崩防风暴]+菜单接线 `b19c946b3`）；优先级：P2；前置：M02。
- 对应：F8.1/F8.2/F8.3。
- 实施：依次接命名版本/自动备份调度与比较面板、评论的revision锚点和marker命令、Audition工程字段/IO/预览/导出和切换入口。复用已有模型，不另建平行版本系统；修改共享schema由本任务串行持有。
- 补充子任务：**M15.1**版本/比较/备份入口；**M15.2**评论优先clip lineage与version_hash，多次复用同源窗、freeze/变速/删除原锚时返回ambiguous/stale，不能把首个同源片段标exact；**M15.3**Audition持久化fit有效时长、短take补尾/留洞规则与离线活动take回退确认，各自关闭。
- 验收/交付：重复同源窗评论不跳错occurrence；take长→短→长、离线→恢复的时长/活动源/输出一致。真实工程评论定位、候选切换、版本恢复各有一次Undo/重开/成片验证；跨split/retime锚点可追溯；旧工程/未知payload不丢，工程版本与调色版本清楚区分。

### M16 · 重测并消除真实4K解码等待热点

- [ ] 状态：进行中（M16.1 首批已收口 `b22d2c15f`：decode-only 离屏复测[3 素材 ok，VT zero-copy 路由健康零 CPU 回退，decode_ms p50 口径登记，真实窗口裁决待安静机]，manifest 在案；M16.2 已收口 `f109c328e`：分域指标+worker 生命周期离屏[DecodeMetrics display/prefetch 两域锁等待/解码/命中/重复开源计数接管理器；CPU fallback worker cancel→in-flight→close→join 生命周期 8 测试]；M16.3a 已收口 `c70d4c315`：大工程打开保存/搜索响应基线离屏[60/600 片段双档实测+复杂度守卫，10×数据≈3×无二次方路径]；**M16.1b+M16.3b 已收口 `5e54134c5`：真实窗口三相位矩阵+冷启动实机基线（用户 2026-10-25 提供真实素材 tests/test_videos 后复开执行）**[bench_m16_real_window.py：冷首帧/seek 矩阵/连续播放三相位×生产路由顺序（Topos 嗅探→VT 零拷贝→VT planar→ffmpeg）×5 素材×5 组交错轮转、每素材独立子进程（验收"独立进程"口径+隔离顺序窗口桥/GL 名空间跨上下文污染）——25/25 组零错误零掉帧 overall PASS；harness 补 Topos source 落地（topos_native 路由+dylib sha256 登记）；冷首帧 p50 193–719ms、seek p50 31–166ms、播放帧 p95 45–133ms、冷启动 p50 7.6s；唯一热点候选=4K H265 decode p95 99ms（重排尾帧，p50 在预算内零掉帧，按"证据显示缺口才改代码"纪律登记后续单 item 轮不动代码）；DNxHR VT 回退原因 unsupported_codec:dnxhd 如实记录走 ffmpeg_planar]；剩余：H265 尾部单 item 优化轮（候选在案）+2h/10h 长压扩档（Windows 侧 W08 首档 600s+1800s+100×重启已在案，Mac 侧随发布节奏））；优先级：P1；前置：M00；比较环境依赖M01。
- 对应：M-I02；A1.3/A1.4/A2.4。
- 实施：先复跑当前HEAD的真实窗口冷首帧/seek/连续播放与decode-only，分出锁等待/重复解码/命中率；历史38.2ms不是当前结论。一次只试一项：管理器预取锁粒度或scheduler/ring接线，保留generation/取消/租约与现有音频时钟。harness补Topos source，登记实际native库，不先改语言。
- 补充子任务：分别记录**M16.1**播放锁/调度热点、**M16.2**source_viewer/decoder worker的cancel→in-flight结束→显式close→join生命周期、**M16.3**冷启动/大工程打开保存/素材搜索缩略图的响应基线。后两项只在证据显示当前缺口时改代码，不将“全量测试累积挂起”直接等同于生产已挂起。
- 验收/交付：独立进程复现与组合运行分别统计，重复开关工程后worker/decoder/句柄回到基线，关闭不依赖GC；10倍素材/片段数的打开保存/搜索按时间复杂度和UI响应实测，阈值开工冻结。同素材/环境至少5组交错对比，主要指标为显示间隔p95/掉帧、首帧/seek、CPU/RSS；预登记限值，噪声内不宣称提升。补无音轨、漂移、seek风暴、反放/速率/rebuffer与关闭；实际Window和离屏结果分列。

### M17 · 监看缩放回读与设备质量

- [x] 状态：**Mac 侧已收口**（M17.1a UYVY 打包候选 `1de6da2b7`，3 测试；M17.1b 已收口 `938d843f2`：GPU 按监看目标缩放后回读候选[gpu_scale_readback 双线性+回读字节 33MB→8.2MB 口径+GPU-vs-CPU 同族对拍 5 测试，离屏 A/B 度量与真实窗口复测同口径]；剩余：监看开/关 A/B 真实窗口裁决+DeckLink 时序/letterbox/range 对拍——**需真机**（deferred_gates 在案，软件子项全交付））；优先级：P1；前置：M00、M05；硬件子项需设备。
- 对应：M-I03；A1.2/F5.2。
- 实施：重测监看开/关；确认热点仍在后，先GPU按监看目标缩放再回读，减少33MB整帧回读和CPU resize；UYVY打包作为独立候选。复用现有背压/线程释放，不把DeckLink GPU占位接口算实现。
- 验收/交付：letterbox/range/色彩/位深/stride/奇数宽/alpha/时序对拍；监看尺寸变化/取消/拔设备恢复正确。无DeckLink/校准设备仅关闭软件子项，F5.2硬件项仍待验；实际输出质量不能由截图推断。

### M18 · 跨机黄金工程、安装包与专业交接门禁

- [ ] 状态：进行中（Mac 侧集成件已具备[黄金工程 builder/三链模型层/发布事务/QC/归档闭包]；**M18.3 已收口 `6c953abbc`：黄金链 3（多画幅多语言交付+工程恢复）Mac 侧端到端集成**[编译→保存→重开→再编译三版本定点一致+revision 前进失效旧溯源+归档闭包自洽可再编译，3 测试]；**M18.4 已收口 `c70d4c315`：打包库 OS/架构/hash/ABI Mac 侧审计**[纯 Python Mach-O 解析+全 bundle 1523 文件实测全 x86_64 单架构零畸形+sha256 manifest 落 validation/M18/bundle_abi/，6 测试]；**M18.4b 已交付 `4f80a27bd`：机器 B 复验接入包**[Windows 侧 PE ABI 审计模块 6 测试+一键复验脚本 m18_machine_b_recheck.py+接入 README，见 validation/M18/machine_B/]；**B 机以其自有计划 W 执行完毕并回传：W00–W10 全 11 包（2026-10-25，合并 `36048dfd4`，M+W 两线首次同枝）**[W01 DLL zig9 重建 SHA256+12 符号 ABI 门+PE machine 校验/W02 生命周期回滚/W03 运行中降级恢复/W04 设备匹配+平面几何门/W05 快照 0 容差+present fence 修复/W06 extras+干净 venv 两态（冻结 exe 未建如实登记）/W07 CI 收口+9 项跨平台缺陷+红基线 76→0/W08 真实长压 600s+1800s+100×重启全绿/W09 固定策略 A/B/W10 跨机归档适配——与 M18 各门的映射见 deferred_gates"W 线证据并入"表]；**Mac 集成侧接收收口 `5e54134c5`**[合并后聚焦门禁 GATE PASS+W 改动测试 Mac 全量重跑绿、W01 loader PE 判定全平台接缝修复、W07 luts 契约文件真正入库]——W07 登记的 S01–S12/黄金链 Windows 分发验收**前提自此齐备**（M+W 同枝），B 机二轮执行仍为外部动作（testfarm 指令有效：pull fix-ui→run_m18_recheck.sh→commit 回传）；验收门剩余：B 机二轮分发验收、Windows 冻结包 PE 全量审计（W01 组件级证据在案）、实机往返/AAF/DeckLink/RIFE/2h-10h 长压——见 [deferred_gates](validation/upgrade_2026-10-24/M18/0a6211b70/deferred_gates.md)，未冒称通过）；优先级：P0（发布前）；硬依赖：M00；其余只按发布声明的支持子集验收。M19软件色彩、M20归档、M21发布/QC等影响该支持子集的正确性须过门；M22/M23/M24新增模式按能力声明逐项门禁，不要求等所有可选扩展。
- 对应：O02；F5/F6/F7/F9。
- 实施：建立三条小型真实生产链：外录+变速，组调色+动画+HDR参考，多画幅多语言交付+工程恢复；Mac执行并交Windows复验。归档依赖收集/重写由M20实施、发布事务/QC由M21实施、交换字段映射由M24实施，本包只做集成验收；验证打包库的OS/架构/hash/ABI；干净配置安装包可打开/导出同一工程。FCPXML/OTIO用真实外部软件生成和接收，AAF等按条件门禁。
- 验收/交付：保存恢复/最终像素/PCM/依赖清单/错误恢复有报告；纯逻辑142绿不替代这些门。发丝/半透明、运动遮挡/切镜、RIFE模型在位、真实复杂图基准补质量证据；设备/SDK/接收端缺失单列未验收，核心已验收版本可独立交付。

### M19 · 软件色彩转换、三页显示与Scope一致性

- [x] 状态：**Mac 侧已收口**（M19.1+1b R01 转绿；M19.2a 三页上下文 `1872dc6b8`、M19.2b 注入时机生命周期已收口 `cdd929f6b`：加载注入[空/None=清空回全局偏好]/关闭清空/切换全量替换[非 merge，A 独有键不残留]契约+控制器三处实体接线[close/auto-create/无工程会话]，5 测试；M19.3 测量域声明 `80cdf056d`、M19.3b UI 角标已收口 `cedb1114a`：Scope 选择器行 domain_badge[域/采样/单位三要素+tooltip 完整声明，标尺/采样口径变化同步刷新]，3 测试；M19.4 已收口 `5ef012a29`：PQ/HLG 参考向量冻结[ST 2084/BT.2100 常数+冻结锚点表+往返测试]；三页读数×向量对拍联测已收口 `9ae28e56d`：[四路读出一致/上下文清除回退/PQ/HLG 描述符↔冻结 EOTF 锚点对拍]，4 测试；剩余：Windows W04 矩阵跨 VT/CUDA 对拍+DeckLink=设备门 M17/M18 在案）；优先级：P1；硬依赖：M00。
- 对应：R01；F5.1/F5.2；Windows W04/W07；源码：`src/shared/color/color_transform_provider.py:664`（可移植索引按审查commit解析）。
- 实施：分子包：provider的processor缺失/非法形状/执行失败返回明确typed结果或抛领域错误，必要转换未完成时预检拒绝或明示受支持降级；不能返回原输入却标成功。抽共享IDT/ODT及偏好，Color/Edit/Composite用同一工作/显示配置；保留Scope独立全帧采样通路，明示测量在IDT/ODT/LUT前后、单位及几何域。
- 验收/交付：processor缺失与flat RGB非法形状注入不冒充转换；RGB/RGBA、负值/超白同域处理。PQ/HLG、full/limited、code值/IRE/nits参考向量冻结reference white/峰值/EOTF/OOTF条件，三页预览与输出一致。软件验收不等DeckLink；真实外部平面YUV数值按W04矩阵跨VT/CUDA/下载/CPU对拍。

### M20 · 归档依赖闭包与两机脱机恢复

- [x] 状态：**Mac 侧已收口**（M20.1a `2f5c4d6cc`：R03 关闭；M20.2a 已收口 `3489ffba9`+`d9b0f45ca`：归档闭包纳入 LUT 外部文件[栈级+layer级 lut_path 收集进 luts/ 目录，缺失明示]；M20.2b 已收口 `7e662f428`：字体/插件/模型身份闭包[dependencies.json 随归档——字体族名+本机 best-effort 解析身份（不分发字体文件，许可）、插件 id/version/content_hash、AI 模型注册表状态；未解析/缺失逐项警告不静默换]，3 测试；M20.2c 已收口 `91abd182f`：LUT 路径重写进归档工程载荷[栈级+layer 级指向包内 luts/，缺失保持原位带警告]，2 测试；跨 OS 实测=M18 域登记）；优先级：P1；硬依赖：M00；条件依赖M02/M15的新快照或候选schema、M04的通用序列。
- 对应：R03；F6.2；Windows W10；源码：`src/features/edit/core/asset_usage_index.py:102`（可移植索引按审查commit解析）。
- 实施：从直接clip引用扩为依赖图：外录audio_sync_bindings、声明保留的候选/快照源、嵌套/序列全文件集、LUT/字体/插件/模型身份。收集允许分发的文件并重写为包内路径；不可复制项保留身份/版本与准确缺失诊断。目录/序列manifest规范相对分隔符、Unicode/大小写冲突、hash算法版本，不能以本机绝对路径作身份。
- 验收/交付：include_unused=False仍包含只经绑定使用的WAV；卸载原素材盘，Mac→Windows→Mac仅靠归档包恢复，像素/PCM与原工程一致。中文/空格/NFC-NFD/大小写同名/CON等目标非法名、长路径/UNC有映射或明确拒绝，不覆盖/错连；LUT路径已重写，依赖丢失不静默换字体/换源。

### M21 · 输出发布事务、真实QC与用户报告

- [x] 状态：**Mac 侧已收口**（M21.1a/1b/1c 发布事务三件已收口；M21.1b+ 已收口 `7ec8f0608`：编码器 sidecar 产出接线[SIDECAR 模式编码前 .srt 产出到临时文件失败即零发布；成片成功后经 publish_file_group 成组就位；取消/失败路径临时清理]，5 测试；M21.2a QC producer `4b8ce1ae2`；M21.2b 已收口 `091c9915c`：逐版本 LUFS/字幕 QC 批量[measure_version_lufs 逐文件实测+偏差、探测失败≠通过；subtitle_qc_summary 逐版本 inspect 汇总]；M21.2c 已收口 `b494ac9ed`：多版本 QC 面板呈现[MultiVersionQcPanel 只读呈现——未测不冒充 0/失败显式；注册表+Deliver/Quality 菜单实体挂载]，8 测试；QC 接批导出运行侧已收口 `2e883f6de`：排空点逐版本 LUFS 实测→信号→主窗口缓存→QC 面板闭环[失败显式≠通过；字幕段未测不冒充]；Windows 文件锁复验=M18 域登记）；优先级：P1；硬依赖：M00；条件依赖M10的多版本字段，基础单任务可先修。
- 对应：R04/R05；F9.2/F9.3/F6.4；Windows W07/W10；源码：`src/shared/export/video_encoder.py:897`（可移植索引按审查commit解析）。
- 实施：M21.1：各编码器复用临时产物策略，输出路径预留/独占，编码→flush/trailer→实际解码校验→QC→manifest后发布；媒体/sidecar/manifest用发布状态机保证同一成功身份，多文件不能假设一次rename全原子。序列恢复按源版本/配置隔离，失败不混新旧帧。M21.2：黑/意外静/坏帧、PCM音轨、逐版本LUFS/true peak和字幕QC接真实测量producer→裁决→UI/manifest/workflow run_summary，未测标unknown，不用默认0冒充。
- 验收/交付：存在同名旧成片时，open/中途写/flush/trailer/probe/manifest/rename任一步失败或取消保留旧成片，只清本任务临时文件；双任务争输出明确拒绝/排队，Windows文件锁失败可恢复。注入坏/静/黑帧、丢音轨、响度超标、字幕越界，报告准确指向版本和区间，失败不能显示交付成功；合法黑场/冻结有可审计排除。

### M22 · 镜头匹配的预览、人工确认与真实提交

- [x] 状态：**Mac 侧已收口**（M22.1 面板 `fa16fd269`；M22.2 测量回调 `8c40e50fc`；M22.3 已收口 `23c1e28dd`：GradeDiff 差异度量[曝光档/色温/色调/饱和比+summary]；菜单挂接经统一注册表 `34c575d3a`；**R07 尾片闭合**（before/after 预览帧由回调+GradeDiff 配套）；页面菜单栏实体挂载已收口 `b19c946b3`：Color/Batch Tools→Batch Shot Match 进 MenuToolbarMixin 实际回调链[headless 安全，需工程上下文]——面板/测量回调/GradeDiff/命令链/菜单入口全通；12 镜头真实 UI 人工验收属 M18 实机验收域）；优先级：P2；硬依赖：M00；条件依赖M05/M07/M09的组链、版本或still引用模式。
- 对应：R07；F4.4；源码：`src/features/color/core/shot_match.py:283`（可移植索引按审查commit解析）。
- 实施：接已有plan_shot_match/ApplyShotMatchCommand到前台：选参考版本/解释锁、逐镜头勾选/排除、before/after实际帧与GradeDiff、低置信确认、一次事务和结果度量。只接通现有算法；更复杂分布匹配算法独立按样片需求升级。
- 验收/交付：两机位同场景12镜头，从真实UI预览→批准→命令→Undo→重开→成片一致；锁定/品牌色/极端光色排除可见，参考过期拒绝、取消零写入、人工修正保留。当前未接入口不能仅凭服务测试关闭。

### M23 · Composite跟踪修正与智能遮罩前台入口

- [x] 状态：**Mac 侧已收口**（M23.1 领域服务+M23.1b 面板已收口；M23.2 已收口 `f2e2b7425`：detector/ML 模型可用性探测[NEEDS_DETECTOR/MODEL_MISSING/AVAILABLE 三态，UI 不冒充]；菜单挂接经注册表 `34c575d3a`——**R08 尾片闭合**（真实 detector 注入按许可条件=M23.2b 登记延期，条件满足后单独立项））；优先级：P2；硬依赖：M00；条件依赖模型/资源/许可和M06动画组合。
- 对应：R08；F7.1；源码：`src/features/composite/nodes/face_track_node.py:146`（可移植索引按审查commit解析）。
- 实施：M23.1先接已有TrackerNode手工修正/失败段增量重跟到节点属性与领域提交；M23.2人脸detector和ML Roto选择/锚点/传播按真实模型可用性接入。未满足条件时UI明确NEEDS_DETECTOR/不可用，透传图像不冒充检测成功；时域worker取消与过时结果拒收复用既有服务。
- 验收/交付：真实Analyze→选锚→修正→传播，遮挡/切镜失败有原因；取消/关闭零半提交，一次Undo、保存重开/成片一致。发丝/半透明边缘容差、模型不可用和GPU性能分开验；模型未具备可延期子项，不能同时声明用户功能完成。

### M24 · 专业交换的精度、版本与不支持项映射

- [x] 状态：**Mac 侧已收口**（2026-10-24 M24.1 已收口 `42f79811d`：R06 工程采样率随 FCPXML 包往返[导入解析 format@audioRate 含 48k 缩写/非法回退+诊断，导出写回 audioRate]，4 测试；M24.2 已收口 `38b8b3dec`：子帧 offset 有理时基映射细则[有理秒→(整帧,样本偏移@声明率)精确分解/合成互逆、48k 参考率整数执行+精确残差记账、导入挂 audio_sample_offset、导出合成有理秒、往返定点稳定]，6 测试；M24.3 已收口 `30c12c245`：活动调色版本字段随包[导出 options[color_versions]→asset-clip@note topos:grade= 标记+对拍声明条目、导入解析进 clip._extra_fields 往返、无版本表输出零变化]，4 测试；AAF 依赖门已登记：AAF SDK 属商业授权（Avid），按计划规则不自动安装/不宣称支持——启用条件=用户提供合法 SDK 授权后单独立项；外部软件实机往返=M24.4+/M18 域登记；AAF SDK 授权条件在案）；优先级：P1（已声明格式）；硬依赖：M00；新增子帧或调色版本映射分别依赖M14/M07字段契约，不等待整包。
- 对应：R06；F6.1/F6.3；Windows W07；源码：`src/features/edit/adapters/fcpxml_adapter.py:639`（可移植索引按审查commit解析）。
- 实施：为FCPXML/OTIO等已声明子集明确子帧offset/实际采样率、有理时间基、活动调色版本与不可表达效果的映射；区分项目/导出采样率与源采样率：项目确为48k时固定输出可以合法，新增sample-offset必须按其声明时基转换，不能被48000默认值掩盖。不支持字段逐项报告bake/flatten/保留metadata/拒绝，由用户选择，不能漏字段后报告无损。AAF/SDK仍按原独立门禁，不自动安装或宣称支持。
- 验收/交付：NTSC、非零子帧偏移、非48k音源、指定活动grade、嵌套/转场往返，外部软件按切点/源帧/PCM/有效grade对拍；元数据保留与实际可渲染效果分别声明。M18只负责X级集成验收，adapter差量由本任务实施。

## 3. 旧台账剩余项的完整归属

本表把已读台账剩余项归到新任务，避免只修新bug而丢掉旧“后续片”。原文存于 [42份台账、46节剩余项摘录](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/validation/upgrade_2026-10-24/prior_plan_residuals.json>)；原台账仍为细节依据。下列是排程组，每个子功能独立实施/关闭，不把整行当一次大改。

| 原任务 | 剩余范围/边界 | 新归属 |
|---|---|---|
| F0/F1.1–F1.3 | 基线/失败ID/真实门禁、异步close/跨测试worker和C级挂起、磁盘满隔离故障/Windows文件锁；不能把全量楔死简单当无关环境。 | M00、M18；W01/W07/W08 |
| F1.4 | Tangent真机、DPI变化/运行拔屏监听、工作区死字段。 | M13、M18；W08 |
| F2.1/F2.2 | 完整patch矩阵/轨头引脚/混合值、媒体池双击map透传、fit-to-fill保调、Alt+X范围清除。 | M13、M14 |
| F2.3/F2.4 | 连续JKL/预后卷/普通trim左边界帧、关键帧导航/插值UI、rotation范围和pivot动画声明。 | M13 |
| F2.5 | 四路GUI+Program同跑、缺路/seek补帧、VT session池；九路降档和线程策略须负载证据。 | M13、M16；W08/升级UW01 |
| F2.6/F2.10 | TC时间基/DF持久化、组级置信/漂移、重同步影响、漂移矫正/素材marker；波形worker/波形图外录源/双源混音。 | M03、M14 |
| F2.7/F5.4 | 混合值与production-info批量、解释覆盖/换源提示、缓存指纹；真实几何链、VFR音频与通用序列插入。 | M04、M12、M13 |
| F2.8/F3.2 | 受控光流provider、frame_blend质量样片、实时反放/速度曲线/transport保调与人工监听。 | M03、M14；升级UM02 |
| F2.9/F9.2 | 滚动片尾/cue动画、同步恢复UI/批量风格、画幅预设/字号/断行、明确字体替代。 | M10/M13画幅/样式；M21字幕QC汇总；M18验收 |
| F3.1/F3.3/F3.4 | 父retime/混合fps嵌套AV/crossfade、ref-only孙序列；子帧对齐/淡化/UI/采样率；GROUP bus、静音target、监听/设备、LUFS降级、surround/duck。 | M02、M14、M18 |
| F4.1/F4.2/F4.5 | 组UI/高级链段、真实GPU对拍；版本/锁/构造器字段保留；qualifier其余域/点集拓扑。 | M02、M05–M07 |
| F4.3/F4.4 | float抓帧/ODT/EXR参考/Lightbox；批量匹配预览面板、实际镜头质量/分布匹配升级。 | M09、M22；M18验收 |
| F4.6/F5.1/F5.2 | 现有soft-clip不等于完整HDR分区轮；OCIO异常恒等降级/显示编码一致性、Scope单位与采样位置、HDR预览统一、校准/DeckLink。 | M19软件实施；M17设备；M18验收；升级UM04 |
| F5.3 | BRAW/R3D SDK、CinemaDNG序列、RAW源控制与真原片；按需求/许可/设备逐格式立项。 | M04、M18；§4条件表 |
| F6.1/F6.3 | 真实外部软件FCPXML/OTIO往返，子帧/版本映射；AAF依赖+真实接收端均未验，Stem替代不冒充AAF。 | M24实施；M18/W07验收；§4条件表 |
| F6.2 | LUT/字体/插件等归档依赖、relink对话框可靠匹配、真实reconform和变速/嵌套重套。 | M12重连；M20归档；M18/W10验收 |
| F6.4/F6.5/F6.6 | 序列恢复按钮/外部回导/AOV；入库UI/多目的备份；清理状态/目录迁移/配额/跨工程引用和Windows重验。 | M10/M21序列；M12入库/代理；M20归档；W08/W10验收 |
| F7.1/F7.2/F7.3 | 发丝/半透明指标、COMP增量retrack/修正、人脸接线或禁用；RIFE真实模型/主观质量/解释缓存；RIP徽标和复杂图基准。 | M08烘焙；M23跟踪入口；M18质量；升级UM02 |
| F8.1/F8.2/F8.3 | 版本IO/自动备份/比较UI；评论→marker命令；Audition工程字段/IO/播放/导出。 | M02、M15 |
| F9.1/F9.3 | 文稿选择/复杂容器映射与实际命令；多版本编译→真实job/队列/恢复/PCM manifest。 | M10/M11实施；M21发布与QC |
| F10.1–F10.6 | 协作、VST/AU、广播影院标准、大3D、OFX、远程渲染均为条件扩展；本轮未验收。 | §4条件表；Windows配套验证 |

## 4. 下一步升级路线（收尾后按门禁实施）

| 阶段/ID | 升级内容 | 启动条件与交付边界 |
|---|---|---|
| 第一阶段 | M00–M12及M19–M21已声明能力的正确性/生产接线；M24精度差量；M16可并行只读 | 三条黄金工程可复现，关键P1关闭，再扩默认能力；不等所有可选功能 |
| 第二阶段 · UM01 | 多机位自适应质量和资源预算 | M13/M16及W08通过后；按4/9路、Program负载、代理与deadline选择每角度分辨率/帧率；UI明示降档，真实窗口记录p95/掉帧/资源 |
| 第二阶段 · UM02 | 时间线光流/高质量时域效果 | M04/M08正确、真实模型/许可/资源明确；先缓存/导出接一个provider，切镜/遮挡/发丝与降级有质量样片，效果注册表精确声明，随后评估实时 |
| 第二阶段 · UM03 | 仅对实测热点试验C/C++下沉 | M16仍证明Python调度/GIL/边界成本主导时才选一个有界操作；稳定C ABI+Python回退+取消/租约对拍，跨语言后仍有收益才扩。使用预编译发布库，不要求用户每次启动编译 |
| 第二阶段 · UM04 | HDR分区创作与真实专业监看 | 实际HDR镜头/设备需求成立后，将现有Toe/Shoulder与新分区工具分别声明；定义stops/边界/rolloff，保肤色与高光样片、负值/超白及成片对拍 |
| 条件阶段 | Metal生产预览、Topos GPU解码、RAW SDK、AAF、OFX/VST/AU、多人/远程渲染、高级3D/影院标准 | 各自记录需求、收益瓶颈/兼容矩阵、SDK与分发条件、小PoC和退出条件；没有依据保持延期。Metal不因存在离屏资产就自动替换GL；远程渲染不因测试农场存在就宣称可用 |

## 5. 验收与开工顺序

1. M00/M01先做，M02/M03/M04/M05与M19–M21按具体文件所有权推进；发现数据/源帧/PCM错误优先于新增UI。
2. M06–M12、M19–M24每包加一条穿过真实生产消费者的回归；现有helper测试继续保留。M13–M15/M22/M23拆成小交互包，M16/M17按实际profile决定变更；条件子项不阻塞无关小修。
3. 首轮窗口连续播放10min，之后按变更风险做30min交互/2h或10h长压；先冻结资源/漂移阈值、采样窗口和关闭回落条件。不得用加速离屏帧数折算窗口运行时长。
4. Mac与Windows共享工程/媒体hash、期望源帧和PCM/图像容差，各自记录环境与实际后端；不将两机fps平均或当成前后提升。
5. 每包产物放 `docs/validation/upgrade_2026-10-24/<任务ID>/<head>/<run_id>/`（未来新增），含命令/退出码、输入hash、原始结果、未验收项、回滚。纯逻辑/真实窗口/媒体/GPU/设备/接收软件分别计数。

本次已执行命令见run manifest。未来开工可复用 `.venv/bin/python -m pip check` 和已列12个focused文件；GPU/窗口任务必须独立进程且使用真实平台插件。现有基准入口为 `scripts/bench_frame_budget.py`、`scripts/bench_monitor_readback.py`；先核当前 `--help` 和真实素材路径，再将完整命令记入本包证据，不把未实现harness当现成验收。

### 5.1 已有 focused test 起点

下列文件均在当前HEAD存在，开工重新核对；本次只执行run manifest列出的12个文件，未执行的起点不冒充已通过。修复时补穿过真实IO/compiler/runner/renderer的新回归，不能只复跑原helper断言。

| 责任包 | 现有测试入口 |
|---|---|
| M02 | [test_f81_project_snapshots.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_f81_project_snapshots.py>) |
| M03 | [test_audio_sync_binding.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_audio_sync_binding.py>) |
| M04 | [test_f54_vfr_alignment.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/media/test_f54_vfr_alignment.py>)、[test_f54_generic_sequence.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/media/test_f54_generic_sequence.py>) |
| M05/M07 | [test_color_group.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_color_group.py>)、[test_color_versions.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_color_versions.py>) |
| M06 | [test_qualifier_keyframes.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/color/test_qualifier_keyframes.py>) |
| M08 | [test_bake_service.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/composite/test_bake_service.py>) |
| M10 | [test_f93_version_compiler.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/deliver/test_f93_version_compiler.py>)、[test_f64_frame_manifest.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/shared/test_f64_frame_manifest.py>) |
| M11 | [test_f91_filler_removal.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/ai_editing/test_f91_filler_removal.py>) |
| M12 | [test_f65_verified_ingest.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_f65_verified_ingest.py>)、[test_proxy_cache_product_loop.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_proxy_cache_product_loop.py>) |
| M13/M14 | [test_trim_professional.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_trim_professional.py>)、[test_audio_graph_parity.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_audio_graph_parity.py>)、[test_sample_time_audio.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_sample_time_audio.py>) |
| M15 | [test_f82_review_comments.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_f82_review_comments.py>)、[test_f83_audition.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_f83_audition.py>) |
| M16/M18 | [test_realtime_playback_telemetry.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_realtime_playback_telemetry.py>)、[test_video_export_preflight.py](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/deliver/test_video_export_preflight.py>) |

每包回滚：保留最小提交或配置开关；schema变更保留旧工程备份与迁移测试，停用入口不丢未知/新增payload。候选后端失败回到已经验收的路径，设备/插件状态和失败原因留在manifest。

## 6. v2 遗漏复核与执行修正

本节记录计划缺项及其补法，**不将14条安排/验收缺项等同于14个新产品bug**。v1的142项测试和9组复现保留；v2仅新增4组只读探针，未重跑原142项，未执行GUI/GPU或Windows真机。原始证据见 [v2复核记录](validation/upgrade_2026-10-24/plan_review_v2.json)、[4组探针结果](validation/upgrade_2026-10-24/plan_omission_probe_results.json) 与 [可移植源码索引](validation/upgrade_2026-10-24/portable_source_index.json)。

| 编号 | 漏项/修正 | 明确责任 | 依据与证据边界 |
|---|---|---|---|
| R01 | 软件色彩/显示/Scope只有索引，没有实施责任 | M19；W04/W07 | `src/shared/color/color_transform_provider.py:664`；provider失败与shape恒等行为有局部复现；三页显示与Scope为静态接线/验收遗漏 |
| R02 | 完整组载荷、Composite消费和组成员编辑漏实施 | M05.1–M05.3；W07 | `src/features/edit/core/color_chain.py:88`；静态：高级链段/完整Layer不能由顺序修复自动补齐 |
| R03 | 归档依赖闭包、LUT重写和跨OS脱机恢复漏实施 | M20；W10 | `src/features/edit/core/asset_usage_index.py:102`；局部复现：绑定WAV被usage index标unused；不冒充完整归档实测 |
| R04 | 编码输出与sidecar缺统一发布事务和保旧成片验收 | M21.1；W07/W10 | `src/shared/export/video_encoder.py:897`；静态：直接打开最终路径，close异常删除最终路径；序列需集合一致性 |
| R05 | 黑/冻/坏帧与逐版本响度未测状态、QC报告缺生产者接线 | M21.2；W07 | `src/features/edit/ai_editing/delivery_quality_gate.py:40`；静态：字段默认0不能证明已经测量；run_summary有原台账欠项 |
| R06 | 交换子帧、实际采样时基与活动grade版本缺adapter实施包 | M24；M18/W07验收 | `src/features/edit/adapters/fcpxml_adapter.py:639`；计划遗漏/静态字段检索，48k项目输出48k本身不视为bug |
| R07 | 镜头匹配参考锁、实际帧预览、批准与命令前台漏实施 | M22；W07按启用模式验收 | `src/features/color/core/shot_match.py:283`；静态API与台账；现有算法接线与算法升级分开 |
| R08 | COMP增量重跟/修正、人脸/ML Roto前台入口漏实施 | M23；W07按启用模式验收 | `src/features/composite/nodes/face_track_node.py:146`；静态/模型条件项；缺detector不宣称用户功能完成 |
| R09 | Bake worker、产物验证与图rewrite发布协调漏实施 | M08.2；W07/W10 | `src/features/composite/bake_service.py:188`；静态：缓存已有原子存储可复用，不误称所有缓存均不原子 |
| R10 | 评论重复源歧义与Audition fit时长契约不完整 | M15.2/M15.3；W07 | `src/features/edit/core/review_comments.py:197`；评论重复源局部复现；Audition为计划契约缺项 |
| R11 | 取消结果、sample split归属与RGBA外部往返验收遗漏 | M12.1/M14.1/M09/M10；W07 | `src/features/edit/core/verified_ingest.py:69`；取消report.all_passed=True有helper复现；采样/RGBA为已声明范围的验收补项 |
| R12 | CUDA同线程current-context与外部YUV颜色数值门遗漏 | W02/W04；M19共享集成 | `src/shared/media/windows_hw/cuda_resident.py:527`；静态：同线程不保证current-context；RGB/HDR真机偏差本轮未测 |
| R13 | 跨平台证据定位与文件系统命名/目录签名边界遗漏 | M20/W10；两份计划§0 | `src/features/edit/core/project_archive.py:56`；静态规范缺口：平台分隔符/大小写/目标保留名，跨OS真机待验 |
| R14 | 硬依赖过大与W09/UW01推广口径倒置 | M07/M09–M12/M18；W09/升级UW01 | `docs/upgrade_plan_W_windows_2026-10-24.md:1`；计划安排修正，不计产品新bug |

实施与验收责任分开：M18/W07是集成发布门禁，不能把未实现功能仅挂到门禁就算已安排。子任务ID分别登记核验/实施/验收/延期，父包只有在声明范围全部关闭后才关闭；硬件/模型/格式条件只约束对应子项。
跨机交接最少包含：提交与文件白名单、接口/schema差异、fixture/媒体hash、原始证据相对路径、实际运行环境/库hash、未支持范围及回滚。新UI/错误提示走已有i18n与结构化diagnostic入口，控件状态仍防信号递归。

## 修订记录

- 2026-10-24 v1：快速复核当前A/B/F实施后状态；12项共享问题、Mac三项、完成/回归门禁两项，并承接Windows七项和旧台账剩余范围；建立19个Mac工作包及下一阶段门禁。产品代码未修改。

- 2026-10-24 v2：补M19–M24六个实施包（主包19→25），细化完整调色/Bake/评论/Audition/采样/RGBA/worker与性能子项；拆分硬与条件依赖，补可移植索引和v2证据。原19个编号保留，全部工作仍未开始；本轮未改产品代码。

- 2026-10-25 v54（开工续五十·W 线回传接收+M16 真实窗口门复开执行）：①机器 B 以自有计划 W 执行 W00–W10 全 11 包并回传，用户合并 `36048dfd4`（M+W 两线首次同枝）——Mac 集成侧完整性验证通过（聚焦门禁 GATE PASS+W 改动测试 Mac 全量重跑 ~1000+ 绿）+两处集成收口 `5e54134c5`（W01 loader PE 判定全平台接缝、W07 luts 契约文件真正入库——此前被 .gitignore 拦截从未入库）；M18 状态行更新（W 证据映射入 deferred_gates"W 线证据并入"表：W01 DLL ABI 组件级证据/W07 分发验收就绪/W08 长压首档），S01–S12/黄金链 Windows 分发验收前提齐备待 B 机二轮。②用户 2026-10-25 提供真实测试素材（tests/test_videos）——M16 安静机真实窗口门复开执行完毕 `5e54134c5`：M16.1b 真实窗口三相位矩阵（bench_m16_real_window.py，5 素材×5 组交错、独立子进程、生产路由含 Topos/零拷贝/planar，25/25 零错误零掉帧 PASS，热点候选 4K H265 decode p95 在案）+M16.3b 冷启动实机基线（p50 7.6s）——M16 剩余仅 H265 单 item 优化候选与长压扩档；W 计划侧同步登记 v3（Mac 集成者接收记录）。

- 2026-10-25 v53（开工续四十九·B 机通道投递）：接入包经既有 gitee 跨机通道投递（`8ec94678f`：testfarm 运行指令+INSTRUCTIONS，fix-ui 推送 origin）；Mac 侧四轮轮询（~22 分钟）暂无 B 机回传——B 侧执行为外部动作（operator 拉取→运行→回传），Mac 侧等待报告。
- 2026-10-25 v52（开工续四十八·用户接入机器 B）：用户经 AskUserQuestion 确认接入 Windows 机器 B——交付 M18.4b 复验接入包（`4f80a27bd`：Windows PE ABI 审计+一键复验脚本+README）。Windows 复验/Windows 侧 ABI 两门自此为"接入包就绪、待 B 机运行"态；M16 安静机门仍待资源。
- 2026-10-25 v51（开工续四十七）：M18.4 打包库 Mac 侧 ABI 审计（`c70d4c315`——dist 产物 1523 Mach-O 全 x86_64 单架构，manifest 在案）；M16.3a 响应基线离屏（`c70d4c315`——60/600 片段双档+复杂度守卫，无二次方路径）。两包离屏可实施子项至此全部交付；开放项收敛为：M16 安静机真实窗口矩阵+冷启动实机基线、M18 Windows 机器 B 复验/Windows 侧 ABI/实机往返/AAF SDK/RIFE 样片/长压——**均为本会话不存在的物理资源/外部授权，接入即开启**（deferred_gates 逐项在案）。
- 2026-10-25 v50（开工续四十六·硬件门离屏审计）：逐项审计 M16/M17/M18 剩余子项的可离屏部分——M16.2 分域指标+worker 生命周期（`f109c328e`）；M17.1b GPU 缩放回读候选（`938d843f2`→M17 Mac 侧整包收口）；M18.3 黄金链 3 端到端（`6c953abbc`）。M16/M18 状态行更新后开放项均为纯硬件/跨机/交互 session 条件（安静机/DeckLink/机器 B/外部软件——deferred_gates 在案）；M17 → [x]（23/25 包）。
- 2026-10-25 v49（开工续四十五·六包核对收口）：M03/M04/M11/M20/M21/M23/M24 七包状态行核对收口 → [x]——各包剩余项均为 M18 实机/Windows/设备域或许可条件（deferred_gates.md 在案，非 Mac 侧可实施项）。**二十包 Mac 侧整包收口**：M02–M15（除 M16/M17 硬件门）+M19–M24（除 M18 门禁本体）。开放项仅剩 M16（真实窗口矩阵/安静机）、M17（DeckLink 设备）、M18（Windows 复验/安装包/实机往返/AAF/DeckLink/RIFE/长压延期门）——按计划 §4 规则与 deferred_gates.md 如实延期，条件不满足不改称通过。
- 2026-10-25 v48（开工续四十四·M06 整包收口）：M06.5 qualifier GPU/CPU 逐像素对拍（`e512c48a7`——真实 keyer.glsl 编译对拍，四组参数全绿）→ **M06 Mac 侧整包收口**（余 Metal 实机=M18）。十四包收口累计 M02/M03/M05/M06/M07/M08/M09/M10/M12/M13/M14/M15/M19/M22。
- 2026-10-25 v47（开工续四十三·M13 整包收口）：M13.9 布线矩阵对话框入口（`e59566c2f`——矩阵视图+稀疏 dst 静默丢弃修复）→ **M13 Mac 侧整包收口**（余真实窗口联测=M18）。十三包收口累计 M02/M03/M05/M07/M08/M09/M10/M12/M13/M14/M15/M19/M22。
- 2026-10-25 v46（开工续四十二）：M13.8 多机位降档+DPI 恢复页面入口（`a62a3dedb`）。
- 2026-10-25 v45（开工续四十一）：M04.3 通用序列分类接媒体池单资产（`b43b63e20`——连号静帧成组+缺口诊断+单 image_sequence 素材）。
- 2026-10-25 v44（开工续四十·M03 整包收口）：M03.5 跨日 TC 偏移声明（`f6647430a`——24h 环归一+显式假设声明）→ **M03 Mac 侧整包收口**（余波形 worker 化实机交互=M18）。十二包收口累计 M02/M03/M05/M07/M08/M09/M10/M12/M14/M15/M19/M22。
- 2026-10-25 v43（开工续三十九）：M06.4 遮罩形状组合 CPU 模型层（`b6237b7bc`）。
- 2026-10-25 v42（开工续三十八）：M11.4 跨容器映射（`d17c121c6`——容器内命中显式跳过，防顶层 ripple 波及容器）。
- 2026-10-25 v41（开工续三十七）：M13.7 cue 动画渲染消费接线（`d8fc61782`）。
- 2026-10-25 v40（开工续三十六）：M06.3 三帧全链像素对拍（`141a640e9`）。
- 2026-10-25 v39（开工续三十五）：M04.2 解释组合+消费者矩阵+指纹失效修复（`7bff2032f`——含 render_cache 漏收覆盖的真 bug）。
- 2026-10-25 v38（开工续三十四·M02 整包收口）：M02.2 真实素材全保真验收联测（`2b12be2c9`——日志/嵌套/字幕/采样级字段 .topos 往返+定点稳定）→ **M02 Mac 侧整包收口**。十一包收口累计 M02/M05/M07/M08/M09/M10/M12/M14/M15/M19/M22。
- 2026-10-25 v37（开工续三十三）：M03.4 同步诊断持久化（`0a98efcee`——诊断快照第七表 JSON/包双向往返+拒绝审计留存）。
- 2026-10-25 v36（开工续三十二）：M03.3 双源混音显式声明（`0854b8304`——mix_mode 字段+统一消费契约+dual 未支持显式拒绝）。
- 2026-10-25 v35（开工续三十一）：M11.3 口头语删除差异预览（`c3b108852`——勾选→真实区间逐 op 呈现→审批同一构建链）。
- 2026-10-25 v34（开工续三十·M10 整包收口）：M10.3 编译字段消费者+revision 硬门（`8661daeb3`）→ **M10 Mac 侧整包收口**。十包收口累计 M05/M07/M08/M09/M10/M12/M14/M15/M19/M22。
- 2026-10-25 v33（开工续二十九·M12 整包收口）：M12.3b auto search 可靠匹配接线（`ea8f06376`）→ **M12 Mac 侧整包收口**。九包收口累计 M05/M07/M08/M09/M12/M14/M15/M19/M22。
- 2026-10-25 v32（开工续二十八）：M03.2 DF/NDF 丢帧换算（`3396978a3`——SMPTE 真跳号显示+DF 逆映射+非法码拒绝）。
- 2026-10-25 v31（开工续二十七·M09 整包收口）：M09.3 四通道 alpha 声明+Lightbox（`2d08797a5`——声明 sidecar/转域族/undeclared 拒绝/比较对话框）→ **M09 Mac 侧整包收口**（余真实窗口人工验收=M18）。八包收口累计 M05/M07/M08/M09/M14/M15/M19/M22。
- 2026-10-25 v30（开工续二十六·M07 整包收口）：M07.4 分享后自动锁（`78442098f`——lock_shared_stacks 首次接生产，经命令栈可撤销）；M07.5 版本比较视图（`442313358`——compare_versions+面板多选比较）→ **M07 Mac 侧整包收口**（余 Windows 复验=M18）。七包收口累计 M05/M07/M08/M14/M15/M19/M22。
- 2026-10-25 v29（开工续二十五）：M22/M15 状态行核对收口——M22 菜单实体挂载（b19c946b3 已交付，状态行滞后于 v17）→ [x]（余 12 镜头真实 UI 人工验收=M18 域）；M15 全部子任务（M15.1a/1b/2/3+菜单）已收口 → [x]。六包整包收口累计 M05/M08/M14/M15/M19/M22。
- 2026-10-25 v28（开工续二十四·M05 整包收口）：M05.2f composite topos_color shader 对齐权威 PRIMARY_GRADE（`3ed10fb5e`——废弃独立近似实现，五路调色一致）→ **M05 Mac 侧整包收口**（余 Windows/实机=M18）。
- 2026-10-25 v27（开工续二十三）：M05.2e Composite 链段消费（`7abd96498`——chain_for_stack 生产侧 + 评估器顺序 ping-pong pass + standalone GL 顺序测试；GPU 测试暴露 composite topos_color shader 参数域预存在近似，登记为剩余切片）。
- 2026-10-25 v26（开工续二十二·M19 整包收口）：三页读数×向量对拍联测（`9ae28e56d`）→ **M19 Mac 侧整包收口**（余 Windows W04/设备门=M17/M18）；M24 AAF 依赖门登记（商业 SDK 授权条件，不自动安装不宣称支持）。
- 2026-10-25 v25（开工续二十一）：M20.2c LUT 路径重写进归档工程载荷（`91abd182f`——归档包自洽，恢复侧不依赖原机 LUT 路径）。M20 Mac 侧剩余仅跨 OS 实测=M18。
- 2026-10-25 v24（开工续二十）：M21.2c 运行侧闭环（`2e883f6de`——批队列排空点逐版本 LUFS 实测→信号→主窗口缓存→QC 面板；含 JobStatus 常量名修复与面板未测诚实性补齐）。M21 Mac 侧剩余仅 Windows 文件锁复验=M18。
- 2026-10-25 v23（开工续十九·两包整包收口）：M08.1c 预览/重开守卫+徽标（`586f53f5f`——RenderPlan.bake_staleness 双模式审计+信号去重+工具栏徽标）→ **M08 Mac 侧整包收口**（余 Windows 文件锁=M18）；M14.3c 采样级 UI 微调（`325f97466`——属性面板子帧偏移可编辑+命令 Undo）→ **M14 Mac 侧整包收口**（余实机/Windows=M18）。
- 2026-10-25 v22（开工续十八）：M05.2d 3D LUT 文件 CPU 加载消费（`dbdd595a4`——apply_lut_file_to_color 接缝接入导出主栈+交付 fail-fast；修复 _apply_3dlut_cpu 上边界 off-by-one 真 bug）。
- 2026-10-25 v21（开工续十七）：M19.3b Scope 测量域 UI 角标（`cedb1114a`——域/采样/单位三要素+tooltip 完整声明）；M24.3 活动调色版本字段随包（`30c12c245`——note 标记双向往返+对拍声明）。
- 2026-10-25 v20（开工续十六）：M21.1b+ 编码器 sidecar 产出接线（`7ec8f0608`——编码前产出/失败零发布/成组发布/取消清理）；M21.2c 多版本 QC 面板（`b494ac9ed`——只读呈现+注册表+菜单实体挂载，未测不冒充 0）；M20.2b 字体/插件/模型身份闭包（`7e662f428`——dependencies.json 随归档+缺失诊断不静默换）。
- 2026-10-25 v19（开工续十五）：M19.2b 工程色彩上下文注入时机生命周期（`cdd929f6b`——加载注入/关闭清空/切换全量替换契约 + 控制器三处实体接线）；M24.2 子帧 offset 有理时基映射细则（`38b8b3dec`——有理秒精确分解/合成互逆、48k 参考率整数执行+残差记账、FCPXML 导入/导出双向接线、往返定点稳定）。
- 2026-10-25 v18（开工续十四）：M14.3b 工程采样率字段接 EditProject（`979c6825d`——dataclass 字段+JSON 双向+工程包转换器 settings 往返，旧工程缺键=0→访问器解析 48000；顺带清理 HEAD 预存的重复 color_groups 字段定义）。M14 包内剩余仅采样级 UI 微调。
- 2026-10-25 v17（开工续十三）：菜单实体挂载完成（`b19c946b3`——七个 MenuActionDescriptor 进 MenuToolbarMixin 实际回调链，headless 安全）；M05.3 组成员策略（`7a991be18`）、M14.2b crossfade/Stem 语义（`ca91ff383`）、M15.1b 自动备份调度器（`a0e6d2d07`）收口。
- 2026-10-25 v16（终态登记）：**S01–S12 与 R01–R11/R14 全部探针 issue_present=False（26/26）**；M12.2b 目录迁移预检（`ea0bae358`）收口。累计 85 提交（`35a9ff904..HEAD`）。每包剩余项均为三类：①页面菜单栏**实体挂载**（注册表 `upgrade_panel_actions` 已单点化，控制器构建期接入）；②真机/Windows/设备/实机门——按 deferred_gates.md 登记的条件延期（条件不满足不改成通过）；③按需求/许可条件显式立项项（计划 §4 规则）。核心已验收版本可独立交付。
- 2026-10-25 v15（开工续十二）：R02/R07/R08 尾片闭合——M05.2c HSL+soft-clip CPU（`a52925800`+矩阵 `fb24f00d8`）、M22.3 GradeDiff（`23c1e28dd`）、M23.2 可用性探测（`f2e2b7425`）；M21.2b 逐版本 QC（`091c9915c`）、M19.4 PQ/HLG 向量（`5ef012a29`）；菜单挂接统一注册表（`34c575d3a`，六面板单点接线）——R 项 14 缺口 14/14 全部关闭。
- 2026-10-24 v14（开工续十一）：M23.1b Tracker 修正面板（`45c05f94c`）、M22.2 控制器测量回调（`8c40e50fc`）收口——R07/R08 的 UI 接线件齐（面板+回调），剩页面菜单挂接。
- 2026-10-24 v13（开工续十）：后续片再收七项——M14.3a 采样率字段迁移（`2ba6bacd2`）、M19.3 Scope 测量域声明（`80cdf056d`）、M21.1b 多文件发布状态机（`f37b8987e`）、M08.2b Bake 进度 UI（`a18351cbe`）、M07.2 版本工具面板（`ca5359b0a`）、M11.2 口头语面板（`0a6211b70`）、M15.1a 快照比较面板（`3cdb3d251`）；M18 延期门登记（`2e9707cca`）。
- 2026-10-24 v12（开工续九）：M15.1a 快照比较 UI（`3cdb3d251`）、M11.2 口头语面板（`0a6211b70`）收口；M18 延期门登记（`2e9707cca`，Mac 集成件盘点+七门延期条件在案）。
- 2026-10-24 v11（开工续八）：M13.1–M13.6 模型层全部落地（4 模块 38 测试）；后续片再收——M05.2b RGB 曲线 CPU 导出（`4f8c4dae3`+`9ed53797a`）、M10.2 序列恢复接 manifest（`3b0dd77be`）、M14.2a 嵌套 retime 音频窗（`da15525cc`）、M19.2a 三页色彩上下文（`1872dc6b8`）、M20.2a 归档 LUT 闭包（`3489ffba9`）、M21.1c 争输出拒绝（`c0f7ac7d7`）；各包状态行同步。
- 2026-10-24 v10（开工续七）：M17.1a 主机侧 UYVY 打包候选（`1de6da2b7`）——监看软件子项先行，真机裁决登记。
- 2026-10-24 v9（开工续六）：M12.3 重连可靠匹配接线（`cac4046be`）、M19.1b R01 探针转绿（`98904acec`）——**v2 遗漏探针 R01/R03/R10/R11 全部 issue_present=False**；v2 复核 14 缺口：R01/R03/R04/R05/R06/R10/R11 + R02/R07/R08 首片已关（11/14），余 R02/R07/R08 后续片在包内登记。
- 2026-10-24 v8（开工续五）：M15.3 Audition fit 契约（`f006503ff`）、M12.2 代理清理 UI 面板（`5e2a5d663`）、M16.1 decode-only 离屏复测首批基线（`b22d2c15f`，真实窗口裁决待安静机）收口；包内状态行同步。
- 2026-10-24 v7（开工续四）：R 项再收四项——R02 首片（M05.2a 逐后端能力矩阵+qualifier 行拆分 `9a84fec20`）、R05（M21.2a QC 真实测量 producer+深测量接线 `4b8ce1ae2`）、R07 首片（M22.1 镜头匹配前台面板 `fa16fd269`）、R08 首片（M23.1 Tracker 修正/增量重跟领域服务 `8f77af1c7`）；R10（M15.2 评论歧义）关闭 `35d0e0966`；M08.2 Bake 协调器收口 `3af303376`；R 项 14 缺口已关 11（余 R02 后续片/R07 后续片/R08 后续片在包内登记）。
- 2026-10-24 v6（开工续三）：R 项攻坚——R01（M19.1 typed 转换结果+预检）、R03（M20.1a 归档闭包含绑定 WAV）、R04（M21.1a 编码发布事务保旧成片）、R06（M24.1 采样率随包往返）四项 v2 复核缺口的 R01/R03/R04/R06 关闭（R02 组载荷/R05 QC/R07 镜头匹配/R08 跟踪待续）；深化片——M06.2 CPU 像素级 qualifier（numpy keyer 接导出）、M08.1b 烘焙陈旧审计接导出守卫、M09.2 抓帧端 float→EXR（S12 全链贯通）、M14.1 split 采样分区契约冻结；各包状态行同步更新。
- 2026-10-24 v5（开工续二）：**十二项共享正确性缺口 S01–S12 全部关闭**（九组逻辑探针 issue_present 全 False）——S01 快照/IO 保真（M02.1）、S02 文稿删除真实命令（M11.1，RangeRippleDeleteCommand+端到端 compiler）、S03 多版本真实 job+离线硬门（M10.1）、S04 PTS 显示帧序（M04.1）、S05/S06 外录同源+TC 契约（M03.1）、S08 链顺序四路统一（M05.1）、S09 插件清单（M07.1）、S10 烘焙签名（M08.1a）、S11 qualifier 导出求值（M06.1）、S12 EXR 参考加载（M09.1）、S07 入库逐项（M12.1a）；各包剩余子项在正文状态行登记；M00/M01 已整包收口。
- 2026-10-24 v4（开工续）：M01 收口（三隔离环境 pip check=0+真模型小样 PASS+四项冲突处置[qwen-tts↔qwen-asr 上游互斥为新发现]+宿主限制登记[torch 2.22 上限/nemo Sortformer/pyannote]+constraints 漂移登记移交 M18）；M02.1/M03.1/M04.1 收口（S01/S05/S06/S04 四探针转 False，S01 另含构造器保真与快照持久化，S05 含成片/Stem 同源消费，S04 含显示帧序索引）；各包剩余子项在正文状态行登记。
- 2026-10-24 v3（开工）：M00 收口——F 计划 53 项差量表冻结（生产接线30/仅API层7，勾选≠用户功能验收）、142 focused 基线零漂移复现、`scripts/m00_focused_gate.py` 分域子进程门禁（零失败真实阻断）+ `focused-logic-gate.yml` CI、失败/缺口登记（S01–S12/M-I01–03/机器B基线引用/F8项开）。原提交与证据未动。
