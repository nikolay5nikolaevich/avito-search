"""
Общие хелперы smoke-тестов (tests/smoke_test.py и tests/publish_smoke_test.py).

Только stdlib + uvicorn: HTTP — через urllib, сервер — uvicorn в демон-потоке.
Каждый тест передаёт СВОЙ base_url/port: порты у тестов разные, чтобы
не конфликтовать ни друг с другом, ни с продакшном (7777).

Тесты запускаются как скрипты (`python tests/<имя>.py`) — каталог tests/
попадает в sys.path автоматически, поэтому `import smoke_helpers` работает.
"""

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def http_get(
    base_url: str,
    path: str,
    params: dict | None = None,
) -> tuple[int, str, dict]:
    """GET-запрос. Возвращает (status_code, body_text, headers)."""
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)


def wait_server_ready(base_url: str, timeout: float = 15.0) -> None:
    """Ждёт, пока сервер начнёт отвечать на /."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Сервер не поднялся за {timeout}с ({base_url})")


def start_server(app: Any, port: int) -> threading.Thread:
    """Запускает uvicorn с приложением app в демон-потоке на 127.0.0.1:{port}."""
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return t
