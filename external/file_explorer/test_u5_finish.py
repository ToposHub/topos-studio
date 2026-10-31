#!/usr/bin/env python3
"""U5 收尾验收（2026-09-07）：

1. 智能文件夹规则联动 color/flag（验收③第四处联动）：
   execute_smart_query 新增 q / tag / color / flag 四类规则的功能断言。
2. "保存为智能文件夹"同语义回放：search:// 的 q/tag 与智能查询规则结果一致。
3. 性能验收①：1000 文件打标后，标签+类型+评分+颜色组合筛选 < 500ms。
4. 性能验收②：5 万行库全库搜索 FTS 路径计时（报告值，断言 < 2s）。

隔离策略：db 路径重定向到临时目录（延迟判定读模块全局，patch 生效），
不触碰真实 topos.db / metadata.db。"""
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
    tmp = tempfile.mkdtemp(prefix="fe_u5_finish_")
    files_dir = os.path.join(tmp, "files")
    os.makedirs(files_dir)

    # --- 隔离：DB 重定向 + 初始化 ---
    db._topos_db_path = os.path.join(tmp, "topos.db")
    db._fallback_db_path = os.path.join(tmp, "metadata.db")
    db.DB_FILE = db._fallback_db_path
    db.init_db()

    import routes
    import webapp

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    def make_file(name, mtime=None):
        p = os.path.join(files_dir, name)
        with open(p, "w") as f:
            f.write("x")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p.replace("\\", "/")

    print("\n== 准备 1000 文件（验收①数据集）==")
    paths = [make_file(f"img_{i:04d}.jpg") for i in range(1000)]
    # 1000 行 meta 一次性入删（真实打标走 /api/batch/meta 在下方计时）
    conn = db._db()
    conn.executemany(
        "INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
        [(p,) for p in paths])
    conn.commit()
    conn.close()

    print("\n== 1. 智能文件夹 color/flag/q/tag 规则联动 ==")
    # 通过正式 API 打颜色/旗标/标签（值域校验路径一并覆盖）
    r = client.post("/api/batch/meta", json={
        "files": paths[:200], "color": 1, "flag": 1}, **auth)
    check("batch 设置 color=1/flag=pick（200 文件）", r.status_code == 200 and r.get_json().get("success"))

    r = client.post("/api/batch/meta", json={
        "files": paths[200:400], "color": 3}, **auth)
    check("batch 设置 color=3（200 文件）", r.status_code == 200)

    r = client.post("/api/batch/meta", json={
        "files": paths[400:500], "flag": 2}, **auth)
    check("batch 设置 flag=reject（100 文件）", r.status_code == 200)

    r = client.post("/api/meta/tags", json={"path": paths[0], "add": ["red"]}, **auth)
    check("单文件加标签 red", r.status_code == 200 and r.get_json().get("success"))
    client.post("/api/meta/tags", json={"path": paths[1], "add": ["hundred"]}, **auth)
    client.post("/api/meta/tags", json={"path": paths[2], "add": ["red", "落日"]}, **auth)

    files = routes.execute_smart_query({"color": 1})
    check("规则 color=1 → 恰 200 个", len(files) == 200, f"got {len(files)}")
    check("color 规则行带 color 字段", files and all(f.get("color") == 1 for f in files))

    files = routes.execute_smart_query({"flag": "pick"})
    check("规则 flag=pick → 恰 200 个", len(files) == 200, f"got {len(files)}")

    files = routes.execute_smart_query({"flag": "reject"})
    check("规则 flag=reject → 恰 100 个", len(files) == 100, f"got {len(files)}")

    files = routes.execute_smart_query({"flag": "noreject"})
    check("规则 flag=noreject → 900 个（1000 剔除 100 拒收）", len(files) == 900, f"got {len(files)}")

    files = routes.execute_smart_query({"tag": "red"})
    check("规则 tag=red 精确匹配 → 2 个（red 不命中 hundred）",
          len(files) == 2, f"got {len(files)}")

    files = routes.execute_smart_query({"tag": "red", "color": 1})
    check("组合 tag=red AND color=1", len(files) >= 1 and all(
        f.get("color") == 1 for f in files), f"got {len(files)}")

    files = routes.execute_smart_query({"q": "落日"})
    check("规则 q=落日（标签子串）→ 1 个", len(files) == 1, f"got {len(files)}")

    files = routes.execute_smart_query({"q": "img_000"})
    check("规则 q 命中文件名", len(files) == 10, f"got {len(files)}")

    files = routes.execute_smart_query({"color": 9})
    check("非法 color=9 不进 SQL（返回全量）", len(files) == 1000, f"got {len(files)}")

    print("\n== 2. '保存为智能文件夹'同语义回放 ==")
    r = client.get("/api/search?q=" + "落日", **auth)
    n_search = len(r.get_json().get("files", []))
    n_smart = len(routes.execute_smart_query({"q": "落日"}))
    check("search(q=落日) 与 smart(q=落日) 结果数一致", n_search == n_smart,
          f"search={n_search} smart={n_smart}")

    r = client.get("/api/search?tag=red", **auth)
    n_search = len(r.get_json().get("files", []))
    n_smart = len(routes.execute_smart_query({"tag": "red"}))
    check("search(tag=red) 与 smart(tag=red) 结果数一致", n_search == n_smart,
          f"search={n_search} smart={n_smart}")

    print("\n== 3. 性能验收①：1000 文件打标 + 组合筛选 <500ms ==")
    r = client.post("/api/batch/meta", json={
        "files": paths, "tags": "batch1000", "color": 2}, **auth)
    check("批量给 1000 文件打标+颜色", r.status_code == 200 and r.get_json().get("success"))

    # 组合筛选（标签 AND 类型 AND 评分 AND 颜色）走 /api/search 全链路
    t0 = time.perf_counter()
    r = client.get("/api/search?tag=batch1000&type=image&min_rating=0&color=2", **auth)
    dt_ms = (time.perf_counter() - t0) * 1000
    n = len(r.get_json().get("files", []))
    check(f"组合筛选 1000 库 <500ms（实测 {dt_ms:.0f}ms，命中 {n}）",
          dt_ms < 500 and n == 1000)

    # 智能文件夹查询（真实 GUI 打开智能文件夹的路径）
    t0 = time.perf_counter()
    files = routes.execute_smart_query({"tag": "batch1000", "color": 2, "flag": "noreject"})
    dt_ms = (time.perf_counter() - t0) * 1000
    check(f"execute_smart_query 组合规则 <500ms（实测 {dt_ms:.0f}ms，{len(files)} 项）",
          dt_ms < 500)

    print("\n== 4. 性能验收②：5 万行库全库搜索 ==")
    bench_dir = os.path.join(tmp, "bench")
    os.makedirs(bench_dir)
    conn = db._db()
    rows = []
    N = 50000
    for i in range(N):
        if i % 10 == 0:
            name = f"落日_{i:05d}.jpg"      # 5000 个命中文件名
            # 全部命中路径真实落盘：/api/search 有 2000 条上限，
            # 截断子集（排序后取前 2000）走真实 stat + 元数据补齐
            p = os.path.join(bench_dir, name).replace("\\", "/")
            with open(p, "w") as f:
                f.write("x")
        else:
            name = f"bench_{i:05d}.jpg"
        p = os.path.join(bench_dir, name).replace("\\", "/")
        rows.append((p, "落日" if i % 50 == 0 else "", "", 0, None))
        if len(rows) >= 5000:
            conn.executemany(
                "INSERT OR IGNORE INTO file_meta (path, tags, comments, rating, cached_info) "
                "VALUES (?, ?, ?, ?, ?)", rows)
            conn.commit()
            rows = []
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO file_meta (path, tags, comments, rating, cached_info) "
            "VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM file_meta").fetchone()[0]
    conn.close()
    check(f"批量插入 {N} 行（FTS 触发器同步）", total >= N, f"table rows={total}")

    t0 = time.perf_counter()
    r = client.get("/api/search?q=" + "落日", **auth)
    dt_ms = (time.perf_counter() - t0) * 1000
    data = r.get_json()
    # [U10] total 语义 = 过滤后截断前命中数（组合筛选不再静默丢结果），
    # files 才是 2000 截断后的返回集
    check(f"5 万行库搜索'落日' <2000ms（实测 {dt_ms:.0f}ms，"
          f"命中 {data.get('total')}，2000 上限截断）",
          dt_ms < 2000 and len(data.get("files") or []) == 2000
          and data.get("total", 0) > 2000)

    t0 = time.perf_counter()
    r = client.get("/api/search?q=" + "落日_00100", **auth)
    dt_ms = (time.perf_counter() - t0) * 1000
    check(f"5 万行库单词精确命中 <2000ms（实测 {dt_ms:.0f}ms）",
          dt_ms < 2000 and r.get_json().get("total") == 1)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
