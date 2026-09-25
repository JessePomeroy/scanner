#!/usr/bin/env python3
"""Decode a textured OBJ's images and screen used UVs; never claim visual approval."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.texture_quality import inspect_textured_obj


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('obj', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_textured_obj(args.obj), indent=2))


if __name__ == '__main__':
    main()
