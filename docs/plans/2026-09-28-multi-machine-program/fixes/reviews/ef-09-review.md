# EF-09 复查（GLM5.3）—— src/features/ai_workflow/tests 18 条（测试质量批）

- 批次：EF-09 ｜ 提交：`cd54778609fc23d03e2eeca04f8e3c5730e2629a`（dev）
- 白名单：`src/features/ai_workflow/tests/`（源码侧测试目录，即本批的"模块目录"）∪ `tests/` ∪ `fixes/reports/ef-09.md`
- 复查日期：2026-10-01 ｜ 只读复查；未运行 pytest（中心已复跑，整目录 453 全绿）

## §6 八项检查结论

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat`：12 个改动文件全部在 `src/features/ai_workflow/tests/` + 报告自身。未触碰任何生产代码（src/ 非 tests 路径零改动）、未触碰 REMEDIATION_PLAN.md |
| 2 | 真实修复 | **通过（18/18 CONFIRMED）** | 本批的"修复"就是测试本身。逐条核对改后断言**真的可证伪**（非改弱），重点抽查 eval-exec 熔断哨兵 / 凭据白名单 / 离线哨兵 / 死分支重写 / 空转守卫，全部成立（逐条见下表） |
| 3 | GLSL/HDR 红线 | **不适用** | 测试目录无 GLSL/HDR/色彩矩阵内容；diff 无相关触碰 |
| 4 | 并发 | **不适用** | 无线程/信号/Qt 并发改动（23621 的子进程导入测试为正向验证，timeout=60 防挂起） |
| 5 | 资源 | **通过** | 无资源对象改动 |
| 6 | 异常 | **通过** | 新增行仅 1 处 `except ValueError:`（test_composite_adapter.py:359 附近，显式类型负例控制流——"构造期拒绝也满足红线"的双防线语义，非吞错）；无裸 `except:` |
| 7 | 测试质量（本批核心） | **通过** | diff 删除的 9 条断言逐一核对均有**等强或更强**替代（见下"删除断言核对"）；新增 52 条断言；未发现恒真/弱化。报告自述的 Check A/B 回退敏感性验证方法正确 |
| 8 | 综合 | **PASS** | 见最终结论 |

## 逐 ID 核实表（全部 18 条，重点=可证伪性）

| ID | 判定 | 证据（当前 HEAD） |
|----|------|------------------|
| AUD-23503 | CONFIRMED | test_comfyui_client.py:384 `_inject_job` 唯一注入 helper（内含 `_JobRecord` import + 私有耦合注释 + "新测试优先 submit 路由"提示）；10 处调用点（:140/:159/:171/:181/:199/:210/:219/:235/:243/:259），散落注入收敛完成 |
| AUD-23505 | CONFIRMED | `TestProtocolConsistency::test_real_implements_fake_surface`：`inspect.signature` 逐参数（name/kind/default）对照 Fake/Real 的 submit/get_job/cancel/job_outputs 四方法 + `job_outputs` 三元组行为形状断言——参数漂移（如丢 keyword-only `workflow_snapshot_hash`）会失败 |
| AUD-23510 | CONFIRMED | test_comfyui_converter.py:100-140 `_install_execution_sentinels`：eval/exec/os.system 触达即 AssertionError 熔断、`__import__` 记录**新加载**模块（`loaded_at_install` 差集防 MonkeyPatch 框架噪音，inspect 预热）；:311-345 测试注入恶意 EvalNode 后断言 `not imports_seen` 且模式匹配不受影响——**红线真可证伪** |
| AUD-23511 | CONFIRMED | test_comfyui_converter.py:84 `_generation_registry()` + :91-97 `@pytest.fixture converter`；206 行保留一处 registry 本体断言的合理用法 |
| AUD-23520 | CONFIRMED | test_comfyui_executor.py `test_server_tamper_marks_success_but_reports_mismatched_actual_hashes`：改名匹配行为；断言报告 hash `sha256:deadbeef` vs 实际 hash 表 `{"bad.png": "sha256:tampered-content"}` **可观测不一致**（经公共协议 `server.job_outputs`），tamper 回归立即可见 |
| AUD-23521 | CONFIRMED | test_comfyui_executor.py:122 `_track_submitted_job_ids`（包装实例 submit 捕获 job id）；:567/:588 两测试改走 `server.get_job(submitted[0]).state`，文件内不再有 `server._jobs` 穿透（grep 核实仅剩注释提及） |
| AUD-23530 | CONFIRMED | test_comfyui_export.py：fixture 主动注入 `api_key`/`authorization`/`credential_ref` + 秘密值 `sk-live-SUPER-secret-42`；断言秘密值不出现在导出 JSON、7 个凭据形状键不在小写化输出、`_topos_meta` 键集合 ⊆ 封闭白名单、其余字段 round-trip 完好——**输入含凭据后红线非空转** |
| AUD-23531 | CONFIRMED | test_comfyui_export.py：`socket.socket`/`urllib.request.urlopen` 哨兵化（触达即 AssertionError），导出两次幂等断言保留——"不联网"从推断变为可证伪 |
| AUD-23541 | CONFIRMED | test_comfyui_importer.py：死分支（`if result.allowed:` 后跟 `allowed is False`）与同义反复 hasattr 断言已删；重写为 `allowed is False` + `deny_code == MISSING_NODE` + `to_dict()` 可序列化三段实断言；docstring 交叉引用 executor 侧强制点（test_submit_requires_preflight_pass） |
| AUD-23552 | CONFIRMED | test_comfyui_models.py:359-410：局部实现同款哨兵（eval/exec/os.system 熔断 + `__import__` 差集记录），注入 EvilNode 后断言 `not fresh_imports` + 元数据断言（evil/unknown 标记）全保留 |
| AUD-23560 | CONFIRMED | test_composite_adapter.py `test_input_must_be_asset_refs_not_textures`：正例升级为 wire format 观测（`json.loads(json.dumps(req.to_dict()))` 后仍是字符串 ID 列表）；负例 texture 形状对象——构造期 `ValueError` 或序列化 `TypeError` 双防线，"静默放行并原样携带"会失败 |
| AUD-23561 | CONFIRMED | test_composite_adapter.py:34-48 模块级 import（registry/NodeExecutorId/NodeSpecRegistry）+ :59/:67 模块级 `_graph_ref()`/`_render_service()` helper；`@pytest.fixture composite_registry` 在位 |
| AUD-23621 | CONFIRMED | test_node_canvas_protocol.py：**子进程** + `sys.meta_path.insert(0, _QtBlocker())`（PyQt6 前缀全屏蔽、find_spec 抛 ImportError）真实重新执行 `src.shared.node_canvas.graph_ui_protocol` 导入，断言退出码 0 + `QT_FREE_IMPORT_OK`——绕开 `sys.modules` 缓存，Qt 无关性真被验证 |
| AUD-23660 | CONFIRMED | test_p6_acceptance.py：`assert result.succeeded, f"error={result.error_code}"` + `assert len(result.candidates) >= 1`，红线 for 循环不再可能空转（报告 Check B 已演示弱守卫形式在探针故障下静默通过、修复形式正确变红） |
| AUD-23670 | CONFIRMED | test_p7_acceptance.py：CONVERTED 分支 `assert len(outcome.findings) >= 1` + target 全部落在受控集合；OPAQUE 分支 verdict + 零 findings——两分支都不可静默通过 |
| AUD-23801 | CONFIRMED | test_workflow_run_bridge.py:99 `test_bridge_closure_forwards_payload`（payload={"stage":"s1"} 透传 + node_id 并入断言）+ :121 `test_bridge_closure_unknown_run_is_silent_noop`（不抛 + 订阅者零事件）；模块 docstring 签名修正 |
| AUD-23804 | CONFIRMED | test_workflow_validator.py `test_apply_with_user_approval_chain_has_no_token_error`：propose_track → user.approval → apply 合法链（approval_token 边来自 approval 节点），断言 `MISSING_APPROVAL_TOKEN not in codes` 且整图零 error——validator 假阳性拒绝合法图会失败 |
| AUD-23806 | CONFIRMED | test_workflow_validator.py `test_exposed_inputs_satisfy_required_port`：同一图正反对照——无 exposed → `MISSING_REQUIRED_INPUT in codes`；`("host_assets","asr","assets")` → 该码消失且 `codes == []`（分支可证伪）；"锁定轨道"在模块 docstring 标注 deferred（apply 期 revision/lock 承担，诚实处理） |

## 删除断言核对（防"改弱过测试"）

diff 共删除 9 条断言，逐一核对替代情况：

| 删除的断言 | 替代 | 强度 |
|-----------|------|------|
| `assert callable(getattr(fake/client, method))` ×2 | inspect.signature 逐参数对照 | 更强 |
| `next(iter(server._jobs.values())).state == CANCELLED` ×2 | `server.get_job(submitted[0]).state`（公共协议，等值） | 等强（去私有穿透） |
| `assert isinstance(a, str)` | wire format JSON 序列化观测 + texture 负例 | 更强 |
| `assert hasattr(mod, "GraphUiProtocol")` / `"DEFAULT_VISUAL_TOKENS"` | 子进程无 Qt 重导入 + 符号断言（子进程内） | 更强 |
| `"timeline"/"edit_plan" not in k` 红线循环 | 原断言保留 + 前置 `assert result.succeeded` + `candidates >= 1` | 更强（不再可空转） |
| `f.target_spec_id in (...)` | 原断言保留 + `findings >= 1` 前置 | 更强 |

无一处"删断言保绿"。新增断言 52 条。

## 红线扫描发现

- 无裸 `except:`；唯一新增 `except ValueError:` 为显式类型负例控制流（非吞错）。
- 未 mock 任何被测对象本身（仅 mock 外部通道：builtins eval/exec、socket/urllib、submit 捕获包装）——与报告声明一致。
- 哨兵设计的一个亮点：`__import__` 用"安装时已加载快照差集"而非直接熔断，避免 pytest 失败渲染懒 import 打成 INTERNALERROR——工程上正确。
- Note（非 ISSUE）：AUD-23621 子进程测试依赖 `parents[4]` 推导工程根并以 `cwd=project_root` 导入 `src.*`，与仓库既有 src-layout 用法一致；若未来目录层级变动该测试会报错（fail-loud，可接受）。

## 回退验证建议（本批语义：注入生产故障 → 新测试应变红）

1. **AUD-23530** — 故障注入点：`src/features/ai_workflow/adapters/comfyui_export.py:175-177`，把 `_CREDENTIAL_KEYS` 元组中的 `"api_key"` 临时删掉（生产凭据剥离回归）。
   - 应失败：`src/features/ai_workflow/tests/test_comfyui_export.py::TestExportRedlines::test_exported_json_has_no_credentials`（`secret_value not in result.api_format_json` / `api_key not in low` 立即命中）
2. **AUD-23660** — 故障注入点：`src/features/ai_workflow/tests/test_p6_acceptance.py` 测试体内把 `server.auto_complete_with_image = True` 临时改 `False`（fetch_outputs 失败，模拟探针故障——报告 Check B 已实测此路径）。
   - 应失败：`src/features/ai_workflow/tests/test_p6_acceptance.py::TestGenerationGuardrails::test_no_candidate_carries_timeline_write_intent`（旧弱守卫形式则静默通过，复现审计指控；新 `assert result.succeeded` 变红并报 `error_code`）

## 最终结论

**PASS**（0 Critical / 0 ISSUE / 1 note）

- 18/18 测试质量缺陷真实修复，全部改强不改弱；越界零（生产代码零触碰）。
- Note：23621 的 `parents[4]` 层级推导对目录重构敏感（fail-loud，无需行动）。
- 建议中心执行 `check_fixes.py --mark`（18 条全 FIXED）。
