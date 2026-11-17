"""Files 新 repository 子包（B 阶段）：库定位器与新 catalog。"""
from src.features.files.repository.catalog import (
    CATALOG_SCHEMA_VERSION,
    CatalogError,
    CatalogSchemaMismatch,
    FilesCatalog,
    LibraryRecord,
)
from src.features.files.repository.locator import FilesDataLocator

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogError",
    "CatalogSchemaMismatch",
    "FilesCatalog",
    "FilesDataLocator",
    "LibraryRecord",
]
