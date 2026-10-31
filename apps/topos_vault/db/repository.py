"""vault 数据读写仓储层。

封装所有 SQLite 访问，对外提供类型化的领域方法。上层（pipeline / server）只通过
本模块读写数据，不直接拼 SQL。

设计要点：
- **指纹是业务主键**：所有 upsert 以 ``fingerprint`` 为准，路径只是线索字段
- **三类素材分方法**：video 走 clip/transcript/face；audio 走 audio_meta；image 走 image_meta
- **路径状态机**：valid / missing / relocated / unknown，由 relink 模块更新
- **不静默降级**：写入失败抛异常，不返回假数据
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from apps.topos_vault.paths import get_db_path


# ---------------------------------------------------------------------- #
# 领域 dataclass（与表对应，但解耦于 SQL）
# ---------------------------------------------------------------------- #

@dataclass
class MediaSource:
    """三类素材统一的身份层。"""
    fingerprint: str
    media_type: str                  # video / audio / image / image_sequence
    path_original: str
    path_current: str | None = None
    path_relative: str | None = None
    file_size: int | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    path_status: str = "valid"       # valid / missing / relocated / unknown
    title: str | None = None
    topos_asset_id: str | None = None
    indexed_at: str | None = None
    updated_at: str | None = None


@dataclass
class Clip:
    """视频镜头片段。"""
    clip_id: str
    fingerprint: str
    clip_index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    shot_type: str | None = None
    keyframes: list[str] = field(default_factory=list)  # 路径数组
    status: str = "pending"


@dataclass
class TranscriptLine:
    """台词。"""
    line_id: int | None             # None = 尚未入库
    clip_id: str
    text: str
    start_sec: float | None = None
    end_sec: float | None = None
    speaker: str | None = None
    character: str | None = None
    emotion: str | None = None


@dataclass
class FaceTrack:
    """人脸轨迹。"""
    face_id: int | None
    clip_id: str
    character: str | None = None
    cluster_id: int | None = None
    face_angle: str | None = None
    completeness: str | None = None


@dataclass
class AudioMeta:
    """音乐/音频整曲元数据。"""
    fingerprint: str
    bpm: float | None = None
    key: str | None = None
    genre: str | None = None
    mood: str | None = None
    instruments: list[str] = field(default_factory=list)
    energy: float | None = None
    vocal_type: str | None = None
    description: str | None = None


@dataclass
class ImageMeta:
    """图片整图元数据。"""
    fingerprint: str
    tags: list[str] = field(default_factory=list)
    caption: str | None = None
    dominant_color: str | None = None
    orientation: str | None = None


# ---------------------------------------------------------------------- #
# Repository
# ---------------------------------------------------------------------- #

class VaultRepository:
    """vault.db 的读写仓储。

    每个方法内部开关连接（与主项目 src/shared/db/database.py 的模式一致），
    避免长连接在多线程下出问题。WAL 模式下读不阻塞写。
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(get_db_path())

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """获取连接的上下文管理器（自动 commit/rollback + 关闭）。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row  # 行可按列名访问
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ================================================================== #
    # media_source（统一身份层）
    # ================================================================== #

    def upsert_media_source(self, src: MediaSource) -> None:
        """插入或更新 media_source（以 fingerprint 为准）。"""
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO media_source (
                    fingerprint, media_type, path_original, path_current,
                    path_relative, file_size, duration_sec, width, height, fps, codec,
                    path_status, title, topos_asset_id, indexed_at, updated_at
                ) VALUES (
                    :fingerprint, :media_type, :path_original, :path_current,
                    :path_relative, :file_size, :duration_sec, :width, :height, :fps, :codec,
                    :path_status, :title, :topos_asset_id, :indexed_at, :updated_at
                )
                ON CONFLICT(fingerprint) DO UPDATE SET
                    path_current = excluded.path_current,
                    path_status  = excluded.path_status,
                    file_size    = COALESCE(excluded.file_size, media_source.file_size),
                    duration_sec = COALESCE(excluded.duration_sec, media_source.duration_sec),
                    width        = COALESCE(excluded.width, media_source.width),
                    height       = COALESCE(excluded.height, media_source.height),
                    updated_at   = :updated_at
                """,
                {
                    "fingerprint": src.fingerprint,
                    "media_type": src.media_type,
                    "path_original": src.path_original,
                    "path_current": src.path_current or src.path_original,
                    "path_relative": src.path_relative,
                    "file_size": src.file_size,
                    "duration_sec": src.duration_sec,
                    "width": src.width,
                    "height": src.height,
                    "fps": src.fps,
                    "codec": src.codec,
                    "path_status": src.path_status,
                    "title": src.title,
                    "topos_asset_id": src.topos_asset_id,
                    "indexed_at": src.indexed_at or now,
                    "updated_at": now,
                },
            )

    def get_media_source(self, fingerprint: str) -> MediaSource | None:
        """按指纹查单个素材。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM media_source WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return _row_to_media_source(row) if row else None

    def get_media_source_by_path(self, path: str) -> MediaSource | None:
        """按当前路径查（用于增量：已导入的不重复）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM media_source WHERE path_current = ? OR path_original = ?",
                (path, path),
            ).fetchone()
        return _row_to_media_source(row) if row else None

    def list_media_sources(
        self, media_type: str | None = None, path_status: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[MediaSource]:
        """列出素材（可按类型/状态过滤）。"""
        clauses = []
        params: list[Any] = []
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        if path_status:
            clauses.append("path_status = ?")
            params.append(path_status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM media_source{where} ORDER BY indexed_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [_row_to_media_source(r) for r in rows]

    def update_path_status(
        self, fingerprint: str, path_current: str | None, status: str,
    ) -> None:
        """更新路径状态（relink 后调用）。"""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE media_source
                SET path_current = ?, path_status = ?, updated_at = ?
                WHERE fingerprint = ?
                """,
                (path_current, status, _now_iso(), fingerprint),
            )

    def count_media_sources(self, media_type: str | None = None) -> int:
        """统计素材数（用于 status 命令）。"""
        with self._conn() as conn:
            if media_type:
                row = conn.execute(
                    "SELECT COUNT(*) FROM media_source WHERE media_type = ?", (media_type,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM media_source").fetchone()
        return int(row[0]) if row else 0

    # ================================================================== #
    # clip（视频镜头）
    # ================================================================== #

    def insert_clip(self, clip: Clip) -> None:
        """插入镜头片段（clip_id 未生成时自动生成 UUID）。"""
        if not clip.clip_id:
            clip.clip_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO clip (clip_id, fingerprint, clip_index, start_sec, end_sec,
                                  duration_sec, shot_type, keyframes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip.clip_id, clip.fingerprint, clip.clip_index,
                    clip.start_sec, clip.end_sec, clip.duration_sec,
                    clip.shot_type, json.dumps(clip.keyframes, ensure_ascii=False),
                    clip.status,
                ),
            )

    def list_clips(self, fingerprint: str) -> list[Clip]:
        """列出某素材的所有镜头（按顺序）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM clip WHERE fingerprint = ? ORDER BY clip_index",
                (fingerprint,),
            ).fetchall()
        return [_row_to_clip(r) for r in rows]

    def update_clip_status(self, clip_id: str, status: str) -> None:
        """更新镜头打标状态。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE clip SET status = ? WHERE clip_id = ?", (status, clip_id)
            )

    def update_clip_keyframes(
        self, clip_id: str, keyframes: list[str], shot_type: str | None = None,
    ) -> None:
        """更新镜头的关键帧路径数组 + 画面景别（visual/caption step 写入）。

        Args:
            clip_id: 镜头 id。
            keyframes: 关键帧 jpg 路径列表。
            shot_type: 景别（特写/近景/中景/全景/远景），None 不更新。
        """
        with self._conn() as conn:
            if shot_type is not None:
                conn.execute(
                    "UPDATE clip SET keyframes = ?, shot_type = ? WHERE clip_id = ?",
                    (json.dumps(keyframes, ensure_ascii=False), shot_type, clip_id),
                )
            else:
                conn.execute(
                    "UPDATE clip SET keyframes = ? WHERE clip_id = ?",
                    (json.dumps(keyframes, ensure_ascii=False), clip_id),
                )

    def get_clip(self, clip_id: str) -> Clip | None:
        """按 clip_id 查单个镜头。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM clip WHERE clip_id = ?", (clip_id,)
            ).fetchone()
        return _row_to_clip(row) if row else None

    # ================================================================== #
    # transcript_line（台词）
    # ================================================================== #

    def insert_transcript_lines(self, lines: Sequence[TranscriptLine]) -> None:
        """批量插入台词。"""
        if not lines:
            return
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO transcript_line
                    (clip_id, text, start_sec, end_sec, speaker, character, emotion)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (l.clip_id, l.text, l.start_sec, l.end_sec, l.speaker,
                     l.character, l.emotion)
                    for l in lines
                ],
            )

    def list_transcript_lines(self, clip_id: str) -> list[TranscriptLine]:
        """列出某镜头的台词。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_line WHERE clip_id = ? ORDER BY start_sec",
                (clip_id,),
            ).fetchall()
        return [_row_to_transcript(r) for r in rows]

    # ================================================================== #
    # face_track（人脸）
    # ================================================================== #

    def insert_face_tracks(self, tracks: Sequence[FaceTrack]) -> None:
        """批量插人人脸记录。"""
        if not tracks:
            return
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO face_track
                    (clip_id, character, cluster_id, face_angle, completeness)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (t.clip_id, t.character, t.cluster_id, t.face_angle, t.completeness)
                    for t in tracks
                ],
            )

    def list_face_tracks(self, clip_id: str) -> list[FaceTrack]:
        """列出某镜头的人脸记录（按 face_id 升序）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM face_track WHERE clip_id = ? ORDER BY face_id",
                (clip_id,),
            ).fetchall()
        return [_row_to_face_track(r) for r in rows]

    def list_all_face_tracks(self, fingerprint: str | None = None) -> list[FaceTrack]:
        """列出所有人脸记录（可按素材指纹过滤，需 JOIN clip）。

        Args:
            fingerprint: 限定某素材。None = 全部。
        """
        with self._conn() as conn:
            if fingerprint is None:
                rows = conn.execute(
                    "SELECT * FROM face_track ORDER BY face_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT ft.* FROM face_track ft
                    JOIN clip c ON ft.clip_id = c.clip_id
                    WHERE c.fingerprint = ?
                    ORDER BY ft.face_id
                    """,
                    (fingerprint,),
                ).fetchall()
        return [_row_to_face_track(r) for r in rows]

    def update_face_cluster(self, face_id: int, cluster_id: int | None,
                            character: str | None = None) -> None:
        """更新人脸的聚类簇 / 角色名（聚类步骤或人工标注调用）。"""
        with self._conn() as conn:
            if character is not None:
                conn.execute(
                    "UPDATE face_track SET cluster_id = ?, character = ? WHERE face_id = ?",
                    (cluster_id, character, face_id),
                )
            else:
                conn.execute(
                    "UPDATE face_track SET cluster_id = ? WHERE face_id = ?",
                    (cluster_id, face_id),
                )

    def list_character_clusters(self) -> list[dict[str, Any]]:
        """列出所有聚类簇 + 角色名（统计用，给人工标注界面）。

        Returns:
            [{cluster_id, character, face_count, sample_face_ids: [...]}, ...]
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT cluster_id, character, COUNT(*) as cnt,
                       GROUP_CONCAT(face_id) as face_ids
                FROM face_track
                WHERE cluster_id IS NOT NULL
                GROUP BY cluster_id
                ORDER BY cnt DESC
                """
            ).fetchall()
        result = []
        for r in rows:
            face_ids_str = r["face_ids"] or ""
            face_ids = [int(x) for x in face_ids_str.split(",") if x]
            result.append({
                "cluster_id": r["cluster_id"],
                "character": r["character"],
                "face_count": r["cnt"],
                "sample_face_ids": face_ids[:5],  # 取前 5 个做样本
            })
        return result

    # ================================================================== #
    # audio_meta（音乐/音频）
    # ================================================================== #

    def upsert_audio_meta(self, meta: AudioMeta) -> None:
        """插入或更新音乐元数据。"""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO audio_meta
                    (fingerprint, bpm, key, genre, mood, instruments, energy,
                     vocal_type, description, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    bpm = COALESCE(excluded.bpm, audio_meta.bpm),
                    key = COALESCE(excluded.key, audio_meta.key),
                    genre = COALESCE(excluded.genre, audio_meta.genre),
                    mood = COALESCE(excluded.mood, audio_meta.mood),
                    instruments = excluded.instruments,
                    energy = COALESCE(excluded.energy, audio_meta.energy),
                    vocal_type = COALESCE(excluded.vocal_type, audio_meta.vocal_type),
                    description = COALESCE(excluded.description, audio_meta.description),
                    updated_at = excluded.updated_at
                """,
                (
                    meta.fingerprint, meta.bpm, meta.key, meta.genre, meta.mood,
                    json.dumps(meta.instruments, ensure_ascii=False),
                    meta.energy, meta.vocal_type, meta.description, _now_iso(),
                ),
            )

    def get_audio_meta(self, fingerprint: str) -> AudioMeta | None:
        """查音乐元数据。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM audio_meta WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return _row_to_audio_meta(row) if row else None

    # ================================================================== #
    # image_meta（图片）
    # ================================================================== #

    def upsert_image_meta(self, meta: ImageMeta) -> None:
        """插入或更新图片元数据。"""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO image_meta
                    (fingerprint, tags, caption, dominant_color, orientation, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    tags = excluded.tags,
                    caption = COALESCE(excluded.caption, image_meta.caption),
                    dominant_color = COALESCE(excluded.dominant_color, image_meta.dominant_color),
                    orientation = COALESCE(excluded.orientation, image_meta.orientation),
                    updated_at = excluded.updated_at
                """,
                (
                    meta.fingerprint,
                    json.dumps(meta.tags, ensure_ascii=False),
                    meta.caption, meta.dominant_color, meta.orientation, _now_iso(),
                ),
            )

    def get_image_meta(self, fingerprint: str) -> ImageMeta | None:
        """查图片元数据。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM image_meta WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return _row_to_image_meta(row) if row else None

    # ================================================================== #
    # vector_index（FAISS 向量映射）
    # ================================================================== #

    def register_vector(
        self, index_name: str, ref_table: str, ref_id: str, model: str, dim: int,
    ) -> int:
        """登记一条 FAISS 向量，返回分配的 vector_id。

        vector_id 必须与 FAISS 索引内部的 id 一致——调用方需先写 FAISS（拿到 id）
        再调本方法登记，或用本方法返回的 id 作为 FAISS 的 add 顺序。
        """
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO vector_index (index_name, ref_table, ref_id, model, dim)
                VALUES (?, ?, ?, ?, ?)
                """,
                (index_name, ref_table, ref_id, model, dim),
            )
            return int(cur.lastrowid)

    def get_vector_ref(
        self, index_name: str, vector_id: int,
    ) -> tuple[str, str] | None:
        """根据 FAISS vector_id 查回 (ref_table, ref_id)。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ref_table, ref_id FROM vector_index WHERE vector_id = ? AND index_name = ?",
                (vector_id, index_name),
            ).fetchone()
        return (row["ref_table"], row["ref_id"]) if row else None

    def list_vector_ids(
        self, index_name: str, ref_ids: Sequence[str],
    ) -> dict[str, int]:
        """查一批 ref_id 对应的 vector_id（用于粗筛后的小范围向量检索）。

        Returns:
            {ref_id: vector_id} 映射。
        """
        if not ref_ids:
            return {}
        placeholders = ",".join("?" * len(ref_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT ref_id, vector_id FROM vector_index
                WHERE index_name = ? AND ref_id IN ({placeholders})
                """,
                [index_name, *ref_ids],
            ).fetchall()
        return {r["ref_id"]: r["vector_id"] for r in rows}


# ---------------------------------------------------------------------- #
# 行 → dataclass 转换（私有）
# ---------------------------------------------------------------------- #

def _now_iso() -> str:
    """当前 UTC 时间 ISO 字符串（与 datetime('now') 对齐）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_media_source(row: sqlite3.Row) -> MediaSource:
    return MediaSource(
        fingerprint=row["fingerprint"],
        media_type=row["media_type"],
        path_original=row["path_original"],
        path_current=row["path_current"],
        path_relative=row["path_relative"],
        file_size=row["file_size"],
        duration_sec=row["duration_sec"],
        width=row["width"],
        height=row["height"],
        fps=row["fps"],
        codec=row["codec"],
        path_status=row["path_status"],
        title=row["title"],
        topos_asset_id=row["topos_asset_id"],
        indexed_at=row["indexed_at"],
        updated_at=row["updated_at"],
    )


def _row_to_clip(row: sqlite3.Row) -> Clip:
    kf = row["keyframes"]
    return Clip(
        clip_id=row["clip_id"],
        fingerprint=row["fingerprint"],
        clip_index=row["clip_index"],
        start_sec=row["start_sec"],
        end_sec=row["end_sec"],
        duration_sec=row["duration_sec"],
        shot_type=row["shot_type"],
        keyframes=json.loads(kf) if kf else [],
        status=row["status"],
    )


def _row_to_transcript(row: sqlite3.Row) -> TranscriptLine:
    return TranscriptLine(
        line_id=row["line_id"],
        clip_id=row["clip_id"],
        text=row["text"],
        start_sec=row["start_sec"],
        end_sec=row["end_sec"],
        speaker=row["speaker"],
        character=row["character"],
        emotion=row["emotion"],
    )


def _row_to_audio_meta(row: sqlite3.Row) -> AudioMeta:
    instr = row["instruments"]
    return AudioMeta(
        fingerprint=row["fingerprint"],
        bpm=row["bpm"],
        key=row["key"],
        genre=row["genre"],
        mood=row["mood"],
        instruments=json.loads(instr) if instr else [],
        energy=row["energy"],
        vocal_type=row["vocal_type"],
        description=row["description"],
    )


def _row_to_image_meta(row: sqlite3.Row) -> ImageMeta:
    tags = row["tags"]
    return ImageMeta(
        fingerprint=row["fingerprint"],
        tags=json.loads(tags) if tags else [],
        caption=row["caption"],
        dominant_color=row["dominant_color"],
        orientation=row["orientation"],
    )


def _row_to_face_track(row: sqlite3.Row) -> FaceTrack:
    return FaceTrack(
        face_id=row["face_id"],
        clip_id=row["clip_id"],
        character=row["character"],
        cluster_id=row["cluster_id"],
        face_angle=row["face_angle"],
        completeness=row["completeness"],
    )
