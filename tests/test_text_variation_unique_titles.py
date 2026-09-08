"""Регрессия: названия всех вариантов пакета должны различаться."""

from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import photo_variation  # noqa: E402
import preparation  # noqa: E402
from text_variation import TITLE_MAX_LEN, vary_listing  # noqa: E402


class UniqueVariantTitlesTest(unittest.TestCase):
    def test_first_variant_is_altered_like_every_other_variant(self) -> None:
        source_title = "Худи Stussy"
        source_description = "Мужское худи в новом состоянии."
        variants = vary_listing(
            source_title,
            source_description,
            3,
            seed=542060848,
            facts={"size": "48 (M)", "brand": "Stussy"},
        )

        self.assertTrue(all(variant.title != source_title for variant in variants))
        self.assertTrue(
            all(variant.description != source_description for variant in variants)
        )
        self.assertTrue(all("артикул" in variant.notes for variant in variants))

    def test_every_draft_receives_a_modified_photo_preset(self) -> None:
        presets = photo_variation.build_modified_presets(20, seed=542060848)

        self.assertEqual(20, len(presets))
        self.assertTrue(all(preset.zoom_pct >= 15.0 for preset in presets))
        self.assertEqual(20, len({photo_variation._preset_key(p) for p in presets}))

        image = Image.new("RGB", (80, 80), color=(120, 80, 40))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        source_bytes = buffer.getvalue()
        self.assertNotEqual(
            source_bytes,
            photo_variation.apply_preset(source_bytes, presets[0]),
        )

    def test_twenty_titles_are_unique_without_articles_or_tracking_numbers(self) -> None:
        variants = vary_listing(
            "Худи Stussy арт.895499",
            "Мужское худи в новом состоянии.",
            20,
            seed=542060848,
            facts={
                "size": "48 (M)",
                "brand": "Stussy",
                "condition": "Новое с биркой",
            },
        )

        titles = [variant.title for variant in variants]
        normalized_titles = {" ".join(title.casefold().split()) for title in titles}

        self.assertEqual(20, len(titles))
        self.assertEqual(20, len(normalized_titles), titles)
        self.assertTrue(all(0 < len(title) <= TITLE_MAX_LEN for title in titles))
        self.assertTrue(all("арт" not in title.casefold() for title in titles), titles)
        self.assertTrue(all("895499" not in title for title in titles), titles)

    def test_size_and_height_numbers_remain_allowed_in_titles(self) -> None:
        variants = vary_listing(
            "Костюм мужской размер 48 рост 176",
            "Костюм в хорошем состоянии.",
            3,
            seed=17,
        )

        self.assertTrue(any(
            "48" in variant.title and "176" in variant.title
            for variant in variants
        ))

    def test_reserved_titles_are_not_reused_during_regeneration(self) -> None:
        first_batch = vary_listing(
            "Худи Stussy",
            "Мужское худи в новом состоянии.",
            20,
            seed=100,
        )
        reserved = {variant.title for variant in first_batch[:19]}

        regenerated = vary_listing(
            "Худи Stussy",
            "Мужское худи в новом состоянии.",
            1,
            seed=200,
            reserved_titles=reserved,
        )

        self.assertNotIn(regenerated[0].title.casefold(), {
            title.casefold() for title in reserved
        })

    def test_manual_edit_rejects_title_used_by_another_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prep_dir = Path(tmp) / "prep_unique"
            for index, title in ((1, "Худи Stussy"), (2, "Худи Stussy в наличии")):
                draft_dir = prep_dir / f"draft_{index:02d}"
                draft_dir.mkdir(parents=True)
                (draft_dir / "title.txt").write_text(title, encoding="utf-8")
                (draft_dir / "text.txt").write_text("Описание", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "отличаться"):
                preparation.update_draft_text(
                    prep_dir,
                    2,
                    "  ХУДИ,   STUSSY! ",
                    "Новое описание",
                )


if __name__ == "__main__":
    unittest.main()
