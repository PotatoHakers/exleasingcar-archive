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
