"""Parse a model without creating a window or OpenGL context.

The result is an application-owned temporary pickle consumed only by the
parent viewer. FBX native decoding stays outside the interactive process.
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path
import sys
import traceback
from typing import Any

from quick_3d_viewer import QuickModelViewer


class ModelParser:
    """Reuse the application's pure parsing methods without a Qt widget."""

    _parse_model_data = QuickModelViewer._parse_model_data
    _load_fbx_parts = QuickModelViewer._load_fbx_parts
    _resolve_fbx_texture = QuickModelViewer._resolve_fbx_texture
    _extract_material_visuals = QuickModelViewer._extract_material_visuals
    _extract_skin_weights = staticmethod(QuickModelViewer._extract_skin_weights)

    def __init__(self) -> None:
        self._fbx_tex_cache: dict[Any, Any] = {}
        self._fbx_embedded_records: list[Any] | None = None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("Usage: quick_3d_parse_worker.py MODEL RESULT", file=sys.stderr)
        return 2
    source, output = args
    try:
        result = ModelParser()._parse_model_data(str(Path(source).resolve()))
        payload = ("ok", result)
    except Exception as exc:  # Process boundary: serialize all parser failures for the parent.
        traceback.print_exc(file=sys.stderr)
        payload = ("error", str(exc))
    temporary = output + ".tmp"
    try:
        with open(temporary, "wb") as stream:
            pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, output)
    except (OSError, pickle.PickleError, TypeError) as exc:
        print(f"Cannot write parser result: {exc}", file=sys.stderr)
        return 1
    finally:
        Path(temporary).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
