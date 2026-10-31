#!/usr/bin/env python3
"""
Freeze Benchmark Media Manifest (P0-06)
冻结 tests/test_videos 真实视频基准矩阵：ffprobe 元数据 + 指纹 + B-01..B-06
场景映射（性能总计划 §6 P0-06 / §22）。

tests/test_videos 是每次功能/性能修复后的必跑真实视频集，不可由 synthetic
fixture 替代。本工具产出机器可读清单 `tests/test_videos/benchmark_media_manifest.json`：

- 每个文件的 codec/profile/pix_fmt/分辨率/fps/时长/大小；
- 指纹（默认 size + 首尾 1MB sha256；--deep 全文件 sha256）；
- 到基准场景 B-01..B-06 的映射与缺口（gap）清单；
- 生成时间、git commit、机器信息（脱敏：无用户路径）。

用法：
    .venv/bin/python tools/freeze_benchmark_media_manifest.py            # 快速指纹
    .venv/bin/python tools/freeze_benchmark_media_manifest.py --deep     # 全量 sha256
    .venv/bin/python tools/freeze_benchmark_media_manifest.py --check    # 校验现有清单
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEDIA_DIR = REPO_ROOT / "tests" / "test_videos"
OUTPUT_PATH = DEFAULT_MEDIA_DIR / "benchmark_media_manifest.json"

MANIFEST_VERSION = 1

#: 忽略的非媒体条目。
SKIP_NAMES = {".DS_Store", ".cache", "tracking_fixtures",
              "topos_test_media_manifest.json", "benchmark_media_manifest.json"}

#: 基准场景最低集（总计划 §6）。每个场景列出该场景需要的关键词与最小覆盖。
BENCHMARK_SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "B-01",
        "desc": "1080p/2K H.264 单轨（原片/代理）",
        "match": ["h264", "H264"],
        "resolution_min": (1280, 720),
    },
    {
        "id": "B-02",
        "desc": "4K H.265 10-bit 单轨（硬解可用/不可用机）",
        "match": ["4K_H265"],
        "resolution_min": (3200, 1600),
    },
    {
        "id": "B-03",
        "desc": "4K ProRes / DNxHR 单轨（软解/upload/代理接管）",
        "match": ["4K_prores", "4K_DNxHR"],
        "resolution_min": (3200, 1600),
    },
    {
        "id": "B-04",
        "desc": "4K Topos 422 Proxy 单轨（decode/import/磁盘/seek/并发导出）",
        "match": ["4K_topos"],
        "resolution_min": (3200, 1600),
    },
    {
        "id": "B-05",
        "desc": "双轨 + GPU 转场 + 调色（pass GPU time/copy bytes）",
        "match": ["2K_prores422HQ", "4K_prores422HQ"],
        "resolution_min": (1280, 720),
    },
    {
        "id": "B-06",
        "desc": "四轨 + 标题 + 字幕 + 常用 effects（render plan/缓存/池峰值）",
        "match": ["2K_h264", "2K_topos"],
        "resolution_min": (1280, 720),
    },
]


def ffprobe_media(path: Path) -> Optional[Dict[str, Any]]:
    """ffprobe 提取流级元数据（json）。失败返回 None。"""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        # OSError 覆盖 ffprobe 未安装（FileNotFoundError）——最常见的失败。
        print(f"  !! ffprobe failed: {e}", file=sys.stderr)
        return None
    video = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    fmt = data.get("format", {})
    fps_str = video.get("r_frame_rate", "0/1")
    try:
        num, den = fps_str.split("/")
        fps = round(float(num) / float(den), 4) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "codec": video.get("codec_name"),
        "codec_long": video.get("codec_long_name"),
        "profile": video.get("profile"),
        "pix_fmt": video.get("pix_fmt"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "duration_s": round(float(fmt.get("duration", 0.0)) or 0.0, 3),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "bit_rate": int(fmt.get("bit_rate", 0) or 0),
        "nb_frames": video.get("nb_frames"),
        "color_range": video.get("color_range"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
    }


def _sha256_partial(path: Path, head_bytes: int = 1 << 20) -> str:
    """快速指纹：size + 首尾各 1MB 的 sha256。"""
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(head_bytes))
        if size > head_bytes * 2:
            f.seek(-head_bytes, 2)
            h.update(f.read(head_bytes))
    return h.hexdigest()


def _sha256_full(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_media_dir(media_dir: Path, deep: bool = False) -> List[Dict[str, Any]]:
    """扫描媒体目录，产出带指纹与场景归属的条目列表。"""
    entries: List[Dict[str, Any]] = []
    files = sorted(
        p for p in media_dir.iterdir()
        if p.is_file() and p.name not in SKIP_NAMES
        and p.suffix.lower() in _MEDIA_SUFFIXES
    )
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}")
        meta = ffprobe_media(path)
        if meta is None:
            print("  !! skipped: ffprobe failed or no video stream")
            continue
        try:
            rel = str(path.relative_to(REPO_ROOT))
        except ValueError:
            rel = str(path)  # 仓库外媒体目录原样记录
        digest = _sha256_full(path) if deep else _sha256_partial(path)
        entries.append({
            "file": rel,
            "fingerprint": digest,
            "fingerprint_mode": "sha256_full" if deep else "sha256_head1mb_tail1mb+size",
            **meta,
        })
    return entries


def map_scenarios(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把素材条目映射到基准场景，标记缺口。"""
    scenarios: Dict[str, Any] = {}
    for spec in BENCHMARK_SCENARIOS:
        matched = []
        for e in entries:
            name = Path(e["file"]).name
            if any(m.lower() in name.lower() for m in spec["match"]):
                w, hgt = e.get("width") or 0, e.get("height") or 0
                mw, mh = spec.get("resolution_min", (0, 0))
                if w >= mw and hgt >= mh:
                    matched.append(e["file"])
        scenarios[spec["id"]] = {
            "desc": spec["desc"],
            "media": matched,
            "covered": bool(matched),
        }
    return scenarios


def machine_info() -> Dict[str, Any]:
    """机器信息（脱敏：不含用户名/路径）。"""
    info: Dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, timeout=10, check=True,
        ).stdout.decode().strip()
        info["git_commit"] = commit
    except (subprocess.SubprocessError, OSError):
        info["git_commit"] = "unknown"
    return info


def build_manifest(media_dir: Path, deep: bool) -> Dict[str, Any]:
    entries = scan_media_dir(media_dir, deep=deep)
    scenarios = map_scenarios(entries)
    return {
        "$schema_version": MANIFEST_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "purpose": (
            "P0-06 冻结的真实视频基准矩阵（tests/test_videos 必跑集）。"
            "每次功能/性能修复后受影响场景必须在本矩阵复验；"
            "synthetic fixture 不可替代。"
        ),
        "machine": machine_info(),
        "media_count": len(entries),
        "scenarios": scenarios,
        "gaps": [sid for sid, s in scenarios.items() if not s["covered"]],
        "media": entries,
    }


_MEDIA_SUFFIXES = {".mov", ".mp4", ".mxf", ".mkv", ".avi", ".webm"}


def check_manifest(path: Path) -> int:
    """校验已冻结清单：文件存在 + 指纹一致 + 无未登记新增文件。

    返回码：0=一致；1=漂移（缺失/指纹变化/未登记新增/坏条目）；2=清单缺失。
    """
    if not path.exists():
        print(f"manifest missing: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text())
    drift = 0
    known_names = set()
    for entry in data.get("media", []):
        rel = entry.get("file", "")
        known_names.add(Path(rel).name)
        f = REPO_ROOT / rel
        if not f.exists():
            print(f"MISSING: {rel}")
            drift += 1
            continue
        mode = entry.get("fingerprint_mode", "")
        if mode.startswith("sha256_head"):
            current = _sha256_partial(f)
        elif mode:
            current = _sha256_full(f)
        else:
            print(f"BAD_ENTRY: {rel} (missing fingerprint_mode)")
            drift += 1
            continue
        if current != entry["fingerprint"]:
            print(f"DRIFT: {rel}")
            drift += 1
    # 未登记新增文件：没有这一项，矩阵可被"删旧加新"静默替换
    # （只表现为一条 MISSING），CI 无法发现未冻结媒体。
    media_dir = path.parent
    if media_dir.is_dir():
        on_disk = {
            p.name for p in media_dir.iterdir()
            if p.is_file() and p.name not in SKIP_NAMES
            and p.suffix.lower() in _MEDIA_SUFFIXES
        }
        for name in sorted(on_disk - known_names):
            print(f"NEW: {media_dir.name}/{name}")
            drift += 1
    print(f"checked {len(data.get('media', []))} entries, drift={drift}")
    return 1 if drift else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", type=Path, default=DEFAULT_MEDIA_DIR)
    parser.add_argument("--deep", action="store_true",
                        help="全文件 sha256（慢）；默认首尾 1MB 指纹")
    parser.add_argument("--check", action="store_true",
                        help="校验现有清单而不是重新生成")
    args = parser.parse_args()

    if args.check:
        return check_manifest(OUTPUT_PATH)

    if not args.media_dir.is_dir():
        print(f"media dir not found: {args.media_dir}", file=sys.stderr)
        return 2

    manifest = build_manifest(args.media_dir, deep=args.deep)
    OUTPUT_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nfrozen {manifest['media_count']} media entries -> {OUTPUT_PATH}")
    print(f"scenario coverage: "
          f"{sum(1 for s in manifest['scenarios'].values() if s['covered'])}"
          f"/{len(manifest['scenarios'])}")
    if manifest["gaps"]:
        print(f"gaps: {manifest['gaps']}")
        return 3  # 覆盖缺口必须可见（CI 门禁语义），不能静默通过
    return 0


if __name__ == "__main__":
    sys.exit(main())
