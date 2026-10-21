"""Описание сохраняет абзацы при подготовке и правке черновиков."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import preparation  # noqa: E402


class PrepareDescriptionNewlinesTest(unittest.TestCase):
    DESCRIPTION = (
        "Худи  Stussy, размер 48.\n"
        "  Замеры:  длина 70 см.\n\n"
        "Телефон: +7 (999) 123-45-67\n"
        "Доставка  по договорённости."
    )

    def setUp(self) -> None:
        temporary_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_dir.cleanup)
        self.root = Path(temporary_dir.name)
        self.prep_dir = self.root / "prep_newlines"
        self.photo = self.root / "photo.png"
        Image.new("RGB", (80, 80), color=(120, 80, 40)).save(self.photo)

    def prepare(self, newline: str) -> None:
        job = {}
        asyncio.run(preparation.run_prep_job(
            "newlines",
            job,
            title="Худи Stussy",
            description=self.DESCRIPTION.replace("\n", newline),
            source_photos=[self.photo],
            drafts_count=2,
            facts={"brand": "Stussy", "size": "48 (M)"},
            base_dir=self.prep_dir,
        ))
        self.assertEqual(job.get("status"), "done", job)

    def assert_generated_description(self, description: str) -> None:
        body, article = description.replace("\r\n", "\n").rsplit("\n\n", 1)
        self.assertRegex(article, r"^арт\.\d{4,6}$")
        self.assertEqual(body, self.DESCRIPTION)

    def check_preparation(self, newline: str) -> None:
        self.prepare(newline)
        source = self.prep_dir / "source" / "text.txt"
        with self.subTest(location="source/text.txt"):
            self.assertEqual(source.read_text(encoding="utf-8"), self.DESCRIPTION)
        cards = preparation.build_result(self.prep_dir, 2)
        self.assertEqual(len(cards), 2)
        for index, card in enumerate(cards, start=1):
            with self.subTest(location=f"card {index}"):
                self.assert_generated_description(card["description"])
            path = self.prep_dir / f"draft_{index:02d}" / "text.txt"
            with self.subTest(location=str(path.relative_to(self.prep_dir))):
                self.assert_generated_description(path.read_text(encoding="utf-8"))

    def check_regeneration(self, newline: str) -> None:
        self.prepare(newline)
        for attempt in range(2):
            card = preparation.regenerate_draft(self.prep_dir, 1)
            with self.subTest(attempt=attempt, location="card"):
                self.assert_generated_description(card["description"])
            path = self.prep_dir / "draft_01" / "text.txt"
            with self.subTest(attempt=attempt, location="draft_01/text.txt"):
                self.assert_generated_description(path.read_text(encoding="utf-8"))

    def check_manual_edit(self, newline: str) -> None:
        self.prepare("\n")
        edited = "Исправленное описание.\n" + self.DESCRIPTION
        card = preparation.update_draft_text(
            self.prep_dir,
            1,
            "Худи Stussy после ручной правки",
            edited.replace("\n", newline),
        )
        with self.subTest(location="card"):
            self.assertEqual(card["description"].replace("\r\n", "\n"), edited)
        path = self.prep_dir / "draft_01" / "text.txt"
        with self.subTest(location="draft_01/text.txt"):
            self.assertEqual(path.read_text(encoding="utf-8"), edited)

    def test_prepare_crlf_preserves_paragraphs(self) -> None:
        self.check_preparation("\r\n")

    def test_prepare_lf_preserves_paragraphs(self) -> None:
        self.check_preparation("\n")

    def test_regenerate_crlf_preserves_paragraphs(self) -> None:
        self.check_regeneration("\r\n")

    def test_regenerate_lf_preserves_paragraphs(self) -> None:
        self.check_regeneration("\n")

    def test_manual_edit_crlf_preserves_paragraphs(self) -> None:
        self.check_manual_edit("\r\n")

    def test_manual_edit_lf_preserves_paragraphs(self) -> None:
        self.check_manual_edit("\n")


if __name__ == "__main__":
    unittest.main()
