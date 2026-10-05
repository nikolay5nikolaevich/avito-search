"""Отбор продавцов и защита от повторной отправки (без живого Авито)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))

import outreach


def card(profile="/brands/shop", reviews="50 отзывов", item="phone_123", city="moskva"):
    return f"""<div data-marker="item">
        <a data-marker="item-title" href="/{city}/telefony/{item}?context=abc">Телефон</a>
        <a href="{profile}">Магазин</a>
        <span data-marker="seller-info/summary">{reviews}</span>
    </div>"""


class ParserTests(unittest.TestCase):
    def test_review_count_requires_explicit_integer_reviews(self):
        for text, expected in [
            ("50 отзывов", 50), ("4 851 отзыв", 4851),
            ("1\u00a0483 отзыва", 1483), ("2\u202f343 отзыва", 2343),
            ("5,0 · 86 отзывов", 86), ("49 отзывов", 49),
            ("5,0", None), ("Нет отзывов", None), ("1,5 тыс. отзывов", None),
            ("4.5 отзывов", None), ("", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(outreach.parse_review_count(text), expected)

    def test_threshold_locality_and_both_identity_aliases(self):
        html = (
            card("/brands/first?src=search&sellerId=abc123", "50 отзывов")
            + card("/brands/first/all?src=other", "60 отзывов", "phone_124")
            + card("/brands/alias?sellerId=abc123", "60 отзывов", "phone_125")
            + card("/brands/low", "49 отзывов", "phone_126")
            + card("/brands/unknown", "5,0", "phone_127")
            + card("/brands/remote", "100 отзывов", "phone_128", "kazan")
        )
        result = outreach.extract_candidates_from_html(html, "moskva", "Телефон")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {
            "seller_key": "seller:abc123", "seller_id": "abc123",
            "profile_url": "https://www.avito.ru/brands/first",
            "seller_name": "Магазин", "review_count": 50,
            "listing_url": "https://www.avito.ru/moskva/telefony/phone_123",
            "city": "moskva", "query": "Телефон",
        })

    def test_unsafe_links_and_missing_seller_never_become_candidates(self):
        html = card("https://evil.example/brands/shop") + card("javascript:alert(1)")
        html += card().replace("/moskva/telefony/phone_123", "https://evil.example/moskva/item")
        self.assertEqual(outreach.extract_candidates_from_html(html, "moskva", "q"), [])


class FakeLocator:
    def __init__(self, page, kind, index=0):
        self.page = page
        self.kind = kind
        self.index = index

    @property
    def first(self):
        return self

    def filter(self, **kwargs):
        return self

    async def all(self):
        # Только кнопка «Написать» встречается в нескольких копиях на странице.
        if self.kind == "open":
            return [FakeLocator(self.page, "open", i)
                    for i in range(len(self.page.open_buttons))]
        return [self]

    async def wait_for(self, **kwargs):
        if self.kind == "widget" and not self.page.widget_attached:
            raise TimeoutError("Виджет мини-мессенджера не найден")
        return None

    async def count(self):
        if self.kind == "auth":
            return 0
        if self.kind == "send":
            # Живой факт: span[data-marker='reply/send'] появляется в DOM
            # ТОЛЬКО после ввода текста. Поиск кнопки до fill() обязан дать 0.
            return 1 if self.page.message and self.page.send_present else 0
        if self.kind == "expand":
            return self.page.expand_available
        return 1

    async def is_visible(self):
        if self.kind == "open":
            return self.page.open_buttons[self.index].get("visible", True)
        return self.kind != "auth"

    async def is_enabled(self):
        raise AssertionError("is_enabled() на <span> ничего не доказывает — нужен aria-disabled")

    async def bounding_box(self):
        if self.kind == "open":
            return self.page.open_buttons[self.index].get("box")
        return None

    async def scroll_into_view_if_needed(self, **kwargs):
        return None

    async def inner_text(self):
        if self.kind == "open":
            return self.page.open_buttons[self.index].get("label", "Написать")
        # У кнопки отправки видимого текста нет: это <span> с одной aria-label.
        return ""

    async def get_attribute(self, name):
        if self.kind == "send" and name == "aria-label":
            return self.page.send_label
        if self.kind == "send" and name == "aria-disabled":
            return self.page.send_aria_disabled
        return None

    async def fill(self, value):
        self.page.message = value[:self.page.truncate_to] if self.page.truncate_to else value

    async def input_value(self):
        return self.page.message

    async def click(self, **kwargs):
        if self.kind == "send":
            self.page.send_clicks += 1
            if self.page.click_error:
                raise TimeoutError("Клик завершился неоднозначно")
        elif self.kind == "open":
            self.page.open_clicks += 1
            self.page.open_click_indexes.append(self.index)
            if self.page.open_navigates_to:
                self.page.url = self.page.open_navigates_to
        elif self.kind == "expand":
            self.page.expand_clicks += 1
        else:
            raise AssertionError("Непредусмотренный клик")

    async def all_text_contents(self):
        return []


class FakePage:
    def __init__(self, *, collection=False, ambiguous=False, click_error=False,
                 send_label="Отправить сообщение", navigation_error=False,
                 send_aria_disabled="false", send_present=True, truncate_to=0,
                 composer_failures=0, expand_available=0, open_navigates_to="",
                 widget_attached=True, open_buttons=None,
                 reload_fixes_widget=False, unread_counter_count=0):
        self.collection = collection
        self.ambiguous = ambiguous
        self.click_error = click_error
        self.navigation_error = navigation_error
        self.send_label = send_label
        self.send_aria_disabled = send_aria_disabled
        self.send_present = send_present
        self.truncate_to = truncate_to
        self.composer_failures = composer_failures
        self.expand_available = expand_available
        self.open_navigates_to = open_navigates_to
        self.widget_attached = widget_attached
        # Гипотеза «клиентский чанк завис» — перезагрузка чинит виджет.
        self.reload_fixes_widget = reload_fixes_widget
        self.unread_counter_count = unread_counter_count
        self.reloads = 0
        # По умолчанию одна видимая копия кнопки «Написать» в вьюпорте (y > 0),
        # как на живой странице после подгрузки виджета.
        self.open_buttons = open_buttons if open_buttons is not None else [
            {"box": {"x": 927, "y": 307, "width": 157, "height": 52}, "visible": True},
        ]
        self.send_clicks = 0
        self.open_clicks = 0
        self.open_click_indexes = []
        self.expand_clicks = 0
        self.outgoing = 0
        self.message = ""
        self.url = "https://www.avito.ru"
        self.navigations = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.url = url
        self.navigations.append(url)
        if self.navigation_error:
            raise TimeoutError("Не загрузилось объявление")
        return None

    async def reload(self, **kwargs):
        # Безопасный GET на _wait_for_widget: если виджет завис из-за
        # ленивого чанка, перезагрузка страницы его чинит.
        self.reloads += 1
        if self.reload_fixes_widget:
            self.widget_attached = True
        return None

    async def content(self):
        if self.collection:
            return card() if len(self.navigations) == 1 else "По вашему запросу ничего не найдено"
        return '<a data-marker="seller-link/link" href="/brands/shop">Магазин</a>'

    async def title(self):
        return "Авито"

    async def wait_for_selector(self, selector, **kwargs):
        if "reply/input" in selector and self.composer_failures > 0:
            self.composer_failures -= 1
            raise TimeoutError("Поле ввода чата не появилось")
        return None

    async def evaluate(self, script, arg=None):
        # Три формы диагностики: состояние отправки, состояние виджета
        # (state.txt при остановке на open_chat), подсчёт — числом.
        if isinstance(arg, dict) and "miniMessenger" in arg:
            return {
                "miniMessenger": 1 if self.widget_attached else 0,
                "unreadCounter": self.unread_counter_count,
                "chatButton": len(self.open_buttons),
            }
        if isinstance(arg, dict) and "input" in arg:
            return {"outgoing": self.outgoing, "empty": not self.message}
        return self.outgoing

    async def wait_for_function(self, script, **kwargs):
        if self.ambiguous:
            raise TimeoutError("Нет подтверждения")
        self.outgoing += 1
        self.message = ""
        return None

    def locator(self, selector):
        if "seller-link" in selector:
            return FakeLocator(self, "seller")
        if "messenger-button/button" in selector:
            return FakeLocator(self, "open")
        # Более специфичный маркер — раньше общего, иначе он проглотит его как подстроку.
        if "mini-messenger/expand" in selector:
            return FakeLocator(self, "expand")
        if "mini-messenger" in selector:
            return FakeLocator(self, "widget")
        if "reply/input" in selector:
            return FakeLocator(self, "input")
        if "reply/send" in selector:
            return FakeLocator(self, "send")
        return FakeLocator(self, "auth")

    async def close(self):
        self.closed = True


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.candidate = {
            "seller_key": "profile:https://www.avito.ru/brands/shop", "seller_id": None,
            "profile_url": "https://www.avito.ru/brands/shop", "seller_name": "Магазин",
            "review_count": 50, "listing_url": "https://www.avito.ru/moskva/telefony/phone_123",
            "city": "moskva", "query": "Телефон",
        }
        self.records = []
        self.contacted = False

    def browser(self, page):
        manager = AsyncMock()
        context = AsyncMock()
        context.new_page.return_value = page
        self.enterContext(patch.object(outreach, "async_playwright", return_value=manager))
        self.enterContext(patch.object(outreach, "connect_over_cdp", AsyncMock(return_value=context)))
        self.enterContext(patch.object(outreach, "_wait_for_cards_and_scroll", AsyncMock()))
        self.enterContext(patch.object(outreach, "_random_delay", AsyncMock()))
        self.enterContext(patch.object(outreach, "is_contacted",
                                      side_effect=lambda sid, url: self.contacted))
        self.enterContext(patch.object(outreach, "record_contact", side_effect=self.record))
        # Остановки в этих тестах реальны (OutreachStoppedError) — дамп пишет
        # на диск (outreach._dump_stop_state); уводим его во временную папку,
        # чтобы тесты не засоряли настоящий debug/ проекта.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.enterContext(patch.object(outreach, "DEBUG_DIR", Path(tmp_dir.name)))
        return context

    def record(self, candidate, status, **kwargs):
        self.records.append((status, kwargs))
        if status in {"sending", "sent", "uncertain"}:
            self.contacted = True

    async def test_collection_never_opens_chat_and_closes_only_owned_page(self):
        page = FakePage(collection=True)
        context = self.browser(page)
        result = await outreach.collect_candidates("moskva", "Телефон", 15)
        self.assertEqual(len(result), 1)
        self.assertEqual(page.open_clicks + page.send_clicks, 0)
        self.assertEqual(self.records, [])
        self.assertTrue(page.closed)
        context.close.assert_not_awaited()

    async def test_success_records_pending_then_sent_and_returns_to_search(self):
        page = FakePage()
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Мой точный\nшаблон")
        self.assertEqual(page.send_clicks, 1)
        self.assertEqual([r[0] for r in self.records], ["sending", "sent"])
        self.assertEqual(self.records[-1][1]["message"], "Мой точный\nшаблон")
        self.assertEqual(result["sent"], 1)
        self.assertIn("?q=", page.navigations[-1])

    async def test_exact_404_listing_is_failed_and_next_candidate_is_sent(self):
        class NotFoundPage(FakePage):
            async def title(self):
                if "item_404" in self.url:
                    return "Ошибка 404. Страница не найдена"
                return await super().title()

            async def wait_for_selector(self, selector, **kwargs):
                if "item_404" in self.url and "seller-link" in selector:
                    raise TimeoutError("Продавец на 404-странице отсутствует")
                return await super().wait_for_selector(selector, **kwargs)

        page = NotFoundPage()
        self.browser(page)
        missing = {**self.candidate, "listing_url": self.candidate["listing_url"].replace("phone_123", "item_404")}
        result = await outreach.send_messages([missing, self.candidate], "Message")

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual([row["status"] for row in result["results"]], ["failed", "sent"])
        self.assertEqual(page.open_clicks, 1)
        self.assertEqual(page.send_clicks, 1)

    async def test_open_chat_clicks_without_explicit_scroll_when_button_rerenders(self):
        page = FakePage()
        self.browser(page)
        opener = FakeLocator(page, "open")
        opener.scroll_into_view_if_needed = AsyncMock(
            side_effect=RuntimeError("Element is not attached to the DOM"),
        )
        with patch.object(outreach, "_pick_chat_button", AsyncMock(return_value=opener)):
            result = await outreach.send_messages([self.candidate], "Message")

        opener.scroll_into_view_if_needed.assert_not_awaited()
        self.assertEqual(page.open_clicks, 1)
        self.assertEqual(page.send_clicks, 1)
        self.assertEqual(result["sent"], 1)

    async def test_navigation_failure_is_failed_without_click(self):
        page = FakePage(navigation_error=True)
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])
        self.assertEqual(result["failed"], 1)

    async def test_unknown_button_stops_before_click(self):
        # Подпись <span> живёт только в aria-label — её и проверяем.
        page = FakePage(send_label="Оплатить")
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError):
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])

    async def test_aria_disabled_send_button_stops_before_click(self):
        page = FakePage(send_aria_disabled="true")
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError):
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])

    async def test_missing_send_button_stops_before_click(self):
        page = FakePage(send_present=False)
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError):
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])

    async def test_template_longer_than_maxlength_never_opens_browser(self):
        page = FakePage()
        self.browser(page)
        with self.assertRaises(ValueError):
            await outreach.send_messages([self.candidate], "я" * 1001)
        self.assertEqual(page.navigations, [])
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual(self.records, [])

    def test_maxlength_counts_utf16_units_like_the_browser(self):
        # Эмодзи вне BMP занимают в поле Авито две единицы, а не одну.
        self.assertEqual(outreach._ui_length("ab"), 2)
        self.assertEqual(outreach._ui_length("🙂"), 2)
        outreach._check_message_fits("🙂" * 500)
        with self.assertRaises(ValueError):
            outreach._check_message_fits("🙂" * 501)

    async def test_truncated_text_stops_before_click(self):
        # Если Авито всё же обрежет текст, обратное чтение обязано остановить пакет.
        page = FakePage(truncate_to=3)
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError):
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])

    async def test_chat_opened_outside_item_page_stops_before_click(self):
        # «Переписка уже есть»: Авито увёл на страницу диалога — продавец не подтверждён.
        page = FakePage(open_navigates_to="https://www.avito.ru/profile/messenger/channel/u2i-1")
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError):
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.open_clicks, 1)
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])

    async def test_collapsed_mini_messenger_is_expanded_once_then_send_continues(self):
        page = FakePage(composer_failures=1, expand_available=1)
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.expand_clicks, 1)
        self.assertEqual(page.send_clicks, 1)
        self.assertEqual(result["sent"], 1)

    async def test_composer_never_appears_marks_candidate_chat_unavailable(self):
        # Каждый полный заход (клик + фолбэк-expand) тратит 2 неудачи поля
        # ввода; при трёх заходах это 6 неудач подряд.
        page = FakePage(composer_failures=6, expand_available=1)
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.open_clicks, 3)
        self.assertEqual(page.expand_clicks, 3)
        self.assertEqual(page.reloads, 2)
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["results"][0]["error"], "Чат недоступен")

    async def test_missing_widget_reloads_twice_then_marks_chat_unavailable(self):
        # Виджет мини-мессенджера так и не подгрузился (живой прогон
        # 22.09.2026: debug/outreach_stop_20260922T063358Z/) — вместо
        # немедленной остановки делаем одну перезагрузку страницы (безопасный
        # GET) и ждём виджет снова; третья неудача останавливает пакет раньше,
        # чем мы пытаемся кликнуть по «Написать».
        page = FakePage(widget_attached=False)
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.reloads, 2)
        self.assertEqual(page.open_clicks, 0)
        self.assertEqual([r[0] for r in self.records], ["failed"])
        self.assertEqual(result["results"][0]["error"], "Чат недоступен")

    async def test_unavailable_chat_moves_to_next_candidate(self):
        class FirstListingHasNoChatPage(FakePage):
            async def goto(self, url, **kwargs):
                await super().goto(url, **kwargs)
                if "phone_456" in url:
                    self.widget_attached = True

        page = FirstListingHasNoChatPage(widget_attached=False)
        self.browser(page)
        next_candidate = {**self.candidate, "listing_url": self.candidate["listing_url"].replace("123", "456")}

        result = await outreach.send_messages([self.candidate, next_candidate], "Письмо")

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["results"][0]["error"], "Чат недоступен")
        self.assertEqual(page.send_clicks, 1)
        self.assertEqual(page.reloads, 2)

    async def test_missing_chat_button_moves_to_next_candidate_after_reload(self):
        class FirstListingHasNoButtonPage(FakePage):
            async def goto(self, url, **kwargs):
                await super().goto(url, **kwargs)
                if "phone_456" in url:
                    self.open_buttons = [{"box": {"x": 1, "y": 1, "width": 1, "height": 1}}]

        page = FirstListingHasNoButtonPage(open_buttons=[])
        self.browser(page)
        next_candidate = {**self.candidate, "listing_url": self.candidate["listing_url"].replace("123", "456")}

        result = await outreach.send_messages([self.candidate, next_candidate], "Письмо")

        self.assertEqual(page.reloads, 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 1)

    async def test_unknown_chat_button_stops_before_next_candidate(self):
        page = FakePage(open_buttons=[{
            "label": "Связаться", "box": {"x": 1, "y": 1, "width": 1, "height": 1},
        }])
        self.browser(page)
        next_candidate = {**self.candidate, "listing_url": self.candidate["listing_url"].replace("123", "456")}

        with self.assertRaises(outreach.OutreachStoppedError):
            await outreach.send_messages([self.candidate, next_candidate], "Письмо")

        self.assertEqual(page.navigations, [self.candidate["listing_url"]])
        self.assertEqual(page.reloads, 0)

    async def test_redirect_on_second_chat_attempt_stops_package(self):
        page = FakePage()
        page.url = self.candidate["listing_url"]
        attempts = 0

        async def _second_attempt_redirects(*_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return False
            page.url = "https://www.avito.ru/profile/messenger/channel/u2i-1"
            raise outreach.ChatUnavailableError("Чат недоступен")

        with patch.object(outreach, "_open_chat_once", side_effect=_second_attempt_redirects):
            with self.assertRaises(outreach.OutreachStoppedError) as error:
                await outreach._open_chat(page, "/moskva/telefony/phone_123", {"reloaded": False})

        self.assertNotIsInstance(error.exception, outreach.ChatUnavailableError)
        self.assertIn("страницы объявления", str(error.exception))
        self.assertEqual(page.reloads, 1)

    async def test_redirect_during_last_composer_wait_stops_package(self):
        page = FakePage()
        page.url = self.candidate["listing_url"]
        attempts = 0

        async def _last_wait_returns_false_after_redirect(*_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return False
            page.url = "https://www.avito.ru/profile/messenger/channel/u2i-1"
            return False

        with patch.object(outreach, "_open_chat_once", side_effect=_last_wait_returns_false_after_redirect):
            with self.assertRaises(outreach.OutreachStoppedError) as error:
                await outreach._open_chat(page, "/moskva/telefony/phone_123", {"reloaded": False})

        self.assertNotIsInstance(error.exception, outreach.ChatUnavailableError)
        self.assertIn("страницы объявления", str(error.exception))
        self.assertEqual(page.reloads, 1)

    async def test_widget_missing_but_reload_fixes_it_then_send_continues(self):
        # Гипотеза «клиентский чанк завис в этой вкладке» — перезагрузка
        # лечит, письмо уходит штатно, повторной перезагрузки не требуется.
        page = FakePage(widget_attached=False, reload_fixes_widget=True)
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.reloads, 1)
        self.assertEqual(page.open_clicks, 1)
        self.assertEqual(result["sent"], 1)

    async def test_picks_in_viewport_copy_among_two_button_copies(self):
        # Как на живой странице: «липкая» панель (y < 0, вне вьюпорта) и копия
        # в блоке продавца (y > 0) — кликать нужно по второй.
        page = FakePage(open_buttons=[
            {"box": {"x": 1136, "y": -50, "width": 106, "height": 52}, "visible": True},
            {"box": {"x": 927, "y": 307, "width": 157, "height": 52}, "visible": True},
        ])
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.open_click_indexes, [1])
        self.assertEqual(result["sent"], 1)

    async def test_composer_appears_only_on_second_attempt(self):
        # Первый заход не даёт поля (composer_failures=2 тратятся на клик +
        # фолбэк-expand), второй заход — полноценный успех без фолбэка.
        page = FakePage(composer_failures=2, expand_available=1)
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.open_clicks, 2)
        self.assertEqual(page.expand_clicks, 1)
        self.assertEqual(result["sent"], 1)

    async def test_unconfirmed_send_reports_feed_and_input_state(self):
        page = FakePage(ambiguous=True)
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError) as error:
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 1)
        self.assertIn("исходящих сообщений было 0", str(error.exception))
        self.assertEqual([r[0] for r in self.records], ["sending", "uncertain"])
        # Счётчик обязан отражать запись "uncertain" в results, иначе сводка
        # пользователю солжёт, что ничего не произошло.
        self.assertEqual(error.exception.result["uncertain"], 1)
        self.assertEqual(error.exception.result["failed"], 0)

    async def test_ambiguous_result_and_click_exception_never_retry(self):
        for option in ("ambiguous", "click_error"):
            with self.subTest(option=option):
                self.records = []
                self.contacted = False
                page = FakePage(**{option: True})
                self.browser(page)
                with self.assertRaises(outreach.OutreachStoppedError):
                    await outreach.send_messages([self.candidate, self.candidate], "Письмо")
                self.assertEqual(page.send_clicks, 1)
                self.assertEqual([r[0] for r in self.records], ["sending", "uncertain"])
                self.assertTrue(page.closed)

    async def test_already_contacted_seller_is_rechecked_before_send(self):
        page = FakePage()
        self.browser(page)
        self.contacted = True
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual(result["skipped"], 1)

    async def test_import_during_chat_preparation_prevents_final_click(self):
        page = FakePage()
        self.browser(page)
        with patch.object(outreach, "is_contacted", side_effect=[False, True]):
            result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(page.send_clicks, 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.records, [])

    async def test_waits_for_react_seller_before_identity_check(self):
        class DelayedPage(FakePage):
            ready = False

            async def wait_for_selector(self, selector, **kwargs):
                if "seller-link" in selector:
                    self.ready = True

            async def content(self):
                return await super().content() if self.ready else "<div>Загрузка</div>"

        page = DelayedPage()
        self.browser(page)
        result = await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(result["sent"], 1)

    async def test_return_failure_preserves_confirmed_sent_result(self):
        class ReturnFailurePage(FakePage):
            async def goto(self, url, **kwargs):
                if "?q=" in url:
                    raise TimeoutError("Не открылась выдача")
                return await super().goto(url, **kwargs)

        page = ReturnFailurePage()
        self.browser(page)
        with self.assertRaises(outreach.OutreachStoppedError) as error:
            await outreach.send_messages([self.candidate], "Письмо")
        self.assertEqual(error.exception.result["sent"], 1)
        self.assertEqual([r[0] for r in self.records], ["sending", "sent"])


if __name__ == "__main__":
    unittest.main()
