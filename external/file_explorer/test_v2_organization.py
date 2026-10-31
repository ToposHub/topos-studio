#!/usr/bin/env python3
"""V2 组织与互通批验收：GAP-04 XMP 回写 / GAP-05 集合 / GAP-06 层级标签 / GAP-07 堆叠。

1.  db：collections/stacks 表存在
2.  集合 CRUD + 成员 add/remove + 列表计数 + 失联行清理（保留回收站成员）
3.  集合路径随迁：rename / batch_move 后成员指向新路径
4.  堆叠：创建 → /api/list 下发 stack_id/cover/size → 换封面 → 取消堆叠
5.  update_meta 的 UPSERT 修复：改评分不再清空 duration/stack_id（V1 回归）
6.  层级标签：设父/防环/父标签检索含子孙（/api/search + 智能文件夹）
7.  XMP：off 模式丢弃；exiftool 可用时 sidecar 写入→读回→反向导入（含冲突保留）
8.  元数据导出 v3（collections/stacks）→ 导入恢复
9.  前端/接线静态断言

隔离策略：DB 与 CONFIG_FILE 均重定向到临时目录。"""
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


def make_jpeg(path, size=(50, 40)):
    from PIL import Image
    Image.new("RGB", size, (30, 90, 200)).save(path, "JPEG")
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="fe_v2_")
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)

    db._topos_db_path = os.path.join(tmp, "topos.db")
    db._fallback_db_path = os.path.join(tmp, "metadata.db")
    db.DB_FILE = db._fallback_db_path
    db.CONFIG_FILE = os.path.join(tmp, "config.json")
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}], "meta_embed": "sidecar"}, f)
    db.init_db()

    import routes
    import webapp
    import meta_sync
    import utils

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    print("\n== 1. DB 表 ==")
    conn = db._db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    check("collections/stacks 表存在", {'collections', 'collection_items', 'stacks'} <= tables)

    print("\n== 2. 集合 CRUD + 成员 ==")
    fa = make_jpeg(os.path.join(lib, "a.jpg"))
    fb = make_jpeg(os.path.join(lib, "b.jpg"))
    fc = make_jpeg(os.path.join(lib, "c.jpg"))
    r = client.post("/api/collections", json={"name": "客户A初选"}, **auth)
    d = r.get_json()
    check("创建集合", r.status_code == 200 and d.get('id'), str(d))
    cid = d['id']
    r = client.post(f"/api/collections/{cid}/items", json={"action": "add", "paths": [fa, fb]}, **auth)
    check("添加成员", r.get_json().get('count') == 2)
    r = client.get(f"/api/collections/{cid}/items", **auth)
    files = r.get_json()['files']
    check("成员列表（与 list 同结构）", {f['name'] for f in files} == {'a.jpg', 'b.jpg'}
          and all('t_ver' in f and 'rating' in f for f in files))
    r = client.get("/api/collections", **auth)
    colls = r.get_json()
    check("列表含计数", colls[0]['count'] == 2 and colls[0]['name'] == '客户A初选')
    r = client.post(f"/api/collections/{cid}/items", json={"action": "remove", "paths": [fb]}, **auth)
    check("移除成员", r.get_json().get('count') == 1)
    # 失联清理：磁盘删掉 a.jpg 且不在回收站 → items GET 清行
    trash_dir = os.path.join(FE_DIR, ".trash")
    os.remove(fa)
    client.get(f"/api/collections/{cid}/items", **auth)
    r = client.get(f"/api/collections/{cid}/items", **auth)
    check("失联成员行被清理", r.get_json()['files'] == [])
    client.put(f"/api/collections/{cid}", json={"name": "客户A定稿"}, **auth)
    r = client.get("/api/collections", **auth)
    check("重命名集合", r.get_json()[0]['name'] == '客户A定稿')

    print("\n== 3. 集合路径随迁 ==")
    fd = make_jpeg(os.path.join(lib, "d.jpg"))
    fe_ = make_jpeg(os.path.join(lib, "e.jpg"))
    client.post(f"/api/collections/{cid}/items", json={"action": "add", "paths": [fd, fe_]}, **auth)
    # 单文件 rename（API 自带磁盘 rename，无需预改名）
    client.post("/api/rename", json={"old_path": fd, "new_name": "d2.jpg"}, **auth)
    new_d = os.path.join(lib, "d2.jpg")
    conn = db._db()
    hit = conn.execute("SELECT 1 FROM collection_items WHERE file_path = ?", (db._meta_key(new_d),)).fetchone()
    conn.close()
    check("rename 后成员随迁", bool(hit))
    # batch_move
    sub = os.path.join(lib, "moved")
    os.makedirs(sub, exist_ok=True)
    client.post("/api/batch/move", json={"paths": [new_d], "dest": sub}, **auth)
    conn = db._db()
    hit = conn.execute("SELECT 1 FROM collection_items WHERE file_path = ?",
                       (db._meta_key(os.path.join(sub, "d2.jpg")),)).fetchone()
    conn.close()
    check("batch_move 后成员随迁", bool(hit))

    print("\n== 4. 堆叠 ==")
    f1 = make_jpeg(os.path.join(lib, "burst_1.jpg"))
    f2 = make_jpeg(os.path.join(lib, "burst_2.jpg"))
    f3 = make_jpeg(os.path.join(lib, "burst_3.jpg"))
    r = client.post("/api/stacks", json={"paths": [f1, f2, f3], "cover": f2}, **auth)
    d = r.get_json()
    check("创建堆叠", r.status_code == 200 and d.get('id'), str(d))
    sid = d['id']
    r = client.get(f"/api/list?path={lib}", **auth)
    fmap = {f['name']: f for f in r.get_json()['files']}
    check("list 下发 stack 字段", fmap['burst_2.jpg'].get('stack_id') == sid
          and fmap['burst_2.jpg'].get('stack_cover') == db._meta_key(f2).replace('//', '/')
          and fmap['burst_2.jpg'].get('stack_size') == 3,
          str(fmap['burst_2.jpg'].get('stack_cover')))
    check("全部成员带同一 stack_id", all(fmap[n].get('stack_id') == sid for n in ('burst_1.jpg', 'burst_2.jpg', 'burst_3.jpg')))
    # 子目录文件：堆叠 PUT 换封面
    client.put(f"/api/stacks/{sid}", json={"cover_path": f3}, **auth)
    conn = db._db()
    cover = conn.execute("SELECT cover_path FROM stacks WHERE id = ?", (sid,)).fetchone()[0]
    conn.close()
    check("换封面", cover == db._meta_key(f3))
    client.post(f"/api/stacks/{sid}/unstack", **auth)
    conn = db._db()
    left = conn.execute("SELECT COUNT(*) FROM file_meta WHERE stack_id = ?", (sid,)).fetchone()[0]
    gone = conn.execute("SELECT COUNT(*) FROM stacks WHERE id = ?", (sid,)).fetchone()[0]
    conn.close()
    check("取消堆叠清空成员与组", left == 0 and gone == 0)

    print("\n== 5. update_meta UPSERT 修复（V1 回归） ==")
    conn = db._db()
    conn.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)", (db._meta_key(f1),))
    conn.execute("UPDATE file_meta SET duration = 42.0, stack_id = 77 WHERE path = ?", (db._meta_key(f1),))
    conn.commit()
    conn.close()
    client.post("/api/update_meta", json={"path": f1, "rating": 4}, **auth)
    conn = db._db()
    row = conn.execute("SELECT rating, duration, stack_id FROM file_meta WHERE path = ?", (db._meta_key(f1),)).fetchone()
    conn.close()
    check("改评分保留技术列", row == (4, 42.0, 77), str(row))

    print("\n== 6. 层级标签 ==")
    r = client.post("/api/tags", json={"name": "项目"}, **auth)
    parent_id = r.get_json()['id']
    r = client.post("/api/tags", json={"name": "客户A"}, **auth)
    child_id = r.get_json()['id']
    r = client.put(f"/api/tags/{child_id}", json={"parent_id": parent_id}, **auth)
    check("设置父标签", r.get_json().get('success'))
    r = client.put(f"/api/tags/{parent_id}", json={"parent_id": child_id}, **auth)
    check("环检测拒绝", r.status_code == 400, str(r.get_json()))
    # 给 f1 打子标签"客户A" → /api/search?tag=项目 命中
    client.post("/api/meta/tags", json={"path": f1, "add": ["客户A"]}, **auth)
    r = client.get("/api/search?tag=项目", **auth)
    names = {f['name'] for f in r.get_json()['files']}
    check("父标签检索命中子标签文件", 'burst_1.jpg' in names, str(names))
    r = client.get("/api/search?tag=不存在", **auth)
    check("不存在标签返回空而非全库", r.get_json()['files'] == [])
    # 智能文件夹规则同语义
    files = routes.execute_smart_query({'tag': '项目'})
    check("智能文件夹 tag 规则含子孙", any(f['name'] == 'burst_1.jpg' for f in files))

    print("\n== 7. XMP ==")
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}], "meta_embed": "off"}, f)
    check("off 模式 enqueue 返回 False", meta_sync.enqueue(f1) is False)
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}], "meta_embed": "sidecar"}, f)
    exif = utils.get_tool_path('exiftool')
    if exif:
        fields = {'rating': 4, 'tags': ['客户A', '外景'], 'comments': '备注X', 'color': 3, 'flag': 1}
        ok = meta_sync.sync_once(f1, fields)
        check("sidecar 写入成功", ok and os.path.exists(f1 + '.xmp'))
        xmp = meta_sync.read_xmp(f1)
        check("读回评分", xmp and xmp['rating'] == 4, str(xmp))
        check("读回标签", xmp and {'客户A', '外景'} <= set(xmp['tags']), str(xmp and xmp['tags']))
        # 层级路径：给"客户A"挂到"项目"下，再写回应出现 项目/客户A
        hier = meta_sync._tag_hier_paths(['客户A'])
        check("层级路径拼接", hier == ['项目/客户A'], str(hier))
        # 反向导入：f3 无 DB 标记 → 采纳；f1 有标记 → 冲突保留
        res = meta_sync.import_from_files(library=lib)
        check("反向导入采纳/冲突统计", res['conflicts'] >= 1 and res['scanned'] >= 3, str(res))
        conn = db._db()
        r3 = conn.execute("SELECT rating FROM file_meta WHERE path = ?", (db._meta_key(f1),)).fetchone()
        conn.close()
        check("已有标记不被覆盖", r3[0] == 4)
    else:
        print("  ⚠️ exiftool 不可用：跳过 XMP 写入/导入实测（队列语义已验证）")
    conn = db._db()
    n_scar = conn.execute("SELECT COUNT(*) FROM file_meta WHERE path LIKE '%.xmp'").fetchone()[0]
    conn.close()
    check("sidecar 不入库", n_scar == 0)

    print("\n== 8. 元数据导出 v3 / 导入 ==")
    client.post(f"/api/collections/{cid}/items", json={"action": "add", "paths": [f1, f2]}, **auth)
    client.post("/api/stacks", json={"paths": [f1, f2]}, **auth)
    r = client.get("/api/meta/export", **auth)
    backup = json.loads(r.get_data(as_text=True))
    check("导出 v3 含 collections/stacks", backup.get('version') == 3
          and backup.get('collections') and backup.get('stacks'))
    coll_names = [c['name'] for c in backup['collections']]
    check("集合成员随导出", any(c['items'] for c in backup['collections']), str(coll_names))
    # 导入（merge）到当前库：不重复创建同名集合
    r = client.post("/api/meta/import", json={"rows": [], "mode": "merge",
                                              **{k: backup[k] for k in ('tags', 'trash', 'collections', 'stacks')}}, **auth)
    d = r.get_json()
    check("导入成功", d.get('success'), str(d))
    r = client.get("/api/collections", **auth)
    check("同名集合不重复", len(r.get_json()) == 1, str(len(r.get_json())))

    print("\n== 9. 前端/接线静态断言 ==")
    def src(rel):
        with open(os.path.join(FE_DIR, rel), encoding='utf-8') as f:
            return f.read()
    html = src("templates/index.html")
    check("侧栏集合区块", 'id="collectionList"' in html and 'createCollection' in html)
    check("集合右键菜单", 'id="collectionContextMenu"' in html and 'handleCollDelete' in html)
    check("右键堆叠/取消/集合移除", 'ctx-stack' in html and 'ctx-unstack' in html and 'ctx-coll-remove' in html)
    check("设置菜单 XMP 模式/导入", 'setMetaEmbedMode' in html and 'runXmpImport' in html)
    actions = src("static/assets/js/fe-actions.js")
    check("集合虚拟视图", 'collection://' in actions and 'openCollectionView' in actions)
    check("拖拽入集", 'addPathsToCollection' in actions and 'text/plain' in actions)
    check("标签树渲染", 'childrenOf' in actions and 'data-tag-id' in actions)
    check("标签拖拽改父", 'application/x-fe-tag' in actions)
    check("堆叠/取消/展开", 'stackSelected' in actions and 'unstackSelected' in actions and 'toggleStackExpand' in actions)
    check("XMP 设置", 'setMetaEmbedMode' in actions and 'meta_embed' in actions)
    check("reloadFiles 集合分流", "cur.startsWith('collection://')" in actions)
    render = src("static/assets/js/fe-render.js")
    check("堆叠折叠过滤", 'coverSeen' in render and 'stack_cover' in render)
    check("封面 ×N 角标", 'stack-badge' in render and 'toggleStackExpand' in render)
    check("右键显隐逻辑", "setV('ctx-stack'" in render and "setV('ctx-coll-remove'" in render)
    check("集合菜单隐藏联动", 'collectionContextMenu' in render)
    scanner_src = src("scanner.py")
    check("扫描器跳过 .xmp", ".xmp" in scanner_src)
    db_src = src("db.py")
    check("集合/堆叠建表", 'CREATE TABLE IF NOT EXISTS collections' in db_src and 'CREATE TABLE IF NOT EXISTS stacks' in db_src)
    routes_src = src("routes.py")
    check("列表隐藏 .xmp", ".xmp" in routes_src and "endswith('.xmp')" in routes_src)
    check("UPSERT 修复", 'ON CONFLICT(path) DO UPDATE SET' in routes_src)
    check("堆叠随列表下发", 'stack_covers' in routes_src and 'stack_sizes' in routes_src)
    check("递归 CTE 标签树", 'WITH RECURSIVE tag_tree' in routes_src)
    meta_src = src("meta_sync.py")
    check("XMP 模块", 'HierarchicalSubject' in meta_src and 'ColorLabel' in meta_src)

    print(f"\n{'='*46}\nV2 验收：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
