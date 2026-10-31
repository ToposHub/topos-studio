"""SUB-P2-06 / SUB-P2-11: FontResolver — font matching, fallback, preflight.

The legacy path (``text_node._build_font``) does ``QFont(family)`` directly with
no fallback, no availability check, no weight abstraction beyond the local
``_QT_WEIGHT_MAP``. This module is the Phase 2A foundation for:

* :class:`FontResolver` — resolves a ``font_family`` request into a concrete
  family + ordered fallback list, using Qt's font database. Result is cached
  (family availability queries are not free).
* :func:`weight_to_qt` — the single weight-name → ``QFont.Weight`` mapping,
  shared between the composite text node and caption renderer (the old code
  had two copies: ``text_node._QT_WEIGHT_MAP`` and the renderer's passthrough).
* :class:`PreflightIssue` / :func:`preflight_styles` — deliver-side check that
  every distinct ``font_family`` referenced by a sequence's cues is installed;
  missing fonts produce a ``PreflightIssue`` with a suggested substitute. Used
  by SUB-P2-11 deliver gate so a project never silently renders with a
  substitute font.

The resolver is Qt-aware but lazily imports ``PyQt6.QtGui``; the weight table
and fallback heuristics are pure-python so they can be unit-tested headless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Iterable, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────
# Weight mapping — single source of truth (was duplicated in text_node + renderer)
# ──────────────────────────────────────────────────────────────────────────

# Canonical weight name order (must match _SUBTITLE_FONT_WEIGHTS in timeline_v2
# and FONT_WEIGHTS in text_node).
FONT_WEIGHTS: tuple[str, ...] = ("light", "normal", "demibold", "bold", "black")

# Weight name → Qt numeric weight (QFont.Weight integer value).
WEIGHT_TO_QT: dict[str, int] = {
    "light": 300,
    "normal": 400,
    "demibold": 600,
    "bold": 700,
    "black": 900,
}


def weight_to_qt(weight_name: str) -> int:
    """Map a caption weight name to a ``QFont.Weight`` numeric value.

    Unknown names fall back to ``normal`` (400) rather than raising — a bad
    weight should never break rendering, only produce a warning via preflight.
    """
    return WEIGHT_TO_QT.get(str(weight_name), 400)


# ──────────────────────────────────────────────────────────────────────────
# FontResolver
# ──────────────────────────────────────────────────────────────────────────

# Built-in fallback chain per script class. Ordered most-specific first.
# These are the families we substitute when the requested family is missing.
_DEFAULT_FALLBACKS: tuple[str, ...] = (
    "Arial",                # ubiquitous sans — almost always present
    "Helvetica",            # macOS default
    "PingFang SC",          # macOS Simplified Chinese
    "PingFang TC",          # macOS Traditional Chinese
    "Hiragino Sans GB",     # macOS CJK fallback
    "Source Han Sans CN",   # macOS Adobe CJK fallback
    "DejaVu Sans",          # Linux default
    "Liberation Sans",      # RHEL/default
    "Noto Sans",            # Noto family — broad Unicode coverage
    "Noto Sans CJK SC",     # CJK Simplified
    "Noto Sans CJK TC",     # CJK Traditional
    "Noto Sans Arabic",     # Arabic
    "Noto Sans Hebrew",     # Hebrew
    ".SF NS",               # macOS system fallback
)


@dataclass
class FontResolution:
    """Result of resolving a ``font_family`` request.

    Attributes:
        requested: the family string the user/style asked for (verbatim).
        resolved: the concrete family to actually pass to ``QFont``. Equals
            ``requested`` when available, else the best available fallback.
        fallbacks: ordered list of fallback families appended after
            ``resolved`` so ``QFont.setFamilies`` can try them in turn.
        is_substitute: True if ``resolved != requested`` (font was missing).
        available: True if ``requested`` was found verbatim in the font db.
    """
    requested: str
    resolved: str
    fallbacks: tuple[str, ...]
    is_substitute: bool
    available: bool
    #: DESK-02A：resolved 是否来自工程内嵌字体（True = 本工程资源注册，
    #: 换机无预装字体也可正确布局； False = 系统字体库）。
    from_project: bool = False

    def families_for_qfont(self) -> tuple[str, ...]:
        """Return ``(resolved, *fallbacks)`` for ``QFont.setFamilies``."""
        return (self.resolved, *self.fallbacks)


class FontResolver:
    """Resolve caption font families against the system font database.

    Thread-safety: the resolver caches the available-family set and per-request
    resolutions. It is safe to construct per-use or share as a singleton; the
    underlying ``QFontDatabase`` queries are read-only.

    The Qt import is lazy so that importing this module does not require a
    running QApplication (the pure-python fallback heuristics work headless).
    Qt is only touched when ``available_families`` is first queried.
    """

    # Injected for testing: if set, used instead of querying QFontDatabase.
    _families_override: ClassVar[Optional[frozenset]] = None

    # DESK-02A：当前工程的内嵌字体族（类级——resolver 实例逐次创建，
    # 工程级状态随工程加载/关闭设置与清除；与 _families_override 同模式）。
    _project_families: ClassVar[frozenset] = frozenset()

    def __init__(self) -> None:
        self._cache: dict[str, FontResolution] = {}
        self._available: Optional[frozenset] = None

    @classmethod
    def set_project_families(cls, families: Optional[Iterable[str]]) -> None:
        """设置/清除当前工程的内嵌字体族（DESK-02A）。

        来自 ``ProjectFontRegistry.from_resources`` 的注册结果；工程
        加载时设置，关闭/换工程时传 None 清除。登记后这些族按"可用"
        参与一切实例的解析（换机无预装字体时字幕仍正确布局）。
        """
        cls._project_families = (
            frozenset(f for f in (families or ()) if f))
        # 短生命周期实例的缓存随实例消亡；类级集变化只发生在工程边界。
        # 存活实例（如同屏面板）缓存失效由其自身重建周期兜底。

    # ── Available families ──

    @classmethod
    def set_available_override(cls, families: Optional[Iterable[str]]) -> None:
        """Test hook: pin the set of "installed" families without Qt.

        Pass ``None`` to clear. When set, ``available_families`` returns this
        set directly and no Qt import happens — enables headless unit tests.
        """
        cls._families_override = frozenset(families) if families is not None else None

    @property
    def available_families(self) -> frozenset:
        """The set of font families Qt reports as installed.

        Cached on first access. Falls back to :data:`_DEFAULT_FALLBACKS` if Qt
        is unavailable (e.g. headless CI without display) so the resolver
        degrades gracefully rather than crashing.
        """
        # DESK-02A：工程字体恒参与（即使测试用 override 模拟"无预装
        # 字体机器"，内嵌字体仍可用——这正是该场景的存在意义）。
        if FontResolver._families_override is not None:
            return FontResolver._families_override | FontResolver._project_families
        if self._available is None:
            self._available = self._query_qt_families()
        return self._available | FontResolver._project_families

    @staticmethod
    def _query_qt_families() -> frozenset:
        """Query PyQt6's QFontDatabase for installed families.

        Wrapped in try/except so a missing/changed Qt API never breaks caption
        resolution. On any failure (including no QApplication for headless /
        AI-automation mode), falls back to a PIL-based filesystem scan so
        font resolution and export preflight still work without Qt.

        ⚠️ ``QFontDatabase.families()`` calls ``qFatal`` (not a Python exception)
        when no QGuiApplication exists, so we MUST check QApplication first.
        """
        # Guard: QFontDatabase.families() triggers qFatal without QApplication.
        try:
            from PyQt6.QtGui import QGuiApplication  # noqa: WPS433
            if QGuiApplication.instance() is None:
                return FontResolver._scan_filesystem_families()
        except Exception:
            return FontResolver._scan_filesystem_families()
        try:
            from PyQt6.QtGui import QFontDatabase  # noqa: WPS433 lazy import
            fams = QFontDatabase.families()
            if fams:
                return frozenset(fams)
        except Exception:
            pass
        return FontResolver._scan_filesystem_families()

    @staticmethod
    def _scan_filesystem_families() -> frozenset:
        """Scan system font directories for available font family names.

        Used when QFontDatabase is unavailable (headless export / AI automation).
        Returns a frozenset of family names derived from font file names
        (``.ttf``/``.ttc``/``.otf``).
        """
        import glob
        import os
        font_dirs = (
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            "C:/Windows/Fonts",
        )
        families: set[str] = set()
        # AUD-15811: 不再硬编码 {"Arial", "Helvetica", ...} 恒为"可用"——
        # 该集合让 headless preflight 在未安装这些字体的机器上误报
        # available，静默替换恰好绕过了 SUB-P2-11 的 deliver gate。
        # 可用性只来自文件系统扫描的真实命中。
        for d in font_dirs:
            if not os.path.isdir(d):
                continue
            # AUD-15811: 同时匹配 .otf（macOS 上大量 OTF-only 族，如
            # Source Han Sans），否则被误报缺失产生噪音告警。
            paths: list[str] = []
            for pattern in ("*.tt[fc]", "*.otf"):
                paths.extend(glob.glob(os.path.join(d, "**", pattern), recursive=True))
            for path in paths:
                base = os.path.splitext(os.path.basename(path))[0]
                # Clean up common suffixes: "Bold", "Regular", "Medium" etc.
                for suffix in (" Bold", " Regular", " Medium", " Light",
                               " Black", " Thin", " Italic", "-Bold", "-Regular"):
                    if base.endswith(suffix):
                        base = base[:-len(suffix)]
                families.add(base)
        return frozenset(families)

    # ── Resolution ──

    def resolve(self, family: str) -> FontResolution:
        """Resolve a requested family to a concrete family + fallback chain.

        * If ``family`` is available verbatim → resolved = family, available.
        * Else, pick the first available family from a fallback heuristic
          (script-aware: CJK / Arabic / Hebrew get their Noto families first),
          mark ``is_substitute=True``.
        * ``fallbacks`` always contains the remaining default chain so Qt can
          try them per-glyph.
        """
        if not family:
            family = "Arial"
        cached = self._cache.get(family)
        if cached is not None:
            return cached

        avail = self.available_families
        # Case-insensitive match (Qt families' casing varies by platform).
        avail_lower = {f.lower(): f for f in avail}

        if family in avail or family.lower() in avail_lower:
            resolved = family if family in avail else avail_lower[family.lower()]
            result = FontResolution(
                requested=family,
                resolved=resolved,
                fallbacks=tuple(self._pick_fallbacks(resolved, avail)),
                is_substitute=False,
                available=True,
                from_project=self._is_project_family(resolved),
            )
        else:
            # Missing — find best substitute.
            subst = self._pick_substitute(family, avail)
            result = FontResolution(
                requested=family,
                resolved=subst,
                fallbacks=tuple(self._pick_fallbacks(subst, avail)),
                is_substitute=True,
                available=False,
            )
        self._cache[family] = result
        return result

    def _is_project_family(self, family: str) -> bool:
        """DESK-02A：族名是否来自工程内嵌注册（大小写不敏感）。"""
        project = FontResolver._project_families
        return family in project or \
            family.lower() in {f.lower() for f in project}

    def _pick_substitute(self, requested: str, avail: frozenset) -> str:
        """Pick the best available substitute for a missing ``requested``."""
        avail_lower = {f.lower(): f for f in avail}
        # Script-aware candidates first.
        candidates = list(self._script_candidates(requested))
        # Then the default chain.
        candidates.extend(_DEFAULT_FALLBACKS)
        for cand in candidates:
            if cand in avail or cand.lower() in avail_lower:
                return cand if cand in avail else avail_lower[cand.lower()]
        # Nothing matched at all — return the requested name verbatim; Qt will
        # do its own internal fallback. We still report is_substitute=True so
        # preflight can warn.
        return requested

    @staticmethod
    def _script_candidates(requested: str) -> tuple[str, ...]:
        """Heuristic: suggest script-specific Noto families based on name."""
        name = requested.lower()
        if "cjk" in name or "chinese" in name or "han" in name or "song" in name \
                or "hei" in name or "ming" in name or "gothic" in name or "mincho" in name:
            return ("Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans CJK JP")
        if "arab" in name:
            return ("Noto Sans Arabic",)
        if "hebrew" in name or "jew" in name:
            return ("Noto Sans Hebrew",)
        if "jp" in name or "japan" in name:
            return ("Noto Sans CJK JP", "Noto Sans CJK SC")
        if "kr" in name or "korea" in name or "hangul" in name:
            return ("Noto Sans CJK KR", "Noto Sans CJK SC")
        return ()

    @staticmethod
    def _pick_fallbacks(resolved: str, avail: frozenset) -> tuple[str, ...]:
        """Build the fallback chain excluding the already-resolved family."""
        out: list[str] = []
        seen = {resolved.lower()}
        for cand in _DEFAULT_FALLBACKS:
            if cand.lower() in seen:
                continue
            if cand in avail:
                out.append(cand)
                seen.add(cand.lower())
        return tuple(out)

    # ── QFont construction ──

    def to_qfont(
        self,
        family: str,
        pixel_size: int,
        weight_name: str = "normal",
        italic: bool = False,
        letter_spacing_px: float = 0.0,
    ) -> Any:
        """Build a ``QFont`` with the resolved family + fallback chain.

        Mirrors the old ``text_node._build_font`` but routes the family through
        :meth:`resolve` so a missing font gets a real substitute + fallback
        list rather than silently rendering with Qt's default.

        Pixel size is used (not point size) to keep 1:1 output-pixel scaling
        regardless of screen DPI, matching the existing renderer contract.
        """
        from PyQt6.QtGui import QFont  # noqa: WPS433 lazy

        res = self.resolve(family)
        font = QFont()
        # setFamilies lets Qt try each family in order per-glyph — the right
        # way to handle CJK/emoji mixed into a latin family.
        try:
            font.setFamilies(list(res.families_for_qfont()))
        except AttributeError:
            # Older Qt: fall back to single family.
            font.setFamily(res.resolved)
        font.setPixelSize(max(1, int(pixel_size)))
        font.setWeight(QFont.Weight(weight_to_qt(weight_name)))
        font.setItalic(bool(italic))
        if letter_spacing_px != 0.0 and pixel_size > 0:
            pct = 100.0 + (letter_spacing_px / pixel_size) * 100.0
            try:
                font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, float(pct))
            except Exception:
                pass
        return font


# ──────────────────────────────────────────────────────────────────────────
# Preflight — deliver-side font availability check (SUB-P2-11)
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreflightIssue:
    """A single font-availability problem found during preflight.

    Attributes:
        severity: ``"error"`` (font definitely missing, no substitute found)
            or ``"warning"`` (missing but a substitute was resolved).
        requested_family: the missing family name.
        suggested_substitute: the family the resolver would use instead
            (empty string if none found).
        affected_clips: ids of cues referencing this family.
        message: human-readable description.
    """
    severity: str
    requested_family: str
    suggested_substitute: str
    affected_clips: tuple[str, ...]
    message: str


def preflight_fonts(
    family_to_clips: Dict[str, Tuple[str, ...]],
    resolver: Optional[FontResolver] = None,
) -> List[PreflightIssue]:
    """Check that every requested font family is available.

    Args:
        family_to_clips: mapping ``{family_name: (clip_id, ...)}`` — typically
            built by walking a sequence's cues and grouping by resolved
            ``font_family``. Use ``collect_families`` to build it.
        resolver: optional :class:`FontResolver` (a fresh one is created if
            omitted).

    Returns:
        List of :class:`PreflightIssue`, one per missing family. Empty list =
        all fonts available. Issues with a found substitute are ``warning``;
        issues where even the substitute chain failed are ``error``.
    """
    res = resolver or FontResolver()
    issues: list[PreflightIssue] = []
    for family, clip_ids in family_to_clips.items():
        if not family:
            continue
        result = res.resolve(family)
        if result.available:
            continue
        # is_substitute True + resolved differs from requested → warning.
        # resolved == requested (no substitute found) → error.
        if result.is_substitute and result.resolved != family:
            issues.append(PreflightIssue(
                severity="warning",
                requested_family=family,
                suggested_substitute=result.resolved,
                affected_clips=tuple(clip_ids),
                message=(f"Font '{family}' not found; will substitute "
                         f"'{result.resolved}' ({len(clip_ids)} cue(s))."),
            ))
        else:
            issues.append(PreflightIssue(
                severity="error",
                requested_family=family,
                suggested_substitute="",
                affected_clips=tuple(clip_ids),
                message=(f"Font '{family}' not found and no substitute "
                         f"available ({len(clip_ids)} cue(s))."),
            ))
    return issues


def collect_families(clips: Iterable[Any]) -> Dict[str, Tuple[str, ...]]:
    """Group clip ids by their ``font_family`` field.

    A convenience to build the ``family_to_clips`` map for
    :func:`preflight_fonts`. Clips without a ``font_family`` attribute are
    grouped under ``""`` (skipped by preflight).
    """
    out: dict[str, list[str]] = {}
    for clip in clips:
        fam = getattr(clip, "font_family", "") or ""
        cid = getattr(clip, "clip_id", "")
        out.setdefault(fam, []).append(cid)
    return {k: tuple(v) for k, v in out.items()}


# ──────────────────────────────────────────────────────────────────────────
# DESK-02A · 工程内嵌字体注册（按内容身份）
# ──────────────────────────────────────────────────────────────────────────
# 布局单位契约（跨端冻结）：caption 的 ``font_size`` 以**时间线分辨率的
# 像素**为单位（0 = 按画布高度自适应）；字距/位置同像素域。双端用同一
# 单位计算换行与锚点，不许一端 pt 一端 px。

@dataclass(frozen=True)
class ProjectFontRegistration:
    """一个工程内嵌字体的注册结果。

    Attributes:
        resource_uri: topos-res:// 引用（内容寻址，权威）。
        sha256: 字体文件内容哈希（诊断/去重）。
        local_path: 解包缓存内的本地文件（本机实现细节，不落盘）。
        family: Qt 报告的字体族名（注册失败为空串）。
        registered: 是否成功注册进 Qt 字体库。
        detail: 失败原因（成功为空串）。
    """

    resource_uri: str
    sha256: str
    local_path: str
    family: str
    registered: bool
    detail: str = ""


@dataclass(frozen=True)
class ProjectFontRegistry:
    """工程字体注册表（DESK-02A）。

    从工程资源索引把 ``kind == "font"`` 的资源物化并注册进 Qt 字体库
    （``QFontDatabase.addApplicationFont``，需 QGuiApplication——headless
    导出/AI 自动化下注册跳过并留诊断，渲染前须补注册）。字体按
    SHA-256 内容身份登记，与文件名/来源机器无关。
    """

    registrations: tuple[ProjectFontRegistration, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def from_resources(
        cls,
        resource_resolver: Any,
        *,
        register_with_qt: bool = True,
    ) -> "ProjectFontRegistry":
        """扫描资源索引中的字体条目并注册。

        Args:
            resource_resolver: ``ProjectResourceResolver``（工程加载产物）。
            register_with_qt: False 时只物化不注册（预检/诊断场景）。
        """
        regs: list[ProjectFontRegistration] = []
        diags: list[str] = []
        for entry in resource_resolver.iter_entries():
            if entry.kind != "font":
                continue
            try:
                resolved = resource_resolver.resolve_uri(entry.uri)
            except Exception as exc:  # noqa: BLE001 — 单字体失败不废整包
                diags.append(f"字体资源 {entry.resource_id} 解析失败: {exc}")
                continue
            family = ""
            registered = False
            detail = ""
            if register_with_qt:
                try:
                    from PyQt6.QtGui import QGuiApplication, QFontDatabase
                    if QGuiApplication.instance() is None:
                        detail = "headless：无 QGuiApplication，暂未注册"
                    else:
                        font_id = QFontDatabase.addApplicationFont(
                            resolved.local_path)
                        if font_id < 0:
                            detail = "Qt 拒绝该字体文件（格式/损坏？）"
                        else:
                            families = QFontDatabase.applicationFontFamilies(
                                font_id)
                            family = families[0] if families else ""
                            registered = bool(family)
                            if not registered:
                                detail = "字体文件已注册但 Qt 未报出族名"
                except Exception as exc:  # noqa: BLE001
                    detail = f"注册异常: {exc}"
                if detail and not registered:
                    diags.append(
                        f"工程字体 {entry.resource_id}: {detail}")
            regs.append(ProjectFontRegistration(
                resource_uri=entry.uri,
                sha256=entry.sha256,
                local_path=resolved.local_path,
                family=family,
                registered=registered,
                detail=detail,
            ))
        return cls(registrations=tuple(regs), diagnostics=tuple(diags))

    @property
    def families(self) -> frozenset:
        """成功注册的字体族集合（喂给 FontResolver.register_project_families）。"""
        return frozenset(r.family for r in self.registrations if r.registered)

    def summary(self) -> str:
        registered = sum(1 for r in self.registrations if r.registered)
        return (f"工程字体：{registered}/{len(self.registrations)} 注册成功"
                + (f"；{len(self.diagnostics)} 条诊断" if self.diagnostics
                   else ""))
