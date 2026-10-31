"""P10.1a 测试：UndoStack（shared/node_canvas，精简撤销栈）。

> 画布编辑需要 undo/redo。Composite 的 undo_stack.py（1168 行，含 branch/coalesce/
> persist）太重，且红线禁止 ai_workflow 引 composite.core。本批在 shared/node_canvas/
> 建精简线性 UndoStack（纯 QObject，无 GPU）。

验证契约：

- push(action) → undo/redo 状态正确（undo_list/redo_list 长度）。
- can_undo/can_redo 随操作变。
- undo_fn/redo_fn 被调（闭包执行）。
- changed signal 在 push/undo/redo 后 emit。
- undo_fn/redo_fn 抛错只记日志，不污染栈（与 runtime subscriber 一致）。
- clear() 重置。
- 空栈 undo/redo no-op 不抛。
- push 后清空 redo_list（丢弃 redo 分支，与多数编辑器一致）。
- 红线：源码不 import moderngl / composite.core。
"""
import os

import pytest


# ---------------------------------------------------------------------- #
# 红线：源码扫描
# ---------------------------------------------------------------------- #


_SRC = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "src", "shared", "node_canvas", "undo_stack.py",
)


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：undo_stack 不 import moderngl / composite.core（纯 QObject）。"""
    with open(_SRC, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite" not in source
    assert "TextureResult" not in source


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


@pytest.fixture
def stack(qapp):
    from src.shared.node_canvas.undo_stack import UndoStack
    s = UndoStack()
    yield s
    s.deleteLater()


def _make_action(undo_log: list, redo_log: list, tag: str = "a"):
    from src.shared.node_canvas.undo_stack import EditAction
    return EditAction(
        description=tag,
        undo_fn=lambda: undo_log.append(tag),
        redo_fn=lambda: redo_log.append(tag),
    )


# ---------------------------------------------------------------------- #
# 基本 push/undo/redo
# ---------------------------------------------------------------------- #


def test_empty_stack_can_undo_can_redo_false(stack) -> None:
    assert not stack.can_undo()
    assert not stack.can_redo()


def test_empty_stack_undo_redo_noop(stack) -> None:
    """空栈 undo/redo 不抛、no-op。"""
    stack.undo()  # 不抛
    stack.redo()
    assert not stack.can_undo()
    assert not stack.can_redo()


def test_push_makes_can_undo_true(stack) -> None:
    stack.push(_make_action([], []))
    assert stack.can_undo()
    assert not stack.can_redo()


def test_undo_calls_undo_fn_and_moves_to_redo(stack) -> None:
    undo_log, redo_log = [], []
    stack.push(_make_action(undo_log, redo_log, "a"))
    stack.undo()
    assert undo_log == ["a"], f"undo_fn 应被调；实际：{undo_log}"
    assert redo_log == [], "undo 不应调 redo_fn"
    # undo 后：undo 空了，redo 有一个
    assert not stack.can_undo()
    assert stack.can_redo()


def test_redo_calls_redo_fn_and_moves_back_to_undo(stack) -> None:
    undo_log, redo_log = [], []
    stack.push(_make_action(undo_log, redo_log, "a"))
    stack.undo()
    stack.redo()
    assert redo_log == ["a"], f"redo_fn 应被调；实际：{redo_log}"
    assert stack.can_undo()
    assert not stack.can_redo()


# ---------------------------------------------------------------------- #
# redo 分支丢弃
# ---------------------------------------------------------------------- #


def test_push_clears_redo_branch(stack) -> None:
    """push 新动作后清空 redo_list（丢弃 redo 分支）。"""
    stack.push(_make_action([], [], "a"))
    stack.undo()  # a 进 redo
    assert stack.can_redo()
    stack.push(_make_action([], [], "b"))  # 新 push 应清 redo
    assert not stack.can_redo(), "push 后应清空 redo 分支"
    assert stack.can_undo()


# ---------------------------------------------------------------------- #
# changed signal
# ---------------------------------------------------------------------- #


def test_changed_signal_on_push(stack) -> None:
    received = []
    stack.changed.connect(lambda: received.append(1))
    stack.push(_make_action([], []))
    assert received == [1]


def test_changed_signal_on_undo_redo(stack) -> None:
    received = []
    stack.changed.connect(lambda: received.append(1))
    stack.push(_make_action([], []))
    stack.undo()
    stack.redo()
    # push + undo + redo = 3 次
    assert received == [1, 1, 1]


# ---------------------------------------------------------------------- #
# 容错：fn 抛错不污染栈
# ---------------------------------------------------------------------- #


def test_undo_fn_raises_does_not_corrupt_stack(stack) -> None:
    """AUD-36302：undo_fn 抛错被吞，action 留在 undo 栈（不转移）。

    旧行为（action 仍移到 redo）会让"栈说已撤销、模型没回滚"的
    栈/模型状态失配——已按审计修复为留在原栈可重试。
    """
    from src.shared.node_canvas.undo_stack import EditAction
    def bad_undo():
        raise RuntimeError("boom")
    stack.push(EditAction("bad", undo_fn=bad_undo, redo_fn=lambda: None))
    stack.undo()  # 抛错被吞
    # 抛错后栈与模型一致：action 仍在 undo 栈，可重试
    assert stack.can_undo()
    assert not stack.can_redo()
    assert stack.depth() == 1


def test_redo_fn_raises_does_not_corrupt_stack(stack) -> None:
    """AUD-36302：redo_fn 抛错被吞，action 留在 redo 栈（不转移）。"""
    from src.shared.node_canvas.undo_stack import EditAction
    def bad_redo():
        raise RuntimeError("boom")
    stack.push(EditAction("bad", undo_fn=lambda: None, redo_fn=bad_redo))
    stack.undo()
    stack.redo()  # 抛错被吞
    assert not stack.can_undo()
    assert stack.can_redo()


# ---------------------------------------------------------------------- #
# clear
# ---------------------------------------------------------------------- #


def test_clear_resets(stack) -> None:
    stack.push(_make_action([], [], "a"))
    stack.push(_make_action([], [], "b"))
    stack.undo()
    stack.clear()
    assert not stack.can_undo()
    assert not stack.can_redo()


def test_clear_emits_changed(stack) -> None:
    received = []
    stack.changed.connect(lambda: received.append(1))
    stack.push(_make_action([], []))
    received.clear()
    stack.clear()
    assert received == [1]


# ---------------------------------------------------------------------- #
# 多次 push 栈深度
# ---------------------------------------------------------------------- #


def test_multiple_pushes_undo_depth(stack) -> None:
    """3 次 push → undo 3 次回空。"""
    for tag in ("a", "b", "c"):
        stack.push(_make_action([], [], tag))
    assert stack.can_undo()
    stack.undo()
    stack.undo()
    stack.undo()
    assert not stack.can_undo()
