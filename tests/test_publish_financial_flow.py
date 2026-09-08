"""Финансовые предохранители полной публикации Авито."""

import asyncio
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import publisher  # noqa: E402
import avito_publish_selectors as psel  # noqa: E402


VIEW_PRICE_SLIDER_SELECTOR = "[data-marker='bid-settings'] [role='slider']"


class FakeKeyboard:
    async def press(self, _key: str) -> None:
        return None


class FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        text: str = "",
        value: str = "",
        attrs: dict[str, str] | None = None,
        checked: bool = False,
        enabled: bool = True,
        elements: list["FakeLocator"] | None = None,
        children: dict[str, "FakeLocator"] | None = None,
        on_click: object = None,
    ) -> None:
        self.visible = visible
        self.text = text
        self.value = value
        self.attrs = dict(attrs or {})
        self.checked = checked
        self.enabled = enabled
        self.elements = elements
        self.children = dict(children or {})
        self.on_click = on_click
        self.clicks = 0

    @property
    def first(self) -> "FakeLocator":
        return self.elements[0] if self.elements else self

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def is_checked(self) -> bool:
        return self.checked

    async def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    async def input_value(self, **_kwargs: object) -> str:
        return self.value

    async def inner_text(self) -> str:
        return self.text

    async def fill(self, value: str, **_kwargs: object) -> None:
        self.value = value

    async def press(self, _key: str) -> None:
        return None

    async def click(self, **_kwargs: object) -> None:
        self.clicks += 1
        if callable(self.on_click):
            self.on_click()

    async def count(self) -> int:
        return len(self.elements) if self.elements is not None else 1

    def nth(self, index: int) -> "FakeLocator":
        if self.elements is None:
            if index != 0:
                raise IndexError(index)
            return self
        return self.elements[index]

    def locator(self, selector: str) -> "FakeLocator":
        return self.children.get(selector, FakeLocator(visible=False))


class DelayedFakeLocator(FakeLocator):
    def __init__(self, *, empty_counts: int, elements: list[FakeLocator]) -> None:
        super().__init__(elements=elements)
        self.empty_counts = empty_counts

    async def count(self) -> int:
        if self.empty_counts > 0:
            self.empty_counts -= 1
            return 0
        return await super().count()


class FakePriceSlider(FakeLocator):
    def __init__(
        self,
        price_input: FakeLocator,
        allowed_prices: tuple[str, ...],
        *,
        delayed_price_update: bool = False,
        initial_index: int | None = None,
    ) -> None:
        if not allowed_prices:
            raise ValueError("allowed_prices must not be empty")
        self.price_input = price_input
        self.allowed_prices = allowed_prices
        self.delayed_price_update = delayed_price_update
        self.index = (
            len(allowed_prices) - 1 if initial_index is None else initial_index
        )
        self.pressed_keys: list[str] = []
        super().__init__(attrs={})
        self._sync()

    def _sync(self) -> None:
        self.price_input.value = self.allowed_prices[self.index]
        self.attrs["aria-valuemin"] = "0"
        self.attrs["aria-valuemax"] = str(len(self.allowed_prices) - 1)
        self.attrs["aria-valuenow"] = str(self.index)

    async def press(self, key: str) -> None:
        self.pressed_keys.append(key)
        if key == "Home":
            self.index = 0
        elif key == "ArrowRight":
            self.index = min(self.index + 1, len(self.allowed_prices) - 1)
        else:
            raise AssertionError(f"Unexpected slider key: {key}")
        self.attrs["aria-valuenow"] = str(self.index)
        if self.delayed_price_update:
            async def update_later() -> None:
                await asyncio.sleep(0.01)
                self.price_input.value = self.allowed_prices[self.index]

            asyncio.create_task(update_later())
        else:
            self.price_input.value = self.allowed_prices[self.index]


class HydratingFakeSlider(FakeLocator):
    """Slider, у которого поле цены отрисовывается ПОЗЖЕ самого slider.

    `hydrate_after_s=None` — поле остаётся пустым до нажатия Home, и React
    дописывает в него значение ПРЕЖНЕЙ позиции раньше новой (наблюдаемая
    рассинхронизация `aria-valuenow` и текстового поля).
    """

    def __init__(
        self,
        price_input: FakeLocator,
        prices: tuple[str, ...],
        *,
        initial_index: int,
        hydrate_after_s: float | None = None,
    ) -> None:
        self.price_input = price_input
        self.prices = prices
        self.index = initial_index
        self.pressed_keys: list[str] = []
        super().__init__(attrs={
            "aria-valuemin": "0",
            "aria-valuemax": str(len(prices) - 1),
            "aria-valuenow": str(initial_index),
        })
        self.price_input.value = ""
        if hydrate_after_s is not None:
            async def hydrate() -> None:
                await asyncio.sleep(hydrate_after_s)
                self.price_input.value = self.prices[self.index]

            asyncio.get_event_loop().create_task(hydrate())

    async def press(self, key: str) -> None:
        self.pressed_keys.append(key)
        stale_index = self.index
        if key == "Home":
            self.index = 0
        else:
            raise AssertionError(f"Unexpected slider key: {key}")
        self.attrs["aria-valuenow"] = str(self.index)

        async def catch_up() -> None:
            # Поле сперва догоняет старую позицию и только потом — новую.
            self.price_input.value = self.prices[stale_index]
            await asyncio.sleep(0.3)
            self.price_input.value = self.prices[self.index]

        asyncio.create_task(catch_up())
        await asyncio.sleep(0)


class FakePage:
    def __init__(
        self,
        url: str,
        locators: dict[str, FakeLocator],
        *,
        texts: dict[str, FakeLocator] | None = None,
    ) -> None:
        self.url = url
        self._locators = locators
        self._texts = dict(texts or {})
        self.keyboard = FakeKeyboard()

    def locator(self, selector: str) -> FakeLocator:
        return self._locators.get(selector, FakeLocator(visible=False))

    def get_by_text(self, text: str, *, exact: bool = False) -> FakeLocator:
        del exact
        return self._texts.get(text, FakeLocator(visible=False))

    async def wait_for_selector(
        self,
        selector: str,
        **_kwargs: object,
    ) -> FakeLocator:
        locator = self.locator(selector).first
        if not await locator.is_visible():
            raise RuntimeError(f"selector not visible: {selector}")
        return locator

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False


def make_price_page(
    *,
    city: str = "Санкт-Петербург",
    minimum: str = "",
    invalid: bool = False,
    action_disabled: bool = False,
    budget: str = "",
    allowed_prices: tuple[str, ...] = ("0,5", "2,25", "35"),
    delayed_price_update: bool = False,
    slider_initial_index: int | None = None,
) -> tuple[FakePage, FakeLocator]:
    action = FakeLocator(
        text="Продолжить с минимальной ценой",
        attrs={"aria-disabled": "true" if action_disabled else "false"},
    )
    minimum_locator = FakeLocator(text=minimum, visible=bool(minimum))
    price_input = FakeLocator(value=allowed_prices[-1])
    slider = FakePriceSlider(
        price_input,
        allowed_prices,
        delayed_price_update=delayed_price_update,
        initial_index=slider_initial_index,
    )
    locators = {
        psel.VIEW_PRICE_CITY_INPUT: FakeLocator(value=city),
        psel.VIEW_PRICE_MODE_SWITCH: FakeLocator(
            attrs={"aria-checked": "false"}
        ),
        psel.VIEW_PRICE_INPUT: price_input,
        VIEW_PRICE_SLIDER_SELECTOR: slider,
        psel.VIEW_PRICE_INPUT_CONTAINER: FakeLocator(
            attrs={"aria-invalid": "true" if invalid else "false"}
        ),
        psel.VIEW_PRICE_MINIMUM_TEXT: minimum_locator,
        psel.VIEW_PRICE_DAILY_BUDGET_INPUT: FakeLocator(value=budget),
        psel.VIEW_PRICE_ACTION_BUTTONS: FakeLocator(elements=[action]),
    }
    page = FakePage(
        "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add",
        locators,
        texts={"Вручную": FakeLocator(text="Вручную")},
    )
    return page, action


class FinancialContractTests(unittest.TestCase):
    """Ломаются при ослаблении денежных и DOM-контрактов."""

    def test_cpxpromo_item_id_requires_exact_screen_path(self) -> None:
        self.assertEqual(
            publisher.parse_cpxpromo_item_id(
                "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add"
            ),
            "8330238411",
        )
        self.assertEqual(
            publisher.parse_cpxpromo_item_id(
                "https://www.avito.ru/cpxpromo/8330238411/"
            ),
            "8330238411",
        )
        self.assertIsNone(
            publisher.parse_cpxpromo_item_id("https://www.avito.ru/additem")
        )
        self.assertIsNone(
            publisher.parse_cpxpromo_item_id(
                "https://www.avito.ru/not-cpxpromo/8330238411"
            )
        )

    def test_ruble_decimal_accepts_nbsp_and_comma_without_float(self) -> None:
        self.assertEqual(
            publisher.parse_ruble_decimal("Минимум\xa0— 0,5\xa0₽"),
            Decimal("0.5"),
        )
        self.assertEqual(
            publisher.parse_ruble_decimal("2,25 ₽"),
            Decimal("2.25"),
        )
        self.assertEqual(
            publisher.parse_ruble_decimal("Цена: 1 234,50 ₽"),
            Decimal("1234.50"),
        )
        self.assertIsNone(publisher.parse_ruble_decimal("цена неизвестна"))
        self.assertIsNone(publisher.parse_ruble_decimal("1 ₽ или 2 ₽"))

    def test_services_require_exact_known_off_switches(self) -> None:
        states = [
            {
                "marker": marker,
                "aria_checked": "false",
                "checked": False,
            }
            for marker in publisher.ALLOWED_SERVICE_SWITCHES
        ]
        self.assertIsNone(publisher.validate_service_switch_states(states))

        unknown_error = publisher.validate_service_switch_states(
            states
            + [
                {
                    "marker": "new/widget/switcher",
                    "aria_checked": "false",
                    "checked": False,
                }
            ]
        )
        self.assertIsNotNone(unknown_error)
        self.assertIn("неизвест", (unknown_error or "").lower())

        enabled_states = [dict(state) for state in states]
        enabled_states[0]["checked"] = True
        enabled_states[0]["aria_checked"] = "true"
        enabled_error = publisher.validate_service_switch_states(enabled_states)
        self.assertIsNotNone(enabled_error)
        self.assertIn("включ", (enabled_error or "").lower())

    def test_services_reject_missing_duplicate_and_disagreeing_state(self) -> None:
        states = [
            {
                "marker": marker,
                "aria_checked": "false",
                "checked": False,
            }
            for marker in publisher.ALLOWED_SERVICE_SWITCHES
        ]

        self.assertIn(
            "отсутств",
            (publisher.validate_service_switch_states(states[:-1]) or "").lower(),
        )
        self.assertIn(
            "повтор",
            (
                publisher.validate_service_switch_states(
                    states + [dict(states[0])]
                )
                or ""
            ).lower(),
        )

        disagreeing = [dict(state) for state in states]
        disagreeing[0]["aria_checked"] = "true"
        self.assertIn(
            "состояни",
            (
                publisher.validate_service_switch_states(disagreeing)
                or ""
            ).lower(),
        )

    def test_user_action_required_preserves_structured_payload(self) -> None:
        action = {
            "type": "view_price_too_low",
            "resumable": False,
            "minimum_view_price": "0.5",
        }
        error = publisher.UserActionRequired(
            "Минимальная стоимость выше указанной",
            user_action=action,
        )

        self.assertEqual(str(error), "Минимальная стоимость выше указанной")
        self.assertEqual(error.user_action, action)


class PriceScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_price_shell_reloads_once_before_reading_city(self) -> None:
        ready_page, action = make_price_page()

        class BlankPricePage(FakePage):
            reloads = 0

            async def reload(self, **_kwargs: object) -> None:
                self.reloads += 1
                self._locators = ready_page._locators
                self._texts = ready_page._texts

        page = BlankPricePage(ready_page.url, {})

        actual = await publisher._step_fill_view_price(
            page,
            publisher.LocationData(
                "Санкт-Петербург", "Невский проспект, 1"
            ),
            1,
            1,
            view_price_max=Decimal("2.25"),
        )

        self.assertEqual(actual, Decimal("0.5"))
        self.assertEqual(page.reloads, 1)
        self.assertEqual(action.clicks, 0)

    async def test_current_manual_radio_marker_confirms_manual_mode(self) -> None:
        page, action = make_price_page()
        page._locators[psel.VIEW_PRICE_MODE_SWITCH] = FakeLocator(visible=False)
        page._texts.clear()
        page._locators[
            "[data-marker='budget-setting-select/option(manual)']"
        ] = FakeLocator(visible=True)
        page._locators[
            "[data-marker='budget-setting-select/option(manual)'] "
            "input[type='radio'][value='manual']"
        ] = FakeLocator(checked=True)

        actual = await publisher._step_fill_view_price(
            page,
            publisher.LocationData(
                "Санкт-Петербург", "Невский проспект, 1"
            ),
            1,
            1,
            view_price_max=Decimal("2.25"),
        )

        self.assertEqual(actual, Decimal("0.5"))
        self.assertEqual(action.clicks, 0)

    async def test_visible_price_onboarding_is_closed_before_reading_manual_mode(self) -> None:
        page, action = make_price_page()
        tooltip = FakeLocator(visible=True)
        close_button = FakeLocator(
            on_click=lambda: setattr(tooltip, "visible", False)
        )

        class TooltipBlockedMode(FakeLocator):
            async def get_attribute(self, name: str) -> str | None:
                if tooltip.visible:
                    raise RuntimeError("onboarding tooltip intercepts the price controls")
                return await super().get_attribute(name)

        page._locators[
            "[data-marker='onboarding-tooltip/autoFromManual/1']"
        ] = tooltip
        page._locators[
            "[data-marker='onboarding-tooltip/autoFromManual/1'] "
            "button:has(svg[data-icon-name='close'])"
        ] = close_button
        page._locators[psel.VIEW_PRICE_MODE_SWITCH] = TooltipBlockedMode(
            attrs={"aria-checked": "false"}
        )

        actual = await publisher._step_fill_view_price(
            page,
            publisher.LocationData(
                "Санкт-Петербург", "Невский проспект, 1"
            ),
            1,
            1,
            view_price_max=Decimal("2.25"),
        )

        self.assertEqual(actual, Decimal("0.5"))
        self.assertFalse(tooltip.visible)
        self.assertEqual(close_button.clicks, 1)
        self.assertEqual(action.clicks, 0)

    async def test_price_screen_replaces_closed_form_tab_with_new_avito_tab(self) -> None:
        class ClosingFormPage(FakePage):
            closed = False

            def is_closed(self) -> bool:
                return self.closed

        button = FakeLocator(text="Продолжить")
        old_page = ClosingFormPage(
            "https://www.avito.ru/additem",
            {
                psel.SAVE_AND_EXIT_BUTTON: FakeLocator(),
                psel.FORM_CONTINUE_BUTTON: button,
                psel.TITLE_INPUT: FakeLocator(value="Костюм Lacoste"),
            },
        )
        price_page, _action = make_price_page()
        price_page.url = "about:blank"

        class Context:
            pages = [old_page, price_page]

        old_page.context = Context()
        price_page.context = old_page.context
        def start_tab_handoff() -> None:
            old_page.url = (
                "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add"
            )

            async def finish_tab_handoff() -> None:
                await asyncio.sleep(0.01)
                price_page.url = old_page.url
                old_page.closed = True

            asyncio.create_task(finish_tab_handoff())

        button.on_click = start_tab_handoff

        result = await publisher._step_continue_listing(old_page)

        self.assertEqual(result, ("8330238411", price_page))

    async def test_filled_form_continue_requires_context_and_clicks_once(self) -> None:
        button = FakeLocator(text="Продолжить")
        page = FakePage(
            "https://www.avito.ru/additem?draftId=123",
            {
                psel.SAVE_AND_EXIT_BUTTON: FakeLocator(),
                psel.FORM_CONTINUE_BUTTON: button,
                psel.TITLE_INPUT: FakeLocator(value="Худи Stussy"),
            },
        )
        button.on_click = lambda: setattr(
            page,
            "url",
            "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add",
        )

        item_id, active_page = await publisher._step_continue_listing(page)

        self.assertEqual(item_id, "8330238411")
        self.assertIs(active_page, page)
        self.assertEqual(button.clicks, 1)

        wrong_page = FakePage(
            "https://www.avito.ru/profile/pro/items",
            {
                psel.SAVE_AND_EXIT_BUTTON: FakeLocator(),
                psel.FORM_CONTINUE_BUTTON: FakeLocator(text="Продолжить"),
                psel.TITLE_INPUT: FakeLocator(value="Худи Stussy"),
            },
        )
        with self.assertRaisesRegex(publisher.StepError, "форм"):
            await publisher._step_continue_listing(wrong_page)
        self.assertEqual(
            wrong_page.locator(psel.FORM_CONTINUE_BUTTON).clicks,
            0,
        )

    async def test_filled_form_continue_accepts_elementtiming_suffix(self) -> None:
        button = FakeLocator(text="Продолжить\ntiming")
        page = FakePage(
            "https://www.avito.ru/additem",
            {
                psel.SAVE_AND_EXIT_BUTTON: FakeLocator(),
                psel.FORM_CONTINUE_BUTTON: button,
                psel.TITLE_INPUT: FakeLocator(value="Худи Stussy"),
            },
        )
        button.on_click = lambda: setattr(
            page,
            "url",
            "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add",
        )

        item_id, active_page = await publisher._step_continue_listing(page)

        self.assertEqual(item_id, "8330238411")
        self.assertIs(active_page, page)
        self.assertEqual(button.clicks, 1)

    async def test_filled_form_continue_rejects_unknown_suffix_without_click(self) -> None:
        button = FakeLocator(text="Продолжить и оплатить")
        page = FakePage(
            "https://www.avito.ru/additem",
            {
                psel.SAVE_AND_EXIT_BUTTON: FakeLocator(),
                psel.FORM_CONTINUE_BUTTON: button,
                psel.TITLE_INPUT: FakeLocator(value="Худи Stussy"),
            },
        )

        with self.assertRaisesRegex(publisher.StepError, "неизвестное название"):
            await publisher._step_continue_listing(page)

        self.assertEqual(button.clicks, 0)

    async def test_too_low_price_stops_before_action_click(self) -> None:
        # Слайдер отдаёт минимум 0,5 (≤ потолка 0,5 — сам слайдер не спорит),
        # но поле помечено невалидным с требованием более высокого минимума
        # (0,8 > потолка) — race/несогласованность на стороне Авито.
        page, action = make_price_page(
            minimum="Минимум — 0,8 ₽",
            invalid=True,
            action_disabled=True,
        )

        with self.assertRaises(publisher.UserActionRequired) as caught:
            await publisher._step_fill_view_price(
                page,
                publisher.LocationData(
                    "Санкт-Петербург", "Адмиралтейская, 1"
                ),
                2,
                3,
                view_price_max=Decimal("0.5"),
            )

        self.assertEqual(caught.exception.user_action, {
            "type": "view_price_too_low",
            "resumable": False,
            "item_index": 2,
            "items_total": 3,
            "minimum_view_price": "0.8",
            "message": (
                "Для объявления №2 Авито требует минимум 0.8 ₽, "
                "а slider не дал этого значения"
            ),
        })
        self.assertEqual(action.clicks, 0)

    async def test_city_mismatch_stops_before_financial_action(self) -> None:
        page, action = make_price_page(city="Москва")

        with self.assertRaisesRegex(publisher.StepError, "город"):
            await publisher._step_fill_view_price(
                page,
                publisher.LocationData(
                    "Санкт-Петербург", "Адмиралтейская, 1"
                ),
                1,
                1,
                view_price_max=Decimal("0.5"),
            )

        self.assertEqual(action.clicks, 0)

    async def test_minimum_price_is_read_back_and_budget_must_stay_empty(self) -> None:
        page, action = make_price_page()
        await publisher._step_fill_view_price(
            page,
            publisher.LocationData("Санкт-Петербург", "Невский, 1"),
            1,
            1,
            view_price_max=Decimal("2.25"),
        )
        self.assertEqual(
            page.locator(psel.VIEW_PRICE_INPUT).value,
            "0,5",
        )
        self.assertEqual(action.clicks, 0)

        budget_page, budget_action = make_price_page(budget="100")
        with self.assertRaisesRegex(publisher.StepError, "бюджет"):
            await publisher._step_fill_view_price(
                budget_page,
                publisher.LocationData("Санкт-Петербург", "Невский, 1"),
                1,
                1,
                view_price_max=Decimal("2.25"),
            )
        self.assertEqual(budget_action.clicks, 0)

    async def test_price_below_avito_minimum_takes_minimum(self) -> None:
        """Минимум Авито ниже потолка → берём минимум, не останавливаемся.

        Решение пользователя 16.08.2026 («всегда бери минимум»): slider уже
        стоит на минимальной позиции, поэтому принимаем ровно её. Кнопка
        подтверждения при этом остаётся ненажатой — клик делает отдельный шаг.
        """
        page, action = make_price_page(
            city="Мурманск", allowed_prices=("1,7", "3", "5")
        )

        actual = await publisher._step_fill_view_price(
            page,
            publisher.LocationData("Мурманск", "улица Воровского, 11"),
            1,
            16,
            view_price_max=Decimal("2"),
        )

        self.assertEqual(page.locator(psel.VIEW_PRICE_INPUT).value, "1,7")
        self.assertEqual(actual, Decimal("1.7"))
        self.assertEqual(action.clicks, 0)

    async def test_minimum_below_cap_never_moves_slider_right(self) -> None:
        """Минимум Авито ниже потолка → слайдер жмёт только Home, ArrowRight
        не нажимается ни разу (поиск точного значения удалён из движка)."""
        page, action = make_price_page(
            city="Мурманск", allowed_prices=("1,7", "3", "5")
        )
        slider = page.locator(VIEW_PRICE_SLIDER_SELECTOR)

        actual = await publisher._step_fill_view_price(
            page,
            publisher.LocationData("Мурманск", "улица Воровского, 11"),
            1,
            16,
            view_price_max=Decimal("2"),
        )

        self.assertEqual(actual, Decimal("1.7"))
        self.assertNotIn("ArrowRight", slider.pressed_keys)
        self.assertEqual(slider.pressed_keys, ["Home"])
        self.assertEqual(action.clicks, 0)

    async def test_avito_minimum_above_cap_stops_before_action_click(self) -> None:
        page, action = make_price_page(
            city="Мурманск", allowed_prices=("1,7", "3", "5")
        )

        with self.assertRaises(publisher.UserActionRequired) as caught:
            await publisher._step_fill_view_price(
                page,
                publisher.LocationData("Мурманск", "улица Воровского, 11"),
                1,
                16,
                view_price_max=Decimal("1.5"),
            )

        self.assertEqual(caught.exception.user_action["type"], "view_price_cap_exceeded")
        self.assertEqual(caught.exception.user_action["maximum_view_price"], "1.5")
        self.assertEqual(caught.exception.user_action["minimum_view_price"], "1.7")
        self.assertEqual(action.clicks, 0)

    async def test_minimum_price_button_without_legacy_marker_advances(self) -> None:
        page, action = make_price_page(
            allowed_prices=("0,4", "2", "4"),
        )
        action.on_click = lambda: setattr(
            page,
            "url",
            "https://www.avito.ru/pro/performance?vasFrom=avito_osp_applied",
        )

        await publisher._step_fill_view_price(
            page,
            publisher.LocationData("Санкт-Петербург", "Невский, 1"),
            1,
            1,
            view_price_max=Decimal("0.4"),
        )
        await publisher._step_continue_view_price(page, "8330238411")

        self.assertEqual(action.clicks, 1)
        self.assertEqual(page.url, "https://www.avito.ru/pro/performance?vasFrom=avito_osp_applied")

    async def test_home_press_waits_for_delayed_price_value(self) -> None:
        """Slider отдаёт цену не сразу (delayed_price_update) — Home ждёт
        обновления price_input, а не читает его раньше времени."""
        page, action = make_price_page(
            allowed_prices=("0,4", "2", "4"),
            delayed_price_update=True,
            slider_initial_index=2,  # начинаем не с минимума
        )

        actual = await publisher._step_fill_view_price(
            page,
            publisher.LocationData("Санкт-Петербург", "Невский, 1"),
            1,
            1,
            view_price_max=Decimal("2"),
        )

        self.assertEqual(page.locator(psel.VIEW_PRICE_INPUT).value, "0,4")
        self.assertEqual(actual, Decimal("0.4"))
        self.assertEqual(action.clicks, 0)

    async def test_unrendered_price_never_passes_as_avito_minimum(self) -> None:
        """Поле цены ещё не отрисовано, а после Home React дописывает в него
        значение ПРЕЖНЕЙ позиции. Это значение выше минимума — принять его
        за минимум нельзя, движок обязан остановиться до денег."""
        prices = ("0,4", "2", "4")
        price_input = FakeLocator(value="")
        slider = HydratingFakeSlider(price_input, prices, initial_index=1)
        page = FakePage(
            "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add",
            {psel.VIEW_PRICE_INPUT: price_input, psel.VIEW_PRICE_SLIDER: slider},
        )

        with self.assertRaisesRegex(publisher.StepError, "не отрисовалась"):
            await publisher._select_minimum_view_price(
                page, price_input, Decimal("2.5"), 1, 5,
            )

        self.assertEqual(slider.pressed_keys, ["Home"])

    async def test_late_rendered_price_is_awaited_and_minimum_is_taken(self) -> None:
        """Обратная сторона той же защиты: поле гидратируется по таймеру —
        движок дожидается значения и берёт настоящий минимум, а не падает."""
        prices = ("0,4", "2", "4")
        price_input = FakeLocator(value="")
        slider = HydratingFakeSlider(
            price_input, prices, initial_index=1, hydrate_after_s=0.2,
        )
        page = FakePage(
            "https://www.avito.ru/cpxpromo/8330238411?vasFrom=item_add",
            {psel.VIEW_PRICE_INPUT: price_input, psel.VIEW_PRICE_SLIDER: slider},
        )

        actual = await publisher._select_minimum_view_price(
            page, price_input, Decimal("2.5"), 1, 5,
        )

        self.assertEqual(actual, "0,4")
        self.assertEqual(slider.pressed_keys, ["Home"])

    async def test_unknown_manual_mode_stops_closed(self) -> None:
        page, action = make_price_page()
        page.locator(psel.VIEW_PRICE_MODE_SWITCH).attrs["aria-checked"] = "true"

        with self.assertRaisesRegex(publisher.StepError, "ручн"):
            await publisher._step_fill_view_price(
                page,
                publisher.LocationData("Санкт-Петербург", "Невский, 1"),
                1,
                1,
                view_price_max=Decimal("0.5"),
            )
        self.assertEqual(action.clicks, 0)

    async def test_price_action_is_never_retried_after_uncertain_timeout(self) -> None:
        page, action = make_price_page()
        with (
            mock.patch.object(
                publisher,
                "_wait_until",
                new=mock.AsyncMock(return_value=False),
            ),
            self.assertRaisesRegex(publisher.StepError, "не подтверждён"),
        ):
            await publisher._step_continue_view_price(page, "8330238411")

        self.assertEqual(action.clicks, 1)


class ServicesAndSuccessTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _services_page(*, unknown: bool = False) -> tuple[FakePage, FakeLocator]:
        switches: list[FakeLocator] = []
        widget_markers: list[str] = []
        for marker in psel.SERVICE_SWITCH_MARKERS:
            checkbox = FakeLocator(checked=False)
            switches.append(FakeLocator(
                attrs={"data-marker": marker, "aria-checked": "false"},
                children={"input[type='checkbox']": checkbox},
            ))
            widget_markers.extend([marker.removesuffix("/switcher"), marker])
        if unknown:
            marker = "new-configurator/widget/surprise/switcher"
            switches.append(FakeLocator(
                attrs={"data-marker": marker, "aria-checked": "false"},
                children={"input[type='checkbox']": FakeLocator(checked=False)},
            ))
            widget_markers.extend([marker.removesuffix("/switcher"), marker])

        button = FakeLocator(text="Продолжить без услуг")
        page = FakePage(
            "https://www.avito.ru/pro/performance?vasFrom=avito_osp_applied",
            {
                psel.SERVICES_ALL_SWITCHES: FakeLocator(elements=switches),
                psel.SERVICES_ALL_WIDGETS: FakeLocator(elements=[
                    FakeLocator(attrs={"data-marker": marker})
                    for marker in widget_markers
                ]),
                psel.SERVICES_CONTINUE_WITHOUT_BUTTON: button,
            },
        )
        return page, button

    async def test_unknown_service_blocks_continue_without_services(self) -> None:
        page, button = self._services_page(unknown=True)

        with self.assertRaisesRegex(publisher.StepError, "неизвест"):
            await publisher._step_skip_services(page)

        self.assertEqual(button.clicks, 0)

    async def test_known_off_services_click_exact_button_once(self) -> None:
        page, button = self._services_page()
        button.on_click = lambda: setattr(
            page, "url", "https://www.avito.ru/profile/pro/items"
        )

        await publisher._step_skip_services(page)

        self.assertEqual(button.clicks, 1)

    async def test_services_wait_for_delayed_react_render_before_click(self) -> None:
        page, button = self._services_page()
        for selector in (
            psel.SERVICES_ALL_WIDGETS,
            psel.SERVICES_ALL_SWITCHES,
        ):
            current = page._locators[selector]
            page._locators[selector] = DelayedFakeLocator(
                empty_counts=1,
                elements=current.elements or [],
            )
        button.on_click = lambda: setattr(
            page, "url", "https://www.avito.ru/profile/pro/items"
        )

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ):
            await publisher._step_skip_services(page)

        self.assertEqual(button.clicks, 1)
        self.assertEqual(page.url, "https://www.avito.ru/profile/pro/items")

    async def test_blank_services_page_reloads_once_before_validation(self) -> None:
        page, button = self._services_page()
        page.reload_calls = 0

        async def reload(*_args: object, **_kwargs: object) -> None:
            page.reload_calls += 1

        page.reload = reload
        button.on_click = lambda: setattr(
            page, "url", "https://www.avito.ru/profile/pro/items"
        )

        with mock.patch.object(
            publisher,
            "_wait_until",
            new=mock.AsyncMock(side_effect=[False, True, True]),
        ):
            await publisher._step_skip_services(page)

        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(button.clicks, 1)
        self.assertEqual(page.url, "https://www.avito.ru/profile/pro/items")


class PhotoUploadTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_input_files_uses_full_photo_upload_timeout(self) -> None:
        class PreviewLocator:
            async def count(self) -> int:
                return 1

        class SlowUploadPage:
            def __init__(self) -> None:
                self.timeout_ms: float | None = None

            async def set_input_files(
                self,
                _selector: str,
                _paths: list[str],
                *,
                timeout: float,
            ) -> None:
                self.timeout_ms = timeout

            def locator(self, _selector: str) -> PreviewLocator:
                return PreviewLocator()

        page = SlowUploadPage()
        with (
            mock.patch.object(
                publisher,
                "_attach_photo_network_probe",
                return_value=None,
            ),
            mock.patch.object(
                publisher,
                "_diag_snap_previews",
                new=mock.AsyncMock(),
            ),
            mock.patch.object(
                publisher.asyncio,
                "sleep",
                new=mock.AsyncMock(),
            ),
        ):
            await publisher._step_upload_photos(page, ("photo.jpg",))

            self.assertEqual(
                page.timeout_ms,
                publisher.PHOTO_UPLOAD_TIMEOUT_S * 1000,
            )

    async def test_incomplete_photo_previews_stop_before_publication(self) -> None:
        class PreviewLocator:
            async def count(self) -> int:
                return 1

        class IncompleteUploadPage:
            async def set_input_files(
                self,
                _selector: str,
                _paths: list[str],
                *,
                timeout: float,
            ) -> None:
                del timeout

            def locator(self, _selector: str) -> PreviewLocator:
                return PreviewLocator()

        with (
            mock.patch.object(publisher, "PHOTO_UPLOAD_TIMEOUT_S", 0.01),
            mock.patch.object(
                publisher,
                "_attach_photo_network_probe",
                return_value=None,
            ),
            mock.patch.object(
                publisher,
                "_diag_snap_previews",
                new=mock.AsyncMock(),
            ),
            mock.patch.object(
                publisher.asyncio,
                "sleep",
                new=mock.AsyncMock(),
            ),
            self.assertRaisesRegex(publisher.StepError, "1/2 превью"),
        ):
            await publisher._step_upload_photos(
                IncompleteUploadPage(),
                ("photo-1.jpg", "photo-2.jpg"),
            )

if __name__ == "__main__":
    unittest.main()
