#!/usr/bin/env python3
"""RAW12/RAW16 CFA 与 half-float16 .toos 原生域逐档质量测量。

输入是无头、紧密排列的 little-endian uint16 平面；RAW 为 R/Gr/Gb/B
四相位，每相位 W/2×H/2；float 为 G/B/R 三个 W×H half 位平面。
报告只描述输入素材，不给 ProRes RAW 或 ProRes 4444 等价结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.topos_image_binding import half_bits_to_codes  # noqa: E402

TIERS = {
    "raw12": tuple(f"raw12-{ratio}" for ratio in (2, 4, 6, 8, 12, 16)),
    "raw16": tuple(f"raw16-{ratio}" for ratio in (2, 4, 6, 8, 12, 16)),
    "float16": ("float-ultra", "float-high", "float-medium", "float-low"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_planes(path: Path, kind: str, width: int,
                 height: int) -> tuple[np.ndarray, ...]:
    if kind.startswith("raw"):
        if width % 2 or height % 2:
            raise ValueError("RAW CFA 宽高必须为偶数")
        shape, count = (height // 2, width // 2), 4
    else:
        shape, count = (height, width), 3
    expected = shape[0] * shape[1] * count * 2
    if path.stat().st_size != expected:
        raise ValueError(f"{path}: 字节数 {path.stat().st_size} != {expected}")
    data = np.fromfile(path, dtype="<u2")
    each = shape[0] * shape[1]
    return tuple(data[index * each:(index + 1) * each].reshape(shape)
                 for index in range(count))


def _int_metrics(source: tuple[np.ndarray, ...],
                 decoded: tuple[np.ndarray, ...], bits: int) -> dict:
    per_phase = []
    for original, actual in zip(source, decoded):
        delta = actual.astype(np.int32) - original.astype(np.int32)
        abs_delta = np.abs(delta)
        mse = float(np.mean(delta.astype(np.float64) ** 2))
        per_phase.append({
            "psnr_db": (math.inf if mse == 0 else
                        10 * math.log10(((1 << bits) - 1) ** 2 / mse)),
            "mae_codes": float(np.mean(abs_delta)),
            "p99_abs_codes": float(np.percentile(abs_delta, 99)),
            "max_abs_codes": int(abs_delta.max()),
            "bit_exact": bool(np.array_equal(original, actual)),
        })
    return {"phase_order": ["R", "Gr", "Gb", "B"],
            "per_phase": per_phase,
            "all_bit_exact": all(p["bit_exact"] for p in per_phase)}


def _float_metrics(source: tuple[np.ndarray, ...],
                   decoded: tuple[np.ndarray, ...]) -> dict:
    per_channel = []
    for original_bits, actual_bits in zip(source, decoded):
        original = original_bits.view(np.float16)
        actual = actual_bits.view(np.float16)
        finite_both = np.isfinite(original) & np.isfinite(actual)
        src_codes = half_bits_to_codes(original_bits).astype(np.int32)
        out_codes = half_bits_to_codes(actual_bits).astype(np.int32)
        ulp = np.abs(src_codes[finite_both] - out_codes[finite_both])
        class_changed = (
            (np.isnan(original) != np.isnan(actual))
            | (np.isinf(original) != np.isinf(actual))
            | (np.isfinite(original) != np.isfinite(actual))
        )
        zero_both = (original == 0) & (actual == 0)
        source_zero = (original_bits & 0x7FFF) == 0
        actual_zero = (actual_bits & 0x7FFF) == 0
        source_negative_zero = original_bits == 0x8000
        per_channel.append({
            "bit_exact": bool(np.array_equal(original_bits, actual_bits)),
            "finite_samples": int(finite_both.sum()),
            "p99_half_ulp": (float(np.percentile(ulp, 99)) if ulp.size else None),
            "max_half_ulp": (int(ulp.max()) if ulp.size else None),
            "special_class_changes": int(class_changed.sum()),
            "zero_sign_changes": int(np.sum(
                zero_both & (np.signbit(original) != np.signbit(actual)))),
            "source_zero_to_nonzero": int(np.sum(source_zero & ~actual_zero)),
            "negative_zero_to_nonzero": int(np.sum(
                source_negative_zero & ~actual_zero)),
            "source_nonzero_to_zero": int(np.sum(~source_zero & actual_zero)),
        })
    return {"channel_order": ["G", "B", "R"], "per_channel": per_channel,
            "all_bit_exact": all(p["bit_exact"] for p in per_channel)}


def cli_sha_matches(actual: str, expected: str) -> bool:
    """CLI SHA 断言（BENCH-0016；纯函数供单测）。

    ``expected`` 为完整十六进制或 >=8 位前缀（大小写不敏感）；空串视为
    "仅记录不断言"（返回 True）。
    """
    norm = (expected or "").strip().lower()
    if not norm:
        return True
    if len(norm) < 8:
        return False
    return actual.strip().lower().startswith(norm)


def run(args: argparse.Namespace) -> dict:
    source = args.source.resolve(strict=True)
    codec_cli = args.codec_cli.resolve(strict=True)
    # BENCH-0016：--record-cli-sha [EXPECTED] —— 把 toos CLI SHA 写入
    # out-dir 的 result.json；带 EXPECTED（完整或 >=8 位前缀）时先断言
    # 匹配再跑（防三代 CLI 混用：matrix 06508c5c / finite 4b8e5366 /
    # audit 333b7df3 的教训），不匹配即 SystemExit，不产生任何档位数据。
    cli_sha = _sha256(codec_cli)
    cli_sha_record = None
    if args.record_cli_sha is not None:
        expected = args.record_cli_sha or None
        match = (cli_sha_matches(cli_sha, expected)
                 if expected is not None else True)
        cli_sha_record = {
            "codec_cli": str(codec_cli),
            "codec_cli_sha256": cli_sha,
            "expected_cli_sha256": expected,
            "match": match,
            "kind": args.kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "note": "BENCH-0016：跨 CLI 版本的压缩比/误差变化属二进制演进"
                    "而非回归；复测前用本记录固定 CLI 版本。",
        }
        if not match:
            raise SystemExit(
                f"CLI SHA 断言失败：期望 {expected}，实际 {cli_sha} "
                "(--record-cli-sha)，拒绝测量")
    original = _read_planes(source, args.kind, args.width, args.height)
    if args.kind == "float16":
        for index, plane in enumerate(original):
            if np.any((plane & 0x7C00) == 0x7C00):
                raise ValueError(
                    f"float-* 有损档不接受 Inf/NaN（平面 {index}）；"
                    "本工具只测有限值，异常值拒绝行为由单独回归测试验证"
                )
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if cli_sha_record is not None:
        result_path = output / "result.json"
        if result_path.exists():
            raise FileExistsError(f"CLI 记录已存在，拒绝覆盖: {result_path}")
        result_path.write_text(
            json.dumps(cli_sha_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"[cli-sha] {cli_sha}"
              + (f"（匹配期望 {expected}）" if expected else "（仅记录）"))
    report_path = output / f"native_domains_{args.kind}.json"
    if report_path.exists():
        raise FileExistsError(f"报告已存在，拒绝覆盖: {report_path}")
    tiers = TIERS[args.kind]
    rows = []
    for tier in tiers:
        encoded = output / f"{tier}.toos"
        decoded_path = output / f"{tier}.raw"
        if encoded.exists() or decoded_path.exists():
            raise FileExistsError(f"档位输出已存在，拒绝覆盖: {tier}")
        encode_cmd = [str(codec_cli), "encode", str(source), "-o", str(encoded),
                      "--width", str(args.width), "--height", str(args.height),
                      "--tier", tier]
        decode_cmd = [str(codec_cli), "decode", str(encoded), "-o",
                      str(decoded_path)]
        subprocess.run(encode_cmd, check=True, capture_output=True, text=True)
        subprocess.run(decode_cmd, check=True, capture_output=True, text=True)
        decoded = _read_planes(decoded_path, args.kind, args.width, args.height)
        metrics = (_float_metrics(original, decoded) if args.kind == "float16"
                   else _int_metrics(original, decoded,
                                     12 if args.kind == "raw12" else 16))
        rows.append({
            "tier": tier, "encoded_bytes": encoded.stat().st_size,
            "source_over_encoded": source.stat().st_size / encoded.stat().st_size,
            "encoded_sha256": _sha256(encoded),
            "decoded_sha256": _sha256(decoded_path),
            "encode_command": encode_cmd, "decode_command": decode_cmd,
            "metrics": metrics,
        })
    report = {
        "status": "synthetic_or_input_domain_measurement_no_release_verdict",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": args.kind, "width": args.width, "height": args.height,
        "source": str(source), "source_sha256": _sha256(source),
        "source_bytes": source.stat().st_size,
        "codec_cli": str(codec_cli), "codec_cli_sha256": _sha256(codec_cli),
        "measurement_script_sha256": _sha256(Path(__file__)),
        "results": rows,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=sorted(TIERS), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--codec-cli", type=Path,
                        default=REPO / "native/topos_codec/build/release/toos")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--record-cli-sha", nargs="?", default=None, const="",
        metavar="EXPECTED_SHA",
        help="把 toos CLI 的 SHA-256 写入 out-dir/result.json（BENCH-0016，"
             "防三代 CLI 混用 06508c5c/4b8e5366/333b7df3 重演）；带 "
             "EXPECTED_SHA（完整或 >=8 位前缀）时先断言匹配再测量，"
             "不匹配直接失败。")
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0:
        parser.error("宽高必须为正")
    report = run(args)
    print(f"{args.kind}: {len(report['results'])} 档；"
          f"报告 {args.out_dir / ('native_domains_' + args.kind + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
