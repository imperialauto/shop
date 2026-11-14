"""The public "Our work" gallery lists only committed, captioned photos."""
import os
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient

from app.gallery import PHOTOS
from app.legacy_redirects import LEGACY_PATHS
from app.main import app

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static", "gallery")


class OurWorkGallery(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_page_shows_every_photo_with_alt_text(self):
        res = self.client.get("/site/our-work")
        self.assertEqual(res.status_code, 200)
        for photo in PHOTOS:
            self.assertIn(f'src="/static/gallery/thumb-{photo.file}"', res.text)
            self.assertIn(f'alt="{photo.caption}"', res.text)

    def test_every_photo_and_thumbnail_is_committed(self):
        for photo in PHOTOS:
            for name in (photo.file, "thumb-" + photo.file):
                with self.subTest(file=name):
                    self.assertTrue(os.path.isfile(os.path.join(STATIC, name)))

    def test_photos_carry_no_location_metadata(self):
        # No EXIF block means no GPS coordinates ride along with the photo.
        for photo in PHOTOS:
            for name in (photo.file, "thumb-" + photo.file):
                with self.subTest(file=name), open(os.path.join(STATIC, name), "rb") as fh:
                    self.assertNotIn(b"Exif\x00\x00", fh.read())

    def test_nav_links_the_gallery_and_old_link_lands_there(self):
        self.assertIn('href="/site/our-work"', self.client.get("/site/").text)
        self.assertEqual(LEGACY_PATHS["/ourwork"], "/site/our-work")


if __name__ == "__main__":
    unittest.main()
