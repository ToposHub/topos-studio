"""EditProjectService — 统一的无 UI 提交入口（TAEP Phase 0）。

这是整个 TAEP / AI 剪辑闭环的提交基石。它的职责只有三件：

1. **执行一个产生新 timeline 的 EditCommand**（或 MacroCommand），得到 proposed
   sequence。
2. **bump revision**（项目级 + timeline 级），使乐观锁可用（TAEP §6.2）。
3. **同步 ref↔sequence 两层状态**：更新轻量 ``EditProject.timelines`` ref 的
   ``name/fps/width/height/sample_rate/format_settings``，并把新 sequence 写回
   sequence map。返回更新后的 ``(project, sequences)``。

它**不**做：UI 刷新、GPU/预览刷新、Qt 信号、文件保存、模型调用、XML 解析。
调用方（EditController / 脚本 / 未来的 transaction_service）拿到返回的
``CommitResult`` 后，再自行决定如何刷新 UI 与 renderer。

为何必须存在（见设计文档 §2.1 / §6.1）：

- 当前不少命令由 ``TimelineView`` 内的 command stack 发起，随后由
  ``EditController._on_edit_timeline_changed`` 同步两层状态；但属性面板、effect
  面板等路径会绕过 ``timeline_changed`` 信号直接 ``command_stack.execute``，导致
  ref↔sequence 同步被跳过。AI / 脚本 / 交换导入必须走**唯一**入口，才能保证
  revision、ref、sequence map 三者一致。
- 保存时 ``EditProjectIO.save_project`` 会做 key 一致性检查（expected vs provided
  timeline_ids），运行时没有等价检查。本服务在 commit 前做同样的校验，把
  ``TAEP_TIMELINE_NOT_FOUND`` 提到提交边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.edit.core.timeline_ops import validate_timeline

if TYPE_CHECKING:
    from src.features.edit.core.commands import EditCommand

# type alias: timeline_id -> 完整可编辑 TimelineSequence（controller 侧维护的层）。
TimelineSequenceMap = dict[str, TimelineSequence]


class RevisionConflict(Exception):
    """乐观锁冲突：``base_revision != current revision``。

    TAEP §6.2：不自动 last-write-wins。调用方应重新读取 snapshot、重新规划。
    对应诊断码 ``TAEP_REVISION_CONFLICT``。

    AUD-13406：``EditProjectService.commit_command`` 不再直接上抛本异常，
    而是折叠为 ``CommitResult(applied=False, error=str(conflict))``——统一
    写入入口的错误通道（此前 domain 校验失败走 CommitResult、revision
    冲突走异常，直接调用方按 docstring 只检查 ``applied`` 会被击穿）。
    异常类保留：结构化字段（``base_revision``/``current_revision``/
    ``timeline_id``）供深层调用方与测试做编程式断言。
    """

    def __init__(self, base_revision: int, current_revision: int, *, timeline_id: str | None = None) -> None:
        self.base_revision = base_revision
        self.current_revision = current_revision
        self.timeline_id = timeline_id
        scope = f" timeline={timeline_id!r}" if timeline_id else ""
        super().__init__(
            f"Revision conflict{scope}: plan was built against revision "
            f"{base_revision} but current is {current_revision}. Refresh the "
            f"snapshot and regenerate the plan."
        )


@dataclass(frozen=True)
class ProjectSequences:
    """controller 视角的两层状态快照。

    ``project.timelines`` 只存轻量 ``TimelineSequenceRef``；完整的可编辑
    ``TimelineSequence`` 由 controller 以 ``dict[timeline_id, TimelineSequence]``
    形式单独维护。本结构把它们绑在一起，避免任何一方被单独修改。
    """

    project: EditProject
    sequences: TimelineSequenceMap

    @staticmethod
    def empty() -> "ProjectSequences":
        return ProjectSequences(project=EditProject.create("Empty"), sequences={})


@dataclass(frozen=True)
class CommitResult:
    """一次原子提交的结果。

    成功时 ``applied=True``，``new_project`` / ``new_sequences`` 为更新后的两层状态，
    ``new_revision`` 为提交后的 timeline revision，``undo_label`` 供 UI 显示。
    失败（revision 冲突 / domain 校验失败 / 异常）时 ``applied=False`` 且
    ``error`` 描述原因；两层状态保持不变（无半套 timeline）。
    """

    applied: bool
    new_project: EditProject
    new_sequences: TimelineSequenceMap
    new_timeline: TimelineSequence | None = None
    new_revision: int = 0
    undo_label: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None


class EditProjectService:
    """无 UI、无 Qt、无 GPU 的统一提交服务。

    所有方法都是纯函数式的：输入 ``ProjectSequences`` + 命令，输出新的
    ``ProjectSequences``（或 ``CommitResult``）。本服务本身无状态，可被
    EditController、脚本引擎、transaction_service、未来的 MCP host 共用。
    """

    # ------------------------------------------------------------------ #
    # 只读校验：把 save-time 的 key 一致性检查提到运行时
    # ------------------------------------------------------------------ #

    @staticmethod
    def assert_sequences_consistent(state: ProjectSequences) -> None:
        """确认 ref 层（``project.timelines``）与 sequence 层 key 一致。

        保存时 ``EditProjectIO.save_project`` 做同样的检查（见
        ``edit_project_io.py:483``）。本方法把它提到提交边界，使 AI/脚本在
        commit 前就能发现“sequence map 不完整”的问题，对应
        ``TAEP_TIMELINE_NOT_FOUND``。
        """
        ref_ids = set(state.project.timelines)
        seq_ids = set(state.sequences)
        if ref_ids != seq_ids:
            missing = ref_ids - seq_ids
            extra = seq_ids - ref_ids
            raise ValueError(
                "Timeline ref/sequence map inconsistent: "
                f"missing sequences={sorted(missing)}, extra sequences={sorted(extra)}"
            )

    @staticmethod
    def get_sequence(state: ProjectSequences, timeline_id: str) -> TimelineSequence:
        """按 timeline_id 取完整可编辑 sequence。

        先查 sequence map；若缺失但 ref 存在，说明是尚未物化的 timeline
        （controller 懒加载场景），抛出明确错误而非返回 None —— AI 层不应自行
        “补建空 sequence”，那是 controller 的职责。
        """
        seq = state.sequences.get(timeline_id)
        if seq is None:
            ref = state.project.timelines.get(timeline_id)
            if ref is None:
                raise ValueError(f"Timeline not found: {timeline_id!r}")
            raise ValueError(
                f"Timeline {timeline_id!r} exists as a ref but has not been "
                f"materialized into a full sequence; the owning controller must "
                f"create it before AI/script commit."
            )
        return seq

    # ------------------------------------------------------------------ #
    # 核心提交：apply command → validate → bump revision → sync 两层
    # ------------------------------------------------------------------ #

    @classmethod
    def commit_command(
        cls,
        state: ProjectSequences,
        timeline_id: str,
        command: "EditCommand",
        *,
        base_revision: int | None = None,
        undo_label: str | None = None,
        validate: bool = True,
    ) -> CommitResult:
        """执行单个 ``EditCommand`` 并原子提交。

        这是 UI/AI/脚本的统一写入入口。流程严格遵循 TAEP §6.1 的两阶段提交的
        内核部分（解析→读取一致性→编译→纯数据模拟→domain 校验→原子提交）：

        1. 读取 sequence（含 key 一致性检查）。
        2. 若提供 ``base_revision``，做乐观锁比较（TAEP §6.2）。
        3. 在副本上 ``command.apply`` 得到 proposed sequence（不可变模型天然在
           “副本”上操作）。
        4. domain 校验：``validate_timeline``（引用完整性、transition 邻接）；
           overlap 在 ``EditTrack.__post_init__`` 与各 command.apply 内已强制。
        5. bump timeline revision（提交边界才 bump，preview/diff 不 bump）。
        6. 同步 ref↔sequence 两层状态，bump project revision。
        7. 返回 ``CommitResult``；任一步失败都不产生半套状态。

        Args:
            state: 当前两层状态。
            timeline_id: 目标 timeline。
            command: 已构造好的 ``EditCommand``（含 ``MacroCommand``）。
            base_revision: 乐观锁基线。None 表示不检查（适合 UI 直接交互路径，
                由用户最新操作定义真相）。
            undo_label: undo 显示名；None 时取 ``command.label``。
            validate: 是否跑 ``validate_timeline`` domain 校验。

        Returns:
            ``CommitResult``。
        """
        try:
            cls.assert_sequences_consistent(state)
            sequence = cls.get_sequence(state, timeline_id)
        except ValueError as exc:
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=str(exc),
            )

        # 乐观锁：AI/脚本路径必须传 base_revision，与当前 revision 比较。
        # AUD-13406：冲突折叠进 ``CommitResult(applied=False)``，与 domain
        # 校验失败的错误通道统一（docstring 承诺"失败 → applied=False 且
        # error 描述原因"）。``RevisionConflict`` 仍导出供深层调用方编程式
        # 处理（base/current/timeline_id 结构化字段保留在异常类上）。
        if base_revision is not None and base_revision != sequence.revision:
            conflict = RevisionConflict(
                base_revision, sequence.revision, timeline_id=timeline_id
            )
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=str(conflict),
                errors=(str(conflict),),
            )

        # 编译 + 纯数据模拟（不可变模型在“副本”上 apply）。
        try:
            proposed = command.apply(sequence)
            # 部分命令返回 (new_timeline, updated_command) 元组（如 MacroCommand，
            # 见 commands.py:8466）。统一取第一个。
            if isinstance(proposed, tuple):
                proposed = proposed[0]
        except Exception as exc:  # command.apply 内的 domain 错误（overlap 等）
            return CommitResult(
                applied=False,
                new_project=state.project,
                new_sequences=dict(state.sequences),
                error=f"Command failed to apply: {exc}",
            )

        # domain 校验：引用完整性 + transition 邻接（overlap 已在 apply 内强制）。
        if validate:
            try:
                issues = validate_timeline(proposed)
            except Exception as exc:
                return CommitResult(
                    applied=False,
                    new_project=state.project,
                    new_sequences=dict(state.sequences),
                    error=f"Timeline validation raised: {exc}",
                )
            if issues:
                return CommitResult(
                    applied=False,
                    new_project=state.project,
                    new_sequences=dict(state.sequences),
                    errors=tuple(issues),
                    error="; ".join(issues),
                )

        # 提交边界：bump revision（只在这里 bump，保证乐观锁语义干净）。
        committed = proposed.with_revision_bumped()

        # 同步 ref↔sequence 两层状态。
        new_project, new_sequences = cls._sync_two_layers(
            state, timeline_id, committed
        )

        label = undo_label if undo_label is not None else getattr(command, "label", "Edit")
        return CommitResult(
            applied=True,
            new_project=new_project,
            new_sequences=new_sequences,
            new_timeline=committed,
            new_revision=committed.revision,
            undo_label=label,
        )

    @classmethod
    def _sync_two_layers(
        cls,
        state: ProjectSequences,
        timeline_id: str,
        new_sequence: TimelineSequence,
    ) -> tuple[EditProject, TimelineSequenceMap]:
        """同步 ref↔sequence 两层状态并 bump project revision。

        复刻 ``EditController._on_edit_timeline_changed`` 的 ref 反向同步逻辑
        （name/fps/width/height/sample_rate/format_settings），但额外 bump
        project revision，并保证两层 key 始终一致。
        """
        project = state.project

        # 1. sequence map 写回。
        new_sequences = dict(state.sequences)
        new_sequences[timeline_id] = new_sequence

        # 2. ref 反向同步：若元数据漂移则重建 TimelineSequenceRef。
        new_fps = float(new_sequence.fps)
        existing_ref = project.timelines.get(timeline_id)
        new_format = dict(getattr(new_sequence, "format_settings", {}))
        ref_needs_update = (
            existing_ref is None
            or existing_ref.name != new_sequence.name
            or existing_ref.fps != new_fps
            or existing_ref.width != new_sequence.width
            or existing_ref.height != new_sequence.height
            or existing_ref.sample_rate != new_sequence.sample_rate
            or existing_ref.format_settings != new_format
        )
        if ref_needs_update:
            created_at = (
                existing_ref.created_at if existing_ref is not None else 0.0
            )
            new_ref = TimelineSequenceRef(
                timeline_id=new_sequence.timeline_id,
                name=new_sequence.name,
                fps=new_fps,
                width=new_sequence.width,
                height=new_sequence.height,
                sample_rate=new_sequence.sample_rate,
                format_settings=new_format,
                created_at=created_at,
            )
            updated_refs = dict(project.timelines)
            updated_refs[timeline_id] = new_ref
        else:
            updated_refs = project.timelines

        # 3. bump project revision + modified_at（两层 key 在 #1 已保证一致）。
        import time as _time

        new_project = replace(
            project,
            timelines=updated_refs,
            revision=project.revision + 1,
            modified_at=_time.time(),
        )
        return new_project, new_sequences

    # ------------------------------------------------------------------ #
    # preview / dry-run：不 bump、不 sync，只得到 proposed sequence
    # ------------------------------------------------------------------ #

    @classmethod
    def preview_command(
        cls,
        state: ProjectSequences,
        timeline_id: str,
        command: "EditCommand",
    ) -> tuple[TimelineSequence, list[str]]:
        """dry-run：在副本上 apply 并校验，**不** bump revision、**不** sync。

        TAEP §5.6 ``mode="preview"`` 的内核。返回 ``(proposed_sequence, errors)``；
        ``errors`` 非空表示不可提交。proposed 的 revision 与 base 相等，便于
        随后用 ``base_revision`` 做乐观锁比较。
        """
        sequence = cls.get_sequence(state, timeline_id)
        try:
            proposed = command.apply(sequence)
            if isinstance(proposed, tuple):
                proposed = proposed[0]
        except Exception as exc:
            return sequence, [f"Command failed to apply: {exc}"]
        issues = validate_timeline(proposed)
        return proposed, issues
