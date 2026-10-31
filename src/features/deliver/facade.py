"""DeliverFacade -- thin re-export layer for deliver types used by app controllers.

Instead of each controller importing individually from
src.features.deliver.ui.* (deep import), the controller imports
from this facade at the feature boundary.

This module is a *re-export shim*: it does not add behaviour, it merely
consolidates the surface area so the app layer has fewer deep-import
lines to maintain.  All imports are lazy (inside functions) so that
the module loads cleanly even when the deliver feature is disabled.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_deliver_page_class():
    """Return the Deliver page class.

    Returns the new :class:`DeliverWorkspace` (video batch export) when it
    can be imported, falling back to the legacy :class:`DeliverPage` single
    form when the workspace's dependencies are missing. Returns ``None`` if
    neither can be imported (deliver feature disabled).
    """
    try:
        from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
        return DeliverWorkspace
    except (ImportError, AttributeError) as e:
        # AUD-12100: module.py guards the same import against AttributeError
        # (partial/circular import of a damaged install); the facade must
        # match, otherwise the exception escapes into main_window's
        # ``except ImportError`` and crashes startup.
        logger.warning("Deliver workspace unavailable: %s", e)
    try:
        from src.features.deliver.ui.deliver_page import DeliverPage
        return DeliverPage
    except (ImportError, AttributeError) as e:
        logger.warning("Deliver deliver_page unavailable: %s", e)
        return None
