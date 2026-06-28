#!/usr/bin/env python3
"""Crop a PLY point cloud by a center point and radius."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--center", type=float, nargs=3, required=True, metavar=("X", "Y", "Z"))
    parser.add_argument("--radius", type=float, required=True)
    args = parser.parse_args()

    try:
        import numpy as np
        import open3d as o3d
    except ImportError as error:
        raise SystemExit("crop_point_cloud.py requires numpy and open3d") from error

    if args.radius <= 0:
        raise SystemExit("--radius must be positive")

    pcd = o3d.io.read_point_cloud(str(args.input))
    points = np.asarray(pcd.points)
    center = np.asarray(args.center)
    distances = np.linalg.norm(points - center, axis=1)
    keep_indices = np.where(distances <= args.radius)[0]

    cropped = pcd.select_by_index(keep_indices)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    o3d.io.write_point_cloud(str(args.output), cropped)

    print(f"kept={len(keep_indices)} total={len(points)} output={args.output}")


if __name__ == "__main__":
    main()
