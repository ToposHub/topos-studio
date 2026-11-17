"""Files 领域服务包（C 阶段）：类型化同步服务 + 异步 facade。"""
from src.features.files.services.domain import (
    FilesCollectionsService,
    FilesMetadataService,
    FilesScanService,
    FilesTaskService,
)
from src.features.files.services.query import (
    FilesLibraryService,
    FilesQueryService,
    classify,
)

__all__ = [
    "FilesCollectionsService",
    "FilesLibraryService",
    "FilesMetadataService",
    "FilesQueryService",
    "FilesScanService",
    "FilesTaskService",
    "classify",
]
