"""M19 尾片 · 三页读数一致性 × PQ/HLG 参考向量对拍联测。

联测冻结两条契约（M19.2a 上下文 + M19.4 向量）：

1. **三页读数一致**：工程级色彩上下文注入后，preview/output ×
   timeline/image 四路 ``descriptor_from_config`` 读出同一工作/显示
   配置——用户全局偏好被工程上下文统一覆盖（三页不漂移）；
2. **向量对拍**：读出的 transfer/primaries 与 hdr_reference_vectors
   的冻结 EOTF 家族对上（pq 描述符 ↔ pq_encode/decode 往返；
   hlg 描述符 ↔ hlg_oetf 往返）——三页显示域读数的单位与 M19.4
   冻结向量同一数学，不另起一套。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.shared.color.color_context_lifecycle import (  # noqa: E402
    on_project_closed,
    on_project_loaded,
)
from src.shared.color.color_space_descriptor import (  # noqa: E402
    ColorPrimaries,
    ColorTransfer,
    descriptor_from_config,
)
from src.shared.color.hdr_reference_vectors import (  # noqa: E402
    hlg_oetf,
    hlg_oetf_inverse,
    pq_decode,
    pq_encode,
)


class _StubConfig:
    """用户全局偏好：preview 页故意配 Rec.709（与工程上下文冲突）。"""

    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


_HDR_CONTEXT = {
    "color.preview.timeline_primaries": "bt2020",
    "color.preview.timeline_transfer": "smpte2084",
    "color.output.timeline_primaries": "bt2020",
    "color.output.timeline_transfer": "smpte2084",
    "color.preview.image_primaries": "bt2020",
    "color.preview.image_transfer": "smpte2084",
    "color.output.image_primaries": "bt2020",
    "color.output.image_transfer": "smpte2084",
}


class TestThreePageReadoutConsistency:
    def setup_method(self):
        on_project_closed()

    def teardown_method(self):
        on_project_closed()

    def test_project_context_unifies_all_four_readouts(self):
        """工程 PQ/BT.2020 上下文覆盖用户 preview 页的 Rec.709 偏好——
        preview/output × timeline/image 四路读出同一配置（三页不漂移）。"""
        user_prefs = _StubConfig({
            "color.preview.timeline_primaries": "bt709",
            "color.preview.timeline_transfer": "rec709",
            "color.preview.image_primaries": "bt709",
            "color.preview.image_transfer": "iec61966_2_1",
        })
        on_project_loaded(_HDR_CONTEXT)
        for scope in ("preview", "output"):
            for kind in ("timeline", "image"):
                d = descriptor_from_config(user_prefs, scope, kind,
                                           is_video=True)
                assert d.primaries == ColorPrimaries.BT2020, (scope, kind)
                assert d.transfer == ColorTransfer.PQ, (scope, kind)

    def test_context_cleared_falls_back_to_user_prefs(self):
        """上下文清除后回用户全局偏好（preview 页 Rec.709 重新生效）——
        生命周期与读数一致性联测。"""
        user_prefs = _StubConfig({
            "color.preview.timeline_primaries": "bt709",
            "color.preview.timeline_transfer": "rec709",
        })
        on_project_loaded(_HDR_CONTEXT)
        on_project_closed()
        d = descriptor_from_config(user_prefs, "preview", "timeline",
                                   is_video=True)
        assert d.primaries == ColorPrimaries.BT709
        assert d.transfer != ColorTransfer.PQ


class TestReadoutVectorCrossCheck:
    """读数 transfer 与冻结 EOTF 向量对拍（同一数学，不另起一套）。"""

    def setup_method(self):
        on_project_closed()

    def teardown_method(self):
        on_project_closed()

    @staticmethod
    def _transfer_for(transfer: str) -> ColorTransfer:
        on_project_loaded({
            "color.preview.timeline_primaries": "bt2020",
            "color.preview.timeline_transfer": transfer,
        })
        return descriptor_from_config(
            _StubConfig({}), "preview", "timeline", is_video=True).transfer

    def test_pq_descriptor_matches_frozen_vectors(self):
        assert self._transfer_for("smpte2084") == ColorTransfer.PQ
        # 冻结向量：PQ 编解码往返（参考白 203 nit 与 1000 nit 锚点）
        for nits in (203.0, 1000.0):
            assert pq_decode(pq_encode(nits)) == pytest.approx(
                nits, rel=1e-6)

    def test_hlg_descriptor_matches_frozen_vectors(self):
        assert self._transfer_for("arib-std-b67") == ColorTransfer.HLG
        # 冻结向量：HLG OETF 逆往返（信号域锚点）
        for signal in (0.25, 0.5, 0.75):
            assert hlg_oetf_inverse(hlg_oetf(signal)) == pytest.approx(
                signal, abs=1e-6)
