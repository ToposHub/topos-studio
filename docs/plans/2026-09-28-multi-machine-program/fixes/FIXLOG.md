# 修复战役日志（FIXLOG）

> 修复 REMEDIATION_PLAN.md 全部条目的执行日志。每波一节：指派/完成/验证/提交。
> 进度对账用 `check_fixes.py`（无参数 = 分桶统计）。

## Wave 1（批 A 第 1 波，2026-09-29）— commit 35b78b10

- **指派**：8 组并行 agent，24 条 Critical（22 AUD + BUG-B4-01，含 2 条交叉引用 dups BUG-B3-01/BUG-C4-02）。
- **结果**：24/24 FIXED，0 INVALID，0 BLOCKED。顺带翻转 plan-02 xfail 锁 2 条（BUG-A2-03、BUG-A2-02 → 正向断言）。计划表共勾选 28 行。
- **验证**：合并 sweep 899 passed / 2 skipped / 6 xfailed（16 个测试文件）；各 agent 自跑邻接测试合计 ~600 用例通过；`py_compile` 全部改动文件通过。
- **新增回归测试**：8 文件 86 用例（tests/{app,color,composite,scripting}/test_fix_*_aud.py 与 test_fix_aud02xx.py）。
- **过程事件**：
  - 一个 agent 违规创建 `fix-ui` 分支 → 已 fast-forward 合回 dev 并删除；后续所有 agent 指令显式禁止 git 分支操作。
  - `test_auto_color.py::test_prores_cast_corrected` 与 `test_tracking_worker_lifecycle.py` 为 pristine-HEAD 既有失败（stash/独立 worktree 对照复现），登记 round=2：AUD-R2-004 / AUD-R2-005。
- **报告**：reports/w1-g1.md … w1-g8.md。

## 待办登记（round=2，已入计划表）

- AUD-R2-001 layer.py:1198 LUT 约定（AUD-25900 同族）
- AUD-R2-002 scripting 示例旧曲线语义
- AUD-R2-003 node_graph_commands 重连启发式
- AUD-R2-004 auto_color ProRes 数值漂移（HEAD 既有失败）
- AUD-R2-005 tracking_worker OpenBLAS 崩溃（环境相关）

## Wave 2（批 A 第 2 波，composite 模块，2026-09-29）— commit 3a14b7d0

- **指派**：8 组并行 agent，30 条 Critical（全 composite 域）。
- **结果**：29/30 由 agent FIXED；AUD-6500 agent 判 BLOCKED（根因在 color_evaluator 参数映射 = AUD-5902 家族），由 orchestrator 补全 13-uniform 映射后 FIXED。顺带核销 bucket D 4 条：AUD-5303（active 标志）、AUD-5902（完整映射）、AUD-6604（cleanup+ctx 先例）、AUD-6709（finally 释放）。计划表共勾选 34 行。
- **验证**：tests/composite 全量 6054 passed / 13 skipped / 3 xfailed（-x 首败即停模式通过；test_advanced_plan_audit 单独排除——其中 f16 用例为 HEAD 既有失败，登记 AUD-R2-006）。BUG-B2-01 两个 strict 锁翻正。
- **新增回归测试**：8 文件 ~120 用例。
- **值得记录的工程判断**：AUD-80001 审查建议的全量 clear() 实测致后续瓦片变暗（纹理池回收损坏），采用逐出式方案并配端到端两瓦片渐变逐像素对照测试。
- **报告**：reports/w2-g1.md … w2-g8.md。

## Wave 3（批 A 第 3 波，edit+deliver 模块，2026-09-29）— commit 75fb56d5

- **指派**：8 组并行 agent，27 条 Critical。
- **结果**：27/27 FIXED，0 INVALID，0 BLOCKED。T-P1-21 metal sobel 两个 strict 容差锁在本机 XPASS（对齐已落地），按测试自带协议翻正。
- **验证**：tests/edit + tests/deliver 全量 8629 passed / 18 skipped；4 个失败 stash 对照全部 HEAD 预存 → 登记 AUD-R2-007（keychain 环境脆弱）、AUD-R2-008（插件元数据链路既有 bug）。
- **新增回归测试**：8 文件 ~110 用例（含真实 GL 2 例、300 轮并发压测、死锁回归）。
- **报告**：reports/w3-g1.md … w3-g8.md。

## Wave 4（批 A 第 4 波，teaching+shared，2026-09-29）— commit d65e0f6a

- **指派**：8 组并行 agent，29 条 Critical。
- **结果**：29/29 FIXED，0 INVALID，0 BLOCKED。
- **验证**：tests/shared + tests/teaching_animation 全量 1126 passed / 4 skipped；architecture guardrails 409 passed（基线重定：app 131 / shared 47 处战役前既有违规，offenders 全量 diff 证明 Wave1-4 零新增，仅行号漂移 → 登记 AUD-R2-009 存量债务）。
- **新增回归测试**：6 文件 ~110 用例（含真实 QThread 取消端到端、变异测试反证）。
- **报告**：reports/w4-g1.md … w4-g8.md。

## Wave 5（批 A 第 5 波，清零，2026-09-29）— commit 95669cff

- **指派**：5 组并行 agent，17 条（shared/video 7、色彩约定 3、plan-02 遗留 3、BENCH 4）。
- **结果**：17/17 FIXED。**批 A 清零：135/135 Critical×S/M 全部修复**。
- **验证**：380 passed / 2 skipped + tools py_compile；BENCH 门禁用真实 bench_out 实测（1 条 p99 超支被正确拦截，与证据逐字吻合）。
- **顺带发现**：AUD-R2-010（3 个 .glsl 的 PAL 系数同族）。
- **报告**：reports/w5-g1.md … w5-g5.md。

## Wave 5b（批 A 补漏波，2026-09-29）— commit d1ec892d

- **背景**：对账发现分波时漏派 8 条（render3d×4、effect_pass×2、edit_gpu_preview_renderer、ai_harness）。
- **结果**：8/8 FIXED。AUD-9108 实测比审查范围更大（cube −X/−Z、cylinder 侧面同反向，一并修）；AUD-88050 旧权重能量翻倍有解析证明；AUD-22020 审批闸门 fail-closed 重排。
- **验证**：65 passed（含真实 GL 链与红-绿反向验证）。
- **顺带登记**：AUD-16603（grade 索引 1000+ 纹理分配）此前被 16600 掩盖，现可达，归其所属批次。
- **报告**：reports/w5b-g1.md … w5b-g4.md。

## Wave 6（批 B+C+R2，2026-09-29）— commit d5af1971

- **指派**：8 组并行 agent，37 条（批 B 27 + 批 C 9 + R2×5，BENCH-0004 与 18800 计入）。
- **结果**：37/37 FIXED。**批 B、批 C 双清零**。三个 HEAD 预存测试失败全部转绿（R2-004 素材前提过时/R2-006 f16 边界 clip/R2-008 扫描序期望）。
- **战役自有回归修复**：plan-03 五插件 GLSL 编译失败（编译器注释误报 + Halation_Extended 重复函数）与插件注册表单例污染——根因修复 + 148 插件测试全绿。
- **验证**：两段 sweep 合计 4690 passed / 9 skipped（color+app+plugins+benchmark / shared+teaching+deliver+w6 新测试）。
- **新登记**：AUD-R2-011（color_decode.py PAL 系数第三处）。
- **报告**：reports/w6-g1.md … w6-g8.md。

## Wave D1（批 D 第 1 波，app/controllers，2026-09-29）— commit fa2b7c2c

- **指派**：8 组 82 条。**账号用量上限中断 3 组**（G1 bench 杂项、G4 composite_controller、G8 page/registry）——限额 2026-10-01 05:08 重置，已建自动续跑（automation-7a5a9ec0）。
- **结果**：5 组完整完成（52 FIXED + 3 closed-invalid），死组中 4 个小而完整的修复经 orchestrator 验证收编（AUD-16205/40014/3201/3202）→ 共勾选 58 行。
- **回退**：composite_controller.py（AUD-2401..2407 待重派）、page_controller.py + adapter.py（AUD-3100..3104/34001/34002 待重派）、G1 的 BENCH-0017/0007/0003/0013/0014/0008 + AUD-14100 待重派；G8 的 playhead 测试翻转随 src 回退一并还原（stash 验证后 tests/app 1067 全绿）。
- **GAP 部分推进**：GAP-002（+20 测试）/GAP-007（+41）/GAP-010（+30）未闭合，留专项覆盖批次。
- **附带登记**（G2 报告）：export_service.run_export_preflight 对两个 preflight 钩子签名不匹配（恒报 "preflight raised"），待后续批次。
- **验证**：新组测试 139 passed；tests/app 全量 1067 passed；tests/edit 全量 8522 passed（G6 跑）。
- **报告**：reports/wd1-g2/3/5/6/7.md（死组无报告）。

## 待重派清单（2026-10-01 自动续跑时优先）

1. AUD-2401..2407（composite_controller，原 G4）
2. AUD-3100..3104、AUD-34001/34002（page_controller/feature_registry 已收编 3201/3202/adapter，原 G8 剩余）
3. BENCH-0017/0007/0003/0013/0014/0008、AUD-14100（原 G1）
4. wave_d_plan.json 的 G9-G104（批 D 剩余 12 波）

## Wave D1-redo（限额恢复后重派，2026-09-30）— commit 5528436b

- **结果**：21/21（G4 7、G8 7、G1 7）。AUD-14100 核实已随 fa2b7c2c 落地（plan_compiler/schema 即其位置），本轮补验。批 D 第 1 波（原 82 条）至此全部处理完毕。
- **验证**：tests/app + tests/benchmark 合并 1320 passed。
- **新增工具**：bench_wrapper_binding_overhead / bench_default_config_playback（含 §4.2 遥测）/ bench_q01_rd_curve / bench_thread_pivot_gate / check_regression_anchors（首跑产出 bench_out/anchor_registry/）。
- **报告**：reports/wd1r-g1/g4/g8.md。

## Wave D2（批 D 第 2 波，app壳+ai_harness+ai_workflow，2026-09-30）— commit 3043b56b

- **指派**：8 组 115 条。
- **结果**：111 FIXED + 2 closed-dup + AUD-22704 由 orchestrator 以 pytest.ini testpaths 扩展收口（src 侧 3 个测试目录进默认收集，总量 30057）。GAP-027 部分推进（11/46 用例覆盖 closeEvent 域）。
- **验证**：tests/app + tests/ai + 三套 src 侧套件 2922 passed。唯一失败经四点提交二分确认为**战役起点即存在**的顺序依赖问题（登记 AUD-R2-012，附最小复现）。
- **报告**：reports/wd2-g9..g16.md。

## 2026-09-30 额度事件与复核记录

- **00:57 五小时额度上限**：Wave D3a 五组全部中断（无报告），残留改动已回退，工作区干净。自动续跑闹钟 automation-bb2a63bc（03:26 触发，含回退+重派+后续波次完整指令）。
- **R2-011 复核降级**：color_decode.py 为标准数字 601 limited-range 实现、内部自洽；原指控存疑，改为 range 契约审计项（详见计划表该行注记）。

## Wave D3a（批 D 第 3 波前半，color+composite 关键帧，2026-09-30）— commit b96d15c5

- **指派**：5 组 80 条（额度二度中断后经用户重置卡恢复重派）。
- **结果**：77 FIXED + 2 closed-dup；AUD-0910 转 G19（需 GLSL+MSL 两侧同改）。
- **验证**：tests/color + g24 新测试 2208 passed / 5 skipped。
- **报告**：reports/wd3-g17/18/20/22/24.md。

## Wave D3b（批 D 第 3 波后半，2026-09-30）— commit 3aa2e48b

- **结果**：33/33 FIXED（G19 17 含 AUD-0910 GLSL+MSL 双侧、G21 16）。
- **附带**：app 边界基线重定 131→129（AUD-2702 净减 2、零新增，offenders 全量 diff 证明）。
- **验证**：tests/color 2275 passed；guardrails 409 passed。
- **报告**：reports/wd3-g19/g21.md。

## Wave D4（批 D 第 4 波，2026-09-30）— commit a178e67e

- **结果**：78 FIXED + 1 closed-nonissue（G23 15、G25 16、G27 15+1、G28 16、G29 16）。
- **测试卫生**：TestAud11106 焦点清理（前序套件 QLineEdit 焦点触发字符快捷键抑制规则，顺序依赖消除）。
- **验证**：tests/composite 6327 passed；tests/color 2353 passed。
- **报告**：reports/wd4-g23/25/27/28/29.md。

## Wave D5（批 D 第 5 波，2026-09-30）— commit 6a6badde

- **结果**：63 FIXED + 1 closed-dup + 1 BLOCKED（AUD-6311 transport 接线属产品决策，保留待议）。
- **登记**：AUD-R2-013（环检测死 DFS，strict xfail 已锁）。
- **验证**：tests/composite 6513 passed。G30 曾违规直接改计划表（"wd5-g30" 标注），已统一修正为真实哈希。
- **报告**：reports/wd5-g26/30/31/33.md。

## Wave D6（含额度中断重派，2026-09-30）— commit 1427e435 + 40051038

- **过程**：G34 中断前完成（9 FIXED）；G32/35/36/38 死于第三次额度中断→回退→用户重置→重派全部完成。两 agent（G30/G36）违规直接勾选计划表，已在书签统一修正/补哈希。
- **结果**：70 FIXED + 10 BLOCKED（白名单缺口：file_output/frame_interpolate/geo3d/group/image_input/render_scheduler/scene3d/base_evaluator——编入 D7）。
- **验证**：tests/composite 6746 passed。
- **报告**：reports/wd6-g32/34/35/36/38.md。

## Wave D7（2026-09-30）— 见 commit

- **结果**：58/58 FIXED（G37 16、G39 17 含 AUD-7502、G40 16、BLK 补派 9）。G39 的两条一行接线由 orchestrator 补上（base_evaluator viewer_node_id、scene3d has_tangent）。附带发现并修复：trimesh 4.x transforms.local 消失导致世界矩阵全 identity、并行视频导出 DecodedFrame 错误导入（首帧即 ImportError）。
- **验证**：tests/composite 6922 passed。
- **报告**：reports/wd7-g37/39/40/blk.md。

## Wave D8a（额度中断四，2026-09-30）— commit 见上

- **结果**：45 FIXED（G43 16、G45 16、G47 13）；G41/G46 死于额度中断已回退待重派；AUD-13401/02/06 编入 D9。
- **验证**：tests/deliver + tests/edit/ai_editing 2242 passed。

## Wave D8b（G41/G46 重派，2026-09-30）

- **结果**：32/32 FIXED。BUG-B1-01/02 strict 锁翻正（AUD-9700/9701）；AUD-9801 顺带修复。
- **验证**：目标套件 215 passed。
- **报告**：reports/wd8-g41/g46.md。

## Wave D9（2026-09-30）— 81 FIXED

- 六组：G44 deliver 面板、G48/G49/G50 ai_editing、G55/G56 edit core。AUD-16603 落地（grade 稳定键分配）。
- BLOCKED→D10：AUD-13900/01/02（media_generation_service）、14101/06（plan_compiler/validator）、14402/10（session_models/skill_registry）、13401/02/06（edit_api/edit_project_service）。
- **验证**：tests/deliver + tests/edit/ai_editing 2218 passed；目标套件 78 passed。
- **报告**：reports/wd9-g44/48/49/50/55/56.md。

## Wave D10（含额度五中断，2026-09-30）

- **结果**：60 FIXED + 1 INVALID（AUD-18813 计划中不存在）；R2-013 环检测修复（xfail 翻正）；AUD-18806 顺带修复。interchange Rational-fps 破损由全量 sweep 抓到并即时修复。
- **预存失败基线**：test_timeline_color_grade_lut 1 + test_timeline_gpu_export_renderer 3（HEAD worktree 复现确认）。
- **死组**：G53（音频 DSP）/G57（edit core 3）回退待重派。
- **报告**：reports/wd10-g42/51/60/63/blk.md。

## Wave D11（2026-09-30）— 77 FIXED

- 六组：G53 音频 DSP 重派、G57 edit core3 重派、G58 工程归档、G61 序列化/字幕/词干、G64 edit UI a 段、BLK3。
- BLOCKED→D12：AUD-17108（commands.py）、16906（mixer_preset）、16911（nested_sequence）；接线遗留：19500（teaching 侧）、16900（transition_mask CPU 1.55 同步）。
- **验证**：tests/edit 9186 passed（52 项预存 GPU 位精确 deselect）。
- **报告**：reports/wd11-*.md。

## Wave D12（2026-09-30）— 125 FIXED

- 八组：G52/G54/G59/G62/G65/G66/G70/G71。BLOCKED→D13：AUD-17614、20203（服务侧半）、26910。
- **验证**：8 个新测试文件单跑全绿（305 用例）；单进程合跑触发原生模块加载崩溃（R2-012 同族顺序依赖，非回归，登记待专项）。
- **报告**：reports/wd12-*.md。

## Wave D13（含额度六中断重派，2026-10-01）— 57 FIXED + 7 GAP 批次

- 四组：G67 场景切割 UI、G69 GAP+misc（+797 语句覆盖）、G72 teaching core、BLK4 补派。
- **批 B 30/30 全清**（R2-003 被 AUD-10602 取代核销）。
- **验证**：新测试 273 passed；各 agent 邻接合计 ~4700+ passed。
- **报告**：reports/wd13-*.md。

## Wave D14（2026-10-01）— 56 FIXED

- 四组：G68 timeline UI 尾段、G73/G74/G75 teaching motion/render/presets。BLOCKED→D15：AUD-28005、29704/05/08（presets manifest/preview）、29801/03、30018（template_promote）、30100（html_native_overlay）。
- **验证**：新测试 160 passed；各组邻接合计 ~2000 passed。
- **报告**：reports/wd14-*.md。

## Wave D15（2026-10-01）— 81 FIXED + 1 closed-dup

- 六组：G76 HTML 渲染器（含 AUD-30100）、G78 teaching UI 尾段（+2 GAP）、G79 杂项、G80 ai_models 上半、G82 codec、G83 color/config/diagnostics 大组（24 条，含 ACEScg 错误矩阵根因修复）。
- **验证**：新测试 293 passed；各组邻接合计 ~3000 passed。
- **报告**：reports/wd15-*.md。

## Wave D16（2026-10-01，含额度七/八中断恢复）— 123 FIXED

- G84 preview_quality 降级系统（5/6，AUD-1100 阻塞 4 轮后于 w17 闭环）、G81r ai_models 下半 18（配置严格模式+备份、tiling 数学、HDR 域缩放、PatchMerging 维度 bug、图标字形）、G85r export 16（locale 命名键、16-bit PNG 编码器、topos_encoder 两阶段提交致命化+flock 判活）、G86 视频编码器 6（原代理在额度中断中幸存完成：rgba64le、防死锁转换队列、声道布局单一真相）、G87r GPU/i18n 18（fence 规范等待、PyObjC autorelease 泄漏实证修复、TextureLease 自旋锁原子化）、G88 io/layout/logging 9（原代理幸存）、G89r media 上半 16（解码世代代数、对称色度偏移、LRU 有界缓存）、G77r HTML 渲染器+场景 14（深度围栏、实例 id 命名空间、单调 revision）。
- 两次额度中断（05:41 / 07:27）：死组回滚+重派；G86/G88 原代理实为存活，通知延迟误判，全程零进度丢失。
- **验证**：各组新测试合计 ~330 passed；邻接合计 ~2500+ passed。
- **报告**：reports/wd16-*.md。

## Wave D17（2026-10-01）— 249 FIXED + 3 GAP 批次进行中 → 批 D 清零

- media 全域：G90 解码源 16、G91 VT/metadata 16、G92 代理/像素格式 16、G93 media 尾+D3D11 前半 16。
- 杂项：G94 D3D11 尾+node_canvas/paths/preferences 13、G100 颜色 widgets/vault 8（HDR 常色度 vectorscope 投影）。
- project：G95 持久化 16、G96 IO 6（归档锁、zip 炸弹双上限）。
- tracking：G97 6（半开区间、轮转分桶、模板纹理方差门控）。
- audio mixer：G98 14 + G99 16（拖动预览/提交分离、风暴门控持久化）。
- video 全域：G101 前段 16、G102 中段 16（渲染线程延迟释放排水）、G103r 播放器核心 17（**含 AUD-1100 播放降级激活闭环**：v3 start/stop/unload 接线）、G104 YUV GPU 尾 8。
- 接线批：W1 presets/render 6（路径遍历防护、GIF 预览线程化）、W2 edit_controller 2（插值命令入撤销栈 + 环检测门控提交；附带修 AddAutomationKeyframe 枚举按值查找恒落空真缺陷）、W3r teaching UI 3（浏览器验证线程化+revision 守卫、全文档元组重排）、W4r 散件 6（FX-C02 计划接生产消费者、默认生成传输安装）。
- **DXGI 根因（23b89b7dc）**：经 Microsoft Learn 官方表核实 NV12=103/P010=104，修正 d3d11_device 历史错值（65=A8_UNORM/100=AYUV）与探测门禁（77=BC3_UNORM），238 项 windows_hw 测试全绿。
- 遗留登记（非计划项）：G101 建议 file_controller 显式 shutdown 接线与 allow_master_fallback 热路径传参；G95 建议补 jsonschema 依赖；G97 建议 tracking_service 双重编码清理（AUD-24907 变通冗余）；tests/edit test_p2_09_session_pool 4K 真媒体本机挂起（环境性）。
- **验证**：各组新测试合计 ~520 passed；邻接合计 ~6000+ passed。
- **报告**：reports/wd17-*.md。

## Wave GAP-D（2026-10-01）— GAP-002/007/010/027 补覆盖 → **批 D 1517/1517 清零**

- GAP1（77c93793e）：edit_controller 11.4%→22.3%（66 用例：命令路由/渲染缓存清理矩阵/盖章栈 id/dispose 断连）+ ai_edit_controller 35.5%→46.8%（44 用例：chat 生命周期/ASR-TTS 失败分支/压缩链）；零源码改动。
- GAP2（07374d3f6）：file_controller（68 用例：manifest 路由/autosave 恢复/保存-放弃-取消矩阵/GPU 延迟销毁）+ main_window（35 用例：页面上下文/按键路由/错误处理）；零源码改动。
- 新登记 **AUD-R2-014**：`_cleanup_obsolete_render_cache_files` 共享路径保护恒不生效（GAP-002 覆盖实证）。
- 收尾修复（d0056d98f）：特效面板硬编码 CJK 迁 I18N + 10 语言包补词条（w12 引入的本地化回归，本地化门禁复检发现）。
- **批 A 135/135 · B 30/30 · C 9/9 · D 1517/1517 全部完成**；剩余 E 712 + F 1（FLASH_REMEDIATION_PLAN.md 交 GLM5.3-flash）+ §7 42（含 R2 登记项，待用户确认/专项）。
- **E 批 flash 会话启动（2026-10-01）**：已完成 5 批待复查勾选——EF-14（25695e98d，13/13，连带修 FalseColor 图例 QRect NameError 既有 bug）、EF-22（9538e926b，AUD-38503 判 INVALID：docstring 当前已正确，执行验证）、EF-13（22e404c8b，12 FIXED + 1 PARTIAL[AUD-48006 根因在白名单外 composite_page] + 1 DUPLICATE）、EF-08（1e8ecd2d6，18/18，2 个既有测试行为同步已披露）、EF-07（efd703545，18 FIXED + 1 决策记录[AUD-1003 保留拷贝，契约约束已注释]）。check_fixes 计数待 GLM5.3 复查 --mark 后推进。
- **E 批 flash 会话第二次记录（2026-10-01）**：累计 19/22 批完成——新增 EF-11（7c62bbdf0，16/16）、EF-12（87600a28c，15/15）、EF-05（c866a5a36，21F+1INV+1BLK）、EF-10（97b2a7ca0，16/16）、EF-09（cd5477860，18/18 测试质量）、EF-06（ba0a770f7，19F+1INV：AUD-11307 审计误报有实据）、EF-17（79e019e12，20/20）、EF-18（cbda06cd1，20F+1INV）、EF-15（377e19318，22F+1INV）、EF-20（246373f8a，24/24）、EF-19（7ff275826，24/24）、EF-16（b6135bdd0，20F+1BLK：AUD-22510 需 help_index.py 测试缝）、EF-21（e74d7996d，23F+1BLK：AUD-12613 根因在 file_controller 调用点）。在途：EF-01（67 条分 3 子运行，part1+part2 完成 48 条待合并提交，part3 在跑）、EF-02/03/04 part1 在跑。check_fixes 计数仍待 GLM5.3 复查 --mark 后推进。
- **E 逐条批全部完成（2026-10-02）**：22/22 批收官——最后三批 EF-01（70dd698e1，edit/core 67 条 3 子运行，169 测试）、EF-02（ffc8d4f38，shared/media 45 条 2 子运行，101 测试）、EF-03（bff172ec6，edit/ui 39 条 2 子运行，121 测试，1 INVALID 补回归）、EF-04（df751224d，ai_editing 37 条 2 子运行，106 测试）。713 条全部处理：约 690 FIXED + 6 INVALID（均附执行验证/代码证据）+ 1 DUPLICATE + 2 BLOCKED（根因在白名单外，报告已登记）+ 决策记录若干。E-rollup（186 模块行）第一波 5 个大模块已并行开工。check_fixes 计数待 GLM5.3 复查 --mark 后推进。
- **FLASH_REMEDIATION_PLAN 执行完毕（2026-10-02 收官）**：三大块全部落地——①E 逐条 22/22 批（713 条：约 690 FIXED、6 INVALID、1 DUPLICATE、2 BLOCKED、决策记录若干）；②E-rollup 186/186 模块行（2922 条 process 项：20 个提交，删未用导入约 1,100+、死代码 80+ 处、可证类型注解约 1,800+ 处，公共 API 零改名，DEFERRED 语义类逐条记录）；③批 F PLUG-004（c117537cb，18 个新 GLSL 插件，402 插件测试+37 编译门禁全过）。连带修复既有 bug 3 个（FalseColor QRect NameError、AUD-34709 部分构造边界回归、AUD-11310 契约测试同步）+ 回归测试约 1,100 项。全程 56 个修复批次提交、dev 分支、未勾选任何复选框（待 GLM5.3 复查 --mark）。check_fixes 计数待复查推进。
- **GLM5.3 复查会话收官（2026-10-02）**：FLASH_REMEDIATION_PLAN 三大块按 §6 八项清单逐批复核完毕并执行 `check_fixes.py --mark`——①E 逐条 22 批（520 条 AUD 全标：508 直标 + 12 invalid/duplicate/blocked 附注；AUD-48006/22510/12613 由复查 closeout f5f6636b8 补齐根因后转 closed）；②E-rollup 186 行（156 行标记于各自 efr 提交哈希，逐行经 git 归属+报告声明双重对账、修复 3 起宽子串误标与 10 起前缀碰撞哈希错位；30 行语义决策类 DEFERRED + 1 行审计未定位留挂）；③批 F（c117537cb + review-fix 327f8fd0c：C1/C2 px→UV 量纲、C3 死滑杆、I1 伪 Delta 修复 + test_plugin_batch2 渲染级语义套件 59 例）。**批 F 复核曾判 FAIL（3 个编译门禁拦不住的功能性 Critical），是本轮复核最有价值的拦截**。中心验证：45 项回退敏感性抽验 42/42+3 全红锁死（并修复 ef-15 测试盲区：_dispatch 锁掩蔽 append 出锁回退）、新测试 2,228 + 插件 461 + 编译门禁 37 复跑零失败、882 改动文件 py_compile 全过。终态：E done=678/713（35 项留挂=30 语义 DEFERRED[t1/t2 报告逐条注记]+1 审计未定位+4 人工门控[DOC-002/003/005 母语翻译与人工确认、PLUG-003 同]）、F done=1/1。复查记录 43 份归档 fixes/reviews/。

## 整体检查收官（2026-10-02，8e523e34b + 4a4d745c3）— Flash/复查会话后全库核验：24 个既有测试回归适配（零源码改动）+ 环境卫生

- **对账终态**：A 135/135 · B 30/30 · C 9/9 · D 1517/1517 · E 678/713（35 留挂 = 30 语义 DEFERRED[t1/t2 报告逐条注记] + 1 审计未定位 + 4 人工门控[DOC-002/003/005、PLUG-003]）· F 1/1 · §7 38 条全待人工确认（含 R2-014）→ TOTAL 2370/2443。§7 与复查前完全一致（此前摘要"46 条"为战役中段旧数）。复查记录 42 份核验齐全（收官提交信息"43"= 42 份 review + 计划表文件数）。
- **全库验证**（6 目录 ~27,140 通过）：edit 9835P/9F、composite 7267P/9F、shared 2426P/1F、app+plugins 1746P/6F、media 3425P/1F、color 2419P/0F（整目录单进程触发 D12 族段错误于 wd3g19 异步导出线程测试，分块两跑各自全绿——进程级脆点非代码问题）。
- **24 个确定性失败基线定性**（c2c6cb053 worktree 对照，254 用例基线全绿）：6 个白名单预存（GPU 位精确 4 + track_sync 1 + bench 资产 1）+ 1 个负载抖动（wd11g53 隔离即过）+ **24 个 Flash/复查会话遗留回归（全部为既有测试未随 E 批生产行为演进而适配，生产代码零缺陷零改动）**。
- **回归修复**：①8e523e34b（19 用例/12 文件）——frozen 快照漂移 5（AUD-48006：tiling 死赋值删除×3 + 构造器/replace×2）、桩契约漂移 8（FakeGraph.generation[AUD-9508]×4、latest_request_id×2、_teardown_comp_gl_resources[AUD-18909]、RIFE sess_options）、异步适配 4（AUD-03908 缓存标签泵事件循环等待+消除在途扫描竞态、AUD-20210 代理探测 QThreadPool 化后延迟驱动 worker 复现时序×2）、p8 源码审计对齐 AUD-7309 三态汇总、wd15g79 删 time monkeypatch（AUD-31903）；②4a4d745c3（5 用例/2 文件）——VT 原生桥夹具补 T-P0-10 pixel_format 契约 + 默认工厂变体加会话能力预检（本机 VT 拒 HEVC p010 -12900 按能力跳过；基线整体 skip 系 ef-02 修好会话初始化后暴露的夹具缺口）、RAW 参数断言适配 AUD-0612 入口浅拷贝（同一性→相等性、user_wb 指向引擎副本）。修复后 12+2 文件 616 用例全绿 + VT/RAW 文件 227 通过 3 能力跳过。
- **环境卫生**：.gitignore 补 `cache/`（paths.get_cache_dir() 运行时缓存根，测试反复重建造成 status 噪音）与 `*.db-shm`/`*.db-wal`（测试 SQLite WAL 边车）；基线对照 worktree 用毕即删；p29 门禁 json 还原。
- **交接状态**：战役+Flash+复查+整体检查全部收官；剩余 = E 35 留挂（语义决策/人工门控）+ §7 38（待用户确认）+ 复查遗留建议项；dev 领先 origin 122 提交未 push。

## 收官小批（2026-10-02，476f02364）— R2-014 修复 + §7 六项健壮性缺陷 + 四条陈旧复选框补记账

- **价值评估驱动**：整体检查后按"第一梯队"清单执行——R2-014（Major）+ §7 高价值项；§7 经全库 ID 标注交叉比对，另发现 4 条（AUD-31901/13504/14014/16316）战役期间已修但复选框陈旧，验证测试通过后补记账（w15 218bb19d4 / ef-04 df751224d / ef-01 70dd698e1）。
- **R2-014**：`_cleanup_obsolete_render_cache_files` 保护集改按过滤谓词补集（manifest 身份）划分——原"路径不在目标集合"过滤把与目标共享路径的有效 manifest 误排除，共享保护恒不生效、删除留悬空 manifest。GAP-002 契约翻转：他 clip 仍引用的共享文件保留；双方都进目标集才允许删除。
- **§7 五项**：AUD-34519 降级路径三处 `_close_quietly` 关闭已创建 source（句柄泄漏）；AUD-35906 `read_audio_samples` channel_count=0 按"不可解码返回 None"防御（除零）；AUD-9921 group_id 8 位 uuid 片段碰撞重生成+完整 uuid 兜底（静默覆盖组）；AUD-8916 MeshPickCache 条目弱引用活性校验（地址复用误命中失效 BVH）；AUD-12614 import_builder 逐素材探测隔离（单个损坏素材不再中断整批导入）。
- **验证**：新增回归 9 例 + GAP-002 契约翻转，定向 198 全绿；composite 7279P/0F、media 3420P+1 白名单、VT 9P/3 能力跳过；py_compile 6 源文件。
- **对账终态**：TOTAL 2379/2443——剩余 64 = E 人工门控 4（DOC-002/003/005、PLUG-003）+ E-rollup 留挂 31（30 语义 DEFERRED + 1 审计未定位）+ §7 待人工确认 29；AUD-R2-014（§8 后独立登记区）已闭环。

## 收官小批二（2026-10-02，ebea65c85）— §7 第二梯队十项 + pyflakes 静态扫描三个真回归

- **第二梯队（并发/正确性/资源/生命周期）**：AUD-6113 fused 源不再回写类级共享 _SHADER_SOURCES（现取+程序缓存）；AUD-6712 readback 保存/恢复调用方 viewport/FBO 绑定；AUD-34803 BGR→RGB 交 C 连续数组；AUD-27508 SVG path 非有限坐标降级；AUD-36404 node_help node_type 标识符白名单（防路径穿越）；AUD-12216/30907 五处 singleShot 改父属 QTimer——**行为测试当场拦下"PyQt6 静态 singleShot 支持上下文重载"的错误首版方案**，成员定时器为最终实现；AUD-19211 角度坐标缓存 0.1° 量化键；AUD-28501 蓝图网格横向线补 y=900；AUD-36915 移除只写不读的 <cwd>/.cache 孤儿 JPEG（保留 ThumbnailProvider 失效副作用）；AUD-9001（E-rollup DEFERRED 提级）model_import_cache 双预算驱逐（64 条/4GiB，最旧先删、保底最新一条）。
- **pyflakes 基线差集扫描**（新增 undefined name 19 条，17 条为字符串注解/沙箱脚本误报）揪出三个 efr 批"删未用导入"实为在用的真回归：**VideoJobStatus**（efr-small-s2，restore_from_disk 的 NEEDS_RELINK 翻转运行时 NameError，tests/deliver 两用例实证）、**QPainter2**（efr-small-s1 删别名致 _create_delete_icon 必崩）、**Mapping**（motion_binding from_dict 的运行时 isinstance）。各附运行时验证+回归测试；pyflakes 已入 .venv 供后续门禁复用。
- **验证**：定向 1210 全绿；composite 7282P/0F、shared 2429P/0F、tests/ui+export 2201P/6F（media_pool×2 白名单 + ef03×3 预存全套件状态污染[隔离必过，上一提交同败] + gpu_encode_surface×1 预存，均与本批无关，经 c53740035 worktree 对照证明）。
- **对账终态**：TOTAL 2390/2443——剩余 53 = E 人工门控 4 + E-rollup 留挂 30（29 DEFERRED + 1 未定位）+ §7 待人工确认 19。
