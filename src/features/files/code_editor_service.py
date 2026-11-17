"""G5：code_editor 接管——等价编辑工具的文件服务（读取/编码/外部变更/
安全保存/恢复）。

G 节行验收：**读取/编码/外部变更/安全保存/恢复，新建与多文件入口；
不依赖旧 routes/JS globals**。纯 Python 域层（Qt 只在窗口层）：

* **读取/编码**：BOM 声明优先（utf-8-sig/utf-16-le/be）；无 BOM 按
  UTF-8 严格解码，失败降级 latin-1 并如实标注 ``declared=False``
  （绝不静默换码保存）；
* **外部变更**：载入快照（mtime_ns+size）；保存前比对——外部已变即
  拒绝（``EXTERNAL_CHANGE``），调用方选择"重载恢复"或"另存"；
* **安全保存**：写 ``.part`` + fsync + ``os.replace`` 原子改名——崩溃
  不会有半成品目标文件；保存后快照更新；
* **恢复**：``reload`` 从磁盘重读（外部变更后取回磁盘真值）；保存失败
  时 ``.part`` 清理，原文件不动。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


class EditorFileError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LoadedFile:
    path: str
    text: str
    encoding: str
    bom_declared: bool
    mtime_ns: int
    size_bytes: int

    def disk_matches(self) -> bool:
        try:
            info = os.stat(self.path)
        except OSError:
            return False
        return (info.st_mtime_ns == self.mtime_ns
                and info.st_size == self.size_bytes)


def read_file(path: str) -> LoadedFile:
    """读取并声明编码；损坏/缺失如实报错。"""
    target = Path(path)
    try:
        raw = target.read_bytes()
        info = target.stat()
    except FileNotFoundError as exc:
        raise EditorFileError("NOT_FOUND", f"文件不存在：{path}") from exc
    except OSError as exc:
        raise EditorFileError("IO_ERROR", f"无法读取：{exc}") from exc
    encoding, declared = "utf-8", False
    for bom, name in _BOMS:
        if raw.startswith(bom):
            encoding, declared = name, True
            break
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        encoding = "latin-1"
        text = raw.decode(encoding)
    if declared and encoding in ("utf-16-le", "utf-16-be") \
            and text.startswith("\ufeff"):
        text = text[1:]   # LE/BE 解码不吞 BOM——BOM 已单列声明，正文去掉
    return LoadedFile(path=str(target), text=text, encoding=encoding,
                      bom_declared=declared, mtime_ns=info.st_mtime_ns,
                      size_bytes=info.st_size)


def save_file(loaded: LoadedFile, text: str) -> LoadedFile:
    """安全保存：外部未变才写；``.part``+fsync+原子改名；返回新快照。"""
    if loaded.bom_declared is False and loaded.encoding == "latin-1":
        # 降级读取的内容按原字节域写回，不静默换码
        pass
    if loaded.path and not loaded.disk_matches():
        raise EditorFileError(
            "EXTERNAL_CHANGE",
            f"文件已被外部程序修改：{loaded.path}（重载或另存，不覆盖他人改动）")
    target = Path(loaded.path)
    part = target.with_name(target.name + ".part")
    try:
        payload = text.encode(loaded.encoding)
        with open(part, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, target)
    except OSError as exc:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        raise EditorFileError("IO_ERROR", f"保存失败（原文件未动）：{exc}") from exc
    info = target.stat()
    return LoadedFile(path=str(target), text=text, encoding=loaded.encoding,
                      bom_declared=loaded.bom_declared,
                      mtime_ns=info.st_mtime_ns, size_bytes=info.st_size)


def reload_file(loaded: LoadedFile) -> LoadedFile:
    """恢复：从磁盘重读（外部变更后取回磁盘真值）。"""
    return read_file(loaded.path)


def new_file_buffer(path: str = "") -> LoadedFile:
    """新建入口的空缓冲（无磁盘快照——首次保存前 path 可空）。"""
    return LoadedFile(path=path, text="", encoding="utf-8",
                      bom_declared=False, mtime_ns=0, size_bytes=0)


__all__ = ["read_file", "save_file", "reload_file", "new_file_buffer",
           "LoadedFile", "EditorFileError"]
