# N12 留档：剪贴板选择域（2026-11-04）

## 实现

- **service**：`op_paste_selected_domains`——按选定域粘贴
  （grade/curves/lut/window/effects；`_PASTE_DOMAIN_TARGETS` 域→载体表）：
  - 只写选定域（仅粘曲线不改 LUT/Window/grade…）；未选域原样保留；
  - **身份重建**：效果实例新 instance_id、窗口形状新 id（不共享来源
    legacy Layer 身份；跨 clip/Compound/工程粘贴安全——多次粘贴各自独立）；
  - LUT 成对粘贴（资源引用 + 强度，R05 域）；锁定/非 Corrector/未知域
    显式拒绝。
- **契约**：`paste_domains` intent action 登记。
- **视图**：`copy_grade` 升级为多域捕获（grade/curves/lut/window/effects
  全捕获自 backend_payload）；右键新增 "Paste Domains" 子菜单（分域 +
  All）；粘贴经 apply_edit 权威提交。

## 测试（tests/color/test_selective_paste_domains.py，3 项 + 视图回归）

仅 curves 粘贴（grade/LUT/窗口不动）；effects/window 身份重建（参数
保留）；锁定与未知域拒绝。
回归：选择性粘贴 + 节点视图 26 passed；原生验证 passed。

## 剩余（如实登记）

跨工程粘贴的**共享定义**冲突重命名/去重规则（跨工程同 id 不同内容）仍
随后续批次（当前跨工程粘贴走 B14 节点载荷通道，共享表现为独立定义——
安全不串改，但未共享）。
