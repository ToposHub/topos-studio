"""Topos Composite entry point.

Topos Composite includes:
- Composite (node-based compositing)
- Color (color grading, core feature)
- Deliver (render and export)

Edit is disabled.
"""
import os
import sys

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)

# Set product mode before importing any src modules
os.environ["TOPOS_PRODUCT_MODE"] = "composite"


def _without_product_args(argv):
    result = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--product":
            skip_next = True
            continue
        if arg.startswith("--product="):
            continue
        result.append(arg)
    return result


def main():
    """Launch Topos Composite (compositing + color + render)."""
    from src.main import main as _main
    sys.argv = [sys.argv[0]] + _without_product_args(sys.argv[1:])
    sys.argv.extend(["--product", "composite"])
    _main()


if __name__ == "__main__":
    main()
