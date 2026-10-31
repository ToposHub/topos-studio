"""FeatureRegistry entry point for the optional Teaching Animation module."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import replace
from typing import Any, Callable, List, Mapping

from src.app.app_context import AppContext
from src.app.page_contract import PageDescriptor
from src.features.edit.ai_editing.assistant_plugins import assistant_plugin_prompt

logger = logging.getLogger(__name__)
from src.shared.i18n.i18n import I18N

try:  # AUD-28005: the browser gate moves onto a QThread; lightweight,
    # Qt-less hosts embedding this module keep working through the guarded
    # fallback below (they then stay on the synchronous validation path,
    # which cannot block an event loop they do not have).
    from PyQt6.QtCore import QThread, pyqtSignal
except ImportError:  # pragma: no cover - Qt-less host embedding only
    QThread = object  # type: ignore[assignment,misc]

    def pyqtSignal(*_args: object, **_kwargs: object) -> None:  # type: ignore[misc]
        """Neutral stand-in so the class body still evaluates without Qt."""
        return None


class _StaticHtmlClient:
    """Adapt one already-returned model document to the HTML service API."""

    def __init__(self, html: str) -> None:
        self._html = str(html or "")

    def complete(self, _messages: Any, *, temperature: float = 0.0,
                 max_tokens: int = 0) -> str:
        del temperature, max_tokens
        return self._html


class _BrowserValidationJob(QThread):
    """Run the design-size HTML browser gate off the UI thread (AUD-28005).

    ``validate_html_scene`` drives the real host renderer at the layer
    viewport — multi-frame candidates, the per-page quality probes and the
    contrast-background pass — which freezes the main thread for the whole
    capture when invoked inside a Qt slot.  The job runs the exact same gate
    (the staging worker thread already proves this path is thread-safe) and
    reports through queued signals, so Qt surfaces are only ever touched on
    the UI thread.
    """

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        scene: Any,
        validate: Callable[[Any], tuple[Any, int]],
    ) -> None:
        super().__init__()
        self._scene = scene
        self._validate = validate

    def run(self) -> None:
        try:
            rendered, validation_frame = self._validate(self._scene)
        except Exception as exc:  # noqa: BLE001 — the diagnosis is surfaced on the UI thread
            self.failed.emit(str(exc))
            return
        data = getattr(rendered, "data", b"")
        width = int(getattr(rendered, "width", 0) or 0)
        height = int(getattr(rendered, "height", 0) or 0)
        if not data or width <= 0 or height <= 0:
            self.failed.emit(f"渲染验证帧 {validation_frame} 为空")
            return
        self.succeeded.emit(rendered)


class TeachingAnimationModule:
    """Optional module that wires the shared scene editor into host surfaces."""

    feature_id = "teaching_animation"
    # A browser validation failure is normally repaired by the host viewport
    # recovery pass.  Keep a small, bounded author-model repair budget for
    # genuinely empty/invalid pages; never loop forever and never swap in a
    # host template behind the user's back.
    _MAX_HTML_BUILD_RETRIES = 2
    # User default: every newly generated Motion Graphic is authored and
    # exported against a Full-HD landscape canvas. The active Edit timeline
    # may have another preview size, but it must not silently downscale the
    # source artwork supplied by the AI author.
    _MOTION_GRAPHIC_OUTPUT_WIDTH = 1920
    _MOTION_GRAPHIC_OUTPUT_HEIGHT = 1080
    # Project-local, sanitized preference notes carried by a Motion Graphic
    # layer.  This is deliberately small: it preserves useful critique across
    # a save/reopen without turning a scene file into an unbounded chat log.
    _HTML_EDIT_FEEDBACK_MEMORY_KEY = "assistant_feedback_memory_v1"
    _MAX_PERSISTED_HTML_EDIT_FEEDBACK_NOTES = 3
    _MAX_PERSISTED_HTML_EDIT_FEEDBACK_CHARS = 280
    # Project-local recovery state for an unfinished creation conversation.
    # This is intentionally separate from the finished clip's feedback memory:
    # it stores only the minimum intent needed to rebuild a plan safely after a
    # restart, never an in-flight model response or an approval grant.
    _MOTION_GRAPHIC_TASK_MEMORY_KEY = "motion_graphic_task_memory_v1"
    _MOTION_GRAPHIC_TASK_MEMORY_VERSION = 1
    _MAX_RECOVERABLE_MOTION_GRAPHIC_TASKS = 8
    _MAX_RECOVERABLE_MOTION_GRAPHIC_TEXT_CHARS = 1800

    def __init__(self) -> None:
        self._provider_registered = False
        self._pending_motion_graphic_requests: dict[str, dict[str, Any]] = {}
        self._pending_scene_edit_requests: dict[str, dict[str, Any]] = {}
        self._pending_html_generate_requests: dict[str, dict[str, Any]] = {}
        self._html_services: dict[str, Any] = {}
        self._main_window: Any | None = None
        self._motion_graphic_discussion_active = False
        # The most recently created/edited Motion Graphic clip, so an
        # untargeted "把标题改成 X" continues editing it even after the
        # selection moved on.  {"asset_id", "scene_id", "display_name"}.
        self._last_motion_graphic_target: dict[str, str] | None = None
        # Keep the actual creative brief separate from later controls such as
        # "确认生成".  Otherwise a long discussion could accidentally ask the
        # model to design an animation whose entire brief is just that control
        # phrase.
        self._motion_graphic_discussion_brief_text = ""
        self._motion_graphic_discussion_wants_options = False
        self._motion_graphic_generation_count = 0
        # OS-07: structured feedback store for revision / critique cycles.
        self._feedback_store: Any = None  # lazily initialized
        # AUD-28005: browser-gate jobs currently running off the UI thread,
        # keyed by a scope token ("confirm:<id>" / "motion:<id>" /
        # "stage:<id>").  The module holds the reference so a running thread
        # is never garbage-collected; the queued completion handlers retire
        # the entries.
        self._browser_validation_jobs: dict[str, Any] = {}

    def _start_browser_validation_job(
        self,
        scope: str,
        scene: Any,
        on_succeeded: Callable[[Any], None],
        on_failed: Callable[[str], None],
    ) -> bool:
        """Start the design-size browser gate on a QThread (AUD-28005).

        Returns True when the validation is now running in the background
        (``on_succeeded`` / ``on_failed`` will be invoked on the UI thread via
        queued signals) or when a job for the same scope is already in
        flight; False when no Qt thread is available, in which case the
        caller must keep its synchronous fallback (a Qt-less host has no UI
        event loop to block).
        """
        if QThread is object:
            return False
        if self._browser_validation_jobs.get(scope) is not None:
            # A second confirm click while the same validation runs must not
            # spawn a second browser session for the same document.
            return True
        job = _BrowserValidationJob(
            scene, validate=self._validate_motion_graphic_scene,
        )

        def _succeeded(rendered: Any) -> None:
            self._finish_browser_validation_job(scope, job)
            on_succeeded(rendered)

        def _failed(message: str) -> None:
            self._finish_browser_validation_job(scope, job)
            on_failed(message)

        job.succeeded.connect(_succeeded)
        job.failed.connect(_failed)
        self._browser_validation_jobs[scope] = job
        job.start()
        return True

    def _finish_browser_validation_job(self, scope: str, job: Any) -> None:
        """Retire one finished validation job on the UI thread."""
        if self._browser_validation_jobs.get(scope) is job:
            self._browser_validation_jobs.pop(scope, None)
        job.wait()
        job.deleteLater()

    @staticmethod
    def _assistant_plugin_prompt(name: str, fallback: str) -> str:
        """Read a user-editable Motion Graphic prompt from its plugin folder."""

        return assistant_plugin_prompt("motion_graphic", name, fallback)

    def is_available(self) -> bool:
        """Return whether the module is enabled by the current app context."""
        return True

    def register_pages(self, context: AppContext) -> List[PageDescriptor]:
        # The independent editor window arrives in Phase 3.  Do not create a
        # page or WebEngine instance merely because the flag is enabled.
        return []

    def register_i18n(self) -> None:
        """Register Motion Graphic editor translations with the app i18n hub."""
        import os
        from src.shared.i18n.i18n import I18N

        I18N.load_module_translations(os.path.join(os.path.dirname(__file__), "locales"))

    def register_actions(self, context: AppContext) -> None:
        """Install the stable Composite node-provider hook when applicable."""
        # Register the Motion Graphic editor's customizable shortcuts at
        # startup so they always appear in the global shortcut-settings
        # dialog, even before any editor window has been opened this
        # session.  The editor itself applies/matches the configured
        # sequences (see ui/mg_shortcuts.py); nothing here binds a QAction.
        try:
            from src.shared.ui.shortcut_manager import ShortcutManager
            from .ui.mg_shortcuts import register_motion_graphic_shortcuts
            sm = ShortcutManager.instance()
            if sm is not None:
                register_motion_graphic_shortcuts(sm)
        except Exception:  # noqa: BLE001 - settings surface must not gate startup
            logger.debug("Motion Graphic shortcut registration skipped", exc_info=True)

        flags = context.get_feature_flags()
        if flags is None or not flags.is_enabled("composite"):
            return
        try:
            from src.features.composite.facade import register_node_provider

            register_node_provider(self.feature_id, self)
            self._provider_registered = True
            try:
                from src.features.composite.core.node_engine import register_node_type
                from .composite_node import MotionGraphicNode
                register_node_type("motion_graphic", MotionGraphicNode, category="Input")
            except (ImportError, ValueError):
                logger.info("Composite Motion Graphic node registration deferred")
        except ImportError:
            logger.info("Composite facade unavailable; provider hook deferred")

    def register_save_handlers(self, context: AppContext) -> None:
        """Attach scene payload callbacks to the host's unified project IO."""
        main_window = context.get_main_window()
        io = getattr(main_window, "edit_project_io", None) if main_window is not None else None
        if io is None:
            return
        try:
            io.set_callbacks(
                get_teaching_animation_payload=getattr(main_window, "_get_teaching_animation_payload", None),
                on_teaching_animation_payload_loaded=getattr(main_window, "_restore_teaching_animation_payload", None),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Teaching Animation save callback registration failed", exc_info=True)

    def register_ai_providers(self, context: AppContext) -> None:
        """Register scene context/commands only when this optional module is on."""
        flags = context.get_feature_flags()
        if flags is None or not flags.is_enabled(self.feature_id):
            return
        from .ai.provider_registry import register_teaching_ai_provider

        register_teaching_ai_provider()

    def get_ai_context_provider(self, scene: Any, **kwargs: Any) -> Any:
        from .ai.context_provider import TeachingSceneContextProvider

        return TeachingSceneContextProvider(scene, **kwargs)

    def get_ai_command_registry(self) -> Any:
        # Return the extended registry so the assistant accepts the full MG-09
        # vocabulary (remove/replace/group/layout/color+typo token/marker/
        # keyframe/audio/...), not just add_element/set_animation.  It is a
        # drop-in superset of the base registry — the 4 base names delegate to
        # the base compiler, the 16 extended names compile here, and dry_run/
        # apply/undo stay balanced even for list-returning compiles.
        from .ai.command_catalog import ExtendedCommandRegistry

        return ExtendedCommandRegistry()

    def on_page_enter(self, page_id: str, context: AppContext) -> None:
        return None

    def on_page_leave(self, page_id: str, context: AppContext) -> None:
        return None

    def get_composite_node_provider(self) -> dict[str, Any]:
        """Return the Phase 4 provider descriptor without importing UI internals."""
        from .host_adapters import create_motion_graphic_node_provider

        provider = create_motion_graphic_node_provider()
        return {
            "provider_id": self.feature_id,
            "node_types": (provider.descriptor.node_type,),
            "status": "available",
            "descriptor": provider.descriptor.to_dict(),
            "provider": provider,
        }

    def get_node_types(self) -> tuple[str, ...]:
        return ("motion_graphic",)

    def create_node(self, scene: Any, node_id: str | None = None) -> Any:
        from .host_adapters import create_motion_graphic_node_provider

        return create_motion_graphic_node_provider().create_node(scene, node_id)

    def dispose(self) -> None:
        """Remove the provider hook if the host unloads the optional module."""
        if self._provider_registered:
            try:
                from src.features.composite.facade import unregister_node_provider

                unregister_node_provider(self.feature_id)
            except ImportError:
                pass
            self._provider_registered = False
        try:
            from .ai.provider_registry import unregister_teaching_ai_provider
            unregister_teaching_ai_provider()
        except ImportError:
            pass
        self._pending_html_generate_requests.clear()
        self._html_services.clear()

    def attach_main_window(self, main_window: Any) -> None:
        """Attach both host entry points after MainWindow has built its docks."""
        self._main_window = main_window
        # Register the portable on-disk library before/alongside templates
        # created in a project.  The Effects panel listens to this registry,
        # so it updates without a UI-to-core dependency.
        try:
            from .core.motion_graphic_generator_registry import (
                get_motion_graphic_generator_registry,
            )
            from .core.motion_graphic_template_library import (
                load_motion_graphic_template_library,
            )

            load_motion_graphic_template_library(get_motion_graphic_generator_registry())
        except (OSError, ValueError):
            logger.exception("Motion Graphic template library load failed")
        # FileController owns the project package, while this feature owns the
        # private pending-request state.  Register two narrow callbacks rather
        # than making the host reach into this module's dict directly.
        main_window._export_motion_graphic_task_memory = self._export_motion_graphic_task_memory
        main_window._restore_motion_graphic_task_memory = self._restore_motion_graphic_task_memory
        controller = getattr(main_window, "_ai_edit_controller", None)
        set_context = getattr(controller, "set_teaching_context_provider", None)
        if callable(set_context):
            set_context(self.get_ai_context)
        set_request_handler = getattr(controller, "set_teaching_request_handler", None)
        if callable(set_request_handler):
            set_request_handler(
                lambda text, target_asset_ids=(): self._handle_ai_request(
                    main_window, text, target_asset_ids=target_asset_ids,
                )
            )
        set_motion_graphic_mode_handler = getattr(
            controller, "set_teaching_motion_graphic_mode_handler", None,
        )
        if callable(set_motion_graphic_mode_handler):
            # Explicit composer mode skips the ambiguous “is this a question?”
            # heuristic and starts the same guarded build flow.  The feature
            # still owns sanitization, validation and the confirmation card.
            set_motion_graphic_mode_handler(
                lambda text, target_asset_ids=(): self._handle_motion_graphic_mode_request(
                    main_window, text, target_asset_ids=target_asset_ids,
                )
            )
        set_discussion_handler = getattr(
            controller, "set_teaching_motion_graphic_discussion_handler", None,
        )
        if callable(set_discussion_handler):
            set_discussion_handler(self._is_motion_graphic_discussion_request)
        set_scene_edit_request_handler = getattr(
            controller, "set_teaching_scene_edit_request_handler", None,
        )
        if callable(set_scene_edit_request_handler):
            set_scene_edit_request_handler(self._is_scene_edit_request)
        set_scene_edit_response_handler = getattr(
            controller, "set_teaching_scene_edit_response_handler", None,
        )
        if callable(set_scene_edit_response_handler):
            set_scene_edit_response_handler(
                lambda text, response: self._handle_model_scene_edit_response(
                    main_window, text, response,
                )
            )
        set_html_generate_response_handler = getattr(
            controller, "set_teaching_html_generate_response_handler", None,
        )
        if callable(set_html_generate_response_handler):
            set_html_generate_response_handler(
                lambda request_id, response: self._handle_model_html_generate_response(
                    main_window, request_id, response,
                )
            )
        # B 方案：module 把"处理 LLM 响应"的 handler 注入 controller。
        # controller 在授权后自己起 teaching_motion_graphic_build chat worker，
        # worker 完成后回调这里的 handler 把响应交回 module 构造场景。
        set_build_response_handler = getattr(
            controller, "set_teaching_motion_graphic_build_response_handler", None,
        )
        if callable(set_build_response_handler):
            set_build_response_handler(
                lambda request_id, response: self._handle_model_motion_graphic_build_response(
                    main_window, request_id, response,
                )
            )
        # 把场景设计契约 system prompt 注入 controller（feature 拥有 prompt 内容，
        # controller 不硬编码 feature 细节）。
        set_build_system_prompt = getattr(
            controller, "set_teaching_motion_graphic_build_system_prompt", None,
        )
        if callable(set_build_system_prompt):
            set_build_system_prompt(self._assistant_plugin_prompt(
                "legacy_scene", self._MOTION_GRAPHIC_BUILD_SYSTEM_PROMPT,
            ))
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        if panel is not None and not getattr(panel, "_teaching_motion_approval_attached", False):
            confirm_signal = getattr(panel, "motion_graphic_confirm_requested", None)
            cancel_signal = getattr(panel, "motion_graphic_cancel_requested", None)
            proposal_signal = getattr(panel, "motion_graphic_proposal_selected", None)
            # AUD-19500：订阅面板专用"改用内置模板"信号（面板侧已按
            # receivers 检测回退 confirm，这里接上后模板路径真正可达）。
            template_signal = getattr(panel, "motion_graphic_template_requested", None)
            if confirm_signal is not None:
                confirm_signal.connect(
                    lambda request_id: self._confirm_motion_graphic_plan(
                        main_window, request_id,
                    )
                )
            if cancel_signal is not None:
                cancel_signal.connect(
                    lambda request_id: self._cancel_motion_graphic_plan(
                        main_window, request_id,
                    )
                )
            if proposal_signal is not None:
                proposal_signal.connect(
                    lambda request_id, proposal_id: self._select_creative_proposal(
                        main_window, request_id, proposal_id,
                    )
                )
            if template_signal is not None:
                template_signal.connect(
                    lambda request_id: self._request_motion_graphic_template(
                        main_window, request_id,
                    )
                )
            panel._teaching_motion_approval_attached = True
        timeline_panel = getattr(main_window, "timeline_panel", None)
        if timeline_panel is not None:
            self._attach_timeline(main_window, timeline_panel)
        media_pool_panel = getattr(main_window, "media_pool_panel", None)
        if media_pool_panel is not None:
            self._attach_media_pool(main_window, media_pool_panel)
        composite_page = getattr(main_window, "composite_page", None)
        if composite_page is not None:
            self._attach_composite(main_window, composite_page)

    @classmethod
    def _safe_motion_graphic_task_text(cls, value: Any) -> str:
        """Bound and redact task context before it enters a project payload."""
        try:
            from src.features.ai_harness.invocation_models import sanitize_for_storage
            text = str(sanitize_for_storage(str(value or ""))).strip()
        except ImportError:  # pragma: no cover - partial feature embedding
            text = str(value or "").strip()
        return text[:cls._MAX_RECOVERABLE_MOTION_GRAPHIC_TEXT_CHARS]

    def _export_motion_graphic_task_memory(self) -> dict[str, Any] | None:
        """Return safe snapshots for unfinished Motion Graphic creation tasks.

        A model request may be interrupted at any phase.  We deliberately save
        the typed brief and reviewed user feedback, but never save model output
        in flight, HTML source awaiting approval, or a grant.  Resuming always
        regenerates the plan and asks for confirmation again.
        """
        from .ai.brief import MotionGraphicBrief

        tasks: list[dict[str, Any]] = []
        for request_id, plan in list(self._pending_motion_graphic_requests.items()):
            if not isinstance(plan, Mapping):
                continue
            brief = plan.get("brief")
            if not isinstance(brief, MotionGraphicBrief):
                continue
            tasks.append({
                "request_id": str(request_id),
                "brief": brief.to_dict(),
                "user_text": self._safe_motion_graphic_task_text(plan.get("user_text", "")),
                "generation_feedback_memory": list(self._normalize_feedback_notes(
                    plan.get("generation_feedback_memory", ()),
                )),
                "resume_phase": str(plan.get("phase", "plan_review") or "plan_review"),
                "seconds": float(plan.get("seconds", brief.duration_seconds or 10.0) or 10.0),
                "fps": float(plan.get("fps", brief.fps) or brief.fps),
                "frame": max(0, int(plan.get("frame", 0) or 0)),
                "track_id": str(plan.get("track_id", "") or ""),
                "variant": max(0, int(plan.get("variant", 0) or 0)),
                "render_mode": "html",
            })
        if not tasks:
            return None
        return {
            "version": self._MOTION_GRAPHIC_TASK_MEMORY_VERSION,
            "tasks": tasks[-self._MAX_RECOVERABLE_MOTION_GRAPHIC_TASKS:],
        }

    def _restore_motion_graphic_task_memory(self, payload: Any) -> int:
        """Restore pending tasks into a safe, explicit-resume state.

        Browser/model workers are never resurrected after restart.  The user
        sends “继续” to restart from a fresh plan, so stale UI state or a lost
        approval cannot mutate the timeline.
        """
        from .ai.brief import MotionGraphicBrief

        self._pending_motion_graphic_requests.clear()
        if not isinstance(payload, Mapping):
            return 0
        try:
            version = int(payload.get("version", 0) or 0)
        except (TypeError, ValueError):
            return 0
        if version != self._MOTION_GRAPHIC_TASK_MEMORY_VERSION:
            return 0
        rows = payload.get("tasks", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        for raw in rows[-self._MAX_RECOVERABLE_MOTION_GRAPHIC_TASKS:]:
            if not isinstance(raw, Mapping):
                continue
            request_id = str(raw.get("request_id", "") or "").strip()
            brief_raw = raw.get("brief")
            if not request_id or not isinstance(brief_raw, Mapping):
                continue
            try:
                brief = MotionGraphicBrief.from_dict(brief_raw)
                seconds = max(0.1, float(raw.get("seconds", brief.duration_seconds or 10.0)))
                fps = max(1.0, float(raw.get("fps", brief.fps)))
                frame = max(0, int(raw.get("frame", 0) or 0))
                variant = max(0, int(raw.get("variant", 0) or 0))
            except (TypeError, ValueError):
                continue
            self._pending_motion_graphic_requests[request_id] = {
                "seconds": seconds,
                "fps": fps,
                "frame": frame,
                "has_timeline": True,
                "track_id": str(raw.get("track_id", "") or "") or None,
                "variant": variant,
                "user_text": self._safe_motion_graphic_task_text(raw.get("user_text", "")),
                "scene": None,
                "build_failed": False,
                "brief": brief,
                "phase": "resume_required",
                "resume_phase": str(raw.get("resume_phase", "plan_review") or "plan_review"),
                "creative_brief_pending": False,
                "plan_approved": False,
                "render_mode": "html",
                "creative_proposals": [],
                "selected_proposal_id": None,
                "generation_feedback_memory": self._normalize_feedback_notes(
                    raw.get("generation_feedback_memory", ()),
                ),
                "requires_button_confirmation": False,
                "allow_json_retry": True,
                "restored_from_task_memory": True,
            }
            restored += 1
        if restored and self._main_window is not None:
            self._append_assistant_message(
                self._main_window,
                I18N.tr("motion_graphic.ai.restore_tasks",
                        "Restored {count} unfinished Motion Graphic task(s). Send 'continue' to "
                        "resume safely from the production plan; earlier model output and "
                        "confirmations will not be reused automatically.", count=restored),
            )
        return restored

    @staticmethod
    def _is_motion_graphic_resume_text(text: str) -> bool:
        compact = re.sub(r"[\s，。！？,.!！?、]+", "", str(text or "").lower())
        return compact in {"继续", "继续生成", "恢复", "恢复任务", "继续这个任务", "继续制作"}

    def _resume_motion_graphic_task(self, main_window: Any, request_id: str) -> bool:
        """Restart a recovered task at a safe model-planning checkpoint."""
        plan = self._pending_motion_graphic_requests.get(str(request_id))
        if plan is None or plan.get("phase") != "resume_required":
            return False
        previous_phase = str(plan.pop("resume_phase", "plan_review") or "plan_review")
        plan["restored_from_task_memory"] = False
        plan["scene"] = None
        plan["plan_approved"] = False
        self._append_assistant_message(
            main_window,
            I18N.tr("motion_graphic.ai.restore_task",
                    "Restored this Motion Graphic task. To avoid stale results from before the "
                    "interruption, I will regenerate a reviewable production plan; nothing will be "
                    "generated or changed on the timeline until you confirm."),
        )
        if previous_phase == "proposals":
            plan["phase"] = "proposals"
            plan["creative_brief_pending"] = False
            triggered = self._trigger_motion_graphic_creative_proposals(main_window, str(request_id))
        else:
            plan["phase"] = "plan_review"
            plan["creative_brief_pending"] = True
            plan["creative_brief_retries"] = 0
            triggered = self._trigger_motion_graphic_creative_brief(main_window, str(request_id))
        if not triggered:
            self._mark_motion_graphic_build_failed(
                main_window, str(request_id), I18N.tr("motion_graphic.ai.plan_channel_unavailable",
        "The AI production-plan channel is not wired up or unavailable after restore"),
            )
        return True

    def get_ai_context(self) -> dict[str, Any]:
        """Return compact live context for the shared AI Assistant Dock."""
        from .scene_store import get_shared_scene_store
        from .host_adapters import motion_graphic_scene_id

        scenes = []
        for scene in get_shared_scene_store().scenes():
            scenes.append({
                "scene_id": scene.scene_id,
                "name": scene.name,
                "width": scene.width,
                "height": scene.height,
                "fps": scene.fps,
                "duration_frames": scene.duration_frames,
                "element_count": len(scene.elements),
                "element_ids": [element.element_id for element in scene.elements],
                "elements": [
                    {
                        "element_id": element.element_id,
                        "type": element.type,
                        "content": element.content,
                        "style": dict(element.style),
                        "transform": element.transform.to_dict(),
                        "visible": element.visible,
                    }
                    for element in scene.elements
                ],
            })
        active_scene_id = None
        registry = getattr(self, "_editor_registry", None)
        windows = getattr(registry, "_windows", {}) if registry is not None else {}
        if windows:
            active_scene_id = next(iter(windows))
        clip_references: list[dict[str, Any]] = []
        main_window = self._main_window
        timeline_panel = getattr(main_window, "timeline_panel", None)
        view = timeline_panel.get_timeline_view() if timeline_panel is not None else None
        timeline = getattr(view, "_timeline", None) if view is not None else None
        selected_ids = tuple(
            getattr(getattr(view, "_selection", None), "selected_clip_ids", ()) or ()
        ) if view is not None else ()
        if timeline is not None:
            timeline_width = int(getattr(timeline, "width", 1920) or 1920)
            timeline_height = int(getattr(timeline, "height", 1080) or 1080)
            timeline_fps = getattr(timeline, "fps", None)
            if hasattr(timeline_fps, "num") and hasattr(timeline_fps, "den"):
                try:
                    fps_value = float(timeline_fps.num) / float(timeline_fps.den or 1)
                except (TypeError, ValueError, ZeroDivisionError):
                    fps_value = 25.0
            else:
                try:
                    fps_value = float(timeline_fps or 25.0)
                except (TypeError, ValueError):
                    fps_value = 25.0
            try:
                current_frame = int(timeline_panel.get_current_frame())
            except (AttributeError, TypeError, ValueError):
                current_frame = 0
            for track in timeline.video_tracks:
                for clip in track.clips:
                    asset_id = str(getattr(clip, "asset_id", ""))
                    scene_id = motion_graphic_scene_id(asset_id)
                    if scene_id is None:
                        continue
                    clip_references.append({
                        "clip_id": clip.clip_id,
                        "scene_id": scene_id,
                        "track_id": clip.track_id,
                        "timeline_start": clip.timeline_start,
                        "timeline_duration": clip.timeline_duration,
                        "selected": clip.clip_id in selected_ids,
                    })
            selected_refs = [item for item in clip_references if item["selected"]]
            if active_scene_id is None and selected_refs:
                active_scene_id = selected_refs[0]["scene_id"]
            if active_scene_id is None and clip_references:
                active_scene_id = clip_references[0]["scene_id"]
        return {
            "provider_id": self.feature_id,
            # The timeline is the source of truth for new Motion Graphic
            # designs.  Keep this separate from scene dimensions so the
            # discussion model cannot mistake an old scene/clip for the
            # current project canvas.
            "current_timeline": (
                {
                    "canvas_width": timeline_width,
                    "canvas_height": timeline_height,
                    "aspect_ratio": f"{timeline_width}:{timeline_height}",
                    "fps": fps_value,
                    "current_frame": max(0, current_frame),
                }
                if timeline is not None else None
            ),
            "active_scene_id": active_scene_id,
            "clip_references": clip_references,
            "scenes": scenes,
            "commands": [
                "add_element", "update_element", "reorder_element", "set_animation",
                "preview_frame", "insert_into_timeline", "replace_clip",
            ],
            "requires_confirmation_for_host_writes": True,
        }

    def _attach_timeline(self, main_window: Any, panel: Any) -> None:
        if getattr(panel, "_teaching_animation_attached", False):
            return
        panel._teaching_animation_attached = True
        panel.teaching_animation_open_requested.connect(
            lambda clip_id, track_id: self._open_timeline_clip(main_window, panel, clip_id, track_id)
        )

    def _attach_media_pool(self, main_window: Any, panel: Any) -> None:
        """Route Media Pool creation intent to the feature-owned asset workflow."""
        if getattr(panel, "_teaching_motion_graphic_attached", False):
            return
        signal = getattr(panel, "create_empty_motion_graphic_requested", None)
        connect = getattr(signal, "connect", None)
        if not callable(connect):
            return
        connect(lambda: self.create_empty_motion_graphic(main_window))
        set_available = getattr(panel, "set_motion_graphic_creation_available", None)
        if callable(set_available):
            set_available(True)
        panel._teaching_motion_graphic_attached = True

    @staticmethod
    def _is_ai_agent_animation_request(text: str) -> bool:
        value = str(text or "").lower()
        has_motion_graphic = any(term in value for term in (
            "motion graphic", "motiongraph", "motion graph", "动态图形",
            "教学动画",
        ))
        has_clip_output = any(term in value for term in (
            "clip", "片段", "动画", "视频", "动图",
        ))
        if not (has_motion_graphic and has_clip_output):
            return False

        # 只有明确的宿主操作意图才执行写入时间线；“如何生成/怎么制作”
        # 等教程问题继续交给普通助手回答，避免把问答误当成写操作。
        has_generation_action = any(term in value for term in (
            "生成", "创建", "制作", "新建", "插入", "添加", "做一个", "做个",
            "generate", "create", "make", "insert", "add",
        ))
        if not has_generation_action:
            return False
        is_question = any(term in value for term in (
            "如何", "怎么", "怎样", "教程", "说明", "步骤", "为什么", "能不能", "是否",
            "how", "why", "tutorial", "steps",
        ))
        has_explicit_request = any(term in value for term in (
            "帮我", "请", "直接", "自动", "替我", "为我", "please", "directly",
        ))
        return not is_question or has_explicit_request

    @staticmethod
    def _is_generation_authorization(text: str) -> bool:
        """Recognize only an explicit go-ahead after the design discussion."""
        value = str(text or "").strip().lower()
        if any(term in value for term in ("不要生成", "先别生成", "暂不生成", "not yet", "don't generate")):
            return False
        # In a live discussion the assistant commonly asks a yes/no question
        # such as “这个方向可以吗？”. Treat short affirmative replies as
        # authorization so the request enters the real build path instead of
        # being sent back to the LLM, which can only say that a button exists.
        compact = re.sub(r"[\s，。！？,.!?、]+", "", value)
        if compact in {
            "可以", "可以了", "好", "好的", "没问题", "确认", "开始吧",
            "继续生成", "就这样", "按这个来", "做吧", "制作吧",
        }:
            return True
        return any(term in value for term in (
            "可以生成", "可以帮我生成", "开始生成", "开始帮我生成", "帮我开始生成",
            "开始制作", "按这个生成",
            "确认生成", "批准生成", "生成吧", "可以做了", "就按这个做",
            "go ahead and generate", "start generating", "generate it", "approved",
        ))

    @staticmethod
    def _is_motion_graphic_planning_request(text: str) -> bool:
        """Return whether a creation-looking request explicitly asks for ideation.

        Planning words keep the deliberate proposal flow available when the
        user actually wants to compare ideas first.  Normal requests can still
        discuss requirements, then generate a single draft for confirmation.
        """
        value = str(text or "").lower()
        return any(term in value for term in (
            "先讨论", "讨论一下", "先聊", "先给方案", "给我方案", "方案对比",
            "提供方案", "推荐方案", "先别生成", "不要生成", "暂不生成",
            "discuss first", "show options", "compare options", "do not generate",
        ))

    def _is_motion_graphic_discussion_request(self, text: str) -> bool:
        """Route design turns to chat while a Motion Graphic brief is active."""
        if self._is_generation_authorization(text):
            return False
        return self._motion_graphic_discussion_active

    @staticmethod
    def _is_scene_edit_request(text: str) -> bool:
        value = str(text or "").lower()
        edit_terms = (
            "修改", "改成", "改为", "替换", "换成", "调整", "更新", "变成",
            "颜色", "色彩", "字体", "字号", "标题", "副标题", "文字",
            "change", "update", "replace", "color", "font", "title",
        )
        return any(term in value for term in edit_terms)

    @staticmethod
    def _is_motion_graphic_edit_request(text: str) -> bool:
        """Does this look like a revision of an already-generated clip?

        Must be combined with a resolvable HTML clip target: the routing only
        fires when the user has an unambiguous Motion Graphic to edit (clip
        open in the editor / selected on the timeline / last generated one),
        so ordinary chat with an edit verb is never hijacked.
        """
        value = str(text or "").lower()
        edit_terms = (
            "修改", "改一下", "改成", "改为", "换成", "替换", "调整", "更新",
            "去掉", "删掉", "移除", "加上", "添加", "再改", "重改", "重新改",
            "不喜欢", "不好看", "不对", "换一版", "换个配色", "字号大一点",
            "change", "update", "replace", "tweak", "modify",
        )
        if not any(term in value for term in edit_terms):
            return False
        # "重新做一个/新生成" 是全新创作意图，仍走完整创建流程。
        fresh_terms = (
            "重新生成", "重新做一个", "重新做一", "全新", "另外做一个",
            "新做一个", "再生成一个", "再来一个", "regenerate", "start over",
        )
        if any(term in value for term in fresh_terms):
            return False
        question_terms = ("如何", "怎么", "怎样", "为什么", "教程", "how ", "why ", "tutorial")
        return not any(term in value for term in question_terms)

    def _motion_graphic_edit_target(
        self, main_window: Any, text: str,
    ) -> tuple[str, Any, str] | None:
        """Resolve the HTML clip an untargeted revision should modify."""
        from .scene_store import get_shared_scene_store

        store = get_shared_scene_store()
        scene = self._resolve_scene_for_edit(main_window, text)
        if scene is not None and getattr(scene, "html_layer", None) is not None:
            return (
                f"teaching_animation:{scene.scene_id}", scene, "Motion Graphic Clip",
            )
        last = self._last_motion_graphic_target
        if last:
            scene = store.get(str(last.get("scene_id", "")))
            if scene is not None and getattr(scene, "html_layer", None) is not None:
                return (
                    str(last.get("asset_id", "") or ""),
                    scene,
                    str(last.get("display_name", "") or "Motion Graphic Clip"),
                )
        return None

    @staticmethod
    def _is_html_generate_request(text: str) -> bool:
        """Recognize explicit Freeform HTML generation, not HTML questions."""
        value = str(text or "").strip().lower()
        html_terms = (
            "freeform html", "html 动画", "html动画", "html 动效", "html动效",
            "html 页面", "html页面", "网页动画", "网页动效", "css 动画", "css动画",
            "html layer", "html图层", "html 图层",
        )
        template_terms = (
            "motion graphic generator", "motiongraphic generator", "motion graphic template",
            "motiongraphic template", "效果生成器", "效果模板", "可复用模板", "可复用生成器",
            "模板", "生成器",
        )
        if not any(term in value for term in html_terms + template_terms):
            return False
        action_terms = (
            "生成", "创建", "制作", "新建", "设计", "做一个",
            "generate", "create", "make", "design", "build",
        )
        if not any(term in value for term in action_terms):
            return False
        question_terms = (
            "如何", "怎么", "怎样", "教程", "说明", "步骤", "为什么",
            "how", "why", "tutorial", "steps",
        )
        explicit_terms = ("帮我", "请", "直接", "自动", "为我", "please", "directly")
        return not any(term in value for term in question_terms) or any(
            term in value for term in explicit_terms
        )

    @staticmethod
    def _is_motion_graphic_template_request(text: str) -> bool:
        """Return whether the user explicitly asks to publish a reusable recipe."""
        value = str(text or "").strip().lower()
        return any(term in value for term in (
            "motion graphic generator", "motiongraphic generator", "motion graphic template",
            "motiongraphic template", "效果生成器", "效果模板", "可复用模板", "可复用生成器",
            "模板", "生成器",
        ))

    def _get_html_service_for(self, scene_id: str) -> Any | None:
        """Return a scene-scoped HTML AI service over the shared scene store."""
        from .scene_store import get_shared_scene_store
        from .ai.service import TeachingAnimationAIService

        scene = get_shared_scene_store().get(str(scene_id))
        if scene is None:
            return None
        service = self._html_services.get(scene.scene_id)
        if service is None:
            service = TeachingAnimationAIService(scene)
            self._html_services[scene.scene_id] = service
        elif service.scene is not scene:
            # AUD-28001: a cached service snapshots the scene it was built
            # with.  Writes that only touch the shared store — editor scene
            # commands, confirmed scene edits, a project reload via
            # ``replace_all`` — never sync into that snapshot, so applying an
            # HTML layer through it and re-registering the result would
            # silently roll the store back to the stale scene.  Refresh the
            # service from the store's current document (the shared store is
            # the single source of truth) before any layer is applied to it.
            service.update_scene(scene)
        return service

    @staticmethod
    def _html_preview_backend() -> Any | None:
        """Return an already-assembled backend without blocking the UI thread.

        The browser-backed preview becomes asynchronous in the editor batch.
        Until then the normal export preflight remains the authoritative HTML
        renderer gate; a missing backend is reported by the outcome and never
        silently treated as preview evidence.
        """
        return None

    @staticmethod
    def _html_generation_constraints(scene: Any) -> Any:
        from .ai.html_generation_protocol import HtmlGenerationConstraints

        return HtmlGenerationConstraints(
            max_duration_seconds=max(1.0, float(scene.duration_frames) / float(scene.fps)),
            fps=float(scene.fps),
            viewport_width=float(scene.width),
            viewport_height=float(scene.height),
        )

    def _resolve_motion_graphic_targets(
        self, main_window: Any, target_asset_ids: tuple[str, ...],
    ) -> tuple[str, Any, str] | None:
        """Resolve an explicit media-pool target without using timeline heuristics.

        ``target_asset_ids`` comes from the media-pool drag payload and survives
        project reloads because it is the project's stable MediaAsset ID.  A
        missing/ambiguous target is reported instead of silently editing the
        currently selected clip.
        """
        from .host_adapters import motion_graphic_scene_id
        from .scene_store import get_shared_scene_store

        ids = tuple(dict.fromkeys(str(value).strip() for value in target_asset_ids if str(value).strip()))
        project = getattr(main_window, "_current_edit_project", None)
        assets = getattr(project, "assets", {}) if project is not None else {}
        store = get_shared_scene_store()
        resolved: list[tuple[str, Any, str]] = []
        missing: list[str] = []
        for asset_id in ids:
            asset = assets.get(asset_id) if hasattr(assets, "get") else None
            media_type = str(getattr(asset, "media_type", "") or "")
            scene_id = ""
            if asset is not None and media_type == "motion_graphic":
                metadata = getattr(asset, "metadata", {}) or {}
                scene_id = str(metadata.get("scene_id", "") or "")
                scene_id = scene_id or str(motion_graphic_scene_id(asset_id) or "")
            elif asset is None and project is None:
                # A host without a project object can still resolve a freshly
                # restored scene-store entry, but it still requires the
                # canonical Motion Graphic asset ID prefix.
                scene_id = str(motion_graphic_scene_id(asset_id) or "")
            if not scene_id:
                missing.append(asset_id)
                continue
            scene = store.get(scene_id)
            if scene is None:
                missing.append(asset_id)
                continue
            display_name = str(
                getattr(asset, "display_name", "") or "Motion Graphic Clip"
            ) if asset is not None else "Motion Graphic Clip"
            resolved.append((asset_id, scene, display_name))

        if missing:
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.target_missing",
                        "The dropped Motion Graphic clip no longer exists or is not loaded with "
                        "the current project: {missing}",
                        missing=", ".join(missing[:4])),
                error_code="motion_graphic_target_missing",
            )
            return None
        unique_scene_ids = {str(item[1].scene_id) for item in resolved}
        if len(unique_scene_ids) != 1 or len(resolved) != 1:
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.target_ambiguous",
                        "This edit targets multiple Motion Graphic clips at once. Drop exactly "
                        "one target clip; otherwise the AI cannot safely decide which to edit."),
                error_code="motion_graphic_target_ambiguous",
            )
            return None
        asset_id, scene, display_name = resolved[0]
        if getattr(scene, "html_layer", None) is None:
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.target_not_html",
                        "Locked onto '{name}', but it is not a web HTML layer and cannot be "
                        "edited in the browser. Convert it to an HTML clip in the Motion Graphic "
                        "editor first.", name=display_name),
                error_code="motion_graphic_target_not_html",
            )
            return None
        return asset_id, scene, display_name

    def _handle_motion_graphic_mode_request(
        self, main_window: Any, text: str, *,
        target_asset_ids: tuple[str, ...] = (),
    ) -> bool:
        """Route explicit mode requests to new creation or an explicit edit target."""
        if target_asset_ids:
            return self._queue_motion_graphic_edit(
                main_window, text, tuple(target_asset_ids),
            )
        return self._queue_motion_graphic_plan(
            main_window, text, build_directly=True, source_text=text,
        )

    def _queue_motion_graphic_edit(
        self, main_window: Any, text: str, target_asset_ids: tuple[str, ...],
    ) -> bool:
        """Start a browser-authoring revision for one existing Motion Graphic.

        The result replaces the target scene's HTML layer IN PLACE (no second
        timeline clip, no intermediate confirmation card): the clip already
        exists and the user asked for a modification, so after the sanitizer
        and the real-browser validation the revised page is applied directly
        through the same undoable write path as manual confirmation.
        """
        from .scene_store import get_shared_scene_store

        target = self._resolve_motion_graphic_targets(main_window, target_asset_ids)
        if target is None:
            return True
        asset_id, scene, display_name = target
        store = get_shared_scene_store()
        request_id = f"html-edit-{uuid.uuid4().hex}"
        known_asset_ids = tuple(
            str(ref.asset_id) for ref in getattr(scene, "asset_refs", ())
            if getattr(ref.asset_id, "")
        )
        constraints = replace(
            self._html_generation_constraints(scene),
            post_generation_quality_gate=False,
            allow_network_resources=True,
            allow_inline_scripts=True,
            allow_event_handlers=True,
            allow_freeform_css=True,
            allow_inline_visual_resources=True,
            allow_gpu_apis=True,
        )
        current_html = str(getattr(scene.html_layer, "sanitized_html", "") or "")
        # Autonomous edit pipeline, step 1-2: explore the current page, then
        # classify.  Simple tweaks go straight to the single-shot revision;
        # structural / multi-intent requests get a planner round first.
        from .ai.html_edit_autonomy import (
            build_edit_dossier, classify_edit_complexity,
            deterministic_minimal_html_edit, explicit_noop_reason,
        )
        dossier = build_edit_dossier(current_html)
        feedback_memory = self._feedback_context_for_motion_graphic(
            scene, asset_id, scene.scene_id,
        )
        noop_reason = explicit_noop_reason(str(text or ""), dossier)
        if noop_reason:
            self._append_user_message(main_window, f"修改「{display_name}」：{text}")
            self._append_assistant_message(
                main_window,
                f"已核对 Motion Graphic Clip「{display_name}」：{noop_reason}，"
                "因此没有发起模型重写，也没有修改片段。",
            )
            self._set_run_status(
                main_window, "无需修改", detail=noop_reason, state="succeeded",
            )
            return True
        minimal_edit_html = deterministic_minimal_html_edit(
            str(text or ""), current_html,
        )
        complexity, complexity_reason = classify_edit_complexity(
            str(text or ""), dossier,
        )
        self._record_user_feedback(
            str(text or ""), request_id=request_id, domain="general",
            artifact_refs=(asset_id, scene.scene_id),
        )
        self._pending_html_generate_requests[request_id] = {
            "scene_id": scene.scene_id,
            "scene_revision": store.revision(scene.scene_id),
            "user_text": str(text or ""),
            "constraints": constraints,
            "known_asset_ids": known_asset_ids,
            "raw_html": "",
            "outcome": None,
            "target_asset_id": asset_id,
            "target_display_name": display_name,
            "current_html": current_html,
            # The pre-edit document: postconditions compare against this, not
            # against the per-step working copy.
            "original_html": current_html,
            # Edits of an existing clip apply directly once the browser
            # validation passes; the undo stack (editor) replaces the
            # pre-confirmation ceremony.
            "auto_apply": True,
            "edit_retries": 0,
            "edit_retry_feedback": "",
            "edit_phase": "plan" if complexity == "complex" else "step",
            "edit_dossier": dossier.summary(),
            "edit_complexity_reason": complexity_reason,
            "edit_plan_steps": (),
            "edit_step_index": 0,
            "step_instruction": "",
            "edit_history": (),
            "feedback_memory": feedback_memory,
        }
        self._append_user_message(main_window, f"修改「{display_name}」：{text}")
        if minimal_edit_html is not None:
            self._append_assistant_message(
                main_window,
                f"已锁定 Motion Graphic Clip「{display_name}」；识别为可精确验证的"
                "局部标题替换，不重写其他页面或样式，正在按原有 HTML 做最小修改并验证。",
            )
            self._set_run_status(
                main_window, "正在精确修改网页", detail="仅替换指定页的主标题…",
            )
            # The deterministic patch still enters the same sanitize,
            # postcondition and browser-validation pipeline as model output.
            # It simply avoids an avoidable whole-document LLM rewrite.
            self._handle_model_html_generate_response(
                main_window, request_id, minimal_edit_html,
            )
            return True
        self._set_run_status(
            main_window, f"修改「{display_name}」",
            detail=(
                f"修改较复杂（{complexity_reason}），先制定分步计划…"
                if complexity == "complex" else "正在读取网页并修改…"
            ),
        )
        if complexity == "complex":
            self._append_assistant_message(
                main_window,
                f"已锁定 Motion Graphic Clip「{display_name}」；这次修改{complexity_reason}，"
                "我会先核对当前网页状态并制定分步计划，再逐步执行——每步完成都会直接更新"
                "这个片段（不新建素材、不弹确认卡，可撤销），全部通过后向你汇报。",
            )
        else:
            self._append_assistant_message(
                main_window,
                f"已锁定 Motion Graphic Clip「{display_name}」；正在读取当前网页并修改，"
                "完成后会直接更新这个片段（不新建素材、不弹确认卡），可继续反馈或撤销。",
            )
        return self._start_html_edit_chat(main_window, request_id)

    @staticmethod
    def _invoke_html_edit_starter(
        starter: Any, request_id: str, user_prompt: str,
        system_instruction: str, *, temperature: float | None = None,
    ) -> bool:
        """Start an edit-phase chat, tolerating minimal starter signatures."""
        try:
            if temperature is not None:
                return bool(starter(
                    request_id, user_prompt,
                    system_instruction=system_instruction,
                    temperature=temperature,
                ))
            return bool(starter(
                request_id, user_prompt, system_instruction=system_instruction,
            ))
        except TypeError:
            # Compatibility with a minimal fake/controller that only accepts
            # the two positional arguments.
            try:
                return bool(starter(request_id, user_prompt))
            except Exception:  # noqa: BLE001
                logger.debug("Motion Graphic edit chat trigger failed", exc_info=True)
                return False
        except (AttributeError, RuntimeError, ValueError) as exc:
            logger.warning("Motion Graphic target edit chat could not start: %s", exc)
            return False

    def _start_html_edit_chat(self, main_window: Any, request_id: str) -> bool:
        """(Re)start the LLM chat for one pending targeted edit.

        Dispatches on the edit phase: ``plan`` asks the planner model for a
        step decomposition (low temperature, JSON contract); ``step`` runs the
        ordinary single-shot revision with the full browser-authoring prompt,
        scoped to one step's instruction when a plan is being executed.
        """
        from .scene_store import get_shared_scene_store

        plan = self._pending_html_generate_requests.get(str(request_id))
        if plan is None:
            return True
        store = get_shared_scene_store()
        scene = store.get(str(plan["scene_id"]))
        if scene is None:
            self._pending_html_generate_requests.pop(str(request_id), None)
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.scene_missing_edit",
                        "The target Motion Graphic scene no longer exists; nothing was edited."),
                error_code="html_scene_missing",
            )
            return True
        controller = getattr(main_window, "_ai_edit_controller", None)
        starter = getattr(controller, "start_html_generate_chat", None)
        if not callable(starter):
            self._handle_model_html_generate_response(main_window, request_id, "")
            return True
        canvas = {"width": int(scene.width), "height": int(scene.height)}
        seconds = max(1.0, float(scene.duration_frames) / float(scene.fps))

        if plan.get("edit_phase") == "plan":
            from .ai.html_edit_autonomy import EDIT_PLAN_SYSTEM_PROMPT
            self._set_run_status(
                main_window, "正在制定分步修改计划",
                detail="核对当前网页状态，拆解修改步骤…",
            )
            user_prompt = (
                f"用户修改需求：{str(plan.get('user_text', '') or '').strip()}\n\n"
                f"当前网页状态档案：\n{plan.get('edit_dossier', '')}\n\n"
                "请按系统提示核对当前网页并输出分步修改计划（只输出 JSON）。"
                "当前网页完整源码：\n"
                "--- CURRENT HTML ---\n"
                + str(plan.get("current_html", "") or "").strip()
                + "\n--- END CURRENT HTML ---"
            )
            started = self._invoke_html_edit_starter(
                starter, str(request_id), user_prompt,
                EDIT_PLAN_SYSTEM_PROMPT, temperature=0.3,
            )
            if not started:
                self._handle_model_html_generate_response(main_window, request_id, "")
            return True

        steps = tuple(plan.get("edit_plan_steps") or ())
        index = int(plan.get("edit_step_index", 0) or 0)
        base_text = str(plan.get("user_text", "") or "")
        retry_feedback = str(plan.get("edit_retry_feedback", "") or "")
        if steps:
            base_text = str(plan.get("step_instruction", "") or "") or base_text
            feedback = (
                f"这是分步修改计划的第 {index + 1}/{len(steps)} 步"
                f"（{steps[index].get('summary', '')}）。只需完成本步要求，"
                "不要做其他步骤的改动。"
                + (f"\n（自动检查：{retry_feedback}）" if retry_feedback else "")
            )
        elif retry_feedback:
            feedback = f"{base_text}\n（自动检查：{retry_feedback}）"
        else:
            # 与原始单发修改路径一致：修改要求同时作为修订说明出现。
            feedback = base_text
        user_prompt = self._build_motion_graphic_html_user_prompt(
            base_text,
            seconds=seconds,
            fps=float(scene.fps),
            current_html=str(plan.get("current_html", "") or ""),
            revision_feedback=feedback,
            **canvas,
        )
        feedback_memory = str(plan.get("feedback_memory", "") or "").strip()
        if feedback_memory:
            user_prompt += (
                "\n以下是该片段此前的用户反馈，仅在不与本轮要求冲突时遵循；"
                "不要把它当作新的硬性模板：\n" + feedback_memory + "\n"
            )
        if steps:
            self._set_run_status(
                main_window, "正在执行修改计划",
                detail=f"第 {index + 1}/{len(steps)} 步："
                f"{steps[index].get('summary', '')}",
            )
        else:
            self._set_run_status(main_window, "正在修改网页", detail="模型设计中…")
        started = self._invoke_html_edit_starter(
            starter, str(request_id), user_prompt,
            self._assistant_plugin_prompt(
                "html_build", self._MOTION_GRAPHIC_HTML_BUILD_SYSTEM_PROMPT,
            ),
        )
        if not started:
            self._handle_model_html_generate_response(main_window, request_id, "")
        return True

    def _trigger_html_generate(self, main_window: Any, text: str) -> bool:
        """Queue an HTML generation request and start the shared chat worker."""
        from .scene_store import get_shared_scene_store
        from .ai.html_llm_generator import build_html_system_prompt

        scene = self._resolve_scene_for_edit(main_window, text)
        if scene is None:
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.scene_unavailable",
                        "Open or select a Motion Graphic scene before generating HTML motion."),
                error_code="html_scene_unavailable",
            )
            return True
        store = get_shared_scene_store()
        constraints = self._html_generation_constraints(scene)
        request_id = f"html-generate-{uuid.uuid4().hex}"
        known_asset_ids = tuple(
            str(ref.asset_id) for ref in getattr(scene, "asset_refs", ())
            if getattr(ref, "asset_id", "")
        )
        self._pending_html_generate_requests[request_id] = {
            "scene_id": scene.scene_id,
            "scene_revision": store.revision(scene.scene_id),
            "user_text": str(text or ""),
            "constraints": constraints,
            "known_asset_ids": known_asset_ids,
            "raw_html": "",
            "outcome": None,
        }
        self._append_user_message(main_window, text)
        controller = getattr(main_window, "_ai_edit_controller", None)
        starter = getattr(controller, "start_html_generate_chat", None)
        if not callable(starter):
            self._handle_model_html_generate_response(main_window, request_id, "")
            return True
        system_prompt = build_html_system_prompt(
            constraints, known_asset_ids=known_asset_ids,
        )
        user_prompt = (
            f"用户需求：{str(text or '').strip()}\n"
            "请严格按照系统约束，只返回完整 HTML 文档，不要 Markdown 或解释文字。"
        )
        try:
            started = bool(starter(
                request_id, user_prompt, system_instruction=system_prompt,
            ))
        except TypeError:
            # Compatibility with a lightweight host/controller double that only
            # accepts the original two positional arguments.
            started = bool(starter(request_id, user_prompt))
        except (AttributeError, RuntimeError, ValueError) as exc:
            logger.warning("HTML generation chat could not start: %s", exc)
            started = False
        if not started:
            self._handle_model_html_generate_response(main_window, request_id, "")
        return True

    def _handle_model_html_generate_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
        """Run the returned HTML through the review-only pipeline."""
        motion_plan = self._pending_motion_graphic_requests.get(str(request_id))
        if motion_plan is not None and motion_plan.get("render_mode") == "html":
            return self._handle_model_motion_graphic_html_response(
                main_window, str(request_id), response,
            )
        plan = self._pending_html_generate_requests.get(str(request_id))
        if plan is None:
            return False
        # Planner phase: the model returned a step plan (JSON), not HTML.
        if plan.get("edit_phase") == "plan":
            return self._handle_edit_plan_response(
                main_window, str(request_id), str(response or ""),
            )
        from .ai.html_llm_generator import strip_markdown_fence
        raw_html = strip_markdown_fence(str(response or ""))
        if not raw_html:
            plan["outcome"] = None
            if plan.get("auto_apply"):
                feedback = "上一次修改没有返回 HTML 内容。请输出完整的 HTML 文档，只完成当前步骤。"
                if self._retry_html_edit_generate(main_window, str(request_id), feedback):
                    return None
                self._pending_html_generate_requests.pop(str(request_id), None)
                scope = self._html_edit_failure_scope(plan)
                self._append_assistant_message(
                    main_window,
                    I18N.tr("motion_graphic.ai.generation_empty_scope",
                            "HTML motion generation returned no content, {scope}", scope=scope),
                    error_code="html_generation_empty",
                )
                self._set_run_status(main_window, I18N.tr("motion_graphic.ai.status_failed", "Edit failed"), detail=scope, state="failed")
                self._complete_html_edit_run(main_window, str(request_id), success=False)
                return None
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.generation_empty",
                        "HTML motion generation returned no content; the scene was not modified."),
                error_code="html_generation_empty",
            )
            return True
        service = self._get_html_service_for(plan["scene_id"])
        if service is None:
            self._pending_html_generate_requests.pop(str(request_id), None)
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.scene_missing_write",
                        "The target Motion Graphic scene no longer exists; the HTML motion was "
                        "not written."),
                error_code="html_scene_missing",
            )
            if plan.get("auto_apply"):
                self._set_run_status(main_window, I18N.tr("motion_graphic.ai.status_failed", "Edit failed"), detail=I18N.tr("motion_graphic.ai.status_scene_missing", "target scene no longer exists"), state="failed")
                self._complete_html_edit_run(main_window, str(request_id), success=False)
                return None
            return True
        outcome = service.generate_html(
            _StaticHtmlClient(raw_html),
            constraints=plan["constraints"],
            known_asset_ids=plan["known_asset_ids"],
            approved=False,
            preview_backend=self._html_preview_backend(),
            preview_frame=0,
        )
        plan["raw_html"] = raw_html
        plan["outcome"] = outcome
        from .ai.html_edit_audit import diff_html_edit
        step_audit = diff_html_edit(
            str(plan.get("current_html", "") or ""), outcome.sanitized_html,
        )
        plan["step_audit"] = dict(step_audit.to_dict())
        # A revision must stay a revision AND must provably satisfy the user's
        # stated requirements: truncation, page collapse and literal intent
        # checks (quoted copy / colors / page add-remove) all gate the apply.
        if plan.get("auto_apply") and outcome.sanitized_html.strip():
            from .ai.html_edit_autonomy import edit_postcondition_issues

            steps = tuple(plan.get("edit_plan_steps") or ())
            index = int(plan.get("edit_step_index", 0) or 0)
            is_final_step = not steps or index >= len(steps) - 1
            shrink = ""
            if plan.get("current_html"):
                shrink = self._motion_graphic_page_shrink_feedback(
                    str(plan["current_html"]), outcome.sanitized_html,
                )
            truncated = "</html>" not in raw_html.lower()
            postissues = edit_postcondition_issues(
                str(plan.get("user_text", "") or ""),
                str(plan.get("original_html", "") or plan.get("current_html", "") or ""),
                outcome.sanitized_html,
            ) if is_final_step else []
            retry_feedback = shrink or (
                "上一次输出似乎被截断了（缺少 </html> 结尾）。请输出完整的 HTML 文档。"
                if truncated else ""
            ) or (
                "修改后仍未满足用户提出的要求：" + "；".join(postissues)
                if postissues else ""
            )
            if retry_feedback and self._retry_html_edit_generate(
                main_window, str(request_id), retry_feedback,
            ):
                return None
            if shrink or postissues:
                # Never apply a revision that lost pages or provably fails the
                # user's stated requirements.
                self._pending_html_generate_requests.pop(str(request_id), None)
                reason = shrink or "；".join(postissues)
                scope = self._html_edit_failure_scope(plan)
                self._append_assistant_message(
                    main_window,
                    I18N.tr("motion_graphic.ai.edit_page_loss",
                            "The web edit was not applied: {reason}. {scope}Try describing the "
                            "change more specifically.", reason=reason, scope=scope),
                    error_code="motion_graphic_edit_page_loss",
                )
                self._set_run_status(
                    main_window, I18N.tr("motion_graphic.ai.status_not_applied", "Edit not applied"), detail=str(reason)[:80], state="failed",
                )
                self._complete_html_edit_run(main_window, str(request_id), success=False)
                return None
            # Targeted edits of an existing clip apply directly: the sanitizer
            # passed above and the confirm path repeats the browser validation
            # before the undoable in-place write.  No confirmation card.
            step_line = (
                f"（第 {index + 1}/{len(steps)} 步：{steps[index].get('summary', '')}）"
                if steps else ""
            )
            self._append_assistant_message(
                main_window,
                f"网页修改已生成并通过安全检查{step_line}，"
                "正在用真实浏览器验证后直接应用…",
            )
            self._set_run_status(
                main_window, "正在验证修改", detail="真实浏览器逐页检查…",
            )
            # AUD-28005: the confirm browser gate runs off the UI thread.  The
            # shared completion callback runs the apply/failure continuation
            # in both the synchronous path and the queued background path.
            def _direct_apply_done(applied: bool) -> None:
                if applied:
                    self._complete_direct_html_edit_apply(
                        main_window, str(request_id), plan,
                    )
                else:
                    self._fail_direct_html_edit_apply(
                        main_window, str(request_id), plan,
                    )

            self._confirm_html_generate_request(
                main_window, str(request_id), completion=_direct_apply_done,
            )
            return None
        reasons = outcome.blocking_reasons
        if plan.get("auto_apply"):
            self._pending_html_generate_requests.pop(str(request_id), None)
            scope = self._html_edit_failure_scope(plan)
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.quality_blocked",
                        "The web edit failed the safety checks: {reasons}. {scope}",
                        reasons="; ".join(reasons[:6]) if reasons else
                        I18N.tr("motion_graphic.ai.sanitized_empty",
                                "the sanitized content is empty"),
                        scope=scope),
                error_code="html_generation_quality_blocked",
            )
            self._set_run_status(
                main_window, "修改未通过安全检查",
                detail=scope, state="failed",
            )
            self._complete_html_edit_run(main_window, str(request_id), success=False)
            return None
        target_line = ""
        if plan.get("target_display_name"):
            target_line = (
                f"目标 Motion Graphic Clip：{plan['target_display_name']} "
                f"（asset_id：{plan.get('target_asset_id', '')}）\n"
            )
        details = (
            target_line +
            f"scene_id：{plan['scene_id']}\n"
            "当前只完成安全检查和预览准备，确认前不会修改场景。\n"
            f"sanitized：{'通过' if outcome.sanitized_html else '失败'}\n"
            f"preview：{'可用' if outcome.renderer_available else '待渲染后端'}\n"
            f"诊断：{'；'.join(reasons[:6]) if reasons else '无阻断诊断'}\n"
            "确认后将通过 SetHtmlLayerCommand 写入，可撤销。"
        )
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        show_plan = getattr(panel, "set_motion_graphic_proposal", None)
        if not callable(show_plan):
            return False
        show_plan(str(request_id), "HTML Motion 动效预览", details)
        return True

    @staticmethod
    def _motion_graphic_page_shrink_feedback(
        current_html: str, revised_html: str,
    ) -> str:
        """Diagnose a revision that collapsed the authored page deck."""
        from .core.html_page_deck import detect_page_count

        current_pages = detect_page_count(str(current_html or ""))
        revised_pages = detect_page_count(str(revised_html or ""))
        if current_pages >= 2 and revised_pages < current_pages:
            return (
                f"浏览器检查发现：当前网页共有 {current_pages} 页"
                f"（data-topos-page），而你这次只输出了 {revised_pages} 页。"
                f"请保留全部 {current_pages} 页、它们的顺序和整体设计语言，"
                "只修改用户要求的部分，并输出完整 HTML 文档。"
            )
        return ""

    @staticmethod
    def _html_edit_failure_scope(plan: Mapping[str, Any]) -> str:
        """Describe exactly what remains after a planned edit step fails.

        Planned edits are applied one step at a time so users can retain work
        completed before a later model/browser/postcondition failure.  The old
        generic wording said the clip was unchanged even after earlier steps
        had been committed, which made recovery needlessly confusing.
        """
        steps = tuple(plan.get("edit_plan_steps") or ())
        index = max(0, int(plan.get("edit_step_index", 0) or 0))
        if not steps:
            return "片段保持原样。"
        total = len(steps)
        if index <= 0:
            return f"修改在第 1/{total} 步停止，片段保持原样。"
        return (
            f"修改在第 {min(index + 1, total)}/{total} 步停止；"
            f"前 {min(index, total)} 步已经写入，保留在当前片段中。"
        )

    def _retry_html_edit_generate(
        self, main_window: Any, request_id: str, feedback: str,
    ) -> bool:
        """Give one targeted edit a repair pass with the exact diagnosis."""
        plan = self._pending_html_generate_requests.get(str(request_id))
        if plan is None or not plan.get("auto_apply"):
            return False
        retries = int(plan.get("edit_retries", 0) or 0)
        if retries >= 1:
            return False
        plan["edit_retries"] = retries + 1
        plan["edit_retry_feedback"] = str(feedback or "").strip()
        plan["raw_html"] = ""
        plan["outcome"] = None
        steps = tuple(plan.get("edit_plan_steps") or ())
        index = max(0, int(plan.get("edit_step_index", 0) or 0))
        retained = (
            f"前 {index} 步已写入，会继续保留。" if steps and index > 0
            else "当前片段尚未改动。"
        )
        self._append_assistant_message(
            main_window,
            "这版修改没有通过检查，正在带着诊断自动重做一次；" + retained,
        )
        self._set_run_status(main_window, "正在自动修复上一版问题", detail=str(feedback or "")[:80])
        return self._start_html_edit_chat(main_window, request_id)

    def _handle_edit_plan_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
        """Consume the planner's step plan; fall back to a single-shot edit.

        Returns ``None`` to the controller-side caller (via the response
        handler) so the chat transport treats this phase as self-driving:
        no "waiting for confirmation" bookkeeping belongs to an edit flow
        that applies directly.
        """
        from .ai.html_edit_autonomy import parse_edit_plan

        plan = self._pending_html_generate_requests.get(str(request_id))
        if plan is None:
            return False
        edit_plan = parse_edit_plan(str(response or ""))
        if edit_plan is None:
            # Not a plan (empty/junk/HTML): degrade gracefully to the direct
            # single-shot revision instead of failing the edit.
            plan["edit_phase"] = "step"
            plan["edit_plan_steps"] = ()
            self._append_assistant_message(
                main_window,
                "修改计划没有按约定返回，改为一次性直接修改；结果仍会先通过"
                "浏览器验证再应用。",
            )
            self._set_run_status(
                main_window, "正在修改网页", detail="计划解析失败，改为直接修改…",
            )
            self._start_html_edit_chat(main_window, request_id)
            return None
        plan["edit_phase"] = "step"
        plan["edit_plan_steps"] = tuple(
            {"summary": step.summary, "instruction": step.instruction}
            for step in edit_plan.steps
        )
        plan["edit_step_index"] = 0
        plan["step_instruction"] = edit_plan.steps[0].instruction
        findings = f"核对当前网页：{edit_plan.issues_found}。" if edit_plan.issues_found else ""
        if len(edit_plan.steps) <= 1:
            self._append_assistant_message(
                main_window,
                f"{findings}已确认可以一次改完（理解：{edit_plan.understanding}），开始修改。",
            )
            self._set_run_status(
                main_window, "正在修改网页", detail="计划确认：一次完成",
            )
        else:
            step_lines = "；".join(
                f"{i}. {step.summary}"
                for i, step in enumerate(edit_plan.steps, start=1)
            )
            self._append_assistant_message(
                main_window,
                f"{findings}已制定 {len(edit_plan.steps)} 步修改计划：{step_lines}。"
                "开始执行第 1 步；每步完成都会直接更新片段。",
            )
        self._start_html_edit_chat(main_window, request_id)
        return None

    def _continue_html_edit_steps(
        self, main_window: Any, request_id: str, plan: dict[str, Any],
    ) -> None:
        """Advance to the next planned step, seeded with the applied HTML."""
        from .scene_store import get_shared_scene_store

        steps = tuple(plan.get("edit_plan_steps") or ())
        index = int(plan.get("edit_step_index", 0) or 0) + 1
        store = get_shared_scene_store()
        scene = store.get(str(plan.get("scene_id", "")))
        if (
            scene is None
            or getattr(scene, "html_layer", None) is None
            or index >= len(steps)
        ):
            return
        refreshed = {
            **plan,
            "scene_revision": store.revision(scene.scene_id),
            "current_html": str(scene.html_layer.sanitized_html or ""),
            "raw_html": "",
            "outcome": None,
            "edit_step_index": index,
            "edit_retries": 0,
            "edit_retry_feedback": "",
            "step_instruction": str(steps[index].get("instruction", "") or ""),
        }
        self._pending_html_generate_requests[str(request_id)] = refreshed
        self._append_assistant_message(
            main_window,
            f"第 {index + 1}/{len(steps)} 步：{steps[index].get('summary', '')}",
        )
        self._start_html_edit_chat(main_window, request_id)

    def _handle_model_motion_graphic_html_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool | None:
        """Stage a model-authored HTML deck as a Motion Graphic scene.

        The HTML is sanitized and statically analysed before a scene is shown
        to the user.  The scene is only inserted into the timeline by the
        existing explicit confirmation button, just like command-generated
        Motion Graphics.
        """
        plan = self._pending_motion_graphic_requests.get(str(request_id))
        if plan is None:
            return False
        # Revision rounds must deliver a complete document: a truncated reply
        # would silently collapse the deck, so repair it before any browser
        # work is spent on it.
        if plan.get("html_edit_context") and "</html>" not in str(response or "").lower():
            if self._retry_motion_graphic_html_build(
                main_window, str(request_id),
                "上一次输出似乎被截断了（缺少 </html> 结尾）。"
                "请保留全部页面并输出完整 HTML 文档。",
            ):
                return True
        # The model response arrives on the UI thread.  In production the
        # browser-backed phase is handed to the controller's QThread worker;
        # tests and lightweight hosts keep the synchronous path below.
        import os as _os
        controller = getattr(main_window, "_ai_edit_controller", None)
        start_background = getattr(
            controller, "start_motion_graphic_postprocess", None,
        )
        if _os.environ.get("TOPOS_TEST_MODE") != "1" and callable(start_background):
            snapshot = self._motion_graphic_html_plan_snapshot(plan)
            started = bool(start_background(
                request_id,
                response,
                lambda rid, reply: self._prepare_motion_graphic_html_response(
                    rid, reply, snapshot,
                ),
                lambda rid, payload: self._finish_background_motion_graphic_html(
                    main_window, rid, payload,
                ),
                lambda rid, error: self._fail_background_motion_graphic_html(
                    main_window, rid, error,
                ),
            ))
            if started:
                self._append_assistant_message(
                    main_window,
                    "模型已返回网页，正在后台启动浏览器并生成预览；你仍可以继续操作软件。",
                )
                self._set_run_status(
                    main_window, "正在后台浏览器验证",
                    detail="逐页检查可见性，期间可继续操作…",
                )
                return None
        # The HTML worker is non-JSON, but providers can still wrap a complete
        # document in ```html fences or a short preamble.  Normalize before
        # dispatching to the sanitizer so those transport details cannot become
        # visible page text or make the build look like an empty response.
        from .ai.html_llm_generator import strip_markdown_fence

        raw_html = strip_markdown_fence(str(response or ""))
        if not raw_html:
            if self._retry_motion_graphic_html_build(
                main_window, request_id, "模型没有返回完整 HTML 文档",
            ):
                return True
            self._mark_motion_graphic_build_failed(
                main_window, request_id, "HTML 生成没有返回内容",
            )
            return False
        from .ai.html_generation_protocol import HtmlGenerationConstraints
        from .ai.service import TeachingAnimationAIService
        from .core.html_layer_commands import build_html_layer_from_pipeline
        from .core.html_page_deck import detect_page_count, parse_model_page_timing, timing_metadata
        from .core.model import TeachingSceneDocument

        canvas = self._brief_canvas_kwargs(plan)
        seconds = float(plan.get("seconds", 10.0) or 10.0)
        fps = float(plan.get("fps", 25.0) or 25.0)
        duration_frames = max(1, round(seconds * fps))
        base = TeachingSceneDocument.create_default(
            name="Motion Graphic HTML Deck",
            width=canvas["width"], height=canvas["height"],
            fps_num=max(1, round(fps)), duration_frames=duration_frames,
        )
        service = TeachingAnimationAIService(base)
        outcome = service.generate_html(
            _StaticHtmlClient(raw_html),
            constraints=HtmlGenerationConstraints(
                # This is a playback reference, not a page-count/layout rule.
                max_duration_seconds=max(1.0, seconds),
                fps=fps,
                viewport_width=float(canvas["width"]),
                viewport_height=float(canvas["height"]),
                # Motion Graphic Clip is a browser/render host.  It does not
                # judge the model's composition after generation; revisions
                # happen through the user/model conversation or the editor.
                post_generation_quality_gate=False,
                # Motion Graphic is a browser-like authoring surface: allow
                # the authored page to use public HTTPS CDN styles, fonts,
                # images and external scripts inside the isolated renderer.
                allow_network_resources=True,
                # Motion Graphic is the browser authoring surface: do not
                # silently delete model-authored JS, Canvas/Web Animations,
                # event-driven composition, or newer CSS properties.
                allow_inline_scripts=True,
                allow_event_handlers=True,
                allow_freeform_css=True,
                allow_inline_visual_resources=True,
                allow_gpu_apis=True,
            ),
            approved=False,
            preview_backend=self._html_preview_backend(),
            preview_frame=0,
        )
        plan["raw_html"] = raw_html
        plan["html_outcome"] = outcome
        preview_stage = next(
            (stage for stage in outcome.stages
             if getattr(stage.stage, "value", stage.stage) == "preview"),
            None,
        )
        preview_failed = bool(
            outcome.preview is not None
            and outcome.preview.get("backend_available", False)
            and preview_stage is not None
            and not preview_stage.passed
        )
        if not outcome.sanitized_html.strip() or preview_failed:
            reasons = "; ".join(outcome.blocking_reasons[:4]) or "浏览器预览无法加载该网页"
            if self._retry_motion_graphic_html_build(
                main_window, request_id,
                "浏览器渲染诊断：" + reasons,
            ):
                return True
            self._mark_motion_graphic_build_failed(
                main_window, request_id, f"HTML 浏览器渲染失败：{reasons}",
            )
            return False

        # A revision that dropped pages is a regeneration in disguise; send it
        # back once with the exact diagnosis before staging it.
        edit_context = str(plan.get("html_edit_context", "") or "")
        if edit_context:
            shrink = self._motion_graphic_page_shrink_feedback(
                edit_context, outcome.sanitized_html,
            )
            if shrink and self._retry_motion_graphic_html_build(
                main_window, request_id, shrink,
            ):
                return True

        # Browser validation proves that a page paints but cannot distinguish
        # an authored explainer from an almost-empty title card.  Run a local,
        # explainable completeness baseline before staging.  It only requests
        # a repair for clearly sparse output; everything else remains an
        # advisory score on the confirmation card, not a taste-based blocker.
        try:
            from .ai.html_design_quality import evaluate_html_design

            html_design_quality = evaluate_html_design(
                outcome.sanitized_html,
                viewport_width=float(canvas["width"]),
                viewport_height=float(canvas["height"]),
            )
            plan["html_design_quality"] = html_design_quality.to_dict()
            plan["html_design_warnings"] = list(html_design_quality.warnings)
            if html_design_quality.needs_repair and self._retry_motion_graphic_html_build(
                main_window,
                request_id,
                html_design_quality.repair_feedback(),
                reason="本地设计完整度检查",
            ):
                return True
        except Exception:  # noqa: BLE001 - quality advice must not hide a valid render
            logger.debug("HTML design completeness check failed", exc_info=True)

        # A healthy-looking page is not necessarily the page the user asked
        # for.  Before a new HTML deck becomes confirmable, enforce the small
        # set of literal or deterministically observable requirements the host
        # can prove: quoted copy, exact/named colors, rich-page requests, and
        # explicitly numbered pages.  Plan-stage feedback is part of the same
        # contract, not merely extra prompt context.
        from .ai.html_edit_autonomy import generation_intent_report

        generation_feedback_notes = self._normalize_feedback_notes(
            plan.get("generation_feedback_memory", ())
        )
        intent_report = generation_intent_report(
            (str(plan.get("user_text", "") or ""), *generation_feedback_notes),
            outcome.sanitized_html,
        )
        plan["html_intent_checks"] = intent_report.to_dict()
        if intent_report.issues:
            feedback = intent_report.repair_feedback()
            if self._retry_motion_graphic_html_build(
                main_window, request_id, feedback, reason="明确需求验收",
            ):
                return True
            # Unlike the advisory design baseline above, an unmet literal
            # user requirement must never be staged after its bounded repair
            # budget is exhausted.
            self._mark_motion_graphic_build_failed(
                main_window,
                request_id,
                f"HTML 没有满足明确需求：{'；'.join(intent_report.issues)}",
            )
            return False

        # Build an immutable staged layer without applying it to the project.
        # ``build_html_layer_from_pipeline`` intentionally requires a writable
        # outcome, so create an internal approved copy only for this detached
        # preview object; the real timeline write still happens in confirm.
        staged_outcome = replace(outcome, approved=True, writable=True)
        page_count = detect_page_count(outcome.sanitized_html)
        # The model owns the rhythm: honour its optional per-page duration
        # declaration (meta topos-page-durations) scaled to the requested
        # clip length; an equal split with hard cuts is only the fallback.
        page_timing = parse_model_page_timing(
            raw_html, page_count=page_count, total_seconds=seconds,
        )
        layer = build_html_layer_from_pipeline(
            staged_outcome,
            layer_id=f"html_{uuid.uuid4().hex}",
            authored_source=raw_html,
            viewport_width=canvas["width"], viewport_height=canvas["height"],
            duration_frames=duration_frames,
            fps_num=max(1, round(fps)), fps_den=1,
            sanitizer_version="html-sanitizer-v1",
            metadata={
                **timing_metadata(page_timing),
                # Motion Graphic Clip is intentionally a freeform browser
                # surface: authored font-family choices must survive capture.
                "font_lock": False,
                "render_profile": "motion_graphic_freeform",
                "network_access": True,
                "allow_inline_scripts": True,
                "allow_event_handlers": True,
                "allow_freeform_css": True,
                "allow_inline_visual_resources": True,
                "allow_gpu_apis": True,
                **(
                    {self._HTML_EDIT_FEEDBACK_MEMORY_KEY: list(generation_feedback_notes)}
                    if generation_feedback_notes else {}
                ),
            },
        )
        from .core.pages import sync_pages_with_html_layer
        scene = sync_pages_with_html_layer(replace(base, elements=(), html_layer=layer))
        plan["scene"] = scene
        # Run the browser validation used by confirmation before showing the
        # approval card.  This is a renderer availability/load check only; it
        # is deliberately not a design-quality gate.
        # Keep lightweight/unit test harnesses that deliberately run without
        # a browser backend focused on staging semantics.  Production runs do
        # this gate before the approval card; confirmation still repeats it.
        import os as _os
        if _os.environ.get("TOPOS_TEST_MODE") != "1":
            # AUD-28005: this fallback only runs when the controller has no
            # background postprocess channel (lightweight hosts).  It used to
            # execute the design-size browser capture on the UI thread and
            # freeze the app until every page probe finished; hand it to a
            # QThread instead and present the card from the queued completion.
            def _staging_validated(_rendered: Any) -> None:
                if self._pending_motion_graphic_requests.get(str(request_id)) is not plan:
                    return  # cancelled while the browser gate ran
                plan["browser_validated"] = True
                self._present_staged_motion_graphic_html(
                    main_window, request_id, plan, layer, scene, canvas, seconds,
                )

            def _staging_failed(message: str) -> None:
                if self._pending_motion_graphic_requests.get(str(request_id)) is not plan:
                    return  # cancelled while the browser gate ran
                plan["scene"] = None
                if self._retry_motion_graphic_html_build(
                    main_window, request_id, f"真实浏览器验证诊断：{message}",
                ):
                    return
                self._mark_motion_graphic_build_failed(
                    main_window, request_id, f"HTML 首帧验证失败：{message}",
                )

            if self._start_browser_validation_job(
                f"stage:{request_id}", scene,
                _staging_validated, _staging_failed,
            ):
                self._set_run_status(
                    main_window, "正在后台用真实浏览器验证网页",
                    detail="逐页检查可见性，期间可继续操作…", state="waiting",
                )
                return True
            # Qt-less host: no event loop to block, keep the synchronous gate.
            try:
                self._set_run_status(
                    main_window, "正在用真实浏览器验证网页",
                    detail="逐页检查可见性…",
                )
                rendered, validation_frame = self._validate_motion_graphic_scene(scene)
                if not rendered.data or rendered.width <= 0 or rendered.height <= 0:
                    raise RuntimeError(f"渲染验证帧 {validation_frame} 为空")
                # The HTML document owns its visual timing.  An authored
                # intro/fade may intentionally be transparent at frame 0;
                # validate_html_scene already proves that every page paints a
                # stable frame in its own time window, so do not turn a valid
                # browser render into a failure merely because the first
                # usable probe is later than frame 0.
            except Exception as exc:  # noqa: BLE001 - show the real browser diagnosis
                logger.debug("Motion Graphic HTML render validation failed", exc_info=True)
                plan["scene"] = None
                if self._retry_motion_graphic_html_build(
                    main_window, request_id, f"真实浏览器验证诊断：{exc}",
                ):
                    return True
                self._mark_motion_graphic_build_failed(
                    main_window, request_id, f"HTML 首帧验证失败：{exc}",
                )
                return False
            plan["browser_validated"] = True
        return self._present_staged_motion_graphic_html(
            main_window, request_id, plan, layer, scene, canvas, seconds,
        )

    def _present_staged_motion_graphic_html(
        self,
        main_window: Any,
        request_id: str,
        plan: dict[str, Any],
        layer: Any,
        scene: Any,
        canvas: Mapping[str, Any],
        seconds: float,
    ) -> bool:
        """Register the staged HTML scene and show its approval card (AUD-28005).

        The continuation shared by the synchronous staging path and the
        queued background browser validation.
        """
        from .scene_store import get_shared_scene_store

        get_shared_scene_store().register(scene)
        plan["html_layer_id"] = layer.layer_id
        self._set_run_status(
            main_window, "网页已就绪",
            detail="请在确认卡片中执行，写入前还会复验", state="waiting",
        )
        creative_brief = plan.get("creative_brief")
        concept = (
            str(creative_brief.get("concept", "") or "").strip()
            if isinstance(creative_brief, dict) else ""
        )
        direction_note = f"；本次自主创意方向：{concept[:80]}" if concept else ""
        self._append_assistant_message(
            main_window,
            f"已生成网页级 Motion Graphic 演示（HTML/CSS，多页自动切换），"
            f"画布 {canvas['width']}×{canvas['height']}{direction_note}；"
            "首帧与每一页都已通过真实浏览器可见性检查，请在确认卡片中执行。",
        )
        self._show_motion_graphic_proposal_card(
            main_window, request_id,
            variant=int(plan.get("variant", 0)),
            seconds=seconds, frame=int(plan.get("frame", 0)),
            graphics_track=None, fallback=False,
        )
        return True

    @staticmethod
    def _motion_graphic_html_plan_snapshot(plan: dict[str, Any]) -> dict[str, Any]:
        """Copy only immutable inputs needed by the browser post-process worker."""
        return {
            "canvas": dict(TeachingAnimationModule._brief_canvas_kwargs(plan)),
            "seconds": float(plan.get("seconds", 10.0) or 10.0),
            "fps": float(plan.get("fps", 25.0) or 25.0),
        }

    def _prepare_motion_graphic_html_response(
        self,
        request_id: str,
        response: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Prepare and browser-validate an HTML clip without touching the UI.

        This method is deliberately free of panel, timeline and assistant
        message calls.  It is run by ``_MotionGraphicPostprocessWorker`` and
        returns an immutable-in-practice payload that the main thread applies.
        """
        del request_id
        from .ai.html_llm_generator import strip_markdown_fence
        from .ai.html_generation_protocol import HtmlGenerationConstraints
        from .ai.service import TeachingAnimationAIService
        from .core.html_layer_commands import build_html_layer_from_pipeline
        from .core.html_page_deck import detect_page_count, parse_model_page_timing, timing_metadata
        from .core.model import TeachingSceneDocument

        raw_html = strip_markdown_fence(str(response or ""))
        if not raw_html:
            raise RuntimeError("模型没有返回完整 HTML 文档")
        canvas = dict(snapshot["canvas"])
        seconds = float(snapshot["seconds"])
        fps = float(snapshot["fps"])
        duration_frames = max(1, round(seconds * fps))
        base = TeachingSceneDocument.create_default(
            name="Motion Graphic HTML Deck",
            width=canvas["width"], height=canvas["height"],
            fps_num=max(1, round(fps)), duration_frames=duration_frames,
        )
        service = TeachingAnimationAIService(base)
        outcome = service.generate_html(
            _StaticHtmlClient(raw_html),
            constraints=HtmlGenerationConstraints(
                max_duration_seconds=max(1.0, seconds),
                fps=fps,
                viewport_width=float(canvas["width"]),
                viewport_height=float(canvas["height"]),
                post_generation_quality_gate=False,
                allow_network_resources=True,
                allow_inline_scripts=True,
                allow_event_handlers=True,
                allow_freeform_css=True,
                allow_inline_visual_resources=True,
                allow_gpu_apis=True,
            ),
            approved=False,
            # The real browser validation below is the expensive step.  Do
            # not perform a second preview capture before it.
            preview_backend=None,
            preview_frame=0,
        )
        if not outcome.sanitized_html.strip():
            reasons = "; ".join(outcome.blocking_reasons[:4]) or "网页清洗后为空"
            raise RuntimeError(f"HTML 浏览器渲染前检查失败：{reasons}")

        staged_outcome = replace(outcome, approved=True, writable=True)
        page_count = detect_page_count(outcome.sanitized_html)
        # Same rhythm ownership as the foreground build path: the model's
        # optional duration declaration wins over an equal split.
        page_timing = parse_model_page_timing(
            raw_html, page_count=page_count, total_seconds=seconds,
        )
        layer = build_html_layer_from_pipeline(
            staged_outcome,
            layer_id=f"html_{uuid.uuid4().hex}",
            authored_source=raw_html,
            viewport_width=canvas["width"], viewport_height=canvas["height"],
            duration_frames=duration_frames,
            fps_num=max(1, round(fps)), fps_den=1,
            sanitizer_version="html-sanitizer-v1",
            metadata={
                **timing_metadata(page_timing),
                "font_lock": False,
                "render_profile": "motion_graphic_freeform",
                "network_access": True,
                "allow_inline_scripts": True,
                "allow_event_handlers": True,
                "allow_freeform_css": True,
                "allow_inline_visual_resources": True,
                "allow_gpu_apis": True,
            },
        )
        from .core.pages import sync_pages_with_html_layer
        scene = sync_pages_with_html_layer(replace(base, elements=(), html_layer=layer))
        rendered, validation_frame = self._validate_motion_graphic_scene(scene)
        if not rendered.data or rendered.width <= 0 or rendered.height <= 0:
            raise RuntimeError(f"HTML 首帧验证为空（候选帧 {validation_frame}）")
        return {
            "raw_html": raw_html,
            "outcome": outcome,
            "scene": scene,
            "layer_id": layer.layer_id,
            "canvas": canvas,
            "seconds": seconds,
            "frame": int(snapshot.get("frame", 0) or 0),
        }

    def _finish_background_motion_graphic_html(
        self, main_window: Any, request_id: str, payload: object,
    ) -> None:
        """Apply a completed browser payload on the Qt main thread."""
        plan = self._pending_motion_graphic_requests.get(str(request_id))
        if plan is None or not isinstance(payload, dict):
            return
        scene = payload.get("scene")
        layer_id = str(payload.get("layer_id", ""))
        raw_html = str(payload.get("raw_html", ""))
        # A revision that collapsed the page deck is rejected here (the worker
        # only sees the response, not the revision context it must preserve).
        edit_context = str(plan.get("html_edit_context", "") or "")
        if edit_context:
            shrink = self._motion_graphic_page_shrink_feedback(edit_context, raw_html)
            if shrink:
                plan["scene"] = None
                plan["raw_html"] = ""
                if self._retry_motion_graphic_html_build(
                    main_window, request_id, shrink,
                ):
                    return
                self._mark_motion_graphic_build_failed(
                    main_window, request_id, f"修改丢失页面：{shrink}",
                )
                return
        if scene is None or not layer_id or not raw_html:
            self._fail_background_motion_graphic_html(
                main_window, request_id, "后台浏览器没有返回完整场景",
            )
            return
        from .scene_store import get_shared_scene_store
        get_shared_scene_store().register(scene)
        plan["raw_html"] = raw_html
        plan["html_outcome"] = payload.get("outcome")
        plan["scene"] = scene
        plan["html_layer_id"] = layer_id
        plan["browser_validated"] = True
        canvas = payload.get("canvas", {})
        seconds = float(payload.get("seconds", plan.get("seconds", 10.0)))
        creative_brief = plan.get("creative_brief")
        concept = (
            str(creative_brief.get("concept", "") or "").strip()
            if isinstance(creative_brief, dict) else ""
        )
        direction_note = f"；本次自主创意方向：{concept[:80]}" if concept else ""
        self._append_assistant_message(
            main_window,
            f"后台浏览器预览已完成：网页级 Motion Graphic 演示（HTML/CSS，多页自动切换），"
            f"画布 {canvas.get('width', scene.width)}×{canvas.get('height', scene.height)}"
            f"{direction_note}；可以在确认卡片中执行。",
        )
        self._show_motion_graphic_proposal_card(
            main_window, request_id,
            variant=int(plan.get("variant", 0)),
            seconds=seconds, frame=int(plan.get("frame", 0)),
            graphics_track=None, fallback=False,
        )
        controller = getattr(main_window, "_ai_edit_controller", None)
        finish = getattr(controller, "finish_motion_graphic_html_postprocess", None)
        if callable(finish):
            finish(request_id, raw_html, success=True)

    def _fail_background_motion_graphic_html(
        self, main_window: Any, request_id: str, error: str,
    ) -> None:
        """Handle a worker failure without blocking or mutating the timeline."""
        self._set_run_status(
            main_window, "浏览器验证失败", detail=str(error)[:80], state="failed",
        )
        if self._retry_motion_graphic_html_build(
            main_window, request_id, f"后台浏览器渲染诊断：{error}",
        ):
            return
        self._mark_motion_graphic_build_failed(
            main_window, request_id, f"HTML 首帧验证失败：{error}",
        )
        controller = getattr(main_window, "_ai_edit_controller", None)
        finish = getattr(controller, "finish_motion_graphic_html_postprocess", None)
        if callable(finish):
            finish(request_id, "", success=False)

    def _confirm_html_generate_request(
        self,
        main_window: Any,
        request_id: str,
        completion: Callable[[bool], None] | None = None,
    ) -> bool | None:
        """Approve the exact reviewed HTML document and update the scene store.

        AUD-28005: in production (``TOPOS_TEST_MODE`` unset) the browser
        re-validation runs on a QThread and this method returns ``None``
        while the gate is in flight; the queued completion applies — or
        refuses — the write and finally invokes ``completion`` exactly once
        with the outcome.  Synchronous callers (tests / Qt-less hosts) get
        the plain bool as before, and ``completion`` is invoked immediately
        in that case too, so phased callers keep a single outcome path.
        """
        from .scene_store import get_shared_scene_store

        def _deliver(outcome_value: bool) -> bool:
            if completion is not None:
                completion(outcome_value)
            return outcome_value

        request_id = str(request_id)
        plan = self._pending_html_generate_requests.get(request_id)
        if plan is None or not plan.get("raw_html"):
            return _deliver(False)
        self._set_run_status(
            main_window, "正在写入并验证场景",
            detail="清洗复验 + 真实浏览器验证…",
        )
        store = get_shared_scene_store()
        current = store.get(str(plan["scene_id"]))
        if current is None or store.revision(current.scene_id) != plan["scene_revision"]:
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.stale_scene_confirm",
                        "Confirmation failed: the target scene changed after the preview. "
                        "Regenerate the HTML motion."),
                error_code="html_generation_stale_scene",
            )
            return _deliver(False)
        service = self._get_html_service_for(current.scene_id)
        if service is None:
            return _deliver(False)
        # Re-run the same raw document through the approval gate.  No second
        # model call means the reviewed preview and committed document match.
        outcome = service.generate_html(
            _StaticHtmlClient(plan["raw_html"]),
            constraints=plan["constraints"],
            known_asset_ids=plan["known_asset_ids"],
            approved=True,
            preview_backend=self._html_preview_backend(),
            preview_frame=0,
        )
        if not outcome.writable:
            plan["outcome"] = outcome
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.render_blocked_list",
                        "The HTML motion cannot be rendered by the browser; the scene was not "
                        "modified: {reasons}",
                        reasons="; ".join(outcome.blocking_reasons[:6])),
                error_code="html_generation_quality_blocked",
            )
            return _deliver(False)
        # Build and validate the candidate before applying the undoable command.
        # The old direct-HTML route only checked static quality here, so a
        # browser-empty document could be written successfully and fail later
        # during timeline/export rendering.
        from .core.html_layer_commands import build_html_layer_from_pipeline
        layer_metadata = self._html_edit_layer_metadata(current, plan)
        candidate_layer = build_html_layer_from_pipeline(
            outcome,
            layer_id=f"html_{uuid.uuid4().hex}",
            authored_source=outcome.authored_source or plan["raw_html"],
            viewport_width=current.width,
            viewport_height=current.height,
            duration_frames=current.duration_frames,
            fps_num=current.fps_num,
            fps_den=current.fps_den,
            sanitizer_version="html-sanitizer-v1",
            metadata=layer_metadata,
        )
        candidate_scene = replace(current, html_layer=candidate_layer)
        import os as _os
        if _os.environ.get("TOPOS_TEST_MODE") != "1":
            # AUD-28005: the design-size browser gate (multi-frame candidates
            # + per-page probes + the contrast-background pass) must not run
            # inside this Qt slot — it froze the main thread for the whole
            # capture.  Hand it to a QThread; the queued completion repeats
            # the revision guard (the scene may change while the browser
            # works) and then applies the undoable write.
            def _validation_succeeded(_rendered: Any) -> None:
                if self._pending_html_generate_requests.get(request_id) is not plan:
                    return  # cancelled while the browser gate ran
                try:
                    self._apply_confirmed_html_generate(
                        main_window, request_id, plan, outcome,
                        candidate_layer, layer_metadata, completion,
                    )
                except Exception:  # noqa: BLE001 - queued slot must still report an outcome
                    logger.exception(
                        "Confirmed HTML write failed after background validation",
                    )
                    if completion is not None:
                        completion(False)

            def _validation_failed(message: str) -> None:
                if self._pending_html_generate_requests.get(request_id) is not plan:
                    return  # cancelled while the browser gate ran
                self._html_generate_confirm_failed(
                    main_window, plan, outcome, message, completion,
                )

            started = self._start_browser_validation_job(
                f"confirm:{request_id}", candidate_scene,
                _validation_succeeded, _validation_failed,
            )
            if started:
                self._set_run_status(
                    main_window, "正在后台复验浏览器渲染",
                    detail="逐页检查可见性，期间可继续操作…", state="waiting",
                )
                return None
            # Qt-less host: no event loop can be blocked, keep the old
            # synchronous gate so the write still cannot happen unverified.
            try:
                self._validate_motion_graphic_scene(candidate_scene)
            except Exception as exc:  # noqa: BLE001 - preserve the render diagnosis
                self._html_generate_confirm_failed(
                    main_window, plan, outcome, str(exc), None,
                )
                return _deliver(False)
        return _deliver(self._apply_confirmed_html_generate(
            main_window, request_id, plan, outcome,
            candidate_layer, layer_metadata, None,
        ))

    def _apply_confirmed_html_generate(
        self,
        main_window: Any,
        request_id: str,
        plan: dict[str, Any],
        outcome: Any,
        candidate_layer: Any,
        layer_metadata: Mapping[str, Any],
        completion: Callable[[bool], None] | None,
    ) -> bool:
        """Apply the validated HTML layer (confirm continuation, AUD-28005).

        Repeats the revision guard: when the browser gate ran in the
        background, the target scene may have changed while the capture was
        in flight, and a stale write must be refused exactly like the
        synchronous one (AUD-28002 semantics).
        """
        from .scene_store import get_shared_scene_store

        def _finish(applied: bool) -> bool:
            if completion is not None:
                completion(applied)
            return applied

        store = get_shared_scene_store()
        current = store.get(str(plan["scene_id"]))
        if current is None or store.revision(current.scene_id) != plan["scene_revision"]:
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.stale_scene_confirm",
                        "Confirmation failed: the target scene changed after the preview. "
                        "Regenerate the HTML motion."),
                error_code="html_generation_stale_scene",
            )
            return _finish(False)
        service = self._get_html_service_for(current.scene_id)
        if service is None:
            return _finish(False)
        layer = service.apply_html_layer(
            outcome,
            layer_id=candidate_layer.layer_id,
            viewport_width=current.width,
            viewport_height=current.height,
            duration_frames=current.duration_frames,
            fps_num=current.fps_num,
            fps_den=current.fps_den,
            sanitizer_version="html-sanitizer-v1",
            metadata=layer_metadata,
        )
        updated = service.scene
        published_template = None
        if self._is_motion_graphic_template_request(plan.get("user_text", "")):
            # An explicit “模板/生成器” request publishes the verified result
            # to the Effects palette.  Ordinary AI-generated clips remain
            # one-off scene instances and do not clutter the palette.
            try:
                published_template = service.promote_html_template(
                    template_id=f"ai_mg_{uuid.uuid4().hex[:10]}",
                    display_name=f"AI · {updated.name or 'Motion Graphic'}",
                    description=I18N.tr(
                        "motion_graphic.ai.published_template_description",
                        "Reusable AI-generated Motion Graphic generator verified in the browser"),
                    publish_to_effects=True,
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                logger.warning("AI Motion Graphic template publish skipped: %s", exc)
        # When the scene is open in the native editor, replay the same
        # immutable command through its command stack so Ctrl+Z restores the
        # previous HTML layer.  The service call above remains the feature
        # service's scene update; this host step keeps UI undo and the shared
        # store in the same document identity.
        registry = getattr(self, "_editor_registry", None)
        editor_window = registry.get(updated.scene_id) if registry is not None else None
        editor_controller = getattr(editor_window, "editor_controller", None)
        if editor_controller is not None and editor_controller.scene is current:
            from .core.html_layer_commands import SetHtmlLayerCommand

            editor_controller.execute(SetHtmlLayerCommand(layer))
            updated = editor_controller.scene
        store.register(updated)
        self._pending_html_generate_requests.pop(request_id, None)
        # Remember the edited clip so follow-up revisions without an explicit
        # target keep modifying it.
        self._last_motion_graphic_target = {
            "asset_id": str(
                plan.get("target_asset_id", "")
                or f"teaching_animation:{updated.scene_id}"
            ),
            "scene_id": str(updated.scene_id),
            "display_name": str(
                plan.get("target_display_name", "") or "Motion Graphic Clip"
            ),
        }
        clear = getattr(
            getattr(main_window, "ai_edit_assistant_panel", None),
            "clear_motion_graphic_proposal",
            None,
        )
        if callable(clear):
            clear(request_id)
        if hasattr(main_window, "edit_project_dirty"):
            main_window.edit_project_dirty = True
        self._set_run_status(main_window, "HTML 动效已写入场景", state="succeeded")
        self._append_assistant_message(
            main_window,
            f"HTML 动效已写入 scene_id={updated.scene_id}，"
            f"layer_id={layer.layer_id}；可通过撤销恢复原场景。"
            + (
                f" 已发布到 Effects → MG 生成器（{published_template.display_name}，"
                f"{len(published_template.params_schema)} 个可调参数）。"
                if published_template is not None else ""
            ),
        )
        return _finish(True)

    def _html_generate_confirm_failed(
        self,
        main_window: Any,
        plan: dict[str, Any],
        outcome: Any,
        message: str,
        completion: Callable[[bool], None] | None,
    ) -> None:
        """Report a failed confirm-path browser gate (AUD-28005 continuation)."""
        plan["outcome"] = outcome
        self._append_assistant_message(
            main_window,
            I18N.tr(
                "motion_graphic.ai.render_blocked_capture",
                "HTML motion not written: the browser verification capture is empty "
                "or unavailable. Diagnostics: {error}", error=message,
            ),
            error_code="html_generation_render_blocked",
        )
        if completion is not None:
            completion(False)

    def _complete_direct_html_edit_apply(
        self, main_window: Any, request_id: str, plan: dict[str, Any],
    ) -> None:
        """Success continuation of the direct-apply HTML edit flow (AUD-28005).

        Previously inlined after the synchronous ``_confirm_html_generate_request``
        call; now shared by the synchronous result and the queued background
        validation completion.
        """
        steps = tuple(plan.get("edit_plan_steps") or ())
        index = int(plan.get("edit_step_index", 0) or 0)
        audit_record = dict(plan.get("step_audit") or {})
        if audit_record:
            history = list(plan.get("edit_history") or ())
            history.append({
                "step": index + 1 if steps else 1,
                "summary": (
                    str(steps[index].get("summary", "") or "")
                    if steps else "直接修改"
                ),
                "audit": audit_record,
            })
            plan["edit_history"] = tuple(history)
            self._append_assistant_message(
                main_window,
                "本步差异：" + str(audit_record.get("summary", "已更新网页。")),
            )
        self._complete_html_edit_run(main_window, str(request_id), success=True)
        if steps and index < len(steps) - 1:
            self._continue_html_edit_steps(main_window, str(request_id), plan)
        elif steps:
            history = tuple(plan.get("edit_history") or ())
            history_line = "；".join(
                f"第 {item.get('step', '?')} 步："
                f"{dict(item.get('audit') or {}).get('summary', '已更新')}"
                for item in history
            )
            self._append_assistant_message(
                main_window,
                f"分步修改全部完成（共 {len(steps)} 步），已应用到当前片段；"
                + (f"变更记录：{history_line}。" if history_line else "")
                + "如有需要可以继续反馈。",
            )
            self._set_run_status(
                main_window, "修改已完成",
                detail=f"已应用到「{plan.get('target_display_name', '')}」",
                state="succeeded",
            )
        else:
            self._set_run_status(
                main_window, "修改已完成",
                detail=f"已应用到「{plan.get('target_display_name', '')}」",
                state="succeeded",
            )

    def _fail_direct_html_edit_apply(
        self, main_window: Any, request_id: str, plan: dict[str, Any],
    ) -> None:
        """Failure continuation of the direct-apply HTML edit flow (AUD-28005)."""
        feedback = (
            "真实浏览器逐页验证没有通过。请保留当前全部页面和用户要求，"
            "修复空白、不可见、超出视口或无法加载的内容后输出完整 HTML。"
        )
        if self._retry_html_edit_generate(main_window, request_id, feedback):
            return
        self._pending_html_generate_requests.pop(str(request_id), None)
        scope = self._html_edit_failure_scope(plan)
        self._set_run_status(
            main_window, "浏览器验证失败",
            detail="本步修改未写入；" + scope, state="failed",
        )
        self._complete_html_edit_run(main_window, str(request_id), success=False)

    @staticmethod
    def _extract_quoted_value(text: str) -> str | None:
        matches = re.findall(r"[“「\"']([^”」\"']+)[”」\"']", str(text or ""))
        return matches[-1].strip() if matches else None

    @staticmethod
    def _color_from_text(text: str) -> str | None:
        value = str(text or "").lower()
        hex_match = re.search(r"#[0-9a-f]{6}\b", value)
        if hex_match:
            return hex_match.group(0)
        colors = {
            "红色": "#ef4444", "红": "#ef4444", "蓝色": "#3b82f6", "蓝": "#3b82f6",
            "绿色": "#10b981", "绿": "#10b981", "紫色": "#8b5cf6", "紫": "#8b5cf6",
            "黄色": "#f59e0b", "黄": "#f59e0b", "白色": "#f9fafb", "白": "#f9fafb",
            "黑色": "#111827", "黑": "#111827", "orange": "#f97316",
            "red": "#ef4444", "blue": "#3b82f6", "green": "#10b981",
        }
        for name, color in colors.items():
            if name in value:
                return color
        return None

    @staticmethod
    def _find_scene_element(scene: Any, text: str) -> Any | None:
        value = str(text or "")
        explicit = re.search(r"(?:element|元素)[\s:_-]*([a-zA-Z0-9_-]+)", value, re.IGNORECASE)
        if explicit:
            element = scene.element_by_id(explicit.group(1))
            if element is not None:
                return element
        lowered = value.lower()
        elements = tuple(scene.elements)
        text_elements = tuple(element for element in elements if element.type == "text")
        if "副标题" in value or "subtitle" in lowered:
            return next(
                (element for element in text_elements if "subtitle" in element.element_id.lower()),
                text_elements[1] if len(text_elements) > 1 else None,
            )
        if "标题" in value or "title" in lowered:
            return next(
                (element for element in text_elements if "title" in element.element_id.lower()),
                text_elements[0] if text_elements else None,
            )
        quoted = re.findall(r"[“「\"']([^”」\"']+)[”」\"']", value)
        for candidate in reversed(quoted[:-1] if len(quoted) > 1 else quoted):
            for element in text_elements:
                if str(element.content) == candidate:
                    return element
        return text_elements[0] if len(text_elements) == 1 else None

    def _resolve_scene_for_edit(self, main_window: Any, text: str) -> Any | None:
        from .scene_store import get_shared_scene_store
        from .host_adapters import motion_graphic_scene_id

        store = get_shared_scene_store()
        explicit = re.search(r"scene[_-][a-zA-Z0-9_-]+", str(text or ""), re.IGNORECASE)
        if explicit:
            scene = store.get(explicit.group(0))
            if scene is not None:
                return scene

        registry = getattr(self, "_editor_registry", None)
        windows = getattr(registry, "_windows", {}) if registry is not None else {}
        for scene_id, window in windows.items():
            controller = getattr(window, "editor_controller", None)
            scene = getattr(controller, "scene", None)
            if scene is not None:
                return scene

        panel = getattr(main_window, "timeline_panel", None)
        view = panel.get_timeline_view() if panel is not None else None
        timeline = getattr(view, "_timeline", None) if view is not None else None
        if timeline is not None:
            selected_ids = tuple(
                getattr(getattr(view, "_selection", None), "selected_clip_ids", ()) or ()
            )
            candidates = []
            for track in timeline.video_tracks:
                for clip in track.clips:
                    if selected_ids and clip.clip_id not in selected_ids:
                        continue
                    asset_id = str(getattr(clip, "asset_id", ""))
                    scene_id = motion_graphic_scene_id(asset_id)
                    if scene_id is not None:
                        candidates.append(scene_id)
            if not candidates and hasattr(panel, "get_current_frame"):
                frame = int(panel.get_current_frame())
                for track in timeline.video_tracks:
                    clip = track.get_clip_at(frame)
                    scene_id = motion_graphic_scene_id(
                        getattr(clip, "asset_id", "") if clip is not None else ""
                    )
                    if scene_id is not None:
                        candidates.append(scene_id)
            if candidates:
                scene = store.get(candidates[0])
                if scene is not None:
                    return scene

        scenes = store.scenes()
        return scenes[0] if len(scenes) == 1 else None

    def _build_scene_edit_plan(self, main_window: Any, text: str) -> dict[str, Any] | None:
        scene = self._resolve_scene_for_edit(main_window, text)
        if scene is None:
            return None
        element = self._find_scene_element(scene, text)
        updates: dict[str, Any] = {}
        quoted = self._extract_quoted_value(text)
        if quoted is not None and element is not None:
            updates["content"] = quoted
        color = self._color_from_text(text)
        if color is not None and element is not None:
            style = dict(element.style)
            style["color"] = color
            updates["style"] = style
        font_size = re.search(r"(?:字号|字体大小|font size)\s*(?:改为|改成|为|to)?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if font_size is not None and element is not None:
            style = dict(updates.get("style", element.style))
            style["font_size"] = float(font_size.group(1))
            updates["style"] = style
        if not updates or element is None:
            return None

        from .ai.commands import TeachingAICommand, TeachingAICommandRegistry
        from .scene_store import get_shared_scene_store

        command = TeachingAICommand(
            "update_element",
            {"element_id": element.element_id, "updates": updates},
        )
        proposal = TeachingAICommandRegistry().dry_run(scene, (command,))
        return {
            "scene_id": scene.scene_id,
            # AUD-28002: record the store revision the proposal was derived
            # from so confirmation can refuse stale proposals (the command
            # contract replaces the whole style mapping — applying a proposal
            # merged against an older element silently rolls back edits the
            # user made while the card was visible).
            "scene_revision": get_shared_scene_store().revision(scene.scene_id),
            "scene": scene,
            "proposal": proposal,
            "element_id": element.element_id,
            "updates": updates,
            "changed_summary": ", ".join(sorted(updates)),
        }

    def _handle_model_scene_edit_response(
        self, main_window: Any, text: str, response: str,
    ) -> bool:
        """Validate a model-produced JSON command plan and show it for approval."""
        raw = str(response or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                return False
            try:
                payload = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return False
        if not isinstance(payload, dict):
            return False
        scene_id = str(payload.get("scene_id", "") or "")
        commands_raw = payload.get("commands")
        if not scene_id or not isinstance(commands_raw, list) or not commands_raw:
            return False

        from .ai.commands import TeachingAICommand
        from .ai.command_catalog import EXTENDED_COMMAND_NAMES, ExtendedCommandRegistry
        from .scene_store import get_shared_scene_store

        scene = get_shared_scene_store().get(scene_id)
        if scene is None:
            return False
        accepted_names = {"add_element", "update_element", "reorder_element", "set_animation"} | set(EXTENDED_COMMAND_NAMES)
        commands = []
        for item in commands_raw:
            if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
                return False
            name = str(item.get("name", ""))
            if name not in accepted_names:
                return False
            arguments = dict(item["arguments"])
            # The model may return only the changed style key.  The command
            # contract replaces the style mapping, so merge against the live
            # element first to avoid silently dropping unrelated properties.
            if name == "update_element" and isinstance(arguments.get("updates"), dict):
                element = scene.element_by_id(str(arguments.get("element_id", "")))
                updates = dict(arguments["updates"])
                if element is not None and isinstance(updates.get("style"), dict):
                    merged_style = dict(element.style)
                    merged_style.update(updates["style"])
                    updates["style"] = merged_style
                arguments["updates"] = updates
            commands.append(TeachingAICommand(name, arguments))
        try:
            proposal = ExtendedCommandRegistry().dry_run(scene, tuple(commands))
        except Exception:
            logger.debug("Model Teaching Animation command plan rejected", exc_info=True)
            return False
        if proposal.is_empty:
            return False

        request_id = f"scene-edit-{uuid.uuid4().hex}"
        element_ids = [
            str(command.arguments.get("element_id", ""))
            for command in commands
            if command.arguments.get("element_id")
        ]
        self._pending_scene_edit_requests[request_id] = {
            "scene_id": scene_id,
            # AUD-28002: revision captured when the proposal was parsed and
            # style dicts were merged against the live element (see the
            # update_element merge above); confirmation compares it below.
            "scene_revision": get_shared_scene_store().revision(scene_id),
            "scene": scene,
            "proposal": proposal,
            "element_id": ", ".join(element_ids) or "多个场景属性",
            "updates": {"commands": len(commands)},
            "changed_summary": str(payload.get("summary", "模型生成的场景命令")),
        }
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        show_plan = getattr(panel, "set_motion_graphic_proposal", None)
        if not callable(show_plan):
            self._pending_scene_edit_requests.pop(request_id, None)
            return False
        show_plan(
            request_id,
            "AI 生成的 Motion Graphic 修改计划",
            f"scene_id：{scene_id}\n"
            f"目标元素：{', '.join(element_ids) or '多个场景属性'}\n"
            f"变更摘要：{payload.get('summary', '模型生成的场景命令')}\n"
            "确认前不会修改场景；确认后执行命令栈并验证首帧。",
        )
        return True

    @staticmethod
    def _requested_duration_seconds(text: str) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|seconds?|s)", str(text or "").lower())
        if match is None:
            return 15.0
        return max(1.0, min(300.0, float(match.group(1))))

    @staticmethod
    def _keyframe_track(property_name: str, values: tuple[tuple[int, float], ...], *, min_val: float, max_val: float) -> Any:
        from src.shared.keyframe import Keyframe, KeyframeTrack

        track = KeyframeTrack(property_name, min_val=min_val, max_val=max_val)
        for frame, value in values:
            track.add_keyframe(Keyframe(frame, value))
        return track

    # ------------------------------------------------------------------ #
    # LLM 驱动的场景构建（取代硬编码 _build_ai_agent_demo_scene）
    # ------------------------------------------------------------------ #

    _MOTION_GRAPHIC_BUILD_SYSTEM_PROMPT = (
        "你是 Topos Color 的 Motion Graphic 动画设计师。请把用户的创意当成一张"
        "可以自由发挥的设计画布：构图、层级、节奏、色彩、字体、装饰、转场和动画"
        "全部由你自行判断。\n\n"
        "你必须严格只输出一个 JSON 对象，不要 Markdown，不要解释文字。"
        "画布尺寸由 user message 给出，左上角是 (0,0)。唯一的视觉硬约束是保持"
        "给定的画幅和分辨率；其余设计选择都交给你。允许元素叠放、出画、留白、"
        "非对称构图、渐变和装饰层都可以自由使用，所有视觉选择由你决定。\n\n"
        "JSON 格式：\n"
        "{\n"
        '  "name": "场景名称",\n'
        '  "background_color": "#rrggbb（可选；也可以用 background shape 自由设计）",\n'
        '  "commands": [\n'
        '    {"name": "add_element", "arguments": {\n'
        '      "element": {"element_id": "唯一 id",\n'
        '                  "type": "text | shape | svg | chart | draw | formula",\n'
        '                  "z_index": 整数（任意层级顺序）,\n'
        '                  "transform": {"x": 数, "y": 数, "width": 数, "height": 数, "rotation": 数（可选）},\n'
        '                  "style": {"color": "#rrggbb", "fill": "#rrggbb", "font_size": 数, "font_family": "字体名", "font_weight": "normal|bold", "letter_spacing": 数, "corner_radius": 数, "stroke": "#rrggbb", "stroke_width": 数, "fill_opacity": 数, "effects": [...]},\n'
        '                  "content": "text 文案，或 shape/svg/chart/draw 对应的结构化内容"}\n'
        '    }},\n'
        '    {"name": "set_animation", "arguments": {\n'
        '      "element_id": "对应的 element_id",\n'
        '      "property": "opacity | x | y | scale_x | scale_y | rotation",\n'
        '      "min_val": 数字, "max_val": 数字,\n'
        '      "keyframes": [{"frame": 整数, "value": 数字}, ...]\n'
        '    }}\n'
        '  ]\n'
        "}\n\n"
        "技术契约（只为保证场景能渲染和播放）：\n"
        "1. commands 必须是非空数组；element_id 必须唯一；type 必须使用上述受支持的类型。\n"
        "2. 画布宽高必须使用 user message 给出的数值；transform 和关键帧必须是有限数字。\n"
        "3. keyframe.frame 是绝对帧号，必须满足 0 <= frame < 总帧数；关键帧值要符合所选属性的含义。\n"
        "4. 初始生成从空场景开始，只使用真正需要的 add_element/set_animation 等命令。\n"
        "5. 可以使用任意数量的元素、任意 z_index、叠层和重叠；可以只做少量关键动画，"
        "也可以做复杂节奏。请优先追求有设计感、叙事性和视觉完成度。\n"
        "6. 不要引用未提供的外部素材或付费资源；内嵌 SVG 必须是安全的静态 SVG。"
    )

    # Direct generation uses a model-authored plan review before the HTML
    # authoring pass. The plan is conversational: it gives the user a chance
    # to correct the direction, but is never a host-side template or quality
    # gate.
    _MOTION_GRAPHIC_CREATIVE_BRIEF_SYSTEM_PROMPT = (
        "你是 Topos Color 的 Motion Graphic 创意总监。请先为用户的主题制定一份"
        "专属的视觉创意计划，展示给用户讨论后再交给 HTML/CSS 设计模型执行。你必须真正理解主题，"
        "提炼一个有记忆点的视觉隐喻、叙事角度和动效语言。\n\n"
        "只输出一个 JSON 对象，不要 Markdown、不要解释文字、不要输出 HTML、坐标或"
        "scene commands。格式如下：\n"
        "{\n"
        '  "concept": "一句话概念和核心视觉隐喻",\n'
        '  "visual_grammar": "构图、空间关系、图形语言和页面之间如何变化",\n'
        '  "palette": ["#rrggbb", "#rrggbb", "#rrggbb"],\n'
        '  "typography": "字体层级、字重、字号对比和排版气质",\n'
        '  "motion_language": "节奏、入场、变形、镜头感和页内动画原则",\n'
        '  "subject_motifs": ["与主题直接相关的视觉母题"],\n'
        '  "beats": [\n'
        '    {"label": "段落名", "purpose": "叙事作用",'
        '     "visual": "画面构成和具体视觉动作",'
        '     "copy": "屏幕文案或文案方向"}\n'
        '  ],\n'
        '  "avoid": ["本次设计明确避免的通用套路"]\n'
        "}\n\n"
        "这是一份给用户审核的创作计划，不是宿主模板，也不是最终 HTML；用户可以继续对话修改它。\n"
        "创作规则：\n"
        "1. beats 的数量、结构和节奏完全由你根据主题与时长决定；可以是连续的单一"
        "构图，也可以是任意数量的段落，由你自行决定节奏和空间关系。\n"
        "2. subject_motifs、文案、构图、尺度和空间关系都由你根据主题原创决定。\n"
        "3. visual_grammar 完全由你判断，下一步模型应把它发展成真正原创的视觉作品。\n"
        "4. 只把画幅和时长当作硬约束；不要自行添加品牌、素材、外部资源或技术限制。\n"
        "5. 必须输出可被标准 JSON 解析器直接解析的 JSON：所有字符串中的双引号、"
        "反斜杠和换行都要正确转义；不要在字符串里写未转义的双引号。不要把"
        "<html>、<head>、<body> 这样的尖括号标签写进字符串；如需表达它们，请写成"
        "html-root、head-tag、body-tag 等普通文本。"
    )

    # The default Motion Graphic path is a browser-backed HTML/CSS deck.  The
    # model owns the website: there is deliberately no host-side art direction,
    # visual template, content recipe, or preferred number of pages here.  The
    # small protocol below is an integration contract only (canvas size, page
    # markers, and the capabilities of the deterministic capture bridge).
    _MOTION_GRAPHIC_HTML_BUILD_SYSTEM_PROMPT = (
        "你是 Topos Color Motion Graphic Clip 内置浏览器中的网页设计模型。用户把你当作"
        "网站设计师：请根据用户需求自由设计并输出完整 HTML 文档。网页的内容、叙事、页数、"
        "HTML 结构、CSS、排版、色彩、图形、SVG、插画、动效、转场和视觉风格全部由你决定；"
        "宿主不规定任何模板、审美方向或内容结构。\n\n"
        "唯一的硬约束只有两项：1）每个 data-topos-page 页面的设计画布使用当前时间线尺寸，"
        "尺寸为当前请求中给出的宽高并保持比例；2）每一页用 data-topos-page 标记，供编辑器按时间切换。"
        "这些页面是一张张可以切换的网页，Motion Graphic Clip 编辑器会按总时长自动切换到下一页并逐帧捕获；"
        "页面数量由你自行决定。"
        "页面切换由编辑器时间轴统一控制；不要用 setInterval、递归 setTimeout 或自定义轮播逻辑"
        "切换 data-topos-page、.active 或当前页索引，否则实时预览和逐帧导出会产生竞争时钟。"
        "除此之外，页面结构、内容、布局、留白、定位、叠层、内部滚动、色彩、字体、图形、动效、"
        "转场和视觉风格全部由你自由发挥，宿主不加入任何审美或结构限制。\n\n"
        "宿主接口（不是审美约束）：只输出 HTML，不要 Markdown 或解释文字；每一页用独立的"
        "data-topos-page=\"1\"、\"2\"、\"3\" 等容器标记。捕获桥接运行在受控环境中，"
        "这是一个真正的浏览器创作环境：允许使用 JavaScript、Canvas、Web Animations、事件处理器、"
        "CSS Houdini/自定义属性、CSS @keyframes、animation、transition、响应式 @media/@supports，"
        "也允许使用 HTTPS CDN、远程字体、图片、外部 CSS 和脚本。已绑定项目素材可按"
        "topos-asset:// 规则引用。浏览器安全层只隔离文件系统、宿主应用、弹窗、下载和本地持久化，"
        "不会替你改变网页的设计或删除作者代码。\n\n"
        "动画时间：按普通网页的习惯创作即可——requestAnimationFrame、setTimeout、"
        "performance.now()、Date.now() 在逐帧捕获时都会自动对齐时间线虚拟时钟，"
        "CSS/JS/Canvas/WebGL 动效会被精确渲染到每一帧，你不需要做任何特殊处理。"
        "（可选进阶：监听 window 的 topos-seek 事件可拿到 frame、time、fps、pageIndex。）\n\n"
        "除此之外没有任何审美、风格、结构、页数、元素类型或内容限制。请像真正的网站设计师"
        "一样自行判断并完成作品；即使主题是 HTML，也不要求展示 HTML 标签，除非这是你认为最好的设计。"
    )

    # ------------------------------------------------------------------ #
    # MG-04: Creative-proposal stage system/user prompts.
    # The proposal stage asks the LLM for 1-3 lightweight creative concepts
    # (narrative, copy, palette, beat sheet) WITHOUT scene commands.  The
    # user picks one, and only then is it compiled into scene commands via
    # a second LLM call (the build stage).
    # ------------------------------------------------------------------ #

    _MOTION_GRAPHIC_PROPOSAL_SYSTEM_PROMPT = (
        "你是 Topos Color 的 Motion Graphic 创意策划师。用户想创建一个"
        "动态图形动画，你需要先提出 2-3 个不同的创意方案供用户选择。\n\n"
        "你必须严格只输出一个 JSON 对象（不要 Markdown，不要解释文字）：\n"
        "{\n"
        '  "proposals": [\n'
        '    {\n'
        '      "proposal_id": "A",\n'
        '      "title": "方案标题（中文，简短，含风格关键词）",\n'
        '      "summary": "一段话概述创意方向（30-80字）",\n'
        '      "beats": [\n'
        '        {"beat_id": "b1", "label": "段落名称",'
        ' "start_seconds": 0.0, "duration_seconds": 3.0,'
        ' "on_screen_copy": "这个段落屏幕上要显示的文案"}\n'
        '      ],\n'
        '      "style": {"palette": {"bg": "#rrggbb",'
        ' "accent": "#rrggbb"}, "tone": "整体风格描述"},\n'
        '      "element_sketch": {"title": "主标题文案",'
        ' "subtitle": "副标题文案",'
        ' "cards": ["卡片1名称", "卡片2名称", "卡片3名称"],'
        ' "background": "背景描述"},\n'
        '      "estimated_risks": ["潜在风险1"],\n'
        '      "rationale": "推荐理由（为什么这个方案适合用户需求）"\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        "创作要求：\n"
        "1. 提供 2-3 个真正不同的创意方向，差异来自你对用户主题的理解。\n"
        "2. beats 只用来表达叙事节奏，数量和节奏由你自行判断。\n"
        "3. on_screen_copy、构图、配色、字体和动效根据用户主题原创。\n"
        "4. element_sketch 描述你真正想做的视觉元素，不要输出 scene commands、坐标或关键帧。"
    )

    @staticmethod
    def _build_motion_graphic_creative_brief_user_prompt(
        text: str, *, seconds: float, fps: float,
        width: int = 1920, height: int = 1080,
        revision_feedback: str = "",
    ) -> str:
        """Build the visible plan-review request for direct generation."""
        cleaned = str(text or "").strip()
        duration_frames = max(1, round(seconds * fps))
        prompt = (
            f"用户需求：{cleaned}\n\n"
            "项目硬约束：\n"
            f"- 画布：{width}x{height} CSS px\n"
            f"- 时长：{seconds:g} 秒（约 {duration_frames} 帧，fps={fps:g}）\n\n"
            "请先写一份主题专属的 Motion Graphic 制作计划，稍后展示给用户审核。"
            "只输出系统提示中定义的 JSON；不要输出 HTML、CSS、坐标、关键帧或宿主操作。"
        )
        if revision_feedback.strip():
            prompt += (
                "\n\n这是一次计划修订。请根据用户反馈重新输出一个完整 JSON 对象，"
                "并严格修正以下问题：\n"
                + revision_feedback.strip()
            )
        return prompt

    @staticmethod
    def _build_motion_graphic_proposal_user_prompt(
        text: str, *, seconds: float, fps: float,
        width: int = 1920, height: int = 1080,
    ) -> str:
        """MG-04: user prompt for the creative-proposal stage.

        Asks the LLM to output 2-3 lightweight creative concepts (no scene
        commands).  The user picks one before any scene is compiled.
        """
        cleaned = str(text or "").strip()
        aspect = "16:9"
        if height > 0:
            ratio = width / height
            if ratio < 0.6:
                aspect = "9:16"
            elif ratio < 0.9:
                aspect = "1:1"
        return (
            f"用户需求：{cleaned}\n\n"
            f"硬约束：\n"
            f"- 总时长：{seconds:g} 秒\n"
            f"- 画幅：{width}x{height}（{aspect}）\n"
            f"- 不要使用外部付费素材\n\n"
            f"请基于用户需求提出 2-3 个创意方案，只输出上述格式的 JSON。"
        )

    @staticmethod
    def _build_motion_graphic_build_user_prompt(
        text: str, *, seconds: float, fps: float,
        width: int = 1920, height: int = 1080,
        proposal_context: str = "",
        creative_context: str = "",
    ) -> str:
        """构造给 LLM 的 user prompt（含用户描述 + 画布/时长约束）。

        MG-11: 画布尺寸从 brief 获取，不再硬编码 1920×1080。
        MG-04: ``proposal_context`` carries the selected creative proposal's
        narrative/copy/style so the scene compiler honours the user's choice.
        """
        duration_frames = max(1, round(seconds * fps))
        cleaned = str(text or "").strip()
        parts = [
            f"用户需求：{cleaned}\n\n",
            "基础技术约束（除此之外请自由设计）：\n"
            f"- 当前项目画布：{width}x{height}；必须保持这个比例和分辨率\n"
            f"- 时长：{seconds:g} 秒（{duration_frames} 帧，fps={fps:g}）\n"
            f"- 动画 keyframe.frame 必须满足 0 <= frame < {duration_frames}\n"
            "- 必须返回可解析、可渲染的 JSON；资源只能引用当前渲染环境实际可用的内容，\n"
            "  不可用的资源请改为内嵌图形或自行绘制，不要让缺失资源破坏画面。\n\n"
            "- JSON 字符串中的换行、引号和 SVG 属性必须正确转义；保持 JSON 紧凑，避免无意义的重复\n"
            "视觉创作完全由你决定；自由选择构图、留白、层叠、重叠、配色、字体、装饰元素、"
            "转场和动画节奏。\n",
        ]
        if proposal_context.strip():
            parts.append(
                f"\n用户已选定以下创意方案，请严格按照方案的文案、配色和节奏"
                f"设计场景命令：\n{proposal_context.strip()}\n"
            )
        if creative_context.strip():
            parts.append(
                "\nAI 创意总监为本次主题制定了以下内部视觉简报。请把它转化为"
                "具体设计，但不要输出简报 JSON；视觉形式完全由你重新判断：\n"
                + creative_context.strip() + "\n"
            )
        parts.append("\n请基于以上信息设计一支完成度高、具有独特视觉风格的 Motion Graphic，只输出上述格式的 JSON。")
        return "".join(parts)

    @staticmethod
    def _build_motion_graphic_html_user_prompt(
        text: str, *, seconds: float, fps: float,
        width: int = 1920, height: int = 1080,
        proposal_context: str = "",
        creative_context: str = "",
        approved_plan_context: str = "",
        feedback_context: str = "",
        revision_feedback: str = "",
        current_html: str = "",
    ) -> str:
        """Build the browser hand-off prompt.

        The canvas is the only design input owned by the host.  ``seconds`` and
        ``fps`` are playback metadata consumed by the editor; they do not tell
        the model how many pages, beats, layouts, or visual motifs to invent.
        ``creative_context`` remains in the signature for persisted/third-party
        callers, but is intentionally ignored so an unreviewed brief cannot
        constrain the website author. The separately named
        ``approved_plan_context`` is included only after the user approves the
        visible plan and is treated as creative conversation context.
        """
        del creative_context
        cleaned = str(text or "").strip()
        parts = [
            f"用户需求：{cleaned}\n\n",
            "项目基础信息（只有两项）：\n"
            f"- 视频交付视口：{width}x{height} CSS px，并保持当前项目比例；这不是固定像素布局要求，"
            "网页必须使用响应式布局（如 100vw/100vh、Grid/Flex、百分比、clamp()），在不同浏览器尺寸自然重排\n"
            "- 你自行决定页面数量；每页使用 data-topos-page 标记，编辑器负责按时间切换页面\n"
            "- 每一页都要推进叙事：包含一个主题主视觉/图解/空间构图，以及至少两组有实质信息的辅助内容；"
            "可自由使用流程、对比、数据、案例、标签、界面、注释等形式，丰富但不堆字\n"
            "除此之外不限制网页设计、内容、结构、视觉表现或动画方式；网页设计、内容和视觉表现全部由你自由发挥；"
            "请输出完整 HTML 文档。\n\n",
        ]
        if revision_feedback.strip():
            parts.append(
                "这是一次网页修改请求。请根据用户这轮反馈修改或重写网页；不要输出"
                "诊断文字，只输出完整 HTML 文档：\n" + revision_feedback.strip() + "\n"
            )
        if current_html.strip():
            parts.append(
                "这是一次对现有网页的修改请求。下面是当前网页的完整源文件：请在它的"
                "基础上做最小必要的修改来实现用户这轮的要求；保留全部 data-topos-page "
                "页面、页面顺序、页数和整体设计语言（除非用户明确要求增删页面或大改"
                "风格）。请直接返回修改后的完整 HTML 文档：\n"
                "--- CURRENT HTML ---\n" + current_html.strip() +
                "\n--- END CURRENT HTML ---\n"
            )
        if proposal_context.strip():
            parts.append(
                "如果下面有用户选定的创意方向，请吸收它的叙事和视觉语言，但不要把它"
                "当成僵硬模板：\n" + proposal_context.strip() + "\n"
            )
        if approved_plan_context.strip():
            parts.append(
                "以下是用户审阅并确认的制作计划。它是本次创作的真实方向，不是宿主模板或"
                "额外技术限制；请吸收其中的主题、叙事和节奏，再自由重构成完成度高的网页作品：\n"
                + approved_plan_context.strip() + "\n"
            )
        if feedback_context.strip():
            parts.append(
                "以下是用户在本次创作流程中追加并保留的要求。请落实到网页本身；若彼此或"
                "与本轮修改要求冲突，以较后的要求为准：\n"
                + feedback_context.strip() + "\n"
            )
        return "".join(parts)

    def _build_scene_from_commands(
        self, payload: dict[str, Any], *, seconds: float, fps: float,
        width: int = 1920, height: int = 1080,
    ) -> Any:
        """把 LLM 返回的 JSON 命令转成一个完整 TeachingSceneDocument。

        MG-11: 画布尺寸从参数获取（源自 brief），不再硬编码 1920×1080。
        校验失败抛 ``RuntimeError``（可读原因），调用方负责处理。
        """
        from .ai.commands import TeachingAICommand
        from .core.model import TeachingSceneDocument

        name = str(payload.get("name", "") or "").strip() or "Motion Graphic 场景"
        background_color = str(payload.get("background_color", "") or "").strip()
        commands_raw = payload.get("commands")
        if not isinstance(commands_raw, list) or not commands_raw:
            raise RuntimeError("LLM 返回的 commands 不是非空数组")

        duration = max(1, round(seconds * fps))
        base = TeachingSceneDocument.create_default(
            name=name,
            width=int(width),
            height=int(height),
            fps_num=max(1, round(fps)),
            duration_frames=duration,
        )
        # 若 LLM 提供了背景色，应用到 scene.background（命令里的全屏 shape 仍负责真正绘制）。
        if background_color:
            from dataclasses import replace as _replace
            base = _replace(base, background={"color": background_color})

        # 把 create_default 自带的占位 title 移除，避免与新场景内容重叠。
        # 通过先记录它的 element_id，再用 update 命令不可行（我们要删除而非改），
        # 故直接构造一个不含该元素的新 base。
        if base.elements:
            from dataclasses import replace as _replace
            base = _replace(base, elements=())

        # 解析命令为 TeachingAICommand。接受完整 MG-09 命令集（4 个基础名 +
        # 16 个扩展名），而不再仅 add_element/set_animation：这样 LLM 可以在
        # 初始生成时就发 set_duration/set_background/add_marker/set_layout/
        # set_color_token/set_typo_token 等高层命令。扩展 registry 是基础
        # registry 的超集，4 个基础名仍委派给基础编译器，扩展名的 dry_run 是
        # list-aware 的。非法名/参数/引用缺失都会在 dry_run 阶段抛错。
        from .ai.command_catalog import EXTENDED_COMMAND_NAMES, ExtendedCommandRegistry
        accepted_names = {"add_element", "update_element", "reorder_element", "set_animation"} | set(EXTENDED_COMMAND_NAMES)
        commands: list[TeachingAICommand] = []
        for item in commands_raw:
            if not isinstance(item, dict):
                raise RuntimeError("LLM 返回的 command 不是对象")
            name_cmd = str(item.get("name", ""))
            arguments = item.get("arguments")
            # DeepSeek sometimes shortens the obvious ``add_element`` shape
            # to ``{"name":"add_element","element":{...}}`` even when the
            # schema says that element belongs under arguments.  Accept that
            # harmless equivalent so visual freedom does not become a brittle
            # formatting failure.
            if not isinstance(arguments, dict) and name_cmd == "add_element":
                direct_element = item.get("element")
                if isinstance(direct_element, dict):
                    arguments = {"element": direct_element}
            if not isinstance(arguments, dict) and name_cmd == "set_animation":
                shorthand_keys = {"element_id", "property", "min_val", "max_val", "keyframes"}
                if shorthand_keys.issubset(item):
                    arguments = {key: item[key] for key in shorthand_keys}
            if not isinstance(arguments, dict):
                raise RuntimeError(f"命令 {name_cmd} 缺少 arguments 对象")
            if name_cmd not in accepted_names:
                raise RuntimeError(f"不支持的命令名：{name_cmd}")
            # add_element/replace_element.element 必须是对象（SceneElement.from_dict 会校验 type 等字段）
            if name_cmd in {"add_element", "replace_element"}:
                element_raw = arguments.get("element")
                if not isinstance(element_raw, dict):
                    raise RuntimeError(f"{name_cmd} 的 element 字段必须是对象")
            commands.append(TeachingAICommand(name_cmd, dict(arguments)))

        # dry_run 会顺次 apply 所有命令并校验（元素冲突、动画目标不存在等都会抛错）
        registry = ExtendedCommandRegistry()
        try:
            proposal = registry.dry_run(base, tuple(commands))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"LLM 生成的命令无法应用到空白场景：{exc}") from exc
        if proposal.is_empty:
            raise RuntimeError("LLM 生成的命令应用后场景无变化")
        return proposal.proposed_scene

    @staticmethod
    def _brief_canvas_kwargs(plan: dict[str, Any]) -> dict[str, int]:
        """Extract canvas width/height from a plan's MotionGraphicBrief (MG-11).

        Returns ``{"width": int, "height": int}`` for passing to prompt /
        scene builders.  Falls back to 1920×1080 only when no brief is stored
        (backward compatibility for older plan dicts).
        """
        brief = plan.get("brief")
        if brief is not None:
            try:
                return dict(zip(("width", "height"), brief.canvas_size()))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Motion Graphic brief canvas_size() failed, "
                    "falling back to 1920x1080: %s", e,
                )
        return {"width": 1920, "height": 1080}

    # ------------------------------------------------------------------ #
    # MG-12: Project context fingerprint for stale detection
    # ------------------------------------------------------------------ #

    @staticmethod
    def _collect_context_fingerprint(
        timeline: Any, graphics_track: Any, frame: int,
    ) -> str:
        """MG-12: compute a stable fingerprint of the project context.

        Uses ``ProjectContextSnapshot.fingerprint()`` from context_collector.py
        so that a stale plan (project/timeline changed after the card was
        shown) can be detected by comparing fingerprints.
        """
        try:
            from .ai.context_collector import ProjectContextSnapshot, TimelineContext
            timeline_id = getattr(timeline, "timeline_id", "") or "unknown"
            revision = int(getattr(timeline, "revision", 0) or 0)
            track_id = getattr(graphics_track, "track_id", None)
            track_label = getattr(graphics_track, "name", None) or "Graphics"
            tc = TimelineContext(
                timeline_id=timeline_id,
                revision=revision,
                track_id=track_id,
                track_label=track_label,
                insert_frame=max(0, frame),
            )
            snapshot = ProjectContextSnapshot(
                project_id="teaching_animation",
                project_revision=revision,
                timeline=tc,
                delivery_profile_id=None,
            )
            return snapshot.fingerprint()
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ #
    # MG-09: Extended command vocabulary
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_supported_edit_commands() -> tuple[str, ...]:
        """MG-09: return all command names supported for scene editing.

        Combines the base registry vocabulary (add_element, update_element,
        reorder_element, set_animation) with the extended registry (remove,
        replace, set_duration, set_background, markers, keyframes, layout,
        color/typo tokens, audio refs).
        """
        base = ("add_element", "update_element", "reorder_element", "set_animation")
        try:
            from .ai.command_catalog import EXTENDED_COMMAND_NAMES
            extended = tuple(sorted(EXTENDED_COMMAND_NAMES))
            return base + extended
        except ImportError:
            return base

    def _build_ai_agent_demo_scene(
        self, duration_seconds: float, fps: float, *, variant: int = 0,
    ) -> Any:
        from .core.model import SceneAnimation, SceneElement, SceneTransform, TeachingSceneDocument

        duration = max(1, round(duration_seconds * fps))
        variant = int(variant) % 3
        palettes = (
            ("#0f172a", ("#2563eb", "#7c3aed", "#db2777", "#059669")),
            ("#172554", ("#06b6d4", "#14b8a6", "#f59e0b", "#f43f5e")),
            ("#1f1235", ("#ec4899", "#8b5cf6", "#3b82f6", "#22c55e")),
        )
        background_fill, step_colors = palettes[variant]
        scene = TeachingSceneDocument.create_default(
            name="AI Agent 介绍动画",
            width=1920,
            height=1080,
            fps_num=max(1, round(fps)),
            duration_frames=duration,
        )
        title = replace(
            scene.elements[0],
            element_id="ai_agent_title",
            transform=SceneTransform(
                x=(120 if variant != 2 else 260),
                y=(120 if variant != 1 else 80),
                width=1200,
                height=90,
            ),
            style={
                "color": "#f9fafb",
                "font_size": (76 if variant == 0 else 82),
                "font_family": "sans-serif",
            },
            content="AI Agent",
        )
        subtitle = SceneElement(
            "ai_agent_subtitle", "text",
            transform=SceneTransform(
                x=(126 if variant != 2 else 266),
                y=(235 if variant != 1 else 185),
                width=1400,
                height=54,
            ),
            style={"color": ("#93c5fd" if variant == 0 else "#a5f3fc"), "font_size": 34},
            content="从问题到行动的智能协作者",
        )
        steps = (("理解", step_colors[0]), ("规划", step_colors[1]), ("执行", step_colors[2]), ("验证", step_colors[3]))
        background = SceneElement(
            "ai_agent_background",
            "shape",
            z_index=-10,
            transform=SceneTransform(x=0, y=0, width=1920, height=1080),
            content={"kind": "rect", "fill": background_fill},
        )
        elements = [background, title, subtitle]
        animations = [
            SceneAnimation(
                "ai_title_x", title.element_id, "x",
                self._keyframe_track("x", ((0, -700.0), (round(fps * 1.2), 120.0)), min_val=-2000, max_val=2000),
            ),
            SceneAnimation(
                "ai_title_opacity", title.element_id, "opacity",
                self._keyframe_track("opacity", ((0, 0.0), (round(fps * 0.8), 1.0)), min_val=0, max_val=1),
            ),
            SceneAnimation(
                "ai_subtitle_opacity", subtitle.element_id, "opacity",
                self._keyframe_track("opacity", ((round(fps * 0.8), 0.0), (round(fps * 1.8), 1.0)), min_val=0, max_val=1),
            ),
        ]
        for index, (label, color) in enumerate(steps):
            if variant == 0:
                x, y, card_width, card_height = 120 + index * 440, 480, 360, 190
            elif variant == 1:
                x, y, card_width, card_height = 150, 360 + index * 155, 760, 118
            else:
                x = 180 if index % 2 == 0 else 1000
                y, card_width, card_height = 390 + (index // 2) * 235, 700, 150
            card_id = f"ai_agent_card_{index}"
            text_id = f"ai_agent_card_text_{index}"
            elements.extend((
                SceneElement(
                    card_id, "shape", z_index=1,
                    transform=SceneTransform(x=x, y=y, width=card_width, height=card_height),
                    content={"kind": "rect", "fill": color},
                ),
                SceneElement(
                    text_id, "text", z_index=2,
                    transform=SceneTransform(
                        x=x + (32 if variant != 2 else 44),
                        y=y + (55 if variant == 0 else 34),
                        width=card_width - 64,
                        height=50,
                    ),
                    style={"color": "#ffffff", "font_size": (42 if variant == 0 else 36)},
                    content=label,
                ),
            ))
            start = min(duration - 1, round(fps * (2.0 + index * (0.7 if variant != 2 else 0.9))))
            end = min(duration - 1, start + max(1, round(fps * 0.8)))
            animations.append(SceneAnimation(
                f"{card_id}_opacity", card_id, "opacity",
                self._keyframe_track("opacity", ((start, 0.0), (end, 1.0)), min_val=0, max_val=1),
            ))
            animations.append(SceneAnimation(
                f"{text_id}_opacity", text_id, "opacity",
                self._keyframe_track("opacity", ((start, 0.0), (end, 1.0)), min_val=0, max_val=1),
            ))
        return replace(scene, elements=tuple(elements), animations=tuple(animations))

    @staticmethod
    def _is_confirmation_text(text: str) -> bool:
        value = str(text or "").strip().lower()
        return value in {
            "确认", "确认执行", "执行", "开始执行", "可以", "没问题", "同意",
            "confirm", "approve", "yes", "ok", "go ahead",
        }

    @staticmethod
    def _is_cancellation_text(text: str) -> bool:
        value = str(text or "").strip().lower()
        return value in {
            "取消", "不要执行", "暂不执行", "停止", "拒绝", "不同意", "不生成",
            "先讨论", "no", "cancel", "reject",
        }

    @staticmethod
    def _append_assistant_message(main_window: Any, text: str, *, error_code: str | None = None) -> None:
        service = getattr(main_window, "_ai_assistant_service", None)
        append = getattr(service, "append_chat_message", None)
        if callable(append):
            append("assistant", text, error_code=error_code)
            return
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        notice = getattr(panel, "set_agent_notice", None)
        if callable(notice):
            notice(text, warning=error_code is not None)

    @staticmethod
    def _append_user_message(main_window: Any, text: str) -> None:
        service = getattr(main_window, "_ai_assistant_service", None)
        append = getattr(service, "append_chat_message", None)
        if callable(append):
            append("user", text)

    @staticmethod
    def _set_run_status(
        main_window: Any, label: str, *,
        detail: str = "", state: str = "active",
    ) -> None:
        """Publish the current pipeline phase to the bottom status surfaces.

        The assistant panel's activity line sits directly above the composer
        (its ``set_activity_status`` contract exists exactly for controllers
        and runtimes to push human-readable phases), and the main-window
        status bar mirrors the same text.  Publishing must never break a
        pipeline, so every failure is swallowed.
        """
        status_map = {
            "active": ("running", True),
            "waiting": ("waiting", False),
            "succeeded": ("succeeded", False),
            "failed": ("failed", False),
        }
        status, active = status_map.get(state, ("running", True))
        text = str(label or "").strip() or "AI 正在工作"
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        set_status = getattr(panel, "set_activity_status", None)
        if callable(set_status):
            try:
                set_status(text, detail=str(detail or ""), active=active, status=status)
            except Exception:  # noqa: BLE001
                logger.debug("run status publish failed", exc_info=True)
        bar = getattr(main_window, "status_bar", None)
        show = getattr(bar, "showMessage", None)
        if callable(show):
            try:
                message = text + (f"｜{detail}" if detail else "")
                show(message, 0 if active else 8000)
            except Exception:  # noqa: BLE001
                logger.debug("status bar publish failed", exc_info=True)

    @staticmethod
    def _complete_html_edit_run(
        main_window: Any, request_id: str, *, success: bool,
    ) -> None:
        """Retire the controller-side AgentRun for one finished edit phase."""
        controller = getattr(main_window, "_ai_edit_controller", None)
        complete = getattr(controller, "complete_motion_graphic_run", None)
        if callable(complete):
            try:
                complete(str(request_id), success=success)
            except Exception:  # noqa: BLE001
                logger.debug("complete motion graphic run failed", exc_info=True)

    def _confirm_motion_graphic_plan(self, main_window: Any, request_id: str) -> bool:
        """Dispatch the shared approval-card button to the matching plan type."""
        request_id = str(request_id)
        if request_id in self._pending_scene_edit_requests:
            return self._confirm_scene_edit_request(main_window, request_id)
        if request_id in self._pending_html_generate_requests:
            # AUD-28005: None means the browser re-validation now runs on a
            # QThread; the confirm was accepted and the queued completion
            # finishes the write, so treat it as handled.
            result = self._confirm_html_generate_request(main_window, request_id)
            return True if result is None else bool(result)
        motion_plan = self._pending_motion_graphic_requests.get(request_id)
        if motion_plan is not None and motion_plan.get("phase") == "plan_review":
            # The card's “开始生成” button is an explicit equivalent of
            # sending “可以”; it advances this request instead of creating a
            # second plan or requiring another text confirmation.
            return self._approve_motion_graphic_plan(
                main_window, request_id, start_generation=True,
            )
        if motion_plan is not None and motion_plan.get("phase") == "generation_confirmation":
            return self._start_motion_graphic_generation(main_window, request_id)
        result = self._confirm_motion_graphic_request(main_window, request_id)
        return True if result is None else bool(result)

    def _request_motion_graphic_template(self, main_window: Any, request_id: str) -> bool:
        """AUD-19500：接收面板"改用内置模板"意图（专用信号宿主半边）。

        在 pending plan 上置 ``use_template = True`` 后走与"确认执行"相同
        的确认路径——MG-05 守卫（LLM 未产出 scene 时拒绝裸确认）据此放行
        内置模板场景。plan 不存在（未知/已完结请求）时直接按普通确认分派，
        由确认链路自行给出结果。
        """
        request_id = str(request_id)
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is not None:
            plan["use_template"] = True
        return self._confirm_motion_graphic_plan(main_window, request_id)

    def _approve_motion_graphic_plan(
        self,
        main_window: Any,
        request_id: str,
        *,
        start_generation: bool = False,
    ) -> bool:
        """Approve the visible plan, optionally starting HTML authoring now."""
        request_id = str(request_id)
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None or plan.get("phase") != "plan_review":
            return False
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
        if callable(clear_card):
            clear_card(request_id)
        plan["plan_approved"] = True
        plan["phase"] = "generation_confirmation"
        plan["creative_brief_pending"] = False
        plan["requires_button_confirmation"] = False
        if start_generation:
            return self._start_motion_graphic_generation(main_window, request_id)
        self._append_assistant_message(
            main_window,
            "已确认这份制作计划。下一步点击“确认生成”后才会调用 AI 生成网页；"
            "如果还要调整，继续告诉我具体修改内容。",
        )
        self._show_motion_graphic_generation_confirmation_card(
            main_window, request_id,
        )
        return True

    def _start_motion_graphic_generation(self, main_window: Any, request_id: str) -> bool:
        """Start HTML authoring after the user confirms the reviewed plan."""
        plan = self._pending_motion_graphic_requests.get(str(request_id))
        if plan is None or plan.get("phase") != "generation_confirmation":
            return False
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
        if callable(clear_card):
            clear_card(str(request_id))
        plan["phase"] = "build"
        plan["creative_brief_pending"] = False
        plan["plan_approved"] = True
        plan["requires_button_confirmation"] = True
        plan["html_retries"] = 0
        plan["html_revision_feedback"] = ""
        self._append_assistant_message(
            main_window,
            "已确认生成。现在交给 AI 自由完成网页设计；生成并预览完成后，"
            "还会再显示最终的“确认执行”按钮，点击后才会插入时间线。",
        )
        if self._trigger_motion_graphic_llm_build(main_window, str(request_id)):
            return True
        self._mark_motion_graphic_build_failed(
            main_window, str(request_id), I18N.tr("motion_graphic.ai.design_channel_unavailable",
        "The HTML/CSS design channel is not wired up or unavailable"),
        )
        return True

    def _cancel_motion_graphic_plan(self, main_window: Any, request_id: str) -> bool:
        """Cancel either a new-clip plan or a follow-up scene-edit plan."""
        request_id = str(request_id)
        if request_id in self._pending_scene_edit_requests:
            kind = "scene_edit"
        elif request_id in self._pending_html_generate_requests:
            kind = "html_generate"
        else:
            kind = "motion"
        return self._cancel_motion_graphic_request(main_window, request_id, kind=kind)

    def _confirm_scene_edit_request(self, main_window: Any, request_id: str) -> bool:
        """Apply a scene diff through the editor command stack and verify it."""
        plan = self._pending_scene_edit_requests.pop(str(request_id), None)
        if plan is None:
            # AUD-28003: never clear a preview card for an unknown/retired
            # request — the card belongs to whichever request is still alive.
            return False
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        clear = getattr(panel, "clear_motion_graphic_proposal", None)
        if callable(clear):
            clear(str(request_id))
        try:
            from .ai.command_catalog import ExtendedCommandRegistry
            from .core.contracts import RenderContext
            from .host_adapters import render_teaching_scene_frame
            from .scene_store import get_shared_scene_store

            store = get_shared_scene_store()
            current = store.get(plan["scene_id"])
            if current is None:
                raise RuntimeError("目标场景已不存在，请重新打开 Motion Graphic Clip")
            # AUD-28002: refuse stale proposals.  The proposal's style dicts
            # were merged against the element as it was when the model reply
            # was parsed; applying them after further user edits would revert
            # those edits (update_element replaces the whole mapping).  Same
            # guard the HTML path applies at confirmation time.
            expected_revision = plan.get("scene_revision")
            if expected_revision is not None and store.revision(current.scene_id) != expected_revision:
                self._append_assistant_message(
                    main_window,
                    I18N.tr("motion_graphic.ai.stale_scene_confirm",
                            "Confirmation failed: the target scene changed after the preview. "
                            "Regenerate the HTML motion."),
                    error_code="teaching_scene_edit_stale",
                )
                return False
            # Apply with the same extended registry that produced the proposal's
            # dry_run, so list-returning compiles (replace_element/group_elements)
            # execute every core command and undo stays balanced.
            registry = ExtendedCommandRegistry()
            proposal = plan["proposal"]
            window_registry = getattr(self, "_editor_registry", None)
            window = window_registry.get(plan["scene_id"]) if window_registry is not None else None
            controller = getattr(window, "editor_controller", None)
            if controller is not None:
                updated = registry.apply(proposal, controller, confirmed=True)
            else:
                from .ui.editor_controller import SceneEditorController
                temporary = SceneEditorController(current)
                updated = registry.apply(proposal, temporary, confirmed=True)
                store.register(updated)

            rendered = render_teaching_scene_frame(
                updated.scene_id,
                0,
                context=RenderContext(
                    fps=updated.fps, width=64, height=36, transparent=True,
                ),
            )
            if not rendered.data or store.get(updated.scene_id) is None:
                raise RuntimeError("修改后的场景验证失败")
            try:
                from .scene_cache import get_shared_scene_cache_coordinator
                get_shared_scene_cache_coordinator().schedule(updated)
            except Exception:
                logger.debug("Teaching Animation scene cache refresh skipped", exc_info=True)
            if hasattr(main_window, "edit_project_dirty"):
                main_window.edit_project_dirty = True
            changed = plan.get(
                "changed_summary",
                ", ".join(sorted(plan.get("updates", {}))),
            )
            self._append_assistant_message(
                main_window,
                f"已确认并完成场景修改：scene_id={updated.scene_id}，"
                f"element_id={plan['element_id']}，修改字段={changed}；首帧验证通过。",
            )
            return True
        except Exception as exc:
            logger.exception("Teaching Animation scene edit failed")
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.scene_edit_failed",
                        "The scene edit was not applied: {error}", error=exc),
                error_code="teaching_scene_edit_failed",
            )
            return False

    def _confirm_motion_graphic_request(
        self, main_window: Any, request_id: str,
    ) -> bool | None:
        """Apply one previously reviewed Motion Graphic plan and verify it.

        AUD-28005: for scenes without an earlier background browser pass the
        confirm-time gate runs on a QThread in production; this method then
        returns ``None`` (accepted, completion queued) instead of freezing
        the confirm click for the whole capture.
        """
        plan = self._pending_motion_graphic_requests.pop(str(request_id), None)
        if plan is None:
            # AUD-28003: unknown/retired request — leave any visible card
            # alone instead of clearing a preview that belongs elsewhere.
            return False
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        clear = getattr(panel, "clear_motion_graphic_proposal", None)
        if callable(clear):
            clear(str(request_id))

        try:
            timeline_panel = getattr(main_window, "timeline_panel", None)
            if timeline_panel is None:
                raise RuntimeError("当前没有可用的时间线面板")
            self._set_run_status(
                main_window, "正在写入时间线并验证",
                detail="注册素材 + 插入片段 + 渲染复验…",
            )
            view = timeline_panel.get_timeline_view()
            if getattr(view, "_timeline", None) is None:
                timeline_panel.show_provisional_timeline()
            timeline = getattr(view, "_timeline", None)
            if timeline is None:
                raise RuntimeError("当前没有可用的时间线")

            scene = plan.get("scene")
            if scene is None:
                # MG-05: LLM 未成功生成时，不再自动套样板冒充定制结果。
                # 只有用户显式选择"改用内置模板"（plan["use_template"] = True）
                # 才创建模板场景。
                if not plan.get("use_template"):
                    self._append_assistant_message(
                        main_window,
                        I18N.tr("motion_graphic.ai.no_scene",
                                "The scene was not generated successfully. Start again, or "
                                "explicitly switch to a built-in template."),
                        error_code="motion_graphic_no_scene",
                    )
                    return False
                scene = self._build_ai_agent_demo_scene(
                    plan["seconds"], plan["fps"], variant=int(plan.get("variant", 0)),
                )

            # MG-06: verify scene hash matches what was approved on the card.
            # If the scene changed after the card was shown (stale), refuse.
            approved_hash = plan.get("approved_scene_hash")
            if approved_hash is not None and scene is not None:
                import hashlib as _hashlib
                import json as _json
                current_payload = _json.dumps(
                    scene.to_dict(), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str,
                )
                current_hash = _hashlib.sha256(
                    current_payload.encode("utf-8")
                ).hexdigest()
                if current_hash != approved_hash:
                    self._append_assistant_message(
                        main_window,
                        I18N.tr("motion_graphic.ai.scene_stale",
                                "The scene changed after confirmation (stale). Review the "
                                "proposal again before confirming."),
                        error_code="motion_graphic_scene_stale",
                    )
                    return False
            from .scene_store import get_shared_scene_store

            store = get_shared_scene_store()
            store.register(scene)
            # HTML 场景在确认卡片出现前已经由后台浏览器线程验证过同一
            # 个不可变 scene。重复启动 Chromium 会再次让确认操作出现
            # 转圈，因此只对没有完成后台验证的旧/JSON 场景保留复验。
            if not (
                plan.get("render_mode") == "html"
                and plan.get("browser_validated") is True
            ):
                # 执行前验证场景至少能渲染一帧，避免把不可渲染的引用写入
                # 时间线。超时、崩溃、CSP/资源错误仍然立即失败。
                import os as _os
                if _os.environ.get("TOPOS_TEST_MODE") != "1":
                    # AUD-28005: this full-size browser gate used to run
                    # inside the confirm slot and froze the UI for the whole
                    # capture.  Run it on a QThread; the queued completion
                    # inserts the clip (and re-reads the timeline, which may
                    # have changed while the browser worked).
                    def _validated(_rendered: Any) -> None:
                        self._apply_confirmed_motion_graphic_scene(
                            main_window, request_id, plan, scene,
                        )

                    def _validation_failed(message: str) -> None:
                        self._motion_graphic_execution_failed(main_window, message)

                    if self._start_browser_validation_job(
                        f"motion:{request_id}", scene,
                        _validated, _validation_failed,
                    ):
                        self._set_run_status(
                            main_window, "正在后台验证场景渲染",
                            detail="真实浏览器捕获复验，期间可继续操作…",
                            state="waiting",
                        )
                        return None
                # Synchronous fallback: test harnesses and Qt-less hosts
                # (no event loop to block) keep the deterministic gate.
                rendered, validation_frame = self._validate_motion_graphic_scene(scene)
                if not rendered.data or rendered.width <= 0 or rendered.height <= 0:
                    raise RuntimeError(
                        f"Motion Graphic 场景验证失败（候选帧 {validation_frame} 为空）"
                    )
            return self._apply_confirmed_motion_graphic_scene(
                main_window, request_id, plan, scene,
            )
        except Exception as exc:
            logger.exception("Motion Graphic plan execution failed")
            self._motion_graphic_execution_failed(main_window, str(exc))
            return False

    def _apply_confirmed_motion_graphic_scene(
        self, main_window: Any, request_id: str,
        plan: dict[str, Any], scene: Any,
    ) -> bool:
        """Insert the validated scene into the timeline (AUD-28005 continuation).

        Called directly in the synchronous path and from the queued
        background-validation completion.  The timeline handles are resolved
        again here so a background capture that outlived timeline edits
        still inserts against the live state.
        """
        try:
            from .host_adapters import MotionGraphicClipCommand
            from .scene_store import get_shared_scene_store

            timeline_panel = getattr(main_window, "timeline_panel", None)
            if timeline_panel is None:
                raise RuntimeError("当前没有可用的时间线面板")
            view = timeline_panel.get_timeline_view()
            timeline = getattr(view, "_timeline", None)
            if timeline is None:
                raise RuntimeError("当前没有可用的时间线")
            store = get_shared_scene_store()

            # Publish the scene as a virtual Media Pool asset before creating
            # the timeline instance.  The clip then references the exact same
            # stable asset id that users can see, drag and reuse from the pool.
            asset_id = self._register_motion_graphic_asset(main_window, scene)

            frame = int(plan["frame"])
            graphics_track = next(
                (
                    track for track in timeline.video_tracks
                    if getattr(track, "track_role", "") == "graphics"
                ),
                None,
            )
            command = MotionGraphicClipCommand(
                scene,
                max(0, frame),
                scene.duration_frames,
                getattr(graphics_track, "track_id", None),
                "overwrite",
                asset_id=asset_id,
            )
            view.execute_command(command)
            clip = (
                view._timeline.get_clip_by_id(command._clip_id)
                if command._clip_id is not None else None
            )
            if (
                clip is None
                or clip.asset_id != asset_id
                or clip.timeline_duration != scene.duration_frames
                or store.get(scene.scene_id) is None
            ):
                raise RuntimeError("Motion Graphic Clip 写入后验证失败")

            try:
                self._open_scene(main_window, scene)
            except Exception:
                logger.exception("Motion Graphic editor window failed to open after insertion")
            artifact_label = (
                "网页级 HTML/CSS Motion Graphic Clip"
                if plan.get("render_mode") == "html"
                else "AI Motion Graphic Clip"
            )
            self._append_assistant_message(
                main_window,
                f"计划已确认并完成验证：已生成 {plan['seconds']:g} 秒 {artifact_label}，"
                "场景渲染和时间线引用均正常。",
            )
            self._last_motion_graphic_target = {
                "asset_id": str(asset_id),
                "scene_id": str(scene.scene_id),
                "display_name": "Motion Graphic Clip",
            }
            self._set_run_status(
                main_window, "Motion Graphic 已写入时间线",
                detail=f"场景渲染和时间线引用均正常（{plan['seconds']:g} 秒）",
                state="succeeded",
            )
            self._motion_graphic_discussion_active = False
            self._motion_graphic_discussion_brief_text = ""
            self._motion_graphic_discussion_wants_options = False
            return True
        except Exception as exc:
            logger.exception("Motion Graphic plan execution failed")
            self._motion_graphic_execution_failed(main_window, str(exc))
            return False

    def _motion_graphic_execution_failed(
        self, main_window: Any, message: str,
    ) -> None:
        """Shared failure surface of the confirm/apply pipeline (AUD-28005)."""
        self._set_run_status(
            main_window, "写入时间线失败", detail=str(message)[:80], state="failed",
        )
        self._append_assistant_message(
            main_window,
            I18N.tr(
                "motion_graphic.ai.execution_failed",
                "The Motion Graphic plan did not complete: {error}", error=message,
            ),
            error_code="motion_graphic_execution_failed",
        )

    @staticmethod
    def _register_motion_graphic_asset(main_window: Any, scene: Any) -> str:
        """Add/update a scene-backed Motion Graphic in the current Media Pool."""
        project = getattr(main_window, "_current_edit_project", None)
        if project is None:
            ensure_project = getattr(main_window, "_ensure_edit_project", None)
            if callable(ensure_project):
                ensure_project()
            project = getattr(main_window, "_current_edit_project", None)
        if project is None:
            # The production MainWindow normally supplies _ensure_edit_project,
            # but the scene feature is also used by lightweight hosts/tests.
            # Keep the same AE-like behavior there: a first generated asset
            # can bootstrap an edit project instead of being left timeline-only.
            from src.features.edit.core.project import EditProject
            project = EditProject.create("Motion Graphic Project")
            main_window._current_edit_project = project

        new_project, asset_id = project.create_motion_graphic_asset(scene)
        apply_project_change = getattr(main_window, "_apply_project_change", None)
        if callable(apply_project_change):
            apply_project_change(new_project)
        else:
            # Keep lightweight/fake hosts and older integrations functional;
            # the real MainWindow uses _apply_project_change for full sync.
            main_window._current_edit_project = new_project
            panel = getattr(main_window, "media_pool_panel", None)
            if panel is not None and hasattr(panel, "set_project"):
                panel.set_project(new_project)
        if hasattr(main_window, "edit_project_dirty"):
            main_window.edit_project_dirty = True
        return asset_id

    @staticmethod
    def _validate_motion_graphic_scene(scene: Any) -> tuple[Any, int]:
        """Validate one scene through the real host renderer at design size.

        All production HTML entry points use this same gate.  The renderer is
        given a local scene mapping so a staged scene is never published to the
        shared store until its browser capture has produced a non-blank frame.
        """
        from .core.contracts import RenderContext
        from .render.html_validation import validate_html_scene

        return validate_html_scene(
            scene,
            context=RenderContext(
                fps=scene.fps, width=64, height=36, transparent=True,
            ),
        )

    @staticmethod
    def _render_motion_graphic_validation_frame(
        scene: Any,
        *,
        context: Any,
        render_teaching_scene_frame: Any,
    ) -> tuple[Any, int]:
        """Backward-compatible wrapper around the shared HTML validation gate.

        Older unit/integration callers inject ``render_teaching_scene_frame``;
        production callers use :func:`validate_html_scene` directly. Keeping
        one implementation prevents preview, confirmation and export from
        drifting into different retry policies.
        """
        from .render.html_validation import validate_html_scene

        return validate_html_scene(
            scene,
            context=context,
            render_frame=render_teaching_scene_frame,
        )

    def _cancel_motion_graphic_request(
        self, main_window: Any, request_id: str, *, kind: str = "motion",
    ) -> bool:
        if kind == "motion":
            controller = getattr(main_window, "_ai_edit_controller", None)
            cancel_background = getattr(
                controller, "cancel_motion_graphic_postprocess", None,
            )
            if callable(cancel_background):
                cancel_background(str(request_id))
        if kind == "scene_edit":
            pending = self._pending_scene_edit_requests
        elif kind == "html_generate":
            pending = self._pending_html_generate_requests
        else:
            pending = self._pending_motion_graphic_requests
        plan = pending.pop(str(request_id), None)
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        clear = getattr(panel, "clear_motion_graphic_proposal", None)
        if callable(clear):
            clear(str(request_id))
        if plan is None:
            return False
        if kind == "motion":
            self._motion_graphic_discussion_active = True
            self._append_assistant_message(
                main_window,
                "已取消这次生成，时间线没有被修改。我们继续讨论风格和内容，满意后你可以说‘可以生成了’。",
            )
        elif kind == "html_generate":
            self._append_assistant_message(
                main_window,
                "已取消 HTML 动效生成，场景没有被修改。",
            )
        else:
            self._append_assistant_message(main_window, I18N.tr(
                "motion_graphic.ai.plan_cancelled",
                "The Motion Graphic edit plan was cancelled; the scene was not modified."))
        return True

    def _queue_motion_graphic_plan(
        self,
        main_window: Any,
        text: str,
        *,
        build_directly: bool = False,
        source_text: str | None = None,
    ) -> bool:
        """Start a Motion Graphic generation request.

        ``build_directly`` skips the optional A/B/C ideation stage after a
        user has finished a discussion and explicitly asks to generate.  The
        resulting scene is still presented on a confirmation card; only its
        click may write the timeline.
        """
        from .ai.brief import MotionGraphicBrief

        timeline_panel = getattr(main_window, "timeline_panel", None)
        if timeline_panel is None:
            raise RuntimeError("当前没有可用的时间线面板")
        view = timeline_panel.get_timeline_view()
        timeline = getattr(view, "_timeline", None)
        timeline_fps = getattr(timeline, "fps", None)
        if hasattr(timeline_fps, "num") and hasattr(timeline_fps, "den"):
            fps = float(timeline_fps.num) / float(timeline_fps.den or 1)
        else:
            try:
                fps = float(timeline_fps or getattr(timeline_panel, "_fps", 25.0) or 25.0)
            except (TypeError, ValueError):
                fps = 25.0
        # Motion Graphic source artwork is intentionally Full HD regardless
        # of the active timeline's preview dimensions. This keeps AI-created
        # clips deliverable at 1080p instead of inheriting a temporary 720p
        # or proxy-sized sequence canvas.
        timeline_width = self._MOTION_GRAPHIC_OUTPUT_WIDTH
        timeline_height = self._MOTION_GRAPHIC_OUTPUT_HEIGHT
        brief_text = str(source_text if source_text is not None else text or "").strip()
        seconds = self._requested_duration_seconds(brief_text)
        frame = int(timeline_panel.get_current_frame()) if hasattr(timeline_panel, "get_current_frame") else 0
        graphics_track = next(
            (
                track for track in getattr(timeline, "video_tracks", ())
                if getattr(track, "track_role", "") == "graphics"
            ),
            None,
        )
        variant = self._motion_graphic_generation_count % 3
        self._motion_graphic_generation_count += 1
        request_id = f"motion-graphic-{uuid.uuid4().hex}"

        # MG-01: construct a typed MotionGraphicBrief from user text instead
        # of a bare dict.  The brief carries canvas/duration/platform/language
        # so downstream code no longer hardcodes 1920×1080 (MG-11).
        brief = MotionGraphicBrief.from_user_text(
            brief_text,
            duration_seconds=seconds,
            fps=fps,
        )
        brief = replace(
            brief,
            width=timeline_width,
            height=timeline_height,
        )

        self._pending_motion_graphic_requests[request_id] = {
            "seconds": seconds,
            "fps": fps,
            "frame": max(0, frame),
            "has_timeline": timeline is not None,
            "track_id": getattr(graphics_track, "track_id", None),
            "variant": variant,
            "user_text": brief_text,
            "scene": None,  # LLM 生成后填充；None 时确认阶段走兜底
            "build_failed": False,  # LLM 失败标记
            "brief": brief,  # MG-01: typed brief
            # Direct generation has an explicit conversation checkpoint:
            # first show the model-authored plan, then wait for the user's
            # "可以" before asking the browser author to write HTML.
            # The plan is communication context, not a host-authored design
            # template or a post-generation quality gate.
            "phase": "plan_review" if build_directly else "proposals",
            "creative_brief_pending": build_directly,
            "plan_approved": False,
            # Motion Graphics are webpage-like by default.  Keep the old
            # command renderer as a compatibility fallback for lightweight
            # hosts/tests that do not expose the HTML chat transport.
            "render_mode": "html",
            "creative_proposals": [],  # list[CreativeProposal]
            "selected_proposal_id": None,  # str | None
            # User feedback entered while reviewing proposals/plans is carried
            # into the eventual HTML authoring call. It is request-scoped and
            # bounded by _normalize_feedback_notes, never reused globally.
            "generation_feedback_memory": (),
            # This flag becomes true only after the plan is approved and the
            # HTML draft has been staged. Timeline writes still require the
            # final confirmation-card button.
            "requires_button_confirmation": False,
            "allow_json_retry": True,
            # MG-12: project context fingerprint (for stale detection)
            "context_fingerprint": self._collect_context_fingerprint(
                timeline, graphics_track, frame,
            ),
        }
        # Show the actual control turn in chat.  ``brief_text`` may contain
        # several prior discussion turns and is used only as model context.
        self._append_user_message(main_window, text)

        if build_directly:
            self._append_assistant_message(
                main_window,
                "我先根据你的需求生成一份 Motion Graphic 制作计划；"
                "你可以继续修改细节，满意后发送“可以”或直接点击计划卡下方的“开始生成”。",
            )
            triggered = self._trigger_motion_graphic_creative_brief(main_window, request_id)
        else:
            # MG-04: users who explicitly ask for ideas still receive a
            # low-cost proposal stage before scene compilation.
            triggered = self._trigger_motion_graphic_creative_proposals(main_window, request_id)
        if not triggered:
            self._mark_motion_graphic_build_failed(
                main_window, request_id,
                I18N.tr("motion_graphic.ai.generation_channel_unavailable",
                        "The AI generation channel is not wired up or unavailable"),
            )
        return True

    def _trigger_motion_graphic_creative_brief(
        self, main_window: Any, request_id: str,
    ) -> bool:
        """Ask the model for the user-visible plan-review stage."""
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        controller = getattr(main_window, "_ai_edit_controller", None)
        starter = getattr(controller, "start_motion_graphic_build_chat", None)
        if not callable(starter):
            # Do not silently skip the user's review checkpoint. A host that
            # cannot expose the planning chat must report an unavailable
            # generation channel instead of jumping straight to HTML.
            return False
        set_prompt = getattr(
            controller, "set_teaching_motion_graphic_build_system_prompt", None,
        )
        if callable(set_prompt):
            set_prompt(self._assistant_plugin_prompt(
                "creative_brief", self._MOTION_GRAPHIC_CREATIVE_BRIEF_SYSTEM_PROMPT,
            ))
        canvas = self._brief_canvas_kwargs(plan)
        user_prompt = self._build_motion_graphic_creative_brief_user_prompt(
            str(plan.get("user_text", "")),
            seconds=float(plan["seconds"]), fps=float(plan["fps"]),
            revision_feedback=str(plan.get("creative_brief_revision_feedback", "")),
            **canvas,
        )
        # Publish the phase before entering the provider call.  The provider
        # can take tens of seconds (and may retry once); without this update
        # the assistant panel and status bar look idle during the entire
        # planning round.
        self._set_run_status(
            main_window, "正在构思制作计划",
            detail="模型正在分析主题、叙事节奏和视觉方向…",
        )
        try:
            try:
                return bool(starter(
                    request_id, user_prompt,
                    temperature=(0.68 if plan.get("creative_brief_retries", 0) else 0.82),
                ))
            except TypeError:
                # Compatibility with older lightweight controller doubles.
                return bool(starter(request_id, user_prompt))
        except Exception:  # noqa: BLE001
            logger.debug("Motion Graphic creative brief trigger failed", exc_info=True)
            return False

    def _trigger_motion_graphic_creative_proposals(
        self, main_window: Any, request_id: str,
    ) -> bool:
        """MG-04 phase 1: trigger the LLM to produce lightweight creative proposals.

        Swaps the controller's system prompt to the proposal prompt, then starts
        the same ``teaching_motion_graphic_build`` chat worker.  The response
        arrives via ``_handle_model_motion_graphic_build_response`` which
        dispatches by ``plan["phase"]``.
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        controller = getattr(main_window, "_ai_edit_controller", None)
        # Swap system prompt to the creative-proposal contract.
        set_prompt = getattr(controller, "set_teaching_motion_graphic_build_system_prompt", None)
        if callable(set_prompt):
            set_prompt(self._assistant_plugin_prompt(
                "proposal", self._MOTION_GRAPHIC_PROPOSAL_SYSTEM_PROMPT,
            ))
        starter = getattr(controller, "start_motion_graphic_build_chat", None)
        if not callable(starter):
            return False
        canvas = self._brief_canvas_kwargs(plan)
        user_prompt = self._build_motion_graphic_proposal_user_prompt(
            str(plan.get("user_text", "")),
            seconds=float(plan["seconds"]), fps=float(plan["fps"]),
            **canvas,
        )
        self._set_run_status(
            main_window, "正在构思创意方案",
            detail="模型正在比较叙事结构和视觉语言…",
        )
        try:
            return bool(starter(request_id, user_prompt))
        except Exception:  # noqa: BLE001
            logger.debug("Motion Graphic creative proposal trigger failed", exc_info=True)
            return False

    def _trigger_motion_graphic_llm_build(
        self, main_window: Any, request_id: str,
    ) -> bool:
        """触发 controller 的 LLM chat 通道生成网页级 Motion Graphic。

        默认使用 ``start_html_generate_chat`` 让模型直接设计完整 HTML/CSS
        演示网页；只有宿主完全未装配 HTML transport 时才使用旧的 scene-command
        JSON 通道。HTML 通道已装配但启动失败会直接上报失败（不再静默降级——
        那会把浏览器画布偷换成受限的 JSON 场景 DSL，用户拿到的设计会明显变差
        且毫无提示）。无论哪条通道，响应都必须先经过本模块的安全检查和确认卡。

        MG-04: 如果用户先选择了创意方案，user prompt 携带用户明确选择的方案；
        direct 模式只在用户确认可见制作计划后携带该计划作为创作上下文，网页仍由模型自主设计。

        Returns:
            True 表示已触发 LLM 通道（确认卡片将由响应 handler 延迟显示）；
            False 表示通道未装配或前置校验失败，调用方应走兜底。
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        controller = getattr(main_window, "_ai_edit_controller", None)
        if plan.get("render_mode") == "html":
            # Use the existing non-JSON HTML transport so the model can write
            # real CSS/HTML instead of escaping a whole document inside JSON.
            html_starter = getattr(controller, "start_html_generate_chat", None)
            if callable(html_starter):
                canvas = self._brief_canvas_kwargs(plan)
                user_prompt = self._build_motion_graphic_html_user_prompt(
                    str(plan.get("user_text", "")),
                    seconds=float(plan["seconds"]), fps=float(plan["fps"]),
                    # The author still owns the implementation and can make
                    # original design decisions, but it must receive the
                    # user-reviewed direction that the preceding plan stage
                    # produced. Otherwise a plan revision is only cosmetic.
                    proposal_context="",
                    creative_context="",
                    approved_plan_context=(
                        self._approved_motion_graphic_plan_context(plan)
                        if plan.get("plan_approved") else ""
                    ),
                    feedback_context=self._format_feedback_memory(
                        plan.get("generation_feedback_memory", ())
                    ),
                    revision_feedback=str(plan.get("html_revision_feedback", "")),
                    current_html=str(plan.get("html_edit_context", "")),
                    **canvas,
                )
                # Set the visible activity phase *before* the blocking model
                # request.  Previously this was published only after
                # ``start_html_generate_chat`` returned, leaving users with
                # a long, indistinguishable waiting vacuum while HTML/CSS was
                # being authored or automatically repaired.
                retry_no = int(plan.get("html_retries", 0) or 0)
                self._set_run_status(
                    main_window, "正在生成网页",
                    detail=(
                        "模型正在编写完整 HTML/CSS/JavaScript…"
                        if retry_no == 0 else
                        f"模型正在修复网页（第 {retry_no} 次）…"
                    ),
                )
                try:
                    started = bool(html_starter(
                        request_id,
                        user_prompt,
                        system_instruction=self._assistant_plugin_prompt(
                            "html_build", self._MOTION_GRAPHIC_HTML_BUILD_SYSTEM_PROMPT,
                        ),
                    ))
                except TypeError:
                    # Compatibility with a minimal fake/controller that only
                    # accepts the two positional arguments.
                    try:
                        started = bool(html_starter(request_id, user_prompt))
                    except Exception:  # noqa: BLE001
                        logger.debug("Motion Graphic HTML trigger failed", exc_info=True)
                        started = False
                except Exception:  # noqa: BLE001
                    logger.debug("Motion Graphic HTML trigger failed", exc_info=True)
                    started = False
                if started:
                    return True
                # The HTML transport exists but declined or failed to start
                # (most often no provider is configured).  Silently swapping
                # the browser canvas for the constrained legacy scene DSL would
                # hand the user a much weaker design without warning; surface
                # the channel failure instead (the caller marks the request
                # failed with a readable reason).
                return False
            # A host that does not expose the HTML transport at all (lightweight
            # embedders/test fakes) keeps the legacy JSON scene channel.
            plan["render_mode"] = "scene"
        # MG-04: restore the scene-build system prompt (proposal phase swapped it).
        set_prompt = getattr(controller, "set_teaching_motion_graphic_build_system_prompt", None)
        if callable(set_prompt):
            set_prompt(self._assistant_plugin_prompt(
                "legacy_scene", self._MOTION_GRAPHIC_BUILD_SYSTEM_PROMPT,
            ))
        starter = getattr(controller, "start_motion_graphic_build_chat", None)
        if not callable(starter):
            return False
        canvas = self._brief_canvas_kwargs(plan)
        # Include whichever internal/user-selected creative direction exists.
        proposal_context = self._build_proposal_context(plan)
        creative_context = self._build_creative_brief_context(plan)
        user_prompt = self._build_motion_graphic_build_user_prompt(
            str(plan.get("user_text", "")),
            seconds=float(plan["seconds"]), fps=float(plan["fps"]),
            proposal_context=proposal_context,
            creative_context=creative_context,
            **canvas,
        )
        try:
            return bool(starter(request_id, user_prompt))
        except Exception:  # noqa: BLE001
            logger.debug("Motion Graphic LLM build trigger failed", exc_info=True)
            return False

    @staticmethod
    def _build_proposal_context(plan: dict[str, Any]) -> str:
        """MG-04: format the selected creative proposal as build-prompt context.

        Returns an empty string when no proposal has been selected (backward
        compatibility with the pre-MG-04 single-call flow).
        """
        proposals: list = plan.get("creative_proposals") or []
        selected_id = plan.get("selected_proposal_id")
        if not proposals or not selected_id:
            return ""
        selected = next(
            (p for p in proposals if getattr(p, "proposal_id", "") == selected_id),
            None,
        )
        if selected is None:
            return ""
        parts: list[str] = [f"方案 {selected.title}"]
        if selected.summary:
            parts.append(f"概述：{selected.summary}")
        if selected.beats:
            beat_lines = [
                f"  - {b.label}（{b.start_seconds:g}-{b.start_seconds + b.duration_seconds:g}s）"
                f"：{b.on_screen_copy}"
                for b in selected.beats
            ]
            parts.append("段落文案：\n" + "\n".join(beat_lines))
        if selected.style.palette:
            palette_str = ", ".join(f"{k}={v}" for k, v in selected.style.palette.items())
            parts.append(f"配色：{palette_str}")
        sketch = dict(selected.element_sketch)
        if sketch.get("title"):
            parts.append(f"主标题：{sketch['title']}")
        cards = sketch.get("cards")
        if isinstance(cards, list) and cards:
            parts.append(f"内容卡片：{', '.join(str(c) for c in cards)}")
        return "\n".join(parts)

    @staticmethod
    def _build_creative_brief_context(plan: dict[str, Any]) -> str:
        """Serialize the approved model-authored plan for the author model."""
        brief = plan.get("creative_brief")
        if not isinstance(brief, dict):
            return ""
        # Keep the second call focused even if a provider returns unexpected
        # extra keys.  The brief is model-authored context, never executable
        # HTML or host commands.
        allowed = (
            "concept", "visual_grammar", "palette", "typography",
            "motion_language", "subject_motifs", "beats", "avoid",
        )
        compact = {key: brief[key] for key in allowed if key in brief}
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    def _show_motion_graphic_proposal_card(
        self, main_window: Any, request_id: str, *,
        variant: int, seconds: float, frame: int,
        graphics_track: Any, fallback: bool = False,
    ) -> None:
        """显示 Motion Graphic 确认卡片（LLM 成功或兜底后共用）。

        MG-02: 优先使用类型化卡片方法 ``set_motion_graphic_proposal_card``，
        传入结构化 card_data（元素/风险/质量/来源/hash）；面板不支持时回退到
        旧的纯文本方法。
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return
        title_suffix = "（兜底版式）" if fallback else ""
        render_mode = str(plan.get("render_mode", "scene"))
        source_label = "html_css" if render_mode == "html" and not fallback else (
            "llm" if not fallback else "fallback_provider_unavailable"
        )

        # MG-02: build structured card data for the typed panel method.
        scene = plan.get("scene")
        canvas = self._brief_canvas_kwargs(plan)
        card_data: dict[str, Any] = {
            "title": f"Motion Graphic Clip{title_suffix}",
            "summary": (
                "LLM 已生成网页级 HTML/CSS 演示页面，可按帧自动切换"
                if render_mode == "html" and not fallback
                else ("LLM 已根据你的描述生成场景" if not fallback
                      else "LLM 不可用，生成失败")
            ),
            "render_mode": render_mode,
            "source_label": source_label,
            "is_fallback": fallback,
            "duration_seconds": seconds,
            "canvas_size": [canvas["width"], canvas["height"]],
            "target_track_label": getattr(graphics_track, "name", None)
                                  or "Graphics",
            "target_insert_frame": frame,
            "elements": [],
            "risks": [],
            "quality_warnings": [],
            "quality_errors": [],
            "actions": ["continue_editing", "cancel", "confirm"],
        }
        if scene is not None:
            card_data["elements"] = [
                {"element_id": el.element_id, "kind": el.type,
                 "description": str(el.content)[:60]}
                for el in scene.elements[:12]
            ]
            card_data["plan_hash"] = getattr(scene, "scene_id", "")[:16]
            if render_mode == "html" and getattr(scene, "html_layer", None) is not None:
                from .core.html_page_deck import detect_page_count

                card_data["page_count"] = detect_page_count(
                    getattr(scene.html_layer, "sanitized_html", "")
                )
                outcome = plan.get("html_outcome")
                if outcome is not None:
                    card_data["render_validation"] = {
                        "status": "passed",
                        "page_count": card_data["page_count"],
                        "detail": "首帧与每一页均已通过渲染可见性检查",
                    }
        creative_brief = plan.get("creative_brief")
        if isinstance(creative_brief, dict):
            motifs = creative_brief.get("subject_motifs")
            if not isinstance(motifs, (list, tuple)):
                motifs = ()
            card_data["creative_direction"] = {
                "concept": str(creative_brief.get("concept", "") or "")[:160],
                "subject_motifs": [
                    str(item)[:80]
                    for item in motifs[:8]
                ],
            }
        # MG-10: surface design validator findings on the card.
        card_data["quality_errors"] = plan.get("design_errors", [])
        card_data["quality_warnings"] = [
            *list(plan.get("design_warnings", []) or ()),
            *list(plan.get("html_design_warnings", []) or ()),
        ]
        html_design_quality = plan.get("html_design_quality")
        if isinstance(html_design_quality, Mapping):
            card_data["html_design_quality"] = dict(html_design_quality)
        html_intent_checks = plan.get("html_intent_checks")
        if isinstance(html_intent_checks, Mapping):
            card_data["html_intent_checks"] = dict(html_intent_checks)
        # MG-03: surface storyboard preview frames on the card.
        storyboard = plan.get("storyboard")
        if storyboard:
            card_data["storyboard"] = storyboard
        # MG-18: surface quality gate report on the card.
        quality_report = plan.get("quality_report")
        if quality_report:
            card_data["quality_report"] = quality_report

        # MG-06: stamp the scene hash so confirm can verify the user is
        # confirming exactly what they saw.  If the scene changes after the
        # card was shown, confirm will refuse with a stale error.
        if scene is not None:
            import hashlib as _hashlib
            import json as _json
            scene_payload = _json.dumps(
                scene.to_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            )
            plan["approved_scene_hash"] = _hashlib.sha256(
                scene_payload.encode("utf-8")
            ).hexdigest()

        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        # MG-02: prefer typed card method.
        show_card = getattr(panel, "set_motion_graphic_proposal_card", None)
        if callable(show_card):
            show_card(request_id, card_data)
            return
        # Fallback to old text method for panels that don't support the typed card.
        show_plan = getattr(panel, "set_motion_graphic_proposal", None)
        if not callable(show_plan):
            self._pending_motion_graphic_requests.pop(request_id, None)
            raise RuntimeError("AI Assistant 不支持 Motion Graphic 计划确认")
        source_text = "LLM 已根据你的描述生成场景" if not fallback else "LLM 不可用，使用内置版式"
        details = (
            f"时长：{seconds:g} 秒 · 插入播放头：{frame} 帧\n"
            f"内容：{source_text}\n"
            f"视觉方案：方案 {variant + 1}{title_suffix}\n"
            f"轨道：{getattr(graphics_track, 'name', None) or '自动使用/创建 Graphics 轨道'}\n"
            "步骤：创建共享场景 → 插入 Motion Graphic Clip → 打开编辑器 → 验证首帧和场景引用\n"
            "这是最终执行确认；点击确认执行后才会修改时间线。"
        )
        show_plan(request_id, f"Motion Graphic Clip{title_suffix}", details)

    def _show_motion_graphic_failure_card(
        self, main_window: Any, request_id: str, *,
        seconds: float, frame: int, reason: str,
    ) -> None:
        """MG-05: 显示生成失败卡片（不再冒充定制结果）。

        用户可以选择重试、修改需求或显式选择内置模板。
        只有用户点击"改用内置模板"后才会创建模板场景。
        """
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        # MG-02: prefer typed card with explicit failure state.
        show_card = getattr(panel, "set_motion_graphic_proposal_card", None)
        if callable(show_card):
            canvas = self._brief_canvas_kwargs(
                self._pending_motion_graphic_requests.get(request_id, {})
            )
            show_card(request_id, {
                "title": "Motion Graphic 生成未完成",
                "summary": f"生成失败：{reason}",
                "source_label": "html_render_validation_failed",
                "is_fallback": False,
                "duration_seconds": seconds,
                "canvas_size": [canvas["width"], canvas["height"]],
                "target_insert_frame": frame,
                "quality_errors": [reason],
                # Do not offer or invoke a host-authored template.  The
                # browser authoring path must stay model-designed; the user
                # can modify the request and trigger another model attempt.
                "actions": ["cancel"],
            })
            return
        # Fallback to old text method.
        details = (
            f"生成失败：{reason}\n\n"
            f"时长：{seconds:g} 秒 · 插入播放头：{frame} 帧\n"
            "你可以：\n"
            "- 重新描述需求后再次发起\n"
            "- 或继续修改当前需求后重新生成\n"
            "系统不会替换为内置模板，也不会修改时间线。"
        )
        show_plan = getattr(panel, "set_motion_graphic_proposal", None)
        if not callable(show_plan):
            self._pending_motion_graphic_requests.pop(request_id, None)
            return
        show_plan(request_id, "Motion Graphic 生成未完成", details)

    @staticmethod
    def _parse_motion_graphic_json_response(response: str) -> dict[str, Any]:
        """Parse a model scene response while tolerating harmless JSON wrapping.

        JSON mode is usually strict, but providers occasionally add a code
        fence, a short preamble, or a trailing comma around a valid scene.  We
        repair only those transport-level details; scene semantics still go
        through ``_build_scene_from_commands`` and its full typed validation.
        """
        raw = str(response or "").strip()
        if raw.startswith("```"):
            raw = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", raw,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
        candidates = [raw]
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            candidates.append(raw[start:end + 1])
        for candidate in candidates:
            for strict in (True, False):
                try:
                    payload = json.loads(candidate, strict=strict)
                    if isinstance(payload, dict):
                        return payload
                except json.JSONDecodeError:
                    pass
            # Trailing commas are a common model formatting slip and are safe
            # to remove because they cannot alter string contents.
            repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                payload = json.loads(repaired, strict=False)
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                pass
        raise ValueError("LLM 返回不是合法 JSON 对象")

    def _handle_model_motion_graphic_build_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
        """LLM 回复到达 → 按 plan phase 分派。

        MG-04: ``plan["phase"]`` 决定处理路径：
        - ``"proposals"``: 创意方案阶段 — 解析 1-3 个文本提案并显示选择卡。
        - ``creative_brief_pending``: 制作计划阶段 — 解析计划并显示审核卡。
        - ``"build"`` (默认): 场景编译阶段 — 解析场景 JSON 并显示确认卡。

        失败时把 plan 标记为 ``build_failed``，显示失败卡片（MG-05）。
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        from .ai.html_llm_generator import strip_markdown_fence
        normalized_response = strip_markdown_fence(str(response or ""))
        # The planner is an authoritative state boundary.  It must finish a
        # visible, user-reviewable plan before any HTML response can be
        # treated as a renderable draft.  Some providers occasionally ignore
        # the JSON planner instruction and return HTML; accepting that here
        # would bypass the plan card and reproduce the premature execution
        # confirmation seen in the UI.
        if plan.get("creative_brief_pending"):
            return self._handle_model_motion_graphic_creative_brief_response(
                main_window, request_id, response,
            )
        if plan.get("render_mode") == "html" and normalized_response.lower().startswith(("<!doctype", "<html")):
            return self._handle_model_motion_graphic_html_response(
                main_window, request_id, normalized_response,
            )
        # MG-04: dispatch by phase
        if plan.get("phase") == "proposals":
            return self._handle_model_creative_proposal_response(
                main_window, request_id, response,
            )
        try:
            payload = self._parse_motion_graphic_json_response(response)
        except ValueError:
            # One automatic retry is useful for a real provider response that
            # was cut off while emitting a rich SVG/gradient scene.  Test
            # fakes without a controller still fall through to the explicit
            # failure card, so this never hides a genuine unavailable channel.
            retries = int(plan.get("json_retries", 0) or 0)
            if retries < 1 and plan.get("allow_json_retry", False):
                plan["json_retries"] = retries + 1
                self._append_assistant_message(
                    main_window,
                    "模型返回的设计 JSON 不完整，正在自动重试一次；不会修改时间线。",
                )
                if self._trigger_motion_graphic_llm_build(main_window, request_id):
                    return True
            self._mark_motion_graphic_build_failed(main_window, request_id, "LLM 返回不是合法 JSON")
            return False
        try:
            scene = self._build_scene_from_commands(
                payload, seconds=float(plan["seconds"]), fps=float(plan["fps"]),
                **self._brief_canvas_kwargs(plan),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Motion Graphic LLM scene build rejected", exc_info=True)
            self._mark_motion_graphic_build_failed(main_window, request_id, f"场景构造失败：{exc}")
            return False

        # 渲染首帧验证（与 confirm 阶段对称），避免把不可渲染的场景推给确认卡。
        try:
            from .scene_store import get_shared_scene_store

            # Preview the freshly compiled immutable scene through the explicit
            # local mapping first. The shared store is updated only after this
            # gate succeeds; this prevents failed previews from leaking a
            # half-validated scene into the project.
            rendered, validation_frame = self._validate_motion_graphic_scene(scene)
            if not rendered.data or rendered.width <= 0 or rendered.height <= 0:
                raise RuntimeError(f"渲染验证帧 {validation_frame} 为空")
            # Storyboard rendering and the eventual timeline command resolve
            # through the shared scene store, so publish only the validated
            # scene before those steps and before showing the card.
            get_shared_scene_store().register(scene)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Motion Graphic LLM scene render validation failed", exc_info=True)
            self._mark_motion_graphic_build_failed(main_window, request_id, f"首帧验证失败：{exc}")
            return False

        plan["scene"] = scene

        # MG-03: render storyboard frames for the confirmation card so the
        # user can see first/middle/last stills before approving.
        storyboard = self._render_storyboard_preview(scene)
        if storyboard is not None:
            plan["storyboard"] = storyboard

        # MG-10: run the design validator on the LLM-produced scene before
        # showing the confirmation card.  Errors are surfaced as quality
        # findings on the card; blocking errors prevent auto-confirm.
        quality_warnings: list[str] = []
        quality_errors: list[str] = []
        try:
            from .ai.design_validator import validate_scene_design
            brief = plan.get("brief")
            expected_frames = brief.duration_frames() if brief else None
            report = validate_scene_design(scene, expected_duration_frames=expected_frames)
            quality_errors = [f.message for f in report.errors]
            quality_warnings = [f.message for f in report.warnings]
            plan["design_errors"] = quality_errors
            plan["design_warnings"] = quality_warnings
        except Exception:  # noqa: BLE001
            logger.debug("Design validator failed to run", exc_info=True)

        # MG-18: run the Motion Graphic quality gate, feeding the storyboard
        # render results and design validator output.  The report is surfaced
        # on the confirmation card so the user sees deliverability status.
        try:
            mg_report = self._run_quality_gate(
                scene, storyboard, quality_errors, quality_warnings, plan,
            )
            if mg_report is not None:
                plan["quality_report"] = mg_report
        except Exception:  # noqa: BLE001
            logger.debug("MG quality gate failed to run", exc_info=True)

        # OS-05: record the scene + design validation as an evidence record
        # so downstream agents can cite what was checked.
        try:
            from src.features.edit.ai_editing.evidence_kinds import EvidenceRecord
            import time as _time
            evidence = EvidenceRecord(
                evidence_id=f"mg-scene-{scene.scene_id[:12]}",
                kind="quality",
                asset_id=str(scene.scene_id),
                source_fingerprint=plan.get("context_fingerprint", ""),
                confidence=1.0 if not quality_errors else 0.5,
                payload={
                    "element_count": len(scene.elements),
                    "design_errors": quality_errors,
                    "design_warnings": quality_warnings,
                    "storyboard_frames": len(storyboard) if storyboard else 0,
                },
                summary=f"MG scene with {len(scene.elements)} elements",
                created_at=_time.time(),
            )
            plan["evidence"] = evidence.to_dict()
        except Exception:  # noqa: BLE001
            logger.debug("Evidence recording failed", exc_info=True)

        self._append_assistant_message(
            main_window,
            f"已根据你的描述生成场景草案（{len(scene.elements)} 个元素），"
            + (f"；{len(quality_errors)} 个设计问题需修正" if quality_errors else "")
            + "，请在确认卡片中查看。确认后会插入时间线并打开编辑器。",
        )
        self._show_motion_graphic_proposal_card(
            main_window, request_id,
            variant=int(plan.get("variant", 0)),
            seconds=float(plan["seconds"]),
            frame=int(plan["frame"]),
            graphics_track=None,  # 已在 plan 里存好 track_id，display 用 None 即可
            fallback=False,
        )
        return True

    def _handle_model_motion_graphic_creative_brief_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
        """Accept the model plan and pause for visible user approval."""
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        try:
            payload = self._parse_motion_graphic_json_response(response)
        except ValueError:
            if self._retry_motion_graphic_creative_brief(
                main_window, request_id,
                "输出必须是完整 JSON；字符串中的双引号、反斜杠和换行必须正确转义，"
                "不要在字符串中嵌入未转义的 HTML 标签。",
            ):
                return True
            self._mark_motion_graphic_build_failed(
                main_window, request_id, "AI 制作计划不是合法 JSON",
            )
            return False
        concept = str(payload.get("concept", "") or "").strip()
        visual_grammar = str(payload.get("visual_grammar", "") or "").strip()
        beats = payload.get("beats")
        if not concept or not visual_grammar or not isinstance(beats, list) or not beats:
            if self._retry_motion_graphic_creative_brief(
                main_window, request_id,
                "必须同时提供非空的 concept、visual_grammar 和 beats 数组。",
            ):
                return True
            self._mark_motion_graphic_build_failed(
                main_window, request_id, "AI 制作计划缺少核心创意、视觉方向或叙事结构",
            )
            return False
        plan["creative_brief"] = payload
        plan["phase"] = "plan_review"
        plan["creative_brief_pending"] = False
        plan["plan_approved"] = False
        plan["requires_button_confirmation"] = False
        plan["build_failed"] = False
        plan["creative_brief_revision_feedback"] = ""
        plan["creative_brief_retries"] = 0
        self._append_assistant_message(
            main_window,
            self._format_motion_graphic_plan_review(plan),
        )
        self._set_run_status(
            main_window, "制作计划待确认",
            detail="请审核创意方向；确认前不会生成网页或修改时间线",
            state="waiting",
        )
        self._show_motion_graphic_plan_review_card(main_window, request_id)
        return True

    @classmethod
    def _motion_graphic_plan_rows(cls, plan: dict[str, Any]) -> list[dict[str, str]]:
        """Convert the model plan into compact, user-facing review rows."""
        brief = plan.get("brief")
        canvas = (
            f"{getattr(brief, 'width', 1920)}×{getattr(brief, 'height', 1080)}"
            if brief is not None else "当前时间线画布"
        )
        creative = plan.get("creative_brief")
        if not isinstance(creative, dict):
            return [{"label": "画布", "value": canvas}]

        rows: list[dict[str, str]] = [
            {"label": "核心创意", "value": str(creative.get("concept", "") or "")},
            {"label": "视觉方向", "value": str(creative.get("visual_grammar", "") or "")},
        ]
        motifs = creative.get("subject_motifs")
        if isinstance(motifs, (list, tuple)) and motifs:
            rows.append({"label": "主题母题", "value": "、".join(str(item) for item in motifs[:8])})
        typography = str(creative.get("typography", "") or "").strip()
        if typography:
            rows.append({"label": "排版气质", "value": typography})
        motion = str(creative.get("motion_language", "") or "").strip()
        if motion:
            rows.append({"label": "动效节奏", "value": motion})
        beats = creative.get("beats")
        if isinstance(beats, list) and beats:
            beat_parts: list[str] = []
            for index, beat in enumerate(beats[:8], start=1):
                if not isinstance(beat, dict):
                    continue
                label = str(beat.get("label", f"段落 {index}") or f"段落 {index}")
                copy = str(beat.get("copy", "") or beat.get("purpose", "") or "").strip()
                beat_parts.append(f"{label}：{copy}" if copy else label)
            if beat_parts:
                rows.append({"label": "叙事结构", "value": "\n".join(beat_parts)})
        palette = creative.get("palette")
        if isinstance(palette, (list, tuple)) and palette:
            rows.append({"label": "色彩方向", "value": "、".join(str(item) for item in palette[:8])})
        avoid = creative.get("avoid")
        if isinstance(avoid, (list, tuple)) and avoid:
            rows.append({"label": "主动避开", "value": "、".join(str(item) for item in avoid[:6])})
        feedback_notes = cls._normalize_feedback_notes(
            plan.get("generation_feedback_memory", ())
        )
        if feedback_notes:
            rows.append({"label": "已采纳反馈", "value": "；".join(feedback_notes)})
        rows.insert(0, {"label": "画布", "value": canvas})
        return [row for row in rows if row["value"].strip()]

    @classmethod
    def _approved_motion_graphic_plan_context(cls, plan: Mapping[str, Any]) -> str:
        """Serialize the user-reviewed plan into a bounded author hand-off.

        The plan is model-authored but user-approved, so it is creative context
        rather than a host template. Retain only compact review rows and bound
        each value so a malformed plan cannot crowd out the user's current
        request in the final HTML call.
        """
        rows = cls._motion_graphic_plan_rows(dict(plan))
        lines: list[str] = []
        for row in rows:
            label = str(row.get("label", "") or "").strip()
            if not label or label == "画布":
                continue
            value = str(row.get("value", "") or "").strip().replace("\n", "；")
            if value:
                lines.append(f"- {label}：{value[:320]}")
        return "\n".join(lines)[:1800]

    @classmethod
    def _format_motion_graphic_plan_review(cls, plan: dict[str, Any]) -> str:
        """Format the review checkpoint as a readable chat message."""
        rows = cls._motion_graphic_plan_rows(plan)
        seconds = float(plan.get("seconds", 0.0) or 0.0)
        lines = [
            "AI Motion Graphic 制作计划（待确认）",
            "",
            "| 项目 | 计划 |",
            "| --- | --- |",
        ]
        for row in rows:
            value = row["value"].replace("\n", "；")
            lines.append(f"| {row['label']} | {value} |")
        lines.extend([
            f"| 时长 | {seconds:g} 秒；网页页面由 AI 自由决定并自动切换 |",
            "",
            "这是沟通计划，不会限制网页设计，也不会修改时间线。满意后发送“可以”；"
            "有任何问题，直接继续告诉我需要调整的内容。",
        ])
        return "\n".join(lines)

    def _show_motion_graphic_plan_review_card(
        self, main_window: Any, request_id: str, *,
        generation_confirmation: bool = False,
    ) -> None:
        """Show the plan card or the follow-up generation confirmation card."""
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return
        canvas = self._brief_canvas_kwargs(plan)
        mode = "generation_confirmation" if generation_confirmation else "plan_review"
        card_data: dict[str, Any] = {
            "title": (
                "确认生成 Motion Graphic"
                if generation_confirmation else "Motion Graphic 制作计划"
            ),
            "summary": (
                "制作计划已确认，点击“确认生成”后才会调用 AI 生成网页。"
                if generation_confirmation
                else "先审核内容和创作方向；确认后才会进入生成确认，不会提前修改时间线。"
            ),
            "source_label": "llm_plan",
            "render_mode": "html",
            "duration_seconds": float(plan.get("seconds", 0.0) or 0.0),
            "canvas_size": [canvas["width"], canvas["height"]],
            "target_insert_frame": int(plan.get("frame", 0) or 0),
            "mode": mode,
            "plan_rows": self._motion_graphic_plan_rows(plan),
            "quality_errors": [],
            "quality_warnings": [],
            "actions": (
                ["cancel", "generate"]
                if generation_confirmation else ["cancel", "start"]
            ),
        }
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        show_card = getattr(panel, "set_motion_graphic_proposal_card", None)
        if callable(show_card):
            show_card(request_id, card_data)
            return
        show_plan = getattr(panel, "set_motion_graphic_proposal", None)
        if callable(show_plan):
            details = self._format_motion_graphic_plan_review(plan)
            try:
                show_plan(
                    request_id, card_data["title"], details,
                    show_confirm=generation_confirmation,
                )
            except TypeError:
                # Compatibility with older lightweight panel doubles.
                show_plan(request_id, card_data["title"], details)

    def _show_motion_graphic_generation_confirmation_card(
        self, main_window: Any, request_id: str,
    ) -> None:
        """Show the explicit pre-generation confirmation requested by the user."""
        self._show_motion_graphic_plan_review_card(
            main_window, request_id, generation_confirmation=True,
        )

    # ------------------------------------------------------------------ #
    # MG-03: Storyboard preview
    # ------------------------------------------------------------------ #

    @staticmethod
    def _storyboard_label(idx: int, total: int) -> str:
        """Human-readable label for a storyboard frame index."""
        if idx == 0:
            return "first_frame"
        if total > 0 and idx >= total - 1:
            return "last_frame"
        if total > 2 and abs(idx - total // 2) <= 1:
            return "mid_frame"
        return f"frame_{idx}"

    def _render_storyboard_preview(
        self, scene: Any, *, count: int = 5,
    ) -> list[dict[str, Any]] | None:
        """MG-03: render storyboard stills at 640x360 for the confirmation card.

        Samples ``count`` frames across the scene duration, renders each at
        storyboard resolution, and returns a list of frame summaries.  Returns
        ``None`` if *every* frame fails to render (graceful degradation — the
        card still shows without a preview).
        """
        from .ai.preview import RenderProfile, sample_storyboard_frames

        try:
            frame_indices = sample_storyboard_frames(scene, count=count)
        except Exception:  # noqa: BLE001
            logger.debug("Storyboard frame sampling failed", exc_info=True)
            return None

        profile = RenderProfile(
            width=640, height=360,
            fps=float(scene.fps),
            tier="storyboard",
            transparent=True,
        )
        frames: list[dict[str, Any]] = []
        has_success = False
        for idx in frame_indices:
            info: dict[str, Any] = {
                "frame_index": int(idx),
                "label": self._storyboard_label(int(idx), scene.duration_frames),
                "time_seconds": round(idx / max(1, scene.fps), 2),
                "render_error": None,
            }
            try:
                from .core.contracts import RenderContext
                from .host_adapters import render_teaching_scene_frame

                rendered = render_teaching_scene_frame(
                    scene.scene_id, int(idx),
                    context=RenderContext(
                        fps=scene.fps,
                        width=profile.width,
                        height=profile.height,
                        transparent=True,
                    ),
                )
                if rendered.data and rendered.width > 0 and rendered.height > 0:
                    info["width"] = rendered.width
                    info["height"] = rendered.height
                    has_success = True
                else:
                    info["render_error"] = "rendered frame is empty"
            except Exception as exc:  # noqa: BLE001
                info["render_error"] = str(exc)[:120]
            frames.append(info)

        return frames if has_success else None

    @staticmethod
    def _run_quality_gate(
        scene: Any,
        storyboard: list[dict[str, Any]] | None,
        design_errors: list[str],
        design_warnings: list[str],
        plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        """MG-18: run the MotionGraphicQualityGate over collected artifacts.

        Converts storyboard frame results into ``RenderFrameResult`` objects,
        feeds them into the gate alongside the scene, and returns the report
        as a dict suitable for the confirmation card.
        """
        from .ai.quality_gate import (
            MotionGraphicQualityGate,
            QualityGateInputs,
            RenderFrameResult,
        )

        render_frames: list[RenderFrameResult] = []
        if storyboard:
            # Map storyboard labels to the quality gate's expected labels.
            label_map = {
                "first_frame": "first",
                "mid_frame": "middle",
                "last_frame": "last",
            }
            for frame_info in storyboard:
                raw_label = str(frame_info.get("label", "frame"))
                gate_label = label_map.get(raw_label, raw_label)
                render_frames.append(RenderFrameResult(
                    label=gate_label,
                    frame_index=int(frame_info.get("frame_index", 0)),
                    succeeded=frame_info.get("render_error") is None,
                    error=frame_info.get("render_error"),
                    is_blank=False,
                ))

        brief = plan.get("brief")
        expected_frames = brief.duration_frames() if brief else None

        inputs = QualityGateInputs(
            scene=scene,
            render_frames=tuple(render_frames),
            expected_duration_frames=expected_frames,
        )
        gate = MotionGraphicQualityGate()
        report = gate.run(inputs)
        return report.to_dict()

    # ------------------------------------------------------------------ #
    # MG-04: Creative proposal handling
    # ------------------------------------------------------------------ #

    def _handle_model_creative_proposal_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
        """MG-04 phase 1 response: parse 1-3 creative proposals.

        The LLM outputs a JSON object ``{"proposals": [...]}``.  Each item is
        parsed into a :class:`CreativeProposal` and stored on the plan.  A
        selection card is then shown so the user can pick one (or ask for a
        revision) before any scene commands are compiled.
        """
        from .ai.proposal import CreativeProposal, NarrativeBeat, StyleTokens

        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        raw = str(response or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                self._mark_motion_graphic_build_failed(main_window, request_id, "创意方案不是合法 JSON")
                return False
            try:
                payload = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                self._mark_motion_graphic_build_failed(main_window, request_id, "创意方案不是合法 JSON")
                return False

        proposals_raw = payload.get("proposals") if isinstance(payload, dict) else None
        if not isinstance(proposals_raw, list) or not proposals_raw:
            self._mark_motion_graphic_build_failed(main_window, request_id, "创意方案缺少 proposals 数组")
            return False

        brief = plan.get("brief")
        brief_id = getattr(brief, "brief_id", "")
        brief_revision = getattr(brief, "revision", 1)
        brief_hash = getattr(brief, "brief_hash", "") or ""

        proposals: list[CreativeProposal] = []
        for idx, item in enumerate(proposals_raw[:3]):
            if not isinstance(item, dict):
                continue
            pid = str(item.get("proposal_id", "")) or chr(ord("A") + idx)
            try:
                beats = tuple(
                    NarrativeBeat.from_dict(b) for b in item.get("beats", [])
                    if isinstance(b, dict)
                )
                style_data = item.get("style", {})
                style = StyleTokens.from_dict(style_data) if isinstance(style_data, dict) else StyleTokens()
                proposal = CreativeProposal(
                    proposal_id=pid,
                    brief_id=brief_id,
                    brief_revision=brief_revision,
                    brief_hash=brief_hash,
                    title=str(item.get("title", f"方案 {pid}")),
                    summary=str(item.get("summary", "")),
                    beats=beats,
                    style=style,
                    element_sketch=dict(item.get("element_sketch", {})),
                    estimated_risks=tuple(str(r) for r in item.get("estimated_risks", [])),
                    source="llm",
                    rationale=str(item.get("rationale", "")),
                )
                proposals.append(proposal)
            except (ValueError, TypeError):
                logger.debug("Failed to parse creative proposal %s", pid, exc_info=True)
                continue

        if not proposals:
            self._mark_motion_graphic_build_failed(main_window, request_id, "无法解析任何创意方案")
            return False

        plan["creative_proposals"] = proposals
        plan["selected_proposal_id"] = None
        # Stay in "proposals" phase until the user picks one.
        self._append_assistant_message(
            main_window,
            f"已生成 {len(proposals)} 个创意方案，请在下方卡片中选择一个"
            "（输入\"方案A/B/C\"或点击对应按钮），或继续描述你想要的修改。",
        )
        self._set_run_status(
            main_window, "创意方案待选择",
            detail="请选择一个方向，或继续描述需要调整的内容",
            state="waiting",
        )
        self._show_creative_proposal_selection_card(main_window, request_id)
        return True

    def _show_creative_proposal_selection_card(
        self, main_window: Any, request_id: str,
    ) -> None:
        """MG-04: render a card listing creative proposals for user selection.

        Uses the typed ``set_motion_graphic_proposal_card`` panel method with
        ``card_data["mode"] = "proposal_selection"`` so the panel can render
        per-proposal selection buttons.  Falls back to the legacy text card.
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return
        proposals: list = plan.get("creative_proposals") or []
        canvas = self._brief_canvas_kwargs(plan)

        # Build compact proposal summaries for the card.
        proposal_items: list[dict[str, Any]] = []
        for p in proposals:
            beats_summary = "; ".join(
                f"{b.label}:{b.on_screen_copy}" for b in p.beats[:4]
            )[:120]
            palette = ""
            if p.style.palette:
                palette = ", ".join(f"{k}={v}" for k, v in list(p.style.palette.items())[:3])
            proposal_items.append({
                "proposal_id": p.proposal_id,
                "title": p.title,
                "summary": p.summary,
                "beats_preview": beats_summary,
                "palette": palette,
                "rationale": p.rationale[:80] if p.rationale else "",
            })

        card_data: dict[str, Any] = {
            "title": "Motion Graphic 创意方案",
            "summary": f"已生成 {len(proposals)} 个方案，请选择一个继续",
            "source_label": "llm",
            "is_fallback": False,
            "duration_seconds": float(plan["seconds"]),
            "canvas_size": [canvas["width"], canvas["height"]],
            "target_insert_frame": int(plan["frame"]),
            "mode": "proposal_selection",
            "proposals": proposal_items,
            "elements": [],
            "risks": [],
            "quality_warnings": [],
            "quality_errors": [],
            "actions": ["cancel"],
        }

        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        show_card = getattr(panel, "set_motion_graphic_proposal_card", None)
        if callable(show_card):
            show_card(request_id, card_data)
            return
        # Fallback: legacy text card
        text_lines = [card_data["summary"]]
        for item in proposal_items:
            text_lines.append(
                f"\n方案 {item['proposal_id']}：{item['title']}\n"
                f"  {item['summary']}\n"
                f"  文案预览：{item['beats_preview']}"
            )
        text_lines.append("\n请输入\"方案A/B/C\"选择，或描述你想要的修改。")
        show_plan = getattr(panel, "set_motion_graphic_proposal", None)
        if callable(show_plan):
            show_plan(request_id, card_data["title"], "\n".join(text_lines))

    def _select_creative_proposal(
        self, main_window: Any, request_id: str, proposal_id: str,
    ) -> bool:
        """MG-04 phase 2: user has chosen a creative proposal.

        Records the selection on the plan, transitions to ``"build"`` phase,
        and triggers the scene-compilation LLM call with the proposal's
        narrative/style context in the prompt.
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return False
        proposals: list = plan.get("creative_proposals") or []
        match = next(
            (p for p in proposals if getattr(p, "proposal_id", "").upper() == proposal_id.upper()),
            None,
        )
        if match is None:
            return False
        plan["selected_proposal_id"] = match.proposal_id
        plan["phase"] = "build"
        # Clear the selection card
        panel = getattr(main_window, "ai_edit_assistant_panel", None)
        clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
        if callable(clear_card):
            clear_card(request_id)
        self._append_assistant_message(
            main_window,
            f"已选择方案 {match.proposal_id}：{match.title}。正在编译为网页级 Motion Graphic...",
        )
        # Trigger scene build with the selected proposal as context.
        triggered = self._trigger_motion_graphic_llm_build(main_window, request_id)
        if not triggered:
            self._mark_motion_graphic_build_failed(
                main_window, request_id, "AI 编译通道未装配或不可用",
            )
        return True

    def _mark_motion_graphic_build_failed(
        self, main_window: Any, request_id: str, reason: str,
    ) -> None:
        """LLM 场景构建失败 → 标记 plan，告知用户，显示失败卡片（MG-05）。

        MG-05 改变：不再自动显示兜底样板卡片冒充定制结果。
        改为显示明确的失败卡片，用户可选择重试、修改需求或显式选择模板。
        """
        plan = self._pending_motion_graphic_requests.get(request_id)
        if plan is None:
            return
        # A failed repair call bubbles through its earlier synchronous callers.
        # Only the innermost failure should produce one message/card; without
        # this guard each exhausted retry was reported repeatedly.
        if plan.get("_failure_reported"):
            return
        plan["_failure_reported"] = True
        plan["build_failed"] = True
        plan["scene"] = None
        logger.info("Motion Graphic LLM build failed (%s)", reason)
        self._set_run_status(
            main_window, "生成失败", detail=str(reason)[:120], state="failed",
        )
        self._append_assistant_message(
            main_window,
            I18N.tr("motion_graphic.ai.llm_build_failed",
                    "AI scene generation did not complete ({reason}). Retry, adjust the "
                    "description, or choose 'use a built-in template'.", reason=reason),
            error_code="motion_graphic_llm_build_failed",
        )
        # MG-05: show a failure card (not a fallback template disguised as
        # custom).  The card clearly states generation failed.
        self._show_motion_graphic_failure_card(
            main_window, request_id,
            seconds=float(plan["seconds"]),
            frame=int(plan["frame"]),
            reason=reason,
        )

    # ------------------------------------------------------------------ #
    # OS-07: Structured feedback recording
    # ------------------------------------------------------------------ #

    _FEEDBACK_KEYWORDS: dict[str, tuple[str, ...]] = {}  # filled below

    @classmethod
    def _ensure_feedback_store(cls) -> Any:
        """Lazily create the FeedbackStore (avoids import at module load)."""
        if cls._feedback_store is not None if hasattr(cls, "_feedback_store") else False:
            return cls._feedback_store
        try:
            from src.features.edit.ai_editing.feedback_store import FeedbackStore
            return FeedbackStore()
        except ImportError:
            return None

    @staticmethod
    def _categorize_feedback(text: str) -> tuple[str, ...]:
        """OS-07: map user text to structured feedback categories.

        Uses keyword matching to classify which aspects the user is
        commenting on. Returns a tuple of category strings from
        ``SUPPORTED_FEEDBACK_CATEGORIES``.
        """
        lowered = str(text or "").lower()
        cats: set[str] = set()
        keyword_map = {
            "pacing": ("节奏", "速度", "太快", "太慢", "快一点", "慢一点", "pacing", "speed"),
            "copy": ("文案", "文字", "标题", "字幕", "内容", "copy", "text"),
            "color": ("颜色", "配色", "色调", "色彩", "蓝", "红", "绿", "color"),
            "audio": ("音乐", "音效", "配音", "声音", "背景音", "audio", "music", "sound"),
            "brand": ("品牌", "logo", "标识", "brand"),
            "platform": ("竖屏", "横屏", "画幅", "比例", "平台", "platform", "aspect"),
            "quality": ("模糊", "清晰", "对比度", "溢出", "重叠", "quality"),
        }
        for cat, keywords in keyword_map.items():
            if any(kw in lowered for kw in keywords):
                cats.add(cat)
        return tuple(sorted(cats)) if cats else ("goal",)

    def _record_user_feedback(
        self, text: str, *, request_id: str = "", domain: str = "general",
        artifact_refs: tuple[str, ...] = (),
    ) -> Any:
        """OS-07: record structured user feedback for later retrieval.

        Creates a ``GeneralFeedbackPatch`` and stores it in the feedback
        store.  Returns the patch or None if the store is unavailable.
        """
        try:
            from src.features.edit.ai_editing.feedback_store import (
                FeedbackStore, SUPPORTED_DOMAINS,
            )
        except ImportError:
            return None
        if self._feedback_store is None:
            self._feedback_store = FeedbackStore()
        cats = self._categorize_feedback(text)
        safe_domain = domain if domain in SUPPORTED_DOMAINS else "general"
        return self._feedback_store.create(
            project_id="teaching_animation",
            run_id=request_id or None,
            domain=safe_domain,
            categories=cats,
            note=str(text or "")[:500],
            artifact_refs=(
                tuple(str(ref) for ref in artifact_refs if str(ref))
                or ((request_id,) if request_id else ())
            ),
        )

    def _feedback_context_for_artifact(self, *artifact_refs: str) -> str:
        """Return a small, deduplicated preference memory for one clip.

        Feedback is scoped to the active clip and never crosses projects.  The
        current request is read before it is recorded, so the author receives
        prior preferences without seeing the same instruction twice.
        """
        store = self._feedback_store
        if store is None:
            return ""
        entries: list[Any] = []
        seen: set[str] = set()
        for ref in artifact_refs:
            getter = getattr(store, "for_artifact", None)
            if not callable(getter):
                continue
            for patch in getter(str(ref)) or ():
                patch_id = str(getattr(patch, "patch_id", "") or "")
                if patch_id and patch_id in seen:
                    continue
                if patch_id:
                    seen.add(patch_id)
                entries.append(patch)
        return self._format_feedback_memory(
            str(getattr(item, "note", "") or "")
            for item in entries
        )

    @classmethod
    def _normalize_feedback_notes(cls, notes: Any) -> tuple[str, ...]:
        """Bound and sanitize preference notes before putting them in a scene.

        A Motion Graphic project is a shareable artifact, not an assistant
        transcript.  Persist only a short, deduplicated tail of feedback and
        run it through the common redaction boundary first.
        """
        try:
            from src.features.ai_harness.invocation_models import sanitize_for_storage
        except ImportError:  # pragma: no cover - only relevant to partial embeds
            sanitize_for_storage = lambda value: value  # type: ignore[assignment]
        if isinstance(notes, str):
            source = (notes,)
        elif isinstance(notes, Mapping):
            source = ()
        else:
            try:
                source = tuple(notes)
            except TypeError:
                source = ()
        normalized: list[str] = []
        seen: set[str] = set()
        for value in source:
            note = str(sanitize_for_storage(str(value or ""))).strip()
            if not note:
                continue
            note = note[:cls._MAX_PERSISTED_HTML_EDIT_FEEDBACK_CHARS]
            key = note.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(note)
        return tuple(normalized[-cls._MAX_PERSISTED_HTML_EDIT_FEEDBACK_NOTES:])

    @classmethod
    def _format_feedback_memory(cls, notes: Any) -> str:
        normalized = cls._normalize_feedback_notes(notes)
        return "\n".join(f"- {note}" for note in normalized)

    def _feedback_context_for_motion_graphic(
        self, scene: Any, *artifact_refs: str,
    ) -> str:
        """Combine saved clip preferences with this session's feedback.

        The layer metadata survives project save/reopen; the in-memory store
        contains the newest feedback entered in this app session.  Both are
        scoped to this exact clip and deduplicated before reaching the model.
        """
        layer = getattr(scene, "html_layer", None)
        metadata = getattr(layer, "metadata", {}) if layer is not None else {}
        stored = (
            metadata.get(self._HTML_EDIT_FEEDBACK_MEMORY_KEY, ())
            if isinstance(metadata, Mapping) else ()
        )
        runtime = self._feedback_context_for_artifact(*artifact_refs)
        runtime_notes = [
            line[2:].strip() for line in runtime.splitlines()
            if line.startswith("- ")
        ]
        return self._format_feedback_memory((*self._normalize_feedback_notes(stored), *runtime_notes))

    def _remember_motion_graphic_generation_feedback(
        self, plan: dict[str, Any], feedback: str,
    ) -> tuple[str, ...]:
        """Keep a bounded, request-local hand-off of user planning feedback."""
        existing = plan.get("generation_feedback_memory", ())
        notes = self._normalize_feedback_notes(
            (*self._normalize_feedback_notes(existing), feedback)
        )
        plan["generation_feedback_memory"] = notes
        return notes

    def _html_edit_layer_metadata(
        self, current: Any, plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build metadata for an edited layer without losing project context.

        ``apply_html_layer`` recomputes page timing from the returned HTML, so
        stale timing must not be copied into the candidate used by browser
        validation.  Other layer metadata, including project-local feedback,
        remains intact.
        """
        old_layer = getattr(current, "html_layer", None)
        old_metadata = getattr(old_layer, "metadata", {}) if old_layer is not None else {}
        metadata = dict(old_metadata) if isinstance(old_metadata, Mapping) else {}
        metadata.pop("page_timing", None)
        feedback = self._feedback_context_for_motion_graphic(
            current,
            str(plan.get("target_asset_id", "") or ""),
            str(plan.get("scene_id", "") or ""),
        )
        feedback_notes = [
            line[2:].strip() for line in feedback.splitlines()
            if line.startswith("- ")
        ]
        if feedback_notes:
            metadata[self._HTML_EDIT_FEEDBACK_MEMORY_KEY] = feedback_notes
        metadata.update({
            "font_lock": False,
            "render_profile": "motion_graphic_freeform",
            "network_access": bool(
                getattr(plan.get("constraints"), "allow_network_resources", False)
            ),
            "allow_inline_scripts": True,
            "allow_event_handlers": True,
            "allow_freeform_css": True,
            "allow_inline_visual_resources": True,
            "allow_gpu_apis": True,
        })
        return metadata

    @staticmethod
    def _extract_proposal_selection(text: str) -> str | None:
        """MG-04: detect if the user text is a proposal selection (e.g. "A", "B", "A方案").

        Returns the matched proposal_id ("A"/"B"/"C") or None.
        """
        import re as _re
        cleaned = str(text or "").strip().upper().replace(" ", "")
        if not cleaned or len(cleaned) > 12:
            return None
        # Match patterns: "A", "B", "C", "方案A", "选A", "选择A", "A方案"
        m = _re.search(r"(?:方案|选|选择)?\s*([ABC])(?:方案)?", cleaned)
        if m:
            return m.group(1)
        return None

    def _handle_ai_request(
        self, main_window: Any, text: str, *,
        target_asset_ids: tuple[str, ...] = (),
    ) -> bool:
        """Route Motion Graphic discussion, generation and explicit approval."""
        # An explicit media-pool target always wins over pending conversation
        # state and current-timeline heuristics.  This is what makes a clip
        # editable after reopening the project without restoring old chat.
        if target_asset_ids:
            return self._queue_motion_graphic_edit(
                main_window, text, tuple(target_asset_ids),
            )
        pending = tuple(
            (request_id, "motion")
            for request_id in self._pending_motion_graphic_requests
        ) + tuple(
            (request_id, "scene_edit")
            for request_id in self._pending_scene_edit_requests
        ) + tuple(
            (request_id, "html_generate")
            for request_id in self._pending_html_generate_requests
        )

        # MG-04: If the latest pending plan is in the "proposals" phase,
        # intercept before the normal confirm/cancel logic so that the user
        # can select a proposal by text without triggering a premature
        # scene-less confirm.
        if pending:
            request_id, kind = pending[-1]
            if kind == "motion":
                plan = self._pending_motion_graphic_requests.get(request_id)
                # Recovered tasks deliberately wait for an explicit user
                # continuation.  This prevents a project reopen from silently
                # restarting a paid/model operation or applying stale output.
                if plan is not None and plan.get("phase") == "resume_required":
                    if self._is_cancellation_text(text):
                        self._append_user_message(main_window, text)
                        return self._cancel_motion_graphic_request(
                            main_window, request_id, kind="motion",
                        )
                    if self._is_motion_graphic_resume_text(text):
                        self._append_user_message(main_window, text)
                        return self._resume_motion_graphic_task(main_window, request_id)
                    self._append_user_message(main_window, text)
                    self._record_user_feedback(
                        text, request_id=request_id, domain="general",
                    )
                    self._remember_motion_graphic_generation_feedback(plan, text)
                    self._append_assistant_message(
                        main_window,
                        "已记录这条补充。这个任务来自上次会话，请发送“继续”后从制作计划安全续接，"
                        "届时会一并采用你的新要求。",
                    )
                    return True

                if plan is not None and plan.get("phase") == "proposals":
                    proposal_id = self._extract_proposal_selection(text)
                    if proposal_id is not None:
                        self._append_user_message(main_window, text)
                        return self._select_creative_proposal(
                            main_window, request_id, proposal_id,
                        )
                    if self._is_cancellation_text(text):
                        self._append_user_message(main_window, text)
                        return self._cancel_motion_graphic_request(
                            main_window, request_id, kind="motion",
                        )
                    # "确认" during proposal phase = accept the first proposal.
                    if self._is_confirmation_text(text):
                        proposals = plan.get("creative_proposals") or []
                        if proposals:
                            self._append_user_message(main_window, text)
                            return self._select_creative_proposal(
                                main_window, request_id, proposals[0].proposal_id,
                            )
                    # Any other text = revision feedback; keep the conversation.
                    self._append_user_message(main_window, text)
                    # OS-07: record structured feedback for future retrieval.
                    self._record_user_feedback(
                        text, request_id=request_id, domain="general",
                    )
                    self._remember_motion_graphic_generation_feedback(plan, text)
                    self._append_assistant_message(
                        main_window,
                        "收到你的想法。请先从已有方案中选择一个"
                        "（输入\"方案A/B/C\"），或者告诉我更多需求。",
                    )
                    return True

                # Direct Motion Graphic mode first pauses on a model-authored
                # plan.  "可以" approves the communication plan and starts
                # a separate generation-confirmation checkpoint. Any other
                # text is sent back to the planner for another visible plan
                # revision. Nothing is rendered or written to the timeline
                # during either conversation phase.
                if (
                    plan is not None
                    and plan.get("phase") == "generation_confirmation"
                ):
                    if self._is_cancellation_text(text):
                        self._append_user_message(main_window, text)
                        return self._cancel_motion_graphic_request(
                            main_window, request_id, kind="motion",
                        )
                    if (
                        self._is_confirmation_text(text)
                        or self._is_generation_authorization(text)
                    ):
                        self._append_user_message(main_window, text)
                        return self._start_motion_graphic_generation(
                            main_window, request_id,
                        )
                    self._append_user_message(main_window, text)
                    self._record_user_feedback(
                        text, request_id=request_id, domain="general",
                    )
                    self._remember_motion_graphic_generation_feedback(plan, text)
                    panel = getattr(main_window, "ai_edit_assistant_panel", None)
                    clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
                    if callable(clear_card):
                        clear_card(request_id)
                    plan["phase"] = "plan_review"
                    plan["plan_approved"] = False
                    plan["creative_brief_pending"] = True
                    plan["creative_brief_revision_feedback"] = str(text or "").strip()
                    plan["creative_brief_retries"] = 0
                    self._append_assistant_message(
                        main_window,
                        "收到，我先回到制作计划继续调整；确认前不会生成网页，也不会修改时间线。",
                    )
                    if self._trigger_motion_graphic_creative_brief(main_window, request_id):
                        return True
                    self._mark_motion_graphic_build_failed(
                        main_window, request_id, "AI 制作计划通道未装配或不可用",
                    )
                    return True

                if plan is not None and plan.get("phase") == "plan_review":
                    if self._is_cancellation_text(text):
                        self._append_user_message(main_window, text)
                        return self._cancel_motion_graphic_request(
                            main_window, request_id, kind="motion",
                        )
                    if (
                        self._is_confirmation_text(text)
                        or self._is_generation_authorization(text)
                    ):
                        self._append_user_message(main_window, text)
                        return self._approve_motion_graphic_plan(
                            main_window, request_id, start_generation=False,
                        )

                    self._append_user_message(main_window, text)
                    self._record_user_feedback(
                        text, request_id=request_id, domain="general",
                    )
                    self._remember_motion_graphic_generation_feedback(plan, text)
                    panel = getattr(main_window, "ai_edit_assistant_panel", None)
                    clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
                    if callable(clear_card):
                        clear_card(request_id)
                    plan["creative_brief_pending"] = True
                    plan["creative_brief_revision_feedback"] = str(text or "").strip()
                    plan["creative_brief_retries"] = 0
                    self._append_assistant_message(
                        main_window,
                        "收到，我先按你的反馈修改制作计划；计划确认前不会生成网页，也不会修改时间线。",
                    )
                    if self._trigger_motion_graphic_creative_brief(main_window, request_id):
                        return True
                    self._mark_motion_graphic_build_failed(
                        main_window, request_id, "AI 制作计划通道未装配或不可用",
                    )
                    return True

                # A generated scene is only a draft until the user presses
                # the card's explicit confirmation action.  Any other message
                # supersedes it rather than being accidentally interpreted as
                # permission to apply the old draft.
                if plan is not None and plan.get("phase") == "build":
                    # AUD-28004: a plan whose build already failed is dead.
                    # The failure card tells the user to re-describe the
                    # requirement and start again, so retire it here instead
                    # of letting the draft interception below swallow the
                    # fresh request ("已保留为草稿…没有执行") or mistake it for
                    # a revision of the failed document (raw_html residue).
                    if plan.get("build_failed"):
                        if self._is_cancellation_text(text):
                            self._append_user_message(main_window, text)
                            return self._cancel_motion_graphic_request(
                                main_window, request_id, kind="motion",
                            )
                        if self._is_confirmation_text(text):
                            self._append_user_message(main_window, text)
                            self._append_assistant_message(
                                main_window,
                                I18N.tr("motion_graphic.ai.no_scene",
                                        "The scene was not generated successfully. Start again, or "
                                        "explicitly switch to a built-in template."),
                                error_code="motion_graphic_no_scene",
                            )
                            return True
                        self._pending_motion_graphic_requests.pop(request_id, None)
                        panel = getattr(main_window, "ai_edit_assistant_panel", None)
                        clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
                        if callable(clear_card):
                            clear_card(request_id)
                        # Fall through on purpose: the new creative text is
                        # routed as a brand-new request by the logic below,
                        # matching what the failure card promised.  The draft
                        # interception below must NOT run for this text.
                    else:
                        if (
                            plan.get("requires_button_confirmation")
                            and self._is_confirmation_text(text)
                        ):
                            self._append_user_message(main_window, text)
                            self._append_assistant_message(
                                main_window,
                                "草案还没有执行。请点击确认卡片中的“确认执行”按钮；"
                                "如需调整，直接告诉我修改内容即可。",
                            )
                            return True
                        if not self._is_confirmation_text(text) and not self._is_cancellation_text(text):
                            panel = getattr(main_window, "ai_edit_assistant_panel", None)
                            clear_card = getattr(panel, "clear_motion_graphic_proposal", None)
                            if callable(clear_card):
                                clear_card(request_id)
                            # Once an HTML draft exists, keep this request alive
                            # and send the current document plus the user's new
                            # words back to the same browser-authoring model.  A
                            # dissatisfied user is editing the webpage, not
                            # starting an unrelated generic-help conversation.
                            if plan.get("render_mode") == "html" and plan.get("raw_html"):
                                self._append_user_message(main_window, text)
                                plan["html_edit_context"] = str(plan.get("raw_html", ""))
                                plan["html_revision_feedback"] = str(text or "").strip()
                                plan["html_retries"] = 0
                                plan["scene"] = None
                                self._append_assistant_message(
                                    main_window,
                                    "收到，我会直接在当前网页上按你的反馈修改；"
                                    "设计仍由 AI 自由发挥，时间线不会先被修改。",
                                )
                                if self._trigger_motion_graphic_llm_build(main_window, request_id):
                                    return True
                                self._mark_motion_graphic_build_failed(
                                    main_window, request_id, "网页修改通道未装配或不可用",
                                )
                                return True
                            self._append_assistant_message(
                                main_window,
                                "已保留为草稿的场景没有执行。请先完成网页设计对话，"
                                "完成后会重新显示确认执行按钮。",
                            )
                            return True

        if pending and self._is_confirmation_text(text):
            self._append_user_message(main_window, text)
            request_id, kind = pending[-1]
            # AUD-28003: route by the request's own kind.  html_generate ids
            # (html-generate-*) are not in the motion dictionary, so routing
            # them to _confirm_motion_graphic_request cleared the preview
            # card and silently dropped the confirmation.
            if kind == "scene_edit":
                return self._confirm_scene_edit_request(main_window, request_id)
            if kind == "html_generate":
                # AUD-28005: None = background browser validation in flight.
                result = self._confirm_html_generate_request(main_window, request_id)
                return True if result is None else bool(result)
            result = self._confirm_motion_graphic_request(main_window, request_id)
            return True if result is None else bool(result)
        if pending and self._is_cancellation_text(text):
            self._append_user_message(main_window, text)
            request_id, kind = pending[-1]
            return self._cancel_motion_graphic_request(main_window, request_id, kind=kind)

        # A revision of an already-generated clip goes straight to the targeted
        # edit path: no plan card, no confirmation card — the clip exists, the
        # user asked for a change, and the result is applied in place (undoable).
        if (
            not self._motion_graphic_discussion_active
            and self._is_motion_graphic_edit_request(text)
        ):
            target = self._motion_graphic_edit_target(main_window, text)
            if target is not None:
                return self._queue_motion_graphic_edit(
                    main_window, text, (target[0],),
                )

        if self._is_html_generate_request(text):
            return self._trigger_html_generate(main_window, text)

        if self._motion_graphic_discussion_active and self._is_generation_authorization(text):
            brief_text = self._motion_graphic_discussion_brief_text or str(text or "")
            return self._queue_motion_graphic_plan(
                main_window,
                text,
                build_directly=not self._motion_graphic_discussion_wants_options,
                source_text=brief_text,
            )

        if (
            self._is_scene_edit_request(text)
            and not self._is_ai_agent_animation_request(text)
            and not self._motion_graphic_discussion_active
        ):
            plan = self._build_scene_edit_plan(main_window, text)
            if plan is not None:
                request_id = f"scene-edit-{uuid.uuid4().hex}"
                self._pending_scene_edit_requests[request_id] = plan
                self._append_user_message(main_window, text)
                updates = plan["updates"]
                summary = []
                if "content" in updates:
                    summary.append(f"文字 → {updates['content']}")
                if "style" in updates and "color" in updates["style"]:
                    summary.append(f"颜色 → {updates['style']['color']}")
                if "style" in updates and "font_size" in updates["style"]:
                    summary.append(f"字号 → {updates['style']['font_size']:g}")
                details = (
                    f"scene_id：{plan['scene_id']}\n"
                    f"目标 element_id：{plan['element_id']}\n"
                    f"变更：{'；'.join(summary)}\n"
                    "确认前不会修改场景、时间线或 Composite 引用；确认后通过场景命令栈执行，并验证首帧。"
                )
                panel = getattr(main_window, "ai_edit_assistant_panel", None)
                show_plan = getattr(panel, "set_motion_graphic_proposal", None)
                if callable(show_plan):
                    show_plan(request_id, "Motion Graphic 场景修改", details)
                    return True
                raise RuntimeError("AI Assistant 不支持场景修改计划确认")
        if self._is_ai_agent_animation_request(text):
            # First request starts a design conversation.  The original brief
            # is retained, because later control phrases such as "开始帮我生成"
            # are not themselves usable scene-design input.
            self._motion_graphic_discussion_active = True
            self._motion_graphic_discussion_brief_text = str(text or "").strip()
            self._motion_graphic_discussion_wants_options = (
                self._is_motion_graphic_planning_request(text)
            )
        elif self._motion_graphic_discussion_active:
            self._append_motion_graphic_discussion_detail(text)
        return False

    def _retry_motion_graphic_html_build(
        self, main_window: Any, request_id: str, feedback: str,
        *, reason: str = "真实浏览器逐页检查",
    ) -> bool:
        """Give the author model one repair pass after a render/quality failure."""
        plan = self._pending_motion_graphic_requests.get(str(request_id))
        if plan is None or plan.get("render_mode") != "html":
            return False
        retries = int(plan.get("html_retries", 0) or 0)
        if retries >= self._MAX_HTML_BUILD_RETRIES:
            return False
        plan["html_retries"] = retries + 1
        plan["html_revision_feedback"] = str(feedback or "").strip()
        plan["scene"] = None
        self._append_assistant_message(
            main_window,
            f"这版 HTML 动画没有通过{reason}，正在保留主题创意并自动修复（第 {retries + 1} 次重做）；"
            "时间线仍未修改。",
        )
        return self._trigger_motion_graphic_llm_build(main_window, request_id)

    def _retry_motion_graphic_creative_brief(
        self, main_window: Any, request_id: str, feedback: str,
    ) -> bool:
        """Give the visible plan stage one strict JSON repair."""
        plan = self._pending_motion_graphic_requests.get(str(request_id))
        if plan is None or not plan.get("creative_brief_pending"):
            return False
        retries = int(plan.get("creative_brief_retries", 0) or 0)
        if retries >= 1:
            return False
        plan["creative_brief_retries"] = retries + 1
        plan["creative_brief_revision_feedback"] = str(feedback or "").strip()
        self._append_assistant_message(
            main_window,
            "AI 的制作计划格式不规范，正在自动修正一次；修正后会先给你审核，"
            "不会生成网页或修改时间线。",
        )
        return self._trigger_motion_graphic_creative_brief(main_window, request_id)

    def _append_motion_graphic_discussion_detail(self, text: str) -> None:
        """Add one user turn to the retained brief without recording controls."""
        detail = str(text or "").strip()
        if not detail or self._is_generation_authorization(detail):
            return
        # These are UI/control conversations, not creative requirements. Do
        # not poison the next build prompt with “选A” or “没有确认按钮”.
        compact = re.sub(r"[\s，。！？,.!?、]+", "", detail.lower())
        if (
            re.fullmatch(r"(?:方案|选|选择)?[abc](?:方案)?", compact, flags=re.IGNORECASE)
            or "确认按钮" in detail
            or "确认卡" in detail
            or "点击确认" in detail
            or "看不到按钮" in detail
            or ("没有看见" in detail and "按钮" in detail)
        ):
            return
        current = self._motion_graphic_discussion_brief_text.strip()
        if not current:
            self._motion_graphic_discussion_brief_text = detail
            return
        if detail not in current:
            self._motion_graphic_discussion_brief_text = (
                f"{current}\n补充要求：{detail}"
            )
        if self._is_motion_graphic_planning_request(detail):
            self._motion_graphic_discussion_wants_options = True

    def create_empty_motion_graphic(self, main_window: Any) -> str | None:
        """Create an empty, scene-backed Motion Graphic in the Media Pool.

        The asset is registered before the editor opens, so the same stable
        ``teaching_animation:<scene_id>`` id is immediately draggable to the
        timeline.  HTML is intentionally absent at this point; the editor's
        *Import HTML* command adds it through an undoable command.
        """
        from .core.model import TeachingSceneDocument
        from .scene_store import get_shared_scene_store

        scene = TeachingSceneDocument.create_default(
            name="Motion Graphic Clip",
            width=self._MOTION_GRAPHIC_OUTPUT_WIDTH,
            height=self._MOTION_GRAPHIC_OUTPUT_HEIGHT,
            fps_num=25,
            duration_frames=25 * 15,
        )
        scene = replace(scene, elements=(), animations=())
        get_shared_scene_store().register(scene)
        try:
            asset_id = self._register_motion_graphic_asset(main_window, scene)
            self._open_scene(main_window, scene)
            self._last_motion_graphic_target = {
                "asset_id": str(asset_id),
                "scene_id": str(scene.scene_id),
                "display_name": str(scene.name),
            }
            panel = getattr(main_window, "media_pool_panel", None)
            reveal = getattr(panel, "reveal_asset", None)
            if callable(reveal):
                reveal(asset_id)
            self._append_assistant_message(
                main_window,
                "已在媒体池创建空 Motion Graphic Clip（1920×1080 / 25fps / 15秒）。"
                "双击素材可打开编辑器，再使用“导入 HTML…”载入网页动画。",
            )
            return asset_id
        except Exception as exc:  # noqa: BLE001 — UI action must remain recoverable
            logger.exception("Create empty Motion Graphic failed")
            self._append_assistant_message(
                main_window,
                I18N.tr("motion_graphic.ai.empty_create_failed",
                        "Creating the empty Motion Graphic clip failed: {error}", error=exc),
                error_code="motion_graphic_empty_create_failed",
            )
            return None

    def open_motion_graphic_asset(self, main_window: Any, asset_id: str) -> bool:
        """Open a Media Pool Motion Graphic asset in its scene editor."""
        from .core.model import TeachingSceneDocument
        from .host_adapters import motion_graphic_scene_id
        from .scene_store import get_shared_scene_store

        project = getattr(main_window, "_current_edit_project", None)
        asset = getattr(project, "assets", {}).get(str(asset_id)) if project is not None else None
        if asset is None or not getattr(asset, "is_motion_graphic", False):
            return False
        metadata = getattr(asset, "metadata", {}) or {}
        scene_id = str(metadata.get("scene_id", "") or "")
        scene_id = scene_id or str(motion_graphic_scene_id(asset_id) or "")
        if not scene_id:
            return False
        store = get_shared_scene_store()
        scene = store.get(scene_id)
        if scene is None:
            # A lightweight host may restore the MediaAsset before the scene
            # payload.  Recreate an empty scene with the stable id so the user
            # can still open it and import a replacement HTML document.
            fps = float(metadata.get("fps", 25.0) or 25.0)
            fps_num = max(1, round(fps))
            duration_frames = max(1, int(metadata.get("duration_frames", round(fps * 15)) or 1))
            scene = TeachingSceneDocument.create_default(
                name=str(getattr(asset, "display_name", "Motion Graphic Clip") or "Motion Graphic Clip"),
                scene_id=scene_id,
                width=max(1, int(metadata.get("width", 1920) or 1920)),
                height=max(1, int(metadata.get("height", 1080) or 1080)),
                fps_num=fps_num,
                duration_frames=duration_frames,
            )
            scene = replace(scene, elements=(), animations=())
            store.register(scene)
        self._open_scene(main_window, scene)
        self._last_motion_graphic_target = {
            "asset_id": str(asset_id),
            "scene_id": str(scene.scene_id),
            "display_name": str(getattr(asset, "display_name", "Motion Graphic Clip")),
        }
        return True

    def _open_timeline_clip(self, main_window: Any, panel: Any, clip_id: str, track_id: str) -> None:
        view = panel.get_timeline_view()
        timeline = getattr(view, "_timeline", None)
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None else None
        if clip is None:
            return
        from .host_adapters import motion_graphic_scene_id
        scene_id = motion_graphic_scene_id(getattr(clip, "asset_id", ""))
        if scene_id is None:
            return
        from .scene_store import get_shared_scene_store
        scene = get_shared_scene_store().get(scene_id)
        if scene is not None:
            self._open_scene(main_window, scene)

    def _open_scene(self, main_window: Any, scene: Any) -> Any:
        from .host_adapters import open_scene_editor
        registry = getattr(self, "_editor_registry", None)
        if registry is None:
            from .ui.editor_window_registry import EditorWindowRegistry
            registry = self._editor_registry = EditorWindowRegistry()
        return open_scene_editor(
            scene,
            registry=registry,
            parent=main_window,
            saver=lambda current_scene: self._save_motion_graphic_scene(
                main_window, current_scene,
            ),
            insert_callback=lambda current_scene: self._insert_existing_scene(main_window, current_scene),
            render_cache_callback=lambda current_scene: self._render_motion_graphic_video_cache(
                main_window, current_scene,
            ),
        )

    def _save_motion_graphic_scene(self, main_window: Any, scene: Any) -> None:
        """Commit an editor scene update and stale any baked copies of it."""
        from .scene_store import get_shared_scene_store

        get_shared_scene_store().register(scene)
        # Refresh Media Pool metadata through the normal immutable-project
        # path before invalidating cache manifests.  A Motion Graphic's source
        # is scene-owned, so its cache cannot remain VALID after any editor
        # save even though the timeline Clip id itself did not change.
        self._register_motion_graphic_asset(main_window, scene)
        project = getattr(main_window, "_current_edit_project", None)
        if project is None:
            return
        from src.features.edit.core.render_cache import RenderCacheManifest

        # Editing a scene makes every baked copy of it obsolete.  Stop any
        # in-flight cache worker for this scene first, then remove old output
        # files before marking their manifests stale.  Without this cleanup,
        # every small HTML edit left a full MP4/MOV behind in the cache folder.
        active_workers = getattr(main_window, "_render_cache_workers", {}) or {}
        for worker in tuple(active_workers.values()):
            manifest = getattr(worker, "_manifest", None)
            if (
                isinstance(manifest, RenderCacheManifest)
                and manifest.render_kind == "motion_graphic"
                and manifest.scene_id == scene.scene_id
            ):
                try:
                    worker.cancel()
                except Exception:
                    pass

        cleanup_files = getattr(main_window, "_cleanup_obsolete_render_cache_files", None)
        if callable(cleanup_files):
            cleanup_files(
                project,
                scene_id=str(scene.scene_id),
                include_rendering=True,
            )

        updated = project
        for raw_manifest in project.render_cache_manifests.values():
            manifest = (
                RenderCacheManifest.from_dict(raw_manifest)
                if isinstance(raw_manifest, dict) else raw_manifest
            )
            if (
                isinstance(manifest, RenderCacheManifest)
                and manifest.render_kind == "motion_graphic"
                and manifest.scene_id == scene.scene_id
            ):
                updated = updated.invalidate_render_cache_for_clip(
                    manifest.timeline_id, manifest.clip_id,
                )
        if updated is project:
            return
        apply_project_change = getattr(main_window, "_apply_project_change", None)
        if callable(apply_project_change):
            apply_project_change(updated)
        else:
            main_window._current_edit_project = updated
            panel = getattr(main_window, "timeline_panel", None)
            if panel is not None and hasattr(panel, "set_project"):
                panel.set_project(updated)

    def _apply_motion_graphic_cache_quality(self, main_window: Any) -> None:
        """Carry the editor's cache tier into the project's cache settings."""
        from .cache_quality import get_cache_quality
        quality = get_cache_quality()
        project = getattr(main_window, "_current_edit_project", None)
        if project is None:
            return
        try:
            settings = project.get_render_cache_settings()
            if getattr(settings, "motion_graphic_quality", quality) == quality:
                return
            from dataclasses import replace as dc_replace
            project.render_cache_settings = dc_replace(
                settings, motion_graphic_quality=quality)
        except Exception:  # noqa: BLE001 — a settings hiccup must not block caching
            pass

    def _render_motion_graphic_video_cache(self, main_window: Any, scene: Any) -> str:
        """Start background cache jobs for timeline clips sharing ``scene``."""
        panel = getattr(main_window, "timeline_panel", None)
        view = panel.get_timeline_view() if panel is not None else None
        timeline = getattr(view, "_timeline", None) if view is not None else None
        if timeline is None:
            return "当前没有可用时间线，无法渲染视频缓存"

        from .host_adapters import motion_graphic_scene_id

        selected_ids = set(
            getattr(getattr(view, "_selection", None), "selected_clip_ids", ()) or ()
        )
        matches: list[tuple[str, str]] = []
        for track in getattr(timeline, "video_tracks", ()):
            for clip in getattr(track, "clips", ()):
                if motion_graphic_scene_id(getattr(clip, "asset_id", "")) == scene.scene_id:
                    matches.append((str(clip.clip_id), str(track.track_id)))
        if not matches:
            return "当前 Motion Graphic 尚未在时间线中，无法渲染视频缓存"

        # If this scene is reused, prefer the clip the user selected; otherwise
        # bake every linked instance so none keep an older scene revision.
        selected_matches = [item for item in matches if item[0] in selected_ids]
        targets = selected_matches or matches
        request_cache = getattr(main_window, "request_render_cache_for_clip", None)
        if not callable(request_cache):
            return "视频缓存服务当前不可用"
        self._apply_motion_graphic_cache_quality(main_window)
        started = sum(
            1 for clip_id, track_id in targets
            if request_cache(clip_id, track_id)
        )
        if started == 0:
            return "视频缓存未能启动，请检查时间线与缓存设置"
        return f"已提交 {started} 个 Motion Graphic Clip 的视频缓存任务"

    def _insert_existing_scene(self, main_window: Any, scene: Any) -> None:
        """Insert an existing shared scene at the host timeline playhead."""
        panel = getattr(main_window, "timeline_panel", None)
        if panel is None:
            return
        view = panel.get_timeline_view()
        timeline = getattr(view, "_timeline", None)
        if timeline is None:
            return
        from .host_adapters import MotionGraphicClipCommand
        asset_id = self._register_motion_graphic_asset(main_window, scene)
        frame = int(panel.get_current_frame()) if hasattr(panel, "get_current_frame") else 0
        fps = float(getattr(panel, "_fps", 25.0) or 25.0)
        graphics_track = next(
            (track for track in timeline.video_tracks if getattr(track, "track_role", "") == "graphics"),
            None,
        )
        command = MotionGraphicClipCommand(
            scene,
            max(0, frame),
            max(1, int(scene.duration_frames or round(fps * 3.0))),
            getattr(graphics_track, "track_id", None),
            "overwrite",
            asset_id=asset_id,
        )
        view.execute_command(command)

    def _attach_composite(self, main_window: Any, page: Any) -> None:
        if getattr(page, "_teaching_animation_attached", False):
            return
        page._teaching_animation_attached = True
        page.node_editor.node_double_clicked.connect(
            lambda node_id: self._open_composite_node(main_window, page, node_id)
        )

    def _open_composite_node(self, main_window: Any, page: Any, node_id: str) -> None:
        from .scene_store import get_shared_scene_store
        node = page.node_graph.get_node(node_id)
        if node is None or getattr(node, "node_type", "") != "motion_graphic":
            return
        scene = get_shared_scene_store().get(str(node.parameters.get("scene_id", "")))
        if scene is not None:
            self._open_scene(main_window, scene)
