"""Независимая регрессия: подготовка сохраняет модель и память в названиях."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import publish_test_isolation  # noqa: F401, E402
import iphone_library
import preparation
from category_profiles import IPHONE


class IphoneTitlesAcceptance(unittest.TestCase):
    def check_memory(self, capacity):
        model = "iPhone 17 Pro Max"
        memory = next(value for value in IPHONE.memory_for(model) if value.startswith(str(capacity)))
        title = f"{model} {capacity} GB"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            templates = root / model / "descriptions"
            templates.mkdir(parents=True)
            (templates / "01.txt").write_text("{модель}, {память}", encoding="utf-8")
            pack = root / model / f"{capacity} GB" / "pack"
            pack.mkdir(parents=True)
            Image.new("RGB", (80, 100), "red").save(pack / "01.png")
            materials = iphone_library.select_materials(
                title, memory, IPHONE.colors_for(model)[0], "eSIM", 20,
                root=root, seed=42,
            )
            photos = materials.pop("source_photos")
            prep_dir = Path(directory) / "prep"
            job = {}
            with patch.object(preparation.random, "randrange", return_value=42):
                asyncio.run(preparation.run_prep_job(
                    "test", job, title=title, description=materials["descriptions"][0],
                    source_photos=photos, drafts_count=20,
                    facts={"brand": "Apple", "condition": "Новое"},
                    base_dir=prep_dir, library=materials,
                ))
            self.assertEqual(job["status"], "done", job.get("error"))
            cards = preparation.build_result(prep_dir, 20)
            self.assertEqual(len(cards), 20)
            self.assertEqual(len({card["title"] for card in cards}), 20)
            for card in cards:
                with self.subTest(index=card["index"], title=card["title"]):
                    self.assertIn(f"{capacity} GB", card["title"])
                    self.assertEqual(
                        iphone_library.validate_title_memory(card["title"], memory), model,
                    )

    def test_512_gb_is_preserved_in_every_prepared_title(self):
        self.check_memory(512)

    def test_1024_gb_is_preserved_in_every_prepared_title(self):
        self.check_memory(1024)


if __name__ == "__main__":
    unittest.main()
