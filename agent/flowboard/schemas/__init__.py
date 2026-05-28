"""Phase 2: Pydantic schemas for the Project / Scene / Shot REST surface.

Models live in the route-adjacent ``schemas`` package so the routes stay
thin and the test suite can import schemas directly to construct request
payloads without pulling in FastAPI.
"""
from .project import (
    ProjectBible,
    ProjectCreate,
    ProjectRead,
    ProjectReadDetail,
    ProjectUpdate,
)
from .scene import SceneCreate, SceneEstablishing, SceneRead, SceneReadDetail, SceneUpdate
from .shot import ShotCreate, ShotRead, ShotUpdate

__all__ = [
    "ProjectBible",
    "ProjectCreate",
    "ProjectRead",
    "ProjectReadDetail",
    "ProjectUpdate",
    "SceneEstablishing",
    "SceneCreate",
    "SceneRead",
    "SceneReadDetail",
    "SceneUpdate",
    "ShotCreate",
    "ShotRead",
    "ShotUpdate",
]
