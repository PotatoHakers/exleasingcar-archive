# Первый глобальный запуск Exleasingcar Archive (Windows)

## Что делает этот режим

Сборщик сначала проходит **все страницы списка аукционов**, записывает найденные аукционы в SQLite и создаёт постоянную очередь первичного сбора. Затем он обрабатывает очередь пакетами, используя общий лимит страниц и времени.

Прогресс глобального обнаружения и состояние очереди сохраняются в базе. Если запуск остановлен по лимиту или процесс завершился, следующий запуск продолжает с последнего сохранённого checkpoint. Обнаружение списка повторяется не чаще раза в 24 часа; незавершённая discovery-сессия всегда возобновляется.

Это первый глобальный bootstrap. Периодическое расписание торгов (5 минут / 2 часа / ежедневно), автоматическая ротация всех detail-страниц и выгрузка файлов в R2 здесь намеренно не включаются: сначала нужно проверить полный проход на реальном объёме данных.

## 1. Подготовить отдельную базу

Не используй для первого глобального запуска data/fresh_api_test.sqlite3, если там уже есть тестовые или проверочные данные. Новая рекомендуемая база:

**data/exleasingcar_archive.sqlite3**

Существующие SQLite-файлы сборщик не удаляет. Перед миграцией любой существующей базы сделай копию. Для SQLite безопаснее использовать механизм backup, а не копировать активный файл, если включён WAL.

## 2. Установить приложение

Открой PowerShell в корне репозитория:

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
~~~

В зависимостях теперь явно указаны Playwright и keyring. Chromium устанавливается отдельной командой.

Если PowerShell запрещает активацию окружения, можно не активировать его и вызывать Python напрямую:

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
~~~

## 3. Сохранить логин в Windows Credential Manager

Пароль не передаётся через аргументы командной строки и не сохраняется в файлах проекта. Выполни команду: логин будет введён обычным приглашением, пароль — скрытым вводом.

~~~powershell
python -c "import getpass,keyring; keyring.set_password('exleasingcar-archive','login',input('Exleasingcar login: ')); keyring.set_password('exleasingcar-archive','password',getpass.getpass('Exleasingcar password: ')); print('Credentials saved to the configured OS keyring.')"
~~~

Проверка без вывода значений:

~~~powershell
python -c "import keyring; print('login:', bool(keyring.get_password('exleasingcar-archive','login'))); print('password:', bool(keyring.get_password('exleasingcar-archive','password')))"
~~~

Не коммить .env, профиль браузера, SQLite и локальные HTML-снимки. Впервые сайт откроется в отдельном persistent Chromium profile: войди в аккаунт обычным способом и вручную пройди только те проверки, которые запросит сам сайт. CAPTCHA и дополнительные проверки сборщик не обходит.

## 4. Сначала проверить полный список и очередь

~~~powershell
.\scripts\run-global-bootstrap.ps1 -PlanOnly
~~~

Команда авторизует браузер, проходит страницы списка аукционов в пределах лимита запуска и заполняет очередь, но **не открывает каталоги самих аукционов**.

При большом списке страниц discovery может не завершиться за один запуск. Просто запусти команду ещё раз: курсор списка сохранён в SQLite.

В конце посмотри:
- сколько страниц списка просмотрено;
- сколько карточек аукционов обнаружено;
- сколько заданий в очереди pending, in_progress, completed.

## 5. Запустить первичный сбор по всей очереди

~~~powershell
.\scripts\run-global-bootstrap.ps1
~~~

По умолчанию включены безопасные лимиты на один запуск:

| Ограничение | Значение |
|---|---:|
| Время | 900 секунд |
| Страницы списка + каталога аукционов суммарно | 40 |
| Live API-лоты | 500 |
| Detail-страницы автомобилей | 60 |

Параметр --max-auctions 0 означает всю доступную очередь, а не один аукцион. Реальный объём одного запуска всё равно ограничен этими бюджетами.

Повторяй запуск, пока очередь первичного сбора не станет пустой. Каждый запуск продолжает незавершённые catalogue checkpoints; завершённые bootstrap-задания не открываются заново только из-за очередного запуска.

Принудительно обновить полный список аукционов:

~~~powershell
.\scripts\run-global-bootstrap.ps1 -ForceDiscovery
~~~

Проверить отдельный аукцион:

~~~powershell
.\.venv\Scripts\python.exe -m exleasingcar_archive.collector --database data/exleasingcar_archive.sqlite3 --profile-dir data/browser_profile --raw-dir data/raw/collector --auction-id 211021
~~~

## 6. Открыть Dashboard

После первого успешного прохода:

~~~powershell
.\scripts\run-dashboard.ps1
~~~

Открой http://127.0.0.1:8765. Dashboard остаётся привязанным к localhost. Не публикуй его напрямую в интернет и не меняй адрес на 0.0.0.0.

## 7. Резервная копия

Создать согласованную SQLite-копию через Python:

~~~powershell
python -c "import sqlite3; source=sqlite3.connect('data/exleasingcar_archive.sqlite3'); target=sqlite3.connect('data/exleasingcar_archive.backup.sqlite3'); source.backup(target); target.close(); source.close(); print('Backup completed.')"
~~~

Храни backup отдельно от рабочей папки проекта и проверяй, что файл действительно открывается.

## Важные границы этой версии

- Глобальный список аукционов открывается и checkpoint-ится постранично. Цикл пагинации не будет молча принят за завершённое обнаружение.
- Очередь первичного сбора и checkpoints хранятся в SQLite; повторная обработка страницы безопасна.
- При трёх последовательных ошибках отдельных аукционов пакет останавливается, а проблемное задание остаётся в очереди на повтор.
- Discovery списка обновляется раз в 24 часа, если не указан -ForceDiscovery.
- Это **ручной пакетный режим**, пока не Windows-служба и не Task Scheduler. Не настраивай автозапуск каждые 5 минут до проверки первого полного bootstrap.
- В этой версии detail-page rotation по-прежнему ограничена общим бюджетом. Полный многодневный планировщик live-refresh и отдельная постоянная очередь detail-enrichment — следующие этапы после проверки глобального bootstrap.
