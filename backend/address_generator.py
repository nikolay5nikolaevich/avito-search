"""Генерация существующих российских адресов через OpenStreetMap.

Внешние запросы выполняются только по действию пользователя. Nominatim нужен
только для поиска границы города; адреса берутся исключительно из Overpass.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


logger = logging.getLogger(__name__)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE_TTL_SECONDS = 24 * 60 * 60
USER_AGENT = "AvitoResearchAddressGenerator/1.0 (local user-triggered tool)"


class AddressGenerationError(RuntimeError):
    """Понятная пользователю ошибка получения адреса."""


@dataclass(frozen=True)
class CityArea:
    osm_type: str
    osm_id: int
    bbox: tuple[float, float, float, float]  # south, north, west, east


_cache: dict[str, tuple[float, list[str]]] = {}
_areas: dict[str, CityArea] = {}
_lock = threading.Lock()
_nominatim_lock = threading.Lock()
_last_nominatim_request = 0.0


def _norm(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _request_json(url: str, *, data: bytes | None = None) -> object:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise AddressGenerationError("OpenStreetMap временно недоступен. Введите адрес вручную.") from exc


def _find_city(city: str) -> CityArea:
    global _last_nominatim_request
    key = city.casefold()
    with _lock:
        cached = _areas.get(key)
    if cached:
        return cached
    # Общий замок делает лимит 1 rps строгим и при двух одновременных batch.
    with _nominatim_lock:
        with _lock:
            cached = _areas.get(key)
            delay = 1.0 - (time.monotonic() - _last_nominatim_request)
        if cached:
            return cached
        if delay > 0:
            time.sleep(delay)
        query = urllib.parse.urlencode({
            "city": city, "country": "Россия", "countrycodes": "ru",
            "featureType": "settlement", "addressdetails": "1", "format": "jsonv2", "limit": "5",
        })
        try:
            payload = _request_json(f"{NOMINATIM_URL}?{query}")
        finally:
            # Лимит считается от начала любой попытки, в том числе неуспешной.
            with _lock:
                _last_nominatim_request = time.monotonic()
    if not isinstance(payload, list) or not payload:
        raise AddressGenerationError(f"Город «{city}» не найден в OpenStreetMap.")
    candidates = [item for item in payload if isinstance(item, dict)]
    item = next((item for item in candidates if item.get("osm_type") == "relation"), candidates[0] if candidates else {})
    bbox = item.get("boundingbox")
    try:
        area = CityArea(
            osm_type=str(item["osm_type"]), osm_id=int(item["osm_id"]),
            bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise AddressGenerationError(f"Не удалось определить границы города «{city}».") from exc
    with _lock:
        _areas[key] = area
    return area


def _overpass_query(area: CityArea) -> str:
    if area.osm_type == "relation":
        scope = f"area({3600000000 + area.osm_id})->.city;"
        selector = "area.city"
    else:
        south, north, west, east = area.bbox
        scope = ""
        selector = f"{south},{west},{north},{east}"
    return (
        "[out:json][timeout:20];"
        f"{scope}(node[\"addr:street\"][\"addr:housenumber\"]({selector});"
        f"way[\"addr:street\"][\"addr:housenumber\"]({selector}););"
        "out tags 250;"
    )


def _load_addresses(city: str) -> list[str]:
    key = city.casefold()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
    area = _find_city(city)
    payload = _request_json(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": _overpass_query(area)}).encode("utf-8"),
    )
    elements = payload.get("elements", []) if isinstance(payload, dict) else []
    addresses: list[str] = []
    seen: set[str] = set()
    for element in elements:
        tags = element.get("tags", {}) if isinstance(element, dict) else {}
        street, house = _norm(tags.get("addr:street")), _norm(tags.get("addr:housenumber"))
        if not street or not house:
            continue
        # Bbox иногда захватывает соседний населённый пункт. Отбрасываем явно
        # чужой addr:city / addr:place, не пытаясь «додумать» город.
        if area.osm_type != "relation":
            tagged_city = _norm(tags.get("addr:city") or tags.get("addr:place"))
            if tagged_city and tagged_city.casefold() != city.casefold():
                continue
        address = f"{street}, {house}"
        marker = address.casefold()
        if marker not in seen:
            seen.add(marker)
            addresses.append(address)
    if not addresses:
        raise AddressGenerationError(f"В OpenStreetMap не нашлись адреса для города «{city}».")
    with _lock:
        _cache[key] = (time.time() + CACHE_TTL_SECONDS, addresses)
    return addresses


def generate_addresses(
    cities: list[str], used_locations: list[dict[str, str]], rng=random.choice,
) -> list[dict[str, str]]:
    """Возвращает адрес на каждую строку, не повторяя уже использованные по возможности."""
    used: dict[str, set[str]] = {}
    for location in used_locations:
        if not isinstance(location, dict):
            continue
        city, address = _norm(location.get("city")), _norm(location.get("address"))
        if city and address:
            used.setdefault(city.casefold(), set()).add(address.casefold())
    loaded: dict[str, list[str] | AddressGenerationError] = {}
    result: list[dict[str, str]] = []
    for city in cities:
        clean_city = _norm(city)
        key = clean_city.casefold()
        if key not in loaded:
            try:
                loaded[key] = _load_addresses(clean_city)
            except AddressGenerationError as exc:
                loaded[key] = exc
        available = loaded[key]
        if isinstance(available, AddressGenerationError):
            result.append({"city": clean_city, "error": str(available)})
            continue
        city_used = used.setdefault(key, set())
        candidates = [value for value in available if value.casefold() not in city_used]
        if not candidates:
            result.append({"city": clean_city, "error": f"Для города «{clean_city}» не осталось уникальных адресов."})
            continue
        selected = rng(candidates)
        city_used.add(selected.casefold())
        result.append({"city": clean_city, "address": selected})
    return result
