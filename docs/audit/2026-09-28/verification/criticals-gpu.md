# Critical 复核报告 — GPU / 渲染路径（AUD-0600, AUD-1214）

复核日期：2026-09-28 · 复核方式：独立只读验证（Read/grep），不依赖原报告结论。
仓库：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`

---

## [AUD-0600] undo/redo 恢复图像后不重新上传 GPU 纹理 / 不重置蒙版脏标记

- 结论：**核心论断成立，维持 Critical**；但"蒙版脏标记未重置导致蒙版缓存命中旧内容"这一子论断**不成立**（见下）。
- 独立证据：
  1. `_restore_state`（image_engine.py:4194-4225）只恢复 CPU 侧 `img_full_original/img_low/img_high`（4201-4206，直接赋 `FileIO._resize_to_fit` 结果）与图层（4211），随后仅 `render_pipeline()`（4225）。全方法无 `RenderCommand.UPLOAD_IMAGE`、无 `set_reference_image`。
  2. `render_pipeline` 组装的 `render_params`（image_engine.py:2915-2947）不含任何图像数据；`'source_image'` 键仅在 `if is_full_res:` 导出分支写入（2948→2966）。渲染线程 `_do_render_impl` 常规路径取 `source_tex = self.pyramid_textures[lod]`（async_renderer.py:2512），而 `pyramid_textures` 只在 `_do_upload`（async_renderer.py:4166-4231）中填充，`_do_upload` 仅由 UPLOAD_IMAGE 命令触发（async_renderer.py:923-933）。全库 UPLOAD_IMAGE 提交点仅 3 处：离线画布（image_engine.py:574）、`set_image`（789）、RAW 重解码（1993），均在 undo/redo 路径之外。
  3. 调用链上亦无缓解：`engine.undo()` 由 layer_controller.py:1871 触发（live 路径），`_sync_ui_after_undo_redo` → `update_canvas`（file_controller.py:981-986）→ `canvas.set_image`（canvas.py:3787-3826）——后者仅用图像 shape 更新场景矩形/LOD 并再次 `render_pipeline()`（3825），同样不上传纹理。canvas 的 `pixmap_item` 被隐藏（3814），显示完全依赖渲染器输出，即旧纹理。
  4. 影响范围：`history.push_state` 每次都携带 image（history.py:44-50），故所有 undo/redo 都走 `_restore_state` 的图像恢复分支；但**仅在撤销跨越破坏性操作（crop/resize/rotate/flip/apply）时用户可见旧图**——纯调色类 undo 前后 CPU 图像相同，旧纹理内容恰好一致，不可见。
  5. 蒙版子论断反证：`_restore_state` 用 `HistoryManager.deserialize_layers`（history.py:140-155）整体替换 `self.layers`，`Layer.from_dict`（layer.py:996+）经 `__init__` 构造且从不覆写 `mask_dirty`（layer.py 中 mask_dirty 仅出现在 448/531/537/552/559/672/755 行，全部 < 996），而 `__init__` 默认 `self.mask_dirty = True`（layer.py:448）。`render_pipeline` 在组装参数前调用 `_update_mask_textures()`（image_engine.py:2750），该方法按新 `img_full_original` 尺寸重新栅格化并提交 `UPDATE_AUX_TEX`（2643-2689）。**即 undo 后蒙版会被新 Layer 对象的默认脏标记重新上传，蒙版并非旧内容**。
  6. dtype 子论断成立：`FileIO._resize_to_fit` 为保 dtype 的 `cv2.resize`（file_io.py:825-828），uint8 源经 `_restore_state` 得到 uint8 `img_high`，绕过 `set_image` 的 `normalize_to_float32`（image_engine.py:780-781），破坏"img_high 为 float32 0..1"不变量（当前无消费点，属潜伏不一致）。
- 与原报告的出入：
  1. 蒙版部分不成立——原报告称"`_update_mask_textures` 因 mask_dirty 未置位直接跳过"，实际因图层对象整体重建且新对象默认 `mask_dirty=True`，蒙版会刷新；"`_restore_state` 未显式置 mask_dirty"字面为真但无实际后果。
  2. 其余（GPU 主纹理滞留旧内容/旧尺寸、img_high dtype 不变量破坏、set_reference_image 未同步）均与独立证据一致。
- 修复建议确认：原建议成立——`_restore_state` 应按 `set_image` 同口径归一化并提交 UPLOAD_IMAGE（或复用 `set_image(..., is_new_file=False)` 前先写回 `img_full_original`）、同步 reference；但"对所有层置 mask_dirty = True"一步可省（已由图层重建隐式满足），dtype 归一化必须保留。

---

## [AUD-1214] `_do_pixel_probe` 从未在 ctx_b 上验证，探测结果取决于隐含当前上下文

- 结论：**缺陷描述成立，建议降级 Critical → Major**。方法本身确实是无上下文绑定、无线程守卫的假阳性探测；但经复核其休眠程度比原报告所述更深——唯一调用方 `_probe_context_sharing` 本身是**零调用死代码**，当前不存在任何运行时触发路径。
- 独立证据：
  1. gl_context_hub.py:77-105 全文复读：`ctx_b` 仅出现在形参与 docstring，未参与任何 GL 调用；无 make_current / use / 线程断言；`_gl.glBindTexture` / `glGetTexLevelParameteriv`（96-100）与 `finally` 中的 `tex.release()`（105）均作用于调用线程的当前上下文。
  2. 调用方核实：composite_viewport.py:11386-11448 `_probe_context_sharing` 在 `self.makeCurrent()`（11409）后注册 "composite_viewer"（11410）并调用 `probe_sharing("composite_renderer", "composite_viewer")`（11413），探测前未 `doneCurrent()`；`CompositeViewer` 为 QOpenGLWidget（941 行），即 UI 线程形态。
  3. 双重休眠：(a) "composite_renderer" 在全 src 无任何 `register_context` 注册（唯一出现即该 probe 调用本身），故 `probe_sharing` 走 ctx_a is None → False 负缓存（gl_context_hub.py:58-60），`_do_pixel_probe` 不会执行；(b) `_probe_context_sharing` 在全 src **没有任何调用点**（grep 仅命中 11386 行定义），`is_direct_texture_enabled` 亦无调用方；tools/inventory_gpu_backends.py 仅提及模块名。
- 与原报告的出入：
  1. 原报告称"唯一调用方在 composite_viewport.py，当前因 renderer 上下文未注册而暂未触发"——正确但不完整：该唯一调用方自身是从未被调用的死代码，休眠是双层的；这一额外缓解原报告未发现。
  2. "误删 viewer 中同名无关纹理"的情景在真实执行形态下不太可能：若该路径被激活，`ctx_a.texture()` 创建与 `tex.release()` 会在同一线程同一当前（viewer）上下文上执行，名字分配与删除自洽；主导失效模式是**自指探测恒返回 True 的假阳性**（在 viewer 上下文查询刚在 viewer 上下文创建的 2x2 纹理必然宽=2），而非跨上下文误删。
- 修复建议确认：原建议成立——探测应由各上下文拥有线程分段执行（producer 线程建纹理、consumer 线程查询），入口加线程/上下文断言，释放回到创建线程；修复优先级可按 Major 排期，但必须在任何直接纹理呈现集成（注册 composite_renderer / 接通调用方）之前完成。

---

## 复核统计

| 编号 | 原级别 | 复核结论 | 复核后建议级别 |
|------|--------|----------|----------------|
| AUD-0600 | Critical | 核心成立（蒙版子论断不成立） | 维持 Critical |
| AUD-1214 | Critical | 成立（双重休眠，无现行触发路径） | 降级 Major |
