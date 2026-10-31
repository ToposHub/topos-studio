# 代码质量还债计划（2026-10-28）· 完成度审计

> 对照 docs/code_quality_debt_plan_2026-10-28.md 的执行总表与第六节"完成定义（整体 DoD）"，
> 本文件是终态审计。执行期 2026-10-28 单日，fix-ui 分支，约 40 笔提交。

## 一、P0~P7 执行总表终态

| 阶段 | 计划验收 | 终态 | 结论 |
|---|---|---|---|
| P0 | AGENTS.md 路径全部存在 | 全部路径 ls 验收过（唯一残留为刻意❌反例） | ✅ |
| P1 | 静默 except 清零/白名单登记，域测全绿 | 裸 except 20→0；pass 体 1270→1210（60 处升级日志/1 处该抛复审单独提交/其余 KEEP）；白名单+JSON 入档；16 批域测全部"失败集合⊆基线" | ✅ |
| P2 | lint 门禁 required，首轮清零 | ruff（E9+F 全量）绿 + mypy 增量域（21 文件）绿，CI lint job 已挂；首轮 E9/F63/F7/F82 清零后又完成 F 全量收紧（1132 处）与 mypy 扩域轮 | ✅（mypy 全量见 DoD 注） |
| P3 | 清单文件清零，10 语言补齐 | 首轮 615 处：569 修复+26 误报跳过（清零）；508 新键×10 语言三批中央合并全部内容级校验（0 改 0 删精确+键）；防回归棘轮 tools/check_i18n_literals.py 入 CI watch | ✅（二轮 31 处漏网登记在册，属计划外新增模式） |
| P4 | 差集报告 + 假 skip 激活 | docs/local_full_run_report_2026-10-28.md：34 失败全基线复现零新增；~737 假 skip 本机真 GPU 激活；CI 差集子项因 Gitee 远端无 artifact 通道如实声明不可达 | ✅（差集子项声明不可达） |
| P5 | 无测试模块清零 | main/products/shared 小模块/preferences/export 七模块 253 例新增全绿并接入 core-gates required | ✅（T5 control_surfaces 核实已有 tangent 4 文件覆盖，无缺口） |
| P6 | 包化完成，import 不破，全量绿 | 10 刀完成 13 文件包；137 测试深导入零破坏；每刀可追溯；终态 tests/edit 13 失败=基线逐条相同；infra 评估结论"不再拆"入档 | ✅ |
| P7 | 编译测试过 + 参数契约 | 3 新插件（Sky_Replace_Matte/God_Rays/Frequency_Split），真实 GL3.3 编译门禁 42/42，一插件一提交 | ✅ |

## 二、整体 DoD 逐条审计

1. **`except...pass` 仅剩登记白名单；裸 `except:` 为 0** ✅
   白名单 = docs/p1_silent_except_whitelist_2026-10-28.{md,json}（1210 处
   KEEP 全量+目录分布+14 处 NEEDS-LOGGER 待人工定清单）；裸 except 0
   （唯一 grep 命中为 docstring 历史文字）。
2. **ruff+mypy 在 PR required 门禁绿** ✅/🔶
   ruff：E9+F 全量绿（CI lint job required 口径）；mypy：files 含 8 个
   域 21 文件全绿。**注**：DoD 字面的"files=src 全量"与计划 P2 正文的
   增量轮次策略矛盾（src 全量实测 11,130 错）——按计划正文执行增量轮，
   已完成 2 轮（首域+扩 5 域），余量登记为后续轮次（每轮先看数再定，
   codec 15 / project 29 为下一批候选）。
3. **i18n 清单文件清零，防回归入 CI** ✅
   首轮清单清零（569 修+26 登记误报）；一致性门禁 5 例 + 字面量棘轮
   tools/check_i18n_literals.py（14 基线全带理由）挂 watch job（按计划
   四：先观察后翻 required）。
4. **本机全量通过率/差集在案，假 skip 激活数登记** ✅
   报告在案（≈97.9% 通过，34 失败全基线复现；激活≈737）；差集子项
   不可达声明在案。
5. **无测试模块清零并入门禁** ✅ core-gates 新增 376 例 required 步。
6. **commands.py 包化可追溯全量绿** ✅ 见 P6 登记文档。

## 三、执行中发现并修复的存量真 bug（计划外收益，均有专commit）

1. grade_order.py 星号 walrus 语法错误（模块自提交起不可导入）
2. composite_render_output：成功态 encoder.close() 失败交付坏文件 +
   RenderOutputResult.status 默认值 'complete' 误导（失败结果带成功状态出门）
3. topos_vault Any/os 缺导入（type:ignore 掩盖 / 真 NameError）
4. render_scheduler bake 审计路径 logger 未定义 NameError
5. composite_viewport GL_TEXTURE1/2（ODT LUT/split-view 运行时）+ y0 漏改名（放大镜）
6. render_cache_worker bitrate sidecar 写路径 Path 未定义（被 except 吞）
7. timeline_ops clip_duration 先用后赋（overwrite 切半分支 UnboundLocalError）
8. timeline_view I18N 先于局部导入（UnboundLocalError）+ timeline_panel QTreeWidgetItem 运行时未导入
9. file_io tier None 直接 .family（崩溃路径）
10. tts_provider 10 行不可达死代码 / F811 复制粘贴重复方法等（F 轮清出）
11. **登记未修**（潜伏问题清单，见 P2-第3轮c 提交）：三命令 undo 数据死
    代码、negative_prompt/guidance_scale/alpha_clamp 参数未下发等 14 项

## 四、交接清单（后续轮次素材，均在册）

- mypy 扩域轮（11,130 错余额；codec=15/project=29 下一批）
- ruff 收紧轮（B006/B008=33、UP、SIM/C4 按需）
- i18n 二轮 31 处（addMenu/getText/_Q 别名）+ watch→required 翻牌
- NEEDS-LOGGER 14 处（模块无 logging 设施，需人工定加 logger 方案）
- 14 项潜伏真问题核实（P2-第3轮c 提交内清单）
- CI nightly junit 差集（待 Gitee artifact 通道）
- 8 个万行 UI 文件拆分（计划明确"不在本计划内，另行立项"）
