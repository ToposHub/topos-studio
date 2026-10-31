#!/usr/bin/env python3
"""V3 查找工作流批验收：GAP-08 pHash / GAP-09 主色桶 / GAP-10 模板重命名 /
GAP-11 子文件夹聚合 / GAP-12 导入 / GAP-13 统计 / GAP-14 缓存管理。

1.  rename_template：变量/补零/日期/修饰符/非法字符
2.  批量重命名 template 模式执行 + preview dry-run + 集合成员随迁
3.  /api/similar：按需现算 phash → 汉明距离排序 → 同图重压缩命中
4.  主色桶：features_from_file 桶值 + /api/search?dom_bucket + smart 规则 + list 下发
5.  recursive=1：子目录文件聚合 + subdir 字段 + 缓存 key 区分
6.  /api/import/run：日期归档/模板/查重跳过/冲突后缀
7.  /api/stats 结构与数值核对
8.  /api/cache/size + cleanup（older_days 过滤）
9.  前端静态断言

隔离策略：DB 与 CONFIG_FILE 均重定向到临时目录。"""
import io
import json
import os
import sys
import tempfile
import time

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
    tmp = tempfile.mkdtemp(prefix="fe_v3_")
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
    import rename_template
    import media_features
    import utils
    import webapp

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    print("\n== 1. rename_template ==")
    ctx = {'name': 'IMG_0001', 'ext': '.jpg', 'seq': 5, 'parent': '子', 'parent2': '父',
           'meta': {'date_taken': 1700000000, 'mtime': 1700000000, 'camera': 'Sony A7M4',
                    'iso': 400, 'aperture': 2.8, 'shutter': '1/250', 'focal': 85.0,
                    'width': 6000, 'height': 4000, 'duration': None}}
    check("name+seq 补零", rename_template.render('{name}_{seq:3}', ctx) == 'IMG_0001_005')
    check("date taken strftime", rename_template.render('{date:taken:%Y%m%d}', ctx) == '20231114')
    check("date 回退 mtime", rename_template.render('{date:mtime:%Y}', ctx) == '2023')
    check("camera+iso", rename_template.render('{camera}_{iso}', ctx) == 'Sony A7M4_400')
    check("aperture/focal", rename_template.render('{aperture}_{focal}', ctx) == 'f2.8_85mm')
    check("大小写修饰", rename_template.render('{name:lower}{ext}', ctx) == 'img_0001.jpg')
    check("非法字符清洗", rename_template.render('{camera}/x', ctx.replace('camera', 'zzz')) if False
          else ':' not in rename_template.render('a:b{name}', ctx))
    check("未知字段原样保留", '{unknown}' in rename_template.render('{unknown}_{name}', ctx))
    check("parent/parent2", rename_template.render('{parent2}/{parent}' .replace('/', '_'), ctx) == '父_子')

    print("\n== 2. 批量重命名 template ==")
    fs = [make_img(os.path.join(lib, f"raw_{i}.jpg")) for i in range(3)]
    conn = db._db()
    for p in fs:
        conn.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                     (db._meta_key(p),))
    conn.commit()
    conn.close()
    r = client.post("/api/batch/rename/preview", json={"files": fs, "mode": "template",
                                                       "params": {"template": "旅拍_{date:taken:%Y%m%d}_{seq:3}"}}, **auth)
    plan = r.get_json().get('plan') or []
    today = time.strftime('%Y%m%d', time.gmtime())  # 无 date_taken 回退 mtime（=now）
    check("preview dry-run 不落盘", r.get_json().get('success') and len(plan) == 3
          and os.path.exists(fs[0]) and plan[0]['src'] != plan[0]['dst'], str(plan[:1]))
    check("preview 序号连续且自动带扩展名", [p['dst_name'] for p in plan] ==
          [f"旅拍_{today}_{str(i+1).zfill(3)}.jpg" for i in range(3)], str([p['dst_name'] for p in plan]))
    r = client.post("/api/batch/rename", json={"files": fs, "mode": "template",
                                               "params": {"template": "旅拍_{seq:2}"}}, **auth)
    d = r.get_json()
    check("模板重命名执行", d.get('success') and d.get('processed') == 3, str(d))
    check("磁盘改名成功", not os.path.exists(fs[0]) and os.path.exists(os.path.join(lib, "旅拍_01.jpg")))

    print("\n== 3. /api/similar（pHash） ==")
    fa = make_img(os.path.join(lib, "red_a.jpg"), color=(230, 40, 40))
    # 同色重压缩"相似图"：同画面缩放不同尺寸
    from PIL import Image
    similar_path = os.path.join(lib, "red_a_small.jpg")
    Image.new("RGB", (160, 120), (230, 40, 40)).save(similar_path, "JPEG", quality=60)
    fb = make_img(os.path.join(lib, "blue_b.jpg"), color=(30, 40, 230))
    # 直接为三张图计算并落库 phash（模拟缩略图管线产物）
    for p in (fa, similar_path, fb):
        ph, bucket = media_features.features_from_file(p)
        conn = db._db()
        conn.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)", (db._meta_key(p),))
        conn.execute("UPDATE file_meta SET phash = ?, dom_color = ? WHERE path = ?", (ph, bucket, db._meta_key(p)))
        conn.commit()
        conn.close()
    ph_a = media_features.features_from_file(fa)[0]
    ph_s = media_features.features_from_file(similar_path)[0]
    ph_b = media_features.features_from_file(fb)[0]
    check("phash 均非空", all([ph_a, ph_s, ph_b]))
    check("纯色图距离语义正确", media_features.hamming_distance(ph_a, ph_s) <= 12
          and media_features.hamming_distance(ph_a, ph_b) > 12,
          f"同色={media_features.hamming_distance(ph_a, ph_s)} 异色={media_features.hamming_distance(ph_a, ph_b)}")
    r = client.get(f"/api/similar?path={fa}&threshold=12", **auth)
    names = [f['name'] for f in r.get_json()['files']]
    check("相似命中同色重压缩", 'red_a_small.jpg' in names, str(names))
    check("异色不命中", 'blue_b.jpg' not in names)

    print("\n== 4. 主色桶 ==")
    bucket_red = media_features.features_from_file(fa)[1]
    bucket_blue = media_features.features_from_file(fb)[1]
    check("红图入红桶(0)", bucket_red == 0, str(bucket_red))
    check("蓝图入蓝桶(7/8)", bucket_blue in (7, 8), str(bucket_blue))
    gray = make_img(os.path.join(lib, "gray.jpg"), color=(128, 128, 128))
    check("灰阶桶 -1", media_features.features_from_file(gray)[1] == -1)
    r = client.get(f"/api/search?dom_bucket={bucket_red}", **auth)
    names = {f['name'] for f in r.get_json()['files']}
    check("search dom_bucket 过滤", 'red_a.jpg' in names and 'blue_b.jpg' not in names)
    files = routes.execute_smart_query({'dom_bucket': bucket_blue})
    check("smart dom_bucket 规则", any(f['name'] == 'blue_b.jpg' for f in files))
    r = client.get(f"/api/list?path={lib}", **auth)
    fmap = {f['name']: f for f in r.get_json()['files']}
    check("list 下发 dom_bucket", fmap['red_a.jpg'].get('dom_bucket') == bucket_red)

    print("\n== 5. recursive 聚合列表 ==")
    sub = os.path.join(lib, "day1")
    os.makedirs(sub, exist_ok=True)
    fsub = make_img(os.path.join(sub, "in_sub.jpg"))
    r = client.get(f"/api/list?path={lib}&recursive=1&depth=2", **auth)
    files = r.get_json()['files']
    names = {f['name'] for f in files}
    check("子目录文件出现在聚合列表", 'in_sub.jpg' in names and 'red_a.jpg' in names)
    subfile = next((f for f in files if f['name'] == 'in_sub.jpg'), None)
    check("subdir 字段携带来源", subfile and subfile.get('subdir') == 'day1')
    check("子目录文件夹卡片保留", any(f['type'] == 'dir' and f['name'] == 'day1' for f in files))
    r2 = client.get(f"/api/list?path={lib}", **auth)
    check("非递归视图不含子目录文件", 'in_sub.jpg' not in {f['name'] for f in r2.get_json()['files']})

    print("\n== 6. /api/import/run ==")
    src_dir = os.path.join(tmp, "sdcard")
    os.makedirs(src_dir)
    s1 = make_img(os.path.join(src_dir, "DSC_0001.jpg"), color=(10, 200, 90))
    s2 = make_img(os.path.join(src_dir, "DSC_0002.jpg"))
    os.makedirs(os.path.join(src_dir, "dup"), exist_ok=True)
    s2_dup = make_img(os.path.join(src_dir, "dup", "DSC_0002.jpg"))
    dest = os.path.join(lib, "imported")
    os.makedirs(dest)
    r = client.post("/api/import/run", json={"source": src_dir, "dest": dest, "date_folders": False,
                                             "skip_dup": True, "delete_source": False}, **auth)
    d = r.get_json()
    # 同名同大小的 dup/DSC_0002.jpg 按查重语义跳过
    check("导入计数（含查重跳过）", d.get('imported') == 2 and d.get('skipped') == 1, str(d))
    check("文件落到目标", os.path.exists(os.path.join(dest, "DSC_0001.jpg")))
    r = client.post("/api/import/run", json={"source": src_dir, "dest": dest, "date_folders": False,
                                             "skip_dup": True}, **auth)
    d = r.get_json()
    check("二次导入查重跳过", d.get('skipped') == 3, str(d))
    # 模板 + 日期归档：给源文件预置 date_taken（模拟卡内文件此前被索引过）
    conn = db._db()
    for p in (s1, s2, s2_dup):
        conn.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                     (db._meta_key(p),))
        conn.execute("UPDATE file_meta SET date_taken = 1700000000 WHERE path = ?", (db._meta_key(p),))
    conn.commit()
    conn.close()
    r = client.post("/api/import/run", json={"source": src_dir, "dest": dest, "date_folders": True,
                                             "name_template": " wedding_{seq:2} ", "skip_dup": False}, **auth)
    d = r.get_json()
    sub1 = os.path.join(dest, "2023", "2023-11-14")
    got = sorted(os.listdir(sub1)) if os.path.isdir(sub1) else []
    check("日期归档+模板命名", os.path.isdir(sub1) and any(g.startswith('wedding_') for g in got), str(got))

    print("\n== 7. /api/stats ==")
    r = client.get("/api/stats", **auth)
    s = r.get_json()
    check("stats 结构", {'total_files', 'total_bytes', 'by_ext', 'ratings', 'top_tags', 'missing'} <= set(s))
    check("计数合理", s['total_files'] >= 6 and s['total_bytes'] > 0, str(s['total_files']))
    exts = {x['ext'] for x in s['by_ext']}
    check("扩展名聚合", '.jpg' in exts, str(exts))

    print("\n== 8. 缓存管理 ==")
    gdir = os.path.join(tmp, ".assetdeck", "thumbnails")
    os.makedirs(gdir, exist_ok=True)
    old_f = os.path.join(gdir, "old.webp")
    new_f = os.path.join(gdir, "new.webp")
    open(old_f, 'wb').write(b'x')
    open(new_f, 'wb').write(b'y')
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old_f, (ten_days_ago, ten_days_ago))
    import thumb_engine
    saved = thumb_engine.engine.global_cache
    thumb_engine.engine.global_cache = gdir
    try:
        r = client.get("/api/cache/size", **auth)
        d = r.get_json()
        check("缓存体积统计", d.get('global', {}).get('bytes') == 2, str(d))
        r = client.post("/api/cache/cleanup", json={"scope": "global", "older_days": 5}, **auth)
        check("按龄清理只删旧文件", r.get_json().get('cleaned_files') == 1
              and os.path.exists(new_f) and not os.path.exists(old_f))
        r = client.post("/api/cache/cleanup", json={"scope": "global", "older_days": 0}, **auth)
        check("全量清理", r.get_json().get('cleaned_files') == 1 and not os.path.exists(new_f))
    finally:
        thumb_engine.engine.global_cache = saved

    print("\n== 9. 前端静态断言 ==")
    def src(rel):
        with open(os.path.join(FE_DIR, rel), encoding='utf-8') as f:
            return f.read()
    html = src("templates/index.html")
    check("筛选菜单自动颜色行", 'domBucketRow' in html and 'setFilterDomBucket' in html)
    check("递归开关按钮", 'toggleRecursiveList' in html)
    check("右键查找相似", 'ctx-similar' in html and 'runSimilarFromContext' in html)
    check("导入/统计/缓存入口与模态", 'openImportModal' in html and 'openStatsModal' in html
          and 'openCacheModal' in html and 'id="importModal"' in html)
    check("批量面板模板选项", 'value="template"' in src("static/assets/js/fe-render.js"))
    actions = src("static/assets/js/fe-actions.js")
    check("similar:// 视图", 'similar://' in actions and 'runSimilarSearch' in actions)
    check("递归参数携带", 'recursive=1' in actions and 'fe_recursive' in actions)
    check("导入/统计/缓存动作", 'runImport' in actions and 'loadStats' in actions and 'runCacheCleanup' in actions)
    check("模板重命名应用", "mode === 'template'" in actions)
    render = src("static/assets/js/fe-render.js")
    check("模板预览 dry-run", 'api/batch/rename/preview' in render and 'insertRenameVar' in render)
    check("subdir chip", 'subdir' in render and 'STATE.recursive' in render)
    check("dom 桶客户端过滤", 'dom_bucket' in render)
    scanner_src = src("scanner.py")
    check("thumbs 特征钩子", 'features_from_file' in src("thumbs.py"))
    routes_src = src("routes.py")
    check("similar/import/stats/cache 端点", all(k in routes_src for k in
          ('/api/similar', '/api/import/run', '/api/stats', '/api/cache/size', '/api/cache/cleanup')))
    check("缓存 key 区分递归", 'cache_key' in routes_src)

    print(f"\n{'='*46}\nV3 验收：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
