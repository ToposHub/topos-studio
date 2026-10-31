#!/usr/bin/env python3
"""Freeze and verify the P0-03 scalable benchmark corpus manifest.

Runs the native ``topos_scalable_corpus`` generator, hashes every artifact
(SHA-256), and binds the result into
``native/topos_codec/docs/scalable_corpus_manifest.json``.  Default mode is a
bit-exact verification against the frozen manifest; ``--update`` rewrites it
(only legitimate when the corpus table version or generator changed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_EXECUTABLES = (
    REPO / "native/topos_codec/build/release/topos_scalable_corpus",
    REPO / "native/topos_codec/build/debug/topos_scalable_corpus",
)
MANIFEST_PATH = REPO / "native/topos_codec/docs/scalable_corpus_manifest.json"
SCHEMA_VERSION = 1
# 记录到 manifest 的许可声明：corpus 全部由项目内确定性生成器合成，
# 不含第三方素材。
LICENSE = {
    "name": "Topos Color project license",
    "note": (
        "all corpus items are synthesized deterministically by the in-tree "
        "topos_scalable_corpus generator from frozen seeds; no third-party "
        "or downloaded material is included"
    ),
}


def _select_executable(requested: Path | None) -> Path:
    candidates = (requested,) if requested is not None else DEFAULT_EXECUTABLES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "topos_scalable_corpus executable not found; build it via "
        "cmake --build native/topos_codec/build/release -t topos_scalable_corpus"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(executable: Path) -> dict[str, Any]:
    listing = subprocess.run(
        [str(executable), "--list"], check=True, capture_output=True, text=True,
    ).stdout
    table = json.loads(listing)
    generator_commit = table["git_commit"]
    items: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="topos_corpus_") as tmp:
        outdir = Path(tmp)
        # The native tool writes into per-item subdirectories but does not
        # create them (portable C, no mkdir); pre-create them here.
        for entry in table["items"]:
            (outdir / entry["id"]).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [str(executable), "--all", "--outdir", str(outdir)],
            check=True, capture_output=True, text=True,
        )
        for entry in table["items"]:
            item_dir = outdir / entry["id"]
            reference = json.loads((item_dir / "reference.json").read_text())
            if reference["corpus_table_version"] != table["corpus_table_version"]:
                raise ValueError(f"{entry['id']}: generator table version mismatch")
            items.append({
                **entry,
                "planes_sha256": _sha256(item_dir / "planes.raw"),
                "plane_bytes": reference["plane_bytes"],
                "quality_reference": {
                    "codec": "V2 (tc_frame_encode)",
                    "profile": entry["profile"],
                    "qmatrix_id": 1,
                    "qp_base": 24,
                    "packet_sha256": _sha256(item_dir / "v2_reference.tpc"),
                    "packet_bytes": reference["v2_packet_bytes"],
                    "psnr_y": reference["v2_psnr_y"],
                },
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus_table_version": table["corpus_table_version"],
        "generator": {
            "tool": "topos_scalable_corpus",
            "git_commit": generator_commit,
            "synthesizer": "tests/support/image_synth.c (xorshift64*, "
                           "deterministic, no wall clock)",
        },
        "license": LICENSE,
        "purpose": (
            "P0-03 frozen scalable gate corpus: flat / gradient / detail-text "
            "/ texture / film grain / synthetic HDR (12-bit PQ) / 4:2:2 / "
            "4:4:4 / 10-12-bit / alpha classes, each anchored to a V2 "
            "quality reference packet for matched-quality comparisons"
        ),
        "items": items,
    }


def verify(manifest: dict[str, Any], frozen: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if manifest["corpus_table_version"] != frozen["corpus_table_version"]:
        problems.append(
            f"corpus table version {manifest['corpus_table_version']} != "
            f"frozen {frozen['corpus_table_version']}"
        )
    frozen_by_id = {item["id"]: item for item in frozen["items"]}
    if len(frozen_by_id) != len(frozen["items"]):
        problems.append("frozen manifest has duplicate item ids")
    for item in manifest["items"]:
        frozen_item = frozen_by_id.get(item["id"])
        if frozen_item is None:
            problems.append(f"unknown item {item['id']} (not in frozen manifest)")
            continue
        for field in ("planes_sha256", "plane_bytes"):
            if item[field] != frozen_item[field]:
                problems.append(
                    f"{item['id']}: {field} changed "
                    f"({frozen_item[field]} -> {item[field]})"
                )
        frozen_q = frozen_item["quality_reference"]
        q = item["quality_reference"]
        if q["packet_sha256"] != frozen_q["packet_sha256"]:
            problems.append(
                f"{item['id']}: V2 reference packet hash changed "
                f"({frozen_q['packet_sha256']} -> {q['packet_sha256']})"
            )
        if q["packet_bytes"] != frozen_q["packet_bytes"]:
            problems.append(
                f"{item['id']}: V2 reference packet size changed "
                f"({frozen_q['packet_bytes']} -> {q['packet_bytes']})"
            )
        if abs(q["psnr_y"] - frozen_q["psnr_y"]) > 1e-6:
            problems.append(
                f"{item['id']}: reference PSNR-Y drifted "
                f"({frozen_q['psnr_y']} -> {q['psnr_y']})"
            )
    missing = set(frozen_by_id) - {item["id"] for item in manifest["items"]}
    for item_id in sorted(missing):
        problems.append(f"frozen item {item_id} missing from generator output")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--update", action="store_true",
                        help="rewrite the frozen manifest instead of verifying")
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(_select_executable(args.executable))
    except (FileNotFoundError, OSError, subprocess.CalledProcessError,
            ValueError, json.JSONDecodeError) as exc:
        print(f"corpus freeze failed: {exc}", file=sys.stderr)
        return 1
    if args.update:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"updated {MANIFEST_PATH.relative_to(REPO)} "
              f"({len(manifest['items'])} items)")
        return 0
    if not MANIFEST_PATH.is_file():
        print(f"frozen manifest missing: {MANIFEST_PATH}", file=sys.stderr)
        return 1
    frozen = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    problems = verify(manifest, frozen)
    if problems:
        print("corpus verification FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"corpus verified: {len(manifest['items'])} items bit-exact against "
          f"frozen manifest (table v{manifest['corpus_table_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
