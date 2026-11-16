"""Files 域：原生素材浏览的契约与协调层。

迁移期数据权威仍是既有 Files 服务（磁盘 + topos.db）；本包只承载
transport 无关的 DTO、错误语义和导入协调，不复制目录扫描、SQL 或文件写入。
"""

from src.features.files.contracts import (
    Capability,
    Entry,
    FilesError,
    FilesErrorCode,
    JobFailure,
    JobProgress,
    JobResult,
    Location,
    LocationKind,
    QueryRequest,
    QueryResult,
    TechnicalMetadata,
    ThumbnailRef,
    UserMetadata,
    identity_key,
    normalize_path,
    parse_entry,
    parse_query_result,
)

__all__ = [
    "Capability",
    "Entry",
    "FilesError",
    "FilesErrorCode",
    "JobFailure",
    "JobProgress",
    "JobResult",
    "Location",
    "LocationKind",
    "QueryRequest",
    "QueryResult",
    "TechnicalMetadata",
    "ThumbnailRef",
    "UserMetadata",
    "identity_key",
    "normalize_path",
    "parse_entry",
    "parse_query_result",
]
