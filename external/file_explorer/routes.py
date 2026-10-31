"""[ARCH-1] 全部 Flask 路由：资产 API + 静态资源 + 智能文件夹查询。

import 本模块即完成 @app.route 注册（webapp.app 上的装饰器副作用），
唯一入口是 main.py 组合根；请勿再以 ``file_explorer.routes`` 包路径导入，
避免路由二次注册。"""
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from flask import make_response, render_template, request, jsonify, send_file, send_from_directory
from send2trash import send2trash
from werkzeug.exceptions import NotFound

import utils
import const
import trash
import webapp
from thumb_engine import (
    engine as thumb_engine, EXT_VIDEO, EXT_IMAGE_PRO, EXT_DESIGN, EXT_RAW_PHOTO,
    EXT_IMAGE_NORMAL, EXT_IMAGE_TOPOS, EXT_AUDIO, EXT_DOCS, EXT_CODE, EXT_ARCHIVE,
    TEXT_FILE_EXTENSIONS, should_skip_file, check_failure_cache,
)
from plugin_system import plugin_manager
from webapp import app, APP_TOKEN, ROOT_PATH, _COOKIE_NAME
from db import (
    _db, _meta_key, CACHE_LOCK, CACHE_TTL, DIR_CACHE,
    invalidate_cache, load_config, save_config, _absorb_nfd_twin, put_dir_cache,
)
from bridge import bridge
from thumbs import (
    thumbnail_queue, thumbnail_pending, thumbnail_lock,
    _rating_update_lock, _rating_update_queue,
    _thumbnail_queue_lock, _thumbnail_update_queue,
)

logger = logging.getLogger(__name__)


# [U5/GAP-03] 颜色标记与旗标的合法值域
COLOR_VALUES = {0, 1, 2, 3, 4, 5}   # 0=无 1红 2黄 3绿 4蓝 5紫
FLAG_VALUES = {0, 1, 2}             # 0=无 1旗标(选用) 2拒收


def _like_escape(s: str) -> str:
    """[U9] LIKE 通配符转义：用户输入的 % / _ 按字面匹配（配合 ESCAPE '\\'）。"""
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _date_str_to_epoch(s: str):
    """[V1/GAP-02] 'YYYY-MM-DD' → 当日零点 epoch 秒；解析失败返回 None。
    date_taken 无时区语义（EXIF 不带 TZ），按 UTC 日界切分保持稳定。"""
    import calendar
    try:
        t = time.strptime(str(s).strip()[:10], '%Y-%m-%d')
        return calendar.timegm(t)
    except (ValueError, TypeError):
        return None


def _clamp_update_value(data, key, allowed):
    """从请求体取 color/flag 类值：未提供返回 None（保持原值），非法返回 False。"""
    if key not in data or data[key] is None:
        return None
    try:
        v = int(data[key])
    except (TypeError, ValueError):
        return False
    return v if v in allowed else False


def _db_resolve_current() -> str:
    """[BUG-P1-04] 取惰性判定后的当前 DB 路径（db.DB_FILE 动态转发）。"""
    import db
    return db.DB_FILE


@app.route('/api/cache/invalidate', methods=['POST'])
def api_cache_invalidate():
    """[ARCH-10] 目录缓存失效端点，供宿主显式调用。

    此前 DIR_CACHE TTL 60s：宿主（Library 面板）改评分/标签后，素材浏览器
    再导航到同一目录最长 60s 仍显示旧值。宿主改完 meta 后 POST 本端点即可
    立即失效。body: {'path': '<目录>'}（失效单个目录，NFC/NFD 变体都清）
    或空 body（全部清空）。需 token（宿主持有 APP_TOKEN，见 adapter）。"""
    data = request.get_json(silent=True) or {}
    p = data.get('path')
    with CACHE_LOCK:
        if p:
            p = str(p).replace('\\', '/')
            for v in (p,
                      unicodedata.normalize('NFC', p),
                      unicodedata.normalize('NFD', p),
                      p.rstrip('/'),
                      p + '/'):
                DIR_CACHE.pop(v, None)
        else:
            DIR_CACHE.clear()
    return jsonify({'success': True})


_last_browsed_dir = None  # [SEC-2] 最近一次 /api/list 浏览的目录（上传白名单）


# --- Flask 路由定义 ---
@app.route('/')
def index():
    """主页渲染，处理主题和插件数据传递"""
    conf = load_config()
    last_path = conf.get("last_path", ROOT_PATH)
    if not os.path.exists(last_path): last_path = ROOT_PATH
    themes_dir = os.path.join(app.static_folder, 'themes')
    available_themes = ['toposcolor']  # 默认主题改为 toposcolor
    if os.path.exists(themes_dir):
        available_themes += [f[:-4] for f in os.listdir(themes_dir) if f.endswith('.css') and f[:-4] != 'toposcolor']
    resp = make_response(render_template('index.html',
                         root=ROOT_PATH.replace('\\', '/'),
                         start_path=last_path.replace('\\', '/'),
                         timestamp=int(time.time()),
                         window_width=conf.get("window_width", 1400),
                         window_height=conf.get("window_height", 900),
                         sidebar_width=conf.get("sidebar_width", 260),
                         theme_list=available_themes,
                         current_theme=conf.get('theme', 'toposcolor'),  # 默认使用 toposcolor
                         plugin_js_files=plugin_manager.js_extensions,
                         # [BUG-P1-12] 传原始 dict，模板侧用 | tojson 安全嵌入
                         # （旧 json.dumps+JSON.parse 单引号包裹，插件名含 ' 即瘫痪前端）
                         file_handlers=plugin_manager.file_handlers,
                         ui_sidebar_bottom=plugin_manager.get_ui_components('sidebar_bottom'),
                         ui_top_menu=plugin_manager.get_ui_components('top_menu'),
                         ui_context_menu=plugin_manager.get_ui_components('context_menu'),
                         plugin_list=plugin_manager.plugins,
                         # [新增] 注入 UI 状态配置
                         init_view_mode=conf.get('view_mode', 'grid'),
                         init_card_size=conf.get('card_size', 140),
                         init_sort_field=conf.get('sort_field', 'name'),
                         init_sort_order=conf.get('sort_order', 'asc'),
                         init_filter_type=conf.get('filter_type', 'all'),
                         init_filter_rating=conf.get('filter_rating', 0),
                         init_smart_expanded=str(conf.get('smart_expanded', 'false')).lower(),
                         init_library_expanded=str(conf.get('library_expanded', 'false')).lower(),
                         init_sidebar_collapsed=str(conf.get('sidebar_collapsed', 'false')).lower()))
    # [SEC-1] 主页面下发 API token；token 每次进程启动轮换
    resp.set_cookie(_COOKIE_NAME, APP_TOKEN, httponly=True, samesite='Lax')
    return resp

@app.route('/api/config', methods=['POST'])
def handle_config():
    """保存前端传来的配置"""
    save_config(request.json)
    return jsonify({'status': 'ok'})

# [ARCH-9] clean_cache_task 已删除：该任务因 NFD/NFC 误删风险被禁用（函数体
# 首行 return），此前 /api/list 每次请求还 spawn 一个线程跑它，纯浪费。

# [PERF-1] union 预计算为模块级 frozenset（此前每文件每次调用都现算）
_IMAGE_TYPES_ALL = frozenset(EXT_IMAGE_NORMAL | EXT_IMAGE_TOPOS | EXT_IMAGE_PRO | EXT_RAW_PHOTO | EXT_DESIGN)

def classify_ext(ext: str) -> str:
    """扩展名 → 前端类型。list_files 与智能文件夹共用（此前两处实现已分叉出错）。"""
    if ext in EXT_VIDEO: return 'video'
    if ext in _IMAGE_TYPES_ALL: return 'image'
    if ext in EXT_AUDIO: return 'audio'
    if ext in EXT_CODE: return 'code'
    if ext in TEXT_FILE_EXTENSIONS: return 'text'
    if ext in EXT_DOCS: return 'doc'
    if ext in EXT_ARCHIVE: return 'archive'
    return 'file'

@app.route('/api/list')
def list_files():
    """列出目录内容（包含元数据、缩略图状态及缓存控制）。

    [V3/GAP-11] recursive=1 时聚合子目录文件（depth 上限 3、总量护栏 2000），
    文件条目带 subdir（相对子路径）供前端显示来源 chip。"""
    path = request.args.get('path', ROOT_PATH)
    # [V3/GAP-11] 递归聚合参数（缓存 key 必须区分模式，防互踩）
    try:
        rec_depth = int(request.args.get('depth', 2) or 2)
    except ValueError:
        rec_depth = 2
    rec_depth = max(1, min(rec_depth, 3))
    recursive = request.args.get('recursive', '0') == '1'
    # [BUG-P1-01] Flask 已解码一次 query string，前端用 encodeURIComponent；
    # 二次 unquote 会把含 "%20"/"%2F" 字面量的名字破坏（目录 100%20done → 100 done）
    if path: path = unicodedata.normalize('NFC', path.replace('\\', '/'))
    # [V5 复查] cache_key 在 NFC 归一化之后计算：此前用原始请求路径，NFD/NFC
    # 两种请求串会各存一份互不失效的缓存
    cache_key = f'{path}|r{rec_depth}' if recursive else path
    if not os.path.exists(path):
        path_nfd = unicodedata.normalize('NFD', path)
        if os.path.exists(path_nfd): path = path_nfd
        else: return jsonify({'files': [], 'error': f'Path not found: {path}'})
    # [V4/GAP-22] 文件夹软锁：锁定目录及其子目录拒绝列表（解锁后会话内放行）
    _lock = _locked_by(path)
    if _lock:
        return jsonify({'files': [], 'locked': True, 'lock_path': _lock['path'],
                        'hint': _lock.get('hint') or '', 'error': '目录已锁定'}), 423
    # [SEC-2] 记录最近浏览目录，作为上传目标白名单
    global _last_browsed_dir
    _last_browsed_dir = path
    # 缓存检查
    with CACHE_LOCK:
        if cache_key in DIR_CACHE:
            data, timestamp = DIR_CACHE[cache_key]
            if time.time() - timestamp < CACHE_TTL: return jsonify(data)
    files = []
    # [PERF-1] 只读探测缓存目录（不再每请求 mkdir/写 .nomedia），
    # 并移除每请求 spawn 的 clean_cache_task 线程（该任务早已禁用）
    cache_dir, is_local = thumb_engine.peek_cache_info(path)
    scan_cache = {}  # [PERF-1] 同一请求内共享 .cache 目录模糊扫描结果

    def _iter_entries(base):
        """[V3/GAP-11] 产出 (entry, subdir_rel)：recursive 时按层展开子目录
        （depth 上限、总量护栏 2000；目录卡片每级只出现一次）。"""
        if not recursive:
            with os.scandir(base) as it:
                for e in it:
                    yield e, ''
            return
        count = 0

        def _walk(d, rel, level):
            nonlocal count
            if level > rec_depth or count >= 2000:
                return
            try:
                with os.scandir(d) as it:
                    items = sorted(it, key=lambda e: e.name)
            except OSError:
                return
            subdirs = []
            for entry in items:
                if count >= 2000:
                    return
                if entry.name.startswith('.') or entry.name == '.cache':
                    continue
                if entry.name.lower().endswith('.xmp'):
                    continue  # [V2/GAP-04] sidecar 不显示
                try:
                    if entry.is_dir():
                        # 目录卡片保留（rel 前缀即其位置），且向下递归
                        count += 1
                        yield entry, rel
                        subdirs.append(entry)
                    else:
                        count += 1
                        yield entry, rel
                except OSError:
                    continue
            for sd in subdirs:
                child_rel = f"{rel}/{sd.name}".lstrip('/')
                yield from _walk(sd, child_rel, level + 1)

        yield from _walk(base, '', 1)

    try:
        with os.scandir(path):
            pass  # 目录可读性探测（保持原异常语义）
        for entry, sub_rel in _iter_entries(path):
            if entry.name.startswith('.') or entry.name == '.cache':
                continue
            # [V2/GAP-04] XMP sidecar 不在网格中显示（对齐 Lightroom 行为）
            if entry.name.lower().endswith('.xmp'):
                continue
            # [FUNC-1] 单条目出错（断链 symlink / 权限 / 竞态删除）只跳过该条，
            # 不再让整个目录显示"无法访问"
            try:
                name = unicodedata.normalize('NFC', entry.name)
                full_path = os.path.join(path, sub_rel, name).replace('\\', '/') if sub_rel \
                    else os.path.join(path, name).replace('\\', '/')
                stat = entry.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d')
                is_dir = entry.is_dir()
                thumb_ver = 0
                if not is_dir:
                    thumb_path = thumb_engine.get_thumb_path(full_path, cache_info=(cache_dir, is_local), scan_cache=scan_cache)
                    if os.path.exists(thumb_path): thumb_ver = int(os.stat(thumb_path).st_mtime)
                if is_dir:
                    files.append({'name': name, 'path': full_path, 'type': 'dir', 'mtime': mtime})
                else:
                    ext = os.path.splitext(name)[1].lower()
                    # [UX-4] 直接下发数值排序键，前端不再反解显示字符串
                    files.append({'name': name, 'path': full_path, 'type': classify_ext(ext), 'size': utils.get_size_str(stat.st_size), 'mtime': mtime, 'size_bytes': stat.st_size, 'mtime_ts': stat.st_mtime, 't_ver': thumb_ver, 'res': None,
                                  'subdir': sub_rel})  # [V3/GAP-11] 来源子目录 chip
            except (OSError, ValueError):
                continue
        files.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
        # 批量获取数据库元数据（标签、评分、备注）
        # [FUNC-9] 空目录不再拼出非法的 IN ()；>999 文件分块查询，
        # 不再触发 SQLite 变量上限被裸 except 静默吞掉
        try:
            paths = [f['path'] for f in files]
            meta_map = {}
            stack_covers, stack_sizes = {}, {}
            if paths:
                conn = _db()
                try:
                    c = conn.cursor()
                    legacy_schema = False
                    for i in range(0, len(paths), const.THUMB_BATCH_CHUNK):
                        chunk = paths[i:i + 500]
                        placeholders = ','.join('?' * len(chunk))
                        if not legacy_schema:
                            try:
                                # [U5/GAP-03] color/flag 随列表下发（卡片角标/前端筛选）
                                # [V1/GAP-02] duration/date_taken 随列表下发（时长角标/
                                # 拍摄日期筛选/时间线切换；旧行为 NULL 前端自然降级）
                                # [V2/GAP-07] stack_id 随列表下发（堆叠折叠）
                                # [V3/GAP-09] dom_color 随列表下发（自动颜色筛选）
                                c.execute(f"SELECT path, tags, comments, rating, cached_info, color, flag, duration, date_taken, stack_id, dom_color FROM file_meta WHERE path IN ({placeholders})", [_meta_key(p) for p in chunk])
                            except sqlite3.OperationalError:
                                legacy_schema = True
                        if legacy_schema:
                            c.execute(f"SELECT path, tags, comments, rating FROM file_meta WHERE path IN ({placeholders})", [_meta_key(p) for p in chunk])
                        for row in c.fetchall():
                            meta_map[row[0]] = ({'tags': row[1], 'comments': row[2], 'rating': row[3], 'cached_info': row[4], 'color': row[5] or 0, 'flag': row[6] or 0,
                                                 'duration': row[7], 'date_taken_ts': row[8], 'stack_id': row[9], 'dom_color': row[10]}
                                                if not legacy_schema else
                                                {'tags': row[1], 'comments': row[2], 'rating': row[3], 'cached_info': None, 'color': 0, 'flag': 0, 'duration': None, 'date_taken_ts': None, 'stack_id': None, 'dom_color': None})
                    # [V2/GAP-07] 堆叠封面/成员数一次取齐
                    sid_list = {m.get('stack_id') for m in meta_map.values() if m.get('stack_id')}
                    if sid_list:
                        ph = ','.join('?' * len(sid_list))
                        stack_covers = dict(conn.execute(
                            f"SELECT id, cover_path FROM stacks WHERE id IN ({ph})", tuple(sid_list)).fetchall())
                        stack_sizes = dict(conn.execute(
                            f"SELECT stack_id, COUNT(*) FROM file_meta WHERE stack_id IN ({ph}) GROUP BY stack_id",
                            tuple(sid_list)).fetchall())
                finally:
                    conn.close()
            for f in files:
                # [V5 复查] 统一经 _meta_key 归一化后查表：NFD 回退分支拼出的
                # 路径与 NFC 键永不相等，此前该分支下整目录元数据显示为空
                m = meta_map.get(_meta_key(f['path']), {'tags': '', 'comments': '', 'rating': 0, 'cached_info': None, 'color': 0, 'flag': 0, 'duration': None, 'date_taken_ts': None, 'stack_id': None, 'dom_color': None})
                sid = m.get('stack_id')
                f.update({'tags': m['tags'], 'comments': m['comments'], 'rating': m['rating'], 'res': m.get('cached_info'), 'color': m.get('color', 0), 'flag': m.get('flag', 0),
                          'duration': m.get('duration'), 'date_taken_ts': m.get('date_taken_ts'),
                          'date_taken': (datetime.fromtimestamp(m['date_taken_ts'], tz=timezone.utc).strftime('%Y-%m-%d') if m.get('date_taken_ts') else None),
                          'stack_id': sid,
                          'stack_cover': stack_covers.get(sid) if sid else None,
                          'stack_size': stack_sizes.get(sid) if sid else None,
                          'dom_bucket': m.get('dom_color')})
        except Exception as e:
            # [BUG-P2-02] DB 锁超时等错误此前被整体吞掉——整目录 tags/rating
            # 静默消失。降级为无元数据展示，但必须留日志可查。
            logger.error(f"/api/list 元数据批量查询失败（以无元数据降级）: {e}")
        response_data = {'files': files}
        # [BUG-P2-03] 带上限淘汰的缓存写入（直写 DIR_CACHE 会无界增长）
        # [V3/GAP-11] key 含递归模式，防普通/聚合列表缓存互踩
        put_dir_cache(cache_key, response_data)
        return jsonify(response_data)
    except Exception as e: return jsonify({'files': [], 'error': str(e)})

@app.route('/api/rename', methods=['POST'])
def api_rename():
    """重命名文件，并同步更新数据库记录"""
    data = request.json
    old_path = data.get('old_path')
    new_name = data.get('new_name')
    
    if not old_path or not new_name:
        return jsonify({'success': False, 'message': '参数缺失'})

    _lg = _lock_guard([old_path])
    if _lg:
        return _lg

    # [SEC-4] 禁止跨目录：new_name 不允许包含路径成分
    new_name = unicodedata.normalize('NFC', str(new_name).replace('\\', '/'))
    if (not new_name or os.path.basename(new_name) != new_name
            or new_name in {'.', '..'}):
        return jsonify({'success': False, 'message': '非法文件名'})

    if not os.path.exists(old_path):
        return jsonify({'success': False, 'message': '源文件不存在'})
        
    try:
        folder = os.path.dirname(old_path)
        new_path = os.path.join(folder, new_name)
        
        if os.path.exists(new_path):
            # [BUG-P3-12] 大小写不敏感卷上仅改大小写时 exists 命中自身；
            # samefile 判定为同一文件才放行
            try:
                if not os.path.samefile(old_path, new_path):
                    return jsonify({'success': False, 'message': '目标文件名已存在'})
            except OSError:
                return jsonify({'success': False, 'message': '目标文件名已存在'})

        os.rename(old_path, new_path)

        # 缓存失效
        invalidate_cache(os.path.dirname(old_path))
        invalidate_cache(os.path.dirname(new_path))

        # 更新数据库中的路径
        # [BUG-P1-03] 裸 except 吞错 = 文件已改名但 tags/rating/comments
        # 滞留旧路径（元数据静默"丢失"）；连接必须在 finally 关闭
        conn = None
        try:
            conn = _db()
            old_key, new_key = _meta_key(old_path), _meta_key(new_path)
            conn.execute("UPDATE file_meta SET path = ? WHERE path = ?",
                         (new_key, old_key))
            # [U9] 结构化标签同步平移，否则 rename 后 /api/search?tag= 与
            # 智能文件夹 tag 规则全部失联、标签计数虚高
            conn.execute("UPDATE file_tags SET file_path = ? WHERE file_path = ?",
                         (new_key, old_key))
            # [V2/GAP-05] 集合成员随迁，文件改名后集合不散架
            conn.execute("UPDATE collection_items SET file_path = ? WHERE file_path = ?",
                         (new_key, old_key))
            # [U10] 目录重命名：全部后代的 meta/标签按前缀平移（对齐 trash.py
            # 与 batch_move 的语义）。此前只改目录自身一行——目录内所有已打标
            # /评分文件 rename 后全部变孤儿行，Ctrl+Z 也救不回
            if os.path.isdir(new_path):
                for table, col in (('file_meta', 'path'), ('file_tags', 'file_path'),
                                   ('collection_items', 'file_path')):
                    esc = trash._like_escape(old_key) + '/%'
                    rows = conn.execute(
                        f"SELECT {col} FROM {table} WHERE {col} LIKE ? ESCAPE '\\'",
                        (esc,)).fetchall()
                    conn.executemany(
                        f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                        [(new_key + '/' + r[0][len(old_key) + 1:], r[0]) for r in rows])
            conn.commit()
        except Exception as e:
            logger.error(f"重命名元数据迁移失败 {old_path} → {new_path}: {e}")
        finally:
            if conn is not None:
                conn.close()
        
        # 处理缩略图缓存迁移
        # [FUNC-6] 三尺寸全部迁移：漏掉 preview 会让改名后同名旧缓存
        # 在灯箱显示错误内容
        try:
            for size in ('small', 'large', 'preview'):
                old_thumb = thumb_engine.get_thumb_path(old_path, size_type=size)
                new_thumb = thumb_engine.get_thumb_path(new_path, size_type=size)
                if os.path.exists(old_thumb):
                    os.rename(old_thumb, new_thumb)
        except Exception as e:
            logger.warning(f"重命名缩略图迁移失败 {old_path}: {e}")

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/update_meta', methods=['POST'])
def api_update_meta():
    """更新文件元数据（备注/标签/评分/缓存信息）"""
    data = request.json
    path = data.get('path')
    
    if not path:
        return jsonify({'success': False})

    _lg = _lock_guard([path])
    if _lg:
        return _lg

    try:
        conn = _db()
        try:
            c = conn.cursor()

            # [U5/GAP-03] color/flag 值域校验：非法值显式报错而非静默入库
            new_color = _clamp_update_value(data, 'color', COLOR_VALUES)
            if new_color is False:
                return jsonify({'success': False, 'message': 'color 非法（0-5）'})
            new_flag = _clamp_update_value(data, 'flag', FLAG_VALUES)
            if new_flag is False:
                return jsonify({'success': False, 'message': 'flag 非法（0-2）'})

            # 先查询是否存在，以支持部分更新
            c.execute("SELECT tags, comments, rating, cached_info, color, flag FROM file_meta WHERE path = ?", (_meta_key(path),))
            row = c.fetchone()

            current_tags = row[0] if row else ''
            current_comments = row[1] if row else ''
            current_rating = row[2] if row and len(row) > 2 else 0
            current_cached = row[3] if row and len(row) > 3 else None
            current_color = row[4] if row and len(row) > 4 else 0
            current_flag = row[5] if row and len(row) > 5 else 0

            # 获取新值，如果未提供则保持原值
            # [BUG-P3-34] rating 值域校验（此前字符串/负数可静默入库）
            if 'rating' in data and data['rating'] is not None:
                try:
                    _rating_val = int(data['rating'])
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'message': 'rating 非法（0-5 整数）'})
                if not 0 <= _rating_val <= 5:
                    return jsonify({'success': False, 'message': 'rating 非法（0-5 整数）'})
                new_rating = _rating_val
            else:
                new_rating = current_rating
            new_tags = data.get('tags', current_tags)
            new_comments = data.get('comments', current_comments)
            new_cached = data.get('cached_info', current_cached)
            if new_color is None:
                new_color = current_color
            if new_flag is None:
                new_flag = current_flag

            # [V2] INSERT OR REPLACE 的隐式 DELETE 会把未列出的列（duration/
            # date_taken/stack_id/mtime…）重置为 NULL——改打用户列的 UPSERT，
            # 其余列原样保留
            c.execute("""
                INSERT INTO file_meta (path, tags, comments, rating, cached_info, color, flag)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    tags = excluded.tags,
                    comments = excluded.comments,
                    rating = excluded.rating,
                    cached_info = excluded.cached_info,
                    color = excluded.color,
                    flag = excluded.flag
            """, (_meta_key(path), new_tags, new_comments, new_rating, new_cached, new_color, new_flag))
            # [U5/GAP-01] tags 有变化时反向同步 file_tags（tags 默认
            # 保持原值时 current_tags == new_tags，同步幂等无害）
            if new_tags != current_tags:
                _sync_file_tags_from_legacy(c, _meta_key(path), new_tags)

            conn.commit()

            # 检查评分是否发生变化，如果变化则通知前端刷新
            if new_rating != current_rating:
                # 添加到评分更新队列
                # [BUG-P2-04] 队列仅由前端 poll 消费：面板未打开时此前无界增长
                with _rating_update_lock:
                    if len(_rating_update_queue) >= const.RATING_QUEUE_MAX:
                        del _rating_update_queue[:len(_rating_update_queue) // 2]
                    _rating_update_queue.append({'path': path, 'rating': new_rating})
                # 通过信号通知 Qt 端
                bridge.rating_updated.emit(path, new_rating)
        finally:
            conn.close()
        # [V2/GAP-04] 异步写回文件 XMP（sidecar/内嵌按配置；off 模式自动丢弃）
        try:
            import meta_sync
            meta_sync.enqueue(path, {'rating': new_rating,
                                     'tags': [t.strip() for t in (new_tags or '').split(',') if t.strip()],
                                     'comments': new_comments,
                                     'color': new_color, 'flag': new_flag})
        except Exception:
            pass
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/rating/poll')
def api_rating_poll():
    """嵌入模式下前端轮询检查星级评定更新"""
    confirmed_paths = request.args.getlist('confirmed')
    
    with _rating_update_lock:
        if confirmed_paths:
            confirmed_set = set(confirmed_paths)
            _rating_update_queue[:] = [item for item in _rating_update_queue if item['path'] not in confirmed_set]
        
        pending_updates = [{'path': item['path'], 'rating': item['rating']} for item in _rating_update_queue]
        
    return jsonify({'updates': pending_updates})

_FFMPEG_AVAILABLE = None  # [ARCH-7] 健康检查用，进程内缓存（结果不会变化）

@app.route('/api/health')
def api_health():
    """[ARCH-7] 就绪/健康探测：免 token（见 _guard_api），只暴露状态布尔值，
    替代独立模式 urlopen×50 打业务页、嵌入模式盲等 100ms 的启动探测方式"""
    global _FFMPEG_AVAILABLE
    if _FFMPEG_AVAILABLE is None:
        _FFMPEG_AVAILABLE = utils.get_tool_path('ffmpeg') is not None
    try:
        port = urlsplit(f'//{request.host or ""}').port or webapp.FLASK_PORT
    except ValueError:
        port = webapp.FLASK_PORT
    return jsonify({
        'status': 'ok',
        'port': port,
        # [BUG-P1-04] 惰性判定后的当前 DB 路径（from-import 的 DB_FILE 可能过期）
        'db': os.path.exists(_db_resolve_current()),
        'ffmpeg': bool(_FFMPEG_AVAILABLE),
    })


# ============================================================================
# [V1/GAP-01] 全库索引器 API
# ============================================================================

@app.route('/api/scan/start', methods=['POST'])
def api_scan_start():
    """触发一次全库增量索引。body 可选 {'library': '<路径>'}（缺省全部素材库）。
    已在扫描中返回 409。"""
    data = request.get_json(silent=True) or {}
    import scanner
    ok = scanner.start(data.get('library'))
    if not ok:
        return jsonify({'success': False, 'message': '扫描已在进行中'}), 409
    return jsonify({'success': True})


@app.route('/api/scan/status')
def api_scan_status():
    """索引器状态：{running, phase, library, scanned, total, last_result}。"""
    import scanner
    return jsonify(scanner.status())

def _plan_renames(files, mode, params):
    """[V3/GAP-10] 重命名计划构建（批量重命名执行与 dry-run 预览共用）。

    返回 (plan, errors)：plan = [{'src','dst','conflict'}]（conflict=目标已被
    其他文件占用，执行时自动 _1 后缀）。mode: replace|append|template。"""
    import rename_template
    plan, errors = [], []
    if mode == 'template':
        tpl = str(params.get('template') or '')
        if not tpl:
            return plan, ['模板不能为空']
        # 模板用到的技术列一次取齐（file_meta 缺行则空 meta 降级）
        meta_map = {}
        conn = None
        try:
            conn = _db()
            cols = 'path, date_taken, mtime, camera, lens, iso, aperture, shutter, focal, width, height, duration'
            try:
                rows = conn.execute(f"SELECT {cols} FROM file_meta").fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute("SELECT path FROM file_meta").fetchall()
                rows = [r + (None,) * 11 for r in rows]
            for r in rows:
                meta_map[r[0]] = dict(zip(
                    ('date_taken', 'mtime', 'camera', 'lens', 'iso', 'aperture',
                     'shutter', 'focal', 'width', 'height', 'duration'), r[1:]))
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()

    seq = 0  # [BUG-P3-13] 独立编号计数器：跳过的文件不再吃掉编号
    try:
        start = int(params.get('start', 1))
    except (TypeError, ValueError):
        start = 1
    for path in files or []:
        if not os.path.exists(path):
            continue
        old_name = os.path.basename(path)
        folder = os.path.dirname(path)
        ext = os.path.splitext(old_name)[1]
        new_name = old_name
        if mode == 'replace':
            find_str = params.get('find', '')
            replace_str = params.get('replace', '')
            if find_str:
                new_name = old_name.replace(find_str, replace_str)
        elif mode == 'append':
            prefix = params.get('prefix', 'File')
            if not prefix:
                prefix = 'File'
            try:
                digits = int(params.get('digits', 2))
            except (TypeError, ValueError):
                digits = 2
            new_name = f"{prefix}_{str(start + seq).zfill(digits)}{ext}"
        elif mode == 'template':
            key = _meta_key(path)
            meta = meta_map.get(key) or {}
            # mtime 缺行时兜底磁盘值
            if not meta.get('mtime'):
                try:
                    meta['mtime'] = os.stat(path).st_mtime
                except OSError:
                    pass
            parent = os.path.basename(folder.rstrip('/\\'))
            parent2 = os.path.basename(os.path.dirname(folder.rstrip('/\\')))
            new_name = rename_template.render(tpl, {
                'name': os.path.splitext(old_name)[0], 'ext': ext,
                'seq': seq + start, 'parent': parent, 'parent2': parent2, 'meta': meta,
            })
            if not new_name:
                continue
            # [V3] 模板未写 {ext} 时自动保留原扩展名（对齐 Bridge/LR 行为）
            # [V5 复查] {ext:upper} 等带修饰符写法此前识别不到导致扩展名双拼
            if not re.search(r'\{\s*ext\s*(:[^{}]*)?\}', tpl):
                new_name += ext
            if new_name == old_name:
                continue
        if new_name != old_name:
            # [V5 复查] replace/append 模式的产物此前不做合法性校验：find/replace
            # 或 prefix 里带 "/" 或 ".." 会把文件改名成路径（文件被移出目录）
            if new_name in ('', '.', '..') or '/' in new_name or os.path.basename(new_name) != new_name:
                errors.append(f"跳过 {old_name}: 新文件名非法")
                continue
            new_path = os.path.join(folder, new_name)
            # 冲突处理：目标已存在且不是自身（[BUG-P3-12] samefile 兜大小写
            # 不敏感卷上仅改大小写的自命中），自动添加后缀
            conflict = False
            if os.path.exists(new_path):
                try:
                    conflict = not os.path.samefile(path, new_path)
                except OSError:
                    conflict = True
            if conflict:
                base_new, ext_new = os.path.splitext(new_name)
                counter = 1
                while True:
                    candidate = os.path.join(folder, f"{base_new}_{counter}{ext_new}")
                    if not os.path.exists(candidate):
                        new_path = candidate
                        break
                    counter += 1
            plan.append({'src': path, 'dst': new_path, 'conflict': conflict})
        seq += 1
    return plan, errors


@app.route('/api/batch/rename', methods=['POST'])
def api_batch_rename():
    """批量重命名接口。mode: replace | append | template（[V3/GAP-10]）"""
    data = request.json
    files = data.get('files', [])
    mode = data.get('mode')
    params = data.get('params', {})

    if not files or not mode:
        return jsonify({'success': False, 'message': '参数缺失'})

    _lg = _lock_guard(files)
    if _lg:
        return _lg

    plan, errors = _plan_renames(files, mode, params)

    if not plan:
        return jsonify({'success': True, 'processed': 0, 'message': '没有文件需要重命名'})

    # [BUG-P1-03] 连接 finally 化：SQL 异常不再泄漏 sqlite 连接
    conn = None
    try:
        conn = _db()
        c = conn.cursor()

        for item in plan:
            try:
                os.rename(item['src'], item['dst'])

                # 更新数据库路径
                # [BUG-P1-03] 带日志降级：单条迁移失败不再静默
                try:
                    c.execute("UPDATE file_meta SET path = ? WHERE path = ?", (_meta_key(item['dst']), _meta_key(item['src'])))
                    # [U9] 结构化标签同步平移（与 rename 单文件路径一致）
                    c.execute("UPDATE file_tags SET file_path = ? WHERE file_path = ?", (_meta_key(item['dst']), _meta_key(item['src'])))
                    # [V2/GAP-05] 集合成员随迁
                    c.execute("UPDATE collection_items SET file_path = ? WHERE file_path = ?", (_meta_key(item['dst']), _meta_key(item['src'])))
                except Exception as e:
                    logger.error(f"批量重命名元数据迁移失败 {item['src']}: {e}")

                # 迁移缩略图 [FUNC-6] 三尺寸全覆盖
                try:
                    for size in ('small', 'large', 'preview'):
                        old_thumb = thumb_engine.get_thumb_path(item['src'], size_type=size)
                        new_thumb = thumb_engine.get_thumb_path(item['dst'], size_type=size)
                        if os.path.exists(old_thumb): os.rename(old_thumb, new_thumb)
                except Exception:
                    pass

                invalidate_cache(os.path.dirname(item['src']))
            except Exception as e:
                errors.append(f"重命名 {os.path.basename(item['src'])} 失败: {e}")

        conn.commit()
    finally:
        if conn is not None:
            conn.close()

    return jsonify({'success': True, 'processed': len(plan) - (len(errors)), 'errors': errors})


@app.route('/api/batch/rename/preview', methods=['POST'])
def api_batch_rename_preview():
    """[V3/GAP-10] 批量重命名 dry-run：返回前后对照与冲突标记（不落盘）。"""
    data = request.get_json(force=True) or {}
    files = data.get('files') or []
    mode = data.get('mode')
    params = data.get('params') or {}
    if not files or not mode:
        return jsonify({'success': False, 'message': '参数缺失'}), 400
    _lg = _lock_guard(files)
    if _lg:
        return _lg
    plan, errors = _plan_renames(files, mode, params)
    return jsonify({'success': True, 'plan': [
        {'src': it['src'], 'dst': it['dst'],
         'src_name': os.path.basename(it['src']), 'dst_name': os.path.basename(it['dst']),
         'conflict': bool(it.get('conflict'))} for it in plan], 'errors': errors})

@app.route('/api/batch/meta', methods=['POST'])
def api_batch_meta():
    """批量元数据更新接口"""
    data = request.json
    files = data.get('files', [])
    rating = data.get('rating')
    tags = data.get('tags') # 逗号分隔字符串
    # [U5/GAP-03] 批量颜色/旗标（None=不修改）
    color = data.get('color')
    flag = data.get('flag')
    if color is not None:
        color = _clamp_update_value({'color': color}, 'color', COLOR_VALUES)
        if color is False:
            return jsonify({'success': False, 'message': 'color 非法（0-5）'})
    if flag is not None:
        flag = _clamp_update_value({'flag': flag}, 'flag', FLAG_VALUES)
        if flag is False:
            return jsonify({'success': False, 'message': 'flag 非法（0-2）'})

    if not files:
         return jsonify({'success': False, 'message': '未选择文件'})

    _lg = _lock_guard(files)
    if _lg:
        return _lg

    # [V5 复查] rating 值域/类型校验：与 update_meta 口径对齐——此前
    # rating=99 直接入库、非数字直接 500
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'rating 非法（0-5）'}), 400
        rating = max(0, min(5, rating))

    conn = None
    sync_jobs = []  # [V2/GAP-04] (path, final_tags) 逐文件收集，提交后统一写回
    try:
        conn = _db()
        c = conn.cursor()

        for path in files:
            updates = []
            values = []

            # 1. 确保记录存在
            c.execute("SELECT tags FROM file_meta WHERE path = ?", (_meta_key(path),))
            row = c.fetchone()
            if not row:
                c.execute("INSERT INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)", (_meta_key(path),))
                curr_tags = ''
            else:
                curr_tags = row[0] or ''

            # 2. 准备更新
            if rating is not None and rating > 0:
                updates.append("rating = ?")
                values.append(rating)

            if tags:
                # 追加标签并去重
                new_tag_list = [t.strip() for t in tags.split(',') if t.strip()]
                curr_tag_list = [t.strip() for t in curr_tags.split(',') if t.strip()]
                final_tags = sorted(list(set(curr_tag_list + new_tag_list)))
                final_tag_str = ",".join(final_tags)

                updates.append("tags = ?")
                values.append(final_tag_str)
                # [U5/GAP-01] 双写 tags/file_tags（迁移后以结构化表为准）
                for t in final_tags:
                    c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                    c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                                 SELECT ?, id FROM tags WHERE name = ?""", (_meta_key(path), t))

            # [U5/GAP-03] 批量颜色/旗标
            if color is not None:
                updates.append("color = ?")
                values.append(color)
            if flag is not None:
                updates.append("flag = ?")
                values.append(flag)

            # 3. 执行更新
            if updates:
                values.append(_meta_key(path))
                sql = f"UPDATE file_meta SET {', '.join(updates)} WHERE path = ?"
                c.execute(sql, values)
                sync_jobs.append((path, {'rating': rating if (rating is not None and rating > 0) else None,
                                         'tags': ([t.strip() for t in (final_tag_str or '').split(',') if t.strip()]
                                                  if tags else None),
                                         'color': color, 'flag': flag}))

        conn.commit()
        # [V2/GAP-04] 批量元数据同样写回 XMP
        try:
            import meta_sync
            for jp, jf in sync_jobs:
                meta_sync.enqueue(jp, {k: v for k, v in jf.items() if v is not None})
        except Exception:
            pass
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        # [U10] 连接 finally 化：循环内任一 SQL 异常不再泄漏 sqlite 连接
        if conn is not None:
            conn.close()

@app.route('/api/metadata/detail')
def api_metadata_detail():
    """惰性加载深度元数据"""
    path = request.args.get('path')
    if not path or not os.path.exists(path):
        return jsonify({})
    # [V5 复查] 锁定目录的深度元数据（机位/GPS/拍摄参数）不泄漏
    _lg = _lock_guard([path])
    if _lg:
        return _lg
    
    try:
        # 使用新的深度元数据解析
        info = utils.get_pro_metadata(path)
        return jsonify(info)
    except Exception as e:
        logger.error(f"Metadata error: {e}")
        return jsonify({})

@app.route('/api/action/snapshot', methods=['POST'])
def api_action_snapshot():
    """触发前端播放器截图"""
    bridge.snapshot_signal.emit()
    return jsonify({'status': 'ok'})

@app.route('/api/delete', methods=['POST'])
def delete_items():
    """[U6/GAP-05] 删除：默认移入应用内回收站（元数据可恢复）。

    body: {paths: [...], permanent: bool}——permanent=true 走旧路径
    send2trash 进系统废纸篓并同步清理 meta/缩略图（不可恢复）。"""
    data = request.get_json(force=True)
    paths = data.get('paths', [])
    if not isinstance(paths, list) or not paths:
        return jsonify({'deleted': [], 'errors': ['缺少 paths']})
    _lg = _lock_guard(paths)
    if _lg:
        return _lg
    if data.get('permanent'):
        return jsonify(_delete_permanent(paths))
    records, errors = trash.trash_items(paths)
    deleted = [r['path'] for r in records]
    return jsonify({'deleted': deleted, 'errors': errors,
                    'records': records})


def _delete_permanent(paths):
    """彻底删除（旧 send2trash 路径 + meta/缩略图同步清理）。"""
    deleted, errors = [], []
    for p in paths:
        target_path = p
        if not os.path.exists(target_path):
            target_path = unicodedata.normalize('NFD', p)
            if not os.path.exists(target_path): target_path = unicodedata.normalize('NFC', p)
        if not os.path.exists(target_path):
            errors.append(f"找不到文件: {os.path.basename(p)}")
            continue
        try:
            # 使用 send2trash 安全删除（移动到回收站）
            send2trash(target_path)
            deleted.append(p)
            invalidate_cache(os.path.dirname(target_path))

            # 删除对应的缩略图 [FUNC-6] 三尺寸全覆盖
            try:
                for size in ('small', 'large', 'preview'):
                    cache_thumb = thumb_engine.get_thumb_path(target_path, size_type=size)
                    if os.path.exists(cache_thumb): os.remove(cache_thumb)
            except Exception: pass

            # [FUNC-6] 元数据行同步清理，避免永久残留（智能文件夹/搜索会查到幽灵记录）
            # [U9] file_tags 与目录后代一并清理（LIKE 通配符转义），
            # 否则标签计数虚高、空标签永不回收
            try:
                conn = _db()
                try:
                    for key in (_meta_key(p), _meta_key(target_path)):
                        conn.execute("DELETE FROM file_meta WHERE path = ?", (key,))
                        conn.execute("DELETE FROM file_meta WHERE path LIKE ? ESCAPE '\\'",
                                     (trash._like_escape(key) + '/%',))
                        conn.execute("""DELETE FROM file_tags WHERE file_path = ?
                                        OR file_path LIKE ? ESCAPE '\\'""",
                                     (key, trash._like_escape(key) + '/%'))
                    # [U9] 零引用标签回收（对齐 api_meta_tags 删标签语义）
                    conn.execute(
                        "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM file_tags)")
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                # [U10] 彻底删除后 meta/标签清理失败此前被静默吞掉——
                # 幽灵行+标签计数虚高无从排查。降级为日志
                logger.error(f"[U10] _delete_permanent 元数据清理失败 {p}: {e}")

        except Exception as e: errors.append(f"删除失败 {os.path.basename(p)}: {str(e)}")
    return {'deleted': deleted, 'errors': errors}


# ============================================================================
# [U6/GAP-05] 回收站视图与恢复/彻底删除/过期清理
# ============================================================================

@app.route('/api/trash')
def api_trash_list():
    trash.purge_expired()  # 节流：1 小时至多实查一次
    return jsonify({'items': trash.list_items()})


@app.route('/api/trash/restore', methods=['POST'])
def api_trash_restore():
    data = request.get_json(force=True)
    ids = data.get('ids') or []
    if not ids or not isinstance(ids, list):
        return jsonify({'success': False, 'message': '缺少 ids'}), 400
    restored, errors = trash.restore_items(ids)
    return jsonify({'success': not errors or len(restored) > 0,
                    'restored': restored, 'errors': errors})


@app.route('/api/trash/purge', methods=['POST'])
def api_trash_purge():
    data = request.get_json(force=True)
    ids = data.get('ids') or []
    if data.get('all'):
        ids = [it['id'] for it in trash.list_items()]
    if not ids or not isinstance(ids, list):
        return jsonify({'success': False, 'message': '缺少 ids'}), 400
    purged, errors = trash.purge_items(ids)
    return jsonify({'success': not errors, 'purged': purged, 'errors': errors})


# ============================================================================
# [U6/GAP-06] 元数据备份（导出/导入）与 re-link 扫描
# ============================================================================

@app.route('/api/meta/export')
def api_meta_export():
    """全量元数据导出为 JSON 附件（标签/备注/评分/颜色/旗标/缓存信息）。

    [U10] v2：补 tags 表定义（标签颜色/层级随备份走）与回收站快照——
    此前"全量备份"不含二者，重装导入后标签颜色尽失、回收站元数据尽失。
    [V2] v3：追加集合（含成员）与堆叠。"""
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            """SELECT path, tags, comments, rating, cached_info, color, flag, stack_id
               FROM file_meta""").fetchall()
        tag_rows = conn.execute("SELECT name, color, parent_id FROM tags").fetchall()
        trash_rows = conn.execute(
            """SELECT original_path, item_name, is_dir, trashed_name, deleted_at, meta_json
               FROM trash""").fetchall()
        coll_rows = conn.execute("SELECT id, name, color, created_at FROM collections").fetchall()
        coll_item_rows = conn.execute(
            """SELECT collection_id, file_path, sort_order, added_at FROM collection_items""").fetchall()
        stack_rows = conn.execute("SELECT id, cover_path, created_at FROM stacks").fetchall()
        payload = {
            'version': 3,
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'rows': [dict(zip(('path', 'tags', 'comments', 'rating', 'cached_info',
                               'color', 'flag', 'stack_id'), r)) for r in rows],
            'tags': [dict(zip(('name', 'color', 'parent_id'), r)) for r in tag_rows],
            'trash': [dict(zip(('original_path', 'item_name', 'is_dir', 'trashed_name',
                                'deleted_at', 'meta_json'), r)) for r in trash_rows],
            'collections': [{
                'id': c[0], 'name': c[1], 'color': c[2], 'created_at': c[3],
                'items': [dict(zip(('file_path', 'sort_order', 'added_at'), i))
                          for i in coll_item_rows if i[0] == c[0]],
            } for c in coll_rows],
            'stacks': [dict(zip(('id', 'cover_path', 'created_at'), r)) for r in stack_rows],
        }
        resp = make_response(json.dumps(payload, ensure_ascii=False))
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        resp.headers['Content-Disposition'] = (
            f'attachment; filename="fe_meta_backup_{stamp}.json"')
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


def _clamp_int(v, lo, hi, default=0):
    """[U10] 导入值域防御：损坏备份可写入 color=99/负 rating——
    与 update_meta/batch_meta 的校验口径对齐"""
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


@app.route('/api/meta/import', methods=['POST'])
def api_meta_import():
    """元数据导入。body: {rows: [{path, tags, ...}], mode: 'merge'|'replace'}。

    merge（默认）：只回填空值，不覆盖现有元数据；replace：整行覆盖。
    路径在磁盘上不存在的行跳过（不计入 restored），由 re-link 负责。
    v2 备份另含标签定义（颜色/层级）与回收站快照（物理文件在本机
    .trash 中存在时才导入）。"""
    data = request.get_json(force=True) or {}
    rows = data.get('rows') or []
    mode = data.get('mode') or 'merge'
    if mode not in ('merge', 'replace'):
        return jsonify({'success': False, 'message': 'mode 非法'}), 400
    applied, skipped, tags_added = 0, 0, 0
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        # [U10] 标签定义：新名插入（含颜色/层级），已有名不覆盖用户现行配置
        for t in (data.get('tags') or []):
            name = str(t.get('name') or '').strip()
            if not name:
                continue
            c.execute("INSERT OR IGNORE INTO tags (name, color, parent_id) VALUES (?, ?, ?)",
                      (name, t.get('color'), t.get('parent_id')))
        for m in rows:
            p = _meta_key(str(m.get('path') or ''))
            if not p or not os.path.exists(p):
                skipped += 1
                continue
            rating = _clamp_int(m.get('rating') or 0, 0, 5)
            color = _clamp_int(m.get('color') or 0, 0, 5)
            flag = _clamp_int(m.get('flag') or 0, 0, 2)
            stack_id = m.get('stack_id')
            try:
                stack_id = int(stack_id) if stack_id else None
            except (TypeError, ValueError):
                stack_id = None
            existing = c.execute(
                "SELECT tags, comments, rating, cached_info, color, flag, stack_id "
                "FROM file_meta WHERE path = ?", (p,)).fetchone()
            tags_str = str(m.get('tags') or '')
            if mode == 'replace' or not existing:
                # [V2] 同 update_meta：REPLACE 会清空未列出列（索引器技术列），
                # 改 UPSERT；replace 语义下用户列以备份值为准
                c.execute(
                    """INSERT INTO file_meta
                       (path, tags, comments, rating, cached_info, color, flag, stack_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET
                         tags = excluded.tags, comments = excluded.comments,
                         rating = excluded.rating, cached_info = excluded.cached_info,
                         color = excluded.color, flag = excluded.flag,
                         stack_id = excluded.stack_id""",
                    (p, tags_str, m.get('comments') or '', rating,
                     m.get('cached_info'), color, flag, stack_id))
                _sync_file_tags_from_legacy(c, p, tags_str)
                tags_added += 1
            else:
                # merge：逐字段回填空值（[V2] stack_id 仅在现有为空时采用）
                new_stack = existing[6] if existing[6] is not None else stack_id
                new = (existing[0] or tags_str,
                       existing[1] or (m.get('comments') or ''),
                       existing[2] or rating,
                       existing[3] if existing[3] is not None else m.get('cached_info'),
                       existing[4] or color,
                       existing[5] or flag,
                       new_stack)
                if new != existing:
                    c.execute(
                        """UPDATE file_meta SET tags = ?, comments = ?, rating = ?,
                           cached_info = ?, color = ?, flag = ?, stack_id = ? WHERE path = ?""",
                        (*new, p))
                    _sync_file_tags_from_legacy(c, p, new[0])
                    tags_added += 1
            applied += 1
        # [U10] 回收站快照：物理文件必须在本机 .trash 存在（跨机器重装时
        # .trash 不随备份走，导入这类行只会制造"恢复必然失败"的僵尸记录）
        trash_restored = 0
        for t in (data.get('trash') or []):
            tn = str(t.get('trashed_name') or '')
            if not tn or not os.path.exists(os.path.join(trash.get_trash_dir(), tn)):
                continue
            if c.execute("SELECT 1 FROM trash WHERE trashed_name = ? AND deleted_at = ?",
                         (tn, t.get('deleted_at') or 0)).fetchone():
                continue
            c.execute(
                """INSERT INTO trash (original_path, item_name, is_dir, trashed_name,
                                      deleted_at, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(t.get('original_path') or ''), str(t.get('item_name') or ''),
                 1 if t.get('is_dir') else 0, tn,
                 t.get('deleted_at') or 0, t.get('meta_json') or '[]'))
            trash_restored += 1
        # [V2] 集合与堆叠（merge 语义：同名集合合并成员；堆叠按成员并集重建）
        colls_added = 0
        for col in (data.get('collections') or []):
            name = str(col.get('name') or '').strip()
            if not name:
                continue
            row = c.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
            if row:
                coll_id = row[0]
            else:
                c.execute("INSERT INTO collections (name, color, created_at) VALUES (?, ?, ?)",
                          (name, col.get('color'), col.get('created_at') or time.time()))
                coll_id = c.lastrowid
                colls_added += 1
            for it in (col.get('items') or []):
                fp = _meta_key(str(it.get('file_path') or ''))
                if not fp:
                    continue
                c.execute("""INSERT OR IGNORE INTO collection_items
                             (collection_id, file_path, sort_order, added_at) VALUES (?, ?, ?, ?)""",
                          (coll_id, fp, it.get('sort_order') or 0, it.get('added_at') or time.time()))
        # [V2] 堆叠：按导出 id 原样重建（rows 的 stack_id 引用该 id）；
        # 已有同 id 堆叠不覆盖本机状态
        stacks_added = 0
        for st in (data.get('stacks') or []):
            cover = _meta_key(str(st.get('cover_path') or ''))
            sid = st.get('id')
            if not cover or not sid:
                continue
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                continue
            if c.execute("SELECT 1 FROM stacks WHERE id = ?", (sid,)).fetchone():
                continue
            c.execute("INSERT OR REPLACE INTO stacks (id, cover_path, created_at) VALUES (?, ?, ?)",
                      (sid, cover, st.get('created_at') or time.time()))
            stacks_added += 1
        conn.commit()
        from db import CACHE_LOCK, DIR_CACHE
        with CACHE_LOCK:
            DIR_CACHE.clear()  # 全量导入后目录缓存整体失效
        return jsonify({'success': True, 'applied': applied, 'skipped': skipped,
                        'trash_restored': trash_restored,
                        'collections_added': colls_added, 'stacks_added': stacks_added})
    except Exception as e:
        logger.error(f"[U6] 元数据导入失败: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/meta/scan_orphans')
def api_meta_scan_orphans():
    """re-link 扫描：找出路径已失联的 meta 行，按 文件名+大小 在已配置
    库目录中寻找同签名文件作为候选新路径。

    返回 {orphans: 总数, matches: [{path, new_path, name, size}]}。
    候选唯一才收录（多命中留给人工，不猜测）。"""
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            """SELECT path, tags, comments, rating, cached_info, color, flag
               FROM file_meta""").fetchall()
    finally:
        if conn is not None:
            conn.close()

    orphans = []
    for r in rows:
        p = str(r[0]).replace('\\', '/')
        if not os.path.exists(p):
            if not _volume_available(p):
                continue  # 卷不可达：不是失联，不参与 re-link
            orphans.append(r)
    if not orphans:
        return jsonify({'orphans': 0, 'matches': []})

    # 库根：已添加的素材库；无配置时退回用户根（限制扫描量）
    conf = load_config()
    roots = [l.get('path') for l in (conf.get('libraries') or []) if l.get('path')]
    if not roots:
        roots = [ROOT_PATH]

    # 建磁盘文件索引 (name_lower, size) → [path]，排除 meta 已知路径
    known = {_meta_key(str(r[0])).lower() for r in rows}
    by_name = {}
    scanned = 0
    MAX_SCAN = 200000
    for root in roots:
        if scanned >= MAX_SCAN:
            break
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fn in filenames:
                if fn.startswith('.'):
                    continue
                scanned += 1
                if scanned >= MAX_SCAN:
                    break
                fp = os.path.join(dirpath, fn).replace('\\', '/')
                if fp.lower() in known:
                    continue
                by_name.setdefault(fn.lower(), []).append(fp)
            if scanned >= MAX_SCAN:
                break

    # 候选唯一才收录（多命中留给人工，不猜测）
    matches = []
    for r in orphans:
        p = str(r[0]).replace('\\', '/')
        name = os.path.basename(p)
        candidates = sorted(set(by_name.get(name.lower(), [])))
        if len(candidates) == 1:
            matches.append({'path': p, 'new_path': candidates[0],
                            'name': name,
                            'tags': r[1] or '', 'rating': r[3] or 0,
                            'color': r[5] or 0, 'flag': r[6] or 0})
    return jsonify({'orphans': len(orphans), 'matches': matches})


@app.route('/api/meta/relink', methods=['POST'])
def api_meta_relink():
    """应用 re-link 映射：批量把孤儿 meta 行指向新路径（缩略图一并迁移）。

    body: {mappings: [{path, new_path}]}。"""
    data = request.get_json(force=True) or {}
    mappings = data.get('mappings') or []
    applied, errors = 0, []
    conn = None
    try:
        conn = _db()
        for m in mappings:
            old, new = str(m.get('path') or ''), str(m.get('new_path') or '')
            if not old or not new or not os.path.exists(new):
                errors.append(f"目标不存在: {os.path.basename(new) if new else old}")
                continue
            try:
                conn.execute("UPDATE file_meta SET path = ? WHERE path = ?",
                             (_meta_key(new), _meta_key(old)))
                # [U9] 结构化标签同步平移（同 rename/batch_move）
                conn.execute("UPDATE file_tags SET file_path = ? WHERE file_path = ?",
                             (_meta_key(new), _meta_key(old)))
                for size in ('small', 'large', 'preview'):
                    old_thumb = thumb_engine.get_thumb_path(old, size_type=size)
                    new_thumb = thumb_engine.get_thumb_path(new, size_type=size)
                    if os.path.exists(old_thumb) and not os.path.exists(new_thumb):
                        try:
                            os.rename(old_thumb, new_thumb)
                        except OSError:
                            pass
                applied += 1
            except Exception as e:
                errors.append(f"{os.path.basename(old)}: {e}")
        conn.commit()
        return jsonify({'success': not errors, 'applied': applied, 'errors': errors})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route('/api/tree')
def get_tree_nodes():
    """获取侧边栏目录树节点"""
    parent = request.args.get('path', ROOT_PATH)
    folders = []
    try:
        if os.path.exists(parent):
            with os.scandir(parent) as it:
                entries = sorted(list(it), key=lambda e: e.name.lower())
                for entry in entries:
                    if entry.is_dir() and not entry.name.startswith('.'):
                        name = unicodedata.normalize('NFC', entry.name)
                        full_path = os.path.join(parent, name).replace('\\', '/')
                        folders.append({'name': name, 'path': full_path})
    except Exception as e:
        logger.error(f"/api/tree 读取失败 {parent}: {e}")
    return jsonify(folders)

@app.route('/api/drives')
def get_drives():
    """获取系统磁盘列表"""
    drives = []
    try:
        if os.name == 'nt':
            import string
            from ctypes import windll
            drives_bitmask = windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if drives_bitmask & 1:
                    path = f"{letter}:\\"
                    drives.append({'name': f"本地磁盘 ({letter}:)", 'path': path.replace('\\', '/')})
                drives_bitmask >>= 1
        else:
            # macOS / Linux
            if sys.platform == 'darwin':
                # 优化：不显示系统盘根目录，只显示用户目录(由前端Root处理)和外接盘
                # drives.append({'name': 'Macintosh HD', 'path': '/'}) 
                if os.path.exists('/Volumes'):
                    for item in os.listdir('/Volumes'):
                        if not item.startswith('.') and item != 'Macintosh HD':
                            drives.append({'name': item, 'path': os.path.join('/Volumes', item)})
            else:
                drives.append({'name': 'Root', 'path': '/'})
                # Linux simple check
                media_roots = ['/media', '/mnt']
                for root in media_roots:
                    if os.path.exists(root):
                        for item in os.listdir(root):
                            drives.append({'name': item, 'path': os.path.join(root, item)})
    except Exception as e:
        logger.error(f"Drive list error: {e}")
    return jsonify(drives)

@app.route('/api/thumb')
def get_thumb():
    """获取或生成缩略图 (异步化)"""
    path = request.args.get('path')
    # [BUG-P1-01] 同 /api/list：去掉二次 unquote
    if path:
        path = unicodedata.normalize('NFC', path.replace('\\', '/'))
    size_type = request.args.get('size', 'small')
    if not path: return "", 404

    # 检查是否为系统文件或代码文件，跳过缩略图生成
    filename = os.path.basename(path)
    skip, reason = should_skip_file(filename)
    if skip:
        return "", 204  # No Content - 表示跳过生成

    # [V4/GAP-22] 软锁目录的缩略图同样不泄漏
    if _locked_by(path):
        return "", 403

    # [OPTIMIZATION] 检查失败缓存，避免重复尝试生成
    if check_failure_cache(path):
        return "", 204  # 之前生成失败，直接跳过

    if not os.path.exists(path):
        path = unicodedata.normalize('NFD', path)
        if not os.path.exists(path): return "", 404

    # 策略：小图片直读
    ext = os.path.splitext(path)[1].lower()
    # [U7/GAP-15] SVG 直接回源：浏览器原生渲染（没有 QuickLook 的平台
    # 此前无栅格化器，缩略图永远失败）
    if ext == '.svg' and os.path.isfile(path):
        resp = make_response(send_file(path, mimetype='image/svg+xml'))
        resp.headers['Cache-Control'] = f'max-age={const.THUMB_HTTP_MAX_AGE_S}'
        return resp
    # [PERF-2] isfile 守卫（目录误传此前 500）；与其他缓存分支一致下发
    # Cache-Control，浏览器不再反复回源
    if ext in EXT_IMAGE_NORMAL and os.path.isfile(path) and os.path.getsize(path) < const.SMALL_IMAGE_DIRECT_BYTES:
        if ext not in {'.heic', '.heif', '.tiff', '.tif', '.bmp'}:
            resp = make_response(send_file(path))
            resp.headers['Cache-Control'] = f'max-age={const.THUMB_HTTP_MAX_AGE_S}'
            return resp

    # 获取缓存路径
    target = thumb_engine.get_thumb_path(path, size_type=size_type)

    # 如果已存在，直接返回
    if os.path.exists(target):
        # [FUNC-11] sidecar 比对：同名重导出（mtime/size 变化）→ 旧缓存作废，
        # 删除并重新排队生成（视频重生成会顺带刷新 file_meta 的 cached_info）
        if thumb_engine.is_stale(path, target):
            try: os.remove(target)
            except OSError: pass
        else:
            resp = make_response(send_file(target))
            resp.headers['Cache-Control'] = f'max-age={const.THUMB_CACHE_HTTP_MAX_AGE_S}'
            return resp

    # 如果不存在，加入异步生成队列
    with thumbnail_lock:
        # [FUNC-5] 去重键含尺寸：small 在队时请求 large 不再被吞
        key = (path, size_type)
        if key not in thumbnail_pending:
            thumbnail_pending.add(key)
            thumbnail_queue.put((path, target, size_type))

    # 返回一个透明 1x1 像素或 Loading 占位图
    placeholder = os.path.join(app.static_folder, 'assets', 'img', 'logo.webp')
    if os.path.exists(placeholder):
        return send_file(placeholder)

    return "", 202

@app.route('/api/file')
def get_file():
    """获取文件内容或触发下载"""
    path = request.args.get('path')
    is_download = request.args.get('download', '0') == '1'
    if not path:
        return "", 400
    # [V4/GAP-22] 软锁目录内的文件不提供内容
    if _locked_by(path):
        return jsonify({'error': '目录已锁定', 'locked': True}), 423
    if os.path.isdir(path):
        # [BUG-P3-14] 目录传给 send_file 抛 IsADirectoryError → 500；
        # 显式 400 + 提示
        return jsonify({'error': 'path 是目录，请用 /api/list'}), 400
    if os.path.exists(path):
        if is_download:
            filename = os.path.basename(path)
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type: mime_type = 'application/octet-stream'
            resp = make_response(send_file(path, as_attachment=True, download_name=filename, mimetype=mime_type))
            try:
                safe_filename = quote(filename)
                resp.headers['Content-Disposition'] = f'attachment; filename="{safe_filename}"; filename*=UTF-8\'\'{safe_filename}'
            except:
                resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            return resp
        else:
            resp = send_file(path, as_attachment=False)
            return resp
    return "", 404

@app.route('/api/upload', methods=['POST'])
def upload_files():
    """上传文件到指定目录"""
    target_path = unicodedata.normalize('NFC', request.form.get('path', ROOT_PATH))
    # [SEC-2] 目标白名单：只允许上传到当前正在浏览的目录，防任意路径写入
    # [BUG-P2-08] 两侧统一 NFC 归一：_last_browsed_dir 可能是 NFD 回退分支
    # 写入的（ExFAT 等卷），realpath 直接比较会把正常上传误判为越权
    if (_last_browsed_dir is None
            or os.path.realpath(target_path) != os.path.realpath(
                unicodedata.normalize('NFC', _last_browsed_dir))):
        return jsonify({'uploaded': [], 'errors': ['目标目录不在允许范围内（仅限当前浏览目录）']})
    if not os.path.exists(target_path):
        return jsonify({'uploaded': [], 'errors': [f'目标目录不存在: {target_path}']})
    _lg = _lock_guard([target_path])
    if _lg:
        return _lg
    uploaded, errors = [], []
    files = request.files.getlist('files')
    for file in files:
        if not file or file.filename == '': continue
        try:
            # [SEC-2] 文件名净化：拒绝带路径分隔符/非法名（secure_filename 会剥掉
            # 全部中文，这里用 basename 等价校验）
            filename = unicodedata.normalize('NFC', (file.filename or '').replace('\\', '/'))
            if (not filename or os.path.basename(filename) != filename
                    or filename in {'.', '..'}):
                errors.append(f'非法文件名: {file.filename}')
                continue
            save_path = os.path.join(target_path, filename)
            # [BUG-P0-02] 同名冲突检测：file.save() 默认静默覆盖——
            # 直接、不可恢复的用户数据丢失。存在即拒绝该文件。
            if os.path.exists(save_path):
                errors.append(f'同名文件已存在，未覆盖: {filename}')
                continue
            os.makedirs(target_path, exist_ok=True)
            file.save(save_path)
            uploaded.append(filename)
            plugin_manager.fire_event('after_upload', file_path=save_path)
        except Exception as e: errors.append(f'上传失败 {file.filename}: {str(e)}')

    if uploaded:
        invalidate_cache(target_path)

    return jsonify({'uploaded': uploaded, 'errors': errors})

@app.route('/api/play_native', methods=['POST'])
def api_play_native():
    """通知播放器组件开始播放"""
    data = request.get_json(silent=True) or {}
    if data.get('path'): bridge.play_video_signal.emit(data.get('path'))
    return jsonify({'status': 'ok'})

@app.route('/api/open_system', methods=['POST'])
def api_open_system():
    """调用系统默认程序打开文件"""
    data = request.get_json(silent=True) or {}
    path = data.get('path')
    # [BUG-P3-15] 执行失败不再静默报 ok
    if not path:
        return jsonify({'status': 'error', 'message': '缺少 path'}), 400
    if not os.path.exists(path):
        return jsonify({'status': 'error', 'message': '文件不存在'}), 404
    if not utils.open_in_os(path):
        return jsonify({'status': 'error', 'message': '系统打开失败'}), 500
    return jsonify({'status': 'ok'})

@app.route('/api/reveal', methods=['POST'])
def api_reveal():
    """在系统资源管理器中显示文件"""
    data = request.get_json(silent=True) or {}
    path = data.get('path')
    if path and os.path.exists(path):
        if not utils.reveal_in_os(path):
            return jsonify({'status': 'error', 'message': '定位失败'}), 500
    elif not path:
        return jsonify({'status': 'error', 'message': '缺少 path'}), 400
    return jsonify({'status': 'ok'})

@app.route('/api/apps/associated')
def api_get_apps():
    """获取关联应用程序列表"""
    path = request.args.get('path')
    if not path or not os.path.exists(path): return jsonify({'apps': []})
    return jsonify({'apps': utils.get_associated_apps(path)})

@app.route('/api/open_with', methods=['POST'])
def api_open_with():
    """使用指定应用打开"""
    data = request.get_json(silent=True) or {}
    path = data.get('path')
    app_path = data.get('app_path')
    # [BUG-P3-15] 参数缺失与执行失败不再静默报 ok
    if not path or not app_path:
        return jsonify({'status': 'error', 'message': '缺少 path/app_path'}), 400
    # [SEC-3] 应用白名单：app_path 必须来自系统扫描出的关联应用列表，
    # 防止被当作"本地任意程序执行"原语
    if not os.path.exists(path):
        return jsonify({'status': 'error', 'message': '文件不存在'}), 404
    allowed = {a['path'] for a in utils.get_associated_apps(path)}
    if app_path not in allowed:
        return jsonify({'status': 'error', 'message': '应用不在关联列表中'}), 403
    if not utils.open_with_app(path, app_path):
        return jsonify({'status': 'error', 'message': '打开失败'}), 500
    return jsonify({'status': 'ok'})

@app.route('/api/action/copy', methods=['POST'])
def api_action_copy():
    """将路径发送到剪贴板信号"""
    paths = request.json.get('paths', [])
    if paths: bridge.copy_files_signal.emit(paths)
    return jsonify({'status': 'ok'})

@app.route('/api/action/paste', methods=['POST'])
def api_action_paste():
    """触发粘贴信号"""
    target_path = request.json.get('path')
    if target_path:
        # [V5 复查] 粘贴目标在锁定目录内时拒绝（CopyWorker 写盘前拦截）
        _lg = _lock_guard([target_path])
        if _lg:
            return _lg
        bridge.paste_files_signal.emit(target_path)
        invalidate_cache(target_path)
    return jsonify({'status': 'ok'})

@app.route('/api/action/paste_cancel', methods=['POST'])
def api_action_paste_cancel():
    """[BUG-P2-06] 取消进行中的粘贴（CopyWorker.cancel 此前无任何调用方）"""
    bridge.cancel_paste_signal.emit()
    return jsonify({'status': 'ok'})

@app.route('/api/action/open_folder_dialog', methods=['POST'])
def api_open_folder_dialog():
    """触发打开目录选择框信号"""
    bridge.open_folder_dialog_signal.emit()
    return jsonify({'status': 'ok'})

@app.route('/api/action/add_library', methods=['GET'])
def api_add_library():
    """触发添加库文件夹信号"""
    bridge.add_library_signal.emit()
    return jsonify({'status': 'ok'})

@app.route('/api/library/list')
def api_library_list():
    """获取所有已固定的库"""
    conf = load_config()
    return jsonify(conf.get("libraries", []))

@app.route('/api/library/unpin', methods=['POST'])
def api_library_unpin():
    """取消固定库"""
    path = request.json.get('path')
    if not path:
        return jsonify({'success': False, 'message': 'Path is required'})
    
    conf = load_config()
    libs = conf.get("libraries", [])
    new_libs = [lib for lib in libs if lib['path'] != path]
    save_config({'libraries': new_libs})
    return jsonify({'success': True})

@app.route('/api/audio/trim', methods=['POST'])
def audio_trim():
    """音频剪切 API，使用 ffmpeg 处理"""
    # [SEC-6] 参数校验：类型/范围/存在性；ffmpeg 加 timeout 防挂死永久占用线程；
    # dst 由源路径派生（同目录同名），天然限制写入范围
    data = request.json or {}
    src_path = data.get('path')
    try:
        start, end = float(data.get('start')), float(data.get('end'))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "start/end 必须为数字"})
    if not src_path or not os.path.exists(src_path):
        return jsonify({"success": False, "message": "源文件不存在"})
    if not (0 <= start < end):
        return jsonify({"success": False, "message": "时间范围非法"})
    _lg = _lock_guard([src_path])
    if _lg:
        return _lg

    base, ext = os.path.splitext(src_path)
    dst_path = f"{base}_trim_{str(start).replace('.', '_')}{ext}"
    # [BUG-P2-09] 冲突检测：此前 ffmpeg -y 直接覆盖已存在的同名 *_trim_* 文件
    counter = 1
    while os.path.exists(dst_path):
        dst_path = f"{base}_trim_{str(start).replace('.', '_')}_{counter}{ext}"
        counter += 1

    ffmpeg_path = utils.get_tool_path('ffmpeg')
    if not ffmpeg_path:
        return jsonify({"success": False, "message": "FFmpeg not found"})

    cmd = [ffmpeg_path, '-y', '-i', src_path, '-ss', str(start), '-to', str(end),
           '-c', 'copy', dst_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True,
                       timeout=const.AUDIO_TRIM_TIMEOUT_S)
        invalidate_cache(os.path.dirname(dst_path))
        return jsonify({"success": True, "path": dst_path})
    except subprocess.TimeoutExpired:
        try: os.remove(dst_path)
        except OSError: pass
        return jsonify({"success": False, "message": f"FFmpeg 处理超时（{const.AUDIO_TRIM_TIMEOUT_S}s）"})
    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "message": str(e.stderr.decode())})

@app.route('/api/mkdir', methods=['POST'])
def api_mkdir():
    """新建文件夹"""
    data = request.json
    parent_path = data.get('path')
    folder_name = data.get('name')
    
    if not parent_path or not folder_name:
        return jsonify({'success': False, 'message': '参数缺失'})

    # [SEC-4] 禁止多级/越权目录创建
    folder_name = unicodedata.normalize('NFC', str(folder_name).replace('\\', '/'))
    if (not folder_name or os.path.basename(folder_name) != folder_name
            or folder_name in {'.', '..'}):
        return jsonify({'success': False, 'message': '非法文件夹名'})

    _lg = _lock_guard([parent_path])
    if _lg:
        return _lg

    try:
        # 规范化路径
        parent_path = unicodedata.normalize('NFC', parent_path.replace('\\', '/'))
        folder_name = unicodedata.normalize('NFC', folder_name)
        new_dir = os.path.join(parent_path, folder_name)
        
        if os.path.exists(new_dir):
            return jsonify({'success': False, 'message': '文件夹已存在'})
            
        os.makedirs(new_dir, exist_ok=True)
        invalidate_cache(parent_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

_SENSITIVE_KEY_RE = re.compile(r'(token|secret|password|api_?key)', re.I)


def _sanitize_plugin_config(cfg):
    """[BUG-P3-32] 插件 config 不再原样下发前端：疑似敏感字段
    （token/secret/password/api_key）打码，其余原样透出。"""
    if isinstance(cfg, dict):
        out = {}
        for k, v in cfg.items():
            if isinstance(v, str) and _SENSITIVE_KEY_RE.search(str(k)):
                out[k] = '******'
            elif isinstance(v, dict):
                out[k] = _sanitize_plugin_config(v)
            else:
                out[k] = v
        return out
    return cfg


@app.route('/api/manager/plugins')
def api_manager_plugins():
    """Get list of active and failed plugins"""
    return jsonify({
        'active': [{**p, 'config': _sanitize_plugin_config(p.get('config'))}
                   for p in plugin_manager.plugins],
        'failed': plugin_manager.failed_plugins
    })

@app.route('/api/thumb/retry', methods=['POST'])
def api_thumb_retry():
    """[UX-3] 手动重试缩略图生成：清除失败缓存并重新排队（此前用户无感知出口）"""
    data = request.json or {}
    path = data.get('path')
    if not path:
        return jsonify({'success': False, 'message': '参数缺失'})
    # [BUG-P1-01] 同 /api/list：去掉二次 unquote
    path = unicodedata.normalize('NFC', str(path).replace('\\', '/'))
    if not os.path.exists(path):
        path = unicodedata.normalize('NFD', path)
        if not os.path.exists(path):
            return jsonify({'success': False, 'message': '文件不存在'})
    thumb_engine.clear_failure(path)
    size_type = data.get('size', 'small')
    if size_type not in ('small', 'large', 'preview'):
        size_type = 'small'
    target = thumb_engine.get_thumb_path(path, size_type=size_type)
    with thumbnail_lock:
        key = (path, size_type)
        if key not in thumbnail_pending:
            thumbnail_pending.add(key)
            thumbnail_queue.put((path, target, size_type))
    return jsonify({'success': True})

@app.route('/api/thumb/poll')
def api_thumb_poll():
    """嵌入模式下前端轮询检查缩略图更新
    
    前端定期请求此端点，获取所有待刷新的缩略图路径。
    如果有更新，前端会批量调用 refreshThumbnail 刷新显示。
    支持前端确认机制：前端传入已处理的路径，后端从队列中移除。
    """
    # 获取前端已确认处理的路径
    confirmed_paths = request.args.getlist('confirmed')
    
    with _thumbnail_queue_lock:
        # 如果前端有确认，移除已处理的项目
        if confirmed_paths:
            confirmed_set = set(confirmed_paths)
            # 过滤掉已确认的路径
            _thumbnail_update_queue[:] = [item for item in _thumbnail_update_queue if item['path'] not in confirmed_set]
        
        # 返回队列中的所有待刷新路径
        pending_paths = [{'path': item['path'], 'mtime': item['mtime']} for item in _thumbnail_update_queue]
        
        return jsonify({'paths': pending_paths})

@app.route('/static_plugin/<plugin_name>/<path:filename>')
def serve_plugin_static(plugin_name, filename):
    """接管并服务插件内部的静态资源

    [SEC-5] send_from_directory 自带目录穿越防护（safe_join），
    且对 plugin_name 做 realpath 归属校验，杜绝 ../ 读任意文件。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    plugins_root = os.path.realpath(os.path.join(base_dir, 'plugins'))
    static_root = os.path.realpath(os.path.join(plugins_root, plugin_name, 'static'))
    if not static_root.startswith(plugins_root + os.sep):
        return "File not found", 404
    try:
        return send_from_directory(static_root, filename)
    except NotFound:
        return "File not found", 404

# --- Smart Folders Logic ---
def _volume_available(path: str) -> bool:
    """[ARCH-10] path 所在卷是否可用。

    外接盘/网络盘卸载时 os.path.exists 全部 False——若据此清理 meta，
    重新挂载后用户标签/评分就丢了。内置卷恒可用；/Volumes/<卷> 不可达
    即视为卸载，跳过孤儿清理。

    [R1-1] 必须把路径归约到卷根 /Volumes/<name>（恰好两个 '/'）再判定。
    此前第一次循环就拿完整文件路径命中 >=3 斜杠条件直接 return
    exists(文件)——调用前提恰是该文件不存在 → 恒 False，外接卷孤儿
    meta 永不清理。"""
    rest = path
    while rest and rest != '/':
        if rest.startswith('/Volumes/') and rest.count('/') == 2:
            return os.path.exists(rest)
        rest = os.path.dirname(rest)
    return True

def _backup_orphan_rows(rows: list) -> None:
    """[BUG-P2-07] 清理孤儿 meta 行前先落盘备份（JSONL 追加）。

    _volume_available 对非 /Volumes 挂载点恒 True：NFS/自定义挂载暂不可达时
    孤儿行会被当"已删除"清掉——网络盘用户的标签/评分由此可找回。
    [U10] 调用方传入的是 7 列行（U5 起 SELECT 含 color/flag），此前按 5 名
    解包必抛 ValueError 且只捕 OSError——异常直接炸穿 execute_smart_query，
    任何含孤儿行的智能文件夹整体 500。改按索引取值，列数向前兼容。"""
    if not rows:
        return
    try:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.assetdeck')
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(backup_dir, 'orphan_meta_backup.jsonl')
        with open(backup_file, 'a', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps({
                    'backed_up_at': time.time(),
                    'path': r[0], 'tags': r[1], 'comments': r[2],
                    'rating': r[3], 'cached_info': r[4],
                    'color': r[5] if len(r) > 5 else 0,
                    'flag': r[6] if len(r) > 6 else 0,
                }, ensure_ascii=False) + '\n')
        logger.info(f"[BUG-P2-07] 孤儿 meta 清理前已备份 {len(rows)} 行 → {backup_file}")
    except OSError as e:
        logger.warning(f"[BUG-P2-07] 孤儿 meta 备份失败（继续清理）: {e}")


def execute_smart_query(rules):
    """Execute smart folder rules and return compatible file list"""
    # [V1/GAP-02] 技术列随行下发（时长角标/拍摄日期筛选）
    # [V5 复查] 排除失联行（索引器确认的 missing=1 按 scanner 策略保留，
    # 但不再出现在智能文件夹结果里）与软锁目录
    query_parts = ["SELECT path, tags, comments, rating, cached_info, color, flag, duration, date_taken "
                   "FROM file_meta WHERE (missing IS NULL OR missing != 1)"]
    params = []
    locks = _active_lock_dirs()

    # Parse rules
    if 'min_rating' in rules and rules['min_rating'] is not None:
        try:
            rating_val = int(rules['min_rating'])
            query_parts.append("AND rating >= ?")
            params.append(rating_val)
        except (TypeError, ValueError): pass
        
    if 'tags_include' in rules and rules['tags_include']:
        # Simple LIKE query for tags
        # [U9] ESCAPE 转义：用户输入的 % / _ 按字面匹配，不作通配符
        query_parts.append("AND tags LIKE ? ESCAPE '\\'")
        params.append('%' + _like_escape(str(rules['tags_include'])) + '%')

    # [U5 收尾] 关键词规则：与 /api/search 同语义（名称/标签/备注子串），
    # "保存为智能文件夹"把当前搜索转成本规则，保证回放结果一致
    if 'q' in rules and rules['q']:
        like = '%' + _like_escape(str(rules['q'])) + '%'
        query_parts.append("AND (path LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR comments LIKE ? ESCAPE '\\')")
        params.extend([like, like, like])

    # [U5 收尾] 精确标签规则：走结构化 file_tags 关联（LIKE 子串会把
    # "red" 命中 "hundred"，此处必须精确等值）
    # [V2/GAP-06] 含全部子孙标签（递归 CTE 展开）
    if 'tag' in rules and rules['tag']:
        tag_ids = _tag_and_descendant_ids(str(rules['tag']))
        if tag_ids:
            ph = ','.join('?' * len(tag_ids))
            query_parts.append(
                f"""AND EXISTS (SELECT 1 FROM file_tags ft
                          WHERE ft.file_path = file_meta.path AND ft.tag_id IN ({ph}))""")
            params.extend(tag_ids)
        else:
            query_parts.append("AND 0")  # 标签不存在：结果恒空而非全库

    # [U5/GAP-03] 颜色标记规则：1红/2黄/3绿/4蓝/5紫（0/空 = 不过滤）
    if 'color' in rules and rules['color'] not in (None, '', 0, '0'):
        try:
            color_val = int(rules['color'])
        except (TypeError, ValueError):
            color_val = 0
        if 1 <= color_val <= 5:
            query_parts.append("AND color = ?")
            params.append(color_val)

    # [U5/GAP-03] 旗标规则：pick=旗标 / noreject=排除拒收 / reject=仅拒收
    if 'flag' in rules and rules['flag'] not in (None, '', 'all', 0, '0'):
        flag_val = str(rules['flag'])
        if flag_val == 'pick':
            query_parts.append("AND flag = 1")
        elif flag_val == 'reject':
            query_parts.append("AND flag = 2")
        elif flag_val == 'noreject':
            query_parts.append("AND flag != 2")

    # [V1/GAP-02] 技术元数据规则：时长 / 拍摄日期 / 相机 / 有无 GPS
    # （列由索引器回填，旧行 NULL = 不过该条件，自然降级）
    def _rule_num(key):
        try:
            return float(rules[key])
        except (TypeError, ValueError):
            return None
    if 'min_duration' in rules:
        v = _rule_num('min_duration')
        if v:
            query_parts.append("AND duration IS NOT NULL AND duration >= ?")
            params.append(v)
    if 'max_duration' in rules:
        v = _rule_num('max_duration')
        if v:
            query_parts.append("AND duration IS NOT NULL AND duration <= ?")
            params.append(v)
    if 'date_from' in rules and rules['date_from']:
        ts = _date_str_to_epoch(str(rules['date_from']))
        if ts:
            query_parts.append("AND date_taken IS NOT NULL AND date_taken >= ?")
            params.append(ts)
    if 'date_to' in rules and rules['date_to']:
        ts = _date_str_to_epoch(str(rules['date_to']))
        if ts:
            query_parts.append("AND date_taken IS NOT NULL AND date_taken < ?")
            params.append(ts + 86400)
    if 'camera' in rules and rules['camera']:
        query_parts.append("AND camera LIKE ? ESCAPE '\\'")
        params.append('%' + _like_escape(str(rules['camera'])) + '%')
    if rules.get('has_gps') in (1, '1', True, 'true'):
        query_parts.append("AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL")
    # [V3/GAP-09] 自动主色桶规则
    if 'dom_bucket' in rules and rules['dom_bucket'] not in (None, ''):
        try:
            query_parts.append("AND dom_color = ?")
            params.append(int(rules['dom_bucket']))
        except (TypeError, ValueError):
            pass

    sql = " ".join(query_parts)
    
    files = []
    orphan_paths = []  # [ARCH-10] 已删除文件的 meta 行，查询后统一清理
    orphan_rows = []   # [BUG-P2-07] 待清理行的完整数据（清理前先备份）
    scan_cache = {}    # [U10] 请求级共享缓存：命中集逐文件 get_thumb_path 此前
                       # 每条都全目录 scandir（2000 条命中 = 2000 次同目录扫描）
    try:
        # [U9] 连接 finally 化：SQL 异常不再泄漏 sqlite 连接
        conn = _db()
        try:
            c = conn.cursor()
            c.execute(sql, params)
            rows = c.fetchall()
        finally:
            conn.close()

        for row in rows:
            path, tags, comments, rating, cached_info = row[0], row[1], row[2], row[3], row[4]
            color = row[5] if len(row) > 5 else 0
            flag = row[6] if len(row) > 6 else 0
            duration = row[7] if len(row) > 7 else None
            date_taken_ts = row[8] if len(row) > 8 else None
            # Ensure path format matches system
            path = path.replace('\\', '/')
            # [V5 复查] 软锁目录内容不进智能文件夹结果
            if _path_in_locks(path, locks):
                continue
            if not os.path.exists(path):
                # [ARCH-10] 智能文件夹此前只跳过不清理：已删文件的 meta 行
                # 永久滞留（库无限增长 + 每次查询白扫）。记录待删除。
                # 卷不可用（外接盘卸载）时不清理，防止误删用户标签。
                # 注：missing=1 行在 SELECT 已被过滤——那是索引器确认失联的
                # 行，按 scanner"不删行、等卷回来"的策略保留；走到这里的
                # 是从未被扫描过的已删文件行，清理语义与 U10 备份一致
                if _volume_available(path):
                    orphan_paths.append(row[0])
                    orphan_rows.append(row)
                continue

            # [ARCH-10] 历史 NFD 行自愈：就地归一；有 NFC 孪生行则
            # [R1-3] 合并非空数据后丢弃本行。
            # [U9] 连接 finally 化 + 不再每行反复开关连接
            nfc = unicodedata.normalize('NFC', path)
            if nfc != path:
                conn = _db()
                try:
                    if conn.execute("SELECT 1 FROM file_meta WHERE path = ?", (nfc,)).fetchone():
                        _absorb_nfd_twin(conn, nfc, row[0])
                        conn.commit()
                        continue
                    conn.execute("UPDATE file_meta SET path = ? WHERE path = ?", (nfc, row[0]))
                    conn.commit()
                finally:
                    conn.close()
                path = nfc
            
            try:
                stat = os.stat(path)
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d')
                size_str = utils.get_size_str(stat.st_size)
                name = unicodedata.normalize('NFC', os.path.basename(path))
                
                # Determine type
                # [FUNC-4] 复用公共 classify_ext：旧实现把 doc/code/archive
                # 复制粘贴成 'image'，类型过滤与图标错乱
                ext = os.path.splitext(name)[1].lower()
                ftype = classify_ext(ext)
                
                # Filter by type if requested
                if 'type' in rules and rules['type'] and rules['type'] != 'all':
                    if ftype != rules['type']: continue

                # Thumbnail
                thumb_path = thumb_engine.get_thumb_path(path, scan_cache=scan_cache)
                t_ver = int(os.stat(thumb_path).st_mtime) if os.path.exists(thumb_path) else 0
                
                # 性能优化：智能文件夹列表加载时不提取视频元数据，优先使用缓存
                video_res = cached_info
                
                files.append({
                    'name': name,
                    'path': path,
                    'type': ftype,
                    'size': size_str,
                    'mtime': mtime,
                    't_ver': t_ver,
                    'res': video_res,
                    'tags': tags,
                    'comments': comments,
                    'rating': rating,
                    'color': color or 0,
                    'flag': flag or 0,
                    'duration': duration,
                    'date_taken_ts': date_taken_ts,
                    'date_taken': (datetime.fromtimestamp(date_taken_ts, tz=timezone.utc).strftime('%Y-%m-%d') if date_taken_ts else None),
                })
            except Exception as e:
                logger.error(f"Error processing file {path}: {e}")
    except Exception as e:
        logger.error(f"Smart query error: {e}")

    # [ARCH-10] 清理已删文件的 meta 行（单次上限 500，防误删大面积爆量）
    if orphan_paths:
        # [BUG-P2-07] 清理前备份完整行数据（误判可找回）。
        # [U10] 备份自身失败不再炸穿整个查询（此前调用点在主 try 之外）
        try:
            _backup_orphan_rows(orphan_rows[:500])
        except Exception as e:
            logger.warning(f"[BUG-P2-07] 孤儿 meta 备份异常（继续清理）: {e}")
        conn = None
        try:
            conn = _db()
            c = conn.cursor()
            c.executemany("DELETE FROM file_meta WHERE path = ?",
                          [(p,) for p in orphan_paths[:500]])
            # [U10] file_tags 一并清理（对齐 _delete_permanent），否则
            # /api/tags 计数对已删文件永久虚高——U9 修复在此处漏网
            c.executemany("DELETE FROM file_tags WHERE file_path = ?",
                          [(p,) for p in orphan_paths[:500]])
            c.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM file_tags)")
            conn.commit()
            logger.info(f"[ARCH-10] 智能文件夹清理已删文件 meta 行 {len(orphan_paths[:500])} 条")
        except Exception as e:
            logger.warning(f"[ARCH-10] 孤儿 meta 清理失败: {e}")
        finally:
            if conn is not None:
                conn.close()

    files.sort(key=lambda x: x['name'].lower())
    return files

@app.route('/api/smart_folders', methods=['GET'])
def get_smart_folders():
    # [BUG-P2-01] conn.close() finally 化：SQL 异常不再泄漏 sqlite 连接
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("SELECT id, name, rules FROM smart_folders ORDER BY created_at DESC")
        folders = [{'id': r[0], 'name': r[1], 'rules': json.loads(r[2])} for r in c.fetchall()]
        return jsonify(folders)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route('/api/smart_folders', methods=['POST'])
def create_smart_folder():
    data = request.json
    name = data.get('name')
    rules = data.get('rules')
    if not name or not rules:
        return jsonify({'error': 'Missing name or rules'}), 400

    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("INSERT INTO smart_folders (name, rules) VALUES (?, ?)", (name, json.dumps(rules)))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route('/api/smart_folders/<int:folder_id>', methods=['PUT'])
def update_smart_folder(folder_id):
    data = request.json
    name = data.get('name')
    rules = data.get('rules')

    if not name or not rules:
        return jsonify({'error': 'Missing name or rules'}), 400

    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("UPDATE smart_folders SET name = ?, rules = ? WHERE id = ?", (name, json.dumps(rules), folder_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route('/api/smart_folders/<int:folder_id>', methods=['DELETE'])
def delete_smart_folder(folder_id):
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("DELETE FROM smart_folders WHERE id = ?", (folder_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route('/api/smart_folders/<int:folder_id>/files')
def get_smart_folder_files(folder_id):
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("SELECT rules FROM smart_folders WHERE id = ?", (folder_id,))
        row = c.fetchone()

        if not row:
            return jsonify({'error': 'Folder not found'}), 404

        rules = json.loads(row[0])
        files = execute_smart_query(rules)
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


# ============================================================================
# [U5/GAP-01] 标签体系 2.0：结构化标签（tags + file_tags），legacy 逗号列双写
# ============================================================================

def _sync_file_tags_from_legacy(c, path: str, tags_str: str) -> None:
    """[U5/GAP-01] update_meta 写 legacy tags 串时反向同步 file_tags：
    重建该文件的标签关联、创建未知名、回收不再被引用的空标签。"""
    names = sorted({t.strip() for t in (tags_str or '').split(',') if t.strip()})
    c.execute("DELETE FROM file_tags WHERE file_path = ?", (path,))
    for name in names:
        c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                     SELECT ?, id FROM tags WHERE name = ?""", (path, name))
    c.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM file_tags)")


def _sync_legacy_tags(c, path: str) -> None:
    """把 file_tags 的标签集合回写 legacy 逗号列（排序去重），保持旧读取
    路径（/api/list 的 tags 字段、批量面板）与结构化表一致。"""
    names = [r[0] for r in c.execute(
        """SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
           WHERE ft.file_path = ? ORDER BY t.name""", (path,)).fetchall()]
    c.execute("UPDATE file_meta SET tags = ? WHERE path = ?",
              (",".join(names), path))


def _tag_and_descendant_ids(name: str):
    """[V2/GAP-06] 标签名 → 该标签及全部子孙的 id（递归 CTE）。
    语义对齐 Lightroom：点父标签 = 选中其子树下所有标签的文件。
    标签不存在返回 None。自持连接（规则解析阶段尚无游标可用）。"""
    conn = _db()
    try:
        c = conn.cursor()
        row = c.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if not row:
            return None
        c.execute("""WITH RECURSIVE tag_tree(id) AS (
                        SELECT ?
                        UNION
                        SELECT t.id FROM tags t JOIN tag_tree tt ON t.parent_id = tt.id)
                      SELECT id FROM tag_tree""", (row[0],))
        return [r[0] for r in c.fetchall()]
    finally:
        conn.close()


@app.route('/api/tags', methods=['GET'])
def api_tags_list():
    """全部标签（含使用计数），按名称排序"""
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        rows = c.execute("""
            SELECT t.id, t.name, t.color, t.parent_id, COUNT(ft.file_path)
            FROM tags t LEFT JOIN file_tags ft ON ft.tag_id = t.id
            GROUP BY t.id ORDER BY t.name COLLATE NOCASE""").fetchall()
        return jsonify([{'id': r[0], 'name': r[1], 'color': r[2],
                         'parent_id': r[3], 'count': r[4]} for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/tags', methods=['POST'])
def api_tags_create():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '标签名不能为空'}), 400
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO tags (name, color, parent_id) VALUES (?, ?, ?)",
                  (name, data.get('color'), data.get('parent_id')))
        conn.commit()
        row = c.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        return jsonify({'success': True, 'id': row[0] if row else None})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/tags/<int:tag_id>', methods=['PUT'])
def api_tags_update(tag_id):
    data = request.json or {}
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'success': False, 'message': '标签名不能为空'}), 400
            # [U10] 改名撞已有标签会制造重名 tags 行（file_tags 双挂、
            # /api/tags 计数错乱）——先拒绝
            dup = c.execute("SELECT id FROM tags WHERE name = ? AND id != ?",
                            (name, tag_id)).fetchone()
            if dup:
                return jsonify({'success': False, 'message': '同名标签已存在'}), 400
            c.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
            # [U10] 此前声称"同步 legacy 列"但代码在改名之后才读 old 且从未
            # 使用——纯死代码。改名后所有挂该标签文件的 legacy 逗号串滞留
            # 旧名（FTS 索引旧文本仍可命中、卡片 chips 显示已改名前的标签）。
            # 按 file_tags 重算受影响文件的 legacy 串。
            paths = [r[0] for r in c.execute(
                "SELECT file_path FROM file_tags WHERE tag_id = ?", (tag_id,)).fetchall()]
            for p in paths:
                _sync_legacy_tags(c, p)
        if 'color' in data:
            c.execute("UPDATE tags SET color = ? WHERE id = ?", (data.get('color'), tag_id))
        # [V2/GAP-06] 层级：设置/变更父标签（防环：沿新父链上溯不得遇到自身）
        if 'parent_id' in data:
            pid = data.get('parent_id')
            if pid is not None:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'message': 'parent_id 非法'}), 400
                if pid == tag_id:
                    return jsonify({'success': False, 'message': '不能把自己设为父标签'}), 400
                cur, seen = pid, {tag_id}
                while cur is not None:
                    if cur in seen:
                        return jsonify({'success': False, 'message': '层级调整会造成标签环'}), 400
                    seen.add(cur)
                    r = c.execute("SELECT parent_id FROM tags WHERE id = ?", (cur,)).fetchone()
                    cur = r[0] if r else None
            else:
                pid = None  # 显式 null = 移到顶层
            c.execute("UPDATE tags SET parent_id = ? WHERE id = ?", (pid, tag_id))
            # 防挂到不存在的父标签上
            if pid is not None and not c.execute("SELECT 1 FROM tags WHERE id = ?", (pid,)).fetchone():
                c.execute("UPDATE tags SET parent_id = NULL WHERE id = ?", (tag_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
def api_tags_delete(tag_id):
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        # [U10] 删除前记录受影响文件，删除后重算 legacy 串（此前 legacy
        # 列滞留已删标签名：搜索仍命中、卡片 chips 仍显示）
        paths = [r[0] for r in c.execute(
            "SELECT file_path FROM file_tags WHERE tag_id = ?", (tag_id,)).fetchall()]
        c.execute("DELETE FROM file_tags WHERE tag_id = ?", (tag_id,))
        c.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        for p in paths:
            _sync_legacy_tags(c, p)
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/meta/tags', methods=['POST'])
def api_meta_tags():
    """单文件标签编辑：{path, add: [名], remove: [名]}；未知名自动创建；
    完成后回写 legacy 逗号列。"""
    data = request.json or {}
    path = data.get('path')
    add = [str(x).strip() for x in (data.get('add') or []) if str(x).strip()]
    remove = [str(x).strip() for x in (data.get('remove') or []) if str(x).strip()]
    if not path:
        return jsonify({'success': False, 'message': '参数缺失'}), 400
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                  (_meta_key(path),))
        for name in add:
            c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                         SELECT ?, id FROM tags WHERE name = ?""", (_meta_key(path), name))
        for name in remove:
            c.execute("""DELETE FROM file_tags WHERE file_path = ? AND tag_id IN
                         (SELECT id FROM tags WHERE name = ?)""", (_meta_key(path), name))
            # 空标签回收：无任何文件引用时删除
            c.execute("""DELETE FROM tags WHERE name = ? AND id NOT IN
                         (SELECT DISTINCT tag_id FROM file_tags)""", (name,))
        _sync_legacy_tags(c, _meta_key(path))
        conn.commit()
        final = [r[0] for r in c.execute(
            """SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
               WHERE ft.file_path = ? ORDER BY t.name""", (_meta_key(path),)).fetchall()]
        invalidate_cache(os.path.dirname(path))
        # [V2/GAP-04] 标签变化写回 XMP
        try:
            import meta_sync
            meta_sync.enqueue(path, {'tags': final})
        except Exception:
            pass
        return jsonify({'success': True, 'tags': final})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


# ============================================================================
# [V2/GAP-05] 静态集合：手工圈选的素材包（区别于动态智能文件夹）
# ============================================================================

@app.route('/api/collections', methods=['GET'])
def api_collections_list():
    """全部集合（含成员计数）"""
    conn = None
    try:
        conn = _db()
        rows = conn.execute("""
            SELECT c.id, c.name, c.color, c.created_at,
                   (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id = c.id)
            FROM collections c ORDER BY c.created_at DESC, c.id DESC""").fetchall()
        return jsonify([{'id': r[0], 'name': r[1], 'color': r[2],
                         'created_at': r[3], 'count': r[4]} for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/collections', methods=['POST'])
def api_collections_create():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '集合名不能为空'}), 400
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("INSERT INTO collections (name, color, created_at) VALUES (?, ?, ?)",
                  (name, data.get('color'), time.time()))
        conn.commit()
        return jsonify({'success': True, 'id': c.lastrowid})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/collections/<int:cid>', methods=['PUT'])
def api_collections_update(cid):
    data = request.get_json(force=True) or {}
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'success': False, 'message': '集合名不能为空'}), 400
            c.execute("UPDATE collections SET name = ? WHERE id = ?", (name, cid))
        if 'color' in data:
            c.execute("UPDATE collections SET color = ? WHERE id = ?", (data.get('color'), cid))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/collections/<int:cid>', methods=['DELETE'])
def api_collections_delete(cid):
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("DELETE FROM collection_items WHERE collection_id = ?", (cid,))
        c.execute("DELETE FROM collections WHERE id = ?", (cid,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/collections/<int:cid>/items', methods=['POST'])
def api_collection_items_update(cid):
    """add/remove 成员。body: {action, paths: []}"""
    data = request.get_json(force=True) or {}
    action = data.get('action')
    paths = [str(p) for p in (data.get('paths') or []) if p]
    if action not in ('add', 'remove') or not paths:
        return jsonify({'success': False, 'message': '参数缺失'}), 400
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if not c.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone():
            return jsonify({'success': False, 'message': '集合不存在'}), 404
        if action == 'add':
            now = time.time()
            for p in paths:
                key = _meta_key(p)
                nxt = c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM collection_items WHERE collection_id = ?",
                                (cid,)).fetchone()[0]
                c.execute("""INSERT OR IGNORE INTO collection_items
                             (collection_id, file_path, sort_order, added_at) VALUES (?, ?, ?, ?)""",
                          (cid, key, nxt, now))
        else:
            c.executemany("DELETE FROM collection_items WHERE collection_id = ? AND file_path = ?",
                          [(cid, _meta_key(p)) for p in paths])
        conn.commit()
        cnt = c.execute("SELECT COUNT(*) FROM collection_items WHERE collection_id = ?", (cid,)).fetchone()[0]
        return jsonify({'success': True, 'count': cnt})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/collections/<int:cid>/items', methods=['GET'])
def api_collection_files(cid):
    """集合成员文件列表（与 /api/list 同结构）。

    行清理策略：磁盘失联但仍在回收站（original_path 匹配）的行保留——
    恢复后成员关系自动复活；两者皆非的行清掉。"""
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if not c.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone():
            return jsonify({'error': '集合不存在'}), 404
        rows = c.execute("""SELECT ci.file_path, fm.tags, fm.comments, fm.rating,
                                   fm.cached_info, fm.color, fm.flag, fm.duration, fm.date_taken
                            FROM collection_items ci
                            LEFT JOIN file_meta fm ON fm.path = ci.file_path
                            WHERE ci.collection_id = ?
                            ORDER BY ci.sort_order, ci.added_at""", (cid,)).fetchall()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

    stale = []
    files = []
    scan_cache = {}
    # [V5 复查] 失联判定三重防御：在回收站（恢复后成员关系复活）/ 卷离线 /
    # NFD 卷上 NFC 键 exists 失败——三者任一命中都保留行，否则此前会把
    # 回收站成员与整块离线卷的成员行永久误删（docstring 承诺的回收站
    # 保留分支此前根本没实现）
    try:
        conn = _db()
        try:
            trash_paths = {r[0] for r in conn.execute(
                "SELECT original_path FROM trash").fetchall()}
        finally:
            conn.close()
    except Exception:
        trash_paths = set()
    for r in rows:
        p = str(r[0]).replace('\\', '/')
        if p in trash_paths:
            continue
        live = p if os.path.exists(p) else (
            unicodedata.normalize('NFD', p) if os.path.exists(unicodedata.normalize('NFD', p)) else None)
        if live is None:
            if _volume_available(p):
                stale.append(r[0])
            continue
        try:
            stat = os.stat(live)
            name = unicodedata.normalize('NFC', os.path.basename(p))
            ext = os.path.splitext(name)[1].lower()
            thumb_path = thumb_engine.get_thumb_path(p, scan_cache=scan_cache)
            t_ver = int(os.stat(thumb_path).st_mtime) if os.path.exists(thumb_path) else 0
            dts = r[8]
            files.append({
                'name': name, 'path': p, 'type': classify_ext(ext),
                'size': utils.get_size_str(stat.st_size),
                'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d'),
                'size_bytes': stat.st_size, 'mtime_ts': stat.st_mtime,
                't_ver': t_ver, 'res': r[4],
                'tags': r[1] or '', 'comments': r[2] or '', 'rating': r[3] or 0,
                'color': r[5] or 0, 'flag': r[6] or 0,
                'duration': r[7],
                'date_taken_ts': dts,
                'date_taken': (datetime.fromtimestamp(dts, tz=timezone.utc).strftime('%Y-%m-%d') if dts else None),
            })
        except OSError:
            continue
    if stale:  # 真失联（不在回收站）→ 清行
        conn = None
        try:
            conn = _db()
            conn.executemany("DELETE FROM collection_items WHERE collection_id = ? AND file_path = ?",
                             [(cid, k) for k in stale])
            conn.commit()
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
    return jsonify({'files': files})


# ============================================================================
# [V2/GAP-07] 堆叠（连拍/多版本归组）
# ============================================================================

@app.route('/api/stacks', methods=['POST'])
def api_stack_create():
    """创建堆叠。body: {paths: [≥2], cover?: path（缺省首个）}"""
    data = request.get_json(force=True) or {}
    paths = [str(p) for p in (data.get('paths') or []) if p]
    if len(paths) < 2:
        return jsonify({'success': False, 'message': '至少选择 2 个文件'}), 400
    cover = _meta_key(data.get('cover') or paths[0])
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        # 成员可能从未被打标/浏览过（无 file_meta 行）——先补基础行，
        # 否则 stack_id UPDATE 影响 0 行，堆叠静默失效
        c.executemany("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                      [(_meta_key(p),) for p in paths])
        c.execute("INSERT INTO stacks (cover_path, created_at) VALUES (?, ?)",
                  (cover, time.time()))
        sid = c.lastrowid
        c.executemany("UPDATE file_meta SET stack_id = ? WHERE path = ?",
                      [(sid, _meta_key(p)) for p in paths])
        conn.commit()
        return jsonify({'success': True, 'id': sid})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/stacks/<int:sid>', methods=['PUT'])
def api_stack_update(sid):
    """调整堆叠：{cover_path?, add?: [], remove?: []}"""
    data = request.get_json(force=True) or {}
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if not c.execute("SELECT 1 FROM stacks WHERE id = ?", (sid,)).fetchone():
            return jsonify({'success': False, 'message': '堆叠不存在'}), 404
        add_paths = [_meta_key(str(p)) for p in (data.get('add') or [])]
        if add_paths:
            c.executemany("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                          [(k,) for k in add_paths])
            c.executemany("UPDATE file_meta SET stack_id = ? WHERE path = ?",
                          [(sid, k) for k in add_paths])
        for p in (data.get('remove') or []):
            c.execute("UPDATE file_meta SET stack_id = NULL WHERE path = ? AND stack_id = ?",
                      (_meta_key(str(p)), sid))
        # [V5 复查] 成员被全部移出时清掉 stacks 行：此前留下空堆叠 + 封面
        # 指向已移出成员，列表侧 stack_size 为 0/None
        if data.get('remove'):
            if not c.execute(
                    "SELECT 1 FROM file_meta WHERE stack_id = ? LIMIT 1", (sid,)).fetchone():
                c.execute("DELETE FROM stacks WHERE id = ?", (sid,))
        if data.get('cover_path'):
            cover = _meta_key(str(data['cover_path']))
            c.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)", (cover,))
            c.execute("UPDATE file_meta SET stack_id = ? WHERE path = ?", (sid, cover))
            c.execute("UPDATE stacks SET cover_path = ? WHERE id = ?", (cover, sid))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/stacks/<int:sid>/unstack', methods=['POST'])
def api_stack_unstack(sid):
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("UPDATE file_meta SET stack_id = NULL WHERE stack_id = ?", (sid,))
        c.execute("DELETE FROM stacks WHERE id = ?", (sid,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/meta/import_xmp', methods=['POST'])
def api_meta_import_xmp():
    """[V2/GAP-04] 反向导入：读文件 XMP（sidecar 优先），DB 默认值时采用。"""
    data = request.get_json(silent=True) or {}
    import meta_sync
    result = meta_sync.import_from_files(library=data.get('library'))
    return jsonify({'success': True, **result})


# ============================================================================
# [V3/GAP-08] 视觉相似搜索（pHash 汉明距离）
# ============================================================================

def _shape_file_entries(paths, scan_cache):
    """路径列表 → 与 /api/list 同结构的文件条目（stat + 缩略图版本）。"""
    out = []
    for p in paths:
        p = str(p).replace('\\', '/')
        if not os.path.exists(p):
            continue
        try:
            stat = os.stat(p)
            name = unicodedata.normalize('NFC', os.path.basename(p))
            ext = os.path.splitext(name)[1].lower()
            thumb_path = thumb_engine.get_thumb_path(p, scan_cache=scan_cache)
            t_ver = int(os.stat(thumb_path).st_mtime) if os.path.exists(thumb_path) else 0
            out.append({'name': name, 'path': p, 'type': classify_ext(ext),
                        'size': utils.get_size_str(stat.st_size),
                        'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d'),
                        'size_bytes': stat.st_size, 'mtime_ts': stat.st_mtime,
                        't_ver': t_ver, 'res': None})
        except OSError:
            continue
    return out


@app.route('/api/similar')
def api_similar():
    """视觉相似：与目标文件 pHash 汉明距离 ≤ threshold 的前 limit 个文件。

    目标无 phash 时按需现算（缩略图已存在则零解码成本），算完回写。"""
    from media_features import features_from_file, hamming_distance
    path = request.args.get('path')
    if not path:
        return jsonify({'files': [], 'error': '参数缺失'}), 400
    path = unicodedata.normalize('NFC', path.replace('\\', '/'))
    _lg = _lock_guard([path])
    if _lg:
        return _lg
    try:
        threshold = min(max(int(request.args.get('threshold', 8) or 8), 0), 32)
    except ValueError:
        threshold = 8
    try:
        limit = min(max(int(request.args.get('limit', 50) or 50), 1), 200)
    except ValueError:
        limit = 50

    ph = None
    conn = None
    try:
        conn = _db()
        row = conn.execute("SELECT phash FROM file_meta WHERE path = ?", (_meta_key(path),)).fetchone()
        ph = row[0] if row else None
    finally:
        if conn is not None:
            conn.close()
    if not ph:
        # 按需现算：优先读已生成的 small 缩略图（零解码成本），回退源文件
        tp = thumb_engine.get_thumb_path(path, size_type='small')
        if not os.path.exists(tp):
            tp = thumb_engine.get_thumb_path(path, size_type='large')
        src = tp if os.path.exists(tp) else path
        ph, bucket = features_from_file(src)
        if ph or bucket is not None:
            conn = None
            try:
                conn = _db()
                c = conn.cursor()
                c.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                          (_meta_key(path),))
                if ph:
                    c.execute("UPDATE file_meta SET phash = ? WHERE path = ?", (ph, _meta_key(path)))
                if bucket is not None:
                    c.execute("UPDATE file_meta SET dom_color = ? WHERE path = ?", (bucket, _meta_key(path)))
                conn.commit()
            finally:
                if conn is not None:
                    conn.close()
    if not ph:
        return jsonify({'files': [], 'error': '该文件暂无可比较的缩略图，请先生成缩略图'})

    scored = []
    conn = None
    try:
        conn = _db()
        for other, oph in conn.execute(
                "SELECT path, phash FROM file_meta WHERE phash IS NOT NULL AND path != ?",
                (_meta_key(path),)).fetchall():
            d = hamming_distance(ph, oph)
            if d <= threshold:
                scored.append((d, other))
    finally:
        if conn is not None:
            conn.close()
    scored.sort(key=lambda x: x[0])
    scan_cache = {}
    files = _shape_file_entries([p for _, p in scored[:limit]], scan_cache)
    dist_map = {p: d for d, p in scored}
    for f in files:
        f['distance'] = dist_map.get(f['path'])
    return jsonify({'files': files, 'phash': ph, 'threshold': threshold})


# ============================================================================
# [V3/GAP-12] 导入模块：源目录 → 素材库（日期归档 + 模板重命名 + 查重）
# ============================================================================

@app.route('/api/import/run', methods=['POST'])
def api_import_run():
    """从源目录导入到素材库目录。body: {source, dest, date_folders?, subdir?,
    name_template?, skip_dup?, delete_source?}"""
    data = request.get_json(force=True) or {}
    source = unicodedata.normalize('NFC', str(data.get('source') or '').replace('\\', '/'))
    dest = unicodedata.normalize('NFC', str(data.get('dest') or '').replace('\\', '/'))
    if not source or not os.path.isdir(source):
        return jsonify({'success': False, 'message': '源目录不存在'}), 400
    if not dest or not os.path.isdir(dest):
        return jsonify({'success': False, 'message': '目标目录不存在'}), 400
    _lg = _lock_guard([dest])
    if _lg:
        return _lg
    date_folders = bool(data.get('date_folders', True))
    subdir_name = str(data.get('subdir') or '').strip()
    name_template = str(data.get('name_template') or '')
    skip_dup = bool(data.get('skip_dup', True))
    delete_source = bool(data.get('delete_source', False))
    import rename_template

    imported = dup_skipped = failed = 0
    errors = []
    cap_hit = False
    conn = _db()
    try:
        c = conn.cursor()
        seq = 1
        for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for name in filenames:
                if name.startswith('.') or name.lower().endswith('.xmp'):
                    continue
                if imported + dup_skipped >= 5000:
                    # [V5 复查] 只记录一次：此前 walk 每个剩余目录都会 append
                    # 一条相同的上限错误
                    if not cap_hit:
                        errors.append('达到单次导入上限 5000 个文件')
                        cap_hit = True
                    break
                src = os.path.join(dirpath, name)
                try:
                    st = os.stat(src)
                except OSError:
                    continue
                # 目标子目录：固定子目录 + 年/年-日（拍摄日期优先，缺省 mtime）
                parts = []
                if subdir_name:
                    parts.append(rename_template.sanitize(subdir_name))
                if date_folders:
                    key = _meta_key(src)
                    row = c.execute("SELECT date_taken FROM file_meta WHERE path = ?", (key,)).fetchone()
                    taken = row[0] if row and row[0] else None
                    # [V5 复查] date_taken 是 timegm(UTC 日) 约定值 → gmtime；
                    # mtime 兜底是真 epoch → 本地日期（与 Finder 显示一致，
                    # 此前东八区凌晨的文件会被归到前一天的文件夹）
                    gm = time.gmtime(taken) if taken else time.localtime(st.st_mtime)
                    parts.append(time.strftime('%Y', gm))
                    parts.append(time.strftime('%Y-%m-%d', gm))
                tdir = os.path.join(dest, *parts) if parts else dest
                try:
                    os.makedirs(tdir, exist_ok=True)
                except OSError as e:
                    errors.append(f"{name}: 建目录失败 {e}")
                    failed += 1
                    continue
                # 目标名：模板渲染或原名
                target_name = name
                if name_template:
                    base, ext = os.path.splitext(name)
                    meta = {}
                    mrow = c.execute(
                        "SELECT date_taken, mtime, camera, iso FROM file_meta WHERE path = ?",
                        (_meta_key(src),)).fetchone()
                    if mrow:
                        meta = {'date_taken': mrow[0], 'mtime': mrow[1], 'camera': mrow[2], 'iso': mrow[3]}
                    if not meta.get('mtime'):
                        meta['mtime'] = st.st_mtime
                    target_name = rename_template.render(name_template, {
                        'name': base, 'ext': ext, 'seq': seq, 'meta': meta,
                        'parent': os.path.basename(source), 'parent2': '',
                    })
                    # [V5 复查] 与 _plan_renames 同口径：模板已含 {ext}（含带
                    # 修饰符写法）时不重复追加，否则双拼扩展名
                    if not re.search(r'\{\s*ext\s*(:[^{}]*)?\}', name_template):
                        target_name += ext
                    if not target_name or target_name in ('.', '..'):
                        target_name = name
                target = os.path.join(tdir, target_name)
                # 查重：同名同体积视为重复
                if skip_dup and os.path.exists(target):
                    try:
                        if os.path.getsize(target) == st.st_size:
                            dup_skipped += 1
                            continue
                    except OSError:
                        pass
                n = 1
                nb, ne = os.path.splitext(target_name)
                while os.path.exists(target):
                    target = os.path.join(tdir, f"{nb} ({n}){ne}")
                    n += 1
                try:
                    shutil.copy2(src, target)
                    imported += 1
                    seq += 1
                    # 入库：基础行 + 后台技术元数据/缩略图
                    c.execute("""INSERT INTO file_meta (path, tags, comments, rating, mtime, size, missing, indexed_at)
                                 VALUES (?, '', '', 0, ?, ?, 0, ?)
                                 ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size, missing = 0""",
                              (_meta_key(target), os.path.getmtime(target), os.path.getsize(target), time.time()))
                    conn.commit()
                    try:
                        from thumbs import _meta_queue
                        _meta_queue.put(target)
                    except Exception:
                        pass
                    if delete_source:
                        try:
                            send2trash(src)
                        except Exception as e:
                            errors.append(f"{name}: 删除源失败 {e}")
                except Exception as e:
                    failed += 1
                    errors.append(f"{name}: {e}")
                    # 半成品清理
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except OSError:
                        pass
    finally:
        conn.close()
    try:
        invalidate_cache(dest)
    except Exception:
        pass
    return jsonify({'success': failed == 0, 'imported': imported, 'skipped': dup_skipped,
                    'failed': failed, 'errors': errors[:20], 'dest': dest})


# ============================================================================
# [V3/GAP-13] 库统计仪表盘 + [V3/GAP-14] 缓存管理
# ============================================================================

@app.route('/api/stats')
def api_stats():
    """库统计：总量/体积/类型分布/评分分布/标签 TOP/失联/时长合计。"""
    conn = None
    try:
        conn = _db()
        # [V5 复查] 失联行（missing=1，卷离线/文件已删）不计入总量/体积/
        # 类型分布/时长合计——统计此前把幽灵行一并计入导致虚高
        total_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(duration), 0) "
            "FROM file_meta WHERE (missing IS NULL OR missing != 1)").fetchone()
        # 扩展名分布在 Python 侧聚合（SQLite 无 reverse()，SUBSTR 反查扩展名不可行）
        ext_agg = {}
        for p, sz in conn.execute(
                "SELECT path, COALESCE(size, 0) FROM file_meta "
                "WHERE (missing IS NULL OR missing != 1)").fetchall():
            ext = os.path.splitext(str(p))[1].lower() or '?'
            e = ext_agg.setdefault(ext, {'count': 0, 'bytes': 0})
            e['count'] += 1
            e['bytes'] += sz or 0
        ext_rows = sorted(ext_agg.items(), key=lambda kv: -kv[1]['bytes'])[:15]
        rating_rows = conn.execute(
            "SELECT rating, COUNT(*) FROM file_meta WHERE rating > 0 GROUP BY rating ORDER BY rating").fetchall()
        color_rows = conn.execute(
            "SELECT color, COUNT(*) FROM file_meta WHERE color > 0 GROUP BY color ORDER BY color").fetchall()
        flag_rows = conn.execute(
            "SELECT flag, COUNT(*) FROM file_meta WHERE flag > 0 GROUP BY flag ORDER BY flag").fetchall()
        tag_rows = conn.execute(
            """SELECT t.name, COUNT(*) FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
               GROUP BY t.id ORDER BY COUNT(*) DESC LIMIT 20""").fetchall()
        missing = conn.execute("SELECT COUNT(*) FROM file_meta WHERE missing = 1").fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM file_meta WHERE indexed_at IS NOT NULL AND indexed_at > ?",
            (time.time() - 7 * 86400,)).fetchone()[0]
        return jsonify({
            'total_files': total_row[0],
            'total_bytes': total_row[1],
            'total_duration': total_row[2],
            'by_ext': [{'ext': k, 'count': v['count'], 'bytes': v['bytes']} for k, v in ext_rows],
            'ratings': {str(r[0]): r[1] for r in rating_rows},
            'colors': {str(r[0]): r[1] for r in color_rows},
            'flags': {str(r[0]): r[1] for r in flag_rows},
            'top_tags': [{'name': r[0], 'count': r[1]} for r in tag_rows],
            'missing': missing,
            'recent_7d': recent,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


def _dir_size_bytes(path: str) -> int:
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def _local_cache_dirs():
    """遍历素材库找 .cache 目录（单层深度不限但总量护栏 200 个）。"""
    out = []
    for lib in (l.get('path') for l in load_config().get('libraries', []) if l.get('path')):
        if not os.path.isdir(lib):
            continue
        for dirpath, dirnames, _ in os.walk(lib, followlinks=False):
            # [V5 复查] .cache 本身以 '.' 开头，此前一刀切剪枝后 walk 永远
            # 不会进入 .cache——下面的 basename 判断是死代码，本地缓存体积
            # 恒为 0、cleanup 永远清不到
            dirnames[:] = [d for d in dirnames
                           if (not d.startswith('.') or d == '.cache')
                           and d not in {'.trash'}]
            if os.path.basename(dirpath) == '.cache':
                out.append(dirpath)
                if len(out) >= 200:
                    return out
    return out


@app.route('/api/cache/size')
def api_cache_size():
    """缩略图缓存体积：全局 .assetdeck + 各库本地 .cache。"""
    try:
        gdir = thumb_engine.global_cache
        gsize = _dir_size_bytes(gdir) if os.path.isdir(gdir) else 0
        local = [{'path': d, 'bytes': _dir_size_bytes(d)} for d in _local_cache_dirs()]
        return jsonify({'global': {'path': gdir, 'bytes': gsize},
                        'local': local,
                        'total_bytes': gsize + sum(x['bytes'] for x in local)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cache/cleanup', methods=['POST'])
def api_cache_cleanup():
    """清理缩略图缓存。body: {scope: 'global'|'local'|'all', older_days?: int}。

    older_days 提供时只清理 mtime 早于 N 天的文件（file_explorer 缩略图
    可随时重建，清理不影响素材本体）。"""
    data = request.get_json(silent=True) or {}
    scope = data.get('scope', 'all')
    try:
        older_days = int(data.get('older_days') or 0)
    except (TypeError, ValueError):
        older_days = 0
    cutoff = time.time() - older_days * 86400 if older_days > 0 else None

    def _clean_dir(d):
        removed = 0
        if not os.path.isdir(d):
            return 0
        for dp, _, fs in os.walk(d, topdown=False):
            for f in fs:
                fp = os.path.join(dp, f)
                try:
                    if cutoff is None or os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                        removed += 1
                except OSError:
                    continue
            if cutoff is None:
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
        return removed

    cleaned = 0
    try:
        if scope in ('global', 'all'):
            cleaned += _clean_dir(thumb_engine.global_cache)
        if scope in ('local', 'all'):
            for d in _local_cache_dirs():
                cleaned += _clean_dir(d)
        with CACHE_LOCK:
            DIR_CACHE.clear()
        return jsonify({'success': True, 'cleaned_files': cleaned})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================================
# [V4/GAP-16] 地理视图 / [GAP-17] OCR / [GAP-18] 导出预设+水印 /
# [GAP-19] 视频标记 / [GAP-21] AI 打标 / [GAP-22] 文件夹软锁
# ============================================================================

@app.route('/api/map/files')
def api_map_files():
    """有 GPS 的素材（地图 pin）。上限 5000，按拍摄时间倒序。"""
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            """SELECT path, gps_lat, gps_lon, date_taken FROM file_meta
               WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL
                 AND (missing IS NULL OR missing != 1)
               ORDER BY COALESCE(date_taken, mtime) DESC LIMIT 5000""").fetchall()
        # [V5 复查] 软锁目录的坐标/文件名不泄漏（与 /api/list 缩略图口径一致）
        locks = _active_lock_dirs()
        files = []
        for p, lat, lon, ts in rows:
            p = str(p).replace('\\', '/')
            if _path_in_locks(p, locks):
                continue
            if not os.path.exists(p):
                continue
            files.append({'path': p, 'name': unicodedata.normalize('NFC', os.path.basename(p)),
                          'lat': lat, 'lon': lon,
                          'date_taken': (datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d') if ts else None)})
        return jsonify({'files': files, 'total': len(files)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/ocr/status')
def api_ocr_status():
    """[V4/GAP-17] OCR 引擎/开关状态。"""
    import ocr_engine
    return jsonify(ocr_engine.status())


@app.route('/api/ocr/run', methods=['POST'])
def api_ocr_run():
    """对单图执行 OCR（同步，手动触发不受 ocr_enabled 开关限制）。"""
    data = request.get_json(force=True) or {}
    path = data.get('path')
    if not path or not os.path.exists(path):
        return jsonify({'success': False, 'message': '文件不存在'}), 400
    _lg = _lock_guard([path])
    if _lg:
        return _lg
    import ocr_engine
    if ocr_engine.detect_engine() is None:
        return jsonify({'success': False,
                        'message': '无可用 OCR 引擎（需要 pyobjc-Vision 或 rapidocr_onnxruntime）'}), 400
    ok = ocr_engine.process(path)
    text = ''
    conn = None
    try:
        conn = _db()
        row = conn.execute("SELECT text FROM ocr_text WHERE file_path = ?",
                           (_meta_key(path),)).fetchone()
        text = row[0] if row else ''
    finally:
        if conn is not None:
            conn.close()
    return jsonify({'success': bool(ok), 'text': text})


@app.route('/api/ocr/text')
def api_ocr_text():
    # [V5 复查] OCR 文本即图内文字内容，锁定目录同样不能读
    _lg = _lock_guard([request.args.get('path') or ''])
    if _lg:
        return _lg
    conn = None
    try:
        conn = _db()
        row = conn.execute("SELECT text, ocr_at FROM ocr_text WHERE file_path = ?",
                           (_meta_key(request.args.get('path') or ''),)).fetchone()
        return jsonify({'text': row[0] if row else '', 'ocr_at': row[1] if row else None})
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/export/presets', methods=['GET', 'POST'])
def api_export_presets():
    """[V4/GAP-18] 导出预设：GET 列表 / POST {name, config}。"""
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if request.method == 'GET':
            rows = c.execute("SELECT id, name, config FROM export_presets ORDER BY id DESC").fetchall()
            return jsonify([{'id': r[0], 'name': r[1], 'config': json.loads(r[2])} for r in rows])
        data = request.get_json(force=True) or {}
        name = (data.get('name') or '').strip()
        if not name or 'config' not in data:
            return jsonify({'success': False, 'message': '参数缺失'}), 400
        c.execute("INSERT OR REPLACE INTO export_presets (name, config, created_at) VALUES (?, ?, ?)",
                  (name, json.dumps(data['config'], ensure_ascii=False), time.time()))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/export/presets/<int:pid>', methods=['DELETE'])
def api_export_preset_delete(pid):
    conn = None
    try:
        conn = _db()
        conn.execute("DELETE FROM export_presets WHERE id = ?", (pid,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        if conn is not None:
            conn.close()


_WATERMARK_POSITIONS = {
    'tl': (0.05, 0.05, 'la'), 'tc': (0.5, 0.05, 'ma'), 'tr': (0.95, 0.05, 'ra'),
    'ml': (0.05, 0.5, 'lm'), 'center': (0.5, 0.5, 'mm'), 'mr': (0.95, 0.5, 'rm'),
    'bl': (0.05, 0.95, 'ld'), 'bc': (0.5, 0.95, 'md'), 'br': (0.95, 0.95, 'rd'),
}


def _load_font(size):
    """跨平台挑一个可用的中文字体；全失败用 Pillow 默认位图字体。

    返回 (font, supports_anchor)：位图默认字体不支持 draw.text(anchor=...)，
    调用方需要据此降级，否则文本水印整体失败。"""
    from PIL import ImageFont
    candidates = [
        '/System/Library/Fonts/PingFang.ttc', '/System/Library/Fonts/Hiragino Sans GB.ttc',
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size), True
            except Exception:
                continue
    try:
        return ImageFont.load_default(), False
    except Exception:
        return None, False


def _apply_watermark(img, wm):
    """图片水印：text 或 image（PNG 叠加）。position: tl..br 九宫格 / tile。"""
    from PIL import Image, ImageDraw
    img = img.convert('RGBA')
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    opacity = max(0.05, min(1.0, float(wm.get('opacity', 0.5))))
    if wm.get('image') and os.path.exists(wm['image']):
        try:
            mark = Image.open(wm['image']).convert('RGBA')
            scale = img.width * float(wm.get('scale', 0.2)) / mark.width
            mark = mark.resize((max(1, int(mark.width * scale)), max(1, int(mark.height * scale))))
            pos_key = wm.get('position', 'br')
            fx, fy, _ = _WATERMARK_POSITIONS.get(pos_key, _WATERMARK_POSITIONS['br'])
            x = int((img.width - mark.width) * fx)
            y = int((img.height - mark.height) * fy)
            overlay.alpha_composite(mark, (x, y))
        except Exception:
            pass
    elif wm.get('text'):
        text = str(wm['text'])
        font, anchor_ok = _load_font(int(wm.get('font_size', 28)))
        color = wm.get('color', '#FFFFFF')
        rgba = (255, 255, 255, 255)
        if isinstance(color, str) and color.startswith('#'):
            try:
                rgba = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (int(opacity * 255),)
            except ValueError:
                pass
        if wm.get('tile'):
            step = max(120, int(wm.get('font_size', 28)) * 6)
            for yy in range(0, img.height, step):
                for xx in range(0, img.width, step):
                    draw.text((xx, yy), text, font=font, fill=rgba)
        else:
            pos_key = wm.get('position', 'br')
            fx, fy, anchor = _WATERMARK_POSITIONS.get(pos_key, _WATERMARK_POSITIONS['br'])
            # [V5 复查] 位图默认字体不支持 anchor——降级为左上角定位，避免
            # 文本水印在无中文字体的机器上整体失败
            draw.text((int(img.width * fx), int(img.height * fy)), text, font=font,
                      fill=rgba, anchor=(anchor if anchor_ok else None))
    return Image.alpha_composite(img, overlay).convert('RGB')


@app.route('/api/export/run', methods=['POST'])
def api_export_run():
    """[V4/GAP-18] 按预设导出图片：尺寸/格式/质量/水印。body:
    {paths, dest, max_w?, max_h?, format?, quality?, name_suffix?, watermark?}"""
    data = request.get_json(force=True) or {}
    paths = [str(p) for p in (data.get('paths') or []) if p]
    dest = str(data.get('dest') or '').replace('\\', '/')
    if not paths or not dest or not os.path.isdir(dest):
        return jsonify({'success': False, 'message': '参数缺失或目标目录不存在'}), 400
    _lg = _lock_guard(paths)
    if _lg:
        return _lg
    from PIL import Image, ImageOps
    fmt = str(data.get('format') or 'jpeg').lower()
    fmt_map = {'jpeg': 'JPEG', 'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}
    pil_fmt = fmt_map.get(fmt, 'JPEG')
    ext = '.jpg' if pil_fmt == 'JPEG' else f'.{fmt}'
    quality = max(1, min(100, int(data.get('quality', 90) or 90)))
    max_w = int(data.get('max_w') or 0)
    max_h = int(data.get('max_h') or 0)
    suffix = str(data.get('name_suffix') or '_export')
    # [V5 复查] 后缀参与 os.path.join 拼接，带路径成分可把导出文件写到
    # dest 之外（与批量改名同口径校验）
    if suffix in ('', '.', '..') or '/' in suffix or '\\' in suffix \
            or os.path.basename(suffix) != suffix:
        return jsonify({'success': False, 'message': '非法文件名后缀'}), 400
    watermark = data.get('watermark') or None
    exported, errors = [], []
    for p in paths[:2000]:
        p = unicodedata.normalize('NFC', p.replace('\\', '/'))
        if not os.path.exists(p):
            errors.append(f"找不到: {os.path.basename(p)}")
            continue
        try:
            with Image.open(p) as img:
                img = ImageOps.exif_transpose(img)
                # [V5 复查] 只传一个轴时同样要缩放（thumbnail 空档 = 不限）
                if max_w or max_h:
                    img.thumbnail((max_w or 10 ** 9, max_h or 10 ** 9), Image.LANCZOS)
                if watermark:
                    img = _apply_watermark(img, watermark)
                # [V5 复查] 无水印分支此前不转 RGB——PNG(带 alpha) 直接存 JPEG
                # 逐张抛 "cannot write mode RGBA as JPEG"
                if pil_fmt == 'JPEG' and img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                base = os.path.splitext(os.path.basename(p))[0]
                out_path = os.path.join(dest, f"{base}{suffix}{ext}")
                n = 1
                while os.path.exists(out_path):
                    out_path = os.path.join(dest, f"{base}{suffix} ({n}){ext}")
                    n += 1
                save_kw = {'quality': quality} if pil_fmt in ('JPEG', 'WEBP') else {}
                img.save(out_path, pil_fmt, **save_kw)
            exported.append(out_path)
        except Exception as e:
            errors.append(f"{os.path.basename(p)}: {e}")
    return jsonify({'success': not errors, 'exported': exported, 'errors': errors[:20]})


@app.route('/api/markers', methods=['GET', 'POST', 'DELETE'])
def api_markers():
    """[V4/GAP-19] 视频 in/out 标记。GET ?path= 列表；POST {path,t_in,t_out?,label?}；
    DELETE {id}。片段导出见 /api/markers/export。播放器内打点 UI 待接入 video_pro。"""
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if request.method == 'GET':
            rows = c.execute("SELECT id, file_path, t_in, t_out, label FROM markers WHERE file_path = ? ORDER BY t_in",
                             (_meta_key(request.args.get('path') or ''),)).fetchall()
            return jsonify({'markers': [{'id': r[0], 't_in': r[2], 't_out': r[3], 'label': r[4]} for r in rows]})
        if request.method == 'POST':
            data = request.get_json(force=True) or {}
            path = data.get('path')
            t_in = data.get('t_in')
            if not path or t_in is None:
                return jsonify({'success': False, 'message': '参数缺失'}), 400
            _lg = _lock_guard([path])
            if _lg:
                return _lg
            c.execute("INSERT INTO markers (file_path, t_in, t_out, label, created_at) VALUES (?, ?, ?, ?, ?)",
                      (_meta_key(str(path)), float(t_in),
                       float(data['t_out']) if data.get('t_out') is not None else None,
                       data.get('label'), time.time()))
            conn.commit()
            return jsonify({'success': True, 'id': c.lastrowid})
        data = request.get_json(force=True) or {}
        c.execute("DELETE FROM markers WHERE id = ?", (data.get('id') or 0,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/markers/export', methods=['POST'])
def api_markers_export():
    """导出标记片段：ffmpeg -ss/-to -c copy（可无损时零转码）。body:
    {marker_id | (path, t_in, t_out), dest}"""
    data = request.get_json(force=True) or {}
    path, t_in, t_out = data.get('path'), data.get('t_in'), data.get('t_out')
    conn = None
    try:
        conn = _db()
        if data.get('marker_id'):
            row = conn.execute("SELECT file_path, t_in, t_out FROM markers WHERE id = ?",
                               (data['marker_id'],)).fetchone()
            if not row:
                return jsonify({'success': False, 'message': '标记不存在'}), 404
            path, t_in, t_out = row[0], row[1], row[2]
        if not path or t_in is None:
            return jsonify({'success': False, 'message': '参数缺失'}), 400
    finally:
        if conn is not None:
            conn.close()
    path = str(path).replace('\\', '/')
    _lg = _lock_guard([path])
    if _lg:
        return _lg
    # [V5 复查] 区间校验：t_out <= t_in 会导出空/负时长片段
    try:
        if t_out is not None and float(t_out) <= float(t_in):
            return jsonify({'success': False, 'message': 't_out 必须大于 t_in'}), 400
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '时间参数非法'}), 400
    dest = str(data.get('dest') or os.path.dirname(path))
    if not os.path.exists(path):
        return jsonify({'success': False, 'message': '源文件不存在'}), 404
    if not os.path.isdir(dest):
        return jsonify({'success': False, 'message': '目标目录不存在'}), 400
    ffmpeg = utils.get_tool_path('ffmpeg')
    if not ffmpeg:
        return jsonify({'success': False, 'message': 'ffmpeg 不可用'}), 400
    base = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(dest, f"{base}_clip_{int(float(t_in))}-{int(float(t_out)) if t_out else 'end'}.mp4")
    n = 1
    while os.path.exists(out):
        out = os.path.join(dest, f"{base}_clip_{int(float(t_in))}-{n}.mp4")
        n += 1
    cmd = [ffmpeg, '-y', '-ss', str(float(t_in))]
    if t_out is not None:
        cmd += ['-to', str(float(t_out))]
    cmd += ['-i', path, '-c', 'copy', out]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='ignore', timeout=const.AUDIO_TRIM_TIMEOUT_S * 6)
        if res.returncode != 0:
            return jsonify({'success': False, 'message': (res.stderr or '')[-300:]}), 500
        return jsonify({'success': True, 'out': out})
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'message': '导出超时'}), 500


# ---- [V4/GAP-22] 文件夹软锁（UI 级防护，非加密；文档如实说明） ----
import hashlib as _hashlib
import hmac as _hmac  # compare_digest 在 hmac；pbkdf2 在 hashlib


def _pwd_hash(pwd: str, salt: bytes) -> str:
    return _hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8', 'ignore'), salt, 60000).hex()


_unlocked_dirs = set()  # 会话级已解锁目录（内存，不落盘）


def _locked_by(path: str):
    """path（目录或文件）是否被某个软锁覆盖（前缀匹配）。返回锁行或 None。"""
    p = _meta_key(path).rstrip('/')
    conn = None
    try:
        conn = _db()
        for lk, h, hint in conn.execute("SELECT path, pwd_hash, hint FROM folder_locks").fetchall():
            if p == lk or p.startswith(lk + '/'):
                if p not in _unlocked_dirs and lk not in _unlocked_dirs:
                    return {'path': lk, 'hint': hint}
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return None


def _active_lock_dirs():
    """[V5 复查] 仍处于锁定状态的目录前缀列表（会话内已解锁的剔除）。

    供搜索/智能文件夹/地图等批量路径过滤一次性取齐，避免逐行开连接。"""
    try:
        conn = _db()
        try:
            rows = [r[0] for r in conn.execute("SELECT path FROM folder_locks").fetchall()]
        finally:
            conn.close()
    except Exception:
        return []
    return [lk for lk in rows if lk not in _unlocked_dirs]


def _path_in_locks(path, locks):
    """path 是否落在任一锁定前缀内；返回命中的锁路径或 None。"""
    if not locks or not path:
        return None
    p = _meta_key(path).rstrip('/')
    for lk in locks:
        if p == lk or p.startswith(lk + '/'):
            return lk
    return None


def _lock_guard(paths):
    """[V5 复查] 写操作软锁守卫：任一路径（含目标目录）落在未解锁的锁定
    目录内 → 423；否则返回 None 放行。锁此前只挡 /api/list、缩略图与
    文件内容三个读端点，rename/move/delete 等写 API 全部绕过。"""
    locks = _active_lock_dirs()
    if not locks:
        return None
    for p in paths:
        lk = _path_in_locks(str(p) if p else '', locks)
        if lk:
            return jsonify({'success': False, 'locked': True, 'lock_path': lk,
                            'message': '目录已锁定，请先解锁'}), 423
    return None


@app.route('/api/locks', methods=['POST'])
def api_lock_set():
    """设置软锁 {path(目录), password, hint?}"""
    data = request.get_json(force=True) or {}
    path = _meta_key(str(data.get('path') or '').replace('\\', '/')).rstrip('/')
    pwd = str(data.get('password') or '')
    if not path or not os.path.isdir(path) or not pwd:
        return jsonify({'success': False, 'message': '参数缺失或目录不存在'}), 400
    salt = os.urandom(16)
    conn = None
    try:
        conn = _db()
        conn.execute("INSERT OR REPLACE INTO folder_locks (path, pwd_hash, hint) VALUES (?, ?, ?)",
                     (path, salt.hex() + '$' + _pwd_hash(pwd, salt), data.get('hint')))
        conn.commit()
        # [V5 复查] 重锁（含换密码）后旧的本会话解锁凭据必须作废：此前
        # _unlocked_dirs 残留，换密码重锁当会话内仍放行
        _unlocked_dirs.discard(path)
        return jsonify({'success': True})
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/locks/unlock', methods=['POST'])
def api_lock_unlock():
    data = request.get_json(force=True) or {}
    path = _meta_key(str(data.get('path') or '').replace('\\', '/')).rstrip('/')
    pwd = str(data.get('password') or '')
    conn = None
    try:
        conn = _db()
        row = conn.execute("SELECT pwd_hash FROM folder_locks WHERE path = ?", (path,)).fetchone()
        if not row:
            return jsonify({'success': False, 'message': '该目录未上锁'}), 404
        try:
            salt_hex, stored = row[0].split('$', 1)
            ok = _hmac.compare_digest(_pwd_hash(pwd, bytes.fromhex(salt_hex)), stored)
        except ValueError:
            ok = False
        if ok:
            _unlocked_dirs.add(path)
        return jsonify({'success': bool(ok)})
    finally:
        if conn is not None:
            conn.close()


@app.route('/api/locks', methods=['DELETE'])
def api_lock_remove():
    """移除软锁（需密码）"""
    data = request.get_json(force=True) or {}
    path = _meta_key(str(data.get('path') or '').replace('\\', '/')).rstrip('/')
    conn = None
    try:
        conn = _db()
        row = conn.execute("SELECT pwd_hash FROM folder_locks WHERE path = ?", (path,)).fetchone()
        if not row:
            return jsonify({'success': False, 'message': '该目录未上锁'}), 404
        try:
            salt_hex, stored = row[0].split('$', 1)
            ok = _hmac.compare_digest(
                _pwd_hash(str(data.get('password') or ''), bytes.fromhex(salt_hex)), stored)
        except ValueError:
            ok = False
        if ok:
            conn.execute("DELETE FROM folder_locks WHERE path = ?", (path,))
            conn.commit()
            _unlocked_dirs.discard(path)
        return jsonify({'success': bool(ok)})
    finally:
        if conn is not None:
            conn.close()


# ============================================================================
# [U5/GAP-02] 全库搜索：FTS5(trigram) 中文子串匹配；FTS 不可用降级 LIKE
# ============================================================================

def _search_term_rows(c, term: str) -> list:
    """单词条 → 命中路径集合：FTS(≥3) 优先，未命中/不可用回退 LIKE。"""
    if len(term) >= 3:
        try:
            c.execute("SELECT path FROM file_meta_fts WHERE file_meta_fts MATCH ?",
                      (f'"{term.replace(chr(34), chr(34) * 2)}"',))
            rows = [r[0] for r in c.fetchall()]
            if rows:
                return rows
        except sqlite3.OperationalError:
            pass
    like = f"%{_like_escape(term)}%"
    c.execute("""SELECT path FROM file_meta
                 WHERE path LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR comments LIKE ? ESCAPE '\\'""",
              (like, like, like))
    return [r[0] for r in c.fetchall()]


@app.route('/api/search')
def api_search():
    """跨目录全库搜索。

    参数：q（名称/标签/备注子串）、tag（精确标签名）、type、min_rating、
    color、flag；[V1/GAP-02] 技术元数据过滤 min_duration/max_duration（秒）、
    date_from/date_to（YYYY-MM-DD，按拍摄日期）、camera（子串）、has_gps=1。
    返回与 /api/list 相同结构的文件列表。"""
    q = (request.args.get('q') or '').strip()
    tag = (request.args.get('tag') or '').strip()
    ftype = request.args.get('type') or ''
    try:
        min_rating = int(request.args.get('min_rating') or 0)
    except ValueError:
        min_rating = 0
    try:
        color = int(request.args.get('color') or 0)
    except ValueError:
        color = 0
    flag = request.args.get('flag') or ''

    paths = []
    tech_map = {}
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        if q:
            # 多词（空格分隔）取交集；单词：FTS trigram（≥3 字符）未命中或
            # 不可用时回退 LIKE。注意 trigram 是连续子串语义，中文逗号分隔
            # 的 tags 不产生跨词 gram——"落日 航拍"（带空格）可命中两标签。
            terms = [t for t in q.split() if t]
            result_sets = []
            for term in terms:
                result_sets.append(set(_search_term_rows(c, term)))
            if result_sets:
                paths = list(set.intersection(*result_sets))
        elif tag:
            # [V2/GAP-06] 含全部子孙标签
            tag_ids = _tag_and_descendant_ids(tag)
            if tag_ids:
                ph = ','.join('?' * len(tag_ids))
                c.execute(f"""SELECT DISTINCT ft.file_path FROM file_tags ft
                              WHERE ft.tag_id IN ({ph})""", tag_ids)
            else:
                c.execute("SELECT NULL WHERE 0")
            paths = [r[0] for r in c.fetchall()]
        else:
            c.execute("SELECT path FROM file_meta")
            paths = [r[0] for r in c.fetchall()]

        # [V1/GAP-02] 技术元数据过滤：SQL 集合求交（列由索引器回填，
        # 旧行 NULL 不过条件）。与智能文件夹规则同语义。
        tech_conds, tech_params = [], []

        def _fnum(key):
            try:
                return float(request.args.get(key))
            except (TypeError, ValueError):
                return None
        v = _fnum('min_duration')
        if v:
            tech_conds.append("duration IS NOT NULL AND duration >= ?"); tech_params.append(v)
        v = _fnum('max_duration')
        if v:
            tech_conds.append("duration IS NOT NULL AND duration <= ?"); tech_params.append(v)
        df = (request.args.get('date_from') or '').strip()
        if df:
            ts = _date_str_to_epoch(df)
            if ts:
                tech_conds.append("date_taken IS NOT NULL AND date_taken >= ?"); tech_params.append(ts)
        dt_to = (request.args.get('date_to') or '').strip()
        if dt_to:
            ts = _date_str_to_epoch(dt_to)
            if ts:
                tech_conds.append("date_taken IS NOT NULL AND date_taken < ?"); tech_params.append(ts + 86400)
        cam = (request.args.get('camera') or '').strip()
        if cam:
            tech_conds.append("camera LIKE ? ESCAPE '\\'"); tech_params.append('%' + _like_escape(cam) + '%')
        if request.args.get('has_gps') in ('1', 'true'):
            tech_conds.append("gps_lat IS NOT NULL AND gps_lon IS NOT NULL")
        # [V3/GAP-09] 自动主色桶（0-11 色相 / -1 灰阶）
        if request.args.get('dom_bucket') not in (None, ''):
            try:
                tech_conds.append("dom_color = ?"); tech_params.append(int(request.args.get('dom_bucket')))
            except (TypeError, ValueError):
                pass
        if tech_conds:
            c.execute(f"SELECT path FROM file_meta WHERE {' AND '.join(tech_conds)}", tech_params)
            tech_set = {r[0] for r in c.fetchall()}
            paths = [p for p in paths if p in tech_set]
        # [V5 复查] 双重排除：失联行（ghost 不再吃掉 SCAN_CAP 截断预算）与
        # 软锁目录（锁定的私有内容不进搜索结果）
        ghost = {r[0] for r in c.execute(
            "SELECT path FROM file_meta WHERE missing = 1").fetchall()}
        locks = _active_lock_dirs()
        if ghost or locks:
            paths = [p for p in paths
                     if p not in ghost and not _path_in_locks(p, locks)]
        # 时长/拍摄日期随结果下发（卡片角标/筛选）：全表一次拉取（10 万行约几十 ms）
        tech_map = {r[0]: (r[1], r[2]) for r in
                    c.execute("SELECT path, duration, date_taken FROM file_meta").fetchall()}
    except Exception as e:
        logger.error(f"/api/search 查询失败: {e}")
        return jsonify({'files': [], 'error': str(e)})
    finally:
        if conn is not None:
            conn.close()

    files = []
    # [U5 收尾] 截断前排序：set 交集转 list 的顺序受进程哈希随机化影响，
    # 直接 [:2000] 每次会话截到不同子集（同一搜索两次结果不同）
    paths.sort()
    # [U10] 截断移到类型/评分/颜色/旗标过滤之后：此前先 [:2000] 再过滤，
    # 大库上"q=IMG + color=红"可能截到 2000 条无色文件再滤成 0 条——
    # 组合筛选结果静默不完整。扫描侧另设步数护栏防全库失控。
    SCAN_CAP = 50000
    scanned = 0
    scan_cache = {}  # [U10] /api/list 同款请求级缓存：避免逐文件 scandir O(N²)
    for path in paths:
        if scanned >= SCAN_CAP:
            break
        scanned += 1
        path = str(path).replace('\\', '/')
        if not os.path.exists(path):
            continue  # 搜索不负责孤儿清理（智能文件夹查询负责）
        try:
            stat = os.stat(path)
            name = unicodedata.normalize('NFC', os.path.basename(path))
            ext = os.path.splitext(name)[1].lower()
            if ftype and ftype != 'all' and classify_ext(ext) != ftype:
                continue
            thumb_path = thumb_engine.get_thumb_path(path, scan_cache=scan_cache)
            t_ver = int(os.stat(thumb_path).st_mtime) if os.path.exists(thumb_path) else 0
            dur, dts = tech_map.get(path, (None, None))
            files.append({
                'name': name, 'path': path, 'type': classify_ext(ext),
                'size': utils.get_size_str(stat.st_size),
                'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d'),
                'size_bytes': stat.st_size, 'mtime_ts': stat.st_mtime,
                't_ver': t_ver, 'res': None,
                'duration': dur, 'date_taken_ts': dts,
                'date_taken': (datetime.fromtimestamp(dts, tz=timezone.utc).strftime('%Y-%m-%d') if dts else None),
            })
        except OSError:
            continue

    # 元数据（rating/tags/color/flag）批量补齐
    if files:
        # [U9] 连接 finally 化：异常路径不再泄漏 sqlite 连接
        conn = None
        try:
            conn = _db()
            c = conn.cursor()
            all_paths = [f['path'] for f in files]
            meta_map = {}
            for i in range(0, len(all_paths), const.THUMB_BATCH_CHUNK):
                chunk = all_paths[i:i + const.THUMB_BATCH_CHUNK]
                placeholders = ','.join('?' * len(chunk))
                c.execute(f"""SELECT path, tags, comments, rating, cached_info, color, flag
                              FROM file_meta WHERE path IN ({placeholders})""",
                          [_meta_key(p) for p in chunk])
                # [U5 收尾] 跨块累积：赋值式覆盖会让 >1 分块时只有最后一块
                # 的元数据生效（评分/颜色过滤凭空丢一半结果）
                meta_map.update({r[0]: r for r in c.fetchall()})
            for f in files:
                r = meta_map.get(_meta_key(f['path']))
                if r:
                    f.update({'tags': r[1], 'comments': r[2], 'rating': r[3],
                              'res': r[4], 'color': r[5] or 0, 'flag': r[6] or 0})
                else:
                    f.update({'tags': '', 'comments': '', 'rating': 0, 'res': None,
                              'color': 0, 'flag': 0})
        except Exception as e:
            logger.error(f"/api/search 元数据补齐失败: {e}")
        finally:
            if conn is not None:
                conn.close()

    # 评分/颜色/旗标过滤（取自 meta 数据）
    if min_rating > 0:
        files = [f for f in files if (f.get('rating') or 0) >= min_rating]
    if color:
        files = [f for f in files if (f.get('color') or 0) == color]
    if flag == 'pick':
        files = [f for f in files if (f.get('flag') or 0) == 1]
    elif flag == 'noreject':
        files = [f for f in files if (f.get('flag') or 0) != 2]

    total = len(files)
    files = files[:2000]  # 上限保护：万级命中不致响应失控（在过滤后截断）
    files.sort(key=lambda x: x['name'].lower())
    return jsonify({'files': files, 'total': total})


# ============================================================================
# [U7] 视图与工作流批：胶片条 / 重复查找 / 批量旋转 / 批量移动
# ============================================================================

@app.route('/api/thumb/strip')
def api_thumb_strip():
    """[U7/GAP-10] 视频胶片条（6 帧横排 tile）。

    首次请求同步生成（ffmpeg tile，超时受 FFMPEG_TIMEOUT_S 约束），
    缓存于源目录 .cache/<名>_strip.jpg；之后直读，悬停 scrub 帧切换
    为纯前端样式更新（<100ms）。"""
    path = request.args.get('path')
    if path:
        path = unicodedata.normalize('NFC', path.replace('\\', '/'))
    if not path or not os.path.isfile(path):
        return "", 404
    if os.path.splitext(path)[1].lower() not in EXT_VIDEO:
        return "", 404
    target = thumb_engine.get_filmstrip_path(path)
    if not os.path.exists(target):
        # [U10] strip 专属失败键：此前与普通缩略图共用 path 键——普通缩略图
        # 失败会连坐拦截 strip；而 strip 自身失败从不入缓存，每次悬停都
        # 同步重跑 ffprobe+ffmpeg。generate_filmstrip 现按 ':strip' 键登记
        if check_failure_cache(path + ':strip'):
            return "", 204
        if not thumb_engine.generate_filmstrip(path, target):
            return "", 204
    resp = make_response(send_file(target))
    resp.headers['Cache-Control'] = f'max-age={const.THUMB_CACHE_HTTP_MAX_AGE_S}'
    return resp


@app.route('/api/duplicates/scan')
def api_duplicates_scan():
    """[U7/GAP-09] 重复文件查找：先按体积分组，仅对"同体积 ≥2 个"的组做
    MD5 全量哈希（万级图库绝大多数体积唯一，避免整库读盘）。

    返回 {groups: [{size, files: [...]}], scanned, hashed, elapsed, truncated}。"""
    import hashlib
    root = (request.args.get('path') or '').strip().replace('\\', '/')
    if not root or not os.path.isdir(root):
        return jsonify({'error': '目录不存在'}), 400
    t0 = time.perf_counter()
    # [U9] 上限保护：同步端点在 waitress 线程内全量读盘，此前无任何上限，
    # 大库/含大视频的库会长时间占满请求线程
    MAX_SCAN = 50000        # 至多枚举 5 万个文件
    MAX_HASH_BYTES = 2 * 1024 ** 3   # 至多读盘 2GB 做 MD5
    by_size = {}
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for fn in filenames:
            if fn.startswith('.'):
                continue
            if scanned >= MAX_SCAN:
                truncated = True
                break
            fp = os.path.join(dirpath, fn).replace('\\', '/')
            try:
                st = os.stat(fp)
            except OSError:
                continue
            scanned += 1
            by_size.setdefault(st.st_size, []).append(fp)
        if truncated:
            break

    def _digest(fp):
        h = hashlib.md5()
        with open(fp, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()

    groups = []
    hashed = 0
    hash_budget = MAX_HASH_BYTES
    for size, fps in sorted(by_size.items(), key=lambda kv: -kv[0]):
        if size == 0 or len(fps) < 2:
            continue
        if hash_budget <= 0:
            truncated = True
            break
        by_digest = {}
        for fp in fps:
            # [U10] 预算前置检查：单文件可达数 GB 的视频组，读完全量
            # 才扣减意味着超预算最多再多读一个整文件
            if hash_budget < size:
                truncated = True
                break
            try:
                by_digest.setdefault(_digest(fp), []).append(fp)
                hashed += 1
                hash_budget -= size
            except OSError:
                continue
        for dup in by_digest.values():
            if len(dup) >= 2:
                groups.append({'size': size, 'files': sorted(dup)})
    groups.sort(key=lambda g: -g['size'])
    return jsonify({'groups': groups, 'scanned': scanned,
                    'hashed': hashed, 'elapsed': round(time.perf_counter() - t0, 2),
                    'truncated': truncated})


@app.route('/api/batch/rotate', methods=['POST'])
def api_batch_rotate():
    """[U7/GAP-14] 批量旋转图片（JPEG 以 quality='keep' 保留原始量化表，
    逼近无损；其余格式重编码）。body: {paths, degrees: 90|180|270}。"""
    data = request.get_json(force=True) or {}
    paths = data.get('paths') or []
    try:
        degrees = int(data.get('degrees') or 0)
    except (TypeError, ValueError):
        degrees = 0
    if degrees not in (90, 180, 270):
        return jsonify({'success': False, 'message': 'degrees 仅支持 90/180/270'}), 400
    _lg = _lock_guard([str(p) for p in paths])
    if _lg:
        return _lg
    from PIL import Image, ImageOps
    rotated, errors = [], []
    for p in paths:
        p = str(p).replace('\\', '/')
        try:
            with Image.open(p) as im:
                orig_format = im.format or 'PNG'
                # JPEG 用原量化表 + 原采样逼近无损；旋转产生新 Image，
                # 不能用 quality='keep'（其只对同对象重存生效）
                qtables = im.quantization if (orig_format == 'JPEG' and hasattr(im, 'quantization')) else None
                # [U9] 先按 EXIF Orientation 摆正再叠加用户旋转：带 Orientation
                # 的照片（iPhone 直出等）直接转像素会在遵循 EXIF 的查看器里
                # 双重旋转/看似没转
                try:
                    oriented = ImageOps.exif_transpose(im)
                except Exception:
                    oriented = im.copy()
                exif_obj = None
                try:
                    exif_obj = oriented.getexif()
                    if exif_obj:
                        exif_obj[0x0112] = 1  # Orientation=1：旋转已烘焙进像素
                        exif = exif_obj.tobytes()
                    else:
                        exif = None
                except Exception:
                    exif = oriented.info.get('exif')
                out = oriented.rotate(-degrees, expand=True)
                tmp = p + '.rotating.tmp'
                save_kwargs = {'format': orig_format}
                if orig_format == 'JPEG':
                    # 旋转产生的新 Image 不带 JPEG 状态，subsampling/quality
                    # 的 'keep' 均不可用；显式量化表即"保留原表"语义
                    if qtables:
                        save_kwargs['qtables'] = qtables
                    save_kwargs['quality'] = 95
                    if exif:
                        save_kwargs['exif'] = exif
                elif exif:
                    save_kwargs['exif'] = exif
                out.save(tmp, **save_kwargs)
            os.replace(tmp, p)
            rotated.append(p)
            # [U9] 缩略图与目录缓存一并失效：此前旧方向缩略图滞留（无 .v
            # sidecar 时 is_stale 恒判有效，永不重生成）
            try:
                for size in ('small', 'large', 'preview'):
                    tp = thumb_engine.get_thumb_path(p, size_type=size)
                    if tp and os.path.exists(tp):
                        os.remove(tp)
                        # [U10] .v sidecar 一并清（孤儿 sidecar 会让陈旧
                        # 检测拿到旧 mtime、新图 is_stale 误判）
                        if os.path.exists(tp + '.v'):
                            os.remove(tp + '.v')
                strip_fp = thumb_engine.get_filmstrip_path(p)
                if os.path.exists(strip_fp):
                    os.remove(strip_fp)
            except Exception as e:
                logger.warning(f"[U10] 旋转后缩略图清理失败 {p}: {e}")
            invalidate_cache(os.path.dirname(p))
        except Exception as e:
            errors.append(f"{os.path.basename(p)}: {e}")
            try:
                if os.path.exists(p + '.rotating.tmp'):
                    os.remove(p + '.rotating.tmp')
            except OSError:
                pass
    if rotated:
        # [U9] 连接 finally 化
        conn = None
        try:
            conn = _db()
            conn.executemany(
                "UPDATE file_meta SET cached_info = NULL WHERE path = ?",
                [(_meta_key(p),) for p in rotated])
            conn.commit()
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
    return jsonify({'success': not errors, 'rotated': rotated, 'errors': errors})


@app.route('/api/batch/move', methods=['POST'])
def api_batch_move():
    """[U7/GAP-14] 批量移动到目标目录（同名自动 "(1)" 后缀，元数据与
    缩略图随迁）。body: {paths, dest}。"""
    data = request.get_json(force=True) or {}
    paths = data.get('paths') or []
    dest = str(data.get('dest') or '').replace('\\', '/')
    if not paths:
        return jsonify({'success': False, 'message': '缺少 paths'}), 400
    if not dest or not os.path.isdir(dest):
        return jsonify({'success': False, 'message': '目标目录不存在'}), 400
    _lg = _lock_guard(list(paths) + [dest])
    if _lg:
        return _lg
    moved, errors = [], []
    # [U9] 单连接 + finally：此前每文件一开一关且无 finally，execute 抛错
    # 即泄漏连接
    conn = None
    try:
        conn = _db()
    except Exception as e:
        logger.warning(f"[U7] 批量移动：DB 连接失败（元数据不迁移）: {e}")
    for p in paths:
        p = str(p).replace('\\', '/')
        if not os.path.isfile(p):
            errors.append(f"找不到: {os.path.basename(p)}")
            continue
        name = os.path.basename(p)
        base, ext = os.path.splitext(name)
        target = os.path.join(dest, name)
        n = 1
        while os.path.exists(target):
            target = os.path.join(dest, f"{base} ({n}){ext}")
            n += 1
        try:
            shutil.move(p, target)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        # 元数据/缩略图随迁（对齐 rename 语义）
        if conn is not None:
            try:
                conn.execute("UPDATE file_meta SET path = ? WHERE path = ?",
                             (_meta_key(target), _meta_key(p)))
                # [U9] 结构化标签同步平移
                conn.execute("UPDATE file_tags SET file_path = ? WHERE file_path = ?",
                             (_meta_key(target), _meta_key(p)))
                # [V2/GAP-05] 集合成员随迁
                conn.execute("UPDATE collection_items SET file_path = ? WHERE file_path = ?",
                             (_meta_key(target), _meta_key(p)))
            except Exception as e:
                logger.warning(f"[U7] 批量移动元数据迁移失败 {p}: {e}")
        for size in ('small', 'large', 'preview'):
            try:
                old_thumb = thumb_engine.get_thumb_path(p, size_type=size)
                new_thumb = thumb_engine.get_thumb_path(target, size_type=size)
                if os.path.exists(old_thumb) and not os.path.exists(new_thumb):
                    os.rename(old_thumb, new_thumb)
            except OSError:
                pass
        moved.append({'from': p, 'to': target})
    if conn is not None:
        try:
            conn.commit()
        except Exception as e:
            logger.warning(f"[U7] 批量移动元数据提交失败: {e}")
        finally:
            conn.close()
    # [U9] 全部源目录 + 目标目录缓存失效（此前只失效第一个源目录）
    try:
        invalidate_cache(dest)
        for d in {os.path.dirname(m['from']) for m in moved}:
            invalidate_cache(d)
    except Exception:
        pass
    return jsonify({'success': not errors, 'moved': moved, 'errors': errors})
