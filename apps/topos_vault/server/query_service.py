"""vault 查询服务核心逻辑。

封装"搜索素材"的完整流程：
    1. SQL 粗筛（按 caption/tags/description LIKE + 类型过滤）
    2. 向量精排（可选，阶段3+实现：Qwen3-Embedding + FAISS）
    3. 格式化为 file_explorer 兼容的 {files: [...]} 结构

当前实现到 Level 1（纯 SQL），Level 2（向量检索）在阶段3 补。
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import Any

from apps.topos_vault.db.repository import MediaSource, VaultRepository


# ---------------------------------------------------------------------- #
# 扩展名 → file_explorer type 分类
# （必须对齐 main.py:485-492 的分类，否则缩略图分支和类型过滤失效）
# ---------------------------------------------------------------------- #

_EXT_VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wmv", ".flv", ".webm",
              ".mxf", ".r3d", ".ari", ".prores"}
_EXT_IMAGE = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".heic",
              ".exr", ".dpx", ".psd", ".cr2", ".nef", ".arw", ".ai"}
_EXT_AUDIO = {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg", ".wma", ".aiff"}


def _classify_file_type(path: str, media_type: str | None = None) -> str:
    """把文件路径转成 file_explorer 认识的 type 字符串。

    Args:
        path: 文件路径（取扩展名）。
        media_type: vault 的 media_type（video/audio/image），优先用这个。

    Returns:
        'video' / 'image' / 'audio' / 'file'（兜底）
    """
    if media_type == "video":
        return "video"
    if media_type == "audio":
        return "audio"
    if media_type in ("image", "image_sequence"):
        return "image"

    ext = os.path.splitext(path)[1].lower()
    if ext in _EXT_VIDEO:
        return "video"
    if ext in _EXT_IMAGE:
        return "image"
    if ext in _EXT_AUDIO:
        return "audio"
    return "file"


def _format_size(size: int | None) -> str:
    """字节大小 → 人类可读（对齐 file_explorer 的格式）。"""
    if not size:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"


def _to_file_explorer_dict(
    src: MediaSource,
    *,
    caption: str | None = None,
    tags: list[str] | None = None,
    mood: str | None = None,
    relevance: float = 0.0,
) -> dict[str, Any]:
    """把 vault 的 MediaSource 转成 file_explorer 渲染需要的字典。

    字段对齐 main.py:482-493 的 /api/list 返回格式。
    path 必须用正斜杠 + NFC 归一化（否则中文路径在 Win/Mac 出问题）。
    """
    path = src.path_current or src.path_original
    path = path.replace("\\", "/")
    path = unicodedata.normalize("NFC", path)

    ftype = _classify_file_type(path, src.media_type)

    # 拼一个展示用的子标题（caption/tags/mood），让用户在瀑布流里看到匹配原因
    subtitle_parts = []
    if caption:
        subtitle_parts.append(caption[:40])
    if tags:
        subtitle_parts.append(" ".join(f"#{t}" for t in tags[:3]))
    if mood:
        subtitle_parts.append(f"#{mood}")

    return {
        "name": os.path.basename(src.path_original),
        "path": path,
        "type": ftype,
        "size": _format_size(src.file_size),
        "mtime": (src.indexed_at or "")[:10],
        "t_ver": 0,            # 缩略图版本，0 = 首次生成
        "res": f"{src.width}x{src.height}" if src.width and src.height else None,
        # vault 特有字段（file_explorer 忽略，但我们的插件 JS 会用）
        "vault_caption": caption,
        "vault_tags": tags or [],
        "vault_mood": mood,
        "vault_media_type": src.media_type,
        "vault_relevance": round(relevance, 3),
        "vault_subtitle": " ".join(subtitle_parts) if subtitle_parts else "",
    }


# ---------------------------------------------------------------------- #
# 搜索结果
# ---------------------------------------------------------------------- #

@dataclass
class SearchHit:
    """单个搜索命中。"""
    source: MediaSource
    caption: str | None = None
    tags: list[str] | None = None
    mood: str | None = None
    relevance: float = 0.0      # 0-1，越高越相关


# ---------------------------------------------------------------------- #
# QueryService
# ---------------------------------------------------------------------- #

class QueryService:
    """素材检索服务。

    支持两种检索模式（自动叠加）：
        1. **SQL 关键词**：在 caption/tags/description/title 里 LIKE（精确子串匹配）
        2. **向量语义**：把 query 转向量，在 FAISS 索引里找相似的（语义近似匹配）

    向量检索是可选增强——向量库为空或 embed provider 不可用时，
    自动退化为纯 SQL，不报错（降级安全）。

    Usage:
        svc = QueryService(repo)
        hits = svc.search("微笑", media_type="image", limit=50)
        files = [svc.to_file_explorer_dict(h) for h in hits]
    """

    def __init__(self, repo: VaultRepository):
        self.repo = repo

    def search(
        self,
        query: str,
        media_type: str = "all",
        limit: int = 100,
        *,
        use_vector: bool = True,
    ) -> list[SearchHit]:
        """搜索素材（SQL 关键词 + 向量语义，自动合并）。

        Args:
            query: 搜索关键词。
            media_type: 'all' / 'video' / 'image' / 'audio'。
            limit: 最多返回多少条。
            use_vector: 是否启用向量语义检索（默认 True）。
                向量库为空或 provider 不可用时自动降级为纯 SQL。

        搜索策略（当前 Level 1 纯 SQL；阶段3 加向量精排）：
            1. 在 image_meta.tags / image_meta.caption 里找
            2. 在 audio_meta.mood / audio_meta.description 里找
            3. 在 media_source.title / path_original 里找（文件名兜底）
            4. 按类型过滤
        """
        query = query.strip()
        if not query:
            return []

        hits: list[SearchHit] = []
        seen_fps: set[str] = set()

        # 用原始 connection 直接查（repository 没有暴露复杂 JOIN 查询）
        import sqlite3
        from apps.topos_vault.paths import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            # --- 图片命中 ---
            if media_type in ("all", "image"):
                rows = conn.execute(
                    """
                    SELECT ms.*, im.tags, im.caption
                    FROM media_source ms
                    JOIN image_meta im ON ms.fingerprint = im.fingerprint
                    WHERE (im.tags LIKE ? OR im.caption LIKE ? OR ms.title LIKE ?)
                      AND ms.media_type IN ('image', 'image_sequence')
                      AND ms.path_status != 'missing'
                    ORDER BY ms.indexed_at DESC LIMIT ?
                    """,
                    (f"%{query}%", f"%{query}%", f"%{query}%", limit),
                ).fetchall()
                for row in rows:
                    fp = row["fingerprint"]
                    if fp in seen_fps:
                        continue
                    seen_fps.add(fp)
                    import json
                    tags = json.loads(row["tags"]) if row["tags"] else []
                    hits.append(SearchHit(
                        source=_row_to_media_source(row),
                        caption=row["caption"],
                        tags=tags,
                        relevance=_calc_relevance(query, [row["caption"] or "", " ".join(tags), row["title"] or ""]),
                    ))

            # --- 音频命中 ---
            if media_type in ("all", "audio"):
                rows = conn.execute(
                    """
                    SELECT ms.*, am.mood, am.description, am.instruments
                    FROM media_source ms
                    JOIN audio_meta am ON ms.fingerprint = am.fingerprint
                    WHERE (am.mood LIKE ? OR am.description LIKE ? OR am.instruments LIKE ?
                           OR ms.title LIKE ?)
                      AND ms.media_type = 'audio'
                      AND ms.path_status != 'missing'
                    ORDER BY ms.indexed_at DESC LIMIT ?
                    """,
                    (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", limit),
                ).fetchall()
                for row in rows:
                    fp = row["fingerprint"]
                    if fp in seen_fps:
                        continue
                    seen_fps.add(fp)
                    hits.append(SearchHit(
                        source=_row_to_media_source(row),
                        mood=row["mood"],
                        caption=row["description"],
                        relevance=_calc_relevance(query, [row["description"] or "", row["mood"] or "", row["title"] or ""]),
                    ))

            # --- 视频/通用命中（文件名 + title 兜底） ---
            if media_type in ("all", "video") or not hits:
                rows = conn.execute(
                    """
                    SELECT * FROM media_source
                    WHERE (title LIKE ? OR path_original LIKE ?)
                      AND path_status != 'missing'
                    ORDER BY indexed_at DESC LIMIT ?
                    """,
                    (f"%{query}%", f"%{query}%", limit),
                ).fetchall()
                for row in rows:
                    fp = row["fingerprint"]
                    if fp in seen_fps:
                        continue
                    seen_fps.add(fp)
                    mt = row["media_type"]
                    # 类型过滤
                    if media_type != "all":
                        type_map = {"video": "video", "image": "image",
                                    "audio": "audio", "image_sequence": "image"}
                        if type_map.get(mt) != media_type:
                            continue
                    hits.append(SearchHit(
                        source=_row_to_media_source(row),
                        relevance=_calc_relevance(query, [row["title"] or "", row["path_original"] or ""]),
                    ))

        finally:
            conn.close()

        # --- 向量语义检索（可选增强）---
        # 把 query 转向量，在 visual/transcript/audio_semantic 索引里找相似的。
        # 能搜到"画面是微笑但 caption 没写微笑"的素材。
        # 向量库为空或 provider 不可用 → 静默跳过，退化为纯 SQL 结果。
        if use_vector:
            vector_hits = self._vector_search(query, media_type, limit, seen_fps)
            hits.extend(vector_hits)

        # 类型过滤（对 image/audio 的 JOIN 查询已过滤，这里兜底 all 模式下的混合结果）
        if media_type != "all":
            type_map = {"video": "video", "image": "image",
                        "audio": "audio", "image_sequence": "image"}
            hits = [h for h in hits if type_map.get(h.source.media_type) == media_type]

        # 按相关度排序
        hits.sort(key=lambda h: h.relevance, reverse=True)
        return hits[:limit]

    def _vector_search(
        self, query: str, media_type: str, limit: int, seen_fps: set[str],
    ) -> list[SearchHit]:
        """向量语义检索（降级安全：任何环节失败都返回空列表）。

        流程：
            1. query → 向量（用 transcript_embed provider）
            2. 在 visual / transcript / audio_semantic 索引里搜索
            3. 把 vector_id 映射回业务表（clip / transcript_line / media_source）
            4. 加载 media_source 构造 SearchHit

        向量命中给较高 relevance（0.5-1.0，按 score 缩放），
        因为语义匹配比 SQL LIKE 更"懂"内容。
        """
        try:
            from apps.topos_vault.providers import get_provider, Capability
            from apps.topos_vault.db.faiss_store import get_vector_store
        except ImportError:
            return []

        # 1. query → 向量
        try:
            embed_provider = get_provider(Capability.TRANSCRIPT_EMBED)
            if not embed_provider.is_available():
                return []
            embed_result = embed_provider.embed_text(query)
            query_vec = embed_result.vector
        except Exception:
            return []

        # 2. 在各索引里搜索
        vector_hits: list[SearchHit] = []

        # 决定搜哪些索引（按 media_type）
        indices_to_search = []
        if media_type in ("all", "image", "video"):
            indices_to_search.append("visual")
        if media_type in ("all", "video"):
            indices_to_search.append("transcript")
        if media_type in ("all", "audio"):
            indices_to_search.append("audio_semantic")

        for index_name in indices_to_search:
            try:
                store = get_vector_store(index_name)
                if store.size == 0:
                    continue
                results = store.search(query_vec, top_k=limit)
                # 3. 映射回业务表
                hits_for_index = self._resolve_vector_results(
                    results, index_name, seen_fps,
                )
                vector_hits.extend(hits_for_index)
            except Exception:
                continue

        return vector_hits

    def _resolve_vector_results(
        self, results, index_name: str, seen_fps: set[str],
    ) -> list[SearchHit]:
        """把向量搜索结果（vector_id + score）映射回 SearchHit。

        向量库里的 id 是业务表 PK（clip_id / fingerprint / transcript_line_id）。
        不同索引的 id 含义不同：
            visual / audio_semantic → 直接是 media_source.fingerprint
            transcript → 是 transcript_line.line_id，需要 JOIN 到 clip 再到 source
        """
        import sqlite3
        from apps.topos_vault.paths import get_db_path

        if not results:
            return []

        hits: list[SearchHit] = []
        conn = sqlite3.connect(str(get_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            for r in results:
                # score 映射到 relevance：余弦 0.5-1.0 → relevance 0.5-1.0
                relevance = max(0.0, min(1.0, r.score))
                if relevance < 0.3:
                    continue  # 太不相关，跳过

                if index_name in ("visual", "audio_semantic"):
                    # id 直接是 fingerprint
                    row = conn.execute(
                        "SELECT * FROM media_source WHERE fingerprint = ? "
                        "AND path_status != 'missing'",
                        (r.id,),
                    ).fetchone()
                    if row and r.id not in seen_fps:
                        seen_fps.add(r.id)
                        hits.append(SearchHit(
                            source=_row_to_media_source(row),
                            relevance=relevance,
                        ))

                elif index_name == "transcript":
                    # id 是 transcript_line.line_id（字符串），JOIN 到 clip → source
                    row = conn.execute(
                        """
                        SELECT ms.*, tl.text, tl.character
                        FROM transcript_line tl
                        JOIN clip c ON tl.clip_id = c.clip_id
                        JOIN media_source ms ON c.fingerprint = ms.fingerprint
                        WHERE CAST(tl.line_id AS TEXT) = ?
                          AND ms.path_status != 'missing'
                        """,
                        (str(r.id),),
                    ).fetchone()
                    if row and row["fingerprint"] not in seen_fps:
                        seen_fps.add(row["fingerprint"])
                        hits.append(SearchHit(
                            source=_row_to_media_source(row),
                            caption=row["text"],
                            relevance=relevance,
                        ))
        finally:
            conn.close()

        return hits

    def to_file_explorer_dict(self, hit: SearchHit) -> dict[str, Any]:
        """把 SearchHit 转成 file_explorer 兼容的字典。"""
        return _to_file_explorer_dict(
            hit.source,
            caption=hit.caption,
            tags=hit.tags,
            mood=hit.mood,
            relevance=hit.relevance,
        )

    # ================================================================== #
    # 高级检索（P2-3）
    # ================================================================== #

    def search_similar_image(
        self, image_path: str, *, media_type: str = "all", limit: int = 20,
    ) -> list[SearchHit]:
        """以图搜图：上传一张图 → 在 visual 索引找画面相似的素材。

        Args:
            image_path: 查询图片路径（本地）。
            media_type: 限定结果类型。
            limit: 最多返回多少条。

        Returns:
            SearchHit 列表（按画面相似度降序）。
        """
        try:
            from apps.topos_vault.providers import get_provider, Capability
        except ImportError:
            return []

        # 查询图 → 向量
        try:
            provider = get_provider(Capability.VISUAL_EMBED)
            if not provider.is_available():
                return []
            embed = provider.embed_image(image_path)
            query_vec = embed.vector
        except Exception:
            return []

        # 在 visual 索引找相似
        from apps.topos_vault.db.faiss_store import get_vector_store
        try:
            store = get_vector_store("visual")
            if store.size == 0:
                return []
            results = store.search(query_vec, top_k=limit * 2)
        except Exception:
            return []

        # 映射回 media_source（visual 索引的 id 可能是 fingerprint 或 clip_id）
        hits = self._resolve_similar_results(results, media_type, limit)
        return hits

    def search_by_bpm(
        self, bpm: float, *, tolerance: float = 5.0, limit: int = 50,
    ) -> list[SearchHit]:
        """按 BPM 找节奏相近的音频。

        Args:
            bpm: 目标 BPM。
            tolerance: 容差（±N BPM 算相近，默认 5）。
            limit: 最多返回多少条。

        Returns:
            SearchHit 列表（按 BPM 差距升序，差距越小越相关）。
        """
        import sqlite3
        from apps.topos_vault.paths import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT ms.*, am.bpm, am.mood, am.description
                FROM media_source ms
                JOIN audio_meta am ON ms.fingerprint = am.fingerprint
                WHERE am.bpm IS NOT NULL
                  AND am.bpm BETWEEN ? AND ?
                  AND ms.media_type = 'audio'
                  AND ms.path_status != 'missing'
                ORDER BY ABS(am.bpm - ?) ASC
                LIMIT ?
                """,
                (bpm - tolerance, bpm + tolerance, bpm, limit),
            ).fetchall()
        finally:
            conn.close()

        hits: list[SearchHit] = []
        for row in rows:
            # relevance：BPM 差距越小越高（容差内映射到 0.5-1.0）
            diff = abs(float(row["bpm"]) - bpm)
            relevance = max(0.5, 1.0 - diff / max(tolerance, 1.0))
            hits.append(SearchHit(
                source=_row_to_media_source(row),
                caption=row["description"],
                mood=row["mood"],
                relevance=relevance,
            ))
        return hits

    def advanced_search(
        self,
        *,
        query: str = "",
        media_type: str = "all",
        character: str | None = None,
        cluster_id: int | None = None,
        mood: str | None = None,
        bpm_range: tuple[float, float] | None = None,
        duration_range: tuple[float, float] | None = None,
        min_relevance: float = 0.0,
        limit: int = 100,
    ) -> list[SearchHit]:
        """组合高级检索（多条件 AND 过滤）。

        所有条件都是可选的（None/空 = 不过滤）。条件之间是 AND 关系。

        Args:
            query: 关键词（走 SQL + 向量语义检索）。
            media_type: 类型过滤。
            character: 角色名（face_track.character）。
            cluster_id: 角色聚类簇 id。
            mood: 情绪标签。
            bpm_range: (min, max) BPM 范围。
            duration_range: (min, max) 时长范围（秒）。
            min_relevance: 最低相关度阈值（0-1）。
            limit: 最多返回多少条。
        """
        # 先用关键词检索（如有），拿到候选集
        if query.strip():
            candidates = self.search(query, media_type=media_type, limit=limit * 3)
        else:
            candidates = self._list_all_hits(media_type, limit * 3)

        # 逐条件过滤
        filtered: list[SearchHit] = []
        for hit in candidates:
            if min_relevance > 0 and hit.relevance < min_relevance:
                continue
            if not self._match_filters(hit, character, cluster_id, mood,
                                       bpm_range, duration_range):
                continue
            filtered.append(hit)

        filtered.sort(key=lambda h: h.relevance, reverse=True)
        return filtered[:limit]

    # ------------------------------------------------------------------ #
    # 高级检索内部辅助
    # ------------------------------------------------------------------ #

    def _resolve_similar_results(
        self, results, media_type: str, limit: int,
    ) -> list[SearchHit]:
        """把以图搜图的向量结果映射回 SearchHit（id 可能是 fingerprint 或 clip_id）。"""
        import sqlite3
        from apps.topos_vault.paths import get_db_path

        if not results:
            return []

        hits: list[SearchHit] = []
        seen: set[str] = set()
        conn = sqlite3.connect(str(get_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            for r in results:
                relevance = max(0.0, min(1.0, r.score))
                if relevance < 0.3:
                    continue
                # 先按 fingerprint 查
                row = conn.execute(
                    "SELECT * FROM media_source WHERE fingerprint = ? "
                    "AND path_status != 'missing'", (r.id,),
                ).fetchone()
                if row is None:
                    # 可能是 clip_id → JOIN 到 source
                    row = conn.execute(
                        """
                        SELECT ms.* FROM clip c
                        JOIN media_source ms ON c.fingerprint = ms.fingerprint
                        WHERE c.clip_id = ? AND ms.path_status != 'missing'
                        """, (r.id,),
                    ).fetchone()
                if row is None or row["fingerprint"] in seen:
                    continue
                # 类型过滤
                if media_type != "all":
                    type_map = {"video": "video", "image": "image",
                                "audio": "audio", "image_sequence": "image"}
                    if type_map.get(row["media_type"]) != media_type:
                        continue
                seen.add(row["fingerprint"])
                hits.append(SearchHit(
                    source=_row_to_media_source(row),
                    relevance=relevance,
                ))
                if len(hits) >= limit:
                    break
        finally:
            conn.close()
        return hits

    def _list_all_hits(self, media_type: str, limit: int) -> list[SearchHit]:
        """无关键词时列出所有素材（供 advanced_search 的候选集）。"""
        import sqlite3
        from apps.topos_vault.paths import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            if media_type == "all":
                rows = conn.execute(
                    "SELECT * FROM media_source WHERE path_status != 'missing' "
                    "ORDER BY indexed_at DESC LIMIT ?", (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM media_source WHERE path_status != 'missing' "
                    "AND media_type = ? ORDER BY indexed_at DESC LIMIT ?",
                    (media_type, limit),
                ).fetchall()
        finally:
            conn.close()
        return [SearchHit(source=_row_to_media_source(r), relevance=0.1) for r in rows]

    def _match_filters(
        self, hit: SearchHit,
        character: str | None, cluster_id: int | None, mood: str | None,
        bpm_range: tuple[float, float] | None,
        duration_range: tuple[float, float] | None,
    ) -> bool:
        """检查一个 hit 是否满足所有过滤条件。"""
        src = hit.source

        # 角色名 / 聚类（需查 face_track）
        if character is not None or cluster_id is not None:
            if not self._source_has_face(src.fingerprint, character, cluster_id):
                return False

        # 情绪
        if mood is not None:
            if hit.mood is None or mood.lower() not in hit.mood.lower():
                return False

        # BPM 范围（音频）
        if bpm_range is not None:
            bpm = self._get_audio_bpm(src.fingerprint)
            if bpm is None or not (bpm_range[0] <= bpm <= bpm_range[1]):
                return False

        # 时长范围
        if duration_range is not None:
            dur = src.duration_sec or 0
            if not (duration_range[0] <= dur <= duration_range[1]):
                return False

        return True

    def _source_has_face(
        self, fingerprint: str, character: str | None, cluster_id: int | None,
    ) -> bool:
        """该素材是否包含指定角色/聚类的人脸。"""
        import sqlite3
        from apps.topos_vault.paths import get_db_path
        conn = sqlite3.connect(str(get_db_path()))
        try:
            if character is not None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM face_track ft
                    JOIN clip c ON ft.clip_id = c.clip_id
                    WHERE c.fingerprint = ? AND ft.character = ?
                    """, (fingerprint, character),
                ).fetchone()
                return int(row[0]) > 0 if row else False
            if cluster_id is not None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM face_track ft
                    JOIN clip c ON ft.clip_id = c.clip_id
                    WHERE c.fingerprint = ? AND ft.cluster_id = ?
                    """, (fingerprint, cluster_id),
                ).fetchone()
                return int(row[0]) > 0 if row else False
        finally:
            conn.close()
        return False

    def _get_audio_bpm(self, fingerprint: str) -> float | None:
        """该素材的 BPM（audio_meta）。"""
        meta = self.repo.get_audio_meta(fingerprint)
        return meta.bpm if meta else None


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #

def _calc_relevance(query: str, fields: list[str]) -> float:
    """简单的相关度打分（0-1）。

    精确匹配 > 词包含 > 字符包含。阶段3 接向量检索后会被语义相似度取代。
    """
    q = query.lower().strip()
    if not q:
        return 0.0
    best = 0.0
    for field in fields:
        if not field:
            continue
        f = field.lower()
        if q == f:
            best = max(best, 1.0)
        elif q in f:
            # 出现位置越靠前越相关
            pos = f.index(q)
            best = max(best, 0.8 - (pos / max(len(f), 1)) * 0.2)
        else:
            # 分词匹配
            words = q.split()
            matched = sum(1 for w in words if w in f)
            if matched:
                best = max(best, 0.3 * (matched / len(words)))
    return best


def _row_to_media_source(row) -> MediaSource:
    """sqlite3.Row → MediaSource（复用 repository 的同名函数逻辑）。"""
    from apps.topos_vault.db.repository import _row_to_media_source as _conv
    return _conv(row)
