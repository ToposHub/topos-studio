"""DESK-05B · 移动主轨磁性 / 附着链接 / ripple 双端编辑语义协议。

目的
----
手机（Dart）端的主时间线交互语义（磁性主轨、A/V 附着链接、ripple）
必须与桌面（Python）端对同一工程产生**一致的结构结果**：手机改完
另存 → 桌面打开，时间线几何与链接关系不得漂移。本模块把三块语义
从桌面生产实现（``timeline_v2`` / ``commands`` / ``timeline_ops``）
提炼成**冻结的协议数据**（纯数据 + digest，移动端照抄语义即可对拍）。

协议内容忠实于生产实现（先读实现再冻结，见各节引用）；桌面侧由
``tests/shared/test_edit_semantics_contract.py`` 用生产命令对拍钉住。

────────────────────────────────────────────────────────────────
1. 磁性主轨（MAGNETIC MAIN TRACK）
────────────────────────────────────────────────────────────────
手机端主轨（video track 0）默认磁性：删除/移动/修剪后，**同轨**后续
片段自动左移闭合空隙（等价桌面 ripple 语义，见 §3）。桌面时间线本身
**没有**"轨道级磁性"属性——桌面打开手机工程时，该属性作为轨道
**未知字段**经 ``EditTrack._extra_fields``（DESK-04A 机制）持久化：

- 移动端**写**：track dict 顶层布尔键 ``magnetic_main_track``
  （常量见 :data:`MAGNETIC_MAIN_TRACK_KEY`）。
- 桌面端**读且保存不丢**：``EditTrack.from_dict`` 把该键收进
  ``_extra_fields``，``to_dict`` 原样回写（roundtrip 不丢）；桌面
  不解释该键——桌面编辑行为仍由命令参数（ripple=True/False）决定。
- 值语义：``true`` = 该轨是磁性主轨（手机侧语义）；缺省/``false`` =
  非磁性。桌面对缺省轨道按非磁性处理（不自动闭合空隙）。

────────────────────────────────────────────────────────────────
2. 附着/链接（A/V LINK，``linked_clip_id``）
────────────────────────────────────────────────────────────────
存储：链接 ID 存在 **ClipInstance.linked_clip_id**（双方各持对方
clip_id，reciprocal 双写）。有效链接的判定
（``TimelineSequence.get_reciprocal_linked_clip``，timeline_v2.py）：

1. 双向互指（A.linked_clip_id == B 且 B.linked_clip_id == A）；
2. 两片段分属 video / audio 轨各一（同轨或双视频的"链接"无效）；
3. 单向残链（split/delete 遗留）**不是**有效链接，不得驱动跟随
   移动或选择联动；加载边界 ``repair_linked_clips`` 会清除残链。

行为（按生产实现冻结）：

- **移动跟随**：拖动/键盘移动一方，reciprocal 伙伴按相同帧位移
  跟随（桌面在 UI 拖拽会话 MoveGroup 实现，链接选择开关
  ``linked_selection_enabled`` 默认开；关闭后仅移动选中方）。
- **选择联动**：选中一方，reciprocal 伙伴进入链接高亮/随动选择。
- **普通删除（DeleteClipCommand / with_clip_removed）**：只删一方；
  **另一方存活且链接自动断开**（partner.linked_clip_id 置 None）。
  undo 恢复片段时恢复链接（快照 ``_unlinked_partner``）。
- **波纹删除（RippleDeleteCommand）**：reciprocal 伙伴**同时**被
  波纹删除（两轨各自闭合空隙），其余同步锁轨道只跟随一次
  （链接轨被排除，避免双重位移）。
- **切分（SplitClipCommand）**：右残片不继承链接（核心命令置
  None）；**双方同切点一起切**的宏（MacroCommand）会把两个右残片
  重新互链成新的 reciprocal 对。左片保留原链接。
- **overwrite 覆盖伙伴**：被覆盖方的链接按残片对应关系重建或断开
  （MacroCommand 重链逻辑），显式解链（SetClipLinkedCommand）优先。

────────────────────────────────────────────────────────────────
3. Ripple（波纹）方向与 sync-lock 例外
────────────────────────────────────────────────────────────────
**方向**：桌面 ripple 一律沿时间轴向**左**闭合（删除/提取后同轨
后续片段左移被删时长；插入/延长则右移推开）。跨轨跟随同方向。
非波纹（lift/普通删除/overwrite）保持空隙、后续片段不动。

**同轨 ripple（delete_clip(ripple=True)）**：删除片段后，同轨
``timeline_start >= 被删片段 timeline_end`` 的片段各左移被删时长；
非波纹删除（ripple=False）后续片段保持原位（留空隙）。

**跨轨 Sync Lock（P1-05）**：本轨波纹编辑（波纹删除/波纹修剪/插入）
发生时，其余轨道的跟随规则：

- ``is_sync_locked == True``（**默认**）：该轨 ``timeline_start >=
  波纹点`` 的片段跟随相同位移（左移按可用间隙钳制，顶住前片不抛
  重叠异常；位移对所有被移片段一致，保持相对间距）。
- ``is_sync_locked == False`（**例外，有意失步**）：该轨**完全忽略**
  波纹位移，片段保持原位（音乐轨/参考轨常见用法）。范围内的删除
  仍执行（片段照删），但后续不闭合——该轨退化为 lift 语义。
- ``is_locked == True``（编辑锁）：锁轨不参与跟随（与 sync-lock
  语义正交）；锁轨上的**本轨**写操作（删除/波纹删/移动/修剪）被
  拒绝（assert_track_editable）。
- 字幕轨不参与跨轨跟随。
- 负向位移（波纹删除）钳制：跟随片段起点 ≥ 前一片段结尾
  （``max(delta, -available_gap)``）；正向位移（插入/延长）不钳。

**机器可读模式常量**（见 :data:`RIPPLE_MODE_*`）描述调用方语义；
range 删除（Lift/Extract）与逐条波纹删除的组合规则见
``timeline_ops.build_range_delete_commands`` docstring（范围内整删
+ 越界切分 + 一次性显式位移 range_duration，避免内部空隙残留）。

────────────────────────────────────────────────────────────────
待双端确认（如实记录的生产内不一致，**不改生产行为**）
────────────────────────────────────────────────────────────────
1. ``timeline_v2.ClipInstance.audio_gain_db`` 注释声明混音顺序
   ``clip gain → clip 包络(fade)``；实时路径（audio_playback.mix_samples）
   实际先 fade 后 gain，导出路径（timeline_export）先 gain 后 fade。
   两级都是线性乘法（数学可交换，数值差 ≤ ulp），行为等价；但文字
   契约与两条实现路径的字面顺序不一致，移动端实现取任一顺序即可，
   **不得**在 fade/gain 之间插入非线性节点。
2. ``RippleDeleteCommand`` 对 reciprocal 伙伴执行"整段波纹删除"——
   若伙伴与被删片时长不同，两轨闭合量不同（各闭各的时长）；跨轨
   其余轨道按**被删片**时长跟随（from_frame = 被删片 timeline_end）。
   双端确认移动端是否复刻此"各闭各的"细节。
3. 桌面无轨道级磁性属性：``magnetic_main_track`` 仅是移动端写入、
   桌面保真的持久化标记；桌面命令层不读取它。移动端另存回桌面后，
   桌面编辑产生的空隙不会被该标记自动闭合。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "SEMANTICS_SET_ID",
    "MAGNETIC_MAIN_TRACK_KEY",
    "RIPPLE_MODE_NONE",
    "RIPPLE_MODE_TRACK",
    "RIPPLE_MODE_ALL_SYNC_LOCKED",
    "SYNC_LOCK_DEFAULT",
    "build_edit_semantics",
    "edit_semantics_digest",
]

#: 语义集标识。任一语义变更（键名/默认值/规则）→ bump set id 并双端同步。
SEMANTICS_SET_ID = "topos.edit-semantics.v1"

#: 轨道 extras 持久化键（DESK-04A 未知字段机制；移动端写、桌面保真）。
#: 语义：True = 该轨为移动端磁性主轨；缺省/False = 非磁性。
MAGNETIC_MAIN_TRACK_KEY = "magnetic_main_track"

#: ripple 调用模式（调用方语义分类；桌面命令层即按此分类实现）。
#: none = 普通删除/移动（lift / overwrite / leave-gap）。
RIPPLE_MODE_NONE = "none"
#: track = 仅本轨波纹（同轨后续闭合，跨轨不动）。
RIPPLE_MODE_TRACK = "track"
#: all_sync_locked = 本轨波纹 + 其余同步锁轨道跟随相同位移。
RIPPLE_MODE_ALL_SYNC_LOCKED = "all_sync_locked"

#: ``EditTrack.is_sync_locked`` 出厂默认（timeline_v2）：True = 跟随。
SYNC_LOCK_DEFAULT = True


def build_edit_semantics() -> dict[str, Any]:
    """构造冻结语义的机器可读形式（确定性：固定遍历序、无时间依赖）。"""
    return {
        "semantics_id": SEMANTICS_SET_ID,
        "magnetic_main_track": {
            "extras_key": MAGNETIC_MAIN_TRACK_KEY,
            "value_type": "bool",
            "written_by": "mobile",
            "desktop_behavior": "preserve via EditTrack._extra_fields "
            "(read on load, rewrite on save); desktop commands never "
            "interpret the flag",
            "default": False,
            "semantics": "mobile main track (video track 0) closes "
            "same-track gaps after delete/move/trim (ripple); desktop "
            "edits remain command-parameter driven (ripple=True/False)",
        },
        "av_link": {
            "storage": "ClipInstance.linked_clip_id (reciprocal, both "
            "members carry the partner clip_id)",
            "valid_link_requires": [
                "reciprocal two-way reference",
                "one video track + one audio track",
                "distinct clip ids",
            ],
            "stale_one_way_link": "not an active link; cleared at load "
            "boundary (repair_linked_clips)",
            "behaviors": {
                "move_follow": "reciprocal partner shifts by the same "
                "frame delta (UI MoveGroup; linked selection toggle "
                "default ON)",
                "selection_follow": "selecting one side highlights and "
                "edits with the reciprocal partner",
                "plain_delete": "delete one side only; partner SURVIVES "
                "with its link cleared; undo restores the link",
                "ripple_delete": "reciprocal partner is ripple-deleted "
                "too; each track closes its own removed duration; other "
                "sync-locked tracks follow once",
                "split": "right residual is unlinked by the core "
                "command; split-both-sides macros re-link the two right "
                "residuals as a fresh reciprocal pair; left keeps link",
            },
        },
        "ripple": {
            "direction": "closes toward earlier time (followers shift "
            "LEFT by the removed duration; inserts push RIGHT)",
            "same_track_rule": "clips with timeline_start >= removed "
            "clip's timeline_end shift left by the removed duration; "
            "ripple=False keeps the gap (lift)",
            "cross_track_follow": {
                "is_sync_locked_true": "followers at timeline_start >= "
                "ripple point shift by the same delta (negative delta "
                "clamped to the available gap before the previous clip)",
                "is_sync_locked_false": "track ignores ripple shifts "
                "entirely (deliberate desync, e.g. music beds); in-range "
                "deletes still happen, gaps stay open on that track",
                "is_locked_true": "locked tracks never follow; writes on "
                "the locked track itself are rejected",
                "subtitle_tracks": "never follow cross-track ripple",
                "uniform_shift": "all followers on a track share one "
                "clamped delta so their relative spacing is preserved",
            },
            "modes": {
                "none": RIPPLE_MODE_NONE,
                "track": RIPPLE_MODE_TRACK,
                "all_sync_locked": RIPPLE_MODE_ALL_SYNC_LOCKED,
            },
            "sync_lock_default": SYNC_LOCK_DEFAULT,
        },
        "open_questions": [
            "clip-gain vs fade application order differs textually "
            "between realtime (fade then gain) and export (gain then "
            "fade); both are linear multipliers and commute",
            "linked ripple delete closes each track by its own member "
            "duration while other tracks follow the edited clip's "
            "duration",
        ],
    }


def edit_semantics_digest(semantics: dict[str, Any] | None = None) -> str:
    """语义集摘要（canonical JSON 的 SHA-256）：双端核对同一份协议。"""
    data = semantics if semantics is not None else build_edit_semantics()
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
