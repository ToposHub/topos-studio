# batch-244 审查报告 — src/features/color/

任务书：docs/audit/2026-09-28/assignments/batch-244.md
编号区间：AUD-24400–AUD-24499
审查文件：1 个（facade.py，实测 254 行）

---

## src/features/color/facade.py (行数: 254)

跨文件核对过的依赖事实（用于定级依据）：
- `engine.color_output` 为 `ColorOutput`（src/features/color/core/image_engine.py:388，类型定义 src/features/color/core/color_output.py），`texture_id`/`is_valid`/`revision` 属性均存在。
- `engine.request_color_output(...)`（image_engine.py:392）与 `engine.render_pipeline(..., render_reason=...)`（image_engine.py:2698）签名与 facade 调用兼容。
- `engine.tracking_store`（image_engine.py:278）为 `TrackingStore`，`all_bindings()` 返回 `TrackingBinding` 列表（src/features/color/tracking/tracking_store.py:62）。
- 消费端：`src/features/composite/ui/composite_page.py:3773`（import 时直接 `payload["mode"] = best.get("mode", ...)`，无词表映射）；tracker 词表 `TRACKER_MODES = ["translation", "trs", "planar"]`（src/features/composite/nodes/tracker_node.py:42）。

### 发现

- [AUD-24400] [Major] 导出跟踪模式未做词表映射，affine/homography 绑定导入 Composite 后 bake 静默丢失 rotation/scale 通道
  - 位置: src/features/color/facade.py:197
  - 证据:
    ```python
    mode = getattr(getattr(binding, "mode", None), "value", "translation")
    ```
    Color 侧 TrackingMode 取值为 "translation"/"trs"/"affine"/"homography"/"camera_3d"（src/shared/tracking/types.py:25-32，且 Color 页 UI 实际提供 translation/trs/affine/homography 四项，src/features/color/ui/tracker_panel.py:149-154）；而 Composite tracker 的模式词表仅为 "translation"/"trs"/"planar"（tracker_node.py:42），其 bake 逻辑用 `mode == "trs"` 门控：
    ```python
    mode = str(track.get("mode", "translation"))
    ...
    if mode == "trs":
        new_tracks["rotation"] = ...
    ```
    （composite_page.py:3974/4006/4012，stabilize 同样在 3849/3876 门控）。
  - 影响: 用户在 Color 页以「仿射」或「透视(homography)」模式跟踪后导入 Composite tracker：`mode` 原样写入 track_data 为 "affine"/"homography"——既不在 tracker 的合法词表内，也不等于 "trs"，于是「Bake to Transform」和「Bake Stabilize」只烘焙 translate_x/translate_y，rotation/scale 数据明明存在于每帧 transforms 中却被静默丢弃，匹配移动结果错误（旋转/缩放不跟随）。"homography" 本应对应 Composite 的 "planar"。
  - 建议: 在 `export_tracking_bindings` 中做显式映射（如 homography→"planar"、affine→"planar" 或降级 "trs" 并记录日志），或在导入端做未知模式的告警与降级提示，禁止超词表字符串直接进入 track_data。
  - 置信度: 高

- [AUD-24401] [Suggestion] 帧循环内的 `TrackStatus` import 未被使用（死代码），且位于每帧迭代内执行
  - 位置: src/features/color/facade.py:212
  - 证据:
    ```python
    for frame, tr in sorted(merged.items()):
        ...
        from src.shared.tracking.types import TrackStatus
    ```
    全文件无任何 `TrackStatus` 引用（状态字符串由 `getattr(..., "value", "valid")` 直接取得）。
  - 影响: 每帧执行一次 import 语句（模块缓存查找）纯属浪费；更重要的是误导读者以为状态枚举在此处被使用，掩盖真实取值路径。
  - 建议: 删除该 import；若未来需要枚举校验，移到模块顶部。
  - 置信度: 高

- [AUD-24402] [Suggestion] 日志使用无占位符的 f-string（F541）
  - 位置: src/features/color/facade.py:129、134
  - 证据:
    ```python
    logger.warning(f"Cannot request render: MainWindow not set")
    ...
    logger.warning(f"Cannot request render: Engine unavailable")
    ```
  - 影响: 无功能影响，但违反项目 lint 风格（多余 f 前缀），与同函数内 `logger.debug(f"...: {reason}")` 混用易造成格式漂移。
  - 建议: 去掉 f 前缀或改用 `logger.warning("...: %s", ...)` 惰性格式。
  - 置信度: 高

### 通过项

- 线程安全：facade 为无状态只读包装；`color_output` 经 `color_output_ready` 队列信号在引擎线程更新，facade 各 getter 仅做属性读取；`export_tracking_bindings` 访问的 `TrackingStore` 明确约定只在 UI 线程读写（tracking_store.py:21），唯一调用方 composite_page 的 `_on_tracker_import_color_requested` 在 UI 线程执行。
- GPU 资源生命周期：不创建/持有任何 ModernGL 对象，仅透传 `texture_id`（int）；deferred-release 的所有权在 engine/renderer 侧，facade 无泄漏面。
- GLSL·HDR 正确性：文件无 GLSL/HDR 处理逻辑。
- 信号防递归：未定义/连接任何 Qt 信号，无递归风险。
- 状态提交一致性：只读导出 + 转发渲染请求，不触碰 undo/redo/revision/持久化状态；导入端的 undo 记录由 composite_page 负责。
- 异常与取消路径：所有跨模块访问一律 `getattr(..., None)` + None 检查，`main_window`/`engine`/`color_output` 缺失时安全返回 None/0/False/[]；`request_render` 双分支（`request_color_output` 优先，`render_pipeline` 兜底）与引擎实际签名核对无误。
- 输入验证：`int(getattr(binding, "reference_frame", 0) or 0)`、`merged[int(frame)]`、transforms/corrections 的 `or {}` 兜底均到位；`matrix` 恒为 9 元组（TrackTransform 构造校验，types.py:117），嵌套 3x3 导出格式与消费端 `len(matrix) != 3` 检查匹配；manual 覆盖 automatic 的合并语义与 `TrackingBinding.transform_at` 一致。
- 性能模式：导出为一次性 O(绑定数 × 帧数) 的纯拷贝，无热点路径；`get_current_*` 系列均为 O(1) 属性读取。
- 类型注解：公开方法返回类型注解完整（Optional[int]/Optional[Tuple[int,int]]/int/bool/Optional[Any]/list）；`main_window: Any` 属有意的宽类型，`export_tracking_bindings -> list` 可补元素类型泛型（随 AUD-24401 一并清理即可，不单列）。

---

审查结论：facade 本体防御性强、无崩溃/泄漏风险；唯一实质缺陷是跨页跟踪数据的模式词表不对齐（AUD-24400，Major），会导致 affine/homography 跟踪导入后烘焙结果静默降级。
