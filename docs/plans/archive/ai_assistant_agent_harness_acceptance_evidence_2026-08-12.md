# AI Assistant Agent Harness 验收证据记录

日期：2026-08-12  
范围：B0–B5 的本地实现收口与发布证据边界
原则：自动化测试、离线 contract、真实 provider 和人工 GUI 验收分开记录；没有真实环境证据的项目不标记为完成。

## 已验证

| 证据 | 命令或来源 | 结果 | 结论 |
| --- | --- | --- | --- |
| 核心回归 | `python3 -m pytest tests/edit/ai_editing src/features/ai_harness/tests tests/integration -qq -rs` | **1889 passed, 2 skipped**；跳过项为未安装 RapidOCR 的真实 OCR 集成、默认关闭的真实生成 provider smoke | 离线回归通过，真实模型/provider 仍按 opt-in 处理 |
| B0 可用性诚实性 | `tests/edit/ai_editing/test_capability_status.py`、`test_model_pack_manager.py` | profile 未经连接验证时为 `unverified`；所有 placeholder SHA 都不能作为可用模型包 | 不再把配置或空目录冒充模型/云能力可用 |
| B1 字幕本地化 | `src/features/ai_harness/tests/test_p1_host_wiring.py`、`test_p2_wiring.py` | 只有已物化并导入 Media Pool 的 TTS 音频才生成 `ADD_CLIP`；缺少时给出诊断 | 不再以 review note 或远端 URL 代替音频时间线操作 |
| B2 视觉语义 | `tests/edit/ai_editing/test_visual_semantics.py`、`test_analysis.py` | 非 demo caption/tag/embedding provider 以镜头首/中/尾帧输出 untrusted evidence；缓存、embedding 和索引可清理/重建 | 视觉语义已进入 AnalysisService/SemanticIndex；无已验证模型会明确失败 |
| B4 Provider Pack contract | `python3 -m pytest tests/integration/generation -q` | contract mock 覆盖 create → poll → cancel/resume → materialize → Media Pool import → provenance；默认真实 provider smoke 跳过 | 离线产品链路通过 |
| B5 eval corpus/rubric | `tests/edit/ai_editing/test_eval_corpus.py` | corpus 使用 opaque fixture id；缺指标或低于阈值时 fail closed | 评测合同通过 |
| 发布证据门 | `tests/edit/ai_editing/test_release_evidence.py` | 缺少 corpus、rubric、性能、隐私、kill switch、可访问性、成本/取消回收或回滚证据时不可发布 | 发布门通过 |
| B3 MCP 本机 transport | `python3 -m pytest tests/integration/mcp -q`、`tests/edit/ai_editing/test_toposctl.py` | 当前 OS 用户 Unix socket（0700 目录、0600 socket）、stdio proxy、`toposctl --socket` 与同一 ToolFacade 均通过；write 工具隐藏且不能由 CLI 自行授权 | 已有实际本机 transport，不监听 TCP/公网 |
| 性能/故障结构 | `tests/edit/ai_editing/test_delivery_benchmark.py`, `test_encode_benchmark.py`, `test_release_test_matrix.py` | probe/编码报告保留 p50/p95 和失败码；质量门故障矩阵全部阻断；真实证据缺失会被矩阵标出 | 证据结构和 fail-closed 规则通过 |
| 生成完整性与取消 | `tests/edit/ai_editing/test_media_materialization_pipeline.py`, `test_http_generation_provider.py`, `test_generation_provider_pack.py` | SHA-256/MIME/ffprobe 失败闭环；Job 取消可传到 HTTP/Provider Pack 并请求远端取消 | 生成候选不会以损坏媒体或幽灵任务进入后续流程 |
| B4 Provider Pack 恢复/超时 | `tests/edit/ai_editing/test_generation_provider_pack.py` | checkpoint 仅保存脱敏 resume metadata；允许的最后一次 poll 若已完成会直接返回；超时会尝试取消远端任务 | 不会因本地 timeout 产生无主远端任务，也不把 prompt/secret 写入 checkpoint |
| Skill/Job 真实装配边界 | `tests/edit/ai_editing/test_llm_role_model_client.py`, `test_tool_facade.py`, `test_edit_api.py` | 已配置 profile 进入受控 RoleExecutor；模型失败明确标记 fallback；EditSession/ToolFacade/导出共用 JobRegistry | 本地可自动验证的执行与任务控制链路已封口 |

## 默认不执行的真实验收

真实 provider 测试必须使用隔离账号、短素材和临时输出目录，且不进入默认 CI：

```bash
TOPOS_RUN_REAL_GENERATION=1 \
TOPOS_REAL_GENERATION_ENDPOINT='https://<isolated-endpoint>' \
TOPOS_REAL_GENERATION_SECRET='<secret-from-secure-shell>' \
python3 -m pytest tests/integration/generation -m real_provider -q
```

测试只把 secret 放进内存 credential store，不打印、不序列化、不写入工程。当前工作区没有执行这项外部调用，因此不能宣称真实 Seedance/云 provider 已验收。真实结果应另存为脱敏 `evidence_ref`，并补 provider、模型版本、费用、取消回收和输出重开信息。

以下项目仍需要测试机、真实受控账号或人工操作记录；它们是发布阻断项，不是可以由本地 mock 修复的代码缺陷：

- 干净机器上的真实 ASR/OCR/LLM 模型包安装、损坏重试和离线退化；
- 真实 LLM 断网/超时，以及 Dock、Brief、帮助和状态修复卡的 GUI 行为；
- 外部桌面 MCP 客户端互操作、GUI write approval、撤销连接和关闭工程后的会话失效；
- 真实生成候选的上传同意、费用提示、缩略图/proxy、Media Pool 重开和商用权利判断；
- 读屏、键盘焦点顺序、对比度和灰度 cohort 的真实回滚演练。

## 发布前填写项

真实验收完成后，发布人应将以下引用填入 `ReleaseEvidenceBundle`，不能使用 demo 或 contract mock 代替：

1. `eval_report_id`、`eval_corpus_version`、`rubric_report_id`；
2. 性能报告（VFR/4K/长转录/并发 Job，含 p50/p95、内存/显存、缓存和 orphan worker）；
3. 安全/隐私审计、kill switch 演练、telemetry opt-in 和数据删除记录；
4. 可访问性检查、真实 provider 成本与取消回收报告；
5. 灰度 cohort 失败率/撤销率/人工评分、回滚 runbook 和已知限制。

在这些引用齐全前，发布门应保持 `releasable=False`。
