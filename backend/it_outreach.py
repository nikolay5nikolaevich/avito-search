"""Бесплатное получение URL-кандидатов для IT-рассылки через Taginfo."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import random
import re
import socket
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

from bs4 import BeautifulSoup


LOGGER = logging.getLogger(__name__)
_TAGINFO_URL = "https://taginfo.geofabrik.de/russia/api/4/key/values"
_PAGE_SIZE = 100
_MAX_PAGES = 6
_MAX_BODY_BYTES = 1_048_576
_USER_AGENT = "AvitoLocalITOutreach/1.0"
_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_RESERVED_TLDS = {"example", "invalid", "local", "localhost", "test"}
_BLOCKED_DOMAINS = {
    "2gis.ru",
    "avito.ru",
    "bing.com",
    "caduk.ru",
    "cataloxy.ru",
    "checko.ru",
    "edusite.ru",
    "facebook.com",
    "flamp.ru",
    "fsin.su",
    "google.com",
    "google.ru",
    "hh.ru",
    "instagram.com",
    "linkedin.com",
    "mail.ru",
    "mos.ru",
    "mvd.ru",
    "ok.ru",
    "orgpage.ru",
    "rusprofile.ru",
    "t.me",
    "telegram.me",
    "twitter.com",
    "vk.com",
    "x.com",
    "yandex.ru",
    "youtube.com",
    "zoon.ru",
}
_EDUCATION_LABELS = {"college", "gymnasium", "lyceum", "school", "university"}
_SITE_WORDS = re.compile(
    r"career|job|vacanc|intern|\bкарьер|\bработ|\bподработ|\bваканс|\bстаж|контакт|contact|about|о\s+компании",
    re.IGNORECASE,
)
_CAREER_WORDS = re.compile(r"career|job|vacanc|\bкарьер|\bработ|\bподработ|\bваканс", re.IGNORECASE)
_INTERN_WORDS = re.compile(r"intern|\bстаж", re.IGNORECASE)
_GENERIC_NAME = re.compile(
    r"^(?:главная|home|контакты|contacts?|карьера|careers?|вакансии|vacancies|jobs?|стажировки?|internships?|работа(?:\s+.*)?|о\s+компании|about(?:\s+us)?)$",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w.-]+\.[a-zA-Zа-яА-Я]{2,})(?![\w.-])")
_HR_LOCALS = {"hr", "jobs", "job", "career", "careers", "recruit", "recruiting", "vacancy", "vacancies"}
_GENERAL_LOCALS = {"info", "contact", "contacts", "hello", "office", "mail", "admin"}


class SourceUnavailableError(RuntimeError):
    """Taginfo недоступен или вернул не пригодный для обработки ответ."""


def _is_blocked_domain(host: str) -> bool:
    """Отсекает платформы и явно непрофильные сайты по доменному имени."""
    if any(host == domain or host.endswith(f".{domain}") for domain in _BLOCKED_DOMAINS):
        return True
    labels = host.split(".")
    return (
        host.endswith(".gov.ru")
        or host.endswith(".edu.ru")
        or host.endswith(".ac.ru")
        or "gosuslugi" in labels
        or bool(_EDUCATION_LABELS.intersection(labels))
    )


def normalize_site_url(value: str) -> str:
    """Возвращает безопасный публичный HTTP(S)-URL либо пустую строку."""
    if not isinstance(value, str) or not value:
        return ""
    if any(character.isspace() or character in "\\\\|<>\"'," for character in value):
        return ""
    if value.startswith("//"):
        return ""

    candidate = value if "://" in value else f"https://{value}"
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        port = parsed.port
        host = parsed.hostname.rstrip(".").lower()
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return ""
    try:
        ipaddress.ip_address(ascii_host)
        return ""
    except ValueError:
        pass

    labels = ascii_host.split(".")
    if (
        len(ascii_host) > 253
        or len(labels) < 2
        or labels[-1] in _RESERVED_TLDS
        or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels)
        or _is_blocked_domain(ascii_host)
    ):
        return ""

    netloc = ascii_host if port is None else f"{ascii_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def domain_key(value: str) -> str:
    """Возвращает ASCII-ключ домена без www, пути, порта и параметров."""
    normalized = normalize_site_url(value)
    if not normalized:
        return ""
    host = urlsplit(normalized).hostname or ""
    return host.removeprefix("www.")


def _fetch_page(page: int) -> dict[str, Any]:
    """Читает одну JSON-страницу Taginfo с ограничением размера ответа."""
    query = urlencode(
        {
            "key": "website",
            "rp": _PAGE_SIZE,
            "page": page,
            "sortname": "value",
            "sortorder": "asc",
        }
    )
    request = Request(
        f"{_TAGINFO_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status < 200 or status >= 300:
                raise SourceUnavailableError(f"Taginfo вернул HTTP {status}.")
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/json" not in content_type:
                raise SourceUnavailableError("Taginfo вернул ответ не в JSON.")
            body = response.read(_MAX_BODY_BYTES + 1)
    except SourceUnavailableError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        raise SourceUnavailableError(f"Taginfo недоступен: {error}.") from error

    if len(body) > _MAX_BODY_BYTES:
        raise SourceUnavailableError("Ответ Taginfo превышает 1 МБ.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceUnavailableError("Taginfo вернул повреждённый JSON.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise SourceUnavailableError("Taginfo вернул JSON без списка data.")
    return payload


def _interleave_page_sites(page_sites: list[list[str]], limit: int) -> list[str]:
    """Чередует кандидаты страниц, чтобы не возвращать начало сортировки."""
    result: list[str] = []
    for position in range(max(map(len, page_sites), default=0)):
        for sites in page_sites[1:] + page_sites[:1]:
            if position < len(sites):
                result.append(sites[position])
                if len(result) == limit:
                    return result
    return result


def _collect_site_urls(payload: dict[str, Any], seen: set[str]) -> list[str]:
    """Нормализует новые домены одной страницы Taginfo."""
    sites: list[str] = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        site_url = normalize_site_url(str(row.get("value") or ""))
        domain = domain_key(site_url)
        if not site_url or not domain or domain in seen:
            continue
        seen.add(domain)
        sites.append(site_url)
    return sites


def discover_sites(limit: int, known_domains: set[str] | None = None) -> list[str]:
    """Собирает до ``limit`` новых URL-кандидатов из максимум шести страниц."""
    if not 1 <= limit <= 100:
        raise ValueError("limit должен быть в диапазоне от 1 до 100.")

    known = {domain_key(domain) for domain in known_domains or set()}
    known.discard("")
    first_page = _fetch_page(1)
    total = first_page.get("total")
    if not isinstance(total, int) or total < 0:
        raise SourceUnavailableError("Taginfo не сообщил корректное total.")

    seen = set(known)
    total_pages = math.ceil(total / _PAGE_SIZE)
    if total_pages <= 1:
        result = _collect_site_urls(first_page, seen)[:limit]
        LOGGER.info("Taginfo дал %s URL-кандидатов из запрошенных %s.", len(result), limit)
        return result

    random_pages = random.sample(
        range(2, total_pages + 1), min(_MAX_PAGES - 1, total_pages - 1)
    )
    minimum_pages = min(len(random_pages), 2 if limit >= 5 else 1)
    random_page_sites: list[list[str]] = []
    for index, page in enumerate(random_pages, start=1):
        # Между любыми двумя запросами к источнику выдерживается одна секунда.
        # ponytail: последовательный поток, параллелить только при изменении лимитов источника.
        sleep(1)
        random_page_sites.append(_collect_site_urls(_fetch_page(page), seen))
        if index >= minimum_pages and sum(map(len, random_page_sites)) >= limit:
            result = _interleave_page_sites(random_page_sites, limit)
            LOGGER.info("Taginfo дал %s URL-кандидатов из запрошенных %s.", len(result), limit)
            return result

    result = _interleave_page_sites(random_page_sites, limit)
    if len(result) < limit:
        result.extend(_collect_site_urls(first_page, seen)[:limit - len(result)])
    LOGGER.info("Taginfo дал %s URL-кандидатов из запрошенных %s.", len(result), limit)
    return result


def _check_site_target(url: str, base_host: str) -> str:
    """Проверяет URL и DNS непосредственно перед сетевым запросом."""
    normalized = normalize_site_url(url)
    if not normalized:
        raise ValueError("Недопустимый URL сайта.")
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    if host != base_host and not host.endswith(f".{base_host}"):
        raise ValueError("Переход на другой домен запрещён.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Нестандартный порт запрещён.")
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("DNS указывает на непубличный адрес.")
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        quote(parsed.path, safe="/%:@"),
        quote(parsed.query, safe="=&;%:+/?,@"),
        "",
    ))


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Не пропускает редиректы без проверки домена, адреса и порта."""

    def __init__(self, base_host: str) -> None:
        self.base_host = base_host

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request:
        checked = _check_site_target(newurl, self.base_host)
        return super().redirect_request(req, fp, code, msg, headers, checked)


def _fetch_site_html(url: str, base_host: str) -> tuple[str, str]:
    """Читает одну HTML-страницу с лимитом размера и безопасными редиректами."""
    checked = _check_site_target(url, base_host)
    request = Request(checked, headers={"Accept": "text/html", "User-Agent": _USER_AGENT})
    # ponytail: DNS проверен до GET и каждого редиректа, но urllib не пинит IP; при публичном хостинге нужен pinned-IP HTTP transport.
    opener = build_opener(ProxyHandler({}), _SafeRedirectHandler(base_host))
    with opener.open(request, timeout=10) as response:
        final_url = _check_site_target(response.geturl(), base_host)
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            raise ValueError("Ответ сайта не является HTML.")
        body = response.read(_MAX_BODY_BYTES + 1)
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError("HTML-страница превышает 1 МБ.")
        charset = response.headers.get_content_charset() or "utf-8"
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    return html, final_url


def _email_candidates(soup: BeautifulSoup, page_url: str, career_page: bool) -> list[tuple[int, str, str, str]]:
    """Выбирает только ролевые почты; персональные и поддержка исключены."""
    candidates: list[tuple[int, str, str, str]] = []
    strings = [unquote(link.get("href", "")[7:]) for link in soup.select('a[href^="mailto:"]')]
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    strings.append(soup.get_text(" ", strip=True))
    seen: set[str] = set()
    for chunk in strings:
        for match in _EMAIL_RE.finditer(chunk):
            email = match.group(1).lower().rstrip(".")
            if email in seen:
                continue
            seen.add(email)
            local = email.split("@", 1)[0]
            if local in _HR_LOCALS:
                kind = "hr"
                priority = 0 if career_page else 1
            elif local in _GENERAL_LOCALS:
                kind = "general"
                priority = 2 if career_page else 3
            else:
                continue
            candidates.append((priority, email, kind, page_url))
    return candidates


def _company_name(soup: BeautifulSoup) -> str:
    meta = soup.select_one('meta[property="og:site_name"]')
    if meta:
        name = meta.get("content", "").strip()
        if name and not _GENERIC_NAME.fullmatch(name):
            return name[:120]
    return ""


def _source_rank(label: str) -> int:
    if _CAREER_WORDS.search(label):
        return 0
    if _INTERN_WORDS.search(label):
        return 1
    if _SITE_WORDS.search(label):
        return 2
    return 3


def _city(soup: BeautifulSoup) -> str:
    item = soup.select_one('[itemprop="addressLocality"]')
    if item:
        value = (item.get("content") or item.get_text(" ", strip=True)).strip()
        if value:
            return value[:80]
    for address in soup.find_all("address"):
        match = re.search(r"\bг\.?\s+([А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?)", address.get_text(" ", strip=True))
        if match:
            return match.group(1)
    return ""


def inspect_site(site_url: str) -> dict[str, str]:
    """Ищет страницы найма и публичную ролевую почту на одном сайте."""
    normalized = normalize_site_url(site_url)
    base_host = domain_key(normalized)
    result = {
        "domain": base_host,
        "company_name": "",
        "city": "",
        "site_url": normalized,
        "vacancy_url": "",
        "internship_url": "",
        "email": "",
        "email_kind": "",
        "source_url": "",
        "status": "error",
        "error": "",
    }
    if not normalized or not base_host:
        result["error"] = "Недопустимый URL сайта."
        return result

    queue = [normalized]
    link_ranks: dict[str, int] = {}
    seen: set[str] = set()
    emails: list[tuple[int, str, str, str]] = []
    fetched_sources: list[tuple[int, str]] = []
    while queue and len(seen) < 6:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            html, final_url = _fetch_site_html(url, base_host)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
            if len(seen) == 1:
                result["error"] = str(error)[:300]
                return result
            LOGGER.info("Пропускаем страницу %s: %s", url, error)
            continue

        soup = BeautifulSoup(html, "html.parser")
        heading = soup.h1.get_text(" ", strip=True) if soup.h1 else ""
        page_label = f"{final_url} {soup.title.get_text(' ', strip=True) if soup.title else ''} {heading}"
        rank = _source_rank(page_label)
        if rank == 3 and final_url == url:
            rank = link_ranks.get(url, 3)
        if rank < 3:
            fetched_sources.append((rank, final_url))
        if not result["company_name"]:
            result["company_name"] = _company_name(soup)
        if not result["city"]:
            result["city"] = _city(soup)
        career_page = bool(_CAREER_WORDS.search(final_url) or _INTERN_WORDS.search(final_url))
        emails.extend(_email_candidates(soup, final_url, career_page))

        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            try:
                target = urljoin(final_url, href)
                target = urlunsplit(urlsplit(target)._replace(fragment=""))
                parsed = urlsplit(target)
                if not normalize_site_url(target) or parsed.port not in (None, 80, 443):
                    continue
            except ValueError:
                continue
            label = f"{link.get_text(' ', strip=True)} {urlsplit(target).path}"
            if not _SITE_WORDS.search(label):
                continue
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or (host != base_host and not host.endswith(f".{base_host}")):
                continue
            if re.search(r"\.(?:pdf|docx?|xlsx?|zip|rar|jpe?g|png)$", parsed.path, re.IGNORECASE):
                continue
            if _INTERN_WORDS.search(label) and not result["internship_url"]:
                result["internship_url"] = target
            elif _CAREER_WORDS.search(label) and not result["vacancy_url"]:
                result["vacancy_url"] = target
            link_ranks[target] = min(link_ranks.get(target, 3), _source_rank(label))
            if target not in seen and target not in queue and len(queue) < 12:
                queue.append(target)

    if emails:
        _, result["email"], result["email_kind"], result["source_url"] = min(emails)
        result["status"] = "found_email"
    else:
        result["source_url"] = min(fetched_sources)[1] if fetched_sources else ""
        result["status"] = "no_email"
    return result
