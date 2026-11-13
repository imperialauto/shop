"""Issue #150: no retired Groq model is used by the Python app."""
import os
import pathlib
import unittest
from unittest.mock import patch

from app import groq_models


class GroqModelTests(unittest.TestCase):
    def test_retired_ids_map_to_supported_models(self):
        with patch.dict(os.environ, {"GROQ_MODEL": "llama-3.3-70b-versatile"}):
            self.assertEqual(groq_models.text_model(), "openai/gpt-oss-120b")
        with patch.dict(os.environ, {"GROQ_MODEL": "", "GROQ_DRAFT_MODEL": ""}):
            self.assertEqual(groq_models.text_model(), "openai/gpt-oss-120b")
        with patch.dict(os.environ, {"GROQ_VISION_MODEL": "meta-llama/llama-4-scout-17b-16e-instruct"}):
            self.assertIsNone(groq_models.vision_model())

    def test_no_route_hard_codes_a_retired_model(self):
        root = pathlib.Path(__file__).resolve().parent.parent / "app"
        for path in root.rglob("*.py"):
            if path.name == "groq_models.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for retired in groq_models.RETIRED:
                self.assertNotIn(f'"{retired}"', text, f"{path.name} uses retired {retired}")


if __name__ == "__main__":
    unittest.main()
