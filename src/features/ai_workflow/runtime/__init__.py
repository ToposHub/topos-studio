"""P9 AI Workflow 运行时接线（plan §10.7 P9）。

本包把 :class:`WorkflowCompiler` 的编译产物接进 :class:`HarnessRuntime`，并为
AI Workflow 画布提供合成（fake）执行服务 + node.* 事件源 + QThread controller。

红线（plan §1.4 / §10.1）：

- **不 import** ``moderngl`` / ``composite.core``（源码扫描）。
- **不传 GPU texture**（端口只传 artifact_type 字符串）。
- **不直接写 timeline**（运行经 ``WorkflowCompiler`` → ``invoke_compiled`` →
  ``HarnessRuntime``；本包的执行服务是合成 fake，无 timeline 副作用）。
- **fake provider 先行**（plan §10.1）：:class:`FakeWorkflowService` 明确标注
  "合成运行"，不假装是真实 LLM/Comfy 推理。
"""
