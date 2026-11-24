import sqlite3
import subprocess
import sys
from pathlib import Path

from exleasingcar_archive.database import SCHEMA_VERSION, ArchiveDatabase

ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = ROOT / "real_site_validation_report.py"


def run_report(database_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), str(database_path)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_validation_report_accepts_current_schema_and_summarizes_scan_tables(tmp_path):
    database = ArchiveDatabase(tmp_path / "validation.sqlite3")
    database.initialize()

    result = run_report(database.path)

    assert result.returncode == 0, result.stderr + result.stdout
    assert f"SQLite schema version: {SCHEMA_VERSION}" in result.stdout
    assert "catalog_scan_runs:" in result.stdout
    assert "catalog_scan_pages:" in result.stdout
    assert "catalog_deferred_api_lots:" in result.stdout
    assert "Catalogue progress:" in result.stdout
    assert "STRUCTURAL CHECKS PASSED" in result.stdout


def test_validation_report_fails_closed_for_an_old_schema(tmp_path):
    database_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 13")

    result = run_report(database_path)

    assert result.returncode == 1
    assert "expected current: 18" in result.stdout
    assert "CHECK FAILED" in result.stdout
