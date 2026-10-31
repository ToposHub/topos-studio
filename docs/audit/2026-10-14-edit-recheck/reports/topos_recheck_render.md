# Render failures 独立只读核对

基线55351a5f→HEAD833dcd382；本报告涉及的四个render/math生产文件在该区间均无diff。故不能写成上一批edit优化引入的回归，它们是当前存在的既存保真问题及旧测试假设。

主任务原始GPU/Metal测试证据：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/pytest-existing-failures.log`。本agent不另启动可见GUI、不再调用GPU；独立小探针仅CPU/真实exporter函数和数学对照，保存在`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/probes/topos_recheck_render_repro.py`及`.out`。GPU诊断函数从真实GradingMath移去export shader缺少的保护块，用于因果定位，不声称本probe是另一轮实际GPU渲染。

## 结论

| 失败 | 分类 | 数值/原因 |
|---|---|---|
| cached LUT exactness | **P1真实CPU交付保真问题** | 同样真实wheel独立复现5427/25344,max4；真实float fallback逐位正确。暗部保护已跨RGB耦合，独立1D通道LUT假设失效 |
| GPU grade u8 | **P1真实primary-grade分叉**，极端fixture放大差异 | 实际GPUmax179；CPU移除暗部保护的诊断精确复现max179、通道[0,0,179]。wheel解析完全一致，非RGB/BGR互换 |
| GPU grade u16 | 同一P1根因，**不新增一个bug计数** | 实际45936；同CPU无保护诊断精确45936。identity已通过，明确grade阶段问题 |
| GPU multi-layer grade | 同一P1根因，**不新增一个bug计数** | 实际max64；诊断composite max63，量化/插值1code残差符合实际GPU精度范围，底层grade差异经top opacity0.65剩35% |
| Metal scratch cache key | **P2旧内部断言/验收缺口，不是已证明像素或资源bug** | 原test数值<=2已通过；当前radius8先materialize chain，分离scratch键是stack_effect_chain_horizontal，旧assert要求stack_effect_horizontal |
| GradingMath dark gate（本轮探针新增发现） | **P1真实参考实现缺陷** | 同合法wheel/HDR像素仅因同批加black像素，最终u8 [255,255,0]→[36,255,0]；数学结果依赖其他像素，违反逐像素grade语义 |

## RENDER-01 · P1 · 独立channel LUT错误假设跨通道保护仍可分解

位置：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_export.py:9337`声明Lift/Gamma/Gain等逐通道独立；`:9345`coupled_controls只识别highlights/vibrance/hue_shift/saturation。
- 同文件`:9375–9380`分别用另两个RGB通道置0的样本跑GradingMath，取单通道结果建256×1×3表；真实fast入口`:9288–9301`通过cv2.LUT回交付。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/color/core/the_decompiler.py:190–220`在gamma后根据luma、min/max与dark_mask进行通道平衡，已是跨RGB条件性处理。

复现：测试的真实lift wheel `(250.67947667772825,0.7041904101271046,0)`（UI范围内），ramp+seed42随机u8 BGR，25344元素。真实 `_apply_color_grade` 输出5427元素不等，max4；gain wheel单独case0差异。给同RGB帧增加alpha列使shape4，强制真实float fallback，则RGB逐位等于GradingMath参照。

数学反证：lift同参数，两个RGB输入 `(0,0,0)` 与`(.1,.1,0)`保持B输入均0，但B输出分别`.1445045024`和`.1494550705`。任何只读B输入的1DLUT都不能同时得到两个正确输出。这不是改test tolerance到4就可消除的实现问题；也是预览float与CPU导出fast可能不一致的原因。

修复方向：先以范围证明/安全门拒绝任何可触发暗部跨通道保护的grade，走真实float路径；若需保持快路，按同一公式将可分离前缀与真实RGB保护stage分开，不把保护抽进单channel LUT。确认saturation分解路径同样执行中间保护，不能只修highlights/vibrance门。

验收：现有wheel exactness原测试全部过（不放宽）；合法wheel颜色网格含black/near-black，fast与float逐位一致或显式有量化误差预算；逐帧缓存启用/禁用、u8/u16/alpha各路径保真。调色内容损失不得静默接受。

## RENDER-02 · P1 · 导出GL/Metal primary-grade复制体落后Color权威文本

位置：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_gpu_export_renderer.py:190–200`gamma直接跳shadows，缺暗部保护。该 `_GRADE_BODY` 为u8/u16共享。
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/core/timeline_metal_export_renderer.py:120–129`同样缺保护，`:155–156`hue前clamp。
- 权威ColorGL：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/color/core/shader_nodes.py:72–89`含逐像素暗部保护；ColorMSL：`.../shader_nodes_msl.py:130–139`同契约。GradingMath对应`:190–220`。
- GL导出`:228` `rgb2hsv(clamp(color,0,1))`；CPUGradingMath`:270–275`明确hue用未clip HDR/负值颜色，导出复制体还有HDR hue语义漂移。GL`:232–233`saturation复用hue前gray，CPU`:281`重算post-hue gray，此边界需一起审查。
- `grade_params_to_uniforms` `.../timeline_gpu_export_renderer.py:784–787`用同GradingMath.params_to_rgb，探针逐项相等，没有证明轮盘解析/通道互换错误。

因果复现：取同test planar源、CPUconvert与量化。保留真实GradingMath其他stage，仅去除export shader缺少的dark保护。u8差max179，channels[0,0,179]，首BGR CPU[0,0,0]/旧公式[0,0,179]；u16差max45936，均精确对上真实GPUtest。stack加opacity0.65顶部图像后max63，实际GPU64仅差1code。

fixture边界：test `GRADE_TYPICAL` 使用gamma sat2、lift bright-1.5、gain bright-2；而ColorWheel `.../src/features/color/ui/color_wheel.py:54`声明sat[0,1]/bright[-1,1]。此fixture会使gamma uniform `(1e6,-1e6,-1e6)`、gain约`5.96e-8`，放大问题，名称“typical”不恰当。但这不能判为仅旧测试假设：**合法UI边界**lift `(60,1,0)`/gamma `(0,1,0)`/gainidentity、black RGB，CPU与权威保护输出u8RGB[19,0,0]，旧导出GL公式[118,0,0]，差99。此是纯CPU对相同公式的因果探针；原实际GPU失败已提供后端执行证据。

影响：根据fallback/grade/effects/backend路线选择，同一工程可能出现CPU/exportGL/exportMetal/Color页面不同颜色；多层与高位深均携带错误，不能归因encoder或OpenGL浮点微误差。当前Metal↔OpenGL parity test可能两边共享错误，不能用两者相等证明与Color/CPU权威一致。

修复方向：primary-grade共用权威IR/GLSL/MSL定义或生成片段，声明版本与跨后端数值契约；补保护、HDR hue与gray顺序，GPU资源生命周期按现有export renderer worker/queue，不在UI调用GPU。先修参考实现的逐像素gate（RENDER-03），再冻结golden，避免复制CPU参考自身缺陷。无授权改动生产代码，本轮只给方案。

验收：保留上述实际GPU failed probes；新增正常/边界wheel+black、正值/负值/>1 HDR、hue+saturation、u8/u16、多层；CPUreference/ColorGL/ColorMSL/exportGL/exportMetal比较（不能只GL↔Metal），容差只覆盖量化/插值与浮点误差。合法UI参数的代表性golden应全过；超范围遗留参数有明确加载/迁移/拒绝政策，而不是只删测试。

## RENDER-03 · P1 · CPU参考暗部soft compression受整幅图像内容影响

位置：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/color/core/the_decompiler.py:193–201`：外层 `if np.any(dark_mask)`；里层`deep_mask = res < -0.5`不与每像素dark_mask相交，`np.where`压缩所有deep channels。Color权威GL `shader_nodes.py:74–81`是**当前像素**luma<0才压缩。CPU后续balance_mask正确与dark_mask相交，但第一次soft compression已经污染非暗部像素。

精确复现（完全合法wheel；HDR内部float合法）：params lift=(60,1,0), gamma=(0,1,0), gain=(0,0,0), saturation=50；同HDR RGB像素 `(1,1.04,0)`：

- 单独grade → float `[1.1404030323,6.5403866768,-30.3595943451]`，交付量化RGB `[255,255,0]`。
- 同批加入另一个blackRGB，再取原同像素 → float `[.1400530338,5.5400362015,-5.0349464417]`，交付量化RGB `[36,255,0]`。

此探针直接调用生产GradingMath两次，无数学替身、GPU、缓存或resize。只改变无关像素，却原像素R差219code，证实像素批次/裁剪/瓦片/图像内容会改变grade。常规SDRtest图像可能不含此HDR门限组合，不能据此排除。

修复方向：deep gate与`dark_mask[...,None]`逐元素相交，逐pixel保护逻辑与权威GL/MSL一致；保证原luma与压缩后min/max的stage顺序，充分区分finite/HDR合法负值与NaN输入验证。不随意clamp HDR绕开。

验收：同像素独立、串批、拆batch、reshape、tile、顺序调换、添加不同无关像素所得grade相同；包含dark/nondark各pixel、deep负通道、>1、正常wheel、后接saturation/hue。CPU与两Color权威shader逐stage一致后再使用其作exportgolden。

## RENDER-TEST-01 · P2 · Metal scratch断言未跟随当前物化路径

位置：test `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/edit/test_timeline_metal_export_renderer.py:649`要求旧 `_buffers['stack_effect_horizontal']`。当前renderer `_should_materialize_effect_chain` `.../src/features/edit/core/timeline_metal_export_renderer.py:3524`，radius>1即走chain；`:4654–4676`在blend之前 `_apply_effect_chain`；`:3700–3702`为radius8 separable申请共享`stack_effect_chain_horizontal`；`:5178–5182`按名复用existing足够大小buffer。

纯policyprobe `kind2,half_x=half_y=8`返回materialize=True。主任务实际Metaltest在旧key断言前已经pixel<=2通过，因此现在不能把该failedcase列为blur画面错误、scratch未释放或不复用。

修复/验收方向：修test绑定真实行为，而不是要求生产重新建废弃缓存键。验证同shape两layer/两frame共享同scratch对象或allocation计数不增、数值仍<=2；尺寸扩大仅按需扩容，release全部清理。单key存在并不能证明原test名称所承诺的“between layers reuse”，这也是要补的验收缺口。

## 建议排期

先修RENDER-03 reference逐pixelgate并建立跨后端primarygolden；再修RENDER-02导出shader共享/同步与RENDER-01快路安全边界；最后更新Metal旧test并补allocation/release行为验收。5个render测试失败实际收敛为2个原失败根因（LUT与primary复制体）加1个本次明确复现的CPU参考缺陷，Metalcache不是另一个生产像素bug。全项均为只读证据，不修改生产或原测试。
