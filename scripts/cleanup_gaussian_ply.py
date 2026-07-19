#!/usr/bin/env python3
"""Create and verify a destructively cleaned Gaussian publication PLY."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.gaussian_cleanup import (  # noqa: E402
    GaussianCleanupError,
    cleanup_gaussian_ply,
    load_gaussian_cleanup_recipe,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Immutable source splat.ply")
    parser.add_argument("output", type=Path, help="Filtered publication PLY")
    parser.add_argument("--recipe", type=Path, required=True, help="Versioned cleanup recipe JSON")
    parser.add_argument("--report", type=Path, required=True, help="Cleanup evidence JSON")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing regular output/report files")
    args = parser.parse_args()

    try:
        output = cleanup_gaussian_ply(
            args.source,
            args.output,
            load_gaussian_cleanup_recipe(args.recipe),
            report_path=args.report,
            overwrite=args.overwrite,
        )
    except GaussianCleanupError as error:
        raise SystemExit(str(error)) from error
    print(output)


if __name__ == "__main__":
    main()
