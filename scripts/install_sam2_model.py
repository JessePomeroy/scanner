#!/usr/bin/env python3
"""Install only the pinned SAM2 checkpoint; never overwrite an existing model."""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.sam2_generator import SAM2_CHECKPOINT_URL, Sam2Runtime, verify_checkpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Reuse an existing copy after SHA256 validation instead of downloading")
    args = parser.parse_args()
    destination = Sam2Runtime.configured().checkpoint.absolute()
    if destination.exists() or destination.is_symlink():
        verify_checkpoint(destination)
        print(f"Pinned checkpoint already installed: {destination}")
        return
    if args.source is not None:
        verify_checkpoint(args.source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        stream = args.source.open("rb") if args.source is not None else urlopen(SAM2_CHECKPOINT_URL, timeout=60)
        with closing(stream), tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".sam2-", delete=False) as output:
            temporary = Path(output.name)
            size = 0
            while chunk := stream.read(1024*1024):
                size += len(chunk)
                if size > 512*1024*1024:
                    raise ValueError("Checkpoint download exceeded its size bound")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        verify_checkpoint(temporary)
        # Link publishes without replacement, including when two installers race.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            verify_checkpoint(destination)
        print(f"Pinned checkpoint installed: {destination}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
