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
