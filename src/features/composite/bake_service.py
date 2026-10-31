"""Render-in-Place / Bake Subtree service (A6).

Bakes a node's evaluated output to an image sequence and rewrites the graph
so downstream consumers read the baked sequence from an ``image_input``
node, while the original subtree stays in the graph (disabled, renamed)
for a one-click Unbake.

The rendering itself is injected as a callback ``render_fn(node_id, frame,
out_path) -> bool`` so the service is fully testable without a GL context;
the UI wires it to a CompositeExportRenderer on a worker thread.

Graph rewrite (one undoable step, mirroring the tracker-bake pattern)::

    before:  [upstream] → TARGET → [consumers...]
    after:   [upstream(disabled, "_baked")] ... BAKE_INPUT(seq path) → [consumers...]

Undo restores the original graph exactly (before/after full-graph dicts are
captured as ``dict`` payloads and applied via :func:`NodeGraph.from_dict`
by the caller-provided appliers).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

BAKED_TITLE_SUFFIX = " (baked)"
BAKE_INPUT_PREFIX = "bake_input_"
# AUD-4603: bake-node parameter remembering the target's pre-bake user label
# ("" == no user label).  Lets Unbake restore the label verbatim — including
# the None case where i18n translation / instance numbering must keep owning
# the display title — even across save/load cycles.
BAKE_ORIGINAL_USER_LABEL_PARAM = "_bake_original_user_label"
#: F7.3：烘焙绑定源签名——目标节点及其上游锥的参数指纹。烘焙后上游
#: 参数变化时签名不匹配 → bake 过期（"更新原图时不悄悄播放旧烘焙"）。
BAKE_SOURCE_SIGNATURE_PARAM = "_bake_source_signature"


def _bake_source_signature(graph: Any, target_id: str) -> str:
    """目标节点 + 其上游锥（全部仍携带参数，仅被禁用）的参数指纹。"""
    import hashlib
    import json as _json

    upstream: "list[str]" = [target_id]
    seen = {target_id}
    frontier = [target_id]
    connections = list(getattr(graph, "connections", []))
    while frontier:
        current = frontier.pop()
        for conn in connections:
            if (getattr(conn, "dst_node_id", None) == current
                    and conn.src_node_id not in seen):
                seen.add(conn.src_node_id)
                upstream.append(conn.src_node_id)
                frontier.append(conn.src_node_id)
    payload = []
    for node_id in sorted(upstream):
        node = graph.get_node(node_id)
        if node is None:
            continue
        payload.append({
            "id": node_id,
            "type": type(node).__name__,
            "params": _json.dumps(
                {k: v for k, v in dict(getattr(node, "parameters", {})
                                       or {}).items()
                 if not k.startswith("_")},
                sort_keys=True, default=str),
            "enabled": bool(getattr(node, "enabled", True)),
        })
    # M08.1（S10）：锥内连线边（dst 在锥内；含端口序——交换 Merge A/B
    # 端口即变化）。此前只哈希节点集与参数，端口交换后签名不变，陈旧
    # 烘焙不被检测。旧工程已记录签名按新形状重算必不等（判 stale 走
    # 保守方向：重新烘焙，不冒充新鲜）。
    edges = sorted(
        (str(getattr(conn, "src_node_id", "")),
         str(getattr(conn, "src_port_name", "")),
         str(getattr(conn, "dst_node_id", "")),
         str(getattr(conn, "dst_port_name", "")))
        for conn in connections
        if getattr(conn, "dst_node_id", None) in seen)
    payload.append({"id": "__connections__", "edges": edges})
    digest = hashlib.sha256(
        _json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return digest


def audit_bake_staleness(graph: Any) -> "list[dict]":
    """M08.1 · 图内全部烘焙节点的陈旧性审计（预览/导出/重开共用）。

    对每个 ``bake_input_*`` 节点调用 :func:`bake_is_stale`，返回结构化
    条目（不修改图、不抛错）：

    - ``{"bake_node_id", "target_id", "stale": True/False/None, "reason"}``
    - ``stale=None`` = 非烘焙节点/无签名（旧工程烘焙——不冒称新鲜，
      也不冒称过期）。

    消费方（渲染入口/导出预检）据此显式提示"正在消费陈旧烘焙"（UI
    徽标/阻断策略由调用方决定），不再静默播放旧烘焙。
    """
    from types import SimpleNamespace as NS

    audit: "list[dict]" = []
    nodes = getattr(graph, "nodes", None)
    if not nodes:
        return audit
    # M08.1：兼容 GraphSnapshot（nodes 为 Dict[str, NodeSnapshot]、
    # connections 为 List[ConnectionSnapshot]——无 get_node/属性连线），
    # 适配成 bake_is_stale 需要的图形态
    probe_graph = graph
    if not callable(getattr(graph, "get_node", None)):
        class _SnapShim:
            def __init__(self, snap):
                self._nodes = snap.nodes
                self.connections = [
                    NS(src_node_id=c.src_node_id,
                       src_port_name=c.src_port_name,
                       dst_node_id=c.dst_node_id,
                       dst_port_name=c.dst_port_name)
                    for c in (snap.connections or ())
                ]
            def get_node(self, node_id):
                snap = self._nodes.get(node_id)
                if snap is None:
                    return None
                return NS(parameters=dict(snap.parameters or {}),
                          enabled=bool(getattr(snap, "enabled", True)))
        probe_graph = _SnapShim(graph)
    for node_id in list(nodes.keys()):
        if not str(node_id).startswith(BAKE_INPUT_PREFIX):
            continue
        target_id = str(node_id)[len(BAKE_INPUT_PREFIX):]
        try:
            stale = bake_is_stale(probe_graph, str(node_id))
        except Exception as exc:  # noqa: BLE001 — 审计不因单节点失败中断
            audit.append({
                "bake_node_id": str(node_id), "target_id": target_id,
                "stale": None, "reason": f"审计失败：{exc}"})
            continue
        if stale is None:
            reason = "旧工程烘焙（无签名）——不冒称新鲜"
        elif stale:
            reason = "上游已变化（参数/启用态/连线/端口）——烘焙过期"
        else:
            reason = "新鲜"
        audit.append({
            "bake_node_id": str(node_id), "target_id": target_id,
            "stale": stale, "reason": reason})
    return audit


def bake_is_stale(graph: Any, bake_node_id: str) -> "bool | None":
    """F7.3：烘焙是否过期（上游参数/启用态变化）。

    Returns:
        True=过期（上游已变，烘焙不应再被信任）；False=新鲜；
        None=非烘焙节点/无签名（旧工程烘焙，不冒称）。
    """
    bake_node = graph.get_node(bake_node_id)
    if bake_node is None or not bake_node_id.startswith(BAKE_INPUT_PREFIX):
        return None
    recorded = bake_node.parameters.get(BAKE_SOURCE_SIGNATURE_PARAM)
    if not recorded:
        return None
    target_id = bake_node_id[len(BAKE_INPUT_PREFIX):]
    if graph.get_node(target_id) is None:
        return None  # 目标已删（unbake 语义另走 plan_unbake）
    return _bake_source_signature(graph, target_id) != recorded


@dataclass
class BakePlan:
    """Everything the rewrite needs, resolved up-front."""

    target_id: str
    bake_node_id: str
    sequence_pattern: str      # e.g. /cache/name.####.exr
    frame_start: int
    frame_end: int
    original_title: str
    # AUD-4603: the target's user_label *including None* at plan time.  The
    # bake relabels the target via user_label, so unbake must restore the
    # exact prior value — comparing display titles against ``node_title``
    # froze i18n translations / instance numbers into permanent renames.
    original_user_label: Optional[str] = None
    before_graph: Dict[str, Any] = field(default_factory=dict)
    after_graph: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BakeResult:
    ok: bool
    plan: Optional[BakePlan] = None
    written: List[str] = field(default_factory=list)
    error: str = ""


def build_sequence_path(output_dir: str, base_name: str,
                        frame: int, pad: int = 4, ext: str = "exr") -> str:
    return os.path.join(output_dir,
                        f"{base_name}.{str(frame).zfill(pad)}.{ext}")


def sequence_pattern_for(output_dir: str, base_name: str,
                         pad: int = 4, ext: str = "exr") -> str:
    """The ``####`` pattern image_input nodes understand."""
    return os.path.join(output_dir, f"{base_name}.{'#' * pad}.{ext}")


def _consumed_output_ports(graph: Any, node_id: str) -> List[str]:
    """Return the distinct output port names of ``node_id`` that have
    at least one downstream consumer."""
    return sorted({
        getattr(conn, "src_port_name", "")
        for conn in getattr(graph, "connections", [])
        if getattr(conn, "src_node_id", None) == node_id
    })


def _primary_output_name(target: Any) -> str:
    """The target's first output port (fallback "result")."""
    return (target.outputs[0].name
            if getattr(target, "outputs", None) else "result")


def plan_bake(graph: Any, target_id: str, output_dir: str,
              frame_start: int, frame_end: int,
              base_name: Optional[str] = None) -> BakePlan:
    """Resolve ids/titles and capture the before-graph for the rewrite."""
    node = graph.get_node(target_id)
    if node is None:
        raise ValueError(f"bake target not found: {target_id}")
    title = node.display_title or target_id
    primary_out = _primary_output_name(node)
    # AUD-4602: a bake produces ONE image sequence for the target's primary
    # output. Consumers on other output ports (keyer "matte", shuffle
    # secondary outs, ...) would be left pointing at the disabled target and
    # evaluate to empty — refuse early, before any frames are rendered.
    secondary_ports = [p for p in _consumed_output_ports(graph, target_id)
                       if p != primary_out]
    if secondary_ports:
        raise ValueError(
            f"bake target {target_id!r} has consumers on non-primary "
            f"output port(s) {secondary_ports}; bake renders the primary "
            f"output {primary_out!r} only")
    if base_name is None:
        base_name = f"bake_{target_id}"
    return BakePlan(
        target_id=target_id,
        bake_node_id=f"{BAKE_INPUT_PREFIX}{target_id}",
        sequence_pattern=sequence_pattern_for(output_dir, base_name),
        frame_start=int(frame_start),
        frame_end=int(frame_end),
        original_title=title,
        original_user_label=getattr(node, "user_label", None),
        before_graph=graph.to_dict(),
    )


def render_sequence(graph: Any, plan: BakePlan, output_dir: str,
                    base_name: str,
                    render_fn: Callable[[str, int, str], bool],
                    frames: Optional[Sequence[int]] = None,
                    pad: int = 4, ext: str = "exr",
                    is_cancelled: Optional[Callable[[], bool]] = None
                    ) -> Tuple[List[str], str]:
    """Render every frame of ``plan.target_id`` via ``render_fn``.

    ``render_fn`` receives (node_id, frame, out_path) and returns True on
    success.  Returns (written_paths, error); ``error`` is empty on full
    success, and any written files are still reported so the caller can
    clean them up.
    """
    written: List[str] = []
    if frames is None:
        frames = range(plan.frame_start, plan.frame_end + 1)
    for frame in frames:
        if is_cancelled and is_cancelled():
            return written, "cancelled"
        out_path = build_sequence_path(output_dir, base_name, frame, pad,
                                       ext)
        try:
            ok = bool(render_fn(plan.target_id, int(frame), out_path))
        except Exception as exc:  # renderer errors abort the bake
            return written, f"render failed at frame {frame}: {exc}"
        if not ok:
            return written, f"render failed at frame {frame}"
        written.append(out_path)
    if not written:
        return written, "no frames rendered"
    return written, ""


# ── M08.2 · Bake 协调器 ─────────────────────────────────────────────

_BAKE_RUN_COUNTER = {"n": 0}


@dataclass
class BakeExecutionResult:
    """M08.2 · 一次烘焙事务的结果（阶段化失败原因）。"""

    ok: bool
    plan: Optional[BakePlan] = None
    published: List[str] = field(default_factory=list)
    manifest_path: str = ""
    written: List[str] = field(default_factory=list)
    error: str = ""
    stage: str = ""   # freeze / render / verify / source_changed / publish


def _verify_frame_readable(path: str) -> bool:
    """逐帧实际解码校验（cv2 可读且非空；cv2 打不开时退化为非零大小）。"""
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return False
    try:
        import cv2
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        return img is not None and img.size > 0
    except Exception:  # noqa: BLE001 — cv2 不可用时按文件级校验
        return True


def _rmtree_safe(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def execute_bake(
    graph: Any, target_id: str, output_dir: str,
    frame_start: int, frame_end: int,
    render_fn: Callable[[str, int, str], bool],
    *, base_name: Optional[str] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    verify_fn: Optional[Callable[[str], bool]] = None,
    apply_rewrite: bool = True,
) -> BakeExecutionResult:
    """M08.2 · Bake 发布事务：产物完整前原图继续有效，不覆盖旧成功产物。

    阶段：
    1. **freeze**——冻结源签名（上游锥指纹）；
    2. **render**——写入独占临时目录（``.topos-partial-bake-<run>/``，
       只动本任务文件）；
    3. **verify**——逐帧实际解码校验 + 帧数核对 + VerifiedFrameManifest
       指纹（复用 sequence_exporter 既有 manifest，不另建平行实现）；
    4. **source_changed**——发布前重算签名，源图中途变化则中止（不发布
       陈旧内容）；
    5. **publish**——旧文件先整体移入 ``.topos-old-bake-<run>/``，新文件
       逐个就位，manifest 最后落位（提交标记）；任一步失败从旧目录回滚，
       恢复旧成功产物；
    6. rewrite——发布成功后才执行图改写（apply_bake_rewrite 一次 Undo）。

    失败/取消：临时与旧目录都只含本任务文件，清理不触碰其他内容；
    图未改写，原图（禁用前的求值）继续有效。
    """
    verify = verify_fn or _verify_frame_readable
    _BAKE_RUN_COUNTER["n"] += 1
    run = _BAKE_RUN_COUNTER["n"]
    temp_dir = os.path.join(
        output_dir, f".topos-partial-bake-{run}")
    old_dir = os.path.join(output_dir, f".topos-old-bake-{run}")
    os.makedirs(temp_dir, exist_ok=True)

    def _fail(stage: str, error: str, plan: Optional[BakePlan],
              written: List[str]) -> BakeExecutionResult:
        # 临时清理只动本任务目录
        _rmtree_safe(temp_dir)
        _rmtree_safe(old_dir)
        return BakeExecutionResult(
            ok=False, plan=plan, written=written,
            error=error, stage=stage)

    # 1. freeze
    try:
        frozen_sig = _bake_source_signature(graph, target_id)
    except Exception as exc:  # noqa: BLE001
        return _fail("freeze", f"源签名计算失败：{exc}", None, [])

    # 2. render（独占临时目录）
    try:
        plan = plan_bake(graph, target_id, output_dir, frame_start,
                         frame_end, base_name=base_name)
    except ValueError as exc:
        return _fail("freeze", str(exc), None, [])
    # 序列写入临时目录，发布时再就位
    temp_plan = BakePlan(
        target_id=plan.target_id, bake_node_id=plan.bake_node_id,
        sequence_pattern=sequence_pattern_for(
            temp_dir, os.path.basename(base_name or f"bake_{target_id}")),
        frame_start=plan.frame_start, frame_end=plan.frame_end,
        original_title=plan.original_title,
        original_user_label=plan.original_user_label,
        before_graph=plan.before_graph)
    written, error = render_sequence(
        graph, temp_plan, temp_dir,
        os.path.basename(base_name or f"bake_{target_id}"),
        render_fn, is_cancelled=is_cancelled)
    if error:
        return _fail("render", error, plan, written)

    # 3. verify（逐帧解码 + 帧数 + manifest 指纹）
    from src.shared.export.sequence_exporter import VerifiedFrameManifest
    manifest = VerifiedFrameManifest()
    expected = plan.frame_end - plan.frame_start + 1
    if len(written) != expected:
        return _fail("verify",
                     f"帧数不完整：期望 {expected} 实得 {len(written)}",
                     plan, written)
    for path in written:
        if not verify(path):
            return _fail("verify", f"帧校验失败（不可解码/空文件）：{path}",
                         plan, written)
        manifest.record(path)
    manifest_path = os.path.join(
        temp_dir, "bake_manifest.json")
    if not manifest.save(manifest_path):
        return _fail("verify", "manifest 写入失败（磁盘满？）", plan, written)

    # 4. source_changed（发布前源签名复核）
    if _bake_source_signature(graph, target_id) != frozen_sig:
        return _fail(
            "source_changed",
            "烘焙期间源图发生变化——中止发布（请重新烘焙）", plan, written)

    # 5. publish（旧文件→old_dir，新文件就位，失败回滚）
    final_paths: List[str] = []
    try:
        moved_old: List[str] = []
        for path in written:
            final = os.path.join(output_dir, os.path.basename(path))
            if os.path.exists(final):
                backup = os.path.join(old_dir, os.path.basename(path))
                os.makedirs(old_dir, exist_ok=True)
                os.replace(final, backup)
                moved_old.append((backup, final))
            os.replace(path, final)
            final_paths.append(final)
        final_manifest = os.path.join(output_dir, "bake_manifest.json")
        if os.path.exists(final_manifest):
            os.replace(final_manifest,
                       os.path.join(old_dir, "bake_manifest.json"))
        # 指纹以最终路径重建（manifest 键=路径；临时路径键在发布后无意义）
        final_manifest_obj = VerifiedFrameManifest()
        for final in final_paths:
            final_manifest_obj.record(final)
        if not final_manifest_obj.save(final_manifest):
            raise OSError("final manifest write failed")
    except OSError as exc:
        # 回滚：新就位文件移回临时并清，旧文件从 old_dir 复位
        for final in final_paths:
            try:
                os.remove(final)
            except OSError:
                pass
        try:
            for name in os.listdir(old_dir):
                os.replace(os.path.join(old_dir, name),
                           os.path.join(output_dir, name))
        except OSError:
            pass
        return _fail("publish", f"发布失败（磁盘满/占用？）：{exc}",
                     plan, written)

    # 6. rewrite（发布成功后才改图）
    if apply_rewrite:
        try:
            apply_bake_rewrite(graph, plan)
        except Exception as exc:  # noqa: BLE001 — 改写失败不撤发布产物
            return BakeExecutionResult(
                ok=False, plan=plan, published=final_paths,
                manifest_path=os.path.join(
                    output_dir, "bake_manifest.json"),
                written=written,
                error=f"图改写失败（产物已发布，请手动重试改写）：{exc}",
                stage="rewrite")
    _rmtree_safe(temp_dir)
    _rmtree_safe(old_dir)
    return BakeExecutionResult(
        ok=True, plan=plan, published=final_paths,
        manifest_path=os.path.join(output_dir, "bake_manifest.json"),
        written=written)


def apply_bake_rewrite(graph: Any, plan: BakePlan) -> None:
    """Mutate ``graph`` in place: disable the target, insert the bake
    ``image_input`` node and rewire its consumers to it.

    The caller wraps before/after dicts in an undo action.  When any rewire
    fails the rewrite raises instead of freezing a half-rewired topology
    into ``plan.after_graph`` (AUD-4600); the target stays enabled and the
    already-added bake node is idempotently reused on retry.
    """
    target = graph.get_node(plan.target_id)
    if target is None:
        raise ValueError(f"bake target not found: {plan.target_id}")

    from .nodes.image_input_node import ImageInputNode

    connections = list(getattr(graph, "connections", []))
    primary_out = _primary_output_name(target)

    # AUD-4602: re-check secondary-port consumers at apply time — the graph
    # may have changed while the frames were rendering.
    secondary_ports = [p for p in _consumed_output_ports(graph, plan.target_id)
                       if p != primary_out]
    if secondary_ports:
        raise RuntimeError(
            f"bake target {plan.target_id!r} gained consumers on "
            f"non-primary output port(s) {secondary_ports} since planning; "
            f"refusing to bake")

    # 1. Insert the bake input node (idempotent on retry).
    bake_node = graph.get_node(plan.bake_node_id)
    if bake_node is None:
        bake_node = ImageInputNode(plan.bake_node_id)
        bake_node.user_label = f"Bake: {plan.original_title}"
        graph.add_node(bake_node)

    # 2. Point it at the sequence pattern + frame range.
    for param, value in (
            ("file_path", plan.sequence_pattern),
            ("frame_start", max(1, plan.frame_start)),
            ("frame_end", max(1, plan.frame_end)),
            ("cache_to_disk", False),
    ):
        if hasattr(bake_node, "set_parameter"):
            try:
                bake_node.set_parameter(param, value)
            except (ValueError, TypeError):
                bake_node.parameters[param] = value
        else:
            bake_node.parameters[param] = value

    # AUD-4603: persist the exact pre-bake user label ("" == none) on the
    # bake node so Unbake — possibly in a later session after save/load —
    # restores it verbatim instead of freezing the bake-time display title
    # (i18n translation / instance number) into a permanent rename.
    bake_node.set_parameter(BAKE_ORIGINAL_USER_LABEL_PARAM,
                            plan.original_user_label or "")
    # F7.3：烘焙绑定源签名（重烘焙/上游变化检测的依据）
    try:
        bake_node.set_parameter(
            BAKE_SOURCE_SIGNATURE_PARAM,
            _bake_source_signature(graph, plan.target_id))
    except (ValueError, TypeError):
        bake_node.parameters[BAKE_SOURCE_SIGNATURE_PARAM] = (
            _bake_source_signature(graph, plan.target_id))

    # 3. Rewire every consumer of the target's primary output to the bake
    #    node.  connect() returns False (it does not raise) when validation
    #    fails — check the return value so a silent failure can never be
    #    captured into plan.after_graph (AUD-4600).
    rewire_failures: List[str] = []
    for conn in connections:
        if (getattr(conn, "src_node_id", None) == plan.target_id
                and getattr(conn, "src_port_name", None) == primary_out):
            # Drop the stale target→consumer edge first so multi-input
            # ports (max_connections > 1) don't end up with two sources
            # after the rewire (AUD-4602).
            graph.disconnect(plan.target_id, primary_out,
                             conn.dst_node_id, conn.dst_port_name)
            try:
                connected = bool(graph.connect(
                    plan.bake_node_id, "image",
                    conn.dst_node_id, conn.dst_port_name))
            except Exception as exc:
                logger.warning("bake rewire failed for %s: %s",
                               conn.dst_node_id, exc)
                connected = False
            if not connected:
                # Restore the original edge so the consumer keeps its input.
                graph.connect(plan.target_id, primary_out,
                              conn.dst_node_id, conn.dst_port_name)
                rewire_failures.append(
                    f"{conn.dst_node_id}.{conn.dst_port_name}")

    if rewire_failures:
        # The bake node is already present (idempotent on retry) but the
        # target stays enabled: the graph remains functional and the bake
        # can be retried after the blocker is fixed.
        raise RuntimeError(
            f"bake rewire failed for consumers: {', '.join(rewire_failures)}")

    # 4. Disable the target subtree root and relabel it.
    target.disabled = True
    # ``title`` is a computed property — the user label drives it.
    target.user_label = f"{plan.original_title}{BAKED_TITLE_SUFFIX}"

    plan.after_graph = graph.to_dict()


def make_undo_action(plan: BakePlan,
                     applier: Callable[[Dict[str, Any]], Any],
                     description: str = "Bake subtree") -> Any:
    """Build an UndoStack action restoring/applying the graph dicts.

    ``applier`` receives a graph dict and returns the rebuilt graph — the
    caller closes over its own graph-rebuild pathway (the same one used by
    project load), so the service stays UI-independent.
    """
    from .core.undo_stack import UndoAction

    def _apply(data: Dict[str, Any]) -> None:
        applier(data)

    return UndoAction(
        "bake_subtree",
        {
            "target_id": plan.target_id,
            "before": plan.before_graph,
            "after": plan.after_graph,
        },
        undo_fn=lambda d: _apply(d["before"]),
        redo_fn=lambda d: _apply(d["after"]),
        description=description,
    )


def _auto_display_title(target: Any) -> str:
    """The display title ``target`` would show with no user label (AUD-4603).

    ``display_title`` resolves ``user_label`` first, then the i18n
    translation, then ``node_title`` + instance label.  The property has no
    suppressed mode, so the label is cleared transiently (saved/restored in
    a finally).  The transient write is confined to the calling UI thread,
    matching the module's UI-thread graph-access convention.
    """
    previous = target.user_label
    target.user_label = None
    try:
        return target.display_title
    finally:
        target.user_label = previous


def plan_unbake(graph: Any, bake_node_id: str) -> Optional[BakePlan]:
    """Reverse lookup: find a bake node and reconstruct its plan.

    The baked files are intentionally kept on disk; project cleanup owns
    their lifecycle.
    """
    bake_node = graph.get_node(bake_node_id)
    if bake_node is None:
        return None
    target_id = bake_node_id[len(BAKE_INPUT_PREFIX):]
    target = graph.get_node(target_id)
    if target is None:
        return None
    pattern = bake_node.parameters.get("file_path", "")
    frame_start = int(bake_node.parameters.get("frame_start", 1))
    frame_end = int(bake_node.parameters.get("frame_end", 1))
    # AUD-4603: resolve the pre-bake user label exactly.  Bakes made after
    # the fix carry it on the bake node ("" meaning "no label").  Pre-fix
    # bakes only have the suffixed label: strip the suffix and treat the
    # result as the auto title (→ None) when it matches what the node would
    # show without a user label — never freeze the translated/instance
    # title into a permanent rename.
    stored_label = bake_node.parameters.get(BAKE_ORIGINAL_USER_LABEL_PARAM)
    if stored_label is None:
        current_label = target.user_label
        if current_label and current_label.endswith(BAKED_TITLE_SUFFIX):
            stripped = current_label[:-len(BAKED_TITLE_SUFFIX)]
            original_user_label = (None if stripped == _auto_display_title(target)
                                   else stripped)
        else:
            original_user_label = current_label or None
    else:
        original_user_label = str(stored_label) or None
    original_title = (original_user_label if original_user_label is not None
                      else _auto_display_title(target))
    return BakePlan(
        target_id=target_id,
        bake_node_id=bake_node_id,
        sequence_pattern=pattern,
        frame_start=frame_start,
        frame_end=frame_end,
        original_title=original_title,
        original_user_label=original_user_label,
        before_graph=graph.to_dict(),
    )


def apply_unbake_rewrite(graph: Any, plan: BakePlan) -> None:
    """Undo the bake rewrite: drop the bake node, re-enable the original.

    Every consumer is reconnected to the restored target *before* the bake
    node is removed.  When any rewire fails (connect() returns False — the
    graph changed since the bake, e.g. a cycle or a renamed port) the bake
    node and its connections are kept intact so downstream consumers stay
    fed, and the error is raised for the caller to act on (AUD-4601).
    """
    target = graph.get_node(plan.target_id)
    primary_out = (target.outputs[0].name
                   if target is not None
                   and getattr(target, "outputs", None) else "result")

    rewire_failures: List[str] = []
    for conn in list(getattr(graph, "connections", [])):
        if getattr(conn, "src_node_id", None) == plan.bake_node_id:
            # Drop the bake→consumer edge first so multi-input ports don't
            # keep a duplicate source next to the restored target edge.
            graph.disconnect(plan.bake_node_id, conn.src_port_name,
                             conn.dst_node_id, conn.dst_port_name)
            try:
                connected = bool(graph.connect(
                    plan.target_id, primary_out,
                    conn.dst_node_id, conn.dst_port_name))
            except Exception as exc:
                logger.warning("unbake rewire failed for %s: %s",
                               conn.dst_node_id, exc)
                connected = False
            if not connected:
                # Put the bake edge back so the consumer is not left
                # dangling; the bake node is kept below.
                graph.connect(plan.bake_node_id, conn.src_port_name,
                              conn.dst_node_id, conn.dst_port_name)
                rewire_failures.append(
                    f"{conn.dst_node_id}.{conn.dst_port_name}")

    if rewire_failures:
        # remove_node() would drop the bake node's remaining connections —
        # with a failed rewire that would permanently disconnect those
        # consumers, so refuse to remove it (AUD-4601).
        raise RuntimeError(
            f"unbake rewire failed for consumers: "
            f"{', '.join(rewire_failures)}; bake node "
            f"{plan.bake_node_id} kept intact")

    if target is not None:
        target.disabled = False
        # AUD-4603: restore the recorded user label verbatim — including
        # None, which returns authority to the i18n title / instance label
        # (user_label has the highest display priority once set, so leaving
        # the baked label in place would permanently pin the translation).
        target.user_label = plan.original_user_label
    try:
        graph.remove_node(plan.bake_node_id)
    except Exception as exc:
        logger.warning("unbake node removal failed: %s", exc)
    plan.after_graph = graph.to_dict()
