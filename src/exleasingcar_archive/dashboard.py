"""Read-only local web dashboard for an Exleasingcar Archive SQLite database."""
from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .version import APP_VERSION

LOT_PAGE_SIZE = 500
MEDIA_SOURCE_HOSTS = frozenset({"data.exleasingcar.com", "www.exleasingcar.com"})


def _safe_exleasingcar_url(value: str | None) -> str | None:
    """Return a plain HTTPS source URL without credentials or query-string tokens."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.casefold() != "https"
        or (parts.hostname or "").casefold() not in MEDIA_SOURCE_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    return value.strip()



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
.wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #edf0f4;vertical-align:top}th{color:#657386;font-weight:600;background:#f8fafc;position:sticky;top:0}tbody tr:hover{background:#f9fbfd}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.muted{color:#778394}.pill{display:inline-block;border-radius:999px;padding:3px 8px;background:#eef2f7;color:#46566c;font-size:11px}.marker-list{display:flex;gap:5px;align-items:flex-start;flex-wrap:wrap;min-width:180px}.marker-pill{display:inline-flex;flex-direction:column;gap:2px;max-width:260px;border:1px solid #dce4ed;border-radius:8px;padding:4px 7px;background:#f8fafc;white-space:normal}.marker-pill>span{font-size:11px;color:#34465c}.marker-pill small{font-size:9px;color:#758398}.good{background:#e8f5ee;color:#187347}.warn{background:#fff4df;color:#8c5a00}.empty{padding:18px;text-align:center;color:#7a8796}
footer{color:#7b8795;font-size:12px;margin-top:20px}
.search-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 14px}
.search-controls label{font-size:13px;color:#536273}
.search-controls input{flex:1;min-width:240px;max-width:560px;padding:10px 12px;border:1px solid #ccd5e0;border-radius:8px;font:inherit}
.search-controls select{min-width:170px;padding:10px 12px;border:1px solid #ccd5e0;border-radius:8px;background:#fff;font:inherit}
#search-status{font-size:12px}
.pagination-controls{display:flex;justify-content:flex-end;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;color:#536273;font-size:12px}
.pagination-controls[hidden]{display:none}
.pagination-controls button{border:1px solid #cbd5e1;background:#fff;color:#23364c;padding:7px 11px;border-radius:7px;cursor:pointer;font:inherit}
.pagination-controls button:disabled{opacity:.45;cursor:default}
.detail-card{background:#fff;border:1px solid #ccd7e3;border-radius:12px;padding:18px;margin:16px 0;box-shadow:0 2px 8px #182b450c}
.detail-card[hidden]{display:none}
.detail-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.detail-top h2{margin:0 0 5px}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:16px 0}
.detail-item{padding:10px 12px;background:#f7f9fc;border-radius:8px;min-width:0}
.detail-item .k{display:block;font-size:11px;color:#687789;margin-bottom:5px}
.detail-item .v{font-size:13px;overflow-wrap:anywhere}
.detail-auctions{margin-top:16px;overflow:auto}
.detail-auctions table{font-size:12px}
.lot-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:14px 0 18px}
.lot-photo{border:1px solid #e1e7ef;border-radius:9px;overflow:hidden;background:#f8fafc}
.lot-photo img{display:block;width:100%;height:150px;object-fit:contain;background:#eef2f7}
.lot-photo .photo-meta{padding:8px 10px;font-size:11px;color:#637185;display:flex;justify-content:space-between;gap:8px}
.lot-photo a,.report-link{color:#235b91;text-decoration:none}
.lot-photo a:hover,.report-link:hover{text-decoration:underline}
.lot-empty{border:1px dashed #cbd5e1;padding:18px;border-radius:9px;color:#6c7a8a;text-align:center}
.lot-price-primary{border:1px solid #cbd7e4;border-radius:10px;padding:15px;margin:14px 0;background:#f8fafc}
.lot-price-primary .k{font-size:12px;color:#596a7e}
.lot-price-primary .v{font-size:27px;font-weight:700;margin:5px 0;font-variant-numeric:tabular-nums}
.lot-prices{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:9px;margin:12px 0}
.lot-subsection{margin:18px 0 0}
.lot-subsection h3{font-size:15px;margin:0 0 9px}
.lot-report-list{display:grid;gap:7px}
.lot-report{padding:9px 11px;border:1px solid #e2e8f0;border-radius:8px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.lot-note{font-size:12px;color:#687789;margin:8px 0}
.lot-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.version{display:inline-block;margin-left:8px;border:1px solid #52657e;border-radius:999px;padding:2px 8px;font-size:11px;color:#e3ebf5;vertical-align:middle}
.open-car,.close-card{border:1px solid #cbd5e1;background:#fff;color:#23364c;padding:6px 10px;border-radius:7px;cursor:pointer;font:inherit;font-size:12px}
.open-car:hover,.close-card:hover{background:#eef3f8}
@media(max-width:640px){.top{padding:20px}.top h1{font-size:20px}main{padding:0 12px 28px}.card .v{font-size:22px}}
</style>
</head>
<body>
<header class="top"><h1>Exleasingcar Archive <span class="version">v__APP_VERSION__</span></h1><p>Локальная панель просмотра SQLite · без изменения данных</p></header>
<main>
<div class="status"><span><i class="dot"></i>Подключено к локальной базе</span><span id="dbpath" class="mono"></span><span id="updated"></span></div>
<div id="metrics" class="cards"><div class="card">Загрузка…</div></div>
<section><h2>Лоты автомобилей</h2><p class="hint">Каждая строка — конкретный лот в конкретном аукционе. Повторный выход одной машины отображается отдельно. Откройте лот, чтобы посмотреть его фотографии, отчёты и собственную историю цен. Маркеры относятся к карточке аукциона.</p><div class="search-controls"><label for="make-filter">Марка</label><select id="make-filter"><option value="">Все марки</option></select><label for="lot-search">Поиск по названию, ID или номеру кузова</label><input id="lot-search" type="search" autocomplete="off" placeholder="Например, Mercedes, 19702473 или VIN"><span id="search-status" class="muted" aria-live="polite"></span></div><section id="car-card" class="detail-card" hidden aria-live="polite"></section><div class="wrap"><table><thead><tr><th></th><th>Аукцион</th><th>ID автомобиля</th><th>Автомобиль</th><th>Тип продажи</th><th>Маркеры аукциона</th><th>Пробег, км</th><th>Номер кузова</th><th>API highest_offer (€)</th><th>Мин. цена продавца (€)</th><th>Ориентир (€)</th><th>Проверка API</th><th>Первое наблюдение</th></tr></thead><tbody id="lots"></tbody></table></div><div id="lot-pagination" class="pagination-controls" hidden><button type="button" id="page-prev">← Назад</button><span id="page-info"></span><button type="button" id="page-next">Дальше →</button></div></section>
<section><h2>История значений API highest_offer</h2><p class="hint">Здесь отображаются все полученные значения поля highest_offer, включая 0, если поле присутствовало в API-ответе. Отсутствующее поле не создаёт наблюдение. Смысл этого поля пока не подтверждён: не считайте каждое значение доказанной публичной ставкой.</p><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Значение highest_offer (€)</th><th>Источник</th></tr></thead><tbody id="bids"></tbody></table></div></section>
<section><h2>История цен продавца</h2><p class="hint">Минимальная и ориентировочная цены — отдельно от API highest_offer и отображаемых цен.</p><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Минимальная цена (€)</th><th>Ориентировочная цена (€)</th><th>Источник</th></tr></thead><tbody id="refs"></tbody></table></div></section>
<section><h2>Отображаемые и фиксированные цены</h2><p class="hint">Начальная/текущая цена открытого аукциона и цена «Купить сейчас» хранятся отдельно от API highest_offer и справочных цен продавца.</p><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Тип цены</th><th>Сумма (€)</th><th>Валюта</th><th>Поле источника</th></tr></thead><tbody id="lotprices"></tbody></table></div></section>
<section><h2>События аукционов</h2><div class="wrap"><table><thead><tr><th>Дата наблюдения</th><th>Аукцион</th><th>ID автомобиля</th><th>Фаза</th><th>Статус ставки</th><th>Конец аукциона</th><th>Осталось, сек.</th><th>FINAL CALL</th></tr></thead><tbody id="events"></tbody></table></div></section>
<footer>Автообновление каждые 5 секунд. Панель работает в режиме чтения: импорт и изменение БД выполняются отдельными командами.</footer>
</main>
<script>
const esc = v => String(v ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const cell = v => `<td>${esc(v)}</td>`;
const moneyCell = v => `<td>${v == null ? '—' : '€ ' + esc(v)}</td>`;
const markerGroupLabels = {
 auction_format:'Формат торгов',
 fixed_price_option:'Фиксированная цена',
 vehicle_condition:'Состояние автомобиля',
 vehicle_category:'Категория автомобиля',
 recommendation:'Рекомендация',
 other:'Другой маркер'
};
const markerBadges = markers => {
 if(!markers || markers.length===0) return '<span class="muted">—</span>';
 return `<div class="marker-list">${markers.map(marker=>{
  const label=marker.label || marker.icon_filename || 'Маркер';
  const group=markerGroupLabels[marker.marker_group] || marker.marker_group || 'Другой маркер';
  return `<span class="marker-pill" title="${esc(marker.marker_group)} · ${esc(marker.icon_filename)}"><span>${esc(label)}</span><small>${esc(group)}</small></span>`;
 }).join('')}</div>`;
};
function rows(id, values, empty) { const el=document.getElementById(id); el.innerHTML=values.length?values.join(""):`<tr><td class="empty" colspan="13">${esc(empty)}</td></tr>`; }
let selectedCarId=null;
let selectedLotId=null;
let currentPage=1;
function renderCarCard(vehicle,lots){
 const panel=document.getElementById('car-card');
 if(!vehicle){panel.hidden=true;panel.innerHTML='';return;}
 const title=vehicle.displayed_title || [vehicle.make,vehicle.model].filter(Boolean).join(' ') || vehicle.source_car_id;
 const chassis=vehicle.chassis_number || (vehicle.chassis_number_fragment ? vehicle.chassis_number_fragment+(vehicle.chassis_number_masked?'… (частично скрыт)':'') : null);
 const fields=[
  ['ID автомобиля',vehicle.source_car_id],
  ['Марка',vehicle.make],
  ['Модель',vehicle.model],
  ['Номер кузова / VIN',chassis],
  ['Местонахождение',vehicle.location_text],
  ['Страна местонахождения',vehicle.location_country_name || vehicle.location_country_code],
  ['Последняя регистрация',vehicle.last_registration_country_name || vehicle.last_registration_country_code],
  ['Впервые добавлен',vehicle.first_seen_at],
  ['Последнее обновление',vehicle.last_seen_at]
 ];
 panel.innerHTML=`
  <div class="detail-top"><div><h2>${esc(title)}</h2><p class="hint">Карточка автомобиля · сохранённые сведения из архива</p></div><button type="button" class="close-card">Закрыть карточку</button></div>
  <div class="detail-grid">${fields.map(([label,value])=>`<div class="detail-item"><span class="k">${esc(label)}</span><span class="v">${esc(value)}</span></div>`).join('')}</div>
  <div class="detail-auctions"><h3>Участие в аукционах (${lots.length})</h3><table><thead><tr><th>ID аукциона</th><th>Аукцион</th><th>Страна</th><th>Тип</th><th>Статус</th><th>Тип продажи</th><th>Маркеры аукциона</th><th>Пробег, км</th><th>Первое появление</th><th>Последнее появление</th></tr></thead><tbody>${lots.map(lot=>`<tr>${cell(lot.auction_id)}${cell(lot.source_auction_name)}${cell(lot.source_auction_country_name)}${cell(lot.auction_type_label)}${cell(lot.auction_status_label)}${cell(lot.sale_type)}<td>${markerBadges(lot.markers)}</td>${cell(lot.mileage_km)}${cell(lot.first_seen_at)}${cell(lot.last_seen_at)}</tr>`).join('') || '<tr><td colspan="10" class="empty">Аукционы не найдены</td></tr>'}</tbody></table></div>
  <p class="hint">История ставок и цен этого автомобиля отображается в таблицах ниже.</p>`;
 panel.hidden=false;
 panel.querySelector('.close-card').addEventListener('click',()=>{
  selectedCarId=null;
  refresh();
 });
}
function renderLotCard(lot,vehicle,otherLots,media,pdfFields,pdfDamages,prices){
 const panel=document.getElementById('car-card');
 if(!lot){panel.hidden=true;panel.innerHTML='';return;}
 const title=vehicle?.displayed_title || [vehicle?.make,vehicle?.model].filter(Boolean).join(' ') || lot.title || vehicle?.source_car_id || 'Лот';
 const money=value=>value==null?'Не наблюдалась':'€ '+String(value);
 const detailFields=[
  ['Аукцион',lot.source_auction_name || lot.auction_id],
  ['ID аукциона',lot.auction_id],
  ['Номер лота',lot.displayed_lot_reference],
  ['Тип продажи',lot.sale_type],
  ['Пробег',lot.mileage_km==null?null:String(lot.mileage_km)+' км'],
  ['VIN / номер кузова',vehicle?.chassis_number || vehicle?.chassis_number_fragment],
  ['Состояние VIN',vehicle?.chassis_number?'Полное значение сохранено как получено':vehicle?.chassis_number_fragment?(vehicle.chassis_number_masked?'Сохранён только маскированный фрагмент':'Сохранён фрагмент; полнота не подтверждена'):'Не сохранён'],
  ['Местонахождение',vehicle?.location_text],
  ['Последняя регистрация',vehicle?.last_registration_country_name || vehicle?.last_registration_country_code]
 ];
 const photoItems=(media||[]).filter(item=>item.media_type==='photo');
 const reportItems=(media||[]).filter(item=>item.media_type==='report');
 const gallery=photoItems.length?'<div class="lot-gallery">'+photoItems.map((photo,index)=>{
  const imageUrl=photo.preview_source_url || photo.source_link_url;
  const image=imageUrl?'<img src="'+esc(imageUrl)+'" alt="Фото '+(index+1)+'" loading="lazy" referrerpolicy="no-referrer">':'<div class="lot-empty">Превью недоступно</div>';
  const link=photo.source_link_url?'<a href="'+esc(photo.source_link_url)+'" target="_blank" rel="noopener noreferrer">Оригинал</a>':'<span>Оригинал недоступен</span>';
  const archive=photo.archived?'<span class="pill good">В приватном архиве</span>':'<span class="pill">Только ссылка источника</span>';
  return '<div class="lot-photo">'+image+'<div class="photo-meta"><span>Фото '+(index+1)+'</span>'+link+'</div><div class="photo-meta">'+archive+'</div></div>';
 }).join('')+'</div>':'<div class="lot-empty">Для этого лота фотографии пока не найдены</div>';
 const primary=prices?.highest_offer;
 const primaryValue=primary?.amount==null?'Нет сохранённого значения':money(primary.amount);
 const secondary=[
  ['Отображаемая цена аукциона',prices?.displayed_auction_price],
  ['Buy Now / фиксированная цена',prices?.fixed_price],
  ['Минимальная цена продавца',prices?.minimum_price],
  ['Ориентировочная цена продавца',prices?.indicative_price]
 ];
 const reports=reportItems.length?'<div class="lot-report-list">'+reportItems.map((report,index)=>{
  const link=report.source_link_url?'<a class="report-link" href="'+esc(report.source_link_url)+'" target="_blank" rel="noopener noreferrer">Открыть PDF</a>':'<span class="muted">Ссылка источника недоступна</span>';
  const label=report.label || 'Inspection report '+(index+1);
  return '<div class="lot-report"><div><strong>'+esc(label)+'</strong><div class="lot-note">Статус архива: '+esc(report.download_status || 'неизвестен')+'</div></div>'+link+'</div>';
 }).join('')+'</div>':'<div class="lot-empty">Ссылки на PDF-отчёты для этого лота не найдены</div>';
 const observations=pdfFields?.length?'<div class="wrap"><table><thead><tr><th>Поле</th><th>Значение источника</th><th>Состояние</th><th>Страница</th><th>Метка отчёта</th><th>Парсер</th></tr></thead><tbody>'+pdfFields.map(field=>'<tr>'+cell(field.field_key)+cell(field.raw_text)+cell(field.state)+cell(field.page_number)+cell(field.source_label)+cell(field.parser_name+' '+field.parser_version)+'</tr>').join('')+'</tbody></table></div>':'<p class="lot-note">Извлечённые поля отчёта пока не сохранены. Сам PDF можно открыть по ссылке выше.</p>';
 const damage=pdfDamages?.length?'<div class="wrap"><table><thead><tr><th>№</th><th>Категория</th><th>Компонент</th><th>Описание из отчёта</th><th>Страница</th><th>Фото в отчёте</th></tr></thead><tbody>'+pdfDamages.map(item=>'<tr>'+cell(item.item_number)+cell(item.category)+cell(item.component)+cell(item.damage_description)+cell(item.page_number)+cell(item.photo_page_number==null?null:'стр. '+item.photo_page_number+', фото '+item.photo_index)+'</tr>').join('')+'</tbody></table></div>':'<p class="lot-note">Сведения о повреждениях из PDF не извлекались или не сохранены.</p>';
 const otherHistory=otherLots?.length?'<div class="wrap"><table><thead><tr><th></th><th>Аукцион</th><th>Тип продажи</th><th>Пробег</th><th>Последний API highest_offer</th><th>Проверка API</th><th>Последнее появление</th></tr></thead><tbody>'+otherLots.map(other=>'<tr><td><button type="button" class="open-car" data-lot-id="'+esc(other.lot_id)+'">Открыть лот</button></td>'+cell(other.source_auction_name || other.auction_id)+cell(other.sale_type)+cell(other.mileage_km)+moneyCell(other.highest_offer)+cell(other.last_api_checked_at)+cell(other.last_seen_at)+'</tr>').join('')+'</tbody></table></div>':'<p class="lot-note">Других аукционных лотов этой машины в архиве нет.</p>';
 panel.innerHTML=
  '<div class="detail-top"><div><h2>'+esc(title)+'</h2><p class="hint">Лот '+esc(lot.lot_id)+' · ID автомобиля '+esc(vehicle?.source_car_id || lot.car_id)+'</p></div><div class="lot-actions"><button type="button" class="close-card">К списку лотов</button></div></div>'+
  '<div class="lot-subsection"><h3>Фотографии</h3>'+gallery+'<p class="lot-note">Превью с исходного сайта отображаются только при наличии доступной ссылки. Файлы, уже сохранённые в приватном R2, не выдаются напрямую: локальный защищённый просмотр будет подключён отдельно после разрешения на R2-чтение.</p></div>'+
  '<div class="lot-subsection"><h3>Основная цена</h3><div class="lot-price-primary"><div class="k">API highest_offer · значение из источника, не подтверждение публичной ставки</div><div class="v">'+esc(primaryValue)+'</div><div class="lot-note">Источник: '+esc(primary?.source || 'нет наблюдения')+' · последнее изменение значения: '+esc(primary?.observed_at || 'нет наблюдения')+' · последняя проверка API: '+esc(lot.last_api_checked_at || 'неизвестно')+'</div></div>'+
   '<div class="lot-prices">'+secondary.map(([label,price])=>'<div class="detail-item"><span class="k">'+esc(label)+'</span><span class="v">'+esc(price?.amount==null?'Не наблюдалась':money(price.amount))+'</span><p class="lot-note">Источник: '+esc(price?.source || '—')+' · наблюдение: '+esc(price?.observed_at || '—')+(price?.source_field?' · поле: '+esc(price.source_field):'')+'</p></div>').join('')+'</div></div>'+
  '<div class="lot-subsection"><h3>Автомобиль и лот</h3><div class="detail-grid">'+detailFields.map(([label,value])=>'<div class="detail-item"><span class="k">'+esc(label)+'</span><span class="v">'+esc(value)+'</span></div>').join('')+'</div><p class="lot-note">VIN показывается только в том виде, в котором он сохранён. Отсутствующие символы не восстанавливаются.</p></div>'+
  '<div class="lot-subsection"><h3>PDF-отчёты</h3>'+reports+'</div>'+
  '<div class="lot-subsection"><h3>Сохранённые сведения из отчётов</h3>'+observations+damage+'</div>'+
  '<div class="lot-subsection"><h3>Другие аукционные лоты этой машины</h3>'+otherHistory+'</div>';
 panel.hidden=false;
 panel.querySelector('.close-card').addEventListener('click',()=>{
  selectedLotId=null;
  selectedCarId=null;
  refresh();
 });
}
async function refresh(){
 try{
  const query=document.getElementById('lot-search').value.trim();
  const selectedMake=document.getElementById('make-filter').value.trim();
  const params=new URLSearchParams();
  if(selectedLotId) params.set('lot_id',selectedLotId);
  else if(selectedCarId) params.set('car_id',selectedCarId);
  else if(query) params.set('q',query);
  if(selectedMake) params.set('make',selectedMake);
  params.set('page',String(currentPage));
  const queryString=params.toString();
  const apiUrl='/api/data'+(queryString?'?'+queryString:'');
  const response=await fetch(apiUrl,{cache:'no-store'}); if(!response.ok) throw new Error(await response.text());
  const d=await response.json();
  document.getElementById('dbpath').textContent=d.database;
  document.getElementById('updated').textContent='Обновлено: '+new Date().toLocaleTimeString();
  const makeFilter=document.getElementById('make-filter');
  const makeOptions='<option value="">Все марки</option>'+(d.available_makes||[]).map(make=>'<option value="'+esc(make)+'">'+esc(make)+'</option>').join('');
  if(makeFilter.dataset.options!==makeOptions){makeFilter.innerHTML=makeOptions;makeFilter.dataset.options=makeOptions;}
  makeFilter.value=d.selected_make || '';
  const metrics=[['Аукционов',d.counts.auctions],['Автомобилей',d.counts.vehicles],['Лотов',d.counts.auction_lots],['Наблюдений highest_offer (API)',d.counts.price_observations],['Цен продавца',d.counts.price_reference_observations],['Отображаемых/фиксированных цен',d.counts.lot_price_observations],['Событий',d.counts.auction_events]];
  document.getElementById('metrics').innerHTML=metrics.map(x=>`<div class="card"><div class="k">${esc(x[0])}</div><div class="v">${esc(x[1])}</div></div>`).join('');
  if(selectedLotId){
   renderLotCard(d.selected_lot,d.selected_vehicle,d.other_lots,d.media,d.pdf_fields,d.pdf_damages,d.lot_prices_summary);
  }else{
   renderCarCard(d.selected_vehicle,d.lots);
  }
  const pagination=d.pagination;
  if(!selectedCarId && !selectedLotId) currentPage=pagination.page;
  const paginationPanel=document.getElementById('lot-pagination');
  if(selectedCarId || selectedLotId){
   document.getElementById('search-status').textContent=selectedLotId?`Лот: ${d.selected_lot?esc(d.selected_lot.lot_id):'не найден'}`:`Карточка: ${d.selected_vehicle?esc(d.selected_vehicle.source_car_id):'не найдена'}`;
   paginationPanel.hidden=true;
  }else{
   const first= pagination.total_lots ? (pagination.page-1)*pagination.page_size+1 : 0;
   const last=Math.min(pagination.page*pagination.page_size,pagination.total_lots);
   document.getElementById('search-status').textContent=`Показаны ${first}–${last} из ${pagination.total_lots}${query?' найденных':' лотов'}`;
   document.getElementById('page-info').textContent=`Страница ${pagination.page} из ${pagination.total_pages}`;
   document.getElementById('page-prev').disabled=!pagination.has_previous;
   document.getElementById('page-next').disabled=!pagination.has_next;
   paginationPanel.hidden=pagination.total_pages<=1;
  }
  rows('lots',d.lots.map(x=>`<tr><td><button type="button" class="open-car" data-lot-id="${esc(x.lot_id)}">Открыть лот</button></td>${cell(x.auction_id)}${cell(x.car_id)}${cell(x.title)}${cell(x.sale_type)}<td>${markerBadges(x.markers)}</td>${cell(x.mileage_km)}${cell(x.chassis)}${moneyCell(x.highest_offer)}${moneyCell(x.minimum_price)}${moneyCell(x.indicative_price)}${cell(x.last_api_checked_at)}${cell(x.first_seen_at)}</tr>`),query?'По этому запросу ничего не найдено':'Лотов пока нет');
  rows('bids',d.bids.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${moneyCell(x.highest_offer)}${cell(x.source)}</tr>`),'Наблюдений API highest_offer пока нет');
  rows('refs',d.references.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${moneyCell(x.minimum_price)}${moneyCell(x.indicative_price)}${cell(x.source)}</tr>`),'Наблюдений цен продавца пока нет');
  rows('lotprices',d.lot_prices.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${cell(x.price_type)}${moneyCell(x.amount)}${cell(x.currency)}${cell(x.source_field)}</tr>`),'Отображаемые и фиксированные цены пока не записаны');
  rows('events',d.events.map(x=>`<tr>${cell(x.observed_at)}${cell(x.auction_id)}${cell(x.car_id)}${cell(x.auction_phase)}${cell(x.bid_status)}${cell(x.auction_end_at)}${cell(x.remaining_seconds)}<td>${x.final_call_active===1?'<span class="pill warn">Да</span>':x.final_call_active===0?'<span class="pill">Нет</span>':'—'}</td></tr>`),'Событий пока нет');
 }catch(e){document.getElementById('updated').textContent='Ошибка чтения базы: '+e.message;}
}
const makeFilter=document.getElementById('make-filter');
makeFilter.addEventListener('change',()=>{
 selectedCarId=null;
 selectedLotId=null;
 currentPage=1;
 refresh();
});
const searchInput=document.getElementById('lot-search');
let searchTimer;
searchInput.addEventListener('input',()=>{
 selectedCarId=null;
 selectedLotId=null;
 currentPage=1;
 clearTimeout(searchTimer);
 searchTimer=setTimeout(refresh,250);
});
document.getElementById('lots').addEventListener('click',event=>{
 const button=event.target.closest('button[data-lot-id]');
 if(!button)return;
 selectedCarId=null;
 selectedLotId=button.dataset.lotId;
 refresh();
});
document.getElementById('car-card').addEventListener('click',event=>{
 const button=event.target.closest('button[data-lot-id]');
 if(!button)return;
 selectedCarId=null;
 selectedLotId=button.dataset.lotId;
 refresh();
});
document.getElementById('page-prev').addEventListener('click',()=>{
 if(currentPage>1){currentPage-=1;refresh();}
});
document.getElementById('page-next').addEventListener('click',()=>{
 currentPage+=1;refresh();
});
refresh();setInterval(refresh,5000);
</script>
</body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    database_path: Path

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("__APP_VERSION__", APP_VERSION)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        request_url = urlsplit(self.path)
        if request_url.path == "/api/data":
            try:
                query_values = parse_qs(request_url.query, keep_blank_values=True)
                search_query = query_values.get("q", [""])[0]
                selected_car_id = query_values.get("car_id", [""])[0]
                selected_lot_raw = query_values.get("lot_id", [""])[0]
                try:
                    selected_lot_id = int(selected_lot_raw) if selected_lot_raw else None
                except ValueError:
                    selected_lot_id = None
                if selected_lot_id is not None and selected_lot_id <= 0:
                    selected_lot_id = None
                selected_make = query_values.get("make", [""])[0]
                try:
                    page = int(query_values.get("page", ["1"])[0])
                except ValueError:
                    page = 1
                payload = self._load_data(
                    search_query=search_query,
                    selected_car_id=selected_car_id,
                    selected_lot_id=selected_lot_id,
                    page=page,
                    selected_make=selected_make,
                )
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

    def _load_data(
        self,
        search_query: str | None = None,
        selected_car_id: str | None = None,
        selected_lot_id: int | None = None,
        page: int = 1,
        selected_make: str | None = None,
    ) -> dict[str, Any]:
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
            selected_car_id = " ".join((selected_car_id or "").split())[:100]
            if (
                isinstance(selected_lot_id, bool)
                or (selected_lot_id is not None and not isinstance(selected_lot_id, int))
                or (selected_lot_id is not None and selected_lot_id <= 0)
            ):
                selected_lot_id = None
            normalized_make = " ".join((selected_make or "").split())[:100]
            available_makes = [
                row["make"]
                for row in connection.execute(
                    """
                    SELECT MIN(TRIM(make)) AS make
                    FROM vehicles
                    WHERE make IS NOT NULL AND TRIM(make) <> ''
                    GROUP BY LOWER(TRIM(make))
                    ORDER BY LOWER(make), make
                    """
                )
            ]
            lots_sql = """
                SELECT al.id AS lot_id,
                       a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id,
                       COALESCE(v.displayed_title, trim(COALESCE(v.make,'') || ' ' || COALESCE(v.model,''))) AS title,
                       al.sale_type,
                       al.displayed_lot_reference,
                       al.mileage_km,
                        al.last_api_checked_at,
                       CASE
                         WHEN v.chassis_number IS NOT NULL THEN v.chassis_number
                         WHEN v.chassis_number_fragment IS NOT NULL THEN v.chassis_number_fragment ||
                              CASE WHEN COALESCE(v.chassis_number_masked,0)=1 THEN '… (скрыт)' ELSE '' END
                         ELSE NULL
                       END AS chassis,
                       (SELECT po.highest_offer FROM price_observations po
                        WHERE po.auction_lot_id=al.id ORDER BY po.observed_at DESC, po.id DESC LIMIT 1) AS highest_offer,
                       (SELECT po.currency FROM price_observations po
                        WHERE po.auction_lot_id=al.id ORDER BY po.observed_at DESC, po.id DESC LIMIT 1) AS highest_offer_currency,
                       (SELECT po.observed_at FROM price_observations po
                        WHERE po.auction_lot_id=al.id ORDER BY po.observed_at DESC, po.id DESC LIMIT 1) AS highest_offer_observed_at,
                        (SELECT pr.minimum_price FROM price_reference_observations pr
                        WHERE pr.auction_lot_id=al.id ORDER BY pr.observed_at DESC, pr.id DESC LIMIT 1) AS minimum_price,
                       (SELECT pr.currency FROM price_reference_observations pr
                        WHERE pr.auction_lot_id=al.id ORDER BY pr.observed_at DESC, pr.id DESC LIMIT 1) AS reference_currency,
                        (SELECT pr.indicative_price FROM price_reference_observations pr
                        WHERE pr.auction_lot_id=al.id ORDER BY pr.observed_at DESC, pr.id DESC LIMIT 1) AS indicative_price,
                       al.first_seen_at,
                       al.last_seen_at,
                       a.source_auction_name,
                       a.source_auction_country_name,
                       a.auction_type_label,
                       a.auction_status_label
                FROM auction_lots al
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
            """
            lot_conditions: list[str] = []
            filter_values: list[str] = []
            if selected_lot_id is not None:
                lot_conditions.append("al.id = ?")
                filter_values.append(selected_lot_id)
            elif selected_car_id:
                lot_conditions.append("v.source_car_id = ?")
                filter_values.append(selected_car_id)
            else:
                if normalized_search:
                    lot_conditions.append(
                        """
                        (
                            lower(COALESCE(v.displayed_title, '') || ' ' ||
                                  COALESCE(v.make, '') || ' ' || COALESCE(v.model, ''))
                                LIKE lower(?)
                            OR lower(v.source_car_id) LIKE lower(?)
                            OR lower(COALESCE(v.chassis_number, '')) LIKE lower(?)
                            OR lower(COALESCE(v.chassis_number_fragment, '')) LIKE lower(?)
                        )
                        """
                    )
                    pattern = f"%{normalized_search}%"
                    filter_values.extend([pattern, pattern, pattern, pattern])
                if normalized_make:
                    lot_conditions.append(
                        "lower(trim(COALESCE(v.make, ''))) = lower(?)"
                    )
                    filter_values.append(normalized_make)

            lots_filter = (
                " WHERE " + " AND ".join(lot_conditions) if lot_conditions else ""
            )
            filter_parameters = tuple(filter_values)
            parameters = filter_parameters

            total_lots = int(connection.execute(
                """
                SELECT COUNT(*)
                FROM auction_lots al
                JOIN vehicles v ON v.id=al.vehicle_id
                """ + lots_filter,
                parameters,
            ).fetchone()[0])
            total_pages = max(1, (total_lots + LOT_PAGE_SIZE - 1) // LOT_PAGE_SIZE)

            requested_page = max(1, int(page))
            if selected_car_id or selected_lot_id is not None:
                current_page = 1
                lots_sql += lots_filter + " ORDER BY al.last_seen_at DESC, al.id DESC"
            else:
                current_page = min(requested_page, total_pages)
                offset = (current_page - 1) * LOT_PAGE_SIZE
                lots_sql += (
                    lots_filter
                    + " ORDER BY al.last_seen_at DESC, al.id DESC LIMIT ? OFFSET ?"
                )
                parameters = (*parameters, LOT_PAGE_SIZE, offset)

            lots = [dict(row) for row in connection.execute(lots_sql, parameters)]
            markers_by_auction: dict[str, list[dict[str, Any]]] = {}
            auction_ids = sorted({
                lot["auction_id"] for lot in lots if lot["auction_id"]
            })
            if "auction_markers" in tables:
                for offset in range(0, len(auction_ids), 900):
                    auction_chunk = auction_ids[offset : offset + 900]
                    placeholders = ", ".join("?" for _ in auction_chunk)
                    marker_rows = connection.execute(
                        f"""
                        SELECT a.exleasingcar_auction_id AS auction_id,
                               m.icon_filename, m.marker_group, m.label
                        FROM auction_markers AS m
                        JOIN auctions AS a ON a.id = m.auction_id
                        WHERE m.is_present = 1
                          AND a.exleasingcar_auction_id IN ({placeholders})
                        ORDER BY m.marker_group, m.icon_filename
                        """,
                        auction_chunk,
                    )
                    for marker_row in marker_rows:
                        marker = {
                            "icon_filename": marker_row["icon_filename"],
                            "marker_group": marker_row["marker_group"],
                            "label": marker_row["label"],
                        }
                        markers_by_auction.setdefault(
                            marker_row["auction_id"], []
                        ).append(marker)
            for lot in lots:
                lot["markers"] = markers_by_auction.get(lot["auction_id"], [])

            history_filter = lots_filter
            history_parameters = filter_parameters
            history_limit = (
                ""
                if normalized_search or normalized_make or selected_car_id or selected_lot_id is not None
                else " LIMIT 100"
            )

            vehicle = None
            if selected_lot_id is not None:
                vehicle_row = connection.execute(
                    """
                    SELECT v.source_car_id, v.displayed_title, v.make, v.model,
                           v.chassis_number, v.chassis_number_fragment,
                           v.chassis_number_masked, v.location_text,
                           v.location_country_code, v.location_country_name,
                           v.last_registration_country_code,
                           v.last_registration_country_name,
                           v.first_seen_at, v.last_seen_at
                    FROM vehicles AS v
                    JOIN auction_lots AS al ON al.vehicle_id = v.id
                    WHERE al.id = ?
                    """,
                    (selected_lot_id,),
                ).fetchone()
                vehicle = dict(vehicle_row) if vehicle_row is not None else None
            elif selected_car_id:
                vehicle_row = connection.execute(
                    """
                    SELECT source_car_id, displayed_title, make, model,
                           chassis_number, chassis_number_fragment,
                           chassis_number_masked, location_text,
                           location_country_code, location_country_name,
                           last_registration_country_code,
                           last_registration_country_name,
                           first_seen_at, last_seen_at
                    FROM vehicles
                    WHERE source_car_id = ?
                    """,
                    (selected_car_id,),
                ).fetchone()
                vehicle = dict(vehicle_row) if vehicle_row is not None else None

            bids = [dict(row) for row in connection.execute(
                f"""
                SELECT po.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, po.highest_offer, po.currency, po.source
                FROM price_observations po
                JOIN auction_lots al ON al.id=po.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                {history_filter}
                ORDER BY po.observed_at DESC, po.id DESC{history_limit}
                """,
                history_parameters,
            )]
            references = [dict(row) for row in connection.execute(
                f"""
                SELECT pr.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, pr.minimum_price,
                       pr.indicative_price, pr.currency, pr.source
                FROM price_reference_observations pr
                JOIN auction_lots al ON al.id=pr.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                {history_filter}
                ORDER BY pr.observed_at DESC, pr.id DESC{history_limit}
                """,
                history_parameters,
            )]
            lot_prices = [dict(row) for row in connection.execute(
                f"""
                SELECT lp.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, lp.price_type, lp.amount,
                       lp.currency, lp.source_field, lp.source
                FROM lot_price_observations lp
                JOIN auction_lots al ON al.id=lp.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                {history_filter}
                ORDER BY lp.observed_at DESC, lp.id DESC{history_limit}
                """,
                history_parameters,
            )]
            events = [dict(row) for row in connection.execute(
                f"""
                SELECT ae.observed_at, a.exleasingcar_auction_id AS auction_id,
                       v.source_car_id AS car_id, ae.auction_phase, ae.bid_status,
                       ae.auction_end_at, ae.remaining_seconds, ae.final_call_active
                FROM auction_events ae
                JOIN auction_lots al ON al.id=ae.auction_lot_id
                JOIN vehicles v ON v.id=al.vehicle_id
                LEFT JOIN auctions a ON a.id=al.auction_id
                {history_filter}
                ORDER BY ae.observed_at DESC, ae.id DESC{history_limit}
                """,
                history_parameters,
            )]
            lot_detail = None
            other_lots: list[dict[str, Any]] = []
            media: list[dict[str, Any]] = []
            pdf_fields: list[dict[str, Any]] = []
            pdf_damages: list[dict[str, Any]] = []
            lot_prices_summary: dict[str, Any] = {}

            if selected_lot_id is not None and lots:
                selected_lot_row = lots[0]
                lot_detail = dict(selected_lot_row)
                highest_offer = bids[0] if bids else None
                latest_displayed = next(
                    (
                        row
                        for row in lot_prices
                        if row["price_type"] == "displayed_auction_price"
                    ),
                    None,
                )
                latest_fixed = next(
                    (
                        row
                        for row in lot_prices
                        if row["price_type"] == "fixed_price"
                    ),
                    None,
                )
                latest_minimum = next(
                    (row for row in references if row["minimum_price"] is not None),
                    None,
                )
                latest_indicative = next(
                    (row for row in references if row["indicative_price"] is not None),
                    None,
                )

                def _price_summary(
                    amount: object,
                    currency: object,
                    price_source: object,
                    observed_at: object,
                    source_field: object = None,
                ) -> dict[str, object]:
                    return {
                        "amount": amount,
                        "currency": currency,
                        "source": price_source,
                        "observed_at": observed_at,
                        "source_field": source_field,
                    }

                lot_prices_summary = {
                    "highest_offer": _price_summary(
                        highest_offer["highest_offer"] if highest_offer else None,
                        highest_offer["currency"] if highest_offer else None,
                        highest_offer["source"] if highest_offer else None,
                        highest_offer["observed_at"] if highest_offer else None,
                    ),
                    "displayed_auction_price": _price_summary(
                        latest_displayed["amount"] if latest_displayed else None,
                        latest_displayed["currency"] if latest_displayed else None,
                        latest_displayed["source"] if latest_displayed else None,
                        latest_displayed["observed_at"] if latest_displayed else None,
                        latest_displayed["source_field"] if latest_displayed else None,
                    ),
                    "fixed_price": _price_summary(
                        latest_fixed["amount"] if latest_fixed else None,
                        latest_fixed["currency"] if latest_fixed else None,
                        latest_fixed["source"] if latest_fixed else None,
                        latest_fixed["observed_at"] if latest_fixed else None,
                        latest_fixed["source_field"] if latest_fixed else None,
                    ),
                    "minimum_price": _price_summary(
                        latest_minimum["minimum_price"] if latest_minimum else None,
                        latest_minimum["currency"] if latest_minimum else None,
                        latest_minimum["source"] if latest_minimum else None,
                        latest_minimum["observed_at"] if latest_minimum else None,
                    ),
                    "indicative_price": _price_summary(
                        latest_indicative["indicative_price"] if latest_indicative else None,
                        latest_indicative["currency"] if latest_indicative else None,
                        latest_indicative["source"] if latest_indicative else None,
                        latest_indicative["observed_at"] if latest_indicative else None,
                    ),
                }

                if vehicle is not None:
                    vehicle_id_row = connection.execute(
                        "SELECT id FROM vehicles WHERE source_car_id = ?",
                        (vehicle["source_car_id"],),
                    ).fetchone()
                    if vehicle_id_row is not None:
                        vehicle_id = int(vehicle_id_row["id"])
                        other_lots = [
                            dict(row)
                            for row in connection.execute(
                                """
                                SELECT al.id AS lot_id,
                                       a.exleasingcar_auction_id AS auction_id,
                                       a.source_auction_name,
                                       al.sale_type,
                                       al.mileage_km,
                                       al.last_api_checked_at,
                                       al.first_seen_at,
                                       al.last_seen_at,
                                       (SELECT po.highest_offer
                                        FROM price_observations po
                                        WHERE po.auction_lot_id = al.id
                                        ORDER BY po.observed_at DESC, po.id DESC
                                        LIMIT 1) AS highest_offer
                                FROM auction_lots al
                                LEFT JOIN auctions a ON a.id = al.auction_id
                                WHERE al.vehicle_id = ? AND al.id <> ?
                                ORDER BY al.last_seen_at DESC, al.id DESC
                                """,
                                (vehicle_id, selected_lot_id),
                            )
                        ]

                if {"lot_media_sources", "media_assets"}.issubset(tables):
                    derivative_columns = """
                        NULL AS card_preview_status,
                        NULL AS card_preview_width,
                        NULL AS card_preview_height,
                        NULL AS gallery_preview_status
                    """
                    derivative_join = ""
                    if "media_derivatives" in tables:
                        derivative_columns = """
                            (SELECT d.status FROM media_derivatives d
                             WHERE d.source_asset_id = asset.id AND d.variant = 'card'
                             ORDER BY d.id DESC LIMIT 1) AS card_preview_status,
                            (SELECT d.width FROM media_derivatives d
                             WHERE d.source_asset_id = asset.id AND d.variant = 'card'
                             ORDER BY d.id DESC LIMIT 1) AS card_preview_width,
                            (SELECT d.height FROM media_derivatives d
                             WHERE d.source_asset_id = asset.id AND d.variant = 'card'
                             ORDER BY d.id DESC LIMIT 1) AS card_preview_height,
                            (SELECT d.status FROM media_derivatives d
                             WHERE d.source_asset_id = asset.id AND d.variant = 'gallery'
                             ORDER BY d.id DESC LIMIT 1) AS gallery_preview_status
                        """
                    media_rows = connection.execute(
                        f"""
                        SELECT m.media_type, m.source_url, m.preview_url, m.label,
                               m.source_order, m.download_status,
                               CASE WHEN m.current_asset_id IS NULL THEN 0 ELSE 1 END
                                   AS archived,
                               asset.mime_type AS archived_mime_type,
                               asset.byte_size AS archived_byte_size,
                               {derivative_columns}
                        FROM lot_media_sources AS m
                        LEFT JOIN media_assets AS asset
                          ON asset.id = m.current_asset_id
                        {derivative_join}
                        WHERE m.auction_lot_id = ?
                        ORDER BY m.media_type, m.source_order, m.id
                        """,
                        (selected_lot_id,),
                    )
                    for media_row in media_rows:
                        item = dict(media_row)
                        # Never return R2 object keys or signed URLs to the browser.
                        item["source_link_url"] = _safe_exleasingcar_url(
                            item.pop("source_url", None)
                        )
                        item["preview_source_url"] = _safe_exleasingcar_url(
                            item.pop("preview_url", None)
                        )
                        media.append(item)

                if "pdf_field_observations" in tables:
                    pdf_fields = [
                        dict(row)
                        for row in connection.execute(
                            """
                            SELECT field_key, source_label, raw_text, normalized_text,
                                   state, page_number, report_sha256,
                                   parser_name, parser_version, extracted_at
                            FROM pdf_field_observations
                            WHERE auction_lot_id = ?
                            ORDER BY page_number, id
                            """,
                            (selected_lot_id,),
                        )
                    ]
                if "pdf_damage_observations" in tables:
                    pdf_damages = [
                        dict(row)
                        for row in connection.execute(
                            """
                            SELECT item_number, category, component,
                                   damage_description, page_number,
                                   photo_page_number, photo_index, photo_caption,
                                   report_sha256, parser_name, parser_version,
                                   extracted_at
                            FROM pdf_damage_observations
                            WHERE auction_lot_id = ?
                            ORDER BY page_number, item_number, id
                            """,
                            (selected_lot_id,),
                        )
                    ]

        return {
            "database": str(self.database_path),
            "schema_version": version,
            "counts": counts,
            "selected_vehicle": vehicle,
            "selected_lot": lot_detail,
            "other_lots": other_lots,
            "media": media,
            "pdf_fields": pdf_fields,
            "pdf_damages": pdf_damages,
            "lot_prices_summary": lot_prices_summary,
            "available_makes": available_makes,
            "selected_make": normalized_make,
            "lots": lots,
            "pagination": {
                "page": current_page,
                "page_size": LOT_PAGE_SIZE,
                "total_lots": total_lots,
                "total_pages": total_pages,
                "has_previous": not bool(selected_car_id) and selected_lot_id is None and current_page > 1,
                "has_next": not bool(selected_car_id) and selected_lot_id is None and current_page < total_pages,
            },
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
