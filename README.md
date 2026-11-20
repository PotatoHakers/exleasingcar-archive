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

Скопируй `.env.example` в локальный `.env` и заполни параметры приватного R2 bucket:

- `EXLEASINGCAR_R2_ACCOUNT_ID`
- `EXLEASINGCAR_R2_BUCKET`
- `EXLEASINGCAR_R2_ACCESS_KEY_ID`
- `EXLEASINGCAR_R2_SECRET_ACCESS_KEY`

Файл `.env` не добавляй в Git. Загрузчик читает его автоматически.

Для первого реального прогона выбери небольшую пачку из одного файла:

```bash
PYTHONPATH=src python -m exleasingcar_archive.media_downloader \\
  --database "data/fresh_api_test.sqlite3" \\
  --batch-size 1
```

По умолчанию уже сохранённые файлы пропускаются. Флаг `--refresh` повторно проверяет ранее скачанные URL и сохраняет новую версию, если содержимое изменилось. Лимиты по умолчанию: 25 MiB на фото и 100 MiB на PDF.
