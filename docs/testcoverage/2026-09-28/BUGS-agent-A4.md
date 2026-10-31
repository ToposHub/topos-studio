# BUGS — agent-A4（src/shared/color/color_picker.py 补测）

日期：2026-09-28
模块：`src/shared/color/color_picker.py`
来源：计划02（测试覆盖补齐）补测过程中发现。以下问题均**未修改源码**，仅记录；
相关测试已固化当前行为（见 `tests/shared/test_color_picker_coverage.py` 中标注 BUG-A4-xx 的用例）。

---

## BUG-A4-01　ACEScg 源空间被静默当作 sRGB 处理；ACEScg 目标空间被套用 sRGB OETF

- 位置：`rgb_to_xyz_linear`（L212-223）、`xyz_linear_to_rgb`（L225-238）、`check_out_of_gamut`（L248-267）
- 现象：
  1. `rgb_to_xyz_linear(r, g, b, "ACEScg")` 落入 `else` 分支使用 `SRGB_TO_XYZ`——模块顶部定义的
     `ACESCG_TO_XYZ`（L183-187）**从未被任何代码引用**。`convert_color_space(255,0,0,"ACEScg","sRGB")`
     返回 `(254,0,0)`，几乎等于恒等变换（正确转换应明显不同）。`"Rec.709"` 源同样被静默按 sRGB 处理。
  2. 反向不对称：`xyz_linear_to_rgb(..., "ACEScg")` 支持 ACEScg 目标，但对结果套用
     `delinearize_srgb`（sRGB gamma），而 `COLOR_SPACES` 声明 ACEScg gamma=1.0（线性）。
     白色 `sRGB → ACEScg` 得 `(243, 254, 248)`，白点不被保持。
  3. `check_out_of_gamut(..., target_space="ACEScg")` 直接 `return False`，色域检查被静默跳过。
- 影响：设置面板（SettingsDialog 色彩空间下拉含 ACEScg）与 CMS 转换在 ACEScg 路径下结果错误且无告警。
- 建议：`rgb_to_xyz_linear` 增加 ACEScg 分支并真正使用 `ACESCG_TO_XYZ`；`xyz_linear_to_rgb` 按目标空间 gamma 编码（ACEScg 保持线性）；未知空间名应显式报错或告警，而非静默回退。
- 关联测试：`TestXyzAndColorSpace::test_acescg_target_output_in_range`（烟雾级，仅锁定输出范围）

## BUG-A4-02　SwatchPage 单独确认颜色会把已保存的拾色器自定义色板/历史抹为默认值

- 位置：`SwatchPage._confirm_color`（L1858-1868）
- 现象：调用 `save_config(self.swatch_data, current_color_hex)` 时未传
  `picker_custom_swatches` / `color_history`，`save_config` 用默认值 `["#333333"]*20` / `[]`
  整体覆写配置文件。已复现：先保存含自定义数据的配置 → 新建 SwatchPage → `_confirm_color()`
  → `load_config()` 中两个字段均被重置。
- 影响：在 `CompactColorStudio` 内被掩盖（`colorConfirmed → _confirm_and_close` 随后会带全量数据再存一次），
  但 SwatchPage 被单独使用（或信号未连接）时造成用户数据丢失。
- 建议：`_confirm_color` 读取现有配置透传 picker 字段，或 `save_config` 对 None 字段保留旧值而非覆写默认值。
- 关联测试：`TestSwatchPage::test_confirm_wipes_picker_fields`（固化当前行为，修复后需反转断言）

## BUG-A4-03　ASE 导出头部块计数与实际写入条目数不一致

- 位置：`SwatchPage._write_ase_file`（L1966-1994）
- 现象：头部 `total_colors = sum(len(row.get_colors()))` 统计**所有**色块（默认色板 8 行 × 20 = 160，
  含 `#333333` 占位），但循环里 `if color_hex != "#333333"` 只写入非占位条目（实测 140 条）。
  头部声明 160、实际 140，违反 ASE 规范（块计数字段必须等于后续块数）。
- 影响：部分解析器（After Effects / 第三方库）会按 160 个块继续读，解析错位或报文件损坏。
- 建议：计数与写入使用同一过滤条件。
- 关联测试：`TestSwatchPage::test_write_ase_file`（同时断言了 160/140 两个现状值）

## BUG-A4-04　delinearize 后用 `int()` 截断导致纯白经 XYZ 路径变成 254

- 位置：`xyz_linear_to_rgb` L237（`int(delinearize_srgb(c) * 255)`）
- 现象：clamp 到 1.0 后 `1.055*1.0 - 0.055` 的浮点结果为 `0.9999999999999999`，乘 255 再
  `int()` 截断得 254。超界正向 XYZ 输入返回 `(254,254,254)` 而非 `(255,255,255)`；白色跨空间
  转换同样受影响（如 sRGB 白 → Display P3 各通道 254）。
- 建议：用 `round()` 或对饱和值显式取 255。同类截断也存在于 `lab_to_rgb`（L306-308）。
- 关联测试：`TestXyzAndColorSpace::test_xyz_linear_to_rgb_clamps`（固化 254 现状）

## BUG-A4-05　饱和蓝的 Lab↔RGB 往返误差达 10/255（约 4%）

- 位置：`rgb_to_lab`（L270-283）/ `lab_to_rgb`（L286-310）
- 现象：`(0,0,255) → Lab(32,79,-107) → (10,0,252)`，R 通道偏差 10。原因：
  `lab_to_rgb` 使用 4 位小数的舍入矩阵常数，且重建出的 R 线性值（≈0.00334）恰好落在 sRGB
  分段膝点（0.0031308）附近，gamma 放大相对误差；加上 `int()` 截断。
  其它原色误差 ≤3（绿 252、红 254）。
- 影响：PickerPage 的 Lab 输入框与 RGB/HSV 之间的换色存在可见偏差（蓝色偏紫红）。
- 建议：矩阵常数提高到 6-7 位有效数字；输出取整用 `round()`；或直接复用 `color_conversion` 的精确实现。
- 关联测试：`TestLabConversion::test_lab_blue_roundtrip_large_error`（固化 (10,0,252) 现状，修复后应改为小容差断言）

## BUG-A4-06（低危）　EyeDropperOverlay 未成功 start() 时点击会 AttributeError

- 位置：`EyeDropperOverlay.start`（L430-446）/ `mousePressEvent`（L448-456）
- 现象：`start()` 在 screen 为 None 或截图为空时提前 `return`（只发 cancelled），不创建
  `self.full_screen_img`；若调用方随后仍 `show()` 了该 overlay，`mousePressEvent` 访问
  `self.full_screen_img` 抛 `AttributeError`。
- 建议：`__init__` 中预置 `self.full_screen_img = None` 并在 `mousePressEvent` 判空；提前 return 前 `self.hide()`。
- 关联测试：`TestDialogsAndOverlay::test_dropper_esc_emits_cancelled`（仅覆盖 Esc 路径，未覆盖该缺陷）

---

### 汇总

| 编号 | 严重度 | 一句话 |
|------|--------|--------|
| BUG-A4-01 | High | ACEScg 源/目标转换矩阵与 gamma 处理错误，常量死代码 |
| BUG-A4-02 | High | SwatchPage 单独确认覆写并清空 picker 自定义色板/历史 |
| BUG-A4-03 | Medium | ASE 头部块计数 160 ≠ 实际条目 140，文件不规范 |
| BUG-A4-04 | Low | int() 截断使白经 XYZ 路径变 254 |
| BUG-A4-05 | Medium | 饱和蓝 Lab 往返 R 通道误差 10 |
| BUG-A4-06 | Low | 取色覆盖层未成功启动时点击崩溃 |
