# C06 lib1_halftone —— CMY 网点套印节点（复用 F13）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C06 行——无 halftone 节点；复用 Halftone_Print 几何 + F13。

## 交付物

- `lib1_halftone_node.py`：Node 类草案（参数表与 F13 oracle DEFAULTS
  逐项一致；tile_padding = max(|reg_offset_x|, |reg_offset_y|)+1——三版
  采样位偏移口径）。

## 复用关系 / 恒等语义

- **单一事实源**：`references/halftone_ref.py` 只再导出 F13
  `references/print_registration_ref.py`（不复制实现）；节点 marker 参数
  快照直接喂 F13 oracle 有效（透传已测）。
- 三版（C 0°/M 15°/Y 30°，pitch 8px）网点覆盖率 + 套印错位 ±off，叠印
  乘性掩码；输出为纸反射域（显示域艺术效果，显式声明）。
- **恒等**：ink_amount=0 精确恒等（任意 paper_base）。

## 验证现状

- marker 协议/参数透传至 F13 oracle/恒等——已测
  （tests/content_library_v1/test_c06_halftone.py；F13 全量语义见
  test_f13_print_registration.py，不重复）。**未验**：L04 接线全家。
