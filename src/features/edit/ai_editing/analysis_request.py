"""TAEP AnalysisRequest 数据合同（M2 分析数据地基）。

把"分析一个素材/一批素材"从"UI 直接调 provider"升级为**类型化、需用户确认的请求合同**
（产品计划 §17.3 / §18.2 / 总体不变量 §1.2「未经用户确认不得分析整批素材」）。

核心问题（M2 审查）：

- 旧 ``AnalysisService.request_analysis`` 是"直接执行"路径：调用方给 asset_id + provider，
  service 立刻跑。没有"用户已确认分析哪些素材、用什么配方、是否上传云"的显式合同。
- 一旦接真实 provider（WhisperX/OCR/云 ASR），任何代码路径（包括模型/脚本）都能触发
  整批素材分析或云上传，绕过确认。

本模块的不可变约束：

1. ``AnalysisRequest`` 是分析的唯一输入合同；``recipe`` / ``provider_ids`` /
   ``data_policy`` / ``budget`` / ``requested_by`` 都是显式字段，不是隐式默认。
2. **``user_confirmed_at`` 为 None 的 request，scheduler 必须拒绝执行**（§17.3）。
   Assistant / Skill 只能创建未确认的 proposal；用户从媒体池或确认卡确认后才置位。
3. 云分析（``data_policy=APPROVED_CLOUD``）必须由调用方先获得用户对"上传范围/费用/保留"
   的单独确认；本合同只记录 policy，不替代确认。
4. 请求不保存 secret / 绝对路径 / 原始媒体；只存 asset_ids、配方、预算、来源标识。

数据合同遵循整帧/有理数时间基（§18.2）：provider 原始 PTS 经 ``TimebaseService`` 转
canonical source ``FrameRange``；本模块不存浮点秒。
"""

from __future__ import annotations

import time as _time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------- #
# schema version（工程 payload 迁移用）
# ---------------------------------------------------------------------- #


#: 本数据合同的 schema 版本。结构变更必须 bump 并提供迁移。
ANALYSIS_REQUEST_SCHEMA_VERSION: str = "topos.analysis_request/v1"

#: 工程 payload 中存放 schema 版本的 key（与既有 caption/editplan schema 约定一致）。
ANALYSIS_REQUEST_SCHEMA_VERSION_KEY: str = "analysis_request_schema_version"


# ---------------------------------------------------------------------- #
# 分析配方（Recipe）——渐进式成本（§17.4）
# ---------------------------------------------------------------------- #


class AnalysisRecipe(str, Enum):
    """分析配方：固定组合的 provider 能力包，而非"完整/不完整"二元按钮。

    用户可勾掉敏感/昂贵模块（人脸/OCR/云端）；Agent/Skill 也只能建议受当前 policy
    允许的 recipe（§17.4）。配方是可编辑的默认组合。
    """

    FAST_LOCAL = "fast_local"
    """媒体探测、波形/响度、镜头边界、关键帧。本地、低资源。手工剪辑轻辅助。"""

    EDITORIAL_LOCAL = "editorial_local"
    """FAST_LOCAL + 本地 ASR、说话人分离、节拍/音乐检测。口播/访谈/粗剪。"""

    VISUAL_SEMANTIC_LOCAL = "visual_semantic_local"
    """EDITORIAL_LOCAL + 本地 OCR、图像/视频 embedding、镜头 caption/tag、质量估计。
    产品片、Vlog、B-roll。本地、较高 GPU/存储。"""

    CLOUD_ENHANCED = "cloud_enhanced"
    """用户批准的云 ASR/vision/video provider。更高质量或本地缺失字段。
    **明示上传、费用、保留期**；``data_policy`` 必须为 ``APPROVED_CLOUD``。"""


class AnalysisDataPolicy(str, Enum):
    """数据策略：本地优先，云端另行同意（§17.1）。"""

    LOCAL_ONLY = "local_only"
    """只跑本地 provider；不上传任何媒体/转录/关键帧。"""

    APPROVED_CLOUD = "approved_cloud"
    """允许上传到用户批准的云 provider。调用方必须已获得对"上传范围/费用/保留"的
    单独确认；本合同只记录 policy，不替代确认。"""


class RequestState(str, Enum):
    """AnalysisRequest 的状态机。

    ``PROPOSED → CONFIRMED → RUNNING → COMPLETED`` 为主路径；
    取消/失败/部分完成见各自状态。
    """

    PROPOSED = "proposed"
    """Agent/UI 创建的提议；``user_confirmed_at`` 为 None。scheduler 拒绝执行。"""

    CONFIRMED = "confirmed"
    """用户已确认（``user_confirmed_at`` 已置位）；等待 scheduler 执行。"""

    RUNNING = "running"
    """scheduler 已接受并开始执行。"""

    COMPLETED = "completed"
    """全部 asset + 全部 recipe 字段成功完成。"""

    PARTIAL = "partial"
    """取消后保留已完成的独立字段；不得报告为 complete（§17.7）。"""

    FAILED = "failed"
    """执行失败（provider 错误/模型缺失/VRAM 不足等）。"""

    CANCELLED = "cancelled"
    """用户/系统取消；已成功的独立 evidence 可由用户保留。"""

    @property
    def is_terminal(self) -> bool:
        return self in (RequestState.COMPLETED, RequestState.PARTIAL,
                        RequestState.FAILED, RequestState.CANCELLED)

    @property
    def is_executable(self) -> bool:
        """只有 CONFIRMED 状态可被 scheduler 接受执行。"""
        return self == RequestState.CONFIRMED


# ---------------------------------------------------------------------- #
# 预算（§10.2 成本/配额/滥用保护）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalysisBudget:
    """单次分析请求的预算上限。

    超预算改为等待确认（§10.2）；None 表示该项不设硬上限（仍受 profile 全局限额约束）。
    """

    max_duration_seconds: float | None = None
    """单次请求最长执行时长（秒）；超时进入 FAILED/PARTIAL。"""

    max_cost_units: float | None = None
    """成本上限（货币或 token 单位，由 provider 解释）；云端配方建议非 None。"""

    max_assets: int | None = None
    """本请求最多覆盖的素材数；防止误把"分析全部素材"扩到超大工程。"""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.max_duration_seconds is not None:
            d["max_duration_seconds"] = self.max_duration_seconds
        if self.max_cost_units is not None:
            d["max_cost_units"] = self.max_cost_units
        if self.max_assets is not None:
            d["max_assets"] = self.max_assets
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AnalysisBudget":
        return AnalysisBudget(
            max_duration_seconds=_opt_float(data.get("max_duration_seconds")),
            max_cost_units=_opt_float(data.get("max_cost_units")),
            max_assets=_opt_int(data.get("max_assets")),
        )


# ---------------------------------------------------------------------- #
# AnalysisRequest
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalysisRequest:
    """类型化分析请求合同（M2 / §17.3）。

    不可变。``propose_analysis`` 创建未确认副本（``user_confirmed_at=None``）；
    ``confirm_analysis`` 返回置位的副本。状态转换由 ``AnalysisService`` 持有，
    本 dataclass 只描述"这一份请求是什么"。

    Attributes:
        request_id: 稳定唯一 id（``uuid4``）。
        project_id: 工程绑定；跨工程 request 无效。
        asset_ids: 本次分析的素材范围（tuple，不可变）。
        recipe_id: 分析配方（决定跑哪些 provider 能力）。
        provider_ids: 实际允许的 provider（recipe 的子集或具体选择）。
        data_policy: 本地 / 已批准云。云需调用方先单独确认上传范围。
        budget: 时长/成本/素材数上限。
        requested_by: 发起方标识（``media_pool`` / ``assistant`` / ``skill``），不含 secret。
        user_confirmed_at: 用户确认时间戳；**None 时 scheduler 必须拒绝执行**（§17.3）。
        project_revision: 创建时工程 revision，用于 stale 判定（工程改动后 request 失效）。
        state: 当前状态（PROPOSED/CONFIRMED/RUNNING/...）。
        created_at: 创建时间戳。
        title: 可选人类可读标题（确认卡展示用）。
    """

    request_id: str
    project_id: str
    asset_ids: tuple[str, ...]
    recipe_id: AnalysisRecipe
    provider_ids: tuple[str, ...]
    data_policy: AnalysisDataPolicy
    budget: AnalysisBudget = field(default_factory=AnalysisBudget)
    requested_by: str = ""
    user_confirmed_at: float | None = None
    project_revision: int | None = None
    state: RequestState = RequestState.PROPOSED
    created_at: float = 0.0
    title: str = ""

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.project_id:
            raise ValueError("project_id cannot be empty")
        if not self.asset_ids:
            raise ValueError("asset_ids cannot be empty (scope is required)")
        if any(not str(a) for a in self.asset_ids):
            raise ValueError("asset_ids must be non-empty strings")
        if not isinstance(self.recipe_id, AnalysisRecipe):
            raise TypeError(f"recipe_id must be AnalysisRecipe, got {type(self.recipe_id)}")
        if not isinstance(self.data_policy, AnalysisDataPolicy):
            raise TypeError(f"data_policy must be AnalysisDataPolicy, got {type(self.data_policy)}")
        # 云配方必须搭配云策略（防御性：配方与策略一致性）
        if self.recipe_id == AnalysisRecipe.CLOUD_ENHANCED and self.data_policy != AnalysisDataPolicy.APPROVED_CLOUD:
            raise ValueError(
                "CLOUD_ENHANCED recipe requires data_policy=APPROVED_CLOUD "
                "(cloud upload needs explicit user consent)"
            )

    @property
    def is_confirmed(self) -> bool:
        """用户是否已确认（§17.3：未确认的 request scheduler 必须拒绝执行）。"""
        return self.user_confirmed_at is not None

    @property
    def is_cloud(self) -> bool:
        """是否涉及云上传（UI 据此额外提示上传范围/费用/保留）。"""
        return self.data_policy == AnalysisDataPolicy.APPROVED_CLOUD

    def with_confirmation(self, confirmed_at: float) -> "AnalysisRequest":
        """返回一份已确认的副本（``user_confirmed_at`` 置位，``state=CONFIRMED``）。

        不可变 dataclass 的标准转换入口；不修改 self。
        """
        return replace(
            self,
            user_confirmed_at=confirmed_at,
            state=RequestState.CONFIRMED,
        )

    def with_state(self, new_state: RequestState) -> "AnalysisRequest":
        """返回一份状态变更后的副本（scheduler 驱动状态机用）。"""
        return replace(self, state=new_state)

    def to_dict(self) -> dict[str, Any]:
        """稳定序列化（工程 payload；sort_keys 用于 hash/比较）。

        不保存 secret / 绝对路径 / 原始媒体；``requested_by`` 只存来源标识。
        """
        d: dict[str, Any] = {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "asset_ids": list(self.asset_ids),
            "recipe_id": self.recipe_id.value,
            "provider_ids": list(self.provider_ids),
            "data_policy": self.data_policy.value,
            "budget": self.budget.to_dict(),
            "requested_by": self.requested_by,
            "state": self.state.value,
            "created_at": self.created_at,
            ANALYSIS_REQUEST_SCHEMA_VERSION_KEY: ANALYSIS_REQUEST_SCHEMA_VERSION,
        }
        if self.user_confirmed_at is not None:
            d["user_confirmed_at"] = self.user_confirmed_at
        if self.project_revision is not None:
            d["project_revision"] = self.project_revision
        if self.title:
            d["title"] = self.title
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AnalysisRequest":
        """从 dict 重建（保留未知字段由调用方处理；本处只读已知键）。

        对缺失/未知字段宽容：旧 payload 缺 ``field_provenance`` 等新字段时仍可加载。
        """
        return AnalysisRequest(
            request_id=str(data["request_id"]),
            project_id=str(data["project_id"]),
            asset_ids=tuple(str(a) for a in data.get("asset_ids", ())),
            recipe_id=AnalysisRecipe(str(data.get("recipe_id", AnalysisRecipe.FAST_LOCAL.value))),
            provider_ids=tuple(str(p) for p in data.get("provider_ids", ())),
            data_policy=AnalysisDataPolicy(
                str(data.get("data_policy", AnalysisDataPolicy.LOCAL_ONLY.value))
            ),
            budget=AnalysisBudget.from_dict(data.get("budget", {})),
            requested_by=str(data.get("requested_by", "")),
            user_confirmed_at=_opt_float(data.get("user_confirmed_at")),
            project_revision=_opt_int(data.get("project_revision")),
            state=RequestState(str(data.get("state", RequestState.PROPOSED.value))),
            created_at=float(data.get("created_at", 0.0)),
            title=str(data.get("title", "")),
        )

    @staticmethod
    def create(
        project_id: str,
        asset_ids: tuple[str, ...] | list[str],
        *,
        recipe_id: AnalysisRecipe = AnalysisRecipe.FAST_LOCAL,
        provider_ids: tuple[str, ...] | list[str] = (),
        data_policy: AnalysisDataPolicy = AnalysisDataPolicy.LOCAL_ONLY,
        budget: AnalysisBudget | None = None,
        requested_by: str = "",
        project_revision: int | None = None,
        title: str = "",
        now: float | None = None,
    ) -> "AnalysisRequest":
        """便捷构造：生成 request_id / created_at；默认未确认（PROPOSED）。

        ``provider_ids`` 为空时由 scheduler 按 recipe 默认填充。
        """
        return AnalysisRequest(
            request_id=str(uuid.uuid4()),
            project_id=project_id,
            asset_ids=tuple(asset_ids),
            recipe_id=recipe_id,
            provider_ids=tuple(provider_ids),
            data_policy=data_policy,
            budget=budget or AnalysisBudget(),
            requested_by=requested_by,
            user_confirmed_at=None,
            project_revision=project_revision,
            state=RequestState.PROPOSED,
            created_at=now if now is not None else _time.time(),
            title=title,
        )


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    return int(v)
