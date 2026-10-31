"""DESK-05B · 磁性主轨 / 附着链接 / ripple 双端语义合同测试。

桌面侧冻结（协议见 ``src/shared/contracts/edit_semantics.py``）；
每条断言都用**生产命令**（commands.py / timeline_ops.py）驱动，钉住
协议声明的行为与实现一致。移动端（Dart）按同一协议实现后应逐条对拍。

待移动联调项：
- ``magnetic_main_track`` 键：移动端写入 + 桌面 roundtrip 保真（本文件
  钉桌面侧半边）；移动端"磁性=ripple 默认开"的行为本身在移动端验证。
- 链接 A/V 波纹删除"各闭各的时长"细节（见 open_questions）双端复核。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.shared.contracts import edit_semantics as es
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import (
    BatchDeleteCommand,
    DeleteClipCommand,
    MoveClipCommand,
    RippleDeleteCommand,
    SplitClipCommand,
)
from src.features.edit.core.timeline_ops import (
    delete_clip,
    move_clip,
    shift_sync_lock_followers,
)
from src.shared.media.metadata import Rational


def _clip(clip_id: str, track_id: str, start: int, duration: int = 100,
          linked_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration + 500,
        linked_clip_id=linked_id,
    )


def _timeline(v_clips=(), a_clips=(), v_sync=True, a_sync=True,
              track_extras: dict | None = None) -> TimelineSequence:
    v1 = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=tuple(v_clips), is_sync_locked=v_sync,
    )
    a1 = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        clips=tuple(a_clips), is_sync_locked=a_sync,
    )
    seq = TimelineSequence(
        timeline_id="tl1", name="Contract", fps=Rational(24, 1),
        video_tracks=(v1,), audio_tracks=(a1,),
    )
    if track_extras:
        # 经持久化边界注入（与移动端写包同路径：dict 顶层未知键 → extras）
        payload_v = v1.to_dict()
        payload_v.update(track_extras)
        seq = seq.with_track_updated(EditTrack.from_dict(payload_v))
    return seq


def _start(tl: TimelineSequence, clip_id: str) -> int | None:
    clip = tl.get_clip_by_id(clip_id)
    return None if clip is None else clip.timeline_start


def _link_of(tl: TimelineSequence, clip_id: str) -> str | None:
    clip = tl.get_clip_by_id(clip_id)
    return None if clip is None else clip.linked_clip_id


# ─────────────────────────────────────────────────────────────────────────────
# 协议数据与 digest
# ─────────────────────────────────────────────────────────────────────────────

class TestSemanticsDigest:
    def test_digest_deterministic(self) -> None:
        assert es.edit_semantics_digest() == es.edit_semantics_digest(
            es.build_edit_semantics())

    def test_set_id_and_key_names_frozen(self) -> None:
        assert es.SEMANTICS_SET_ID == "topos.edit-semantics.v1"
        assert es.MAGNETIC_MAIN_TRACK_KEY == "magnetic_main_track"
        # 协议数据里的键名与常量一致（防两处漂移）
        data = es.build_edit_semantics()
        assert (data["magnetic_main_track"]["extras_key"]
                == es.MAGNETIC_MAIN_TRACK_KEY)
        assert set(data["ripple"]["modes"].values()) == {
            es.RIPPLE_MODE_NONE, es.RIPPLE_MODE_TRACK,
            es.RIPPLE_MODE_ALL_SYNC_LOCKED,
        }
        assert data["ripple"]["sync_lock_default"] is True

    def test_semantics_reference_real_defaults(self) -> None:
        """sync_lock 默认值声明与生产字段一致。"""
        import dataclasses
        field_default = next(
            f.default for f in dataclasses.fields(EditTrack)
            if f.name == "is_sync_locked")
        assert field_default is es.SYNC_LOCK_DEFAULT


# ─────────────────────────────────────────────────────────────────────────────
# 1. 磁性主轨：extras 持久化键
# ─────────────────────────────────────────────────────────────────────────────

class TestMagneticMainTrackExtras:
    def test_key_roundtrips_through_edit_track(self) -> None:
        """移动端写入的键：桌面 load → save 不丢（DESK-04A extras 机制）。"""
        track = EditTrack.from_dict({
            "track_id": "v1", "name": "V1", "track_type": "video",
            "index": 0,
            es.MAGNETIC_MAIN_TRACK_KEY: True,
        })
        assert track._extra_fields[es.MAGNETIC_MAIN_TRACK_KEY] is True
        payload = track.to_dict()
        assert payload[es.MAGNETIC_MAIN_TRACK_KEY] is True

    def test_desktop_default_is_not_magnetic(self) -> None:
        """缺省轨道（桌面新建/旧工程）无该键 → 桌面按非磁性处理。"""
        track = EditTrack(track_id="v1", track_type="video", index=0)
        assert es.MAGNETIC_MAIN_TRACK_KEY not in track.to_dict()
        assert es.build_edit_semantics()["magnetic_main_track"][
            "default"] is False

    def test_plain_delete_keeps_gap_on_desktop_regardless_of_flag(self) -> None:
        """桌面命令不解释该键：普通删除仍留空隙（不自动闭合）。"""
        tl = _timeline(
            v_clips=[_clip("c1", "v1", 0, 100), _clip("c2", "v1", 100, 100)],
            track_extras={es.MAGNETIC_MAIN_TRACK_KEY: True},
        )
        new_tl, _ = DeleteClipCommand.create("v1", "c1").apply(tl)
        assert _start(new_tl, "c2") == 100  # 不闭合
        # 且轨道 extras 在编辑后仍保真
        v1 = new_tl.get_track_by_id("v1")
        assert v1.to_dict()[es.MAGNETIC_MAIN_TRACK_KEY] is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. ripple：方向 / sync-lock 轨 vs 非同步轨
# ─────────────────────────────────────────────────────────────────────────────

class TestRippleDirection:
    def test_ripple_delete_closes_gap_same_track(self) -> None:
        """同轨 ripple：被删片之后的片段左移被删时长（向左闭合）。"""
        tl = _timeline(v_clips=[
            _clip("c1", "v1", 0, 100),
            _clip("c2", "v1", 100, 100),
            _clip("c3", "v1", 200, 100),
        ])
        new_tl = delete_clip(tl, "c1", ripple=True)
        assert _start(new_tl, "c2") == 0
        assert _start(new_tl, "c3") == 100

    def test_plain_delete_keeps_gap(self) -> None:
        """非波纹删除（lift）：后续保持原位。"""
        tl = _timeline(v_clips=[
            _clip("c1", "v1", 0, 100), _clip("c2", "v1", 100, 100),
        ])
        new_tl = delete_clip(tl, "c1", ripple=False)
        assert _start(new_tl, "c2") == 100

    def test_ripple_delete_command_sync_track_follows_desync_stays(self) -> None:
        """核心协议断言：sync-lock 轨 vs 非同步轨在 ripple delete 后的位移差异。

        v1（编辑轨，sync=True）删 c1；a1 上后续 a_m：
        - a_sync=True  → a_m 跟随左移 100（跨轨对齐）
        - a_sync=False → a_m 原位不动（有意失步；协议例外条款）
        """
        for a_sync, expected in ((True, 0), (False, 100)):
            tl = _timeline(
                v_clips=[_clip("c1", "v1", 0, 100),
                         _clip("c2", "v1", 100, 100)],
                a_clips=[_clip("a_m", "a1", 100, 100)],
                a_sync=a_sync,
            )
            new_tl, _cmd = RippleDeleteCommand.create(tl, "v1", "c1").apply(tl)
            assert _start(new_tl, "a_m") == expected, (
                f"a_sync={a_sync}: expected {expected}, "
                f"got {_start(new_tl, 'a_m')}")

    def test_negative_shift_clamped_by_gap(self) -> None:
        """负向跟随按可用间隙钳制：顶住前片结尾，不抛重叠异常。"""
        tl = _timeline(
            v_clips=[_clip("c1", "v1", 0, 100)],
            a_clips=[_clip("a_prev", "a1", 0, 80),
                     _clip("a_m", "a1", 100, 100)],
        )
        new_tl, _moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["v1"], from_frame=100, delta=-50)
        assert _start(new_tl, "a_m") == 80  # 只移 20（顶住 a_prev）

    def test_uniform_shift_preserves_spacing(self) -> None:
        """钳制后的位移对所有被移片段一致（相对间距不变）。"""
        tl = _timeline(
            v_clips=[_clip("c1", "v1", 0, 100)],
            a_clips=[_clip("a_prev", "a1", 0, 80),
                     _clip("a_m", "a1", 100, 100),
                     _clip("a_n", "a1", 220, 100)],  # 间距 120-100=20
        )
        new_tl, _moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["v1"], from_frame=100, delta=-50)
        assert _start(new_tl, "a_m") == 80
        assert _start(new_tl, "a_n") == 200  # 同样只移 20，间距 120 保持

    def test_batch_delete_linked_pair_follows_once(self) -> None:
        """链接 A/V 对同批波纹删除：其余轨道按合并跨度只跟随一次。"""
        tl = _timeline(
            v_clips=[_clip("v_c", "v1", 0, 100, linked_id="a_c"),
                     _clip("v_after", "v1", 100, 50)],
            a_clips=[_clip("a_c", "a1", 0, 100, linked_id="v_c"),
                     _clip("a_after", "a1", 100, 50)],
        )
        # 第三条同步锁轨道（另一视频轨）上放跟随者
        extra_track = EditTrack(
            track_id="v2", name="V2", track_type="video", index=1,
            clips=(_clip("x_follower", "v2", 100, 50),),
        )
        tl = tl._rebuild(video_tracks=(tl.video_tracks[0], extra_track))
        new_tl, _cmd = BatchDeleteCommand.create(
            tl, ("v_c", "a_c"), ripple=True).apply(tl)
        # v2 上的 follower 只左移一次 100（不会因 A/V 两轨各移一遍变 0 起点越界）
        assert _start(new_tl, "x_follower") == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. 附着/链接：linked_clip_id 双端协议
# ─────────────────────────────────────────────────────────────────────────────

def _linked_timeline() -> TimelineSequence:
    """v_c ↔ a_c 互链（video track v1 + audio track a1）。"""
    return _timeline(
        v_clips=[_clip("v_c", "v1", 0, 100, linked_id="a_c")],
        a_clips=[_clip("a_c", "a1", 0, 80, linked_id="v_c")],
    )


class TestAVLinkProtocol:
    def test_reciprocal_link_is_valid(self) -> None:
        tl = _linked_timeline()
        v_c = tl.get_clip_by_id("v_c")
        assert tl.get_reciprocal_linked_clip(v_c).clip_id == "a_c"

    def test_one_way_stale_link_is_not_active(self) -> None:
        """单向残链不构成有效链接（协议：不得驱动跟随/联动）。"""
        tl = _timeline(
            v_clips=[_clip("v_c", "v1", 0, 100, linked_id="a_c")],
            a_clips=[_clip("a_c", "a1", 0, 80, linked_id=None)],
        )
        assert tl.get_reciprocal_linked_clip(
            tl.get_clip_by_id("v_c")) is None

    def test_same_track_type_link_is_not_active(self) -> None:
        """同轨/双视频的链接无效（协议：必须一 video 一 audio）。"""
        tl = _timeline(
            v_clips=[_clip("v_c", "v1", 0, 100, linked_id="v_d"),
                     _clip("v_d", "v1", 100, 100, linked_id="v_c")],
        )
        assert tl.get_reciprocal_linked_clip(
            tl.get_clip_by_id("v_c")) is None

    def test_plain_delete_unlinks_but_keeps_partner(self) -> None:
        """普通删除一方：另一方存活、链接自动断开（协议冻结行为）。"""
        tl = _linked_timeline()
        new_tl, cmd = DeleteClipCommand.create("v1", "v_c").apply(tl)
        assert new_tl.get_clip_by_id("a_c") is not None  # 伙伴存活
        assert _link_of(new_tl, "a_c") is None            # 链接断开
        # undo 恢复片段并恢复链接
        restored = cmd.undo(new_tl)
        assert _link_of(restored, "a_c") == "v_c"
        assert _link_of(restored, "v_c") == "a_c"

    def test_ripple_delete_removes_both_members(self) -> None:
        """波纹删除一方：reciprocal 伙伴同时被波纹删除。"""
        tl = _timeline(
            v_clips=[_clip("v_c", "v1", 0, 100, linked_id="a_c"),
                     _clip("v_after", "v1", 100, 50)],
            a_clips=[_clip("a_c", "a1", 0, 80, linked_id="v_c"),
                     _clip("a_after", "a1", 80, 50)],
        )
        new_tl, _cmd = RippleDeleteCommand.create(tl, "v1", "v_c").apply(tl)
        assert new_tl.get_clip_by_id("a_c") is None
        assert _start(new_tl, "v_after") == 0   # v1 闭 100
        assert _start(new_tl, "a_after") == 0   # a1 闭 80（各闭各的时长）

    def test_move_does_not_silently_move_partner_at_model_layer(self) -> None:
        """模型层 MoveClipCommand 只动目标片段——伙伴跟随是 UI 会话
        （MoveGroup，链接选择默认开）组合两条 Move 命令实现的；双端协议
        是"reciprocal 伙伴按相同 delta 移动"，这里按该组合语义钉住。"""
        tl = _linked_timeline()
        # UI 组合：对双方各发一条 Move（同 delta）
        new_tl = MoveClipCommand.create("v1", "v_c", 40).apply(tl)
        new_tl = MoveClipCommand.create("a1", "a_c", 40).apply(new_tl)
        assert _start(new_tl, "v_c") == 40
        assert _start(new_tl, "a_c") == 40
        assert _link_of(new_tl, "v_c") == "a_c"  # 链接保持

    def test_split_right_residual_unlinked_by_core_command(self) -> None:
        """核心 SplitClipCommand：右残片不继承链接（宏层同切点重链，
        见协议 split 条款）。"""
        tl = _linked_timeline()
        new_tl = SplitClipCommand.create(
            "v1", "v_c", 50, right_clip_id="v_right").apply(tl)
        assert _link_of(new_tl, "v_right") is None
        assert _link_of(new_tl, "v_c") == "a_c"  # 左片保留链接

    def test_repair_clears_stale_links_keeps_valid_pairs(self) -> None:
        tl = _timeline(
            v_clips=[
                _clip("v_ok", "v1", 0, 100, linked_id="a_ok"),
                _clip("v_stale", "v1", 100, 100, linked_id="a_gone"),
            ],
            a_clips=[_clip("a_ok", "a1", 0, 100, linked_id="v_ok")],
        )
        repaired = tl.repair_linked_clips()
        assert _link_of(repaired, "v_ok") == "a_ok"   # 有效对保留
        assert _link_of(repaired, "v_stale") is None  # 残链清除
