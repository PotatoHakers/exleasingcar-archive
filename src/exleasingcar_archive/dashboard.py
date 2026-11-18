"""Read-only local web dashboard for an Exleasingcar Archive SQLite database."""
from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exleasingcar Archive — просмотр базы</title>
<style>
:root{font-family:Inter,Segoe UI,Arial,sans-serif;color:#17202a;background:#f5f7fa}
*{box-sizing:border-box}body{margin:0}.top{background:#152238;color:#fff;padding:24px 28px}
.top h1{margin:0 0 6px;font-size:24px}.top p{margin:0;color:#c8d3e2;font-size:13px}
main{max-width:1500px;margin:22px auto;padding:0 20px 40px}.status{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:16px;color:#536273;font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;background:#1a9b68;display:inline-block;margin-right:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:#fff;border:1px solid #e1e7ef;border-radius:12px;padding:15px;box-shadow:0 1px 2px #182b4510}
.card .k{color:#667487;font-size:12px}.card .v{font-size:25px;font-weight:700;margin-top:7px;font-variant-numeric:tabular-nums}
section{background:#fff;border:1px solid #e1e7ef;border-radius:12px;margin:16px 0;padding:16px;box-shadow:0 1px 2px #182b4510;overflow:hidden}
h2{margin:0 0 12px;font-size:17px}.hint{color:#697789;font-size:12px;margin:-4px 0 12px}
.wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #edf0f4;vertical-align:top}th{color:#657386;font-weight:600;background:#f8fafc;position:sticky;top:0}tbody tr:hover{background:#f9fbfd}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.muted{color:#778394}.pill{display:inline-block;border-radius:999px;padding:3px 8px;background:#eef2f7;color:#46566c;font-size:11px}.good{background:#e8f5ee;color:#187347}.warn{background:#fff4df;color:#8c5a00}.empty{padding:18px;text-align:center;color:#7a8796}
footer{color:#7b8795;font-size:12px;margin-top:20px}
.search-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 14px}
.search-controls label{font-size:13px;color:#536273}
.search-controls input{flex:1;min-width:240px;max-width:560px;padding:10px 12px;border:1px solid #ccd5e0;border-radius:8px;font:inherit}
#search-status{font-size:12px}
@media(max-width:640px){.top{padding:20px}.top h1{font-size:20px}main{padding:0 12px 28px}.card .v{font-size:22px}}
</style>
</head>
<body>
<header class="top"><h1>Exleasingcar Archive</h1><p>Локальная панель просмотра SQLite · без изменения данных</p></header>
<main>
<div class="status"><span><i class="dot"></i>Подключено к локальной базе</span><span id="dbpath" class="mono"></span><span id="updated"></span></div>
<div id="metrics" class="cards"><div class="card">Загрузка…</div></div>
<section><h2>Лоты автомобилей</h2><p class="hint">Каждая строка — автомобиль в конкретном аукционе. Повторный выход на торги отображается отдельным лотом.</p><div class="search-controls"><label for="lot-search">Поиск по названию, ID или номеру кузова</label><input id="lot-search" type="search" autocomplete="off" placeholder="Например, Mercedes, 19702473 или VIN"><span id="search-status" class="muted" aria-live="polite"></span></div><div class="wrap"><table><thead><tr><th>Аукцион</th><th>ID автомобиля</th><th>Автомобиль</th><th>Тип продажи</th><th>Пробег, км</th><th>Номер кузова</th><th>Последняя ставка</th><th>Мин. цена продавца</th><th>Ориентир</th><th>Первое наблюдение</th></tr></thead><tbody id="lots"></tbody></table></div></section>
<section><h2>История реальных ставок</h2><p class="hint">Здесь отображаются только положительные значения highest_offer. Нулевая ставка не считается сделанной ставкой.</p><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Максимальная ставка</th><th>Источник</th></tr></thead><tbody id="bids"></tbody></table></div></section>
<section><h2>История цен продавца</h2><p class="hint">Минимальная и ориентировочная цены — отдельно от реальных ставок.</p><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Минимальная цена</th><th>Ориентировочная цена</th><th>Источник</th></tr></thead><tbody id="refs"></tbody></table></div></section>
<section><h2>Отображаемые и фиксированные цены</h2><p class="hint">Начальная/текущая цена открытого аукциона и цена «Купить сейчас» хранятся отдельно от ставок и справочных цен продавца.</p><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Тип цены</th><th>Сумма</th><th>Валюта</th><th>Поле источника</th></tr></thead><tbody id="lotprices"></tbody></table></div></section>
<section><h2>События аукционов</h2><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Фаза</th><th>Статус ставки</th><th>Конец аукциона</th><th>Осталось, сек.</th><th>FINAL CALL</th></tr></thead><tbody id="events"></tbody></table></div></section>
<footer>Автообновление каждые 5 секунд. Панель работает в режиме чтения: импорт и изменение БД выполняются отдельными командами.</footer>
</main>
<script>
const esc = v => String(v ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const cell = v => `<td>${esc(v)}</td>`;
function rows(id, values, empty) { const el=document.getElementById(id); el.innerHTML=values.length?values.join(""):`<tr><td class="empty" colspan="12">${esc(empty)}</td></tr>`; }
async function refresh(){
 try{
  const query=document.getElementById('lot-search').value.trim();
  const apiUrl='/api/data'+(query?'?q='+encodeURIComponent(query):'');
  const response=await fetch(apiUrl,{cache:'no-store'}); if(!response.ok) throw new Error(await response.text());
  const d=await response.json();
  document.getElementById('dbpath').textContent=d.database;
  document.getElementById('updated').textContent='Обновлено: '+new Date().toLocaleTimeString();
  const metrics=[['Аукционов',d.counts.auctions],['Автомобилей',d.counts.vehicles],['Лотов',d.counts.auction_lots],['Наблюдений ставок',d.counts.price_observations],['Цен продавца',d.counts.price_reference_observations],['Отображаемых/фиксированных цен',d.counts.lot_price_observations],['Событий',d.counts.auction_events]];
  document.getElementById('metrics').innerHTML=metrics.map(x=>`<div class="card"><div class="k">${esc(x[0])}</div><div class="v">${esc(x[1])}</div></div>`).join('');
  document.getElementById('search-status').textContent=query?`Найдено лотов: ${d.lots.length}`:`Показаны последние ${d.lots.length} из ${d.counts.auction_lots}`;
  rows('lots',d.lots.map(x=>`<tr>${cell(x.auction_id)}${cell(x.car_id)}${cell(x.title)}${cell(x.sale_type)}${cell(x.mileage_km)}${cell(x.chassis)}${cell(x.highest_offer)}${cell(x.minimum_price)}${cell(x.indicative_price)}${cell(x.first_seen_at)}</tr>`),query?'По этому запросу ничего не найдено':'Лотов пока нет');
  rows('bids',d.bids.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${cell(x.highest_offer)}${cell(x.source)}</tr>`),'Положительных ставок пока не записано. Это нормально, если highest_offer равен нулю.');
  rows('refs',d.references.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${cell(x.minimum_price)}${cell(x.indicative_price)}${cell(x.source)}</tr>`),'Наблюдений цен продавца пока нет');
  rows('lotprices',d.lot_prices.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${cell(x.price_type)}${cell(x.amount)}${cell(x.currency)}${cell(x.source_field)}</tr>`),'Отображаемые и фиксированные цены пока не записаны');
  rows('events',d.events.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${cell(x.auction_phase)}${cell(x.bid_status)}${cell(x.auction_end_at)}${cell(x.remaining_seconds)}<td>${x.final_call_active===1?'<span class="pill warn">Да</span>':x.final_call_active===0?'<span class="pill">Нет</span>':'—'}</td></tr>`),'Событий пока нет');
 }catch(e){document.getElementById('updated').textContent='Ошибка чтения базы: '+e.message;}
}
const searchInput=document.getElementById('lot-search');
let searchTimer;
searchInput.addEventListener('input',()=>{
 clearTimeout(searchTimer);
 searchTimer=setTimeout(refresh,250);
});
refresh();setInterval(refresh,5000);
</script>
</body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    database_path: Path

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        request_url = urlsplit(self.path)
        if request_url.path == "/api/data":
            try:
                search_query = parse_qs(
                    request_url.query, keep_blank_values=True
                ).get("q", [""])[0]
                payload = self._load_data(search_query=search_query)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except (sqlite3.Error, KeyError) as exc:
                self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _load_data(self, search_query: str | None = None) -> dict[str, Any]:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Database not found: {self.database_path}")
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < 5:
                raise sqlite3.DatabaseError(
                    f"Database schema is {version}; run the collector or snapshot importer "
                    "once to migrate it to schema 5"
                )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {
                "auctions", "vehicles", "auction_lots", "price_observations",
                "price_reference_observations", "lot_price_observations",
                "auction_events",
            }
            missing = required - tables
            if missing:
                raise sqlite3.DatabaseError(
                    "Missing database tables: " + ", ".join(sorted(missing))
                )

            count_tables = (
                "auctions", "vehicles", "auction_lots", "price_observations",
                "price_reference_observations", "lot_price_observations",
                "auction_events",
            )
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in count_tables
            }
            normalized_search = " ".join((search_query or "").split())[:100]
            lots_sql = """
                SELECT a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id,
                       COALESCE(v.displayed_title, trim(COALESCE(v.make,'') || ' ' || COALESCE(v.model,''))) AS title,
                       al.sale_type,
                       al.mileage_km,
                       CASE
                         WHEN v.chassis_number IS NOT NULL THEN v.chassis_number
                         WHEN v.chassis_number_fragment IS NOT NULL THEN v.chassis_number_fragment ||
                              CASE WHEN COALESCE(v.chassis_number_masked,0)=1 THEN '… (скрыт)' ELSE '' END
                         ELSE NULL
                       END AS chassis,
                       (SELECT po.highest_offer FROM price_observations po
                        WHERE po.auction_lot_id=al.id ORDER BY po.observed_at DESC, po.id DESC LIMIT 1) AS highest_offer,
                       (SELECT pr.minimum_price FROM price_reference_observations pr
                        WHERE pr.auction_lot_id=al.id ORDER BY pr.observed_at DESC, pr.id DESC LIMIT 1) AS minimum_price,
                       (SELECT pr.indicative_price FROM price_reference_observations pr
                        WHERE pr.auction_lot_id=al.id ORDER BY pr.observed_at DESC, pr.id DESC LIMIT 1) AS indicative_price,
                       al.first_seen_at
                FROM auction_lots al
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
            """
            parameters: tuple[str, ...] = ()
            if normalized_search:
                lots_sql += """
                    WHERE (
                        lower(COALESCE(v.displayed_title, '') || ' ' ||
                              COALESCE(v.make, '') || ' ' || COALESCE(v.model, ''))
                            LIKE lower(?)
                        OR lower(v.source_car_id) LIKE lower(?)
                        OR lower(COALESCE(v.chassis_number, '')) LIKE lower(?)
                        OR lower(COALESCE(v.chassis_number_fragment, '')) LIKE lower(?)
                    )
                """
                pattern = f"%{normalized_search}%"
                parameters = (pattern, pattern, pattern, pattern)
            lots_sql += " ORDER BY al.last_seen_at DESC, al.id DESC"
            if not normalized_search:
                lots_sql += " LIMIT 500"
            lots = [dict(row) for row in connection.execute(lots_sql, parameters)]
            bids = [dict(row) for row in connection.execute(
                """
                SELECT po.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, po.highest_offer, po.source
                FROM price_observations po
                JOIN auction_lots al ON al.id=po.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                ORDER BY po.observed_at DESC, po.id DESC LIMIT 100
                """
            )]
            references = [dict(row) for row in connection.execute(
                """
                SELECT pr.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, pr.minimum_price,
                       pr.indicative_price, pr.source
                FROM price_reference_observations pr
                JOIN auction_lots al ON al.id=pr.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                ORDER BY pr.observed_at DESC, pr.id DESC LIMIT 100
                """
            )]
            lot_prices = [dict(row) for row in connection.execute(
                """
                SELECT lp.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, lp.price_type, lp.amount,
                       lp.currency, lp.source_field
                FROM lot_price_observations lp
                JOIN auction_lots al ON al.id=lp.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                ORDER BY lp.observed_at DESC, lp.id DESC LIMIT 100
                """
            )]
            events = [dict(row) for row in connection.execute(
                """
                SELECT ae.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, ae.auction_phase, ae.bid_status,
                       ae.auction_end_at, ae.remaining_seconds, ae.final_call_active
                FROM auction_events ae
                JOIN auction_lots al ON al.id=ae.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                ORDER BY ae.observed_at DESC, ae.id DESC LIMIT 100
                """
            )]
        return {
            "database": str(self.database_path),
            "schema_version": version,
            "counts": counts,
            "lots": lots,
            "bids": bids,
            "references": references,
            "lot_prices": lot_prices,
            "events": events,
        }

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the console output concise; BaseHTTPRequestHandler's default is noisy.
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start a read-only local dashboard for an Exleasingcar SQLite archive."
    )
    parser.add_argument(
        "--database", type=Path, default=Path("data/fresh_api_test.sqlite3")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    database_path = args.database
    if not database_path.is_file():
        raise SystemExit(
            f"Database not found: {database_path}. Run snapshot_import first."
        )

    # Deliberately bind to localhost only. The dashboard has no write endpoints.
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {})
    handler.database_path = database_path
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Dashboard is running: http://{args.host}:{args.port}")
    print(f"Database (read-only): {database_path}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
