"""Cross-feature render output source protocol.

This module defines :class:`RenderSource`, the shared structural-typing
contract for any component that produces render output (live viewer,
exporter, thumbnail generator, etc.).  Feature modules depend on this
protocol instead of concrete implementations, keeping the render pipeline
decoupled from specific UI or export backends.

Threading semantics: the protocol itself carries no locking requirements.
Implementations are expected to be cheap to query from the UI thread;
heavy GPU work must happen inside the implementation's own worker
pipeline, not in these methods.
"""

from __future__ import annotations

from typing import Any, Protocol


class RenderSource(Protocol):
    """Protocol for render output sources (viewer, export).

    Defines the interface for any component that produces render output.
    Feature modules can depend on this protocol to access rendered results
    without coupling to a specific viewer or export implementation.
    """

    #: Stable identifier for this source (unique among registered sources).
    source_id: str

    #: Human-readable display title shown in UI selectors.
    title: str

    def is_available(self) -> bool:
        """Return True if this render source is currently available.

        Returns:
            True if the source can produce output, False otherwise.
        """
        ...

    def get_current_output(self) -> Any:
        """Get the current render output.

        Returns:
            The current rendered output (CPU image data, or a GPU texture
            handle such as a ModernGL texture / int texture ID, depending on
            the implementation).

        Ownership contract (AUD-1702):
            The returned object remains owned by the *source*. Callers must
            treat it as a borrowed reference:
            * never release/destroy it (``release()``/``__del__``/GL delete
              belong to the source's render pipeline — GPU objects are freed
              deferred on the owning render thread, per project rules);
            * never cache it beyond the current use — any render cycle may
              replace or free it;
            * GPU-flavored outputs are only valid on the source's render
              thread / GL context that created them; consuming them from
              another thread or context is undefined. CPU (numpy) outputs
              are thread-safe to read but must still not be freed by the
              caller if documented so by the implementation.
        """
        ...
