"""Приёмка фото прямо в папке памяти без изменения пользовательских файлов."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import publish_test_isolation  # noqa: E402, F401
import iphone_library as library  # noqa: E402
from PIL import Image  # noqa: E402


DIRECT_ID = "__memory__"


class DirectMemoryPackTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "iphone"
        self.memory_dir = self.root / "Iphone 17" / "256 GB"
        self.memory_dir.mkdir(parents=True)
        shared = self.root / "description"
        shared.mkdir()
        (shared / "iphone.txt").write_text(
            "{модель}, {память}, {цвет}, {SIM}", encoding="utf-8"
        )
        self.names = ["01.jpg", "02.png", "03.jpeg"]
        self._photos(self.memory_dir, self.names)

    def _photos(self, directory: Path, names: list[str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(names):
            Image.new("RGB", (80 + index, 60), (index * 10, 20, 30)).save(
                directory / name
            )

    def _catalog(self) -> dict:
        return library.catalog("iPhone 17", "256 ГБ", root=self.root)

    def _select(self, **changes: object) -> dict:
        values = dict(
            title="iPhone 17", memory="256 ГБ", color="Чёрный",
            sim_type="eSIM", drafts_count=2, pack_id=DIRECT_ID,
            root=self.root, seed=5,
        )
        values.update(changes)
        return library.select_materials(**values)

    def test_direct_photos_are_available_as_main_pack(self) -> None:
        listing = self._catalog()

        self.assertEqual(listing["error"], "")
        direct = next(pack for pack in listing["packs"] if pack["id"] == DIRECT_ID)
        self.assertEqual(direct["name"], "Основной")
        self.assertTrue(direct["available"])
        self.assertEqual(direct["photos_count"], 3)
        self.assertEqual({photo["id"] for photo in direct["photos"]}, set(self.names))

    def test_selection_uses_only_requested_model_and_memory_without_moving_files(self) -> None:
        self._photos(self.root / "Iphone 17" / "512 GB", ["other-memory.jpg"])
        self._photos(self.root / "Iphone 17 Pro" / "256 GB", ["other-model.jpg"])
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}

        result = self._select(photo_order=list(reversed(self.names)))

        self.assertEqual(result["model"], "iPhone 17")
        self.assertEqual(result["memory"], "256 ГБ")
        self.assertEqual(result["pack_id"], DIRECT_ID)
        self.assertEqual(result["photo_order"], list(reversed(self.names)))
        self.assertEqual(
            result["source_photos"],
            [self.memory_dir / name for name in reversed(self.names)],
        )
        self.assertEqual(len(result["descriptions"]), 2)
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob("*")})

    def test_direct_pack_preview_is_jpeg(self) -> None:
        preview = library.preview_photo(
            "iPhone 17", "256 ГБ", DIRECT_ID, "02.png", root=self.root
        )

        with Image.open(io.BytesIO(preview)) as picture:
            self.assertEqual(picture.format, "JPEG")
            picture.load()

    def test_legacy_named_pack_coexists_with_direct_photos(self) -> None:
        self._photos(self.memory_dir / "Второй пак", ["legacy.jpg"])

        listing = self._catalog()
        selected = self._select(pack_id="Второй пак")

        self.assertEqual(listing["error"], "")
        self.assertEqual({pack["id"] for pack in listing["packs"]}, {DIRECT_ID, "Второй пак"})
        self.assertTrue(all(pack["available"] for pack in listing["packs"]))
        self.assertEqual(selected["source_photos"], [self.memory_dir / "Второй пак" / "legacy.jpg"])

    def test_empty_memory_directory_is_unavailable(self) -> None:
        empty_root = self.root / "empty-library"
        (empty_root / "iPhone 17" / "256 GB").mkdir(parents=True)
        (empty_root / "description").mkdir()
        (empty_root / "description" / "iphone.txt").write_text("Описание", encoding="utf-8")

        listing = library.catalog("iPhone 17", "256 ГБ", root=empty_root)

        self.assertTrue(listing["error"])
        self.assertFalse(any(pack["available"] for pack in listing["packs"]))
        with self.assertRaises(ValueError):
            self._select(root=empty_root)

    def test_more_than_ten_direct_photos_make_pack_unavailable(self) -> None:
        self._photos(self.memory_dir, [f"extra-{index}.jpg" for index in range(8)])

        listing = self._catalog()

        self.assertTrue(listing["error"])
        direct = next(pack for pack in listing["packs"] if pack["id"] == DIRECT_ID)
        self.assertFalse(direct["available"])
        self.assertTrue(direct["error"])
        with self.assertRaises(ValueError):
            self._select()

    def test_pack_and_photo_traversal_identifiers_are_rejected(self) -> None:
        for invalid in ("..", "../256 GB", "..\\256 GB", "C:\\outside", "/outside"):
            with self.subTest(identifier=invalid):
                with self.assertRaises(ValueError):
                    self._select(pack_id=invalid)
                with self.assertRaises(ValueError):
                    library.preview_photo("iPhone 17", "256 ГБ", invalid, "01.jpg", root=self.root)
                with self.assertRaises(ValueError):
                    library.preview_photo("iPhone 17", "256 ГБ", DIRECT_ID, invalid, root=self.root)


if __name__ == "__main__":
    unittest.main()
