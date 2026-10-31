"""[U6/GAP-05] 应用内回收站。

删除流程改造：此前 /api/delete 直接 send2trash 进系统废纸篓并同步删除
meta 行——评分/标签即丢，应用内无法恢复。现在删除 = 文件移入
<file_explorer>/.trash/ + file_meta 行快照进 trash 表；恢复 = 移回原
路径（父目录丢失时自动重建、占用时自动改名）+ 还原 meta 行；彻底删除
= 物理移除并清理缩略图；30 天过期自动物理清理。

缩略图不搬迁：缩略图按源路径存放在源目录 .cache/ 下，恢复后路径不变
自动重新匹配；仅彻底删除时按原路径清理三尺寸缩略图。

跨卷：.trash 与素材可能不在同一卷，统一用 shutil.move（同卷快路径、
跨卷复制回退）；移动失败则回退 send2trash 并置 trashed_name=''——该行
不可应用内恢复，只能从系统废纸篓手工找回。"""
import json
import logging
import os
import shutil
import time
import unicodedata

from send2trash import send2trash

from db import _db, _meta_key, invalidate_cache

logger = logging.getLogger(__name__)

TRASH_RETENTION_DAYS = 30
_META_FIELDS = ("path", "tags", "comments", "rating", "cached_info", "color", "flag")


def get_trash_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trash")
    os.makedirs(d, exist_ok=True)
    return d


def _reserve_trashed_slot(name: str, is_dir: bool = False):
    """trash 目录内唯一槽位：保留扩展名，时间戳 + 独占创建占位。

    [U9] 此前仅用 int(time.time()) 做后缀——同一秒内删除两个同 basename
    的文件第二个会静默覆盖第一个，加了存在性循环兜底。
    [U10] 存在性检查与移动之间仍有 TOCTOU 窗口（waitress 多线程并发删除
    同名文件时两请求可算出同一候选名）。改为先独占创建占位锁死槽位，
    后到者必然 FileExistsError 递增重试。
    文件用 O_CREAT|O_EXCL 占位（rename 原子替换之）；目录用 mkdir 占位
    （POSIX rename 目录可原子替换空目录；文件占位会让 move 抛 EEXIST）。"""
    base, ext = os.path.splitext(name)
    stamp = int(time.time())
    trash_dir = get_trash_dir()
    n = 0
    while True:
        candidate = f"{base}__{stamp}{ext}" if n == 0 else f"{base}__{stamp}_{n}{ext}"
        slot = os.path.join(trash_dir, candidate)
        try:
            if is_dir:
                os.mkdir(slot)
            else:
                fd = os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            return slot, candidate
        except FileExistsError:
            n += 1


def _release_trashed_slot(slot: str, is_dir: bool = False) -> None:
    """移动失败时删除占位，避免残留锁死该候选名（目录占位仅剩空壳）。"""
    try:
        if not slot or not os.path.exists(slot):
            return
        if is_dir:
            os.rmdir(slot)  # 仅当为空时才移除
        else:
            os.remove(slot)
    except OSError:
        pass


def _move_into_slot(target: str, slot: str, is_dir: bool) -> None:
    """把 target 移到已占位的 slot 路径。

    文件：shutil.move 直接可用（同卷 rename 原子替换文件占位；跨卷
    copyfile 以 'wb' 截断占位后写入）。
    目录：shutil.move 对"已存在的目标目录"语义是移入其内层（错误路径），
    必须自己控制——同卷 os.rename 目录可原子替换空目录；跨卷 copytree
    合并进空占位目录后删除源。"""
    if not is_dir:
        shutil.move(target, slot)
        return
    try:
        os.rename(target, slot)
    except OSError:
        shutil.copytree(target, slot, dirs_exist_ok=True)
        shutil.rmtree(target)


def _like_escape(s: str) -> str:
    """[U9] LIKE 前缀匹配的通配符转义：目录名里的 % / _ 是合法字符，
    不转义会把无关目录的后代 meta 一并命中。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snapshot_meta(path: str):
    """取该路径（含目录的全部后代）的 file_meta 行快照。"""
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            """SELECT path, tags, comments, rating, cached_info, color, flag
               FROM file_meta WHERE path = ? OR path LIKE ? ESCAPE '\\'""",
            (_meta_key(path), _like_escape(_meta_key(path)) + "/%")).fetchall()
        return [dict(zip(_META_FIELDS, r)) for r in rows]
    except Exception as e:
        logger.warning(f"[U6] meta 快照失败 {path}: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()


def _delete_meta_rows(conn, path: str) -> None:
    """删除该路径及全部后代的 file_meta/file_tags 行（FTS 由 AD 触发器同步）。"""
    key = _meta_key(path)
    conn.execute("DELETE FROM file_meta WHERE path = ?", (key,))
    conn.execute("DELETE FROM file_meta WHERE path LIKE ? ESCAPE '\\'",
                 (_like_escape(key) + "/%",))
    conn.execute("""DELETE FROM file_tags WHERE file_path = ?
                    OR file_path LIKE ? ESCAPE '\\'""",
                 (key, _like_escape(key) + "/%"))


def trash_items(paths):
    """删除入口：移入应用内回收站。返回 (records, errors)。

    records: [{id, path, name, is_dir, in_os_trash}]（前端撤销栈据此恢复）。"""
    trash_dir = get_trash_dir()
    records, errors = [], []
    for p in paths:
        p = str(p)
        target = p
        if not os.path.exists(target):
            for norm in (unicodedata.normalize('NFD', p), unicodedata.normalize('NFC', p)):
                if os.path.exists(norm):
                    target = norm
                    break
        if not os.path.exists(target):
            errors.append(f"找不到文件: {os.path.basename(p)}")
            continue
        name = unicodedata.normalize('NFC', os.path.basename(target.rstrip('/')))
        is_dir = os.path.isdir(target)
        dest, trashed_name = _reserve_trashed_slot(name, is_dir=is_dir)
        meta_snapshot = _snapshot_meta(target)
        try:
            _move_into_slot(target, dest, is_dir)
        except Exception as e:
            _release_trashed_slot(dest, is_dir=is_dir)
            # 跨卷移动失败等：回退系统废纸篓（数据不丢，但应用内不可恢复）
            logger.warning(f"[U6] 移入应用内回收站失败，回退系统废纸篓 {target}: {e}")
            try:
                send2trash(target)
                dest = None
            except Exception as e2:
                errors.append(f"删除失败 {name}: {e2}")
                continue
        conn = None
        try:
            conn = _db()
            c = conn.cursor()
            c.execute(
                """INSERT INTO trash (original_path, item_name, is_dir, trashed_name,
                                      deleted_at, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_meta_key(p), name, 1 if is_dir else 0,
                 '' if dest is None else trashed_name,
                 time.time(), json.dumps(meta_snapshot, ensure_ascii=False)))
            # 快照已入库，原 meta 行统一删除（含系统废纸篓回退场景），
            # 防止智能文件夹/搜索查到幽灵记录
            _delete_meta_rows(conn, target)
            conn.commit()
            records.append({'id': c.lastrowid, 'path': p, 'name': name, 'is_dir': is_dir,
                            'in_os_trash': dest is None})
        except Exception as e:
            # [U10] 入库失败时文件已离开原目录——此前只报错不移回，文件
            # 躺在 .trash/<name>__<ts>（trash 表无记录：回收站视图不可见、
            # 过期清理永不触及）= 对用户"人间蒸发"。移回原位；移回也失败
            # 才降级为带路径的报错文案。
            if dest is not None and os.path.exists(dest) and os.path.exists(os.path.dirname(target.rstrip('/'))):
                try:
                    shutil.move(dest, target.rstrip('/'))
                    logger.info(f"[U6] trash 入库失败，文件已移回原位 {target}")
                except Exception as e2:
                    logger.critical(f"[U6] trash 入库失败且回移失败，文件滞留 {dest}: {e2}")
                    errors.append(f"删除失败 {name}: 元数据写入异常，文件暂存于 {dest}")
                    continue
            errors.append(f"删除失败 {name}: {e}")
            logger.error(f"[U6] trash 入库失败 {target}: {e}")
        finally:
            if conn is not None:
                conn.close()
        try:
            invalidate_cache(os.path.dirname(target.rstrip('/')))
        except Exception:
            pass
    return records, errors


def list_items():
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            """SELECT id, original_path, item_name, is_dir, deleted_at, meta_json
               FROM trash ORDER BY deleted_at DESC""").fetchall()
        items = []
        for rid, opath, name, is_dir, deleted_at, meta_json in rows:
            try:
                meta_rows = len(json.loads(meta_json or '[]'))
            except Exception:
                meta_rows = 0
            items.append({'id': rid, 'original_path': opath, 'name': name,
                          'is_dir': bool(is_dir), 'deleted_at': deleted_at,
                          'meta_rows': meta_rows})
        return items
    except Exception as e:
        logger.error(f"[U6] 回收站列表失败: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()


def _trashed_file_path(row) -> str:
    """trashed_name 为空 = 已回退系统废纸篓（不可应用内恢复）。"""
    if not row[3]:
        return None
    return os.path.join(get_trash_dir(), row[3])


def _remap_meta_path(old_path, root_old, root_new):
    """快照行路径 → 恢复后路径：根行用 root_new；后代按前缀平移。"""
    if not old_path:
        return root_new
    if old_path == root_old:
        return root_new
    prefix = root_old.rstrip('/') + '/'
    if old_path.startswith(prefix):
        return root_new.rstrip('/') + '/' + old_path[len(prefix):]
    return old_path


def restore_items(ids):
    """恢复：移回原路径 + 还原 meta 行。返回 (restored, errors)。"""
    restored, errors = [], []
    conn = None
    try:
        conn = _db()
        for rid in ids:
            row = conn.execute(
                """SELECT id, original_path, item_name, trashed_name, meta_json
                   FROM trash WHERE id = ?""", (rid,)).fetchone()
            if not row:
                errors.append(f"记录不存在: #{rid}")
                continue
            _rid, opath, name, trashed_name, meta_json = row
            src = _trashed_file_path(row)
            if not src or not os.path.exists(src):
                errors.append(f"文件已不在回收站: {name}")
                continue
            dest = opath
            parent = os.path.dirname(dest)
            try:
                if parent and not os.path.exists(parent):
                    os.makedirs(parent, exist_ok=True)
                if os.path.exists(dest):
                    # [U10] "(恢复)" 副本此前只加一次后缀——同名已有多条
                    # 恢复记录（反复导入同名→删除→恢复）时 shutil.move
                    # 静默覆盖刚恢复的文件。加计数循环兜底。
                    base, ext = os.path.splitext(name)
                    n = 1
                    while True:
                        cand = os.path.join(
                            parent,
                            f"{base} (恢复){ext}" if n == 1 else f"{base} (恢复) ({n}){ext}")
                        if not os.path.exists(cand):
                            dest = cand
                            break
                        n += 1
                shutil.move(src, dest)
            except Exception as e:
                errors.append(f"恢复失败 {name}: {e}")
                continue
            # 还原 meta 快照（已存在同路径行时不覆盖用户新数据）。
            # [U9] 回写失败时回滚 + 把文件放回回收站：此前只记日志继续，
            # 留下"文件已恢复但 trash 行还在"的僵尸记录（后续 purge 还会
            # 误删刚恢复文件的缩略图）。
            try:
                c = conn.cursor()
                for m in (json.loads(meta_json or '[]') if meta_json else []):
                    nk = _meta_key(_remap_meta_path(m.get('path'), opath, dest))
                    if c.execute("SELECT 1 FROM file_meta WHERE path = ?", (nk,)).fetchone():
                        continue
                    c.execute(
                        """INSERT INTO file_meta (path, tags, comments, rating,
                                                  cached_info, color, flag)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (nk, m.get('tags') or '', m.get('comments') or '',
                         m.get('rating') or 0, m.get('cached_info'),
                         m.get('color') or 0, m.get('flag') or 0))
                    for t in [x.strip() for x in (m.get('tags') or '').split(',')
                              if x.strip()]:
                        c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                        c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                                     SELECT ?, id FROM tags WHERE name = ?""", (nk, t))
                c.execute("DELETE FROM trash WHERE id = ?", (rid,))
                conn.commit()
            except Exception as e:
                conn.rollback()
                moved_back = False
                try:
                    if os.path.exists(dest):
                        shutil.move(dest, src)
                        moved_back = True
                except Exception as e2:
                    logger.critical(f"[U6] 恢复失败且回滚也失败，文件滞留 {dest}: {e2}")
                if moved_back:
                    errors.append(f"恢复失败 {name}: {e}")
                else:
                    # 文件已在原路径但记录未删——下次恢复会报"已不在回收站"
                    restored.append({'id': rid, 'path': dest, 'name': name})
                if moved_back or not os.path.exists(dest):
                    logger.error(f"[U6] 恢复元数据回写失败（已回滚） {dest}: {e}")
                continue
            restored.append({'id': rid, 'path': dest, 'name': name})
            try:
                invalidate_cache(os.path.dirname(dest))
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()
    return restored, errors


def _cleanup_thumbs(original_path: str) -> None:
    """彻底删除后按原路径清理三尺寸缩略图（恢复场景不需要——路径不变自动重配）。"""
    from thumb_engine import engine as thumb_engine
    try:
        for size in ('small', 'large', 'preview'):
            tp = thumb_engine.get_thumb_path(original_path, size_type=size)
            if tp and os.path.exists(tp):
                os.remove(tp)
    except Exception as e:
        logger.warning(f"[U6] 缩略图清理失败 {original_path}: {e}")


def purge_items(ids):
    """彻底删除：物理移除回收站内文件 + 清理缩略图。返回 (purged, errors)。"""
    purged, errors = [], []
    conn = None
    try:
        conn = _db()
        for rid in ids:
            row = conn.execute(
                """SELECT id, original_path, item_name, trashed_name
                   FROM trash WHERE id = ?""", (rid,)).fetchone()
            if not row:
                errors.append(f"记录不存在: #{rid}")
                continue
            _rid, opath, name, trashed_name = row
            try:
                src = _trashed_file_path(row)
                if src and os.path.exists(src):
                    try:
                        # [U10] os.remove 对目录恒抛 IsADirectoryError → 此前
                        # 目录的"彻底删除"实际全部落入系统废纸篓（不释放空间、
                        # 过期目录在 Finder 废纸篓无限堆积）。按类型分流。
                        if os.path.isdir(src):
                            shutil.rmtree(src)
                        else:
                            os.remove(src)
                    except FileNotFoundError:
                        pass  # 与恢复线程并发的窗口期：文件已被移走
                    except OSError:
                        send2trash(src)  # 只读卷等 remove 失败，兜底系统废纸篓
                _cleanup_thumbs(opath)
                conn.execute("DELETE FROM trash WHERE id = ?", (rid,))
                conn.commit()
                purged.append(name)
            except Exception as e:
                errors.append(f"彻底删除失败 {name}: {e}")
    finally:
        if conn is not None:
            conn.close()
    return purged, errors


_last_expired_check = 0.0


def purge_expired(days: int = TRASH_RETENTION_DAYS, force: bool = False) -> int:
    """物理清理超过保留期的回收站条目。带 1 小时节流（force 跳过，供启动调用）。"""
    global _last_expired_check
    now = time.time()
    if not force and now - _last_expired_check < 3600:
        return 0
    _last_expired_check = now
    cutoff = now - days * 86400
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT id FROM trash WHERE deleted_at < ?", (cutoff,)).fetchall()
        if not rows:
            return 0
    finally:
        if conn is not None:
            conn.close()
    purged, errors = purge_items([r[0] for r in rows])
    if purged:
        logger.info(f"[U6] 回收站过期清理：物理删除 {len(purged)} 项"
                    + (f"，失败 {len(errors)} 项" if errors else ""))
    return len(purged)
