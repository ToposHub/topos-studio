"""N07-B：Timeline 段出口一致性回归（复核发现的三缺陷修复）。

1. Edit 预览 timeline 段不再被跳过（`_current_timeline` 无写入方恒 ""，
   以 worker 自持 `_timeline_sequence` 兜底）；
2. `chain_stack_ids` 指纹含 timeline 栈（防 timeline 调色后磁盘缓存
   陈旧"绿缓存"）；
3. 调整剪辑链解析豁免 timeline 段（`exclude_timeline`——下方 clip 已
   各自应用过，防 timeline look 双重应用）。
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

from src.features.edit.core.color_chain import (
    chain_extra_params, chain_stack_ids,
)


def _stack(stack_id: str, **params: float) -> NS:
    return NS(stack_id=stack_id, grading_params=dict(params),
              curves_data=None, hsl_curve_data=None, mixer_params=None,
              lut_path=None, soft_clip_params={}, layers=())


def _project(timeline_stack: NS | None = None) -> NS:
    """EditProject 形状：``timeline_grade_stack_id(tid)`` 是 callable
    （resolve_timeline_stack 的取栈路径），栈体在 color_stacks。"""
    stacks = {"s-main": _stack("s-main", contrast=10.0)}
    if timeline_stack is not None:
        stacks[timeline_stack.stack_id] = timeline_stack
    return NS(
        color_groups={}, color_stacks=stacks,
        timeline_grade_stack_id=(
            (lambda tid: timeline_stack.stack_id if tid == "tl1" else "")
            if timeline_stack is not None else (lambda tid: "")),
    )


_CLIP = NS(color_stack_id="s-main", color_trim_stack_id=None)


def test_chain_stack_ids_include_timeline_stack():
    """指纹修复：timeline 栈 id 必须进 chain_stack_ids（漏收=绿缓存）。"""
    timeline = _stack("s-tl", exposure=1.0)
    project = _project(timeline)
    ids = chain_stack_ids(project, "tl1", _CLIP)
    assert "s-tl" in ids


def test_chain_extra_params_exclude_timeline():
    """调整剪辑豁免：exclude_timeline=True 时 timeline 段不出现。"""
    timeline = _stack("s-tl", exposure=1.0)
    project = _project(timeline)
    normal = chain_extra_params(project, "tl1", _CLIP)
    assert [e["_chain_segment"] for e in normal] == ["timeline"]
    exempt = chain_extra_params(project, "tl1", _CLIP,
                                exclude_timeline=True)
    assert exempt == []


def test_preview_timeline_segment_resolves_from_worker_sequence():
    """预览修复存在性：resolver 无 _current_timeline 时以 worker 自持
    ``_timeline_sequence`` 兜底（此前恒 "" → timeline 段被跳过）。

    生产函数在 QThread worker 上，裸构造会触发未初始化 QObject 的
    属性访问 RuntimeError（N10 同款）——链解析的数值语义由
    ``test_chain_extra_params_exclude_timeline`` 与既有 chain 套件
    覆盖，此处锁定修复代码在位。
    """
    import inspect
    from src.features.edit.core import timeline_preview_renderer as tpr
    source = inspect.getsource(tpr)
    assert '_timeline_sequence", None' in source, (
        "预览 timeline_id 解析缺 _timeline_sequence 兜底")


def test_adjustment_grade_snapshot_exempts_timeline():
    """GPU 调整剪辑快照豁免：grade_snapshot lambda 传 exclude_timeline=True。"""
    import inspect
    from src.features.edit.core import timeline_preview_renderer as tpr
    # grade 快照 lambda 在 worker 类中（build_render_operations 装配处）
    module_source = inspect.getsource(tpr)
    assert "grade_snapshot=lambda lyr: self._get_preview_color_grade(" \
        in module_source, "GPU 调整剪辑 grade 快照未走豁免入口"
    assert "exclude_timeline=True" in module_source
    # 导出/CPU 预览的调整剪辑调用点同语义
    from src.features.edit.core import timeline_export as te
    export_source = inspect.getsource(te.TimelineExporter)
    assert "composite, layer, exclude_timeline=True" in export_source, \
        "导出调整剪辑调用点未豁免 timeline 段"
    assert "composite_buffer, layer, exclude_timeline=True" \
        in module_source, "CPU 预览调整剪辑调用点未豁免 timeline 段"
