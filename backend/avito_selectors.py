"""
CSS-селекторы и атрибуты для парсинга Авито.

Сгруппированы по двум контекстам:
  - SEARCH: страница поиска (список карточек объявлений)
  - ITEM:   страница отдельного объявления

ВАЖНО: Авито регулярно меняет вёрстку. Все атрибуты вида data-marker
стабильнее CSS-классов (классы меняются чаще). Именно data-marker
предпочтительны для выборки.

СТАТУС ПОДТВЕРЖДЕНИЯ:
  Селекторы помечены как «ПОДТВЕРЖДЕНО» или «НЕ ПОДТВЕРЖДЕНО».
  «ПОДТВЕРЖДЕНО на живой странице» — проверено лично по сохранённым
  debug/search.html и debug/item.html с реального сеанса CDP.
  «ПОДТВЕРЖДЕНО» (без уточнения) — из исходного кода работающих парсеров
  (xailiry/avito-parser-sdk, Duff89/parser_avito).

АЛЬТЕРНАТИВНЫЙ МЕТОД (рекомендованный Duff89/parser_avito):
  Авито встраивает весь каталог объявлений в тег:
      <script type="mime/invalid" data-mfe-state="true">
  в виде JSON-объекта. Поле catalog.items содержит массив объявлений
  с полями id, title, urlPath, priceDetailed, addressDetailed, geo,
  sortTimeStamp. Этот метод надёжнее HTML-селекторов, но требует
  разбора JSON (реализован в parser.py).
"""

# ---------------------------------------------------------------------------
# Страница поиска — список карточек объявлений
# ---------------------------------------------------------------------------

# Контейнер одной карточки объявления.
# Содержит все данные по конкретному лоту.
# ПОДТВЕРЖДЕНО на живой странице: debug/search.html — 50 карточек
SEARCH_CARD: str = "[data-marker='item']"

# Идентификатор объявления (числовой ID).
# Получается как атрибут: card.get("data-item-id")
# ПОДТВЕРЖДЕНО: xailiry/avito-parser-sdk, Duff89/parser_avito
SEARCH_CARD_ID_ATTR: str = "data-item-id"

# Заголовок карточки — одновременно является ссылкой <a> на объявление.
# Атрибут href содержит относительный путь вида /moskva/...
# ПОДТВЕРЖДЕНО: data-marker="item-title" используется в xailiry/avito-parser-sdk
SEARCH_CARD_TITLE: str = "[data-marker='item-title']"

# Цена в карточке поиска.
# ПОДТВЕРЖДЕНО: data-marker="item-price-value" — xailiry/avito-parser-sdk
SEARCH_CARD_PRICE: str = "[data-marker='item-price-value']"

# Гео-блок карточки поиска: адрес/метро (район, улица, станция метро).
# ПОДТВЕРЖДЕНО на живой странице: debug/search.html — атрибут data-marker="item-location".
# Классы вида geo-root-XXXX меняются с каждой сборкой — НЕ использовать.
# Текст содержит район/улицу и, возможно, станцию метро (формат уточняется
# в diag.py при следующем прогоне — см. логирование первых 3 карточек).
SEARCH_CARD_LOCATION: str = "[data-marker='item-location']"

# Описание карточки поиска (сниппет). ПОДТВЕРЖДЕНО на живой странице:
# debug/search.html — <meta itemprop="description" content="..."> внутри
# каждой [data-marker='item'], 50 карточек — 50 таких meta. Текст — в
# атрибуте content, а не в get_text(). На более новой вёрстке (см. ниже)
# этого meta может не быть вовсе — резерв.
SEARCH_CARD_DESCRIPTION: str = "meta[itemprop='description']"

# Описание карточки поиска — новая вёрстка. ПОДТВЕРЖДЕНО на живой странице:
# debug/search_live_noutbuki.html (28.09.2026, выдача «ноутбук Gigabyte G5 MF»,
# 41 карточка) — текст лежит в первом p[style*='--module-max-lines-size']
# карточки. ТЕМ ЖЕ стилем размечены имя продавца и «N отзыв(ов)» — их отличают
# два признака (см. SEARCH_CARD_SELLER_MARKER ниже и parser._is_not_description):
#   1) параграф вложен в <a> (имя продавца — всегда ссылка на профиль);
#   2) у параграфа СВОЙ data-marker с подстрокой "seller" (у «N отзывов» —
#      data-marker="seller-info/summary").
# Оба признака проверены и на живой разметке, и на синтетике тестов.
SEARCH_CARD_DESCRIPTION_TEXT: str = "p[style*='--module-max-lines-size']"

# Признак 2 выше — элемент блока продавца со своим data-marker (имя, рейтинг,
# «N отзывов»). ПОДТВЕРЖДЕНО на живой странице: debug/search_live_noutbuki.html
# — data-marker="seller-info/summary" у параграфа с отзывами.
SEARCH_CARD_SELLER_MARKER: str = "[data-marker*='seller']"

# Адрес/метро в карточке поиска (старый селектор — оставлен как резерв).
# ПОДТВЕРЖДЕНО: data-marker="item-address" — xailiry/avito-parser-sdk.
# На живой странице (debug/search.html) адрес/метро лежит в item-location,
# а не в item-address — используй SEARCH_CARD_LOCATION как основной.
SEARCH_CARD_ADDRESS: str = "[data-marker='item-address']"

# ПОДТВЕРЖДЕНО в debug/search.html: отзывы и ссылки профилей внутри карточки.
SEARCH_SELLER_REVIEWS: str = "[data-marker='seller-info/summary']"
SEARCH_SELLER_LINK: str = "a[href*='/brands/'], a[href*='/user/']"

# ПОДТВЕРЖДЕНО в debug/item.html.
ITEM_SELLER_LINK: str = "a[data-marker='seller-link/link']"

# ---------------------------------------------------------------------------
# Страница профиля продавца (все объявления) — разбор продавца (seller_scan.py)
# ---------------------------------------------------------------------------
# Источник: debug/seller_profile_map_20260923T085405Z.txt и
# debug/seller_profile_20260923T085405Z_1_scrolled.html — живая разведка
# 23.09.2026 карточки-ленты на /brands/<id>/items/all и /user/<id>/profile/all.

# Карточка объявления на странице профиля продавца.
# ПОДТВЕРЖДЕНО живой разведкой 23.09.2026: на этой странице [data-marker='item']
# (селектор выдачи поиска, SEARCH_CARD) НЕТ ВООБЩЕ — карточки пронумерованы,
# 'item_list_with_filters/item(0)' .. 'item(169)' для 170 карточек на странице.
# Внутри каждой — [data-marker='item-title'] со ссылкой на объявление.
SELLER_PROFILE_CARD: str = "[data-marker^='item_list_with_filters/item(']"

# «Показать все» под первыми карточками витрины продавца (/brands/<id>/all):
# витрина показывает только 12–15 объявлений, полная лента — по этой кнопке.
# ПОДТВЕРЖДЕНО дампами debug/seller_scan_short_20260927T181806Z (это <a> с
# href=/brands/<id>/items?s=profile_search_show_all) и
# debug/seller_scan_short_20260923T111159Z (это <button type=button> без href).
SELLER_SHOW_ALL_BUTTON: str = "[data-marker='item_list_with_filters/show_all_button']"
SELLER_SHOW_ALL_LABEL: str = "Показать все"

# ---------------------------------------------------------------------------
# Чат с продавцом (рассылка) — разведка 21.09.2026, артефакты в debug/
# ---------------------------------------------------------------------------
# Источники:
#   debug/item_chat_map_20260921T200831Z.txt  — кнопка «Написать» на двух раскладках,
#   debug/chat_send_map_20260921T202037Z.txt  — лента переписки с реальными сообщениями,
#   debug/send_button_map_20260921T203306Z.txt — кнопка отправки «до/после» ввода текста.
# Каждое сообщение здесь необратимо: при несовпадении подписи или разметки
# сценарий обязан остановиться (см. outreach.py), а не «разбираться сам».

# Кнопка открытия чата на странице объявления, текст «Написать».
# ПОДТВЕРЖДЕНО в debug/item_chat_map_20260921T200831Z.txt: на обеих проверенных
# раскладках по ДВЕ копии (основная + «липкая» панель), обе visible/enabled.
# Старый [data-marker='messenger-button/link'] на живой странице отсутствует
# вовсе (0 элементов) — это была разметка разлогиненного посетителя.
ITEM_CHAT_BUTTON: str = "button[data-marker='messenger-button/button']"

# Допустимые подписи кнопки открытия чата.
# ПОДТВЕРЖДЕНО живьём только «Написать»; «Написать сообщение» оставлено как
# НЕ ПОДТВЕРЖДЁННЫЙ, но безопасный синоним (клик по ней ничего не отправляет).
ITEM_CHAT_BUTTON_LABELS: frozenset[str] = frozenset({"Написать", "Написать сообщение"})

# Контейнер плавающего виджета мини-мессенджера (может быть свёрнут).
# ПОДТВЕРЖДЕНО повторной разведкой 21.09.2026 на ОДНОМ И ТОМ ЖЕ объявлении с
# разницей 49 секунд: debug/chat_existing_map_20260921T212054Z.txt (сбой) —
# контейнера [data-marker='mini-messenger'] в DOM НЕТ ВООБЩЕ, клик по «Написать»
# проходит, но за 15 с мониторинга ничего не появляется (HTML до/после клика
# почти не меняется); debug/chat_existing_map_20260921T212143Z.txt (успех) —
# контейнер уже в DOM (свёрнут: expand=1, счётчик непрочитанных=3), тот же клик
# открывает поле ввода за 1 с. Вывод: мини-мессенджер — ленивый чанк, страница
# грузится с wait_until="domcontentloaded", то есть раньше, чем чанк готов;
# пока контейнера нет, обработчик клика по «Написать» ещё не навешен и клик
# уходит в пустоту. Ждать нужно attached, не visible — свёрнутый виджет уже
# готов принимать клики, но сам не visible.
MINI_MESSENGER: str = "[data-marker='mini-messenger']"

# Счётчик непрочитанных чатов в шапке сайта — диагностический маркер, в
# сценарий открытия чата не участвует, читается только в дамп остановки
# (см. outreach._dump_stop_state), чтобы отличить «чанк завис в этой вкладке»
# от «Авито придерживает чат после нескольких писем подряд». Подтверждён в
# дампах разведки 21.09.2026 (debug/chat_send_map_20260921T202037Z.txt и др.).
HEADER_UNREAD_CHATS_COUNTER: str = "[data-marker='header/unread-chats-counter']"

# Переключатель «развернуть» свёрнутого мини-мессенджера.
# Чат открывается плавающим виджетом поверх объявления, а не отдельной страницей.
# ПОДТВЕРЖДЕНО: в debug/item_logged_in_20260921T200831Z_2.html (объявление до
# клика) mini-messenger/expand есть — виджет свёрнут в список переписок;
# в debug/chat_logged_in_20260921T200831Z.html (чат открыт) его уже нет,
# вместо него mini-messenger/minimize и mini-messenger/messenger-page-link.
MINI_MESSENGER_EXPAND: str = "[data-marker='mini-messenger/expand']"

# Полная страница переписок — https://www.avito.ru/profile/messenger, диалоги в
# ней a[data-marker='channels/channelLink'] (ПОДТВЕРЖДЕНО в
# debug/chat_send_map_20260921T202037Z.txt). Рассылка туда НЕ ходит и селекторов
# для неё не держит: там личность собеседника не сверить с кандидатом.

# Форма ответа и поле ввода.
# ПОДТВЕРЖДЕНО в debug/send_button_map_20260921T203306Z.txt и в HTML-дампах:
# <form data-marker="reply"> → <textarea data-marker="reply/input"
# placeholder="Сообщение" rows="1" maxlength="1000">. Это обычная textarea.
# Рядом в DOM живёт ВТОРАЯ textarea [data-marker='icebreakers/textarea'] —
# поэтому селектор намеренно привязан и к форме, и к маркеру поля.
CHAT_FORM: str = "form[data-marker='reply']"
CHAT_INPUT: str = f"{CHAT_FORM} textarea[data-marker='reply/input']"

# Жёсткий лимит поля ввода Авито. ПОДТВЕРЖДЕНО: maxlength=1000 в обоих дампах.
# Текст длиннее браузер молча обрежет при наборе — проверяем ДО отправки.
CHAT_INPUT_MAXLENGTH: int = 1000

# Кнопка отправки. ПОДТВЕРЖДЕНО в debug/send_button_map_20260921T203306Z.txt:
# это <span role="button" data-marker="reply/send" aria-label="Отправить сообщение"
# aria-disabled="false">, БЕЗ видимого текста. В DOM появляется только после
# того, как в поле введён текст (до ввода на её месте reply/attachImage).
# Следствия для кода: подпись читается из aria-label (inner_text пуст),
# «доступность» — из aria-disabled, а искать кнопку можно только после fill().
CHAT_SEND: str = f"{CHAT_FORM} span[data-marker='reply/send']"
CHAT_SEND_ARIA_LABELS: frozenset[str] = frozenset({"Отправить сообщение", "Отправить"})

# Лента переписки. ПОДТВЕРЖДЕНО в debug/chat_send_map_20260921T202037Z.txt.
CHAT_HISTORY: str = "[data-marker='messagesHistory']"
CHAT_MESSAGE: str = "[data-marker='message']"

# Исходящее сообщение (наше) в ленте.
# Атрибута data-status у Авито НЕТ ВООБЩЕ — в разведке он пуст у всех элементов,
# поэтому прежний [data-status='sent'] не мог совпасть никогда.
# ПОДТВЕРЖДЕНО три независимых признака исходящего:
#   1) класс контейнера message-base-module-right-<хеш> (у входящего ...-left-);
#   2) класс текста message-text-module-root_right-<хеш>;
#   3) иконка статуса icon/messenger-statusDelivered → icon/messenger-statusRead,
#      которой у входящих сообщений нет.
# Хвосты-хеши генерируются при сборке Авито и меняются с их релизом, поэтому
# завязываемся только на префикс CSS-модуля, а признаки объединяем через ИЛИ.
# ВНИМАНИЕ: селектор рассчитан на нативный document.querySelectorAll (в нём
# :has() поддержан Chrome 105+), а не на движок локаторов Playwright.
CHAT_OUTGOING_MESSAGE: str = ", ".join(
    f"{CHAT_HISTORY} {CHAT_MESSAGE}{marker}" for marker in (
        "[class*='message-base-module-right-']",
        ":has([class*='message-text-module-root_right-'])",
        ":has([data-marker^='icon/messenger-status'])",
    )
)

# ---------------------------------------------------------------------------
# Страница объявления — детальная карточка
# ---------------------------------------------------------------------------

# Заголовок (h1) объявления.
# НЕ ПОДТВЕРЖДЕНО на живой странице — возможный вариант по общей практике.
# В новой вёрстке Авито заголовок может быть внутри блока title-info.
# Не менять/не заменять: на этот селектор уже завязана аналитика (parser.py).
ITEM_TITLE: str = "h1[class*='title']"

# Заголовок (h1) объявления — версия для «Разбора продавца» (seller_scan.py).
# ПОДТВЕРЖДЕНО на живой странице тем же дампом 21.09.2026, что и просмотры
# ниже: h1[data-marker='item-view/title-info'].
ITEM_TITLE_CONFIRMED: str = "h1[data-marker='item-view/title-info']"

# Цена объявления.
# НЕ ПОДТВЕРЖДЕНО на живой странице — общий вариант.
ITEM_PRICE: str = "[class*='price-value']"

# Адрес/метро на странице объявления.
# НЕ ПОДТВЕРЖДЕНО на живой странице — общий вариант.
ITEM_ADDRESS: str = "[class*='item-address']"

# Описание объявления (длинный текст).
# ПОДТВЕРЖДЕНО: data-marker="item-view/item-description" — xailiry/avito-parser-sdk
ITEM_DESCRIPTION: str = "[data-marker='item-view/item-description']"

# Просмотры ВСЕГО (суммарные за всё время).
# ПОДТВЕРЖДЕНО на живой странице: debug/item.html — views_total=841
ITEM_VIEWS_TOTAL: str = "[data-marker='item-view/total-views']"

# Просмотры СЕГОДНЯ / за последние сутки.
# ПОДТВЕРЖДЕНО на живой странице: debug/item.html — views_today=42
# ВАЖНО: Авито показывает «просмотров сегодня», а не «за день».
# В нашей логике это и есть «просмотров за день» (нужный показатель).
ITEM_VIEWS_TODAY: str = "[data-marker='item-view/today-views']"

# Дата публикации объявления.
# ПОДТВЕРЖДЕНО на живой странице: debug/item.html — текст вида "· 14 мая в 19:20"
# (ведущий «·» и неразрывные пробелы \xa0 — очищаются в parse_avito_date).
ITEM_DATE: str = "[data-marker='item-view/item-date']"

# Дата публикации — запасной вариант через тег <time>.
# НЕ ПОДТВЕРЖДЕНО на живой странице — общий HTML-стандарт.
ITEM_DATE_TIME_TAG: str = "time[datetime]"

# Имя продавца.
# ПОДТВЕРЖДЕНО: data-marker="seller-info/name" — xailiry/avito-parser-sdk
ITEM_SELLER_NAME: str = "[data-marker='seller-info/name']"

# ---------------------------------------------------------------------------
# JSON-блок на странице поиска (альтернативный способ получения данных)
# ---------------------------------------------------------------------------

# Тег скрипта, содержащего встроенный JSON с данными каталога.
# ПОДТВЕРЖДЕНО: Duff89/parser_avito (метод find_json_on_page):
#   soup.select('script[type="mime/invalid"][data-mfe-state="true"]')
# JSON содержит поля:
#   state.data.catalog.items[] — массив объявлений
#   state.data.catalog.items[].id
#   state.data.catalog.items[].title
#   state.data.catalog.items[].urlPath  (относительная ссылка)
#   state.data.catalog.items[].priceDetailed.value (цена в копейках)
#   state.data.catalog.items[].priceDetailed.string (строка «12 000 ₽»)
#   state.data.catalog.items[].addressDetailed.locationName
#   state.data.catalog.items[].geo.formattedAddress
#   state.data.catalog.items[].sortTimeStamp  (UNIX-время в миллисекундах)
#   state.data.searchCore — параметры поиска для пагинации
#   state.data.context    — контекст для API-запросов следующих страниц
SEARCH_JSON_SCRIPT: str = "script[type='mime/invalid'][data-mfe-state='true']"

# ---------------------------------------------------------------------------
# Пагинация (страница поиска)
# ---------------------------------------------------------------------------

# Кнопка «Следующая страница».
# НЕ ПОДТВЕРЖДЕНО на живой странице — наиболее вероятный вариант.
SEARCH_NEXT_PAGE: str = "[data-marker='pagination-button/nextPage']"

# ---------------------------------------------------------------------------
# Удобные агрегаты для использования в parser.py
# ---------------------------------------------------------------------------

# Словарь всех селекторов страницы поиска
SEARCH: dict[str, str] = {
    "card":         SEARCH_CARD,
    "card_id_attr": SEARCH_CARD_ID_ATTR,
    "title":        SEARCH_CARD_TITLE,
    "price":        SEARCH_CARD_PRICE,
    "location":     SEARCH_CARD_LOCATION,   # основной (ПОДТВЕРЖДЕНО на живой странице)
    "address":      SEARCH_CARD_ADDRESS,    # резерв
    "description":  SEARCH_CARD_DESCRIPTION,
    "description_text": SEARCH_CARD_DESCRIPTION_TEXT,
    "seller_marker": SEARCH_CARD_SELLER_MARKER,
    "json_script":  SEARCH_JSON_SCRIPT,
    "next_page":    SEARCH_NEXT_PAGE,
}

# Словарь всех селекторов страницы объявления
ITEM: dict[str, str] = {
    "title":        ITEM_TITLE,
    "price":        ITEM_PRICE,
    "address":      ITEM_ADDRESS,
    "description":  ITEM_DESCRIPTION,
    "views_total":  ITEM_VIEWS_TOTAL,
    "views_today":  ITEM_VIEWS_TODAY,
    "date":         ITEM_DATE,
    "date_time":    ITEM_DATE_TIME_TAG,
    "seller_name":  ITEM_SELLER_NAME,
}
