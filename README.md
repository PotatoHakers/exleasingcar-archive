# Exleasingcar Archive

Локальный архив публичных данных Exleasingcar.

## Проверка кода

```bash
source .venv/Scripts/activate
python -m ruff check src tests
python -m pytest -q
```

## Ограниченный тестовый сбор через браузер

Для прогона одного аукциона с ограничением количества страниц:

```bash
PYTHONPATH=src python -m exleasingcar_archive.collector \
  --database "data/fresh_api_test.sqlite3" \
  --auction-id 209633 \
  --max-auctions 1 \
  --max-detail-pages 5
```

## Импорт сохранённого HTML-снимка

В репозитории есть небольшой пример снимка в `tests/fixtures/fresh_docu_sample/`:

```bash
PYTHONPATH=src python -m exleasingcar_archive.snapshot_import \
  --auction-list tests/fixtures/fresh_docu_sample/auction_list.html \
  --catalog tests/fixtures/fresh_docu_sample/car_list.html \
  --vehicle-detail tests/fixtures/fresh_docu_sample/car.html \
  --api-response tests/fixtures/fresh_docu_sample/response.txt \
  --database data/parser_validation.sqlite3
```

Команда импортирует сохранённые данные в локальную SQLite-базу и не выполняет сетевых запросов.

## Архивация фотографий и PDF в Cloudflare R2

**Расходы R2 — жёсткое ограничение проекта.** Загрузчик не обращается к R2, пока не получит актуальную account-wide аналитику Cloudflare и не проверит внутренний бюджет. При ошибке аналитики он останавливается.

Скопируй `.env.example` в локальный `.env`. Для S3-доступа к приватному bucket заполни:

- `EXLEASINGCAR_R2_ACCOUNT_ID`
- `EXLEASINGCAR_R2_BUCKET`
- `EXLEASINGCAR_R2_ACCESS_KEY_ID`
- `EXLEASINGCAR_R2_SECRET_ACCESS_KEY`

Для проверки расходов создай **отдельный** Cloudflare API token с разрешением только `Account Analytics Read` и заполни `EXLEASINGCAR_CF_ACCOUNT_ANALYTICS_TOKEN`. Не расширяй S3-токен на права управления аккаунтом и не добавляй секреты в Git.

Защитные лимиты по умолчанию установлены на половине бесплатного тарифа R2 Standard: 500 000 Class A операций, 5 000 000 Class B операций и 5 GB пикового хранения. Можно уменьшать эти значения, но приложение не позволит поднять их выше этих внутренних потолков. Локальный журнал резервирования хранится в `data/r2_usage_budget.sqlite3`; его нельзя удалять или сбрасывать между запусками.

Перед любой реальной загрузкой проверь account-wide потребление:

```bash
PYTHONPATH=src python -m exleasingcar_archive.r2_budget_report
```

Команда только читает Cloudflare Analytics и локальный журнал; она не делает запросов к R2 bucket. Она должна завершиться статусом `READY`. Если аналитика недоступна или устарела, загрузчик блокируется.

Только после успешного отчёта можно запускать ограниченный прогон:
Прогон R2:

```bash
PYTHONPATH=src python -m exleasingcar_archive.media_downloader \
  --database "data/fresh_api_test.sqlite3" \
  --batch-size 1
```

По умолчанию уже сохранённые файлы пропускаются. Флаг `--refresh` повторно проверяет ранее скачанные URL и сохраняет новую версию, если содержимое изменилось. Лимиты размера файла: 25 MiB на фото и 100 MiB на PDF.

Cloudflare учитывает квоты на уровне аккаунта: другие R2 bucket/приложения в том же аккаунте используют тот же бесплатный пул. Для надёжной защиты не запускай другие R2-загрузчики в этом аккаунте и не используй Infrequent Access. Внутренний guard останавливает операции **этого приложения**, но Cloudflare не предоставляет жёсткого автоматического spend cap для R2. Прямые presigned GET-ссылки пока отключены, поскольку браузерные чтения нельзя учитывать локальным журналом; чтение медиа нужно будет проводить через защищённый прокси приложения.
