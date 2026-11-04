"""Ожидание гидратации финальной кнопки не расширяет денежный allowlist."""

import asyncio
import unittest

import publish_test_isolation  # noqa: F401
from test_publish_financial_flow import FakeLocator, FakePage, psel, publisher


class HydratingButton(FakeLocator):
    """Первое чтение фиксирует промежуточную подпись региональной формы."""

    def __init__(self) -> None:
        super().__init__(text="Разместить timing")
        self.reads = 0

    async def inner_text(self) -> str:
        self.reads += 1
        return "Разместить timing" if self.reads == 1 else "Продолжить timing"


def filled_form(button: FakeLocator) -> FakePage:
    return FakePage(
        "https://www.avito.ru/additem",
        {
            psel.SAVE_AND_EXIT_BUTTON: FakeLocator(),
            psel.TITLE_INPUT: FakeLocator(value="Тестовое объявление"),
            psel.FORM_CONTINUE_BUTTON: button,
        },
    )


class RegionalContinueButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_label_waits_until_button_is_enabled(self) -> None:
        class EnablingButton(FakeLocator):
            checks = 0

            async def is_enabled(self) -> bool:
                self.checks += 1
                return self.checks > 1

        button = EnablingButton(text="Продолжить timing")
        page = filled_form(button)
        button.on_click = lambda: setattr(
            page, "url", "https://www.avito.ru/cpxpromo/1234567890",
        )
        await asyncio.wait_for(publisher._step_continue_listing(page), timeout=12)
        self.assertGreaterEqual(button.checks, 2)
        self.assertEqual(button.clicks, 1)

    async def test_transient_unknown_waits_for_known_label_before_one_click(self) -> None:
        button = HydratingButton()
        page = filled_form(button)

        def transition() -> None:
            self.assertGreaterEqual(button.reads, 2)
            page.url = "https://www.avito.ru/cpxpromo/1234567890"

        button.on_click = transition
        item_id, active_page = await asyncio.wait_for(
            publisher._step_continue_listing(page), timeout=12,
        )
        self.assertEqual(item_id, "1234567890")
        self.assertIs(active_page, page)
        self.assertEqual(button.clicks, 1)

    async def test_persistent_unknown_stops_without_click_within_bound(self) -> None:
        button = FakeLocator(text="Разместить timing")
        page = filled_form(button)
        with self.assertRaises(publisher.StepError):
            await asyncio.wait_for(publisher._step_continue_listing(page), timeout=12)
        self.assertEqual(button.clicks, 0)

    def test_publish_labels_are_not_known_continue_labels(self) -> None:
        self.assertNotIn("Разместить", publisher.CONTINUE_BUTTON_LABELS)
        self.assertNotIn("Разместить timing", publisher.CONTINUE_BUTTON_LABELS)


if __name__ == "__main__":
    unittest.main()
