@echo off
REM Запуск Chrome в режиме отладки для подключения парсера (CDP).
REM Открывает отдельное окно Chrome с выделенным профилем, обычный Chrome не трогает.
REM
REM --remote-allow-origins=* ОБЯЗАТЕЛЕН для новых Chrome (~v111+, тем более 149):
REM   без него браузерный websocket подключается, но Chrome отклоняет CDP-сессии
REM   к вкладкам по origin-политике, и Playwright connect_over_cdp виснет до таймаута
REM   («<ws connected> ... Timeout 30000ms exceeded»).
REM --remote-debugging-address=127.0.0.1 фиксирует привязку к IPv4-loopback, чтобы
REM   Node/Playwright (резолвит localhost в 127.0.0.1) гарантированно достучался.
REM --disable-features=WebUIOmniboxPopup профилактически отключает новую WebUI-
REM   выпадашку адресной строки. Chrome 150 может всё равно показать её в CDP как
REM   type=browser_ui. Backend только диагностирует эту цель и НИКОГДА не вызывает
REM   для неё /json/close: такой запрос может завершить весь Chrome с вкладками.
REM shortcut: HTTP/2 ломает загрузку фото (ERR_HTTP2_PROTOCOL_ERROR), включить после проверки исправленного сетевого канала.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --disable-http2 --disable-features=WebUIOmniboxPopup,WebUIOmniboxAimPopup --user-data-dir="%USERPROFILE%\avito-chrome-profile"
