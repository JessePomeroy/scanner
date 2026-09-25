"""Shared, explicit OpenMVS texture settings for plans and desktop recovery."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TextureSettings:
    # Native A/B tests on our OpenMVS 2.4 build reproduce black atlases with
    # either seam path enabled. Keep both off until a replacement is validated.
    global_seam_leveling: bool = False
    local_seam_leveling: bool = False
    resolution_level: int = 0

    def __post_init__(self):
        if type(self.resolution_level) is not int or self.resolution_level < 0:
            raise ValueError('Texture resolution level must be a non-negative integer')
        if type(self.global_seam_leveling) is not bool or type(self.local_seam_leveling) is not bool:
            raise ValueError('Texture seam settings must be booleans')

    def arguments(self) -> list[str]:
        return ['--resolution-level', str(self.resolution_level),
                '--global-seam-leveling', str(int(self.global_seam_leveling)),
                '--local-seam-leveling', str(int(self.local_seam_leveling))]

    def as_dict(self) -> dict[str, bool | int]:
        return asdict(self)
