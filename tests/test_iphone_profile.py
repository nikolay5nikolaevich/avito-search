"""Профиль «iPhone»: данные разведки 2026-10-20, валидация, API и каскад полей."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import category_profiles  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402
import app as app_module  # noqa: E402

IPHONE = category_profiles.IPHONE

GOOD_FIELDS = {
    "title": "iPhone 17 Pro 256 ГБ новый",
    "trade_type": "Продаю своё",
    "condition": "Новое",
    "model": "iPhone 17 Pro",
    "memory": "256 ГБ",
    "color": "Синий",
    "description": "Новый, запечатанный. Не активирован. SIM + eSIM.",
    "price": "120000",
    "locations_json": json.dumps([{"city": "Москва", "address": "ул. Арбат, 1"}], ensure_ascii=False),
    "view_price_max": "5",
}
PHOTOS = [("p.jpg", "image/jpeg", 1024)]


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, script: str):
        if "el.options" in script:
            prefix = self.selector.split("'")[1].rsplit("/", 1)[0]
            return self.page.select_options(prefix)
        if "checked" in script:
            return self.selector in self.page.checked
        raise AssertionError(script)


class FakePage:
    """Форма телефонов: каждое поле каскада появляется после выбора предыдущего."""

    def __init__(self, memory=None, colors=None, extra_auto_checked=False) -> None:
        self.memory = memory if memory is not None else [("458968", "256 ГБ"), ("458850", "512 ГБ")]
        self.colors = colors if colors is not None else [("458738", "Синий"), ("458735", "Оранжевый")]
        self.selected: dict[str, str] = {}
        self.checked: set[str] = set()
        self.clicks: list[str] = []
        self.extra_auto_checked = extra_auto_checked

    def select_options(self, prefix: str):
        if prefix == "model":
            return [["22446491", "iPhone 17 Pro"], ["22446489", "iPhone 17"]] if self.selected.get("brand") else []
        if prefix == "memory_size":
            return [list(o) for o in self.memory] if "model" in self.selected else []
        if prefix == "color":
            return [list(o) for o in self.colors] if "memory_size" in self.selected else []
        return []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def click(self, selector: str, timeout: int = 0) -> None:
        self.clicks.append(selector)
        self.checked.add(selector)
        if self.extra_auto_checked and "params[121588]" in selector:
            self.checked.add("[data-marker='params[110395]/option(431241)']")


def _patch_engine(page: FakePage):
    async def fake_type(_page, selector, value):
        page.clicks.append(f"type:{selector}={value}")

    async def fake_click_text(_page, text, timeout_s=4.0):
        if text == "Apple":
            page.selected["brand"] = "Apple"
            return True
        return False

    async def fake_combobox(_page, prefix, label, option_id):
        page.selected[prefix] = str(option_id)
        page.clicks.append(f"combo:{prefix}={option_id}")

    async def no_pause():
        return None

    async def no_sleep(_s):
        return None

    return [
        mock.patch.object(publisher, "_clear_and_type", fake_type),
        mock.patch.object(publisher, "_click_visible_text", fake_click_text),
        mock.patch.object(publisher, "_select_combobox", fake_combobox),
        mock.patch.object(publisher, "_pause", no_pause),
        mock.patch.object(publisher.asyncio, "sleep", no_sleep),
    ]


def _draft(**over) -> publisher.DraftData:
    fields = {**GOOD_FIELDS, **over}
    return publisher.build_draft_data(fields, ["p.jpg"], category="iphone")


def _run_fill(page: FakePage, draft: publisher.DraftData):
    patches = _patch_engine(page)
    for p in patches:
        p.start()
    try:
        return asyncio.run(publisher._step_fill_fields(page, draft, IPHONE))
    finally:
        for p in patches:
            p.stop()


class IphoneProfileTest(unittest.TestCase):
    def test_profile_data(self) -> None:
        self.assertIs(category_profiles.get_profile("iphone"), IPHONE)
        self.assertEqual(IPHONE.expected_category, {"category_id": "84", "params[110680]": "458500"})
        self.assertEqual(IPHONE.full_path, ("Электроника", "Телефоны", "Мобильные телефоны"))
        self.assertEqual(IPHONE.model_options, {
            "iPhone 17": 22446489, "iPhone 17 Pro": 22446491, "iPhone 17 Pro Max": 22446492,
            "iPhone 17e": 22545058, "iPhone 18 Pro": 22734708, "iPhone 18 Pro Max": 22734712,
        })
        self.assertEqual(IPHONE.memory_options["1024 ГБ"], 458897)
        self.assertEqual(IPHONE.color_options["Зеленый"], 458744)
        self.assertEqual(IPHONE.trade_type_options["Продаю своё"], 20018)
        self.assertEqual(IPHONE.colors_for("iPhone 18 Pro Max"), IPHONE.colors_for("iPhone 18 Pro"))
        self.assertFalse(IPHONE.has_size)

    def test_clothing_profiles_unchanged(self) -> None:
        for p in (category_profiles.JACKETS, category_profiles.SNEAKERS, category_profiles.VESTS):
            self.assertTrue(p.has_size)
            self.assertFalse(p.has_model)
            self.assertEqual(p.fixed_brand, "")
            self.assertEqual(p.trade_type_param, 0)

    def test_api_exposes_phone_fields(self) -> None:
        body = json.loads(asyncio.run(app_module.api_publish_categories()).body)
        cats = {c["key"]: c for c in body["categories"]}
        ph = cats["iphone"]
        self.assertEqual(ph["sizes"], [])
        self.assertFalse(ph["has_size"])
        self.assertEqual(ph["fixed_brand"], "Apple")
        self.assertIn("iPhone 18 Pro Max", ph["models"])
        self.assertEqual(ph["model_colors"]["iPhone 17 Pro"], ["Оранжевый", "Серебристый", "Синий"])
        self.assertIn("2048 ГБ", ph["model_memory"]["iPhone 17 Pro Max"])
        self.assertEqual(cats["jackets"]["models"], [])
        self.assertTrue(cats["jackets"]["has_size"])

    def test_valid_phone_form(self) -> None:
        self.assertEqual(publisher.validate_publish_form(GOOD_FIELDS, PHOTOS, IPHONE), [])

    def test_wrong_combinations_rejected(self) -> None:
        cases = [
            ({"model": "iPhone 18"}, "model"),
            ({"memory": "128 ГБ"}, "memory"),       # у 17 Pro нет 128
            ({"color": "Белый"}, "color"),          # не цвет 17 Pro
            ({"color": "Зелёный"}, "color"),        # одежное написание
            ({"memory": ""}, "memory"),
            ({"condition": "Отличное"}, "condition"),
        ]
        for over, field in cases:
            with self.subTest(over=over):
                errs = publisher.validate_publish_form({**GOOD_FIELDS, **over}, PHOTOS, IPHONE)
                self.assertIn(field, {e["field"] for e in errs})

    def test_no_size_or_brand_needed(self) -> None:
        errs = publisher.validate_publish_form({**GOOD_FIELDS, "size": "", "brand": ""}, PHOTOS, IPHONE)
        self.assertEqual(errs, [])

    def test_clothing_still_requires_size(self) -> None:
        fields = {**GOOD_FIELDS, "condition": "Отличное", "color": "Чёрный", "size": ""}
        errs = publisher.validate_publish_form(fields, PHOTOS, category_profiles.JACKETS)
        self.assertEqual({e["field"] for e in errs}, {"size"})

    def test_draft_uses_fixed_brand_and_checkpoint_roundtrip(self) -> None:
        draft = _draft(brand="Samsung")
        self.assertEqual(draft.brand, "Apple")
        self.assertEqual((draft.model, draft.memory, draft.size), ("iPhone 17 Pro", "256 ГБ", ""))
        restored = publish_state._deserialize_draft(publish_state._serialize_draft(draft)) \
            if hasattr(publish_state, "_serialize_draft") else None
        if restored is not None:
            self.assertEqual(restored, draft)

    def test_cascade_happy_path_order(self) -> None:
        page = FakePage()
        selected = _run_fill(page, _draft())
        self.assertEqual(selected, "Apple")
        combos = [c for c in page.clicks if c.startswith("combo:")]
        self.assertEqual(combos, ["combo:model=22446491", "combo:memory_size=458968", "combo:color=458738"])
        self.assertIn("[data-marker='params[121588]/2850684']", page.clicks)
        self.assertIn("[data-marker='params[110395]/option(431241)']", page.clicks)
        self.assertIn("[data-marker='params[2822]/option(20018)']", page.clicks)

    def test_extra_condition_not_clicked_when_auto_checked(self) -> None:
        page = FakePage(extra_auto_checked=True)
        _run_fill(page, _draft())
        self.assertNotIn("[data-marker='params[110395]/option(431241)']", page.clicks)

    def test_missing_memory_option_fails_closed_with_list(self) -> None:
        page = FakePage(memory=[("458850", "512 ГБ")])
        with self.assertRaises(publisher.StepError) as ctx:
            _run_fill(page, _draft())
        msg = str(ctx.exception)
        self.assertIn("256 ГБ", msg)
        self.assertIn("Доступно: 512 ГБ", msg)
        self.assertNotIn("memory_size", page.selected, "соседнее значение не выбирается")

    def test_missing_color_option_fails_closed(self) -> None:
        page = FakePage(colors=[("458735", "Оранжевый")])
        with self.assertRaises(publisher.StepError) as ctx:
            _run_fill(page, _draft())
        self.assertIn("Цвет", str(ctx.exception))
        self.assertNotIn("color", page.selected)

    def test_field_never_appears_times_out(self) -> None:
        page = FakePage(memory=[])
        patches = _patch_engine(page)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(publisher.StepError) as ctx:
                asyncio.run(publisher._select_cascade_combobox(
                    page, "memory_size", "Встроенная память", "256 ГБ", 458968, timeout_s=0.05,
                ))
        finally:
            for p in patches:
                p.stop()
        self.assertIn("не появилось", str(ctx.exception))

    def test_manufacturer_not_found_stops(self) -> None:
        page = FakePage()
        patches = _patch_engine(page)

        async def never(_page, text, timeout_s=4.0):
            return False

        for p in patches:
            p.start()
        try:
            with mock.patch.object(publisher, "_click_visible_text", never):
                with self.assertRaises(publisher.StepError):
                    asyncio.run(publisher._step_fill_fields(page, _draft(), IPHONE))
        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main()
