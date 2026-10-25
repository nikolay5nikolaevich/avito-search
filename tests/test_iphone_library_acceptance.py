"""Независимая приёмка библиотеки iPhone: только временные материалы."""
import asyncio
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import publish_test_isolation  # noqa: F401, E402
import preparation
import publisher
import publish_state


class LibraryAcceptance(unittest.TestCase):
    def setUp(self):
        self.lib = importlib.import_module("iphone_library")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "library"
        self.model = "iPhone 17 Pro Max"
        self.text = "{модель}, {память}, {цвет}, {SIM}\r\n\r\n  Цвета: синий и оранжевый.  Телефон +7 (999) 123-45-67"
        descriptions = self.root / self.model / "descriptions"
        descriptions.mkdir(parents=True)
        (descriptions / "01.txt").write_bytes(self.text.encode("utf-8"))
        for memory, pack, color in [("512 GB", "Первый", "red"), ("512 GB", "Второй", "blue"), ("1024 GB", "Терабайт", "green")]:
            directory = self.root / self.model / memory / pack
            directory.mkdir(parents=True)
            for name in ["02.png", "01.png"]:
                Image.new("RGB", (100, 120), color).save(directory / name)

    def select(self, **changes):
        values = dict(title="17 Pro Max iPhone", memory="512 ГБ", color="Синий", sim_type="eSIM", drafts_count=3, root=self.root, seed=42)
        values.update(changes)
        return self.lib.select_materials(**values)

    def test_english_order_and_longest_model(self):
        for title in ["iPhone 17 Pro Max", "17 Pro Max iPhone", "  IPHONE   17  Pro Max 512 GB"]:
            self.assertEqual(self.lib.resolve_model(title), self.model)
        self.assertEqual(self.lib.resolve_model("17 Pro iPhone"), "iPhone 17 Pro")
        self.assertEqual(self.lib.resolve_model("iPhone 17e"), "iPhone 17e")

    def test_unknown_or_ambiguous_model_never_falls_back(self):
        for title in ["iPhone 18", "iPhone 17e Pro", "iPhone 170", "iPhone 17 Pro и iPhone 17 Pro Max", "iPhone 17 Pro Maximum"]:
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.lib.resolve_model(title)

    def test_memory_filters_and_color_does_not(self):
        catalog = self.lib.catalog(self.model, "512 ГБ", root=self.root)
        self.assertEqual({p["id"] for p in catalog["packs"]}, {"Первый", "Второй"})
        first = self.select(pack_id="Первый")
        other_color = self.select(pack_id="Первый", color="Оранжевый")
        self.assertEqual(first["source_photos"], other_color["source_photos"])
        self.assertEqual([p.name for p in first["source_photos"]], ["01.png", "02.png"])
        self.assertEqual(self.select(memory="1024 ГБ")["pack_id"], "Терабайт")

    def test_random_uses_one_pack_and_n_texts_with_exact_substitutions(self):
        selected = self.select(drafts_count=7)
        self.assertEqual(len({p.parent for p in selected["source_photos"]}), 1)
        self.assertEqual(len(selected["descriptions"]), 7)
        self.assertEqual(len(selected["template_names"]), 7)
        expected = self.text.replace("{модель}", self.model).replace("{память}", "512 ГБ").replace("{цвет}", "Синий").replace("{SIM}", "eSIM").replace("\r\n", "\n")
        for text in selected["descriptions"]:
            self.assertEqual(text.replace("\r\n", "\n"), expected)

    def test_invalid_sim_memory_pack_and_counts_rejected(self):
        for changes in [{"sim_type": ""}, {"sim_type": "auto"}, {"memory": "128 ГБ"}, {"title": "iPhone 17 Pro Max 256 GB"}, {"drafts_count": 0}, {"drafts_count": 21}, {"pack_id": "missing"}, {"pack_id": "../Терабайт"}, {"pack_id": str(self.root.parent)}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.select(**changes)

    def test_unknown_tokens_empty_text_and_corrupt_pack_rejected(self):
        template = self.root / self.model / "descriptions" / "01.txt"
        for text in ["{цена}", "   "]:
            template.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.select()
        template.write_text("Телефон новый", encoding="utf-8")
        photo = self.root / self.model / "512 GB" / "Первый" / "01.png"
        photo.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            self.select(pack_id="Первый")

    def test_missing_library_never_uses_another_model(self):
        with self.assertRaises(ValueError):
            self.select(title="iPhone 17 Pro")

    def test_external_directory_link_is_rejected(self):
        external = self.root.parent / "outside"
        external.mkdir()
        Image.new("RGB", (80, 80), "red").save(external / "01.png")
        link = self.root / self.model / "512 GB" / "Внешний"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.select(pack_id="Внешний")

    def test_empty_and_over_limit_packs_are_unavailable(self):
        memory_dir = self.root / self.model / "512 GB"
        (memory_dir / "Пустой").mkdir()
        excess = memory_dir / "Много"
        excess.mkdir()
        for index in range(11):
            Image.new("RGB", (80, 80), "red").save(excess / f"{index:02d}.png")
        catalog = self.lib.catalog(self.model, "512 ГБ", root=self.root)
        for pack in catalog["packs"]:
            if pack["id"] in {"Пустой", "Много"}:
                self.assertFalse(pack["available"])
                self.assertTrue(pack["error"])
                with self.assertRaises(ValueError):
                    self.select(pack_id=pack["id"])

    def test_explicit_contradictions_rejected_but_missing_facts_allowed(self):
        for sim in ["eSIM", "SIM + eSIM", "SIM + SIM"]:
            self.lib.validate_phone_text("Доставка по договорённости.", sim)
        for description in ["Активирован вчера", "Упаковка вскрыта", "SIM + SIM"]:
            with self.subTest(description=description), self.assertRaises(ValueError):
                self.lib.validate_phone_text(description, "eSIM")

    def test_snapshot_regeneration_and_recovery_do_not_reread_library(self):
        selected = self.select(pack_id="Первый")
        photos = selected.pop("source_photos")
        originals = {p: p.read_bytes() for p in photos}
        prep_dir = self.root.parent / "prep_acceptance"
        job = {}
        asyncio.run(preparation.run_prep_job("acceptance", job, title=selected["source_title"], description=selected["descriptions"][0], source_photos=photos, drafts_count=3, facts={}, base_dir=prep_dir, library=selected))
        self.assertEqual(job["status"], "done", job)
        cards = preparation.build_result(prep_dir, 3)
        self.assertEqual(len({card["title"] for card in cards}), 3)
        self.assertEqual(len({card["description"] for card in cards}), 3)
        for path, contents in originals.items():
            self.assertEqual(path.read_bytes(), contents)
            path.write_bytes(b"changed source")
        (self.root / self.model / "descriptions" / "01.txt").write_text("ДРУГОЙ ТЕКСТ", encoding="utf-8")
        snapshot = preparation.read_library_snapshot(prep_dir)
        self.assertEqual(snapshot["sim_type"], "eSIM")
        self.assertEqual(snapshot["pack_id"], "Первый")
        regenerated = preparation.regenerate_draft(prep_dir, 1)
        self.assertEqual(regenerated["description"].rsplit("\n\n", 1)[0], cards[0]["description"].rsplit("\n\n", 1)[0])
        self.assertEqual(preparation.build_result(prep_dir, 3)[1:], cards[1:])
        before_recovery = preparation.build_result(prep_dir, 3)
        (prep_dir / "draft_03" / "text.txt").unlink()
        self.assertIsNotNone(preparation.recover_prep_job(prep_dir))
        self.assertEqual(preparation.read_library_snapshot(prep_dir), snapshot)
        self.assertEqual(preparation.build_result(prep_dir, 3)[:2], before_recovery[:2])
        self.assertEqual(preparation.build_result(prep_dir, 3)[2], cards[2])
        for card in preparation.build_result(prep_dir, 3):
            self.assertEqual(card["library"]["pack_id"], "Первый")
            self.assertEqual(card["library"]["sim_type"], "eSIM")
        with self.assertRaises(ValueError):
            preparation.update_draft_text(prep_dir, 1, cards[0]["title"], "Упаковка вскрыта")
        with self.assertRaises(ValueError):
            preparation.update_draft_text(prep_dir, 1, "iPhone 17 Pro 256 GB", "Описание")


class ExplicitSimAcceptance(unittest.TestCase):
    def test_explicit_sim_no_longer_requires_description_facts(self):
        for sim in ["eSIM", "SIM + eSIM", "SIM + SIM"]:
            with self.subTest(sim=sim):
                options = publisher._phone_required_options("Доставка по договорённости", sim_type=sim)
                value, label = {"eSIM": (3338069, "Только eSIM"), "SIM + eSIM": (3338066, "SIM + eSIM"), "SIM + SIM": (3338067, "2 SIM")}[sim]
                self.assertCountEqual(options, [(184666, value, label), (184667, 3375453, "Неактивированный"), (167055, 3270714, "Запечатана")])

    def test_legacy_missing_sim_is_not_guessed(self):
        with self.assertRaises(publisher.StepError):
            publisher._phone_required_options("Новый телефон")

    def test_all_sim_values_survive_checkpoint_and_summary(self):
        for sim in ["eSIM", "SIM + eSIM", "SIM + SIM"]:
            draft = publisher.DraftData(title="iPhone 17 Pro Max", trade_type="Товар приобретён на продажу", condition="Новое", size="", brand="Apple", color="Синий", description="Телефон", price=100000, locations=(publisher.LocationData(city="Москва", address="Москва"),), view_price_max=Decimal("2"), photo_paths=(), category="iphone", model="iPhone 17 Pro Max", memory="512 ГБ", sim_type=sim)
            payload = publish_state._serialize_draft(draft)
            restored = publish_state._deserialize_draft(payload)
            self.assertEqual(restored.sim_type, sim)
            self.assertEqual(restored.summary()["sim_type"], sim)
            payload.pop("sim_type")
            self.assertEqual(publish_state._deserialize_draft(payload).sim_type, "")


if __name__ == "__main__":
    unittest.main()
