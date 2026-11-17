# Exleasingcar Archive

Локальный архив публичных данных Exleasingcar

```bash
source .venv/Scripts/activate
python -m ruff check src tests
python -m pytest -q
```

## Тестовый сбор через браузер

Для ограниченного прогона одного аукциона:

```bash
PYTHONPATH=src python -m exleasingcar_archive.collector \
  --database "data/fresh_api_test.sqlite3" \
  --auction-id 209633 \
  --max-auctions 1 \
  --max-detail-pages 5
```


```bash
PYTHONPATH=src python -m exleasingcar_archive.snapshot_import \
  --auction-list docu/auction_list.html \
  --catalog docu/car_list.html \
  --vehicle-detail docu/car.html \
  --api-response docu/response.txt \
  --database data/parser_validation.sqlite3
```
