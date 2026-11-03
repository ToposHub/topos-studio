"""Versioned UI-only drag intent; resources are resolved by the host catalog."""
from __future__ import annotations
import json
from collections.abc import Mapping
from typing import Any
from PyQt6.QtCore import QMimeData

RESOURCE_MIME = 'application/x-topos-color-resource'


def resource_mime_data(entry: Mapping[str, Any]) -> QMimeData:
    data = QMimeData()
    payload = {'version': 1, 'kind': entry['kind'], 'resource_id': entry['id']}
    data.setData(RESOURCE_MIME, json.dumps(payload).encode('utf-8'))
    data.setText(str(entry.get('name', entry['id'])))
    return data


def decode_resource_mime(data: QMimeData) -> dict[str, Any] | None:
    if not data.hasFormat(RESOURCE_MIME):
        return None
    raw = bytes(data.data(RESOURCE_MIME))
    if len(raw) > 16384:
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('kind') not in ('effect', 'lut')
            or not isinstance(value.get('resource_id'), str)
            or not value['resource_id'] or len(value['resource_id']) > 1024):
        return None
    return {key: value[key] for key in ('version', 'kind', 'resource_id')}
