# Exleasingcar Archive

Локальный архив публичных данных Exleasingcar.

## Проверка кода

Активируй виртуальное окружение и выполни проверки из корня репозитория:

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

**Расходы R2 — жёсткое ограничение проекта.** Приложение должно отказываться от R2-операций, если не может подтвердить достаточный запас account-wide бюджета. При ошибке или устаревшей аналитике запись блокируется.

Скопируй `.env.example` в локальный `.env`. Для S3-доступа к приватному bucket заполни:

- `EXLEASINGCAR_R2_ACCOUNT_ID`
- `EXLEASINGCAR_R2_BUCKET`
- `EXLEASINGCAR_R2_ACCESS_KEY_ID`
- `EXLEASINGCAR_R2_SECRET_ACCESS_KEY`

Для проверки расходов используй **отдельный** Cloudflare API token только с разрешением `Account Analytics Read` и переменную `EXLEASINGCAR_CF_ACCOUNT_ANALYTICS_TOKEN`. Не расширяй S3-токен на права управления аккаунтом и не добавляй секреты в Git.

Внутренние защитные потолки приложения установлены на половине бесплатного тарифа R2 Standard:

- 500 000 Class A операций;
- 5 000 000 Class B операций;
- 5 GB пикового хранения.

Эти лимиты независимы друг от друга. Квоты Cloudflare общие на уровне аккаунта: другие buckets и приложения расходуют тот же пул. Локальный журнал резервирования хранится в `data/r2_usage_budget.sqlite3`; его нельзя удалять или сбрасывать между запусками.

Проверить доступность account-wide аналитики и локальный бюджет можно командой:

```bash
PYTHONPATH=src python -m exleasingcar_archive.r2_budget_report
```

Команда читает Cloudflare Analytics и локальный журнал, но не обращается к R2 bucket. Статус `READY` означает только, что проверка бюджета прошла на момент отчёта. Он **не является разрешением** на реальную загрузку и не гарантирует отсутствие расходов: Cloudflare не предоставляет жёсткий автоматический spend cap для R2.

**Любой реальный smoke test с R2 требует отдельного явного согласования владельца проекта.** До такого согласования запрещены реальные R2 HEAD/GET/PUT, загрузки, скачивания и получение/использование presigned URL. CI и обычные тесты должны использовать fake R2, mock analytics и mock HTTP без реальных credentials.

Плановые ограничения downloader: до 25 MiB на фото и 100 MiB на PDF; конкретные лимиты не заменяют account-wide budget guard. Используй только класс Standard, не переключайся на Infrequent Access. Прямые presigned GET-ссылки нельзя выдавать для обычного просмотра: чтение медиа должно быть реализовано через контролируемый приложением proxy/accounting.
