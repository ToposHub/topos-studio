# Batch-017 静态审查：src/shared/render

- 审查日期: 2026-09-28
- 审查范围: `src/shared/render/__init__.py`（10 行）、`src/shared/render/source.py`（31 行）
- 问题编号区间: [AUD-1700–AUD-1799]
- 附加验证: 全仓 grep 确认 `RenderSource` / `src.shared.render` 在仓库内（src/、tests/）无任何消费者；grep 命中的 `src.shared.render_diagnostics` 是另一个模块，与本包无关。

## src/shared/render/__init__.py (行数: 10)

### 发现

（无）

### 通过项
- 纯 re-export 模块：仅从 `.source` 导入 `RenderSource` 并通过 `__all__ = ["RenderSource"]` 导出，无任何逻辑、无状态、无可执行路径。
- 线程安全: 无共享可变状态，N/A。
- GPU 资源生命周期: 模块内不触碰 GPU 资源。
- 状态提交一致性 / 信号防递归 / 异常与取消路径: 无信号、无状态提交、无 I/O，N/A。
- 性能模式: 导入为静态单次加载，无循环导入迹象（`source.py` 仅依赖 `typing`）。

## src/shared/render/source.py (行数: 31)

### 发现

- [AUD-1700] [Suggestion] 死代码：RenderSource 协议在全仓库无任何消费者
  - 位置: src/shared/render/source.py:6-11
  - 证据:
    ```python
    class RenderSource(Protocol):
        """Protocol for render output sources (viewer, export).

        Defines the interface for any component that produces render output.
        Feature modules can depend on this protocol to access rendered results
        without coupling to a specific viewer or export implementation.
        """
    ```
  - 影响: 文档声称"Feature modules can depend on this protocol"，但全仓 grep（src/ 与 tests/，`RenderSource`、`shared.render`）显示除包自身 `__init__.py` 的 re-export 外零引用。作为 shared 层的解耦抽象，它目前是纯维护负担：要么按计划接入（viewer/export 实现该协议），要么在确定不用时删除，避免后来的贡献者误以为存在这条解耦路径。
  - 建议: 二选一——挑选一个真实消费方（如 program_viewer / composite_viewport 的输出句柄）落地该协议；或删除 `src/shared/render` 包，待真正需要时再引入。若保留，在模块 docstring 标注"预留、暂无消费者"。
  - 置信度: 高（grep 验证）

- [AUD-1701] [Suggestion] `get_current_output() -> Any` 返回类型过弱，削弱 shared 层协议的类型价值
  - 位置: src/shared/render/source.py:25
  - 证据:
    ```python
    def get_current_output(self) -> Any:
        """Get the current render output.

        Returns:
            The current rendered output (image data, texture, etc.).
        """
    ```
  - 影响: 这是跨 feature 的正式输出协议，返回 `Any` 意味着所有实现方与消费方都绕过类型检查（裸 cast / 属性试探），协议形同虚设；且"image data, texture, etc."多种形态混在一个返回值里，消费方无法区分。项目规范要求强类型注解，这里恰是分层边界上最需要精确类型的位置。
  - 建议: 收窄为明确的输出类型（如 `TextureHandle | ndarray | None` 的 Union，或为协议引入 `TypeVar`/`Generic[T]` 让实现方绑定具体输出类型）；至少在 docstring 中枚举合法返回形态及其判别方式。
  - 置信度: 高

- [AUD-1702] [Suggestion] 返回 GPU 资源时缺少所有权/线程亲和性契约
  - 位置: src/shared/render/source.py:25-31
  - 证据:
    ```python
    def get_current_output(self) -> Any:
        """Get the current render output.

        Returns:
            The current rendered output (image data, texture, etc.).
        """
        ...
    ```
  - 影响: docstring 暗示可能返回纹理（"texture, etc."），但未约定：该纹理归属哪个 ModernGL context（本项目渲染在独立 render 线程上下文）、返回后调用方能否长期持有、是否可能被生产方复用/释放、跨线程使用是否安全。一旦未来按此协议实现（现有 viewer 均通过"拷贝到 viewer 自有纹理"的模式规避跨上下文问题），契约缺失极易导致悬空纹理或在 UI 线程触碰 render 线程的 GL 对象（Critical 级问题的温床）。
  - 建议: 在协议 docstring 中写明 GPU 输出的所有权语义与线程亲和性，例如"返回的纹理仅在 render 线程 context 内有效，消费方必须在同 context 下拷贝/ blit 后才可持有"；或直接规定协议只返回 CPU 侧数据（ndarray/bytes），GPU 资源另行通过带生命周期管理的句柄类型传递。
  - 置信度: 中（契约缺失是事实；实际影响取决于未来实现方式）

- [AUD-1703] [Suggestion] `is_available() == False` 时 `get_current_output()` 的契约未定义
  - 位置: src/shared/render/source.py:17-31
  - 证据:
    ```python
    def is_available(self) -> bool:
        """Return True if this render source is currently available.
        ...
    def get_current_output(self) -> Any:
        """Get the current render output.
        ...
    ```
  - 影响: 两个方法之间的前置条件没有约定：源不可用时调用 `get_current_output` 应返回 `None`、返回过期数据、还是抛异常（何种异常）均未说明。视频导出/预览这类有取消与异步路径的场景下，实现方各自解释会导致消费方防御性 try/except 泛滥或漏判 None。
  - 建议: 在 `get_current_output` docstring 中明确："仅当 `is_available()` 为 True 时可调用；不可用时返回 `None`（或抛 `RenderSourceUnavailableError`）"，二选一并统一。
  - 置信度: 高（契约缺失是事实，当前无运行时影响）

### 通过项
- 线程安全: 纯 `Protocol` 结构类型定义，无可执行状态与副作用，N/A。
- GPU 资源生命周期: 模块本身不创建/持有/释放任何 ModernGL 对象（仅契约层面的缺口已单列为 AUD-1702）。
- GLSL/HDR 正确性: 无着色器代码、无像素/颜色空间处理，N/A。
- 信号防递归: 无 Qt 信号，N/A。
- 状态提交一致性: 无状态机/无 commit 路径，N/A。
- 异常与取消路径: 无 I/O 与执行路径（契约层面的未定义行为已单列为 AUD-1703）。
- 输入验证: 无参数方法 + 只读属性声明，N/A。
- 性能模式: 无运行时代价（仅 class 定义时的字节码创建）。
- 类型注解: 方法签名均有返回类型注解；属性 `source_id: str`、`title: str` 类型明确（`-> Any` 的强度问题单列为 AUD-1701）。
- `from __future__ import annotations` 已正确使用，无运行时注解求值开销。
