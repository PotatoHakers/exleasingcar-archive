# Exleasingcar Archive

An open-source project for collecting and archiving vehicle prices
and auction results from Exleasingcar.

Запуск .venv
cd ~/projects/exleasingcar-archive
source .venv/Scripts/activate

Запуск тестов
python -m ruff check src tests
python -m pytest -q

Запуск тестового браузера
PYTHONPATH=src python -m exleasingcar_archive.collector --database "data/fresh_api_test.sqlite3" --auction-id 209633 --max-auctions 1 --max-detail-pages 5