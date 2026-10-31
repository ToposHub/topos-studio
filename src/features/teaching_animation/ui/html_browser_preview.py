"""Interactive HTML/CSS preview surface for the Motion Graphic editor.

The final renderer still captures deterministic frames in Chromium.  This
widget is deliberately different: while editing, the user sees the authored
HTML/CSS in a real Qt WebEngine browser and the host only injects a small,
trusted page-navigation/selection bridge.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from PyQt6.QtCore import QEvent, QObject, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QStackedLayout, QLabel, QWidget
from src.shared.i18n.i18n import I18N

from ..core.html_page_deck import (
    HOST_OWNED_DOM_IDS,
    PageDeckTiming,
    ensure_page_markers,
    page_deck_editor_html,
)


# P1-UI-01：DevTools Apply 前必须剥离的宿主 scaffolding 选择器（唯一事实源）。
# 页面层共享 id 由 HOST_OWNED_DOM_IDS 生成，防止注入与清理两侧漂移；以下是
# 预览/采集层专属节点与 qtwebchannel 脚本。tests/ui/test_devtools_ownership_contract
# 直接导入本常量做覆盖校验，不解析 JS 模板源码。
DEVTOOLS_STRIP_SELECTORS: tuple[str, ...] = (
    *(f"#{dom_id}" for dom_id in HOST_OWNED_DOM_IDS),
    "#topos-preview-scrollbars",
    "#topos-native-vector-overlay",
    "#topos-native-draw-surface",
    "#topos-native-selection-style",
    'script[src="qrc:///qtwebchannel/qwebchannel.js"]',
)


class _HtmlBrowserBridge(QObject):
    pageChanged = pyqtSignal(int)
    elementSelected = pyqtSignal(str)
    domMutated = pyqtSignal()
    nativeShapeDrawn = pyqtSignal(str, float, float, float, float)
    nativeTextEdited = pyqtSignal(str, str)

    @pyqtSlot(int)
    def notifyPageChanged(self, index: int) -> None:  # noqa: N802 — JS bridge API
        self.pageChanged.emit(int(index))

    @pyqtSlot(str)
    def notifyElementSelected(self, node_id: str) -> None:  # noqa: N802 — JS bridge API
        self.elementSelected.emit(str(node_id))

    @pyqtSlot()
    def notifyDomMutated(self) -> None:  # noqa: N802 — JS bridge API
        self.domMutated.emit()

    @pyqtSlot(str, float, float, float, float)
    def notifyNativeShapeDrawn(  # noqa: N802 — JS bridge API
        self, kind: str, x: float, y: float, width: float, height: float,
    ) -> None:
        self.nativeShapeDrawn.emit(str(kind), float(x), float(y), float(width), float(height))

    @pyqtSlot(str, str)
    def notifyNativeTextEdited(  # noqa: N802 — JS bridge API
        self, element_id: str, text: str,
    ) -> None:
        self.nativeTextEdited.emit(str(element_id), str(text))


class HtmlBrowserPreview(QWidget):
    """A lazy QWebEngineView that displays the editable HTML document."""

    pageChanged = pyqtSignal(int)
    elementSelected = pyqtSignal(str)
    loaded = pyqtSignal(bool)
    zoomChanged = pyqtSignal(float)
    domMutated = pyqtSignal()
    shapeDrawn = pyqtSignal(str, float, float, float, float)
    textEdited = pyqtSignal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgHtmlBrowserPreview")
        self._layout = QStackedLayout(self)
        self._placeholder = QLabel(I18N.tr("motion_graphic.browser.initializing", "Initializing HTML preview…"), self)
        self._placeholder.setObjectName("mgBrowserPlaceholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self._placeholder)
        self._view: Any | None = None
        self._profile: Any | None = None
        self._channel: Any | None = None
        self._bridge: _HtmlBrowserBridge | None = None
        self._devtools_page: Any | None = None
        self._devtools_view: Any | None = None
        self._ready = False
        self._available = False
        self._document = ""
        self._pending_page = 0
        self._current_page = 0
        self._page_count = 1
        self._design_width = 1920
        self._design_height = 1080
        self._zoom_factor = 1.0
        self._fit_zoom = True
        self._native_markup = ""
        self._native_draw_tool: str | None = None
        # AUD-30907：父属单发定时器——预览销毁后 fit-zoom 回调必不触发
        # （PyQt6 静态 singleShot 无上下文对象重载）。
        self._fit_zoom_timer = QTimer(self)
        self._fit_zoom_timer.setSingleShot(True)
        self._fit_zoom_timer.setInterval(0)
        self._fit_zoom_timer.timeout.connect(self._apply_fit_zoom)

    @property
    def view(self) -> Any | None:
        return self._view

    @property
    def available(self) -> bool:
        return self._available

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def native_draw_tool(self) -> str | None:
        """The active browser-canvas draw tool, if any."""
        return self._native_draw_tool

    @property
    def current_page(self) -> int:
        return self._current_page

    @property
    def zoom_factor(self) -> float:
        """Current browser zoom (1.0 means 100% of the authored canvas)."""
        return self._zoom_factor

    @property
    def fit_zoom(self) -> bool:
        """Whether the preview follows the available widget size."""
        return self._fit_zoom

    def ensure_browser(self) -> bool:
        """Create the WebEngine lazily; fall back on offscreen/unsupported Qt."""
        if self._view is not None:
            return self._available
        # Qt WebEngine does not provide a useful visible interactive surface on
        # the offscreen QPA plugin.  The editor's deterministic capture fallback
        # remains available for tests and headless environments.
        if QApplication.platformName().lower() == "offscreen":
            self._placeholder.setText(I18N.tr("motion_graphic.browser.desktop_required", "HTML browser preview requires a visible desktop environment"))
            return False
        try:
            from PyQt6.QtWebChannel import QWebChannel
            from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
            from PyQt6.QtWebEngineWidgets import QWebEngineView
        except Exception as exc:  # pragma: no cover - optional dependency
            self._placeholder.setText(I18N.tr("motion_graphic.browser.unavailable", "HTML browser unavailable: {error}", error=exc))
            return False

        profile = QWebEngineProfile(f"topos-motion-graphic-{id(self)}", self)
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        settings = profile.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        view = QWebEngineView(self)
        # Keep the page below its view in the QObject tree so the page is
        # destroyed before the profile when the preview widget is torn down.
        page = QWebEnginePage(profile, view)
        view.setPage(page)
        view.setToolTip(I18N.tr("motion_graphic.browser.zoom_hint", "Use the mouse wheel to zoom the HTML preview; View > Fit Preview restores the default fit."))
        view.installEventFilter(self)
        page.setBackgroundColor(QColor(18, 18, 18))
        channel = QWebChannel(page)
        bridge = _HtmlBrowserBridge(self)
        channel.registerObject("toposPageBridge", bridge)
        page.setWebChannel(channel)
        page.loadFinished.connect(self._on_load_finished)
        bridge.pageChanged.connect(self._on_bridge_page_changed)
        bridge.elementSelected.connect(self.elementSelected)
        bridge.domMutated.connect(self.domMutated)
        bridge.nativeShapeDrawn.connect(self.shapeDrawn)
        bridge.nativeTextEdited.connect(self.textEdited)
        self._view, self._profile, self._channel, self._bridge = (
            view, profile, channel, bridge,
        )
        self._available = True
        self._layout.addWidget(view)
        self._layout.setCurrentWidget(view)
        if self._document:
            self._load_document()
        self._apply_fit_zoom()
        return True

    def set_document(
        self,
        document: str,
        timing: PageDeckTiming,
        *,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        global_background: str = "#111827",
        page_backgrounds: tuple[str, ...] = (),
    ) -> None:
        """Set the authored HTML source and retain the selected page."""
        marked = ensure_page_markers(str(document or ""))
        prepared = page_deck_editor_html(
            marked,
            timing,
            global_background=global_background,
            page_backgrounds=page_backgrounds,
        )
        if viewport_width is not None and int(viewport_width) > 0:
            self._design_width = int(viewport_width)
        if viewport_height is not None and int(viewport_height) > 0:
            self._design_height = int(viewport_height)
        if prepared == self._document:
            if self._fit_zoom:
                self._apply_fit_zoom()
            return
        self._document = prepared
        self._page_count = max(1, timing.page_count)
        self._pending_page = max(0, min(self._pending_page, timing.page_count - 1))
        self._ready = False
        # A new authored document should start from the predictable fit state;
        # wheel zoom is an explicit, temporary inspection choice.
        self._fit_zoom = True
        if self._view is not None and self._available:
            self._load_document()
        self._apply_fit_zoom()

    def set_design_size(self, width: int, height: int) -> None:
        """Update the CSS/design canvas used by the fit calculation."""
        if int(width) > 0:
            self._design_width = int(width)
        if int(height) > 0:
            self._design_height = int(height)
        if self._fit_zoom:
            self._apply_fit_zoom()

    def set_native_overlay(self, markup: str) -> None:
        """Update native MG DOM without reloading the running web page."""
        markup = str(markup)
        if markup == self._native_markup:
            return
        self._native_markup = markup
        self._apply_native_overlay()

    def set_native_time(self, seconds: float) -> None:
        """Scrub native-layer CSS animations without touching the page.

        The overlay's keyframe animations are paused and read their playhead
        from one ``--topos-time`` variable, so a timeline tick is a single
        style-property write instead of a per-element DOM patch.
        """
        if self._view is None or not self._ready:
            return
        value = max(0.0, float(seconds))
        self._view.page().runJavaScript(
            "(() => {"
            "const root = document.getElementById('topos-native-vector-overlay');"
            "if (!root) return;"
            "window.__toposApplyingNativeOverlay = true;"
            f"root.style.setProperty('--topos-time', '{value:.4f}s');"
            "requestAnimationFrame(() => { window.__toposApplyingNativeOverlay = false; });"
            "})()"
        )

    def set_native_draw_tool(self, kind: str | None) -> None:
        """Route a browser-canvas drag into one native shape creation signal."""
        normalized = str(kind).lower() if kind is not None else None
        if normalized == self._native_draw_tool:
            return
        self._native_draw_tool = normalized
        self._apply_native_overlay()

    def fit_to_view(self) -> None:
        """Restore automatic fit-to-preview sizing."""
        self._fit_zoom = True
        self._apply_fit_zoom()

    def zoom_in(self) -> None:
        """Increase the preview zoom without changing the authored HTML."""
        self._set_zoom(self._zoom_factor * 1.1, manual=True)

    def zoom_out(self) -> None:
        """Decrease the preview zoom without changing the authored HTML."""
        self._set_zoom(self._zoom_factor / 1.1, manual=True)

    def set_page(self, index: int) -> None:
        self._pending_page = max(0, min(int(index), self._page_count - 1))
        self._current_page = self._pending_page
        if self._view is not None and self._ready:
            self._view.page().runJavaScript(
                f"window.toposPageDeck && window.toposPageDeck.show({self._pending_page});"
            )
        self.pageChanged.emit(self._current_page)

    def step_page(self, delta: int) -> None:
        self.set_page(self._current_page + int(delta))

    def select_node(self, node_id: str) -> None:
        """Highlight a DOM node selected from the Inspector or browser canvas.

        Authored editable slots (``data-topos-node-id``) and editor-owned
        native layers (``data-topos-native-element``) share one highlight so
        clicking the Layers panel and clicking the DOM select the same node.
        """
        if self._view is None or not self._ready:
            return
        import json
        literal = json.dumps(str(node_id), ensure_ascii=False)
        self._view.page().runJavaScript(
            "(() => {"
            # Selection bookkeeping must not look like a DevTools edit: wrap
            # the class/style writes in the same guard the overlay uses.
            "window.__toposApplyingNativeOverlay = true;"
            "const ensureStyle = () => {"
            " let s = document.getElementById('topos-native-selection-style');"
            " if (!s) {"
            "  s = document.createElement('style'); s.id = 'topos-native-selection-style';"
            "  s.textContent = '.topos-native-selected{outline:2px dashed #ff8800!important;"
            "outline-offset:2px!important;}';"
            "  document.head.appendChild(s);"
            " }"
            "};"
            "document.querySelectorAll('[data-topos-selected]').forEach(n => {"
            "n.classList.remove('topos-node-selected'); n.removeAttribute('data-topos-selected');});"
            "document.querySelectorAll('.topos-native-selected').forEach(n => n.classList.remove('topos-native-selected'));"
            f"const id = {literal};"
            "if (id) {"
            " ensureStyle();"
            " let n = Array.from(document.querySelectorAll('[data-topos-node-id]'))"
            "   .find(x => x.dataset.toposNodeId === id);"
            " if (!n) {"
            "  n = Array.from(document.querySelectorAll('[data-topos-native-element]'))"
            "   .find(x => x.dataset.toposNativeElement === id);"
            "  if (n) n.classList.add('topos-native-selected');"
            " } else {"
            "  n.classList.add('topos-node-selected');"
            " }"
            " if (n) n.setAttribute('data-topos-selected','1');"
            "}"
            "requestAnimationFrame(() => { window.__toposApplyingNativeOverlay = false; });"
            "})();"
        )

    def reload_document(self) -> None:
        if self._view is not None and self._available and self._document:
            self._load_document()

    def create_devtools_view(self, parent: QWidget) -> QWidget | None:
        """Attach Qt WebEngine's real Chromium DevTools frontend.

        ``QWebEnginePage.setDevToolsPage`` is the same frontend used by the
        browser's Elements/Console/Sources panels.  It edits the live preview
        page directly; the editor exposes a separate explicit apply action for
        persisting those live DOM/CSS changes into the versioned HTML layer.
        """
        if not self.ensure_browser() or self._view is None:
            return None
        if self._devtools_view is not None:
            return self._devtools_view
        try:
            from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
            from PyQt6.QtWebEngineWidgets import QWebEngineView
        except Exception:
            return None
        profile = self._profile or self._view.page().profile()
        devtools_view = QWebEngineView(parent)
        devtools_page = QWebEnginePage(profile, devtools_view)
        devtools_view.setPage(devtools_page)
        # Qt 6.10 exposes Chromium's ForceDarkMode setting.  Apply it to the
        # DevTools page only: authored HTML keeps its own intended appearance,
        # while the Elements/Console/Sources frontend always opens dark.
        dark_mode = getattr(QWebEngineSettings.WebAttribute, "ForceDarkMode", None)
        if dark_mode is not None:
            devtools_view.settings().setAttribute(dark_mode, True)
        devtools_page.setBackgroundColor(QColor("#202124"))
        self._view.page().setDevToolsPage(devtools_page)
        self._devtools_page = devtools_page
        self._devtools_view = devtools_view
        return devtools_view

    @property
    def devtools_view(self) -> QWidget | None:
        return self._devtools_view

    @property
    def native_markup(self) -> str:
        """The overlay markup the editor last installed (R-07 diff baseline)."""
        return self._native_markup

    def read_live_native_overlay(self, callback: Callable[[str], None]) -> None:
        """Return the live overlay subtree AFTER the user's DevTools session.

        Edits made on native (scene-owned) elements live here, not in the
        authored document — the authored read strips this whole subtree.
        """
        if self._view is None or not self._ready:
            callback("")
            return
        self._view.page().runJavaScript(
            "document.getElementById('topos-native-vector-overlay')?.innerHTML ?? ''",
            lambda html: callback(str(html or "")))

    def read_authored_document(self, callback: Callable[[str], None]) -> None:
        """Return the live DOM without host-only editor runtime scaffolding."""
        if self._view is None or not self._ready:
            callback("")
            return
        # P1-UI-01：清理选择器来自模块级 DEVTOOLS_STRIP_SELECTORS（由共享的
        # HOST_OWNED_DOM_IDS 清单生成），防止注入与清理两侧漂移（历史缺陷：
        # 背景 style/controller 未被清理，DevTools Apply 把宿主 scaffolding
        # 写回 authored 文档）。
        remove_js = ",\n".join(
            f"    '{selector}'" for selector in DEVTOOLS_STRIP_SELECTORS
        )
        script = r"""
(() => {
  const root = document.documentElement.cloneNode(true);
  const remove = [
__HOST_OWNED_SELECTORS__,
  ];
  remove.forEach((selector) => root.querySelectorAll(selector).forEach((node) => node.remove()));
  const runtimeAttributes = [
    'data-topos-hidden', 'data-topos-transitioning', 'data-topos-transition-role',
    'data-topos-transition', 'data-topos-original-translate', 'data-topos-page-recovered',
    'data-topos-selected',
  ];
  root.querySelectorAll('*').forEach((node) => {
    runtimeAttributes.forEach((name) => node.removeAttribute(name));
    node.classList.remove('topos-page-auto-box', 'topos-page-active', 'topos-node-selected');
    node.style.removeProperty('--topos-transition-progress');
    node.style.removeProperty('--topos-transition-duration');
    if (!node.getAttribute('style')?.trim()) node.removeAttribute('style');
  });
  return '<!doctype html>\n' + root.outerHTML;
})()
""".replace("__HOST_OWNED_SELECTORS__", remove_js)
        self._view.page().runJavaScript(
            script,
            lambda value: callback(str(value or "")),
        )

    def close(self) -> None:
        devtools_page = self._devtools_page
        if self._view is not None and self._devtools_page is not None:
            try:
                self._view.page().setDevToolsPage(None)
            except Exception:
                pass
        if self._devtools_view is not None:
            self._devtools_view.close()
            self._devtools_view.deleteLater()
            self._devtools_view = None
        if devtools_page is not None:
            devtools_page.deleteLater()
        self._devtools_page = None
        if self._view is not None:
            view = self._view
            page = self._view.page()
            try:
                view.setPage(None)
            except Exception:
                pass
            view.close()
            view.deleteLater()
            try:
                page.deleteLater()
            except RuntimeError:
                pass
            self._view = None
        if self._profile is not None:
            try:
                self._profile.deleteLater()
            except RuntimeError:
                pass
        self._profile = None
        self._channel = None
        self._bridge = None

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 — Qt API spelling
        super().resizeEvent(event)
        if self._fit_zoom:
            # Layout geometry is final only after the resize event returns.

            self._fit_zoom_timer.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Use the preview wheel as zoom input instead of browser scrolling."""
        if watched is self._view and event.type() == QEvent.Type.Wheel:
            wheel = event
            delta = int(wheel.angleDelta().y())
            if not delta:
                delta = int(wheel.pixelDelta().y())
            if delta:
                # Trackpad deltas are much smaller than a mouse notch; both
                # directions intentionally use the same smooth multiplicative
                # scale so repeated wheel motion feels consistent.
                steps = delta / 120.0 if abs(delta) >= 15 else (1.0 if delta > 0 else -1.0)
                self._set_zoom(self._zoom_factor * (1.1 ** steps), manual=True)
                wheel.accept()
                return True
        return super().eventFilter(watched, event)

    def _load_document(self) -> None:
        if self._view is None:
            return
        self._view.setHtml(self._document, QUrl("about:blank"))

    def _apply_native_overlay(self) -> None:
        """Install/update DOM layers while keeping CSS/JS animation running."""
        if self._view is None or not self._ready:
            return
        self._view.page().runJavaScript(self._native_overlay_script())

    def _native_overlay_script(self) -> str:
        markup = json.dumps(self._native_markup, ensure_ascii=False)
        tool = json.dumps(self._native_draw_tool, ensure_ascii=False)
        width = int(self._design_width)
        height = int(self._design_height)
        return f"""(() => {{
  window.__toposApplyingNativeOverlay = true;
  const markup = {markup};
  const tool = {tool};
  let root = document.getElementById('topos-native-vector-overlay');
  if (!root) {{
    root = document.createElement('div');
    root.id = 'topos-native-vector-overlay';
    root.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:2147483000;overflow:visible;';
    document.body.appendChild(root);
  }}
  root.innerHTML = markup;
  root.querySelectorAll('[data-topos-native-element]').forEach(node => {{
    // Native layers stay clickable for selection while the select tool is
    // active; a draw tool hands all pointer input to the surface above.
    node.style.pointerEvents = tool ? 'none' : 'auto';
    node.style.cursor = 'default';
  }});
  root.onclick = event => {{
    if (tool) return;
    const hit = event.target && event.target.closest
      ? event.target.closest('[data-topos-native-element]') : null;
    if (hit) window.toposPageBridge?.notifyElementSelected?.(hit.dataset.toposNativeElement);
  }};
  root.ondblclick = event => {{
    if (tool) return;
    const hit = event.target && event.target.closest
      ? event.target.closest('[data-topos-native-element]') : null;
    if (!hit || hit.tagName !== 'DIV' || hit.isContentEditable) return;
    event.preventDefault();
    const id = hit.dataset.toposNativeElement;
    const before = hit.textContent;
    // Suppress the DevTools mutation watcher for the whole editing session:
    // in-place typing mutates editor-owned nodes, never authored HTML.
    window.__toposApplyingNativeOverlay = true;
    hit.contentEditable = 'true';
    hit.focus();
    const range = document.createRange();
    range.selectNodeContents(hit);
    const selection = window.getSelection();
    selection.removeAllRanges(); selection.addRange(range);
    const commit = () => {{
      hit.contentEditable = 'false';
      const after = hit.textContent;
      requestAnimationFrame(() => {{ window.__toposApplyingNativeOverlay = false; }});
      if (after !== before) window.toposPageBridge?.notifyNativeTextEdited?.(id, after);
    }};
    hit.onblur = commit;
    hit.onkeydown = key => {{
      if (key.key === 'Enter' && !key.shiftKey) {{ key.preventDefault(); hit.blur(); }}
      if (key.key === 'Escape') {{ hit.textContent = before; hit.blur(); }}
      key.stopPropagation();
    }};
  }};
  let surface = document.getElementById('topos-native-draw-surface');
  if (!surface) {{
    surface = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    surface.id = 'topos-native-draw-surface';
    surface.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:2147483500;pointer-events:none;';
    document.body.appendChild(surface);
  }}
  const designWidth = {width}, designHeight = {height};
  surface.setAttribute('viewBox', `0 0 ${{designWidth}} ${{designHeight}}`);
  surface.setAttribute('preserveAspectRatio', 'none');
  surface.style.pointerEvents = tool ? 'auto' : 'none';
  surface.style.cursor = tool ? 'crosshair' : 'default';
  let start = null, guide = null;
  const point = event => {{
    const rect = surface.getBoundingClientRect();
    return {{x:(event.clientX-rect.left)*designWidth/Math.max(1,rect.width), y:(event.clientY-rect.top)*designHeight/Math.max(1,rect.height)}};
  }};
  const updateGuide = current => {{
    if (!start || !guide) return;
    if (tool === 'ellipse') {{
      guide.setAttribute('cx',(start.x+current.x)/2); guide.setAttribute('cy',(start.y+current.y)/2);
      guide.setAttribute('rx',Math.abs(current.x-start.x)/2); guide.setAttribute('ry',Math.abs(current.y-start.y)/2);
    }} else {{
      guide.setAttribute('x',Math.min(start.x,current.x)); guide.setAttribute('y',Math.min(start.y,current.y));
      guide.setAttribute('width',Math.abs(current.x-start.x)); guide.setAttribute('height',Math.abs(current.y-start.y));
    }}
  }};
  surface.onpointerdown = event => {{
    if (!tool || tool === 'path' || event.button !== 0) return;
    start = point(event);
    guide = document.createElementNS('http://www.w3.org/2000/svg', tool === 'ellipse' ? 'ellipse' : 'rect');
    guide.setAttribute('fill','rgba(255,136,0,.16)'); guide.setAttribute('stroke','#ff8800'); guide.setAttribute('stroke-dasharray','6 4');
    if (tool === 'ellipse') {{ guide.setAttribute('cx',start.x); guide.setAttribute('cy',start.y); guide.setAttribute('rx','0'); guide.setAttribute('ry','0'); }}
    surface.appendChild(guide); surface.setPointerCapture(event.pointerId); event.preventDefault();
  }};
  surface.onpointermove = event => {{ if (start && guide) updateGuide(point(event)); }};
  surface.onpointerup = event => {{
    if (!start) return;
    const end=point(event), x=Math.min(start.x,end.x), y=Math.min(start.y,end.y), w=Math.abs(end.x-start.x), h=Math.abs(end.y-start.y);
    if ((tool === 'line' || tool === 'arrow') ? (w >= 2 || h >= 2) : (w >= 2 && h >= 2))
      window.toposPageBridge?.notifyNativeShapeDrawn?.(tool,x,y,Math.max(2,w),Math.max(2,h));
    if (guide) guide.remove(); start=null; guide=null;
  }};
  requestAnimationFrame(() => {{ window.__toposApplyingNativeOverlay = false; }});
}})()"""

    def _set_zoom(self, value: float, *, manual: bool) -> None:
        value = max(0.1, min(4.0, float(value)))
        if manual:
            self._fit_zoom = False
            if self._view is not None:
                self._set_scrollbars_for_manual_zoom()
        self._zoom_factor = value
        if self._view is not None:
            self._view.setZoomFactor(value)
        self.zoomChanged.emit(value)

    def _apply_fit_zoom(self) -> None:
        if not self._fit_zoom:
            return
        width = self._view.width() if self._view is not None else self.width()
        height = self._view.height() if self._view is not None else self.height()
        if width <= 0 or height <= 0 or self._design_width <= 0 or self._design_height <= 0:
            return
        # Keep the entire authored canvas visible and preserve its aspect ratio.
        fit = min(width / float(self._design_width), height / float(self._design_height))
        self._set_scrollbars_for_fit()
        self._set_zoom(max(0.1, min(4.0, fit)), manual=False)

    def _set_scrollbars_for_fit(self) -> None:
        if self._view is None or not self._ready:
            return
        # QWebEngineView is a QWidget, not QAbstractScrollArea; its page
        # scrollbars are Chromium-rendered and cannot be configured with Qt's
        # setHorizontal/VerticalScrollBarPolicy API.  Use a host-owned style
        # inside the page instead, leaving authored HTML/CSS untouched.
        self._view.page().runJavaScript(
            """(() => {
              let s = document.getElementById('topos-preview-scrollbars');
              if (!s) {
                s = document.createElement('style');
                s.id = 'topos-preview-scrollbars';
                document.head.appendChild(s);
              }
              s.textContent = 'html,body{scrollbar-width:none!important;} '
                + 'html::-webkit-scrollbar,body::-webkit-scrollbar{width:0!important;height:0!important;}';
            })()"""
        )

    def _set_scrollbars_for_manual_zoom(self) -> None:
        if self._view is None or not self._ready:
            return
        self._view.page().runJavaScript(
            "(() => { const s = document.getElementById('topos-preview-scrollbars');"
            " if (s) s.remove(); })()"
        )

    def _on_load_finished(self, ok: bool) -> None:
        self._ready = bool(ok)
        self.loaded.emit(bool(ok))
        if ok and self._view is not None:
            # Track structural/style changes made through Chromium DevTools.
            # The observer intentionally ignores the selection class managed
            # by this editor, so simply clicking the preview does not create a
            # false "unsaved DevTools edit" warning.
            self._view.page().runJavaScript(
                "(() => {"
                "if (window.__toposDevToolsMutationObserver) return;"
                "let queued = false;"
                "const notify = () => {"
                " if (queued) return; queued = true;"
                " requestAnimationFrame(() => { queued = false;"
                "  window.toposPageBridge?.notifyDomMutated?.(); }); };"
                "window.__toposDevToolsMutationObserver = new MutationObserver(records => {"
                " if (window.__toposApplyingNativeOverlay) return;"
                " if (records.some(r => r.type !== 'attributes' || r.attributeName === 'style')) notify();"
                "});"
                "window.__toposDevToolsMutationObserver.observe(document.documentElement, {"
                " subtree:true, childList:true, characterData:true, attributes:true, attributeFilter:['style']"
                "});"
                "})();"
            )
        if ok:
            self._apply_native_overlay()
            self._apply_fit_zoom()
            self.set_page(self._pending_page)

    def _on_bridge_page_changed(self, index: int) -> None:
        self._current_page = max(0, int(index))
        self.pageChanged.emit(self._current_page)


__all__ = ["HtmlBrowserPreview"]
