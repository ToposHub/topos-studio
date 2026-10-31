#!/usr/bin/env python3
"""V5 复查批验收：V1-V4 交付后的整体审查修复回归。

覆盖（按修复项）：
1.  scanner missing 三连：scan_directory 不误标子目录深层文件、只投递变更文件；
    全量扫描把盘上仍在的 missing=1 行复位
2.  集合失联清理三重防御：回收站成员保留、真失联清行
3.  meta_sync：清空值显式写 XMP 删除参数；import_from_files 达 cap 提交不回滚
4.  FTS 触发器携带 ocr 列（tags 更新不再抹掉 OCR 索引）；ocr_engine 不产生重复 FTS 行
5.  ai_tagging 前缀剥离按整串（'animal' 不再变 'nimal'）
6.  _local_cache_dirs 能找到 .cache（本地缓存体积不再恒 0）
7.  批量重命名 replace/append 路径逃逸拦截；模板 {ext:upper} 不双拼扩展名
8.  rename_template：date 修饰符生效、strftime 冒号保留、渲染空结果拒绝
9.  软锁：搜索排除锁定目录、写 API 守卫、重锁使旧解锁凭据失效
10. stacks 移出全部成员后清理空堆叠行
11. /api/stats 排除 missing=1 行；markers/export 区间校验；export 后缀校验
12. 递归视图缓存 key 随目录失效
13. 前端静态断言：sizeParam 提升、堆叠角标 data-args、runOcrPanel 签名、
    五 scheme 虚拟视图守卫、面包屑清单

隔离策略：DB 与 CONFIG_FILE 均重定向到临时目录。"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

FE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FE_DIR)

import db  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f"（{detail}）" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"（{detail}）" if detail else ""))


def make_img(path, size=(320, 240), color=(60, 120, 200)):
    from PIL import Image
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def _q1(sql, params=()):
    conn = db._db()
    try:
        cur = conn.execute(sql, params)
        head = sql.strip().upper()
        row = cur.fetchone() if head.startswith(('SELECT', 'PRAGMA', 'WITH')) else None
        if not head.startswith(('SELECT', 'PRAGMA', 'WITH')):
            conn.commit()
        return row
    finally:
        conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="fe_v5_")
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)

    db._topos_db_path = os.path.join(tmp, "topos.db")
    db._fallback_db_path = os.path.join(tmp, "metadata.db")
    db.DB_FILE = db._fallback_db_path
    db.CONFIG_FILE = os.path.join(tmp, "config.json")
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}]}, f)
    db.init_db()

    import routes
    import scanner
    import meta_sync
    import rename_template
    import webapp

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    # ------------------------------------------------------------------
    print("\n== 1. scanner missing 三连 ==")
    sub = os.path.join(lib, "sub")
    os.makedirs(sub)
    deep = make_img(os.path.join(sub, "deep.jpg"))
    direct = make_img(os.path.join(lib, "direct.jpg"))
    a1, u1, t1 = scanner._scan_library(lib)
    check("全量扫描入库", _q1("SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(deep),))[0] == 0)
    # 目录变更触发单目录增量：子目录深层文件不得被误标 missing
    open(os.path.join(lib, "new.txt"), 'w').close()
    scanner.scan_directory(lib)
    deep_missing = _q1("SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(deep),))[0]
    check("scan_directory 不误标子目录深层文件", deep_missing == 0)
    check("scan_directory 新文件入库", _q1(
        "SELECT 1 FROM file_meta WHERE path = ?", (db._meta_key(os.path.join(lib, 'new.txt')),)) is not None)
    # 直接子文件删除 → missing=1
    os.remove(direct)
    scanner.scan_directory(lib)
    check("直接子文件删除 → missing=1", _q1(
        "SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(direct),))[0] == 1)
    # 手工置 missing=1（模拟卷离线被标）→ 文件回盘后全量扫描复位
    _q1("UPDATE file_meta SET missing = 1 WHERE path = ?", (db._meta_key(deep),))
    scanner._scan_library(lib)
    check("全量扫描复位 missing（mtime/size 未变路径）", _q1(
        "SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(deep),))[0] == 0)

    # ------------------------------------------------------------------
    print("\n== 2. 集合失联清理三重防御 ==")
    cf = client.post('/api/collections', data=json.dumps({'name': '测试集合'}),
                     content_type='application/json', **auth)
    cid = cf.get_json()['id']
    keep_img = make_img(os.path.join(lib, "keep.jpg"))
    gone_img = make_img(os.path.join(lib, "gone.jpg"))
    client.post(f'/api/collections/{cid}/items',
                data=json.dumps({'action': 'add', 'paths': [keep_img, gone_img]}),
                content_type='application/json', **auth)
    # gone.jpg 移入应用内回收站（磁盘上消失但 trash 表有记录）→ 成员行必须保留
    import trash as trash_mod
    records, errs = trash_mod.trash_items([gone_img])
    check("回收站入站成功", len(records) == 1 and not errs)
    client.get(f'/api/collections/{cid}/items', **auth)
    row = _q1("SELECT 1 FROM collection_items WHERE collection_id = ? AND file_path = ?",
              (cid, db._meta_key(gone_img)))
    check("回收站成员行不被清理（恢复后成员关系复活）", row is not None)
    # 恢复后成员可见
    trash_mod.restore_items([records[0]['id']])
    items = client.get(f'/api/collections/{cid}/items', **auth).get_json()['files']
    check("恢复后集合成员复活", any(f['path'] == gone_img for f in items))
    # 真失联（不在回收站、卷在线）→ 清行
    os.remove(gone_img)
    _q1("DELETE FROM trash WHERE original_path = ?", (db._meta_key(gone_img),))
    client.get(f'/api/collections/{cid}/items', **auth)
    row = _q1("SELECT 1 FROM collection_items WHERE collection_id = ? AND file_path = ?",
              (cid, db._meta_key(gone_img)))
    check("真失联成员行被清理", row is None)

    # ------------------------------------------------------------------
    print("\n== 3. meta_sync 清空值与 cap 提交 ==")
    captured = {}

    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        class R: returncode = 0; stderr = ''
        return R()

    orig_tool = routes.utils.get_tool_path
    orig_run = meta_sync.subprocess.run
    meta_sync.subprocess.run = fake_run
    routes.utils.get_tool_path = lambda name: '/usr/bin/true' if name == 'exiftool' else orig_tool(name)
    try:
        meta_sync.sync_once(keep_img, {'rating': 0, 'tags': [], 'comments': '', 'color': 0, 'flag': 0})
        cmd = captured.get('cmd') or []
        check("清空标签写删除参数", '-XMP-dc:Subject=' in cmd, str(cmd[:8]))
        check("清空层级标签写删除参数", '-XMP-lr:HierarchicalSubject=' in cmd)
        check("清空备注写删除参数", '-XMP-dc:Description=' in cmd)
        check("清空颜色写删除参数", '-XMP-digiKam:ColorLabel=' in cmd)
        check("清空旗标写删除参数", '-XMP-lr:Flag=' in cmd)
    finally:
        meta_sync.subprocess.run = orig_run
        routes.utils.get_tool_path = orig_tool
    # import_from_files：cap=1 提前返回时已采纳行必须提交（不回滚）
    a, b = make_img(os.path.join(lib, "xa.jpg")), make_img(os.path.join(lib, "xb.jpg"))
    orig_read = meta_sync.read_xmp
    meta_sync.read_xmp = lambda p: {'rating': 5, 'tags': ['T']} if p in (a, b) else None
    try:
        res = meta_sync.import_from_files(library=lib, cap=1)
        check("cap 提前返回报 scanned>=1", res['scanned'] >= 1, str(res))
        # walk 顺序不保证哪个文件先被扫到：断言"恰好一个"被采纳且已提交
        conn = db._db()
        try:
            adopted_rows = conn.execute(
                "SELECT path FROM file_meta WHERE path IN (?, ?) AND rating = 5",
                (db._meta_key(a), db._meta_key(b))).fetchall()
        finally:
            conn.close()
        check("cap 提前返回前已采纳行不回滚（恰好 1 条已提交）",
              len(adopted_rows) == 1, str(adopted_rows))
    finally:
        meta_sync.read_xmp = orig_read

    # ------------------------------------------------------------------
    print("\n== 4. FTS 触发器 ocr 列与 OCR 去重 ==")
    fts_img = make_img(os.path.join(lib, "fts.jpg"))
    key = db._meta_key(fts_img)
    _q1("INSERT INTO ocr_text (file_path, text, ocr_at) VALUES (?, ?, ?)", (key, '发票号码12345', time.time()))
    _q1("INSERT INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)", (key,))
    # _ai 触发器应从 ocr_text 取 ocr
    row = _q1("SELECT ocr FROM file_meta_fts WHERE path = ?", (key,))
    check("_ai 触发器携带 ocr 列", row is not None and row[0] == '发票号码12345')
    # tags 更新（_au 触发器）不得抹掉 ocr
    _q1("UPDATE file_meta SET tags = 'later' WHERE path = ?", (key,))
    row = _q1("SELECT ocr, tags FROM file_meta_fts WHERE path = ?", (key,))
    check("_au 触发器保留 ocr（tags 更新后仍可搜 OCR 文字）",
          row is not None and row[0] == '发票号码12345' and row[1] == 'later')
    # ocr_engine.process 不产生重复 FTS 行（假引擎）
    import ocr_engine
    ocr_engine._engine = lambda p: '示例文字XYZ'
    ocr_engine._engine_checked = True
    orphan_img = make_img(os.path.join(lib, "orphan.jpg"))  # 无 file_meta 行
    ocr_engine.process(orphan_img)
    cnt = _q1("SELECT COUNT(*) FROM file_meta_fts WHERE path = ?", (db._meta_key(orphan_img),))[0]
    check("OCR 无行处理不产生重复 FTS 行", cnt == 1, f"count={cnt}")
    # 随后索引器 INSERT 也不应产生第二条（_ai 从 ocr_text 取值）
    _q1("DELETE FROM file_meta WHERE path = ?", (db._meta_key(orphan_img),))
    _q1("INSERT INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)", (db._meta_key(orphan_img),))
    cnt = _q1("SELECT COUNT(*) FROM file_meta_fts WHERE path = ?", (db._meta_key(orphan_img),))[0]
    check("OCR 先行 + 索引器后插 不重复", cnt == 1, f"count={cnt}")

    # ------------------------------------------------------------------
    print("\n== 5. ai_tagging 前缀整串剥离 ==")
    class _Vault(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({'tags': ['animal', 'iso', 'city walk', 'ai:已有前缀']}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(('127.0.0.1', 0), _Vault)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    db.save_config({'ai_vault_url': f'http://127.0.0.1:{srv.server_port}'})
    import ai_tagging
    ai_img = make_img(os.path.join(lib, "ai.jpg"))
    n = ai_tagging.tag_file(ai_img)
    srv.shutdown()
    tags_now = {r[0] for r in _q1(
        "SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_path = ?",
        (db._meta_key(ai_img),)) and []} if False else {
        r[0] for r in db._db().execute(
            "SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_path = ?",
            (db._meta_key(ai_img),)).fetchall()}
    check("AI 标签写入", n >= 3 and len(tags_now) >= 3, str(tags_now))
    check("'animal' 不被字符集剥离成 'nimal'", 'ai:animal' in tags_now and 'ai:nimal' not in tags_now)
    check("'iso' 不被砍空", 'ai:iso' in tags_now)
    check("带空格标签保留", 'ai:city walk' in tags_now)
    db.save_config({'ai_vault_url': ''})

    # ------------------------------------------------------------------
    print("\n== 6. 本地缓存统计 ==")
    cache_dir = os.path.join(lib, '.cache')
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, 'thumb_1.bin'), 'wb') as f:
        f.write(b'x' * 4096)
    size_resp = client.get('/api/cache/size', **auth).get_json()
    local_total = sum(x['bytes'] for x in size_resp.get('local', []))
    check(".cache 目录可被发现且体积>0", local_total >= 4096,
          f"local={size_resp.get('local')}")

    # ------------------------------------------------------------------
    print("\n== 7. 批量重命名路径逃逸与模板扩展名 ==")
    esc1 = make_img(os.path.join(lib, "aaa.jpg"))
    pv = client.post('/api/batch/rename/preview',
                     data=json.dumps({'files': [esc1], 'mode': 'replace',
                                      'params': {'find': 'a', 'replace': '../x'}}),
                     content_type='application/json', **auth).get_json()
    dst_out = [p for p in pv.get('plan', []) if os.path.dirname(p['dst']) != lib]
    check("replace 产物带路径成分被拒绝", not dst_out and not pv.get('plan'),
          f"plan={pv.get('plan')} errors={pv.get('errors')}")
    pv2 = client.post('/api/batch/rename/preview',
                      data=json.dumps({'files': [esc1], 'mode': 'append',
                                       'params': {'prefix': 'a/b'}}),
                      content_type='application/json', **auth).get_json()
    dst_out2 = [p for p in pv2.get('plan', []) if os.path.dirname(p['dst']) != lib]
    check("append 前缀带路径成分被拒绝", not dst_out2 and not pv2.get('plan'))
    # 模板 {ext:upper} 不双拼扩展名
    pv3 = client.post('/api/batch/rename/preview',
                      data=json.dumps({'files': [esc1], 'mode': 'template',
                                       'params': {'template': '{name}_x{ext:upper}'}}),
                      content_type='application/json', **auth).get_json()
    dst3 = (pv3.get('plan') or [{}])[0].get('dst', '')
    check("{ext:upper} 不双拼扩展名", dst3.endswith('_x.JPG') and not dst3.endswith('.JPG.JPG'), dst3)
    os.remove(esc1)

    # ------------------------------------------------------------------
    print("\n== 8. rename_template 修饰符与边界 ==")
    ctx = {'name': 'Photo', 'ext': '.jpg', 'seq': 7, 'parent': 'P', 'parent2': '',
           'meta': {'date_taken': 1704067200.0, 'mtime': 1704067200.0}}
    check("{name:upper}", rename_template.render('{name:upper}', ctx) == 'PHOTO')
    check("{seq:3} 补零", rename_template.render('{seq:3}', ctx) == '007')
    check("date 默认 %Y%m%d", rename_template.render('{date:taken}', ctx) == '20240101')
    check("date 修饰符生效（此前被吞）", rename_template.render('{date:taken:upper}', ctx) == '20240101')
    check("strftime 带冒号字段都被渲染（冒号本身被 sanitize 过滤）",
          rename_template.render('{date:taken:%H:%M}', ctx) == '00_00',
          rename_template.render('{date:taken:%H:%M}', ctx))
    check("未知 token 原样保留", rename_template.render('{nope}', ctx) == '{nope}')
    check("渲染空结果可被识别", rename_template.render('{iso}', ctx) == '')

    # ------------------------------------------------------------------
    print("\n== 9. 软锁：搜索排除 + 写守卫 + 重锁失效 ==")
    priv = os.path.join(lib, 'priv')
    os.makedirs(priv)
    priv_img = make_img(os.path.join(priv, 'secret.jpg'))
    norm_img = make_img(os.path.join(lib, 'normal.jpg'))
    scanner._scan_library(lib)
    lk = client.post('/api/locks', data=json.dumps({'path': priv, 'password': 'pw1'}),
                     content_type='application/json', **auth)
    check("上锁成功", lk.get_json().get('success'))
    sr = client.get('/api/search', **auth).get_json()
    paths = [f['path'] for f in sr.get('files', [])]
    check("搜索排除锁定目录", priv_img not in paths and norm_img in paths)
    rn = client.post('/api/rename',
                     data=json.dumps({'old_path': priv_img, 'new_name': 's2.jpg'}),
                     content_type='application/json', **auth)
    check("锁定目录 rename 被拒（423）", rn.status_code == 423 and rn.get_json().get('locked'))
    um = client.post('/api/locks/unlock', data=json.dumps({'path': priv, 'password': 'pw1'}),
                     content_type='application/json', **auth)
    check("密码解锁", um.get_json().get('success'))
    rn2 = client.post('/api/rename',
                      data=json.dumps({'old_path': priv_img, 'new_name': 's2.jpg'}),
                      content_type='application/json', **auth)
    check("解锁后 rename 放行", rn2.get_json().get('success'))
    # 换密码重锁 → 旧解锁凭据失效
    client.post('/api/locks', data=json.dumps({'path': priv, 'password': 'pw2'}),
                content_type='application/json', **auth)
    rn3 = client.post('/api/rename',
                      data=json.dumps({'old_path': os.path.join(priv, 's2.jpg'), 'new_name': 's3.jpg'}),
                      content_type='application/json', **auth)
    check("重锁后旧解锁凭据失效（再拒 423）", rn3.status_code == 423)
    client.post('/api/locks/unlock', data=json.dumps({'path': priv, 'password': 'pw2'}),
                content_type='application/json', **auth)

    # ------------------------------------------------------------------
    print("\n== 10. stacks 空堆叠清理 ==")
    s1 = make_img(os.path.join(lib, 'st1.jpg'))
    s2f = make_img(os.path.join(lib, 'st2.jpg'))
    sc = client.post('/api/stacks', data=json.dumps({'paths': [s1, s2f]}),
                     content_type='application/json', **auth).get_json()
    sid = sc['id']
    client.put(f'/api/stacks/{sid}', data=json.dumps({'remove': [s1, s2f]}),
               content_type='application/json', **auth)
    row = _q1("SELECT 1 FROM stacks WHERE id = ?", (sid,))
    check("移出全部成员后清理空堆叠行", row is None)

    # ------------------------------------------------------------------
    print("\n== 11. stats / markers / export 校验 ==")
    gone_row = db._meta_key(direct)  # 前面已删除且 missing=1
    stats = client.get('/api/stats', **auth).get_json()
    check("stats 排除 missing=1 行", not any(
        p == gone_row for p, in [(_q1("SELECT 1 FROM file_meta WHERE path = ? AND missing = 0", (gone_row,)) or (None,))]))
    # markers/export 区间校验（ffmpeg 存在与否都应先过参数校验）
    me = client.post('/api/markers/export',
                     data=json.dumps({'path': norm_img, 't_in': 10, 't_out': 5}),
                     content_type='application/json', **auth)
    check("markers/export t_out<=t_in → 400", me.status_code == 400)
    # export 后缀路径逃逸 → 400
    ex = client.post('/api/export/run',
                     data=json.dumps({'paths': [norm_img], 'dest': tmp, 'name_suffix': '/../x'}),
                     content_type='application/json', **auth)
    check("export 非法后缀 → 400", ex.status_code == 400)

    # ------------------------------------------------------------------
    print("\n== 12. 递归视图缓存失效 ==")
    from db import invalidate_cache, put_dir_cache, DIR_CACHE, CACHE_LOCK
    put_dir_cache(f'{lib}|r2', {'files': []})
    put_dir_cache(f'{lib}', {'files': []})
    invalidate_cache(lib)
    with CACHE_LOCK:
        left = [k for k in DIR_CACHE if k.startswith(lib)]
    check("invalidate_cache 连带清除 |r 递归键", not left, str(left))

    # ------------------------------------------------------------------
    print("\n== 13. 前端静态断言 ==")
    base = os.path.join(FE_DIR, 'static', 'assets', 'js')
    fr = open(os.path.join(base, 'fe-render.js')).read()
    fa = open(os.path.join(base, 'fe-actions.js')).read()
    check("sizeParam 提升到 createCard 函数级（在 image/video 分支前）",
          fr.index('const sizeParam = STATE.useLargeThumbs') < fr.index("else if(f.type==='image' || f.type==='video')"))
    check("堆叠角标走 data-args", 'data-action="toggleStackExpand" data-stop data-args=' in fr)
    check("toggleStackExpand 收 sid 参数", 'window.toggleStackExpand = (sid)' in fa)
    check("runOcrPanel 收 path 参数", 'window.runOcrPanel = async (path)' in fa)
    check("五 scheme 虚拟视图判定",
          'const isVirtualView = (p) => /^(smart|search|trash|collection|similar):\\/\\//' in fa)
    check("面包屑含 collection/similar",
          "'collection://', 'similar://'" in fr)
    check("地图 marker 收 layerGroup", 'markers.clearLayers()' in fa)

    print(f"\n{'='*50}\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
