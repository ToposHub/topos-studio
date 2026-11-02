"""图取样契约与取样身份（S03/S05，spec docs/color_nodes_backend_implementation_plan.md §6.8）。

S03 契约（本模块是唯一事实来源，UI/E02/E03/面板都从这里取常量）：

1. **graded 默认输入 = 主 Output 的完整画面**。节点选择（``active_node_changed``）
   只是导航，绝不隐式把测量输入改成单节点输出。
2. **单节点 / Key 测量是独立显式模式**：目标身份必须带 ``node:``/``key:``
   前缀并显式展示输入标签；不存在"选中的节点"隐式测量。
3. **取样位置**：clip 图 Output 之后、显示变换之前（scene-linear 工作域，
   HDR 不截断）。显示变换（monitor transform）不属于测量域；
   group pre→clip→group post→timeline 顺序化（E27）落地后在此登记
   各级取样点。
4. **单位与通道**：测量值为 scene-linear（无 1.0 clamp）；transfer/primaries
   由 demand 的 ``scope_transfer``/``scope_primaries`` 显式声明（CIE 线性化
   与假色 stops 的 metadata 来源）；alpha 不参与 luma/chroma 密度
   （独立通道）；Window/mask 权重只有用户显式开启
   （``mask_enabled``）才应用。
5. **source/reference 语义独立**：``source``（未调色）/``reference``（参考
   still）不走 GPU graded 通道（CPU 路径），target 身份区分三者。

S05 身份（``ScopeSampleIdentity``）：在 revision 契约上补充 session/stack
owner、graph render revision、frame、颜色上下文与显式取样目标身份，
供面板拒绝迟到/跨 clip/跨图版本的结果。``source_content_revision`` 与
frame 是**下界**（"不早于"），其余字段精确相等——与
``ScopeDemand.source_revision`` 的 lower-bound 哲学一致：GPU_SCOPE 低优先
级 lane 故意排在 video 之后，晚到的更新帧是合法的，旧帧永远非法。
"""

from __future__ import annotations

from dataclasses import dataclass, replace, field
from typing import Optional, Tuple

#: graded 默认测量目标：主 Output 完整画面（S03 §1）
TARGET_OUTPUT = "output"
#: 独立语义：未调色源画面（CPU 路径）
TARGET_SOURCE = "source"
#: 独立语义：参考 still（CPU 路径）
TARGET_REFERENCE = "reference"

#: 显式单节点/Key 测量目标前缀（S03 §2：独立显式模式，不与 graded 混用）
TARGET_NODE_PREFIX = "node:"
TARGET_KEY_PREFIX = "key:"

#: 面板输入源 → 取样目标身份（graded/source/reference 三分保留）
PANEL_SOURCE_TO_TARGET = {
    "graded": TARGET_OUTPUT,
    "source": TARGET_SOURCE,
    "reference": TARGET_REFERENCE,
}


def explicit_node_target(node_id: str) -> str:
    """构造单节点显式测量目标（带输入标签语义）。"""
    return f"{TARGET_NODE_PREFIX}{node_id}"


def explicit_key_target(node_id: str) -> str:
    """构造 Key 权重显式测量目标。"""
    return f"{TARGET_KEY_PREFIX}{node_id}"


def parse_target(target: str) -> Tuple[str, Optional[str]]:
    """解析目标身份 → (基目标, 显式测量对象 node_id | None)。

    ``"node:abc"`` → ``("node", "abc")``；``"output"`` → ``("output", None)``。
    未知目标返回 ``("unknown", None)``（fail-visible，不臆测）。
    """
    if not isinstance(target, str):
        return ("unknown", None)
    if target.startswith(TARGET_NODE_PREFIX):
        return ("node", target[len(TARGET_NODE_PREFIX):] or None)
    if target.startswith(TARGET_KEY_PREFIX):
        return ("key", target[len(TARGET_KEY_PREFIX):] or None)
    if target in (TARGET_OUTPUT, TARGET_SOURCE, TARGET_REFERENCE):
        return (target, None)
    return ("unknown", None)


def target_label(target: str) -> str:
    """测量域标签（与取样域一致性的展示来源；S03 验收用）。"""
    base, node_id = parse_target(target)
    labels = {
        TARGET_OUTPUT: "Graded · Output (full frame)",
        TARGET_SOURCE: "Source (ungraded)",
        TARGET_REFERENCE: "Reference still",
        "node": f"Node {node_id} (explicit)",
        "key": f"Key {node_id} (explicit)",
    }
    return labels.get(base, "Unknown target")


@dataclass(frozen=True)
class ScopeSampleIdentity:
    """一次 scope 取样的完整身份（S05）。

    - ``session_id``/``scope_owner_id``：clip 栈/组归属（切 clip 立即失配）；
    - ``graph_revision``：图 render revision（参数/连线/结构变化递增）；
    - ``source_content_revision``/``frame``：**下界**——执行 lane 可能分析
      更新的 front（合法），更旧的必须丢弃；
    - ``color_context``：测量域 metadata（transfer, primaries）；
    - ``target``：S03 取样目标（默认 ``output``）。
    """

    session_id: str = ""
    scope_owner_id: Optional[str] = None
    graph_revision: int = 0
    source_content_revision: int = 0
    frame: int = 0
    color_context: Tuple[str, ...] = ()
    target: str = TARGET_OUTPUT

    def with_source_content_revision(self, revision: int) -> "ScopeSampleIdentity":
        """执行侧回填实际分析的 front revision（下界语义）。"""
        return replace(self, source_content_revision=int(revision))

    def allows(self, result_identity: "ScopeSampleIdentity") -> bool:
        """迟到/串扰拒绝（S05）：结果身份是否可显示。

        精确字段必须相等；``source_content_revision``/``frame`` 允许结果
        更新（不早于需求）。任一缺失时交由调用方的 revision 契约兜底。
        """
        if not isinstance(result_identity, ScopeSampleIdentity):
            return False
        if self.session_id != result_identity.session_id:
            return False
        if self.scope_owner_id != result_identity.scope_owner_id:
            return False
        if self.graph_revision != result_identity.graph_revision:
            return False
        if self.color_context != result_identity.color_context:
            return False
        if self.target != result_identity.target:
            return False
        if result_identity.source_content_revision < self.source_content_revision:
            return False
        if result_identity.frame < self.frame:
            return False
        return True
