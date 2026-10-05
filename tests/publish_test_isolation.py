"""
Общая точка изоляции тестов, трогающих publisher.py/app.py, от боевых
артефактов (аудит resume/checkpoint, находки F28/N01/N03).

Любой тест, который импортирует `publisher` или `app` (напрямую или через
другой модуль), должен импортировать ЭТОТ модуль ПЕРВЫМ — до `import app`,
`import publisher`. При импорте он:

  - выставляет AVITO_CDP_URL на мёртвый порт (127.0.0.1:1), чтобы случайный
    непромоканный шаг сценария не постучался в живой Chrome;
  - отключает файловый хэндлер logs/publisher.log (F28: он вешался при
    импорте publisher.py безусловно и рос от каждого прогона тестов);
  - подменяет TMP_PUBLISH_DIR (app.py и publisher.py) и DEBUG_PUBLISH_DIR
    (publisher.py) на общий временный каталог — боевые tmp/publish и
    debug/publish тестами не трогаются.

Порядок импорта тестовых файлов не гарантирован (unittest discover
импортирует все test_*.py, прежде чем что-то запустить, но КАКОЙ файл
импортируется первым — не определено). Поэтому модуль не только выставляет
переменные окружения до собственного импорта publisher/app, но и подчищает
уже присоединённые хэндлеры и подменяет уже прочитанные значения атрибутов
на самих объектах модулей — это работает независимо от того, кто успел
импортировать publisher/app раньше.

Точечные mock.patch.object(...) внутри отдельных тестов (например,
tests/test_publish_checkpoint.py) продолжают работать как раньше: они лишь
временно перекрывают значения, выставленные здесь, и возвращают их обратно
после теста — не боевые пути.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TESTS_DIR.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"
for _p in (str(_TESTS_DIR), str(_PROJECT_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Переменные окружения — по возможности ДО первого импорта publisher/app
# в процессе (для стандартных путей: смотри докстринг про порядок импорта).
DEAD_CDP_URL = "http://127.0.0.1:1"
os.environ["AVITO_CDP_URL"] = DEAD_CDP_URL
os.environ["AVITO_DISABLE_PUBLISHER_FILE_LOG"] = "1"

import app as app_module  # noqa: E402
import publisher  # noqa: E402

# Общий временный каталог на весь тестовый процесс.
_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="avito-tests-isolation-"))
TMP_PUBLISH_DIR = _ISOLATED_ROOT / "tmp_publish"
DEBUG_PUBLISH_DIR = _ISOLATED_ROOT / "debug_publish"
TMP_PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_PUBLISH_DIR.mkdir(parents=True, exist_ok=True)

# Подмена атрибутов уже импортированных модулей — работает независимо от
# того, что publisher/app могли быть импортированы раньше этого файла.
app_module.TMP_PUBLISH_DIR = TMP_PUBLISH_DIR
publisher.TMP_PUBLISH_DIR = TMP_PUBLISH_DIR
publisher.DEBUG_PUBLISH_DIR = DEBUG_PUBLISH_DIR
app_module.CDP_URL = DEAD_CDP_URL


def _strip_real_file_handlers() -> None:
    """Снимает файловый хэндлер logs/publisher.log, если publisher.py был
    импортирован (и успел его повесить) до этого модуля."""
    for handler in list(publisher.logger.handlers):
        if isinstance(handler, logging.FileHandler):
            publisher.logger.removeHandler(handler)
            handler.close()


_strip_real_file_handlers()

atexit.register(shutil.rmtree, _ISOLATED_ROOT, ignore_errors=True)
