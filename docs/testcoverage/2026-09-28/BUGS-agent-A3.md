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
