from pathlib import Path
import sys
import tempfile
import unittest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.texture_quality import TextureQualityError, inspect_textured_obj, write_texture_report


class TextureQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.obj = self.root / 'asset.obj'
        self.obj.write_text('mtllib asset.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n'
                            'vt 0.1 0.1\nvt 0.3 0.1\nvt 0.1 0.3\nusemtl face\nf 1/1 2/2 3/3\n')
        (self.root / 'asset.mtl').write_text('newmtl face\nmap_Kd image.png\n')
        Image.new('RGB', (16, 16), (100, 130, 160)).save(self.root / 'image.png')

    def test_healthy_texture_is_decoded_but_never_visually_approved(self):
        report = write_texture_report(self.obj, self.root / 'quality.json')
        self.assertEqual(report['status'], 'checks_passed')
        self.assertEqual(report['near_black_fraction'], 0)
        self.assertEqual(report['faces'], 1)
        self.assertEqual(report['textures']['image.png']['size'], [16, 16])
        self.assertEqual(report['visual_quality'], 'manual review required')

    def test_black_used_region_warns_even_when_rest_of_atlas_is_bright(self):
        image = Image.new('RGB', (16, 16), (255, 255, 255))
        image.paste((0, 0, 0), (0, 11, 5, 16))
        image.save(self.root / 'image.png')
        report = inspect_textured_obj(self.obj)
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['near_black_fraction'], 1)

    def test_black_unused_atlas_does_not_raise_false_darkness_warning(self):
        image = Image.new('RGB', (16, 16), (0, 0, 0))
        image.paste((150, 150, 150), (0, 11, 5, 16))
        image.save(self.root / 'image.png')
        self.assertEqual(inspect_textured_obj(self.obj)['status'], 'checks_passed')

    def test_corrupt_image_cannot_publish_a_success_report(self):
        (self.root / 'image.png').write_bytes(b'not an image')
        with self.assertRaises(TextureQualityError):
            write_texture_report(self.obj, self.root / 'quality.json')
        self.assertFalse((self.root / 'quality.json').exists())

    def test_missing_uv_and_unsafe_texture_references_fail(self):
        valid_obj = self.obj.read_text()
        for face in ('f 1 2 3', 'f 1/0 2/2 3/3', 'f 1/8 2/2 3/3'):
            self.obj.write_text(self.obj.read_text().split('f ')[0] + face + '\n')
            with self.assertRaises(TextureQualityError):
                inspect_textured_obj(self.obj)
        self.obj.write_text(valid_obj)
        (self.root / 'asset.mtl').write_text('newmtl face\nmap_Kd ../escape.png\n')
        with self.assertRaisesRegex(TextureQualityError, 'local filenames'):
            inspect_textured_obj(self.obj)

    def test_samples_are_bounded_and_negative_indices_work(self):
        prefix = self.obj.read_text().split('f ')[0]
        self.obj.write_text(prefix + 'f 1/-3 2/-2 3/-1\n' * 100)
        report = inspect_textured_obj(self.obj, max_samples=7)
        self.assertEqual(report['faces'], 100)
        self.assertEqual(report['sample_count'], 7)

    def test_oversized_records_and_polygons_fail_with_bounded_input(self):
        valid = self.obj.read_text()
        self.obj.write_text('#' + 'x' * 65536 + '\n' + valid)
        with self.assertRaisesRegex(TextureQualityError, 'record exceeds'):
            inspect_textured_obj(self.obj)
        self.obj.write_text(valid.split('f ')[0] + 'f ' + '1/1 ' * 17 + '\n')
        with self.assertRaisesRegex(TextureQualityError, '16-corner'):
            inspect_textured_obj(self.obj)


if __name__ == '__main__':
    unittest.main()
