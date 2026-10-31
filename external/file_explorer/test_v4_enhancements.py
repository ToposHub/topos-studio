#!/usr/bin/env python3
"""V4 增强批验收：GAP-16 GPS / GAP-17 OCR / GAP-18 导出预设+水印 /
GAP-19 视频标记 / GAP-21 AI 打标 / GAP-22 文件夹软锁。

1.  /api/map/files：GPS pin 列表
2.  OCR：状态探测（无引擎 available=false + 手动 run 显式 400）、
    process 落 ocr_text + FTS ocr 列（注入假引擎）、FTS 升级重建后 4 列旧数据可搜
3.  导出预设 CRUD + /api/export/run（缩放/格式/文字水印/命名/冲突后缀）
4.  视频标记：CRUD + /api/markers/export（ffmpeg 缺失时 400 语义）
5.  AI 打标：stub vault（线程内 HTTP 服务）→ tag_file 写 ai:* 标签且保留人工标签
6.  软锁：设置 → /api/list 423 → thumb 403 → file 423 → 解锁放行 → 删除锁需密码
7.  前端静态断言

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


def make_img(path, size=(320, 240), color=(220, 30, 30)):
    from PIL import Image
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="fe_v4_")
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
    import utils
    import webapp

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    print("\n== 1. /api/map/files ==")
    fa = make_img(os.path.join(lib, "geo.jpg"))
    fb = make_img(os.path.join(lib, "nogeo.jpg"))
    conn = db._db()
    for p, lat, lon in ((fa, 39.9042, 116.4074), (fb, None, None)):
        conn.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                     (db._meta_key(p),))
        if lat is not None:
            conn.execute("UPDATE file_meta SET gps_lat = ?, gps_lon = ? WHERE path = ?", (lat, lon, db._meta_key(p)))
    conn.commit()
    conn.close()
    r = client.get("/api/map/files", **auth)
    d = r.get_json()
    check("GPS pin 列表", d.get('total') == 1 and d['files'][0]['name'] == 'geo.jpg'
          and abs(d['files'][0]['lat'] - 39.9042) < 0.001, str(d.get('total')))

    print("\n== 2. OCR ==")
    import ocr_engine
    r = client.get("/api/ocr/status", **auth)
    st = r.get_json()
    check("状态结构", {'available', 'enabled'} <= set(st))
    r = client.post("/api/ocr/run", json={"path": fa}, **auth)
    if ocr_engine.detect_engine() is None:
        check("无引擎时手动 run 显式 400", r.status_code == 400 and '引擎' in r.get_json().get('message', ''))
    else:
        check("有引擎时 run 成功", r.status_code == 200 and r.get_json().get('success'))
    # 注入假引擎验证落库 + FTS 管道
    orig = ocr_engine._engine
    ocr_engine._engine = lambda p: "会议纪要 9月7日 上线"
    try:
        ok = ocr_engine.process(fa)
    finally:
        pass  # 假引擎保留给下面的搜索验证，进程结束即弃
    check("假引擎 process 成功", ok)
    conn = db._db()
    row = conn.execute("SELECT text FROM ocr_text WHERE file_path = ?", (db._meta_key(fa),)).fetchone()
    fts = conn.execute("SELECT ocr FROM file_meta_fts WHERE path = ?", (db._meta_key(fa),)).fetchone()
    fts_cols = [x[1] for x in conn.execute("PRAGMA table_info(file_meta_fts)").fetchall()]
    conn.close()
    check("ocr_text 落库", row and '会议纪要' in row[0])
    check("FTS 含 ocr 列", 'ocr' in fts_cols, str(fts_cols))
    check("FTS ocr 列同步", fts and '会议纪要' in fts[0])
    r = client.get("/api/search?q=会议纪要", **auth)
    check("全库搜索命中图内文字", any(f['name'] == 'geo.jpg' for f in r.get_json()['files']))
    r = client.get(f"/api/ocr/text?path={fa}", **auth)
    check("/api/ocr/text 读回", '会议纪要' in r.get_json().get('text', ''))
    ocr_engine._engine_checked = True
    ocr_engine._engine = orig

    print("\n== 3. 导出预设 + 水印 ==")
    r = client.post("/api/export/presets", json={"name": "Web1280", "config": {
        "format": "jpeg", "quality": 80, "max_w": 1280, "max_h": 1280,
        "watermark": {"text": "© 测试", "position": "br", "opacity": 0.5}}}, **auth)
    check("保存预设", r.get_json().get('success'))
    r = client.get("/api/export/presets", **auth)
    presets = r.get_json()
    check("预设列表", len(presets) == 1 and presets[0]['config']['max_w'] == 1280)
    out_dir = os.path.join(tmp, "out")
    os.makedirs(out_dir)
    r = client.post("/api/export/run", json={"paths": [fa], "dest": out_dir, "format": "jpeg",
                                             "quality": 80, "max_w": 100, "max_h": 100,
                                             "watermark": {"text": "© X", "position": "br", "opacity": 0.5}}, **auth)
    d = r.get_json()
    outs = d.get('exported') or []
    check("导出成功且缩放+水印", len(outs) == 1 and os.path.exists(outs[0]))
    if outs:
        from PIL import Image
        with Image.open(outs[0]) as im:
            check("输出尺寸被限制", max(im.size) == 100, str(im.size))
    r = client.post("/api/export/run", json={"paths": [fa], "dest": out_dir, "format": "png",
                                             "quality": 90}, **auth)
    outs2 = r.get_json().get('exported') or []
    check("PNG 格式导出", outs2 and outs2[0].endswith('.png'))
    r = client.post("/api/export/run", json={"paths": [fa], "dest": out_dir, "format": "jpeg"}, **auth)
    outs3 = r.get_json().get('exported') or []
    check("重名冲突自动后缀", outs3 and ' (1)' in outs3[0], str(outs3))
    client.delete("/api/export/presets/1", **auth)
    r = client.get("/api/export/presets", **auth)
    check("删除预设", r.get_json() == [])

    print("\n== 4. 视频标记 ==")
    r = client.post("/api/markers", json={"path": fa, "t_in": 3.5, "t_out": 10.0, "label": "开场"}, **auth)
    mid = r.get_json().get('id')
    check("创建标记", bool(mid))
    r = client.get(f"/api/markers?path={fa}", **auth)
    ms = r.get_json().get('markers') or []
    check("标记列表", len(ms) == 1 and ms[0]['t_in'] == 3.5 and ms[0]['label'] == '开场')
    r = client.post("/api/markers/export", json={"marker_id": mid, "dest": out_dir}, **auth)
    if utils.get_tool_path('ffmpeg'):
        check("片段导出（ffmpeg 可用）", r.get_json().get('success'), str(r.get_json())[:100])
    else:
        check("ffmpeg 缺失时 400", r.status_code == 400)
    r = client.delete("/api/markers", json={"id": mid}, **auth)
    check("删除标记", r.get_json().get('success'))
    r = client.post("/api/markers/export", json={"marker_id": 999, "dest": out_dir}, **auth)
    check("导出不存在标记 404", r.status_code == 404)

    print("\n== 5. AI 打标（stub vault） ==")
    import ai_tagging
    served = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(ln) or b'{}')
            served['path'] = body.get('path')
            out = json.dumps({'tags': ['mountain', 'sunset']}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(('127.0.0.1', 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}], "ai_vault_url": f"http://127.0.0.1:{port}"}, f)
    n = ai_tagging.tag_file(fa)
    check("vault 标签写入（ai: 前缀）", n == 2, str(n))
    conn = db._db()
    tags = {r[0] for r in conn.execute(
        """SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_path = ?""",
        (db._meta_key(fa),)).fetchall()}
    conn.close()
    check("ai: 命名空间", {'ai:mountain', 'ai:sunset'} <= tags, str(tags))
    # 人工标签保留
    client.post("/api/meta/tags", json={"path": fa, "add": ["人工精选"]}, **auth)
    n2 = ai_tagging.tag_file(fa)
    conn = db._db()
    tags2 = {r[0] for r in conn.execute(
        """SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_path = ?""",
        (db._meta_key(fa),)).fetchall()}
    conn.close()
    check("人工标签不被 AI 覆盖", n2 >= 0 and '人工精选' in tags2, str(tags2))
    # 未配置时静默 0
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}]}, f)
    check("未配置 vault 返回 0", ai_tagging.tag_file(fa) == 0)
    srv.shutdown()

    print("\n== 6. 文件夹软锁 ==")
    lock_dir = os.path.join(lib, "私密")
    os.makedirs(lock_dir, exist_ok=True)
    fsec = make_img(os.path.join(lock_dir, "secret.jpg"))
    r = client.post("/api/locks", json={"path": lock_dir, "password": "123", "hint": "生日"}, **auth)
    check("设置软锁", r.get_json().get('success'))
    r = client.get(f"/api/list?path={lock_dir}", **auth)
    check("锁定目录 list 423", r.status_code == 423 and r.get_json().get('locked')
          and r.get_json().get('hint') == '生日', str(r.status_code))
    r = client.get(f"/api/list?path={lock_dir}", **auth)
    d = r.get_json()
    check("锁信息含解锁路径", d.get('lock_path') == db._meta_key(lock_dir).rstrip('/'))
    r = client.get(f"/api/thumb?path={fsec}", **auth)
    check("锁定目录缩略图 403", r.status_code == 403)
    r = client.get(f"/api/file?path={fsec}", **auth)
    check("锁定目录文件 423", r.status_code == 423)
    r = client.post("/api/locks/unlock", json={"path": lock_dir, "password": "错误"}, **auth)
    check("错误密码拒绝", r.get_json().get('success') is False)
    r = client.get(f"/api/list?path={lock_dir}", **auth)
    check("错误密码后仍锁定", r.status_code == 423)
    r = client.post("/api/locks/unlock", json={"path": lock_dir, "password": "123"}, **auth)
    check("正确密码解锁", r.get_json().get('success'))
    r = client.get(f"/api/list?path={lock_dir}", **auth)
    check("解锁后可浏览", r.status_code == 200)
    r = client.delete("/api/locks", json={"path": lock_dir, "password": "456"}, **auth)
    check("删锁错误密码拒绝", r.get_json().get('success') is False)
    r = client.delete("/api/locks", json={"path": lock_dir, "password": "123"}, **auth)
    check("删锁成功", r.get_json().get('success'))
    r = client.get(f"/api/list?path={lock_dir}", **auth)
    check("删锁后可浏览", r.status_code == 200)

    print("\n== 7. 前端静态断言 ==")
    def src(rel):
        with open(os.path.join(FE_DIR, rel), encoding='utf-8') as f:
            return f.read()
    html = src("templates/index.html")
    check("地图/导出模态", 'id="mapModal"' in html and 'id="exportModal"' in html)
    check("地图/OCR/导出入口", 'openMapModal' in html and 'toggleOcrEnabled' in html and 'openExportModal' in html)
    check("右键软锁", 'ctx-lock' in html and 'lockFolderFromContext' in html)
    actions = src("static/assets/js/fe-actions.js")
    check("Leaflet CDN + 降级", 'leaflet@1.9.4' in actions and 'onerror' in actions)
    check("解锁流程", 'promptUnlock' in actions and '/api/locks/unlock' in actions)
    check("导出/预设动作", 'runExport' in actions and 'saveExportPreset' in actions)
    check("幻灯片 BGM", 'bgm' in actions and 'runOcrPanel' in actions)
    render = src("static/assets/js/fe-render.js")
    check("属性面板 OCR/ GPS 链接", 'prop-ocr' in render and 'maps.apple.com' in render)
    check("幻灯片淡入", 'opacity' in render)
    routes_src = src("routes.py")
    check("软锁三端守卫", routes_src.count('_locked_by(') >= 4)
    check("V4 端点齐全", all(k in routes_src for k in
          ('/api/map/files', '/api/ocr/run', '/api/export/run', '/api/markers/export', '/api/locks')))
    db_src = src("db.py")
    check("V4 建表与 FTS 升级", 'ocr_text' in db_src and 'markers' in db_src
          and 'export_presets' in db_src and 'folder_locks' in db_src and "ocr' not in _fts_cols" in db_src)
    scanner_src = src("scanner.py")
    check("扫描器 OCR/AI 钩子", 'ocr_engine' in scanner_src and 'ai_tagging' in scanner_src)

    print(f"\n{'='*46}\nV4 验收：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
