"""素材重连（relink）——三级匹配机制。

素材移动/重命名/换盘后，如何重新连接到原有的打标数据。

核心思路（与项目主 app 的 RelinkMediaDialog 对齐，但多一级指纹精确匹配）：

    Level 0（0ms）：path_current 直接存在 → 用
    Level 1（<1ms）：path_original 同目录 + 同名 + 同 file_size → 用
        （覆盖"整个文件夹一起移动"的最常见场景，如换外接盘挂载点）
    Level 2（核心，按类型算指纹）：候选目录扫描 → 指纹精确匹配
        video: xxhash 采样 / audio: Chromaprint / image: pHash
        （解决改名/换盘/改后缀——只要内容一样就能认出）
    Level 3（兜底）：ffprobe 抽 duration + WxH + codec → 模糊匹配 → 用户确认
        （覆盖重编码场景：字节变了但内容是同一部片）

relink 幂等：可反复跑，只更新 path_current + path_status，不动打标数据。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterator, Sequence

from apps.topos_vault.db.repository import MediaSource, VaultRepository
from apps.topos_vault.fingerprint import verify_fingerprint, parse_fingerprint_type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 结果数据类
# ---------------------------------------------------------------------- #

@dataclass
class RelinkResult:
    """单个素材的重连结果。"""
    fingerprint: str
    media_type: str
    found: bool
    new_path: str | None
    matched_level: int          # 0/1/2/3，0=最便宜直接命中，-1=未找到
    detail: str = ""

    def __str__(self) -> str:
        if not self.found:
            return f"[MISS] {self.fingerprint[:24]}... ({self.media_type}) {self.detail}"
        level_desc = {0: "路径直查", 1: "同目录同名", 2: "指纹匹配", 3: "特征兜底"}.get(
            self.matched_level, "?"
        )
        return f"[OK L{self.matched_level}:{level_desc}] {self.fingerprint[:24]}... → {self.new_path}"


@dataclass
class RelinkReport:
    """整批 relink 的汇总报告。"""
    total: int = 0
    found: int = 0
    missed: int = 0
    by_level: dict[int, int] = None  # type: ignore
    results: list[RelinkResult] = None  # type: ignore

    def __post_init__(self):
        if self.by_level is None:
            self.by_level = {}
        if self.results is None:
            self.results = []

    def summary(self) -> str:
        lines = [
            f"relink 报告：{self.found}/{self.total} 找到，{self.missed} 未找到",
            f"  Level 分布：{self.by_level}",
        ]
        if self.missed > 0:
            lines.append("  未找到的素材（需手动指认或检查候选目录）：")
            for r in self.results:
                if not r.found:
                    lines.append(f"    {r}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Relinker
# ---------------------------------------------------------------------- #

class Relinker:
    """素材重连器：对 missing 素材执行三级匹配。

    Usage:
        relinker = Relinker(repo)
        report = relinker.relink_missing(candidate_dirs=["/Volumes/NewDisk"])
        print(report.summary())
    """

    def __init__(self, repo: VaultRepository):
        self.repo = repo

    # ------------------------------------------------------------------ #
    # 状态检测：哪些素材丢了？
    # ------------------------------------------------------------------ #

    def refresh_path_status(self) -> int:
        """扫描所有素材，更新 path_status（valid/missing）。

        调用 relink 前先跑一遍，把"路径还指向有效文件"和"已丢失"分开。

        Returns:
            missing 数量（path_status 被更新为 'missing' 的素材数）。
        """
        missing_count = 0
        for src in self.repo.list_media_sources(limit=100000):
            current = src.path_current or src.path_original
            if os.path.exists(current):
                # 路径有效：统一标 valid（含之前 relocated 但路径仍有效的——
                # refresh 应反映当前事实，而不是冻结历史状态）
                if src.path_status != "valid":
                    self.repo.update_path_status(src.fingerprint, current, "valid")
            else:
                # 路径无效：无论之前是 valid/relocated/missing，都标 missing
                # （修复原 bug：relocated 素材二次移动后不更新，导致检测不到丢失）
                if src.path_status != "missing":
                    self.repo.update_path_status(src.fingerprint, current, "missing")
                    missing_count += 1
        return missing_count

    # ------------------------------------------------------------------ #
    # 三级匹配
    # ------------------------------------------------------------------ #

    def relink_missing(
        self,
        candidate_dirs: Sequence[str],
        max_per_level: int = 0,
    ) -> RelinkReport:
        """对 path_status='missing' 的素材执行三级重连。

        Args:
            candidate_dirs: 用户指定的候选搜索目录（如新的外接盘根目录）。
            max_per_level: 每个素材在某 Level 最多检查多少个候选文件（0=不限）。

        Returns:
            RelinkReport 汇总。
        """
        report = RelinkReport()
        missing = [
            s for s in self.repo.list_media_sources(path_status="missing", limit=100000)
        ]
        report.total = len(missing)

        # 收集候选目录下的所有文件（一次性扫描，避免重复 walk）
        candidates_by_dir = {
            d: list(_walk_files(d, max_per_level)) for d in candidate_dirs
        }

        for src in missing:
            result = self._relink_one(src, candidate_dirs, candidates_by_dir)
            report.results.append(result)
            if result.found:
                report.found += 1
                report.by_level[result.matched_level] = (
                    report.by_level.get(result.matched_level, 0) + 1
                )
                # 更新数据库
                self.repo.update_path_status(
                    src.fingerprint, result.new_path, "relocated"
                )
            else:
                report.missed += 1

        return report

    def relink_one(
        self, fingerprint: str, candidate_dirs: Sequence[str],
    ) -> RelinkResult:
        """对单个素材执行重连（供 UI 单独调用）。"""
        src = self.repo.get_media_source(fingerprint)
        if src is None:
            return RelinkResult(
                fingerprint=fingerprint, media_type="unknown",
                found=False, new_path=None, matched_level=-1,
                detail="fingerprint 不在库中",
            )
        candidates_by_dir = {d: list(_walk_files(d)) for d in candidate_dirs}
        return self._relink_one(src, candidate_dirs, candidates_by_dir)

    # ------------------------------------------------------------------ #
    # 三级匹配的内部实现
    # ------------------------------------------------------------------ #

    def _relink_one(
        self,
        src: MediaSource,
        candidate_dirs: Sequence[str],
        candidates_by_dir: dict[str, list[str]],
    ) -> RelinkResult:
        """对单个素材依次尝试 Level 0/1/2/3。"""

        # ---- Level 0：path_current 直接命中 ----
        current = src.path_current or src.path_original
        if current and os.path.exists(current):
            return RelinkResult(
                fingerprint=src.fingerprint, media_type=src.media_type,
                found=True, new_path=current, matched_level=0,
                detail="记录的路径仍然有效",
            )

        # ---- Level 1：path_original 同目录 + 同名 + 同 file_size ----
        level1 = self._try_level1(src)
        if level1 is not None:
            return level1

        # ---- Level 2：候选目录指纹匹配 ----
        for d in candidate_dirs:
            level2 = self._try_level2(src, candidates_by_dir.get(d, []))
            if level2 is not None:
                return level2

        # ---- Level 3：媒体特征模糊匹配 ----
        for d in candidate_dirs:
            level3 = self._try_level3(src, candidates_by_dir.get(d, []))
            if level3 is not None:
                return level3

        # 全部失败
        return RelinkResult(
            fingerprint=src.fingerprint, media_type=src.media_type,
            found=False, new_path=None, matched_level=-1,
            detail=f"三级匹配未命中（候选目录 {list(candidate_dirs)} 无匹配）",
        )

    def _try_level1(self, src: MediaSource) -> RelinkResult | None:
        """Level 1：原目录还在？同名文件？file_size 相等？"""
        orig_dir = os.path.dirname(src.path_original)
        orig_name = os.path.basename(src.path_original)
        candidate = os.path.join(orig_dir, orig_name)

        if not os.path.exists(candidate):
            return None

        # file_size 快速筛
        try:
            actual_size = os.path.getsize(candidate)
        except OSError:
            return None

        if src.file_size is not None and actual_size != src.file_size:
            return None

        return RelinkResult(
            fingerprint=src.fingerprint, media_type=src.media_type,
            found=True, new_path=candidate, matched_level=1,
            detail="原目录同名 + 同大小",
        )

    def _try_level2(self, src: MediaSource, candidates: list[str]) -> RelinkResult | None:
        """Level 2：指纹精确匹配（核心级）。

        对每个候选文件用 verify_fingerprint 验证。指纹自带类型前缀，
        verify 内部分派到对应算法（xxhash/Chromaprint/pHash）。

        ⚠️ 降级警告：若指纹是 ``size:`` 前缀（依赖库缺失时的兜底），
        此级会退化为"纯文件大小匹配"——同大小不同文件会误配。
        装上 xxhash/pyacoustid/imagehash 后真实指纹能区分。降级模式下
        的匹配结果在 detail 里标注，供用户人工复核。
        """
        # 先用 file_size 粗筛（O(1)），减少要算指纹的候选数
        if src.file_size is not None:
            candidates = [
                c for c in candidates
                if _safe_size(c) == src.file_size
            ]

        fp_type = parse_fingerprint_type(src.fingerprint)
        is_degraded = fp_type == "size"

        for cand in candidates:
            try:
                if verify_fingerprint(cand, src.fingerprint):
                    detail = f"指纹匹配（{fp_type}）"
                    if is_degraded:
                        # size: 模式下多个同大小文件会都"匹配"，取第一个但明确警告
                        detail += " ⚠️降级模式(仅大小匹配)，建议装 xxhash/chromaprint/imagehash 后复核"
                    return RelinkResult(
                        fingerprint=src.fingerprint, media_type=src.media_type,
                        found=True, new_path=cand, matched_level=2,
                        detail=detail,
                    )
            except Exception as e:
                logger.debug("verify_fingerprint failed for %s: %s", cand, e)
                continue

        return None

    def _try_level3(self, src: MediaSource, candidates: list[str]) -> RelinkResult | None:
        """Level 3：媒体特征模糊匹配（兜底）。

        用 duration + width + height 组合签名匹配。覆盖"素材被重编码"的场景——
        字节和指纹都变了（xxhash/Chromaprint/pHash 失效），但时长和分辨率不变。

        ⚠️ 区分度低于指纹：不同素材可能有相同时长+分辨率。
        匹配后**不自动认领**，返回 found=True 但调用方应提示用户确认。
        """
        from apps.topos_vault.probe import probe_signature, MediaSignature

        # 从 media_source 已有字段构造原始签名（入库时记录的）
        if src.duration_sec is None:
            # 没记录时长，无法做特征匹配
            return None
        original_sig = MediaSignature(
            duration_sec=src.duration_sec,
            width=src.width,
            height=src.height,
            codec=src.codec,
        )

        # 按扩展名粗筛候选（减少要 probe 的文件数）
        orig_ext = os.path.splitext(src.path_original)[1].lower()
        ext_candidates = [
            c for c in candidates
            if os.path.splitext(c)[1].lower() == orig_ext
        ]

        for cand in ext_candidates:
            try:
                cand_sig = probe_signature(cand)
            except Exception as e:
                logger.debug("probe_signature failed for %s: %s", cand, e)
                continue

            if cand_sig is None:
                continue

            if original_sig.matches(cand_sig, tolerance=1.0):
                return RelinkResult(
                    fingerprint=src.fingerprint, media_type=src.media_type,
                    found=True, new_path=cand, matched_level=3,
                    detail=f"特征模糊匹配（dur≈{src.duration_sec:.0f}s "
                           f"{src.width}x{src.height}）⚠️区分度低，建议用户确认",
                )

        return None


# ---------------------------------------------------------------------- #
# 辅助函数
# ---------------------------------------------------------------------- #

def _walk_files(root: str, limit: int = 0) -> Iterator[str]:
    """遍历目录下所有文件（递归）。

    Args:
        root: 根目录。
        limit: 最多返回多少个文件（0=不限）。
    """
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            # 跳过隐藏文件和 vault 自己的缓存
            if name.startswith(".") or ".topos-vault" in dirpath:
                continue
            yield os.path.join(dirpath, name)
            count += 1
            if limit and count >= limit:
                return


def _safe_size(path: str) -> int | None:
    """安全的 file_size 读取（失败返回 None）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return None
