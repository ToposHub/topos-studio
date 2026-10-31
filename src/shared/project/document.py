from __future__ import annotations

from typing import Protocol


class ProjectDocument(Protocol):
    """Protocol for project documents that can be saved/loaded.

    Defines the interface for any document that participates in the
    project save/load lifecycle. Feature modules can expose their
    data as a ProjectDocument to integrate with the project system
    without coupling to a specific document implementation.
    """

    document_type: str
    dirty: bool

    def save_to_path(self, path: str) -> None:
        """Save the document to the given path.

        Args:
            path: File system path to save to.
        """
        ...

    def load_from_path(self, path: str) -> None:
        """Load the document from the given path.

        Args:
            path: File system path to load from.
        """
        ...
