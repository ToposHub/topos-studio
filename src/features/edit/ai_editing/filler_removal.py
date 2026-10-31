"""F9.1 · 删除口头语（filler removal）——词级检测与可审批删除计划。

现状（调研）：词级时间戳（`CaptionWordTiming`，帧基）与 canonical
plan→command→approval 链（`compile_plan` 白名单 + `state_fingerprint`
stale 判定 + PREVIEW 红线）**均已存在**；缺的是"口头语检测→删除计划"。

本模块补齐缺口（纯函数，模型在位时零依赖）：
- ``detect_filler_words``：`word_timings` + 语言词表（zh/en 内置，
  可扩展）→ 待删区间（帧域 + 置信度 + 命中词）；**低置信词不删**
  （<0.6 标记 review，人工排除）；混合语言按词表并集处理；
- ``build_filler_removal_plan``：待删区间 → 既有 ``EditPlan``
  （DELETE_CLIP/trim 语义经白名单 op，**PREVIEW 模式**——走与
  ``wf-talking-head-fine-cut`` 相同的 preview→approval 链，**不新增
  旁路**）；``base_revision`` 绑定当前工程 revision（过时计划拒绝
  由 ``state_fingerprint`` 既有机制执行）；
- 幂等：同输入（同词表+同 revision+同 timings 摘要）→ 同 plan
  内容哈希（plan_repository 的 content_hash 语义自然获得——
  批量重试不反复删同段）。

首版边界（诚实声明）：帧对齐（样本级精度归 F3.3 条件依赖，计划
原文允许）；跨 split/retime/嵌套片段的支持范围 = 平铺片段（复杂
容器结构登记后续片）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from src.features.edit.core.captions.model import CaptionWordTiming
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanTarget,
    TAEP_PROTOCOL_VERSION,
)

#: 内置口头语词表（zh/en 常见填充词；可经 extra_lexicon 扩展）
DEFAULT_FILLER_LEXICON: Dict[str, Tuple[str, ...]] = {
    "en": ("um", "uh", "erm", "like", "you know", "i mean", "sort of",
           "kind of", "basically", "actually", "literally"),
    "zh": ("嗯", "呃", "啊", "这个", "那个", "就是", "然后", "对吧",
           "怎么说呢", "等于说"),
}

#: 词置信度低于此值 → 不自动删，标记 review（人工排除）
FILLER_CONFIDENCE_FLOOR = 0.6


@dataclass(frozen=True)
class FillerHit:
    """一个口头语命中（帧域区间 + 置信）。"""

    text: str
    start_frame: int
    end_frame: int
    confidence: float
    review_only: bool = False   # 低置信 → 人工确认，不进删除计划

    @property
    def span_frames(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class FillerDetectionResult:
    """检测汇总（删除项与 review 项分离——不静默删低置信）。"""

    hits: Tuple[FillerHit, ...] = ()
    review_hits: Tuple[FillerHit, ...] = ()

    @property
    def deletable(self) -> Tuple[FillerHit, ...]:
        return tuple(h for h in self.hits if not h.review_only)


def detect_filler_words(
    word_timings: Sequence[CaptionWordTiming],
    *,
    languages: Sequence[str] = ("en", "zh"),
    extra_lexicon: Optional[Dict[str, Tuple[str, ...]]] = None,
    confidence_floor: float = FILLER_CONFIDENCE_FLOOR,
) -> FillerDetectionResult:
    """从词级时间戳检测口头语。

    低置信词（< floor）**不删**——进 review_hits 供人工排除（计划
    原文）；混合语言按词表并集。命中词原文匹配（大小写不敏感、去
    首尾标点）。
    """
    lexicon: Dict[str, Tuple[str, ...]] = {}
    for lang in languages:
        lexicon[lang] = tuple(DEFAULT_FILLER_LEXICON.get(lang, ()))
    if extra_lexicon:
        for lang, words in extra_lexicon.items():
            lexicon[lang] = lexicon.get(lang, ()) + tuple(words)
    fillers = {w.lower() for words in lexicon.values() for w in words}

    hits: List[FillerHit] = []
    reviews: List[FillerHit] = []
    for timing in word_timings:
        word = str(timing.text).strip().strip(",.!?;:、，。！？；：").lower()
        if not word or word not in fillers:
            continue
        hit = FillerHit(
            text=str(timing.text), start_frame=int(timing.start_frame),
            end_frame=int(timing.end_frame),
            confidence=float(timing.confidence),
            review_only=float(timing.confidence) < confidence_floor)
        (reviews if hit.review_only else hits).append(hit)
    return FillerDetectionResult(
        hits=tuple(sorted(hits, key=lambda h: h.start_frame)),
        review_hits=tuple(sorted(reviews, key=lambda h: h.start_frame)))


def timings_fingerprint(
    word_timings: Sequence[CaptionWordTiming],
) -> str:
    """词级时间戳摘要（幂等锚：同输入同哈希——批量重试不重复删）。"""
    payload = json.dumps(
        [{"t": w.text, "s": w.start_frame, "e": w.end_frame,
          "c": round(float(w.confidence), 4)}
         for w in word_timings],
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clips_intersecting_range(timeline, start_frame: int, end_frame: int):
    """与 [start_frame, end_frame) 相交的全部 (track, clip)（视频/音频轨）。

    M11（S02）：词时间（时间线帧域）→ 真实片段区间映射。返回按
    timeline_start 排序的列表；相交区间裁剪到片段边界。
    """
    hits = []
    for track in (list(getattr(timeline, "video_tracks", ()) or ())
                  + list(getattr(timeline, "audio_tracks", ()) or ())):
        for clip in getattr(track, "clips", ()) or ():
            s = max(start_frame, clip.timeline_start)
            e = min(end_frame, clip.timeline_end)
            if s < e:
                hits.append((track, clip, s, e))
    hits.sort(key=lambda h: (h[1].timeline_start, h[0].track_id))
    return hits


def _is_container_clip(clip, project=None) -> bool:
    """M11.4：容器片段（嵌套序列/合成/生成器/调整等虚拟宿主）。

    顶层区间删除（split+ripple）会波及整个容器的内容——命中位于
    容器内部时不可映射为顶层 delete op（需进容器内部执行）。
    判定：clip.composite_id（合成）或宿主工程中素材为虚拟类型；
    无工程时仅 composite_id 可判（保守：能判出的跳过，判不出的
    按普通片段——不扩大跳过面）。
    """
    if getattr(clip, "composite_id", None):
        return True
    if project is None:
        return False
    asset = (getattr(project, "assets", None) or {}).get(
        str(getattr(clip, "asset_id", "")))
    return bool(asset is not None and getattr(asset, "is_virtual", False))


def build_filler_removal_plan(
    detection: FillerDetectionResult,
    *,
    target: PlanTarget,
    base_revision: int,
    plan_id: str,
    timeline=None,
    project=None,
    title: str = "删除口头语",
) -> EditPlan:
    """待删区间 → EditPlan（PREVIEW 模式，走既有审批链）。

    M11（S02）：词时间映射到**真实 track/clip**——每个 (命中, 片段)
    相交区间编译为一个带 start_frame/end_frame 的 delete_clip op
    （plan_compiler 编译为 RangeRippleDeleteCommand：split×2 +
    ripple 中段），不再使用伪 clip_id（此前 ``filler:10-15`` 形态
    无 track_id，真实 compiler 直接 CompileError；整片删除也不能
    表达词区间）。

    Args:
        timeline: 目标时间线（TimelineSequence）——词帧域的归属解析。
            None（旧行为兼容/测试）：按伪 id 形态生成（不再推荐，
            会与真实 compiler 不兼容——调用方应传时间线）。
        project: M11.4——宿主工程（容器片段判定：嵌套/合成/虚拟宿主
            的命中显式跳过并留原因，不静默映射为波及整个容器的顶层
            删除）。None 时仅 composite_id 可判。

    review 项**不进计划**（人工排除后重跑检测再入计划）。
    """
    operations: List[EditOperation] = []
    skipped: List[str] = []

    # 收集全部 (hit, track, clip, s, e) 相交区间
    pending = []
    for hit in detection.deletable:
        if timeline is None:
            # 兼容路径：无时间线时无法解析归属——不产出非法 op
            skipped.append(
                f"{hit.text}@[{hit.start_frame},{hit.end_frame})：未提供"
                "时间线，无法映射到真实片段")
            continue
        intersections = _clips_intersecting_range(
            timeline, hit.start_frame, hit.end_frame)
        if not intersections:
            skipped.append(
                f"{hit.text}@[{hit.start_frame},{hit.end_frame})：不与任何"
                "片段相交（间隙？）")
            continue
        for track, clip, s, e in intersections:
            # M11.4：容器内命中——顶层 ripple 会波及整个容器，显式跳过
            if _is_container_clip(clip, project):
                skipped.append(
                    f"{hit.text}@[{hit.start_frame},{hit.end_frame})：命中"
                    "位于容器片段内"
                    f"（{clip.clip_id}——嵌套/合成），需进入容器内部"
                    "执行，不映射顶层删除")
                continue
            pending.append((hit, track, clip, s, e))

    # M11（S02）：ripple 删除会左移后续内容——op 按**起点降序**（从右
    # 到左）发射，先删右侧区间，左侧 op 的原始坐标保持有效。
    pending.sort(key=lambda item: item[3], reverse=True)
    op_index = 0
    for hit, track, clip, s, e in pending:
            covers_whole = (s <= clip.timeline_start
                            and e >= clip.timeline_end)
            if covers_whole:
                # 整段命中：整段删除（区间两界即片段两界，compiler 的
                # RangeRippleDelete 拒绝零宽残留——用无区间的整段语义）
                params = {
                    "track_id": track.track_id,
                    "clip_id": clip.clip_id,
                    "reason": f"filler:{hit.text}",
                }
            else:
                params = {
                    "track_id": track.track_id,
                    "clip_id": clip.clip_id,
                    "start_frame": s,
                    "end_frame": e,
                    "reason": f"filler:{hit.text}",
                }
            operations.append(EditOperation(
                op_id=f"filler-{op_index}",
                kind=OperationKind.DELETE_CLIP,
                params=params,
                intent={"reason": "filler_removal",
                        "word": hit.text,
                        "confidence": round(hit.confidence, 3),
                        "hit_range": [hit.start_frame, hit.end_frame]},
            ))
            op_index += 1
    return EditPlan(
        plan_id=plan_id,
        base_revision=int(base_revision),
        target=target,
        mode=PlanMode.PREVIEW,
        protocol=TAEP_PROTOCOL_VERSION,
        title=title,
        operations=tuple(operations),
        rationale=("跳过：" + "；".join(skipped)) if skipped else "",
    )


__all__ = [
    "DEFAULT_FILLER_LEXICON",
    "FILLER_CONFIDENCE_FLOOR",
    "FillerHit",
    "FillerDetectionResult",
    "detect_filler_words",
    "timings_fingerprint",
    "build_filler_removal_plan",
]
