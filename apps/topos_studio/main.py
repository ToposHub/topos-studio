"""Topos Studio entry point.

Topos Studio includes all features:
- Edit (timeline editing)
- Color (color grading)
- Composite (node-based compositing)
- Deliver (export and delivery)
"""
import os
import sys

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)

# Set product mode before importing any src modules
os.environ["TOPOS_PRODUCT_MODE"] = "studio"


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
    """Launch Topos Studio with all features enabled."""
    from src.main import main as _main
    # Override product mode
    sys.argv = [sys.argv[0]] + _without_product_args(sys.argv[1:])
    sys.argv.extend(["--product", "studio"])
    _main()


if __name__ == "__main__":
    main()
