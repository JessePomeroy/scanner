"""Explicit policy for which reconstruction stages consume reviewed masks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


MaskProfileName = Literal["scene_geometry", "object_foreground"]


@dataclass(frozen=True)
class MaskStageProfile:
    name: MaskProfileName
    description: str
    colmap_features: bool
    colmap_stereo_fusion: bool
    openmvs_densification: bool
    openmvs_texturing: bool

    def as_dict(
        self,
        *,
        capture_masks_available: bool,
        dense_masks_available: bool,
    ) -> dict[str, Any]:
        """Return the effective, JSON-safe stage policy for processing reports."""
        return {
            "name": self.name,
            "description": self.description,
            "capture_masks_available": capture_masks_available,
            "dense_masks_available": dense_masks_available,
            "stages": {
                "colmap_features": capture_masks_available and self.colmap_features,
                "colmap_stereo_fusion": dense_masks_available and self.colmap_stereo_fusion,
                "openmvs_densification": dense_masks_available and self.openmvs_densification,
                "openmvs_texturing": dense_masks_available and self.openmvs_texturing,
            },
        }


SCENE_GEOMETRY_PROFILE = MaskStageProfile(
    name="scene_geometry",
    description=(
        "Keep full images for stable scene alignment; apply reviewed masks to dense "
        "fusion, OpenMVS geometry, and texture selection."
    ),
    colmap_features=False,
    colmap_stereo_fusion=True,
    openmvs_densification=True,
    openmvs_texturing=True,
)

OBJECT_FOREGROUND_PROFILE = MaskStageProfile(
    name="object_foreground",
    description=(
        "Constrain alignment, dense geometry, and texture selection to the reviewed "
        "foreground for object or turntable captures."
    ),
    colmap_features=True,
    colmap_stereo_fusion=True,
    openmvs_densification=True,
    openmvs_texturing=True,
)

_PROFILES: dict[MaskProfileName, MaskStageProfile] = {
    SCENE_GEOMETRY_PROFILE.name: SCENE_GEOMETRY_PROFILE,
    OBJECT_FOREGROUND_PROFILE.name: OBJECT_FOREGROUND_PROFILE,
}


def mask_stage_profile(name: MaskProfileName) -> MaskStageProfile:
    """Resolve a validated API profile name to its immutable stage policy."""
    return _PROFILES[name]
