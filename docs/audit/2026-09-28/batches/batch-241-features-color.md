# batch-241 [P2] src/features/color//

编号区间：AUD-24100–AUD-24199

## src/features/color/__init__.py (行数: 4)

### 发现
- [AUD-24101] [Suggestion] 包入口 eager import ColorModule，把 features→app 依赖链引入所有 `src.features.color.*` 导入
  - 位置: src/features/color/__init__.py:1-4
  - 证据:
    ```python
    from src.features.color.facade import ColorFacade
    from src.features.color.module import ColorModule

    __all__ = ["ColorFacade", "ColorModule"]
    ```
  - 影响: `src/features/color/module.py` 顶层 `from src.app.page_contract import ...` / `from src.app.app_context import ...`（module.py:16-17，已核实）。经由本 `__init__`，任何对 `src.features.color` 任意子模块（如 tracking 垫片）的导入都会先执行这两条 app 层导入：特性层反向依赖应用层被包入口放大，且 app 层未来变重/循环时 color 全域导入失败。
  - 建议: `__init__` 只保留 facade（轻，顶层仅 stdlib），ColorModule 改为 PEP 562 `__getattr__` 懒加载，或让宿主直接从 `src.features.color.module` 导入。
  - 置信度: 中（导入链为代码事实；「features 不应依赖 app」的分层约定来自任务说明，具体边界以项目 plan 为准）

### 通过项
- 导出一致性：`__all__` 与实际导入符号一致（ColorFacade、ColorModule 均存在，facade.py 254 行内定义）。
- 异常路径：无 try/except，无需评估。
- 输入验证 / 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交 / 性能：无逻辑，不适用（facade.py 顶层仅 `typing`/`logging`，无循环导入）。
