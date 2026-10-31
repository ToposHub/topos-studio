# batch-261 [P2] src/features/scripting//

编号区间：AUD-26100–AUD-26199

## src/features/scripting/module.py (行数: 14)

### 发现
- [AUD-26101] [Suggestion] 工厂方法与模块级函数缺参数/返回类型注解
  - 位置: src/features/scripting/module.py:9、13
  - 证据:
    ```python
    def create_engine(self, image_engine):
        return ScriptEngine(image_engine)


    def create_script_engine(image_engine):
        return ScriptingModule().create_engine(image_engine)
    ```
  - 影响: `image_engine` 无注解、返回类型未标注（可注为 `ScriptEngine`）；对照同仓 `AiWorkflowModule` 的全注解风格，本文件削弱静态检查与 IDE 支持。`image_engine` 实际为 color 引擎对象（调用点 main_window.py:1285 传 `self.engine`），类型未文档化。
  - 建议: 补 `image_engine: Any`（或具体引擎协议）与 `-> ScriptEngine` 注解；顺带为类补 docstring 说明这是脚本特性工厂而非 FeatureModule。
  - 置信度: 高

### 通过项
- 通过：职责正确——纯工厂，无状态；唯一调用点已核实为 `src/app/main_window.py:1283-1285`（`create_script_engine(self.engine)`），无协议误用（本类不实现 FeatureModule，也不需要 feature_id/register_*）。
- 通过：`is_available()` 恒 True 合理——`ScriptEngine` 与本文件同包必可导入，无需探测。
- 异常路径：无 try/except；`ScriptEngine(image_engine)` 构造失败应显式上抛，符合「不吞领域错误」。
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交 / 输入验证 / 性能：无逻辑，不适用。
