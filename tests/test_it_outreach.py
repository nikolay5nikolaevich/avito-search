"""Проверки бесплатного discovery URL-кандидатов через Taginfo."""

from __future__ import annotations

import json
from email.message import Message
import pathlib
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

import it_outreach  # noqa: E402


class _Response:
    """Минимальный ответ Taginfo для подмены внешнего HTTP."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._body = json.dumps(payload).encode()
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self._body


class _HtmlResponse:
    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self.body = html.encode("utf-8")
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def __enter__(self) -> _HtmlResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, _size: int) -> bytes:
        return self.body


_PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]


class ItOutreachTests(unittest.TestCase):
    """Контракты нормализации и получения кандидатов из Taginfo."""

    def test_normalizes_public_urls_and_rejects_unsafe_or_irrelevant_values(self) -> None:
        """Ловит допуск IP, userinfo, соцсетей и служебных имён в кандидаты."""
        self.assertEqual(
            it_outreach.normalize_site_url("example.ru/path"),
            "https://example.ru/path",
        )
        self.assertEqual(
            it_outreach.normalize_site_url("http://www.example.ru/"),
            "http://www.example.ru/",
        )
        self.assertEqual(
            it_outreach.domain_key("https://пример.рф/contacts?x=1"),
            "xn--e1afmkfd.xn--p1ai",
        )
        for value in (
            "ftp://example.ru",
            "https://user@example.ru",
            "https://127.0.0.1",
            "https://localhost",
            "https://vk.com/company",
            "https://yandex.ru/maps",
            "https://avito.ru/item",
            "https://mvd.ru/news",
            "https://transport.mos.ru",
            "https://fsin.su/news",
            "https://edusite.ru/about",
            "https://caduk.ru/contacts",
            "https://school.example.ru",
            "https://one.example.ru;https://two.example.ru",
            "not a url",
        ):
            self.assertEqual(it_outreach.normalize_site_url(value), "", value)

    def test_discovers_unique_public_domains_and_skips_known_ones(self) -> None:
        """Ловит возврат дублей, запрещённых URL и уже проверенных доменов."""
        response = _Response(
            {
                "total": 100,
                "data": [
                    {"value": "https://www.example.ru/about"},
                    {"value": "https://vk.com/example"},
                    {"value": "known.ru"},
                    {"value": "https://shop.example.org/catalog"},
                    {"value": "https://example.ru/contacts"},
                ],
            }
        )
        with patch("it_outreach.urlopen", return_value=response), patch(
            "it_outreach.sleep"
        ):
            found = it_outreach.discover_sites(2, {"known.ru"})

        self.assertEqual(
            found,
            ["https://www.example.ru/about", "https://shop.example.org/catalog"],
        )

    def test_returns_empty_result_when_taginfo_total_is_zero(self) -> None:
        """Ловит выборку случайных страниц из пустого диапазона Taginfo."""
        with patch(
            "it_outreach.urlopen", return_value=_Response({"total": 0, "data": []})
        ) as get, patch("it_outreach.sleep"):
            found = it_outreach.discover_sites(1)

        self.assertEqual(found, [])
        self.assertEqual(get.call_count, 1)

    def test_stops_after_six_taginfo_pages_when_candidates_are_filtered(self) -> None:
        """Ловит бесконечное чтение Taginfo, когда в ответах нет кандидатов."""
        response = _Response(
            {"total": 1000, "data": [{"value": "https://avito.ru/item"}]}
        )
        with patch("it_outreach.urlopen", return_value=response) as get, patch(
            "it_outreach.sleep"
        ) as sleep:
            found = it_outreach.discover_sites(1)

        self.assertEqual(found, [])
        self.assertEqual(get.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_prefers_later_random_pages_over_abundant_first_page(self) -> None:
        """Ловит подмену бедной случайной выборки началом сортировки Taginfo."""
        first_page = _Response(
            {
                "total": 1000,
                "data": [
                    {"value": f"https://first-{index}.example.ru"}
                    for index in range(1, 11)
                ],
            }
        )
        fetched_pages: list[int] = []

        def fake_open(request: Request, timeout: int) -> _Response:
            page = int(parse_qs(urlsplit(request.full_url).query)["page"][0])
            fetched_pages.append(page)
            if page == 1:
                return first_page
            if len(fetched_pages) == 2:
                return _Response(
                    {"total": 1000, "data": [{"value": "https://transport.mos.ru"}]}
                )
            return _Response(
                {
                    "total": 1000,
                    "data": [
                        {"value": f"https://random-{page}-{index}.example.ru"}
                        for index in range(1, 6)
                    ],
                }
            )

        with patch("it_outreach.urlopen", side_effect=fake_open), patch(
            "it_outreach.sleep"
        ) as sleep:
            found = it_outreach.discover_sites(5)

        self.assertEqual(len(fetched_pages), 3)
        self.assertEqual(fetched_pages[0], 1)
        random_page = fetched_pages[2]
        self.assertEqual(
            found,
            [
                f"https://random-{random_page}-1.example.ru",
                f"https://random-{random_page}-2.example.ru",
                f"https://random-{random_page}-3.example.ru",
                f"https://random-{random_page}-4.example.ru",
                f"https://random-{random_page}-5.example.ru",
            ],
        )
        self.assertEqual(sleep.call_count, 2)

    def test_inspects_home_and_relevant_pages_and_prefers_hiring_email(self) -> None:
        pages = {
            "https://acme.ru/": """
                <html><head><meta property="og:site_name" content="Акме"></head>
                <body><a href="/contacts">Контакты</a>
                <a href="/career">Вакансии</a>
                <a href="/internship">Стажировки</a>
                <a href="https://outside.ru/jobs">Чужая вакансия</a>
                <p>info@acme.ru</p></body></html>""",
            "https://acme.ru/contacts": """
                <html><body><address>г. Киров, ул. Ленина, 1</address>
                <a href="mailto:support@acme.ru">Поддержка</a></body></html>""",
            "https://acme.ru/career": """
                <html><body><h1>Работа в Акме</h1>
                <a href="mailto:hr@acme.ru">hr@acme.ru</a></body></html>""",
            "https://acme.ru/internship": "<html><body>Стажировка</body></html>",
        }
        fetched = []

        def fake_open(request: Request, timeout: int) -> _HtmlResponse:
            url = request.full_url
            fetched.append(url)
            return _HtmlResponse(url, pages[url])

        with patch("it_outreach.socket.getaddrinfo", return_value=_PUBLIC_DNS), patch(
            "it_outreach.build_opener", return_value=SimpleNamespace(open=fake_open)
        ):
            result = it_outreach.inspect_site("https://acme.ru/")

        self.assertEqual(result["domain"], "acme.ru")
        self.assertEqual(result["company_name"], "Акме")
        self.assertEqual(result["city"], "Киров")
        self.assertEqual(result["vacancy_url"], "https://acme.ru/career")
        self.assertEqual(result["internship_url"], "https://acme.ru/internship")
        self.assertEqual(result["email"], "hr@acme.ru")
        self.assertEqual(result["email_kind"], "hr")
        self.assertEqual(result["source_url"], "https://acme.ru/career")
        self.assertEqual(result["status"], "found_email")
        self.assertNotIn("https://outside.ru/jobs", fetched)
        self.assertLessEqual(len(fetched), 6)

    def test_keeps_career_link_when_no_email_and_skips_failed_inner_page(self) -> None:
        pages = {
            "https://acme.ru/": "<a href='/career'>Карьера</a><a href='/contacts'>Контакты</a>",
            "https://acme.ru/career": "<h1>Вакансии</h1>",
        }

        def fake_open(request: Request, timeout: int) -> _HtmlResponse:
            url = request.full_url
            if url not in pages:
                raise OSError("HTTP 404")
            return _HtmlResponse(url, pages[url])

        with patch("it_outreach.socket.getaddrinfo", return_value=_PUBLIC_DNS), patch(
            "it_outreach.build_opener", return_value=SimpleNamespace(open=fake_open)
        ):
            result = it_outreach.inspect_site("https://acme.ru/")

        self.assertEqual(result["status"], "no_email")
        self.assertEqual(result["vacancy_url"], "https://acme.ru/career")
        self.assertEqual(result["email"], "")
        self.assertEqual(result["source_url"], "https://acme.ru/career")

    def test_no_email_source_is_a_fetched_relevant_page(self) -> None:
        pages = {
            "https://acme.ru/": "<a href='/career'>Карьера</a><a href='/contacts'>Контакты</a>",
            "https://acme.ru/contacts": "<h1>Контакты</h1>",
        }

        def fake_open(request: Request, timeout: int) -> _HtmlResponse:
            url = request.full_url
            if url not in pages:
                raise HTTPError(url, 404, "Not found", {}, None)
            return _HtmlResponse(url, pages[url])

        with patch("it_outreach.socket.getaddrinfo", return_value=_PUBLIC_DNS), patch(
            "it_outreach.build_opener", return_value=SimpleNamespace(open=fake_open)
        ):
            result = it_outreach.inspect_site("https://acme.ru/")

        self.assertEqual(result["status"], "no_email")
        self.assertEqual(result["vacancy_url"], "https://acme.ru/career")
        self.assertEqual(result["source_url"], "https://acme.ru/contacts")

    def test_privacy_processing_is_not_classified_as_job(self) -> None:
        pages = {
            "https://acme.ru/": """
                <a href='/privacy.html'>Обработка персональных данных</a>
                <a href='/work'>Работа у нас</a>
                <a href='/contacts'>Контакты</a>""",
            "https://acme.ru/privacy.html": "<h1>Обработка персональных данных</h1>",
            "https://acme.ru/work": "<h1>Работа у нас</h1>",
            "https://acme.ru/contacts": "<h1>Контакты</h1>",
        }
        fetched: list[str] = []

        def fake_open(request: Request, timeout: int) -> _HtmlResponse:
            url = request.full_url
            fetched.append(url)
            return _HtmlResponse(url, pages[url])

        with patch("it_outreach.socket.getaddrinfo", return_value=_PUBLIC_DNS), patch(
            "it_outreach.build_opener", return_value=SimpleNamespace(open=fake_open)
        ):
            result = it_outreach.inspect_site("https://acme.ru/")

        self.assertEqual(result["vacancy_url"], "https://acme.ru/work")
        self.assertIn("https://acme.ru/work", fetched)
        self.assertIn("https://acme.ru/contacts", fetched)
        self.assertNotIn("https://acme.ru/privacy.html", fetched)

    def test_part_time_work_is_classified_without_matching_processing(self) -> None:
        pages = {
            "https://acme.ru/": """
                <a href='/privacy.html'>Обработка персональных данных</a>
                <a href='/part-time'>Подработка</a>""",
            "https://acme.ru/part-time": "<h1>Подработка</h1>",
        }
        fetched: list[str] = []

        def fake_open(request: Request, timeout: int) -> _HtmlResponse:
            url = request.full_url
            fetched.append(url)
            return _HtmlResponse(url, pages[url])

        with patch("it_outreach.socket.getaddrinfo", return_value=_PUBLIC_DNS), patch(
            "it_outreach.build_opener", return_value=SimpleNamespace(open=fake_open)
        ):
            result = it_outreach.inspect_site("https://acme.ru/")

        self.assertEqual(result["vacancy_url"], "https://acme.ru/part-time")
        self.assertIn("https://acme.ru/part-time", fetched)
        self.assertNotIn("https://acme.ru/privacy.html", fetched)

    def test_company_name_requires_explicit_site_name(self) -> None:
        self.assertEqual(
            it_outreach._company_name(it_outreach.BeautifulSoup(
                "<title>Карьера | Вакансии в Москве</title><h1 itemprop='name'>Backend Developer</h1>", "html.parser"
            )),
            "",
        )
        self.assertEqual(
            it_outreach._company_name(it_outreach.BeautifulSoup(
                "<meta property='og:site_name' content='ACME'><title>Карьера | Вакансии в Москве</title>", "html.parser"
            )),
            "ACME",
        )

    def test_rejects_private_dns_and_cross_domain_redirect_before_http_get(self) -> None:
        with patch("it_outreach.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]), patch("it_outreach.build_opener") as opener:
            result = it_outreach.inspect_site("https://acme.ru/")
        self.assertEqual(result["status"], "error")
        opener.assert_not_called()

        handler = it_outreach._SafeRedirectHandler("acme.ru")
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.ru/")
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://acme.ru:8080/")
        with patch("it_outreach.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))
        ]), self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://jobs.acme.ru/")

    def test_start_page_failure_has_error_status(self) -> None:
        def forbidden(_request: Request, timeout: int) -> _HtmlResponse:
            raise HTTPError("https://acme.ru/", 403, "Forbidden", {}, None)

        with patch("it_outreach.socket.getaddrinfo", return_value=_PUBLIC_DNS), patch(
            "it_outreach.build_opener", return_value=SimpleNamespace(open=forbidden)
        ):
            result = it_outreach.inspect_site("https://acme.ru/")
        self.assertEqual(result["status"], "error")
        self.assertIn("403", result["error"])


if __name__ == "__main__":
    unittest.main()
