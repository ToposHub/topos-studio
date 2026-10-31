# batch-235 审查报告：src/features/ai_workflow/tests/

- 批次: batch-235 [P2]，编号区间 AUD-23500–AUD-23599
- 文件数: 8（2,981 行）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 总计: Critical 0 / Major 1 / Suggestion 20

本批次全部为纯 Python 测试代码（无 UI、无 OpenGL、无 GLSL），线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归四个维度不适用；已核对全部被测 API（`RealComfyClient`、`FakeComfyServer`、`ComfyWorkflowExecutor`、`ComfyPreflightChecker`、`ComfyWorkflowExporter`、`ComfyToToposConverter`、`CompositeSubgraphAdapter`、`GenerationConsent` 等）签名与行为，测试断言与当前实现一致。发现集中在测试有效性（空转/同义反复断言）、私有结构白盒耦合与死代码。

## src/features/ai_workflow/tests/__init__.py (行数: 0)
### 发现
（无）
### 通过项
- 通过（无逻辑）：空包初始化文件，不导出任何符号。

## src/features/ai_workflow/tests/test_comfyui_client.py (行数: 322)
### 发现
- [AUD-23501] [Major] /queue→QUEUED 映射测试空转：注入态即期望态，且 fake 对未命中路由静默返回空 JSON
  - 位置: src/features/ai_workflow/tests/test_comfyui_client.py:136-144（辅证 :75、:302-311）
  - 证据:
    ```python
    def test_queue_pending_maps_to_queued(self) -> None:
        client, _ = _client({
            ("GET", "/history/abc"): {},
            ("GET", "/queue"): json.dumps({
                "queue_running": [], "queue_pending": [["abc"]],
            }),
        })
        client._jobs["abc"] = _record(client, "abc")
        assert client.get_job("abc").state == ComfyJobState.QUEUED
    ```
    `_record` 注入 `state=ComfyJobState.QUEUED`（:309）；`_ScriptedOpener` 未命中路由时 `return _FakeResponse(body or b"{}")`（:75）静默返回空对象；实现侧 `_refresh_state` 在 history/queue 均未命中时"保持当前状态"（comfyui_client.py:359 起）。
  - 影响: 模块 docstring 验收明确要求"状态 → /history/{id} + /queue"HTTP 映射，但本测试即使 client 完全不请求 /queue、或请求路径拼写错误，断言依然通过（状态保持注入的 QUEUED）。/queue 轮询路径回归不可检测，属于验收项的假覆盖。
  - 建议: 注入 `state=RUNNING`（或 FAILED 以外的非 QUEUED 态）再断言轮询后回到 QUEUED，并顺带断言 `("GET", "/queue") in opener.calls`；同时考虑让 `_ScriptedOpener` 对未注册路由抛错（fail-loud），消除静默默认值对路由回归的掩盖。
  - 置信度: 高（已对照 `_refresh_state` 实现验证空转路径成立）
- [AUD-23502] [Suggestion] `if False` 死表达式伪装成 submit 调用
  - 位置: src/features/ai_workflow/tests/test_comfyui_client.py:129
  - 证据:
    ```python
    client.submit(payload={}, workflow_snapshot_hash="h") if False else None
    # 手工注入 job 记录（避免 submit 路由）
    client._jobs["abc"] = _record(client, "abc")
    ```
  - 影响: 读者会误以为 submit 被调用过；表达式永不为真，纯噪音，掩盖了测试实际依赖手工注入的事实。
  - 建议: 删除该行，仅保留注释说明记录为手工注入。
  - 置信度: 高
- [AUD-23503] [Suggestion] 7 处测试直接注入私有 `_jobs` 并导入私有 `_JobRecord`
  - 位置: src/features/ai_workflow/tests/test_comfyui_client.py:131,143,152,170,180,189,205（导入于 :303）
  - 证据:
    ```python
    from src.features.ai_workflow.adapters.comfyui_client import _JobRecord

    return _JobRecord(
        external_job_id=job_id,
        profile_id=client.profile_id,
        ...
    )
    ```
  - 影响: 测试与 `_JobRecord` 私有构造签名强耦合；实现重构字段（如增加 progress 字段默认值变化）会同时打爆全部状态测试，且测试行为可能偏离真实 submit 产生的记录。
  - 建议: 为测试提供一个公开的注入途径（如 client 内部工厂参数或测试专用 helper 放在被测模块的 `if TYPE_CHECKING` 之外的可导出位置），或至少集中封装 `_record` 并注释私有耦合风险。
  - 置信度: 高
- [AUD-23504] [Suggestion] 下载落盘断言硬编码 "/" 分隔符
  - 位置: src/features/ai_workflow/tests/test_comfyui_client.py:215
  - 证据:
    ```python
    assert (tmp_path / (out.local_path.split("/")[-1])).exists()
    ```
  - 影响: 若 `local_path` 使用平台原生分隔符（Windows `\`），`split("/")` 取不到文件名，断言退化为检查 tmp_path 目录本身存在而恒真（或直接失败）；正确性依赖实现恰好用 "/" 拼接。
  - 建议: 用 `pathlib.Path(out.local_path).name` 或 `os.path.basename` 提取文件名。
  - 置信度: 高
- [AUD-23505] [Suggestion] 协议一致性测试仅 getattr callable，与验收"签名 + 行为"不符
  - 位置: src/features/ai_workflow/tests/test_comfyui_client.py:262-271
  - 证据:
    ```python
    for method in ("submit", "get_job", "cancel", "job_outputs"):
        assert callable(getattr(fake, method, None)), method
        assert callable(getattr(client, method, None)), method
    ```
  - 影响: 模块 docstring 验收声明"与 FakeComfyServer 协议一致（签名 + 行为）"，但本测试不校验参数签名、返回类型（如 `job_outputs -> (outputs, hashes, cost)` 三元组），Real/Fake 协议漂移不会被捕获（executor 侧恰因协议漂移敏感才专门加了 job_outputs 测试）。
  - 建议: 用 `inspect.signature` 比较两方法签名，或对 `job_outputs` 返回三元组结构做一次 Real 侧断言（现有 TestOutputs 已部分覆盖，可补齐 Fake/Real 返回形状对照）。
  - 置信度: 高
- [AUD-23506] [Suggestion] cloud Bearer 头只测 happy path
  - 位置: src/features/ai_workflow/tests/test_comfyui_client.py:232-247
  - 证据:
    ```python
    store = type("S", (), {"get_secret": lambda self, ref: "sk-test"})()
    client = RealComfyClient(profile, store, opener=capture)
    client.submit(payload={}, workflow_snapshot_hash="h")
    assert captured["auth"] == "Bearer sk-test"
    ```
  - 影响: 未覆盖反向断言：local profile 不得携带 Authorization 头、cloud 但 store 缺 secret 时不得发送 `Bearer None`。实现 `_headers` 中 `if secret:` 分支（comfyui_client.py:299 附近）无负路径测试，若条件误写为 `if self._profile.is_cloud`，泄漏空 Bearer 头的回归不可检测。
  - 建议: 增加 local profile 捕获头为 None、cloud+get_secret 返回 None 时头缺失两个用例。
  - 置信度: 高
### 通过项
- 异常与取消路径：submit HTTPError→RuntimeError、瞬时轮询错误回退缓存态、terminal job cancel no-op 均有真实断言。
- 状态提交一致性：ProfileRegistry save/load roundtrip 与坏条目跳过覆盖到位。
- 输入验证：profile 构造约束经 models 测试覆盖；registry `load_from_config` 半成品条目跳过有断言。
- 性能模式：sleep 注入消除真实等待，无慢测试风险。
- 类型注解：辅助类与测试方法均有完整注解。

## src/features/ai_workflow/tests/test_comfyui_converter.py (行数: 299)
### 发现
- [AUD-23510] [Suggestion] "转换不执行 custom node"红线测试不可证伪
  - 位置: src/features/ai_workflow/tests/test_comfyui_converter.py:284-299
  - 证据:
    ```python
    raw["999"] = {
        "class_type": "EvalNode",
        "inputs": {"code": "import os; os.system('rm -rf /')"},
    }
    snap = _imported(raw)
    ...
    outcome = converter.convert(snap)
    # 应识别为 text_to_image（恶意节点不影响模式匹配）
    assert outcome.verdict == ConversionVerdict.CONVERTED
    ```
  - 影响: 恶意节点只是 dict 字面量，`convert()` 中不存在任何可执行路径，测试无论实现如何都通过；它实际只复验了模式匹配（与第一个测试重复），对"不执行"这一红线零检测力（若未来引入动态 import/eval 回归，本测试照样绿色）。
  - 建议: 用可观测手段固化红线：在测试中替换/monkeypatch `builtins.__import__`、`eval`、`exec` 或 subprocess 为哨兵并断言未被调用，使红线可证伪。
  - 置信度: 高
- [AUD-23511] [Suggestion] 每个测试重复 registry/converter 构建样板，缺 fixture
  - 位置: src/features/ai_workflow/tests/test_comfyui_converter.py:92-94（同构重复于 :102-104、:114-116、:147-149 等 10+ 处）
  - 证据:
    ```python
    reg = NodeSpecRegistry()
    register_generation_node_specs(reg)
    converter = ComfyToToposConverter(registry=reg)
    ```
  - 影响: 三行样板在十余个测试中逐字重复；`register_generation_node_specs` 签名或 converter 构造变化需改十余处，且每次重建注册表增加无效开销。
  - 建议: 提取 `@pytest.fixture` 返回配置好的 converter（或模块级 fixture），测试只写差异部分。
  - 置信度: 高
- [AUD-23512] [Suggestion] 直接索引 findings[0] 前未断言数量
  - 位置: src/features/ai_workflow/tests/test_comfyui_converter.py:108、:151
  - 证据:
    ```python
    outcome = converter.convert(snap)
    assert outcome.findings[0].target_spec_id == "topos.generation.upscale"
    ```
  - 影响: 若 converter 误产出 0 个 findings，测试以 IndexError 报"error"而非清晰断言失败；与同文件 :97 先断 `len(outcome.findings) == 1` 的写法不一致。
  - 建议: 索引前补 `assert len(outcome.findings) == 1`（或 `assert outcome.findings`）。
  - 置信度: 高
### 通过项
- 输入验证：未知 workflow OPAQUE、partial workflow 不转、tampered golden 不识别等负路径覆盖到位且非空转。
- 异常与取消路径：不适用（纯转换无 IO/异步）。
- 状态提交一致性：source_workflow_hash 关联、to_dict/to_json 可序列化均有断言。
- 类型注解：测试方法均有 `-> None` 注解。

## src/features/ai_workflow/tests/test_comfyui_executor.py (行数: 680)
### 发现
- [AUD-23520] [Suggestion] hash-mismatch server 测试名与断言不符，outputs 从未被断言
  - 位置: src/features/ai_workflow/tests/test_comfyui_executor.py:249-269
  - 证据:
    ```python
    def test_server_complete_with_hash_mismatch_returns_no_outputs(self) -> None:
        # 红线（§9.4 #5）：输出 hash 不一致时拒绝（executor 处理）
        ...
        ref3, outputs = server.complete_with_outputs(
            ref.external_job_id,
            outputs=(ComfyOutputArtifact(... content_hash="sha256:deadbeef" ...),),
            tamper_hash=True,
        )
        # tamper 模式下，server 标记 SUCCESS 但 outputs 的实际 hash 与报告不一致
        # （由 executor 在 fetch_outputs 时校验）
        assert ref3.state == ComfyJobState.SUCCESS
    ```
  - 影响: 测试名宣称 "returns_no_outputs"，但 `outputs` 解包后从未断言（也无 `assert len(outputs) ...`）；实际只复验了 SUCCESS 状态。Fake server tamper 行为（报告 hash 与 actual_output_hashes 不一致）本身零覆盖，若 tamper_hash 参数实现回归（如不再篡改 actual hashes），:465 的 executor 测试将误报到错误层面且本测试仍绿色。
  - 建议: 补断言 tamper 后 `hashes` 与 artifact 报告的 content_hash 不一致（通过 `server.job_outputs` 取实际 hash 表对照），使本测试匹配其名称。
  - 置信度: 高
- [AUD-23521] [Suggestion] 两处测试穿透 `server._jobs` 私有结构，与本文件声明的协议约束相悖
  - 位置: src/features/ai_workflow/tests/test_comfyui_executor.py:546、:567
  - 证据:
    ```python
    assert result.error_code == ComfyExecutorErrorCode.TIMEOUT
    assert next(iter(server._jobs.values())).state == ComfyJobState.CANCELLED
    ```
    同文件 :569-570 明确写道："executor 不穿透 _jobs 私有结构（Fake/Real 同 job_outputs 协议）"。
  - 影响: 自我矛盾：协议测试声明以 `job_outputs`/`get_job` 为公共面，但超时/协作取消测试直接迭代 `_jobs`；`_FakeJob` 字段重构时这些测试崩溃方式晦涩（AttributeError 于私有对象），且 `next(iter(...))` 假设恰有一个 job，未来 executor 重试策略变化会引入误报。
  - 建议: 改用 `server.get_job(...)`（submit 返回的 ref 已持有 external_job_id）并断言精确 job ID 的状态。
  - 置信度: 高
### 通过项
- 异常与取消路径：timeout→provider cancel、cooperative cancel 传播、server fail、cancelled 结果映射覆盖全面且断言到具体 error_code。
- 输入验证：preflight 失败拒交、consent 不完整/ provider 不匹配拒绝、幂等冲突、成本超预算覆盖到位。
- 状态提交一致性：provenance 完整性（provider_id/model_id/model_version 带 workflow hash）断言具体。
- 线程安全：无并发使用，poll_interval/timeout 参数边界（executor 构造校验）间接覆盖。
- 类型注解：完整。

## src/features/ai_workflow/tests/test_comfyui_export.py (行数: 298)
### 发现
- [AUD-23530] [Suggestion] "不含凭据"红线测试空转：输入 fixture 本身不含任何凭据
  - 位置: src/features/ai_workflow/tests/test_comfyui_export.py:135-143
  - 证据:
    ```python
    result = exporter.export_snapshot(snap)
    low = result.api_format_json.lower()
    # 红线（§0 第四条）：不写 API key / credential
    for bad in ("api_key", "apikey", "secret", "password", "credential_ref",
                "bearer", "authorization"):
        assert bad not in low, f"exported JSON leaks {bad!r}"
    ```
  - 影响: `snap` 来自纯 SDXL workflow，源数据从未含凭据字段；即使 exporter 未来把 profile.credential_ref 原样写入 `_topos_meta`，本测试仍通过（源里没有该字段可复制）。红线（不写 credential）对"导出会带出凭据"的场景零检测力。
  - 建议: 构造含 credential 的 `ComfyConnectionProfile`（或带 api_key 形状字段的 workflow 输入）参与导出路径，再断言导出 JSON 不含其值；至少断言 `_topos_meta` 的键集合为封闭白名单。
  - 置信度: 高
- [AUD-23531] [Suggestion] "只读不联网不执行"测试仅验证两次导出结果一致
  - 位置: src/features/ai_workflow/tests/test_comfyui_export.py:126-133
  - 证据:
    ```python
    def test_export_is_readonly_no_network(self) -> None:
        # 红线：导出不联网 / 不执行
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        # 多次导出无副作用
        r1 = exporter.export_snapshot(snap)
        r2 = exporter.export_snapshot(snap)
        assert r1.api_format_json == r2.api_format_json
    ```
  - 影响: 名称承诺验证"不联网/不执行"，实际只验证确定性（幂等输出）；若实现加入网络探测或执行逻辑但输出仍稳定，测试照样通过。同文件无任何 socket/monkeypatch 防护。
  - 建议: 测试内 monkeypatch `socket.socket`（或 `urllib.request.urlopen`）为抛错哨兵后再导出，使"不联网"可证伪；或改名以匹配实际断言。
  - 置信度: 高
### 通过项
- 输入验证：bindings 写入 payload、占位节点 class_type 以下划线前缀不伪装、`_topos_meta` 排除后节点数等价断言到位。
- 状态提交一致性：workflow_snapshot_hash + schema_version 可追溯断言具体。
- 异常与取消路径：不适用（同步只读导出）。
- 类型注解：完整。

## src/features/ai_workflow/tests/test_comfyui_importer.py (行数: 382)
### 发现
- [AUD-23540] [Suggestion] 死变量 + hasattr 探测疑似不存在的方法 `_replace_if_possible`
  - 位置: src/features/ai_workflow/tests/test_comfyui_importer.py:261-269
  - 证据:
    ```python
    report_with_required = ComfyImportReport(
        snapshot=report.snapshot._replace_if_possible() if hasattr(report.snapshot, "_replace_if_possible") else report.snapshot,
        required_bindings=(ComfyInputBinding(slot=slot, resolved_value="placeholder"),),
    )
    # 这个 slot 必填但 resolved_value 是 placeholder（未真实绑定）→ 视为未绑定
    # 简化：用空 binding 测试
    ```
  - 影响: `report_with_required` 构造后从未使用（后续走 `report2` 分支）；`_replace_if_possible` 在 `ComfyWorkflowSnapshot`（frozen dataclass，仅有 from_api_format/to_dict）上不存在，hasattr 恒 False——遗留的试探代码与三段"改用…"注释说明本测试是边写边改的残稿，可读性差且易误导后续维护者以为存在该方法。
  - 建议: 删除 `report_with_required` 段与 slot2 死代码，保留 `required_unbound_slot_ids` 路径并清理注释。
  - 置信度: 高
- [AUD-23541] [Suggestion] "JobRef 不可绕过 preflight"测试为死分支 + 同义反复断言，验证力为零
  - 位置: src/features/ai_workflow/tests/test_comfyui_importer.py:363-382（辅证 :361）
  - 证据:
    ```python
    result = ComfyPreflightChecker().check(report, capabilities=caps)
    # 当 preflight 失败时，调用方**不应**创建 JobRef；测试体现这一契约
    assert result.allowed is False
    # 如果允许，那才能构造 JobRef（这里是反证）
    if result.allowed:
        _ = ComfyJobRef(
            external_job_id="x", profile_id="local",
            workflow_snapshot_hash=report.snapshot.content_hash,
        )
    ```
    同类模式 :361 `assert not hasattr(result, "submitted") or not getattr(result, "submitted", False)`——属性不存在时恒真。
  - 影响: 上一行已断言 `allowed is False`，`if result.allowed:` 分支永不可达；JobRef 构造本身无任何 gate 可测（注释也承认"由调用方串起"）。测试名承诺"cannot construct Jobref without passing preflight"，实际未验证任何强制机制，给人"该约束已被测试锁定"的假象；若调用方绕过 preflight 直接建 JobRef，无测试能发现。
  - 建议: 要么删除本测试，要么把约束下沉为可测机制（如 `ComfyJobRef.for_allowed_report(report)` 工厂要求 `result.allowed`），再测绕过路径抛错。
  - 置信度: 高
- [AUD-23542] [Suggestion] 对诊断文案的子串断言脆弱（与英文措辞强耦合）
  - 位置: src/features/ai_workflow/tests/test_comfyui_importer.py:243-250
  - 证据:
    ```python
    low = d.suggested_action.lower()
    assert "manually" in low or "your comfyui" in low
    # 不应出现 "topos will download" 这类暗示代下载的措辞
    assert "topos will download" not in low
    assert "auto download" not in low
    assert "auto-download" not in low
    ```
  - 影响: 断言依赖实现的具体英文措辞；项目有 i18n 工具链，一旦 suggested_action 走本地化或仅改一个词（如 "manual placement"），测试失败但行为并未回归；反向三条黑名单也未覆盖 "we'll fetch"/"will be downloaded" 等等价措辞。
  - 建议: 在 `ComfyPreflightResult`/诊断对象上增加结构化标志（如 `offers_download: bool = False`）并断言标志，措辞断言仅保留为冒烟。
  - 置信度: 中（当前实现为英文且测试通过；问题为脆弱性而非错误）
### 通过项
- 输入验证：非法 raw（非 dict/空/缺 class_type）、非 object JSON 拒绝覆盖到位。
- 异常与取消路径：preflight 各 deny_code（MISSING_NODE/MISSING_MODEL/MISSING_BINDING/UNPINNED_CUSTOM_NODE）负路径断言具体。
- 状态提交一致性：report to_dict 可序列化 + node_count/output_node_count 摘要断言到位。
- 线程安全：importer 无网络/线程，由 capabilities DTO 注入，测试与此一致。
- 类型注解：完整。

## src/features/ai_workflow/tests/test_comfyui_models.py (行数: 538)
### 发现
- [AUD-23550] [Suggestion] `pytest.raises(Exception)` 捕获过宽
  - 位置: src/features/ai_workflow/tests/test_comfyui_models.py:207-209
  - 证据:
    ```python
    b = ComfyInputBinding(slot=slot, source_artifact_id="text/doc-001")
    with pytest.raises(Exception):
        b.source_artifact_id = "tampered"  # type: ignore[misc]
    ```
  - 影响: frozen dataclass 应抛 `dataclasses.FrozenInstanceError`（AttributeError 子类）；用 `Exception` 会放过任意无关异常（如拼写错属性触发的其他错误也判通过），断言精度低，与 .clinerules"不得宽泛捕获掩盖领域错误"精神相悖。
  - 建议: 改为 `pytest.raises(dataclasses.FrozenInstanceError)`。
  - 置信度: 高
- [AUD-23551] [Suggestion] 绑定"类型兼容"正向用例是回显断言，未测任何类型校验
  - 位置: src/features/ai_workflow/tests/test_comfyui_models.py:189-191
  - 证据:
    ```python
    # 兼容：integer
    b = ComfyInputBinding(slot=slot, source_artifact_id="", resolved_value="42")
    assert b.resolved_value == "42"
    ```
  - 影响: 断言只是回显构造入参，对 `resolved_value="abc"`（非整数串）是否应被 integer slot 接受没有任何立场；测试名"值类型必须匹配 slot"的前半部分实际零覆盖（当前实现也不校验字面量类型，测试固化了这一缺口而非暴露它）。
  - 建议: 明确产品语义：若要求校验则补 `resolved_value="abc"` + integer slot 抛 ValueError 的负例；若不校验则在注释/断言中显式记录该行为边界。
  - 置信度: 高
- [AUD-23552] [Suggestion] "快照不执行 custom node"红线测试不可证伪
  - 位置: src/features/ai_workflow/tests/test_comfyui_models.py:359-370
  - 证据:
    ```python
    raw["666"] = {"class_type": "EvilNode", "inputs": {"__evil__": "rm -rf /"}}
    snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
    # 解析成功，但只是元数据；不执行任何东西
    evil = [n for n in snap.nodes if n.class_type == "EvilNode"]
    assert len(evil) == 1
    ```
  - 影响: 与 converter/importer 中的同名测试同病：dict 字面量无执行通道，测试实际只验证元数据解析；"不执行"红线无观测手段，未来引入 eval/import 类回归不可检测。
  - 建议: monkeypatch `builtins.eval`/`__import__`/`os.system` 为哨兵并断言零调用，使红线可证伪（三个文件的同类测试可共用一个 helper）。
  - 置信度: 高
### 通过项
- 输入验证：空 content_hash、非法 artifact_kind、非法 base_url scheme、非法 kind、空 workflow、非 dict 根等负例覆盖强。
- 状态提交一致性：content_hash 稳定/随内容变化、to_dict roundtrip（raw_payload 刻意不外泄）断言到位。
- GPU 资源生命周期：JobRef/输出工件不持有 GPU 对象的键名断言到位。
- 性能模式：纯 dataclass 构造，无性能敏感点。
- 类型注解：完整。

## src/features/ai_workflow/tests/test_composite_adapter.py (行数: 462)
### 发现
- [AUD-23560] [Suggestion] "输入是 asset_ref 不是 texture"红线测试为同义反复
  - 位置: src/features/ai_workflow/tests/test_composite_adapter.py:161-172
  - 证据:
    ```python
    req = CompositeSubgraphRequest(
        request_id="sub-001",
        graph_ref=CompositeGraphRef(...),
        input_asset_ids=("asset/1",),
    )
    # input_asset_ids 都是字符串 ID
    for a in req.input_asset_ids:
        assert isinstance(a, str)
    ```
  - 影响: 构造时传入字符串元组，再断言元素是 str——回显构造入参，任何实现都能通过；红线"不允许 texture 输入"（如传入 moderngl Texture 对象应被拒绝）从未被负例验证。
  - 建议: 补负例：`input_asset_ids=(object(),)` 或模拟 texture 对象应触发 ValueError（若实现未拒绝，则是产品缺陷由本测试暴露）。
  - 置信度: 高
- [AUD-23561] [Suggestion] 规格注册测试 4 处方法内重复局部 import + 两类重复 helper
  - 位置: src/features/ai_workflow/tests/test_composite_adapter.py:400-405、:424-429、:438-442、:452-456（helper 重复 :211-215 与 :302-309）
  - 证据:
    ```python
    def test_register_composite_specs(self) -> None:
        from src.features.ai_workflow.core.node_specs import (
            NodeExecutorId, NodeSpecRegistry,
        )
        from src.features.ai_workflow.adapters.composite_adapter import (
            register_composite_node_specs,
        )
        reg = NodeSpecRegistry()
        register_composite_node_specs(reg)
    ```
  - 影响: 同一三行 import 在 4 个测试方法内逐字重复，`_graph_ref`/`_service` 在两个测试类中重复定义；维护成本与噪音，违背文件顶部已集中 import 的常规。
  - 建议: 提升为模块级 import + `@pytest.fixture`（注册好的 registry），删除重复 helper。
  - 置信度: 高
### 通过项
- 异常与取消路径：GRAPH_NOT_FOUND/GRAPH_STALE 拒绝、取消传播、terminal 后 cancel no-op、失败后 fetch 无半写入输出覆盖到位。
- GPU 资源生命周期：GraphRef/RenderJobRef to_dict 键名不含 texture/opengl/context、输出端口类型不含 texture 的红线断言到位。
- 状态提交一致性：schema_version + content_hash 过期校验（GRAPH_STALE）有真实负例。
- 输入验证：空 request_id、空输入 + 非 WHOLE 拒绝覆盖到位。
- 类型注解：完整。
