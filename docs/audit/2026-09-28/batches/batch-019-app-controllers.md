# batch-019 审查报告 — 主题 src/app/controllers//（AUD-1900–AUD-1999）

审查人按 docs/audit/2026-09-28/assignments/INSTRUCTIONS.md 九维度执行。

## src/app/controllers/__init__.py (行数: 1)
### 发现
（无）
### 通过项
- 通过（无逻辑）：仅一行注释说明包用途（mixin 类从 MainWindow 抽出），无代码。

## src/app/controllers/agent_v2_chat_worker.py (行数: 100)
### 发现
- [AUD-19001] [Suggestion] run() 顶层 except Exception 将任意异常转为用户可见 failed(str)
  - 位置: src/app/controllers/agent_v2_chat_worker.py:84-86
  - 证据:
    ```python
        except Exception as exc:  # pragma: no cover - driver already catches, defensive
            self.failed.emit(str(exc))
            return
    ```
  - 影响: 编程错误（TypeError/AttributeError 等）与领域错误被统一压缩为一条失败文案，调试时难以区分；`.clinerules` 禁止宽泛 except 掩盖领域错误。此处虽经 failed 信号外抛未静默吞掉，但仍损失异常类型与栈信息。
  - 建议: 至少 `logger.exception(...)` 记录完整栈后再 emit；或按异常类型分层处理。
  - 置信度: 中（已有注释表明是防御性边界，非无意为之）。
- [AUD-19002] [Suggestion] finished_result 信号契约与实现不一致（异常路径不发）
  - 位置: src/app/controllers/agent_v2_chat_worker.py:40-42, 84-89
  - 证据:
    ```python
    #: Full result object, emitted exactly once alongside the terminal signal
    #: so the controller can record usage / artifacts.
    finished_result = pyqtSignal(object)
    ...
        except Exception as exc:
            self.failed.emit(str(exc))
            return
    ```
  - 影响: 模块 docstring 承诺 finished_result 与终结信号成对发出，但 run_chat 抛异常路径只发 failed、不发 finished_result，controller 在该路径无法记录 usage/artifacts，依赖该假设的调用方行为分叉。
  - 建议: 异常路径构造一个带 error 字段的 ChatRunResult 再发 finished_result，或修正 docstring 明确"仅成功返回时发出"。
  - 置信度: 高（契约文字与代码路径可直接对照）。
- [AUD-19003] [Suggestion] cancel_requested 与已成功结果存在竞态：晚到的取消会丢弃完整回复
  - 位置: src/app/controllers/agent_v2_chat_worker.py:92-97
  - 证据:
    ```python
        category = classify_chat_result(result)
        if category == "cancelled" or self.cancel_requested:
            self.cancelled.emit()
        elif category == "failed":
    ```
  - 影响: run_chat 已完整返回成功结果后（category 为 ok），若 UI 侧恰在此前置位 cancel_requested，成功回复被丢弃并显示为已取消；finished_result 已携带完整结果，UI 与 usage 记录出现"结果已生成但展示为取消"的不一致窗口。
  - 建议: 结果完整且非 cancelled 类别时优先按结果类别分发，cancel_requested 仅用于驱动器内部协作取消判断；或明确接受该语义并注释。
  - 置信度: 中（协作式取消的固有边界，属设计取舍而非缺陷）。
- [AUD-19004] [Suggestion] 构造参数 parent 缺类型注解
  - 位置: src/app/controllers/agent_v2_chat_worker.py:55
  - 证据:
    ```python
        stream: bool = True,
        parent=None,
    ) -> None:
    ```
  - 影响: 该文件其余参数均有注解，唯独 parent 裸露，类型检查覆盖不全。
  - 建议: 注解为 `parent: QObject | None = None`。
  - 置信度: 高
### 通过项
- 线程安全：run() 在 QThread 工作线程执行阻塞调用，信号从工作线程 emit 经 Qt 队列连接送达 UI 线程，符合"UI 线程不得阻塞/不得操作 OpenGL"铁律；不涉及 OpenGL。
- GPU 资源生命周期：不涉及 ModernGL 对象。
- GLSL·HDR 正确性：不涉及。
- 信号防递归：worker 只发射信号不接收，无递归回路。
- 状态提交一致性：不涉及 undo/redo/持久化。
- 输入验证：参数均由强类型注解约束，driver 侧负责领域校验；导入的 ChatRunDriver/ChatRunResult/classify_chat_result 已验证存在于 src/features/edit/ai_editing/chat_run_driver.py（class 定义分别位于 99/72 行，函数位于 713 行）。
- 性能模式：单次后台调用，无热路径问题。
