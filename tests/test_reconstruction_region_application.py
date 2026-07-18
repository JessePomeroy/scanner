from __future__ import annotations

from pathlib import Path
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.reconstruction_region import ReconstructionRegion  # noqa: E402
from app.reconstruction_region_application import (  # noqa: E402
    ReconstructionRegionApplicationError,
    point_is_in_region,
    verify_point_cloud_in_region,
    write_openmvs_roi_file,
)


class ReconstructionRegionApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.region = ReconstructionRegion(
            center=(1.0, 2.0, 3.0),
            extents=(4.0, 2.0, 2.0),
            orientation_xyzw=(0.0, 0.0, 2 ** -0.5, 2 ** -0.5),
            source="user_sparse_preview",
            revision=1,
        )

    def test_writes_openmvs_world_to_local_obb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_openmvs_roi_file(root, self.region)
            rows = [[float(value) for value in line.split()] for line in path.read_text().splitlines()]

        self.assertEqual(len(rows), 5)
        for actual, expected in zip(rows[0], [0.0, 1.0, 0.0]):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(rows[1], [-1.0, 0.0, 0.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(rows[3], [1.0, 2.0, 3.0])
        self.assertEqual(rows[4], [2.0, 1.0, 1.0])

    def test_oriented_point_classification(self) -> None:
        self.assertTrue(point_is_in_region((1.0, 3.9, 3.0), self.region))
        self.assertFalse(point_is_in_region((3.1, 2.0, 3.0), self.region))

    def test_verifies_ascii_ply_with_face_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mesh.ply"
            path.write_text(
                "ply\nformat ascii 1.0\nelement vertex 3\n"
                "property float x\nproperty float y\nproperty float z\n"
                "element face 1\nproperty list uchar int vertex_indices\nend_header\n"
                "1 2 3\n1 3 3\n0 2 3\n3 0 1 2\n"
            )
            result = verify_point_cloud_in_region(path, self.region)

        self.assertEqual(result.point_count, 3)
        self.assertEqual(result.outside_point_count, 0)

    def test_verifies_binary_little_endian_ply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cloud.ply"
            path.write_bytes(
                b"ply\nformat binary_little_endian 1.0\nelement vertex 2\n"
                b"property float x\nproperty float y\nproperty float z\nend_header\n"
                + struct.pack("<ffffff", 1, 2, 3, 1, 3, 3)
            )
            result = verify_point_cloud_in_region(path, self.region)

        self.assertEqual(result.point_count, 2)

    def test_rejects_any_vertex_outside_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cloud.ply"
            path.write_text(
                "ply\nformat ascii 1.0\nelement vertex 2\n"
                "property float x\nproperty float y\nproperty float z\nend_header\n"
                "1 2 3\n99 99 99\n"
            )
            with self.assertRaisesRegex(ReconstructionRegionApplicationError, "1 of 2"):
                verify_point_cloud_in_region(path, self.region)


if __name__ == "__main__":
    unittest.main()
