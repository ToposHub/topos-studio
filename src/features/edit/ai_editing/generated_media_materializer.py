"""生成媒体物化器（M11 provider-neutral 文件接缝）。

生成 provider 只需要返回本地路径、``file://`` 或 HTTP(S) 预览句柄；本模块负责把
句柄转换成项目可导入的本地媒体文件。缓存是可重建数据，不进入工程 JSON。

安全边界：

- 只接受本地文件、file://、http://、https://；其它 scheme 明确拒绝；
- 下载有 Content-Length / 实际字节数上限；
- 先写同目录临时文件，再原子替换，失败不会留下半个媒体文件；
- 文件名只由 asset_id 和 URL 后缀组成，不信任远端 Content-Disposition。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.features.edit.ai_editing.generation_models import GeneratedAsset
from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobRegistry
from src.features.edit.ai_editing.model_pack_manager import default_cache_dir


logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class MaterializeResult:
    """生成句柄物化结果；失败时 ``path`` 为空。"""

    path: str | None
    message: str
    cancelled: bool = False
    sha256: str = ""
    mime_type: str = ""
    size_bytes: int = 0

    @property
    def succeeded(self) -> bool:
        return bool(self.path)


class GeneratedMediaMaterializer:
    """把生成候选的 provider 句柄转换成可导入的本地文件。"""

    def __init__(
        self,
        cache_dir: str | os.PathLike[str] | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        opener: Callable[..., Any] | None = None,
        timeout: float = 60.0,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._cache_dir = Path(cache_dir) if cache_dir is not None else (
            default_cache_dir() / "generated_media"
        )
        self._max_bytes = int(max_bytes)
        self._opener = opener or urllib.request.urlopen
        self._timeout = float(timeout)

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def materialize(self, asset: GeneratedAsset) -> MaterializeResult:
        """物化一个候选的预览句柄。"""
        return self._materialize(asset)

    def materialize_with_progress(
        self,
        asset: GeneratedAsset,
        *,
        progress: Callable[[float, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> MaterializeResult:
        """物化候选，并在下载过程中报告进度/协作取消。"""
        return self._materialize(asset, progress=progress, is_cancelled=is_cancelled)

    def _materialize(
        self,
        asset: GeneratedAsset,
        *,
        progress: Callable[[float, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> MaterializeResult:
        if is_cancelled is not None and is_cancelled():
            return MaterializeResult(None, "生成媒体下载已取消", cancelled=True)
        handle = str(asset.preview_handle or "").strip()
        if not handle:
            return MaterializeResult(None, "provider 尚未提供媒体句柄")
        if handle.startswith("stub://"):
            return MaterializeResult(None, "当前 provider 只提供虚拟句柄，尚无可导入文件")

        parsed = urllib.parse.urlparse(handle)
        # Windows 盘符路径（C:\… / C:/…）被 urlparse 误判为单字母 scheme
        # （'c'）→ 误报"不支持的媒体句柄协议"。盘符 + 绝对路径还原为
        # scheme=''，走下方本地文件分支（macOS/Linux 无单字母 scheme
        # 绝对路径，行为不变）。
        if (
            parsed.scheme
            and len(parsed.scheme) == 1
            and parsed.scheme.isalpha()
            and os.path.isabs(handle)
        ):
            parsed = parsed._replace(scheme="")
        if parsed.scheme in ("", "file"):
            path = self._local_path(handle, parsed)
            if path is not None and os.path.isfile(path):
                if progress is not None:
                    progress(1.0, "已复用本地生成媒体")
                return self._with_metadata(
                    path, "已复用本地生成媒体", expected_sha256=asset.expected_sha256,
                )
            return MaterializeResult(None, "生成媒体本地文件不存在")
        if parsed.scheme in ("http", "https"):
            return self._download(
                asset, parsed, progress=progress, is_cancelled=is_cancelled,
            )
        return MaterializeResult(None, f"不支持的媒体句柄协议：{parsed.scheme}")

    @staticmethod
    def _local_path(handle: str, parsed: urllib.parse.ParseResult) -> str | None:
        if parsed.scheme == "file":
            if parsed.netloc not in ("", "localhost"):
                return None
            return urllib.parse.unquote(parsed.path)
        return handle

    def _download(
        self,
        asset: GeneratedAsset,
        parsed: urllib.parse.ParseResult,
        *,
        progress: Callable[[float, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> MaterializeResult:
        url = urllib.parse.urlunparse(parsed)
        suffix = self._suffix(parsed)
        target = self._cache_dir / f"{self._safe_id(asset.asset_id)}{suffix}"
        try:
            if target.is_file() and target.stat().st_size > 0:
                # AUD-13504：缓存命中优先走 sidecar 记录（size+sha256），跳过
                # 全文重哈希——512MB 上限的视频每次复用预览时整段重哈希是秒级
                # 阻塞。仅当调用方给的 expected 与记录不一致时才回退全文重算，
                # 保持"复用前必须校验"的既有 fail-closed 语义。
                cached = self._cached_result_from_sidecar(
                    target, "已复用生成媒体缓存", expected_sha256=asset.expected_sha256,
                )
                if cached is None:
                    cached = self._with_metadata(
                        str(target), "已复用生成媒体缓存", expected_sha256=asset.expected_sha256,
                    )
                if cached.succeeded:
                    if progress is not None:
                        progress(1.0, "已复用生成媒体缓存")
                    return cached
                target.unlink(missing_ok=True)
        except OSError:
            pass

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        temp_path: str | None = None
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Topos-Color-AI-Materializer/1.0"},
            )
            response = self._opener(request, timeout=self._timeout)
            with response:
                # Content-Length 是服务器响应头（外部数据）：非数字或非正数时视为
                # 未知长度，交给逐块 max_bytes 截断兜底，绝不能作为除数。
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(raw_length) if raw_length else None
                except ValueError:
                    content_length = None
                if content_length is not None and content_length <= 0:
                    content_length = None
                if content_length is not None and content_length > self._max_bytes:
                    return MaterializeResult(None, "远程媒体超过本地缓存大小上限")
                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{self._safe_id(asset.asset_id)}.",
                    suffix=suffix or ".bin",
                    dir=self._cache_dir,
                )
                written = 0
                with os.fdopen(fd, "wb") as output:
                    while True:
                        if is_cancelled is not None and is_cancelled():
                            return MaterializeResult(None, "生成媒体下载已取消", cancelled=True)
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self._max_bytes:
                            return MaterializeResult(None, "远程媒体超过本地缓存大小上限")
                        output.write(chunk)
                        if progress is not None and content_length:
                            progress(
                                min(0.99, written / content_length),
                                "正在下载生成媒体",
                            )
                    output.flush()
                    os.fsync(output.fileno())
                if written <= 0:
                    return MaterializeResult(None, "远程媒体为空")
            os.replace(temp_path, target)
            temp_path = None
            if progress is not None:
                progress(1.0, "已下载并缓存生成媒体")
            result = self._with_metadata(
                str(target), "已下载并缓存生成媒体", expected_sha256=asset.expected_sha256,
            )
            if not result.succeeded:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                # AUD-13504：下载成功后把 <size, sha256, mime> 写成 sidecar，
                # 之后的缓存复用按记录跳过全文重哈希。写入失败不影响本次结果
                # （下次复用退回全文重算的保守路径）。
                self._write_sidecar(target, result)
            return result
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            return MaterializeResult(None, f"生成媒体下载失败：{type(exc).__name__}")
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _safe_id(asset_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in asset_id)
        return safe[:100] or "generated-asset"

    @staticmethod
    def _sidecar_path(target: Path) -> Path:
        return target.with_name(target.name + ".meta.json")

    @classmethod
    def _write_sidecar(cls, target: Path, result: MaterializeResult) -> None:
        """Persist the just-computed checksum so cache reuse can skip re-hashing."""
        try:
            cls._sidecar_path(target).write_text(
                json.dumps({
                    "schema_version": 1,
                    "size_bytes": int(result.size_bytes),
                    "sha256": str(result.sha256),
                    "mime_type": str(result.mime_type),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "generated media sidecar write failed for %s: %s", target.name, exc,
            )

    @staticmethod
    def _load_sidecar(target: Path) -> dict[str, Any] | None:
        """Read the sidecar record; malformed/missing records return None."""
        try:
            raw = json.loads(
                GeneratedMediaMaterializer._sidecar_path(target).read_text(
                    encoding="utf-8",
                ),
            )
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or int(raw.get("schema_version", 0) or 0) != 1:
            return None
        size = raw.get("size_bytes")
        digest = raw.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool):
            return None
        if not isinstance(digest, str) or not digest:
            return None
        return {
            "size_bytes": size,
            "sha256": digest.lower(),
            "mime_type": (
                str(raw["mime_type"]) if isinstance(raw.get("mime_type"), str) else ""
            ),
        }

    @classmethod
    def _cached_result_from_sidecar(
        cls, target: Path, message: str, *, expected_sha256: str = "",
    ) -> MaterializeResult | None:
        """Build a reuse result from the sidecar without re-hashing the file.

        Returns ``None`` when the sidecar is missing/invalid, when the recorded
        size no longer matches the file, or when the caller's ``expected_sha256``
        disagrees with the record — the caller then falls back to a full re-hash
        (fail-closed).  A matching record short-circuits the O(file-size) digest.
        """
        record = cls._load_sidecar(target)
        if record is None:
            return None
        try:
            size = target.stat().st_size
        except OSError:
            return None
        if size <= 0 or size != int(record["size_bytes"]):
            return None
        digest = str(record["sha256"])
        expected = expected_sha256.strip().lower()
        if expected.startswith("sha256:"):
            expected = expected[7:]
        if expected and expected != digest:
            return None
        return MaterializeResult(
            str(target), message, sha256=digest,
            mime_type=str(record["mime_type"]), size_bytes=size,
        )

    @staticmethod
    def _with_metadata(
        path: str, message: str, *, expected_sha256: str = "",
    ) -> MaterializeResult:
        """计算可审计的 checksum/MIME；不把绝对路径写入结果序列化合同。"""
        digest = hashlib.sha256()
        size = 0
        try:
            with open(path, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            expected = expected_sha256.strip().lower()
            if expected.startswith("sha256:"):
                expected = expected[7:]
            if expected and digest.hexdigest().lower() != expected:
                return MaterializeResult(
                    None, "生成媒体 checksum 校验失败", sha256=digest.hexdigest(),
                    mime_type=mime_type, size_bytes=size,
                )
            return MaterializeResult(
                path, message, sha256=digest.hexdigest(),
                mime_type=mime_type, size_bytes=size,
            )
        except OSError:
            return MaterializeResult(None, "物化文件无法读取以进行校验")

    @staticmethod
    def _suffix(parsed: urllib.parse.ParseResult) -> str:
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        if suffix and len(suffix) <= 10 and suffix[1:].isalnum():
            return suffix
        guessed, _ = mimetypes.guess_type(parsed.path)
        if guessed:
            guessed_suffix = mimetypes.guess_extension(guessed)
            if guessed_suffix:
                return guessed_suffix
        return ""


class AsyncGeneratedMediaMaterializer:
    """在 daemon worker 中执行物化，并把状态写入共享 JobRegistry。"""

    def __init__(
        self,
        materializer: GeneratedMediaMaterializer,
        jobs: JobRegistry,
    ) -> None:
        self._materializer = materializer
        self._jobs = jobs

    def start(
        self,
        asset: GeneratedAsset,
        *,
        on_progress: Callable[[JobHandle, float, str], None] | None = None,
        on_completed: Callable[[JobHandle, MaterializeResult], None] | None = None,
    ) -> JobHandle:
        job = self._jobs.submit(JobKind.GENERATION, label=f"物化生成媒体 {asset.asset_id}")

        def worker() -> None:
            job.mark_running()

            def report(fraction: float, message: str) -> None:
                job.update_progress(fraction, message)
                if on_progress is not None:
                    on_progress(job, fraction, message)

            try:
                result = self._materializer.materialize_with_progress(
                    asset,
                    progress=report,
                    is_cancelled=lambda: job.status.value == "cancelling",
                )
                if result.cancelled or job.status.value == "cancelling":
                    job.complete({"asset_id": asset.asset_id, "cancelled": True})
                elif result.succeeded:
                    job.complete({"asset_id": asset.asset_id, "path": result.path})
                else:
                    job.fail("MATERIALIZE_FAILED", result.message)
                if on_completed is not None:
                    on_completed(job, result)
            except Exception as exc:  # noqa: BLE001
                if job.status.value == "cancelling":
                    job.complete({"asset_id": asset.asset_id, "cancelled": True})
                else:
                    job.fail("MATERIALIZE_EXCEPTION", type(exc).__name__)
                if on_completed is not None:
                    on_completed(
                        job,
                        MaterializeResult(
                            None,
                            "生成媒体下载已取消"
                            if job.status.value == "cancelled"
                            else "生成媒体物化失败",
                            cancelled=job.status.value == "cancelled",
                        ),
                    )

        threading.Thread(
            target=worker,
            name=f"topos-generated-materialize-{job.job_id[:8]}",
            daemon=True,
        ).start()
        return job


__all__ = [
    "AsyncGeneratedMediaMaterializer", "DEFAULT_MAX_BYTES",
    "GeneratedMediaMaterializer", "MaterializeResult",
]
