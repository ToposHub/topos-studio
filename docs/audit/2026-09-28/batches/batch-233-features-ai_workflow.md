# batch-233 [P2] src/features/ai_workflow//

编号区间：AUD-23300–AUD-23399

## src/features/ai_workflow/module.py (行数: 111)

### 发现
- [AUD-23301] [Suggestion] `except TypeError` 兜底可掩盖 `set_callbacks` 内部真实 TypeError，且仅记 debug 级日志
  - 位置: src/features/ai_workflow/module.py:82-88
  - 证据:
    ```python
    try:
        edit_project_io.set_callbacks(
            get_ai_workflow_payload=get_payload,
            on_ai_workflow_payload_loaded=on_loaded,
        )
    except TypeError:
        logger.debug("edit_project_io lacks ai_workflow callback slots", exc_info=True)
    ```
  - 影响: 已核实 `src/features/edit/core/edit_project_io.py:653-668` 的 `set_callbacks` 签名当前包含这两个 kwargs，即该兜底今日不会因签名不匹配触发；但 `TypeError` 同样会被 `set_callbacks` 实现体内的真实 bug（如对 None 回调误调用）抛出并被此处吞掉，默认 INFO 级别下 debug 日志不可见，表现为 workflow 数据静默不入工程文件且难以排查。
  - 建议: 用 `inspect.signature(edit_project_io.set_callbacks).parameters` 显式检查槽位存在后再调用，移除裸 `except TypeError`。
  - 置信度: 高（吞异常路径为代码事实；实际触发概率低，故仅 Suggestion）
- [AUD-23302] [Suggestion] 类型注解不完整：裸 `List` 返回注解与未注解参数
  - 位置: src/features/ai_workflow/module.py:55、96
  - 证据:
    ```python
    def register_pages(self, context: Any) -> List:
    ```
    ```python
    def register_panels(self, panel_registry) -> None:
    ```
  - 影响: `List` 无类型参数等价于 `List[Unknown]`，`panel_registry` 完全无注解；与文件其余方法（`context: Any`、`page_id: str`）风格不一致，削弱静态检查覆盖。
  - 建议: 改为 `-> list[Any]`（或 `List[Any]`），并为 `panel_registry` 补注解（协议/注册器类型）。
  - 置信度: 高

### 通过项
- 异常路径：构造器仅捕 `ImportError`（46-48 行），粒度符合「不宽泛掩盖领域错误」铁律，且 warning 级别可见。
- 状态提交一致性（持久化）：`register_save_handlers` 经 `edit_project_io.set_callbacks` 注册取/还原回调；已核实对方实现（edit_project_io.py:681-683 附近）对 None 回调有 `is not None` 检查，本文件 `getattr(..., None)` 缺省传入不会导致保存期崩溃；`main_window`/`edit_project_io` 缺失时提前 return，不会 AttributeError。
- 线程安全：无 OpenGL/GPU 调用，无自建线程；装配期方法在宿主启动流程调用。
- 信号防递归：不接触 Qt 信号，不适用。
- 输入验证：`context` 侧全部走 getattr 容错 + None 提前返回，外部宿主形态变化不会崩溃。
- 性能模式：构造器内的 UI import 只在实例化时发生一次；i18n 目录基于 `__file__` 计算，无重复 IO。
- GLSL·HDR 正确性：不涉及渲染与像素数据。
