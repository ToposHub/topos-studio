# batch-313 — src/shared/

编号区间：AUD-31300–AUD-31399。审查日期：2026-09-28。

## src/shared/__init__.py (行数: 3)

### 发现

（无发现。）

### 通过项
- 通过（无逻辑）：纯 re-export 文件。完整内容：
  ```python
  from src.shared.feature_flags import FeatureFlags, DEFAULT_FEATURE_FLAGS

  __all__ = ["FeatureFlags", "DEFAULT_FEATURE_FLAGS"]
  ```
  导出 `FeatureFlags`（特性门控类）与 `DEFAULT_FEATURE_FLAGS`（默认开关表，含 library/color/edit/composite/deliver/teaching_animation=True 及 AI 相关默认 False）。
- 依赖检查：feature_flags.py 仅依赖 stdlib（os/logging/dataclasses/typing），包级导入不会引入 Qt/OpenGL 等重依赖，`import src.shared` 轻量。
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交 / 异常取消 / 输入验证 / 性能 / 类型注解：均不适用于无逻辑的 re-export。
