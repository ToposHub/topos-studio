# batch-260 [P2] src/features/scripting/examples//

编号区间：AUD-26000–AUD-26099

说明：本批为面向用户的示例脚本（脚本面板中以 `topos` 全局对象执行），非产品执行路径；严重度按示例质量评。

## src/features/scripting/examples/advanced_workflow.py (行数: 51)

### 发现
- [AUD-26001] [Suggestion] 示例无任何输入验证与错误处理，占位路径必然失败
  - 位置: src/features/scripting/examples/advanced_workflow.py:3、35
  - 证据:
    ```python
    topos.open("/path/to/portrait.cr2")
    ```
    ```python
    topos.apply_lut("cinematic.cube", intensity=0.7)
    ```
  - 影响: 示例是用户脚本的模板；照抄后在文件不存在时直接抛异常中断，用户看到的是栈回溯而非「先检查文件」的正确范式，与「外部数据须验证」铁律的示范方向相反。
  - 建议: 至少在 open/LUT 处演示一次 try/except 或存在性检查，或在文件头注释说明示例假定路径存在。
  - 置信度: 高

### 通过项
- 通过：`~/Desktop/...` 波浪号路径可用——已核实 `ToposAPI.open/save` 均做 `os.path.expanduser`（src/features/scripting/api.py:419、449）。
- 通过：曲线控制点用 0-255 整数元组（16 行），符合 Python 侧 API 约定（`.0` 显式浮点铁律针对 GLSL，不适用）。
- 状态提交一致性：脚本末尾 46-51 行为纯注释（P1-COLOR-24 API 展望），不产生副作用。

## src/features/scripting/examples/basic_grade.py (行数: 23)

### 发现
- [AUD-26002] [Suggestion] 示例间控制台输出 API 混用：`topos.print` 与内建 `print` 不一致
  - 位置: src/features/scripting/examples/basic_grade.py:23 与 src/features/scripting/examples/composite_batch_params.py:24、src/features/scripting/examples/composite_build_graph.py:27-32（对照）
  - 证据:
    ```python
    output = topos.save("/output/graded.jpg", format="JPEG", quality=95)
    topos.print(f"Exported to: {output}")
    ```
    而 composite 两例：
    ```python
    print("params updated; generation", result["generation_before"],
          "->", result["generation_after"])
    ```
  - 影响: 已核实 `ToposAPI.print` 实现即 `print(message)`（src/features/scripting/api.py:1220-1223）且引擎以 `_StdoutRedirect` 重定向 sys.stdout（src/features/scripting/engine.py:154-171），两者功能等价、都能到达脚本控制台；但作为用户模板混用两种习惯，暗示存在两种输出通道，易被误读为语义不同。
  - 建议: 统一示例输出风格（建议统一 `topos.print`，与三个调色示例一致）。
  - 置信度: 高

### 通过项
- 通过：AUD-26001 的验证缺失问题同样体现在本文件（4 行 `topos.open("/path/to/image.jpg")`），已在该条统一记录，不重复计。
- 参数取值范围合理（exposure 0.5 / contrast 15 等），无 HDR 无理由 clamp 示范问题（导出 JPEG 为用户显式选择的交付格式）。

## src/features/scripting/examples/batch_grade.py (行数: 30)

### 发现
- （并入 AUD-26001）18-19 行 `list_files` 结果未判空即送 `topos.batch`：
  ```python
  files = topos.list_files("/path/to/photos/", extensions=[".jpg", ".cr2", ".nef"])
  topos.print(f"Found {len(files)} images")
  ```
  批处理示例应演示空清单/部分失败的处理路径（`results` 30 行只统计成功数），不重复开条。

### 通过项
- 通过：回调函数 `cinematic_grade(t)` 通过参数接收 API 对象而非闭包全局，模式干净。
- 通过：`list_files` / `batch` API 均存在（api.py:1158、1095 已核实）。

## src/features/scripting/examples/composite_batch_params.py (行数: 29)

### 发现

（无）

### 通过项
- 通过：演示了结构化校验 + 单一 undo 组的 plan 用法，检查 `result["applied"]` 后才消费 `result["report"]["errors"]`（23-29 行），错误路径示范正确。
- 通过：注释指明 node id 来源（快照 API / 前置构建脚本），降低悬空 id 风险。
- AUD-26002 的输出风格问题在本文件（24 行 `print`）已统一记录。

## src/features/scripting/examples/composite_build_graph.py (行数: 32)

### 发现

（无）

### 通过项
- 通过：单一 plan 内完成 常量→变换→模糊→output 全拓扑（7-24 行），与「整个拓扑变更落为一个 undo 组」的设计一致；`to_port`/`from_port` 名与 connect 语义配对正确。
- 通过：`result["applied"]` 为假时走 errors 分支（30-32 行），失败路径有示范。
- AUD-26002 的输出风格问题在本文件（27 行 `print`）已统一记录。
