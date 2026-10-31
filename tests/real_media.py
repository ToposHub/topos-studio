"""真实视频测试素材统一入口（C02，2026-09-27 检查计划）。

规范语料 = ``tests/test_videos`` 下由 ``tools/generate_topos_baseline_corpus.py``
生成的 2K/4K × 六档 ProRes + TOPOS 集（487 帧 @25fps、带 pcm_s24le 音轨）；
帧数/字节/逐档校验冻结在已提交的
``tests/test_videos/topos_prores_baseline_manifest.json``（.gitignore 白名单），
大文件本身不入库——重建方式见 ``tests/test_videos/README.md``。

历史命名（``2K_topos_proxy.mov`` 等 96 帧小语料）已于 2026-09-22 重导时退役；
这里提供 ``canonical_video()`` 把旧名映射到冻结语料，旧名引用必须经此映射，
避免测试再次散落硬编码文件名。

跳过语义（禁止真实媒体回归静默缩水）：

- 默认（开发机缺素材）：``pytest.skip`` —— 不阻塞日常单测；
- ``TOPOS_REAL_MEDIA_STRICT=1``（real-media 专用作业/固定基准机）：
  素材缺失直接 ``pytest.fail`` —— 缺素材不允许表现为 skip。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_VIDEOS = REPO_ROOT / "tests" / "test_videos"
BASELINE_MANIFEST = TEST_VIDEOS / "topos_prores_baseline_manifest.json"

#: real-media 专用作业设置；置 1 后素材缺失 = fail（不是 skip）。
STRICT = os.environ.get("TOPOS_REAL_MEDIA_STRICT") == "1"

#: 2026-09-22 重导前的历史命名 → 冻结语料规范名（topos_prores_baseline_manifest
#: 的 topos_naming 字段同源）。旧档位小写名 + 兼容别名都映射到新名。
_LEGACY_ALIASES: dict[str, str] = {
    "2K_topos_proxy.mov": "2K_topos_422_Proxy.mov",
    "2K_topos_lt.mov": "2K_topos_422_LT.mov",
    "2K_topos_standard.mov": "2K_topos_422.mov",
    "2K_topos_hq.mov": "2K_topos_422_HQ.mov",
    "2K_topos_4444xq.mov": "2K_topos_4444_XQ.mov",
    "2K_topos.mov": "2K_topos_422_HQ.mov",
    "4K_topos_proxy.mov": "4K_topos_422_Proxy.mov",
    "4K_topos_lt.mov": "4K_topos_422_LT.mov",
    "4K_topos_standard.mov": "4K_topos_422.mov",
    "4K_topos_hq.mov": "4K_topos_422_HQ.mov",
    "4K_topos_4444xq.mov": "4K_topos_4444_XQ.mov",
    "4K_topos.mov": "4K_topos_422_HQ.mov",
}


def canonical_video(name: str) -> str:
    """历史/别名文件名 → 冻结语料规范名；规范名原样返回。"""
    return _LEGACY_ALIASES.get(name, name)


def _missing(names: "list[str]") -> None:
    if STRICT:
        pytest.fail(
            "real-media 作业素材缺失: "
            + ", ".join(names)
            + "（TOPOS_REAL_MEDIA_STRICT=1 禁止以 skip 缩水覆盖；"
              "重建方式见 tests/test_videos/README.md）"
        )
    pytest.skip("真实素材缺失: " + ", ".join(names))


def require_video(*names: str) -> Path:
    """返回第一个存在的素材路径（名字先经 canonical_video 归一）；
    全部缺失时按 STRICT 语义 skip/fail。"""
    resolved = [canonical_video(n) for n in names]
    for n in resolved:
        p = TEST_VIDEOS / n
        if p.is_file():
            return p
    _missing(resolved)


def baseline_entry(path: "Path | str") -> dict:
    """从已提交基线清单取该文件的冻结条目（帧数/字节/校验位）。

    素材不在清单 = 拒绝（无指纹口径的回归不允许进 real-media 作业）。
    """
    data = json.loads(BASELINE_MANIFEST.read_text(encoding="utf-8"))
    rel = str(Path(path).resolve().relative_to(REPO_ROOT))
    for entry in data["entries"]:
        if entry["path"] == rel:
            return entry
    raise AssertionError(
        f"素材未冻结在 {BASELINE_MANIFEST.name}: {rel}（请运行 "
        f"tools/generate_topos_baseline_corpus.py 更新清单后再跑）"
    )


def expected_frames(path: "Path | str") -> int:
    """该素材的冻结帧数（替代测试里写死的 96/240 等历史值）。"""
    return int(baseline_entry(path)["frames"])
