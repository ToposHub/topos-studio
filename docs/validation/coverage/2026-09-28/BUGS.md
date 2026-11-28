# 计划02 疑似 Bug 台账（补测过程中发现，未修改 src）

---

<!-- 来源：BUGS-agent-A1.md -->

# 测试覆盖补齐 — Agent A1 疑似 Bug 报告

模块：`src/features/color/core/async_renderer.py`
日期：2026-09-28
来源：补测过程（tests/color/test_async_renderer_coverage.py 编写与验证）
约定：只记录，不修改 src。

---

## BUG-A1-01

- **编号**：BUG-A1-01
- **位置**：`src/features/color/core/async_renderer.py` `_do_bake_layer()`，
  `build_composition_shader(...)` 调用处（约 line 6418-6430，`request_id=render_request_id`）
- **描述**：`_do_bake_layer` 作用域内从未定义 `render_request_id`（全文件唯一的
  `render_request_id = params.get('render_request_id')` 赋值在 `_do_render_impl`，
  line 2319）。当栈前缀（`stack_prefix`）中存在**未冻结**（非 `is_baked`）的层时，
  该层会走到 `build_composition_shader(request_id=render_request_id, ...)`，
  Python 在求值实参时抛出 `NameError: name 'render_request_id' is not defined`。
  异常沿 `_handle_command` → `run()` 的 G-P0-11 try/except 被吞掉（仅打印
  traceback），`layer_baked_ready` 信号永不发射，整个 bake 静默失败。
- **证据**：
  1. 复现脚本（`AsyncRenderer.__new__` 桩 + `ctx=Mock()` + 一个非 baked 前缀层）
     稳定输出：`NameError: name 'render_request_id' is not defined`。
  2. `grep -n "render_request_id" async_renderer.py`：全部 6 处中只有
     `_do_render_impl` 内有赋值（line 2319），`_do_bake_layer` 内零赋值。
  3. 只有 `stack_prefix` 全部为已冻结层（走 `baked_tex is not None` 直通分支）
     时才不触发——正常“冻结最上层”的 bake 一定经过未冻结层，即正常路径必炸。
- **影响**：`BAKE_LAYER`（AI Baking 冻结层）功能在包含任一未冻结层的栈前缀上
  完全失效（无信号、无结果纹理上传），且无可见错误提示（只有控制台 traceback）。
- **置信度**：高（确定性复现，静态赋值缺失）。
- **修复建议**（未实施）：在 `_do_bake_layer` 开头补
  `render_request_id = data.get('render_request_id')`，或改用
  `request_id=data.get('render_request_id')`。

---

## BUG-A1-02（轻微 / 仅影响测试替身）

- **编号**：BUG-A1-02
- **位置**：`src/features/color/core/async_renderer.py` `_handle_command()` 的
  `RESET_VIDEO_DENOISE_HISTORY` 分支（line ~975，
  `pipeline = getattr(self, 'video_denoise_pipeline', None)`）
- **描述**：本文件对 `__new__` 构造的轻量测试桩有明确的防御惯例——`run()` 与
  `_do_render` 等处注释写明“__dict__.get 守卫：测试可经 __new__ 构造跳过
  __init__”，并用 `self.__dict__.get(...)` 读取可选属性。原因是未执行
  `QObject.__init__` 的实例上，缺失属性的普通 `getattr(self, x, default)` 会抛
  `RuntimeError`（PyQt6 抛 RuntimeError 而非 AttributeError，`getattr` 的默认值
  兜不住）。`_handle_command` 该分支用的是普通 `getattr`，与文件自身惯例不一致。
- **证据**：补测中以 `AsyncRenderer.__new__(AsyncRenderer)` 桩直接调用
  `_handle_command(RESET_VIDEO_DENOISE_HISTORY, None)` 抛
  `RuntimeError: super-class __init__() of type AsyncRenderer was never called`；
  改为显式设置 `video_denoise_pipeline=None` 后正常。
- **影响**：生产路径不受影响（真实实例总是跑过 `__init__`）；仅使测试替身在该
  分支上更难构造，属一致性/可测性瑕疵。
- **置信度**：高（现象确定）；严重度：低。
- **修复建议**（未实施）：改为
  `pipeline = self.__dict__.get('video_denoise_pipeline', None)` 与文件内既有
  惯例对齐。

---

## 附：本次补测覆盖数据（同口径对比）

- 口径：`pytest tests/color --cov=src.features.color.core.async_renderer`
  （3550 statements）
- 补测前（不含新文件）：20%（miss 2844）
- 补测后（含 `tests/color/test_async_renderer_coverage.py`）：33%（miss 2384）
- 新增覆盖语句：+460


---

<!-- 来源：BUGS-agent-A2.md -->

# BUGS-agent-A2：image_engine.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/features/color/core/image_engine.py`（含 `src/features/color/core/layer.py` 序列化侧）
- 来源：Plan-02 补测（`tests/color/test_image_engine_coverage.py`）
- 处置原则：**只报告，不修改 src**。对应回归用例以 `xfail(strict=False)` 锁定在
  `tests/color/test_image_engine_coverage.py`，修复后应转为普通断言（去掉 xfail 标记）。

严重度：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）> low（健壮性缺口）。

---

## BUG-A2-01（medium）：undo/redo 不恢复翻转状态

- 位置：`ImageEngine._restore_state`（image_engine.py ~L4194）
- 现象：`flip_image()` 通过 `history.push_state(..., flip_h=..., flip_v=...)` 把翻转
  标志存入快照（`HistoryManager.push_state` 完整保存 `flip_h`/`flip_v` 字段），但
  `_restore_state` 只消费 `image`/`layers`/`active_layer_index`，从不读取
  `state.get('flip_h'/'flip_v')`。
- 后果：翻转 → 撤销后，像素恢复了，但 `get_flip_state()` 仍返回翻转后的标志；
  1) 项目保存/加载（`set_flip_state`）会写入错误翻转态；
  2) 随后再执行翻转/`reset_flip` 时 `_apply_flip_state` 以 `img_source_full`
     为基准重算，产生双重翻转或与历史不一致的画面。
- 验证：`tests/color/test_image_engine_coverage.py::TestUndoRestoreFlipState`
  （xfail）。push 前后 flip 标志在 undo 后未回到 False。
- 建议修复方向：`_restore_state` 中读取 `state['flip_h']/['flip_v']` 并回写
  `self._flip_h/_flip_v`（注意 redo 栈路径 `HistoryManager.undo/redo` 保存当前态时
  也应带上 flip 字段，当前 redo_state/undo_state 未记录 flip）。

## BUG-A2-02（medium）：_restore_state 破坏 img_high/img_low 的 float32[0,1] 不变量

- 位置：`ImageEngine._restore_state`（image_engine.py ~L4203-4206）
- 现象：恢复图像时
  `low = FileIO._resize_to_fit(image, LOW_RES_SIZE); high = ...; self.img_low = low; self.img_high = high`。
  `FileIO._resize_to_fit`（cv2.resize）保持输入 dtype——`img_full_original` 是
  uint8，因此 undo 后 `img_high/img_low` 变成 **uint8 0..255**。
- 对比：所有其他写路径（`set_image` 的 `normalize_to_float32`、
  `_on_raw_update_finished`、`enter_offline_mode`）都产出 float32 0..1。
- 后果：undo 后任何以 `img_high` 为 float[0,1] 前提的消费方（`set_reference_image`
  的 else 分支 `astype(float32)` 不归一化 → 参考纹理全白、`request_color_output`
  的 eff_visible_rect 尺寸换算、`render_cached_image` 的 `(img_high*255)`）行为
  异常。
- 验证：`tests/color/test_image_engine_coverage.py::TestUndoRestorePreviewDtype`
  （xfail）：undo 后 `img_high.dtype == np.uint8` 且 max=255。
- 建议修复方向：与 `set_image` 相同的 `normalize_to_float32` 归一化后再赋值。

## BUG-A2-03（high）：undo/redo 无法恢复调色参数 —— Layer.to_dict 按引用序列化 grading_params

- 位置：`src/features/color/core/layer.py` `Layer.to_dict`（~L934）：
  `"grading_params": self.grading_params`（`mixer_params`/`effects_params`/
  `soft_clip_params`/`qualifier_params`/`plugins` 同样按引用）。
- 现象：`HistoryManager.push_state` → `_serialize_layers` → `layer.to_dict()`
  生成的快照 dict **与活动 Layer 共享同一个 grading_params dict 对象**。压栈后
  用户的任何参数修改都会直接改写已入栈的快照。
- 已端到端验证（headless 脚本）：
  ```
  layer.grading_params['exposure'] = 1.5; push_state(...)
  layer.grading_params['exposure'] = 9.0
  engine.undo() -> 返回 True，但 exposure 仍是 9.0（快照被污染）
  to_dict()['grading_params'] is layer.grading_params -> True
  ```
- 后果：非破坏性调色工作流（改参数 → 继续 → undo）完全无法回退参数值；redo
  同理。影响 wheel/curve/LUT 等全部经 `grading_params` 的操作历史。
- 验证：`tests/color/test_image_engine_coverage.py::TestUndoRestoreGradingParams`
  （xfail）。
- 建议修复方向：`to_dict` 对 dict 型字段做 `.copy()`（或 `copy.deepcopy`，
  注意 plugins 内嵌 dict）；`HistoryManager.push_state` 的 image 拷贝已做，layers
  序列化依赖 to_dict 的正确性。

## BUG-A2-04（low）：_render_mask_items_recursive 对未知条目类型的防御分支不可达

- 位置：`ImageEngine._render_mask_items_recursive`（image_engine.py ~L2584）
- 现象：循环第一行 `if not item.visible:` 在 `isinstance(MaskGroup/MaskShape)`
  判定之前执行；传入无 `visible` 属性的杂项对象（旧序列化数据/编程错误）直接
  `AttributeError`，注释里"跳过未知类型"的 `else: continue` 分支永远无法对这类
  对象生效。
- 影响：仅健壮性；正常运行数据不会出现杂项条目。
- 备注：测试以带 `visible` 属性的未知对象锁定"真跳过"分支；
  无属性对象崩溃为现状（未写为断言，避免把崩溃固化为契约）。

## 观察（不计入 BUG，供参考）

- `ImageEngine.remove_layer(index)` 对越界 index 无保护（`len(layers)>1` 即解引用），
  调用方传错 index 会 IndexError（对比 `copy_layer`/`paste_layer`/`duplicate_layer`
  均有边界检查）。
- `Layer.to_dict` 的 `plugins` 也按引用序列化，与 BUG-A2-03 同根因；修 to_dict 时
  一并处理。


---

<!-- 来源：BUGS-agent-A3.md -->

# BUGS-agent-A3：deliver_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/deliver_controller.py`
- 来源：Plan-02 补测（`tests/app/test_deliver_controller_coverage.py`）
- 处置原则：**只报告，不修改 src**。BUG-A3-01/02/03 的行为已用"锁定当前行为"的
  断言写入测试（见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）> low（健壮性缺口）。

---

## BUG-A3-01（high）：无 .topos 侧车的文件批量导出必然失败（UnboundLocalError）

- 位置：`DeliverControllerMixin._batch_export_single_file`（deliver_controller.py
  ~L1600-1631）
- 现象：sidecar 存在性检查的 `else` 分支（无 `.topos` 文件时）只设置
  `self.engine.layers = [Layer("Layer 1")]`，**从不给局部变量 `project_data` 赋值**；
  约 20 行之后的翻转状态恢复 `if isinstance(project_data, dict):` 读取该变量，
  抛 `UnboundLocalError: cannot access local variable 'project_data'`。
- 后果：批量导出队列中任何一个没有 `.topos` 侧车的文件（最常见场景：刚拖进队列、
  从未保存过 .topos 的新图）都会在加载后被该异常打断；上层
  `_process_next_batch_file` 的 `except Exception` 把它记为 FAILED
  （`error=UnboundLocalError...`），用户看到的是"渲染失败"，实际是控制器崩溃。
  队列自动保存侧车（`_auto_save_current_file_before_batch`）只覆盖"当前正在编辑的
  那一张"，救不了队列里其余文件。
- 复现（最小）：见
  `tests/app/test_deliver_controller_coverage.py::TestBatchExportSingleFile::test_missing_topos_sidecar_crashes_with_unboundlocal_bug`
  （`pytest.raises(UnboundLocalError)` 直接锁定）。
- 建议修复方向：`else` 分支补 `project_data = None`（或把 sidecar 加载统一成
  `project_data = ... if os.path.exists(...) else None`）；修复后把上述用例改为
  断言导出成功（输出文件名、`engine.layers == [Layer("Layer 1")]`）。

## BUG-A3-02（medium）：perform_quick_export 缺少 uint16→uint8 降位转换

- 位置：`DeliverControllerMixin.perform_quick_export`（deliver_controller.py ~L204-210）
- 现象：dtype 安全转换只处理 `np.float32` 分支；`np.uint16` 源（如 16-bit TIFF
  扫描件）对 `.jpg/.jpeg/.png/.bmp` **不做 `/256` 降位**，直接把 uint16 buffer 传给
  `FileIO.save_image`。同一逻辑在 `perform_panel_export`（~L158-160）和
  `_batch_export_single_file`（~L1729-1731）都有 uint16 分支，唯独 quick export
  漏了。
- 后果：JPEG 编码器收到 uint16 通常写盘失败或被底层钳制，表现为"快速导出失败/
  画面异常"，而同一个文件走面板导出或批量导出却是正常的——三条导出路径行为
  不一致。
- 锁定用例：
  `TestPerformQuickExport::test_uint16_passthrough_documents_missing_conversion`
  （当前断言 `saved.dtype == np.uint16`，修复后应翻转为 `np.uint8` 且值 = 原值/256）。
- 建议修复方向：照抄 panel export 的 `elif export_img.dtype == np.uint16:` 分支。

## BUG-A3-03（low）：sRGB 回退编码对负值像素放大而非钳制

- 位置：`_encode_composite_linear_to_srgb` 的 sRGB 回退分支（deliver_controller.py
  ~L73-82）
- 现象：`mask = rgb <= 0.0031308` 把**负值**也归入线性段，执行 `12.92 * rgb`
  且不钳制（`np.maximum(rgb, 0.0)` 只在幂律分支里），输入 -0.5 输出 -6.46，
  超出 [0,1] 编码范围；幂律分支对同样的负值却会钳到 0。
- 后果：仅在色彩变换 provider 不可用、走 sRGB 回退、且 composite 求值产生负值
  （高增益/超采样振铃）时触发，负值被放大 ~13 倍后喂给编码器，可能写成
  回绕伪影。触发面窄，故 low。
- 锁定用例：
  `TestEncodeCompositeLinearToSrgb::test_fallback_negative_handling_documents_amplification_bug`
  （当前断言输出 -6.46，修复后应断言 0.0）。
- 建议修复方向：mask 改为 `(rgb >= 0.0) & (rgb <= 0.0031308)`，或先
  `rgb = np.maximum(rgb, 0.0)` 再分段。

---

## 备查（不改代码的观察）

- `_batch_export_single_file` 返回类型注解为 `-> str`，但 6 条失败路径都返回
  `None`；调用方按真值判断所以无行为问题，仅为注解失真（低价值，顺带记录）。
- `_toggle_video_export_pause` 的 resume 分支直接 `self._video_export_paused_total += ...`
  （无 getattr 兜底），而 `_video_export_elapsed`/`_cleanup_video_exporter` 对同一
  属性都用 `getattr(..., 0.0)` 兜底。正常流程 `perform_video_export` 会先初始化该
  属性，目前不可达 AttributeError，仅风格不一致。
- `_batch_export_complete` 在批量导出前无打开文件（`_batch_original_*` 全 None）时
  不回滚 engine 状态，导出结束后 engine 里残留的是队列最后一个文件的图像/路径。
  疑似有意（无处可回滚），但与"恢复引擎原始状态"的注释不符，留待产品判断。


---

<!-- 来源：BUGS-agent-A4.md -->

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


---

<!-- 来源：BUGS-agent-B1.md -->

# BUGS-agent-B1 — composite_viewport.py 补测过程中发现的疑似 Bug

- 模块：`src/features/composite/ui/composite_viewport.py`（11,512 行）
- 发现日期：2026-09-28
- 发现方式：为补测试（tests/composite/test_composite_viewport_coverage.py）用真实
  Qt 鼠标事件（`mousePressEvent` / `mouseMoveEvent` / `mouseReleaseEvent`）驱动
  ROI / PAINT 工具时复现；既有测试均直接调用 `_handle_tool_press/_move/_release`
  私有方法，绕过了事件分发层，因此该路径从未被测试覆盖。
- 复现环境：PyQt6 6.10.2 offscreen（QT_QPA_PLATFORM=offscreen），无需 GL。
- 处理约定：按任务规则 **不修改 src**；两个行为缺陷已以 `xfail(strict=True)`
  回归用例固化在
  `tests/composite/test_composite_viewport_coverage.py::TestMouseEventDispatchRegression`
  （修复后会翻成 XPASS 失败，提醒同步移除标记）。

---

## BUG-B1-01 ROI 工具：真实鼠标事件无法完成 ROI 拖拽（不提交、不发信号）

- 位置：
  - `CompositeViewer._handle_roi_press`（~L8664）
  - `CompositeViewer.mouseMoveEvent` / `mouseReleaseEvent`（~L7606 / ~L7728）
- 根因：`_handle_roi_press` 只设置 `_user_roi_drag_rect` 并 `return True`，
  从不调用 `self._interaction_state.start_drag(...)`。而
  `mouseMoveEvent` 只在 `self._interaction_state.is_dragging` 为 True 时才分发
  `_handle_tool_move`；`mouseReleaseEvent` 同样只在 `is_dragging` 时才调用
  `_handle_tool_release`。结果：ROI 拖拽的实时矩形更新与松手提交路径
  （`_handle_roi_move` / `_handle_roi_release` → `_apply_user_roi` →
  `user_roi_changed` 信号）在真实鼠标交互下永远不可达。
- 用户可见症状：
  1. Shift+R 进入 ROI 工具后按住左键拖拽，画面无任何反馈（无拖拽框）；
  2. 松手后 ROI 不生效，`user_roi_changed` 信号 0 次发射，渲染区域不变；
  3. `_user_roi_drag_rect` 残留为按压时的 (x, y, 0, 0)，直到按 Esc 或下次
     press 才被覆盖。
- 复现脚本（offscreen）：
  ```python
  v.set_tool_mode(ViewerToolMode.ROI); v.set_texture(5, 1000, 1000)
  v.mousePressEvent(QMouseEvent(...LeftButton, QPointF(100,100), ...))
  v.mouseMoveEvent(QMouseEvent(...Move, QPointF(300,250), buttons=LeftButton, ...))
  v.mouseReleaseEvent(QMouseEvent(...Release, LeftButton, QPointF(300,250), ...))
  # 期望：_user_roi_rect 非空、user_roi_changed 发射 1 次
  # 实际：_user_roi_rect 为 None、信号 0 次、_user_roi_drag_rect 残留
  ```
- 既有测试为何没拦住：`tests/composite/test_composite_viewer_roi.py::
  test_roi_drag_interaction_commits` 直接调用 `viewer._handle_tool_press/_move/
  _release`，绕过了事件分发门控。
- 固化用例：`test_roi_drag_commits_through_real_mouse_events`（xfail strict）。
- 修复方向（供参考，未实施）：`_handle_roi_press` 成功时调用
  `start_drag(tool=ROI, ...)`（或让 mouseMove/Release 对 ROI/PAINT 这类
  “无 interaction_state 拖拽” 工具按各自活动标志分发）。

## BUG-B1-02 PAINT 工具：真实鼠标事件下笔画永不提交

- 位置：
  - `CompositeViewer._handle_paint_press`（~L8712）
  - `CompositeViewer.mouseMoveEvent` / `mouseReleaseEvent`（同上）
- 根因：与 BUG-B1-01 同源。`_handle_paint_press` 只写
  `_paint_drag_points`（首点）/`_paint_clone_src` 并 `return True`，不调用
  `start_drag()`；`is_dragging` 恒为 False，因此
  - move 事件不分发到 `_handle_paint_move` → 笔画永远只有 1 个点；
  - release 事件不分发到 `_handle_paint_release` →
    `paint_stroke_committed` 信号 0 次发射（页面无法组装 undo），
    `_paint_drag_points` / `_paint_drag_node_id` 松手后残留不清。
- 部分路径仍然可达的原因：Alt+click 克隆源（`_paint_clone_src`）与
  `[` / `]` 笔刷快捷键发生在 press/keyPress 阶段，不受该门控影响。
- 复现结论（offscreen 实测）：press 后 `_paint_drag_points == [[...]]`（1 点），
  两次 move 后仍为 1 点，release 后 `strokes == 0`、
  `_paint_drag_points` 仍非 None。
- 既有测试为何没拦住：`tests/composite/test_viewer_paint_tool.py` 全部直接调用
  `_handle_paint_press/_move/_release`。
- 固化用例：`test_paint_stroke_commits_through_real_mouse_events`（xfail strict）。

## BUG-B1-03（低风险，代码健康）get_state / set_state 在同类中重复定义，前一份是死代码

- 位置：
  - `get_state` L2929、`set_state` L2961（前一份）
  - `get_state` L10975、`set_state` L11012（后一份，运行时生效）
- 说明：Python 取后一定义，前一份永不执行。两份实现并不等价（前一份缺少
  `resolution_mode` / `magnifier_*` / `background_mode` / legacy
  `show_bbox` 别名等键，且 `set_state` 不走 `set_channel_mode` 等 setter）。
  行为当前正确（后一份生效），但任何人修改前一份都会静默失效；且
  `tests/composite/test_composite_viewer_compare.py::test_viewer_compare_mode_round_trip`
  读取的键恰好在两份里都存在，掩盖了差异。
- 建议：删除 L2929–L3026 的死代码副本（未实施）。

---

### 测试执行结论

- 新增：`tests/composite/test_composite_viewport_coverage.py`
  （121 passed + 2 xfailed(strict)，`-p no:cacheprovider`）。
- 既有 viewport 相关测试回归无干扰（详见任务汇报）。


---

<!-- 来源：BUGS-agent-B2.md -->

# BUGS-agent-B2 — node_properties_panel.py 补测过程中发现的疑似 Bug

- 模块：`src/features/composite/ui/node_properties_panel.py`（7,683 行）
- 发现日期：2026-09-28
- 发现方式：为补测试（`tests/composite/test_node_properties_panel_coverage.py`）
  通读全模块 + 用真实 `set_node`/`set_nodes` → 控件 `setValue`/`setCurrentIndex`
  信号路径驱动参数编辑时复现。多选路由缺陷即审查所指「多选路由写错节点」。
- 复现环境：PyQt6 6.10.2 offscreen（QT_QPA_PLATFORM=offscreen），无需 GL。
- 处理约定：按任务规则 **不修改 src**；两个行为缺陷已以 `xfail(strict=True)`
  回归用例固化：
  - `TestMultiSelectionRoutingRegression`（BUG-B2-01，2 例）
  - `TestPresetSnapshotRegression`（BUG-B2-02，1 例）
  修复后会翻成 XPASS 失败，提醒同步移除标记。

---

## BUG-B2-01 多选：编辑第二个节点 section 的参数被路由/写入第一个节点

- 位置：
  - `NodePropertiesPanel._on_param_changed`（~L4418-4433）
  - 与 `set_nodes` 的交互（~L1884-1891：`_current_node_id` 固定为
    `selected_ids[0]`，即第一个选中节点）
- 根因：所有参数行（`_create_param_row_for_section` 等工厂）把控件信号接到
  面板级的 `_on_param_changed`，而该方法用 **面板当前** 的
  `self._current_node_id` / `self._current_parameters` 做路由、相等性短路和
  section 同步。多选（`set_nodes` 传 ≥2 个节点）时 `_current_node_id` 恒为
  第一个选中节点，因此：
  1. 在 n2 的 section 里改控件 → `parameter_changed("n1", key, value)` 发给
     n1（CompositePage 会把参数写到 n1 并以 n1 推 undo/渲染）；
  2. `_current_parameters[key] = value` 污染 n1 的参数缓存，
     `section.parameters[key] = value` 写进 **n1 的 section**；
  3. n2 自己的 `section.parameters` 与面板显示保持旧值（面板与图数据脱节）；
  4. 相等性短路 `if self._current_parameters.get(key) == value: return` 比较
     的是 **n1 的缓存值**——若 n2 的新值恰好等于 n1 的现值，编辑被 **完全
     吞掉**（0 次信号发射）。
- 用户可见症状：框选两个同类节点，在下方（第二个）节点的参数面板里拖
  mix/scale 等，实际变化的是最上方第一个节点；某些值下改了没反应。
- 复现脚本（offscreen）：
  ```python
  panel.set_nodes([data_n1(gain=1.0), data_n2(gain=1.0)])
  panel.parameter_changed.connect(log)
  panel._sections["n2"]._param_widgets["gain"]["widget"].setValue(0.25)
  assert log.calls == [("n1", "gain", 0.25)]        # 期望 ("n2", ...)
  assert panel._sections["n2"].parameters["gain"] == 1.0   # n2 未更新
  ```
- 修复方向建议：行工厂在闭包里捕获本 section 的 node_id（或把 node_id 作为
  sender property 随信号传递），`_on_param_changed` 按 section 路由并读写该
  section 的 parameters；相等性短路也应对该节点的参数做。
- 固化用例：`TestMultiSelectionRoutingRegression`（2 例，strict xfail）。

### 附带观察（同根因，记录备查）BUG-B2-01a

- `_current_node_ids` / `_common_parameters` / `_param_values_per_node` 只在
  `__init__` 初始化，`set_nodes` 从不填充；`_compute_common_parameters` /
  `_create_multi_selection_params_group` / `_create_multi_selection_param_row`
  / `_on_multi_param_changed`（`parameters_changed_batch` 批量信号，
  CompositePage L6225 有连接、L6881 有处理器）在现行面板流程中 **不可达**
  （死代码）。多选批量编辑 + 批量 undo 的整条链路实际依赖上面的错误路由
  （逐个 `parameter_changed`）在工作。
- `_create_multi_param_changed` 本身的载荷契约（node_ids + 每节点旧值字典）
  是正确的，已由 `test_legacy_batch_signal_payload` 直接锁定，修复路由时可
  复用。

---

## BUG-B2-02 预设快照：combo 参数存的是显示文案而非原始选项值

- 位置：`NodeSectionWidget._collect_current_values`（~L1445-1449）
  ```python
  elif kind in ("combo", "channel", "bool_channel"):
      data = widget.currentData()
      values[key] = data if data is not None else widget.currentText()
  ```
- 根因：`_create_combo_row` 用 `combo.addItems(display_items)` 只放 **显示
  标签**（`_option_display_label` → `option_value.title()`），从不 `setItemData`，
  因此 `currentData()` 恒为 None，回退到 `currentText()` —— 是 `"Gaussian"`
  而不是原始值 `"gaussian"`。change 信号走的是 index→options[idx] 的原始值
  映射（正确），但「保存预设」(`_on_save_preset` → `_collect_current_values`)
  走的是显示文案：预设里存的值无法匹配任何原始 option，重新应用后 combo
  无法定位、参数值与合法枚举脱节。
- 受影响面：所有 option 参数且原始值不是 Title-Case 的节点预设（blur_type、
  filter、format_mode、pivot_mode 等大量小写原始值）。
- 附带小问题（同一方法）：`kind in ("float", "int")` 分支
  `values[key] = widget.value()` 返回 DragValue 的 float —— int 参数（如
  samples/width）在预设里变成 16.0 而非 16，类型漂移（多数下游能容忍，记录
  备查）。
- 修复方向建议：`addItems` 时用 `setItemData(i, raw_value)`；或
  `_collect_current_values` 对 combo 通过 `options[currentIndex()]` 反查原始值
  （与 change 处理器同一条路径）。
- 固化用例：`TestPresetSnapshotRegression::test_collect_current_values_combo_returns_raw_option`
  （strict xfail）。

---

## 汇总

| 编号 | 严重度 | 一句话 | 固化用例 |
|------|--------|--------|----------|
| BUG-B2-01 | 高（数据写错节点） | 多选时第二节点 section 的编辑被路由/写入第一个节点，相等值时被吞 | TestMultiSelectionRoutingRegression（2 xfail） |
| BUG-B2-01a | 观察 | 批量多选 UI/信号链路为死代码，`_current_node_ids` 永不填充 | test_legacy_batch_signal_payload 锁载荷契约 |
| BUG-B2-02 | 中（预设损坏） | 预设快照存 combo 显示文案而非原始选项值；int 漂移为 float | TestPresetSnapshotRegression（1 xfail） |

（未修改任何 src 文件。）


---

<!-- 来源：BUGS-agent-B3.md -->

# BUGS-agent-B3 — src/app/ui/preferences_dialog.py 补测发现的疑似 bug

- 日期：2026-09-28（补测 agent B3，计划02 测试覆盖补齐）
- 模块：`src/app/ui/preferences_dialog.py`（2,685 行，覆盖 7% → 补测后 90%）
- 新增测试：`tests/app/test_preferences_dialog_coverage.py`（57 用例，全绿）
- 规则：以下问题均 **未修改 src**；其中 01–04 已有"锁定现状"用例，修复时需同步更新对应断言。

---

## BUG-B3-01（Critical）Apply 会清空 file_explorer 的停用插件列表

- 位置：`PreferencesDialog._apply_settings()`（约 2033–2050 行）+ `_setup_ui()`
- 现象：`_plugin_list` 只在用户点击 Advanced > "Refresh Plugins"（`_refresh_plugins`）后才填充；对话框构造时为空。`_apply_settings` 无条件遍历 `_plugin_list` 收集停用项并写回 `file_explorer` 自己的 config.json。因此用户只要打开偏好设置并按 Apply/Save（无需进 Advanced 页），FE 的 `disabled_plugins` 就会被整表清空为 `[]`——之前在 FE 里停用的插件全部被重新启用。
- 加重因素：
  1. 写的是 FE 侧 config（`fe_main.save_config`），绕过宿主 ConfigManager，Cancel-after-Apply 的快照回滚（`_on_cancel`）**无法撤销**该副作用；
  2. `external/file_explorer` 在仓库内可导入，现有测试 `tests/ui/test_developer_mode_prefs.py::test_toggling_developer_mode_requires_restart` 等直接调用 `_apply_settings`/`_on_save`，会在开发者本机真实触发该写回（无 FE 桥隔离）。
- 复现：见 `test_apply_without_refresh_clears_fe_disabled_plugins`（锁定现状：断言 Apply 后 FE config 的 `disabled_plugins == []`）。
- 建议：`_apply_settings` 仅在插件列表非空（即已 Refresh、UI 有真实数据）时才写回 FE config；或构造时静默执行一次 `_refresh_plugins`。

## BUG-B3-02（Minor）Restore Defaults 漏掉 smart 阈值；DEFAULT_CONFIG 缺键

- 位置：`_on_restore_defaults()`（约 2205–2211 行）+ `src/shared/config/config_manager.py::DEFAULT_CONFIG`
- 现象：`render_cache_auto_threshold` 在 `_load_current_settings`（默认 6）与 `_apply_settings`（约 1984 行）均有处理，但：
  1. `DEFAULT_CONFIG` 没有该键（load 只能靠硬编码回退 6）；
  2. `_on_restore_defaults` 的 Render Cache 段没有重置它，恢复默认后 smart 阈值保持用户值。
- 复现：`test_restore_defaults_skips_smart_threshold_lock_in`（锁定现状：恢复默认后阈值仍为 21）。
- 建议：DEFAULT_CONFIG 增加 `"render_cache_auto_threshold": 6`，并在 `_on_restore_defaults` 补一行重置。

## BUG-B3-03（Minor）Timeline 分辨率预设下拉与配置不同步

- 位置：`_load_current_settings()`（约 1860–1879 行，Timeline Defaults 段只回填 width/height spinbox）
- 现象：预设 combo（`_timeline_res_combo`）加载后恒停在第 0 项 "4K UHD"，即使配置是 1080p/720p；用户一旦触碰下拉即触发 `_on_timeline_res_preset_changed` 把宽高覆盖为预设值，与打开对话框时的显示不符（Apply 本身读 spinbox，不触碰下拉则数据无损）。
- 复现：`test_timeline_resolution_preset_combo_not_synced_with_config`（锁定现状：1080p 配置 + combo 显示 4K）。
- 建议：load 时按 width/height 反查匹配的预设索引；无匹配则选 "Custom"。

## BUG-B3-04（Minor）Restore Defaults 漏掉 Pass Fusion Mode

- 位置：`_on_restore_defaults()` Performance 段（约 2164–2189 行）
- 现象：`pass_fusion_mode` 有 load（默认 conservative）与 apply，但恢复默认时未被重置，用户设为 "off"/"aggressive" 后 Restore Defaults 不生效。
- 复现：`test_restore_defaults_skips_pass_fusion_mode_lock_in`（锁定现状：恢复默认后仍为 "off"）。
- 建议：在恢复 Performance 段时补 `pass_fusion_mode` 重置。

---

## 其他观察（未写锁定用例，修复时一并考虑）

- 非法枚举回退到"下拉第一项"而非 DEFAULT_CONFIG 默认值（`_select_combo_by_data` 与各 load 循环的通用行为）：如 `default_timeline_fps` 非法时落到 "23.976"（默认是 "25"）、`log_level` 非法时落到 "debug"（默认 "warning"）、`video_playback_resolution` 非法时落到 3840（默认 1920）。行为一致且不会崩，但与"默认值"语义有偏差（见 `test_unknown_enum_values_fall_back_to_first_choice`）。
- `PreferencePageDescriptor` 文档声称 factory "Must be lazy — only called when user opens the page"，但 `_setup_ui` 对全部页面前置调用 factory；仅注释/实现不一致，无功能影响。


---

<!-- 来源：BUGS-agent-B4.md -->

# BUGS-agent-B4：dock_layout_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/dock_layout_controller.py`
- 来源：Plan-02 补测（`tests/app/test_dock_layout_controller_coverage.py`，65 用例）
- 处置原则：**只报告，不修改 src**。三个问题的"当前行为"均已用锁定断言写进测试
  （见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度定义：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）>
low（健壮性缺口）。

---

## BUG-B4-01（high）：5 个 Edit dock 未注册进 all_docks，浮动后不可拖拽、尺寸不持久

- 位置：`DockLayoutMixin.setup_docks` 的 `self.all_docks = [...]` 构造块及其后的
  append 序列（dock_layout_controller.py ~L967-1020）。
- 现象：以下 5 个 dock 通过 `create_custom_dock` 创建，但从未进入 `all_docks`：

  | 属性 | objectName |
  |------|-----------|
  | `dock_properties` | dockProperties |
  | `dock_edit_effects` | dockEditEffects |
  | `dock_caption_workspace` | dockCaptionWorkspace |
  | `dock_adr_take_sheet` | dockAdrTakeSheet |
  | `dock_multicam` | dockMulticam |

  注册集合 = 基础列表（26 个）+ 条件 append（edit_media_pool / program_viewer /
  source_viewer / edit_timeline / edit_info / ai_edit_assistant / composite 家族 /
  deliver）。源码里正好缺上述 5 个。
- 后果：源码 L1001-1003 的注释已经说明过这类缺口对 Keyframes/Spline 的教训——不在
  `all_docks` 里的 dock：
  1. `_perform_update_all_titlebars` / `_sync_floating_docks` 跳过它们 → 浮动后
     顶部只剩创建时安装的 1px 隐形标题栏，**无法拖动浮动窗口、也无法拖回停靠**
     （与 Keyframes/Spline 当年的 bug 同机制，tests/ui/
     test_composite_dock_floating_titlebar.py 是那次修复的回归测试）；
  2. `_save_all_docked_sizes` / `_restore_docked_sizes` 跳过它们 → 停靠尺寸
     不持久化，布局恢复后 splitter 比例丢失。
  受影响的是 Edit 页核心面板（Properties / Effects / Captions / ADR / Multicam），
  用户把它们浮动后就会踩中。
- 锁定用例：
  `test_created_docks_missing_from_all_docks_are_exactly_the_known_five`
  （源码级断言：`created - registered == {上述 5 个}`，注释指明修复后应收紧为
  `== set()`）；行为侧另见
  `test_registered_docks_get_titlebar_maintenance`（注册成员获得标题栏维护）。
- 建议修复方向：在 setup_docks 的 append 段为 5 个属性补 `all_docks.append(...)`
  （注意它们都是条件创建，需沿用 `if self.dock_xxx:` 判空），然后把锁定用例断言
  翻转为 `missing == set()`。

## BUG-B4-02（medium）：_enforce_page_dock_visibility 漏掉 dock_caption_workspace

- 位置：`DockLayoutMixin._enforce_page_dock_visibility` 的 `edit_only` 列表
  （dock_layout_controller.py ~L2910-2913）。
- 现象：`edit_only` 收录了 program_viewer / source_viewer / edit_timeline /
  edit_info / edit_effects / ai_edit_assistant / adr_take_sheet / multicam，唯独没有
  `dock_caption_workspace`；该方法其余分支也从未引用它。
- 后果：Captions 是 Edit 专属面板（setup_docks 中与 Properties 同栈 tab 化，且
  `initialize_page_layouts` 的"保存布局前置隐藏列表"（~L1111-1114、~L1155-1158）
  都包含它）。但页面可见性强制这一最终防线不覆盖它：一份把 Captions 泄漏到
  Color/Composite 页的旧布局，恢复后 Captions 会留在错误页面；ADR / Multicam 会被
  强制隐藏而 Captions 不会，行为不一致。
- 锁定用例：`test_enforce_caption_workspace_is_not_page_hidden_bug_pin`
  （Color 页 enforce 后 ADR/Multicam 隐藏、Captions 仍可见，注释指向本条）。
- 建议修复方向：把 `self.dock_caption_workspace` 加进 `edit_only` 列表，然后把
  锁定用例断言翻转为 `_hidden(...) is True`。

## BUG-B4-03（medium）：initialize_page_layouts 的页 dock 隐藏清单漏掉 dock_deliver
与 dock_node_properties（靠后续分支的 hide-all 兜底才不暴露）

- 位置：`DockLayoutMixin.initialize_page_layouts` 硬编码分支的收尾隐藏段
  （dock_layout_controller.py ~L1434-1452；对比 ~L1111-1136 的前置隐藏段）。
- 现象：收尾段逐项隐藏 edit/composite 家族 dock，但清单不含
  `dock_deliver` 和 `dock_node_properties`：
  - `dock_node_properties` 在 Composite 分支被 `setVisible(True)`（~L1353）后，
    收尾段从不重新隐藏——实测 initialize 结束后它在非 Composite 页保持可见
    （锁定用例第一段）。
  - `dock_deliver` 在 Deliver 分支被 `setVisible(True)`（~L1331）后，只有当
    Composite 分支随后执行其 `for d in self.all_docks: setVisible(False)` 时才被
    **顺带**隐藏；Composite 不可用的配置下（如相关 feature 关闭/图像模式变体）
    它在非 Deliver 页保持可见（锁定用例第二段用
    `_DockHost(composite_available=False)` 复现）。
- 后果：不变量"页面专属 dock 只在本页可见"在这两条路径上靠巧合维持而非靠设计；
  `_enforce_page_dock_visibility` 本身倒是有 dock_node_properties（~L2934），
  但 `_enforce` 不含 dock_deliver（见下），initialize 与 enforce 两套清单都不完整。
- 附带发现（同族，未单列）：`_enforce_page_dock_visibility` 完全没有
  `dock_deliver` 分支——非 Deliver 页上它同样不会被强制隐藏（锁定用例
  `test_enforce_deliver_dock_is_not_page_hidden_bug_pin`）。修复时两处应一并补齐。
- 锁定用例：`test_initialize_falls_back_to_hardcoded_defaults`（两段 BUG-B4-03
  pin：node_properties 泄漏 + 无 Composite 分支时 deliver 泄漏）、
  `test_enforce_deliver_dock_is_not_page_hidden_bug_pin`。
- 建议修复方向：initialize 收尾隐藏清单补 `dock_deliver` / `dock_node_properties`
  （以及前置两段清单对齐）；`_enforce_page_dock_visibility` 补
  `if self.current_page != "Deliver": hide dock_deliver`；然后把上述锁定断言翻转。

---

## 补测过程记录（非 src 缺陷，供后续测试参考）

- Qt 6.10.2 offscreen 下，`apply_default_layout` / `initialize_page_layouts` 这类
  长 `tabifyDockWidget` 链在**已 show 的** QMainWindow 上有约 50-70% 概率段错误
  （生产路径总在 show 之前执行，故线上不受影响；`tests/edit/test_workspace_presets.py`
  亦采用不 show 宿主）。本文件提供 `layout_host`（不 show）fixture 供这些路径
  使用，`host`（show）fixture 用于需要 `isVisible()` 语义的用例。
- 交付物：`tests/app/test_dock_layout_controller_coverage.py`，65 个用例全绿；
  单文件即覆盖 `dock_layout_controller.py` 可执行行 55%（基线 21%；未覆盖主体是
  `setup_docks` 的完整 MainWindow 装配，需真实面板栈，不属于 headless 可测单元）。


---

<!-- 来源：BUGS-agent-C1.md -->

# BUGS-agent-C1 — scene3d_node.py 补测过程发现的疑似 bug

模块：`src/features/composite/nodes/scene3d_node.py`（覆盖率补齐，agent-C1）
日期：2026-09-28
说明：以下问题均为阅读/补测时发现，**未修改任何 src 代码**。编号 BUG-C1-xx。

---

## BUG-C1-01 `_render_id_pass` finally 块引用可能未绑定的 `pick_map`（NameError 掩盖原始异常）

- 位置：`scene3d_node.py:2532-2641`（`_render_id_pass`）
- 现象：`pick_map, node_pick_ids = self._build_pick_id_map(...)` 在 `try:` 块内（约 :2558），
  但回传 `self._p7_id_pick_info = {...}` 的赋值位于 `finally:` 块内（:2632，且引用 `pick_map`）。
  若 try 体在赋值前抛异常（如 `prog['u_proj']` KeyError / uniform 缺失），finally 会先抛
  `NameError: pick_map`，掩盖原始异常根因；`_do_evaluate` §6b2 的 `except Exception` 只能
  记录到 NameError，排障信息丢失。
- 影响：低概率（uniform 异常时才触发），但一旦触发日志指向错误根因。
- 建议：把 `_p7_id_pick_info` 回传移出 finally（放 try 成功路径末尾），或在 finally 内
  先判断 `pick_map` 是否已定义。

## BUG-C1-02 `_render_shadow_catchers` 未判空 `self._render_fbo` 直接 `.use()`

- 位置：`scene3d_node.py:1723`（`_render_shadow_catchers`）
- 现象：早退守卫只检查 `light_view_proj is None or self._shadow_texture is None`（:1714），
  随后直接 `self._render_fbo.use()`。shadow target 与 render target 生命周期虽在
  `_release_render_target` 中一同释放，但两者分配路径独立（shadow 懒建、render target
  按 size/msaa/aov 重建）；存在 `_shadow_texture` 存活而 `_render_fbo` 为 None 的窗口
  （如 MSAA 降级分支释放后未重建时），届时 AttributeError。
- 影响：防御性缺口，正常求值顺序下不易触发。
- 建议：`_render_fbo is None` 一并纳入早退条件。

## BUG-C1-03 性能诊断字段 `model_cache_hit` 恒为 None（`_perf_model_cache_hit` 从未写入）

- 位置：`scene3d_node.py:4921`（`_do_evaluate` §7）
- 现象：`_pstats.model_cache_hit = getattr(self, "_perf_model_cache_hit", None)`，
  但 `_perf_model_cache_hit` 在 `__init__` 及整个 src/ 中都**没有任何赋值点**
  （grep 全仓仅此一处读取）。渲染诊断里的 `perf_stats.model_cache_hit` 永远是 None，
  与相邻的 `_last_vbo_cache_hit`（在 `_ensure_flat_meshes_gl` 中真实汇总）不一致。
- 影响：诊断面板/日志中模型缓存命中率指标失效（恒空），非崩溃。
- 建议：要么在 Model3D payload 导入缓存路径回写该字段，要么删除该指标避免误导。

## BUG-C1-04 `_sort_meshes_by_material` 对非 dict 的 mesh 条目会 AttributeError

- 位置：`scene3d_node.py:1195`（排序 key `pair[1].get('material')`）
- 现象：同函数内 `_is_transparent` 对非 dict 条目做了 `isinstance(fm, dict)` 防御并视为
  不透明，但排序 key 直接 `.get(...)`。若 `flat_meshes` 混入非 dict 条目（其它辅助路径
  如 `_material_aov_key`、`_build_component_pick_meshes` 都按 dict 约定防御了非 dict），
  `opaque.sort(...)` 抛 AttributeError，整帧渲染失败。
- 影响：当前内部数据均为 dict，属健壮性/一致性缺口。
- 建议：排序 key 与 `_is_transparent` 使用同一防御（`fm.get('material') if isinstance(fm, dict) else None`）。

## BUG-C1-05 Geo3D 的 GPU ID 拾取在纯渲染器架构下画不出任何几何

- 位置：`scene3d_node.py:2609-2628`（`_render_id_pass` 路径 B）与 `:4505` 附近的
  `_do_evaluate` §5（`has_builtin_primitive = False` → `_release_mesh_geometry()`）
- 现象：ID pass 渲染 Geo3D payload 时使用 `self._get_id_write_vao(ctx, None, '__procedural__')`
  （注释自述"Geo3D 的 mesh 数据不在 payload（只有 vao），退化为用 procedural id VAO"）。
  该调用要求 `self._mesh_vbo is not None`；但 Scene3D 现为纯渲染器，`primitive="none"`、
  `has_builtin_primitive=False`，procedural 几何始终被释放（`_mesh_vbo=None`）→
  `_get_id_write_vao` 返回 None → `if id_vao is not None` 恒假 → Geo3D 对象的 object-id
  从不写入 `_id_texture`，viewer GPU 点击拾取对 Geo3D 恒返回背景未命中。
  CPU 组件拾取路径（`_build_component_pick_meshes`）用 payload.mesh 是正常的，仅 GPU
  ID pass 失效。
- 影响：P7 GPU 点选拾取对 Geo3D 节点功能性失效（medium）。
- 建议：为 Geo3D payload 的 ID pass 构建独立 VAO（可复用 `_get_id_write_vao(ctx, gp.mesh, ...)`
  的 mesh 分支），或复用 `_build_component_pick_meshes` 已有的 payload mesh 数据。

---

## 汇总

| 编号 | 严重度 | 一句话 |
|------|--------|--------|
| BUG-C1-01 | Low | ID pass finally 块 NameError 风险，掩盖原始异常 |
| BUG-C1-02 | Low | shadow catcher 未判空 `_render_fbo` |
| BUG-C1-03 | Low | `model_cache_hit` 诊断恒 None（字段从未写入） |
| BUG-C1-04 | Low | 材质排序对非 dict mesh 条目崩溃（防御不一致） |
| BUG-C1-05 | Medium | Geo3D 的 GPU ID 拾取画不出几何（procedural VAO 恒 None） |


---

<!-- 来源：BUGS-agent-C2.md -->

# BUGS-agent-C2 — video_playback.py 补测过程发现的疑似 bug

模块：`src/shared/video/video_playback.py`（覆盖率补齐，agent-C2）
日期：2026-09-28
说明：以下问题均为阅读/补测时发现，**未修改任何 src 代码**。编号 BUG-C2-xx。
验证方式：headless 脚本 + 新增测试 `tests/video/test_video_playback_coverage.py`
（BUG-C2-01 / BUG-C2-03 以 characterization test 固定当前行为；BUG-C2-02 的
死锁无法安全地在测试中复现，用独立脚本验证，测试侧以 QRecursiveMutex
workaround 绕开后覆盖队列逻辑本身）。

---

## BUG-C2-01 16-bit BGR 帧转换把白色变成黑色（除以 (1<<bit_depth) 后直接截断）

- 位置：`video_playback.py:3499`（`EnhancedVideoPlaybackEngine._process_decoded_frame`，
  `pixel_format in ('bgr24', 'bgra', 'bgra64')` 的 `data.dtype == np.uint16` 分支）
- 现象：
  ```python
  return (data[..., :3] / (1 << frame.bit_depth)).astype(np.uint8)
  ```
  除以 65536 后结果落在 `[0, 1)`，`astype(np.uint8)` 截断为 0——**任何 16-bit BGR
  帧都会变成全黑**。实测：`np.full((1,1,3), 65535, np.uint16)` → `0`。
  对照同文件 `ProxyPrefetchWorker._process_frame`（:667）的正确写法是
  `(data / 65535.0 * 255.0).astype(np.uint8)` → `255`。
- 触发条件：首帧/HQ 显示路径（`_show_first_frame_via_coordinator` →
  `_process_coordinator_frame` → `_process_decoded_frame`）遇到 16-bit BGR/BGRA
  packed 帧（如 bgra64 / bgr48 高位深源），画面全黑但不报错。
- 影响：高位深 BGR 素材首帧黑屏，易被误判为解码失败。
- 建议：改为 `(data[..., :3] / 65535.0 * 255.0).astype(np.uint8)`（与 worker 路径对齐）。
- 测试：`TestDecodedFrameProcessing::test_bgr16bit_truncates_to_black_bug_c2_01`
  固定当前（错误）行为。

## BUG-C2-02 ProxyPrefetchWorker 非递归 QMutex 嵌套加锁 → 必然死锁

- 位置：`video_playback.py:489-540`（`get_ready_frame`）与 `:612-628`（`_add_frame`）
  / `:630-637`（`_remove_frame`）
- 现象：`_ready_frames_mutex = QMutex()` 默认 **NonRecursive**，但存在两条嵌套加锁路径：
  1. `get_ready_frame` 在 `self._ready_frames_mutex.lock()` 的 try 块内调用
     `self._remove_frame(best_match[0])`（:534），而 `_remove_frame` 第一行再次
     `self._ready_frames_mutex.lock()`（:632）→ **只要取到帧就死锁**；
  2. `_add_frame` 队列满时在持锁状态下调用 `self._remove_frame(min_idx)`（:624）
     → **队列溢出丢帧时死锁**。
- 验证：独立脚本（daemon 线程 + 3s join 超时）确认两条路径均挂死；将
  `_ready_frames_mutex` 换成 `QRecursiveMutex` 后一切正常。
- 触发条件：legacy v2 播放路径（`_on_play_tick` 旧架构分支调用
  `get_ready_frame`；预读线程 run() 满队调用 `_add_frame`）。当前主路径为 v3
  PlaybackCoordinator（不使用该 worker），故线上未被普遍触发；但
  `_start_proxy_prefetch_worker` / 旧架构回退一旦激活即冻结 UI/预读线程。
- 影响：线程永久挂死（无异常、无日志），极难排障。
- 建议：`_remove_frame` 提供不加锁的内部版本（如 `_remove_frame_locked`），
  持锁调用方直接使用；或将 QMutex 换成 QRecursiveMutex（最小修复）。
- 测试：`tests/video/test_video_playback_coverage.py` 的 `_make_worker` 用
  QRecursiveMutex 替换后覆盖全部队列语义（文件内有注释指回本条）。

## BUG-C2-03 基类 VideoPlaybackEngine 引用子类属性 `_playback_clock` / `_proxy_prefetch_worker`，从未初始化

- 位置：
  - `seek_to_time`（:2254）、`get_current_time`（:2331）、`get_time_string`
    （:2345-2346）、`last_frame`（:2278）、`set_playback_speed`（:2291）直接读
    `self._playback_clock`；
  - `_check_proxy_ladder`（:2228）读 `self._proxy_prefetch_worker`。
  两者只在 `EnhancedVideoPlaybackEngine.__init__` 中赋值，基类 `__init__` 没有。
- 现象：纯基类实例调用以上方法抛
  `AttributeError: 'VideoPlaybackEngine' object has no attribute '_playback_clock'`
  （已实测复现）。`_check_proxy_ladder` 在 `_proxy_size` 已设置且性能监控触发
  降级时才会踩到 `_proxy_prefetch_worker`（同样 AttributeError）。
- 影响：基类被单独实例化/被新子类复用时的潜在崩溃；也说明这些方法实际上
  只对 Enhanced 子类有效，基类接口契约失真。
- 建议：基类 `__init__` 中初始化 `self._playback_clock = None`、
  `self._proxy_prefetch_worker = None`。
- 测试：`TestBaseEngineStubAndInfo::test_base_time_helpers_require_subclass_clock_bug_c2_03`
  固定当前行为；`test_last_frame_with_unknown_total_holds_current` 显式置空后
  覆盖 fallback 分支。

## BUG-C2-04（低）首帧三级处理的中间状态 FRAME_PATH_DEGRADED / TIMEOUT_PROXY 永远不可观测

- 位置：`video_playback.py:3148`、`:3206`、`:3223`（`_show_first_frame_via_coordinator`）
- 现象：Level-1/2 中间状态（`FRAME_PATH_DEGRADED`、`TIMEOUT_MASTER`、
  `TIMEOUT_PROXY`）一旦走到 Level-3，会被无条件覆盖为 `PLACEHOLDER`
  （`should_fallback` 恒为 False），而所有成功路径都提前 return。
  因此调用方（`load_video`）永远看不到这些中间状态；
  `FirstFrameResult.is_displayable` 对 `FRAME_PATH_DEGRADED` 的分支经此路径不可达。
- 影响：仅可观测性/诊断价值损失，无功能错误。
- 建议：Level-3 保留进入原因（如在 result 上附 `degraded_from` 字段），或删除
  不可达状态枚举避免误导。


---

<!-- 来源：BUGS-agent-C3.md -->

# BUGS-agent-C3：page_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/page_controller.py`
- 来源：Plan-02 补测（`tests/app/test_page_controller_coverage.py`，139 用例）
- 处置原则：**只报告，不修改 src**。问题的"当前行为"已用锁定断言写进测试
  （见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度定义：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）>
low（健壮性缺口）。

---

## BUG-C3-01（medium）：时间线传输条槽闭包在每次进入 Composite 页时重复连接累积

- 位置：`PageManagementMixin._configure_composite_timeline_playback_controls`
  （page_controller.py ~L1400-1424 的 `control_connections` 连接循环）。
- 现象：每次进入 Composite 页都会重跑该函数，为 viewer 传输条（VideoControlBar）
  的 13 个信号（seek/scrub×3/play/next/prev/first/last/mark_in/mark_out/clear_io/
  loop）**新建一批闭包槽**并连接。函数顶部的拆卸逻辑只处理
  `frame_changed` / `state_changed` / `_kf_transport_state_slot`（有
  `_composite_controls_*` 属性记录的槽）；对 13 个 bar 信号的拆卸循环是：

  ```python
  for signal_name, slot in control_connections:
      signal = getattr(bar, signal_name, None)
      ...
      while True:
          try:
              signal.disconnect(slot)
          except TypeError:
              break
      signal.connect(slot)
  ```

  这里的 `slot` 是**本次调用新创建**的闭包，从未连接过，`disconnect` 立即抛
  TypeError 而跳出循环——上一次（上一次进入页面时）连接的旧闭包从未被拆除。
- 后果：第 N 次进入 Composite 后，每个 bar 信号上挂着 N 个闭包；旧的闭包捕获
  旧的 `controller` / 旧 `clip_start` / `clip_total`。用户点一次播放/seek 会触发
  N 次槽调用：轻则重复 seek/重复 set_playback_range（表现为主观卡顿、日志刷屏），
  重则旧闭包把播放头拽回旧 clip 的坐标系（时间线重排或 comp clip 范围变化后）。
  对比同文件 `_configure_composite_comp_clip_controls`（~L1583-1599）：那条路径把
  `slots` 存进 `self._comp_clip_bar_slots` 并在下一次调用时先 disconnect 旧槽，
  是正确写法——本条疑似是遗漏了对称的记录/拆卸。
- 复现：见锁定用例。
- 锁定用例：`tests/app/test_page_controller_coverage.py::
  test_timeline_controls_bar_slots_accumulate_across_rewires` —— 当前断言
  `len(bar.play_toggled.slots) == 2`（钉住现状）。修复后应改为 `== 1`，并且
  断言旧槽被移除（frame/state 槽的对称行为可参考同文件
  `test_timeline_controls_rewire_disconnects_previous_frame_state_slots`，
  那两个信号目前是正确去重的）。
- 修复建议：仿照 comp-clip 路径，把每次的 `control_connections` 列表存入
  `self._composite_timeline_bar_slots`，函数开头对旧列表逐个 disconnect。

---

## BUG-C3-02（low）：`_configure_composite_timeline_playback_controls` 对
返回 None 的 `comp_clip_total_frames()` 缺少兜底（防御不一致）

- 位置：page_controller.py ~L1249-1253：

  ```python
  total_frames = getattr(page, "comp_clip_total_frames", None)
  if callable(total_frames):
      clip_total = max(1, int(total_frames()))
  else:
      clip_total = max(1, int(getattr(controller, "timeline_duration", 1) or 1))
  ```

- 现象：属性存在且可调用时直接 `int(total_frames())`，没有 `or 1` 兜底；
  一旦返回 `None` 就抛 `TypeError`，直接打断整个
  `_configure_composite_timeline_playback_controls`，传输条不显示、
  Keyframes transport 也不装配。
- 影响评估：**当前真实 `CompositePage` 不会触发**——`comp_clip_total_frames`
  （composite_page.py L4586，`-> int`）委托 `_total_frames_for_node()`，
  其各分支都 `max(1, ...)` 永不返回 None。该缺口只对轻量页面替身/旧版
  CompositePage 实现（`getattr(self, "comp_clip_total_frames", None)` 的
  duck-typing 探测本身就是在容忍这类对象）构成崩溃风险。
- 对照：else 分支（读 `timeline_duration`）有 `or 1` 兜底，说明作者本意是
  两条路径都该防 0/None，callable 分支漏了。
- 锁定用例：无（补测里 `test_timeline_controls_seed_bar_from_controller_duration`
  用"页面没有 `comp_clip_total_frames` 属性"的替身覆盖 else 分支；callable
  返回 None 的崩溃路径就是缺陷本身，未锁定）。修复后可补一条
  `comp_clip_total_frames = lambda: None` → clip_total 回退 timeline_duration
  的用例。
- 修复建议：`clip_total = max(1, int(total_frames() or 1))`，或对
  `total_frames()` 结果做 try/except 回退到 `timeline_duration`。

---

## 备注（非缺陷，仅记录）

1. `switch_from_edit_to_color(clip_id: str = None)` / `switch_from_edit_to_composite(
   clip_id: str = None, ...)` / `switch_to_deliver(source: str = "color",
   timeline_id: str = None)` 的默认值应为 `Optional[...]`（PEP 484 风格问题，
   运行时无害）。
2. `_show_color_timeline_clip_immediate_thumbnail_preview` 在缩略图路径失败时
   也会递增 `_color_static_preview_token`（作废旧请求），这是有意为之的
   语义（token 即"最新请求 wins"），不是缺陷。


---

<!-- 来源：BUGS-agent-C4.md -->

# BUGS-agent-C4：color_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/color_controller.py`
- 来源：Plan-02 补测（`tests/app/test_color_controller_coverage.py`，111 用例）
- 处置原则：**只报告，不修改 src**。以下问题的"当前行为"均已用锁定断言
  （bug pin）写进测试（见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度定义：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）
> low（健壮性缺口）。

---

## BUG-C4-01（high）：遮罩增/删/清空/建组/层级拖拽全部绕过 undo 栈

- 位置：`on_mask_shape_added` / `on_mask_shape_removed` / `on_all_masks_cleared` /
  `on_mask_group_added` / `on_mask_hierarchy_updated` / `on_brush_stroke_added`
  （color_controller.py L35-92、L183-225、L968-1114、L1238-1287）。
- 现象：这些方法直接改动 `layer.mask_shapes` 数据树并置 dirty、重渲染，但从不调用
  `self.engine.history.push_state(...)`。对照同文件的
  `on_duplicate_mask_shape`（L118-120）和 `on_copy_masks_to_layer_requested`
  （L165-167）——窗口复制类操作在改动前会以 `copy_image=False` 提交快照，
  说明遮罩结构的"可撤销"语义在代码库中是存在的，只是上述 6 个入口没有接上。
- 后果（用户可见）：
  1. 误删一个精心调好的 Power Window 后 **Ctrl+Z 无法恢复**（撤销栈里没有
     删除前快照，回退的是上一次调色参数）；
  2. 误点 "Clear All Masks" 清空整页窗口后同样不可撤销；
  3. 树形面板拖拽改层级（`on_mask_hierarchy_updated`）不可撤销，配合
     BUG-C4-03（结构缺失 id 被静默丢弃）会造成不可逆的数据丢失。
- 锁定用例：`test_mask_add_remove_clear_group_never_push_undo_state_bug_pin`
  ——把 add → remove → clear → group → hierarchy 五类操作跑一遍后断言
  `engine.history.pushed == []`，并对照 duplicate 操作会 push（`copy_image=False`）。
  注释写明修复后应改为逐操作断言 `pushed` 非空。
- 建议修复方向：与 `on_duplicate_mask_shape` 一致，在各结构改动入口先
  `push_state(img_full_original, layers, active_layer_index, copy_image=False)`
  再改数据树；注意 `process_mask_update`（参数微调）刻意不入栈是对的，
  不要顺手加上。

## BUG-C4-02（high，审查 Critical 项）：根级 MaskGroup 使三条 Brush 流程 AttributeError 崩溃

- 位置：`on_brush_toggled` L1174（`if s.type == "brush"`）、
  `on_brush_params_changed` L1204、`on_brush_stroke_added` L1246——三处都只遍历
  根级 `layer.mask_shapes` 并直接读 `s.type`。
- 现象：`MaskGroup`（src/features/color/core/layer.py L242-273）**没有 `type`
  实例属性**（只有类常量 `TYPE = "group"`，序列化时才写入 dict）。根级列表自
  P1-COLOR-09 起可包含群组，因此任何"先把若干窗口打包成组、再开 Brush 工具"的
  操作序列都会 `AttributeError: 'MaskGroup' object has no attribute 'type'`。
- 崩溃路径（已锁定）：
  1. 选中 2 个窗口 → Masks 面板 "Group"（或拖拽建组）→ 根级出现 MaskGroup；
  2. 点 Brush 按钮 → `on_brush_toggled(True)` 遍历根级 → 读 `group.type` → 崩溃；
  3. 即使先有 Brush 形状再建组，之后画笔参数变化（`on_brush_params_changed`）
     或落笔（`on_brush_stroke_added`）同样崩溃。
  另注：三处的遍历还只查根级、不递归群组——即使 MaskGroup 补上 `type`，
  组内的 brush 形状也永远找不到（现有 brush 形状会被重复创建）。
- 对照（安全写法已在同文件）：`_iter_trackable_shapes`（L263-281）用
  `isinstance(item, MaskGroup)` 递归展开 + `getattr(shape, "type", "")` 兜底，
  是群组安全的遍历范式。
- 锁定用例：`test_brush_flows_crash_on_root_level_mask_group_bug_pin`
  （参数化 3 条流程，`pytest.raises(AttributeError)` 锁定当前崩溃）；安全路径由
  `test_iter_trackable_shapes_is_group_and_type_safe`（getattr 兜底 + 递归展开）
  与 `test_brush_toggled_on_creates_brush_shape_when_missing` 等正向用例覆盖。
- 建议修复方向：三处遍历改用 `_iter_trackable_shapes` 风格的递归展开（brush
  也是叶节点类型），或最小修复：遍历时 `isinstance(s, MaskGroup)` 跳过 /
  `getattr(s, "type", None)`。修复后把 3 个 pin 用例翻转为行为断言
  （群组在场时 brush 开关/参数/落笔正常）。

## BUG-C4-03（medium）：on_mask_hierarchy_updated 丢弃结构中缺失的遮罩（不可逆数据丢失）

- 位置：`on_mask_hierarchy_updated` L1084-1103（`build_from_struct`）。
- 现象：重建数据树时只接收 `structure` 里出现的 id；原树中存在但新结构缺失的
  遮罩被静默丢掉（不报错、不进 undo——叠加 BUG-C4-01）。
- 后果：Qt 树控件如果发出一份不完整的拖拽结构（或上游面板 bug），用户会直接
  丢失窗口且无提示无撤销。保守做法应是把缺失项追加到根级（或至少校验
  结构完整性后拒绝应用）。
- 锁定用例：`test_mask_hierarchy_updated_rebuilds_tree_and_drops_unknown_ids_bug_pin`
  ——传入缺一项的结构，断言被丢的 id 在 `get_mask_item_by_id` 中已查不到。
- 建议修复方向：`build_from_struct` 完成后，把 `all_items` 中未被消费的项
  append 回根级（保持顺序），或完整性校验失败时整体回滚。

## BUG-C4-04（medium）：_tracker_frame_context 的 total_frames 回退是死代码，无时间线剪辑时跟踪范围塌缩为 [0,0]

- 位置：`_tracker_frame_context` L606-611。
- 现象：

  ```python
  frame_start, frame_end = 0, 0
  ...clip 解析（无剪辑时 frame_start/frame_end 保持 0/0）...
  if frame_end < frame_start:          # 0 < 0 恒为 False
      total_frames = ...video_controller.total_frames
      frame_end = max(frame_start, int(total_frames) - 1)
  ```

  `frame_end < frame_start` 在无时间线剪辑时永远为 False（双双默认 0），因此
  `total_frames` 回退分支（L607-611）**不可达**（本次覆盖率运行显示这 5 行
  0 命中，与推断一致）。
- 后果：Color 会话有 clip_id 但 `_current_timeline_sequences` 解析不到剪辑时
  （如会话恢复早于时间线加载），`_ensure_tracker_binding` 把
  `source_frame_start/end` 写成 0/0、参考帧被钳到 0：
  1. Tracker 面板 "单帧/首帧/末帧" 导航全部失真（L606 之后的
     `reference = max(frame_start, min(reference, frame_end))` 把任何参考帧钳为 0）；
  2. 跟踪任务范围只覆盖第 0 帧，表现为"跟踪跑一下就完成、无结果"。
- 锁定用例：`test_tracker_frame_context_resolves_timeline_and_total_frames`
  ——无剪辑 + `total_frames=11` 时断言当前返回 `("c1", "/m.mov", 0, 0, 0)`
  （参考帧被钳到 0），注释写明修复后应为 `(..., 3, 0, 10)`。
- 建议修复方向：把守卫改成"剪辑缺失时"而不是"frame_end < frame_start"，
  例如 `if clip is None:` 时用 `total_frames` 推导 `frame_end`；或初始
  `frame_end = -1` 使现有比较生效。修复后翻转上述 pin 断言，并同步检查
  `tests/app/test_color_controller_coverage.py` 中
  `test_start_tracker_*` / `test_ensure_tracker_binding_creates_and_updates`
  使用的 time-line fixture（它们当前靠剪辑提供真实帧范围来绕开此缺陷）。

---

### 附：非缺陷观察（不单独立 BUG）

- `on_crop_applied(panel_params=None)` 的 `panel_params` 参数完全未使用
  （几何一律取 `canvas.get_crop_data()`）；是死参数/坏味道，可在下次触碰时清理。
- `on_mask_group_added` 在传入的全是失效 id 时仍会创建一个空群组并提示
  "Packed 0 items"；行为可用但略反直觉，测试
  `test_mask_add_remove_clear_group_never_push_undo_state_bug_pin` 顺带覆盖了
  该分支。


---

<!-- 来源：wd10-g42（批 D：GAP-013/GAP-030 补测） -->

# 测试覆盖补齐 — wd10-g42 疑似 Bug 报告

模块：`src/features/composite/ui/node_editor_widget.py`
日期：2026-09-30
来源：补测过程（tests/composite/test_fix_wd10g42_aud.py 编写与验证）
约定：只记录，不修改 src。

---

## BUG-WD10G42-01

- **编号**：BUG-WD10G42-01
- **位置**：`src/features/composite/ui/node_editor_widget.py`
  `_get_upstream_nodes()`（约 line 3869-3885）与 `_get_downstream_nodes()`
  （约 line 3887-3905，同构）
- **描述**：递归遍历永远是死代码。调用方在递归前先执行
  `visited.add(src_id)`，而被调用方入口守卫是
  `if node_id in visited: return visited`——递归调用必然命中该守卫直接
  返回，从不继续展开下一跳。链路 src→blur→out 上
  `_get_upstream_nodes("out")` 返回 `{'blur'}`（仅一跳直接输入），
  transitive 上游 `src` 永远不会被收集；下游同构。
- **证据**：
  1. 手工 trace（3 节点链，`_get_upstream_nodes` 包装打印）：内层递归
     `_get_upstream_nodes('blur', visited={'out','blur'})` 立即返回，
     最终结果 `{'blur'}`，而 `get_input_connections('blur')` 明明含有
     src 连接。
  2. 受影响用户路径：`NodeEditorWidget._select_upstream_nodes()` /
     `_select_downstream_nodes()`（菜单/快捷键"选择上游/下游"）与
     `NodeEditorActions.select_upstream()/select_downstream()`——
     "select upstream" 在 3 节点链上只选中直接 feeder，跨两跳的
     依赖（如 clip→transform→output 中从 output 选 upstream 只到
     transform）漏选。
- **影响**：Major（功能性）：上游/下游选择只工作一跳；与命名承诺
  （"Select all upstream connected nodes"）不符。无崩溃、无数据损坏。
- **置信度**：高（确定性 trace 复现 + 静态读码）。
- **锁定用例**：`tests/composite/test_fix_wd10g42_aud.py` ::
  `TestGap030SelectionOps::test_select_upstream_downstream` 与
  `TestGap013SelectionDelegates::test_upstream_downstream_delegates`
  ——两处均按当前一跳行为 pin（注释标明 BUG-WD10G42-01），修复后应
  翻转为传递闭包断言（`{"src","blur"}` / `{"blur","out"}`）。
- **修复建议**（未实施）：去掉调用方的预添加（只递归
  `self._get_upstream_nodes(src_id, visited)`，由被调用方
  `visited.add(node_id)` 统一标记），或把入口守卫改为
  `if node_id in visited: return visited` 前不预先 add 邻居。

---

### 附：非缺陷观察（不单独立 BUG）

- `PresetManager.export_preset(name, path)` 对 presets_dir 内的目标路径
  无警告：导出到 presets_dir 内的 `p1.json` 会被后续 `delete_preset("p1")`
  一并删除（同一文件）。补测用例
  `TestGap030Presets::test_export_import_roundtrip` 以导出到外部目录规避，
  行为本身可用但易误伤。
- `SplineView.render_to_painter` 结束后把 `self._viewport` 置 None
  （丢弃而非恢复调用前状态）：紧随其后的交互会先走一次懒重建，行为正确，
  仅多一次重算。
