"""Assemble the reviewable plan from audit evidence, without product changes."""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[4]
OUT=Path(__file__).resolve().parent
PLAN=ROOT/'docs/composite_full_function_audit_optimization_plan_2026-10-14.md'

EXTRA=r'''
## 补充优化与验证计划

以下条目区分测试基础设施问题、测得的性能不足、明确未提供的能力、尚需实机验证的任务。未验证项不计入“已复现业务 bug”。工时为人日粗估，必须在定位后重估；共用基础设施的条目不能把工时简单相加。

| 编号 | 类别 / 优先级 | 问题与证据 | 解决方法 | 验收标准 | 粗估 / 依赖 |
|---|---|---|---|---|---|
| QA-01 | 测试基础设施 / P2 | 隔离运行 test_ai_remove_wiring 18例有4例因 OpenEXR disabled 失败；开启 OPENCV_IO_ENABLE_OPENEXR=1 后18例全过。src/main.py:51、shared/io/file_io.py:11已设置启动开关，因此这些不是4个生产效果bug。scripts/run_composite_tests.sh缺同等环境契约。 | 在测试/bootstrap最早位置、导入cv2前设置并验证OpenEXR能力；测试runner与产品启动共用受控环境前提；缺codec时提供可定位错误，不能依靠测试顺序先导入FileIO才成功。保留本次失败与补环境复测证据。 | 单文件/整套/隔离runner使用相同启动条件；AI cache真正写读EXR成功；无EXR构建给明确能力诊断。 | 0.5日，独立 |
| QA-02 | 测试崩溃 / P1 | test_fix_nodes1_aud2.py:194声称fake GL且无需context，实际FileOutputNode._query_draw_fbo_binding在file_output_node.py:264调用原生glGetIntegerv，进程SIGSEGV(-11)。该文件未完整执行，不能算全套通过，也不能据此声称真实用户窗口必崩。 | fake单测同时mock FBO查询/恢复等全部原生边界；另在真实GPU测试里检验真实FBO恢复；测试标记真实GL需求，runner继续进程隔离并保留returncode、stderr、未执行用例。不得用宽泛except宣称可捕获native crash。 | 原文件完整执行无native调用；真实context下readback和故障清理通过；整套无SIGSEGV；无可用GPU时明确skip而非假通过。 | 0.5–1日；可立即修测试，但不能代替R-01真实pool回归 |
| QA-03 | 基准可复现性 / P2 | tools/bench_composite_playback.py:24硬编码topos-color01并插到sys.path首位，本次在topos-color02直接执行可能测错checkout。bench_all_composite_nodes_real_topos.py固定历史输出目录/已退役素材名；GPU coverage script视频只找Windows桌面目录。 | 全部以__file__定位本checkout；媒体用tests/real_media.py和冻结清单；提供media/output路径参数；JSON写HEAD、dirty状态、源路径/指纹、设备/驱动、质量/代理/输出尺寸。只复用内容签名一致的提取帧。 | 从含空格目录、另一个checkout、CI均测当前HEAD；无素材明确blocked；不覆盖旧日期结果；输入与报告媒体身份相同。 | 1–2日，独立 |
| QA-04 | 性能/正确性门禁不足 / P1 | GPU coverage的静态image_input跨frame命中缓存，不能代表视频每帧效果成本；LUT override使用file_path而实际参数为lut_path，Defocus覆盖size而实际为max_radius，可非空直通却被判通过；all-nodes driver仅因“状态可解释”返回0，failed也可退出0。现有大量测试对mock/结构/同样错误的串并行一致性断言，未覆盖本次多个输出错误。 | 校验参数键/有效port及目标节点确实被求值；动态视频/参数扰动与cache-hit分别测；非identity fixture与直通参考做像素差和目标数学断言；模型/后端资源blocked与功能failed分开；真实app benchmark原counted_submit把ScopeDemand当dict导致AttributeError并abort，计数器必须只处理dict request；本次已在审计副本修复并复测、逐场景重置配置；原脚本不调用mw.close而直接SystemExit，live CompositeRenderThread在interpreter终结时SIGSEGV，不能当正常用户关窗缺陷；副本已走生产closeEvent（临时工程的question明确返回Discard）并检查线程终止；失败分支有异常退出清理。产品源码未改。CLI提供inventory模式与strict gate模式，strict出现failed应非0退出。将本次各定向复现转成行为回归，用真实pool和真实encode→decode补关键路径。 | 错lut路径、无效参数、Paint直通、全黑成果、缺帧成果故意注入时门禁必失败；静态cache数字不混作视频效果FPS；报告区分failed/blocked/skipped，所有CI错误可定位。 | 2–3日；随U/N/R修复分批纳入 |
| C-01 | CLI能力缺口 / P2 | scripts/topos_render.py:204仅接受exr/png/jpg/jpeg，没有视频编码入口；交互导出的视频能力不能通过无头CLI使用。 | 复用CompositeRenderOutput的视频路径与同一preflight/输出schema，增加codec/pix_fmt/descriptor/fps/帧范围/取消参数和结构化结果；不另造求值器。先完成R-03～10的色彩与完整性合同，再开放。 | 相同graph/frame范围GUI与CLI像素/帧数/色彩标签一致；错误退出非0，取消明确；序列原有用法兼容。 | 2–3日；依赖R-03～10 |
| P-01 | 已测性能不足 / P1优化 | 真实2K/4K TOPOS、DNxHR及ProRes4444的简单视频源→Output离线预览明显慢于H.264/H.265/422来源；详细数值见性能表，部分4K远超25fps的40ms预算，4K TOPOS422 Proxy中位733.8ms、复杂图726.9ms；名字带Proxy不代表合成节点当前自动使用低分辨率。frame trace仅能先定位到eval段，不能凭此认定全部是GPU算力不足。 | 将eval分成seek/decode、planar→RGBA、IDT、CPU拷贝、upload、节点GPU、readback；记录每帧后端及降级原因。按测量优先复用native/GPU planar输入、减少uint16→float32→拷贝转换；preview使用可重建代理与GPU IDT，export保持高位深合同。按codec/profile建立能力矩阵，避免所有高位深来源统一走昂贵CPU路径。 | 同批媒体同尺寸同质量独占复测；低成本图preview p95目标≤40ms(25fps)、专业目标≤16.7ms(60fps)，未达到时提供有效代理档并证明无错帧；最终导出不通过降位深换速度。 | 分解1–2日，优化3–5日；依赖QA-03/04及R-06 |
| P-02 | 已测跳帧不足 / P1优化 | 4K随机100→240→10→400来源帧的同步离线请求存在数百ms至秒级尾延迟；4个seek只是探查，不能当成熟p95 SLA或认定GUI一定同样卡住。 | 对GOP索引、解码重排/关键帧距离、shared provider cache、source→comp帧映射做分段计时；scrub合并latest请求并丢弃过期结果；关键帧附近小范围预取，代理缓存按文件签名/帧/色彩/质量区分。将UI seek-to-present端到端测量与同步渲染请求单独统计。 | 冷/热、正/反向、首/尾、长GOP至少100次seek；无旧帧回显；preview代理seek p95≤100ms，专业≤50ms；最终full-res精确请求不能以缓存错帧满足指标。 | 1–2日定位，2–4日优化；依赖N-06/07确保时间语义 |
| P-03 | 已测导出吞吐不足 / P2优化 | 多种4K简单图linear_f32离线导出每帧明显慢于srgb_u8预览，常见约0.5秒及更高；这是渲染+读回、未含实际视频编码，不能把数字称为导出成片FPS。 | 对full-frame/tiled strategy做相同精度对照，分解tile上游重复求值、texture pool、tile stitch、readback/PBO、CPU数组复制；在VRAM预算允许时比较full-frame f32+异步PBO与逐tile回读；源与公共上游按内容/源帧共享，逐tile工作只覆盖有效ROI。修R-11资源生命周期后再调并行度，保证owner-thread清理。 | 相同float输出与单帧参考误差达标、tile边界无缝；吞吐目标先提升≥2倍并写实测，p95/内存均不回退；1/2/4并行实际编码基准输出严格等价、无缺帧。 | 1–2日定位，3–5日优化；依赖R-11/15、N-06/07 |
| P-04 | 性能诊断不足 / P2 | 本次真实frame trace多项gpu_time_ms、cpu_upload_bytes、cache_hit、pool_peak_bytes为null。eval_ms/readback_ms虽可用，但不足以区分GPU同步、CPU解码和内存开销。 | 在已有PerfMonitor/frame trace补可用GPU timer query、每阶段CPU计时、上传/下载字节、真实cache命中、pool高水位、decode backend/fallback；能力不可用保留明确原因。采样/延迟回收查询，不能为监控每帧glFinish阻塞。 | 图、帧、request、source/quality有统一关联；同一问题可从报告定位到阶段；render-thread自驱播放的1次submit可以产生多帧，不能将emissions/submit当成功率；开启采样开销≤2%，不支持的GPU计时有原因。 | 1–2日；前置于P-01/02/03深入优化 |
| V-01 | 实机验证任务 / P1 | 核心编辑与保存完整旅程覆盖不足，定向slot测试没有证明整个真实窗口操作与最终磁盘文件全部正确。 | 建立新建CompClip→添加视频/图层→关键帧/表达式→撤销/重做→切Edit→保存.topos→关闭重开→Deliver旅程。复制工程到临时目录，比较source身份/层/图/视图/未知字段。含旧schema、非法表达式、失败载入和取消。 | 对U-01/02/05/11/12和N-01缺陷修复后至少5条真实旅程通过；最终落盘重开一致，原用户工程不改。 | 1–2日；依赖第一批持久化修复 |
| V-02 | 实机交互任务 / P2 | 关键帧、图层、曲线两种编辑方式以及焦点/快捷键的一致性仍需逐手势验收。 | 测鼠标选择/框选/多选、向量分量、Auto-key、Bezier切线拖动、expression错误、trim/slide、I/O播放区间、锁/solo、Ctrl+Z不同防抖时序、编辑↔合成transport联动。每一步比对live model/undo/预览/序列化。 | 形成可复跑手势清单与屏幕录像/状态快照；U-03/04/07/08/14/15/16不再复现；焦点在属性/搜索/曲线时快捷键行为清楚。 | 2–3日，依赖参数/连线事务修复 |
| V-03 | 大素材操作验证 / P2 | dependency scan/collect/relink存在同步service入口，本次未复制GB媒体，未证实具体UI阻塞时长。 | 在测试工程内对35素材做缺失/重复路径、重链、收集、取消、磁盘不足；测event-loop heartbeat，不把文件拷贝放UI线程；采用现有service的任务/进度接口、原子提交与取消清理。 | 重链后节点/项目/source provider一致且undo有效；收集保存可在另路径重开；UI heartbeat p95<5ms、无>20ms长阻塞或提供实测优化排期；不覆盖原素材。 | 1–2日验证，修复按测量另估 |
| V-04 | 大图恢复验证 / P2 | page loader逐节点重建视觉，尚无50/200/1000节点真实页面载入指标。 | 修U-06时复用既有batch build_from_graph；测解析、模型构建、连线/装饰、缩略图、layout/repaint分别耗时；缓存只保留可重建数据；加载有进度且可取消。 | 50/200/1000节点含组/便签/层分别p50/p95与UI heartbeat记录；模型/视觉数量准确，恢复无重复、无隐藏新图；目标指标以基准后设定。 | 1日验证，1–3日优化；依赖U-02/05/06/13 |
| V-05 | 显示与平台验收 / P2 | 本机没有验证真实HDR显示器输出、全部DPI/Dock尺寸、Windows/Linuxcompute和所有3D材质的视觉参考。 | 按原有view/renderer入口测alpha棋盘、HDR>1、通道/AOV、ROI/代理、A/B差值、像素检查、示波器、3D相机/透明材质/输入override；跨平台做GL4.1/compute/Metal支持矩阵，包含中英文和125%/150%/200%DPI。 | 每项记录设备与参考截图/像素差；可用能力有一致结果，不可用能力明确诊断；不把MacGL4.1性能推广到compute平台。 | 2–4日；依赖R-13～20与N-09/10 |
| V-06 | 长时稳定性验证 / P1 | 本次媒体基准是短样本、独立进程，未证明30分钟播放、反复导出/取消、窗口关闭和GPU/native frame lease的长期稳定性。 | 在独立测试实例跑30分钟连续+循环播放，100次seek/切页、20次串并行序列导出与取消、关闭窗口；记录RSS/VRAM/pool/decoder session/线程数与分位延迟。区分正常bounded cache和不可回落增长。 | 无崩溃/死锁/过期帧；取消和关闭在owner-thread清理；预热后资源在预算内稳定、停止后可回落；任何native crash单独保存栈。 | 1–2日；依赖R-08～12生命周期修复 |

| V-07 | 已观察冷启动失败 / P1验证 | 连续真实实例中，前例关闭保存current_page=Composite；下例冷启动直接进入Composite，30秒后renderer仍未初始化，日志等待gl_initialized。这是初始化失败观察，不是已经播放4K后崩溃，随后SIGSEGV来自异常退出未清理。 | 每例基准重置配置，分别以Color/Composite为起始页验证；沿页面on_enter、共享GL创建、gl_initialized绑定检查是否互相等待；必要时由公共GPU初始化service在任意GPU页面首次进入时触发，避免靠先打开Color。异常退出也走生产closeEvent。 | 从全新/已保存Composite偏好启动，3秒内完成共享GL与renderer初始化或给明确错误；无需手动先切Color；实例关闭后线程归零；不把测试生命周期错误算产品渲染崩溃。 | 0.5–1日定位，1–2日修复；依赖V-01/05/06 |

## 实施批次与依赖

| 批次 | 要解决的核心风险 | 范围 | 工作顺序与完成门槛 |
|---|---|---|---|
| B0：保存与状态安全 | 工程信息丢失、跨合成改写、失败操作丢动画 | U-01/02/03/04/05/09/11/12/13，N-01，QA-01/02 | 先统一graph payload与session事务，再修pending与失败回滚；真实临时.topos保存重开旅程V-01通过。可先单独修测试runner，不能当业务已修。 |
| B1：图编辑与动画语义 | 错误撤销、幽灵线、局部表达式与父子变换错误 | U-06/07/08/10/14/15/16，N-02/03/04/05/12 | 连线替换/粘贴同一个命令事务；参数求值唯一入口；loop与base轨道政策带迁移；model/visual/undo/serialization一致。 |
| B2：可靠渲染与交付 | Paint无效、颜色/位深/透明错误、取消/丢帧假成功 | R-01～13、R-15、R-22/23/24，RG-02/05/07，QA-04 | 先修真实pool上传与色彩合同，再修Result完整性/清理；encode→decode色条、透明图、缺帧/取消注入全部通过；禁止补帧默默成功。 |
| B3：时间与数据通道 | 共用源不同时间分支错帧、ID误选、AOV语义丢失 | N-06/07/08/09/10/11，RG-08，R-23 | 先统一(node,frame,port)请求与缓存，再补光流；Cryptomatte按位ID、标准输出与schema迁移；逐帧与外部EXR读取验证。 |
| B4：3D与能力补齐 | 3D预览导出不一致、材质控件无效、未接线功能 | R-14/16～21，NG-01～06，RG-01/03/04/06，C-01，N-13 | 先修可证实的设置/材质路径；R-20先复现混合shader模型；能力不足按真实可用资源分批上线；RG-03的volume与NG-05合并设计，不重复计算工时。 |
| B5：性能与稳定性 | 高位深预览、长GOP跳帧、4K导出、关键帧快照 | N-14、P-01～05、QA-03/04、V-02～07 | 先补阶段归因，再优化；错误输出修复后重建性能基线；快照及代理达到帧预算，30分钟稳定性和平台矩阵通过。 |

同一工程师的总体工期不能把每行相加；共享事务、色彩和求值基础设施可合并实现，也有迁移与平台复验成本。第一批实施应先完成B0的最小可验证修改，再逐批review和测试，不一次重写整个系统。

## 修复完成标准

每项都以“复现不再成立 + 用户预期结果正确”验收，不能只改索引状态为通过。状态建议使用：待修复 → 实现中 → 定向回归通过 → 集成/真实素材通过 → 完成。静态证据项先复现再改；模型/依赖阻塞独立列出，不能用skip计为功能通过。

工程/动画数据变更走既有command/service，保留undo、revision与未知字段；schema调整写迁移；GPU对象在创建线程清理；HDR与alpha由明确合同处理，不能通过clamp或降低位深掩盖错误。修复后重跑关联测试即可，最终再做一次隔离全套与同素材基准。
'''

def main() -> None:
    texts={name:(OUT/'source_notes'/f'topos_composite_{name}_audit_20261014.md').read_text() for name in ('ui','nodes','render')}
    texts['render']=re.sub(r'\bN0([1-8])\b',r'RG-0\1',texts['render'])
    index=[]
    for name,text in texts.items():
        seen=set()
        for line in text.splitlines():
            cells=[c.strip() for c in line.strip().strip('|').split('|')]
            if not line.startswith('|') or not re.fullmatch(r'(?:U-\d{2}|N-\d{2}|NG-\d{2}|R-\d{2}|RG-\d{2})',cells[0]): continue
            ident=cells[0]
            if ident in seen: continue
            seen.add(ident)
            if name=='render' and ident.startswith('RG'):
                index.append((ident,'能力补齐/验证',cells[1],'静态能力盘点','按依赖设计后估算'))
            else:
                index.append((ident,cells[1],cells[2],cells[3],cells[-1]))
    index.append(("R-24","P1","4K关窗后合成渲染线程仍未停止","A-APP+B，10/16再次观察","定位1日、修复2–4日；依赖R-11/V-06"))
    env=json.loads((OUT/'environment.json').read_text())
    suite=json.loads((OUT/'suite_results.json').read_text())
    totals={k:sum(r.get(k,0) for r in suite['rows']) for k in ['tests','passed','failures','errors','skipped']}
    header=f'''# 合成页面全面功能检查与优化计划表（2026-10-14）

状态：审计与计划已完成；最后续验2026-10-16。产品缺陷尚未修复。本次新增的是计划和证据，没有修改产品源码或测试素材。

检查基线：`{env['git_head']}`。项目根目录：`{ROOT}`。范围覆盖合成页面的节点编辑、图层、关键帧/表达式、查看器、源素材/时间处理、2D效果、3D材质/场景、GPU缓存/资源、保存与Edit/Deliver集成、图片/序列/视频导出。

**主要结论：54项核心问题与性能项（U 16项、N 14项、R 24项），另外列出能力不足、基准质量和待实机验收任务。** 这些不是54个全都已用真实窗口复现的bug：下面逐项注明运行证据、注入失败证据、静态调用链与性能测量。高优先项是工程信息丢失、跨合成撤销、表达式失败丢动画、错误颜色/alpha输出和不完整成果误报成功。正常路径大量测试通过，不能覆盖这些边界错误。

## 检查方法、覆盖与边界

- 读取`.clinerules`和AGENTS工作流；三路Explore/Review分别审查UI/持久化、节点/图模型、渲染/导出/3D，最后合并重复主题与核对证据。旧计划只用于背景，不作为当前bug成立依据。
- 当前合成模块有300个Python文件，注册表99个键；包含别名、兼容键和可选延迟注册，并非99个独立可用效果。参数/端口清单见`docs/audit/2026-10-14-composite/evidence/node_inventory.json`。
- 322个合成测试文件，加34个相关app/UI/project/edit文件，共356个文件逐文件独立进程运行。环境为Qt offscreen、standalone GL、BLAS单线程，避免一个native崩溃吞掉整个套件。
- 用户35个2K/4K视频全部做ffprobe，覆盖H.264、10bit HEVC、DNxHR 10/12bit、ProRes各档与TOPOS各档。通过生产CompositeFrameRenderer测23次请求/来源（3次预热/冷启动、12次顺序、4次seek、4次linear_f32导出），另测4个Grade→Gaussian Blur(24px)→Merge复杂图，合计39个场景。
- 定向复现使用真实模型/page/SaveRouter处理入口；GPU上传缺陷使用真实pool和8×8独立GL线程；编码色彩使用真实numpy/PyAV转换。失败/取消场景会以fake保存器/编码器注入，避免覆盖用户工程或写无用大文件。
- 没有逐按钮完成所有原生窗口操作、真实HDR屏幕观感、外部付费AI提供商、所有FBX/glTF/EXR/AOV组合与跨平台验证。代码/模型复现不能等同鼠标交互全部通过；这些明确进入V系列及各项验收标准。真实应用播放的短测结果与关闭状态单独记录，不能凭signal频率声称所有交互完成验收。

## 自动测试结果（2026-10-14原始全套）

| 项目 | 结果 | 解释 |
|---|---|---|
| 隔离执行文件 | {suite['completed_files']}/{suite['total_files']} | 354文件退出0；1文件4个EXR环境失败；1文件native SIGSEGV |
| 可获取XML统计的用例 | {totals['tests']} | native崩溃文件没有完整XML，未运行完的用例没有计入“通过” |
| 原始结果 | {totals['passed']}通过 / {totals['failures']}失败 / {totals['skipped']}跳过 | 原始日志/XML保留，没有用复测覆盖原记录 |
| EXR启动条件复测 | 18/18通过 | 打开OPENCV_IO_ENABLE_OPENEXR后原4失败消失；按启动环境问题QA-01归类 |
| 校正启动条件后的可计数结果 | {totals['passed']+4}通过 / {totals['skipped']}跳过 | 仍有1个崩溃文件未完整验证，不能写“全套全过” |
| native崩溃 | test_fix_nodes1_aud2.py，returncode=-11 | fake GL测试漏mock原生FBO查询，见QA-02；不能推断真实应用同样必崩 |
| 本次新发现 | 独立复现与审查发现 | 不用现有测试通过率代替功能正确性结论 |

跳过项包括不可用Metal后端、4.3 compute与测试明确的环境限制；原始XML有逐项原因。统计把pytest标记的skip/xfail统一保留为“跳过”，不算通过。日志与环境见`docs/audit/2026-10-14-composite/evidence/`；运行耗时合计约{sum(r['wall_s'] for r in suite['rows'])/60:.1f}分钟。

## 测试环境

macOS15.3.1，Intel i9-9900K，32GiB RAM，AMD Radeon RX6900XT，OpenGL4.1；Python3.12、numpy1.26.4、ModernGL5.12、PyAV17.0.1、Qt6.10.0（低于pyproject当前声明的6.10.2下限，升级构建仍需复验）。本机GL没有4.3 compute能力，片元/可用native路径结果不能直接推广到其他平台。完整版本和设备参数见environment.json。

'''
    perf=['## 真实素材性能测量\n\n所有数字单位ms。顺序预览12帧、seek4次、导出4帧，样本很短，nearest-rank p95会等于最大值；用于发现瓶颈，不能替代100次seek/30分钟播放验收。预览是离线同步求值+GPU读回，不是GUI实际显示FPS；export是linear_f32渲染+读回，没有包含视频编码/写盘。采样RSS是CPU进程内存，不是VRAM。\n\n| 素材 / 场景 | 顺序预览中位 / p95 | Seek中位 / 最大 | linear_f32导出中位 | 采样RSS峰值MiB | 结果 |\n|---|---:|---:|---:|---:|---|']
    for p in sorted((OUT/'media_bench').glob('*.json')):
        d=json.loads(p.read_text()); summary=d.get('summary',{})
        preview=summary.get('preview_sequential',{});seek=summary.get('preview_seek',{});export=summary.get('export_sequential',{})
        valid=all(r.get('ok') and r.get('finite') for r in d['rows']) and len(d['rows'])==23
        def n(v):return f'{v:.1f}' if isinstance(v,(int,float)) else '—'
        perf.append(f"| {Path(d['file']).name} / {d['profile']} | {n(preview.get('median_ms'))} / {n(preview.get('p95_ms'))} | {n(seek.get('median_ms'))} / {n(seek.get('max_ms'))} | {n(export.get('median_ms'))} | {n(d.get('peak_sampled_rss_mib'))} | {'23请求有效' if valid else '失败/不完整，查原始JSON'} |")
    snap=OUT/'snapshot_perf.json'
    if snap.exists():
        d=json.loads(snap.read_text()); perf.append('\nCPU快照独占复测（合成Transform图、2次预热+10次记录，不含解码/GPU）：\n\n| 节点 | 动画轨道 | 每轨关键帧 | 表达式 | snapshot中位ms | 最大ms |\n|---:|---:|---:|---|---:|---:|')
        for r in d['results']:perf.append(f"| {r['nodes']} | {r['tracks']} | {r['keys_per_track']} | {r['expression']} | {r['median_ms']} | {r['max_ms']} |")
        perf.append('\nN-14的最终依据以上述独占复测为准；子报告的并发初测只保留作过程记录。大量关键帧每帧深拷贝/重复解析发生在UI snapshot路径，应先使用不可变轨道和revision缓存，再保证子帧/父变换语义完整，目标snapshot p95≤5ms，不能仅优化GPU。')
    real=OUT/'real_app_runs.json'
    if real.exists():
        runs=json.loads(real.read_text());perf.append('\n真实应用驱动结果（独立临时配置/测试工程；1920×1080合成、24fps；请求15秒）：\n\n| 素材 | 驱动退出状态 | 关键输出 |\n|---|---|---|')
        for r in runs:
            log=(OUT/r['log']).read_text()
            selected=[l.strip() for l in log.splitlines() if any(k in l for k in ['viewer emissions','viewer updates','playhead stalls','viewer frozen','viewer max gap','mode=','audit teardown: composite','AssertionError','RuntimeError'])]
            perf.append(f"| {r['media']} | {r['returncode']} | {'；'.join(selected[-6:]).replace('|','/')} |")
        perf.append('\n该驱动测composite_frame_ready信号边界与播放头，不是物理显示器呈现。driver失败或超时只能作为未完成验收记录；不能借无头渲染的吞吐推断GUI可实时。详见*.real_app.log及V系列；已经完成播放但正常关闭失败的具体合同错误见R-24。')
    gui_followup=ROOT/'docs/audit/2026-10-16-composite-followup/evidence/gui_results.json'
    if gui_followup.exists():
        perf.append('\n2026-10-16真实窗口续验：每例独立临时config、起始页Color，1920×1080合成、24fps、4K来源、15秒。\n\n| 来源 | frame_ready信号率 | 超过1秒间隙 | 正常关闭验证 |\n|---|---:|---|---|')
        for row in json.loads(gui_followup.read_text())['rows']:
            log=(ROOT/row['log']).read_text()
            rate=re.search(r'viewer emissions:.*rate=([0-9.]+)/s',log)
            stall=re.search(r'viewer gaps > 1s: ([0-9]+)',log)
            close=re.search(r'audit teardown: composite_renderer_running=(True|False) window_visible=(True|False)',log)
            state=('renderer='+close.group(1)+', visible='+close.group(2)) if close else '未到达关闭验证'
            perf.append(f"| {row['media']} | {rate.group(1) if rate else '未取得'} /s | {stall.group(1) if stall else '未取得'} | exit={row['returncode']}；{state} |")
        perf.append('\nTOPOS在本次窗口路径达到约23.9次信号/秒且关闭成功；不能把离线4K完整读回约733.8ms/帧解释成所有TOPOS界面播放均慢。窗口合成输出1080p、离线输出4K，且生产异步呈现与standalone读回不同，不能直接计算加速比。P-01优先检查离线路径的decode/backend、CPU转换与读回，P-05单列HEVC窗口中后段退速。信号率仍不等同物理显示器帧率或逐帧像素正确性。')
    master=['## 问题索引与计划状态\n\n所有产品修复与能力补齐项当前均“待修复/待验收”；审计副本本身的计数器/临时配置/关闭修补已经执行，QA系列仍要求同步修复正式基准入口。P1为优先阻止数据丢失/错误成果或主要功能无效，P2为后续可靠性/性能与能力补齐，P3为较低优先体验/文档。工时不是承诺日期。RG-03与NG-05的volume主题合并排期，RG-03还覆盖pointcloud，避免重复建设。\n\n| 编号 | 优先级 | 问题 | 证据 | 工时 / 依赖 |\n|---|---|---|---|---|']
    for row in index:master.append('| '+' | '.join(row)+' |')
    sections=[]
    for name,title in [('ui','UI、图编辑、撤销与工程持久化详细计划'),('nodes','节点、动画、表达式与时间求值详细计划'),('render','GPU、色彩、导出与3D详细计划')]:
        text=texts[name]
        text=re.sub(r'\n\n(?=\| (?:R-23|RG-08) \|)', '\n', text)
        for p in (OUT/'focused_repro').iterdir(): text=text.replace('/tmp/'+p.name,str(p))
        text='\n'.join(text.splitlines()[1:])
        text=re.sub(r'(?m)^(#{1,5}) ',lambda m:'#'+m.group(1)+' ',text)
        sections.append('\n## '+title+'\n'+text)
    evidence='''\n## 证据与重跑入口\n\n- evidence/suite_results.json、pytest_logs/*.log/*.xml：逐文件原始退出与用例结果。\n- evidence/exr_env_rerun.log/.xml：EXR前提复测。\n- evidence/media_probe.json、environment.json、node_inventory.json：素材/环境/节点底数。\n- evidence/media_bench/*.json/*.log：每帧耗时、实际decoded_frame、像素shape/dtype/hash、frame trace、diagnostics。\n- evidence/focused_repro/：各项独立复现脚本和输出；evidence/source_notes/：三路原始子报告。\n- evidence/run_isolated_suite.py、run_media_benchmark.py、run_real_app_benchmark.py：审计重跑入口；从当前项目根目录用.venv/bin/python执行，性能基准与套件顺序运行以减少争用。\n\n基准使用临时GL/测试工程；不写原视频，不应把输出路径中的fake文件误当交付视频。本次pytest改写了已跟踪的p29基准JSON，审计输出另存evidence后已恢复原文件，最终产品与原测试数据无变更。\n'''
    followup=(ROOT/"docs/audit/2026-10-16-composite-followup/continuation.md").read_text()
    PLAN.write_text(header+'\n'.join(perf)+'\n\n'+'\n'.join(master)+'\n'+EXTRA+'\n'+followup+'\n'+'\n'.join(sections)+evidence,encoding='utf8')
    print(PLAN,'index core/capability rows',len(index),'bytes',PLAN.stat().st_size)

if __name__=='__main__':main()
