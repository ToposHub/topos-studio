"""批改名计划协议（B 阶段，总计划 B 表第 7 行）。

规则：preview 返回**计划 id、版本与源身份指纹**；execute 只执行已确认
计划里记录的 (源→目标) 对——源在预览后发生变化（size/mtime 指纹不符）
则整体拒绝、要求重新预览，绝不现场重算另一份计划偷偷执行。

模式：template（{name}/{ext}/{seq}/{date}/{camera}）、replace、sequence。
命名模板通过独立纯函数渲染。
"""
from __future__ import annotations

import hashlib
import copy
import os
import secrets
import stat
import threading
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.features.files.repository.catalog import FilesCatalog, nfc
from src.features.files.transactions.service import FileOperationService, ItemOutcome

PLAN_VERSION = 1
_MAX_ITEMS = 5000


class PlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceIdentity:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    mode: int

    @classmethod
    def capture(cls, path: Path) -> "SourceIdentity":
        stat = path.lstat()
        return cls(path=nfc(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                   device=stat.st_dev, inode=stat.st_ino, mode=stat.st_mode)

    def matches_disk(self) -> bool:
        try:
            current = Path(self.path).lstat()
        except OSError:
            return False
        return (current.st_size == self.size
                and current.st_mtime_ns == self.mtime_ns
                and current.st_dev == self.device and current.st_ino == self.inode
                and current.st_mode == self.mode)


@dataclass
class PlanItem:
    source: str
    current_name: str
    proposed_name: str
    conflict: bool = False
    reason: str = ""


@dataclass
class BatchPlan:
    plan_id: str
    version: int
    mode: str
    items: list[PlanItem] = field(default_factory=list)
    identities: dict[str, SourceIdentity] = field(default_factory=dict)
    consumed: bool = False
    library_id: str = ""

    @property
    def has_conflicts(self) -> bool:
        return any(item.conflict for item in self.items)


_TEMPLATE_TOKEN = re.compile(r"\{(name|ext|seq|date|camera)(?::(\d+))?\}")


def render_template(template: str, *, name: str, ext: str, seq: int,
                    date: str, camera: str) -> str:
    """极简模板渲染；未知 token 原样保留（可读性优先，不静默吞）。

    ``{seq:N}`` 支持显式宽度（UI 批改名默认模板 ``{name}_{seq:3}`` 依赖它）；
    其余 token 的 ``:N`` 后缀按未知 token 原样保留。
    """

    def _sub(match: re.Match) -> str:
        token, width = match.group(1), match.group(2)
        if token == "seq" and width:
            return f"{seq:0{max(1, min(9, int(width)))}d}"
        values = {"name": name, "ext": ext, "seq": f"{seq:03d}",
                  "date": date, "camera": camera or "unknown"}
        return values[token]

    return _TEMPLATE_TOKEN.sub(_sub, template)


class BatchRenamePlanner:
    """preview → （用户确认）→ execute 的两段式协议。"""

    def __init__(self, catalog: FilesCatalog,
                 service: FileOperationService | None = None) -> None:
        self._catalog = catalog
        self._service = service or FileOperationService(catalog)
        self._plans: dict[str, BatchPlan] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ preview

    def preview(self, library_id: str, sources: list[str | Path], mode: str,
                options: dict[str, Any]) -> BatchPlan:
        if mode not in ("template", "replace", "sequence"):
            raise PlanError("BAD_MODE", f"未知批改名模式 {mode}")
        if not sources:
            raise PlanError("EMPTY", "没有可预览的源")
        if len(sources) > _MAX_ITEMS:
            raise PlanError("TOO_MANY", f"单批上限 {_MAX_ITEMS}")

        paths = [Path(os.path.abspath(s)) for s in sources]
        for path in paths:
            if not path.exists():
                raise PlanError("SOURCE_MISSING", f"源不存在: {path.name}")
            if path.is_symlink():
                raise PlanError("SOURCE_SYMLINK", "批改名不接受符号链接")
        if len({nfc(p) for p in paths}) != len(paths):
            raise PlanError("DUPLICATE_SOURCE", "源列表存在重复项")

        plan = BatchPlan(plan_id=secrets.token_hex(16),
                         version=PLAN_VERSION, mode=mode, library_id=library_id)
        proposed_names = self._propose(library_id, paths, mode, options)

        taken = {p.name for p in paths}  # 批内现状名参与冲突判断
        for path, proposed in zip(paths, proposed_names):
            item = PlanItem(source=nfc(path), current_name=path.name,
                            proposed_name=proposed)
            if proposed in taken and proposed != path.name:
                item.conflict, item.reason = True, "批内目标重名"
            elif ((path.parent / proposed).exists() or (path.parent / proposed).is_symlink()) and proposed != path.name:
                item.conflict, item.reason = True, "目标已存在于磁盘"
            elif "/" in proposed or "\\" in proposed or "\0" in proposed or proposed in ("", ".", ".."):
                item.conflict, item.reason = True, "非法名字"
            else:
                taken.add(proposed)
            plan.items.append(item)
            plan.identities[item.source] = SourceIdentity.capture(path)
        with self._lock:
            if len(self._plans) >= 128:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan.plan_id] = plan
        return copy.deepcopy(plan)

    def _propose(self, library_id: str, paths: list[Path], mode: str,
                 options: dict[str, Any]) -> list[str]:
        if mode == "replace":
            find = str(options.get("find") or "")
            if not find:
                raise PlanError("BAD_OPTIONS", "replace 模式需要 find")
            replace = str(options.get("replace") or "")
            return [p.name.replace(find, replace) for p in paths]
        if mode == "sequence":
            prefix = str(options.get("prefix") or "")
            start = int(options.get("start") or 1)
            width = max(1, min(6, int(options.get("width") or 3)))
            keep_ext = bool(options.get("keep_ext", True))
            names = []
            for index, path in enumerate(paths):
                stem = f"{prefix}{start + index:0{width}d}"
                names.append(f"{stem}{path.suffix}" if keep_ext else stem)
            return names
        # template
        template = str(options.get("template") or "")
        if not template:
            raise PlanError("BAD_OPTIONS", "template 模式需要 template")
        names = []
        for index, path in enumerate(paths):
            row = self._catalog.asset(library_id, path) or {}
            date_value = row.get("date_taken") or row.get("mtime") or time.time()
            date_text = time.strftime("%Y%m%d", time.localtime(date_value))
            names.append(render_template(
                template, name=path.stem, ext=path.suffix.lstrip("."),
                seq=index + 1, date=date_text, camera=str(row.get("camera") or "")))
        return names

    @staticmethod
    def _fingerprint(paths: list[Path], mode: str, options: dict[str, Any]) -> str:
        digest = hashlib.sha256()
        digest.update(mode.encode("utf-8"))
        digest.update(repr(sorted(options.items())).encode("utf-8"))
        for path in sorted(nfc(p) for p in paths):
            digest.update(path.encode("utf-8"))
        return digest.hexdigest()[:16]

    # ------------------------------------------------------------ execute

    def execute(self, plan_id: str, library_id: str) -> list[ItemOutcome]:
        with self._catalog._lock, self._lock:
            return self._execute_locked(plan_id, library_id)

    def _execute_locked(self, plan_id: str, library_id: str) -> list[ItemOutcome]:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise PlanError("PLAN_UNKNOWN", "计划不存在（会话内有效，请重新预览）")
        if plan.consumed:
            raise PlanError("PLAN_CONSUMED", "计划已执行，不得重复执行")
        if plan.library_id != library_id:
            raise PlanError("LIBRARY_MISMATCH", "计划不属于当前素材库")
        if plan.version != PLAN_VERSION:
            raise PlanError("PLAN_VERSION", "计划版本不被支持")
        if plan.has_conflicts:
            raise PlanError("PLAN_HAS_CONFLICTS", "计划含冲突项，先解决再执行")

        stale = [identity.path for identity in plan.identities.values()
                 if not identity.matches_disk()]
        if stale:
            raise PlanError(
                "STALE_PLAN",
                f"{len(stale)} 个源在预览后发生变化（如 {Path(stale[0]).name}），"
                "请重新预览生成新计划")

        for item in plan.items:
            target = Path(item.source).parent / item.proposed_name
            if item.proposed_name != item.current_name and (target.exists() or target.is_symlink()):
                raise PlanError("STALE_PLAN", "预览后目标被占用，请重新预览")

        plan.consumed = True  # 先占位：执行中途失败也不允许原样重放
        outcomes: list[ItemOutcome] = []
        for item in plan.items:
            if item.proposed_name == item.current_name:
                outcomes.append(ItemOutcome(item.source, item.source,
                                            status="done", code="UNCHANGED"))
                continue
            outcomes.append(self._service.rename(
                library_id, item.source, item.proposed_name, policy="error"))
        return outcomes

    # ------------------------------------------------------------ 查询

    def get(self, plan_id: str) -> BatchPlan | None:
        with self._lock:
            return copy.deepcopy(self._plans.get(plan_id))
