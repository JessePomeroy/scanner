#!/usr/bin/env python3
"""Create a separate, verified low-poly Retro export from a textured scan OBJ."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.retro_export import export_retro_asset
from app.retro_style import RetroStyle, TEXTURE_SIZES, TRIANGLE_BUDGETS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, help="New output directory; existing results are never overwritten")
    parser.add_argument("--triangles", type=int, choices=TRIANGLE_BUDGETS, default=500)
    parser.add_argument("--texture-size", type=int, choices=TEXTURE_SIZES, default=256)
    parser.add_argument("--dither", action="store_true")
    args = parser.parse_args()
    result = export_retro_asset(args.source, args.destination,
                               RetroStyle(args.triangles, args.texture_size, args.dither), progress=lambda message: print(message, flush=True))
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
