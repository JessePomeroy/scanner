# Reconstruction Pipeline

The backend currently supports the command sequence needed for local
reconstruction:

1. `colmap feature_extractor`
2. `colmap exhaustive_matcher`
3. `colmap mapper`
4. `colmap image_undistorter`
5. `colmap patch_match_stereo`
6. `colmap stereo_fusion`
7. Optional OpenMVS mesh and texturing commands

Install COLMAP/OpenMVS locally before using reconstruction mode. Validation mode
does not require those native tools.
