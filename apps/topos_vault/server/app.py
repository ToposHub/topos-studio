"""vault 查询服务的 Flask 应用。

进程 C 入口：``topos-vault serve``。

端点（全部只读，GET）：
    GET /api/search?q=<关键词>&type=<all|video|image|audio>&limit=<N>
        → {files: [{name, path, type, ...}, ...]}  （格式对齐 file_explorer）
    GET /api/stats
        → {total, by_type, ...}
    GET /api/health
        → {status: "ok", db: "..."}

端口：默认 8288（避开 file_explorer 的 8188），被占用自动找下一个。
"""

from __future__ import annotations

import logging
import socket
from typing import Any

from flask import Flask, jsonify, request

from apps.topos_vault.db.repository import VaultRepository
from apps.topos_vault.paths import get_db_path
from apps.topos_vault.server.query_service import QueryService

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8288  # file_explorer 用 8188，vault 用 8288


def create_app(repo: VaultRepository | None = None) -> Flask:
    """创建查询服务的 Flask app。

    Args:
        repo: 注入的 repository（测试用）。None 则用默认 vault.db。
    """
    app = Flask(__name__)
    if repo is None:
        repo = VaultRepository()
    svc = QueryService(repo)

    # ---- /api/search ----
    @app.route("/api/search", methods=["GET"])
    def search() -> Any:
        """搜索素材，返回 file_explorer 兼容格式。"""
        q = request.args.get("q", "").strip()
        media_type = request.args.get("type", "all").strip().lower()
        limit = min(int(request.args.get("limit", "100")), 500)

        if not q:
            return jsonify({"files": [], "query": q, "total": 0})

        if media_type not in ("all", "video", "image", "audio"):
            media_type = "all"

        hits = svc.search(q, media_type=media_type, limit=limit)
        files = [svc.to_file_explorer_dict(h) for h in hits]

        return jsonify({
            "files": files,
            "query": q,
            "type": media_type,
            "total": len(files),
        })

    # ---- /api/stats ----
    @app.route("/api/stats", methods=["GET"])
    def stats() -> Any:
        """库统计（给 UI 的状态栏用）。"""
        return jsonify({
            "total": repo.count_media_sources(),
            "by_type": {
                "video": repo.count_media_sources("video"),
                "audio": repo.count_media_sources("audio"),
                "image": repo.count_media_sources("image"),
                "image_sequence": repo.count_media_sources("image_sequence"),
            },
            "missing": len(repo.list_media_sources(path_status="missing", limit=100000)),
            "db_path": str(get_db_path()),
        })

    # ---- /api/health ----
    @app.route("/api/health", methods=["GET"])
    def health() -> Any:
        """健康检查。"""
        return jsonify({"status": "ok", "service": "topos-vault"})

    # ---- /api/similar_image（以图搜图，P2-3）----
    @app.route("/api/similar_image", methods=["GET"])
    def similar_image() -> Any:
        """以图搜图：?path=<本地图片路径>&type=<all|video|image>&limit=<N>"""
        path = request.args.get("path", "").strip()
        media_type = request.args.get("type", "all").strip().lower()
        limit = min(int(request.args.get("limit", "20")), 200)
        if not path:
            return jsonify({"files": [], "error": "missing path"})
        if media_type not in ("all", "video", "image"):
            media_type = "all"
        hits = svc.search_similar_image(path, media_type=media_type, limit=limit)
        files = [svc.to_file_explorer_dict(h) for h in hits]
        return jsonify({"files": files, "query_path": path, "total": len(files)})

    # ---- /api/by_bpm（BPM 匹配，P2-3）----
    @app.route("/api/by_bpm", methods=["GET"])
    def by_bpm() -> Any:
        """按 BPM 找节奏相近音频：?bpm=<N>&tolerance=<±N>&limit=<N>"""
        try:
            bpm = float(request.args.get("bpm", "0"))
        except ValueError:
            return jsonify({"files": [], "error": "invalid bpm"})
        tolerance = float(request.args.get("tolerance", "5.0"))
        limit = min(int(request.args.get("limit", "50")), 200)
        hits = svc.search_by_bpm(bpm, tolerance=tolerance, limit=limit)
        files = [svc.to_file_explorer_dict(h) for h in hits]
        return jsonify({"files": files, "bpm": bpm, "total": len(files)})

    # ---- /api/advanced（组合检索，P2-3）----
    @app.route("/api/advanced", methods=["GET"])
    def advanced() -> Any:
        """组合高级检索：多条件 AND 过滤。

        参数（全部可选）：
            q=<关键词>  type=<类型>  character=<角色名>  cluster=<聚类id>
            mood=<情绪>  bpm_min=<N>  bpm_max=<N>
            dur_min=<秒>  dur_max=<秒>  min_relevance=<0-1>  limit=<N>
        """
        bpm_range = None
        if request.args.get("bpm_min") or request.args.get("bpm_max"):
            try:
                bpm_range = (
                    float(request.args.get("bpm_min", "0")),
                    float(request.args.get("bpm_max", "1e9")),
                )
            except ValueError:
                bpm_range = None
        duration_range = None
        if request.args.get("dur_min") or request.args.get("dur_max"):
            try:
                duration_range = (
                    float(request.args.get("dur_min", "0")),
                    float(request.args.get("dur_max", "1e9")),
                )
            except ValueError:
                duration_range = None
        cluster = None
        if request.args.get("cluster"):
            try:
                cluster = int(request.args.get("cluster"))
            except ValueError:
                pass

        hits = svc.advanced_search(
            query=request.args.get("q", ""),
            media_type=request.args.get("type", "all"),
            character=request.args.get("character") or None,
            cluster_id=cluster,
            mood=request.args.get("mood") or None,
            bpm_range=bpm_range,
            duration_range=duration_range,
            min_relevance=float(request.args.get("min_relevance", "0")),
            limit=min(int(request.args.get("limit", "100")), 500),
        )
        files = [svc.to_file_explorer_dict(h) for h in hits]
        return jsonify({"files": files, "total": len(files)})

    return app


def find_available_port(start: int = DEFAULT_PORT, max_tries: int = 20) -> int:
    """找可用端口（避开 file_explorer 的 8188）。"""
    for port in range(start, start + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port in {start}-{start + max_tries}")


def run_server(port: int | None = None, host: str = "127.0.0.1") -> None:
    """启动查询服务（阻塞，供 CLI serve 调用）。"""
    from apps.topos_vault.db import schema

    schema.init_db()  # 确保 db 存在
    if port is None:
        port = find_available_port()

    app = create_app()
    logger.info("vault 查询服务启动：http://%s:%d", host, port)
    print(f"✅ vault 查询服务：http://{host}:{port}")
    print(f"   搜索示例：http://{host}:{port}/api/search?q=微笑&type=all")
    print("   Ctrl+C 停止")
    app.run(host=host, port=port, debug=False, threaded=True)
