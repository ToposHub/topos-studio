"""format_codec_label：Info/Properties 面板 Codec 显示标签的统一格式化。

信息显示优化（M-info-codec）：
- ProRes：FFmpeg profile 名（Standard/HQ/LT/Proxy/4444/4444 XQ）→ 导出
  面板同口径档位标签（'ProRes 422 HQ' 等）——FFmpeg 的 profile 名不含
  "422"，需映射补齐家族后缀；
- Topos：native probe 的 profile 名已含族名（'Topos 422'）→ 直接使用，
  不重复族名；
- 其余编码（h264 'High' 等）→ '族名 profile' 拼接；
- 缺失/大小写/未知 profile 的边界行为。
"""

from __future__ import annotations

import pytest

from src.shared.media.metadata import format_codec_label


class TestProresLabels:
    """FFmpeg ProRes profile 名 → 导出面板同口径档位。"""

    @pytest.mark.parametrize(
        "profile, expected",
        [
            ("Standard", "ProRes 422"),
            ("HQ", "ProRes 422 HQ"),
            ("LT", "ProRes 422 LT"),
            ("Proxy", "ProRes 422 Proxy"),
            ("4444", "ProRes 4444"),
            ("4444 XQ", "ProRes 4444 XQ"),
        ],
    )
    def test_profile_mapped_to_family_suffix(self, profile: str,
                                             expected: str) -> None:
        assert format_codec_label("prores", profile) == expected

    def test_profile_case_insensitive(self) -> None:
        assert format_codec_label("ProRes", "hq") == "ProRes 422 HQ"

    def test_unknown_profile_falls_back_to_generic_join(self) -> None:
        assert format_codec_label("prores", "Future") == "ProRes Future"

    def test_missing_profile_shows_family_only(self) -> None:
        assert format_codec_label("prores", None) == "ProRes"
        assert format_codec_label("prores", "") == "ProRes"


class TestToposLabels:
    """topos 源 profile 名已含族名，不得重复（此前显示 'topos / Topos 422'）。"""

    @pytest.mark.parametrize(
        "profile, expected",
        [
            ("Topos 422", "Topos 422"),
            ("Topos 4444", "Topos 4444"),
            ("Topos 4444 XQ", "Topos 4444 XQ"),
        ],
    )
    def test_family_prefix_not_duplicated(self, profile: str,
                                          expected: str) -> None:
        assert format_codec_label("topos", profile) == expected

    def test_missing_profile_shows_family_only(self) -> None:
        assert format_codec_label("topos", None) == "Topos"


class TestGenericCodecs:
    """非特判编码：'族名 profile' 拼接；无 profile 仅族名。"""

    def test_h264_with_profile(self) -> None:
        assert format_codec_label("h264", "High") == "h264 High"

    def test_codec_only(self) -> None:
        assert format_codec_label("h264", None) == "h264"

    def test_empty_inputs(self) -> None:
        assert format_codec_label(None, None) == ""
        assert format_codec_label("", "") == ""

    def test_profile_only(self) -> None:
        assert format_codec_label(None, "Topos 422") == "Topos 422"


class TestToposImageLabels:
    """图片族命名：TMET label（'toos High 12bit'）原样显示；类名兜底与
    旧缓存 'Image HQ' 形式不重复拼接。"""

    def test_tmet_label_shown_verbatim(self) -> None:
        assert format_codec_label("topos-image", "toos High 12bit") == "toos High 12bit"
        assert format_codec_label("topos-image", "toos Ultra 10bit") == "toos Ultra 10bit"

    def test_video_tier_label_shown_verbatim(self) -> None:
        assert format_codec_label("topos-image", "Topos 422") == "Topos 422"
        assert format_codec_label("topos-image-sequence", "Topos 422 HQ") == "Topos 422 HQ"

    def test_class_suffix_joined_with_family(self) -> None:
        assert format_codec_label("topos-image", "HQ") == "Topos Image HQ"
        assert format_codec_label("topos-image", "RAW") == "Topos Image RAW"
        assert format_codec_label("topos-image-sequence", "Preview") == \
            "Topos Image Sequence Preview"

    def test_legacy_cached_profile_normalized(self) -> None:
        assert format_codec_label("topos-image", "Image HQ") == "Topos Image HQ"
