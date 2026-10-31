"""打标步骤的统一接口 + 执行结果。

每个 step 是一个独立的处理单元（如"镜头切割"、"ASR 转写"），
接收一个素材（media_source）+ 仓储（repo），产出打标数据写入 DB。

step 的设计要点：
- **幂等**：同一素材重复跑同一 step 不破坏已有数据（用 status 字段跳过）
- **可失败**：抛 StepFailed，编排器记录失败但不中断整批
- **可跳过**：is_done() 判断该素材这一步是否已完成（断点续跑）
- **不直接加载模型**：模型由 provider 管理，step 只调 provider
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from apps.topos_vault.db.repository import MediaSource, VaultRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #

class StepFailed(Exception):
    """单个步骤执行失败。"""

    def __init__(self, step_name: str, fingerprint: str, reason: str, *,
                 recoverable: bool = True):
        self.step_name = step_name
        self.fingerprint = fingerprint
        self.recoverable = recoverable
        super().__init__(f"[{step_name}] {fingerprint}: {reason}")


class StepSkipped(Exception):
    """步骤被跳过（素材类型不符 / 依赖未就绪 / 已完成）。"""

    def __init__(self, step_name: str, fingerprint: str, reason: str):
        self.step_name = step_name
        self.fingerprint = fingerprint
        super().__init__(f"[{step_name}] {fingerprint}: {reason}（跳过）")


# ---------------------------------------------------------------------- #
# 步骤基类
# ---------------------------------------------------------------------- #

class PipelineStep(ABC):
    """打标步骤基类。

    子类实现 ``_run()`` 和 ``is_done()``。
    编排器调 ``execute()``，它处理跳过/失败/日志的公共逻辑。
    """

    step_name: str = "base"           # 步骤名（日志/状态用）
    applies_to: tuple[str, ...] = ()  # 适用的 media_type（空 = 所有）

    def __init__(self, repo: VaultRepository):
        self.repo = repo

    def execute(self, source: MediaSource) -> "StepResult":
        """执行步骤（公共逻辑：类型检查 + 跳过 + 异常处理）。"""
        # 类型检查
        if self.applies_to and source.media_type not in self.applies_to:
            return StepResult.skipped(source.fingerprint, self.step_name,
                                      f"类型 {source.media_type} 不适用")

        # 断点续跑：已完成则跳过
        try:
            if self.is_done(source):
                return StepResult.skipped(source.fingerprint, self.step_name, "已完成")
        except Exception as e:
            logger.warning("is_done 检查失败 %s: %s", source.fingerprint, e)

        # 执行
        try:
            detail = self._run(source)
            return StepResult.done(source.fingerprint, self.step_name, detail or "")
        except StepSkipped as e:
            return StepResult.skipped(source.fingerprint, self.step_name, str(e))
        except StepFailed as e:
            logger.error("步骤失败 %s: %s", source.fingerprint, e)
            return StepResult.failed(source.fingerprint, self.step_name, str(e),
                                     recoverable=e.recoverable)
        except Exception as e:
            # 未预期的异常 → 默认可恢复（单个素材失败不中断整批）
            # 只有明确标记不可恢复的（如 ModelPackNotInstalled）才中断后续步骤
            logger.warning("步骤未预期异常 %s/%s: %s", source.fingerprint, self.step_name, e)
            return StepResult.failed(source.fingerprint, self.step_name,
                                     f"未预期异常：{e}", recoverable=True)

    @abstractmethod
    def _run(self, source: MediaSource) -> str:
        """实际执行逻辑（子类实现）。

        Returns:
            完成详情（人类可读，记入日志/状态）。

        Raises:
            StepSkipped: 主动跳过（依赖未就绪等）。
            StepFailed: 执行失败。
        """
        ...

    def is_done(self, source: MediaSource) -> bool:
        """该素材的此步骤是否已完成（断点续跑用）。

        默认实现：检查 source.path_status，子类可覆盖检查更细粒度的状态
        （如 clip.status / transcript_line 是否存在）。
        """
        return False


# ---------------------------------------------------------------------- #
# 执行结果
# ---------------------------------------------------------------------- #

class StepStatus(str, Enum):
    DONE = "done"           # 成功完成
    SKIPPED = "skipped"     # 跳过（类型不符/已完成/依赖未就绪）
    FAILED = "failed"       # 失败


@dataclass
class StepResult:
    """单个步骤的执行结果。"""
    fingerprint: str
    step_name: str
    status: StepStatus
    detail: str = ""
    recoverable: bool = True

    @classmethod
    def done(cls, fp: str, step: str, detail: str = "") -> "StepResult":
        return cls(fp, step, StepStatus.DONE, detail)

    @classmethod
    def skipped(cls, fp: str, step: str, reason: str = "") -> "StepResult":
        return cls(fp, step, StepStatus.SKIPPED, reason)

    @classmethod
    def failed(cls, fp: str, step: str, reason: str = "", *, recoverable: bool = True) -> "StepResult":
        return cls(fp, step, StepStatus.FAILED, reason, recoverable=recoverable)

    @property
    def is_done(self) -> bool:
        return self.status == StepStatus.DONE

    @property
    def is_failed(self) -> bool:
        return self.status == StepStatus.FAILED
