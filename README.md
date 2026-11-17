# Exleasingcar Archive

Локальный архив публичных данных Exleasingcar. Проект работает в режиме чтения:
не размещает ставки, не покупает автомобили и не изменяет лоты.

## Разработка и тесты

В Git Bash из каталога проекта:

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

Для полного прохода по карточкам выбранного аукциона укажи `--max-detail-pages -1`.
Сначала рекомендуется проверить небольшой прогон, затем запускать полный.

## Типы продажи и история цен

Схема SQLite версии 5 добавляет тип продажи к конкретному лоту и отдельную
таблицу `lot_price_observations`. При открытии старой базы миграция выполняется
автоматически через `ArchiveDatabase.initialize()` и сохраняет прежние записи.

- `open_auction`: открытый аукцион. Публично отображаемая начальная/текущая цена
  сохраняется отдельно; положительный `highest_offer` остаётся в истории ставок.
- `closed_auction`: закрытый аукцион. Тип сохраняется даже без доступной цены.
- `fixed_price`: фиксированная цена извлекается из `.fixed-price .buy-offer`;
  schema.org `offers.price` используется только как запасной источник после
  подтверждения фиксированного режима.
- Неизвестный режим остаётся `NULL`, сбор остальных данных продолжается.
- `price_reference_observations` по-прежнему хранит справочные минимальную и
  ориентировочную цены продавца. Они не считаются ставками или ценой покупки.

Панель просмотра (`dashboard`) показывает тип продажи и историю отображаемых/
фиксированных цен отдельно от ставок.

## Проверка сохранённых HTML-снимков без входа на сайт

В архиве есть снимки аукциона 210275. Их можно импортировать в отдельную
проверочную БД:

```bash
PYTHONPATH=src python -m exleasingcar_archive.snapshot_import \
  --auction-list docu/auction_list.html \
  --catalog docu/car_list.html \
  --vehicle-detail docu/car.html \
  --api-response docu/response.txt \
  --database data/parser_validation.sqlite3
```

Импортёр предупредит, если для каталога не переданы все страницы пагинации.
