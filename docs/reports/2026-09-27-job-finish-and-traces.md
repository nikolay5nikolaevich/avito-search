# Финал пакета: следы пропущенных и обработка ошибок — 27.09.2026

Бриф 05 аудита resume (находки F04/S4, F24/S3, F16, S2, F37, F21, F19, S5).
Клики, их проверки и порядок «запись → клик» не менялись. Добавлена одна запись
checkpoint **после** клика «Продолжить» — до цены просмотра.

## Что изменилось

**id созданного объявления сразу на диске (F04/S4).** Когда `_step_continue_listing`
возвращает `item_id`, `_run_single_item` пишет `current_item_id` и `current_item_url`
(`/cpxpromo/<id>`, если вкладка на нём) в job и тут же сохраняет checkpoint. Это
отдельная запись после отметки `money_click = "clicked"` из брифа 03: id известен
только после возврата шага. Если запись не удалась, пакет останавливается до цены
просмотра: «Объявление №N (id …) могло быть создано на Авито, но сохранить это на
диск не удалось — неизвестно, создано ли оно…». Клик не повторяется ни сейчас, ни
при resume: отметка `clicked` уже на диске, план даст `skip_item`. Оба поля
сбрасываются в `None` в начале каждого объявления, как `money_click`.

**Стоп по повторному id (F37).** Сразу после этой записи id сверяется со всеми id
пакета: отправленными (`published_items`, у старых checkpoint — из `published_urls`)
и пропущенными (записи `skipped_items`). Совпадение останавливает пакет **до**
`fill_view_price` со статусом `needs_user_action`:
«Авито вернул уже созданное объявление №1 (id 7730001) вместо нового №2, проверьте
кабинет Авито. Публикация остановлена до оплаты просмотра; автоматика это объявление
повторно не отправит. Отправлено 1 из 3.» Если номер ранее встреченного неизвестен
(старый checkpoint), вместо «№1» будет «из этого пакета». Resume после такого стопа
не менялся: текущий номер уходит в `skip_item`, потому что клик был.

**`done` на диске, пропуски не теряются (F16, F24/S3).** Итог задачи — любой, включая
`done`, — пишется в checkpoint во внешнем `finally` `run_publish_job` **до** уборки
(`publish_state.write_final_state`). Уборка:
- `done` без пропусков — каталог `tmp/publish/<job_id>` удаляется целиком, как раньше;
- `done` с пропусками — остаётся `publish-state.json`, фото и прочее удаляются.
  После рестарта recover отдаёт такую задачу со статусом `done` (раньше `done`
  выбрасывался); `/status` показывает `skipped_items`, `resume_available: false`.
  `/resume` для `done` отвечает 409 «Пакет уже завершён — продолжать нечего.
  Пропущенные (№2) проверьте в кабинете Авито вручную.»
  Подготовленные варианты (`prep_*`) после такого `done` удаляются по правилу брифа 04.

Признак «завершена с пропусками» — `status == "done"` и непустой `skipped_items`.
Отдельного статуса нет.

**Пропуск последнего объявления (S2).** Если пакет остановился на последнем объявлении
после клика (или когда клик мог уйти), продолжать нечем: плана resume нет. Теперь
такое объявление при остановке сразу попадает в `skipped_items` записью с номером,
id, шагом и адресом. Тексты называют номер:
- 409 `/resume` и `resume_unavailable_reason`: «Продолжать нечего: все объявления
  пакета отправлены или пропущены. Пропущенные (№3) проверьте в кабинете Авито вручную.»
- recover после рестарта: «Сервер был перезапущен. » + тот же текст.

У checkpoint, записанных до этого правила, последнего нет в `skipped_items`. Тогда
recover добавляет запись только в память, а текст берётся из обновлённой причины:
«…пропустить пришлось бы последнее объявление пакета №3, а оно уже создано на Авито
(или могло быть создано)…».

**Обвязка `run_publish_job` (F21, F19, S5).** Дамп сбоя и закрытие своей вкладки
выполняются внутри `async with async_playwright()`, пока драйвер жив. В дампе теперь
есть скриншот и HTML. Лог «своя вкладка закрыта» пишется только после успешного
`close()`. При неудаче пишется warning «свою вкладку закрыть не удалось».
`_dump_failure` по-прежнему не бросает исключений. Без живой страницы он теперь
пишет warning, а не молчит. Если дамп всё же упадёт, это попадёт в лог, а статус
к этому моменту уже терминальный. `CancelledError` переводит задачу в `interrupted`
(память и диск) и пробрасывается дальше. `_run_publish_and_cleanup` получил страховку:
необработанное исключение даёт `failed` с текстом, checkpoint пишется, traceback
уходит в лог `app` (ERROR + exc_info). Задача asyncio при этом завершается без
исключения. Итог, который publisher уже выставил (`done`/`failed`/…), страховка не
перезаписывает — она трогает только `queued`/`running`.

Цикл по объявлениям вынесен из `run_publish_job` в `_publish_items` без изменений
в шагах. Рабочая вкладка живёт в `active_page_ref[0]`.

## Формат `skipped_items`

Новый формат — список записей, по одной на номер, по возрастанию `item_index`:

```json
{"item_index": 2, "item_id": "7730002",
 "item_url": "https://www.avito.ru/cpxpromo/7730002",
 "step": "fill_view_price", "city": "Москва", "address": "улица Спековая, 2"}
```

Неизвестное — `null`. `item_id`/`item_url`/`step` берутся из job, только если он
описывает именно это объявление (`job["item_index"] == N`). Город и адрес берутся
из геолокации варианта в draft. Адрес лежит только в checkpoint `tmp/publish/`,
в дампы `debug/` он не попадает. Запись создаётся в двух местах: при `/resume` в
режиме `skip_item` для пропускаемого и при остановке на последнем (S2).

Старый формат — список чисел `[2, 5]`, в том числе смешанный `[2, {...}]`. Он
читается везде (`resume_plan`, recover, `/status`, `/resume`) через
`publish_state.skipped_numbers` / `normalize_skipped_items`. Нераспознанная запись
даёт fail-closed, как раньше: план «checkpoint не распознан».

Решение по нормализации (единообразно):
- `/status` всегда отдаёт записи: `7` → `{"item_index": 7, остальные null}`.
  Нераспознанный список отдаётся как есть.
- В checkpoint старые числа переписываются записями, как только туда добавляется
  новый пропуск (`add_skipped_item` нормализует весь список). Без нового пропуска
  файл на диске не переписывается.

Новые поля job: `current_item_id`, `current_item_url` (текущее объявление),
`published_items` — `[{"item_index", "item_id"}]` отправленных, для сверки F37.
В `/status` они не выводятся.

## Пример `/status` — «завершена с пропусками»

Снят со стенда. Пакет из 3, №2 упал на `fill_view_price` после клика, resume
пропустил его и довёл пакет, затем был рестарт сервера:

```json
{
  "status": "done",
  "error": null,
  "items_total": 3,
  "items_published": 2,
  "resume_available": false,
  "resume_plan": null,
  "resume_unavailable_reason": null,
  "skipped_items": [
    {
      "item_index": 2,
      "item_id": "7730002",
      "item_url": "https://www.avito.ru/cpxpromo/7730002",
      "step": "fill_view_price",
      "city": "Москва",
      "address": "улица Спековая, 2"
    }
  ]
}
```

Остальные поля ответа прежние.

## Вложенность до и после

До:

```text
try:
    save checkpoint; step = connect_chrome
    async with async_playwright():
        connect, preflight, своя вкладка, цикл объявлений, done (только в памяти)
except Exception:              # драйвер УЖЕ остановлен
    dump(page)                 # страница мертва -> только error.txt
    status/error; save checkpoint
finally:                       # драйвер УЖЕ остановлен
    page.close()               # no-op, но лог «своя вкладка закрыта»
    rmtree(tmp_dir) при done   # done не на диске, skipped_items удаляются
# CancelledError мимо except; в app._run_publish_and_cleanup страховки нет
```

После:

```text
try:
    save checkpoint; step = connect_chrome
    async with async_playwright():
        try:
            connect, preflight, своя вкладка, _publish_items(...), done в памяти
        except Exception:          # драйвер жив
            status/error в память -> dump(живая страница); сбой дампа -> лог
        except CancelledError:
            interrupted в память; raise
        finally:                   # драйвер жив
            close(своя вкладка); «закрыта» — только если close() прошёл
except Exception:                  # до/при старте или при остановке драйвера
    итог уже есть -> только warning; иначе status/error + dump(без страницы)
except CancelledError:
    interrupted, если итога ещё нет; raise
finally:                           # любой исход
    queued/running -> failed (страховка от BaseException)
    стоп на последнем созданном -> запись в skipped_items (S2)
    write_final_state: checkpoint на диск, ПОТОМ уборка при done

app._run_publish_and_cleanup:
    try: тело (авто-подготовка, run_publish_job, journal, уборка prep)
    except CancelledError: queued/running -> interrupted + checkpoint; raise
    except Exception: logger.error(exc_info); queued/running -> failed + checkpoint
```

## Разведка №6: остаётся ли вкладка после остановки драйвера

Пункт 3 [live-recon-checklist](../_internal/briefs/resume-audit/live-recon-checklist.md).
Скрипт — `tests/recon_tab_after_driver_stop.py`. Имя не `test_*`, поэтому в
discover он не попадает. Авито и VPN не нужны. Скрипт запускает отдельный Chrome
со временным профилем на порту 9333 (рабочий Chrome на 9222 не трогает), только
`about:blank`. Сценарии:
- A — `close()` после выхода из `async with` (так было до брифа);
- B — `close()` внутри (так стало).

Вкладки считаются по `/json/list`.

Запуск (PowerShell, по строке):

```powershell
cd C:\Users\TBG\Desktop\программирование\avito
.venv\Scripts\python.exe tests\recon_tab_after_driver_stop.py
```

Если Chrome стоит не в `C:\Program Files\Google\Chrome\Application\chrome.exe`,
перед запуском:

```powershell
$env:CHROME_PATH = "D:\путь\к\chrome.exe"
```

Окно Chrome откроется и закроется само. Итог печатается и пишется в
`debug/recon-resume/tab_after_driver_stop.txt`. Ожидание по аудиту: в A —
«ВКЛАДКА ОСТАЛАСЬ» (`close()` без ошибки или с ошибкой соединения), в B — «вкладка
закрыта». Если в A вкладка закрылась, F21 в части вкладок в жизни не проявлялся,
а перенос остаётся полезным ради скриншота в дампе.

## Проверка

Приёмка — `tests/test_publish_job_finish.py`: 23 теста. Их написал отдельный агент
до реализации. Все зелёные, ожидания не менялись.

Весь набор: `cd tests && ../.venv/Scripts/python.exe -m unittest discover -s . -p "test_*.py"` —
439 тестов, 437 прошли, 1 пропущен, 1 падает:
`test_publish_batch_contract.PublishStateMachineTests.test_single_item_runs_publication_steps_in_order`.
Этот тест закрепляет точную последовательность вызовов `_run_single_item`, включая
записи checkpoint. Новая запись id после `continue_listing` обязательна по брифу,
и приёмка её требует. Правка теста ждёт решения тех. лида: в ожидаемый список после
`"continue_listing"` добавить `"checkpoint:continue_listing"`.

Старые тесты, обновлённые под новый формат `skipped_items`:
- `tests/test_publish_checkpoint.py` (`test_resume_endpoint_skips_item_created_after_financial_step`):
  вместо `[2]` проверяются номер записи и её шаг `continue_view_price`;
- `tests/test_publish_status_contract.py` (`test_skipped_items_are_exposed_and_default_to_empty`):
  `/status` отдаёт `[7]` записью с `null`-полями;
- `tests/test_publish_disk_state_invariants.py` (`violations`): номера пропущенных
  берутся через `publish_state.skipped_numbers` — `int(dict)` падал на записях.

`tests/test_publish_single_package.py::test_real_subprocess_lock_blocks_and_releases`
до брифа падал «PID 19004 ≠ 4828». Причина — стенд, а не lock.
`.venv\Scripts\python.exe` на Windows — лаунчер: настоящий интерпретатор он
запускает дочерним процессом. Проверено: `Popen.pid` = 21052, `os.getpid()` в
ребёнке = 2060. Теперь ребёнок печатает `READY <os.getpid()>`, и тест сверяет lock
с этим PID. Код lock не трогался.

`tests/publish_smoke_test.py` (вне discover) падал на шаге 17 («start с prep_id —
409»): шаги 16, 20 и 21 оставляют `failed` с отправленным объявлением, и новый
`/start` блокирует правило брифа 04. Сломался ещё с брифом 04, а не с этим. Smoke
теперь закрывает такие задачи через `/api/publish/close` (хелпер
`_close_publish_job`), как пользователь после проверки кабинета: 43 проверки, OK.
`prepare_smoke_test.py` (16) и `smoke_test.py` — OK.

После приёмки добавлено:
- `test_publish_batch_contract.py::test_single_item_runs_publication_steps_in_order`:
  в ожидаемый порядок добавлена запись `checkpoint:continue_listing` после клика —
  прямое следствие F04/S4, клики и их порядок прежние.
- `write_final_state`: если финальная запись `done` не удалась, а пропуски есть,
  каталог не убирается — иначе рядом с устаревшим checkpoint пропали бы файлы.

Живой Chrome и платная публикация не запускались.

## Открытые вопросы

- **Фронт до брифа 06 покажет пропуски как «№[object Object]».** `DraftPage.jsx`
  (`skippedItems.map((n) => \`№${n}\`)`) ждёт числа, а `/status` теперь отдаёт записи.
  Фронт по брифу не трогали, чинит бриф 06 (`item_index`).
- **Задачу «завершена с пропусками» нельзя закрыть** через `/close` (409, закрывается
  только остановленная). Она лежит в `tmp/publish/` и видна после каждого рестарта,
  пока владелец не уберёт каталог в `trash/`. Нужна ли кнопка «проверил» — вопрос
  к брифу 06.
- Если Авито открыл экран цены в новой вкладке (`_resolve_cpxpromo_page`), закрывается
  только она. Исходная вкладка формы на совести Авито — так было и до брифа.
