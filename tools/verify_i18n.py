#!/usr/bin/env python3
"""
i18n Translation Alignment Checker for Topos

Verifies that every locale file has all keys present in the en_US reference,
and that placeholders survive translation ({names}, % signs, newlines).

Scans every locales directory:
  - src/shared/i18n/locales/
  - src/features/*/locales/

Usage:
    python tools/verify_i18n.py [--verbose]
    python tools/verify_i18n.py --locale zh_CN
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PH = re.compile(r"\{[^{}]*\}")


def flatten_dict(d, parent_key='', sep='.'):
    """Flatten a nested dictionary into dot-notation keys."""
    items = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}" if parent_key else ""
        new_key += k
        if isinstance(v, dict):
            items.update(flatten_dict(v, new_key, sep))
        else:
            items[new_key] = v
    return items


def locale_dirs():
    """All locales directories (shared + per-feature)."""
    dirs = [PROJECT_ROOT / "src" / "shared" / "i18n" / "locales"]
    dirs.extend(sorted((PROJECT_ROOT / "src" / "features").glob("*/locales")))
    return [d for d in dirs if d.is_dir()]


def load_locale_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return flatten_dict(json.load(f))


def placeholder_issues(base_val, target_val):
    """Structural differences that would break rendering/format."""
    issues = []
    if sorted(PH.findall(base_val)) != sorted(PH.findall(target_val)):
        issues.append(f"placeholders base={PH.findall(base_val)} target={PH.findall(target_val)}")
    if base_val.count("\n") != target_val.count("\n"):
        issues.append("newline-count")
    if base_val.count("%") != target_val.count("%"):
        issues.append(f"percent base={base_val.count('%')} target={target_val.count('%')}")
    return issues


def main():
    parser = argparse.ArgumentParser(
        description='Verify i18n translation alignment across locales')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show matched key counts per locale')
    parser.add_argument('--locale', type=str, default=None,
                        help='Specific locale to check (default: all)')
    args = parser.parse_args()

    dirs = locale_dirs()
    if not dirs:
        print("❌ Error: No locales directories found")
        sys.exit(1)

    total_missing = total_extra = total_empty = total_placeholder = 0
    hard_fail = False

    print("=" * 60)
    print("  Topos i18n Translation Alignment Checker")
    print("=" * 60)

    for locales_dir in dirs:
        label = locales_dir.parent.name if locales_dir.parent.parent.name == "features" \
            else "shared"
        locale_files = sorted(locales_dir.glob('*.json'))
        base_file = locales_dir / 'en_US.json'
        if not base_file.is_file():
            print(f"\n⚠️  [{label}] no en_US.json in {locales_dir}, skipping")
            continue

        base_flat = load_locale_file(base_file)
        print(f"\n📂 [{label}] {locales_dir.relative_to(PROJECT_ROOT)}")
        print(f"   base en_US: {len(base_flat)} keys | locales: "
              f"{', '.join(f.stem for f in locale_files)}")

        for lf in locale_files:
            if lf.stem == 'en_US':
                continue
            if args.locale and lf.stem != args.locale:
                continue
            try:
                target_flat = load_locale_file(lf)
            except json.JSONDecodeError as e:
                print(f"   ❌ {lf.stem}: invalid JSON: {e}")
                hard_fail = True
                continue

            missing = sorted(set(base_flat) - set(target_flat))
            extra = sorted(set(target_flat) - set(base_flat))
            empty = [k for k in set(base_flat) & set(target_flat)
                     if target_flat[k] in ('', None)]
            ph_issues = []
            for k in set(base_flat) & set(target_flat):
                b, t = base_flat[k], target_flat[k]
                if isinstance(b, str) and isinstance(t, str):
                    for issue in placeholder_issues(b, t):
                        ph_issues.append(f"{k}: {issue}")

            total_missing += len(missing)
            total_extra += len(extra)
            total_empty += len(empty)
            total_placeholder += len(ph_issues)
            if missing or extra or empty or ph_issues:
                hard_fail = True

            status = "✅" if not (missing or extra or empty or ph_issues) else "❌"
            print(f"   {status} {lf.stem}: {len(target_flat)}/{len(base_flat)} keys, "
                  f"missing={len(missing)} extra={len(extra)} empty={len(empty)} "
                  f"placeholder-issues={len(ph_issues)}")
            if args.verbose:
                for k in missing:
                    print(f"      MISSING {k}")
                for k in extra:
                    print(f"      EXTRA    {k}")
                for k in empty:
                    print(f"      EMPTY    {k}")
                for issue in ph_issues[:20]:
                    print(f"      PH       {issue}")
                if len(ph_issues) > 20:
                    print(f"      ... and {len(ph_issues) - 20} more placeholder issues")

    print("\n" + "=" * 60)
    if not hard_fail:
        print("\n✅ All locales are perfectly aligned!")
        sys.exit(0)
    print(f"\n⚠️  Issues found: missing={total_missing} extra={total_extra} "
          f"empty={total_empty} placeholder-issues={total_placeholder}")
    print("💡 Run with --verbose for the full key list")
    sys.exit(1)


if __name__ == '__main__':
    main()
