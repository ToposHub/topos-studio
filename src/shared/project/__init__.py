"""
Project Module
项目相关共享数据模型
"""

from src.shared.project.color_stack import ColorStackData
from src.shared.project.save_router import SaveRouter, SaveContext, SaveTarget
from src.shared.project.topos_project import ToposProject
from src.shared.project.manifest import ToposManifest, ProjectMode
from src.shared.project.topos_project_io import ToposProjectIO, ToposProjectIOConfig
from src.shared.project.topos_project_controller import ToposProjectController, PageType, DirtyState
from src.shared.project.schema_v1_spec import SCHEMA_REGISTRY_V1, get_schema, validate_json
from src.shared.project.payload_cache import ProjectArchivePayload

# Public protocol for cross-feature document save/load
from src.shared.project.document import ProjectDocument

__all__ = [
    "ColorStackData",
    "SaveRouter",
    "SaveContext",
    "SaveTarget",
    "ToposProject",
    "ToposManifest",
    "ProjectMode",
    "ToposProjectIO",
    "ToposProjectIOConfig",
    "ToposProjectController",
    "PageType",
    "DirtyState",
    "SCHEMA_REGISTRY_V1",
    "get_schema",
    "validate_json",
    # Module data preservation
    "ProjectArchivePayload",
    # Cross-feature protocol
    "ProjectDocument",
]