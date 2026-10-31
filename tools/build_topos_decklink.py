#!/usr/bin/env python3
"""Build the native Topos DeckLink bridge extension for macOS."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SDK_INCLUDE = ROOT / "external" / "blackmagic" / "decklink_sdk_12_1" / "Mac" / "include"
BRIDGE_SOURCE = ROOT / "src" / "native" / "decklink_bridge" / "topos_decklink.cpp"
DISPATCH_SOURCE = SDK_INCLUDE / "DeckLinkAPIDispatch.cpp"


def main() -> int:
    if sys.platform != "darwin":
        print("topos_decklink is only buildable on macOS", file=sys.stderr)
        return 2

    missing = [path for path in (SDK_INCLUDE, BRIDGE_SOURCE, DISPATCH_SOURCE) if not path.exists()]
    if missing:
        for path in missing:
            print(f"missing: {path}", file=sys.stderr)
        return 2

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    include_py = sysconfig.get_config_var("INCLUDEPY")
    if not include_py:
        print("Could not locate Python headers for this interpreter", file=sys.stderr)
        return 2

    output = ROOT / "src" / f"topos_decklink{ext_suffix}"
    command = [
        os.environ.get("CXX", "clang++"),
        "-std=c++17",
        "-O2",
        "-fPIC",
        "-dynamiclib",
        "-undefined",
        "dynamic_lookup",
        "-I",
        str(include_py),
        "-I",
        str(SDK_INCLUDE),
        str(BRIDGE_SOURCE),
        str(DISPATCH_SOURCE),
        "-framework",
        "CoreFoundation",
        "-o",
        str(output),
    ]

    print("Building", output)
    subprocess.check_call(command, cwd=ROOT)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
