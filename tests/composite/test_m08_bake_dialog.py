"""M08.2b · Bake 进度对话框回归（阶段文案/取消/结果呈现）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.composite_core

from src.features.composite.ui.bake_progress_dialog import BakeProgressDialog


def _graph():
    nodes = {"t": NS(parameters={"p": 1}, enabled=True,
                     display_title="T", outputs=None, user_label=None)}
    return NS(nodes=nodes, connections=[],
              get_node=nodes.get, to_dict=lambda: {"nodes": {"t": {}}})


def _render(node_id, frame, out_path):
    import cv2
    import numpy as np
    return cv2.imwrite(out_path, np.zeros((4, 4, 3), np.float32))


class TestBakeProgressDialog:
    def test_stage_labels_complete(self):
        assert set(BakeProgressDialog.STAGE_LABELS) == {
            "freeze", "render", "verify", "source_changed",
            "publish", "rewrite"}

    def test_result_rendering_ok_and_failure(self, qtbot):
        dlg = BakeProgressDialog(
            _graph(), "t", "/tmp/never", 0, 2, _render)
        qtbot.addWidget(dlg)
        ok_result = NS(ok=True, published=["a", "b", "c"],
                       error="", stage="")
        dlg._on_result(ok_result)
        assert "烘焙完成" in dlg._stage_label.text()
        assert "3 帧" in dlg._detail.text()
        fail_result = NS(ok=False, published=[], error="渲染失败",
                         stage="render")
        dlg._on_result(fail_result)
        assert "失败" in dlg._stage_label.text()
        assert "渲染帧序列" in dlg._stage_label.text()
        assert dlg._detail.text() == "渲染失败"
