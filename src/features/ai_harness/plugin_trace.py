"""Bridge bounded plugin lifecycle facts into the unified run trace."""

from __future__ import annotations

from typing import Callable

from src.features.ai_harness.plugin_status import PluginEvent, PluginEventJournal
from src.features.ai_harness.run_trace import RunTraceStore, TraceEvent


_TOOL_START_KIND = "plugin.tool_started"
_TOOL_FINISH_KINDS = frozenset({
    "plugin.tool_completed",
    "plugin.tool_cancelled",
    "plugin.tool_timed_out",
    "plugin.tool_failed",
    "plugin.tool_rejected",
})


class PluginRunTraceAdapter:
    """Project plugin calls into ``run.tool_*`` trace facts.

    Registration and activation events without a ``run_id`` stay in the
    plugin journal.  Invocation events with a ``run_id`` become ordinary
    assistant trace events, retaining ``plugin_event_kind`` so the Run Drawer
    can show the plugin-specific diagnosis without teaching the projection
    about every plugin implementation.
    """

    def __init__(
        self,
        trace_store: RunTraceStore,
        *,
        session_id_for_run: Callable[[str], str] | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._session_id_for_run = session_id_for_run or (lambda _run_id: "default")
        self._unsubscribe: Callable[[], None] | None = None

    def attach(self, journal: PluginEventJournal) -> None:
        self.close()
        self._unsubscribe = journal.subscribe(self.record_event)

    def close(self) -> None:
        unsubscribe = self._unsubscribe
        self._unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()

    def record_event(self, event: PluginEvent) -> TraceEvent | None:
        run_id = str(event.payload.get("run_id", "") or "").strip()
        if not run_id:
            return None
        try:
            session_id = str(self._session_id_for_run(run_id) or "default")
        except Exception:
            session_id = "default"
        kind = str(event.kind)
        if kind == _TOOL_START_KIND:
            trace_kind = "run.tool_started"
        elif kind in _TOOL_FINISH_KINDS:
            trace_kind = "run.tool_finished"
        else:
            trace_kind = kind
        payload = dict(event.payload)
        payload["plugin_id"] = event.plugin_id
        payload["plugin_event_kind"] = kind
        payload.setdefault("call_id", str(payload.get("invocation_id", "")))
        if kind in _TOOL_FINISH_KINDS:
            payload.setdefault(
                "outcome",
                "success" if kind == "plugin.tool_completed" else "failure",
            )
        return self._trace_store.append(
            session_id=session_id,
            run_id=run_id,
            source="assistant",
            kind=trace_kind,
            payload=payload,
            event_id=f"plugin-event:{event.plugin_id}:{event.sequence}",
            timestamp=event.timestamp,
        )


__all__ = ["PluginRunTraceAdapter"]
