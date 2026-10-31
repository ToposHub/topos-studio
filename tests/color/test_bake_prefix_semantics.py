"""P0-COL-04：bake = 栈前缀语义 + 前缀失效校验回归。

缺陷：`bake_layer` 只把「原图 + 单层」交给 bake，而预览循环把 baked
纹理当作「到该层为止的完整栈前缀」替换消费——冻结非底层图层时，下方
图层效果在画面中丢失；且下层修改后 bake 永不失效，预览持续显示过期
前缀。

修复：
1. bake payload 携带 `stack_prefix`（0..n 层完整 DTO），渲染线程逐层
   合成（GL 侧）；
2. bake 时记录前缀内容摘要，`render_pipeline`/`request_color_output`
   入口校验，下层变更/源图替换自动解冻（本文件锁定 CPU 侧语义）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.features.color.core.layer import Layer
from src.shared.video.render_commands import RenderCommand


@pytest.fixture
def engine(image_engine):
    """mocked-GL ImageEngine + mock renderer + 三层栈。"""
    engine = image_engine
    engine.layers = [Layer("L0"), Layer("L1"), Layer("L2")]
    engine.renderer = MagicMock()
    return engine


def test_bake_payload_contains_full_stack_prefix(engine):
    """bake 命令必须携带 0..n 层完整 DTO 前缀 + 摘要（P0-COL-04 核心）。"""
    engine.img_full_original = None  # 走 source_size=(0,0) 分支即可
    engine.bake_layer(1)

    submits = [c.args for c in engine.renderer.submit.call_args_list
               if len(c.args) >= 2 and c.args[0] == RenderCommand.BAKE_LAYER]
    assert submits, "必须提交 BAKE_LAYER 命令"
    payload = submits[0][1]
    prefix = payload.get('stack_prefix')
    assert prefix is not None and len(prefix) == 2, (
        "stack_prefix 必须包含 0..layer_index 共 2 层 DTO（修复前只有单层）"
    )
    assert prefix[0]['id'] == engine.layers[0].id
    assert prefix[1]['id'] == engine.layers[1].id
    assert payload.get('prefix_digest'), "必须携带前缀摘要供失效校验"


def test_prefix_digest_changes_when_lower_layer_changes(engine):
    """下层调色参数变化必须改变上层前缀摘要。"""
    from src.features.color.core.image_engine import _layer_render_data

    prefix = [_layer_render_data(l) for l in engine.layers[:3]]
    d1 = engine._compute_prefix_digest(prefix)
    d2 = engine._compute_prefix_digest(prefix)
    assert d1 == d2, "相同前缀必须产生相同摘要"

    engine.layers[0].grading_params["contrast"] = 33.0
    prefix_changed = [_layer_render_data(l) for l in engine.layers[:3]]
    d3 = engine._compute_prefix_digest(prefix_changed)
    assert d3 != d1, "下层参数变化必须改变前缀摘要"

    # 仅上层自身变化不影响「以下层为底」的其他前缀语义——摘要按前缀
    # 截断计算：layer2 的前缀含 layer2 自身，改 layer2 也变化
    engine.layers[2].opacity = 0.5
    prefix2 = [_layer_render_data(l) for l in engine.layers[:3]]
    assert engine._compute_prefix_digest(prefix2) != d3


def test_validate_unbakes_stale_prefix(engine):
    """下层变更后，渲染入口校验必须自动解冻其上的冻结层。"""
    target = engine.layers[2]
    from src.features.color.core.image_engine import _layer_render_data

    digest = engine._compute_prefix_digest(
        [_layer_render_data(l) for l in engine.layers[:3]])
    engine._bake_prefix_digests[target.id] = digest
    target.is_baked = True
    target.baked_file_name = f"baked_{target.id}.exr"

    # 无变化：保持冻结
    engine._validate_baked_prefixes()
    assert target.is_baked is True

    # 下层变更 → 自动解冻（删文件 + 清 GPU 纹理 + 清台账）
    engine.layers[1].grading_params["exposure"] = 1.5
    with patch("src.shared.io.file_io.FileIO.delete_baked_layer") as del_mock:
        engine._validate_baked_prefixes()
    assert target.is_baked is False, (
        "下层变更后冻结层必须被自动解冻（bake 过期）"
    )
    assert target.baked_file_name is None
    del_mock.assert_called_once_with(f"baked_{target.id}.exr")
    clears = [c.args for c in engine.renderer.submit.call_args_list
              if len(c.args) >= 2
              and c.args[0] == RenderCommand.CLEAR_BAKED_TEXTURE]
    assert clears, "解冻必须提交 CLEAR_BAKED_TEXTURE 清理 GPU 纹理"
    assert target.id not in engine._bake_prefix_digests


def test_validate_ignores_layers_without_digest(engine):
    """无台账的冻结层（旧数据/磁盘加载）不误伤——保持现有行为。"""
    target = engine.layers[1]
    target.is_baked = True
    target.baked_file_name = "baked_x.exr"
    engine._validate_baked_prefixes()
    assert target.is_baked is True, "无摘要记录的冻结层不应被解冻"


def test_unbake_discards_recorded_digest(engine):
    """手动解冻后必须清理摘要台账，避免复活过期校验。"""
    target = engine.layers[0]
    engine._bake_prefix_digests[target.id] = "deadbeef"
    with patch("src.shared.io.file_io.FileIO.delete_baked_layer"):
        engine.unbake_layer(0)
    assert target.id not in engine._bake_prefix_digests
    assert target.is_baked is False


def test_source_epoch_changes_digest(engine):
    """源图替换（_source_epoch 递增）必须使全部前缀摘要过期。"""
    from src.features.color.core.image_engine import _layer_render_data

    prefix = [_layer_render_data(l) for l in engine.layers[:2]]
    d1 = engine._compute_prefix_digest(prefix)
    engine._source_epoch += 1
    assert engine._compute_prefix_digest(prefix) != d1, (
        "源图替换必须改变前缀摘要（baked 内容基于旧源图）"
    )
