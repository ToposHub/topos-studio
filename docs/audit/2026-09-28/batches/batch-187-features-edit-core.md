# batch-187 审查报告 [P1] src/features/edit/core//

任务书：docs/audit/2026-09-28/assignments/batch-187.md
编号区间：AUD-18700–AUD-18799

## src/features/edit/core/timeline_occlusion.py (行数: 869)

### 发现

- [AUD-18700] [Major] O6 ROI 计划的"单矩形缩小区域"分支不可达，且若可达则把遮挡矩形当作可见区域（语义反转）
  - 位置: src/features/edit/core/timeline_occlusion.py:747-769（成因在 468-477）
  - 证据:
    ```python
    # layer_coverage 唯一的 opaque+rect 出口（line 473-474）：
    if rect is not None and rect_covers_canvas(rect, canvas_w, canvas_h):
        return LayerCoverage("rect", rect, True, "rect_full_cover_opaque")

    # _compute_region_plan_impl（line 747-769）：
    elif any(kind == "full" for kind, _ in opaque_covers):
        region_by_index[input_index] = LayerVisibleRegion(
            clip_id, track_index, None, True)
    else:
        if len(opaque_covers) == 1:
            region_by_index[input_index] = LayerVisibleRegion(
                clip_id, track_index, opaque_covers[0][1], False)
    ...
    elif coverage.kind == "rect" and coverage.rect is not None:
        if rect_covers_canvas(coverage.rect, canvas_w, canvas_h):
            opaque_covers.append(("full", None))
        else:
            opaque_covers.append(("rect", coverage.rect))
    ```
    `coverage.opaque` 为 True 且 kind=="rect" 仅当 `rect_covers_canvas` 已通过（同参数重复调用，结果必然一致），故 line 769 的 `("rect", coverage.rect)` 永远不会 append；`opaque_covers` 只含 `("full", None)`，line 747 的 `any(kind == "full")` 恒真，line 751-761 整个 else 分支为死代码。
  - 影响: ①函数 docstring 与注释承诺的"单矩形覆盖时给出缩小区域"（部分覆盖 → ROI 收缩）永远不产出——tests/edit/test_timeline_occlusion.py:519-530 也实测 `region is None`（其注释把原因误写为 "unknown kind"，实际 kind=="rect" 且 opaque=False）；②若未来解除该不可达限制，`region=opaque_covers[0][1]` 存入的是"上方遮挡矩形"，与 `LayerVisibleRegion.region` 文档"可见矩形……可见区域 = 画布减去其上方全部确定性不透明覆盖"（line 640、679-680）正好相反，消费方接入 ROI 解码/局部合成后将处理被遮挡区域、丢弃真正可见区域，造成画面错误。O6 当前默认关闭（`TOPOS_TIMELINE_OCCLUSION_ROI=0`）且 src 内无调用方，属尚未引爆的数据契约缺陷。
  - 建议: 要支持部分覆盖 ROI，需让 `layer_coverage` 输出"确定性不透明但仅部分覆盖"的结论（opaque=True + 不覆盖画布的 rect），并让 region 输出"画布减覆盖"的保守外包络（或明确把契约改名为"被遮挡矩形"并同步全部文档/测试注释）；否则删除死分支，文档如实声明"当前只有 full/完全覆盖两种结论"。
  - 置信度: 高（不可达性可由两处同参数调用 `rect_covers_canvas` 逻辑证明；设计意图部分的判断为中）

- [AUD-18701] [Suggestion] 未使用的导入 `field`
  - 位置: src/features/edit/core/timeline_occlusion.py:40
  - 证据:
    ```python
    from dataclasses import dataclass, field
    ```
    全文件仅此一处出现 `field`，所有 dataclass 均未调用 `field(...)`。
  - 影响: 死导入，维护噪音。
  - 建议: 改为 `from dataclasses import dataclass`。
  - 置信度: 高

- [AUD-18702] [Suggestion] `effects_background_independent` 的宽泛 except 静默吞异常、无日志
  - 位置: src/features/edit/core/timeline_occlusion.py:394-395
  - 证据:
    ```python
        return True
    except Exception:  # noqa: BLE001 — 目录不可用时按未声明处理
        return False
    ```
    对比同文件另两处 fail-closed 回退均有 `logger.warning`（line 626、701-702）。
  - 影响: 目录导入失败/查询异常被完全掩盖，线上无法区分"效果未声明 background_independent"与"效果目录故障"，违背 .clinerules"宽泛 except 不得掩盖领域错误"的可诊断性要求（行为本身 fail-closed 正确，仅缺日志）。
  - 建议: 在 except 中加 `logger.debug`/`warning` 记录异常后再 return False。
  - 置信度: 高

- [AUD-18703] [Suggestion] `compute_visible_region_plan` 的 fail-closed 保护未覆盖函数入口（解包与转场分支在 try 之外）
  - 位置: src/features/edit/core/timeline_occlusion.py:682, 685-694
  - 证据:
    ```python
    canvas_w, canvas_h = output_size          # line 682：try 之外
    layer_tuple = tuple(layers or ())
    if tuple(transitions or ()):              # 转场保守分支亦无 try 保护
        conservative = tuple(
            LayerVisibleRegion(
                str(getattr(layer, "clip_id", "") or ""), ...
    ```
    而 try/except"异常回退为保守整画布"从 line 695 才开始；`layer.clip_id` 是鸭子类型 property，抛错会直接向外传播。
  - 影响: 形参契约外的输入（如 output_size 不是二元组）或异常 property 时，本函数直接抛异常而非 docstring 承诺的保守整画布 RegionPlan。实际触发概率低（内部类型化调用、O6 暂无消费方），故仅 Suggestion。
  - 建议: 将 output_size 解包与保守分支构造一并纳入 try，或在入口显式校验 output_size 长度后回退。
  - 置信度: 高（代码结构确定）；实际影响低

### 通过项
- 线程安全：纯分析模块无共享可变状态；`OcclusionTelemetry` 用 `threading.Lock` 保护 deque（snapshot 先拷贝再聚合）；`_ENV_CACHE` 读-算-写幂等（同一环境变量必得同一布尔值），最坏只是重复计算，无竞态危害；预览（渲染 worker）与导出共用全局遥测实例有锁保护。
- GPU 资源生命周期：不创建/持有任何 OpenGL、Metal、纹理或缓冲对象，符合计划 §4.1 纯分析约束。
- GLSL·HDR 正确性：无 GLSL 代码；无 HDR 数据 clamp；不透明判定用 `OPACITY_EPSILON=1e-6` 容差，不截断 HDR 值。
- 信号防递归：无 Qt 信号/控件，纯函数式输出（frozen dataclass），不涉及。
- 状态提交一致性：不修改 TimelineRenderPlan 不可变模型，只输出新的 OcclusionDecision/RegionPlan，无 undo/redo/持久化交互。
- 异常与取消路径：`_analyze_impl` 全扫描（含 facts_of 回调与 track_index 直取）包裹于 try/except 并 `logger.warning` + keep_all（fail-closed 回退为保留全部层而非黑帧）；导出消费方（timeline_export.py:6707-6722）另有外层 try 兜底；转场/单层/开关关闭均保守保留。
- 输入验证：transform（8 元组、有限性、退化缩放拒绝）、opacity（float 转换、有限性、≥1-ε）、crop（与渲染侧 crop_is_identity 语义一致且更保守：负值/羽化均阻止遮挡）、未知 Alpha（PixelFormatRegistry 查询 + 名称启发式，未知名一律按有 Alpha 处理）、非 Normal 混合、效果链、转场、composite 绑定层全部 fail-closed。
- 几何镜像正确性：`transformed_coverage_rect` 的坐标系、output_scale、pivot/custom 锚点与 m00..m12 仿射公式与 adjustment_composite.apply_transform_cpu（line 55-73）逐行一致；`transform_is_identity` 与 adjustment_composite.transform_is_identity 同语义（已核对源码）；任意旋转角度正确 fail-closed（仅 90° 倍数放行）；`rect_covers_canvas` 含 1px 安全膨胀。
- 性能模式：O(n) 纯 Python 几何扫描，无逐像素操作；遥测 deque maxlen=8192 有界；`get_effect_catalog` 为双检锁单例，函数内 deferred import 在模块已加载后仅为 sys.modules 查找，热路径开销可忽略。
- 类型注解：公开函数与数据类均有完整类型注解；`layer: Any` 为文档化的鸭子类型契约（ClipLayerInfo），可接受。
