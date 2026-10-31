"""topos-vault CLI 入口。

子命令：
    topos-vault init                 初始化 vault.db（建表 + 迁移）
    topos-vault status               查看库状态（路径/素材数/provider/依赖）
    topos-vault index <目录>         给目录下的素材做 AI 打标（阶段1+实现）
    topos-vault relink <目录>        素材移动后重连（三级匹配）
    topos-vault providers [能力]     列出 provider（可按能力过滤）

对齐项目主 app 的 toposctl CLI 模式：argparse 子命令 + 明确的错误返回。
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。返回退出码（0=成功，非0=失败）。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # P2-7：启动时配置结构化日志（环境变量可覆盖）
    from apps.topos_vault.logging_config import setup_logging
    setup_logging()

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topos-vault",
        description="Topos Vault — AI 影视素材库管理系统（打标/检索/重连）",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="初始化 vault.db（建表 + 迁移）")
    p_init.set_defaults(func=_cmd_init)

    # status
    p_status = sub.add_parser("status", help="查看库状态")
    p_status.set_defaults(func=_cmd_status)

    # index（阶段1+实现完整逻辑，当前只打印计划）
    p_index = sub.add_parser("index", help="给目录下的素材做 AI 打标")
    p_index.add_argument("path", help="要打标的目录或文件")
    p_index.add_argument("--type", choices=["video", "audio", "image", "auto"],
                         default="auto", help="素材类型（auto=按扩展名推断）")
    p_index.add_argument("--phased", action="store_true",
                         help="分阶段模式（24GB 显存友好：大模型分时加载）")
    p_index.set_defaults(func=_cmd_index)

    # relink
    p_relink = sub.add_parser("relink", help="素材移动后重连")
    p_relink.add_argument("dirs", nargs="+", help="候选搜索目录")
    p_relink.add_argument("--dry-run", action="store_true",
                          help="只检测不写库（查看会重连哪些）")
    p_relink.set_defaults(func=_cmd_relink)

    # cluster（人脸角色聚类）
    p_cluster = sub.add_parser("cluster", help="人脸角色聚类（把同一角色的人脸归一类）")
    p_cluster.add_argument("--eps", type=float, default=None,
                           help="DBSCAN 距离阈值（默认 0.4）")
    p_cluster.add_argument("--min-samples", type=int, default=None,
                           help="DBSCAN 最小样本数（默认 2）")
    p_cluster.add_argument("--list", action="store_true",
                           help="只列出聚类结果，不重聚类")
    p_cluster.set_defaults(func=_cmd_cluster)

    # history（任务历史，P2-7）
    p_history = sub.add_parser("history", help="查看打标任务历史")
    p_history.add_argument("--limit", type=int, default=20, help="显示条数（默认 20）")
    p_history.add_argument("--stats", action="store_true",
                           help="只显示聚合统计")
    p_history.set_defaults(func=_cmd_history)

    # watch（增量索引，P2-2）
    p_watch = sub.add_parser("watch", help="监听目录变化，自动增量打标")
    p_watch.add_argument("dirs", nargs="+", help="要监听的素材目录")
    p_watch.add_argument("--once", action="store_true",
                         help="只做一次全量增量扫描，不持续监听（补漏）")
    p_watch.set_defaults(func=_cmd_watch)

    # serve
    p_serve = sub.add_parser("serve", help="启动查询服务（供 file_explorer 插件调用）")
    p_serve.add_argument("--port", type=int, default=None, help="端口（默认 8288）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.set_defaults(func=_cmd_serve)

    # providers
    p_prov = sub.add_parser("providers", help="列出 AI 能力的 provider")
    p_prov.add_argument("capability", nargs="?", help="能力名（不填列全部）")
    p_prov.set_defaults(func=_cmd_providers)

    return parser


# ---------------------------------------------------------------------- #
# 子命令实现
# ---------------------------------------------------------------------- #

def _cmd_init(args) -> int:
    """init：初始化数据库。"""
    from apps.topos_vault.db import schema

    db_path = schema.init_db()
    version = schema.get_schema_version()
    print("✅ vault.db 已初始化")
    print(f"   路径：{db_path}")
    print(f"   schema 版本：{version}")
    print("   表：media_source / clip / transcript_line / face_track / "
          "audio_meta / image_meta / vector_index")
    return 0


def _cmd_status(args) -> int:
    """status：库状态总览。"""
    from apps.topos_vault.paths import describe_paths
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.providers import Capability, list_providers
    from apps.topos_vault.fingerprint.video import _HAS_XXHASH
    from apps.topos_vault.fingerprint.audio import is_chromaprint_available
    from apps.topos_vault.fingerprint.image import is_phash_available

    paths = describe_paths()
    print("=== 路径 ===")
    for k, v in paths.items():
        print(f"  {k:16s} {v}")

    print()
    print("=== 数据库 ===")
    version = schema.get_schema_version()
    print(f"  schema 版本：{version}")
    if version > 0:
        repo = VaultRepository()
        total = repo.count_media_sources()
        print(f"  素材总数：{total}")
        for mt in ("video", "audio", "image"):
            n = repo.count_media_sources(mt)
            if n:
                print(f"    {mt}: {n}")
        # 离线素材
        missing = len(repo.list_media_sources(path_status="missing", limit=100000))
        relocated = len(repo.list_media_sources(path_status="relocated", limit=100000))
        if missing:
            print(f"  ⚠️ 路径丢失：{missing}（需 relink）")
        if relocated:
            print(f"  已重连：{relocated}")
    else:
        print("  （未初始化，运行 topos-vault init）")

    print()
    print("=== 指纹依赖 ===")
    deps = [
        ("xxhash (视频)", _HAS_XXHASH),
        ("chromaprint (音频)", is_chromaprint_available()),
        ("imagehash (图片)", is_phash_available()),
    ]
    for name, ok in deps:
        status = "✅ 已装" if ok else "⚠️ 未装（降级到 size: 兜底）"
        print(f"  {name:24s} {status}")

    print()
    print("=== AI 能力 provider ===")
    for cap in Capability:
        providers = list_providers(cap)
        print(f"  {cap.value}:")
        for p in providers:
            gpu = "[GPU]" if p.requires_gpu else "[CPU]"
            default = " (默认)" if p.provider_id == list_providers(cap)[0].provider_id else ""
            print(f"    {gpu} {p.provider_id:30s} {p.display_name}{default}")

    print()
    print("=== 模型包（models/ai_editing/）===")
    from apps.topos_vault.model_specs import check_installation_status
    for m in check_installation_status():
        status = "✅ 已装" if m["installed"] else "⬜ 未装"
        gpu = f" [需GPU {m['min_vram_mb']}MB]" if m["requires_gpu"] else " [CPU]"
        print(f"  {status} {m['pack_id']:28s} {m['size_gb']:5.1f}GB{gpu}")
        print(f"           {m['display_name']}")

    return 0


def _cmd_index(args) -> int:
    """index：打标（阶段1+实现完整 AI 流水线，阶段0 做扫描+指纹+入库）。"""
    path = os.path.abspath(args.path)
    if not os.path.exists(path):
        print(f"错误：路径不存在 {path}", file=sys.stderr)
        return 1

    return _scan_and_register(path, args.type, phased=getattr(args, "phased", False))


def _scan_and_register(path: str, media_type: str, *, phased: bool = False) -> int:
    """用编排器打标。

    Args:
        phased: True 用 PhasedOrchestrator（24GB 显存友好，大模型分时加载）。
    """
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.task_history import TaskHistory

    schema.init_db()
    repo = VaultRepository()
    history = TaskHistory()

    with history.record("index", {
        "path": path, "type": media_type, "phased": phased,
    }) as run:
        if phased:
            from apps.topos_vault.pipeline.phased_orchestrator import PhasedOrchestrator
            orch = PhasedOrchestrator(repo)
            print("🔧 分阶段模式（大模型分时加载）")
        else:
            from apps.topos_vault.pipeline.orchestrator import Orchestrator
            orch = Orchestrator(repo)

        def progress(info):
            # 简洁的进度打印（兼容普通和分阶段进度信息）
            basename = os.path.basename(info.current_path)
            phase = getattr(info, "phase", "")
            phase_str = f"[{phase}] " if phase else ""
            print(f"  {phase_str}[{info.current}/{info.total}] ({info.current_step}) {basename[:40]}")

        if os.path.isfile(path):
            if phased:
                # 单文件分阶段：拿 dict 结果
                results_by_phase = orch.index_file(path, media_type=media_type, progress=progress)
                all_results = []
                for phase_results in results_by_phase.values():
                    all_results.extend(phase_results)
                batch = BatchReportAdapter(path, all_results)
                print(f"\n✅ {batch.summary()}")
            else:
                report = orch.index_file(path, media_type=media_type, progress=progress)
                batch = BatchReportAdapter(path, report)
                print(f"\n✅ {batch.summary()}")
        else:
            batch_report = orch.index_directory(path, media_type=media_type, progress=progress)
            print(f"\n✅ {batch_report.summary()}")
            # 把汇总写进任务历史
            run.total = batch_report.total
            run.done = batch_report.done
            run.skipped = batch_report.skipped
            run.failed = batch_report.failed

        # 分阶段模式跑完 unload 所有模型（腾显存）
        if phased:
            from apps.topos_vault.pipeline.phased_orchestrator import unload_all_providers
            unload_all_providers()

    return 0


def _cmd_history(args) -> int:
    """history：查看打标任务历史（P2-7）。"""
    from apps.topos_vault.db import schema
    from apps.topos_vault.task_history import TaskHistory

    schema.init_db()
    history = TaskHistory()

    if args.stats:
        stats = history.aggregate_stats()
        if stats["total_runs"] == 0:
            print("（暂无任务记录）")
            return 0
        print("=== 任务历史统计 ===")
        print(f"  总运行：{stats['total_runs']}")
        print(f"  成功：{stats['success']}（{stats['success_rate']*100:.0f}%）")
        print(f"  失败：{stats['failed']}")
        print(f"  平均耗时：{stats['avg_duration_sec']:.1f}s")
        print(f"  处理素材：{stats['done_items']}/{stats['total_items']}")
        return 0

    runs = history.list_runs(limit=args.limit)
    if not runs:
        print("（暂无任务记录）")
        return 0

    print(f"=== 最近 {len(runs)} 次任务 ===")
    for r in runs:
        status_icon = {"done": "✅", "failed": "❌", "running": "⏳"}.get(r.status, "?")
        dur = f"{r.duration_sec:.1f}s" if r.duration_sec is not None else "..."
        items = f" {r.done}/{r.total}" if r.total else ""
        print(f"  {status_icon} {r.started_at}  {r.command:<8s} {dur:>7s}{items}")
        if r.detail.get("error"):
            print(f"      错误：{r.detail['error']}")
    return 0


class BatchReportAdapter:
    """单文件结果适配成有 summary() 的对象（CLI 统一打印用）。"""

    def __init__(self, path: str, results: list):
        self._path = path
        self._results = results

    def summary(self) -> str:
        from apps.topos_vault.pipeline.steps import StepStatus
        done = sum(1 for r in self._results if r.status == StepStatus.DONE)
        skipped = sum(1 for r in self._results if r.status == StepStatus.SKIPPED)
        failed = sum(1 for r in self._results if r.status == StepStatus.FAILED)
        return f"{os.path.basename(self._path)}: {done} 完成 / {skipped} 跳过 / {failed} 失败"


def _cmd_relink(args) -> int:
    """relink：素材移动后重连。"""
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.relink import Relinker

    schema.init_db()
    repo = VaultRepository()
    relinker = Relinker(repo)

    # 先检测丢失
    missing = relinker.refresh_path_status()
    if missing == 0:
        print("✅ 没有丢失的素材（所有路径有效）")
        return 0

    print(f"检测到 {missing} 个素材路径丢失")
    print(f"在候选目录搜索：{args.dirs}")
    print()

    if args.dry_run:
        report = relinker.relink_missing(args.dirs)
        print(report.summary())
        print()
        print("（--dry-run，未写库）")
        return 0

    report = relinker.relink_missing(args.dirs)
    print(report.summary())
    return 0 if report.missed == 0 else 2


def _cmd_cluster(args) -> int:
    """cluster：人脸角色聚类。"""
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.character_cluster import (
        cluster_characters, _DEFAULT_EPS, _DEFAULT_MIN_SAMPLES,
    )

    schema.init_db()
    repo = VaultRepository()

    # --list：只列出已有聚类
    if args.list:
        clusters = repo.list_character_clusters()
        if not clusters:
            print("（暂无聚类，先跑 index 完成人脸检测，再跑 cluster）")
            return 0
        print(f"共 {len(clusters)} 个角色簇：")
        for c in clusters:
            name = c["character"] or "（未命名）"
            print(f"  簇 {c['cluster_id']:3d}  {name:12s}  {c['face_count']} 张脸")
        return 0

    # 跑聚类
    eps = args.eps if args.eps is not None else _DEFAULT_EPS
    min_samples = args.min_samples if args.min_samples is not None else _DEFAULT_MIN_SAMPLES
    result = cluster_characters(repo, eps=eps, min_samples=min_samples)

    if result is None:
        print("（无人脸向量，先跑 index --type video 完成人脸检测）")
        return 0

    print(f"✅ 聚类完成：{result.n_clusters} 个角色簇 / "
          f"{result.n_noise} 噪声 / {result.n_total} 张脸")
    print()
    print("角色名需人工指派。列出各簇：")
    print("  topos-vault cluster --list")
    return 0


def _cmd_providers(args) -> int:
    """providers：列出 AI 能力的 provider。"""
    from apps.topos_vault.providers import Capability, list_providers

    caps = [Capability(args.capability)] if args.capability else list(Capability)
    for cap in caps:
        try:
            cap_enum = Capability(cap) if isinstance(cap, str) else cap
        except ValueError:
            print(f"未知能力：{cap}", file=sys.stderr)
            return 1

        providers = list_providers(cap_enum)
        print(f"\n{cap_enum.value}:")
        if not providers:
            print("  （无 provider）")
            continue
        for p in providers:
            gpu = "[GPU]" if p.requires_gpu else "[CPU]"
            vram = f" min_vram={p.min_vram_mb}MB" if p.requires_gpu else ""
            print(f"  {gpu} {p.provider_id}")
            print(f"        {p.display_name}{vram}")
            if p.notes:
                print(f"        备注：{p.notes}")
    return 0


def _cmd_watch(args) -> int:
    """watch：监听目录变化，自动增量打标（P2-2）。"""
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.watcher import VaultWatcher, is_watchdog_available

    if not is_watchdog_available():
        print("错误：watchdog 未安装。pip install watchdog", file=sys.stderr)
        return 1

    schema.init_db()
    repo = VaultRepository()

    # --once：只做一次全量增量扫描（把监听目录里所有未打标的补上）
    if args.once:
        from apps.topos_vault.pipeline.orchestrator import Orchestrator
        orch = Orchestrator(repo)
        total = 0
        for d in args.dirs:
            if not os.path.isdir(d):
                continue
            print(f"扫描 {d}...")
            report = orch.index_directory(d, media_type="auto")
            total += report.total
            print(f"  {report.summary()}")
        print(f"\n✅ 增量扫描完成，共处理 {total} 个文件")
        return 0

    # 持续监听
    valid_dirs = [d for d in args.dirs if os.path.isdir(d)]
    if not valid_dirs:
        print("错误：没有有效的监听目录", file=sys.stderr)
        return 1

    watcher = VaultWatcher(repo, valid_dirs)
    watcher.start()
    print(f"✅ 开始监听 {len(valid_dirs)} 个目录（Ctrl+C 停止）")
    print(f"   {', '.join(valid_dirs)}")
    print("   新文件自动打标，移动自动重连，删除标记丢失")

    try:
        import time
        while True:
            time.sleep(10)
            s = watcher.stats
            print(f"  统计：新增 {s.created} 移动 {s.moved} 删除 {s.deleted} "
                  f"跳过 {s.skipped} 失败 {s.failed}")
    except KeyboardInterrupt:
        print("\n停止监听...")
        watcher.stop()
        s = watcher.stats
        print(f"✅ 已停止。新增 {s.created} 移动 {s.moved} 删除 {s.deleted} "
              f"跳过 {s.skipped} 失败 {s.failed}")
    return 0


def _cmd_serve(args) -> int:
    """serve：启动查询服务。"""
    from apps.topos_vault.server.app import run_server
    run_server(port=args.port, host=args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
