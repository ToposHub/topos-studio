"""模型导入持久缓存 —— 大模型二次打开不重复解析/转换/提取（Milestone E / P4）。

对应 master_plan_2026-07-07.md §8 (P4)。当前 Model3D/Scene3D 的导入缓存是
进程内 path+mtime（首次加载后驻留），但跨进程重启会丢，且 FBX fallback GLB
每次都用 TemporaryDirectory 重新跑 assimp CLI 转换（慢）。

本模块提供跨进程的磁盘缓存：
- import_cache_dir：~/Library/Caches/ToposColor/model_import_cache/（macOS）
  或 <temp>/ToposColor/model_import_cache/（其它平台兜底）。
- cache key：source_path + source_mtime_ns + source_size + 导入参数（normalize/
  flip_uv_v/axis/uv）+ 版本号 + 工具版本（assimp/trimesh）。
- manifest：每个缓存条目一个 json sidecar，记录 key 字段 + 缓存文件名 + 规模统计。
- 缓存内容（P4 第一阶段聚焦最高价值的两项）：
    1. FBX fallback GLB（P4-2）：assimp CLI 转换的 GLB 持久化，二次加载直接用；
       Model3D 另从 GLB accessor 恢复动画与蒙皮。
    2. 转换规模统计（P4-6）：mesh/triangle/material/texture 数，供 UI 显示。

设计原则（§8.5 风险）：
- 不用 pickle（安全）；用 json manifest + 原始 GLB/贴图文件。
- 缓存版本随数据结构升级递增（IMPORT_CACHE_VERSION）；版本不匹配直接失效。
- 源文件 mtime/size 变化自动失效（P4-5）；同路径覆盖后可靠失效。
- 缓存目录只存"派生文件"（GLB/贴图）；不缓存解析后的 SceneGraph（numpy 对象
  序列化复杂且易随版本失效，第一阶段不做 P4-4 的 SceneGraph npz 缓存）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# 缓存版本与目录
# ──────────────────────────────────────────────────────────────────────────

# 缓存数据结构版本。下列任一变化时递增：
# - manifest 字段含义改变
# - GLB 转换语义改变（assimp 版本/fork 不同输出）
# 升级后旧缓存自动失效（版本不匹配）。
IMPORT_CACHE_VERSION = 1

# 工具版本探针（best-effort；探不到用 "unknown"，仍可作为 key 一部分）。
def _assimp_version_tag() -> str:
    try:
        import shutil
        import subprocess
        bin_path = shutil.which("assimp")
        if bin_path:
            proc = subprocess.run(
                [bin_path, "version"], check=False, capture_output=True,
                text=True, timeout=5)
            out = (proc.stdout or proc.stderr or "").strip().splitlines()
            return out[0] if out else "assimp-cli"
    except Exception:
        pass
    return "unknown"


def _trimesh_version_tag() -> str:
    try:
        import trimesh
        return f"trimesh-{getattr(trimesh, '__version__', '?')}"
    except Exception:
        return "no-trimesh"


def _default_cache_dir() -> str:
    """缓存目录（§8.2）：macOS 用 ~/Library/Caches，其它平台用 temp 兜底。"""
    if sys.platform == "darwin":
        home = os.path.expanduser("~")
        return os.path.join(home, "Library", "Caches", "ToposColor",
                            "model_import_cache")
    # Linux/Windows：用 temp 目录下的稳定子目录
    import tempfile
    return os.path.join(tempfile.gettempdir(), "ToposColor", "model_import_cache")


# ──────────────────────────────────────────────────────────────────────────
# 缓存 Key
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ImportCacheKey:
    """稳定的导入缓存 key（§8.3）。

    包含源文件标识 + 导入参数 + 版本号。任一变化即视为不同缓存条目。
    hash 稳定（跨进程一致），用作缓存文件名前缀。
    """
    source_path: str
    source_mtime_ns: int
    source_size: int
    normalize: bool
    flip_uv_v: bool
    axis_mode: str
    uv_v_mode: str
    cache_version: int = IMPORT_CACHE_VERSION
    assimp_tag: str = ""
    trimesh_tag: str = ""

    def digest(self) -> str:
        """16 位 hash 摘要（用作缓存文件名前缀）。"""
        h = hashlib.md5(repr((
            self.source_path, self.source_mtime_ns, self.source_size,
            self.normalize, self.flip_uv_v, self.axis_mode, self.uv_v_mode,
            self.cache_version, self.assimp_tag, self.trimesh_tag,
        )).encode("utf-8"))
        return h.hexdigest()[:16]


def build_cache_key(source_path: str, normalize: bool, flip_uv_v: bool,
                    axis_mode: str = "auto", uv_v_mode: str = "auto",
                    assimp_tag: Optional[str] = None,
                    trimesh_tag: Optional[str] = None) -> ImportCacheKey:
    """从源文件 + 导入参数构建 ImportCacheKey。

    源文件不存在时抛 ValueError（调用方应先检查 path）。
    """
    if not source_path or not os.path.isfile(source_path):
        raise ValueError(f"源文件不存在: {source_path!r}")
    st = os.stat(source_path)
    return ImportCacheKey(
        source_path=os.path.abspath(source_path),
        source_mtime_ns=st.st_mtime_ns,
        source_size=st.st_size,
        normalize=bool(normalize),
        flip_uv_v=bool(flip_uv_v),
        axis_mode=str(axis_mode),
        uv_v_mode=str(uv_v_mode),
        cache_version=IMPORT_CACHE_VERSION,
        assimp_tag=assimp_tag or _assimp_version_tag(),
        trimesh_tag=trimesh_tag or _trimesh_version_tag(),
    )


# ──────────────────────────────────────────────────────────────────────────
# 缓存条目 Manifest
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class ImportCacheEntry:
    """单个缓存条目的 manifest（json sidecar）。

    对应一个 ImportCacheKey 的派生产物。记录缓存了哪些文件 + 规模统计。
    """
    digest: str                          # ImportCacheKey.digest()
    source_path: str
    source_mtime_ns: int
    source_size: int
    created_at: float                    # unix timestamp
    cache_version: int
    # 派生文件（相对 cache_dir 的文件名）
    fallback_glb: Optional[str] = None   # FBX→GLB fallback 产物（P4-2）
    # P4-3：嵌入贴图提取完成标记。True = 本次源文件版本已提取过 .fbm 贴图，
    # 可跳过重复提取（_persist_embedded_texture 的 PNG 重编码很慢）。
    # 源文件 mtime/size 变化时 key 变 → 自动重新提取。
    textures_extracted: bool = False
    # 规模统计（P4-6，供 UI 显示 cache hit/miss + 模型规模）
    mesh_count: int = 0
    triangle_count: int = 0
    material_count: int = 0
    texture_count: int = 0


# ──────────────────────────────────────────────────────────────────────────
# 缓存管理器
# ──────────────────────────────────────────────────────────────────────────

class ModelImportCache:
    """跨进程的模型导入磁盘缓存。

    用法::

        cache = ModelImportCache()           # 默认目录
        key = build_cache_key(path, normalize, flip)
        entry = cache.get(key)                # 命中返回 ImportCacheEntry，未命中 None
        if entry and entry.fallback_glb:
            glb_path = cache.file_path(entry.fallback_glb)
            # 直接加载 GLB，跳过 assimp CLI 转换
        else:
            # 正常转换，转换后 cache.put(key, fallback_glb=glb_path, ...)

    线程安全：单个 Model3D 节点的 render thread 访问；多节点共享一个 cache dir
    时 manifest 文件名含 digest，不会冲突。写 manifest 用 atomic rename 避免损坏。
    """

    MANIFEST_EXT = ".json"

    #: AUD-9001：磁盘缓存双预算——条目数与总字节数，超出按 created_at
    #: 最旧先驱逐（put 尾部触发）。历史 put 无任何限制，源文件每次
    #: 版本迭代（mtime 变 → 新 digest）都追加 GLB+贴图副本，重度 3D
    #: 使用会无限累积。
    MAX_ENTRIES = 64
    MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

    def __init__(self, cache_dir: Optional[str] = None, enabled: bool = True) -> None:
        self._cache_dir = cache_dir or _default_cache_dir()
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def cache_dir(self) -> str:
        return self._cache_dir

    def _ensure_dir(self) -> None:
        """惰性创建缓存目录（首次写入时）。"""
        if not os.path.isdir(self._cache_dir):
            try:
                os.makedirs(self._cache_dir, exist_ok=True)
            except OSError as exc:
                logger.warning("无法创建模型导入缓存目录 %s: %s",
                               self._cache_dir, exc)

    def _manifest_path(self, digest: str) -> str:
        return os.path.join(self._cache_dir, f"import_{digest}{self.MANIFEST_EXT}")

    def file_path(self, rel_name: str) -> str:
        """缓存文件相对名 → 绝对路径。"""
        return os.path.join(self._cache_dir, rel_name)

    def get(self, key: ImportCacheKey) -> Optional[ImportCacheEntry]:
        """查找缓存条目。命中且文件存在返回 ImportCacheEntry；否则 None。

        失效检查（P4-5）：
        - manifest 缺失/损坏 → None
        - cache_version 不匹配 → 删除旧条目 + None
        - source_mtime_ns/size 与当前源文件不一致 → 删除 + None
        - 引用的派生文件（fallback_glb）不存在 → 删除 + None
        """
        if not self._enabled:
            return None
        mpath = self._manifest_path(key.digest())
        if not os.path.isfile(mpath):
            return None
        try:
            with open(mpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        # 版本失效
        if int(data.get("cache_version", 0)) != IMPORT_CACHE_VERSION:
            self._purge(key.digest())
            return None
        # 源文件变化失效（mtime_ns + size）
        if (int(data.get("source_mtime_ns", 0)) != key.source_mtime_ns
                or int(data.get("source_size", 0)) != key.source_size):
            self._purge(key.digest())
            return None
        entry = ImportCacheEntry(
            digest=data["digest"],
            source_path=data.get("source_path", ""),
            source_mtime_ns=int(data.get("source_mtime_ns", 0)),
            source_size=int(data.get("source_size", 0)),
            created_at=float(data.get("created_at", 0.0)),
            cache_version=int(data.get("cache_version", IMPORT_CACHE_VERSION)),
            fallback_glb=data.get("fallback_glb"),
            textures_extracted=bool(data.get("textures_extracted", False)),
            mesh_count=int(data.get("mesh_count", 0)),
            triangle_count=int(data.get("triangle_count", 0)),
            material_count=int(data.get("material_count", 0)),
            texture_count=int(data.get("texture_count", 0)),
        )
        # 引用的派生文件必须存在（否则条目无效）
        if entry.fallback_glb:
            if not os.path.isfile(self.file_path(entry.fallback_glb)):
                self._purge(key.digest())
                return None
        return entry

    def put(self, key: ImportCacheKey, *,
            fallback_glb: Optional[str] = None,
            textures_extracted: bool = False,
            mesh_count: int = 0, triangle_count: int = 0,
            material_count: int = 0, texture_count: int = 0) -> ImportCacheEntry:
        """写入/更新缓存条目。fallback_glb 若给的是绝对路径会被复制进缓存目录。

        textures_extracted=True 标记本次源文件版本已提取嵌入贴图（P4-3），
        下次同 key 加载可跳过 _persist_embedded_texture 的 PNG 重编码。

        返回写入的 ImportCacheEntry。若缓存禁用或写入失败，返回 entry 但不落盘
        （调用方仍可正常加载，只是没缓存加速）。
        """
        entry = ImportCacheEntry(
            digest=key.digest(),
            source_path=key.source_path,
            source_mtime_ns=key.source_mtime_ns,
            source_size=key.source_size,
            created_at=time.time(),
            cache_version=IMPORT_CACHE_VERSION,
            textures_extracted=bool(textures_extracted),
            mesh_count=mesh_count, triangle_count=triangle_count,
            material_count=material_count, texture_count=texture_count,
        )
        if not self._enabled:
            return entry
        # 确保缓存目录存在（派生文件入库 + manifest 写入都需要）
        self._ensure_dir()
        # 派生文件入库
        if fallback_glb and os.path.isfile(fallback_glb):
            rel = self._ingest_file(fallback_glb, key.digest(), ".glb")
            entry.fallback_glb = rel
        # manifest 落盘（atomic：写临时文件再 rename）
        self._write_manifest(entry)
        # AUD-9001：写入后执行预算驱逐（新条目一并计入统计）
        self._evict_over_budget()
        return entry

    def _evict_over_budget(self) -> None:
        """条目数/总字节超预算时按 created_at 最旧先驱逐（AUD-9001）。

        解析失败的损坏条目视作最旧（created_at=-1）优先清除。
        """
        import glob as _glob
        try:
            manifest_paths = _glob.glob(os.path.join(
                self._cache_dir, f"import_*{self.MANIFEST_EXT}"))
        except OSError:
            return
        entries: list[tuple[float, str, int]] = []  # (created_at, digest, bytes)
        total = 0
        for mpath in manifest_paths:
            digest = os.path.basename(mpath)[len("import_"):-len(self.MANIFEST_EXT)]
            created = -1.0
            size = 0
            try:
                size = os.path.getsize(mpath)
                with open(mpath, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                created = float(data.get("created_at", 0.0))
                if data.get("fallback_glb"):
                    derived = self.file_path(str(data["fallback_glb"]))
                    if os.path.isfile(derived):
                        size += os.path.getsize(derived)
            except (OSError, ValueError):
                created = -1.0  # 损坏条目：最优先驱逐
            entries.append((created, digest, size))
            total += size
        count = len(entries)
        if count <= self.MAX_ENTRIES and total <= self.MAX_TOTAL_BYTES:
            return
        entries.sort(key=lambda item: item[0])
        for _created, digest, size in entries:
            if count <= 1:
                # 至少保留最新一条：单个条目自身超预算不应写完即被自逐
                break
            if (count <= self.MAX_ENTRIES
                    and total <= self.MAX_TOTAL_BYTES):
                break
            self._purge(digest)
            count -= 1
            total -= max(0, size)

    def _ingest_file(self, src_abs: str, digest: str, ext: str) -> str:
        """把派生文件复制进缓存目录，返回相对名。"""
        rel = f"{digest}{ext}"
        dst = self.file_path(rel)
        try:
            import shutil
            shutil.copyfile(src_abs, dst)
        except OSError as exc:
            logger.warning("缓存派生文件失败 %s: %s", src_abs, exc)
            return ""
        return rel

    def _write_manifest(self, entry: ImportCacheEntry) -> None:
        """原子写 manifest（临时文件 + rename，避免损坏）。"""
        mpath = self._manifest_path(entry.digest)
        tmp = mpath + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({
                    "digest": entry.digest,
                    "source_path": entry.source_path,
                    "source_mtime_ns": entry.source_mtime_ns,
                    "source_size": entry.source_size,
                    "created_at": entry.created_at,
                    "cache_version": entry.cache_version,
                    "fallback_glb": entry.fallback_glb,
                    "textures_extracted": entry.textures_extracted,
                    "mesh_count": entry.mesh_count,
                    "triangle_count": entry.triangle_count,
                    "material_count": entry.material_count,
                    "texture_count": entry.texture_count,
                }, fh)
            os.replace(tmp, mpath)
        except OSError as exc:
            logger.warning("写缓存 manifest 失败 %s: %s", mpath, exc)
            try: os.remove(tmp)
            except OSError: pass

    def _purge(self, digest: str) -> None:
        """删除某条目（manifest + 引用的派生文件）。"""
        mpath = self._manifest_path(digest)
        # 先读 manifest 拿派生文件名
        derived = []
        if os.path.isfile(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("fallback_glb"):
                    derived.append(data["fallback_glb"])
            except (OSError, ValueError):
                pass
        for rel in derived:
            try: os.remove(self.file_path(rel))
            except OSError: pass
        try: os.remove(mpath)
        except OSError: pass

    def clear(self) -> int:
        """清空整个缓存目录（用户手动清理用）。返回删除的条目数。"""
        removed = 0
        if not os.path.isdir(self._cache_dir):
            return 0
        for name in os.listdir(self._cache_dir):
            p = os.path.join(self._cache_dir, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        return removed


# 模块级单例（Model3D/Scene3D 共享）。可被测试替换。
_default_cache: Optional[ModelImportCache] = None


def get_default_cache() -> ModelImportCache:
    """获取模块级默认缓存实例（惰性创建）。"""
    global _default_cache
    if _default_cache is None:
        _default_cache = ModelImportCache()
    return _default_cache
