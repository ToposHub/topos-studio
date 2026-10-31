"""渲染/编译诊断 — 线程安全的近期 shader 编译失败环形缓冲。

本模块是 AI 助手「受限诊断」能力的数据源（对应工具
``topos.get_render_diagnostics``）。它让 Agent 能回答「为什么我的效果/
着色器没有生效」这类问题，而**无需读取任何文件、源码或路径**。

设计约束（TAEP §7 / §12 红线）：
  * 只捕获**运行时编译输出**（GLSL/driver 报错 + 失败涉及的标识符）。
  * **不读取文件**，不存储绝对路径，不保留 shader 源码。
  * 失败涉及的「主体」用标识符表示（color_pipeline/composition 传 plugin_id，
    effect_pass 传 effect_type）——都是项目内 basename/枚举值，不含路径。
  * 写入方（shader_compiler / effect_pass_runner，运行在渲染线程）调用
    :func:`record_compile_failure`，该函数**永不抛异常**——诊断收集的 bug
    绝不能拖垮渲染。
  * 读取方（tool_facade）通过 :func:`get_render_diagnostic_sink` 拿到单例，
    再对返回字符串做 ``sanitize_untrusted`` 标记后喂给模型。

分层：本模块在 ``src/shared``，color core 与 shared effects 都可写，
ai_editing feature 只读——不产生任何反向依赖。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_DEFAULT_CAPACITY = 50
# 单条 GLSL 报错最长保留字符数（driver 偶尔会吐很长文本，截断避免环形缓冲膨胀）。
_MAX_MESSAGE_LEN = 4000
_MAX_IDENTIFIER_LEN = 128
CODE_SHADER_COMPILE_FAILED = "SHADER_COMPILE_FAILED"
CODE_SHADER_UNDECLARED_UNIFORM = "SHADER_UNDECLARED_UNIFORM"
CODE_EFFECT_PASS_COMPILE_FAILED = "EFFECT_PASS_COMPILE_FAILED"

# 合法的来源标签：权威 enum 在 AI 工具的 tool schema（tool_facade，
# 使用下方 SOURCE_* 常量构造），与此处保持一致。
SOURCE_COLOR_PIPELINE = "color_pipeline"
SOURCE_COMPOSITION = "composition"
SOURCE_EFFECT_PASS = "effect_pass"

# AUD-37200: 合法来源集合（此前审计指出该集合"定义后从未使用"——
# 现供 record() 做告警级校验；未知 source 仍照常记录，仅补诊断日志，
# 不改变写入行为）。
_VALID_SOURCES = frozenset(
    {SOURCE_COLOR_PIPELINE, SOURCE_COMPOSITION, SOURCE_EFFECT_PASS}
)


def _normalize_context_identifier(value: object | None) -> str:
    """Normalize a correlation id without retaining path syntax."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)
    return text[:_MAX_IDENTIFIER_LEN]


def _normalize_context_identifiers(values: Iterable[object] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    if values is None:
        candidates: Iterable[object] = ()
    elif isinstance(values, (str, bytes)):
        candidates = (values,)
    else:
        try:
            iter(values)
            candidates = values
        except TypeError:
            candidates = (values,)
    for value in candidates:
        identifier = _normalize_context_identifier(value)
        if identifier and identifier not in seen:
            seen.add(identifier)
            normalized.append(identifier)
    return tuple(normalized)


@dataclass(frozen=True)
class CompileDiagnostic:
    """一条捕获到的 shader/program 编译失败。

    Attributes:
        timestamp: ``time.time()`` 秒级时间戳。
        source: 来源标签（color_pipeline / composition / effect_pass）。
        severity: 严重级别（当前只有 "error"）。
        code: 稳定的机器可读诊断代码。
        subjects: 失败涉及的标识符（plugin_id 列表 或 effect_type），**不含路径**。
        message: GLSL/driver 编译报错原文（已截断）。
        request_id: 触发本次编译的渲染请求关联 ID。
        layer_id: 触发失败的图层关联 ID。
        plugin_ids: 本次 composition 编译涉及的插件 ID。
    """

    timestamp: float
    source: str
    severity: str
    subjects: tuple[str, ...]
    message: str
    code: str = CODE_SHADER_COMPILE_FAILED
    request_id: str = ""
    layer_id: str = ""
    plugin_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "severity": self.severity,
            "code": self.code,
            "subjects": list(self.subjects),
            "message": self.message,
            "request_id": self.request_id,
            "layer_id": self.layer_id,
            "plugin_ids": list(self.plugin_ids),
        }


class RenderDiagnosticSink:
    """线程安全、容量有界的近期编译诊断环形缓冲。"""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, int(capacity))
        self._entries: list[CompileDiagnostic] = []
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(
        self,
        *,
        source: str,
        message: str,
        subjects: Iterable[str] | None = None,
        severity: str = "error",
        code: str | None = None,
        request_id: str | None = None,
        layer_id: str | None = None,
        plugin_ids: Iterable[str] | None = None,
    ) -> CompileDiagnostic:
        """追加一条诊断。返回写入的（已规整的）条目。

        ``subjects`` 去重并转字符串；``message`` 截断到 ``_MAX_MESSAGE_LEN``。
        """
        # dedupe + str-coerce subjects, 保留顺序
        subs: list[str] = []
        seen: set[str] = set()
        if subjects is None:
            subject_values: Iterable[object] = ()
        elif isinstance(subjects, (str, bytes)):
            subject_values = (subjects,)
        else:
            try:
                iter(subjects)
                subject_values = subjects
            except TypeError:
                subject_values = (subjects,)
        for s in subject_values:
            # AUD-37201: subjects 与其余 ID 字段一致走防路径规整
            # （模块契约要求"不含路径"），不再只做 str 化去重。
            sid = _normalize_context_identifier(s)
            if sid and sid not in seen:
                seen.add(sid)
                subs.append(sid)
        msg = (message or "")[:_MAX_MESSAGE_LEN]
        # AUD-37200: 枚举外取值补告警（仍照常记录，不改写入行为）。
        if source not in _VALID_SOURCES:
            logger.warning(
                "render diagnostic source %r outside known set %s; "
                "recorded as-is",
                source,
                sorted(_VALID_SOURCES),
            )
        if severity != "error":
            logger.warning(
                "render diagnostic severity %r outside known set "
                "{'error'}; recorded as-is",
                severity,
            )
        entry = CompileDiagnostic(
            timestamp=time.time(),
            source=str(source),
            severity=str(severity),
            code=_normalize_context_identifier(code) or CODE_SHADER_COMPILE_FAILED,
            subjects=tuple(subs),
            message=msg,
            request_id=_normalize_context_identifier(request_id),
            layer_id=_normalize_context_identifier(layer_id),
            plugin_ids=_normalize_context_identifiers(plugin_ids),
        )
        with self._lock:
            self._entries.append(entry)
            overflow = len(self._entries) - self._capacity
            if overflow > 0:
                # 丢弃最旧的，保留最近 capacity 条
                del self._entries[:overflow]
        return entry

    def recent(
        self, *, limit: int = 20, source: str | None = None,
    ) -> list[CompileDiagnostic]:
        """返回最近的诊断（**最新在前**），可按来源过滤。"""
        limit = max(0, min(int(limit), self._capacity))
        with self._lock:
            snapshot = list(self._entries)
        if source:
            snapshot = [e for e in snapshot if e.source == source]
        snapshot.reverse()  # 最新在前
        return snapshot[:limit] if limit else []

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def count(self, *, source: str | None = None) -> int:
        with self._lock:
            if source:
                return sum(1 for e in self._entries if e.source == source)
            return len(self._entries)


# ---------------------------------------------------------------------- #
# 模块级单例（懒加载；测试可用 set_render_diagnostic_sink 替换/重置）
# ---------------------------------------------------------------------- #

_SINK: RenderDiagnosticSink | None = None
_SINK_LOCK = threading.Lock()


def get_render_diagnostic_sink() -> RenderDiagnosticSink:
    """返回全局诊断单例（首次调用时懒创建）。"""
    global _SINK
    if _SINK is None:
        with _SINK_LOCK:
            if _SINK is None:
                _SINK = RenderDiagnosticSink()
    return _SINK


def set_render_diagnostic_sink(sink: RenderDiagnosticSink | None) -> None:
    """替换/重置全局单例（测试用）。传 ``None`` 恢复为懒加载默认实例。"""
    global _SINK
    with _SINK_LOCK:
        _SINK = sink


def record_compile_failure(
    *,
    source: str,
    message: str,
    subjects: Iterable[str] | None = None,
    severity: str = "error",
    code: str | None = None,
    request_id: str | None = None,
    layer_id: str | None = None,
    plugin_ids: Iterable[str] | None = None,
) -> None:
    """从渲染热路径上报一次编译失败。

    **永不抛异常**：诊断收集自身的任何错误都只记 debug 日志后被吞掉——
    诊断绝不能成为渲染崩溃的原因。调用方（shader_compiler / effect_pass_runner）
    无需再自己包 try/except。
    """
    try:
        get_render_diagnostic_sink().record(
            source=source,
            message=message,
            subjects=subjects,
            severity=severity,
            code=code,
            request_id=request_id,
            layer_id=layer_id,
            plugin_ids=plugin_ids,
        )
    except Exception:  # noqa: BLE001 — 诊断不能破坏渲染
        logger.debug("record_compile_failure failed", exc_info=True)
