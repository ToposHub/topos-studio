# batch-018 审查报告 — 主题 src/app//（AUD-1800–AUD-1899）

审查人按 docs/audit/2026-09-28/assignments/INSTRUCTIONS.md 九维度执行。

## src/app/__init__.py (行数: 5)
### 发现
（无）
### 通过项
- 通过（无逻辑）：纯 re-export，导出 `AppContext`、`PageDescriptor`、`FeatureModule`、`FeatureRegistry`，`__all__` 与导入一致。
- 已验证被引用模块均存在：src/app/app_context.py、src/app/page_contract.py、src/app/feature_registry.py。

## src/app/app_context.py (行数: 39)
### 发现
- [AUD-18002] [Suggestion] feature_flags 缺失时 is_feature_enabled 一律放行（fail-open）
  - 位置: src/app/app_context.py:36-39
  - 证据:
    ```python
    def is_feature_enabled(self, feature_id: str) -> bool:
        if self.feature_flags is None:
            return True
        return self.feature_flags.is_enabled(feature_id)
    ```
  - 影响: dataclass 所有字段默认 None，局部构造/测试场景下未装配 feature_flags 时，所有被门控的（含未完成/实验性）功能静默启用；与"门控默认应可预期"的期望相反（fail-closed 更安全）。
  - 建议: 至少在 docstring 记录 fail-open 语义；更稳妥的是返回 False 或记录 warning 日志后按显式策略处理。
  - 置信度: 中（src/main.py:230 实际装配了 feature_flags，主路径不受影响，风险集中在局部构造场景）。
- [AUD-18001] [Suggestion] 全部字段与 getter 返回类型为 Any，类型注解形同虚设
  - 位置: src/app/app_context.py:20-31
  - 证据:
    ```python
    main_window: Any = None
    config_manager: Any = None
    feature_flags: Any = None
    ...
    def get_main_window(self) -> Any:
        return self.main_window
    ```
  - 影响: 静态类型检查与 IDE 补全对上下文对象完全失效，调用方误用字段名/方法无法在编译期发现。
  - 建议: 用 `if TYPE_CHECKING:` 导入 MainWindow/ConfigManager/FeatureFlags/I18N/SaveRouter 等真实类型做注解（避免运行时循环导入）。
  - 置信度: 高
### 通过项
- 线程安全：纯数据容器，无共享可变状态，无跨线程访问点。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL。
- GLSL·HDR 正确性：不涉及着色器。
- 信号防递归：不涉及 Qt 信号。
- 状态提交一致性：不涉及 undo/redo/持久化。
- 异常与取消路径：is_feature_enabled 对 None 有显式分支，无裸引用崩溃。
- 性能模式：无热路径。
