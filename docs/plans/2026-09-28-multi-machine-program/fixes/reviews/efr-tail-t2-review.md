# 复查报告：efr-tail-t2（rollup 清尾 T2：28 条带行号单项）

- 提交：`fb086e33fa1abf4a4e293a1c2d5204499af42736`（[fix-E efr-tail-t2]，19 files，+296/−106）
- 执行报告：`docs/plans/2026-09-28-multi-machine-program/fixes/reports/efr-tail-t2.md`
- 复查日期：2026-10-02 ｜ 只读复查，未运行 pytest

## 结论：PASS-with-notes

## 1. 范围核对（含跨提交归属梳理）

提交含报告新增 + 18 个源文件 = 本报告 13 文件清单 − commands.py（**AUD-16216 的 9 命令类序列化实际落入 tail-t1 提交 5baa8c8b3**，diff 已在 t1 review 中核验：纯增量 to_dict/from_dict + `_MACRO_SUBCOMMAND_TYPES` 登记）＋ s5 报告的 6 个文件（preview_quality/keyframe/graph_ui_protocol/panels registry/actions/preferences __init__，逐一 diff 核对与 s5 声明一致，详见 s5 review）。全部改动可溯源到本组 8 报告并集；无越界、无 REMEDIATION_PLAN.md/FIXLOG.md/plugins 改动（docs/plugins/2026-09-28/i18n 只读评估，确无改动）。

## 2. FIXED 项抽查明细（16 项中抽查 12 项，全部在位）

| 条目 | 抽查内容 | 结果 |
|---|---|---|
| #1 AUD-16216 | commands.py 9 类序列化（在 t1 提交中核实，17 个新增 to_dict/from_dict def） | ✓ |
| #2 AUD-31202 | main.py --project 存在性 + .topos 后缀校验，非法路径 logger.error；不拒绝启动（产品决策未做，注释声明） | ✓ |
| #3 AUD-31203 | `def main() -> int`、module_failures 注解、不可达 `return 0` 删除（os._exit 后，带注释） | ✓ |
| #4 AUD-31713 | make_volcengine_generation_provider(:757) 返回注解改公共联合；urllib.parse/Mapping 确由 s3 批完成（:326 在用） | ✓ |
| #7 AUD-33106 | export_panel 95 处 I18N.tr（新增 8 处含 key+英文模板） | ✓ |
| #9 AUD-34402 | color_decode 3 处 logger.warning（模块 logger 新增），回退结果照旧 | ✓ |
| #10 AUD-34423 | color_metadata `ColorPrimaries.AP1: 'ap1'`(:208) + 注释；get_standard_name 注解 | ✓ |
| #12 AUD-34520 | errors.py `PermissionError(MediaError)` → `MediaPermissionError`(:91)；全仓无 `from errors import PermissionError`（__init__/error_handling 再导出清单核对均不含），剩余 PermissionError 命中全部为内建引用——改名零影响成立 | ✓ |
| #13 AUD-34910 | `_hwaccel_context` 全仓 0 命中（6 处写入已删） | ✓ |
| #15 AUD-35719 | `Optional[callable]`/`callable` 注解 0 残留；死 DTO `ThumbnailImageResult` 全仓 0 命中 | ✓ |
| #17 AUD-37013 | topos_project_controller 模块级 `import time`（:25，带 AUD 注释），4 处函数内导入消除 | ✓ |
| #18/19 AUD-37200/37201 | render_diagnostics `_VALID_SOURCES` frozenset 恢复 + record() 告警级校验（照常入库）；subjects 走 `_normalize_context_identifier` | ✓ |

行为披露核验：本批唯二"计划行明确要求的增量补全"（命令序列化、subjects 规整）披露属实；render_diagnostics 校验为告警级不改写入行为；I18N.tr 未收录 key 返回英文原文与改前逐字符相同（tr 语义抽查成立）。

## 3. DEFERRED 理由核对表（12 条逐条）

| 条目 | ID | 主题 | 报告理由 | 复查判定 |
|---|---|---|---|---|
| 5 | AUD-32802 | 握手失败仍继续 | 需改 _handle_command 控制流，会话恢复策略属语义决策 | ✓ |
| 6 | AUD-32804 | INITIATE_COMMS 严格解码 | 放宽校验 = 协议接受面变更 | ✓ |
| 9a | AUD-34405 | resolve/转换器集合对齐 | 扩/缩任一侧都改变接受/拒绝面 | ✓ |
| 9b | AUD-34406 | chroma_siting 校验 | 新增拒绝 = 行为变更，合法值域属规格决策 | ✓ |
| 11 | AUD-34503 | decode_queue None 哨兵 | 需重构入队协议（错误通道） | ✓ |
| 14 | AUD-35131 | macos_vt 顺序分支补 try | 把异常上抛改吞掉返回 None，触碰 except 语义红线 | ✓（台账对照一致） |
| 16 | AUD-36702 | descriptor 同空容忍 | 构造期抛错 = 合同收紧行为变更 | ✓ |
| 20 | AUD-37203 | 单例从不 clear | 接线生命周期点属设计决策；clear() 是测试复用的公共 API 不可删 | ✓ |
| 22 | AUD-38110 | style_sheet C 常量承诺 | 量化 70 色 97 处未收录 + QSS 花括号全量转义 = 大规模视觉回归风险 | ✓（台账对照一致，量化理由扎实） |
| 24 | AUD-38703 | CFA 位深谓词不一致 | 13–15 位解码曲线改变，规格决策 | ✓（台账对照一致） |
| 25 | AUD-39407 | session load 非原子 | 并发语义重构（锁范围/epoch），有死锁取舍 | ✓（台账对照一致） |
| 26 | docs/plugins i18n | 目录归属批 F 工作区，台账无 AUD 条目 | 只读评估，零改动 | ✓ |

12/12 条理由成立，台账对照抽 5 条原文一致。

## 4. 红线

- 裸 except：无新增（macos_vt 补 try 正确 DEFERRED） ✓
- 信号 connect：零改动 ✓
- GLSL/色彩矩阵：零触碰（color_decode 仅加告警，矩阵回退结果不变） ✓
- 公共 API：`PermissionError→MediaPermissionError` 为唯一改名，经全仓导入面核实零使用方（内建引用不受影响）；其余零改名 ✓

## 5. py_compile（8 文件）

main.py / errors.py / color_decode.py / color_metadata.py / render_diagnostics.py / export_panel.py / hwaccel.py / routing_dialog.py → 全部 OK。

## 6. 综合判定

**PASS-with-notes**。Notes：① 最大 FIXED 项（commands.py AUD-16216）实际在 t1 提交、本提交代收 s5 的 6 文件——提交/报告非双射（内容全部可溯源）；② 其余真实性、红线、编译全部通过。
