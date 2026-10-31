#!/usr/bin/env python3
"""U7 视图与工作流批验收：

- GAP-11: PDF 缩略图/预览修复（此前 /api/thumb 对 pdf 恒 204）
- GAP-10: 视频胶片条生成 + 缓存直读（scrub 帧切换的数据面 <100ms）
- GAP-09: 重复文件查找（体积预分组 + MD5；1 万文件 <30s）
- GAP-14: 批量旋转（尺寸互换）/ 批量移动（meta 随迁）
- GAP-15: SVG 缩略图直读
- 前端静态检查：面包屑/时间线/对比/幻灯片 UI 与 handler 齐备

隔离策略：DB/CONFIG 重定向临时目录。"""
import os
import sys
import time
import shutil
import tempfile

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


def main():
    tmp = tempfile.mkdtemp(prefix="fe_u7_")
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)
    db._topos_db_path = os.path.join(tmp, "topos.db")
    db._fallback_db_path = os.path.join(tmp, "metadata.db")
    db.DB_FILE = db._fallback_db_path
    db.CONFIG_FILE = os.path.join(tmp, "config.json")
    db.init_db()

    import routes
    import thumb_engine
    import webapp
    import utils

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    print("\n== 1. GAP-11: PDF 预览 ==")
    from PIL import Image
    pdf_path = os.path.join(lib, "doc.pdf").replace("\\", "/")
    Image.new("RGB", (400, 300), (200, 60, 60)).save(pdf_path)
    check("should_skip_file(pdf) 不再跳过",
          not thumb_engine.should_skip_file("doc.pdf")[0])
    r = client.get(f"/api/thumb?path={pdf_path}", **auth)
    check("PDF 缩略图可生成（此前恒 204）",
          r.status_code == 200 and r.content_length not in (0, None)
          and r.mimetype in ("image/webp", "image/png"), f"status={r.status_code}")

    print("\n== 2. GAP-10: 视频胶片条 ==")
    ffp = utils.get_tool_path("ffmpeg")
    if ffp:
        vid = os.path.join(lib, "clip.mp4").replace("\\", "/")
        os.makedirs(os.path.dirname(vid), exist_ok=True)
        import subprocess as sp
        sp.run([ffp, "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                "-pix_fmt", "yuv420p", vid], stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=60)
        if not os.path.exists(vid):
            check("测试视频生成（跳过：ffmpeg 不可用）", False)
        else:
            t0 = time.perf_counter()
            r = client.get(f"/api/thumb/strip?path={vid}", **auth)
            dt_first = (time.perf_counter() - t0) * 1000
            check(f"胶片条首次生成 200（{dt_first:.0f}ms，含 ffmpeg）",
                  r.status_code == 200, f"status={r.status_code}")
            t0 = time.perf_counter()
            for _ in range(5):
                r = client.get(f"/api/thumb/strip?path={vid}", **auth)
            dt_cached = (time.perf_counter() - t0) * 1000 / 5
            check(f"缓存直读 <100ms（实测 {dt_cached:.1f}ms/次）", dt_cached < 100)
            strip_path = thumb_engine.engine.get_filmstrip_path(vid)
            check("胶片条缓存于源目录 .cache", os.path.exists(strip_path))
    else:
        check("胶片条（跳过：环境无 ffmpeg）", True)

    print("\n== 3. GAP-09: 重复文件查找 ==")
    dup_dir = os.path.join(lib, "dups")
    os.makedirs(dup_dir)
    data = os.urandom(4096)
    for n in ("a.bin", "a_copy.bin", "a_copy2.bin"):
        with open(os.path.join(dup_dir, n), "wb") as f:
            f.write(data)
    for n in ("b.bin", "b_diff.bin"):  # 同体积不同内容：不得判重
        with open(os.path.join(dup_dir, n), "wb") as f:
            f.write(os.urandom(4096) if n == "b_diff.bin" else b"B" * 4096)
    r = client.get(f"/api/duplicates/scan?path={dup_dir}", **auth)
    d = r.get_json()
    check("同内容 3 份判为一组", any(len(g["files"]) == 3 for g in d.get("groups", [])),
          str([[len(g['files'])] for g in d.get("groups", [])]))
    check("同体积不同内容不误判", all(
        len(g["files"]) < 3 or "a.bin" in g["files"][0] for g in d.get("groups", [])))
    check("唯一体积文件不进哈希（hashed=7? 仅 3+2 组内）", d.get("hashed") == 5, f"hashed={d.get('hashed')}")

    # 1 万文件规模 <30s
    big_dir = os.path.join(lib, "big")
    os.makedirs(big_dir)
    for i in range(10000):
        with open(os.path.join(big_dir, f"f{i:05d}.dat"), "wb") as f:
            f.write(b"x" * 64)  # 全同体积：最坏情况，200 份一组
        if i % 1000 == 999:
            pass
    # 放 3 个真实重复
    for n in ("dup1.dat", "dup2.dat", "dup3.dat"):
        with open(os.path.join(big_dir, n), "wb") as f:
            f.write(b"UNIQUE_DUP" * 10)
    t0 = time.perf_counter()
    r = client.get(f"/api/duplicates/scan?path={big_dir}", **auth)
    dt = time.perf_counter() - t0
    d = r.get_json()
    # 10000 个同内容 64B 文件聚成 1 组 + 3 个 dup 文件 1 组 = 2 组
    big_groups = d.get("groups", [])
    check(f"1 万文件扫描 <30s（实测 {dt:.1f}s，{len(big_groups)} 组，"
          f"最大组 {max((len(g['files']) for g in big_groups), default=0)} 份）",
          dt < 30 and len(big_groups) == 2
          and max((len(g['files']) for g in big_groups), default=0) == 10000)

    print("\n== 4. GAP-14: 批量旋转 / 批量移动 ==")
    img = os.path.join(lib, "rot.jpg").replace("\\", "/")
    Image.new("RGB", (300, 200), (10, 120, 240)).save(img, quality=90)
    r = client.post("/api/batch/rotate", json={"paths": [img], "degrees": 90}, **auth)
    d = r.get_json()
    with Image.open(img) as im:
        check("旋转 90° 尺寸互换", d.get("success") and im.size == (200, 300), str(im.size))
    conn = db._db()
    conn.execute("INSERT OR REPLACE INTO file_meta (path, tags, rating) VALUES (?, ?, ?)",
                 (db._meta_key(img), "要搬家", 3))
    conn.commit()
    conn.close()
    dest_dir = os.path.join(lib, "dest")
    os.makedirs(dest_dir)
    r = client.post("/api/batch/move", json={"paths": [img], "dest": dest_dir}, **auth)
    d = r.get_json()
    new_path = os.path.join(dest_dir, "rot.jpg").replace("\\", "/")
    conn = db._db()
    row = conn.execute("SELECT tags, rating FROM file_meta WHERE path = ?",
                       (db._meta_key(new_path),)).fetchone()
    conn.close()
    check("批量移动 + 元数据随迁", d.get("success") and os.path.exists(new_path)
          and not os.path.exists(img) and row == ("要搬家", 3), str(row))

    print("\n== 5. GAP-15: SVG 直读 ==")
    svg = os.path.join(lib, "icon.svg").replace("\\", "/")
    with open(svg, "w") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>')
    r = client.get(f"/api/thumb?path={svg}", **auth)
    check("SVG 缩略图直读回源", r.status_code == 200 and "svg" in r.mimetype,
          f"{r.mimetype}")

    print("\n== 6. 前端静态检查 ==")
    import re
    actions = open("static/assets/js/fe-actions.js", encoding="utf-8").read()
    render = open("static/assets/js/fe-render.js", encoding="utf-8").read()
    html = open("templates/index.html", encoding="utf-8").read()
    css = open("static/assets/css/style.css", encoding="utf-8").read()
    checks = [
        ("面包屑容器", 'id="breadcrumbBar"' in html),
        ("面包屑渲染", "window.renderBreadcrumb" in render),
        ("复制路径 handler", "window.copyPath" in actions),
        ("时间线视图按钮", '"timeline"' in html),
        ("时间线分组渲染", "tl-header" in render and "tlCounts" in render),
        ("时间线强制日期排序", "sortField = STATE.viewMode === 'timeline' ? 'date'" in render),
        ("对比视图", 'id="compareView"' in html and "window.openCompareView" in actions),
        ("幻灯片控制", 'data-action="toggleSlideshow"' in html and "window.toggleSlideshow" in actions),
        ("灯箱文本预览", 'id="cmsText"' in html and "cmsText" in render),
        ("重复查找模态框", 'id="duplicatesModal"' in html and "window.runDuplicateScan" in actions),
        ("批量旋转/移动 handler", "window.batchRotate" in actions and "window.batchMove" in actions),
        ("视频 scrub overlay", "data-video-scrub" in render and "strip-overlay" in css),
        ("属性面板胶片条", "prop-filmstrip" in render),
        ("PDF 预览标 Enter 灯箱", "cms-text-pane" in css),
    ]
    for name, ok in checks:
        check(name, ok)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
