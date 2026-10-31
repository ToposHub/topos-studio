"""P9.1 测试：FakeWorkflowService（合成 node.* 事件源）。

> runtime 当前从不发 ``node.*`` 事件（``_emit`` 永远 ``node_id=""``）。
> 本批新建纯 Python 的 :class:`FakeWorkflowService`，按拓扑序逐节点 sleep + 回调
> 发 ``started/succeeded`` 事件，作为 Run Drawer 节点状态色的**合成**数据源
> （fake-provider-first；真实 LLM/Comfy 留 P9+ 真实环境）。

验证契约：

- ``run_workflow`` 按节点拓扑序触发 ``started → succeeded`` 事件序列。
- ``emit_node_event`` 回调签名 ``(kind, node_id) -> None``，纯函数注入。
- cancel 中途：``cancel_requested`` 返回 True 后，未 started 的节点跳过；
  正在运行的节点不发 succeeded；**取消的运行不产出 artifact**（AUD-23400）。
- 返回 ``WorkflowResult`` 字段兼容 ``LegacyHarnessAdapter._record_workflow_result``
  的 getattr 探测（``diagnostics`` / ``degraded`` / ``artifact_id`` / ``variants`` /
  ``missing_evidence`` / ``degradation``）。
- 红线：源码不 import moderngl / composite.core；不写 timeline。
- 诚实标注：类 docstring 标"合成运行"。
"""
import ast
import os
import time
from typing import Any, List, Set, Tuple

import pytest

from src.features.ai_workflow.runtime import fake_workflow_service as _RUNTIME_MODULE


# ---------------------------------------------------------------------- #
# 红线：源码扫描（moderngl / composite.core / timeline 写）
# ---------------------------------------------------------------------- #

# AUD-23600：通过 runtime 包自身定位源码目录（相对路径 ``..`` 拼接在 tests
# 目录被移动/复制后会静默落空，导致全部红线断言空转通过）。
_SRC_DIR: str = os.path.dirname(_RUNTIME_MODULE.__file__ or "")


def _runtime_source_paths() -> List[str]:
    """runtime/*.py 全量路径；目录缺失/为空时 fail-fast（AUD-23600）。"""
    if not _SRC_DIR or not os.path.isdir(_SRC_DIR):
        pytest.fail(f"runtime 源码目录缺失，红线扫描无法执行：{_SRC_DIR!r}")
    paths = [
        os.path.join(_SRC_DIR, name)
        for name in sorted(os.listdir(_SRC_DIR)) if name.endswith(".py")
    ]
    if not paths:
        pytest.fail(f"runtime 源码目录无 .py 文件，红线扫描无法执行：{_SRC_DIR!r}")
    return paths


def _scan_sources() -> str:
    """聚合 runtime/*.py 源码文本（供文本级红线断言）。"""
    chunks: List[str] = []
    for path in _runtime_source_paths():
        with open(path, "r", encoding="utf-8") as f:
            chunks.append(f.read())
    return "\n".join(chunks)


def _collect_imports() -> Set[str]:
    """AST 扫描 runtime/*.py 的全部 import 模块名（AUD-23601）。

    子串检查（``"import moderngl" not in source``）可被 ``from moderngl
    import Context``、``import src.features.composite.core`` 等形式绕过；
    AST 解析 ``Import`` / ``ImportFrom`` 后按模块名精确判定，无盲区。
    """
    modules: Set[str] = set()
    for path in _runtime_source_paths():
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    modules.add(node.module)
    return modules


def _imports_root(modules: Set[str], root: str) -> bool:
    """是否 import 了 ``root`` 本身或其任何子模块（``root.`` 前缀）。"""
    return any(
        m == root or m.startswith(root + ".") for m in modules
    )


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：runtime 源码不 import moderngl / composite.core（任何导入形式）。"""
    imports = _collect_imports()
    assert not _imports_root(imports, "moderngl"), (
        f"runtime 包不允许 import moderngl（任何形式）；发现："
        f"{sorted(m for m in imports if 'moderngl' in m)}"
    )
    assert not _imports_root(imports, "src.features.composite.core"), (
        f"runtime 包不允许 import composite.core（任何形式）；发现："
        f"{sorted(m for m in imports if 'composite.core' in m)}"
    )
    source = _scan_sources()
    assert "TextureResult" not in source, "不传 GPU texture（plan §1.4.1）"


def test_source_does_not_write_timeline() -> None:
    """红线：runtime 源码不直接写 timeline（plan §1.4.3）。"""
    source = _scan_sources()
    assert "timeline_write" not in source
    assert "CommandStack" not in source
    assert "commit_macro" not in source


def test_fake_service_docstring_honest() -> None:
    """诚实标注：FakeWorkflowService docstring 含'合成'。"""
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    assert "合成" in (FakeWorkflowService.__doc__ or ""), (
        "FakeWorkflowService docstring 必须标注为'合成运行'（fake provider 诚实）"
    )


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


class _FakeWorkflowInput:
    """最小 wf_input：携带 node_ids 列表（拓扑序）。

    真实 ``invocation_to_workflow_input`` 产的 wf_input 结构更复杂，但
    FakeWorkflowService 只关心节点 id 列表（从 ``invocation.inputs`` 的
    ``node_<id>_params`` keys 反推）。这里用一个简单 stub 验证核心契约。
    """

    def __init__(self, *, node_ids: Tuple[str, ...] = ()) -> None:
        self.node_ids = node_ids


@pytest.fixture
def fake_service() -> Any:
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    return FakeWorkflowService(node_duration_s=0.0)


# ---------------------------------------------------------------------- #
# 事件序列
# ---------------------------------------------------------------------- #


def test_run_workflow_emits_started_succeeded_per_node(fake_service: Any) -> None:
    """每个节点触发 started → succeeded 一对事件，按拓扑序。"""
    events: List[Tuple[str, str]] = []

    def emit(kind: str, node_id: str) -> None:
        events.append((kind, node_id))

    wf_input = _FakeWorkflowInput(node_ids=("n1", "n2", "n3"))
    fake_service.run_workflow(
        wf_input, emit_node_event=emit, cancel_requested=lambda: False,
    )
    assert events == [
        ("started", "n1"), ("succeeded", "n1"),
        ("started", "n2"), ("succeeded", "n2"),
        ("started", "n3"), ("succeeded", "n3"),
    ], f"事件序列应严格按拓扑序；实际：{events}"


def test_run_workflow_result_fields_present(fake_service: Any) -> None:
    """返回 WorkflowResult 字段齐全，兼容 _record_workflow_result getattr 探测。"""
    wf_input = _FakeWorkflowInput(node_ids=("n1",))
    result = fake_service.run_workflow(
        wf_input, emit_node_event=None, cancel_requested=lambda: False,
    )
    # _record_workflow_result 探测的字段（getattr 带 default）
    assert hasattr(result, "diagnostics")
    assert hasattr(result, "degraded")
    assert hasattr(result, "artifact_id")
    assert hasattr(result, "variants")
    assert hasattr(result, "missing_evidence")
    assert hasattr(result, "degradation")
    # 成功运行：diagnostics 空、degraded False
    assert tuple(result.diagnostics) == ()
    assert result.degraded is False


def test_run_workflow_produces_artifact(fake_service: Any) -> None:
    """合成运行成功后产一个 artifact_id（非 None），让 _record_workflow_result 记 ArtifactRef。"""
    wf_input = _FakeWorkflowInput(node_ids=("n1", "n2"))
    result = fake_service.run_workflow(
        wf_input, emit_node_event=None, cancel_requested=lambda: False,
    )
    assert result.artifact_id is not None
    assert isinstance(result.artifact_id, str) and result.artifact_id


# ---------------------------------------------------------------------- #
# Cancel
# ---------------------------------------------------------------------- #


def test_cancel_mid_run_skips_remaining_nodes(fake_service: Any) -> None:
    """cancel 在 n1 运行后生效：n1 仍 succeeded，n2/n3 不发事件。

    AUD-23400：取消的运行**不产出 artifact**（fake provider 诚实红线——
    旧行为取消后仍返回非空 fake artifact_id，使取消的 run 被记成成功）。"""
    events: List[Tuple[str, str]] = []

    def emit(kind: str, node_id: str) -> None:
        events.append((kind, node_id))

    def cancel_requested() -> bool:
        # n1 succeeded 后置 cancel=True
        return len(events) >= 2

    wf_input = _FakeWorkflowInput(node_ids=("n1", "n2", "n3"))
    result = fake_service.run_workflow(
        wf_input, emit_node_event=emit, cancel_requested=cancel_requested,
    )
    # n1 完整（started+succeeded），n2/n3 不出现
    assert ("started", "n1") in events
    assert ("succeeded", "n1") in events
    assert "n2" not in [nid for _, nid in events], "cancel 后不应再启动 n2"
    assert "n3" not in [nid for _, nid in events]
    # 取消结果不带 artifact，且带可观测的 CANCELLED 诊断
    assert result.artifact_id == "", "取消的运行不得产出 artifact（AUD-23400）"
    assert any(d.get("code") == "CANCELLED" for d in result.diagnostics)


def test_cancel_before_first_node_emits_nothing(fake_service: Any) -> None:
    """一开始就 cancel：无任何节点事件，也不产出 artifact（AUD-23400）。"""
    events: List[Tuple[str, str]] = []
    result = fake_service.run_workflow(
        _FakeWorkflowInput(node_ids=("n1", "n2")),
        emit_node_event=lambda k, nid: events.append((k, nid)),
        cancel_requested=lambda: True,
    )
    assert events == []
    assert result.artifact_id == ""
    assert any(d.get("code") == "CANCELLED" for d in result.diagnostics)


# ---------------------------------------------------------------------- #
# 反推节点 id（从 wf_input）
# ---------------------------------------------------------------------- #


def test_derive_node_ids_from_empty_input_falls_back_to_single_workflow_node(
    fake_service: Any,
) -> None:
    """wf_input 无节点信息时退化为单节点 'workflow'（与 _record_workflow_result
    的 producer_node_id='workflow' 一致）。"""
    events: List[Tuple[str, str]] = []
    fake_service.run_workflow(
        _FakeWorkflowInput(node_ids=()),
        emit_node_event=lambda k, nid: events.append((k, nid)),
        cancel_requested=lambda: False,
    )
    node_ids = {nid for _, nid in events}
    assert node_ids == {"workflow"}, (
        f"无节点信息时应退化为 'workflow' 单节点；实际：{node_ids}"
    )


def test_node_duration_s_sleeps_between_events() -> None:
    """node_duration_s > 0 时，started 与 succeeded 之间确有 sleep。"""
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    svc = FakeWorkflowService(node_duration_s=0.05)
    timestamps: List[Tuple[str, float]] = []

    def emit(kind: str, node_id: str) -> None:
        timestamps.append((kind, time.monotonic()))

    wf_input = _FakeWorkflowInput(node_ids=("n1",))
    svc.run_workflow(
        wf_input, emit_node_event=emit, cancel_requested=lambda: False,
    )
    started_t = next(t for k, t in timestamps if k == "started")
    succeeded_t = next(t for k, t in timestamps if k == "succeeded")
    assert succeeded_t - started_t >= 0.04, "node_duration_s 应在 started/succeeded 间 sleep"
