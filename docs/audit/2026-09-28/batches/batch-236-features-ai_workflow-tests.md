# batch-236 审查报告 — src/features/ai_workflow/tests/（8 文件，2,265 行）

编号区间：AUD-23600–AUD-23699。审查日期：2026-09-28。
本批全部为纯 Python 测试文件（无 Qt/GL/线程代码），九维度中 GPU/GLSL/HDR/信号防递归均不适用；
重点维度：输入验证（测试自身的断言有效性）、异常与取消路径、类型注解。
关键实现核对源：`src/features/ai_workflow/core/node_specs.py`、`core/workflow_models.py`、
`adapters/comfyui_converter.py`、`adapters/composite_adapter.py`。

统计：Critical 0 / Major 3 / Suggestion 17。

---

## src/features/ai_workflow/tests/test_fake_workflow_service.py (行数: 231)

### 发现

- [AUD-23600] [Major] 红线源码扫描在 runtime 目录缺失时静默返回空串，全部红线断言空转通过
  - 位置: src/features/ai_workflow/tests/test_fake_workflow_service.py:40
  - 证据:
    ```python
    _SRC_DIR = os.path.join(
        os.path.dirname(__file__), "..", "runtime",
    )

    def _scan_sources() -> str:
        chunks: List[str] = []
        if not os.path.isdir(_SRC_DIR):
            return ""
    ```
  - 影响: `_SRC_DIR` 依赖 `..` 相对拼接；一旦 tests 目录被复制/移动或 runtime 目录改名，`_scan_sources` 返回 `""`，`"import moderngl" not in ""` 等全部红线断言静默通过，P9.1 红线门（GPU 隔离 / 不写 timeline）整体失效且无任何报错。
  - 建议: 目录不存在时 `pytest.fail(...)`（或直接 `assert os.path.isdir(_SRC_DIR)`）；更稳妥的做法是通过 `runtime` 包自身定位（`import ...runtime; os.path.dirname(...runtime.__file__)`）。
  - 置信度: 高

- [AUD-23601] [Major] GPU 红线子串检查可被 `from moderngl import ...` 等导入形式绕过
  - 位置: src/features/ai_workflow/tests/test_fake_workflow_service.py:52
  - 证据:
    ```python
    source = _scan_sources()
    assert "import moderngl" not in source, "runtime 包不允许 import moderngl"
    assert "from src.features.composite.core" not in source, (
        "runtime 包不允许 import composite.core"
    )
    ```
  - 影响: `from moderngl import Context` 不含子串 `"import moderngl"`（ModernGL 的常见导入形式），`import src.features.composite.core` 不含 `"from src.features.composite.core"`，均可绕过扫描。红线守卫存在系统性盲区。
  - 建议: 改为模块名级断言（`"moderngl" not in source` / `"composite.core" not in source`），或用 `ast` 解析 import 语句后精确判定。
  - 置信度: 高

- [AUD-23602] [Suggestion] `cancel_after` 死变量
  - 位置: src/features/ai_workflow/tests/test_fake_workflow_service.py:161
  - 证据:
    ```python
    cancel_after = {"n": 0}
    ...
    def cancel_requested() -> bool:
        # n1 succeeded 后置 cancel=True
        return len(events) >= 2
    ```
  - 影响: `cancel_after` 从未被读写，误导读者以为取消计数逻辑在其中。
  - 建议: 删除该变量。
  - 置信度: 高

- [AUD-23603] [Suggestion] fixture 返回类型标注为 `Any`，丢失类型信息
  - 位置: src/features/ai_workflow/tests/test_fake_workflow_service.py:95
  - 证据:
    ```python
    @pytest.fixture
    def fake_service() -> Any:
    ```
  - 影响: 类型注解维度缺失；测试体内对返回值的所有方法调用无静态检查。
  - 建议: 标注 `-> "FakeWorkflowService"`（`TYPE_CHECKING` 导入即可，不破坏文件内延迟导入风格）。
  - 置信度: 高

### 通过项
- 异常与取消路径：中途取消 / 首节点前取消用例断言具体事件序列，无空转断言
- 性能模式：`node_duration_s=0.0` 默认避免真实 sleep；唯一计时用例用 `time.monotonic`，0.05s sleep 对 0.04s 阈值有余量，不易 flaky
- 状态提交一致性：WorkflowResult 字段探测（diagnostics/degraded/artifact_id 等）与 getattr 契约一一对应
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：纯 Python 测试，不涉及
- 输入验证：`_FakeWorkflowInput` 空 node_ids 回退 'workflow' 的边界有用例覆盖

---

## src/features/ai_workflow/tests/test_generation_node_specs.py (行数: 285)

### 发现

- [AUD-23610] [Suggestion] 注释声称校验字段标注，但断言仅检查 `"type"` 存在
  - 位置: src/features/ai_workflow/tests/test_generation_node_specs.py:156
  - 证据:
    ```python
    # 每个字段有 unit / range / cache_impact / upload_impact / cost_impact 标注
    for k, v in props.items():
        assert isinstance(v, dict)
        # 至少有 type
        assert "type" in v
    ```
  - 影响: JSON Schema 输出（`to_json_schema()`）本身未验证携带 unit/range/cache_impact 等标注——这些仅在 `field_metadata` 上另行验证。若 `to_json_schema` 丢弃标注，外部 schema 消费者拿不到，本测试仍通过。
  - 建议: 对 schema 字段额外断言元数据键（如 `x-unit` / `x-cache-impact`）被注入，或删掉与断言不符的注释。
  - 置信度: 高

- [AUD-23611] [Suggestion] `test_no_full_access_or_skip_approval_node` 实际仅覆盖 generation specs
  - 位置: src/features/ai_workflow/tests/test_generation_node_specs.py:134
  - 证据:
    ```python
    # 红线（§4.4）：不暴露"完全访问"/"跳过审批"/"忽略许可证"
    reg = NodeSpecRegistry()
    register_generation_node_specs(reg)
    for s in reg.all_specs():
    ```
  - 影响: 测试名是"不存在完全访问节点"（暗示全局），但 registry 只注册了 generation specs；M4 / Composite / Comfy specs 不在本断言范围内（test_p6 有等价全局覆盖，但本处断言范围与名称不符，易误信已全覆盖）。
  - 建议: 如 p6 一样注册全部四组后再断言，或改测试名限定"生成节点"。
  - 置信度: 高

- [AUD-23612] [Suggestion] unit/range 断言范围与注释不一致
  - 位置: src/features/ai_workflow/tests/test_generation_node_specs.py:214
  - 证据:
    ```python
    for fname, m in meta.items():
        # 数字字段必须有 unit 或 range
        assert "unit" in m or "range" in m, (
            f"{fname} missing unit/range annotation"
        )
    ```
  - 影响: 注释说"数字字段"，代码对全部字段（含 prompt 等文本字段）断言；未来新增无单位的文本字段会误报失败（过度严格），或注释先行失效。
  - 建议: 按字段类型过滤（仅数值字段要求 unit/range）或修正注释与断言一致。
  - 置信度: 高

- [AUD-23613] [Suggestion] `pytest` 导入未使用
  - 位置: src/features/ai_workflow/tests/test_generation_node_specs.py:24
  - 证据: `import pytest`，全文件无 `pytest.` 使用。
  - 影响: 死导入。
  - 建议: 删除。
  - 置信度: 高

### 通过项
- 输入验证：inpaint mask / i2i 参考图 / t2v duration 等关键端口与参数键均有针对性断言
- 异常与取消路径：本文件无异常路径需求
- executor 受控（`NodeExecutorId.ALL`）、timeline_write_required=False、不输出 texture、LoRA/ControlNet 不进核心 schema 等 red-line 断言真实有效
- 类型注解：测试方法均有 `-> None`

---

## src/features/ai_workflow/tests/test_node_canvas_protocol.py (行数: 128)

### 发现

- [AUD-23620] [Major] 红线子串检查可被其他导入形式绕过（同 AUD-23601 模式）
  - 位置: src/features/ai_workflow/tests/test_node_canvas_protocol.py:29
  - 证据:
    ```python
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    assert "from PyQt6" not in source  # 纯数据 + Protocol
    ```
  - 影响: `from moderngl import ...`、`import src.features.composite.core`、`import PyQt6.QtCore` 均不含对应子串，可绕过"本模块纯数据 + Protocol、无 Qt/GPU 依赖"的红线。
  - 建议: 改为模块名级子串（`"moderngl"` / `"PyQt6"` / `"composite.core"`）或 AST import 扫描。
  - 置信度: 高

- [AUD-23621] [Suggestion] "无 Qt 环境可导入"测试实际未验证任何 Qt 无关性
  - 位置: src/features/ai_workflow/tests/test_node_canvas_protocol.py:125
  - 证据:
    ```python
    # 重新导入模块（确认不依赖 Qt）
    import importlib
    mod = importlib.import_module("src.shared.node_canvas.graph_ui_protocol")
    assert hasattr(mod, "GraphUiProtocol")
    assert hasattr(mod, "DEFAULT_VISUAL_TOKENS")
    ```
  - 影响: `importlib.import_module` 命中 `sys.modules` 缓存（该模块已被 :24 的测试导入），不会重新执行模块；且当前测试环境装有 PyQt6，即使重新执行也无法证明"无 Qt 环境"。两个 `hasattr` 断言与 Qt 依赖无关，测试整体空转。
  - 建议: 依赖 :22-31 的源码扫描作为唯一守卫并删除本测试；或用子进程以屏蔽 `PyQt6` 的方式真实重导入。
  - 置信度: 高

- [AUD-23622] [Suggestion] `sys` 导入未使用
  - 位置: src/features/ai_workflow/tests/test_node_canvas_protocol.py:18
  - 证据: `import sys`，全文件无 `sys.` 使用。
  - 影响: 死导入。
  - 建议: 删除。
  - 置信度: 高

### 通过项
- 输入验证：unknown category / unknown artifact type 回退、diamond 审批端口等断言具体
- 异常与取消路径：`NotImplementedError` 基类契约用 `pytest.raises` 验证
- GPU / GLSL / HDR / 信号 / 线程：不涉及（纯数据协议测试）

---

## src/features/ai_workflow/tests/test_node_specs.py (行数: 185)

### 发现

- [AUD-23630] [Suggestion] executor 白名单硬编码字符串元组，与 `NodeExecutorId.ALL` 重复
  - 位置: src/features/ai_workflow/tests/test_node_specs.py:96
  - 证据:
    ```python
    assert spec.executor_id in (
        "analysis_provider", "tts_provider", "translate_provider",
        "llm_role", "taskgraph", "workflow_runner", "editorial_builder",
        "read_only", "control", "render", "ffmpeg",
    )
    ```
  - 影响: 已核对 `core/node_specs.py:123-127` 的 `NodeExecutorId.ALL` 与该元组逐项一致（11 项），但两处重复维护；枚举新增 executor 时此测试会假失败（或两侧不同步时漏检）。
  - 建议: 改为 `assert spec.executor_id in NodeExecutorId.ALL`（test_p6 已是此写法）。
  - 置信度: 高

### 通过项
- 输入验证：非法 `direction` 用 `pytest.raises(ValueError)` 覆盖；未知 spec_id 返回 None 覆盖
- 16 个 M4 spec id 清单完整（逐一数过为 16 项，与 plan §4.1 一致）
- naming convention / 端口 artifact type 覆盖 / apply 需 approval_token 等 red-line 断言真实
- 状态提交一致性：approval_token 输出契约有断言
- 类型注解：完整

---

## src/features/ai_workflow/tests/test_p10_3_templates.py (行数: 184)

### 发现

- [AUD-23640] [Suggestion] 手动注册 Composite spec 冗余且注释过时
  - 位置: src/features/ai_workflow/tests/test_p10_3_templates.py:140
  - 证据:
    ```python
    """新模板的 spec_id（含 Composite）在 registry 存在（Composite 需接线）。"""
    # 手动注册 Composite spec（模拟 dock 接线后）
    from src.features.ai_workflow.adapters.composite_adapter import (
        register_composite_node_specs,
    )
    register_composite_node_specs(full_registry)
    ```
    而同文件 :173-184 断言 `register_official_specs` 已直接注册 `topos.composite.*` / `topos.comfy.*`；已核对 `core/node_specs.py:457-483`：`register_official_specs` 内部确实接线 Composite + ComfyUI，且 `NodeSpecRegistry.register` 同版本幂等（node_specs.py:236-239）。
  - 影响: 死代码 + 过时注释，误导读者以为 Composite spec 尚未接进 official registry、需要外部"模拟接线"。
  - 建议: 删除手动注册与"需接线"注释，保留直接断言。
  - 置信度: 高

- [AUD-23641] [Suggestion] fixtures 缺返回类型注解
  - 位置: src/features/ai_workflow/tests/test_p10_3_templates.py:36
  - 证据:
    ```python
    @pytest.fixture
    def full_registry():
        """register_official_specs + Composite + ComfyUI spec（模拟 dock 接线后）。"""
        reg = NodeSpecRegistry()
    ```
    （同 :44 `def templates():`）
  - 影响: 类型注解维度缺失。
  - 建议: 标注 `-> NodeSpecRegistry` / `-> WorkflowTemplateRegistry`。
  - 置信度: 高

### 通过项
- 输入验证：三个新模板结构（节点 spec_id 集合、exposed_input、实例化 nodes/edges 数）断言具体
- 异常与取消路径：`require` 抛错路径隐式覆盖
- 状态提交一致性：不涉及
- 线程安全 / GPU / GLSL / HDR / 信号：不涉及

---

## src/features/ai_workflow/tests/test_p5_acceptance.py (行数: 286)

### 发现

- [AUD-23650] [Suggestion] 死变量 `g` 与虚假注释（构造并不拒绝 <3 段 spec_id）
  - 位置: src/features/ai_workflow/tests/test_p5_acceptance.py:252
  - 证据:
    ```python
    g = WorkflowGraph(
        workflow_id="wf-evil", name="x",
        nodes=(WorkflowNode(node_id="hack", spec_id="exec.python"),),
    )
    # spec_id 违反命名规范（< 3 段）→ WorkflowNode 构造就拒
    # 改用合规命名但未注册的 spec
    ```
  - 影响: 已核对 `core/workflow_models.py:107-108`：`WorkflowNode.__post_init__` 只校验 `spec_id` 非空，不校验段数——注释所述"构造时就拒"为假；`g` 实际创建成功且从未使用（死代码 + 误导性文档）。
  - 建议: 删除 `g` 及注释；若想验证构造期拒绝，应改 `pytest.raises` 并先让模型层真的实现该校验。
  - 置信度: 高

### 通过项
- 状态提交一致性：双入口（chat / node graph）同 skill_id + target、approval gate（timeline_write=approval_required）、workflow user_text='' 防 prompt 注入等红线断言完整且具体
- 输入验证：validator 拒绝未知 spec / 缺 approval_token 两类恶意 graph
- 异常与取消路径：不涉及运行时取消（编译期验收）
- 类型注解：测试方法均完整
- 线程安全 / GPU / GLSL / HDR / 信号：不涉及

---

## src/features/ai_workflow/tests/test_p6_acceptance.py (行数: 572)

### 发现

- [AUD-23660] [Suggestion] `if result.succeeded:` 守卫可使红线断言空转通过
  - 位置: src/features/ai_workflow/tests/test_p6_acceptance.py:354
  - 证据:
    ```python
    result = ex.fetch_outputs(req, ref2)
    if result.succeeded:
        for cand in result.candidates:
            # GeneratedAsset 不应有 timeline_ref 字段
            d = cand.to_dict()
            for k in d:
                assert "timeline" not in k.lower()
    ```
  - 影响: 测试名承诺"候选不携带 timeline 写入意图"红线；若 fake/executor 行为变化导致 `result.succeeded` 为 False，循环整体跳过、测试静默通过，红线未被验证（空转通过）。
  - 建议: 前置 `assert result.succeeded`（本用例已设 `server.auto_complete_with_image = True`，成功是预期），再做字段断言。
  - 置信度: 高

- [AUD-23661] [Suggestion] `ComfyJobState` 导入未使用
  - 位置: src/features/ai_workflow/tests/test_p6_acceptance.py:41
  - 证据: `from src.features.ai_workflow.adapters.comfyui_models import (... ComfyJobState ...)`，全文件仅此一处出现（用的是 `CompositeRenderJobState`，来自 composite_adapter）。
  - 影响: 死导入，且与 `CompositeRenderJobState` 名字相近，易混淆。
  - 建议: 删除。
  - 置信度: 高

- [AUD-23662] [Suggestion] 与 test_p7 重复复制的 fixture 助手已发生内容漂移
  - 位置: src/features/ai_workflow/tests/test_p6_acceptance.py:94
  - 证据: p6 `_sdxl_t2i_workflow()` 的 KSampler inputs 为 `{"seed": 12345, "cfg": 7.0, ...}`；test_p7_acceptance.py:97 的同名助手为 `{"seed": 12345, "cfg": 7.0, "steps": 30, ...}`（多 `"steps": 30`）。`_full_consent`（confirmed_at 日期不同）与 `_local_profile` 亦整段复制。
  - 影响: 同名 golden 助手两处维护且已漂移；后续修 golden workflow 时容易只改一份，造成两文件行为基线不一致。
  - 建议: 提取到 `tests/conftest.py` 或共享 helpers 模块。
  - 置信度: 高

### 通过项
- 异常与取消路径：Composite render 取消、Comfy job 取消 / hash 篡改 / fail 均断言无候选、无半写入（`output_asset_id == ""`）
- 状态提交一致性：唯一 timeline writer == apply_approved_plan、consent 不完整 server 收不到 job（`job_count == 0`）等红线断言具体且非空转
- 输入验证：preflight 缺模型 / 缺绑定 / 未知 custom node 的 deny code 断言准确
- 性能模式：全部 Fake 服务，无真实网络/sleep
- 类型注解：helper 与方法基本完整（`_setup`/`_setup_executor` 缺返回注解，轻微）
- 线程安全 / GPU / GLSL / HDR / 信号：不涉及（Fake 层）

---

## src/features/ai_workflow/tests/test_p7_acceptance.py (行数: 394)

### 发现

- [AUD-23670] [Suggestion] C5 红线测试的 for 循环在 findings 为空时空转通过
  - 位置: src/features/ai_workflow/tests/test_p7_acceptance.py:362
  - 证据:
    ```python
    # 关键：未知节点本身不被翻译成 Topos 节点
    for f in outcome.findings:
        # finding 的 source_node_ids 含未知节点 ID（仅记录），但 target 是高层节点
        # 不是"未知节点 → 某个 Topos 节点"的翻译
        assert f.target_spec_id in (
            "topos.generation.text_to_image", "topos.generation.upscale",
        )
    ```
  - 影响: 已核对 `adapters/comfyui_converter.py:321-323`：模式不匹配时 `findings` 为空元组且 verdict=OPAQUE。若未来 converter 收紧为"含未知节点即 OPAQUE"（正是该测试注释 :358-360 承认的可能），循环体不执行、测试空转通过，与测试名"未知节点不被伪装成 Topos 节点"的红线意图不符。
  - 建议: 显式断言 `outcome.verdict` 及 findings 非空/内容；更精确的红线是断言未知节点 id 只出现在 `source_node_ids`、绝不映射为任何 target。
  - 置信度: 高

- [AUD-23671] [Suggestion] `pytest` 与 `GOLDEN_FIXTURES` 导入未使用
  - 位置: src/features/ai_workflow/tests/test_p7_acceptance.py:24
  - 证据:
    ```python
    import pytest
    ...
    from src.features.ai_workflow.adapters.comfyui_converter import (
        ComfyToToposConverter, ConversionVerdict, GOLDEN_FIXTURES,
    )
    ```
    全文件无 `pytest.` 与 `GOLDEN_FIXTURES` 的其他使用。
  - 影响: 死导入。
  - 建议: 删除两项。
  - 置信度: 高

### 通过项
- 异常与取消路径 / 输入验证：完整链路（import → bind → preflight → submit → fetch）与 credential 清洗（`sk-secret` 不出现在导出 JSON）断言具体
- 状态提交一致性：候选 provenance 指向 ComfyUI provider、prompt 只存 hash 不存原文，断言准确
- 双轨（C4 转换 + C2 直执行）、C5 OPAQUE 仍可执行的边界断言真实
- 性能模式：Fake server，无网络/sleep
- 线程安全 / GPU / GLSL / HDR / 信号：不涉及

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-23600, AUD-23601, AUD-23620 |
| Suggestion | 17 | AUD-23602/03/10/11/12/13/21/22/30/40/41/50/60/61/62/70/71 |

共性主题：三个 Major 均为"红线守卫自身可被绕过/空转"（源码扫描子串盲区、目录缺失静默通过），建议统一改为 AST import 扫描 + 目录存在性 fail-fast；其余为死代码/死导入、注释与断言不符、helper 重复漂移、测试空转通过（`if succeeded:` 守卫、空 findings 循环）与类型注解缺失。
