"""API -> validated observations -> persistent state -> branded Plotly HTML.

Run: python build.py (live APIs); python build.py --demo (labelled fixtures).
No API credentials are serialized to HTML or the state file.
"""
import argparse
import base64
from datetime import date, datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import sys
import zlib
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
TR = ZoneInfo("Europe/Istanbul")
PALETTE = ["#143d69", "#c49338", "#319185"]


class DataError(Exception):
    pass


def session():
    client = requests.Session()
    client.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"], respect_retry_after_header=False)))
    return client


def get_json(client, url, **kwargs):
    # Do not leak requests exception strings: FRED uses a query-string API key.
    try:
        response = client.get(url, timeout=(10, 40), allow_redirects=False, **kwargs)
        if response.status_code != 200:
            raise DataError(f"API HTTP {response.status_code}")
        return response.json()
    except (requests.RequestException, ValueError):
        raise DataError("API erişimi veya JSON yanıtı başarısız") from None


def validate(rows, today):
    if not isinstance(rows, list):
        raise DataError("Gözlemler liste biçiminde değil")
    clean = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("Gözlem nesne biçiminde değil")
        raw = row.get("value")
        if raw in (None, "", ".", "-", "NA"):
            continue  # provider-defined missing value; never replace with zero
        try:
            d = date.fromisoformat(row["date"])
            v = float(raw)
        except (ValueError, TypeError, KeyError):
            raise DataError("Geçersiz tarih veya değer") from None
        if d > today or not math.isfinite(v):
            raise DataError("Gelecek tarihli veya sonlu olmayan gözlem")
        key = d.isoformat()
        if key in clean and clean[key] != v:
            raise DataError("Çelişkili mükerrer tarih")
        clean[key] = v
    if not clean:
        raise DataError("Kullanılabilir gözlem bulunamadı")
    return [{"date": d, "value": clean[d]} for d in sorted(clean)]


def fetch_fred(client, spec, start, today):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise DataError("FRED_API_KEY tanımlanmamış")
    rows, offset = [], 0
    while True:
        payload = get_json(client, "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": spec["code"], "api_key": key, "file_type": "json",
                    "observation_start": start, "observation_end": today.isoformat(),
                    "sort_order": "asc", "limit": 100000, "offset": offset})
        if not isinstance(payload, dict) or "observations" not in payload:
            raise DataError("FRED gözlem şeması değişmiş")
        batch = payload["observations"]
        if not isinstance(batch, list):
            raise DataError("FRED gözlemleri liste biçiminde değil")
        rows.extend(batch)
        offset += len(batch)
        try:
            count = int(payload.get("count", len(rows)))
        except (ValueError, TypeError):
            raise DataError("FRED gözlem sayısı geçersiz") from None
        if offset >= count:
            break
        if not batch:
            raise DataError("FRED sayfalama yanıtı eksik")
    return validate(rows, today)


def fetch_evds_legacy(client, spec, start, today):
    # OFF by default. This is the documented EVDS2 contract, not a claimed EVDS3 adapter.
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        raise DataError("EVDS_API_KEY tanımlanmamış")
    if not re.fullmatch(r"[A-Za-z0-9.]+", spec["code"]):
        raise DataError("EVDS seri kodu geçersiz")
    begin = date.fromisoformat(start).strftime("%d-%m-%Y")
    end = today.strftime("%d-%m-%Y")
    url = ("https://evds2.tcmb.gov.tr/service/evds/series=" + spec["code"]
           + f"&startDate={begin}&endDate={end}&type=json")
    payload = get_json(client, url, headers={"key": key})
    if not isinstance(payload, dict) or "items" not in payload:
        raise DataError("EVDS2 şeması doğrulanamadı; EVDS3 uyarlaması gerekebilir")
    field = spec["code"].replace(".", "_")
    rows = []
    for item in payload["items"]:
        try:
            d = datetime.strptime(item["Tarih"], "%d-%m-%Y").date().isoformat()
            raw = item[field]
        except (KeyError, ValueError, TypeError):
            raise DataError("EVDS günlük seri şeması doğrulanamadı") from None
        rows.append({"date": d, "value": raw})
    return validate(rows, today)


def demo_rows(spec, today):
    # Synthetic data, deliberately labelled in every chart and the panel header.
    rows = []
    for n in range(180):
        d = today - timedelta(days=179-n)
        if d.weekday() >= 5:
            continue
        base = 3.5 if spec["id"] == "us2y" else 4.1
        rows.append({"date": d.isoformat(), "value": round(base + .3*math.sin(n/20), 3)})
    return rows


def watermark(fig, logo):
    if logo.is_file():
        uri = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode()
        fig.add_layout_image(source=uri, xref="paper", yref="paper", x=.5, y=.5,
            xanchor="center", yanchor="middle", sizex=.60, sizey=.60,
            sizing="contain", opacity=.15, layer="below")
    else:
        # Typographic placeholder, not an invented official BFRC logo.
        fig.add_annotation(text="BFRC", x=.5, y=.5, xref="paper", yref="paper",
            showarrow=False, opacity=.15, font={"size": 82, "color":"#143d69"})


def make_figure(chart, records, today, demo=False):
    needed = chart.get("spread") or [s["id"] for s in chart["series"]]
    if not all(i in records for i in needed):
        raise DataError("Grafiğin bazı serileri henüz yüklenemedi")
    fig = go.Figure()
    if "spread" in chart:
        a, b = [dict((r["date"], r["value"]) for r in records[i]["observations"]) for i in needed]
        dates = sorted(a.keys() & b.keys())  # exact common dates; no forward filling
        if not dates:
            raise DataError("Fark hesabı için ortak tarih yok")
        values = [(a[d]-b[d])*chart.get("multiplier", 1) for d in dates]
        fig.add_trace(go.Scatter(x=dates, y=values, name="10 yıl − 2 yıl",
            line={"color":PALETTE[0], "width":2}, connectgaps=False))
        fig.add_hline(y=0, line_color="#9aabb9", line_dash="dot")
        latest = dates[-1]
    else:
        endings = []
        for n, item in enumerate(chart["series"]):
            rows = records[item["id"]]["observations"]
            # Explicit gaps between observations avoid interpolating holidays visually.
            mapping = {r["date"]: r["value"] for r in rows}
            first, last = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
            dates = [(first+timedelta(days=k)).isoformat() for k in range((last-first).days+1)]
            fig.add_trace(go.Scatter(x=dates, y=[mapping.get(d) for d in dates],
                name=item["label"], line={"color":PALETTE[n % len(PALETTE)], "width":2},
                connectgaps=False))
            endings.append(rows[-1]["date"])
        latest = min(endings)
    last_fetch = min(records[i]["fetched_at"] for i in needed)
    status = " · DEMO / SENTETİK VERİ" if demo else ""
    if any(records[i].get("fallback") for i in needed):
        status += " · SON BAŞARILI VERİ (API GÜNCELLEMESİ BAŞARISIZ)"
    if any((today-date.fromisoformat(records[i]["observations"][-1]["date"])).days
           > records[i]["max_age_days"] for i in needed):
        status += " · VERİ TARİHİ EŞİĞİ AŞILDI"
    fig.update_layout(template="plotly_white", height=520, paper_bgcolor="white",
        plot_bgcolor="white", font={"family":"Arial, sans-serif", "color":"#19344b"},
        title={"text":escape(chart["title"]),"x":.03}, hovermode="x unified",
        margin={"l":65,"r":25,"t":110,"b":145},
        legend={"orientation":"h","y":1.10,"x":0},
        yaxis_title=chart["unit"],
        xaxis={"type":"date","rangeselector":{"buttons":[
            {"count":6,"label":"6 ay","step":"month","stepmode":"backward"},
            {"count":1,"label":"1 yıl","step":"year","stepmode":"backward"},
            {"label":"Tümü","step":"all"}]}})
    watermark(fig, ROOT / CONFIG["logo_path"])
    for text, shift in [
        ("Kaynak: " + chart["source"], -65),
        ("Grafik: Mehmet Faruk Bekir", -83),
        (f"Son gözlem (ortak/asgari tarih): {latest} · API kontrolü: {last_fetch}", -101),
        (status.strip(" ·"), -119)]:
        if text:
            fig.add_annotation(text=escape(text), x=0, y=0, yshift=shift, xref="paper", yref="paper",
                xanchor="left", showarrow=False, font={"size":10})
    return fig, status


CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def build(demo=False):
    today = datetime.now(TR).date()
    stamp = datetime.now(TR).isoformat(timespec="seconds")
    output = ROOT / ("demo" if demo else "dist")
    cache_file = ROOT / "state" / "cache.json"
    old = json.loads(cache_file.read_text()) if cache_file.exists() and not demo else {}
    records, saved, problems, successes = {}, {}, [], 0
    with session() as client:
        for spec in CONFIG["series"]:
            if not spec.get("enabled", True):
                continue
            identity = {"provider":spec["provider"],"code":spec["code"]}
            try:
                if demo:
                    rows = demo_rows(spec, today)
                else:
                    fetcher = {"fred":fetch_fred,"evds_legacy":fetch_evds_legacy}[spec["provider"]]
                    rows = fetcher(client, spec, CONFIG["start_date"], today)
                record = {**identity,"observations":rows,"fetched_at":stamp}
                saved[spec["id"]] = record
                successes += 1
            except (DataError, KeyError) as error:
                problems.append(f"{spec['id']}: {error}")
                previous = old.get(spec["id"])
                if not previous or any(previous.get(k) != v for k, v in identity.items()):
                    continue  # never use a cache belonging to a different series
                record = {**previous, "observations":validate(previous["observations"], today)}
                saved[spec["id"]] = record
                record = {**record,"fallback":True}
            records[spec["id"]] = {**record,"max_age_days":spec["max_age_days"]}
    if not records:
        raise DataError("Hiç veri yok; mevcut yayın korunmalı")
    output.mkdir(exist_ok=True)
    (output / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    cards, status_rows = [], []
    for chart in CONFIG["charts"]:
        try:
            fig, status = make_figure(chart, records, today, demo)
        except DataError:
            continue
        fragment = pio.to_html(fig, full_html=False, include_plotlyjs=False,
            div_id=chart["id"], config={"responsive":True,"displaylogo":False,
            "toImageButtonOptions":{"format":"png","filename":"bfrc-"+chart["id"],
                                    "width":1400,"height":700,"scale":2}})
        cards.append('<section class="card">'+fragment+'</section>')
        status_rows.append({"chart":chart["id"],"status":status})
    if not cards:
        raise DataError("Hiç grafik üretilemedi")
    banner = "DEMO — Tüm değerler sentetiktir; yayımlamayın." if demo else "API verileriyle otomatik güncellenen grafikler"
    warnings = "".join("<li>"+escape(p)+"</li>" for p in problems)
    document = '''<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BFRC | Güncel Grafikler</title><script src="plotly.min.js"></script>
<style>body{margin:0;background:#f2f5f8;color:#19344b;font:15px Arial,sans-serif}
main{max-width:1250px;margin:auto;padding:20px}h1{margin-bottom:6px}
.card{background:white;border:1px solid #dce4ec;border-radius:12px;margin:20px 0;overflow:auto}
.notice{padding:14px;background:#fff0ce;border-radius:8px}p{line-height:1.6}
.plotly-graph-div{min-width:600px}footer{font-size:12px}a{color:#143d69}</style></head><body><main>'''
    document += f"<h1>BFRC Güncel Grafikler</h1><p>{escape(banner)}</p>"
    document += f'<p id="build-time" data-built="{stamp}">Panel üretimi: {stamp}</p>'
    document += '<p id="stale" class="notice" hidden>Panel 24 saattir yenilenmemiş. Son gözlem tarihlerini kontrol edin.</p>'
    if warnings:
        document += '<div class="notice">Bazı API kontrolleri başarısız:<ul>'+warnings+'</ul></div>'
    document += ''.join(cards)
    document += '<footer>Grafik: Mehmet Faruk Bekir · Son gözlem tarihi ile API kontrol saati farklıdır. Veriler kaynak tarafından revize edilebilir.</footer>'
    document += '''</main><script>
function checkAge(){const t=new Date(document.getElementById('build-time').dataset.built);
document.getElementById('stale').hidden=Date.now()-t.getTime()<86400000;}
checkAge();setInterval(checkAge,60000);
</script></body></html>'''
    (output / "index.html").write_text(document, encoding="utf-8")
    (output / "status.json").write_text(json.dumps({"built_at":stamp,"demo":demo,
        "fresh_fetches":successes,"errors":problems,"charts":status_rows},ensure_ascii=False,indent=2))
    if not demo:
        cache_file.parent.mkdir(exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved,ensure_ascii=False,indent=2), encoding="utf-8")
        tmp.replace(cache_file)
    print(f"{len(cards)} grafik üretildi: {output / 'index.html'}")
    for problem in problems:
        print("UYARI:", problem)
    if successes == 0 and not demo:
        # Workflow deploys the cached-data warning panel, then marks the run failed.
        (output / "all_failed.flag").write_text("All provider fetches failed")
    else:
        (output / "all_failed.flag").unlink(missing_ok=True)


# Selection panel v2. EVDS3 contract verified against TCMB's docId=8 guide.
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
import unicodedata

# Group units/source labels read from TCMB public catalogue on 13 October 2026.
PANEL_GROUP_META = {'bie_abanlbil': {'name': 'Merkez Bankası Analitik Bilanço', 'unit': 'Bin TL', 'source': 'TCMB'}, 'bie_abstc2': {'name': 'Merkez Bankası Bilançosu - Stand By (Cari)', 'unit': 'Bin TL', 'source': 'TCMB'}, 'bie_rktufey': {'name': 'Reel Efektif Döviz Kuru - TÜFE Bazlı', 'unit': 'Endeks', 'source': 'TCMB'}, 'bie_dkdovytl': {'name': 'Döviz Kurları', 'unit': 'Türk lirası', 'source': 'TCMB'}, 'bie_apifon': {'name': 'TCMB Net Fonlaması ve TCMB Ağırlıklı Ortalama Fonlama Maliyeti', 'unit': 'milyon TL ve yüzde', 'source': 'TCMB'}, 'bie_tsanaymt2021': {'name': 'Sanayi Üretim Endeksi - Mevsim ve Takvim Etkisinden Arındırılmış (TÜİK)', 'unit': '2021=100', 'source': 'TÜİK'}, 'bie_tukfiy2025': {'name': 'Tüketici Fiyat Endeksi (2025=100)', 'unit': 'Endeks (2025=100)', 'source': 'TÜİK'}, 'bie_tukfiy2003': {'name': 'Tüketici Fiyat Endeksi (Genel) (2003=100)', 'unit': 'Endeks (2003=100)', 'source': 'TÜİK'}, 'bie_pbpanal1': {'name': 'Parasal Durum', 'unit': 'bin TL', 'source': 'Bankalar'}, 'bie_hpbitablo1': {'name': 'Para Arzı', 'unit': 'bin TL', 'source': 'Bankalar, TCMB, Takasbank, MKK, HMB'}, 'bie_abres2': {'name': 'Merkez Bankası Rezervleri', 'unit': 'milyon ABD doları', 'source': 'TCMB'}}

EVDS3 = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"
# These group identifiers were read from TCMB's live public catalogue.
PANEL_GROUPS = ["bie_abstc2", "bie_abanlbil", "bie_abres2", "bie_hpbitablo1",
    "bie_dkdovytl", "bie_apifon", "bie_tukfiy2003", "bie_tukfiy2025",
    "bie_rktufey", "bie_tsanaymt2021", "bie_pbpanal1"]


def evds3_json(client, path, params):
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        raise DataError("EVDS_API_KEY tanımlanmamış")
    # EVDS uses path parameters, not a conventional question-mark query.
    return get_json(client, EVDS3 + path + urlencode(params), headers={"key":key})


def period_date(raw):
    """Map period labels to dates without silently changing the source frequency."""
    raw = str(raw).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%Y-%m", "%m-%Y", "%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{4})[-/ ]?[QqÇç]([1-4])", raw)
    if m:
        return date(int(m[1]), 3*int(m[2])-2, 1).isoformat()
    raise DataError("Desteklenmeyen EVDS dönem biçimi")


def panel_catalog(client, cache, today):
    # Refresh metadata weekly; changing the configured group list refreshes it immediately.
    previous = cache.get("_panel_meta", {})
    if previous.get("groups") == PANEL_GROUPS and previous.get("at"):
        age = (today-date.fromisoformat(previous["at"])).days
        if age < 7:
            return previous["series"], previous, []
    groups = evds3_json(client, "datagroups/", {"mode":0,"type":"json"})
    if not isinstance(groups, list):
        raise DataError("EVDS veri grubu listesi doğrulanamadı")
    groups=[{k.upper():v for k,v in g.items()} for g in groups if isinstance(g,dict)]
    lookup = {g["DATAGROUP_CODE"]:g for g in groups if "DATAGROUP_CODE" in g}
    catalog, errors = [], []
    for code in PANEL_GROUPS:
        group = lookup.get(code)
        if not group:
            errors.append(code + ": EVDS listesinde bulunamadı")
            continue
        try:
            rows = evds3_json(client, "serieList/", {"type":"json","code":code})
            if not isinstance(rows, list):
                raise DataError("EVDS seri listesi doğrulanamadı")
            info=PANEL_GROUP_META.get(code,{})
            name = group.get("DATAGROUP_NAME") or group.get("DATAGROUP_TYPE") or info.get("name") or code
            for r in rows:
                if not isinstance(r,dict):
                    continue
                r={k.upper():v for k,v in r.items()}
                if not r.get("SERIE_CODE") or not r.get("SERIE_NAME"):
                    continue
                unit = r.get("BIRIMI") or group.get("BIRIMI") or info.get("unit") or "Kaynak birimi"
                if code == "bie_apifon":
                    unit = "%" if r["SERIE_CODE"] == "TP.APIFON4" else "Milyon TL"
                catalog.append({"id":r["SERIE_CODE"],"code":r["SERIE_CODE"],
                    "name":r["SERIE_NAME"],"group_code":code,"group":name,
                    "unit":unit,
                    "frequency":r.get("FREQUENCY_STR") or group.get("FREQUENCY_STR") or "",
                    "source":(group.get("DATASOURCE") or info.get("source") or "TCMB") + " / EVDS",
                    "provider":"evds3"})
        except DataError as error:
            errors.append(code + ": " + str(error))
            # Retain metadata for a group that was temporarily inaccessible.
            catalog.extend(s for s in previous.get("series",[]) if s["group_code"]==code)
    if not catalog:
        raise DataError("EVDS gösterge listesi yüklenemedi")
    return catalog, {"groups":PANEL_GROUPS,"at":today.isoformat(),"series":catalog}, errors


def fetch_panel_group(group_code, specs, start_year, today):
    buckets = {s["id"]:[] for s in specs}
    labels = {s["id"]:{} for s in specs}
    # Annual windows contain <=366 observations, below EVDS's 1000-row limit.
    with session() as client:
        for year in range(start_year, today.year+1):
            last = min(date(year,12,31), today)
            result = evds3_json(client, "", {"datagroup":group_code,
                "startDate":date(year,1,1).strftime("%d-%m-%Y"),
                "endDate":last.strftime("%d-%m-%Y"),"type":"json"})
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise DataError("EVDS gözlem şeması doğrulanamadı")
            for row in result["items"]:
                if not isinstance(row, dict) or "Tarih" not in row:
                    raise DataError("EVDS tarih alanı bulunamadı")
                d = period_date(row["Tarih"])
                for s in specs:
                    field = s["code"].replace(".","_")
                    if field not in row:
                        continue
                    buckets[s["id"]].append({"date":d,"value":row[field]})
                    labels[s["id"]][d] = str(row["Tarih"])
    records = {}
    stamp = datetime.now(TR).isoformat(timespec="seconds")
    for s in specs:
        if not buckets[s["id"]]:
            continue
        try:
            rows = validate(buckets[s["id"]],today)
        except DataError:
            continue
        # Original period labels are retained in tooltips, including monthly series.
        records[s["id"]] = {**s,"observations":rows,"periods":labels[s["id"]],
                            "fetched_at":stamp}
    if not records:
        raise DataError("Grup içinde kullanılabilir gözlem yok")
    return records


def panel_figure(record):
    rows = record["observations"]
    fig = go.Figure(go.Scatter(x=[r["date"] for r in rows], y=[r["value"] for r in rows],
        customdata=[record.get("periods",{}).get(r["date"],r["date"]) for r in rows],
        name=record["name"], mode="lines", line={"color":"#143d69","width":2},
        hovertemplate="Dönem: %{customdata}<br>Değer: %{y:,.4f}<extra></extra>"))
    # Native period labels and source units are preserved; missing values are never zeros.
    fig.update_layout(template="plotly_white",height=620,
        title={"text":escape(record["name"]),"x":.03}, showlegend=False,
        margin={"l":70,"r":30,"t":100,"b":145},
        yaxis_title=record["unit"],font={"family":"Arial, sans-serif","color":"#19344b"},
        xaxis={"type":"date","rangeselector":{"buttons":[
            {"count":6,"label":"6 ay","step":"month","stepmode":"backward"},
            {"count":1,"label":"1 yıl","step":"year","stepmode":"backward"},
            {"label":"Tümü","step":"all"}]}})
    watermark(fig, ROOT/CONFIG["logo_path"])
    details = [("Kaynak: "+record["source"],-65),
               ("Grafik: Mehmet Faruk Bekir",-83),
               ("Son dönem: "+record.get("periods",{}).get(rows[-1]["date"],rows[-1]["date"])
                +" · Veri kontrolü: "+record["fetched_at"],-101),
               ("SON BAŞARILI VERİ — YENİLEME BAŞARISIZ" if record.get("fallback") else "",-119)]
    for label,shift in details:
        if label:
            fig.add_annotation(text=escape(label),xref="paper",yref="paper",x=0,y=0,
                yshift=shift,xanchor="left",showarrow=False,font={"size":10})
    return fig


def write_panel(output, records, errors, stamp, demo=False):
    output.mkdir(exist_ok=True)
    (output/"plotly.min.js").write_text(get_plotlyjs(),encoding="utf-8")
    choices = []
    (output/"charts").mkdir(exist_ok=True)
    for n,r in enumerate(records.values()):
        item={k:r[k] for k in ("id","name","group","source","unit","frequency")}
        item["file"] = f"charts/{n}.json"
        choices.append(item)
        (output/item["file"]).write_text(pio.to_json(panel_figure(r)),encoding="utf-8")
    (output/"panel-data.json").write_text(json.dumps({"choices":choices},
        ensure_ascii=False),encoding="utf-8")
    (output/"status.json").write_text(json.dumps({"built_at":stamp,"demo":demo,
        "series_count":len(choices),"errors":errors},ensure_ascii=False,indent=2),encoding="utf-8")
    warnings = "".join("<li>"+escape(e)+"</li>" for e in errors)
    header = '<p class="notice">DEMO — Sentetik değerler; gerçek veri değildir.</p>' if demo else ""
    if warnings:
        header += '<details class="notice"><summary>Bazı veriler yenilenemedi</summary><ul>'+warnings+'</ul></details>'
    html = '''<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BFRC Grafik Paneli</title>
<script src="plotly.min.js"></script><style>
body{margin:0;background:#f4f6f9;color:#19344b;font:15px Arial,sans-serif}
main{max-width:1150px;margin:auto;padding:24px}h1{font-size:25px;margin:0 0 20px}
label{display:block;font-weight:bold;margin-bottom:8px}
select{width:100%;padding:14px;border:1px solid #bdcad7;border-radius:8px;background:white;color:#19344b;font:16px Arial}
#chart{min-width:650px}#chart-wrap{margin-top:18px;background:white;border-radius:12px;overflow:auto;border:1px solid #dce4ec}
#info,footer{font-size:12px;line-height:1.6;margin-top:12px}.notice{background:#fff0ce;padding:12px;border-radius:8px}
#message{padding:20px}button{border:0;border-radius:6px;background:#143d69;color:white;padding:10px;cursor:pointer}
</style></head><body><main><h1>BFRC Grafik Paneli</h1>HEADER
<label for="series">Gösterge seç</label><select id="series" disabled><option>Göstergeler yükleniyor…</option></select>
<p id="info"></p><p id="stale" class="notice" hidden>Panel 24 saattir yenilenmemiş. Son dönem tarihini kontrol edin.</p>
<div id="chart-wrap"><div id="message" role="status">Veriler yükleniyor…</div><div id="chart"></div></div>
<footer>Son panel kontrolü: STAMP<br>Veriler kaynak tarafından revize edilebilir. Seçim listesi panelin veri kataloğunu kapsar.</footer>
</main><script>
const built=STAMP_JSON;
function age(){document.getElementById('stale').hidden=Date.now()-new Date(built).getTime()<86400000;}
age();setInterval(age,60000);
const select=document.getElementById('series'),msg=document.getElementById('message');
fetch('panel-data.json?v='+encodeURIComponent(built)).then(r=>{if(!r.ok)throw Error();return r.json()}).then(panel=>{
 select.replaceChildren();const groups=new Map();
 for(const c of panel.choices){if(!groups.has(c.group)){const g=document.createElement('optgroup');g.label=c.group;groups.set(c.group,g);select.append(g);}
 const o=document.createElement('option');o.value=c.id;o.textContent=c.name;groups.get(c.group).append(o);}
 let generation=0;
 async function draw(){const token=++generation,id=select.value,c=panel.choices.find(x=>x.id===id);
 document.getElementById('info').textContent=c.group+' · '+c.frequency+' · '+c.unit;
 msg.hidden=false;msg.textContent='Grafik yükleniyor…';document.getElementById('chart').hidden=true;
 try{const reply=await fetch(c.file+'?v='+encodeURIComponent(built));if(!reply.ok)throw Error();
 const fig=await reply.json();if(token!==generation)return;
 document.getElementById('chart').hidden=false;
 await Plotly.react('chart',fig.data,fig.layout,{responsive:true,displaylogo:false,
 toImageButtonOptions:{format:'png',filename:'BFRC-'+id.replaceAll('.','-'),width:1400,height:700,scale:2}});
 if(token===generation)msg.hidden=true;
 }catch(e){if(token===generation)msg.textContent='Bu grafik yüklenemedi. Başka bir gösterge seçin veya sayfayı yenileyin.';}}
 const first=panel.choices.find(c=>c.name.toLocaleLowerCase('tr').includes('net iç varlık'));
 if(first)select.value=first.id;select.disabled=false;select.addEventListener('change',draw);draw();
}).catch(()=>{msg.textContent='Veriler yüklenemedi. Sayfayı yenileyin.';select.replaceChildren(new Option('Veriler yüklenemedi',''));});
</script></body></html>'''
    html=html.replace("HEADER",header).replace("STAMP_JSON",json.dumps(stamp)).replace("STAMP",escape(stamp))
    (output/"index.html").write_text(html,encoding="utf-8")


def build_panel(demo=False):
    today=datetime.now(TR).date()
    stamp=datetime.now(TR).isoformat(timespec="seconds")
    path=ROOT/"state/cache.json"
    cache=json.loads(path.read_text()) if path.exists() and not demo else {}
    previous=cache.get("_panel",{})
    records,errors={},[]
    if demo:
        for spec in ({"id":"demo-net","name":"Net İç Varlıklar (DEMO)","group":"TCMB Bilançosu",
                      "source":"Sentetik demo","unit":"Bin TL","frequency":"İŞ GÜNÜ"},
                     {"id":"us2y","name":"ABD 2 yıl getirisi (DEMO)","group":"ABD",
                      "source":"Sentetik demo","unit":"%","frequency":"GÜNLÜK"}):
            records[spec["id"]]={**spec,"observations":demo_rows(spec,today),"fetched_at":stamp}
        write_panel(ROOT/"demo",records,[],stamp,True)
        print("Seçimli demo üretildi")
        return
    with session() as client:
        try:
            specs,meta,metadata_errors=panel_catalog(client,cache,today)
            errors.extend(metadata_errors)
            cache["_panel_meta"]=meta
        except DataError as error:
            errors.append("EVDS kataloğu: "+str(error))
            specs=cache.get("_panel_meta",{}).get("series",[])
        grouped={}
        for spec in specs:
            grouped.setdefault(spec["group_code"],[]).append(spec)
        def load(code,items):
            old=[previous.get(s["id"]) for s in items]
            # EVDS's guide recommends daily fetching. Other scheduled runs reuse today's data.
            if old and all(r and r.get("fetched_at","").startswith(today.isoformat())
                           and r.get("code")==s["code"] for r,s in zip(old,items)):
                return {r["id"]:r for r in old}
            return fetch_panel_group(code,items,2020,today)
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks={executor.submit(load,c,ss):(c,ss) for c,ss in grouped.items()}
            for task in as_completed(tasks):
                code,items=tasks[task]
                try:
                    current=task.result()
                    records.update(current)
                    missing=[s for s in items if s["id"] not in current]
                    if missing:
                        errors.append(code+f": {len(missing)} seride kullanılabilir gözlem yok")
                except DataError as error:
                    errors.append(code+": "+str(error))
                    missing=items
                for s in missing:
                    old=previous.get(s["id"])
                    if old and old.get("code")==s["code"]:
                        records[s["id"]]={**old,"fallback":True}
        # Preserve the working FRED indicators alongside the Turkish catalogue.
        for code,name in [("DGS2","ABD 2 yıl tahvil getirisi"),("DGS10","ABD 10 yıl tahvil getirisi")]:
            spec={"id":code,"code":code,"name":name,"group":"ABD faizleri","unit":"%",
                  "frequency":"GÜNLÜK","provider":"fred","source":"ABD Hazine Bakanlığı / FRED"}
            try:
                records[code]={**spec,"observations":fetch_fred(client,spec,"2020-01-01",today),"fetched_at":stamp}
            except DataError as error:
                errors.append(code+": "+str(error))
                if previous.get(code):records[code]={**previous[code],"fallback":True}
    if not records:
        raise DataError("Hiç veri alınamadı; mevcut yayın korunuyor")
    # A first launch with no Turkish data fails visibly rather than claiming a full EVDS panel.
    if not any(r.get("provider")=="evds3" for r in records.values()):
        raise DataError("EVDS verisi alınamadı. "+"; ".join(errors[:6]))
    ordered=sorted(records.values(),key=lambda r:(
        PANEL_GROUPS.index(r.get("group_code")) if r.get("group_code") in PANEL_GROUPS else 100,
        r["name"]))
    records={r["id"]:r for r in ordered}
    output=ROOT/"dist"
    write_panel(output,records,errors,stamp)
    cache["_panel"]={k:{x:v for x,v in r.items() if x!="fallback"} for k,r in records.items()}
    path.parent.mkdir(exist_ok=True)
    tmp=path.with_suffix(".tmp");tmp.write_text(json.dumps(cache,ensure_ascii=False),encoding="utf-8");tmp.replace(path)
    if records and all(r.get("fallback") for r in records.values()):
        (output/"all_failed.flag").write_text("All APIs failed")
    else:(output/"all_failed.flag").unlink(missing_ok=True)
    print(f"{len(records)} seçilebilir gösterge üretildi")
    for error in errors:print("UYARI:",error)





# Official source metadata for the exact selected codes, checked 13 October 2026.
CURATED_SPECS = {'TP.AB.C1': {'id': 'TP.AB.C1', 'code': 'TP.AB.C1', 'name': 'Altın (Milyon ABD Doları)', 'group_code': 'bie_abres2', 'group': 'Merkez Bankası Rezervleri', 'unit': 'milyon ABD doları', 'frequency': 'HAFTALIK(CUMA)', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.AB.C2': {'id': 'TP.AB.C2', 'code': 'TP.AB.C2', 'name': 'Döviz (Milyon ABD Doları)', 'group_code': 'bie_abres2', 'group': 'Merkez Bankası Rezervleri', 'unit': 'milyon ABD doları', 'frequency': 'HAFTALIK(CUMA)', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.AB.N05': {'id': 'TP.AB.N05', 'code': 'TP.AB.N05', 'name': '2 NET DIŞ VARLIKLAR (A+B+C)', 'group_code': 'bie_abstc2', 'group': 'Merkez Bankası Bilançosu - Stand By (Cari)', 'unit': 'Bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.AB.N15': {'id': 'TP.AB.N15', 'code': 'TP.AB.N15', 'name': '3 NET İÇ VARLIKLAR', 'group_code': 'bie_abstc2', 'group': 'Merkez Bankası Bilançosu - Stand By (Cari)', 'unit': 'Bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.AB.TOPLAM': {'id': 'TP.AB.TOPLAM', 'code': 'TP.AB.TOPLAM', 'name': 'Toplam (Milyon ABD Doları)', 'group_code': 'bie_abres2', 'group': 'Merkez Bankası Rezervleri', 'unit': 'milyon ABD doları', 'frequency': 'HAFTALIK(CUMA)', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.BRUTDBORCOZEL.G15': {'id': 'TP.BRUTDBORCOZEL.G15', 'code': 'TP.BRUTDBORCOZEL.G15', 'name': '2. Kısa Vadeli', 'group_code': 'bie_brutdborcozel', 'group': 'Özel Sektörün Yurt Dışından Sağladığı Kredi ve Borç Senetleri Yükümlülükleri - Borçlu Dağılımı', 'unit': 'milyon ABD doları', 'frequency': 'ÜÇ AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.BRUTDBORCOZEL.G28': {'id': 'TP.BRUTDBORCOZEL.G28', 'code': 'TP.BRUTDBORCOZEL.G28', 'name': '3. Uzun Vadeli', 'group_code': 'bie_brutdborcozel', 'group': 'Özel Sektörün Yurt Dışından Sağladığı Kredi ve Borç Senetleri Yükümlülükleri - Borçlu Dağılımı', 'unit': 'milyon ABD doları', 'frequency': 'ÜÇ AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.ENFBEK.PKA12ENF': {'id': 'TP.ENFBEK.PKA12ENF', 'code': 'TP.ENFBEK.PKA12ENF', 'name': 'Piyasa katılımcılarının 12 ay sonrası yıllık enflasyon beklentileri (%)', 'group_code': 'bie_enfbek', 'group': 'Sektörel Enflasyon Beklentileri', 'unit': 'Yüzde', 'frequency': 'AYLIK', 'source': 'TCMB, TÜİK / EVDS', 'provider': 'evds3'}, 'TP.FHGE.S01': {'id': 'TP.FHGE.S01', 'code': 'TP.FHGE.S01', 'name': '1.Finansal Hizmetler Güven Endeksi-Genel', 'group_code': 'bie_fhge', 'group': 'Finansal Hizmetler Güven Endeksi', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.GENENDEKS.T1': {'id': 'TP.GENENDEKS.T1', 'code': 'TP.GENENDEKS.T1', 'name': 'Genel Endeks (2003=100)', 'group_code': 'bie_tukfiy2003', 'group': 'Tüketici Fiyat Endeksi (Genel) (2003=100)', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TÜİK / EVDS', 'provider': 'evds3'}, 'TP.GY1.N2': {'id': 'TP.GY1.N2', 'code': 'TP.GY1.N2', 'name': '1. Reel Kesim Güven Endeksi', 'group_code': 'bie_rkgey2', 'group': 'Reel Kesim Güven Endeksi', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.GY1.N2.MA': {'id': 'TP.GY1.N2.MA', 'code': 'TP.GY1.N2.MA', 'name': '1.Reel Kesim Güven Endeksi-MA', 'group_code': 'bie_rkgema', 'group': 'Reel Kesim Güven Endeksi Mevsimsellikten Arındırılmış', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.HPBITABLO1.11': {'id': 'TP.HPBITABLO1.11', 'code': 'TP.HPBITABLO1.11', 'name': '2.M2', 'group_code': 'bie_hpbitablo1', 'group': 'Para Arzı', 'unit': 'bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Bankalar, TCMB, Takasbank, MKK, HMB / EVDS', 'provider': 'evds3'}, 'TP.HPBITABLO1.12': {'id': 'TP.HPBITABLO1.12', 'code': 'TP.HPBITABLO1.12', 'name': '2.1.Vadeli Mevduat (TL)', 'group_code': 'bie_hpbitablo1', 'group': 'Para Arzı', 'unit': 'bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Bankalar, TCMB, Takasbank, MKK, HMB / EVDS', 'provider': 'evds3'}, 'TP.HPBITABLO1.15': {'id': 'TP.HPBITABLO1.15', 'code': 'TP.HPBITABLO1.15', 'name': '2.2.Vadeli Mevduat (YP)', 'group_code': 'bie_hpbitablo1', 'group': 'Para Arzı', 'unit': 'bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Bankalar, TCMB, Takasbank, MKK, HMB / EVDS', 'provider': 'evds3'}, 'TP.HPBITABLO1.18': {'id': 'TP.HPBITABLO1.18', 'code': 'TP.HPBITABLO1.18', 'name': '3.M3', 'group_code': 'bie_hpbitablo1', 'group': 'Para Arzı', 'unit': 'bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Bankalar, TCMB, Takasbank, MKK, HMB / EVDS', 'provider': 'evds3'}, 'TP.HPBITABLO1.2': {'id': 'TP.HPBITABLO1.2', 'code': 'TP.HPBITABLO1.2', 'name': '1.M1', 'group_code': 'bie_hpbitablo1', 'group': 'Para Arzı', 'unit': 'bin TL', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Bankalar, TCMB, Takasbank, MKK, HMB / EVDS', 'provider': 'evds3'}, 'TP.IHRISICREV4.TT': {'id': 'TP.IHRISICREV4.TT', 'code': 'TP.IHRISICREV4.TT', 'name': 'Toplam', 'group_code': 'bie_ihrisicrev4', 'group': 'Genel Ticaret Sistemine Göre Dış Ticaret Ekonomik Faaliyetlere (ISIC, Rev. 4) Göre İhracat', 'unit': 'bin ABD doları', 'frequency': 'AYLIK', 'source': 'TÜİK / EVDS', 'provider': 'evds3'}, 'TP.ITHISICREV4.TT': {'id': 'TP.ITHISICREV4.TT', 'code': 'TP.ITHISICREV4.TT', 'name': 'Toplam', 'group_code': 'bie_ithisicrev4', 'group': 'Genel Ticaret Sistemine Göre Dış Ticaret Ekonomik Faaliyetlere (ISIC, Rev. 4) Göre İthalat', 'unit': 'bin ABD doları', 'frequency': 'AYLIK', 'source': 'TÜİK / EVDS', 'provider': 'evds3'}, 'TP.KFE.TR': {'id': 'TP.KFE.TR', 'code': 'TP.KFE.TR', 'name': 'Konut Fiyat Endeksi (KFE)', 'group_code': 'bie_kfe', 'group': 'Konut Fiyat Endeksi', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.KKO.MA': {'id': 'TP.KKO.MA', 'code': 'TP.KKO.MA', 'name': 'İmalat Sanayi Kapasite Kullanım Oranı_Mevsimsellikten Arındırılmış', 'group_code': 'bie_kkoisma', 'group': 'İmalat Sanayi Kapasite Kullanım Oranı - Mevsimsellikten Arındırılmış', 'unit': 'Yüzde', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.KKO2.IS.TOP': {'id': 'TP.KKO2.IS.TOP', 'code': 'TP.KKO2.IS.TOP', 'name': 'İmalat Sanayi Kapasite Kullanım Oranı', 'group_code': 'bie_kko2', 'group': 'İmalat Sanayi Kapasite Kullanım Oranı', 'unit': 'Yüzde', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.KREHACBS.A1': {'id': 'TP.KREHACBS.A1', 'code': 'TP.KREHACBS.A1', 'name': '1. TOPLAM YURT İÇİ KREDİ HACMİ', 'group_code': 'bie_krehacbs', 'group': 'Krediler - Bankacılık Sektörü', 'unit': 'bin TL', 'frequency': 'AYLIK', 'source': 'Bankalar / EVDS', 'provider': 'evds3'}, 'TP.KTF17': {'id': 'TP.KTF17', 'code': 'TP.KTF17', 'name': 'Ticari Krediler (TL, Akım, %)', 'group_code': 'bie_kt100h', 'group': 'Kredi Faiz Oranları (Akım)', 'unit': 'Ağırlıklı ortalama', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Mevduat, Kalkınma ve Yatırım Bankaları / EVDS', 'provider': 'evds3'}, 'TP.KTFTUK': {'id': 'TP.KTFTUK', 'code': 'TP.KTFTUK', 'name': 'Tüketici Kredisi (İhtiyaç+Taşıt+Konut) (TL, Akım, %)', 'group_code': 'bie_kt100h', 'group': 'Kredi Faiz Oranları (Akım)', 'unit': 'Ağırlıklı ortalama', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Mevduat, Kalkınma ve Yatırım Bankaları / EVDS', 'provider': 'evds3'}, 'TP.KVDB07': {'id': 'TP.KVDB07', 'code': 'TP.KVDB07', 'name': '3.Bankalar', 'group_code': 'bie_kvdbs', 'group': 'Kısa Vadeli Borç Stoku', 'unit': 'milyon ABD doları', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.KVDB12': {'id': 'TP.KVDB12', 'code': 'TP.KVDB12', 'name': '4.Diğer Sektörler', 'group_code': 'bie_kvdbs', 'group': 'Kısa Vadeli Borç Stoku', 'unit': 'milyon ABD doları', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.KVDB19': {'id': 'TP.KVDB19', 'code': 'TP.KVDB19', 'name': '5.Toplam', 'group_code': 'bie_kvdbs', 'group': 'Kısa Vadeli Borç Stoku', 'unit': 'milyon ABD doları', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.RK.T1.Y': {'id': 'TP.RK.T1.Y', 'code': 'TP.RK.T1.Y', 'name': 'TÜFE Bazlı Reel Efektif Döviz Kuru (2025=100)', 'group_code': 'bie_rktufey', 'group': 'Reel Efektif Döviz Kuru - TÜFE Bazlı', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.TRY.MT06': {'id': 'TP.TRY.MT06', 'code': 'TP.TRY.MT06', 'name': 'Toplam (TL Mevduat, Akım, %)', 'group_code': 'bie_mt100h', 'group': 'Mevduat Faiz Oranları (Akım)', 'unit': 'Ağırlıklı ortalama', 'frequency': 'HAFTALIK(CUMA)', 'source': 'Mevduat Bankaları / EVDS', 'provider': 'evds3'}, 'TP.TUFE1YI.T1': {'id': 'TP.TUFE1YI.T1', 'code': 'TP.TUFE1YI.T1', 'name': '1.Yurt İçi Üretici Fiyat Endeksi', 'group_code': 'bie_tufe1yi', 'group': 'Yurt İçi Üretici Fiyat Endeksi', 'unit': '2003=100', 'frequency': 'AYLIK', 'source': 'TÜİK / EVDS', 'provider': 'evds3'}, 'TP.TUKFIY2025.GENEL': {'id': 'TP.TUKFIY2025.GENEL', 'code': 'TP.TUKFIY2025.GENEL', 'name': 'Genel Endeks', 'group_code': 'bie_tukfiy2025', 'group': 'Tüketici Fiyat Endeksi (2025=100)', 'unit': 'Endeks', 'frequency': 'AYLIK', 'source': 'TÜİK / EVDS', 'provider': 'evds3'}}
CURATED_LOGO = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAPoAAACHCAYAAAA/UEQiAAAACXBIWXMAAAsTAAALEwEAmpwYAAAisElEQVR4nO2deZgcVbm431PTc2bNNpnJQhYICSkIcA2yIwIKIrhQiAQJi4ygXKKAuPDzXsSrAop63QAxiuQSQWQRhUIEZQ0ESIiQBIghJYGELJPJZE8mmZmaTNfvj+900hm6e7pnetY+7/P0091Vp06dqu6vznfO+RYVRRFdRSnV5Tosvc/cBYtH6+LiulE1VYysGc6WbdtZubqOdesbWN+wAeCcGbXTH+7tdhYK+ZDNBE7earJYLH0WK+gWSwFgBd1iKQCsoFssBYAVdIulAIj1dgMs+zJ3weJTKsvLnhtRM5xRNVWsXlvPe2vWsa5hA1u2bAE4aEbt9OW90baRI2oOW7gk+MvI6ipG1Qxnzbp63ltTz7r1DWzu5bZZMmN7dIulALCCbrEUAFZ172dMnjTxijeWLf/G6BHVDBlUwYpVa1m9roH6+vXsaGwEKJ9RO72pN9s2qmY4w4YMYuXqOlatrad+fQPbd+zo1bYVOrZHt1gKACvoFksBYFX3TjB3weJVNcOHjRtVU0WR47BydR2r162nfn0DLS0tq2fUTh/f2220WJKxPbrFUgBYQbdYCgCruvcwcxcsHl2i9R5X0E2bt/De2nrq6hto2LCBDRs3/k/l+HGdrv/QKYc8u2z5yuNG1gynOFbEilV1rK2XYUVTc7MdVhQotke3WAoAK+gWSwFgVfc8o5TipVffiBKqeXNzCytX11G3voH69Q28/c67sw475ODebqalwLA9usVSAFhBt1gKAKu6p2DugsW3DB0y6OpRNcMZXFkuBjFrxR2zcedOgNW93cb+yKFTDnl26dsrjhs9opoSHRM7/boG6hsaaGpqYkbtdBtltJuwPbrFUgBYQbdYCgCruhcQSile/KesCIyqqSJsbWXFqrXUrd+wZ0Xg0IPd3m5mp5i7YPER5WWlC0dVVzFqxHDq6htYubaedfXr2bR5C8AHZ9ROX9Tb7ewtbI9usRQAVtAtlgKgIFX3uQsWf6ayouwvo2qqGVVTxco161i1Zh3r6tezZds22traZvV2GwuRuQsWR6NHDGdU9XBad+9mxWqx01+/voHW3bv/NKN2+nm93cb+iu3RLZYCwAq6xVIAFKTqbul/xGKx4vmL/iU+BNVVbNvRyHtJPgRr6+puPGjigb3dzD6LFfR+hFKK4lisxFGKKIpoa2vDUYoSXczgQZUUx4ooLdGDlVKEra3E420opSgt0QyqrKC0tKQkVuTQ1tZGU3MLbfE2AIqKitC6mFhRrKyXL9HSTVhB72GKY7Gi4uIYsVgRURRRXFxM9bChlJVo9h8zkprq6mmxWBHFsSK2bd9BWWkJB00Yz6QJ41FEAFdGUURLcwvNTc1oXcy40SMZM7KGKIqIYEoURWzdtp0oiiiKOYyorqJm+DCiiBFRFNHS0sLa+gaiKEIpxUiznyi6IIoi6hs2sq5hI0pJju7hVcOorKhAx2KXNDe30LBxM01NzezctYt4FKG1prS0lCiKhuUzp7clf1hBzyNlpaVUDR2sq4YOZsigSnRxjKIih4MmjGPShLEUFxdT5BTVRlFEFMWJx9soKy2htEQTjw8DIB6PDo+IiMelx1ZKUVTkEI8ionjPCZG0Uc4XKyrCKSuFeDRx9+7dbN/RyrbtOxIPFgYPqqSysoJ4PH5pfcNG6hs2oWMxIuK0trZSXl6O4zi0tLSM3Lmricadu3BUeY9di8UKek44jsOwIYOpGjq4bHBlBcWxGI7jMGH8WCZPOgBHKRylRiLvABRFEcVFRcSN0MR7UFh7iyiKaAlD4lEcogitiykujtHc3DxhXVMTdfUNYB4SEFFeVkYsFmPnzl1s3hJDx2KUlOhevoqBhRX0FBTHYiUlWlPkOLS1xRk+bCjVw4ZSWlKCUgrlqGqQMbP0uEUo63eVM6IxRMRiRRQVFdG4cyc7GhtZuXotEIFyiOJxSkpKiKLokB2NjVSUlTBs8KDebnq/o+AFva2tjeJYjJEjqhk9opqKinIqykovUo6DAtra4pSa3kV1jzQ3AduBbUnvjcBOoBXYat53RlHUpJRqMWUS7ALCPaq2UltTnUT27/2uFE4UMdh8LY8iNFHkgNqzDSgGSoAKYDAwyGxPfK80nyvNK6/E43GieJxYLEYURW5DwwbWr29g8ZK3KDYPh92724jFYrS1tY1ra4vnuwkDhoIR9Lr1G4uUUgcrxVFha+v5SjnEYkVs2d5IaYmmrKwURZeFeQOwHqgHNpnXRrN9E7A56X0bsL2yory1KyfsKzw195ViYBhQleI98RoO1AD7ASPM507R2rqblrAVzIRma+vu2iVvLWP5O5qqqqHEiopoDluxwi+ofMySdlNP1yVWr2sYAZzgwAkodYJS6gilVLlSifYqHEfarYBED66Uev93RSOwxlHOKmCVctQaYJ1Saq1SqkHe2VAciw0Ioe0pHn7i2RhENVEUjY4iRkZRNCqKopFRFI2CaFw8YnwUj48DRiY0lsQEIFEk8x5pvieXB4ji8dejiBcioleB14BlM2qnt/XSpWdFPlcwBoygr1yzbgpwoqPUCcAJynEOAmP6Z8bSRmhTCfpO5TjvKlihlJJ3+f6eUmr1oMryrb10WRbgvocfL4miaFwUReOjKBoXwXiiaFw8isYTRRMjOIAoinUg6EQRJKYAkSHPYuAVYD7w0oza6Wt74fLSYgUdWL5yzSTgo46jPgqc4jjOSGDPbLdyxLo3SdBblFJvK8XbSqkA1L8dR70N/LumamhDj1+AJW/MfuCRIqJofDyKJhFFkyM4MIqiSeY1EShJIeipWA3MS3otnFE7vde0tIIU9GXvvDfCUeoM4FTgVMdxxgB7emUnIdhK7QT+rRxnCbDUgaUotUQptXK/kdV2wFZg3HHPgwoYG8Xjk6KIKRHRFOBQ86ru4PAmRODnAnOA+TNqpzd3Z3uTKQhBXxK8W+Q46njgDEepT6DUVCfpREawGxxHLQIWO46zCFjkKLV8/JhRVqAtHTJz9n017BV6FzgMmAKMTHNIC6LmzzGveTNqp7d0V/sGrKC/sWx5FfApRzmfBk5zHDUUjDqu1DuOEqEGFjmOs3jSAWPr8nJiiyUJ8wCYChyR9JqMzNsm0wQ8DzwFPA28OaN2et6kc0AJ+qKl/x7voM5GqbOV4iSgyFHODmC+46j5wDxHqfkHTzpgS5cbarF0kpmz76sAPoAI/VTgg4gGkGzCV48I/BPAP2bUTt/UlXP2e0F/7c1lByulpinFOSg11UEtQ6l5SjEfeNlRztLD3AOt+m3p08ycfV8xovYfCxxv3hP5tuLI+P5x4PEZtdMX51p/vxT0f76+dCIwTSlnulKUKKWeU4pnUOr5I6ZM3tDlRlgsfYCZs+8bigj8scBx5r0KmdH3gUeAF7KZze83gj5/0b/GKvicUupopdgGPKeUM+eo/zi4vssntVj6CTNn3zcZOAY4ATgJGI309A8CT6ab0OvTgv7Sq2+UK6XOULC/cpxWBU8de8ShQZdPYrF0gHa9IcB4oAyxyd+N+A1sBOrCwO8Tw8GZs+8bDnwYOAUZ978N/AV4Jrmn73OC/uI/X1egXOWoKgXFSqlXTjjy8B5bb9Su91HkB+4MzeYVZ699+oYw8Ls0kZLUtjOAUfmoKwf+GAZ+2K4d44GPdqKuVsTBZjdis78GqA8Dv9fNR7XrHQWcAZyMTJBlWhdvBd4BFiJj56fCwO8THdDM2fcNQQT/MMRXIgDmXXHJ+XmT9HwJegWoXR8+5gO94mytXe8RwMtztZuRNdPHgPvDwO/UrL92vTnIH7EnGRYG/tZ27TgbeDhP9bchvdCLwJ+BJ3uqt9SuVwZcDvwncEgXq3sbuA+4Mwz8PpM400zylV1xyfnb81Vnry+v5YNuEvRkmoHfADfkKvADVNDb8zbwXeSB2C0Pe+16CrgMuJH8a0hx4E/I77s0z3V3mnyq7jbcc3aUAtcAy8wwwbIvBwF/BJ7UrrdfvivXrjcGeBb4Hd0zDHKAzwFLtOvdr11vQjeco1exgp4bI4B/aNf7RG83pI9yGrBAu97h+apQu97xwKvIxFV3oxCB707tsFcomMATeSQGPKhdb0oY+Kt6uzF9kDHA09r1jg0Df2VXKjLa02PILHpHrASeAd5A1qx3IhFxhiCBLg5HHhYjutKm/spAF/RXgPMz7B+MaDXlwFBkffNo4CzzOR0VwI+AC7rQtkXArV04PhO7ciz/SSCd30AMCRM1GpgEfAj4CPuafrZnBPCQdr3jwsDfnWNbANCudwTwKB0L+YPALWHgv5xFnUVI2y9CfrvizrStPzLQBb25E73KLO16VwJfAH6G9AqpOE+73tVh4G/sZNtWhYE/u5PH5puludwn7XpDgauB65CYcqk4EpgB3JZrY7TrDUcmDisyFFsKfCEM/AXZ1muWBJ9GNI7rgW+ZNhbl2sb+hh2jpyAM/N1h4P8O8X1P54ZYhKzhFhxh4G8NA/8GxNIr04PuWtOL5sqdwP4Z9j8EHJWLkLcnDPw1YeBfhTyQXutsPf0FK+gZCAP/n0CmFMpTe6gpfZIw8BcC0zIUGQecmEud2vU+C5ydocg9wHlh4DflUm86wsB/HXlg3ZKP+voqVtA75sEM+8b0WCv6KGHgz0F62HRkvRypXU8Dv8hQZA6irud1rT4M/DAM/GsQI5xet/jrDqygd8yyDPtsUkLh9xn2TcmhnksRLSAVW4ELu9P0Ngz8O5CJyT5jNJMvBvpkXD7IZAm3o8da0bd5KcO+dGGZ9sFYvn09Q5Hvh4Hf7RGFwsD/R3efozewPXrHDMuwz66jA8YsuDHN7sFptrfnZMTCLhV1wK9zbZdlL1bQOyZTjzS/x1rR90k3bt6Z5fEXZth3e3tvPEtuWEHvmHSzxtuQoIAFj3a9GOntDbZmWc2ZabZHyEy7pQtYQc+AGTd+Kc3u34aB32M+932cTDnU1nd0sHa9Q0m/gjG/L7mQ9lesoGfmOlKvla8DftizTenTHJFh36Isjj8qwz6rNeUBO+ueAhOS6EbgqhS7W4Hzw8DflmJfoZLJm+/ZLI6fmmHf3NyaYkmFFXSDdr1SZM33HOAKJMVve1qBc8PAfyEPpyzXrndAHuppz5rOOpJ0BmP3nm4i7a0w8P+VRTUHZ9j3Zs6NsryPgS7oR2nXW5xh/yDEZr2CjvNwrUJ68nl5atvHgBV5qiuZCYjLZk9xE+L5l4pfZllHumAVW8LA73CMb+mYgS7oiewaXSGOjMd/HAZ+urXigkS73qXAV9LsXgbclWVV6VyCbVjwPGEn4zrGQcwiv6Jdr6NevyDQrqe1691IeoefVqA2DPxsUw6nM0qygp4nBnqPni8Sifau0673U+AnYeB3WxbNvoh2vUpkLH06EoU1kxvpjDDwX8mh+nT/w7xFQS10rKDnxmDgBsDTrueFgb+2txuUJ+7XrpfKJiARXaaG9OPoZOLAV8LAz+Tauw/a9dIZ2ljyyEAX9LVkdqEEmYwbhHiiVSPpccd2cMyRwPPa9U4MA7+z6uVqslt6ypXOzCMcm4fzbgQuDgP/7zkeZ42OeoCBLujLjZ9xTpiQxRcjE03p3CYnAn/SrndKJ10nF4aBX9uJ4/oaccRN9Vth4OecLDMM/Fbtpg26mikunSUH7GRcCsLArwsD/8eAC/w0Q9ETkfFqIRJHorJMCgP/0s4IeRLp3H0zeQ5acsAKegbCwG8KA/9a0i8hgcRFK8T76AArwsDPhy1AurhzmSLxWnKgEP+gORMG/q+R+OKpmIAkLujPPIXk7k71eivDcT/XrpePa083qbmf8YyzdBF7E7PnV8Cn0uw7DXiyB9uSby5PF+5Zu14NkiklVbZaB0lmcXQY+O904fzpAngUI0t6S7pQtwXbo+fCC0jq4FTkY9a6T2LG3ueQfnZ8GPBoF5fJ3siw78gu1GsxWEHPEhNeON14tKPluH5NGPivIRFS0zEFuLcLcxWLM+w7vZN1WpKwgp4b6WaWB/zscBj4dyPDl3R8GjEm6gwLkFn8VJypXS9dNhhLllhBz410ttuFch+/jgxh0vFt7Xrn5VqpCS6ZzitwGPIQsXSBQvmD5otUPupQIDbZxknlPNLPkgPMNgkSc+VvGfZ9rRP1WZKwgp4lJofYhDS7V/ZgU3oV4x/+WSBdVNYy4BHtermmJ76H9Or7CSaFsqWTWEHPnuNIn92zoJZ/jGfalzMUGY+kTc7ahDUM/DVAJjv527TrdXuaY+16pWZJcUBhBT17MmURebrHWtFHMB5qv8lQ5MPknjL5fzPsm4LE8es2tOtVIcZDmWLM90usoGeBdr0LkLXkVGwBnujB5vQlvkrmJBaXa9fL1PPvg0nYOCdDkW9p1/tctvXlgna9iUhqqZyyv/YXrKB3gPljzc5Q5Nf5SuHb3zDZU84hcySYW7TrnZJDtVeSfvwPcI9JrZw3tOtdjKzlZwpS2a+xgp4G7XojtOvdCdyPmGKmYj3wk55rVd8jDPx1yORcuqXHGDJePyDL+v4F/CBDkWLEPfiGro7ZtetN0a73FHA3EmBjwGJt3Q3mTzMWSSZwFnAuUNrBYV8IA78gltYyEQb+y9r1rgFuT1NkOGIme3wY+NnkYvsBYlacLl68Ar6DRPr5DvDXbHOmG+u9DwPXAJ6pa8Az0AV9qna9OWn2OUhoqCJghHnlwtfDwC/Usfn7CAP/19r1jkRynKficOBu7XrndiSUYeC3ade7EHgG+GCGov+BeNit0K53vyn/Zhj4DYkCxgZ/FJIk4mRkqFFw7q8DXdCHID9uPmkDrjauq5Z9+TIi0Een2X8O8D/A9zuqKAz8rWbt/B907DQ0Afhv80K7XiuwC1HHi7Jq+QDHjtFzYyVwshXy1JjIuJ8FGjIU+552vbOzrG8b8BFkDJ0LxchDvjNCvg2JST+gsIKeHY2Iw8ahYeC/1NuN6cuYzKfnkd6lF+AP2vUOz7K+pjDwLwHOR5Jbdhf1iEYwrhMBLvs8VtDTEwGvIHbW48LA/24Y+Lt6uU39gjDwnwe+kaFIBTI5l853IFWdDyDLX98mi1TMOTAPqAUOCAP/R2Hgp4tf168ZKGP0FcDrnTiuFdiJ9D6bEJXzXUR1m2+8qrrKclLnJuuOvGuZ2Eb6e5Rp3bpThIF/q3a9yWQ2QLmOzA+E9nVuB36oXe/nSPacaYi/ei5uwiHwMvBX4OE8xbzr86goympVInMlqiBWKCx9EO16CjgEybF3IDKjXok41zQhD/NNyAN8KfBqGPj9IpZ8PmQzgRV0i6WPkk9Bt2N0i6UAsIJusRQAVtAtlgLACrrFUgBYQbdYCgAr6BZLAdDvDWaMe2killuYznrNxC8rBxrDwN+dtH0w0GLstBPbYsha7Lb2nlZm3XZIilO0Ii6P1enSG/UG2vWGIi6Zv8mUy127XiXgWLfbgclA6NE/iYRz2gLs1K4X1673snY9t125n5kyM9ptfyPFthNN2VQCPSTpfMmvexEf9r5maTUU+C7iqpkS87BcBWwycdPyina9O2wU195lIAh6gg8g7oonIn/qHyd2mGADnwUSDhf5YLo5X+J1OfAo0JmY5r3NqYBG8pR/phvqP53USRotPUS/V92TWBUG/lZgpXa9eewbXOBDQA3iD+1r19svDPy6Lp6vvr2Krl3vWOBq4ELTg12G2FRfjUSr+XkY+H8wZX+FPBTKkAfQj02klgnAL4DfI5rGfsADYeDfaI47GYlIO84c97sw8B/TrncrEqShHKgDfhYG/nNZXss0JC10aD7PMudKtOUaJPfaBsSh5PPASMR+/lFzXZG5/v8C9keSPNwNTEKCelxt3FOfRfwLRoSBf7N2vcvM/nmm3p+Y63sRifRzCPAQMNO05VAkUus3zTm/gWS5HYb4KtwZBv6D7S9Qu96VwH5h4F+XtO1qYFAY+D/QrncG8E1gDGIqe20Y+O9mef/6PAOpRz9cu95U7XrTgI8BtyTtOw+JPvI35M+aj+CCU7XrnZL0OgB5uHzS7B8PXID88e9BHEpma9fbz+wvR4T5RsQN9u8mGsoQJMTRLUic878AN2jX+5h2vdFmW2DqnW/KgkTLudfUtwH4WzZquFHbP2PO8yfg1KTjEm15CdGYHERbegG4CXjEnG96Uqjk1aZtLyD3+RUkCMRCU34hEpwiEUzicOBa4A/mnsQQDeA3SLz8B5BgFa8h8QD+yN4wUCAC7iPBLP4JPKBdb2qKS10FXKtdr9pcdxFwPbBOu96pyMPkYSR4RjPwhCkzIBhIPfpjSKCBCsSBYRPso7Z/Jwz8uHa9h5G43bnGHG/PTezrc/0j3h+woBE4Ogz8Vu16dyE91geAujDwLzXtqwE2Iw+FiUnHnh4G/jJTZhqSPrgJ0Qx+Y3qbJxMZTMPArzVlhyO9aS3SG2ZKnwTwcUSreByZUNyFCP6spDLnmKQNe9CuV4bE2DvftG0JMAj4vzDwF5u2FZmwUI3Ai2HgzzbHntuuDe8CJyacTbTrAXwjDPyZ5vvngVlh4N9svp9n7uMjYeBfb7YNAd4CrkDCRi1ud44nkPs8DdEOTkP+K39CHnK3hIF/u6lrIbAV0bhe7eD+9QsGkqDvb8IPVSLq32Pa9cYiP9ZoYIcJO7wK+E/teuPDwF/VhfN9ysQh30OKyCltJl8ZYeA3a9fbAZSYmfvvIL1HFamFMdnDaj0i4IuRXm2xdr0nETX4Lu16zUjQhKuAanONkD56bTKfA95EgmKCTE7uUd8NQdI1fgSZ2JyKBGuoRNTuZYigzdWu9zTwHHAXMu7viDUpPMqSQ2g3tvu+HRPUUbveRchk44HIfUyZp908bO8FLkIE/QLgz2Hg7zAawBEmTl2CCBmeDAgGkuoOQBj4jcgPOQTJ7nEB8kf5CRKf/XKk55pmDtnN+0P9Jv4s6UIYd5ULEV/sLyK9SlYTeObapiJjyWYks8l9wNnA94CvAOVh4E9MXcO+mHTEZyHj/dnmNRFR36tTlK9ExuQvA8PDwN8PedgkYrwfg8xH7EAiuT6cTTs6i3a9Q5Fh0a+QsfZ4ZH4iHbORPG6HIPdsttkeIeP/U5JeByIPqwHBQOrRkzkO+fHqELX9vxJqGYB2vVmIoP8M6a1OaHf8ycDaLEMTd4bJwFth4D9m2pNVTHGj5m8JA/8O4A7telchEVcWACvCwP+LKTc4y3acjmgK4018toRKvhFZKmyfhWUM8lCcmRSUY5A5bhiwMwz8uxAt41L2Do9akeFBvnGR3/k2MywrBtLmUg8D/w3teq8jmsYW9maFWQQcFQb+nnjy2vXKSZ/0sd8xkAT9ZmMUMxYZf92A/BGqeX/P8mfgUu1645En+T+MWvciogXMQHrN7mIe8B0TKWUz0rNnw4eAH2jX+z0y430pMsk4z2y/FVGnv5BlfecBzyaEHCRGm3a9vyMPwvaC/i4y0Xe7dr1HgTORh+KbyJj5DjMXsdG0LdEjvgF8xTyAurrakcyrSFTeP2rXew3R3joK2z0b+c1vDAM/IcjfA54z8zdzEJX9IuAkBkim3IGgur+DzFC3ICrji8DxYeB/D+mtrkuxlPYM8EtgVBj4TyM/aBsyXh0NTA8D/5dpztdizrcmTVtmms9Lkz4nmAm8Y+LBz0Amy1zkD3oLIkQbzOdkC7WHEKF7Dhk7H4+ong8BXzTLaJcBBwGHIX/SRBu3J9XdnndJndjwF8h8wGb23ttEfvSPm7pOQ4YNVwDPI0J3KzIx9zVkXX66qe8qRICOQe7z88iyI+0+J/g/5P4luBfpdfe5H2aOxUO0heORWfTvtzu2Pfeaa9ozBxEG/jwkRPVac337I+P+jiYy+w02wowl75ghxTFh4F/c223pz+QzwsxAUt0tvYx2vWuRsX1CS7H0EaygW/LJbMRo5e0w8AeM2jsQsKq7xdJHscEhLRZLTljVPQNm+e2DYeA/0m77icCuMPAXdtN5/xtoCAN/VoeFu+f84xEHmbfCwE+2inOATwNNYeA/2Yl6b0LW+7t8XcZpSA/E9EndgRX0zHwUMa5oPza5CVlfre2m8x5L767fJq77VfbNjHoG4pjyHnBAJ+o9kfdbIXaWyxDrRyvoWWAFvZsxRjwVXU3vZCy12pIj4STtK0ZMQDeb7zGg0rjtJpdzTLlt7etIwUZgsna9E8PAf9Fsuxp4G1kjT643hgS42JocvSdXTD3DEOu/jPWEgX9hpv2WfbGC3gWMV9U3gSMTziva9b5tvp9j/J1/CFRo11uOaAZfRoxf3gA+EQb+UnPcHYhzxw3tznEyYtk3zHxfghj0LNGu9yLiTHIWUKNd7wnErPceYLTZ/zHjUPOwKecYJ5jbw8DPZP23E/En/ybworEPPwb4OUmWfMbP+ybETXabdr2vhoF/tzGBPRcxhrkKcTVNvq5DEAvF28LAn6ldbwZiHz8UaNSu9/Uw8O/Urvc4Yq2YzPmI+e7YMPAvz3ANFoOdjOsaTyDWbWfCnh7zCuBp4+P8C0SwqxFnmomIz7WDWF8l94wjEE+29mxAzEnHIOryevZas41FLMKmIRZdZwK/Bi5GHDOORSzoQFwxjzTnmA58Q7ve8R1c323AJ7TrHYQI6yzEQQhzvZ9GhPwTSKdxJTBLu94YRPDPMG27nSRrNeO7/xTwqBHy0xGno7MRj7tLgN+acl9kr6PJC4j9+VJzHR2Zu1oMVtC7QBj4G5Ae6/Nm00eA4YiZZS3weBj4d4eBvymHaC/tz7EUeaBMRMa4O9k3es7/hoH/vJkcWwP8Kgz8Z0zq4gAzlg4D/x7EpPUkxJa7hX3931OdeyXirfY9xADm1nZFLkfMUeuQQBsvISa3iQyqAXBqGPi3JzQXc+6ngQfDwP8vs+1LSPCIVYgn3SLEZv+kMPDrTDsmID35RTaAZe5Y1T0zmRYy28z7XcBDxnvrYuQPvE273jhEPe8Sxv/7fiTM00Iy92JtSPCN5O+lJlLK7xEnlteQcbYiu9//F4j/wP1h4K82QSES7I842pyWtG0He+9NS/souqYNDhLoIsEEJORUcorl1kT7zL29G7g5DPyXs2izpR1W0DOTiFJT0c5ldRDi5gh7I5dchoxJzzTbd5Jb3u50/BR4ErjEuGJeQ+6z/R9HesMjwsB/E0C73lnZHBgG/kva9X4L/DbF7l1I5Jdr2+8w7UzFLCSIxAPa9Y40vXUj8Psw8L+a5piZiLZyYzZttrwfq7pn5nWkV98TOVa73sFI8IfXAMzs8N2IW+yaMPDnmqIvAGdpiWM3SLtesoNHCzLWdE2dNaRXowcD/zZC7iDBEXNlEBJg4x1zvklk8NtuTxj4V4SBvyjFrjnAuTopNp12vQO06+2fobpdyARfADxiVhPmAJ819yFRz/7a9SabCc9PIip7p2f0Cx3bo2fAqKo3A3dq1zsf6XlOR4T4oaSidwH/D/hd0rbbkEmqhIDMM+8txuf7EcR/+0tIhJl0f+I/A9dr1zsOUZWHIxNyufA80ou+pl3vXeRBlY/Ahzcj92OZmeEfhqjfH8l0kAnrNA25N3ciY/0zTT1zkbX2k5DYdb/CaA5Jw4br89D2gsIKegeEgf9t7Xp/QwIsFCPRSv/arnepQsaUdycdtws42cxYg0wubUdCEoP4jF+IjLmvQUJKJbLM3Jz0+TpkbD4ZmQxbzt4Y6Zezb0DKyzG9tuFrwLow8Ou1630QGVqESGCKo5GAEal4lvQBIv6KmUE3cxHHIA+0Keb6rjJLf+sQjSiZ65GAGYSBv9YsHbqIZvkhRNinIA/Ur4WB/6bp0dvzDjIEKE/TRks7rFNLHtCudz+SzihZxS9HZpPnIGr6lcgfeWJizd1iyYR1aulDaNcbgWQ6+V27XQ4SrHEh0rN9APiMFXJLb/D/AZL0FDEwI4RTAAAAAElFTkSuQmCC'

# Curated BFRC panel: only the 18 menu headings in the supplied reference.
import textwrap

def line(code,label,factor=1,transform="level",color=None):
    item={"code":code,"label":label,"factor":factor,"transform":transform,"color":color}
    if code=="TP.GENENDEKS.T1":item["alternates"]=["TP.TUKFIY2025.GENEL"]
    return item

CURATED_CHARTS = [
 {"id":"net-ic","name":"Net İç Varlıklar","unit":"Milyar TL","lines":[line("BFRC.DAILY.NIV","Net iç varlıklar",1e-6)]},
 {"id":"rezerv","name":"Brüt Rezervler","unit":"Milyar ABD doları","lines":[line("TP.AB.TOPLAM","Toplam",.001),line("TP.AB.C2","Döviz",.001),line("TP.AB.C1","Altın",.001)]},
 {"id":"enflasyon","name":"TÜFE–ÜFE","title":"TÜFE ve Yİ-ÜFE — Yıllık Değişim","unit":"Yıllık değişim (%)","note":"Genel endekslerden aynı ayın önceki yıl değeriyle hesaplanır.","lines":[line("TP.GENENDEKS.T1","TÜFE",1,"yoy"),line("TP.TUFE1YI.T1","Yİ-ÜFE",1,"yoy")]},
 {"id":"beklenti","name":"TÜFE Beklenti Anketi","title":"12 Ay Sonrası Yıllık TÜFE Beklentisi","unit":"%","lines":[line("TP.ENFBEK.PKA12ENF","Piyasa katılımcıları")]},
 {"id":"redk","name":"Reel Efektif Döviz Kuru","unit":"Endeks (2025=100)","lines":[line("TP.RK.T1.Y","TÜFE bazlı reel efektif döviz kuru")]},
 {"id":"ticaret","name":"Dış Ticaret Dengesi","title":"12 Ay Birikimli Mal Dengesi","unit":"Milyar ABD doları","note":"Genel ticaret sistemi. Sarı sütun: ithalat − ihracat; pozitif değer açık demektir.","lines":[line("TP.IHRISICREV4.TT","İhracat",1e-6,"rolling12"),line("TP.ITHISICREV4.TT","İthalat",1e-6,"rolling12")],"difference_bar":True},
 {"id":"fin-guven","name":"Finansal Güven Endeksi","title":"Finansal Hizmetler Güven Endeksi","unit":"Endeks","lines":[line("TP.FHGE.S01","Finansal hizmetler güven endeksi")]},
 {"id":"kapasite","name":"İmalat Sanayi Kapasite Kullanım Oranı","unit":"%","lines":[line("TP.KKO2.IS.TOP","Arındırılmamış"),line("TP.KKO.MA","Mevsimsellikten arındırılmış")]},
 {"id":"kisa-borc","name":"Kısa Vadeli Dış Borç Stoku","unit":"Milyar ABD doları","lines":[line("TP.KVDB19","Toplam",.001),line("TP.KVDB07","Bankalar",.001),line("TP.KVDB12","Diğer sektörler",.001)]},
 {"id":"konut","name":"Nominal Konut Fiyat Endeksi","unit":"Endeks","lines":[line("TP.KFE.TR","Türkiye")]},
 {"id":"faiz","name":"Kredi ve Mevduat Faizleri","unit":"Yıllık faiz (%)","note":"TL, akım, ağırlıklı ortalama. Mevduat serisi toplam vadeleri kapsar.","lines":[line("TP.KTF17","Ticari kredi"),line("TP.KTFTUK","Tüketici kredisi"),line("TP.TRY.MT06","Mevduat")]},
 {"id":"kredi","name":"Kredi Miktarı","title":"Toplam Yurt İçi Kredi Hacmi","unit":"Milyar TL","lines":[line("TP.KREHACBS.A1","Toplam kredi hacmi",1e-6)]},
 {"id":"ozel-uzun","name":"Özel Sektörün Yurt Dışından Sağladığı Uzun Vadeli Kredi Borcu","title":"Özel Sektör — Uzun Vadeli Dış Borç","unit":"Milyar ABD doları","note":"Güncel EVDS tanımı: kredi ve borç senetleri yükümlülükleri; orijinal vade.","lines":[line("TP.BRUTDBORCOZEL.G28","Uzun vadeli yükümlülükler",.001)]},
 {"id":"ozel-kisa","name":"Özel Sektörün Yurt Dışından Sağladığı Kısa Vadeli Kredi Borcu","title":"Özel Sektör — Kısa Vadeli Dış Borç","unit":"Milyar ABD doları","note":"Güncel EVDS tanımı: kredi ve borç senetleri yükümlülükleri; orijinal vade.","lines":[line("TP.BRUTDBORCOZEL.G15","Kısa vadeli yükümlülükler",.001)]},
 {"id":"para","name":"Para Arzları","unit":"Milyar TL","lines":[line("TP.HPBITABLO1.2","M1",1e-6),line("TP.HPBITABLO1.11","M2",1e-6),line("TP.HPBITABLO1.18","M3",1e-6)]},
 {"id":"mevduat","name":"Vadeli Mevduatlar","unit":"Milyar TL","lines":[line("TP.HPBITABLO1.12","TL vadeli mevduat",1e-6),line("TP.HPBITABLO1.15","YP vadeli mevduat (TL karşılığı)",1e-6)]},
 {"id":"net-dis","name":"Net Dış Varlıklar","unit":"Milyar TL","lines":[line("BFRC.DAILY.NDV","Net dış varlıklar",1e-6)]},
 {"id":"guven","name":"Reel Kesim Güven Endeksi","unit":"Endeks","lines":[line("TP.GY1.N2","Arındırılmamış"),line("TP.GY1.N2.MA","Mevsimsellikten arındırılmış")]}
]


def monthly_map(record):
    result={}
    for row in record["observations"]:
        key=row["date"][:7]
        if key in result:
            raise DataError("Aylık dönüşüm için bir ayda birden fazla gözlem var")
        result[key]=row["value"]
    return result


def previous_month(key,lag):
    year,month=map(int,key.split("-"));n=year*12+month-1-lag
    return f"{n//12:04d}-{n%12+1:02d}"


def transformed(record,spec):
    factor=spec.get("factor",1)
    if spec.get("transform","level")=="level":
        return [(r["date"],None if r["value"] is None else r["value"]*factor) for r in record["observations"]]
    monthly=monthly_map(record);result=[]
    for key in sorted(monthly):
        if spec["transform"]=="yoy":
            base=monthly.get(previous_month(key,12))
            v=100*(monthly[key]/base-1) if base not in (None,0) else None
        else:
            parts=[monthly.get(previous_month(key,k)) for k in range(12)]
            v=sum(parts) if all(x is not None for x in parts) else None
        result.append((key+"-01",None if v is None else v*factor))
    return result


def curated_figure(chart,records,demo=False):
    if chart.get("labour_snapshot"):return labour_snapshot_figure(chart)
    candidates=[[s["code"]]+s.get("alternates",[]) for s in chart["lines"]]
    if not all(any(c in records for c in group) for group in candidates):
        raise DataError("Bu gösterge için veri henüz alınamadı")
    requested=list(dict.fromkeys(c for group in candidates for c in group if c in records))
    colors=["#668ba5","#8da66e","#c79136"]
    fig=go.Figure();series=[];latest_dates=[]
    for n,spec in enumerate(chart["lines"]):
        combined={}
        for code in candidates[n]:
            if code not in records:continue
            for d,v in transformed(records[code],spec):
                # Compute each index's YoY on its own base before combining percentages.
                # Never concatenate index levels across a change of base year.
                if v is not None or d not in combined:combined[d]=v
        points=sorted(combined.items())
        valid=[(d,v) for d,v in points if v is not None]
        if not valid:
            raise DataError("Hesaplama için yeterli dönem yok")
        latest_dates.append(valid[-1][0]);series.append(dict(points))
        common=dict(x=[d for d,v in points],y=[v for d,v in points],name=spec["label"],
            hovertemplate="%{x|%d.%m.%Y}<br>%{y:,.2f} "+escape(chart["unit"])+"<extra>%{fullData.name}</extra>")
        color=spec.get("color") or colors[n%len(colors)]
        if chart.get('observation_days_only'):
            common.update(x=[d for d,v in valid],y=[v for d,v in valid])
        if chart.get('daily_bars'):
            if chart.get('monthly_bars'):
                fig.add_trace(go.Bar(**common,width=20*86400000,
                    marker_color=['#974b63' if v is not None and v<0 else '#468698' for v in common['y']]))
            else:
                fig.add_trace(go.Bar(**common,marker_color=color))
        else:
            extra={'fill':'tozeroy','fillcolor':'rgba(67,36,84,0.18)'} if chart.get('area') else {}
            fig.add_trace(go.Scatter(**common,mode='lines',connectgaps=False,
                line={'color':color,'width':2 if chart.get('area') else 3},**extra))
    if chart.get("difference_bar"):
        common=sorted(series[0].keys()&series[1].keys())
        common=[d for d in common if series[0][d] is not None and series[1][d] is not None]
        if not common:
            raise DataError("Dış ticaret hesabı için ortak dönem yok")
        fig.add_trace(go.Bar(x=common,y=[series[1][d]-series[0][d] for d in common],
            name="Dış ticaret açığı",marker_color="#ffbc00",opacity=.95,
            hovertemplate="%{x|%Y-%m}<br>Açık: %{y:,.2f} milyar ABD doları<extra></extra>"))
    title=chart.get("title",chart["name"])
    title="<br>".join(escape(x) for x in textwrap.wrap(title,62))
    last=min(latest_dates);latest=date.fromisoformat(last)
    earliest=min(d for values in series for d,v in values.items() if v is not None)
    fig.update_layout(template="none",height=760,paper_bgcolor="#eff6fa",plot_bgcolor="#eff6fa",
        font={"family":"Arial, sans-serif","size":16,"color":"#102536"},
        title={"text":"<b>"+title+"</b><br><span style='font-size:18px;font-weight:700'>"+escape(chart["unit"])+" · Son gözlem: "+latest.strftime("%d.%m.%Y")+"</span>",
               "font":{"family":"Courier New, monospace","size":27},"x":.025,"y":.97,"xanchor":"left"},
        margin={"l":30,"r":85,"t":110,"b":190},hovermode="x unified",bargap=.32,
        legend={"orientation":"h","x":.5,"xanchor":"center","y":-.17,
                "font":{"size":16,"family":"Arial Black, Arial, sans-serif"}},
        xaxis={"type":"date","range":[earliest,last],"showgrid":False,"showline":True,
               "linecolor":"#344654","linewidth":1,"tickformat":"%Y-%m","dtick":"M6",
               "tickangle":-45,"ticks":"outside","tickfont":{"size":16,"family":"Arial Black, Arial, sans-serif"},"fixedrange":False},
        yaxis={"side":"right","showgrid":True,"gridcolor":"rgba(44,66,83,0.35)",
               "griddash":"dot","zeroline":True,"zerolinecolor":"#7d8f9b",
               "tickformat":",.1f","tickfont":{"size":17,"family":"Arial Black, Arial, sans-serif"},"showline":False,"fixedrange":False})
    if chart.get('legend_top'):
        fig.update_layout(showlegend=True,legend={'y':1.02,'yanchor':'bottom','x':0,'xanchor':'left'},
                          margin={'t':155})
    if chart.get("daily_bars"):
        padding=12 if chart.get("monthly_bars") else 1
        fig.update_xaxes(range=[(date.fromisoformat(earliest)-timedelta(days=padding)).isoformat(),
                                (latest+timedelta(days=padding)).isoformat()])
    if chart.get('observation_days_only'):
        observed={d for values in series for d,v in values.items() if v is not None}
        day=date.fromisoformat(min(observed))
        missing=[]
        while day<latest:
            if day.isoformat() not in observed:missing.append(day.isoformat())
            day+=timedelta(days=1)
        fig.update_xaxes(rangebreaks=[{'values':missing,'dvalue':86400000}] if missing else [])
    fig.add_layout_image(source=CURATED_LOGO,xref="paper",yref="paper",x=.5,y=.53,
        xanchor="center",yanchor="middle",sizex=.75,sizey=.90,sizing="contain",opacity=.15,layer="below")
    labels=[(chart.get("source_label") or "Kaynak: TCMB / EVDS"+(" / London Fix" if chart["id"] in ("net-altin-degisim","net-altin") else ""),-109),
            ("Grafik: Mehmet Faruk Bekir",-129)]
    footer_shift=-105
    for text,_ in labels:
        wrapped=textwrap.wrap(text,width=62) or [text]
        fig.add_annotation(text="<b>"+"<br>".join(escape(part) for part in wrapped)+"</b>",
            x=0,y=0,xref="paper",yref="paper",yshift=footer_shift,
            xanchor="left",yanchor="top",align="left",showarrow=False,
            font={"family":"Courier New, monospace","size":20 if text.startswith("Grafik:") else 17,"color":"#102536"})
        footer_shift-=len(wrapped)*26+10
    fig.update_layout(margin={"b":210})
    warnings=[]
    if demo:warnings.append("DEMO — SENTETİK VERİ")
    if any(records[c].get("fallback") for c in requested):warnings.append("SON BAŞARILI VERİ — API YENİLEMESİ BAŞARISIZ")
    if warnings:
        fig.add_annotation(text=" · ".join(warnings),x=.5,y=1,xref="paper",yref="paper",
            yshift=15,showarrow=False,font={"size":12,"color":"#a65b0b"})
    return fig


def period_intersects(day,start,end,frequency):
    """A quarterly label denotes a whole quarter, not just its first day."""
    observed=date.fromisoformat(day)
    freq=unicodedata.normalize('NFKD',frequency.lower().replace('ı','i')).encode('ascii','ignore').decode()
    if 'uc aylik' in freq or 'quarter' in freq:
        first=date(observed.year,((observed.month-1)//3)*3+1,1)
        last=(date(first.year+1,1,1) if first.month==10 else date(first.year,first.month+3,1))-timedelta(days=1)
    elif freq in ('aylik','monthly'):
        first=observed.replace(day=1)
        last=(date(first.year+1,1,1) if first.month==12 else date(first.year,first.month+1,1))-timedelta(days=1)
    elif freq in ('yillik','yearly','annual'):
        first=date(observed.year,1,1);last=date(observed.year,12,31)
    else:first=last=observed
    return first<=end and last>=start


def is_daily_spec(spec):
    text=unicodedata.normalize('NFKD',spec.get('frequency','').lower().replace('ı','i')).encode('ascii','ignore').decode()
    return 'gun' in text or text in ('daily','business daily','d')


def reusable_after_release(record,spec,now):
    """Reuse today's fetch only until an intraday publication checkpoint is crossed."""
    if not record or record.get('provider')!='evds3' or record.get('code')!=spec['code']:
        return False
    try:
        fetched=datetime.fromisoformat(record['fetched_at']).astimezone(TR)
    except (KeyError,ValueError,TypeError):return False
    now=now.astimezone(TR)
    if fetched.date()!=now.date() or fetched>now:return False
    weekly='HAFTALIK' in spec.get('frequency','').upper()
    if weekly and now.weekday()!=3:return True
    if weekly:
        checkpoints=[(14,30)]
        if spec['group_code'] in ('bie_kt100h','bie_mt100h'):checkpoints.append((17,0))
    elif spec['group_code']=='bie_dkdovytl':checkpoints=[(10,0),(14,30),(15,30)]
    elif spec['group_code'] in ('bie_abanlbil','bie_swaptektarf'):checkpoints=[(10,0),(14,30),(17,0)]
    else:checkpoints=[(10,0),(14,30)]
    return not any(fetched<now.replace(hour=h,minute=m,second=0,microsecond=0)<=now for h,m in checkpoints)


def fetch_curated_group(specs,today):
    # Fetch only the few codes used by the curated charts, never all group subitems.
    buckets={s["code"]:[] for s in specs};periods={s["code"]:{} for s in specs}
    print("EVDS alınıyor:",specs[0]["group"],flush=True)
    with session() as client:
        start=date(2019,1,1)
        while start<=today:
            end=min(today,start+timedelta(days=899))  # below EVDS's 1000-observation cap
            result=evds3_json(client,"",{"series":"-".join(buckets),
                "startDate":start.strftime("%d-%m-%Y"),"endDate":end.strftime("%d-%m-%Y"),"type":"json"})
            if not isinstance(result,dict) or not isinstance(result.get("items"),list):
                raise DataError("EVDS veri yanıtı doğrulanamadı")
            for row in result["items"]:
                d=period_date(row["Tarih"])
                if not period_intersects(d,start,end,specs[0].get('frequency','')):
                    # Ignore non-overlapping observations, retaining valid quarterly boundaries.
                    continue
                for code in buckets:
                    field=code.replace(".","_")
                    if field in row:
                        buckets[code].append({"date":d,"value":row[field]})
                        periods[code][d]=str(row["Tarih"])
            print("  ",start,end,"tamam",flush=True)
            start=end+timedelta(days=1)
    result={};stamp=datetime.now(TR).isoformat(timespec="seconds")
    for spec in specs:
        try:rows=validate(buckets[spec["code"]],today)
        except DataError:continue
        result[spec["code"]]={**spec,"observations":rows,"periods":periods[spec["code"]],"fetched_at":stamp}
    if not result:raise DataError("Seçili serilerde kullanılabilir veri yok")
    return result


def write_curated(output,records,errors,stamp,demo=False):
    output.mkdir(exist_ok=True);(output/"charts").mkdir(exist_ok=True)
    (output/"plotly.min.js").write_text(get_plotlyjs(),encoding="utf-8")
    choices=[];available=0
    for chart in CURATED_CHARTS:
        item={"method":chart.get("method",""),"default_years":chart.get("default_years",5),"id":chart["id"],"name":chart["name"],"unit":chart["unit"],"file":"charts/"+chart["id"]+".json"}
        try:
            fig=curated_figure(chart,records,demo)
            (output/item["file"]).write_text(pio.to_json(fig),encoding="utf-8");available+=1
        except DataError as error:
            item["error"]=str(error)
        choices.append(item)
    (output/"panel-data.json").write_text(json.dumps({"choices":choices},ensure_ascii=False),encoding="utf-8")
    (output/"status.json").write_text(json.dumps({"built_at":stamp,"demo":demo,
        "menu_count":len(CURATED_CHARTS),"available_charts":available,"errors":errors},ensure_ascii=False,indent=2),encoding="utf-8")
    warning='<p class="notice">DEMO — Sentetik değerler; gerçek veri değildir.</p>' if demo else ''
    if errors:
        warning+='<details class="notice"><summary>Bazı veriler yenilenemedi</summary><ul>'+''.join('<li>'+escape(e)+'</li>' for e in errors)+'</ul></details>'
    html='''<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BFRC Güncel Grafikler</title><script src="plotly.min.js"></script><style>
*{box-sizing:border-box}body{margin:0;background:white;color:#173248;font:15px Arial,sans-serif}
main{max-width:1240px;margin:auto;padding:20px 12px}header{display:flex;align-items:center;gap:16px;margin-bottom:20px}
header img{width:105px;height:auto}h1{font-size:22px;margin:0;font-weight:600}header p{font-size:12px;margin:6px 0;color:#667c8e}
select{width:100%;padding:13px;border:1px solid #bccbd5;border-radius:5px;background:#fff;color:#173248;font-size:15px}
label{display:block;font-size:12px;font-weight:bold;margin:0 0 7px}.toolbar{display:flex;justify-content:flex-end;gap:8px;margin:12px 0}
button{background:#eff6fa;border:1px solid #cbd7df;border-radius:4px;padding:7px 12px;color:#284c64;cursor:pointer}button.active{background:#173e5b;color:white}
#wrap{overflow:auto;background:#eff6fa;border-radius:4px}#chart{min-width:650px}.notice{background:#fff2d3;padding:10px;font-size:12px}
#message{padding:30px;font-size:14px}footer{font-size:11px;color:#6e8291;padding-top:12px}
</style></head><body><main><header><img src="LOGO" alt="BFRC"><div><h1>Güncel Grafikler</h1><p>Bahçeşehir Üniversitesi Finansal Araştırma ve Uygulama Merkezi</p></div></header>WARNING
<label for="series">GÖSTERGE</label><select id="series" disabled><option>Yükleniyor…</option></select>
<div class="toolbar"><button data-years="0.5">6 ay</button><button data-years="1">1 yıl</button><button data-years="3">3 yıl</button><button data-years="5" class="active">5 yıl</button><button data-years="0">Tümü</button></div>
<p id="stale" class="notice" hidden>Panel 24 saattir yenilenmemiş. Son veri tarihlerini kontrol edin.</p>
<div id="wrap"><div id="message" role="status">Grafik yükleniyor…</div><div id="chart"></div></div><footer>Panel güncellemesi: STAMP</footer></main><script>
const built=STAMP_JSON,select=document.getElementById('series'),msg=document.getElementById('message'),graph=document.getElementById('chart');
let generation=0,currentFigure=null,currentYears=5;
function age(){document.getElementById('stale').hidden=Date.now()-new Date(built).getTime()<86400000;}age();setInterval(age,60000);
function applyRange(){if(!currentFigure)return;const snapshot=Boolean(currentFigure.layout.meta?.labour_snapshot);document.querySelectorAll('[data-years]').forEach(b=>b.disabled=snapshot);if(snapshot)return;const dates=currentFigure.data.flatMap(t=>t.x.filter((d,i)=>d&&typeof t.y[i]==='number'&&Number.isFinite(t.y[i]))).sort();if(!dates.length)return;const end=dates[dates.length-1];
const start=new Date(end+'T12:00:00Z');start.setUTCMonth(start.getUTCMonth()-Math.round(currentYears*12));const begin=currentYears===0?dates[0]:start.toISOString().slice(0,10);
const values=[];for(const t of currentFigure.data){for(let i=0;i<t.x.length;i++){if(t.x[i]>=begin&&t.x[i]<=end&&typeof t.y[i]==='number'&&Number.isFinite(t.y[i]))values.push(t.y[i]);}
if(t.type==='bar'||t.fill==='tozeroy')values.push(0);}
const hasBars=currentFigure.data.some(t=>t.type==='bar');
const padded=(day,offset)=>new Date(new Date(day+'T00:00:00Z').getTime()+offset*86400000).toISOString().slice(0,10);
const padDays=currentFigure.data.some(t=>t.type==='bar'&&t.width>=20*86400000)?12:1;
const axisRange=hasBars?[padded(begin,-padDays),padded(end,padDays)]:[begin,end];
const update={'xaxis.autorange':false,'xaxis.range':axisRange,'xaxis.dtick':currentYears>0&&currentYears<=1?'M1':currentYears===3?'M3':'M6'};
if(values.length){const low=Math.min(...values),high=Math.max(...values),pad=(high-low||Math.abs(high)||1)*.08;update['yaxis.autorange']=false;update['yaxis.range']=[low-pad,high+pad];}
Plotly.relayout(graph,update);}
document.querySelectorAll('[data-years]').forEach(b=>b.addEventListener('click',()=>{currentYears=Number(b.dataset.years);
document.querySelectorAll('[data-years]').forEach(x=>x.classList.toggle('active',x===b));applyRange();}));
fetch('panel-data.json?v='+encodeURIComponent(built)).then(r=>{if(!r.ok)throw Error();return r.json()}).then(panel=>{
select.replaceChildren();for(const c of panel.choices){const o=document.createElement('option');o.value=c.id;o.textContent=c.name;select.append(o);}
async function draw(){const token=++generation,id=select.value,c=panel.choices.find(x=>x.id===id);currentFigure=null;
let note=document.getElementById('gold-method');if(!note){note=document.createElement('p');note.id='gold-method';note.style.cssText='font:13px Arial;line-height:1.5;color:#354c5e';graph.parentNode.insertBefore(note,graph);}note.textContent=c.method||'';note.hidden=!c.method;
msg.hidden=false;msg.textContent='Grafik yükleniyor…';graph.hidden=true;
if(c.error){msg.textContent='Bu gösterge şu an hazır değil: '+c.error;return;}
try{const reply=await fetch(c.file+'?v='+encodeURIComponent(built));if(!reply.ok)throw Error();const fig=await reply.json();if(token!==generation)return;
graph.hidden=false;await Plotly.react(graph,fig.data,fig.layout,{responsive:true,displaylogo:false,toImageButtonOptions:{format:'png',filename:'BFRC-'+id,width:1600,height:950,scale:2}});
if(token===generation){currentFigure=fig;currentYears=c.default_years??5;document.querySelectorAll('[data-years]').forEach(b=>b.classList.toggle('active',Number(b.dataset.years)===currentYears));msg.hidden=true;applyRange();}}
catch(e){if(token===generation)msg.textContent='Grafik yüklenemedi. Sayfayı yenileyin veya başka gösterge seçin.';}}
select.disabled=false;select.addEventListener('change',draw);draw();
}).catch(()=>{msg.textContent='Veriler yüklenemedi. Sayfayı yenileyin.';});
</script></body></html>'''
    html=html.replace('LOGO',CURATED_LOGO).replace('WARNING',warning).replace('STAMP_JSON',json.dumps(stamp)).replace('STAMP',escape(stamp))
    (output/'index.html').write_text(html,encoding='utf-8')
    if GOLD_ADJUSTED_CODE in records:
        (output/"gold-adjustment-audit.json").write_text(json.dumps(records[GOLD_ADJUSTED_CODE]["audit"],ensure_ascii=False,indent=2),encoding="utf-8")
    return available


def curated_demo(today):
    # Synthetic monthly fixtures, labelled in all graphs, never written into the live cache.
    result={}
    for n,(code,spec) in enumerate(CURATED_SPECS.items()):
        rows=[]
        for year in range(2019,today.year+1):
            for month in range(1,13):
                d=date(year,month,1)
                if d>today:continue
                k=(year-2019)*12+month
                v=100+k*.7+10*math.sin(k/7+n)
                if spec['unit'].lower().find('bin')>=0:v*=1e6
                elif 'milyon' in spec['unit'].lower():v*=1000
                rows.append({'date':d.isoformat(),'value':v})
        result[code]={**spec,'observations':rows,'fetched_at':datetime.now(TR).isoformat(timespec='seconds')}
    return result


def build_curated(demo=False):
    today=datetime.now(TR).date();stamp=datetime.now(TR).isoformat(timespec='seconds')
    if demo:
        records=demo_with_fx(today)
        for i,(code,_,_) in enumerate(CONSTRUCTION_SERIES):
            records[code]={'observations':[{'date':f'{y}-{m:02d}-01','value':round(4*math.sin((y*12+m)/(3+i*6)),2)} for y in range(2021,today.year+1) for m in range(1,13) if date(y,m,1)<=today]}
        n=write_curated(ROOT/'demo',records,[],stamp,True)
        print(f'{n} demo grafik üretildi');return
    path=ROOT/'state/cache.json';cache=json.loads(path.read_text()) if path.exists() else {}
    previous=cache.get('_panel',{});records={};errors=[];groups={}
    for code,spec in CURATED_SPECS.items():groups.setdefault(spec['group_code'],[]).append(spec)
    def load(items):
        prior=[previous.get(s['code']) for s in items]
        if not any(is_daily_spec(s) for s in items) and all(reusable_after_release(r,s,datetime.now(TR)) for r,s in zip(prior,items)):
            return {s['code']:{**r,**s} for r,s in zip(prior,items)}
        return fetch_curated_group(items,today)
    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks={executor.submit(load,items):items for items in groups.values()}
        for task in as_completed(tasks):
            items=tasks[task]
            try:
                current=task.result();records.update(current)
                missing=[s for s in items if s['code'] not in current]
                if missing:errors.append(items[0]['group']+f': {len(missing)} seçili seride veri yok')
            except (DataError,KeyError,TypeError) as error:
                errors.append(items[0]['group']+': '+str(error));missing=items
            for s in missing:
                old=previous.get(s['code'])
                if old and old.get('provider')=='evds3':records[s['code']]={**old,**s,'fallback':True}
    if not records:raise DataError('EVDS verisi alınamadı. '+'; '.join(errors[:4]))
    attach_daily_fx(records,cache,errors,datetime.now(TR))
    attach_analytic_daily(records,errors)
    attach_gold_adjustment(records,cache,errors,today)
    apply_excel_gold_archive(records,today)
    attach_construction(records,previous,errors,today)
    output=ROOT/'dist';count=write_curated(output,records,errors,stamp)
    if not count:raise DataError('Seçili göstergeler için grafik üretilemedi')
    cache['_panel']={k:{a:v for a,v in r.items() if a!='fallback'} for k,r in records.items()}
    path.parent.mkdir(exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8');tmp.replace(path)
    if all(r.get('fallback') for r in records.values()):(output/'all_failed.flag').write_text('All APIs failed')
    else:(output/'all_failed.flag').unlink(missing_ok=True)
    print(f'{len(CURATED_CHARTS)} menü başlığı / {count} hazır grafik',flush=True)
    for error in errors:print('UYARI:',error,flush=True)


FX_SPECS={'TP.AB.A02': {'id': 'TP.AB.A02', 'code': 'TP.AB.A02', 'name': 'A.1 DIŞ VARLIKLAR', 'group_code': 'bie_abanlbil', 'group': 'Merkez Bankası Analitik Bilanço', 'unit': 'Bin TL', 'frequency': 'İŞ GÜNÜ', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.AB.A10': {'id': 'TP.AB.A10', 'code': 'TP.AB.A10', 'name': 'P.1 TOPLAM DÖVİZ YÜKÜMLÜLÜKLERİ', 'group_code': 'bie_abanlbil', 'group': 'Merkez Bankası Analitik Bilanço', 'unit': 'Bin TL', 'frequency': 'İŞ GÜNÜ', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.AB.A13': {'id': 'TP.AB.A13', 'code': 'TP.AB.A13', 'name': 'P.1ba Kamu ve Diğer Döviz Mevduatı', 'group_code': 'bie_abanlbil', 'group': 'Merkez Bankası Analitik Bilanço', 'unit': 'Bin TL', 'frequency': 'İŞ GÜNÜ', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.DK.USD.A.YTL': {'id': 'TP.DK.USD.A.YTL', 'code': 'TP.DK.USD.A.YTL', 'name': '(USD)  ABD  Doları  (Döviz  Alış)', 'group_code': 'bie_dkdovytl', 'group': 'Döviz Kurları', 'unit': 'TL / ABD doları', 'frequency': 'İŞ GÜNÜ', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.SWAPTEKTAR.TOTALSTOKALIMYONLU': {'id': 'TP.SWAPTEKTAR.TOTALSTOKALIMYONLU', 'code': 'TP.SWAPTEKTAR.TOTALSTOKALIMYONLU', 'name': '19.TOPLAM - STOK (Alım Yönlü)', 'group_code': 'bie_swaptektarf', 'group': 'TCMB Taraflı Swap İşlemleri', 'unit': 'Milyon ABD doları', 'frequency': 'İŞ GÜNÜ', 'source': 'TCMB / EVDS', 'provider': 'evds3'}, 'TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU': {'id': 'TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU', 'code': 'TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU', 'name': '20.TOPLAM - STOK (Satım Yönlü)', 'group_code': 'bie_swaptektarf', 'group': 'TCMB Taraflı Swap İşlemleri', 'unit': 'Milyon ABD doları', 'frequency': 'İŞ GÜNÜ', 'source': 'TCMB / EVDS', 'provider': 'evds3'}}
FX_WEEKLY_SEEDS=[{'date': '2023-12-29', 'total': -72457.0, 'domestic': -48746.0}, {'date': '2024-01-05', 'total': -71815.0, 'domestic': -48366.0}, {'date': '2024-01-12', 'total': -68825.0, 'domestic': -45386.0}, {'date': '2024-01-19', 'total': -65501.0, 'domestic': -42147.0}, {'date': '2024-01-26', 'total': -74342.0, 'domestic': -51099.0}, {'date': '2024-02-02', 'total': -72808.0, 'domestic': -49587.0}, {'date': '2024-02-09', 'total': -72448.0, 'domestic': -49259.0}, {'date': '2024-02-16', 'total': -70344.0, 'domestic': -47220.0}, {'date': '2024-02-23', 'total': -68651.0, 'domestic': -45418.0}, {'date': '2024-03-01', 'total': -68293.0, 'domestic': -45055.0}, {'date': '2024-03-08', 'total': -74747.0, 'domestic': -51356.0}, {'date': '2024-03-15', 'total': -79307.0, 'domestic': -55969.0}, {'date': '2024-03-22', 'total': -80307.0, 'domestic': -57080.0}, {'date': '2024-03-29', 'total': -80991.0, 'domestic': -57898.0}, {'date': '2024-04-05', 'total': -76799.0, 'domestic': -53569.0}, {'date': '2024-04-09', 'total': -75617.0, 'domestic': -52336.0}, {'date': '2024-04-19', 'total': -67640.0, 'domestic': -44495.0}, {'date': '2024-04-26', 'total': -62574.0, 'domestic': -39586.0}, {'date': '2024-05-03', 'total': -59865.0, 'domestic': -36813.0}, {'date': '2024-05-10', 'total': -54490.0, 'domestic': -31398.0}, {'date': '2024-05-17', 'total': -48650.0, 'domestic': -25355.0}, {'date': '2024-05-24', 'total': -45474.0, 'domestic': -22264.0}, {'date': '2024-05-31', 'total': -43971.0, 'domestic': -20811.0}, {'date': '2024-06-07', 'total': -41573.0, 'domestic': -18300.0}, {'date': '2024-06-14', 'total': -35514.0, 'domestic': -12496.0}, {'date': '2024-06-21', 'total': -32181.0, 'domestic': -9093.0}, {'date': '2024-06-28', 'total': -27829.0, 'domestic': -4719.0}, {'date': '2024-07-05', 'total': -25422.0, 'domestic': -2322.0}, {'date': '2024-07-12', 'total': -25475.0, 'domestic': -2351.0}, {'date': '2024-07-19', 'total': -25242.0, 'domestic': -2051.0}, {'date': '2024-07-26', 'total': -23186.0, 'domestic': -55.0}, {'date': '2024-08-02', 'total': -22496.0, 'domestic': 180.0}, {'date': '2024-08-09', 'total': -22338.0, 'domestic': 0.0}, {'date': '2024-08-16', 'total': -22087.0, 'domestic': 225.0}, {'date': '2024-08-23', 'total': -22311.0, 'domestic': 0.0}, {'date': '2024-08-29', 'total': -22339.0, 'domestic': 0.0}, {'date': '2024-09-06', 'total': -22332.0, 'domestic': 0.0}, {'date': '2024-09-13', 'total': -22324.0, 'domestic': 0.0}, {'date': '2024-09-20', 'total': -21862.0, 'domestic': 500.0}, {'date': '2024-09-27', 'total': -21303.0, 'domestic': 1095.0}, {'date': '2024-10-04', 'total': -21468.0, 'domestic': 970.0}, {'date': '2024-10-11', 'total': -21107.0, 'domestic': 1300.0}, {'date': '2024-10-18', 'total': -20114.0, 'domestic': 2262.0}, {'date': '2024-10-25', 'total': -17789.0, 'domestic': 4570.0}, {'date': '2024-11-01', 'total': -15495.0, 'domestic': 6913.0}, {'date': '2024-11-08', 'total': -15584.0, 'domestic': 6808.0}, {'date': '2024-11-15', 'total': -14955.0, 'domestic': 7375.0}, {'date': '2024-11-22', 'total': -19204.0, 'domestic': 3081.0}, {'date': '2024-11-29', 'total': -18481.0, 'domestic': 3875.0}, {'date': '2024-12-06', 'total': -16735.0, 'domestic': 5561.0}, {'date': '2024-12-13', 'total': -15461.0, 'domestic': 6822.0}, {'date': '2024-12-20', 'total': -15330.0, 'domestic': 6963.0}, {'date': '2024-12-27', 'total': -19906.0, 'domestic': 2392.0}, {'date': '2025-01-03', 'total': -21043.0, 'domestic': 1281.0}, {'date': '2025-01-10', 'total': -15060.0, 'domestic': 7199.0}, {'date': '2025-01-17', 'total': -12027.0, 'domestic': 10223.0}, {'date': '2025-01-24', 'total': -10695.0, 'domestic': 11588.0}, {'date': '2025-01-31', 'total': -6464.0, 'domestic': 13131.0}, {'date': '2025-02-07', 'total': -7233.0, 'domestic': 12295.0}, {'date': '2025-02-14', 'total': -6806.0, 'domestic': 12788.0}, {'date': '2025-02-21', 'total': -6310.0, 'domestic': 13309.0}, {'date': '2025-02-28', 'total': -7720.0, 'domestic': 12012.0}, {'date': '2025-03-07', 'total': -8515.0, 'domestic': 11156.0}, {'date': '2025-03-14', 'total': -8495.0, 'domestic': 11164.0}, {'date': '2025-03-21', 'total': -9895.0, 'domestic': 9775.0}, {'date': '2025-03-28', 'total': -18346.0, 'domestic': 1401.0}, {'date': '2025-04-04', 'total': -17572.0, 'domestic': 2124.0}, {'date': '2025-04-11', 'total': -18082.0, 'domestic': 1616.0}, {'date': '2025-04-18', 'total': -18202.0, 'domestic': 1508.0}, {'date': '2025-04-25', 'total': -18581.0, 'domestic': 1178.0}, {'date': '2025-05-02', 'total': -19005.0, 'domestic': 576.0}, {'date': '2025-05-09', 'total': -19473.0, 'domestic': 123.0}, {'date': '2025-05-16', 'total': -19592.0, 'domestic': 10.0}, {'date': '2025-05-23', 'total': -19563.0, 'domestic': 23.0}, {'date': '2025-05-30', 'total': -18954.0, 'domestic': 336.0}, {'date': '2025-06-05', 'total': -19619.0, 'domestic': 0.0}, {'date': '2025-06-13', 'total': -19200.0, 'domestic': 269.0}, {'date': '2025-06-20', 'total': -17947.0, 'domestic': 1654.0}, {'date': '2025-06-27', 'total': -17931.0, 'domestic': 1559.0}, {'date': '2025-07-04', 'total': -19205.0, 'domestic': 316.0}, {'date': '2025-07-11', 'total': -19081.0, 'domestic': 833.0}, {'date': '2025-07-18', 'total': -18440.0, 'domestic': 1056.0}, {'date': '2025-07-25', 'total': -17793.0, 'domestic': 1732.0}, {'date': '2025-08-01', 'total': -17594.0, 'domestic': 2033.0}, {'date': '2025-08-08', 'total': -17848.0, 'domestic': 1622.0}, {'date': '2025-08-15', 'total': -17553.0, 'domestic': 1915.0}, {'date': '2025-08-22', 'total': -17014.0, 'domestic': 2412.0}, {'date': '2025-08-29', 'total': -16683.0, 'domestic': 2738.0}, {'date': '2025-09-05', 'total': -16742.0, 'domestic': 2645.0}, {'date': '2025-09-12', 'total': -17578.0, 'domestic': 1872.0}, {'date': '2025-09-19', 'total': -17117.0, 'domestic': 2365.0}, {'date': '2025-09-26', 'total': -15383.0, 'domestic': 4069.0}, {'date': '2025-10-03', 'total': -15562.0, 'domestic': 3817.0}, {'date': '2025-10-10', 'total': -17272.0, 'domestic': 2133.0}, {'date': '2025-10-17', 'total': -16126.0, 'domestic': 3227.0}, {'date': '2025-10-24', 'total': -15614.0, 'domestic': 3982.0}, {'date': '2025-10-31', 'total': -15000.0, 'domestic': 3065.0}, {'date': '2025-11-07', 'total': -14748.0, 'domestic': 2690.0}, {'date': '2025-11-14', 'total': -17000.0, 'domestic': 3145.0}, {'date': '2025-11-21', 'total': -14156.0, 'domestic': 3326.0}, {'date': '2025-11-28', 'total': -14393.0, 'domestic': 3198.0}, {'date': '2025-12-05', 'total': -15350.0, 'domestic': 2091.0}, {'date': '2025-12-12', 'total': -13429.0, 'domestic': 4312.0}, {'date': '2025-12-19', 'total': -13682.0, 'domestic': 3533.0}, {'date': '2025-12-26', 'total': -12250.0, 'domestic': 4904.0}, {'date': '2026-01-02', 'total': -14272.0, 'domestic': 3104.0}, {'date': '2026-01-09', 'total': -12826.0, 'domestic': 4519.0}, {'date': '2026-01-16', 'total': -12237.0, 'domestic': 4901.0}, {'date': '2026-01-23', 'total': -11900.0, 'domestic': 5146.0}, {'date': '2026-01-30', 'total': -10756.0, 'domestic': 5981.0}, {'date': '2026-02-06', 'total': -13532.0, 'domestic': 3912.0}, {'date': '2026-02-13', 'total': -14304.0, 'domestic': 2989.0}, {'date': '2026-02-20', 'total': -13579.0, 'domestic': 3658.0}, {'date': '2026-02-27', 'total': -12941.0, 'domestic': 3417.0}, {'date': '2026-03-06', 'total': -13762.0, 'domestic': 2646.0}, {'date': '2026-03-12', 'total': -13689.0, 'domestic': 1683.0}, {'date': '2026-03-19', 'total': -9218.0, 'domestic': 2581.0}, {'date': '2026-03-27', 'total': -14881.0, 'domestic': 2416.0}, {'date': '2026-04-03', 'total': -27262.0, 'domestic': -10589.0}, {'date': '2026-04-10', 'total': -23436.0, 'domestic': -6869.0}, {'date': '2026-04-17', 'total': -18668.0, 'domestic': -2463.0}, {'date': '2026-04-24', 'total': -17839.0, 'domestic': -1519.0}, {'date': '2026-04-30', 'total': -17397.0, 'domestic': -878.0}, {'date': '2026-05-08', 'total': -15848.0, 'domestic': 444.0}, {'date': '2026-05-15', 'total': -14941.0, 'domestic': 1643.0}, {'date': '2026-05-26', 'total': -19212.0, 'domestic': -2569.64531}, {'date': '2026-06-05', 'total': -17404.0, 'domestic': -789.68893}, {'date': '2026-06-12', 'total': -15981.0, 'domestic': 1080.88879}, {'date': '2026-06-19', 'total': -17408.0, 'domestic': -431.47822999999994}, {'date': '2026-06-26', 'total': -14662.0, 'domestic': 2123.94172}, {'date': '2026-07-03', 'total': -15045.0, 'domestic': 1369.4628400000001}, {'date': '2026-07-10', 'total': -13741.0, 'domestic': 2739.63}, {'date': '2026-07-17', 'total': -13478.0, 'domestic': 3197.14962}, {'date': '2026-07-24', 'total': -12819.0, 'domestic': 3589.82}, {'date': '2026-07-31', 'total': -13349.0, 'domestic': 3245.19}, {'date': '2026-08-07', 'total': -12421.0, 'domestic': 3798.92639}, {'date': '2026-08-14', 'total': -12859.0, 'domestic': 3479.52857}, {'date': '2026-08-21', 'total': -10987.0, 'domestic': 5138.09}, {'date': '2026-08-28', 'total': -10723.0, 'domestic': 5784.81961}, {'date': '2026-09-04', 'total': -12242.0, 'domestic': 4337.65}, {'date': '2026-09-11', 'total': -12286.0, 'domestic': 4270.36841}, {'date': '2026-09-18', 'total': -12703.0, 'domestic': 3789.69}, {'date': '2026-09-25', 'total': -13484.0, 'domestic': 3014.106}, {'date': '2026-10-02', 'total': -15933.0, 'domestic': 599.49993}]
import io
import zipfile
import xml.etree.ElementTree as ET
from html import unescape
from urllib.parse import urljoin, urlsplit

FX_LEVEL_CODE='BFRC.NETFX.SWAPEX'
FX_CHANGE_CODE='BFRC.NETFX.SWAPEX.CHANGE'
FX_RAW=['TP.AB.A02','TP.AB.A10','TP.AB.A13','TP.DK.USD.A.YTL',
        'TP.SWAPTEKTAR.TOTALSTOKALIMYONLU','TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU']
CURATED_SPECS.update(FX_SPECS)
CURATED_CHARTS[:0]=[
 {'id':'netfx-gunluk','name':'Swap Hariç Net Döviz Pozisyonu',
  'title':'TCMB Swap Hariç Net Döviz Pozisyonu','unit':'Milyar ABD doları',
  'note':'Hazine döviz mevduatı dahildir. Geçmiş seri BFRC arşivinden; yeni günler API verilerinden hesaplanır.',
  'default_years':3,'area':True,'lines':[line(FX_LEVEL_CODE,'Swap hariç net döviz pozisyonu',.001,color='#432454')]},
 {'id':'netfx-degisim','name':'Swap ve Hazine Hariç Net Döviz Pozisyonu Günlük Değişimi',
  'title':'TCMB Swap ve Hazine Hariç Net Döviz Pozisyonu Günlük Değişimi','unit':'Milyon ABD doları',
  'note':'Hazine hariç pozisyonun önceki iş gününe göre değişimi. Kur ve değerleme etkilerini içerir.',
  'default_years':.5,'daily_bars':True,'lines':[line('BFRC.NETFX.TREASURYEX.CHANGE','Günlük değişim',color='#432454')]}
]


def read_xlsx_rows(raw):
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        ss=[]
        if 'xl/sharedStrings.xml' in book.namelist():
            ss=[''.join(s.itertext()) for s in ET.fromstring(book.read('xl/sharedStrings.xml')).findall('s:si',ns)]
        names=sorted(n for n in book.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n))
        result=[]
        for name in names:
            for row in ET.fromstring(book.read(name)).findall('.//s:sheetData/s:row',ns):
                cells={}
                for cell in row:
                    v=cell.find('s:v',ns);value=v.text if v is not None else ''
                    if cell.get('t')=='s' and value:value=ss[int(value)]
                    elif cell.get('t')=='inlineStr':value=''.join(cell.find('s:is',ns).itertext())
                    elif value:
                        try:value=float(value)
                        except ValueError:pass
                    if value!='':cells[re.sub(r'\d','',cell.get('r',''))]=value
                if cells:result.append(cells)
        return result


def parse_weekly_liquidity(raw,today):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files=[n for n in archive.namelist() if n.lower().endswith('.xlsx')]
        if len(files)!=1:raise DataError('Haftalık rezerv ZIP dosyasında tek XLSX bekleniyor')
        rows=read_xlsx_rows(archive.read(files[0]))
    forward=other=gold_ounces=None
    def norm(value):
        return unicodedata.normalize('NFKD',str(value).lower().replace('ı','i')).encode('ascii','ignore').decode()
    for row in rows:
        text=' '.join(norm(v) for v in row.values() if isinstance(v,str))
        if 'milyon troy ons' in text:gold_ounces=row
        if text.startswith('ii.2.') and 'forward' in text:forward=row
        if text.startswith('ii.3.f ') and 'diger alacak' in text:other=row
    if forward is None or other is None:raise DataError('Haftalık swap düzeltmesi satırları bulunamadı')
    dates={}
    for row in rows[:12]:
        if not any('milyon abd' in norm(v) for v in row.values() if isinstance(v,str)):continue
        for col,v in row.items():
            d=None
            if isinstance(v,(int,float)) and 36000<v<70000 and v==int(v):
                d=date(1899,12,30)+timedelta(days=int(v))
            elif isinstance(v,str):
                # The newest TCMB column can be text while older dates are serials.
                # Match complete dates only; a monthly label is not a weekly date.
                text=v.strip()
                for pattern,fmt in [(r'\d{1,2}\.\d{1,2}\.\d{4}','%d.%m.%Y'),
                                    (r'\d{1,2}/\d{1,2}/\d{4}','%d/%m/%Y'),
                                    (r'\d{4}-\d{2}-\d{2}','%Y-%m-%d')]:
                    if re.fullmatch(pattern,text):
                        try:d=datetime.strptime(text,fmt).date()
                        except ValueError:pass
                        break
            if d is not None and date(1998,1,1)<=d<=today:
                dates[col]=d.isoformat()
    result=[]
    for col,d in dates.items():
        a,b=forward.get(col),other.get(col)
        ounces=(gold_ounces or {}).get(col)
        if isinstance(a,(int,float)) and isinstance(b,(int,float)):
            result.append({'date':d,'total':a+b,'gold_million_ounces':ounces,'source':'TCMB haftalık rezerv/likidite tablosu'})
    if not result:raise DataError('Haftalık swap düzeltmesinde tarih/değer eşleşmedi')
    return result


def fetch_weekly_liquidity(today):
    page=('https://www.tcmb.gov.tr/wps/wcm/connect/tr/tcmb%2Btr/main%2Bmenu/istatistikler/'
          'odemeler%2Bdengesi%2Bve%2Bilgili%2Bistatistikler/uluslararasi%2Brezervler%2Bve%2Bdoviz%2Blikiditesi')
    def download(client,url):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in ('www.tcmb.gov.tr','tcmb.gov.tr'):
            raise DataError('Beklenmeyen TCMB kaynak adresi')
        try:
            r=client.get(url,timeout=(10,40));r.raise_for_status()
            if len(r.content)>15_000_000:raise DataError('TCMB kaynak dosyası beklenenden büyük')
            return r.content
        except requests.RequestException:raise DataError('TCMB haftalık tablo bağlantısı başarısız') from None
    with session() as client:
        html=download(client,page).decode('utf-8')
        matches=re.findall(r'href=["\']([^"\']*URDL_(\d{8})\.zip[^"\']*)["\']',html,re.I)
        if not matches:raise DataError('TCMB haftalık XLSX bağlantısı bulunamadı')
        candidates=[]
        for href,day in matches:
            if datetime.strptime(day,'%Y%m%d').date()<=today:candidates.append((day,href))
        if not candidates:raise DataError('Geçerli haftalık TCMB dosyası bulunamadı')
        _,href=max(candidates)
        return parse_weekly_liquidity(download(client,urljoin(page,unescape(href))),today)


def daily_fx_records(records,weekly,now):
    if not all(c in records for c in FX_RAW):raise DataError('Günlük net döviz hesabının bazı girdileri eksik')
    values={c:{r['date']:r['value'] for r in records[c]['observations'] if r['value'] is not None} for c in FX_RAW}
    assets,liabilities,treasury,usd,buy,sell=[values[c] for c in FX_RAW]
    domestic={d:sell[d]-buy[d] for d in buy.keys()&sell.keys()}
    anchors=[]
    for item in sorted(weekly,key=lambda r:r['date']):
        d=item['date'];m=item.get('domestic',domestic.get(d))
        if m is not None:anchors.append((d,item['total']-m))
    if not anchors:raise DataError('Yabancı swap düzeltmesi için ortak tarih yok')
    level=[];change=[];details=[];last_value=None;last_date=None;anchor_index=-1
    for d in sorted(assets):
        while anchor_index+1<len(anchors) and anchors[anchor_index+1][0]<=d:anchor_index+=1
        valid=(anchor_index>=0 and all(d in v for v in [liabilities,treasury,usd,domestic]) and usd.get(d,0)>0)
        if valid:valid=(date.fromisoformat(d)-date.fromisoformat(anchors[anchor_index][0])).days<=14
        if not valid:
            # Never compute a daily change across an internal missing-input gap.
            level.append({'date':d,'value':None})
            last_value=None;last_date=None;continue
        anchor_day,q=anchors[anchor_index]
        # EVDS balance sheet is in thousand TL; swap amounts are in million USD.
        f=(assets[d]-liabilities[d])/(1000*usd[d])
        h=treasury[d]/(1000*usd[d])
        r=domestic[d]+q;t=f+r;u=t+h  # workbook: F,H,R,T,U columns
        level.append({'date':d,'value':u})
        if last_value is not None:
            change.append({'date':d,'value':u-last_value})
        details.append({'date':d,'T':t,'U':u,'F':f,'H':h,'R':r,'Q':q,'anchor_date':anchor_day})
        last_value=u;last_date=d
    while level and level[-1]['value'] is None:level.pop()
    if not level or not change:raise DataError('Günlük net döviz hesabında yeterli ortak gözlem yok')
    t_changes=[]
    level_dates={r['date'] for r in change}
    by_day={r['date']:r for r in details}
    previous_detail=None
    for row in details:
        if previous_detail is not None and row['date'] in level_dates:
            t_changes.append({'date':row['date'],'value':row['T']-previous_detail['T']})
        previous_detail=row
    fetched=min(records[c]['fetched_at'] for c in FX_RAW)
    base={'provider':'derived','source':'TCMB / EVDS ve haftalık rezerv tablosu; hesaplama: BFRC',
          'fetched_at':fetched,'frequency':'Günlük tahmin','group':'Günlük net döviz pozisyonu',
          'weekly_anchor':details[-1]['anchor_date'],'estimated':True}
    if any(records[c].get('fallback') for c in FX_RAW):base['fallback']=True
    return {
        'BFRC.NETFX.TREASURYEX.CHANGE':{**base,'id':'BFRC.NETFX.TREASURYEX.CHANGE','code':'BFRC.NETFX.TREASURYEX.CHANGE',
                       'name':'Hazine ve swap hariç pozisyon günlük değişimi','unit':'Milyon ABD doları','observations':t_changes},
        'BFRC.NETFX.TREASURYEX':{**base,'id':'BFRC.NETFX.TREASURYEX','code':'BFRC.NETFX.TREASURYEX',
                       'name':'Swap ve Hazine hariç net döviz pozisyonu','unit':'Milyon ABD doları',
                       'observations':[{'date':r['date'],'value':r['T']} for r in details]},
        FX_LEVEL_CODE:{**base,'id':FX_LEVEL_CODE,'code':FX_LEVEL_CODE,'name':'Swap hariç net döviz pozisyonu',
                       'unit':'Milyon ABD doları','observations':level},
        FX_CHANGE_CODE:{**base,'id':FX_CHANGE_CODE,'code':FX_CHANGE_CODE,'name':'Günlük net döviz pozisyonu değişimi',
                       'unit':'Milyon ABD doları','observations':change}},details


def attach_daily_fx(records,cache,errors,now):
    previous=cache.get('_fx_weekly',{})
    by_date={r['date']:r for r in FX_WEEKLY_SEEDS}
    by_date.update({r['date']:r for r in previous.get('rows',[])})
    pseudo={'provider':'evds3','code':'weekly-liquidity','fetched_at':previous.get('fetched_at','')}
    spec={'code':'weekly-liquidity','group_code':'weekly-liquidity','frequency':'HAFTALIK'}
    if previous.get('date_parser_version')!=3 or not reusable_after_release(pseudo,spec,now):
        try:
            fetched=fetch_weekly_liquidity(now.date())
            for r in fetched:
                by_date[r['date']]={**by_date.get(r['date'],{}),**r}
            cache['_fx_weekly']={'fetched_at':now.isoformat(timespec='seconds'),
                                 'date_parser_version':3,'rows':list(by_date.values())}
        except (DataError,ValueError,zipfile.BadZipFile,ET.ParseError) as error:
            errors.append('Haftalık swap düzeltmesi: '+str(error))
    records[NET_GOLD_ASSET]={'observations':[
        {'date':r['date'],'value':r['gold_million_ounces']} for r in by_date.values()
        if isinstance(r.get('gold_million_ounces'),(int,float)) and r['gold_million_ounces']>0]}
    try:
        derived,details=daily_fx_records(records,list(by_date.values()),now)
        records.update(derived)
        cache['_fx_calculation_last']=details[-1]
        latest_input=max(r['date'] for r in records[FX_RAW[0]]['observations']
                         if r.get('value') is not None)
        if details[-1]['date']<latest_input:
            errors.append('Net döviz pozisyonu '+details[-1]['date']+' tarihinde kaldı; '
                          'günlük bilanço '+latest_input+' tarihine kadar mevcut. '
                          'Swap referansı: '+details[-1]['anchor_date']+
                          '. Eksik günlük girdileri veya haftalık swap güncelliğini kontrol edin.')
    except DataError as error:errors.append('Net döviz pozisyonu: '+str(error))


def demo_with_fx(today):
    records=curated_demo(today);rows={c:[] for c in FX_RAW};weekly=[]
    for k in range(1100):
        day=today-timedelta(days=1099-k)
        if day.weekday()>4:continue
        d=day.isoformat();values=[6e9+k*1e6,4e9,3e8,30+k*.015,3000,3500+100*math.sin(k/9)]
        for c,v in zip(FX_RAW,values):rows[c].append({'date':d,'value':v})
        if not weekly or day.weekday()==4:weekly.append({'date':d,'total':-16000,'domestic':values[5]-values[4]})
    for c in FX_RAW:records[c]['observations']=rows[c]
    derived,_=daily_fx_records(records,weekly,datetime.now(TR));records.update(derived)
    attach_analytic_daily(records,[])
    prices=[];stocks=[]
    for k in range(1100):
        d=today-timedelta(days=1099-k)
        if d.weekday()<5:
            prices.append({'date':d.isoformat(),'value':2500+k*.5})
            if d.weekday()==4:stocks.append({'date':d.isoformat(),'value':80000.})
    records['TP.AB.C1']['observations']=stocks
    for code,grams in [(NET_GOLD_ASSET,25.616),*[(c,500e6) for c in WORKBOOK_LIABILITY_CODES]]:
        records[code]={**CURATED_SPECS.get(code,{}),'observations':[{'date':r['date'],'value':grams} for r in stocks],
            'fetched_at':datetime.now(TR).isoformat(timespec='seconds')}
    records['BFRC.GOLD.SWAP.USD']={'observations':[{'date':r['date'],'value':2500.} for r in prices]}
    records[GOLD_ADJUSTED_CODE]=gold_adjustment_records(records,prices,today)
    attach_gold_level(records)
    return records


# Daily analytical balance sheet definitions; independent of swap availability.
CURATED_SPECS['TP.AB.N06']={'id':'TP.AB.N06','code':'TP.AB.N06',
    'name':'2A Net Uluslararası Rezervler (1+2+3)','group_code':'bie_abstc2',
    'group':'Merkez Bankası Bilançosu - Stand By (Cari)','unit':'Bin TL',
    'frequency':'HAFTALIK(CUMA)','source':'TCMB / EVDS','provider':'evds3'}
CURATED_SPECS.pop('TP.AB.N15',None)
CURATED_SPECS.pop('TP.AB.N05',None)
CURATED_SPECS['TP.AB.A15']={**CURATED_SPECS['TP.AB.A02'],'id':'TP.AB.A15','code':'TP.AB.A15','name':'Merkez Bankası Parası'}
CURATED_CHARTS.insert(0,{'id':'netfx-bilanco','name':'Net Uluslararası Rezervler (Haftalık)',
    'title':'TCMB Net Uluslararası Rezervleri (Haftalık)','unit':'Milyar ABD doları',
    'area':True,'default_years':3,
    'note':'Stand By (Cari) bilançosundaki net uluslararası rezervler; aynı günün USD alış kuruyla çevrilir.',
    'lines':[line('BFRC.WEEKLY.NIR','Net uluslararası rezervler',.001,color='#432454')]})
CURATED_CHARTS.insert(2,{'id':'netfx-hazine-haric','name':'Swap ve Hazine Hariç Net Döviz Pozisyonu',
    'title':'TCMB Swap ve Hazine Hariç Net Döviz Pozisyonu','unit':'Milyar ABD doları',
    'area':True,'default_years':3,'note':'Hazine döviz mevduatı hariçtir. Geçmiş seri BFRC arşivinden; yeni günler API verilerinden hesaplanır.',
    'lines':[line('BFRC.NETFX.TREASURYEX','Hazine ve swap hariç net döviz pozisyonu',.001,color='#432454')]})
CURATED_CHARTS.append({'id':'netfx-degisim-hazine-dahil',
    'name':'Swap Hariç Net Döviz Pozisyonu Günlük Değişimi',
    'title':'TCMB Swap Hariç Net Döviz Pozisyonu Günlük Değişimi',
    'unit':'Milyon ABD doları','daily_bars':True,'default_years':.5,
    'note':'Hazine dahil pozisyonun önceki iş gününe göre değişimi. Kur ve değerleme etkilerini içerir.',
    'lines':[line(FX_CHANGE_CODE,'Günlük değişim',color='#432454')]})
CURATED_CHARTS.sort(key=lambda c: {'netfx-hazine-haric':0,'netfx-degisim':1,'netfx-gunluk':2,'netfx-degisim-hazine-dahil':3,'netfx-bilanco':4}.get(c['id'],5))
for chart in CURATED_CHARTS:
    if chart['id'] in ('net-ic','net-dis'):
        chart['name']+=' (Günlük Analitik Bilanço)'
        chart['area']=True
        chart['default_years']=.5
        chart['note']=('Net dış varlıklar = dış varlıklar − toplam döviz yükümlülükleri.' if chart['id']=='net-dis' else
                       'Net iç varlıklar = merkez bankası parası − net dış varlıklar; değerleme dahil.')

def attach_analytic_daily(records,errors):
    def calculate(code,inputs,formula,unit):
        if not all(c in records for c in inputs):
            errors.append(code+': günlük analitik bilanço girdisi eksik');return
        maps=[{r['date']:r['value'] for r in records[c]['observations'] if r['value'] is not None} for c in inputs]
        dates=sorted(set.intersection(*(set(m) for m in maps)))
        rows=[]
        for d in dates:
            vals=[m[d] for m in maps]
            v=formula(*vals)
            if v is not None:rows.append({'date':d,'value':v})
        if not rows:errors.append(code+': ortak günlük gözlem yok');return
        records[code]={'id':code,'code':code,'provider':'derived','source':'TCMB / EVDS — Günlük Analitik Bilanço; hesaplama: BFRC',
            'unit':unit,'frequency':'İŞ GÜNÜ','fetched_at':min(records[c]['fetched_at'] for c in inputs),
            'observations':rows,'fallback':any(records[c].get('fallback',False) for c in inputs)}
    calculate('BFRC.DAILY.NDV',['TP.AB.A02','TP.AB.A10'],lambda a,l:a-l,'Bin TL')
    calculate('BFRC.DAILY.NIV',['TP.AB.A15','BFRC.DAILY.NDV'],lambda m,n:m-n,'Bin TL')
    calculate('BFRC.WEEKLY.NIR',['TP.AB.N06','TP.DK.USD.A.YTL'],lambda n,u:n/(1000*u) if u>0 else None,'Milyon ABD doları')
    if 'BFRC.WEEKLY.NIR' in records:
        records['BFRC.WEEKLY.NIR'].update(frequency='HAFTALIK(CUMA)',source='TCMB / EVDS — Stand By (Cari) Net Uluslararası Rezervler')


from html.parser import HTMLParser

LBMA_CODE='BFRC.LBMA.AM'
GOLD_ADJUSTED_CODE='BFRC.GOLD.NET.CHANGE'
GOLD_LEVEL_CODE='BFRC.GOLD.NET.USD'
FX_EX_GOLD_CODE='BFRC.NETFX.TREASURYEX.GOLDNETEX.CHANGE'
FX_EX_GOLD_LEVEL='BFRC.NETFX.TREASURYEX.GOLDNETEX.LEVEL'
FX_EX_GOLD_LEVEL_TITLE='TCMB Swap ve Hazine Hariç Net Döviz Pozisyonu (Altın Etkisi Çıkarılmış)'
FX_EX_GOLD_TITLE='TCMB Swap ve Hazine Hariç Net Döviz Pozisyonu – Günlük Değişim (Altın Etkisi Çıkarılmış)'
GOLD_CHART_TITLE='Net Altın Varlığı - Günlük Değişim'
NET_GOLD_ASSET='BFRC.TCMB.RESERVES.GOLD.MILLION.OZT'
WORKBOOK_LIABILITY_CODES=('TP.BL0822','TP.BL132','TP.BL136','TP.BL089','TP.BL141')
for code in WORKBOOK_LIABILITY_CODES:
    CURATED_SPECS[code]={'id':code,'code':code,'name':code,
        'unit':'Bin TL',
        'frequency':'HAFTALIK(CUMA)','group_code':'bie_mbblnch',
        'group':'Merkez Bankası Haftalık Vaziyeti (Yeni)','provider':'evds3','source':'TCMB / EVDS'}
GOLD_ADJUSTMENT_METHOD='Net altın = günlük değerlenmiş altın varlığı − günlük değerlenmiş altın pasifleri − altın swap stoku.'
CURATED_CHARTS[:0]=[
    {'id':'netfx-seviye-altin-haric','observation_days_only':True,'name':FX_EX_GOLD_LEVEL_TITLE,'unit':'Milyar ABD doları',
     'area':True,'default_years':0,'lines':[line(FX_EX_GOLD_LEVEL,'Seviye: net pozisyon − net altın',.001,color='#432454')]},
    {'id':'netfx-degisim-altin-haric','observation_days_only':True,'name':FX_EX_GOLD_TITLE,'unit':'Milyon ABD doları',
     'daily_bars':True,'default_years':0,'lines':[line(FX_EX_GOLD_CODE,'Günlük değişim: net pozisyon farkı − net altın farkı',color='#432454')]},
    {'id':'net-altin','observation_days_only':True,'name':'Net Altın Varlığı','unit':'Milyar ABD doları','area':True,
     'default_years':0,'lines':[line(GOLD_LEVEL_CODE,'Net altın',.001,color='#432454')]},
    {'id':'net-altin-degisim','observation_days_only':True,'name':GOLD_CHART_TITLE,'unit':'Milyon ABD doları','daily_bars':True,
     'default_years':0,'lines':[line(GOLD_ADJUSTED_CODE,'Günlük net altın değişimi',color='#432454')]}]

def attach_gold_level(records):
    adjusted=records[GOLD_ADJUSTED_CODE]
    records[GOLD_LEVEL_CODE]={**adjusted,'id':GOLD_LEVEL_CODE,'code':GOLD_LEVEL_CODE,
        'name':'Net Altın Varlığı',
        'observations':[{'date':r['date'],'value':r['net_gold_million_usd']} for r in adjusted['level_audit']]}

    fx_level=records.get('BFRC.NETFX.TREASURYEX',{})
    gold={r['date']:r['value'] for r in records[GOLD_LEVEL_CODE]['observations'] if r['value'] is not None}
    level_points=[{'date':r['date'],'value':r['value']-gold[r['date']]
                   if r['date'] in gold and r['value'] is not None else None}
                  for r in fx_level.get('observations',[])]
    if any(r['value'] is not None for r in level_points):
        records[FX_EX_GOLD_LEVEL]={**fx_level,'id':FX_EX_GOLD_LEVEL,'code':FX_EX_GOLD_LEVEL,
            'name':FX_EX_GOLD_LEVEL_TITLE,'source':fx_level['source']+'; '+adjusted['source'],
            'observations':level_points}

    fx=records.get('BFRC.NETFX.TREASURYEX.CHANGE',{})
    common=[r for r in level_points if r['value'] is not None]
    points=[{'date':cur['date'],'value':cur['value']-prev['value']}
            for prev,cur in zip(common,common[1:])]
    if points:
        records[FX_EX_GOLD_CODE]={**fx,'id':FX_EX_GOLD_CODE,'code':FX_EX_GOLD_CODE,
            'name':FX_EX_GOLD_TITLE,'source':fx_level['source']+'; '+adjusted['source'],
            'observations':points}

class FixingTableParser(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None;self.cell=None

def parse_lbma_am_table(html,today):
    parser=FixingTableParser();parser.feed(html);points={}
    for row in parser.rows:
        if len(row)!=2:continue
        try:
            d=datetime.strptime(row[0],'%d. %B %Y').date()
            value=float(row[1].replace(',',''))
        except (ValueError,TypeError):continue
        if date(2019,1,1)<=d<=today and math.isfinite(value) and value>0:
            points[d.isoformat()]=value
    if not points:raise DataError('London AM fiyat tablosunda geçerli tarih/fiyat bulunamadı')
    return [{'date':d,'value':points[d]} for d in sorted(points)]

def fetch_lbma_am(today,client=None,*,year=None):
    url='https://www.westmetall.com/en/markdaten.php?action=table&field=USD_ozt_London'
    if year is not None:url+=f'&year={year}'
    def read(active):
        reply=active.get(url,timeout=(10,30),allow_redirects=False)
        if reply.status_code!=200:raise DataError(f'London AM {year or today.year}: HTTP {reply.status_code}')
        if len(reply.content)>3_000_000:raise DataError('London AM fiyat tablosu beklenenden büyük')
        rows=parse_lbma_am_table(reply.text,today)
        rows=[r for r in rows if r['date'].startswith(str(year or today.year)+'-')]
        if not rows:raise DataError(f'London AM {year or today.year}: fiyat yok')
        return rows
    try:
        if client is not None:return read(client)
        with session() as active:return read(active)
    except requests.RequestException:raise DataError(f'London AM {year or today.year}: kaynağa erişilemedi') from None

# Weekly ounces and signed liabilities (million USD), keyed by reference date.
GOLD_WEEKLY_EXCEL_REFERENCES={
    '2021-12-31': (21.146, -12426.945720014499),
    '2022-01-07': (21.306, -11781.853178035413),
    '2022-01-14': (21.316, -11799.005614596783),
    '2022-01-21': (21.243, -11569.47769540132),
    '2022-01-28': (21.249, -11917.689406924923),
    '2022-02-04': (21.588, -11814.945492821205),
    '2022-02-11': (21.684, -11858.171662816667),
    '2022-02-18': (21.854, -12149.815852457954),
    '2022-02-25': (21.817, -13543.21631459091),
    '2022-03-04': (21.743, -12807.020832826687),
    '2022-03-11': (21.79, -13161.550797923104),
    '2022-03-18': (21.741, -12107.80804312224),
    '2022-03-25': (21.789, -12206.972078180015),
    '2022-04-01': (21.883, -11894.159427230948),
    '2022-04-08': (21.903, -12002.90696171269),
    '2022-04-15': (21.953, -12189.500508501185),
    '2022-04-22': (22.005, -12105.870826244696),
    '2022-04-29': (21.94, -11775.800102176745),
    '2022-05-06': (21.972, -11927.81179410126),
    '2022-05-13': (22.014, -11827.07462631365),
    '2022-05-20': (22.048, -11218.989775285916),
    '2022-05-27': (22.552, -12366.391904986707),
    '2022-06-03': (22.561, -12112.915195122489),
    '2022-06-10': (22.563, -12648.188843418255),
    '2022-06-17': (22.702, -12370.194859211548),
    '2022-06-24': (22.711, -12140.999206658065),
    '2022-07-01': (22.692, -11838.599301193306),
    '2022-07-08': (22.819, -11664.700699040379),
    '2022-07-22': (22.894, -11310.449979920777),
    '2022-07-29': (22.995, -11827.217821793287),
    '2022-08-05': (22.987, -11941.955618436381),
    '2022-08-12': (23.008, -11991.224359846467),
    '2022-08-19': (23.087, -11801.410236978656),
    '2022-08-26': (23.164, -11770.565983554501),
    '2022-09-02': (23.231, -11441.273060775731),
    '2022-09-09': (23.253, -11510.94556270471),
    '2022-09-16': (22.632, -11932.180968673225),
    '2022-09-23': (22.608, -11699.960296105737),
    '2022-09-30': (23.683, -11481.83904195356),
    '2022-10-07': (22.732, -11939.029789574728),
    '2022-10-14': (23.774, -11634.392333650827),
    '2022-10-21': (23.788, -11308.470811294128),
    '2022-10-28': (23.674, -11534.422977782702),
    '2022-11-04': (23.76, -11124.943369072305),
    '2022-11-11': (23.98, -12033.236507109677),
    '2022-11-18': (24.287, -12734.809077037879),
    '2022-11-25': (24.404, -12618.818221111665),
    '2022-12-02': (26.143, -12788.603713530132),
    '2022-12-09': (26.313, -12856.21647843016),
    '2022-12-16': (26.298, -12669.967395961865),
    '2022-12-23': (27.28, -12914.64919010854),
    '2022-12-30': (25.297, -13055.217059392879),
    '2023-01-06': (28.16529, -13295.848393391885),
    '2023-01-13': (29.00581, -13657.220190236516),
    '2023-01-20': (29.93201, -14452.369600823524),
    '2023-01-27': (30.76162, -14333.552685524672),
    '2023-02-03': (31.23248, -14172.672277244586),
    '2023-02-10': (30.24369, -13681.338585356723),
    '2023-02-17': (29.79477, -13455.084245098675),
    '2023-02-24': (29.81895, -13199.5208613671),
    '2023-03-03': (30.37681, -13685.08176698107),
    '2023-03-10': (30.62229, -13872.87321707375),
    '2023-03-17': (32.30912, -14566.168999104168),
    '2023-03-24': (32.63277, -14759.739229096542),
    '2023-03-31': (32.11717, -14345.528174179082),
    '2023-04-07': (26.08124, -14849.061876268299),
    '2023-04-14': (25.60336, -14781.161046667345),
    '2023-04-28': (23.409, -14078.320364060852),
    '2023-05-05': (22.42402, -14521.079858017622),
    '2023-05-12': (22.08266, -14401.250082049937),
    '2023-05-26': (21.46877, -13586.871628388255),
    '2023-06-02': (21.3255, -14047.286651003215),
    '2023-06-09': (21.38642, -14460.392577094979),
    '2023-06-16': (21.393, -13623.446907893176),
    '2023-06-23': (21.52545, -12685.346461503006),
    '2023-07-07': (21.18346, -10502.20738066216),
    '2023-07-14': (21.30383, -10705.478235528697),
    '2023-07-21': (21.32394, -11181.309482542765),
    '2023-07-28': (21.24205, -11094.33425457463),
    '2023-08-04': (21.38693, -10857.56207096752),
    '2023-08-11': (21.48941, -10769.547945489834),
    '2023-08-18': (21.55833, -10655.335530093593),
    '2023-08-25': (21.55727, -10840.11624863316),
    '2023-09-01': (21.5284, -10699.997626564396),
    '2023-09-08': (21.48676, -10534.930171283908),
    '2023-09-15': (21.60596, -10598.719750799595),
    '2023-09-22': (21.64446, -10794.216342397453),
    '2023-09-29': (21.70791, -10528.03667844471),
    '2023-10-06': (21.80753, -10258.844504624853),
    '2023-10-13': (21.82905, -10262.506028992675),
    '2023-10-20': (21.95245, -10889.318173665368),
    '2023-10-27': (22.21195, -10877.125288656427),
    '2023-11-03': (22.36638, -10994.334469684089),
    '2023-11-10': (22.50877, -10655.47250221989),
    '2023-11-17': (22.70984, -10994.986264649582),
    '2023-11-24': (22.80269, -10598.290487921862),
    '2023-12-01': (22.94671, -11097.95020956911),
    '2023-12-08': (23.08577, -10808.6942755036),
    '2023-12-15': (23.06108, -11053.89156383546),
    '2023-12-22': (23.30038, -11114.223573520101),
    '2023-12-29': (23.35351, -11491.618549642419),
    '2024-01-05': (23.37849, -10923.163968722569),
    '2024-01-12': (23.44125, -10896.386807409768),
    '2024-01-19': (23.51584, -10761.795561175257),
    '2024-01-26': (23.75278, -10745.957775216291),
    '2024-02-02': (23.80742, -10802.588128855019),
    '2024-02-09': (23.90216, -10707.853830772137),
    '2024-02-16': (23.99335, -10479.872600494775),
    '2024-02-23': (24.3999, -11642.943292829601),
    '2024-03-01': (24.58579, -12006.912832049462),
    '2024-03-08': (24.32284, -11802.214777916357),
    '2024-03-15': (24.42287, -11675.551123261352),
    '2024-03-22': (24.54146, -11711.895790810315),
    '2024-03-29': (24.639, -12164.293393160338),
    '2024-04-05': (24.76702, -14612.272284479968),
    '2024-04-19': (24.8856, -15830.91604564516),
    '2024-04-26': (25.15668, -15123.196743575836),
    '2024-05-03': (25.07811, -15548.734714566304),
    '2024-05-10': (25.38919, -15154.451589349988),
    '2024-05-17': (25.43332, -15637.869558596383),
    '2024-05-24': (25.41998, -15541.595435924675),
    '2024-05-31': (25.4982, -15156.05146708167),
    '2024-06-07': (25.57569, -15174.272682491648),
    '2024-06-14': (24.97634, -14966.081492564132),
    '2024-06-21': (25.011, -15615.254271171325),
    '2024-06-28': (24.95149, -15545.643677391072),
    '2024-07-05': (24.82363, -16075.7971888129),
    '2024-07-12': (24.72836, -16498.904473349594),
    '2024-07-19': (24.51132, -16642.383798930503),
    '2024-07-26': (23.62718, -17155.76882774976),
    '2024-08-02': (23.51778, -17453.37474016),
    '2024-08-09': (23.89267, -18054.30353933029),
    '2024-08-16': (23.86895, -18228.14579341942),
    '2024-08-23': (23.87635, -18482.540229176495),
    '2024-09-06': (23.90776, -18476.219419084202),
    '2024-09-13': (23.87639, -18694.75558062088),
    '2024-09-20': (23.89573, -18986.18412832747),
    '2024-09-27': (23.89204, -19314.07250437355),
    '2024-10-04': (23.91449, -19245.06017217842),
    '2024-10-11': (24.17208, -19726.283008213104),
    '2024-10-18': (24.18266, -20248.243715850196),
    '2024-10-25': (24.23259, -20402.635481727106),
    '2024-11-01': (24.24648, -20697.29339559043),
    '2024-11-08': (24.27625, -20735.486070615127),
    '2024-11-15': (24.38687, -20063.98162970739),
    '2024-11-22': (24.35259, -21012.152237084294),
    '2024-11-29': (24.36657, -21481.497871714564),
    '2024-12-06': (24.4182, -20936.154564941608),
    '2024-12-13': (24.46362, -20977.00548401273),
    '2024-12-20': (24.48271, -20518.04768151551),
    '2024-12-27': (24.49745, -21369.420911387824),
    '2025-01-03': (24.61871, -21734.96603448435),
    '2025-01-10': (24.63963, -22840.357960992165),
    '2025-01-17': (24.58742, -23340.44560871083),
    '2025-01-24': (24.62318, -24278.057783972064),
    '2025-01-31': (24.597, -24973.804987075713),
    '2025-02-07': (24.617, -24910.582648336625),
    '2025-02-14': (24.66782, -25432.397877497475),
    '2025-02-21': (24.67738, -25702.1686731672),
    '2025-02-28': (24.694, -24576.693041900202),
    '2025-03-07': (24.70888, -25334.04757193503),
    '2025-03-14': (24.67218, -25311.027442068847),
    '2025-03-21': (24.67122, -25647.42151475975),
    '2025-03-28': (24.72061, -22782.201664768487),
    '2025-04-04': (24.72972, -23621.924004187327),
    '2025-04-11': (24.732, -23951.953166226915),
    '2025-04-18': (24.742, -24679.817698053026),
    '2025-04-25': (24.793, -24245.848469446315),
    '2025-05-02': (24.81, -23340.301537161326),
    '2025-05-09': (25.003, -23907.12207580924),
    '2025-05-16': (25.001, -22867.320738727638),
    '2025-05-23': (25.23, -24503.00349853475),
    '2025-05-30': (25.251, -24564.408995708403),
    '2025-06-05': (25.276, -24868.275699708924),
    '2025-06-13': (25.304, -25511.555923248678),
    '2025-06-20': (25.33459, -26465.308255255488),
    '2025-06-27': (25.35299, -25824.542840309514),
    '2025-07-04': (25.38049, -24871.09917966483),
    '2025-07-11': (25.38692, -25490.4881027646),
    '2025-07-18': (25.44036, -25959.36540422233),
    '2025-07-25': (25.465, -26573.200046817085),
    '2025-08-01': (25.518, -26584.703316946332),
    '2025-08-08': (25.539, -26963.02084976263),
    '2025-08-15': (25.59, -27003.56771111447),
    '2025-08-22': (25.609, -27366.991119390077),
    '2025-08-29': (25.628, -28343.27711998452),
    '2025-09-05': (25.624, -29250.691062476275),
    '2025-09-12': (25.611, -29045.79822647101),
    '2025-09-19': (25.631, -29591.895536564047),
    '2025-09-26': (25.676, -32125.51759649009),
    '2025-10-03': (25.692, -32738.050022076077),
    '2025-10-10': (25.612, -31656.667676039415),
    '2025-10-17': (25.625, -35422.40431344357),
    '2025-10-24': (25.719, -34124.097229519946),
    '2025-10-31': (25.719, -32908.535347504185),
    '2025-11-07': (25.724, -32588.406834960704),
    '2025-11-14': (25.807, -34598.31189176359),
    '2025-11-21': (25.876, -33957.50494674557),
    '2025-11-28': (25.876, -34878.41104375578),
    '2025-12-05': (25.964, -34595.37486047889),
    '2025-12-12': (25.964, -37644.66432972918),
    '2025-12-19': (26.089, -37406.37247771),
    '2025-12-26': (26.088, -39998.32558150411),
    '2026-01-02': (26.107, -37410.80138730374),
    '2026-01-09': (26.107, -39637.74658905704),
    '2026-01-16': (26.268, -41840.70900859067),
    '2026-01-23': (26.27, -44845.06156517712),
    '2026-01-30': (26.415, -47303.6870669746),
    '2026-02-06': (26.408, -43396.25719880212),
    '2026-02-13': (26.603, -44418.55858691208),
    '2026-02-20': (26.317, -44222.376958647845),
    '2026-02-27': (26.445, -45784.4176416819),
    '2026-03-06': (26.86, -44308.21284445457),
    '2026-03-13': (26.391, -43909.90611299152),
    '2026-03-19': (24.807, -41067.620687701805),
    '2026-03-27': (22.585, -38671.17762075264),
    '2026-04-03': (22.317, -39851.18195584961),
    '2026-04-10': (22.502, -41748.532456731846),
    '2026-04-17': (23.488, -42125.68704907013),
    '2026-04-24': (23.527, -41529.110830728),
    '2026-04-30': (23.53816, -41749.001562180114),
    '2026-05-08': (23.55894, -41836.64745011087),
    '2026-05-15': (23.57328, -41420.16110375276),
    '2026-05-22': (23.4659, -39694.47171761601),
    '2026-06-05': (23.56432, -39871.83804621638),
    '2026-06-12': (23.43265, -38777.266175949655),
    '2026-06-19': (23.45642, -36826.94410048145),
    '2026-06-26': (23.45726, -37563.72527287704),
    '2026-07-03': (23.43233, -38336.71395890838),
    '2026-07-10': (23.44564, -38067.2059937435),
    '2026-07-17': (23.773, -38869.69918896792),
    '2026-07-24': (24.732, -39917.29482037028),
    '2026-07-31': (24.835, -39944.50618237204),
    '2026-08-07': (24.95312, -43235.45966837788),
    '2026-08-14': (24.90508, -42894.72009590473),
    '2026-08-21': (24.92853, -46893.92786258504),
    '2026-08-28': (25.44318, -47805.893815635936),
    '2026-09-04': (25.489, -45210.761080757926),
    '2026-09-11': (25.396, -43506.64676999698),
    '2026-09-18': (25.438, -43652.113965021),
    '2026-09-25': (25.553, -43171.63416456601),
    '2026-10-02': (25.573, -41820.33195359841),
    '2026-10-09': (25.558, -41965.58430813124),
    '2026-10-16': (25.574, -43268.12736500203),
    '2026-10-23': (25.616, -41922.69630078836),
}

def gold_adjustment_records(records,prices,today):
    def points(code):
        return {r['date']:float(r['value']) for r in records.get(code,{}).get('observations',[])
                if isinstance(r.get('value'),(int,float)) and math.isfinite(r['value']) and r['date']<=today.isoformat()}
    days=points('TP.AB.A02')
    am={r['date']:r['value'] for r in prices if r['date']<=today.isoformat() and r['value']>0}
    gold_stock=points(NET_GOLD_ASSET)
    liabilities=[points(c) for c in WORKBOOK_LIABILITY_CODES]
    usd=points('TP.DK.USD.A.YTL')
    swaps=points('BFRC.GOLD.SWAP.USD')
    # Workbook liabilities are negative USD values; freeze the ounce amount at the weekly AM.
    # Verified weekly workbook inputs only. Never seed daily fixing prices or swap values.
    reference_inputs=GOLD_WEEKLY_EXCEL_REFERENCES
    anchors=[]
    for d in sorted(gold_stock.keys() | reference_inputs.keys()):
        if d not in am:continue
        if d in gold_stock and usd.get(d,0)>0 and all(d in m and m[d]>=0 for m in liabilities):
            ounces=gold_stock[d]
            passive_usd=-sum(m[d] for m in liabilities)/(1000*usd[d])
            reference_source='TCMB / EVDS'
        elif d in reference_inputs:
            ounces,passive_usd=reference_inputs[d]
            reference_source='Excel haftalık referans'
        else:continue
        anchors.append((date.fromisoformat(d),d,ounces,passive_usd/am[d],reference_source))
    output=[];audit=[];level_audit=[];previous=None
    for d in sorted(days):
        eligible=[a for a in anchors if a[0]<=date.fromisoformat(d) and (date.fromisoformat(d)-date.fromisoformat(a[1])).days<=28]
        if not eligible or d not in am or d not in swaps:
            if level_audit or d in ('2026-05-26','2026-06-01'):
                missing=(['haftalık altın miktarı/pasif referansı'] if not eligible else [])
                if d not in am:missing.append('London AM fiyatı')
                if d not in swaps:missing.append('altın swap stoku')
                level_audit.append({'date':d,'net_gold_million_usd':None,'missing_inputs':missing})
            continue
        _,ref,ounces,passive_ounces,reference_source=eligible[-1]
        price=am[d];swap=swaps[d];gross=ounces*price;basis='EVDS'
        passive=passive_ounces*price
        net_gold=gross+passive-swap
        row={'date':d,'weekly_gold_reference':ref,'weekly_gold_reference_source':reference_source,'source':basis,'gold_million_ounces':ounces,
             'gold_price_usd':price,'gross_gold_million_usd':gross,
             'passive_million_ounces':passive_ounces,'gold_liabilities_signed_million_usd':passive,
             'gold_swap_million_usd':swap,'net_gold_million_usd':net_gold}
        level_audit.append(row)
        if previous:
            gold_change=net_gold-previous['net_gold_million_usd']
            output.append({'date':d,'value':gold_change})
            audit.append({**row,'previous_date':previous['date'],'net_gold_change_million_usd':gold_change})
        previous=row
    if not any(r.get('net_gold_million_usd') is not None for r in level_audit):raise DataError('Net altın için ortak gözlem bulunamadı')
    return {'id':GOLD_ADJUSTED_CODE,'code':GOLD_ADJUSTED_CODE,'provider':'derived',
        'name':GOLD_CHART_TITLE,'unit':'Milyon ABD doları','frequency':'İŞ GÜNÜ',
        'source':'TCMB / EVDS; London AM — Westmetall','estimated':True,
        'method':GOLD_ADJUSTMENT_METHOD,'observations':output,'audit':audit,'level_audit':level_audit,
        'fetched_at':datetime.now(TR).isoformat(timespec='seconds')}

def attach_gold_swap_series(records,today):
    code='TP.SWAPTEKTAR.SWAPXSATIMSTOKUSDTUTAR'
    spec={'id':code,'code':code,'group':'TCMB Taraflı Swap İşlemleri',
          'frequency':'İŞ GÜNÜ','provider':'evds3','source':'TCMB / EVDS'}
    result=fetch_curated_group([spec],today)
    records['BFRC.GOLD.SWAP.USD']=result[code]

def attach_gold_adjustment(records,cache,errors,today):
    saved=cache.get('_lbma_am_source_only',{})
    prices={r['date']:r for r in saved.get('observations',[]) if isinstance(r.get('value'),(int,float)) and r['value']>0}
    complete=set(saved.get('archive_years',[]))
    failed=False
    with session() as client:
        for year in [today.year,*range(today.year-1,2018,-1)]:
            if year<today.year and year in complete:continue
            try:
                fresh=fetch_lbma_am(today,client,year=year if year<today.year else None)
                prices={d:r for d,r in prices.items() if not d.startswith(str(year)+'-')}
                prices.update({r['date']:r for r in fresh})
                if year<today.year:complete.add(year)
            except DataError as error:errors.append('Altın fiyatı: '+str(error));failed=True
    cache['_lbma_am_source_only']={'source':'Westmetall London Fixing (AM)',
        'archive_years':sorted(complete),'fetched_at':datetime.now(TR).isoformat(timespec='seconds'),
        'observations':[prices[d] for d in sorted(prices)]}
    ordered=[prices[d] for d in sorted(prices)]
    records[LBMA_CODE]={'id':LBMA_CODE,'code':LBMA_CODE,'provider':'public-table',
        'source':'Westmetall London Fixing','observations':ordered,'fallback':failed,
        'fetched_at':cache.get('_lbma_am_source_only',{}).get('fetched_at','')}
    try:attach_gold_swap_series(records,today)
    except (DataError,KeyError,TypeError) as error:errors.append('Altın swap: '+str(error))
    # Print source coverage before joining; this survives a failed calculation.
    inputs=[('Günlük bilanço','TP.AB.A02'),('USD alış','TP.DK.USD.A.YTL'),
            ('London AM',LBMA_CODE),('Altın swap','BFRC.GOLD.SWAP.USD'),
            ('Haftalık altın miktarı',NET_GOLD_ASSET)]
    inputs.extend((code,code) for code in WORKBOOK_LIABILITY_CODES)
    for label,code in inputs:
        dates=sorted(r['date'] for r in records.get(code,{}).get('observations',[])
                     if r.get('value') is not None)
        print(f'ALTIN GİRDİ | {label} | {len(dates)} gözlem | son: {dates[-1] if dates else "yok"}',flush=True)
    try:
        records[GOLD_ADJUSTED_CODE]=gold_adjustment_records(records,ordered,today)
        attach_gold_level(records)
        audit=records[GOLD_ADJUSTED_CODE]['level_audit']
        for row in audit[-25:]:
            if row.get('missing_inputs'):
                print('ALTIN ATLANDI | '+row['date']+' | '+', '.join(row['missing_inputs']),flush=True)

        for row in records[GOLD_ADJUSTED_CODE]['level_audit']:
            if row['date'] in ('2026-05-26','2026-06-01') and row.get('missing_inputs'):
                errors.append('Net altın '+row['date']+': '+', '.join(row['missing_inputs']))
        levels=records['TP.AB.A02']['observations']
        latest=max(r['date'] for r in levels if r.get('value') is not None)
        last=max(r['date'] for r in records[GOLD_LEVEL_CODE]['observations'] if r['value'] is not None)
        if last<latest:
            missing=next((r.get('missing_inputs',[]) for r in records[GOLD_ADJUSTED_CODE]['level_audit']
                          if r['date']==latest),[])
            errors.append('Net altın '+last+' tarihinde kaldı; '+latest+' eksik: '+(', '.join(missing) or 'günlük girdiler'))
    except DataError as error:errors.append('Net altın: '+str(error))
# Snapshot of published workbook cells: date, P, Q, AE, AF, Sheet3 G, AG, R, R change (R_t - R_t-1).
EXCEL_GOLD_ARCHIVE=(
    'c-ox(TXHPTkpyV&rJr$9;(l36%j}?K-e&)!pL(*0$ZBb%G>b(c@kaul9`1HgQ&amt|IhzpnU?wAlz(sk$It)w|M!3WUtj#k|L5n|$M*ksA3Ofvee'
    'C>y_p$5$`1!Y=_j<j~`#!I0IgaD{{M4UM{Qi~mJbvYVz1Dr1_v74;Y5n;(->-dN*5lf)d)=;cJwN`!7a#by9n*Eaj`NzYbDyq#-v0OBU;6jY?Rc%'
    'zF>Uj;p4<Gh%*$)qj(Ohh`8t;TqvxOh;`x8ue(&4sn(t{jr#v_R%<KBP_Iu9f^22GJ&c{;LF6Fql*SfCfInU?0?mzqdy3ZxQ;y9*hzs~u&lD{9%L'
    'ay_A<xkGXe9z0h{+wC;a$UzhPy2dJ*ZNq;)^+Unl3%<p$8xRrbpFg!e)D~0AJ_F-&THD93;E)CE18zpv0ul$%-Ml$|2g+pwr1b%YtGBcFL^9wx0H'
    'K7*4K2**S=imy<J}RIla#1*zalD@_+YZDc?N*Z_6QFUq_a9-OHNY`1!ZY>y*vAk8I{TFY9wD*I3GZ=0#6yR=@3e@&EEma?ZE?T=UzPb3Q+o^7r$W'
    'aviUIS+d+|+j3w2?aS-RHs;A3scCzjle;U~&K#z^_w|_fd0v0A;8z~m&wRaYIdcY{EBWI2tmM4syq_sgZP|o8mE)B|wC__^oO^X|zn8L1UCMf2FQ'
    '+>Dn4>-ArEaf#na_DSa}t*QxsX4fwu~i*XSwEc-CVYx<Id|_@?NKT%~#p3$13KrikO7U3E%epx~`vXdQI8#oT_P#W!OGe@z>K?#gUaA*SzI0T+=l'
    'FZ0Bp)^201PE8O?zUeuCggVrqd&Y?f9eLIeyZQft=x=gud`8M;hJWt41AIMskb$=cAoDIsIJ@=n2CicFk?8cl09l7z3#q47->-^fM*x);+aPrPyd'
    '2l|q?B$lNUH6Z*{QbP89P?|w?lW6+A4@FVvd6ky$9+uql>0tCF3xg{rOd}`_RhESTJxcN$t{0k&3^X2P1na#{(k=7mgDTlS>keS>~K!#{yKAw_C5'
    'dg+K&CPl(S38b$hM3dFN*TequOZ=bBI4`6uV$Zah}<_v2Z}apa!wca3h&Z62HQsV&dtRL2wKvOiXGjg>6NE59MTd#|~IKj-{9j~v5nZeH}cZyzi9'
    '^YN@?cfDf$&YbR?o}c4-%{C*)I0k>4AGUtE--k92P4gBTG2MF%!IJyON5v$%1^4`UX8(NtpFg+0vf5*fy~_gg)Un0n9NCT-*L$9i|1M?8YX0Z`<T'
    'W4hOYx`2&mK!2tCaU1=W^t~{O{ZI|NOXx<l^r8vD`USvB=I+{%71$9?iz&RXr9mF7kDruQ|88wlU7@*x8HxYsx`e_K)NG=hF^rtbATe_U%f9u>9m'
    '2<!3p$S#8YlvOHF?j#bRLk;gUV!Rv?#%I}TQ+^5a|T=tLm^5@f5kpqzzvc|>b@NBtR=WD&!geb>$+&Sm_V;S2xsB;cg%=MK(Csz3<zW!L}dygeLx'
    '%=hgp#J%|D>Ln{HFtE4&)bhgM>#HQ;<^M2`4La`SjgL=P1(aO|2K18F-HmQa;A3S%$?t!3-@tAzWBhuZMpL@V&?8;U$*#}pMzE2xIJ-N4)kL!<Mu'
    'jMv6YEwT<@H{c$A!{H6|dA`~29GKcCK)#FEAP=8xW6BE}q=<9JO8mEs0+<png4WsJ+4peeuj;s)+zI%AIoqmxg>aORffsDCWwn@?Cy))>FM%?(s?'
    'ffFC?c}+WGpRg%c{r7t2X{_f;$gt%L>~qZ2TKABhzY=Fmb0WR@;|+cB{6EJv;meVSr{liGDkp!KIDF2P+xBeHV=43d_T+5mx6WsFIU&tYtaM&*7N'
    '1ja9sg@z{(L+OiQj)+OPx^OqpeG*!b{}(#0y;aV;#emh`lB*Jf<lB5jVAoB<@^_gsJQCj9<Ta+=htsg;6oLc`Obmk!9Z97AKT#$=P}=WL%%kVy`p'
    'iW?qkxElii+9Q&L%{PAY~eA+UE`PaN&LaJ@~+3pwXo43|rZF^ke`FO8ldnIk#*8HaYrwAgBEL$A=IZer*wtM?r$rqn+V((mACN?(3IsUnF*OMJ3z'
    'sR)~)IXLp;(;x9Jz<pmC~H1yBN8en7@Kp@<2Lr?b7Q{wgw44RRz*6N7%dklMkuE^`<g&u&OM(Wo@eA3@d&x~33}G}>;zbGS2>20^XHi7OMV>NFP^'
    'tGc{8kF{z;<QI6?MkO*C^XiGg#fo=bV#mLv(gyez9vGLs*z=Zk^NN8;F*PcQiA<Lv%=G$&u;m$Kgai<qOFDf^Px?P28JVfB^ol+%7@-;!VEL+h5n'
    'FWHKeb4yHdeww&HpXUp6m-F?rg#d*Zm6_w(nsnH~9Gk~l-ac8pyC$rQ3C%J8S(jIW)C9v>euBl%E%|?bxE?t!s+TMex!iNiY|d{^)|q#EO%IQ}@W'
    '_Xl=haQ!6ZrN7Caa6H$gl9`pDFP-kH-ln^nWEBi8+bAPjvSqcuOK0Ta?3|OLRQ0Pm_%N>gi4}n4l|1I`-)&J}jp$Za$Asap=#?>#OIpmhGCPz<ZL'
    'hoT@~2BFwJ$juS{!u{_Revkb<NiOSK7nasZZBuUBxxv$A26Y@Rdly4rlD;q-_!+*dDvULd+@+E>-+<WZJKL0we>j-1>&5}niL5Gqw=Qjs&x$EQ3;'
    '2~3hkL7qS<nPD3C2<Q$CKB`tEw_dcc`xfBU(N-7E@bIKk}T#sB`sP5BqT$~A$ui%%`eR-w|jrC<IktFjGCLZ);`A~&ww~!Z~1rr`+PrE(JOP`hcA'
    '>Z6Z|I*y+q60RBpxekrDs-aMmCi*r6p^M3S;BB8W-O_bPvnTeyCQ^y@o{P26Aua~HYE{OczuUhaABUMzM1Glt^1k}p1x)#M-q)CnFiJJVM#`6*A^'
    'r(EbfoP>CLtfu!SuRLZiyD4$o-N-vw%e;uyW63AZ^!K~@=J|iI_E+wSbbXv3saaCuWNK5kGiN-J{$nlISW7;2LrwE^g24PJ{x)f5pr`!Ce2x2KEq'
    '_0swd8*!eoAVQ%xkTOj(eUP)aAS+u>2hztRtRHfD`AM5SgP&R1P(bDd(T&9J<9he6Hn-=d+e2x$~MREuJ@d+O_{Ia@aNbNb=LeD}0=o#<EE$1Dpn'
    '{j1iV9<!$HH7k9pT_CHa!?>-QdpWE{~rzF7%X7i_G-R>z+llbANqdXobrr~UU`Fbaum?W4<5?1CaSns^m@8{cJA3O8K^Vyl4oB+2;bO4msd<-ub*'
    'ePMl4mJ-A`B+R1OK{|Tu|%Xvv-1Vc5u~2t_Os~Pwmuj0#q<A!tyyvc|KMLSeJ}$KbYTd2WGUQZE#oQ$7D`C5EK|RMgdyKNekQp>e(LalyryqH5Tq'
    ';n_JZGULIjREFLRIU$t%k{O7iqLzm0CCPnU4)a545Aw4At?fMk{rFCdubTE2MR#>5{=b`s`UPM&h(^3$ZOm-lo$mU6zAGC^-=M|_3A$ATp8N($36'
    'aX!l$Up;Rrxj8Y$yi<ag94kP7u)!6`A4|PGU(v8I)3F4ah`&GN4Ds<$wd`YZ9avTn^5;^%cs@%B0+%#BmniYcoeXsk%%2wq00Jd^-05Lord$KwrN'
    '=!PTvCf*mI=R-Y3DY?s(q~F@8@kw98k_^7M7cTfZ}qk0<h!^9m-J27$0wF8SXiEFqbdOUGiRY=<=cD+rHi<B3wTF^WTqKN`RHPBA|UN3a=FhodcD'
    'XW<k^QHaDitFHQiGOfSbRmTJyXPYRJlbK&Q5AU{y+ub$6J?t<3&{<lOUEBU9ylGn#?Cn?H%dz_UK+a@?XMosI?$7C=0);WENv|?GM#2>f$i|1A2;'
    'wP>I7coD{K60S)6%vG|9&xHf?9Y{q*fw#*E7{}$@sGjWeio2GyviPLpY-;+p4;-p^I6Hk`a{D6;ki?ZTH+RicLdxxr$@ZM{wJ~p9v<$V^B72NSpw'
    '@OlSrwCrT(zCUp%h95g;^yZk%ACeJ?T2Qu-b|D!Y}<`#381I4U{mvEYfj^13-!2219B*zB10<s;z#<^yUz&7pIWvjFHmKqH6sg2-6vHveACI*k{U'
    '2!6X{jX83zo$x2`G)Cg?@&C9$Up#MP;tld<M3#wCF5!6Kn84_FA*%fmXs^RL=N1MNSVbD~^gP!s&v7l~`pH~AZ0+CA|4YCYFPTg*w>a_J>8(LcC)'
    'jG<TQau&mkZk7*@v^_TgEJ`S$Bev+`1ys`Bcn9Z2QLr`uq8RveqESaIScrIEBRAx!tiwdB1@-9w_?upY$Vj_;}Db)L>b8K1V%ON2V!JMN+73`8>C'
    'OKmX4^lrgV@&Ft6mlbv|+6)`dKqe+?0$5O^!KKG(o4qSYAbKqF$dx2iXIE&LiZu3{4u$;J^gjsAzqM2npx5(gQfo@6F@3QGu)<%APaG;lTHZV}`A'
    '0$4Raqe7fst__c>2o<>JfFn?54%~)%ds$u{TK2>V&s7QDS$tga*n;p-OD@7zf(E`2%n8w6T9z74*UOlO@BY%8|?-s{uH+b`_389#R{07Z{oh~A4l'
    'dIS2{sRP_%r*7@$iyn(v(>k<TRujx{+ylIL$epvWE@c-RDSU%=f|uf>`>#z>CwxjZ*!7`xa-C|PXbnY<?GO)SCApsx;P=nc=M{QbD&5-4Mbb|>`D'
    '7NwRuDX8X5CA3@<s6SUS;%%8#NE^XG<46+i{p?4|d;uvy1`CeBb0vR2ZY4nU5JKVrE;m}t5?~?GZt&btBk~I#D;crEhHDd$JV8zJtXmL*@gpLN=f'
    'BU7J^A}_D~T&w)IoRv^_M9gJ^50$P0)ATk9E8uu()~gARIuLlbHT)At9BnLL?u!)1Qy`Dofo1fXE(O#tf?^Rl`7aiP5)@Lo&Y+uw>ZT+yv_Z%x*B'
    '^YaNBI*zaUFe)h4Hzdz8|a}2V~<R5@avz1C?NuHD5=G@=MV>!dFfX;Tz069tkwtvA1b{R1q$KkMlyr93I&tei4#4-mKTOkoiK;jfPX}3a1>dWinE'
    '{|9{8=VXyi5pae0gXX`N4S{M-uuHTe)GJgur|3&tq&#tfepsQW}z|JYOCwxEiEHd*x`;b2ie@bMj=N+Sfx0AE(h)79sT{dmE>Aw`z}98?)U>fMh?'
    'kV#s*&N!w)U<+obHOObGyT&~Iqx(z@`>!^UXAR6ecB7oQNe2>hYAL6L7E?mr+$EMdaY*qRun?YW@k4U0`pT~dC`sZZ5wMq5j;dPpqqk3!}bkBj`L'
    'yhs_xB6-_G;899UTw@2ZB_8W&Stu37phJsb{6@+uFnmE8LH76;&vpF$xLha|n3ysAvv4?mVBY8xsQ~26Vvao4agP1S+H;WNWecq~Yk_vnDoY8CAt'
    'fJM^35mgO3u*kdY`C?u2o)a-b(DxBJw()%NdljD+y=nUdcUYDb~*wH|BsPzg<;%J}}F_A1}<ZD}v`_4B%TpzJt0Ei4y)k%>E#WAjTzsauJjEC7Q6'
    '|J6sr+7hk*IAG`9+2f8V%kS~aKVjHy01Adp4=0;5_o{vpg2jwgkl$Ss#NkqO|Gro|cV$WqziS6f)P5JY2Xivhu{LuyZYNSIG+GNac;$fVP-^PFc5'
    '43H+H=te63P)^L&In?X7ax-(Ao}0$=a1)Y%FR37&?z7g_nn0%X$%yf0QCNivF!iBw{wDC(0~Py7$Bw`%-|N{wd4wM{2wpq@8^4kgq_J!V$br|QJd'
    'lu^T;i;#<aQ~Yk6}v?RYXdlvrOk^~M<(9B?Tqc<ZqrOvx`kkk#bczIcPMjmnw``t_1ppJIrB-s@v6!>}a&NuC=&g2tt~5>)R%qszbI^^Q-@=AV!M'
    '6F|t}19&Iyk8h0SAAGM|>)1lz_G2N#vdm>EXXE0~l*og~#;L}&4{!~~;<1u1p3hPesl>)zYH0Yb$(aHm#uo&h#66ql-zym;ja<IN4N$fTHXrYiye'
    'sHqE?gYf{gEJk^SE^=*`dV6y4<yHxTLo|Oui8C>9Hk)qJgpSQU|^p?QMc#Zg%I_gLba#=Z^gKxOamF730qGtkSlG1nTgIA0@1vABW`4(b&<kvUI>'
    '_&ic<TPDQM|m4iQcy?;L))1GYf)r4?@b*wS6*~Bb0XC!y|cb#ni4;3Mf;)RohDCD;!&pmO4bDR=sUCT%1|L@2D3DIun_jF|@9JzmiXkVD)$$S#D<'
    'iI{wGDrxkTOX28!X5miL}DsFfnijFgJT^Z^rkPK&sq{ezm(QvaxZx8Y#W(+L2kC{cL{TU(~B|eQ*fNc9EmX+WbN?gTX{St@^dABKJJJlq!RPaY5;'
    's<E|#*;<g8y0n)Ci97@sZW=U(C4-UVOtn*(Ad;J~(gl6d}pI;EG~;}@2Cah_d9zdNZ)i=l4LsGg5?3}*fXZg2w<Tu5XPrgE%LTp=8ac>j;3eDMLR'
    'S-}0tee(C$Q94Y;CZ{)X8;*RO!ecf2Xq^abbsgo7#ck~%_<UN(yw`4hv<dwEcvh07g@_P0l&h7z;|GAaVEafRvn`Kh9Ai_CvdS?BwSVN-q>4Pj?<'
    'PcD#ruy<`QigMh4r7%@seyP_Xa^pxXbpyo4M@AYR<7KI|AfdPCJeZq$Zx<;gVLyihk_M-_Q32CA7m>j5lUbR6Eo}<jsM;?}tSh(PK7NSe)I%oSSj'
    'D*v15YD;~x4!KeM=@gnJrUs0$vW)|KR%=;zOQ#}%cKbEqN`xE9un5PIH6A#HQ&qy6wBSJ%*mrwuq-6wb+oV<7e(bMgr*tflKbIQ&!!@q9P@g^Fdd'
    '_sa^d9)NN2)SS{nK=?tJo)Fr{o;8IN3D3oA_o|YlL&)mDld2juckEqJA^#mRO9^P@NQy~AR;Yn;oTjKR|*FRyB{n0^YI1+x$_GqDMH27q#j2+p7<'
    '7OpYvlOqxWbM`P{mwLXb-Imu-I4e=UX9K0?_q9&aWn3s^nf3Vqy9V2wP6P^3ijyYO<nLs^B3mvV2M8T6%P^cYWhX43op^W1*(ymBFqq%M|3+rhej'
    '@<(wuwR8lnjQ4&n<s3`lRi*SZgfoF@E@6O*T+V!JJrVoIQvQBk92=5X`O&I*;fLQ^!vMB<k2$;d^T_rt<7r_*CgxC0y0ZKOUnzCyQ@VeBv~zs(d?'
    'VQ$Y{jS`fvL!aOM`#2AP#Vox%KN~EzJf)5f+EXMxo2yw+u1}bu2_;*2JeD9K|ml7ahp_k~ORN<YoPw%`;E~QZh@*_}G&X$R2we$`T}NC(Cy{G$LS'
    'LF3S1mN0#x$<JJL>J=k3=;LTzaO9aY~5k!i3u45kS&`uR#BQJWDP6A1sKKw&}YyMcrpN}_M%KJ;ya)>jRK~jjZ&u%Q}+sO=%$4a*Mo6+;QBMax%H'
    'Qz@;OmZDVeyP|VpYP`H4|Fw6i;@JSih#@$3^OMh`Sw)#-JTbDFphN@>czXE+rd13(9~Ydd{5ZGvw-@|<3VIww-idjOrcAh;tTm_R$<(KJHq2lIz~'
    'oL%97j}?7?M;cCwkL;?4$pc&y}`=d%=zYC3~9tywE*Zg$B83`w7JUhe7nj%rh64vD}PtIt0B?Wx)%j|>KruNmj|JTBin|IdF-y`_9@scXyaCr`~6'
    'Z6c@VO&*cHW{a1WRVAfX%5^yHnt5W#j}Pke-%t1cY^uniB!kW8vTx(JxyoD~McBs;dWY8nD}5-a0nJmAs|9$-+q^}G*XK&UcwVfMq*PyJUTAJxwk'
    '{}6EI=%6-epjZd48PQ0f`0@5oQE6+O);A0E~ya^=c+0{b<7d`|)tT^5Wp8%3p{xS#P#YK@|{$z;{1aQkQ&aOhMibhc3Zc;G(<zqceuP3hnU>Q@(j'
    '#6T{UNq6eg~xrPhjDTzn&0@c08N=Dj|nEj<d%Vr-fh8EN1;O7R2^GBTW%?C8BB^*FB&gwMOp?n?89IQvB^%*j~vyNG7CR}eq@$jW$6H-wbw48)w`'
    ')7ahSD!#nKXnjFBRK`j&gSHU!8sCeDrY?|(L2lrkSs`HoBjxd)I^7qk7@Ibxtu>w?-w7inq2AT_5mmNt2Y`$YgNTdGJQVRa*qqGLotVamCp7XNVC'
    '%m5DrCRAz|ako&Mqj+UR!S9e|y~T*p%vlKr9)C0X<1S`Vc5R0GYS<iRJs$89!BFWB71nlx_wudwo~PgsvPbt0W=PCn{HSNL+!1&w&i?@ogAKXE_H'
    '$!u)s_M2uvK@k(GBpC~}ex92@pZA_F%;0ohWXHf^@eiKD<lM_3-X2R?-tpXtz?&NpL_~Un*N_@;(8D0IIHRBO+@H^D^av7})6(J)*hNEVg70idfC'
    'n|A$4d4QGvun~n~E5jg^&hLj0O~JZ+!ZUUjBUCIyi<yQRL>ne@+||FZGr=&cEqA=NqZ@@!*@QPL{lISaV)|T+PBtB|pAD`YylvKs}=}E;0{ZaGpX'
    'c)v6ruC(F%FJr*+pe|c6+R*u|-B(=2}UawUSU@Rwq>A9G1J|KKilbX@hZhdY^5^ufEK*HGL{dbea`Jd1~_Y%@qlelYPy-{=p2I<#0QF5M3`QrJ1i'
    'eRBD<U*Z*_xQ%#Hw(T1+p{vzb6aX&0Ee%2G+}Eo{)q|8x0ZeG>JmvoA4~b>`79+NT#isug9{Rrhfci>@!pB8a>yQgG7wu8z!#7+7CTjdmOEns1gJ'
    'Mm{O|>yOZn;(|D5DSL3ve?*WbNj2Vau?O(ylc%tN;Ww`Mfy$(mA+T@(aX@QofIq>GPj`TKcq$Z<{rpC@%H{|I_=s;F%!#BUD^Hxht-E4e)IswTWJ'
    '{T8fc?rg<;(**Qf$rsOiK{-)Zn}-B3S)IxYE{p1XQn~eh?n*Co(uV|TH^M+XI=oM{_!R8qSZa59nz(PC|A(JA@kA#~Kj3o8V>vxJK_^OXlF9RaET'
    '!i*=$Qf&CenPB|5GF{-Yo7{UOInZ3SWI7iwQS_fP-K%wuQ60<kND@Ng?fb0&u?J%B(q;AkrH<Dy>#i^`(5#LHll5`<VfJ^MQXZdrocu!h|m?=6Ib'
    'If1OZ6_w4pu%*aOQ3dA&nKn~PU9TbNax~l=m@ln|Q=6Ns40gu7gRvmyfMt9eml0SWGI^a?tYpJ1IxisEYB8T!8%at1nf3*ke$Jzbj@qcSei)+O+2'
    'uEN61mKXKFaoJQ?2lVCqA1XKe^K^VZSm)ygSlPRFVU&-0pWi0e3v4t)NrURqy6ZxI~`XEWS~Ol!{&}4DtjebM1j$MJvg)pFlKzAO+VcJQN;Z26As'
    'KI`47AytKAFw<0R;0+O=rzzjMax9l7KL=*dcS5hQ?tbZar()etl;Kl|Ihdfr+De)DKby#+H1Hr<BKaiP;Za<%IXS~AfuDaNd0a}ydiWQ!%JQ=30R'
    'hHsw#hZPotD#Z^h`t>S3S8?rD%v@3GuIE~6dfVH|x8)>GWS)LVUJ$96uD;~uBMtcK19k?*aV?)u##<Aea@E;0b3Fpx>mAb}tG(1j^NkmZ9P}0Ptn'
    'JLs-^)kX{mt_ZFnN&}SXcysAYw3g+hG`_`8QGTdPj94KJ}Xkk)piSQ-@0crTiY9!5^;io9C_MM9@m<EGg!!Z|24;oQwZ#%;IAu<5B~B`8Cw6l$M~'
    'b=P1|`a+#lv1>ZdWPX#D084Y);+xijt>-__<@)Fzcbqv%pkhGr36$xGbTex}_1nmmeT7f>msK1}jN}P%bV=|dXI3hpGsPiRhRh5mYz8*_yJ`{W_q'
    '48M-BGGVLJ`dExaH)}oKRT<wc|J?YhP=oK?SyGU&I%T;EiuvDE)IU!lo8Iw9Lyy<B>N4#nU5&#FXKxm82iZQzj)j-;v-X7-Oz|U6pu&zgw>--qm1'
    '@g$*?FhaXIcn{<-x6CeK>XPV{iBA3(=9&%47p7^6ZXvdNE5z2JweX{Bi}Ki|$9wakLsuafJjv8L4gZE%TWp8l6G-d7*^S6t=QPRIo|bRmf+98YSL'
    '7j@|tf2?JM3DTR}a+5Ah)7w=j%xPA?ox6`y`^^XbHD1$ZM*gd1%{;cM--UcSvp+GvkJXGoA;ps%c751-1M|8yRHP2dR?@Ti+?PKeubE9{jS3lxIy'
    'F6@QS7cn1S=pW`LU9F+#qMWoYkA0UQVLk8wWAxKXLp=7V^#WPEZ@*QqxQsCe|$GP0pR5E|K=pzQEs08B_^fVfw>T_+rMB;HY>7`@7{h`8yxJ@SEr'
    'Vg}jlVg<1?&MYbW?ej?sY(Ait{yv+l*)Q_GR5#uvJ%uft@eA$G77;Jy~sA&HA@IPdQP!u)4=Oib>Zp^6{eI^Uya6eq&fGo*u5HL%=1Ok=rWe>yY%'
    '?YHc^I7Bl=6T!UPeaW%5iq`V0t)@I@D_*uv64{+$;(bKOl~e0GuE2=29dSgw8TImeLtP?H_zLYg>H|=rPMIPF`4JKdWY$VuXa7y>eoB<0KvbuiaL'
    '<ZI!7##+*a$RHm&C+{^Id0q-qVD@>(twrst-DmVEhA<a?+(Uhk{}ODr*=s@J=>Mt{atP6dI&M90sCeDQo%l0YR?fI}I2C3Etx(wxh?)T%7~FeZbV'
    '5JWy?_V_08xi2}Bky}WEC?S_#{8-Bu&;Ns_CNfZ57R(7yG@#tDIWlDP@9N$Ch9?t>u7nJdAt74=(!yrK(A>xi$!YeF_w&UEtR**HXYWi_vH3uLP&'
    '<548aaP6Htu(rueJ2WH^pZMABi(r(eJ1$<UURfo*!%Z;`x7Aq&dsC23267Fhz<lvOY%R#r9ZA?a4$UbHPhd$=10ZpB~Qjtdic`;JK7<o)3vJK`d@'
    'uFu()_H-*PU-`E>e2PcmDv6OLv!kH6!5Z>EGh5uq6Ws(VZaEXr3UHRhqECn_dmorI`+Gq`ECc2sB04|^Jr&kDm87r}9uhsCAdv{dX>psedy*}*QH'
    '_!hQ+a+R%p<ik0h1xQnicgF|*{7-kJl0ZwoG*z{H#>=2uSAM~wM3dGy3^0KeDQqNve}h5Kz-IZwm)TZCY-ow{w)t~`27y@wc8<2t%%dw<7*aowzS'
    '$V35}mC`QmviAt4KcBicrgMla671mBoix!~*XwTwVtJ&ivN@TsgJw$Ze3;jSXeZ1?rKEnj@#A3>|Q;6o?{<4!DjHLwbLfcn2amojb;X$mJmnie~l'
    'S1q_XNZaVLO>trQSjrb4a9n1|{N7-<7B@taTq?4%^#G4*@BIcTi66IR6KyyiEhT*V24utljvq>7e?OgdtTpsA$-h;=RGrz0xw!%D_whKjgPx#d9Y'
    'cO#Nuxvl5PS$GOUFfN;Dh%2#p4#j(g1BSD%N{+ZAl@M3X!IHuA?@EN(>HnJUgD){&Q2xZ15XgIxha>$o~EKKN)M`lpE-i0_RV{l!Orz?uovF^|_A'
    'WlvT$U!f%>|M(wQyFM6;C8O7s+`1{=_ga|aj!*3}<wi8I06(xu}<ya%oJ?`=dXdSzkF8L&M6N0S9<W*@YHi;eouqxj?|1YUlWsTYf!#p3%3aOge_'
    '9e3ZT@Je6`~;WsrQ1k6cATx)(pi91Pa4Xv57Ne0ANYrtJnoO2V{6V4_Yjia?J>%>kM}eP3fZ!`fLfq<-KDXfBu;l#L0q%Xm-P4JSxDLkT20LDkrV'
    'w0u;!YWNzU;od))6hf4ZvP+y?rfWKgpOsJAKcrR(a$EC2bl{Xn$Q;Su<!<+~I{uvZjaZwC2c+(x9YBJrY^2%k_VpG7yU<W$)3{4}#)Ja0!19PXoH'
    'MY%E9VyB111}DP6^VPha@<MfW6{v>fun%`1;E;})0Qp0-^_%DI%19Eya8i#v<&7qrmo#qo=dKK&teX|$dsXxzQfmj!Y9Gh0`0Q%<=J|hRa^_Mc*l'
    'j>SN$F@q;9Q|!0&|b045z$$i~6Wk+Nz5b+&P)c>K?TB?8D;!{d|_95M4<)M`Jn)0v&{XvU@YV=EqXjw@<z`=cPOxIAUOUlY&>fUj>HiM@!vTA8=Z'
    '3@^Om+0EDKL4MX{dBJvTxuJJs*qkXLZm}siZhl0sarJ|6h|8<sgf4re@K9HpxN-bGgvY`Mk_jpA?tpj=xSe$>Q>=yHO$@H2jG^roONCYuS@)w{N_'
    'k?cz#~b?daW5#@2!iL{<oE|Ujue2;kq(=l>li+Hg%S}U#OqF?OO@}Irw~7K;yzx_7tdQsaLXKI?ID4~k`Ls6itu)7PsjXN$J;0477!A`4}x@t<xo'
    'p54UcL1cr|}No>k<`MAq;WJUP6>+B3^1`FB*l^Rbd)QYI|&(0&Na9@S;Xw$~kzd%1hv&!v3veAcoMPhb5>EWHJtL?(IbbklNw{WAF0!lrDryIy5g'
    'Yjll`NX(Jl7mD4DK%W=+tLNF2y}l+kUeRt00jNx9K+7aZ(8AwqDRx~Evz)F(ol?i4eiRFh;@~zW$a!K2-+jPMUNn6KQ^C+h>P%@QMUI2SIYQ&FWX'
    'xigu`z_9+6>qk3qNFy7Xf{sp(K*a$F}_Sv}KU)SS)l~+qaZkO?WrJ&(YBk>%*or&fjE4FJBMPkWD|@nurh>Pn$5~{Cq)QJpWH#O0!2Upp`neJ<TU'
    'WPZTP2-OnA_x)L~Ue#PQnH-;v14t(Sw;Mwo`T*@~e_@|{q1HlCvC6BzJtAh?nov*(&5iEsGNtCQJ9|0C?DWONAnJTp0!k=Vx&pr9#@qdcyTTf^QC'
    'eY(Vr;#t^j2yZkm&ZbSeGs5tx=79AkokvGhkO{fmLiWj&2u4tKmM;>r~FeH($pGeWh%AqWB{81G|yvlkA+Y_A;Plb4X1KW`=bgSF^GcC`(q`4KW{'
    '1QTV9^lx(zjM!B;dGXYw0<t3+fe4e}{fR;Qj22L?#Q*W*X`t@_cs)Bjw_-;Z}s%4AAVJF6EihM#O}HK>sCj)!S$gm0&ydepz!oTFYd$!y3jn|T!5'
    '{&7mac)rYM(`evJl&F75z1Axu49CbmH>9TIT1Nv@_Ea6Xy2Ld|i7>D!j*oVN?>=Bt3inj8E&HUXy(>QpY*O?PSw6RA+#QPN+Kb6oVr%XCNhRV~1~'
    '+B7=VHEizPCpaL2u4J#BAA<LDA!k_D5t;=-O}AJ0B0R5L;h<a_nQW^RY!c_;NgH2VXt^UP<7LeXHinhPe?VGPA<^sS_SN-w~foPAH%9Z6+8@dT)n'
    'pJ)MC3qi2x+#nU)uU2SzvxTf5(mnh*@8cSmMxWo<c@ugdAo)xktaLle2z3H0CyWX@GeXQe)5B$?^r;<#MIyYOkacI-bg-bdb>$d-PJo5-<L1P8MM'
    'Gt3ZJSgj&ltD}aQ<*$J?#~w=aDfu4#hO9b=5w3hy%>{@oD*|9vET7n&bUOej|$Z%owS5J@mMc2VZzbbIjv`%<D193FDH4@R2Gc2O2I_~cH1EmS~)'
    'c9V<GE!Nx}Vl*icT=n}#ig72Du6_vHL|MSnj2J}Ws%J1x>A%s<DV6_FIjqWo2-oX1Q$@f6}EiO~@3)a1zymOVXdJKjHN;a@$UmE?M1V0jAw$tc+d'
    '{hGRpg`V7VM`}^dDh>&h^^?ryo#r>^@8R<qX?8tV^2PK2^i}6<CoQTvQUxToSJhpsgna%I<C)*H>^ALOnh#)S>NL6&rAqVx<6NtO{j?~5Kc8cg>S'
    '>A(2MLtqhdJ2j;qg0h3A0}J>3%Gw4msD+2&$cq|5VuUB4d^sh8B{A5XduF`r`S-ZFK~A;PUb+{w5tmYlQuvt?!Sk{D!S@fAB_9hz!-Kg9|!YftH&'
    'Q2cOg5zWM+VT{2)|73J2o$_%>_Cdst5P0f$Rj3n7`p_r^V3uF@0(auVnX;D5&9egCFUpx<`m7W2+X4p<?I}7q6W)~Xr;}wk%Ung9lsH~n|78qJrt'
    'xzxT)l96%EBfYn=T^z`s%otpoTo0l9fhB@!fB7oJOWy!#+Si*i!kqUIm>F(wRwIJ)Bk=ri_jaIH*@PNOd_2m`Y>OG27zOJoevXR)Eak1dl^LW(sb'
    '3l=0zy}GI;^``&`Et&u1x!qZfsf{QbO)eLQyb+FfW&zbUmAQ->V?-eEbAqc8}U8t|B^Ae`-$e3DbXdEBeXsW8Pu87$FW^N|Fgw;+o|(e*s9*VvWZ'
    'euB3M#B+-?aQIteJQ2AM*1=cLcPVWKC8y#o{(zQd8n;XjNBvkyO^UKv$Usc-?)P)BJBUidZT<l1iRV(j`G7YhVlr{$##VRRgSqeui{4^Y<?Huq-c'
    'jwOKaaY$1nE>4Q7N*CI&be51)eVXiw{T#h58!bwWwY#K<FaPwk`xm?&nKdMwT2iki-O`MW0-Mcg$xp@M^5F($BN|)dx~q$PtRAKih3soQ1;gOGAj'
    '3Jj3K3_I4SWG7a`k>vuetc`fmwXlv45Vn`o$<%`D)M`B+sq}A2hf2g|01KpgMDM9yhTSlyZZw-4kKpIMSl_;{6Mmwm#eH@r?p8r>o{rph-RPNd|U'
    '_kmWlky<d^~d2I!G7>{rC*2|20jq$5;LnswgY1O3|IbsK8FRZs?zd0M5_n~h*`)*8?dpj4`W;KdNUs&Z<<dfnHF3^BepNdi<0$UK9=(L^Ogeh0dV'
    'c~)z9j!WCc4}Q1$n7c|<Hluk%qMsK(j@ju<1lX=z_*$dw5?KPmFxeL&1&g5hasqDhJw<fPLO9epNgl{fd_wJiUM_(`MdC%Q@~6oO!EQyWa87sAJr'
    'HuKf<g__m|OH@|OMu@m<eAC{JUDUy2c`RiZms~cA0twXjxmVHpPiz960LV%oAj03z1C5CXwO{B)n;4#G$Rx}ZQ_o*S$@x9BZ@Y^NENx5JM^ChJQx'
    '3zW(y21Be%O{TJ`mf&ZR@y@pzI0^*~mobWZh$NbYeahbG)(R<<+5sN-2ij8PbGAoZ{z3NdEf7j{kiApFBQ?N?S+D$#bV&QTroZn|;xA@Eb{329B*'
    'u9w=j+SB#U<uRA_2)Cx>}d@wV<dOq9IDC?r6I+cd}qu&FkuoLanCLT)}Fg6jAsjLm}f3zK!AT$|0fbH>VbievUmXlN|IpJ2f(4kVx+4)>8p<Tkc='
    'Xyr;j~zV0f=$(VJB}3$E3-;w8AI`LXukTyKixRGmSEeajxA*tg|D2t;n6`C*~rJad8Yt5crTOi&ha1pn5y|*VS8GnAHrMTec~S(sZGI?1&+-X@W|'
    '&*z=!)+Maaj32BQ==D+PLDVfjs%eSs}<NlaJO-}=#a_|5aW2t+Qze$tC)WuZgWNJf{610-O16z-NceZ@S87ZR1xv&&Xx>$RG!Cw(4gar<a&`{Dz!'
    'L4+z3>2@;7-o<P*8tKg`BCh9iF{69e+`@!}N3G*%gyvw!Z>a+Lhe*ITAL!iFBz4nZESlef$%5Ns^snIN`aMi&{?B;7s+{I1@+juvfNN4et-fV=7?'
    '>g-0wmvjz|b7S+~*z|{3tr#8&%J{VnRXj9)97is5fm@*4i}B>{1;gS@Tr`(;FIlfA%(i^#Q3}4$06dF$}&_E8`lbsk)J-`#l`PkRaBt3C*8iS1VX'
    'eQ|YU+MHQL<Pzm_%1FFKe@$=vjYHf{*btMwz_bYOs9>-<~5fkow;qa#1(NXN>qG%D@P0Fh>`R~Pi^ZY-R>5Z%cd8c)R7If=PF%8(enrZ!5O2y5Yr'
    'B~~GVKEIwaQ(mWKSKIjq2sZXZ=TOmLIilxD4PLudq)kL<&qWWpc`KOyQugc@0VXno}IMzVb;O@W8%V0G8}V1k@Hh0`isZ25|l7tV&~bAkt6Ms1co'
    '>=xzooos&0<TWgNy@<@i;<Cn{uoN&dafpUoCuJ^!yt^qsZ1cI<Jg5zPv6!%#1?+{b+`(#^fpCYe-W5_(9ziEeHD!6_fpv&j0@^OSyYu+DZ#CCmeQ'
    'yi_l$T6O>K4YiO?2GBkE;?T_lX>|o1uPQj^+*}_s-oJYOpGNSVYL2$ErKQ;Tq(EkIj+dd)TE8A^sj@lD4T(92Fkit0lg?vRV<(*6=78s7zWD&J)m'
    '8|0<!)QoOg6GrHZu6Rd5`Nerl4%tOU{^%v#Cfm@yQL-4kPmO`h4@e4l5-0r8BY>oEs1?n*UNzK?Y7`{&8^Ew^<>DG2`Ev%*udn!hoy19q9!L02&B'
    '<&8f4-+5Erx(LaJuo%3oar3eP|Cd<<&KNBWV7InSyCW~)r1vGt|XQ1$$uh(I%|5X5#x{NR#jS3H27HURA7m6jLwn}_PwT4(hv$Bc0?bP1T1wW5=*'
    'X*^>*!f4Tr9^i(h(9ai;*h>v74}}2VFq%@Hvs^0(tHjNh8uwbM_uNBniIx4w_zMxR!l5Q4YC#Rpir2tw<g0yjG5gF_`Ql_s>PqmD^UqEcsq{ota|'
    'c|%ZB&2l>1kjy!sezO@gsYr&fXAw2S2^bU5~E2x2>1Vl&tExx#(yBssTJtYmahbiQxauDnZ4#I%``q|AmSUgh9#@^cfj6z=tmOx2(GrvWNX9=Oed'
    'XdFacA$6=rYr3j*MUYZzPkR?d()_&C94&dT?uUxOW0@U{uvbyqblRMor2Ibz;t)*(bhD*|w>Br>R>7U_AxpOz=15-o?of|^hW5m;zzcFJR9|Xq$@'
    'n0q?}EsVuW8_x3rs}dhZx_ic#gq=bof}{Ired{d!k=*Y-7|YftT!~%qq}Qsw<md3Dl$4a<2)`Ii`&oV9KBzSMJsr`O+&!EAAS?K`N{!RZpg@9%pZ'
    'ktGQNCrv1D%R3s5MjKkl#Q<piWBIZj_t0P#VAuE?;J=^Z6LCo39PfF!zJCInwB&>gM2q`5?H*?Z`-$ar-wiA4nq-s+<Mq@LbUUPHLBuS_ak3!-lf'
    'R;G%)#0BVuettrXz*C)utW=aoW9(tb+f$PwpcJx=9}itEzL25qRmPLI2J!03cHSW0Mv%9%UtJ5dU2XFz9^Xm;uF;TNqhBvPmmR~{TUF75pc15HzX'
    'sAS?<Ro8~W_6lTevB`*LFNSMk$kE=LZusH&x;zz#QY-lQ*_M^EK$f_+=!mR|1@x9UcA){4{SCpJKap2nlS1Y7OhT#8u4*NiaFCU`ul{2teTbFZ<;'
    'no5Y!%Rqqw(5eIVT?8&JldY+`l<0Nnn=>No9&K?66i;VisT`gqn*N)}=%&`UBkvfct=XaSae43W%R>T6Y;}U!T`=m>ZB3_=`#*r;SmcP9+&Ho|yG'
    'XN;1cD-Ro#crJ{ei%)bQwsw<RO~b@ykJVO{qIYj!WP7(JpSe@im4;ueE7JBjXr||8^(!AYWo}E%BW^)bAYAMfGU<A9QFe^BqpD!eK}Ebbx+j5>w;'
    'A6hU%XeyVZ&>OjcGZ~49A7P(qcj{O{0r}_3Z82|Z9crd2<Tx{?J_qIaIj(1BX<uo{8U?V!WH`ViLeORF}s&OxgcEvK9+u{9BF2MrRVO*~sNGA=m1'
    '*luq%)M>LJ1+TE9eOLwX)><{<Do&`Kdi9>xjYVMBWu&k4S7TZpnJ_NT?T@4%bM+{|Iw8hYrOyG)(`-5Y0RcH<FKlK_#<nth-wZ=oj4M;m7Fz<ihH'
    'Ss%~aZeZ6pC`y4x{2l?6ip)2rZ7e&5*0&6MMMR_wfFVqwx4VqR?Hq?i4Bkt5yfS=UZrI%Dp@54cWryy=b6cV~@N^hbl#PZM(S0U}jqH?Gkzw(R3('
    '_6|RFii3`ybFP5<f|Y+RMd|XYNjy!4NSQC5hv<G0Q#zmu`}(*tBL!@s8uFb$<`iAF3rnG$c-U#8mNixoXwf4fEJ**aQ3un~UZcFdy%&39HaYl9V5'
    'PO}g@{1siK@w73rWS*@2_fQ@bMBT7A>xHK5mVjk5z6X*50*RtyP3LJGm*LjY@NhloYEwCywxoDYKw}t%2HE>aiX&$yg;zU7EZ&s^<1T?uK%sbFH*'
    '<wt8fs!O2E-#E^l#OawJjU4jKgA8Tv~v>I^3NpHMDXK85bxOGlke%4>3=DaJ+(DyJ%LZPY(&0g&?RS_|33^6zOE-e8p;p(-AcsF<W>5ZAYj`-ZjQ'
    'C}^FY98$+hIeD(1K=KadyJ7>$@0%jL);F;+07)F>iDG2$pu#9D{K3-T5VNMRzJoF7G7;j3->0<tR0}dK=^{7TTZ+;14kL%8)ylH%<(b)bsFqN1c*'
    'FOXV@JN3po<biooVoAX%71i&(c2q&?chdt;_Bk`w8in;iv*AFaNbN(TtvTV}{IsU9T|+2|Z3I2%+>6Oh+)XpMQ}C~z;6dI(-snv*%v?RRPnoc}vF'
    'X)LpLk`N)m6#r$fdAT~{$V<yA$Wmcr!bFYTHov%fKN=Z~&WK+_mtfv+okw6DN>db1vW@wl6$CmoX4*>?kQWDef*Q<pGkr?KU>gJf+ke<Kj${l$mY'
    'q6Tj~V+gRzr~~-4ZQN*<3;lYX6rup<KkPjKY`p4rKmLRESZ=rhHLSr3aIUT;Hy932>3f7r@L5$pyFnIcwr1+5v5DCFPhu`fHKzGLpOoUERpk1CIC'
    '8(FuMNsWq9Gc+&Mg<rpzhjQ~5da<BYVfn%i##LAb!U?g=G##Bh)%!+BJ0!48169O=GB*M={(RQxbv!Pwf`B>o?ktIseeRFCLi;vjMXyywJ6=AMzk'
    ')N2O?TVsT{y4;x>&@xf=N?eHOB2FFuBQplF=g@3M$~3e^EqfdYEvq9YrktgnAYWhu1TXPmz&M^SYW|%2LIK$zz0A-`r4YPcH<1BR4cuDBU6%Hkl#'
    'K{p=eZLxEXHwF@_)s_@_?A)u61g@}O=*ABx_-+@usWf%H}pJ^Jl2qlv00NR|66!hbAr8)H25g5#bF>vDW_#~WogHyntUMc^e}Ni+s9e(NcbMa)wh'
    '2DIW@qMQY@XYseTk)Tqxzfr4>p-P<pd693%+sG+73(J)^ALFppus@bK#x~@4zno;@MNV~l#+lhp(#0v;>kSnOp%NRCH8IKCFn3A`gve$WfydQva#'
    'EqhvMT3>We{P@Z90I_cmT<XvT*XLA8(%U2JLt~zmGM(2RZ<VWH23hnNy#@R|95;(9La(T07DY1Z<ExRN{vUf(9lH!l{Oo)GsBMAn6eS6Q}0OTIkY'
    'a^0rQdR3p9#6|RI}Rk326WEC=R?`b=i$0~<B{JmB{)}fO*YsB0Ph$u}U>yqu`5S|G0V!d0wh3mMLUa8l@Q&t(D`|8|ycWHf(R4%AHyXhTXlIsGTJ'
    '|s@9PW-HNZ|K~|UL^dx_us30i^rp*k@H)t+#0*TRb?;EFxCSd_OeRIowvBOZ}X^9V-}#LXoj!2H|H`0xm{T$$k}Agtt5T3KU+@T+<})ud~C!-7kh'
    '&VY9Z)6S$bcOMb<hZm(Uk1O=2ariVIayatU{SvO*_bL`M;Gw73>$il5Yzs}kwj$gW@N2IP?r!$}2|%*g<)mX7Qu$BC`JcujCXP2fbo^R38g-N$2v'
    'qZo$rxfY&TvA%1Yoxiy`^j*a&S0%Yoh|HvI2)^b29X?`J7gdVWB2j+C9>_nrN3ykjt7KBn!H|Dn<sOL0n4K$SoRxg_PMZ^mvp)~yTZi6^a1&R-AC'
    'vR32x{gA6&eF>OxuK??7R-g$^2MmOOt=`AQ|!k+kptjY7<)F?Fl3-O*Mt$w^OQ;#{yr3@*#T?WSX<${p0MYzxlDqW7Nk<N9JGiQf_JfTCysY_sxO'
    'a43td>_M$6%D1ym*G%nxX->Ml0h-S3IVKyfiY$0`R;5RS4R_NLrvBIJiNi-J2igFskVcGg{9=N0biU^Dq4%WNSEOE$6DB*Tv^di2Bm4h@VEPE3-I'
    '$1aGc@k*-+yoe{n4ekeds0_4#1hLJ(v-2VN3|&`PH37)1({s^0}9~C+?YeV^XWOA?XghTW08Z@YtR;%UBsUm*XHD9Hz72Q7L431;_~M;DS(mwlT`'
    '=-@j8y7pC7N+(b6KgT-wHO5XrnK%^XG~h~M}{ox&vW;)$p7f+spT`7-lJq#wKaZa7(#g-Y~Who<UAe;3uyCXg@rk<6M~YkRrGnDS)fiKX)P3@sMP'
    'c;(m9F0M-aY75H6dl~CH9Yu7_g`>&o6|Wub0G9bt5jU(Z+1aIkXn#2F!P}F+&>Y{9nWw?O&+9jAQy`eT5Zm<QT0@`(THt}?jprC{HC>%zSPbwYbd'
    'P7dR}r`(;x~+Jtr9vz*|`e2D<sKxtc4uZinXsin6b&b$rsN+@=fYf#m0{FeL4OgMJ9jJfF+l@g}3}Y@P={Xi2fMTVjy3X=p`dVG(w`Z47Js)DJ@Q'
    '5S9+DQch)Lbk66dV?<;O()+S~{j)1#?$nc0U{?n-*P=X*sYhm})Sq>~?QDMpix3L%7m~pEN4PXMfOmD%DCeD{4?^PJYw<V>#bkf~ac|MB!o%TG00'
    'cUsts#lDJ9=dtBu%Ms@Se*aupBM`rZMXzaRspys9TAMA2!)FPmY_{gvx?n`fxy$yV&8x{3ftw{DxjgJuT2EGeugz}43kzDv-5e5F3@=V-CxMt$J~'
    '^{UsV7#vxXUd81~WQzEE0^br4(3o$Wb$RSJ1zsaDLyn9U>>4kO2oljqzCi3s7yUDT+>=PF%^TlgV@m`A(6%~PnF1JYWVVu$K2D1HnFse~vg5R8h|'
    'sNHk@b&=M0o@3IBD%;$-*7WdXeJ5FV%xpmJj}Q$X@zkVxsCHNYv5|xL3*~O_eeZBN!x0doZ<#%PFgl#;G7ps@GjLbn%F0nLV=C=}ifUxb#WT#0MG'
    'hIpa&e69mD5h`!8}PhPRd(44bH|^S25&`VqZ&Ol^$EQV4u@6<~Y>G(1TK3;Hy(8a9@04FNI_HA)}DmfE4#T7u#3f!uLapX9*w6Y{C;)t`i512jW!'
    'ia{IJQC5CPMz47bLPmU^$!v-DykU7?C3_RUi7%5WV*a^(ks}cvS9iX{%{=pDi(^Qf4iJ<Q+MKEuePF<DL=MIcrAIsc^p314MKyVdoNS{t2$+h-+g'
    'ND5g=M7gkYaj;<&*jug4{hU+Y-Q|%Hk-P{fNAC6IZS|iiFkD#%^t|o-RQk~_yqkZ$4;FfG@;Wx7CA&;)ayFoG+%P2w@TnaaBeP=8BNS)&dW;+uI!'
    '9$i?O?2Em^AZP%an0*<SpDY|xA-b2fm2ik!XGt`9K)Gg)FX4%3^fWu3T3{m$f#dHQXS)_3Bg)9F<W_S^_evi35bV!9+>q}qdrXp4d~#J6cS_^GpA'
    'I+&UA{n169iYNKuB+TfLOyWH+?scoWTq9u`sv4P8E@WeBtW>bbr3G`!A>AK~93tZrAvocz-Ji?gqFdK-dWR;D7&FzAwZ&PgR#Wu}FtT!MM7dN9)&'
    'XW1z_^u`fg#Mg+s@ksbD`U<4ocK<)lIl+#$0hK>ZbJuRJiuX1sb`JqX1;#bMMy7N7;Ty(%@gD5qfFBQp(%PPlIB37SAEP*j476Rxo;MO;Y`=lH<@'
    'e3hf=2OG&=UA|!6EF?g&znOFn;b^Cv3OO7?xV79S2cYFr%mF6>dg!J08ddi-aqNb^0eEBS?%$8w9y8?UinX@5)7P^k-nzD^~H1pO8;<s_j`Tw!ho'
    'vt7;y0vQ6U`6S!(h<pSFY>X}kpp$u&DBZKv5s5eDY~H)$CapihYYSVT(d*OTH0JIEwVJSLv$;2AjLyK5zDDc@2E0|>7=+ZODA-#&>SJD7H<@i7{K'
    'aH90&(F8Rzq*?{8&Wv5(Dz5_gt!UW0ZIoU#o*%pg&NCB>k9SG;$ae?V0$IIGZY=`F&hTuaDUmEg%2(bVDUvmXWIV8Pp~<)RyN?1)lT%+^ZKp)Gk&'
    '+dc?{cvH<eWdTOg$kpNzO4@g<y>+RjscctM@T;#=O^RmD2JyQ(4&`!nUgqfmB#}}ZSapwb+H=_<Zd7?+@&(us{<X0Xm12N5t6@HD`=A!);ah58%I'
    '&R8yfiO0#Sc;9Xx6(y$+w2hW)B77AtOiXI|ZNWG+Sfhg<Nawqf@W;sFj=7PE9A@?GmtToJ{HvJ-w<SkSC}y{z4bN-4BQU7Ro(Lz>-E}jmwx~kT2J'
    'PN5KZUp)7L@VV}*n4A_@kaHBTsJuYY4)Sb)2yl~tRgRO8f{SNMoz14PD*62zp!fQzod^Yc>Dwl@53peA}aKkQh$ZO%+zs$mrxXI6rQ81Md>=(X*I'
    'Xf#=MqcnLBG|0p?v&w8gCSJAAS+L8MPr`gJ^&jz)~v;2E{}gO;b!44mRI{@*2>n2WGO%$k9R(Fw>GLIYfFwXwW6$hh*vI9-r$RQ!!VFVndijwy63'
    'mAruc>ADwyqV=3mfrA<tP=Av2_d*Dc`vU?laGn_=q|{KYV}82#ABI9*QY{@lwki^Oz6Qz5hjZ`G2VwrVVItpl7IUseQCLQ{wFNkH}*hr8(-YdDot'
    '5aS9_cPJd%zD<P}750O;-xz>I1sL50#DG)QBRuo6#(X6-KYs(R>pK|&n8J<E3gcjsxE-i2)6(uXJ{@v{b5Z>~0$GE#bfKJ5)#Mi}$85w6kpL~UG;'
    'VrxD^5f4@s9JID7nlX9zUk8C>ZS2PSj6>GC%Cm$b{kr!3R+Ac)1xv5G88uU<sJAdc3@i;|rC8QMvCia6yI#rA(P}1ai{J7p@y3F+<jFZ#_2@^Gb='
    'pyk6ZQEwXbz2lUmcLdK3Yc{t0F2sv*?Y?@p!x9{MBo3dR%Ve-K7!WushsDO>>!7#;9hM@AaRK<fQpCk_Am>kMeDK#F|xz;JQHXh!JOD2stiGp;5H'
    'tJ50g;;f2?))%D|Cy=*<+<rX!NGkot%QvLauxlgYWsB2E3`9R+i1;+`I<T&=<0zJtF29lcY;P(6K8?(#ZgpF7kdr&Lu-3)A$a%q&e00Hv=;GuiSJ'
    'n(`(9yOn#Km+|0ufYb8%ti;+AO4iUEg?q%PjgFzMA<WS*D1rIf9ZH_@<S8=cCl&IO94de?H);>&Gqht1Eft-k%$k_dS-kj-<EBQi=PDWmuOm+~*5'
    'I^wPqJuAm_>5)8W%67mI$*rbxYv%;qiWx@_q}dX9FMDO$OP=&1Q3q$`<^3K<5?YvjbCT{x1vwyzQ%nZ$oYdntHoU!aUWiKKpz6OI;#Ey~`5hbGhn'
    '$0@kjzalpBY+A2izM&#_)kr;9`5R;dsWSece!llhS|$M4LoWp^6w*dKiodaa&N6I!Q{OY8)?A?RS>8y|Xk$u>?H%V~gW#oX{*bMcQp$g&C4GOcZg'
    '+J(Pi^mEGhlxg?p~#=_89m8jY!_|T0_N@MHj0Bd0RDX$<dM0>vT*_Mh4P^h`L`LV!SAUW5o1Q9;IC)>aylP@V$EoNPgR)35&C2O`V0AnekbtmrV+'
    'FevFL!{i;q@_pjh1B2@m%}-{xcP(>|8^2B4UF$sWn$0*U_#qtonzb+TWutVl8W!9fJP2f6{M5ktaE~~7<w$2Rq>Qrsz+lU$$Cawap(aNWVwrU!~A'
    '_iYSc8UU7{^)9z)!xax=CTa1d3sFv6au=PDbxX^DL4s$0xta|uMQR|`?+U2>3B-W)IJY%0PbaE|T6O`qm_MCu)*K&mkYB>_)=lTOceZ<3rDS4U_w'
    'ITJVxo#Kek2jIceGBxyhD6f%G#LdlZ<C<6kqp%hYO~`fGs%2^1HiTO6SFg%KQk|Q?vIvESdRgXFjn=zYRiOp5d(o%Gjx60taZLFJlJx^OR=va>Bx'
    '=6>PIkBdq-RV*au)3r@4>;85^>wK?Pz%lyrDc~B$XToN6<|9tvybL>5vhwCIu+mo0Qy^&@{_uhH#H&^m84<LUVR7v=vc02C`!kFq4#ca<#6<I!Dm'
    '9mwFuh;h>0jS47%uUKe*Y^v(Mwx&R|e;+wqZD!BsZn!H(#NmFDh4rjH3Vl38<1ya?)LE$P-B;s13@&)dW=`@P3q)zlL3NlxTj*n%o<MME*$>Y&5X'
    'u~Sv;sJ|q6@w+@S@X*b3u;O!ne(Oz!mVc#oZKVAFysWlw;4|m%kqL@bwJNqg572YW6A@v6z!xx8i?{0s{XK-kN#NSyXS4p7CRKkx5`16;&g!qvS('
    '|PapK{JZUI|SzztN1o&u+X9yY4%z&3&*OGiE=!$f()6V$$Vx2V#o&Q;$8pb3W!o)A2c8ujFJl_SL+60`BahGa@O&yIY7wead9j94DnhoQ~dD8WSX'
    'D1Mr6RBp77c^BrD!E>vzLk$+0Zl*QYo2^Rv@&&AMZPSf$NT+6?g^=yTmXCalOqYy-WOS}o2XmEkaY?1k3g~gXDVE`|+<Qg5^!OXiYp4NiPWJ$Xa#'
    '6339vv76Ne&9CG2j4OsbOtf<vs{KPI^4f{zcR=L0UV@md7&RV;=Wv*u=65sQ7z*F<K;b?I%8S1aiBT`l=?g)Z(MB5S-Ch0+n0a$T5#en5+ak0zI~'
    '}>aqISur^?`>~iH94m>kUidgXVFr~2Tm-P{042ftt^Gm1ZS+2$Kv0BiMwIytY5bNAJ>T+wW8n>6@pLHO9d%^P<^s7Pt)mG<~nt7ptUCRJ7WoSbO;'
    'U=sKTBrlX4QpVRP0;_V_uwG1^04P4!9_|iIHT3JI^LEkc|1JgI4{>n`+<fDa#SUfAotS(M&Sn9FU>hyrT1l$aSbMaX9=OO*{GvSACouhi-q=LQpF'
    '+MGX%WpbD7I~6fBy4@ia!c$#b!<+sw(P-78dr_+PBD)|vlK)2V=%86{V_cN0E|5M@W0SsH&f|21tB8d1tYBNDGU+a3(2^Kd4!s1Hif*TaLq$3KHw'
    'PPMqh?241)G46rMBbI-lt@_Vb0qd%I&jo{U8x#pNXK#Jw1}k1~<&vokIYYVQs`Dkzp5cx(=jb~b31inM5u=vQ1YX2;Pm4K5t(=wM$Umnrb58dF(#'
    '+?Yuy^9rPMC)FQmr{CAt{!Lh1Z6IQX)4aMhUG=9BJknCM{myO?ABkK1#O4QCf`%k`oz#*s(`Nzhh+`^!@q12j^)u^T+`Al{!ZNG9Su1&Ju_9{AZ0'
    '#6HhuUhx#kOJ5|Qe#^CIgxC({=HL0A;8iA;XlWJ5YoT)7|2Z~$6%Oz!<jpV>DF*16r@cpsIYp~mdkpL_tnW>$>5-!^aA}!R9#m4EPzx!FldQO?Os'
    '+dVvTjeOF6yEnGk3R0UU?J*Rivh`3t9k0$cPE+P11@e8L{Y~HA}5&ndYYnpP~5J{%j!~S)q1N!41;GN+i^8yD+TxNI<=SoK(y%<^Noj<S)~>mbII'
    '?i03BXHu23-oBVs$n-Sp{%b#VwruqFwm+j_Rvr1F3M=A`fMOhSnAa&L5{&~YU&3Npt2t!#h_`8}L5wTG>Bd-r(iB*~l+9iHCyy_6RKDA(kZM5Y-+'
    'GuwL_0h?3(D!b<Dgrpx|3~n+5U}e9LHTK%*Eqq~jYc-tPyR>qvOUX=~QMSIzW_2_iWUHIJg_K<zp1F-8Hqf+!U*|DQ`)DpV-UE?tQ)y`q<O=hzdh'
    'E<67=M&hOy7d8lk<2ib-ZyQiqW`Xk>n+|e<^9<F;w+Yd@(py*Ftj?x_ocJQN`9%ug1u_YAu@xYJk-`(5jfkC`RzmNY_`63^Fb4_n`m!<5wf&IN+C'
    'v<M$dzyH>7MB3Xpw>S`9Y)lsNoghsT}%3I}PL!(Ez=|8d+V$1i7+DrQG4WG}#WovsTX^JLkg)hTc&$&Xr%u@-OiF~*B-^?rAhm@9z?D|;gU@N10$'
    '=eLvy4pt<U)u@VtlHY5yrt6a*6(lrtm~Q(1mNGA?NSZnELCc>Qwx~xw}QZ?g=1DfHHc$Wrc7C&WFniG2d)wK_7d`6or$9r<FVAXZ6*8H1!}IVD+O'
    '(9(G-bUf1wFWtCtx!r9lT;%OZn64xKw}`Q6#q$i+ECfeti~4VmYyO$o~E2Di`E<~ODJMz#@KwwFQb=ltv3UiRm!Z{JqDgT(K`>f(&*%x}3nC0z1^'
    'Ke{S9e`2LTZdlZ4Nxj=PO18C~JbDY-r_p|otBibP0JJDLjsgLkROK3Do0RO*#y~?BPjgh`Bkso%k2l%>28YN)nAX;Ds*(p|fcw*{i*@w(m9BERF9'
    '+`$Q>#s>*=6f(ltI8@v$S3rz@8XOAD0ONw(1l;?UF#*SVAYXinWIr(ay0rP09ETs_*Z<Eo@)GlW~Cf8sn-T4p7Ou^i=Tkq><y`o1<sQ1$R2NgnI#'
    '4+v0)wx)AXJgXHTF1A$a4JQx?yt^`uS#HBBZSzKj~V`~kQs%oWf9?KkE38x7t2rvDU?TSz+qi!M$#453$x*~Adw@yBiOw{J$d8&*}YT#~#`mdxYk'
    'G6^PXydK^Yxyh_%UoVnGMOt}(i;gJ)7*enue$Jip`$xWB&B~NyO(>tRL4SYoxP1)gqEoC;S*G^K_g}Wmg*I(T4O{VP^~OeN<uG0R%l#nM-a*0rxE'
    'L|MG6uIa>s|XQ1;&fmTZNX->p~qe4NiA2a)Y}J@gsj@y8o6WE1m4S^H2eWZ>5#Jh>f2PXi!I(-AuGcJ(o5m8;2YqT#c(47$}c<zq~<7F_A}|LdA$'
    '#um*^In_#Rlte++t>{12*aoS^8Kd%$Q~=!|(%c5mQ3;W&tsv{>#ejCBDRS$!3Q@Cf6{?`&)V|@nQj4_p@OH#2T9Wl1pL%Iprve?7Pua0QLGXrlmh'
    '3(lPo92UpV1(tk}H=Ba<@rw3*G3vg=ZZd(8RQ}_vRp6#<U${zEm&<vGF>!YGEhI0wB3-<6bKuG&+Vz_na<Kua8#p3-f?ZdID4yRA(DiO$u=j4>bC'
    't5>dIWm2|>>pwf_igP_Z^AR`u;B>UDce*z@$Hku;aQ=7VJ(TraEWNrwHD~r;3%9~N!kkz^1dfiW1lN&_}0ZGgSy8Wj0bN45v<6+iEgVfw?gdPoWf'
    '02zS69N_F<w(9YDaIGasRFjg@lw{#>eVE)d{mE#Ejn1>)As3OD&cGK!Ztw5J!o)~G+Q|dfq84`$k;TB6$!CHZ;zD@WeT)NdTDViQzbWlIxX2mE}Z'
    'LwpuP>=3jUcWiE0NPkhxwR0E*sp3^fQ&K-iLSaufh`PY_NJ``_CT6y;BLg;$~Q)<2ktnD%+?)AD%j?>4E}H!Q!k97(UL4S$=sCf8#x)6t|A?T&b('
    'N7b#WaJ$M|uh#b)Z&IW5=LG`FIcue}fyi>rTAj@~c}X2X3Hr^(a91_usnXZKbs&yWN=Y_57k{|{!kzQ7nZRP8A%UN@6~49ND`YQET2rouSqU++Sj'
    'Gayy7LtpBAbZ6ssISt5r)Y$gLwe3ps}^wat&^0#qaQ@N_dUS$KHIbvVoEIFp_yUeT;1RPi2wNa1_LLz2mDw4x;VR@G>V=w~B8K>6|LdLZ2RinMfl'
    '+r9>Q1<D74|?|9cBbuYc_g_aLVk(7T~+PP=0(r-S*@s|C71vE=u%7$%+NiefojY+Y3uHPZ0rUa-97jz=Iq8nzxF@#0Cjkh**+z$-nxI5j_0t6fS+'
    'S<+D)>PqF&hFj<Z~ltmGeSVabI$d#%wdU=(>TY9;Ya6z2vpGr;x1~g%MG6^3Tyh!)!r0M9jbK4urQTfhSm~jK%)>F<tH*l(E8mm(d#uvVi821tM7'
    'hQS85+BVX^ig>v?&s@*NZzd7;H$XUW=3YI`*jS6Yd8FI)6rQ7Xm~1Adiuq~LcJN&ybqO#Sz!0MERN#^Zl_)eoNIBCRps4c=I2br~Ca3@xrj%}rfN'
    'Ecm<8=NPq=gBmM27G2m&@2M_ORdBs1S~a|LWuZ#WP{T+WXf#77Vn5X`)l7wM`5RYNX;D&uc47M88ey^YyqvScYO!c<Mcf%1)7#?>CR%62Vn0siJw'
    'gpLj$!+xAE>c_?pN3#pe-M)^SOAnwS&(!2mTwR+WY80kV_YiRTA!<%Rd!KrC%L6Dker;J*I?|oyt~Gwh>PKK$DensLxN^^gFyf-pM4IPhgM(?A6Y'
    '1MQ+0?MO;+m=vSj|8QU^rh|ERHSa_;i#3Js}nQmx124`*11z)GI3wY8r;XYTOW)e1NTm#YQyu8iB-0|hA#qoIE@3e>OMf=m_9Dns=>`(Qy&r1C{l'
    'X(0ugahMdN~CND7yq2CQ_dLu;@cp!YT(xM@^hIZK4HYS8++~r*OGxOa8-(V{lPZhxaI?xF+J_%P^(D(Rcvrm^16jJx|oEND7AT8iy0Az9+f!7tF#'
    '~lP%;b9C(QG?-1_#9Lt{6o*3t~>Mig5#Mp5u?TRFPxE)x-;b&9}J!H!)Hcj1pdd`_z=M|+O7@XvPK*k#-Row88XqqAV&?D}2%i@C;^Hl5~5Ip~*C'
    '{Z=dp5USHcn{7ZE{Z?um+TE3?IidzcTc;0Ir#P>xlAki>4bqQ${8;7aYDXh7XZj>2d=I!m8AxU(I5|sU7Kf-;9?s!T4MebaXsp*leC>@b_3hVID+'
    '&lHkYQiQ5>iHvwZUbKE(su2hNaMMOD~w<m`T`Ek97`fd9-u#DkLenZ8!XEMTQxP3%MQ&f1)7Vca)W@Xq=fH>UQeYBJZ8!n-Gez*w>D%MO8>17UR~'
    'MfJOt-7W(~kOwvy+YGK0(;2)10KJqfj*Q>_cc&c!Za`Z5}lBk0pD(J@XyyQRJe2}9bilQE;23}dwx}D>yl8(*=+?a~iv>*4z3G>3n{6mTi1^^h`='
    'RD0w&V{Ty^l5soavnkkQ~zQjw=(_SwZWxB?snDAv%p*4WjW$xhvEybMy&J}@_Of~X<4CUs24h#xGp>RYAYLpB)yN3<DNB@kd3N|=5&Hmz+*QO$is'
    '&ZS;JC^a~@5IT1)m1m1zU@Y--H9;oGPReV|03RmvN09~vH!=EALq5D$Q$6ES9uH7(-Ob0YU~x7Ibc8%CSbE$y^L)y_XO7u_fu`U&>O{TVr~nL+w~'
    'V-T>rw)O#Ul^OQS+hZC;h`xr3y@m(99EP&&m{u^D;sr@&Yc2U=1^*vR$k<i15~H}AJ<ha^H}3Q(>FEbPX|xGVifaBmO!`{`gmuimb^J)BWQd><gQ'
    'i@ukqTRzNJwP}`5>ULKb=HGt(aeDpu3Y=bxhd-T~%@#`a_VAV3jf5&2hc(t5PgOh1wOvS}0|drF&TQ|43J$mfRg-vsSS9Y|oToq3KXq@19DHN7_a'
    'R4xWQaMFL2<(nV{t@ZBM`1{s$Gp8IUhvKs#3Jeh>j9CtSSOaKFqM-|sMXTn64B<|<c8NpB)Q3S)vf-G92nB--ONvDS7Y8KiSz_FSQmHXQja!KRNL'
    '!G+HmYB?Ls2bl<NnNHv4}uZr6-rtR*C_+rfVS$R%?3C#40Fexe!TOcu?WOD**D6?eFf6Ool^~!3kUe8B8GlbIrfw5dk`2Qpw5VGMJB01konzV`!o'
    'P}d+)ps0x_^Uf^<>ZQ?eAZI^KUV9__*kuG9Lx%>Sq_=9c5*&?#&(P9&TJir&wP<O+#cQB0Jrx`JgcPjhQilW@5R963O#)<r$PbIc4xD)dMcVErH2'
    '7rin!hI?C848A5qVH+5gUyD57$>XZ(rp=x_Lw4YY=I&K>0OZrX<`OGlYi;ss>$e1dR}~DZQP#bUoJX{I4kbszrrPXGaBlIc?^cs%@`??42cIn8On'
    '*8SI*P;nSl{-BEh*i#YtTgV{N@V*!VICfi|plb6dt-e83jh$lCGO0Seu6c_tZM1F?DLXGb3=Cnpd>-U@kf&p>ir)bqzN(xSM5IT?=)hj+NSf_fMQ'
    '}p~DrAhelPK&Bw&M!CXY9vYiFg)$X>A2dSmfLeG%YF6#{_&URE=8I7xxVTgilU^ZFtIiwhKeOj+ikgDeB)M|QCC5Nn<(6~MpIS}=v9SPC(T<q!}K'
    'PFdnEDvGlxl?>srkxHFn2n@z%f~&%6E5epHOhz`2Wz6q`P8BL$G{Oh#Cc$WI$I2ip<%E6aVQ@gos-B4{CAf2SmHSpoy=z6DE-UG>folzxhyiWqq?'
    '3X_=LX*uDI+P^K?S%TJ<iE>FpT>NNqPrp1O`QRp;lFEiqqZ0s~ZUnJP$4vrEjGlS~z7YM<NoeBakwgcmV%1S72<<0--IC!)$~td`lC05=liPSPxn'
    '3XE}7KF7F0i@Y&)i0dOXR(wUb%)tp@j%%h>u2<)jdr)XAJ$G#cv647d6?|Onw>Y2XHF*@u3pI+ZgJ%o>S-M05o_00Tmz#-J_j-73PE;bNdF*5^;o'
    'OsTYYzUYdXdp=T7PsT*8XgG)@|}J`0;qho-4xP-mc&0B8UGzI<iZyRdUECVi%eg&eEeM>{HMFwUPADQ^ISy`ktIsG$RO;!6Vp7QproB!P3+Xia*3'
    '_^-C+V@q%U%oA=PTpvtk=@?7I^j8_NmEbU32-xW|wC`40K3@WuD!Z9mvm#4qqW6}wo<DB5!)OKnWSkQ;bWtN{noP7iEtApzw66fNXm;hTCdtKUUE'
    '4i3S?rg_nmBTA8rI=N*KIU};dU@aGsU$!`j>|H25wGgmo9QAt2u{Y1hC0R-gr?c;;TtM0=V(f{+U&6jKdB5}ElIR8JjjgD-k-g4WVu?K+0t3Z??t'
    '|o2n1K0_OqQ}+tB?Y#zHdpCd=|uLdr|koU~D1dc9(qxwhBr*{KmaviHl?i4ChH3jvT-Tn(H><{xOkIN)#*&>yNNahRyPmp1aWl<y=$Gn(%D<c$I*'
    '+<;5QfH9*5P%I1zRD-&x0<mjG_4Z!vZM`Zn5l_8?PN`IE9U~xn%ujTgGy|>XHQ7+W%{97c`v+;KNZzlq?H}jzokpDPf2gQIzRTVA6H#_!HdCYA!3'
    '1PRfV%VNU78r=*B2KnxoH(DZX0j>%w6zuso1qPnZ=bWY~7<NTDS#Z_`r%q?2rIjdgxs|mbni*gxI5-hv-rZ>829W%u*1=@3Nbca2qGJA~;c<LTJp'
    'X=?Gp{_jmzA>Q<K$e&(a<F_h|vPhYi`<e9EcRT_G8{FU0tL3cMc_4&>RR?vFR7>Ku2Hm?o#qzCkB&F#;v?tT?uT{0!xRd<ush}D$uEK*p(&e!<ms'
    '3w$XM~n!5o!UzQAuwTc>WIy|4S@5zHYek~r|0b%?$Jy|OceDKQ42Qet5(@;u6-G%s9nB#ak-if4vT3sq*;L{rQ0gT{A>F8D(^|(@hKkY+>p@%b&7'
    'QQ%8}HO9gen-^T@Ayfwl?*fgcNfE3z+&Emy@ESnW|D#?IEQxK^_;?niY&36AonTO2zxq$JRuv<ozOj7g&gb+!qAE?-Y~*o<p}b~HEq9TK8>Gc)Ac'
    'MZ1a3l!J7Oo44oLyx(v^LWk9u(M0mtAlE2UP)ItJuPk#aPVc~1R$6qom$Q;Zwn|!@dT+zC<8>+Nv|i@e>6BOgJ{zVd`<%2|d3|_wKle!2{Wf}izw'
    'OcW4$_z6l&1a#@yzA<Q4L+pqS&djw#6OxCc0$mn*>5u$G{0kZXSBY)o>;0twwqy^+9Pt$jX1mF)%F901mWZ13b_+CMAcZ4J*s<+T-=E?;r=f2p=U'
    '>*@|q(Uz&+`=*%Hqc^42I(WQWH(_$gr0aO{>20>UVy^4IMWA}#llWS~cRbPTOE_(F;!O}4*lm_pHZYu;q1G8X;lI~-jqi$a#scn3#MssD-_IXrA6'
    'b<(V!B@K?^Y=IXT@o%UvQOqhHhiaXZF+-ui9K;u&WJYeQm=oH&0I=}UC1cM49)eDDRJoWND%zr;Ur^?Bk7THbWj4wYBab@3-Gi-P0LlurwclO23W'
    'U!)4Ek-(fGY~sO2_vY^5E0Wi<|!<VtxfFG_nmc)5`OO~_i?=RK8SrP2i_CiEZB=vbczbHKm|m=ZqJWpc<n6nHZZ{uuq=-ud7p3k(kxACRcz89{HU'
    '09UWMZXRfh?>p1HLuFMmqw_jcLnrNqbF2B(o6o+xwe7PMJ<j#HmxK3WBnT->s0JGCKa}%L-m=ck3nY+~NnQrx4HfjWGh+AFq=>0CU8<9;oS|-sYw'
    'D?J&n<EL*4p~QC;7vSU&woIBRNyWQZCycvUu0K!ZVX>K1cfy-d#=-4Sdvsp%EqPK^i7gbGC4w<Qb4%kxZcET>*Mcr1DG-)EJM-*4z~u%)2b@UsS!'
    'DXr^O<LCr5Zi=Om6lBxW?z&BTL8SRl52A-L8WTiTIjDiztB`AZM5XFE=`tk$*0AVPNi-3>DZ0+Y+5eRTx!g3XmZsm(#Ekw3_yU{h4spKTZyyuU+j'
    '`5PQQIE$0N2R{~23i>D0;=u&t4fBw!)?JcTbhGoQQwuotU@17X(ybw$M!B#WIl`P_@#aD>ik9tCi%ftbz^HYK~naGh6&Xkl^2wCksZwQZcpCh#>{'
    'WUP@s%{XkyQa{xGUYgLkr!x<50?N5tD5)%?f2VCh!rF##p?DZEhD?yN$v1TTjM&)Gx}(8W3O9rT!2C1h4~)zMmN<oM2Y=rcQhHw|C!ZkY?phbd2K'
    'UQgZJrsc|tt*C}~?~Gt*!(-gXaQR#ZL>{%gj%p|!SQDkreA@2Gvwk$hl`~j*^!qIPn>G?k^oUTdeq|D$SBYuU`n)wm58~27#1@+=jO~x0LIXiL=;'
    'rHM&qIdLPfuRt5-W&JOelbA3>6zn<`+{UfukvRwb=mHn>*Xiv!hcj4JJ?6vtf}cXI<1RQCEqQQ^k0!@_esSHx&H>TD)<lu!{{!W~Npfa&>l4zBz$'
    'hiq)EG2rv^vgETL-KSY*XZ)zjz?jgEpAjBWOt4rA1qy7?ajqsuiHQXB6JNR605f1pneUI*=lj1af75bj>L_ar}`7RX)dvTa@x+}10_3Gd;KVxvhE'
    'x<Y}Cf@!iRRfWBNBE?;(#Q;l=B&JZiS!o&rbsEwRzj4f(dq6%$5w}Xu5pm6MHG|9GVZ>e^!iG%p-NUG15n=T&2|d4mv%Z42-+*E>|QtJx*ci-Oqn'
    '2zvKbd70Dg^PJeV$+*0kLM8+6XE5wf=-nPd7QV!`iqj#e44iEP>6-ec|XztCSvO%q@NnF=b&Osbm*e9tNqpaS?3GZzATYD1jXm@3+65W0qkMTJm2'
    'LAdEMA<e+Sj#|MjiU*=lf0Ux}Tj%F`YeDFaP+Kr5reW0x(dWI?{@vsmO}UH4*^T5oBAfZb=uY%4FNkIJw#FqFGGgYhQ~BSx%9EA@6}IzkH`HB?Yc'
    '#d3!Z~BI&P3h`o86W^F3=Ez;B6uKY970mW4fzgz8I3dGs7)2Y4#>3<zUIj6Z99gT%ygT1)iz%NZOAEgiPfG_hPBW>Zw}QE;Zg7@N_Z5=tP%1LgY='
    '<%BYRU5{J3Ojx5uGd_rM7Gy|hRP|&tfF;<R7Uu*9?UT`22;S(E2i*f7tV3SPHyjL8#Z0Pkqn!AZ9Bmvc!T1y~QLWZpT*0!9YXx|8uh8tziA1fU_6'
    'jON<h&jfB45<f0nDj;F{^>QepIV~yMiTE)dNmCZLvd<hZ<HmBD~D~|XtSCQ{*@G}^#~*Xs82w=gYaxkO{!;-L!h~thNHI6XT0(5m>KO7YF++Zjy*'
    'BsF=a%SU+nUtK%G4?w7AahstZwN*AZ8l_oSyK_v=+xx72#H%lP&&=6tHGq<&`Fk7I6xIF8tZD(9kpL2csq8i&?G1+H9|)>fn>sYVE-G)ky^wq$*G'
    '22N~h#Wa?YqpaG?wPKZF&^}0r>Hs>{&bY!x6Glre)L%;q)u*RMT)h+I0!n^L-bO%*)qi=(`#&n`Yg?jbs0PfZ-JsxGQyP>jfhRw%<f?h%M6%k8`w'
    'GYtDw(#Z=3O5cD<G^A2I(Au*2M+QZRq6QYsb{k(Zd1D+vr@vgwbpRPt#C#Jr+95CisrKuc%$#IaMt3x+`L|D7;WGS(HPU*kVA)k2Vus%er!_`?`)'
    'cV!Kxk><K~TP1lOf9HVMc(xOs_6lf9p#t#>gG08+@+x(lHe!ttKt_nb5%jp}B9k2Q*02j$wN?Q&+TvQvTGQ%~JTzv-=mP%`?F~ZCx14Zye54Xk<W'
    'hkl$x;`3k+;??Kk9$;si;qLv&9a&_4V{lg{^yR90F-h8+~%Qks}f^EBUwC9*2%Dw3^jD4d0_`aGk}-A{vs_R5E2NI1yo&Gm`R^_3b(9cyW7`8KQ;'
    'j@b}6=K{vs@xvhN_f{T79|-*O8Yw#9*<!C5Fg8f?gcVeuWE{x{|X2-lH`Ob6?Td9IYgq2WHR4^ba3D&>{AWn4NK-c!7+iGACfQ#~fX;z*TdT`eHv'
    '7ioGT(LC=@Bct17gwieB7v^3})dJm0uN4bAnDQ9Is<8pvOP{Y`5)%d8x&K;6CnQS8+~&--T*r*P#COJc=CxLK&}6C$Xbpncn}Dco$PX|J@!fvY{q'
    'O%YozXavP45+gHMONtWcft|{IFP>QxVhOs8x#}6h&no6|hxj8#gGw>!SDvO^AUY41kEO*YF%#Ab86iXKvEZMjvvi@Iyi#BVyLy(eVAwf3V3lZkU='
    'p&*L5Bm#CG)UCpfO6*4p7s%olg>6n3jP$5|N_VVr>bwp&iNHQ1Lstfh59UN=n6@97;GF+qr`_3?QW39OFSvG<pSI2X$BSf+^zgk#IBe?rJwGc_(0'
    '<&apJa%lAO&hatVO6O1@}Rv<bYf!g3MLWGSzbfkf<|9$*T*|N%C9zvLE#W7qO%F$HaPTpF&}q*h%YF3zs&x~mQ;1Nlbu<yO;_JlJ^jkAQ{27{94Q'
    '7;oTg-Cy?wp@%*NgslLj>`*FAPq&z_R8LQ3Uh?>6x?u3V2X?^SE`a*l4|9Gb^6_hFI(DPm{?eF!7{!xtpNH+96HZnD*w0u6R_B)4HE4}EHPC0aLI'
    'P{jK@%Ye?!uq;|#7taN&qs6{$b?FGA^z}Ngu+6(`+bpz6@x>4N*!@l^?=d-DI=gp7EAFp?M+pN&7nG;)QKdyl&}T_87%+_&O(^J$vq?e4Ok4)OKs'
    'I%Twt(r&M&>AwwZkT7;IrF*4*ppD$Z-^TirIgx@*Zdm&%9TBWUFeN15VUDH5Ma&)B%>d0pQ)#UX8UX&?ru*;xdQ1B7?UwxfE~V(DkFlIBKe`Vn8;'
    'l)!kZH>C#z9qR;}?C?#%*UA*(}bq)!JM3j;|q<|8Be|UwGo0Pz@MKm5BOq~&G3QGH1j5wh@WFL-Rd7}7dvzMr#is-|)MZ;2Gr8T@%;8}=LUV?4dK'
    ')co~7+Da#A5x<CyVPe2M5+Xf7q2z9ZH9(O8lga$l|rOd=9}mqB<NR-&`a~iR?bBQf|~hqFz7gE5y`pIlhSxxF#?i>x5)=rdSod?U0qU?`h#ZWu{I'
    'HTy!27+o4I}o55Rw~4E|=;Ay}7O4??AdHu#`QtVdl<Fy;v()bweclMOy7AyQ5^8}Ay;fErE6IZWkKmv%r|4*6m(GS?Hdb}XU#?u0V^xu@&9MPbN`'
    'EhfP&RiWqzK!rt=Vi_ADA~1|D106g+a|K$|sfp9x0~9W)0A)%y>Iq)pDqbV7xI|%vG))wEQJpZ~aP+Ncl{LptUKOAGSmmI@EVP!S$z(d)*)8>3+b'
    '5+Yj-PEbBt}A8H|CDwvjn4}=9VhAyAUt?Hj`a#@mcAY$gC<p9FRCyhhWR$gt(RYEP1dEO;}E9AjX6pkCmQdSQFa(SyVDFCud00Y}}W61YsRrdvr~'
    'wS2Zpxw6G2V`PW$yRCC-q)$kwcl!++1sigz-S+AusNG|g{pssQ&WEPObS*&E7l4=Xps#>bYI<I$|wtjYHK@d?{So#z?xTwsKyK{5zF*8hkaPLelp'
    'a7Got})S}uP86!V+)``wv9oHzUhR~bF|z;2$+yKL{<SD59c*i=CZhNuHkb#2iT6cjqi|GvmNdBocuA<M<E?tI&$??UJ!kyBs9UGr)Cg?Mtzr-K~8'
    '7y1i09@yW%Pch%e)+Hdp!R+znperQv&^LTwQ``9nUd$>B2}8vH?9Roz^}C@L3vlPnO+W%oKk#`K(>99b#?*HlLEC{Y`g!G3R}|9Krks|F>>RH{C0'
    'Dxk5!_-b2GDhf_&hNKeiqXIT_d;U=Rr;d>NW##ATKNU5ERA14=Celt7E~&5})Yt?{jk)rs(*Q8d7YTsOp}&v!(Ax@ED5(=T7N}o+n@c?yA?QPW|7'
    'dp#3Z`$!i=0MUDX1dfs?K43KbH8Pf?Cxrc63;8(dfLyEWixI2XOmDooGdy<l2OSRXOXFwN9O{cMLL=cEA@IBi%r4{lOu{>Aq!$dxxl?@plx~DQ;-'
    'j)Y75z*uV5ylolWB9Q8Ce9aKtZU;7onSD$i{sF{$P*jQR@_SCAEVGKx+N~*J?T_5vklv5~v;X#`&xNQ|!rV6d)k7n0h%_#)TnRYYLtDmiM8VNRc4'
    'HMh?+{$4#XQQ_-^<1b)MUHl8quW1q(2X^cJ3oLFdj;KphLru;0Tn}rf!~*gseQG^E1>9nHnKo#=Zf#`{hGA(t<sEhG=m!xFt_>qm*ln;jxN}(3a>'
    'g>6vBQ?62|T;*<@0ISk1b5sozv;t3qRQRTMB_63Wa&$Lnr4!P((u@%CnC3Io~-CZsa#?jISQ(vapd<KsBVWLM1lYHR8AxHIp1S^@%cmn2e8jS`T-'
    'AeX0LoC=C*uf%oKHfaI3=b2|Fl47v$bdUL(ld9Vy6(?h9Y_0O<T1kX$Z-Xq}ma!0CMw7nw&S(t1JJkLDH95`-M_^>sUjhNUzBZvnkXb36Cf8XyI8'
    'I9p8Ymg}4uIL^D{d6VKVnHequS<54gplRm5EwatgR%AYF3D44PBokZu2}eIjZq}x9s*C=di><IZzZIbhN{$;eUj;g$xdd5oq1SnM9EG^9Dc_zs%v'
    '=nhu_CHusGtjEJWMd=L7by4gh=ks`p<+fu$+-Vo7O=jfkl!0fkPP4luIYaGT=Fs%;Ini1ya+C^4rQ>Acg(1s}D9GSpjoRJvgW$YoGQcZSrhi>vQg'
    'O0ftH+Uw#skBK8uU#Nj_NEh`!<GNd3#xz^0|MK-kIE-MY|noh&k%c%b;iWwLQU{>qr(guS>I$b1z4TL;w=-TunQF3&H{41Hk6<-4kS!x#V6IFjc>'
    '2KQ%TIiYmmlOTFYU;C<55hz^dhz#~S~mwQ(BC++jTs{PL_fHy#Epvoe&`fQGY0=IQ<)3bXE5Y|Rk{i7!A0)r;QoRg|lV7T|TUPF>@<6n5roj%LIa'
    '91^xyXNDU1W0iw)a8;|}Y76z;`5WX%?hcf>Y7@sSzEZ?1+)T{+ot2XUHBuLbGS`k9+SYdCB2em=$W`k5sRJ6bG)AEtabbjPU2hq=sz5yi?Kgd1^8'
    '2l|(Wy&9NG!5~TP@MRW6lcaj2XJK1^}(u`xyLst0ExPqJedgU){I9T*|~knqzQCM9zs!xM$*9WL5`^GMAgbj4^$)p$J#!g#o)CxBaaPLQ+Zb)#M|'
    'zl#aMyH0i^D3stvyyDH8h))%ryHmMDREzKbQc9Bo*ej++%{&`KZK{;0e;=5VdZniP;Q?RUft_iz-5j`ar^6@W*k%f-o$=g-^e}jw9iU}EZA{vuKl'
    'hE_>=Z+GdU42a!n?@_>6a}!knR!d-IGN_*UX6h-W0_?l)MCd7;GO&#HB7k_A}&#Gqz@om1C-_Q;z!#QR#PGy?W^lq&0K7`Y)++CRe9hs$q-)E0o@'
    'F|F=A0Er;T6Y)_EO+vI|z?X%!f6PrUJ%CX-$*dzbL2Rg1`9%hg+9Bkfg)IkOOKea4n=?kGiCyl4slcV%S^p;@j@pEYN#;ib0CS4(yWvTAd*?NtEb'
    'thhoO0@O$KBxC*{jSrH~-r-s%NVaF!Snl}7k|hRsjxvp5&=43=<a(_0cvHkXmWv+037Apkqycdw;Zpp?JsC51i=rLoWKms;#jWZ(g~6j^1BN;wK*'
    'SGF?NehTYquANZJ>lA`qKT|p1H+1)OpANQd(ju9xEK&QBm_;K{(^AlPHKAswXIH#<%pwl~(1Zu*-SWG28Xo<`4I-l~nNmtKxa2y@)V$<UAZ$HlgL'
    'U%QOb9BtmFj_WOEO2YuGmxfxCRSm*Wbno>!=1e5@iF}+!r;UctYkCNO+gSzr6Nnw~qRfFVbqIE>^Qpya!JIlH3*k5BCNp}cWn(<c<5gikc1)^7I*'
    'sQ&+&6uM7r!gABAZrivIi^(;`RJ5uU=GUk*4ZQ!Uo>pei(7i5_x2uN6`fP3Y?wZ&Hxqj+4;VdWFRgrU)3!*ZG3MROR)<1cebvDl@Bxn{&;MQH{GZ'
    'k?<H^@@2hcdQNvVFQg%)X_K>rtUhZSE8sC{m&uYi}#HlB;dS1WHb@3!rB%GUpXvhFTPava&V^jz+<r;G8Qod3ic--2b9^R!4m^sQ4BfiRMQLUr%8'
    '|8br23~Ml#>ZgRG44^O(+b|B)eL5oS|5Ahhq_kP%l(3Wia7d5=x<J!vC>lt~NkM={pOTrm#rN2lLr_g>y78akd*oGAr}4y<k_0gqi2cYnwF&}BR!'
    '>fO=+k^?CShRKnQKZqMgn~O@#|XU^uaEa5fA>B2fQ@Gmhj+OerX$ArYPski@v!qGmDO!W;S1tS%id{3sOkvM^|CVf4<6?I_&VqHlOc0?^eI3o~|#'
    'Pyw(NJWB4o5v&{X=U~pMCOS{9P|3J#ucbR3KW{V7!-ZvH`!?t!I+&NyZ@t&52^<?hw34%@O7`Ta+u2KoJgsyOz1YP(frovj3C98n>oLLE;_amP*9'
    'LhE}+NCe@0ait0rQlQ2T%T`pExjuS0tdJ#FcO3dH>B$QnfJ5We0vwxEjaWdOHWhQ>*&MV{^j@^ph`ZZA~mG>eNs0q>qPp#7COtgCF1HwdBo2pRw5'
    'SvAx>?2>jgAYj4)DfiE?XwY|&d7+Ow&AQW$S#V$_BQYvgG5N@)GJMuCF+8ZB}>h5aX8paF}4(WqDU*Aizdi<p2!h&9tUhjVv@lyU4l&C-}Im+ba>'
    '2&;?RjbBY$VDMSqN2IUcP&c0-bw$nd7_z|_qbl8GaEyy2+G~j)KSz8#q8L}w<?~wOyz0gpB%UPPI3q++X?w%K)e!2FTyp~JE8qYx;Xs#6pZw6*Ie'
    '*ASoUx60D+;JtG;%xdA*buw8t-yYwVtlj6}x=6yS!$7(EYtxE?+SRnKnBei`P+>iwA<Ava#&C{cH%`Za!-P+R?S@tI-weHOI^QxH+3rtwL<QC*Js'
    '{S+vivyJSYJ(*UzjVSRe{ZPP2Bk?W5g-x}wvZ(*sF|G_mb#m-cXLcM^FJz3ceF(M1XfBR#aQ%tmXpDuk0?7dCMd|VyWNnau``l`{UaYy))z7h!D0'
    'y2h<5E(9mF*wEfTH?gW5+IAmFy`D<88^thw!g;MwXFY&aNT?faP}?=oO+{aBpL;E=R4dz183;#_1Bw5tf*EiEyWXBKqKp^wMRS_@nO+$n7n|oSV+'
    '>>Iwyg7(R7BNa`91HqxXE!Ux*M3Cg<z;rH`q*;U<BuX0BUP`by?j{N-=&as8lCuw9d2F4Tds>#KGoiQGelj~Z=g+r7AMGUwJD<+a4A8|`*;LksSc'
    'qnRfRY~$_#Akw7wQ~G@<x-wnpSTf%qaQ)_Bsf)(UgvWkCQs|HJ;sO$1Jp3p|&GW}&aLU>;Xqao@qXW8Cp7ly!8~Rr|%CCq`vy_)QHX)-B101<Lio'
    'l<EvQE&kF+G&4-nffiyXZRIlYNR}zy&7QOJL9sm1s^#M*Tf{xEy9ya~c%3*73ykZ8y`arCh<!>&5(1*4@FHEZ;bBV}3~MBg%*>RKtNX!cDs#8F}D'
    'U=ga`b9pQGjI*Qo}UP`*jVf9G#<0{wsbDP;Gkk`C&>~MbXk97MK^B<ACbA<A|w#q3S)w3Ae_dbzT;cAv5_k6qy{BPPkDhsYT<9-#JM>&p(&?(<IM'
    'zHBoeQhOsE&KOTekhth-pmQ;@%7x88>h_4b>4cHrIa5!q{G+tymq1~9(AKKsXwv-25=W9aoC8;gZGs$6(1S1o={@>J7D+ui`UPb>&o<7<7&xCX<L'
    'LahS4u9qTdoUYm06s@0!m0wx_vmGTA?`FZ>%J)-9ONV<li{v%ES|cJVBMu_-NhL3ZR^ACR=DQn=K1+z!iKaNL4JSfzjY=MCYgXe4B#Mo{ZS!ONUE'
    'fH1TQDEpTjQJ0~t9?7f5_(q6bjkTXV!%VS1en*kQdz}d#Q$>Z+oq<}`)Wog;>R!<(Yfv;IAFnihXj8gRC=`=E-`n`XG}`x4n8|6cq~7QOXVvV1w+'
    'm_|$nyPa&UA|OKRE0(k!E}A8Yin14i_(9`GJUJKDSGxEm6W#D6DLU-KwpwRJqEH{FH93xTSB^DH``MQ=K4&q(W{y3nGiorr6-j>|8yKKVA!+NvjV'
    '%LB^O<K+{9eQGUP@>##kOZAjofna%hFev}a^QUFp^G(V@-uxi79n_4NO-d^ZwXGTFEq(;mXA`N71uyepV&OTBwtiMZ~Nn%Igvpkz9^b`7F*tvnx+'
    '}vDuT6?FMrp2fLyR7benfE&=n(*RM|1Q@FiOk|~cC~wXg{Tv2P{ON@jCM%@cZYFo2xP21=!YV)30?oU()Hhzt~@mCG3vds(ek0F8?;9_;h;B`07$'
    '3SDvu2i$kUzE``kx<i8pwRHyBNk5X85R?QXe~^PYj=%>QjqcaqKV<XGM`4nG+E=if2P`jw0-@I_lOlL5+wn!oxjmY|9N8Z$9n(K_a)<+F=IIn*^8'
    'Rv9D5C;u?rlXzpn1oM69Nlp^0Px0Ow?{rSQRVBSM&>Hg)!|oo*YlSm*CJ10D%lJHHA)7H8#!xkgL1CxnoKjOJJpBoNO{Fs)y>NvY-(!e;pGk4ap8'
    'gS^idQXhUDtrx+J|R4xElK#iW3y+7K<dgn!dk($I2m9F<vEOF^lK)0?4>0D|k4K$08Rh1eZOuq1ji5;|@uyOq%nk(PT%D0}Ae!90#yg<4us|ACV%'
    '@o?aXft@c^NPB;7!Oyms^?)h3~B@v&D>&-Fsr(++6YT<oRAsl7+5H>K*oAHrf{OrXu=o10@@C1$Da{PXx#c((-APz;FFJlb@v#&D~64LN$h_`?Vo'
    'S~*%BqTTNe<ToAI73wpn)yyLgE#4?>{cA$l04cE7Rjz~<#=t7G1MXoc{ZR@91A6p1OjDdtT9n$K+#VH@Zkb{^e&Hwq$e8+8h?rcfNE!(`ZNl~*E*'
    'l?FUizk8Y^|3bQ6Ex9`=z~a0H|}$s!0~bnfp|1LmedV}-VD?dE4*0frRtMb}HHXy&oY18+~-0|p8O)zfq1I8sU0!Rdzeca7h8F4jCcrqgFTRTH#v'
    '<BcDmDc=qcxzC}ukSan<Nhtt)D8%SG6^Zf4Eqw$MbnfK!Ms9<O_Mv}2>JzkIYj#Ta{mv@&bwxS)7Dz+Z*E(lD7pcB~7<21YXImHRvp4_s%A5PyVQ'
    '*OYvFD7B%Ynk|&hTnRw^9?AG^BC8LS6D0mCbWf=bX`AIqk}qf(|me-sHVn=;{Fi35BqeS^Zk+OzJYPG(_Gr)%0-+X6rZ`R}8wVQCnQ0dXA&JcLoX'
    'Js81CpQ_&Ugw16Cmb0*=}WJtp$5?Ht~=a{3{`OQ;6sFL@<sE0s6PBdHGYne0E5SbpbpW~m}#%U-&;K?-Mes0q|N`oFRi7biG8U}y3I;^CIP(67N9'
    '<De2iLpUEg|F(G-|mQ+4{4Zx<4NBMeN!MS*4<UkIir3pb8ecN<<Nv1>GF8QlCfMucamr=n4gq@6Lq*t7dJV(>)jhuW41Y14Cls&rf)!aahE!$`?u'
    ';`ub-LyI^HmVS7Wn9m-R<^#E79eBsJ#!YWA5^8~;-BgT{|jmbO&WYC&Ewlm(!110eUk{r~UDa4{{`NZgHFbcgYWgw*X#f>nQ*&ra~${*?0&Wb#)R'
    'Z!2%MGTGUDy97bv|6Spfh>GeM1-~27&h5;(byRVfso+iF0GcovddwOTGzO0d=U3NjUFVHQpm#(1(`0M<=?t`#(agSQODwqSlXTqQ{v*5&+)KlM+@'
    'SW?Zu!liev<s?Kx@%XbG+W53Wi%9BrLV<5yI28qf|!0B|=*$$opnMz>C4Wi-0y6-@{W%W1>CiDq(*58*zMX$eTM6@^|@!X@U!TFLPEyhEAW#yZg!'
    'L1KaBsbs|duzGEs%OZAZBmWE?_s{bz;tM|nHgMYEsE-E-i&%or|-mna7YLpB`ze}>Ja+-9?f6G~?xM!MwUW?r3$>TD&Xd;f^Z@fwhB;+=PMHU%pX'
    'M!VKg1y&EO!-oM$L99w$VypqgSF_R%Gbtji!^}BQ?T;g{_Xm^{7#sM1LF)U^ZE31hz~hmOPo3Lw2#+Tis!iaFX~=<uuKM>6%krw3{Ff)Ab20?fgH'
    '?@E}n@J@iY2H?^rx{iDB|L1d$r*9tOnD@DJVF4sT?~1L5O*qCyyt{ShzlTI8&S3gg8w)+akc;V`Qf1UCL}r&oy+!&r)n5Rh$@tP2UDrWlv>Gsiol'
    '8vuuCoy#8XXi{=-%G52~tY4BOW;K!`>eH_=<EEOT4-k&HzIMrZaoiELfva3^lPwc4|7J~{tzBm}%-U~pxd(=EP$V*VV_MVqJiGV=%D<bYs0_(7s-'
    '|~MhjTJ3`J+=X@P1}Nm%&M`FkkRxd+(IrYvYMb>{4?mN`rifl{G0s2Rs6n1Fj`x-U}{O>wvjnwi?NA2{yRpemM*C=Uz<|y%`|aE}&Qbq&5GQTj<z'
    '99^Dws5n-&5QFn#H-(`M7sBa-`#&?OtMz8IYjBzm}vsIJ@n!yq}Dr0Svb_gB4=g$64esVYi`9{Cq&Y!4Xr#mfs*{*{?j<6QK@_`B%ulYr7@VI3QE'
    'W7@@f^Xx>`0uTZKDt4-5T)_=3rqARM_8sVaPCmeLC`1)F}Cu{N{=yO|Ad=$@X%W*%2_H$TZs~7ZN6K!=~+k@M^vfT213p`hyyn{SPTZN^R>uXwLM'
    'OUgg5n$UBYQ19JyB<rpF>dnGlkR*#X0ATC(0{f?D(I!WZY<N>CPLT5QCFdczw!`S%5m0k)3gyc`awPeWYV@r2IH`?suQJB#nWi-Qrq;yyvO0BVh3'
    'M`x@E9y^w1+}5tO2bn>(y?nqk2wWKO{+sfnyLspf_i*=O-OLHv+0C>dyQ~WQ{U7sUEoNOhzx->F$84^;;%VL#e#=gP69%ILB(b)st2bn8<}Wzu-X'
    'MH_B?&uq0BCgm8(_%n6U;RJy-Tg414I6!+L+0uqq9JAN#<;)kC(8<FCZGcO{TBcI?r#X+Dyy1ZVok>iGJ0Y&V4Oxb>*HnqF6!`v!6Elg}@qn_xdJ'
    'EeeepxgYTchMFr&A=*E5t*1LBd=krkod^7aiJ<Ws(;zietpYy%U_9AZxZ;O{6qeXj)>XL`~+Ou@+YbM0;uT74AAoN}QyP~bxP}1RPfgxs(<JKO(H'
    'Rt-oxiM8#^0mfKL5Dtn|IHsl<&(tCY;L(;54@S4uGfR1<6mKKoVl$L`-pW>P%ROdSXRn0ds9CQuHy=YO!OQR`1Ox>Oc<r;1EYtd6lP;b8U@px*0>'
    '#!`?;%el1H7`Z$z{xP4<7hh`Gw;>)#X3`4PssUf(22_cA!2Q?pXE0Xa7ewm{!?G9@stQIy8Vyw4LPKl^VX>!Nr+_q>;NP23RTBfl{aja40##X>^+'
    '8x7aH4IdegU(1|bf0Ms!LNZritRdBowL+H2`(~9FE8;qt){oTjk_;$6;L%u?TIJ^(4=LJ;zQl1)^WYf-I_P;HC`K7&?f}xG0@LpIg%w}>TH|c^+h'
    'GYa4J-}1$I95Im>$&;DnvpEH5$al_y?y%MIs^2)U|>Ricv)ML<Q&^8fPKpnmvA=DW8ZI+^D2)drS+BJuGmw^b@!4d9Sgr+Xt*geW&y_JPHnCNXle'
    '33W*;=Mg4t}G=n$da41_L9=lfXGN1KfVhyAFJke|k(tqg(LlB))p!~@<Yq-K#iM3?xg{&j?`g|WcbEan~noJB8?rJ!<$}&djvk{KNS`-pnk^4^Q`'
    '#XSN6~u0WCRTUJ!zls|0nHc&0l~FQ_|ka6H`yt-S!I)nafWbA*A#C8yf)eeb0a{%kM~-?;i~mbZM`l&Gl}IFWWdYy@N!tG-iNt}O;F;ObI1jO`2>'
    '6;_~5B%SoJrLweX=FH-=8k9Z+G@vVG71ptmBrWSLwD4$ZObwflcTY;&F8s7?LckGLgCv=^laruc(NV~G3XIs!8i{k$N^9Qg^U-8pXoDlKzd4M5eM'
    'q_ZhWl?g{X-cdl$UQIQirkP&I)yQV-(Z30$jZObVkF?5p&&9DcG28J0>rIR^Shx{7dB}rtMBcP-3>)@BqN0DuO(?^!uxJqkv6hMk1ESlB?;zWC1~'
    '^T%j#9>w)E>mubve<V5pPVgpdxl1^4BV-<M{;M(*4`{Gq=jbr?)_P1$_CV*NR|w5PmI?0Jj2x=E*>P{}g)h0ap@+E%<q2KWOqxi#3k9I(ke(e;aY'
    'k_%45413B-p4AuQr63xk;d>|KnpL$7y+Eocx&@17A$Y~X~;5F<6pF1=vEZ|)<MiOvqq!otn8aKG8ON@47%aF^=d@ktiAB&p-wt*6_$%fJ`QmZ1X|'
    '2Gw%tDK|=+kph!6S1WL^o37JTVo|wd*t|d{Zy`NGX>@m#&@YI{C3CrU8no73S3LQOqd;$yoAR39-pUmo}sJ}(6(iw!ev1idoU~@N!_o7el5>u(FD'
    '8(#C!TtkCw>E!cX#c<3DaCQ-!T{*ob#3&_n%ke}%cH@r%=a{2P_^U4}ez&=Aq8{10&X<InOJj4ik51zt4c4+Cw5?Q1uh*WIS>&fgUJ_k^8Y=?d*N'
    'Uc>#9VrPembWzujaI5p!P~1Mr4g_B8xmIulK9rj*{Nqhd!y$CBkKBdnHs2ReHaY+&_#R1>eAxe`dbGrOH=E2FUE8z7vf|59qh2L5j9Y*hHB}lH7~'
    '9CDB`3+20zW<L-1Sad?Qu`bTkLvD!QX<%natvwvbkLKgvhuEl%jNAQNs`}p4Z3oFSG{z3>y6Lj(g{A&YayzNca$_0<ON@i_Uv|=+MTR?puisYbzT'
    '2$=E(E{b)Sr+?H#*C+DCx2*gh)Zo2}h@gSzRMleC81OUPE=Xw7-c{JB7ZQR@rZ8<d>a~Kn2+F4%B^S9C_)4;~b>Ygc)2sb3V&rGFL-vVs(JDDqFv'
    'D<WR@deGzI^j8+4b<s>ycIfm1_p;e@3qQFvp{$D4}RjXj|q`!flA^GDJgqfTPV#l-$SN|1CC7mJYdK9e7~Ku5uCmhy5o_Q?-q)*>gb()IxtK2XDu'
    '#5>z~|3<Up6-b3fi|+~#cp17tU|y8OA6USy@Fz7kZ&4>?{<1M%xh7E*Ls6s+-+(S#t`9EHO*3<(@wK#GPOWuqg}5z#dlcb0=ir+RDzrH_m7H8LBU'
    '_IkaRIPYb`2^H2#$7PF@^*afDqO`C@f6N_LHq)R<<G%xOZd2ro+^+_^c68E8)&#q=7J`Heu}Mcx@Vl?uU`RE)y9jTl?Q-|7VvL0NobMO%nv$32iV'
    'OjT%rCT|bd^c>Xo-4BCBbq)8^+!>t(KB`;hxyCwP;%9r<A5k^Jn}ke`<(*x?HdfU2{l%AsZb{=pYt-16zIx^_7a?c&+mO3Uk!Afm_B0eRC~qofvK'
    'mh0o=2km~$h<3DR(^Zumptwy|p8^Scc{<r@WV_RtO8GYj_LqbNW>%`at*5$RPd7VY;7feADHY3zQ--Da+?T~f;)D-{is~zuP=ytNE!!Qv$L47%YJ'
    '7BrhN#T7ep2bzcSXz@eY0ADO-CgwjR_>*vJF9&!O4BYM<?ll0t(J-M7x3%OBC-bo!_m>t<+T`>T9L%Mi)`G^Lia$1k(G^WboY*ZraD0H8(vVaARu'
    'zn>*AaMOtVcX#}TBC<i*n!8A;p<;5c@x=e5B3DW8TFIQ^n%LliRBjVm^48`8koUx<2%bU#KeZaz}(m|LIrqKUF~)X{D~31V&hYE1;Hbm?a>06(O5'
    'A5Q(9V17?#^s5r3<;^GZTIK}yxp&((K8crYFsYSjX^(P)tdj2mutZtoY)pl;>k0@=1Uhb+xvi&4(G2mY%13AiBo(Jrd(I{_XMFtz2Bp@oQC@51`j'
    'FmFdp50(@Md^lGza4z>e6+Fy4uZF%Z7M(sPE#0o2ba7+T-E-2Nq2yh1zg?JcL~pp=kv=EJ$7I9i3j&WuvEi(#?cYkFA(eC$h{V*5|eB%sVZvD@kU'
    'aWUU|y2tz0FZf;o{B$z1bGnIf4u#5-4QEzz<x{vnAY1BdWG^N12z!=hW(F!yJ-{)-kv0+2Ci@b@QCY+6-TJ>|hf6Tc%o<;}=Jd_;)-^NC)&m_!u4'
    'eIlwrSj>SLlrHx19alN8_<5Z@H64c&O=3^N?U)VZH2!Tci2;aiI6%bJ9qCL|A+2>;`6;><Eg)X&3Oq!hQQ<!r(2AQcJr_JkU*8@I;__BZzOga;nD'
    'm#%-Y?8k21a{$p<I2_M1&RDiXPtbSNx31J<MF`6?^R)uAi^f8P})1jC=w__fAme&gbVpSJ0Hwn+`=8Hpa%i5x*US`iA-VSTVgJ09hz0E=$N6ux^6'
    'tyAH_>~5cTpL<N+RO5RjrYu*B&^80zZEALeFKm<%u2kpwDlEQgRgX#RDxkp_rj;%}CJ>xfo16AyqCh`9dL&PF_6RbBxMwW=vQm39FTl=m%LXRGql'
    'r#og1E~LDx(_g{w)0246_Wky`-ci`PcG34%hF7S-?|bxN-c)2$IRVl4qMm{3s?3<d*E{-e0fM{#z9D?IZu*ZI)WaWWD-Xd9Ux;_AlbpA#>IhI$U6'
    'r2N4p@fM7GErNGVg^V%@KTGcaFR3+0Df=LkXcxx#1{cnur=>=zEvz<3ySR{GrWqWM(ETgyk#hRpaJZ}VjTMStWx<Hes&lQ|OW1w6`cUn=JTd;q!h'
    '}RP52Y&Pwl*C=|w;tuki=?d*{(AhB<OLao#Bx0L>a|#`QC{EMY_5KISb@&Lg|_UB6cN|aNBHS}ZLhR_Kn{(1Qsl1Lxu4N1Q;hQa)0xGOq4oIN$6v'
    '8yPwJa5sR00T8}|Fd#K75)XyKO{*EAUhRcu<bITe47?2<~P-BT8{i4p*vdVB8Zx}ZGkj}6*YvgD{w&B4$AsE1>b+xKEKwjVNV`n)5KDHyw%#8^RN'
    '@k3W_b6_jH&uT_=0`^WQo^dDr%tgkr5t^%XAC0fzEz3l#-Le6bwg~xJSk7^^b!p**&1Q~H<JSuNuGx~PI97w_{w|5Q8}0EWT*3HZFWJ=&?HXkouf'
    '_Oe0d;uKKI+4kx~s(L-OFJa|8Kgwm53Nvr9v0d;|M<W?If7%tFTrO+`p~;?R%-way9p4c6|~a!YQjk6$xC|l=9FNPD|z@GL;x3?(zOFDy?E1XdZv'
    '3VpO!IT2v(*bm4Nb>Z8dofF=nSa0d)oGj$u|u)FNXYnk)-UTFeoWu6Qfqg-}}0hhY6<+(H{>oo0`1PUy_D)i+}OEA_j?HWVm-2ec1JX#Lz04_0A7'
    'MJrx52i;ppudj#7R%Z`o)%Jn^m+f7ecPygzm$LFK#S=u_;Ja@w_lM!8GSpb;iHyGzz_OqbEaOqx7q6jc>SH3mSF{cmPWRlX>(GcP@E<?8bc*PQFz'
    'eMVSHh&U)$?$vwiP2SA}-Jbl8qklFAV^$?8-6pjLeH8C)`E$oC`O$qDzlPr#dyyxZ^>xb&{$gXG!1a1{PIJ5|m<b>->blMH42NeGRjF%cT&^IqmN'
    'ZxO^RPqi-n1`m{NK|>X!X?zdGoDu6|DlzDf#*~{PdnX)>M&3EsDYkX@0)1=-KXGKWO+ba#`OLR1{*rbYwC<+6{h2BwPHsWwuVsEOJDdm5^@nY)L2'
    'F1cw_wFdFa<X7@z&FbJ!@u+BL&1Fxr2Zp`l>iQAOATW6F<?|ZjK>ds{EvYG+hEYp#xz5P)RsVF+&)#|Mpk5f5%S7UPMt`J9mC7)|*fNJ9PP@aHYA'
    '$(cq6y5v0diL7#A`+Kc4~f?=@TXSbG}WbRJ|OissBP~V(s8q0^iah>{I!g;N+oqS<?eL(ZVbLM9_vU9q0E1r-&#*dW@TDTKDhlzS=QsZCZJ$h0Gu'
    'G_z!tnOrXRFW;FH`x3>^RzwowlMo9g3QN^`wZFv8`lo9CsSSDk9!*PNa&WmBKDXdIJ((~&kNjO6t+LK&N5#}BR*8^<)gz$?9-AGCjS!^kVcQmFeI'
    '>PGP)_iir)G8)hoKt6C&%i&HMtW)Lq)o^Byd3|K4}bB<*tUg?bJN;Uap}KbG-C^T2S73jB#vzn74Hfq5Km|M3_rH1_HL9wPxs%$ynW*_I3uqx>jq'
    'epmJhUio~Zen!o3C7x!rW>Gwz*CMa4K9%RrSVcw>=?c%oCtarFW_;g(833=dpVu7h5OF|bsK;GyVu5BKCDOCyk>X3^1#mKsq67z6#jNKfl~H!USk'
    '@?7#{a#zNyShc_rI(BUUpO#l=9W}=e)MCn2v2%HXe<f+;4`b%-Dc@b$vE!omE2L@@WF%+JZ%woe{S<m&v_N33<Gpa|#Jd<{clnp2Gf}{TW=E2wz-'
    'Pudip`c-Y>-xDq~*{TQ!|AGt!+$G^EL8$eSxh@E`0Y<M%qPr`@i1?wh$;<vJVnE28JK5lai!yJZZMmP`M@wS!#9k)sjL7Wk4ua5-B*D_~W1aEK%?'
    'ARrQl=S_StmlS=P!b^6q3m4KC>*;Z0M5?W&gppGsY0Yyw6FFbuIW%_l#8m_(B+tQI=q%L)ux;R&JZ4UMKI`gy|?wRGf(K}aZW6Pi4s!jN2XBeNg9'
    '6CK^SsR_7b$X9y@lukXPf2+JZXvq#(>>Y<+u62U16CBxn4r(}I8}Pku*~wx4retxcF*ax2F4RcFpykLl@e<dp{$hmrA0u=<&?G~_b7bMtqN>nqZ}'
    'SL)6@3(ndoq_k-rkn)m^7DwL|pLt9*G~*pd>(u^+j?vE>McXZ*NYh{crAXTA|2+0t99>O*9$=BDT%ia2BB9ZN<mbQxKK^a9H!;{*6e*|xbvSmm8F'
    'yVqd*^A!jJk&|d`FNSUDk>{Q~vP`wOtqbj+xP5?_6`4jvs!1ot0mQUPzxyWs7P(KANDDPf%jHv?PY5w2fxXHmU15jegZWYGk#0=MXK3OnG<6BMh`'
    'w_qdFYzn=_y0#=bUxUhX}(}=pDI8hZ*BtEYm>-N=Ij=wwOk2JK;Hc4D$6Y|gyWj9|DX{v$iOgbppVsWeGRr?s~&MFRBfl15mbv0UnI$%LQ5Qo!%$'
    'Lce!Rtk(gpvRC7&Kk!enN|NTbNdRM#L^lAimg~8Q7iYIkVdn7gc4`;LifT4bfeMG#<LXZzWYDM^r8vqGVcRfmq}MQy|qKCWpi|l>Ola+o>G<^q0X'
    'cCi=amI-fNMwRb(ieTC#J`5fh4Wlp%(4f^KvjfNHSHZuZJHZkREn&EyAuUqdtR!40RBB@wunb3>`raHFXx9(a9kn#l&NPa&3hPGxQX`GEE>JcUXx'
    '+dfI*raS36#9X!%d|5jE8o(F#U7@Brq&(NfhCSA6?#MAF_s;P!e6}_ul{Vn&mRNpTC1n3;C$YlpLCe)QP+`72R<!4}&abTCZ1({@LG>5zG=rkV7q'
    'Y7VP_}UmY3p)(kH-F8I!-FfCi|`Oqb<PDOlLh-$=02|KV9X==BiRtGfGp`Bm+l+oxU_inLzfx5~uwuw0vxR>HppDKHoUm?KdEV?fw9WGF8P?F*`k'
    'C3T5*N^7QcBenL45d%-W1MYE8JO!3<X!5@LlZGV|w_Om!P5Q)z=C82Q9@asD>`EjhTwVsn&Ja5crzgG8p_N{Hr=ji9^7v;*v0e13E<m@DVt#k=%7'
    '^ap=<Km{henUoNt&rs=4PDDf1WRy}ccB&*@4dcCXyGm=%YH9&rcjJ4_Af!T<VrDv{LRGoV^^eXHy@4Ml^S0xQ~Qy8``F7$+x{vs%iy`E=vBE2O99'
    ';HR4i@{LjJ+LIA-x$G>jYiT{Ox#qn5ga``<OryBZhMZ?fSktH{SZ94D(+#$P>|Ph-S-8wPRD48%!xSar3eRwi-(<x7(Gz#?&!BKdte?tM11sb{u*'
    'V!jh>QCn)1sEsPOUaOoDd<vv%Dmk}r-gS=>ojkk84`t7ty4OV1Y3j(2{Cs*Xul?KF;*b24i!g(Y*xflH+z5|~4u(K-J9wJysIz?WAv&eb01oTx1D'
    'eQCN@GqkBmsGC{#X90e6#++jDTc^ok_XJ#t49RNELCz)TH&(5H|Elkszti-|xhaU|PcWIU{r!LQgH4@`&@z{g(H=GJl<aN6q{9)(gd(lvnSGMVX{'
    '+&m?nmmQslQ(5@J!@T@J#(2jWKdpWI$cK6gW@+gx^@S3|Eo8bpUgW{YWgz-~`-n^dL1>-NIjQhQkT5geT-f!hTr6h{haS`+zY%*vW92J^;6L?1_O'
    '-_80zVIy2+D4Bt0pfsJXBuGfCI<k+f<Cx^2PMimBfU>oI{s^uUR}9`T?oyJ#tH*(d`b8S?_YD$)5mep<+yO^R8emG$Y@%OVo`ITFo~oBC9|HdA-A'
    'MGo2qV{W1gPb`0rWX_iBXbG&45ewh$@DHi<(xVhVa`^!bu)nWsaqJ!8D7udQfuLl@!&vYUwCMxscWH;ckSiQHrpOY;|4S>^#8{c1s+GQx?@rDzuA'
    'qPJm`x)+|v^W6&rur8eTQ!yr%+z@)Q1+%UyNA111TaUoz*E%OPbT21~U|`RV_YX&_KIe1i^V@6axfPso1j)VEW`g4bDaeG8bKmELYV1E?mhxFY9R'
    'SLzGohe2=97k%#72{cDkK?;+?cr2caadE&+F0un>H_?hKBMp1*PJ$9zV^=*(Ukcm~;C-FX&RqxRHZqRQ=3u_9yMlrOTa_GeQFbcT=~paQhWTfM{4'
    '1FlxP<s$sl)TIqrrUxmkHae_D+QjMQNd(1)UdLc7yG$e%7Q^btdodO#iLetH@i8mwZ-m1TIXp?`z^2aCdFwAh&`cW^L+Z2!fsRSp05+V2L!Qd!RU'
    '9fJ_n!JC^P@JdQBjXRCrZUzm=Uc$vH+n?kyr5FVB)J%9osUm;wQYmuO3B=2FU}Yup6s*C6TTz%a^R0U%w69D1{Xo)^i9kZpat#-1aD0~xBu1C?_U'
    ';YtuEOqPPua;x(~1Z>1Pq5mK&Q^%#c#ArAE1K5uJf+vGlBY+tn8K<JGyGid^x4v{v{m$IKoVyk~pX8NLGt6`x9iyu20l`<KT_4D(A;E1bKKpwgtr'
    '58%?ggqexZ&EY67f>^#mlt7WR)re<JBJi=%FLh+snaIe3Mc%s6Zz4P8%VJ;JucD^-n}G2n)5-K%-ooYm%i|pXp0xljWYta!eHWedk9;a55l|SPx|'
    'X+K)Vv5;fh7ae3>|Ys?bioEqWTsnHuiZsOK7z5K7J-omMrej+2Mk{B4*!0;S2u-1N{88ARS+jq{g#BhbMR(JpwNj&N|zJ5DFSXT^<p|pjzWH)Qdq'
    'G15R;WvpKULiEH5-#%?xm#71jqX#jc?<!RbEZoBIg^%U)%wLsX-*sS1$UaRabge~*?nW^T-M6ppAvCUk3ENk{XS5H!hE&_yD4*v`H&G&kL-%JQ5^'
    'CjluYijxtpAKq<#hQ8z_i1Z%HTujGEHpM9C)~_9&t9u+Lo#CiCCCs02H*9MkK=?s+BiyYu&h&~91Mk&bkul1l&z*vO2}vZwu*Q1Fe0c=^o5C4zX8'
    'Uz`n=K;PrsH^qF*10!6ss8<*v3AWcylW`w^}}6F;c;yX4tDZ{!sm$OQODYGA!`DjRC)j;isP9Kl-xkA6!Rws8Y7dK#acDV@Yz1WAaFGq0LBCU-qo'
    'ktveIy&#llp^FCoTBNVXU9!R%e#cod^;7f^<3C{uyzV#~WpO;sCKBOnk?u?zH2OLEEOUIiziSs_Pc&Z?eTjr?JNz=|g4~3}6S>Kwh>g4AZ`wx~NH'
    'x!Eq0V$ookxxcbEtRIGuJ=nfH`YG<G%~J82>HEnRV+P>lvsdscx;|{!gzC*7Bj%&gv*Cjy#TAgN%4dnW{EMX||b%B*NqJYJ)_NV_@C?uC&Y0<*NO'
    'AB;ZE11!E<>$(rPbEmwAHa)%wn82c$KCY7GH2V0ME;FR5JJ}<;MuEBfxBAEW5DA+qCU1^@67|Nns=4*VFI|Ki9h~;?wqFskvo;fhw#s8(Ddt*P{4'
    'B#QOhb&af>InG$nIkqDTaYJH-_JG!YILE-cE-168hG`mZIpN>!?(cXtzUv|T8?Pp^-2jkJA*vo1`sW;mD)QAy`V;~;112NIMSp$2z5jDfpep69yO'
    'olg9!Oesn{q`k_Xh(Y`~M&QVqJO8yL|m;c2T7Wa%9<4UXn!FVe#i@MFd(G|%)|)Z71g@JGv3sS!SIWtz&Kc5LmKx!ofaKI(cJC!!23<s;Uts)=;|'
    'TXV{^{w+1^_-@0~_zcH`_En@4F1d=`ZPkf6+Qn-66)gn`j-&6j$g@Stt6Y6)P%Is1EO1vGsq`%+R4*mhjy>AwrwfBwEM)V+%`q!ZUHsp2$n>!HnY'
    '}m8p--)t%jey%$zI(F!bcVO!hRwL8K2_(ynjp8hdjTrF;fI+8FeR`o69FVHX-d<EV-dLY?yA69$;QYjB;F}_4^TB1p%L?-C$gO?uEKd{rn!9>Lhm'
    'k4R|Eyr=o9{WZ){mZ@#`HT*tf=NeK8b{m*qu`}tlD1d&h-{<x^@|L4_QfXhSzh^)B2l2-!)>Wk`8(l=hO-&nFIdBXH2z$OvHQ(dw84W+q5`?Ga1b'
    'QOH9vf<E0H%c>x=5$OWGhL4#l~i*r;5A$;9h@HH?s-mws)V6C1miYmSY@98Hb8lx<HblkLekwSawLy#zol-7FQ1tPFzLL2rwaCb9qbK<$S$el$^O'
    ';7bY`m~lw(<2frt+g3*(867r5S}*s7uIGH>7W-Q3aIpwLc}^TDre+OVHlcti_t-Dt;op?l1uTCx~e<<?z`*bs5+wM_NLWeE05h`)U%`S%j*N9_BU'
    '@Aavh+M_L+0l@hquyB<M!#1kE0<Y$(XOtC`E5@tFVQeNb3T|c2N8F~h#PlrR=OhVLF^RonntZLY?da%#qvyMn5A~xU<Z=jdDh2wB{HLhArJ`-~^I'
    '<nUiUwWgS1|Sl3aE?v&Ts=0#l3$?;tZ^@%vd8*VD2f1kskzlDkgLs-~Rf3;O$1cXW3ryIdczQ)tfh~6RXKu5U`5GVAb0$Xj*aU4S;5N>Wt0TQl8Y'
    'O0B9VI;)whJr`x==;f~^S=J<?%qSg?DQ|7syLC8~ios`XluHg2?@@|bTW)d-5lQF8J8p_0s0bk_<RQ{Q@x{JZQwqp<NbI9$p|E-yVL*)HggXt+d_'
    'pF^Q6iH*Xk&dqRGILc>Ufi!WpDEE^RcC{thDJWw4ZmbmmD=`x(U^Q|X{LVC{HEKyk%P|g{EXZ8=g4s?F8BMB!VfIPjyNr_0k9=T+2ob#zJ7Al|5A'
    'MGNQ?*m;|j=t3KV;l@$_0})yKi+BP53DDZK~Sg3LjlTTugrld~{00nD56k2J#3$<RSO&1AQ)e~McrCFE9Oyy!-~P$pj&gnNGKuCJN+vU|F0+Ov*)'
    '$gex_Ikk{7)>P+<yVrU&4HdcvPjjXx0t!(ERY#F)J%L<l>deK*JG%aBM#}~~z8-pEFr|iu92S}l1Vnx4!RO;J$Kr0ZK;z;%n;B^PYui5O<uXCzZ%'
    'y6tw=zC06h7lWE*_pYQN}LXtGl!q(9EQUW4gYkGsOS%ncr%5NI239XOP~$#`-uD?mKzAJFMLuwX{qAPaI$a^U+7SFa-D4D(8j-Kr7r<1C9m-7gTq'
    'KRA3DkDx)34y0j{G&-{768b%!c;)thJ!1bX!gdK$3aes<-#5x1uDZ2uc-c4BmMG3t>+Z6bupLwVrUaOo1Qaqp(B^+tvx4bgKRBkN|$I^J$A|C(Pd'
    '&+qfj7O5jOAnx-Fqy4}FoP6izVoa|RKxd$SD@Ikr|b;c=G&ZG(kN_@EY36$_xsJf<`HDHY6Dr4h}i55@tEB44n>In6g!k)ftWre0{4|YS|6rd$ob'
    '4wK3uts=uvp9tj^eg4{?T;4Wku<z0k7@(a?Ww)e%OST9Q>CuccNX*?oWgO&=2R_Uc0q&P0u*KzxD+@74reiQ14dRISVqeNPqOlo=rV{}K^<i`OQs'
    'unI*ocTcvZmo1{>bI}&KEJe>*(?U<7<Mz2<OKqd4Ij^+Xb%!N{oC)>(c${=c$w<1t##~HZpCBqQiDiHIQKt;Wk`?Dpsg?MLLJ!Oa`7>l{=Q`YjO^'
    '?Ju4XuX1{92culz@c4tDM)N3yhOKU2^LLEqt)T9YYroZdKCQh|@MB@691);q`kFV}6tKTYwZKr@1r^nL0kae}^jmcE8`+mRPR&Vv2H`emo$&Gp*v'
    '`#`t<kn-ldXin`j2??RoPGR=AAkln%cMdj?~drLlPaWcV{vym7}-dUjA<)wB$lyM(c)gKqR+yDkq(u&s>iZ(2LjyQ{Y+2O9e&D=rN{95KL=Zcd3;'
    'PM$<L$C8G3zJ!b=P8r~scy9x_1xe=4QM=VzJ&98AJW*sYY<S!mAv@;a`<gPElV+W<+$1T%%z`Ee~f<@9xDOF{_@<M-<u^j>~AO&7gX6j61;GNe67'
    'mDANV7)a@OYYZ88r5BmG)`^lr|`*--GxeL~Fg8wI~w&i*N511*^7l%J4`O^aKD#D_5+93$H<Z{+=2{(G}L=b$!B_85MViTmrQipMwod~ZUIeupX-'
    '=+6|paJk~PRqgW0ai(AXb)y7OQE&)?x5DT)uTEdS`)`pnIctZ^kvQO@%(~*MzJJNSaIS%0B2Gk;$lh9cCtD1Oc5(6B6kXcgm+<@hOdrFua8RQ6t?'
    'Pdbi9LUy2xx?~MOvov-Q>63JyofBn+`?h0Zk>pIL9%u(%)sSvxfxyz&p%go?_Zf5S0o_?mkP_b(4o%)`KlI&O;)tFQ(P^Jw>}5Ki$>!1fFllgOCZ'
    'K6cH$o%O~}*R`)7R%OS)dr{m|q96#`DnN8G@3uJ+5P?u7=>YtB}3sRC;=`y=4-A%=|KGlS)Mzo`9;luY(z6c}9Vow^~v1|Q!`+oIc*AgFR4k!1YN'
    '^h;)`@woweN6>}H~6*8dhb`n-Nn>GhwTS+*f``V<NR^Q*xbA$QtKvC>EC!dG<Amhh5e#{QspmRS0?8l*<L|RTxi!aFsV=VhP8BeG7SF;x=eG2l`U'
    'm`6&*8?oK_|q)TkA_yF5=DSdsJ}Q~=B7w1-@`kL~L5!Dv|v2fLHZE;FEQ{Pd5}IPkY7@(<60tj?`b&zfDxU4WtCfiNkkPivf?IYk~p{p)=ErpS%5'
    '16D&*-|6F!9(S4bYb{0()QK~JL9IS{9i7LEW3+!dF-n+O+JCsYjH_f&Kd`)rh9BZU;tPtdhhIb+2aF3YpH>+8)@(k*{%e)jcl_IrDQ88~pr9|-P%'
    '6U@aIrPYSN!`^{-cTVca6)JWrKXb!)}ne{%~K0$b(ZZOVTQV^2lM!N4gj(UE-51-_alceB;-5)*{E%_MA6wbMnt{0#lrYe^>m&uFr0QAyxaS{gX0'
    'J6piafi`>hU8a*4~S%hJvX~DBrm3W3V<O2N<$2Z$(k8bxi2i*_+_*&yPm)b-Q>|*))Va>X%w2m(iwndI@eiY~Ik%`8&G(lz0WCk;Gb-$YQu7>EOK'
    'O4;aL2AH|wlpjlZ&t))umrq2uGz6!YO)xyYIi^HMb1!a!3UdW0lDCoKTR^!0eyg2QS`%l&io{l&`<xAa1D+ZFp@PIs~z8jdJi)w$BtFQE2Gl!()U'
    'vW&8u72vITcUY?67io$I{bOFU<8_M<g>Zgmo8KUvxfQNQMh_{C9dt%jWi#m{#6%7nDq63zO@uT>yl=|sEEdl`t3^1n&5Hdv}-o0%kC+hm)>?tU^_'
    'f$TLE6`T5A=9HqyVdk7y_umPSfhSNR_oDsAeNdh2lR%qFwN7U+8RQ?(zZn7Rjl#O61z)x)5%$P$9Tx1JBWvZJpMqH1q(n@ggg3HIyQf2IdoA-TH#'
    '_QvJt7HBEPpZ%#K?Q{cM%AY9fg)=Ks=K?YYnfsa|94qgG0&kVx&KJmh3}3MB&f8ci#CZ1LavN0oCbzCuE+RFg(3}UhUT+Cm<$(yFdTD>W5)UFljs'
    'Pq;Zd$ZI6lAc_QcS$I(H_4K;n_5Qs;&8+JIa?nZ>vsllOjVU|F4dleG~CWpbw&BEUy`(VOvp1-!B>kIa~iw`R08*4Zasyl+B;_?5<D`4EBFi>`R%'
    'ZxFit*c}?LMOYEK>)dy_;DO9!et2TMc42d{EOQ04`jOEqkA+Gkz##;fvDon*D9w+`qsw#1Z~8k!H5@F$@j;m2|F^b>=^i)Qht=j(w1&Y7$xMDX2!'
    's7{6ZwW?wANs+Q#0pbuEo;n}n+MHy#1A{W#!t=1MnZ?%`^Gt#8sKz}dIFvG9hwnHh7e>pa25ftGG$&QX*_Hs_iMcs4)(j_6~K_i^tTZHC&lp<#Z@'
    'By$RVsl$!@e>FmH_{nUJ(H+Ly<)RO!dc4*+xqci-^d*??#VHW5bNB5^x7@i}XL5NpcO1jh3y=M++9I-^9uF*rCLExZ4tJAN09?9blA!XEWs@NsIV'
    '<<OKMOPRaMnDDDc&#SbWjR86Vym-nPWzHOX(P+t@@m$KAe-EM<(P$-ZO*pP!nS?d~NNLd3ymu?GR7-Ltqx>;QV}Yz)Ha~FGjNyF4RG3(ryG>R`s|'
    '@zLxoXsY=>yj%qi(D%!i87j?za+eJBTa0@DR(GZOvK(9Snv1#+3=Sq<G!`?p%_*6tLmM-2p;v`*L@of6-)uE!d0{>k`e`iWL2jKd5mFpMx#n>;m*'
    'p4!(d$oTIm>m`c0EnlbaX6)d>{Ie#xa<z`1XAi;9NXlh6g3rb``J=Nx&i3sUFVEd1_3LL{Z4)lDR*icc%@yPUdx>O<XQIRCue=m0*xPvp}=>Wqc8'
    '=}C61654ZkJgnwvcs?b+E^03%(j>x;(cq7o3L<Fad$xLeW~kgbiN*=?qfauYPP6)d-zi@(00FVt`9yESlp)Xi!AB<6W~`xp0qt?OUiv=z^P{sItW'
    ';#BptKUprwTU6xb)s5Tgr2j>08K(xB=u(}r=O^|r!6EGKSe~h*#J!{ay<bkS-&4|tPSnXHc$Fnm;-AfB9NfUsL{{XN{<cw|iFfR<;=?>MhR%}*n#'
    '#L6zTvEC;akrdSRf%Oh;QK#tmi(`zV!U~e4-!e_1nAp{Zjo5Knec3h>zZY?}yoCyf`Emrp~fIpWiSDe!%gaT^yV9dLUqBpQK|#C;ONyf?tVgYBwE'
    '~6m5nbky$kOx|Okc#33TA9}C!2n&jQ@RZj7JY#GBs#L2|SDbmJa;HyqpW1Yf5so0rs_Ha&_HBuGf+-9q@muur3*=o1o9!Fdp&w=Oun3daJ21A{fq'
    'fd>u-fKSYN`w>V`~AG9i)CE8#@P`7Y@2!Fa3oKmz3xh$dJ!*YLzxvF%jh&Z)9qe_6HzKPG$(w@Ud;2<1QFVs%aje(V=v5?$8;5GbAJ7s!q9o~DY#'
    'y5=M1JE?H&<1MBC>l(~P#Y0>RW*XISkf8KZ=xuTw0fI63L%0`tXti~@%scl_ff79$<C!gs7j7HT_^8?xOG%<l|`HHp)F%?hBuU(o$8+wUn;++I*J'
    '-VcTkdeA6(+@U*$2)2Sq@$(f3M;kOmDx{HigkyTgvT#e&xT1w~g&s2AG1#ekI)zXeP@2a+>lAWKnWhMQgn`*!Yy6V^{PPLWVXp>f-@Szxo?AW7D1'
    '|*v%(TH&fQeLhkjuIguv5S{s4sc%&8a%u1;i2}&ObE{r!Axenj4lQZ8mnX*%eb!-Q3p_C#>}rCSq(w@RHcXh*lYeo?lqI-*MsSL6<Nec+42DQkBL'
    'R0r0qlX2ppS=6I)y0i1MDn@mXUa?_nZWK9jjq~$ngI?}==yyum^-w%6k>`?8yp5zgkvauA{y3`tA4(Mq8K^fFfjNnmmuRw~(0C!M<wR^Y`uo9DFb'
    '?q9K^Z(A0zA9fLs{ycJN1xcGq%;aBcXH`Z%KqiG%DE9qICtmVOKr$4suLjX+{EY81K-!;p7+dNxP2~)#KL^T6XZBIA>nc>n9@9;;HJf^V4%%(P<w'
    'i@vQH}Xe+;r#!>D!7-L<@5$$MVQR4L5VH!$_0eEj@Y+dr(!JLx}Bp}P~cx!9NQ5aL(Pg8v!$*gSl0c}?myQ|KkpIifm$!NPjAZ~x0$6_F|!BWYq-'
    'OprbpywCsnz@KSTa~an11=c;QX4c=(R&}k2YBTgxzFih}u6ZS)P<Jf_zJl>-%}UcQE;&qEcf-IAV9}+w=Gxs;*G#mDk|&TF9P=;L>yY59ck*|kGn'
    '*Re?sra~KDEs@VQr@7a??ym(va#&frjhl$RGWuELr~#0`D_ut4OI>yCAsFF*W*QTL<hPn}~S%u#Qlnj!(`Ey3oQogh~jmKPZ)7pZeEY<r2W{Wg^k'
    'B>>`QCCa)r(n$w3{`!FXLqhqLGh+LlaGxPX5eU2<2#XzN%$~2(cg;9Ek;=?nF01Lce`_XZr6goso3ex`Dg}LWV?F@>Bg;hea2io}3zI)aeTHDvz-'
    'C&RpaOlEjlwpd5r`VpkOs}T|YLn`$RG<qx0qfO#zk--HQJUDA0eJnYz7e16j~cVTmO1-bvX8;V)Qy*qf#U0fKX6)sX0&DMm6@ALA2C_V0!buR1Zr'
    '_~2VwM9@W|RYOf6w(u$GQDO8GF?51f|^3GMhilf)|-ePm9*p7*agds<^ZfHKwe1PRHPqOj&oz?*F2G$w3cMxeDMSqcC>a510huA&!KaD0~R2^~tI'
    'Hdiz8@mgl^lLq}uiuM@zMQ}QdENIvY{rTImzQ5)wv9-U5+ejDj)tuVKV`eE_-J)up5@)6j(&8^urSv!I)o)kxIi|NmG^dGTxAC%Ou5xz-3tJ}fi5'
    'sqR>DiMZjGKcGfmOjA?zPPOyUgpiT&PDtfq&btG$E#v8#$y5dcMEk+)IW^tcXiJrl+tMu_-<Cao!UrF+|*5)r7>TFPCyn)=d0?Dcp@xN3<O|y;gi'
    'Y#VMQMYmc83ALcnUfP0FW&49cpLY@epZcZTE0928uUS!%oWEWrEyD*o6???vdV`&lC&P_qaqoM6GS$OjC9uV@Tyzv2ZoB^jwB#Mr4y!OmF?V)f%1'
    'K9XQoS207(uKy>z?(&&?fV&ZC_&90H64||2sfARZglzJD=54tZa1ZGJZdV&ND+lg(^BX%nBiqo3kp%;*`+v<GO)j50q*a}NB<14Zs949PO{{bBbo'
    'BdnG_xArx1rbxJvLZSxs!s&oPr{BR&vb6S}|wu-bpag2wFs7Ehu1zvo;rJSp1A)4Atgdi;h=`D6my-w%Bnz)to9?56UN`wPh?S7TR4RJ}~+8zLV5'
    'm|m5-jvy60DfG?s{oz1L&AKML>#M2Kjr}yA53wmpfjVm=^aVBZEOf`0h|uHpxL>Ex*8zr@Ivxt`I{WMtxf(7_KW2IZIoH#Zpf6oSamFsv0io^J<e'
    '5d@bfcpznR&}z>hV*;?)lM$GgjVwNSL2vb}mSBb#uoS1s3sI<UQSAg3pQ9)w_3UHMNK8OO5u>W$xlr66@)_d!yjRO|8d;5U}iXPAsK?H<TB522Gi'
    'qrL|m0%<0dxF_0A#2a{wUTVN?%2pXV;>R^Aro_Q$MJilraw7{u1m%^gpl#cT$n{f|4+66xT70{E1r2Ja7q_-IZGG}(k)z8d$u>j@dE#z23;-5yXz'
    'X3e_1bHgQCGd8{5}~+1y?)JkorGehGNpKmr?VL|>2~dH+*O=%FUO})U%}n&vOzy#T@F);39utg7#}w7d1`%1JS0zA<=vDha`2~yy#U%LnN4g5z=%'
    'Ec<o{Oq%*&)1#qD$f-V$8)Gvm!|Q!W@5XAtl-BhT$AU3ZLZw4~Uh>%%{jvDZOGS3^NDCNE|cKrQZ0=jWa;%2==?Pf_J>1@QX~_i;+&c%g2d@0?HS'
    '+OLcW<}<BP1bn*URc|ye&sP4m?&&Q@<e4INVqp=M<D0y`S|lSM7%A3xm(gNJ*T`EUb15WwfJ##%SW*t0Y1qZc7XZD!^n=fL&ZiGVCnqjx?6~Q-!`'
    'bgk2<&uZ$??zPA_R7oVx}FGiDz+joU~N!AV^=jyP5~%&lckT@!>A<Cscea$&^;yLr*LyT7M<c2dwP1$Z760*(;NnoW@B<XHU2J_~1TeK<{iycq{5'
    '|5ge`-AAdjf2zJLbtCZ7*ym``e7%Nw=YE*JPOwMEZFVu+Oa`i*&+q0W@=Dj`q&v(=p0sM4X)omv!`ou3?w0uRS%|47n(O^>ZcDzb=8Hjh9`lWfiR'
    'qmt&(!=;0T#i2l(L;79ltODbne<R@CW!}yygWVqWpgC#fY&11D+#cY3(+n&d^G%^=P$IB_TI36^9>3t8XxA<Z)D8yiIshQEnf?at}Ax#XgRLVz5A'
    '^1j%joCR<1Z^OA&updQqX!7t+Rk{U3(r^S%3=#8_}Ba$iWiS)3SKwP^UQjf6Daf3F#I_36wGXH{dU!r|+R*XEFv2CtVvP*mee^`gta1<g2E&5^$~'
    '8D{TxoV@%X6M6M{e>@W`an@2A@3(Q<;m;v_I>6%_8!rWPjKgv9N#99E6O0yF?Di>oem$nTk!kf8k6}aI8#N}f2z(k2AoIqw`ITI2WV)6HFTCg>3I'
    '~24=jdyd+w^dw_Da4=0<p{ikf>*R6?ey_nrMo#44o!8PE6t2c_zqwpD?SXWQ7iF1$tc!xzXdxl-nf_XrII<mmeoWarWO9f_~7V*Rj8H51;Q{@(fo'
    'X--cD<p7U*Z6&i~z*O`^VaXvxfEieuLuZ!O(D$ZkqI_D=eN@M~dk2-70;?h9vE%~qj&Cs2DoEIhbH6x{I$fbr+K0_V9PM~?8GzWmCkmaFI5y^jz@'
    'H(E!8MT4qnGs`roW|Mq1Mwnvy}QpRCg%K2@tZAu>?_DnNBnt)NNI@kD5td^pCAK2z3Ek2^_F~^X6N~<fO@_+Ah~<vK!DkNl24Zq>Ga${`ak`gcWi'
    'y?*pm@PJ9V95F|h9pne*4w8~x1aAr+;j)33I*8_Eaql!@|+I|3DZGO!$z+yr%WV-~%C$Z8^;d*ne?@PY%@nITnz;PUz&<7{$w|IoV{c$=vrEE(67'
    'TjL)SZ!2wg5omBOWKf6R`%1<eAMtULQFz11k5`NVp9GOq^@d~nyTnP9*o#A5bt1Rm_{W?=KDEg9M$f*YUzNUI%6POtikf7E;bbsCzLol(=64s;8X'
    'Z4A$0zjC8P2OG!jXi6D5!+VS^~x@c+EEPT4MX9?S`3=ms3Cg2g~W#y+X;rlGmfQUs6CS5;EDFoh(T?L@pQ2c&8pw43Io6^b>b^zvDm9<4BOFD=to'
    'F{Mn++M(g?%@a%d>i@n{?YmNWjAq6JDnv6eflzy(gvhzM}gu8@BYy8AB!gKl(TjP793?LY8k~ghVu&VCTnn-ZGYE~()?Q+R;+dsB4E}!D!7i=QIh'
    '$<dg2skIN&2nCMwjzjNSMGlip7a}Fu_s0e%usTjJG(xEe)sIOrMsjWrk$zAyGx<IA;FrGpux&Zqm_4m3(T>lJvj8P2-1q1K!Gbzm*rxVHLqn>5y5'
    'DT?b^_ehGn8b@a@?S(TT45>ul(h{^dtDXVaYgL{0N1KVjesl~#W#E+g5Lu@n44i=$2vxjWJT^X$>awdN~yyI_+zUj3}+D;p$zmNFg1;Qpo}AAM1P'
    'z@yQ)|J#D$?mq12&T;J?lIyX(o%ZYbYmN)g#uH>3G(PZ8_}xz(Vm687_#i4J8Wo4NwocNTrM}-`vh!NzJ*A@K+{T9xzPJPo^&Y?lp4o@|deptshA'
    'PWv{oFJ0)}2LHQNL&gCIxZ?Uo;K%-$HhhmUwE_ECDq*`uN|*m$IRmcZ19rdv3WK$uM3EZMOvL7P+cC)hzi@fpnj;FB@zB1dP%w+nmP4@bqT-unT2'
    'd>(-uC;E|<jpP#jjZRDUI<Cb}DGsW8#Q+HrBq&k!LY$$7fBO>1aR=9k=Ob(O~hC20$kKspxl3csu2U)^?=!zGrF|hZ&PCku+&lm=UYh6=Fn*z&no'
    'OlZW+>a#ivSdmgYK6tGD`n)-k5I(11%QD;Yv#0kt*>ebO8vL!MeGMRsSmNOOPQM*D)*MU`wA0929kN=0ShGsym^a?`_TuXGTAG>1!MiLNpp|^pNT'
    'aY7ZPe~%2qv&gjVY}QC9HzdxiV_W-urC8}@i(We$wx<arMk&mjkX^gFeQR6F;KF?`x=NIsT=MhTa%%%G6#{7d7VB^P6<(qtg<2d+UQ0=`eg_?=Ib'
    ';k@Z@{B@#OXIteCI4Jz;gBo9|-6R9B7k%1rU7{EHhq4x^DLVv@!lY=rNp@-j3o9{cAKTetBF!NOzCuDaPFw!WmZ=%8m%GLhgSkx>astwi-rpZ#k!'
    '7CW2kz9voZa>o#Tz51dr|X{;{O!({p+DTlX3k+$^9v)YKz3#r+v{V-H9^CODbq<9Mtr3)R1GwXd^x(W&Gwd&KjB>heOUBT3+h8zZcneO`(QOZ!%3'
    'EEKF!z26##jKt#EqIYEUOX8(oB`jJnpcSJ=mU1eI(GfAx_5s6MZp(WhfPQ<;SLu=$uk{*sc6xUC2U3<YYKyv5bHO{%&mLvOe_9+dY;CEtO;?JJt&'
    'iwF7YM+lEU_SAmC&2`q+ITnbws$RHP%ydK)rn(*F%Bl3UGB;y8@I)swKM2`;Ae)+;LauHdI~(Uz7{zPp^NGE$;p2%0k~%cOeh#^%y0syNIMbJq$>'
    'nd@%Vp=_0j9^t9!hqItowOYwAbXH@*SK;7WtJ&Ia&O9PX%@?uks5#(Qc-In|!+wN1{e;}(-Q$a63;mlG$tI`&9_S~VovIbsCKR$5ZEf}X(QZjhE$'
    'cnpimQK-f`n=>Sv^xj7`Y3Fz2Sz1m?<aFSSlqm<+!V+HZ<-9xk`1Ji%X-Dw@EdR#GPbhl7rr9wJ0*tFq<%g_cT|?d#_~^W0GJbe>4FVd!?RWl6Eg'
    '0X|GJi>Wa!q)A{MnhHZ$#DNq<if`b7DqMl6>oNdK9%s$}0y!U2t}xYMk#P8BB#r`pPcDCF{mYl>QGJoVd+1-s#M3H`c#c7X>$&6j)>ygD0{2XQIH'
    '&l@O5lgbny!%bflRDQ*kCjc%6fQcx_|vprDN=mCs>I6=Y_$cf33h{1tq3nG@wN%)xTe_~1Ccghq1Q2F4jpXWTe;Thewk!MvBN0?@snrm5RF#P-5x'
    '#vzvg2_E)*L5trV*;PJ!McoSo}gdab@A|?ncdDR&aAo^E*#yo7(N@fJqZK$$)(c!<-5P!?km@pdNUiE5tX&~LSEX~&c`FOZ7r{L&LYSiQNzRdeMv'
    '*-5-H1_qujXjfz*27^th%D1Ubf{fK!ifsjV?()X{@D6~JEv3BuF(eIJKE)iAbJqmv4LQm}5^lcE;LnVbgy^zS<7y)ltFbq%m1!=It028wHfW+vh7'
    'hNp9b!7(vxXB+k<mEG~gFFrM3tF4GBjrDUWxI^IY`N@s{nKwsYzWbe&0wZ-tL0!^hCinH))xRS%69F->#>ZP*g>ZDe=eDo0jvwuxQon458=nh9Ia'
    '2JJnqoV?jWz@?cn^1yvnlO1i7*k(*JkxP=H>S36vr@Yc*LqGDOV}|TIH;Xh@4F8l!D`DA%?ix8r`_Yv8mT6%#A>B)Uad)-8Vp-9>)&l+&wjzR;n?'
    '=*mloJ8>3P|ZYq^y4=-?VO?AA=v6&8#QJIbQ_9z3W#QtjWthB1=s4n|(_R!)jQ({9^ZC784WctZFL9`zT=^{~{0#itU38w$HhEg9%94!G#?+(IHY'
    '50lqe$9B6rUSlTH}fuM<hMG&OGowReV~6Yk&bFX)mJf5qsQ!ioV&$d$JOyu7QOmdWTPI)KGUSI9_5=vSZ$?8WHyC3JGoP6d~N_cIc@JoJ=C?Xe*('
    'Q8KaRv%=4H~VvkctVGXK3p1}rRYnAH)eZGr>EwNOdMko%2YEtrb503U4pkROL|+`>7bF!H0atR!E*WW}sdB2SjG9V_5}Khb&xfwA-AvI}S<^3vS@'
    'yUcBNi5T)YkuD<l?PrG4B9O2ZInsW(Gimka72&b+h2qedPvr#(^o_=gg}_?3$nDY*>=G(~lz8lzm_LI-aOUygt}U>S)68MC(j;H6RnGbd4bko(z*'
    'x00x-f{{riLH=VMzH<{^%nq|09aIu(=H%5_ax?+gT11e9~_|1+Fx+jS>0S<j&dOh%@#H<4XGSqbs8)_PJ)S{f}v3S-u0lP{h6$*7oz7_J?1s@_Wu'
    '`D3-=k7Z)2bpfcIxynwa=aG|NT<-gYDx=xYi=pW|IH(k7e=a*|P)3A_hFzR@edzsx-=J!i&ZH@D$`{9|`nXELzw%Oc3g2!+XWN=wx-Acp)zLZ+p_'
    '^}kyYcrT_PNm#RKq}dWNVoq1Tfp%6xHt-)XorVP^T9w&@u9X2ZcHMqo+_o+LT3@zWlqewex%*|oE1cAp<K<L_??RR2rxSKeE9_^Bxlp6Ioz;s5-p'
    'Fvi6mE($^VM_ea<vH<{c10BI%Q`JkP?RyP_M7{GaDNtGK&s!l%*URuk$-{$1Lvtk=Gtqj@F>@Y)-=6{)a-uM^{bzF#@7qPP-G?7|%xYSE4Ln>1qT'
    'z!2?dV2vW&{Q``Gu#5cnlXrc2t#j^0^jVU|$E~jhP`E#Os)j+f_A6UI*eS+P-_D+@?d;Yhy7IfV>sWH5PZPrr*A|D=Be?pRQo3=c_;8ci9kg_huk'
    'pY*4Rz0T`?bpZKN-wp@+*)$Q;w!g;G6@j_tQtdG%%HdD(gIH@24E;CwYi7=euc!v`OmM7)U;?+!j#BLsz!uKRf?Qc%XO;CLlX!K?UURxX<@WXZK&'
    '5{J|u?;Dv3zp>|in2qLgOb~0T$TqPUm*f@T6f6U4gv+m#2G(K|wl%>@3xQM1Wd*tm~W?o}HcS?iIt2-+5FQWVBOb97>=Jln|o3B!*9Is80G_=K^w'
    'qu-(D{bvhY6?N+2J|=kWLC{^G?(bn&lE%~3iBn4)Y#j{zFw4u_PsXo@+~`N>favA(_WAIN)JfaC6(*-V$O?N@r611@9f^iIJ;oE<do%XksrZ;xM='
    'RMdPaC+x?M5k?(N^yQ&DcWX2Je-RH>Yg8FWT28eMsZ#?T)rvt1c?(LnQPdM?*{m9vZkJynu?OfqCzts)23ZsB|6v9=<4InHOW+70Ah@hd1@sWeSH'
    'VkqZ}P>NP!QA_~B2|L@BKkkrT^_wc+ta<JJiad1cdzy9rA*fmA>}-9^#AIEa-VAdyFhKgfU5D`UnWGt;d3a4C_sPrMX<ii^Xg+h0&kxG^yBEMKar'
    '*ARcEhvX2}2J(dzbSAG;)!=2>WKbl~34B=(Wo4&DZZV&KK=iKPGTCz1g_(Xc?Fhas>m_WYXT%>HCtl`(#ep@hZQXhypnj?y$168zJSG`35#8nAn='
    'CjNyeGOZ<V;M<q;i9whj+%(hGeZQ2~Y7j(Vj$2jejp`{*QQ`wi1lwC|alfFhna(vn!S+1XR#!RA=n=12#Z>Hu)PHMxnql25cv0zUgb%{XF%;n}0M'
    '9PrkJKFDm7x}%ajfct{!uWvMo4aX`i2!E+B|~lP>-vq~xxji*;}fR|IvI~@N$WO+5j<VBr`Jg!zO(aR1<pN1poUpC;XmlUBva9|+nk!CnKT`*!~8'
    '22^$9sLPSGT+c1G=Iz)7%#6X;wo>X&jum=T)gZdYXmQ8Z7~>A!Nk6^R(>jU{Ko5@hG<kndBW2HbMG2(v<uN!z8rXoZI?$o2J|&r#nBOP!0umcb=u'
    'P_ifAX#AvdaHU;AZBPKqLZnIOR9j>sA5(CEjXHkH(7fi#!2=j_aGq$JTO+=hHi*>elkZkPK#tP*&x*Qvt#P6vE@nBU74_g44r)Q7W)PymAB-b%><'
    '@;>&okMl>1hRw?Zv8pzCmP$KhR<T_T!8mOH4s{PNj-=N=j>(58^XUGAT_a(JX1U0+!|15~sGl?)hB=U}0N2jcz$KBGM^*IT9{s(#v>{#-qLhH$0O'
    'lyQF6zMK5aZtdnqGImwT}EyVu@l`~ozg=;aNrAZw9C+cOc#0dvkUT5R_#T&rWy5EkceMtj=2)8GUi}N{=gS?x+>EO#o9e*&DCj)P<=!8lq(c=9y('
    '_7jDW^nMGyx^F6-klxqH`<+VFbjk`dH*9Oj=xFu7Wp;uJ#8<n%}5_VbTjH)_kf!~wCoB&@#WMI(=+lt3B$~x!>#vIYK7qJu5Q`_hfOLz0&!h?61g'
    'G!lp*VA9skCI902Wm&e}s?ycRhPG*>RZ%Mfxc>s<Xb>QPNQDBQ;{S&|FMkXWb(Tb0mNE5Ul!a(h4<)<|<`ExUA^u>~SL9CXq8h8c^K`YS{{;Gr|X'
    'A0+BJy3U`kA96;dnX~@xAxfpTi=Xzx5?igLxue!3k#JuprolG|$)?a9Un24puX*#_kDTVq?cop&`XkzykzW_ic*}-JZvH!S#ARTPXh+cjTG(!e#H'
    'RmS<N9ydHWB>iXfn=JG(>R}XeKhqyo>D!l{C2b_~M~sf6THB1B_)aO2kzEw)^6x8@T+G$4rz<b~zo}1)JjL@3U<SrwK4ZbHg<4w0bRZX1{lkK>`!'
    '>o@oCCy3m`_uGb@y>VPH;e>(R1aD+4_4XMV9aAlC>ReSX2K2^=5G?5(3fI|(27`&EnYepblK|ncA*Aih5tw-?J*u%EhGIKr4{rF|XfKMhWxtwCTv'
    'c`8NV%SDY@^+Jl^B|S#QA7R%AGV>wuNjKngOjF;fQltzPQ&Z-^ktfJlG9+3$`+&*i!x-NlH0O}Sb44ToXM;N({$ZOhTpxa8mN_@!D)K?F^m4#ugc'
    '@`J-X^)wb9ykx8{X5=3Pl~Vd#E3+u6NnTwx@Of2WTP?#n+o{%FT9H3O|U{M-3jVMasaMLj_RPkJTuwiDclmRDrq9=!r5xX=P4f<<qrBswL-QZ|oO'
    'f}okYQY?N-xYe4aBIHpwc+F_EmFYVRIvUqcPHlQ6@e<qee%e_L<!UnjUUVd9LMw_3+U4;tlkjttuAa;9qB%><DLIImm#cPabCv{$rin!Bqpx_mDj'
    '+Z^O!okjz?BegawHb>PVS(^JzR(*9naqdPWsE5JNV{iYeMvsa*M8EP4lh2Lj246YS<d#Kj+|`t^)JxkPKzscxY?#N*#rF|16>Yh}&$$YhX0O6z2p'
    '6B|uK7!LhX{f$6o#5?;&9MNC`xTiTtmiH`nLY@F0yn5f$-sV2D(O~B9G$)jG<U*C;Dugnn6Z$l&7szZ8=|E{R1<1?lY*hz&MRCr!K2G3MQKS1!D{'
    'Np`7vQg}>HD)7@tNdAWVR|o{4*~DkR982hDl{+6;wL%(^XV@HRZ{A;*-c9{+jPmhT{2owp)Z&z{xRC%Nnmbe3YW9a@#tup$9<mi7fun6@8{hvX@9'
    'Q02P?bv)p!BSoeHjlAoYFFAU^~hei`LHwr`2?vK46BGZ~V;DUvV=e+&;+%1r(n+ctRCIBRYbSxP(|8AM5pI|^hRtn0N#TcoD&IK`g$rcds%Q7%~Y'
    '=~7F|OXRm@sHGC%aZ<5fr3ydw087CG1aRQ{ua)cNP#CYI%9A)xA?%KO)nav%T8fz!nz^}PlJ*+nobO-Kr_HgSY)JT^Hr!7xRrLb8{O&5Ml0=PUMe'
    'qT~DAl4O&<U4uf0~Hiv@zI=ux{?NQYMLpmyG!ax!aW*_AY<LHv9ujp)-xW;;wPOf6C|o%^*kj_k73z%0D>)+xsYf;0QLYH%UnD5eC9PTTxR2MWZ%'
    'mb9U4JxIUvB==u)j5Fq_h!nX{Z>~r^&*%Ao50*y=3xM_?URQD}}#rFDex+oF`jv{GQp6sfHAB5l{S4N>Y-hNx6*}S3VM3Z>8APp%tI6>)|imlba9'
    'Xk~ekZ{HI$G*W&fw9D7uywuEX7~VjkG%t7<C%k_r_X5JUO!}4ME4+%MD1iGtVTpVw?8@zPkm$aPnSLKOMOd0MjsDGa-CBsqd)(GmJ2yYFNB(0S?f'
    '((vV)myG8F7CgY2gC*f{+NMW1xX+xl9hT~d<XQGAs9_6n~@;LkvZhHqX&<2YWm3*H;FB&hs?{pI^uiW&!(^r*$gU36K^X-{`{+>=SW13+?UQ0VM&'
    'nslw*f1k?h_jlbGZ~Te4@H!Tnbz?Vl?oE8PTQA>4o~_^9ZBOg7@o`YH7}-Ko5cKbqXJlB-g5@6tH?lnaK}RO%0>@TUw@0V+2Tar0UFU{D{QV}L{e'
    'aMRaG1L<I+YmH;fX}N(EWW1wJb~!T4n4@4qoXK3p->1kFl!n#=AZ_$9$@IqojEhcpFS#!v@}I_`_KynJCzEq2uHDeAd@#*!@szaQxc@dF)1n#ifs'
    'v#zdVlS37l|vDsP*wIv-rI=%{aW}?0X=l2!PX3@JFwU{~1Xdq$CCY4ULru!qY$G12BCr!5=mpaBsgQC|8+Z>O0Rlxk*VB<`=zd1&fj+=aMk2;)qV'
    '^AXUK;-}b-*E~1YuYCW_Zziwmx`cmn(}k&!OeC`RXjRX^j=e6Ce_yl-rcalA7)PWln*gOV!ORy^QUQ;iLjb>*@I&ie{wd|A--eHON!$mOBO)NWk~'
    '!X`dIG}q2o5z@R(ipo|;+p``A;JreYfMpo=&Dw+<hPy5D&i<*ohtj)h2_)**wqg7tq?E*gdEY+gd$ie4)Hz|6zE!B{wJF+G@0%!8X-&;Nlr^tzaQ'
    '6<VoN>GgBaGC_}Sq?Ssyvc}2(Z98+3TVIW6RxA&2AS8|)?`9AV8j%EB$Bf(~qL4+L{MO*Km|JK7nYp${W5{1<0J5h>4!5jQFl+SMSU_4{vA{}+Jd'
    'X+IftF(oAUpk^AbBox|9_LA7NLJic=s8sCZ2>zkpF7VX-%!olYtbtx1ewoEXdf%fAE728cP`a;l&%(gn7*g;EC<c6!HQH$Cwi7Yj1ZAn!r5x$u6<'
    '|=h>MnJ?2U`*2`N#I$559GbNVYv6c>Aj8T*AovpA9<A2gwM<dNZId&5>SU^-)E7zSI93ukeNm=D~x!j2<h^5oCNKJ;j{ox8ndsHThs<~ckoaK+sq'
    'IUiT@)gbM6TSAB4sb|w=6-b>H5~%~k4DSbrSw*mM2*Mp5)nY$c-)IZOKbfhtg|cS6Vy5#=RZ--5q$gN)gKM4<WVekuO-gnhreIjF#<kefUYj-41<'
    '~wZeMsKk0+|lr}X2cxVBYH1$6?=*jVN2>siWRa?$*u)OT$0A^;Mto!Us(+|2HTx}EOYHj{f=U&pKRcbQGX%0{3E^zHi6QV|;Yo18Omr;|~@KK0v1'
    'C4J4X(Sr}UZo@IZ>VD4A7N#Wg?!?=G;A4(X!C<a%%)(rX2?J3^C;u5Z<%f0!&f*W!f39_zPu<`_1lgruU!De!GBw&I%4q(iM=3m91ysE$flRMXc~'
    'R!=?d(b!4fQp{B8IEh%=q?=>1Z^vZDhB)V;xZJ<bY-Gy^F0gZ`@yRXZJ-tE<A(^d@XsfD5JZG=#<;z>tv`k*6=R)&$*<NqTYSQHGh*U9$B2J>eGa'
    'Y^uBsKFD%SE&%#pVjn+MJM_f@(<>!;(Oj`GQmB-vTODw;O3{_a!>&&Dv4@2{dOc$9c2l_a>L2qjcUNAFdNs?hIQ6eoaS*e=jW|z?_HP7_5#!*t`%'
    'Jpi~bbBoH?JQ{Kc)yzGygoc3LpAVBG}!pa#c4pj0!d^tCjV;O8oVrUN7h&nU*Nt^dEIWi=WpgeWAb=P5mWg!J`JGazC774h~0@^u97D0wZ5{|HD<'
    'qGn|l*6uQ1yPPKJJvP(8uE?7<PdP(r;AABIdgL*A_UE}3=)X@AanuZ6c@728R%mJ|uTG>H!S(c8DzBHMFOM7Wjf5PJo}bfnY$k3&CK*`~>GgKIH1'
    '5VU~C=NEbussk$VGM71C16>;<<%OMsjAnEw-D>XY#{7;B*w`J-rqANDv+tR%J=sXztW*l3NY&lpHi<%PcSKbC>-d?M#;1hjC3`!k!Vl57PMCWpsF'
    'tmL@0{L~k^715_Kd>p`yWWRR%uW>PVr>uXVM*~G+liFo#+Xl1Z<BWNfq4~&0pnG-m!~5|3T$iVHX`R@EHzB<R68m)J+X3TchiP`cgEX-WVtXy>9W'
    ')aB);lV?WhI<+h512`zd~%BFvbq_J@c@j#P$kP_J&>TE?}XMt+WQ>er9UgkENEjhDp?9$SVl?bd^JlmfMpkkqzkZ3++_>;+A+?QlrMa}<1;PPO{Y'
    'Brv^H0ZeDu_&X~$;HVJP93rrK$+ryyg<Lww=2%558unIC)6K)kuWc|o4iBHX<tRWX^L!LIRxzjn7YhF%+s(>kQ}{dAT{1u?WD~yzfO}fz~mmZ2s^'
    '`%p4uNXtwdB|y~7QJ0?rccC;d;2F;`jRoJP1N4O#ELoD+qB%bw?6vEBVPr|mhpLj&uf-B&K4gA>&~k|1Rx1b{=Cy#W5o@F7UoG5>UkMAy}w7$LLF'
    'F7PGFz5J0;MZPwo$qOZ?aTnvM{`jX6@Pgae(+?(N^e<@?YmNHxMg16@;APfK0o|(O8!cgnKUs9desYr=cT<U)W@sfDaX@4V_a0Yu$-Qg5kYYTRSG'
    '7JzVhhFBKAG&tRQi}?5*bl6i^CWn^P+jV9D#PymGkmMQaHLmde)S}!YGXVP?%!OiD4rOv*vUDjMdmX#yekUZJI?%4>Krud4K(kb10@_O`w#7Xtvv'
    'RcbT=)pZ2u1<|I5m$fJF|OC)-5EH`Qm!xs9;E)*AS?pLitVusi3(vbgF;2Afzfe)D8-K*oY+RH>U`F^c)nT_eE{;i~q%PoJKno75!wal;|rQND_4'
    'aEwLVBpwsHx5?6?i!xYDI?l3n^|gjqf<S?A`O~3sUkBX3Fw?oOc2>|c`_k@tNyc}|2w<`yAvn2=6V@@7hrzg6J5}Q7V?K>1TTt$M14N)eM@6;FYU'
    'R0!?ql<q;tBIX&!#g{G%BWH#kRwlC=~<4*_>--)9d_ZNi16D1R;S+uz54eww4LrS&zIr=Vw@#7g?{wwED))67MM3i^Qa@2j&f1yDcjf>J($e??c)'
    '^D^(6^<qDybgY``uJP3i<qi;!vqb+bhyI=2Y0Ei@PwK4I3~nvq@mfkvg4XcmY{csRXhU5!jr<?N^<=j-hGAhZEd#@f5A0~?&Yl1JOjS@#zPqZAXC'
    '*rhb2u==_*&O=uGD+$-}3k0@!eM54N|Eu8eh{^a%A`)hk*}s92W6J^>RC(&!5>0LcDomU`LHST$;v{C8^(=C@v`)G>f?xuFS87Bsma3naA(E=G8Y'
    'ST&Qx3=zcA8&hX*{Y1)3s>|+45JT@_YXkY%)2Oc%!t`yozGX-aN_x4lPS0rOI3Zre4sNlZju}Bne4C=@sPD?<jgA3a7&=P5okxRZ-IOlhz_VB)yM'
    'KYlNRWQNsKnOhVP2R6m2F$36k;Ap~5R`h*sJ1a)9=8#LDs-&z)-z-{e)y8^YDy9W5+?>&;;wPk2x{V4(@h`mFK2G&vb=<raKEx}b2A%wEX2~flt1'
    'Yhe&zaECE@qP#kk;OiyJ146{@>IJg9=!AS+sGbTwO7W@ov1gxHZQ1GBIQ!d%=R07&@>e^)qXck+gQi*+1d^4=}=Jm;BsOyv2Ugzl_UzZ2iLv-w?h'
    '`^QnbWJBp^M6=Ytxf^@~+Bd!$+ZbWYL|S7y{tAQ9{DK}G<wf+rJOR4wuT^gINk?UVrocF-md>MTd)GrZP<s&Zge#Q%k|$N)UFVTpiWWJ*^pmZ9!8'
    'C#o;0PeaF0V)~SvkY@AoE$$e{BZ!AsgHd)jt{<+x|cF%v@;I+EziuR}A2`O=*gg5te`tL$jc%p5YmS_vN%^dmHbYt^~9|X3Vi85TPzP$Uc*L8A5a'
    '|E?|6~mZ@GDM*~YcLvSJVWwh{ms<NN=L!Vpa>UR<|x;O@|;$8hk?u^QDPn@c$9_lzd_o?S0RiR1%|69vFIjAf~mr3r^9SwFDA-V)E*~_%@VlG+G5'
    '$|}!e%2Ir7&oI~R8Yukjpz4~V?&p`%JAMW#2{Z?(aHCt3q}cYH4;9OC2Ql;a;=Ox_fbW;qcp89I*M%!fv^~KB?N`r)#Q7_*gxVXy1f4swzHw@-3i'
    'OK*Ba;NZP>8N3VtTYQryBU9>M~HvoCNLJIKJMFKO=;H0?y%OnGL0L*<3JGgaMJhjx(0u+dM_15w%D?H*+PJd23cNFP<#_&YWY?!QZ%`3+<&N{0_?'
    '!ct(Y`#-(1@wvp2@q46mr7Sq_Ioa{FFOAFb3vw&P_}*i#kc`{F`m-7aeiXg9hkLj7uYi#^cXyF;A7cuE6i>qFdi+{xg^=6LcnLp6dM1!twj|zewy'
    's}bfo|vSBGa5&(1Sz}Y(Ojxjj>|E7WYPk4jEfDxd`B3-)Q>9j?Q98BWg_>AJ;QXFUy5u$glQ4vin?Vd!}()fx_g}@dQwyV5xTE;ZFz!I&IYlI2ml'
    'o@~vxyU4UMiIn$Xd2Gl!CaZ&wQjByeYk;L!#S-}gH_+v97!;iD-()qYXX)C|$wt2(l@BT$EEnT~eW%9aB=(rY6AHpH84IycnohS~#IRrDl?cN|1h'
    'b-czm-HqSJI{0yXti`S>S3;MqGydnqkt3e|1rSq)jMAs`@E2yVXx18jz^wOMlfH{c=l=EosjrIS1IY`O3=5dh4e|5fc_y>9ok-%!o)wFnx~lDE6p'
    'qLMsuaLL@CO(tA8FGhE~gxq~m-ak~6@8A-f3@h3hB#l(x7oR3^Excpth|DvB^wbTmHkqv+BPXUKF3A06bT8GIVCZ*Q_7sJKgGs-&pBnPm#wJ!?l8'
    'bL~(&^Y}wvi@g6!IHHFtH0El{kaBxXN1d<ubF|D&u7<Z@U8ls<rDgn*0Akzh|I()FX#fn7H|nzid(;AVbI=KFCaa+h1Ar~tkSZ`KbaW({_myY;Cf'
    '}rUSX>PVPtHp9UnelQ1VxmhW4vW&M|@S!iVHf*J7yarF#AvrlaC@p1*ccjP%6RdozzwIL_#~qp}Xf(Li)MQNrIeB20t#OzhBVyy_{X;CmK^X8J<`'
    '`T^$*5XT3XVS0tghf$n`rIh&912Wt$q<vOion@DW6pTv2;-l#%AUU|%JGP!=xa=&B0on=U-5f7H7<@5QU{$Vcj8!JPSXtMyqEM>ARD}MrZ54HCWe'
    'L-=lizj>N$6r$Tt}ocYD~F>*Ht!y2ED4;iT?EK}#||XLs%>PZ`Zx<Rdm3RnQz<T7M?UXWW->AV(@J#8Y)TWmGAIHcx-?;x2<MTbVmz<XMrWvv;80'
    '}w^WLZ5fQpxi9Rk7I0fLY-lfDJfV&*<E!;|ul(+b_~E-wAa=t*!mYT5m@&TJ-Ye9S%~4Msg~Aule?r9jvTWdhw$mz)poD#J_cHOx9OZfM*6%|T~-'
    '{uFHanGQFvsKLsia8+f(7cjyUUj*q=FOqz5{2!Ux$z1=Ad$zAN&)n=6AIWW%acs=yfEu=vZm1~7Dz`e=TgnOqbhZSbZH@Y8dc>3wFqT52b^V$LoT'
    ';AmB_NC<_ck;cbv%yVL_@Gs1NuiS8RzBuq2FI_mloeB_6v;9cuw{0V`1188X=2Lhyk#cqenx_U}^t?9`A`o{wOrnZDOtN;>yLA7mp=qZkw}5vd%7'
    'MRPD)M{#lxN9hzs!=C{|cIXNm+Kz(>)FLYuj>(w7WKr6=v$FM?~aS}g2lcqGTyhiD_cGmi}Svp+k0MMA)!*wF|Clj*pMI4t&l<VGSQfwsAU|tJD8'
    'WjhqYsT?f=QoCSA=1@k(F&;9?V|1yY>-H|`GW_=cdZ6il_)Z|+H44k3Qs1-?h3d+;~(&7Udy3uz^kdn6f_}I{PsM}LFv>p^Zv#CmidG`YFS=i()8'
    '%zF#qQ8GPbx1ZC~`Rf!GfzQJ3Fz^EWbgU~`_=e_I~3)Qs=8(v70g;Q1mGoDs-`kRL)5_n3*(ehgkQb#7;840C2a`5SJS&)*^M_I2q!xOE;uHVprx'
    '8&YV}%TN-I=!%+$QgZ2fKLxXlZ9!Z1Ab~$SsF#GwVDsfj_2yb96)3XOXX&nfB&!?mKfg62pd<J43HZOP_~)<VXxj81y-K|RbkHqVt>G<Uo|^fwDS'
    '1b^CqWfUmLV%lej%=C#Q>U5{wSW{HZE|sM9+TG0qG5zo_ADlz9mT*=k>+QK7JXVFrc7&e|_{#(v+73!O-rUejY+sZj+%cM5R}B9KFy43g0EG2@U1'
    'P`y|z3{5}46$anhRJ1kd<4}s+76BA%g;anhtp~$K5%THF|hEXu9qAt?*zr3rt(1N_jzml=F#;UeXnGLb4w4#V2i6qIX)#giy0aX*ff+*CU+d6-{g'
    '3-jrl~sB#^VY-cxlYE@em1Q<)0l%+s4ZrfgcM-xk!SBPyz2UQr8CRh*o9VC?|Yl1*ojpx-AUe)_9lsKpj%9oeMY#NbK}hJXb;zXX$V*8v`*Lp4G1'
    '6TY&O_QJDZuMM22$8qTV+z5Nks0iqAwX_gd&O1Ke>0Jm<Y5ykfoo2Vp`6GGB=9=BID`p?NTSR6VUy1!U{BYmdtT9*Ks?XU>h6ZnIi^T&K0v9E6)F'
    'T}S1qy!$aeowu5tWS`e3-yKfl^^C<y@-;U(7T9oi=LEI2+Z)@8=|SDO3D=_5LS6CX*an3o{&pmKu^l%j^wW`uR4l4KR2!!w$ds0tvS3bTehnB|#j'
    'oqpHpgl_e<$(nV`1aI)d;E31yi2d6wv!R(?>$BjkHI;AJ-&_l4RL{3sP5|O6BV^T_x}|b&(AGoxHfJ2vRe9qbD3Xgf$0@VMraj?X}3WMJ{mmdO*n'
    'lf2hlrJvND=bM4q{LTdI4{#EIBF>z|RecHYHWPM>KHV#+(TzZaP`?EZ(PpU)Xd9N7773J^v&tyi?7P{W?ZoJ>iYkoivg~bJSHPws2`M+BsH;>B4o'
    '@7_v;nvb?Zj=5+TIlAr&uis)<m1=O$yu9uZ`+U-&TjW9?(9fGXD=DQ0cYT6>(In?rwAAiP2Bry@9Y|P#pRG*&v?QWF<yL^c*o?4KR%8kq9*>!eHy'
    'ms4zI8m_3cs47jrAz3~yl%XF-L*jmabo-r#7miJ`pn4Iprq24KcQ<WV2s{qeMIW*5JjzJ}AwILIpOH9?@E&b0drj6Y>1Hf^NC)V&r+Z{+^{F*heR'
    'b@nH7In>f^@{xVqrMOQ@qO+kOk$G_}4BTww_+SQw>z@K-pZB+G#-l(zyIO}1GCN;5VAnd9W79nGOWE8tmEfliCa^tqy~>UYMxgX~bcA|bIsJBOdJ'
    'MlLRp7SI#PNx?Q7)<&W0P@*t(4D>r0sbvbDduh9ySlKyZYd7Me;su#O|?+8gdsMoZM&Z2Dk<6Y@^fhKy-vB@5e*}K`cak7*pII;6Ke5Bbr=`eLr-'
    'MlxbrR)?NK!HT)%*x(RuF`L9K89qLynBiRX^9gvUcpN@(3h)zlRdaym5@v8_Z#yx^{V#QMU>MV#?=3!=^$I|@K1sXf?xFX$iq7uvk9d;{ceYtao8'
    '8^}IK_o7JZyWoUB<iB^tILad?EG_x$6uPB5(d6HB){KqY?6dX<}`%Z164kKsU&p2{wK2NY=!>ig7HvD5arIAE@P47<-EUSY=0r6Bd2r}o#p*jPLw'
    ')p-db9lnaPi0M*_#CC1r;06L;y>fpxt~a}Y`60v#IfuH&FraIFKYpLotlTYi558@J;D2V$Zt)-(-rOHGDXyy1iHlBeX=`q=-yxao&Ol5l=CaDR@Z'
    'U(eN9W8{;H<oB+`Ssm|#bU@OCzERvk>R-jVK*h5ZK1C!Lr-2ZFf)0D%B|n7W5#`C1vB;DF6~H`E`MtbW`Am$CM#nQr_SX8t3wz3+j)sg6^wYlh`Z'
    '3Ly!MAudcBt5X$`66Wx`gB1W}JrrQ8zVCytO{Zv6U1nau*>?qt7j;z*1ZpR)N3kT)vcTQQ2yJUm<Q3UF-2=xX0hj>h_4&W<rb1rjcdCG<a9GFEbu'
    'ZFb?Y6so{pn`?E)9Kg5{Jb`7wxH&Gy#cRN022YmajAFtFDbMso}By7~88Lyr^1`5)#dlMgR<p3R=t>czSq%6q~o|koFArWRzCM4Rw*Ak1;fbzAvr'
    'pg2S%zR`%gD<aq6v&V*q?q7403QZ0<IHkxT({Q}zg-U$;sa)4ta5hbKHcC>5ehcOTW#lJQ(1ultm8mZO|YUPw}0>H<4FS1>V%E*Irim6`x4jDrJ2'
    'X;^E*+e7q|NlG$>>HS~LrT!fTC_E9nUpNPLF#wNaeE1y+#hCkEQ@hNBRYy7ASL2EW}5ger`F?0no*>b9W$iV0-GQgr%;;=rmBJW5cHct1qFEpT4&'
    'Zk<!66Wifyq0dZ=V{gJF{v=8pwPU^y#+1=zo#4S8WcAnG+3CA?2?WklxIY|dcX`xmAv606w{<Oej6#G{Pj?x>=$v$oWVM7e7l3fiOm^;brZ}#zAM'
    '}gY024imUZ4qAaC~Yv3L(aC;Z8!-1gHtf#KV9bcxVh~*1dDMF3VS*INo)Y;5^)`%?|=nySPv9Xe?jCupl{e@hh#EP|{HpArHOQx&PNX<f@Mf-F77'
    'K8yJzp)WdJngns7C*sIq^<%d}#Yk)8P{#p^9QZigz85BlcVgiamg~Q6H(=vDZ%zGv}ItB8CEDg~hd$;X%0DZ%~!RADFj2~M@-sr4<Y#}5FLS_%DH'
    '$qD)TUjjpa=T0SG*p(*QuF=#6h-o=QoKVyE;zqopSjwO+VRer-?+f}IEI&JGmJy6Gw9J7eXa4FGZ%CrZ_cIiSwY?J>GFq=d&mcoEO?k%)M~@CkSg'
    '=loV>(y9B4KUz2an*oO8_KZpcSN!<n8C{rs0sW2!1)<Rx{S-JGAyIIm?k%RtIC6LmK@r4z3@(^)7G1A5>BRz=A82pdN7ke9>5YyQj_cmrG<Y5i=e'
    'gX2%?ib`GNLbN9_ll}9HcVy-S`VY~7|L<oJl=^<$Q?ADo&;Ezz1ml6Fjya|*gKaXV9*3;rN<_|+uBn`|?wa0++%1iTm{FFduZsl4QZI?dVI1ybDq'
    'Y1+&?&&n1O1P;d{PbkD`A8Fd^*S1OeK9#loEOe&k6@E;kv24NlGO!ReW~V(JeI0b`KlH7xZGWj77O$0G=mtvL+kSD2)X5yJpfh8)u*>QZ7!?1%zg'
    'Q-%@7tfsr-#vi-Hl%v9*)H8fqc;XCC|ZKcHQwY7u+6`Tyxzk)i<a7PwmfZhL(tA9<dWLJ`G-M-x4Toivl`romOZy@SydX`9)RIZF*fOEP71~b*6L'
    '-%y&`*@Ea)<He)vW_ZMAITX8ZhN!Aokre|LTNX$`=KpK9SQoOv-MqN#sH@y&L97h)1Ka9pQJ9yS^SQ$5C*>jR3EqUqbi~a62CbhkTDXG07}8z5al'
    'w16L9Ac*LBQSya%W;lp{Ya&rYW{j`^08`}=ig381~<Y9hOEn%cLnt}gubyU{@3H>{r);Hcy9S|n7-o!DT*%g`l6KM5#L6%r-nICWA-x~Z)Ra%(MO'
    'ivUT!lt#WeAId9;y=cdMyFROQklW|=?XSe<<IB<xpsB=a!una}D!1g{2we%?Y|BJIdU;B41ZX1_lU@U0q0i?YadP$=OQKobDK{cxlLF06j@t3-C?'
    '*Vw%(WLO0-tr-g&0*)_FM!5b?JbuQ0F>FN{Z?Yh%C*_qLaseLCn3X+Gsui&z_WmC0B@j9)GP0fi1<gNuHOsH~pht%3WTqJ3GdR#vB{*C|=j~?;3M'
    'bHGz_wyeIv0pcuu2g5hwFD~v9V=0kG~V=uJ4v%cUcfhY~QIXCR7HL%V3L^t&vsoF&F`b84fFFN|1F<a_9|C(0X8m9nc>+@|lu1I#mP~1>YZkVt6m'
    'wwhfG!f}oBQsG%T{`nxtomJNti;85%y*4%`s@B9M0)%zT^tbcH?aHKe5sV&U)kRJan>kV;nz+|iOQC=e-^34poN43d);1Lw>R8fK$N@GWrs#VL&{'
    '=j``DbzxFR1V=mYZ;XtNsM)psC`Tyl6;Y994fOaAdlu+^YK?YaArHh;ijcsKOlwivmeWhyaHhrm8Xz?nyKkUeYVSpndt-j;c5!7a48G?_{9BwWZZ'
    '(>9Ml!hn{^OYo!g5j8)b)2P@CIriBtCYX#kH9l$Q^^eg%OVi5Z3#s&3rJZuq0tq`fE_M4d0}NL_6o}N)b2NR~EV!HtT^|i87X`8!SwFsV!zkW8hw'
    'PIZMR<eI3jJNzpC{OJZT6!v1rWyTsCCPI(zR**EJV+rW!fyq1N+d3v{^<W!~!U>==CLm%J?ixWuRo??X0P<_Jm~<B6wy)DQ9OszdI4`tza3yvE7h'
    '~b)Ol+?-TZo+<IxU#_UJ{KbCC>|G#v=mf4Jgtx6M=@B!)xKkC4fV#CAWgkcz8bHpRfE@i<mpXY+|Uhw&p)qb~~co8odCTzaf8JMB_!RCUK3SgDT5'
    '`_qKOooZNl9<X>f4@ElPxA@dEDsY;nwQ*z=>Q>R--^so4pj+LOxvoZ)a4abBT;f~#2{yGU6JaZmJPsK_31m8L-jBjPp$mx>3Iz7c&Oa55~%E|djI'
    'zRd40O@7#-7*x~uTvv1qfMsw-KPvfKY?!#o^vaoo`c<qGZpsv4&0+hzm~fJmUfbbzfg;&9Qj{o)A`*n`vmns__>A5(LtSYTQcA^DikXO%Wg3NYGk'
    '_6reXcFcn=nM7dJWD^c|I>(@QAm;w*>y1_JT7)u`6F7~Z)q5ahTA-oQ3SeDe3cN<S?*agv;^DV<w~It99(jTS^r7#EY0K30#F-nXyz7ps{T8sqx3'
    'o6!8pi`Q*f0K(@9k`3->M^B2)d`hR1ji~FAt~T?HK#nf+=jF#ia~ST(F?G-N(<6H_k0=ri@2dGrHq*J~p6W?hb~j!Wq>bYeu0_CgZuKd~q!GDK+#'
    'Q%|~2a?WJ#`JwYGxY~L+QiJ%d8e*+j7fLpwi_w4BqXAUNw7=L+a#h)a(TGX!#(PtZKG-2pO7;a)>-adY-X~Htrc`D^>ma;0$l&R_sl}=s`Do&Z~&'
    'UU?B<QcjhY4!~{45Jn6(ox*gCNR&C!&qA)lGn?~>?&T8>}b8dn~+i{Tlp36m~3U7*f@@OVToBUTan}O&U{WFx)x@wUb-4OYXr=clg9%9qoE~G*co'
    '^Y<I7_(0a-Zxkh%Ao*Q_NunPH*gw@-e=dgr{70TtkV%tQkWRZ|~rbb8xOF1rv!VUnaayJ><4f+O1QVch6p%Nuvea6v;YiR&jR&6@OWJfTbLSxZpR'
    'NLa8@M*ox4*8XsJ+4{UR(>pb`kA~ifIXB$3xRXmpq1+u<Bf8xRaJa(p`2DbyK6;_AM>RH5c@NlZ;}|OB`3KOz|6f}ZiF-he?Du1fVTa})!qjpdG2'
    '|Oh<k&p^tnm7G84CmU6hcEZH7ZjlpeFE*0))lpRMhDfjlzm~{>!2*M|mrwc&m*<eu=d*b{+unvAdV=NUvkE%gVC*_gwcceRl6>%?EF+w2f=oK1-a'
    '%nn|$s!FY~WNpKq_y!g|nR5KS=CKMYj@x7deEquF0^JX9c7J#m1Fjkh@t-#nJMTv5Bz8PJ|tFJ!ie)j<=)d3tIF<)=D^X232y5=-uW6q6LxvIQ1>'
    '6ld5@~(vb`sEbTS7baHVkyf^*oj)*0PMx#lIsSnI#%h}e^jAQ^!_C$>Ugc)nn=q|5OBin@y`KP+a4|{@_*~JYu;$F9)Cr;?$C$OW7zQopdwT|prO'
    'U5>_ow$?JV%V_!2keWA^&jX?}oyW*N}#x+W`rT*YYd$T<e#EAeYgEPr=srwKayEuEPx<)1aq{hRrSaOEsUlex5}6Lu)sI%m2ft+CFY&%GEKN$xvY'
    '%!7GR8upoK04|8<ZoG*qi{w_ih|)d}zV97`*ClgNR*Y~qS95Vd(DXcae;3&$j4y*Cg=XtA{VJq$A?@PB9|sd``aypGtK}06wu+E}{qzZv0T@4N<('
    'AW`)bnk}gBq>eCt`zFc}QjMe6fH<n++@C1=;JFD21r?`n=gwi$@Ghl~IMtGx6y2jJJ}Ucg=N8PAJSPCL|w)R=}2zuHlUUtPsEBz2w?51ELujBPzM'
    'I#H_lgLMwh?S_EWp?74G{f6pX_&j09u_<r4VcvGK96vh2$UrA#~Zp1Ub5Fh^F3(IWvLV%XL%fG)#k>dd!U0hTb+Jl-b@aN;>euA!0CKIlUXnD^BG'
    'GZmS8VEli6Tbo0Q8@%rf7Up5NHNgAq`|~*Ot|+?F)Pem5zwKNek5X)LB{V)3`~E{p5!cRsOREh{-Lm2H94n#MK`I@CyR;0!Dq1OzLs1=@N)m;u1#'
    'E?J><T=pXAsi3vE?R?F26JZ@0KCVB%a;`Y7P&2ohA<2|`m`c)Lc`XF7MS;fpI4fAb5hVO>yHjru|#umPK?gf}FC=l(khpT{=ozF{0a-Z{|K>+dq%'
    'ZiuF(lPsLd%tb#%ml=&A`kclM4}*@rGt)I)Yqmn4)&sj5PW#{}E9~;F%d`E<92=jYf<<H|K8d*^8qYH4k>i19_Xz(&I^tIMS>bqX&LXSsl%XDCVO'
    'TZC)t=19Y+bgw@!%StJ%N8Uxu`FF+8&9k0TZj=um1p{FRTwUYkUG`B>I%`J5inPi8usk_-RIiJ+Hs<ex%h`mH1Jv#z}giFQb~S>Y-r6*t_s)4EO='
    '<%(J#L#V~cdP_ZlL>l?SgCX50Ox^dbkEC^Bb`ZuDfhHyfzwWQi@@Bioa?K`RS^!*sefe15cycu+e8R4f5OS6#y8yGdcO^G0T5Yn}JdVJVNgzU41Q'
    '|w&FlW=G5a=ex+<TNy|{cys+*qJq0Fvuwe=+|hbGj11~@juMmT;(_tcZ~}eeGjl$kJQ67<K|yY)5bqRDMXAqJ6`7`#xWl|{cyiu=>+VzWxIt<IEq'
    '%4kwu5a1*xI#Rc13%aM>2d=Ic~=%c<aGO|N`?P8`=7T$3?ShC@R)bA{T^E^H6VJlCaFz8EgV#*7#WbQjk4gP}+}j+yS^qV<v@uIo%68n1;k((yZr'
    'lEbJ12iIkM*^b~Nj3_Q<r3=D#3AgwC%E!${qW5?x#wQY(Qin@|f{|#sCC;}W*vQLyyTcNooX!YpY;qUyr+hzIg1W4WTEYPX+J|Bz`O^DVkAUBFr`'
    'EfSOdyS4Tj}^QPZZbxE$~jrbQU42s!}9?P#N~>IlkFf;!jWyY!fB=cIA{@RMp2XpWn?`Jy1ES6)leXD7L?b+1}o$r2wJ6<)>SVi<bjS<1&hozcaA'
    'A(an{bNGPf<?bb1{W&AP&^(W21XwIPOP%3v7)#|NUnwsr)|7pU!{mpHw%oQa)j&EM!C(^<wr}bnb(u`L~QgYo+;Y3niMi1er8FqcvIF3SWLTUu9K'
    '!U+-^2aFvDK<1TX>Lpe;yvlqAmV_itx>xk)!Chj{*4DhZ((~zwMZj<`*H|1HBzq_<+^F30B^b<KmbnSE<bzO*3Tlx_NaBp-@Ci^gsIXoy(5agI5|'
    ';9JYH)`z)?cFOKbew<KL7dQ7)b-!Ibeq{_m1HdKSQHhxsW<=2{riC=;13NL)s(c6Xm*Fev3_`z&y5jnO_J7Iq1QzQ-R?!01an6^2W`>6EVH%UXj$'
    'OuXV!Zrjr3Wg)OHxvLP_Ek^&1@5SJY(!UAc+%(Nc|G4Y}pXZea?2gB;I?;(+&+qd%-ezPlT64%)sbpg#9%VGJxY)M0ZnL-f$@pj>{lp$80z1>4a&'
    'g6?+1at&x%5W4R%f&F5k6GUW$A_mj7bxa3t&7DQZO^#BIGU~70cbghKw`ah3sH}@^|)n3XRsvqDnh#YU8uLB`OgXo&P>av&pdp-)(+75`&u4NE`$'
    'kD+q;~OBU#ydekIrU79jkI0pBFZVX!AH2!~F?-uE@P}M3!3uOi~i2C^Q&b&g?1kFzI<V*2jjOVy?$p5NZ%F{|-_umYgF2RAc3Tx2Yx1bYg(QI5#='
    'C3jvjw%x~aORp$&%E7*5Pa4+Hb&3*lV*nfUG~wqUvg>e&*UC#sS;1A9@*~UY`5_2Y>^MXhhAC<UuGXvdl)m>vPWm<aZ!Pe!xk`BQ*~;?pT>_`QU#'
    'WXRAGbNJ|675s{&+%EtJ=vklcP%6>i`l!^bd^;#f%cnMrAh?Lj+iRPw})D{ZgzL7$VLX}WGY1vqGe7XhXO7-4aHlDtXAN|QIR^r(o5`z&!(6XwE{'
    'Zahp^(5RpC=`Wbl$v35;BGjRkmq@Su_iw7a7YTT0D9WGu8#l#ec`vXJHv^LPsKzC1fQNp`_Lb*65G8?l%(oCf&*um<1o2K_JWoBW@ZZK1?P~f@j)'
    'J~xee9bX^9-N(!|iH<!<Qgl+-prL_M58IFBmwF9%3ioVQQiEygE)ftN#V{nlcn@co3J4C-nQqJgt%VlUvGfNT8r=a-lv*jdkU4m!VHs3pi9M7T6@'
    'y*qdMaRZdWe3ntcE`CDtt6hgb!-r$zzb=~UI%@|c7LTWMsbKY;V<a_<D(&m#%WYHrV_wTnm=tC(!b_N9>iLZ{aK5hr5JKve<`|eI4vx~;yF0%2&Z'
    'T7<9<S2I>nlJ9XqthdLL}>uGppi@oKK#KvBCudE_Q&VzHUZH-JsA<GC@vkx$gBPm*>x)EvDe4~lb}R_%I)D{(=^ptpqI@3=ZGJtzxETe1G${r;BT'
    'qmnvz*Y4qOHsm8RqtG*%bxsc))#eSaFccOwvHx`P-L(~R41D~td)Lnw(cQSq(h)t_aaGtieVhW=6Ecba9J#eM$v4^4f6$uq-rbXmp|&+ozBG7hU{'
    '`@GKXSw5Sm;)>LnmVdloqv|&_St%vDXu<So4_vz2#CP7y-#q3@P2e&wubYeh@)MQ()C&~4`j%_6(9r&rmra4{PZ&0-Pd(9fCC%yYPc7k^<y~1OgG'
    'kgoo9*~jrg4Z#ZO<-MgP616beUhKm(ygM!fR}uOBB#j<X|XLDy}mlh?euEEtXY{A1y-W*Nw?QrVaiwg&U9Uv&XLg3#GWbW$8YNmhc4^z&I+hoAj2'
    'B%?%z`o&ZI2u*TdH^=tgKaZG#K1QWF}ZmEl3${BU?D6IFhWX&d;Z#WI-;OO#@wz~M4L8$=U5#-2|;e0<wZ}VO9(x!C2QlO84mO-uG5@6*yimcHDG'
    'oRfwntoEgxB|-A(J^QXG?g32kMvZS${%C|7)TY#WdbJs&WJbrsLZtSQIGH7*DzNHF)i8O6&iNXq5~o9`O)QC6sWeKN<kd)Qx;dF<3KWHSp25P`i0'
    'nJRAz6E<?aYiV5wF71Vcu-qsuD&F2M{a5o5G*-*`bm(OINuXO@1w?<K~(gE3Kd=f%v5tTWCjh3#fSR=S`y-k8o<3jUD1Dlr4cQOJ{Gt~+ULyam$u'
    'l2H4@{{hy0lrg`t*kA={I5Oytom-_FxizyZ-ud;{<^OaC<Ofn&nV6mNV+tOV9@tQZdI5JD(qwD%RXu=#B2XEnJEyD19BHfM(!Fq>d*X(E`%8>8Co'
    'L5{nlAWRAY_Iy-^k4=c@x-*zW6M1Y}?z6p1Gxp?_B!mX3fZDfw%}Af!hxOxrC+-2~G_cw^BTf073t)NmItRC#4$C$H{WCUt~XUNqc<&f{#ek$B8d'
    'tQx;U&ts3R-+}r(Kqn*cOGO{p3Hjm7IGcW7p%s$+wkE1ZEsQ5hc+su2{CE2`*q|sc6K1vUFQvqGokW<Us$mNadO}K{(uM=oOy$UDYDnimD(OX7<_'
    ')AiK8hd~njD>QEa%>m0Hf{m03^Cib=1*yCq!AlXlD-E;o|=%JQaN?98y1iLn8NW#N=VvE=|zuM2N>vi47TumZV@!7?@Wntpowa=SKc2@#nb46dX1'
    'Yz7w*7oe*YghSIJ8$N#00zQoNn3<@jf|w8Fo#r|OWGfbk!+6RFD@`u?c!AoXS)^!CONw3lm#55>h*yq$LB?RqV(+e0B!<+DuHnci`s$m2AVsP<%='
    'LrHddIc9Z67y8av-79P75iIYB^2C1(zf(<1>7AT^twbKXH(rzp)}}>1wV~`2M3`Ei>2KB~@ejuiV(j%7+5gPT;W#-U*4Z~{Hbx~9ZKhE&k@M=qoM'
    '7A_IM>AU@kGuZh*nJLX@Re3wH;L+)gPj)=iii>)DTPZD7=IWAHZHz8VCEPqkwQC=<V+syVG27{jPksF~E4uZ_KBpAg;j}GQK{%vZIdS9gkASTr~y'
    '+o5@&!)@c0!<c$jt-b?}Uq|u^Wnn{wU)81<iF3O@&=8CdZ^9mO{zFmN);YYzNK6{~1Pmh~0j}vE^<tc+rfsMauTgQ7fe*O;*4bbxwQ{w48k-jZX2'
    'e?X)e)QJ%=-l!~&lyYRkvvlD<g+tv{V9&wn|cYXf8Ng$kB)A=HA3K{#H8^lPGgv7j8iBMy6)0eXG=BmcH>W6as*Xa>;0PbFc?{mIp#SpnZ8m7__z'
    'saRcH>5`obFtqcKjfJ2kO-<OK0MzKd+Pgshj)cfLKuKk<sj6Mx{7Nh4`Lr<O7u=rY)ahWcaG#JK;>{l*DK@hnHNNBM_nRo9nhO^a`Q(O1$HZfNlS'
    'bGE$3ZT9fS2rgMZ+fSF*w^&E6T=x)THg_VCVW#zLrp`s?kjD@3l<O=$T?y9G81FJ*aE>(b5s2?nbbctQ<umK1i1i`3QK>8pc4q$a@a}|J>gp~Tp9'
    'Q|1+>sU*FMQz|n&M!1H(4SJAjINPM}Kz_xz>Ppd@3-Z@IXsMt&Lo%{Q1+)@NTndO0cLJk36LS8oQ9&dm#hnr{ltGf<pQ|cE<7a{#~Q-)*4oPA+Wq'
    'zfv-tj-ie;v4W^AdJ?v$iyUM^B-?MBeyuY^)$El>DaxLf;IUq*>PtmJL2eiTe@hNWcOq<N$e6yuE5P8#jHvf81pC(+kYQ%f@7~}1ua34ymaZ;Uus'
    'kBm<h{>u=X3A12x@LT=N7AmFioni8E%N>o2nz)2iYs5`O>nt5OUHxf0%A-&?n7q>DH{rXpj*T75#T@b?mB)$6II{sS^Ss5d%kwjPLv5t-#D4Py4H'
    'WaXSU`w9j6)&4nM!4qS%RX>wWC!WM18ds%H*(WiH6VB|2?<5~u(<tIw;6p)JV4xP0F2d3)^+rg_3Rw&ph*G6UU7xPY}85uLX2Ox3-DVx25VYIoUG'
    '<ipz(g#(EJpRm0yA!kmHhsUszg3H2QBTiO7m}g?%F~=7{0ay>=SU;su7ctMVVI%*<_`?3BkLy>zea7LPdO-P@bf+KxHB2hVh3lI9z-~HnL<@^_A-'
    'KE!7<J`-AniGe3@~_=*SazaU-KiC)^SDgH&@y2?{}{j+Jhe+MZQh)Albc6WEQlsV@`7J!?14<a{5$%q-(hO5I+(*MsN?lLXpa%8Ub|UzPVjfu8>Z'
    'lp!qRtY@4)9RHO{7W6Nvl;{JcusTcxA5>ZK+fX6lsd<LJ(QzK|8*JMpWL;Q$wGa*IjCvt(T^wkS*680W3ANR6SWQjiUYHhc4G}!@#vO71QIqbwA0'
    '3U+U1OeK=x1O%I&GW#skw}kT<|2Q_F?W|Oy#++K!To(g8G}M@lA=QId*g*Xdq?(&!Q2EIl57hyp9;;Q|EB6P>x2l|Vk##YlRSl@jmY`GWxm~eT$^'
    '1x6#76VS5ZYOiyh}3Ehp6P&bdpNMgVr!c=V2fK^*iSx#~7Pp;22tNePMpjN~0{6}46zSESfq!5B&A7<0EPw*I+=<XZm<Lf$zA8^<d4S-DRSc!&^@'
    'Q6-!vOrV4lfNwHjWUYEAP;M`y1{sV8zKKqf^nb*%XXXazXPjl$is4B7VL0IX0Oq_Y)F9bNViuxi!in{>$WawZ`u@#eCHv%6+HZC5?i?UC!M3E!_+'
    'tFxa!A&-eC=gYP`ueoE&Y60LL;ZozP$C-MEfb`OTgcXE~a<HY4+0IT#%!#)I8AJUnR&p?~g7Jzfs<=tZG`EH1(<-f;5h|3Gg;2FFRy;ClgTECZX)'
    '>*3}(w+-<O_sS6|rxMMHG(#g49@CaBbEw&Oj1!b>9$AXdRt8HrM`h6g;Ms;hvQ^Dk0+=s?{f&FbnO%wmg6YSaJSAR611hZY52m9<o<JdZ2wOj0%B'
    'y0wLw3{rNPq-O(nuVoHxjCwHTb_KRBo6QWU7pukpMx$;JrHbc8ZVFuS(+_y_r4JLyL`G!Kj9^LsFvctmSrZ?$}c{Wp_@5UQ0ipj{*9L()}I!i32?'
    'ksnvtc~m?Sft?RUCaB);eK!ZZRqqDIQUq1RwmE_T4QRdKOph?o7N(q44>PoM~mn$ceqKdfQZgh9pr+tH6tJ>K^5q-jHFHyts|*`2B!W>gcgynRgC'
    '427xew)g$KFO9(TEOgv2v{Z)hAEI0sBch0_7|WxH2O$xn0~PvD^42>Eyimw^g_~s5Mwh|A#}PgX-(Sfjr1kecTLfzllD^eO^>C(^i@6G`xO~R%@4'
    'PM=mHzS-1Np@IF|h!PKQk#`Me7tilKh_zV}M}n9^$i$Qre>@_Kiec-7-c8h~!w0@paf~e+rEH^^%?U9N!olE9B?&xA*9?Mrt{S$!{rVi)w>0MuK)'
    'BvIk95J@e)Nb^N4nt^g+osUGvKD*<)8BMSxkJrXJETIY7W0xt75km-{d@4vZJg_ZZwF8w{9Fu(8pS5f?S*RiX|y%g_Jx}B?*KDtCOzFh2Zmv9+h;'
    'Ska@P99rV^|w}TSBV|Qj4C`*_RyfyA*Q7i_ikUCjbr@-REF`n{YxaiufihLOP?j)mMG6-5dm?;q93=9V=L#L7oQ9YGt~?Xd1;IKZr+(!2{sFbWj>'
    'Bps}UjU58&s=2YXNP++FkJhY@_-A{ja$$alZKzjWkT2EE<Cugp`I^6u4x4I#pbQ9d3k014UCR8f`WnkCm>mf>Bof>!l})Q|{li2`C3rJxc=ZhptZ'
    'YwzjONrZ^hDel@>%n=qkxBljeK$h_Md(gNyiehYv;VN@cPleie4e7c7)gO?t^eeJx{G2e;!OmY362l)F>nWgKe~T;y$^8i|!VnXl!>ICS%K&o$vK'
    'X70q@P7)z!7TFouBQ|rBRDl{WhKh5Di*6$}=vSmBp1L+K_Hb6?{g>C6(Rhk>VhH;_4nVLH8|R!VMIv>9HIek}|BnwnQb>rXWOy7n<QnN6NBXGoK~'
    'yEm4}#g3IUcjg7C&Yc7G8B$!0&D7awFY4cRy97b$Lg+a!ajK;F>ZlT6YRyD1vV8?|?Gp$39S!8gs9r3}GHmn&dsJ0Nmgx26^nFp!vqs!xZrxc4MN'
    'tAp}P_qNUl>{7j3sebB$+aUDl?;EO41MSQn`1&h?_q90!Q-yl_-1^!7Z;e;`C+`=SWU03=+4vV9PYay<?r@mXs4}?1P;t;l(n&y1{qmXsLafbu|-'
    'etmy681&hs!RV_B2Tv^CDX%Z|43__4<gxx9M}5f-PA{l^U4O&S*3fP++SJRsv3#q!yMs(sdIizJw@f$QLRw^EuLgSxrD#KdI4PBr~SZQY;r81OJ`'
    '!y?4ohDL+N8+x!^@8oHkAJcdAbQxh=N#=D^JPUg~>jf=|0JmV!?`L_o$4dchgQHI%g_0Nk3*~d0={JnSrSQzKF=6nYZI5ipw)vV`cu?c_0NSGxML'
    'goRF!ntq#E-gX+`$gAY5g`W{r;_Dx)M2#&-vUwFN|rOKBftu;@Mrc%dwvV9J`iB;v?*haS?r7LTeZ8@XP1ZO!z1Z0?rmqki};YO*TWQ0O1!dbZmk'
    '>pwH$9ouZk_rv-I7(+Qq6rvJZHu1mwP;0mJ%8DplcU=I(*Qx$T&Nwf<KFmn6dI=ifC^}#Ab+%Yz?zCK1Y|IHmxZARHW_9nhzsPTpOW(T9Co1`pXS'
    'b{05N-jSW$c`S*XN4wV+QQ7_^duF|@g5I(8e}Ew+;}7^EHkxkTml*kfz!BEEo=CO$X`6)^b_NY=TCjI(_6Hz<juQn6e@h+*Sv>+<5@#vy2=^V(f='
    ';bcbWD_`VN>ctUoL&K|_@GH%|!@dR#=u*l5~NtME4RR?o5Pe$9ZnBcP*4pmbSc+_7iQBoY={XM8R)^LT^-wHwpOQR|UnNY_BP;&+WUM`DlPKy!Q}'
    '<GFrk46;-d{h6Z!t!cW`13Kx+7`c_SCPqq?Wr^$5$V;RiUnZlEr|2r#G`FhbUk@Hw7WgKn8B&FrjXCQdwxsp-O4E~}e_eorl?pC|(i_YfI`ch@f<'
    'QoVn%mTga+kVAMm+vwk&_u9r5G{HQuWNu^4Bn}h8eby&++xx?pH~#S1C%8dG*gYOP0U18VgnT+$KG0DXNQr)<|^Ye1qCT=OYFh+sjs!<@RvQunF-'
    'yci=sW)~)>!C|gMliIGX|M}{`WoYYN(;xVVX{?RLMCc&Zww&d8OcG=5=`af^Z+bW9=EQ==?+E4YPGodaGy@1Ox5tUsRJe{&0O9APS`(*vs)l)E`t'
    'HcYxLsyxI5h9Qn%OETG@-R*`%1bz>u8<<1a{|NTPhl}u>1wFD2B3U$pNx<ENBdfih(bIhyVra4X!MQtW!gH@PX=IsxxeZu($4)hnKxzFbT>Qz-pR'
    '3ZES}yU)vjo(@-}Mt`bKcITwML7^Rqx(B*%hDs-f-pQB*O@R7)|4pb46Q>8Qfd<A3O_T+w5pC%7b3niJf4mLg+67;WECn~jUwo9lOcA@9~0+7$;?'
    'D__wdPh$0)T$5Zp>t}u5YMa7uUI{s+s<}JZ3O@)8z#(8)J`=V%M#d;pY){)1!D&S8-FYXyc+B>yqI1mrsLG8;ax{)h@i?oe6AW-K9^@Fdy}f-~_)'
    'zVj&l1m;2pohd;poG0+hUj9jvYnN5o6!X3L*J`BXf<XNx4mdjo{xxh^PnM!BQnE0;lCz2D?GY8e)tX@NJLS;6`SsAllC5Y0Z!Pd0kuycQpgW8>b*'
    'w*U)8>&TXVdhX#{dDr|sv)3|om)T87+Bh4)lAX6?FL+YZT74BvNak2#cPH^3Lt6~R~41bGRK=EM2<G}J#)9ZasYgfXQH>881?MzyB=?$JY`D~AK!'
    '8B1^Al=eR^M3J;{Bn@%_q$W4!H}?n453uRp3%huD4yok6L)kh@tsVpyb`ww<v7#MLc*pM@v}9$5=x!p$o6js;nsHKFp|8gGAz*&7J$_O-j!l)D?F'
    '3bRZmSkXQ4%&P}xm}F*<L4Z`U>)GU_%E(k(W4#GEAp!^)?ELSfuTaq|3Kqw$v!kN%wY%&iOgLuaND0|T??w<JDQ@!1q`ZLcQU8@3#q)I*9FyI(Ex'
    '=Jt2ezjGuh(ROF4a_#uxrrWUqjYN>@M`5)4=GWrP4ZlB2990iZvmlFvL;%=7a^g+}3E$faBCd;FoRC&I%Bs!d@>+>d-l<RTmUym}s%J5}!QhVxJ*'
    'Hn+82z0Cu1S0;U?{5e&C`fJ=Ujqm_^ffaMold*3!9ADVXaZ-AnHIcO5gOAxfb7%fPzJw?9pRNy_6}^vx($KG*M%1e2D=4CLW>_z|N}W7YDW2naez'
    'l*w&4W=w{i!U-MyoZA|=nHO^fHkulP8kE2?84EV1|M+R!+Qpw{_b4sdF9Jnpl?wadnE441*O!<3~xK1VBea>9oV+zDQ!RtFkvu5f<Fg7}K*UtgyE'
    '(puAmW7;+cEN2v>O-1^co(g*h;q9Z+V4|#6waLH6JpMFO!Vq9Z=N2RqmD&Q`T9P1dfqjyOjNl`O>(hWq>7=>rv~~Yms+Bi8XFyW7Cw==*2FVEZdw'
    'z;B0mmZlLHbS+|@nt%f&?h=p1U_7y!j4d-Pz}nw3E~ao?EUw=IX59f&12jh6=YN_ETC?dl!U^zRZ~w{Sezp)UmgQJ692Xt_<^xu7C&zN<Va_V`Cj'
    '{HMfR9`Er#`Q5Gw!%3%QNHZQ1g7vZ5U#&75-ZL$#Q0~d9smJ*dQ5$kH?=owa@8(SzxudN9+E&~zSn&*=CaGD^d;Zvkq+GbsxYcjPu-GenuXq!IQb'
    'e$80^gabOhQacpR7VeL9EYP+(|+N_Fc8rXvU1I>Tf!5!+dU;|1ELtHV7-}LyU)N7?!zACLY_47Q8hI^qD+z&--lNt@ZzfZSYVS!9O@CY3^6Q36UX'
    'qG0Uv;{h?f8M_ydLQ_aN($OiJl3%<ln`~F>Iul4xffb}HUq>TJ;Z*P|G<0@kW*T&=Jcd;>B&z+f8lg826vaK^@#F@*Ghif}$+K7(&!=oXnf5v0!i'
    'NGEuu5`JCZIu9hpwj&%99m>AHHram2~@Gfv@HbkU4q;Ac5{rMWHJxRH=^Z|cjq@L@19=snRN92F=8;iFD#1Q4mJJwYTB@WgvK3Zv_1IjS7d6ua%j'
    'Cbd*Ah0W7gNkO?0x?665^8y(ddP&^GzTE`$Uv4SE;nX(w~HVSOk#nA}sCh1vN+1e6}O<~+Eyu27}kw-{Lf0@~Lnq+w2%BCpp8QT=Y@YxhE4m_s3E'
    'u!mx;g*oaVhJsS8-S-M2H+FzC!Fu{}7a`R)LG=F5(=b?}Hs-SBS<tZ_JER_s7wW9aGka|dKAy&5G&Pzs<MebR$AtJSvON-5pPm(u>u^Of%>Q-$61'
    '&Nc?IN>YH6yXibZ!0<{Fy-k<Mb_DdluM*fi9uVd*(A~zscVzcA%OJdDf0_2vPV>XsxG10;Kcr8eL^5X?(wXdB_JJmb3-t`(tMIC<7I|zxt=J3PtP'
    'A!Mz6RQ(8>TtOx$r&HNP``|N@28^XWU_*;rLHzAN<8_pIXH-NW>{TQVY?3|xfR{O}>@?|V>Apg;ZU7UlA3r1`7abyOem{=QWodGJ`Ts^?$sxI9CO'
    '}DT4oFaJDCK6Iz{(&ywrVC=khsxWAf;zb7a^BJLK5MM@0r6)udiAj)fiWI$dSNT+sLM8P@=ea?orKw~pL%k^z+{R?Qi<}|!21#X{n*Ks!JBBewQN'
    'a^aWDuf5#!^Zl4(!Ee^NL#{AYb_lIIg9aSfa`J?0q>#vgV~^q$HvEC_5Z>2clSeC(m_c%-Qu#}WxJU;+pw^;>=#LujSrw~gX{-rctDocmok*S}lY'
    '33w@$WNdZE_ls_mOqmB%drlp?#t4C%>dfO-^0d-x(E?@QFK1DdiktM=<2NbfH9O1=^Hx*#u~50|Gs559(+hcdvsv2KSmsyH+9)b<8@X)W&sN@z#;'
    '9sMGk}Y_gcC0MOZ<m+;g?;T6rSlQK-FkO-(cfUJgH?mLkwhyVs*&=Uyg}Q!xYR3dCuwW$>udwcw#iEvit7fk9Hvw%Tvqzv&z>ViN=F3H=bJaqG`o'
    'ky|rig*W=l76RE>?bP1Ljt+DG7!iQS+v`Pp(mv9d<e!umbgV(r+zlegJa@gZ7K+TA^cp$IMZel1DWX2YKzH*Z;5S-*LygUEGiN=Ey@ZZ0*l6?5PK'
    'FhI><>?v6;u8*O^v6s#Sv-rv1<ea80H2+}$U&j5FOHpBbrX<eL<ZoLeqUbB+n9@-blpE+c-KR2gWr5f0v#o4iF->L77dL^&bASsI&ru5*N-%<ryM'
    '%Y&uvBpjMoMraL!uCllh~;LIP>e-$DiClXJ`7L(GgLo2pEP84rmKX1zb(_q-sk*$Fn_>m3m#o1@988#E1prcYCfavzXahf|9vhL+hU^v+l}cjaH)'
    'qs8spS;`-f*QI+^iq)M?ouj_uP*F@k8>#ipAiRH8c(y|F`#bv<mHLkf{}E$r-2UD^L<(iP={*10IcC<^qGkjbps)b2<4I6L5U#HiuYeydf0N0s6;'
    'dXw-Ps;l>OWb)%qQ2PT;Kn*MkSCvuGqmn%={b3fq>!dY}%W!10L&5>NXye_{Y6;GS^d+mW6JDOLN!+YxDu`l3D0M46mI=+Thw8Hxa$s!-?%U#^0s'
    'v_{(>;z_1@cJOna`<T7=#Wpbk(MFx)h#81XNlXB#|?%1wpnz#NZ{s+nEMa6(+UbRJjC@G%jUz#1cfWta?Ac@X+5OYWtls!IZbf$IroP+abc%`Fwi'
    'oyg#J~Ke$Y?u18UH{HO=51l_spab6hb92>TB-w?$Q=fuQL_^nDx`X367&{8{u>67Rvqq#ZpK{Fu7p<{&iDHfzAceFi9k;qkri}IcNUI90uF*8o7v'
    'KlYQht3oK4oFVAwDooZ~GfhQMaj7+(D?$-3)<&5r_ji$a8Ot)ajZsOY@1V`{wJ#$^)n{w#58#-&{bqJgsioRe(&FD(lYr9#Ly0m_Oe0*y=HsAU76'
    '%xXIxg<y}D^y1!j{aQ+4LqIo=$FTizGv^ggBNh&T+4M(!DLx5H`4Uq1S)+;v;gE&X6wt-%2T^_1l3p_R2cp(m7!`0~aH<DH1sa<(b-4du^7KQ~l0'
    'aK>xF$vM>!zd}OxbJHk~dfu*e9ETJIBJdhWE1m8U0(JlE<aihP+qXmS<j|CK~_-v}WN-pFr~yF8xWLpgiNTImOgPP!*WqT=Mc<Gtxjicb6y*qV8*'
    'ce9hBET^e#zk^;}sGVw~E?-Ff^lE{%DOaP5GrV0qui!Ou~rlO45-Kfp*Bhn%c2Nmf_(|kNLu%faEj|zh0n#}9&={HcUqQq@X_^r|184_$w18T6In'
    '&kLD&!R0+U-_44PB<aU^VPUske?3-U);%#U@FGMCH+mzNCGhy{ruxyYiUp}3nF;Me2#Nv6d$`tZ3nN4(8IZw0%XUzni6vR$gcI&942g^6?&ncN!8'
    'mz@AwukwU$e@8UmE;C(QdOD<J<oi=#rfm)**wm3+OdB(gGHHTDyO392kYSJHaduR6>uUR*HHoR0sQ_xJcFHt6B!{Jbk>TH{_mUP6-I0!~KZH6Eqk'
    '0vp6LG(+m6`#tjrBUjjL(}TYbUMbgiS@7>a8Q~JS!#(${HFleeto^+;&)7o&6=3B@GI@9Y5ZcT6c@Vn@J~Mtdy2Q8-5HQC{Hp6$$Ja1F~vm716;P'
    'E5Lr%#me1|q53i?oG-cFR}yJxzxfMGp$r+cnhJOkJyAMr+2q?2;wQ5MaSn{{23Q-2>^k6{iGuT0uVzX!P|u$9DWMY{y_XDJhl^q`sXDGtNwRY>5p'
    'nrs8eK{r*kZNpgT^H+G;g_r@<)bTUa}TI;35d-FiPYtd}SXMuYQ)Fwd3AWC)aYHs3sKM|NteYo+zHu`}-gd`qQ($XV`f(yrlE!G0rTA}TP8(iTvv'
    '*TcYq(MM*mq-W+V@5(}%|oOA-zCFNcYc?6&9*4EX~8PznNnW0X<RuTpv!hoSXQhT*7g4U+dJQ!KjV*-{$b#Ykm_z2n`+^+AC;))q`aCcDQt}cpY>'
    'Pzio4>Pu9$FWx#MyGJAJ?DyHyrXfbr8raP%b<Jqo!wHm=GMmG07SUtE{-A8Z&xDCdJV3#mrP1f_45_a`6{887|}!J4d3PC>%f%^N)iJU@FLdE&$J'
    '#sANjt<ve(x?(i4b%%%26zbNZqZOdyE&W9}H8k8H{Y1r=tjL6iweg=((Qj<av4I-L`?$xKl-qSDvUZs|%A#NNd`A)9rwxX^N5&poI<>}Ujc+YxS~'
    'Lv?<}&Pj{Ivo^&KSh7DWipi!Veb?fOQ2scK~Nj8>7D13+DHxZ%&u3xQeyhd*za{`u5;xZl;m+evf}H1(JqePyO>JqH(OS`=0~-x5<}Mu!5hx%7{n'
    '8i+~{YtlV#yEpO66{!j)R=cJ2k<svK_rf#&NE#FN_4nJ96-sI$mOXAdZM&@yXM_^sks@QZXP`A$_z1O5~EbFXkoV@vkilN{O--zN92Qw>(8G6${g'
    'bbRUlI5Q-xwQ3yZCpYMsm=$Yp}ZgE)fkdmsB3_i`8`U;?CZ7zfhU@FLwJ1_s^r<CAnq}0>EwqNK1aST`)Y1#`lxgB>!x5&I{WbQhQ8(3ov!YA;j{'
    't_5~R)Uk-SJFa1^kbFXf6IZ4^uZ<|c)P3J|0E2Pi<J2L5hA^B$NtM+at)mHLyOqR}`-#gq7!B=oz+aUN5-DJ&d5l#<ZpW4QT$|1#w}=#ik-#U20w'
    '^<Y!d?Ef{|Thi5#nk3PEfrqDvhc!%&JDuB3y&`u<*&lfOoMc16bX8{vNfGhUU(67T)`DZMNfRiaaA7HTxu;Bfn*Fytn4P`cpE{a7oCG><N1tWd7R'
    '$h-w0PSEr(8s8k};%?**_<#@*5+3n}^UcLFTgCIqDG>nL%y(<JGkC2h5z}bpyCAWm~IXi~V<+vkjJh(w4)Tsf~eH|E{p9n{f*0?0Gb5DK^ZYGNrf'
    'V%UYubHavN{dawNs1bOKoF<uXk!29Z4JmiOX1mFVfo+~vNN|-y)*xaQiDVP?NK7(TK_Zyz~!`idNdfw(v<2N4vN71sTTXqoC6a?ZmmeAw8M8@9<5'
    '{`^8GWlxX?vdtS?QW_u75_dD$>r&pIhY9>FrJC633VPqNvXhZSRxO_|IWS<IJEw|`zd&tKF6n1J%^w6xaBN5OGWc&*Q6D3);!4!fJeq{*M`y!Y^H'
    'rAdf8_k7j<YAx_EWgxCRvANW#7+pxc+A7F(gx=?=F%LZ3y>%b!R$NuaC*di}QFT~zemOy>Eg#F(LCGHAj!OJLd=Q2~&h&2uOx#GE(WUND|Qu(-E5'
    'xYtfXj%nU?q{lvHAD8>kq?vH;{H(7shjQ?xgt$u$Qv=&#O$IV}>%MnBXmQde763Vl<=+C{-!)jHX4BY_H5;`f=zq7`aV7*Jna=XQT&<+KW(pbS6K'
    'va$lm!-pP0R$a*JqJl=O^_aacHSzDAK@d!n>I($G2D`0%&b_-4L-V31r;*UEbj#>~Y=u>%4lI<x9R<1c4p+y>3j4@zH{vp7OL{pc)&{<6h=@n8^}'
    '$K1&2}tzSARzs8h2bB$I>4n)?L@X?7aSI8|b$~t`A5pamoZ=b3Y0)e9~ont+}({DorGW79I{G26~;j7%d3qBA~59o(8h2r&mKg3bu9FB2@D|tm}?'
    '-;k54FR*9Z=gap8=_Txk{RPgQ)lQpTQ$TWy(?RyQPlOO=+hm#34qPC{c}z^Ea&wF#u0TEehOxZvUz;af0yX?mzNvgFVH5Epq}?LpK?dZ(Vi$L<Io'
    'N(ZBRw?&O{>xpdE00WS*%~t8c7AA|z8*ipKG|a<z<Yh>m@l&&mD6Q@B#YA-p-xKU<D_OJ(P8$!nmYrT}1TYMbERSgt<}+SpguDZzOr-0ueEj^0RQ'
    'ZZp{{iVFn^-=5n?I91Rfwl>#r;VNa%7^KLZ(hD9Bz6eGy^=FL`*P+ln7uy|Oo8*)BgGF7`w#(m=z;+F~Mn00l{uhoApn*wG%a9CWp}ebgy#Ck`3A'
    'Yd-gIBLOT7J?KIrw-AsA+>+Q`3XIZ0C24qp}fn$rHn?&SeU1NMS^%@@$20ylLo?DdE5IPrCM!h^~c=`@T;7FAZe>_H9>Oi|8D0iJ1W}8ddH~*OxB'
    'WY}L4x&}~nJtqrn#UKY1MgF`wUilMQ;f|<yY57XwDCt}G=%|pLXHDbCE$uhW}Vha3dZVcDa1jSo%C_h+9fJ2Yx+12XDJ92jM!$Mog!E|FPpfv6=z'
    '&;BcTVgCtvo3L>wROkbEa4ZOCLH%YsVMgpxJr!DTbij3Txv&g=ni!`8sm2%y}=8cf*gqAVktK^$nelhsBbaZqsnZyXbvI)&gcDih2iQYmPf+3g-U'
    '*$9hmWAkL^&>ooZnTZN3)2cN}$(VYk>0WMv-gdbft@zg~p_g~QM9N}#(T^p8KqpS~d@AiT<ts>AV%fu#u5m;SC0(>hQZ!MyN^R`}vuUiq%Z^?iwc'
    'b7q{&`>&Iimkt_SFxP#AYI~oux)Q1AR*&T*h8f3`39;PFpDV{Sb|)MRhc=M%^oa;$c$d$YY*Zx!Y&;lGgBi00k0C{shLtHU*PHjm^?u!_>m4D6y-'
    'kx)t4*JI+-7OI?xw7-LDvu4$897<t;c6NKGTc2O>_&RJtW)~71ReY?YG~LaJRyyH8$E;u%d>Qw}e_v9xgxdSfFVQJos5z9oE(>eY%<68Vdn~)mF?'
    'EaB$dvVAZsa`o`vUKT5MbOS{6KZ7TM(q+8fh)cx(V!m%B?<K~RdoJ#!21Sk0%gp75CP-*<afq%y{uuL-Je+g$!k9@nNLPeJq_>wKiVAExQiyRKdA'
    'mtJD4lHuShc>`V=q#oJdq{GDA>Mb9<89V@B?-Cs;HU}DMCRsm-anenEu2+)aZfr3zMJlaU!=Kjz()D-6J9fxJfjYw?oUz)rH@pt?P#FL1*N9g`zI'
    'aGGEAFzDo%}sKC67JvS0#oF^BLG|Mup=8a0^iNBG-$8)g(a)(KZJlx9X$q;A3W6gea(7rlZC5=I!=mrOuR0v(`F5+BnuZ7P|}ULTJK@CZr%Klj*N'
    '<oo}tG%}$%;WF>woTdG|lafWLfV#B+1i7yZ7g_5qF$gYns;e#tBk6;6{N(#()&gyH@$>AwVr*Yav~@cY5nfV)QR2&vDOUFWK9R0|CrNx7INGT)`v'
    '{Ww{Ok<8Q??yqO4~0vYrrqLIlh>&ZSf`?Goxcm!mY8i7_@ndBIcem{dMd_XR*fSD#TnFCf%`X4UgSoJwBhIn_uS_g!RSL{ATf9%kQ&oSFGMY&7FV'
    'O+JGcVI@&^}W$KJw!RB*Br;WsPmG1svte$5CC46&Z5P3|%3jzb9EVRU>tS2HpY>oTRy=4y7Wc~RNAK0<TGz_NVm$6ppFw!v0dDM<kGIrmDZR`iq8'
    '*{E$jW=(+LFi4T8lms0tWLi6x3eV#Q%&#FeXxqo-S~8c+r8(1YsEQL(^UnZ+p@N7vtfrLC;-uBA*|l<BE4IpqQ1M?JLG^4x1b4KJW3k_4q&g9201'
    'eaIUKu?*1U>!lp5k5tlV@`<>?>9Gf#M|L%zMoXO*%>j7kBcP2<-ie?2nLqe=7X>po`?9$~gN!|Msb``pn4V)eeIo|6$q_%f?qgyGzbgh#P21`#Gy'
    'M{U+o8&%YIMI9?0-_dxfNLs-IKT9077zw<S-NUiSxA(#r&v9Z9HXf4<#<X@Ey+CXG4j#_9gp4kXo0N~OA%+p%9;x6u^HC~)I1}qo42cBsxZP<}ye'
    '*l$7w_3E1RLz`@AYr%hI)#XOtLAhW+R0QYqQV*MfaJypEG*vlJdS@fC%c_EcbR(bfBEnFn2le+O{jpPt)q{DuqE)X`C{bs4d2iW9w@g9WMO<nb0L'
    '<{VlrZY)!GuapOBswUk-`t)#_ZzXpG?A&fS&djQPrxN#Vf;T6h5eoNU=_^(rtLLVYe&zj2weAkj###gQVD=WLX@9vxCgQu0VTQ0&^KP$BFV5R6u('
    'Q)Q^xaVhKYM~0C1C6bK*R0QR!UoWfjWck+(oYJy;Qsh6|KW5Ga&45*Y}nEb{^<-fVVr1X9dzjvdU$J-STPFkXMO6i7yH-KOwn}bcr_vMDQsB@5jx'
    '~=Xn|e1GLM9^dyWvldM<edPdOT9?yQsgCji;mHRFBg_My3Y5Lb~U{x{5$_23q73L4b+GaMVEv)~nAq7SrnWfau16KHy7#=b_)c05f|o_|Mxp8VKw'
    'p(-x+wAF{`a1mFa`pzY>Rdt_Rp*ai!yCud8*HBp}7>P~y4xTMw=D9vA92?_R4#}bzdyD#<s?4PyxfEKHcp7-;pGb`)jH}2d7DSqJw<Zv4o)EOPJ#'
    'KtMQAebSq<I{l>Q>&JkqV_eOYkZT0kf5V^!}gCX|C|LLUUNjL<GCyI}kW2y_jr>0{5x5Tk>C0q2^-wPi$ecYhmiN3PwMuhPn`X9-2V6pd(Tzm#G^'
    '_v|v5%Gurikf%e43r_#z}eSXeALp2i?!~j#CDU)|^8oD-NnWLD#A;?f&oMpmVB2%8!DrcdrmmhykMAn|(%h;=cqUJ$Ro?1fSt2%i%>%%X=?&-07='
    'Na@&r7Q=|r}myxwfI)QRf0iPw#g>Gaz_~gxvV~EyJ?FL0McikH?$o%wrs>J*&3|x*q1!iyWO#bsI_zwg1{q6$`580ai2aa)LG++B@HT}l8(&hd2~'
    '@V>>ppQ<h!z2Ckh0Y_^gT-H(}KKZWUxySHK-MZ&G56ENS$15GKv1OdrjmCtf(+2tBT0WEf)A!pcTu^t^hwOsIZk!?04L7k=LAuB!Tw`fvrJjaLmk'
    'R5$99Ps1}t7oo5#iyF(L2g&|v5XB34qw@&?8hgs2UgY_t4SV!!a9PUP8voZ4S4<h+i5+9hO=Dc9H)2O|Yv*_4>7uoEy*CHbW=lEk=_wvtYPd?tr9'
    '9RIhZ-)Vo(U+G>orWVZsJ{&f?A)n_<W~k2L&iDxnFafFB|QO877oL0nV%gI=L+OC|<|sttnXz1kp%i8u$(zctfBg6W-_wsv1F^bnG<}$YZHG4~|0'
    'r*w|iK?|#G{wjJG6q{lknYoGkM&eO;;3JdD$2C;=h-(Zv@j^I;_N9=z?m$^d44r-TOmuGowb~A6lAw$yOdo|K>OjGih;O4TPp=z7OP(wn_L-`&_{'
    '<a+P7tv6jC^yQpe$W`l5w_y!VN{59y)w?&TTW#=Lf-%4!RIp7J6)o&WL{C6j*GGLzLgd*Tqb~>gf2&kv>69Nv%=cAlN>A>>_t!dbCP_S&x&&PgzY'
    'Ti2zf?R(eu9;F0ddWpSfC0_U6NK?PU8b(jK`M&GXDSWlaq95us`XCCmY%R*0%9&&nU-mp(-k*bQxTNjDFf3l09?GF!Vs{Tj%>@?@wXEfDZ+i$-ar'
    'pr6Yzhm=F6y8SI|_j!j|Oe44f#+&E5cIb@6qV|OuqLRUY%l*<3EL^QF1lWhap#tu__`FtlF29%ra)8b)X@uY7o~dMSHa1g5{R?FIE&g9Oi$YMxY3'
    '7UjEV9>Hnsdw*xja7jagP@gR=tOGsGJzHR4)JJ==>f}{`kCz9F7TK-UT;lqm2g>Y0^s#pO63-N)3yi8VyNIunm9u5{dixoj`@MJ+A$;zFukFe-lU'
    'Zcmu~rPNz1)lWaDH^F)*5Jij@BHAGwweW0iD7?mVl^jmPwEl6Q}q6YK;&1d`?zZGkqvg$5uj|&4el>EXV^Zu;P>x`1D8!>9ERTCJ9e<H}c|MIH!G'
    'Cy1jOBrVQ0$pzL?evnh5n)J`l`(=I31&aD8{^0#q_%aD8E!E6n#~+$CJ2bCKyX=w*YmSNZ!(HDDE?I#vCQC!hp(DlulbHhY6ljfY9VJ3RiS`@et#'
    '0BIdd3ba11fo*;t7$U0&ZZmGs8OiRG4lcdR&8UzCzZmbd)AWBa?jE-h((P?ON22$Oy!t>jr}!U5J*K=?6YJ=+h*bMtEO8)4y}^11cc8q28nn}8oQ'
    'HQM+14+6#*hyhQvoXY%R)-WXe)xmoK$8BSuB|4j3@PC8lFuA`a+l{}%0dm*+Ct73<^N}4nCLsa@S8kz8eP36tJITPb*y{BMDh!JC_bD^zL5GDFrv'
    'VjA!LE1}!7dI(&s-)5_u;vI7U}A86{iV?C#htz<koQHg{Zs+joep7PF-9!C}f;!N<zKbjYUgO7}!G5)0K708fNVLkOAk%A$LqzpFrd3w)LNs7U6F'
    'fw%-1~{OY;Hy(iJVV7z-atwqQ4KRN-4#t!ov=7iiQs{HsVOBtX?aKFe9i?Jl}^xD2bh%CuqHUr@MUET8tOFAN}W^q}Vb(qD^Il6MniaAJe_1O>IU'
    'NlNd(_G^^XwMvS(ooaK6Nq8^qbuTlS)+)P!givLGu^k7`MWOMRVF=zXPF0_`zEbn_5Au2jSTMoTd}T^++Zd6DfrFz<NHO|i3pg1;b`zdgUzSqnG('
    '^)?ZeFV=DFV2!1Iykt8pIj;y&(p5Hi$AhNR<0d;ICwAQzOI!c)~EnKOW%nHQq~xoY23ErCV9-?W37Hyx{3fD(v<+r`91pQi3eK0F^>IT+|;Cma7|'
    'XVR2v%l6yQPrd!rt+=WaB*)89fruI9`o<BXy_l-01>td>K`Syho446zKMU;Qf{Y2%xn%%WaUE@DbWy_IVHQ<Jx=BLJT{$o0NhYdX2D%^&_;QiN!I'
    '?~Mg_G8mjE%pheg%P&x~0o4Vf@$%K&O$f@sZN$vvD_Hlb<0?Do}f9(lt!_N&v~S@Xn9X{U;!_T!y3DO$r0L2|&~*orZhM`;WL~B39YSzL%FPN8LD'
    '0%ruCmj>41z3nCoT-{^;o9pJmpRl2qlZ}l4L=>(l}jCz5(h!zt3)VwpGaxP!W4)2Re97Z~w)0V_M_K6FHrv1*Lk+f-%ifWKt^(oO=sG~<BZ}v{|!'
    '|JhXe0`Rf8c-hYc+idl+1KpOF69m-`@=Z$@~2EfCqoE|;2C<gOi#}I=gFXhS7lcb6u2U%PfbS-Ti3wlUq=;NhO5*ExbIostOAETi}SsFYMlgu>sq'
    'LpNg~lr;0h`>JwYh4obYp<sQX81_pvC{VX4uWmFGE;+8Gpo$*o>H5c%l*X&#A1Z-8!3kpz>f^xO6KvJ}s>7FJojkNx^AG=n_VoE50_U?OZew(^io'
    'Y#tgY*eJdj#}<2<s1L-3b-K@B%5BM*76mzC1c}nvh+L7*Zn`^65oRx=skI{DP3;fKYe&&bJNM5!J~e`)Mw052nsa<SlM=h9zj$TwabbHI2nox;Kn'
    'I`}dZ-3DRra?p4(C$aNuJj853IoWN^g9*5BWxT8=Tu5GlpkddPiH6J(8TYg5k5wut4c>Lt<_qU;i0vJFjl{1J`*|Sl<Y>i{OYDRZxFZevFSt&MGT'
    '=+-%MGpMSDH*LoQnIJt6XvvoUImL}(Fz-^ztge70Z$>sh0-O-Q98e}i}Ew?K%p%Yg?vggi>jlOk!c3Yl<=7J>CSm%P-9)XWo_nfxkCZv43up)GMs'
    'sF>?rdv>b3XEMzJf-xYnD@ANQ~-g-eymS$W@P^?6Khb0Gy+GF2S-EId6g<cyK9C_wIj8Ic{bu*zyDM&p+-KY1eQ4(-;(%TTc3>dXK7_#lJV4$i<0'
    'nOiF(*)<_usHyj@k#GI4yC2ujBFO;B_EL%3+86iF>9LS7nuX+CdYL#m==__}eU`(YYj@;~KnA(^~~K#}E`{}eFuR9$X)nS7K!@ORFsXvAX;y@i^j'
    '?R)=rQjVFp^*UG9&e^s0nr73TyL-WdREv}1KN#Di<ld&CWSu^)Qz$gB5wt{^LBB$-_Y$89?|QV6x6~u!(2^0Q76R@zuJ8<ue^4gV^H0fXME_Z3aj'
    '2V%N^bk*&9?oYD`2z<$q+8rKZd)4M)jrjL_BZ4gHbfC0qQj~LZxuY)!j{9j|Kh|JJxVHWfg4o8+FD>de=4=jZMlLtO5`F{;adWt>C)?Vhh;P6g&*'
    'X@%0LiJknvlsAXvsXlkMaE|QmPo!y>*#_g2NuaG5gUl>$&HbCTZ72jC&7;4vaz9J)C(<O1VLyy+j4G8mhos&kt!(L!!)7u=Sy^{QsOpg_MgtmHEi'
    '<3nj5B<JgjTt<bCMB*<lSQqx!!BEON_9a43<&kQ44U*?el@)o0Jkd;l3&iqhAn|~;IkKnqo*P!_Qu+rQ9ly$+!2Wcm#r${rSE7)p<y!KRM(28`~+'
    'yoRC;PN7si4zUFes9(~QlS0`pa=K*p2ST-_bl^qOOvJ)VR#OxvG#yx1+C5(h@uPw#cr%?9JHk+8)rW5Y7J@G2V*kAI8iS8Ev*Yz3q6m;j^l{_e>%'
    'Bk|Bw>9AH7$PC&v(i*dNj9i7gG{V+=8K<wY?suJMV1GQ-xYk{)Mgvo)I`gkwvvC2J`bJxCLmHD$CSlD1hv8(f?WOQ&&2r$ZJ%zjXIjk}Pc&36lag'
    ';N69<AF(x6k&48S@6_c0ccDiBcwvNKmkXPb->!s@zCH`N{Safa4W)*A?8u165{rzxxZf?U{FEQ|9P$Y!fl6SMI<L?ag9kvoezy7fAz6TX;AX^vq?'
    'gg#ZNpeQQc5lt@m&zhqxs;suc+P#s*1ege|@`M}myXyyQ*Gq{*&-A{o5yoQgWMX5I$Rx^BESHnF%$}2rZcp&<5+%ZE(|J=gaz|{qo@qfQ-TO~2J2'
    'Hn~r5Ms)HEA39$P+S1|+C5$rhsHH^@}qu$dZ8%P5U@zWlDg~^g@|H^R@&wyp~w7^W{8!c4VFo1efP}l(%y0~ou5Oe^eqWMY+-z1<GYlQV9$8%YFr'
    'Yh0u9~hyGBH1A!ULyj4A7N(}(Q;=kIR$r^L`rsPvO6$|QIE9DTm){?rs@ZQb3;3CVnTZ3z&{q#5oaOx-7Q|3ajU1Vbz9F+DL&kcy=k=to(jBvNx2'
    '7;vUD_|21*(;KlP$V-=)=4S#(W8rw8`@H3`W6H4oEb}Oi<+&3fV|zY`4gb;8l~@TNA`<4O){yiq!vU2@)=c0HZ};OWY^;>}ar{kEwWOMinD7dZhh'
    '@<+SQYbYQPQ@nqaXht=3V^!@ii->zMtmVGPM!i?}}D6`#I!iN$b*30BhueCWs*;IpGp!x-r*6twg1qZ#?mlC%8fUjAoViMkszv<t&*#Es2;2Q_}l'
    '9r#<Ctcn&mt{EuTimpR@ReZ1f3n#?`LRPPw9+919JfvpCMYq>$^UJ8a!P88RoYqhnn$cz0Cv~?{b%=CakGy=_Sn5%ts6vJs7S!uM7t1H~m)%Gm3g'
    '43Ud{M<jA4?Ua_4wAV)u-!=(rNi=eyTm9reV~#!+@VNZwb>ZB#s^myNpgH~5uQ4?1argj8_ZW$To;rY6;yhvcfteFzj}k#EQwH3GM(@H^Jo&p`Bx'
    '102zjCp>-6xMpcq2xN)J@7>}AmH<d;>Fzx?}E%4;9^m;jQXA!O?hXqcToZ{rl%80LPW$t#bFbJF|2GD%`;zJWIo<^H$Cv-^$Tknm#(W-K}KKc~JJ'
    'Sz49;rqgOBZ1XUd!}wFg2KvfjbDY@f!(Sp~w-t>C6=H;l(1Sc46Up*xpW|rAsqSyFMo%@9#%FOd>BOJUD#yzs$n!TkZWD`9ZyO!S;!@|hOAzcwiu'
    '$w099c(rXPVe|1|cgy{?A`^BlGKRP~^9A?<d(Tl#{Oe*s-_Il1hO}L@7VyWUN{p&-^yP)DLXN_;R&wFwu`Vgd8RXB`x7?o{3oT-5Nw0<l!U)ZN;)'
    'Xg?}80VVG{9BHaaEr@NFNpNZ2}p8WfnFr4!y6Nz@6+!b*mduCi-zJo1lKO<|GLI+W<Ta0cKjdMJf8;PK<#D8dnM7s$t8g8VI2#&5(&s8j1!u3l{&'
    'k1@5sXot0X2w*1^$F*^#Hitav)%dC@4JW3wQ3HcBJD~t6<*;F@cG|enhQGx0Lv=!hU0Kpsml}{jYJ(oPaIqr?-Vj1d*ijaH4xUCQ|%%g6S%J9U;n'
    '-##pJzo1!pdo^Eju0gu?{{K=6)kD8B<<i~i}VqLK((=S|oh@`p7FNdxwLR0c*q(jHUn5!a8`(2Pa=%`ay74czgp&Cz}xW8HV*w$8tcECq^Uw#>Mf'
    '7pE8ycdy^(yy8R(uC(VQ$gt>Z2B;`aF~?pioq}1%`H~Auh!^m(X0KghZ|EsKF1Sce63(Vkca*fH>$B1O65!$+9?R!DFFROqqiMQLP+OD|(QzUNx@'
    'cts6Y%j{Cf$;3?5Cu2&D2SAXR`&6b1>og--@3}Po>M$DTMFyh2PTL?vz`brX{_I0nV~FF?rwnvqlvKBs>kdgS^k(Ut+U8G~9Mh7JH%zC?Wc|BD+J'
    'Y4~dou{>Lux&XSR)$2%vYNk-23|757VR%Bva3_M?vK2GUm+Nk<Azg@mbyT-HedA+;CsLRbWi}gG!V$HZl`$+#sq`k@0kb$gDNKiHd30`1=P8}Dwy'
    '8R4XoG>C?aUu)Z@~|*xPBegoSB4fjQ*yJeZbtY^MAK!<5%7FJ%5v7VP8N_OX3&G4cI$P<Je24+ZVn#HmZ>Iy%em!cfo?g0kCM&qc}0M)J<C?3nao'
    '}KR(A&{6xm6{<Zg*3X3A@pLWtvs+m_e&bGj7J3)%LYB|=CZoa#1%TE;&p(cB1=4nFz4D#BRgh;VbS75af#tHnXOHGUf+W^B26meYvaLqJ@#xr_FR'
    'K($gC>tp?*v}r7j7yY|PnFBNu$%59z+p6U|FZi7(@92Cn3-GTMp=^NDDezmACyEpUwF}`{Hp-sr_>rJ^W+Oj4gQ0@PCrP`J3ebM30sPZAU+Z<Oe0'
    '(y?=b3b6@abbIA{kb6l26xXJ_(>GN!uwkQK%~1nQC`wGaM$Q+!pZ=Y}#qQGL<IhJ2Sv(l2&uDKBjHhxTh|!;idzzU_j8T!3=%2^HQs{UVr7$AZzh'
    'pJxJ1YsMO^opg(znevd2w=*Xr8klnY7EW+dduhBDFaGGCcws+}Fu_mr%x?S#+46e|2R*BZ*3iYnFQ0ntg?S6L1LYCyjVoMvf=y)8p{7WMa$to`C)'
    'qVZD!122+PR}v77=@CUhw)%2cklo6SBZGlmuo%#YVxB18m_kGDg^ZN8kekOSM8Lb?Yp{m#=l@w=E<cgF9p6ZA1y2~x(P>RY3Oz;+YDJ$cq-mXun8'
    '848AIL$(6Pk@cOGtWxev{av_V-lyhlQ`|Cb&*S6TL)39-x!-RUHtyCG6HzVT#%D|`GFN+d4nL+SRzGZ_)vGYQvl)F4|Gb7n|PkfY#48Q9BYzStYd'
    'v)`k<1n8Tw2|n3Ybd?C>*ap_`TRd~;hG!_+mawy>{K%$dqAKnQQ$2xu!iZeeFXI?VYr94Ex=B2G{%WKmKyl2Cs__BN9?zExfCiM_-#98gCjgcj6a'
    '3T?GzHD5QIYljuCbUZR>WUA6#AT_JxrIy_4>Wb;v`6Zy76tfbH=Wt{`E%)Y+xqL0X>l6)R0+N@`eO$2I<U(bks~o>}acTqru^^d@s!sX4I{Sf<NE'
    '%HK#I5nsPfR?ZyPa;><nZG@8_INN?Vby?_@Bp`I8G6aobsyXVm3cwu80!@XH;%YR~)ewn4!t-9ZSE%rnY-*Np*)yvzXrDhoNSMqqxp$xsbw{h)wI'
    'QTRKG9jjsR*qe^EgK-hxV-tCL8M?7wvBrxtrt?}L@RpET$>n@=YzA%h;!5`4uc;*1vQCW$}=F;?cvNP7YT5){MEN!b1)N7k^%`UKs=oU6L%!D4sT'
    '<506Q3>S~{U9stgAi0CCgbYIig$jjlbfwYtezN{vBP24ZG%v`+6Q0_TQqs+94rpY9tA-QV}4Jc~|SlQg}Vsh;Eq$)|XT7dFZZCT<b(9yF>1$6{br'
    'K5WYve-;Tx0%xXcNaeGKMMi-mDPRsS+>H@rj+3}1*D|jRzLoR;!#}seLW}5Ze_=O_=;Vt5myYL+I@ye#aNY+AJvkoI*&Zw!#@%{)1IL$58E2K_MU'
    's0pF4OLg!O@n2HH&{RQt+T8M5PI)rVu|H@b)YNgC<Lu`gf^H2`w8Wyh8h&LqnNtC%eOfpiW)Bac)maq47A<29<P53mCU*pCg%5xh2#E+>NBzO_2<'
    'fu4xS?i~1DofWe&<Vi;h}8fe~aP48bn%GGtoi<2+SF7?)I8K1dq3nO;qlOZk99r`8chf})h&mT-Rv`8#6ZJeg8qmH#FWv_GrP^OD|qdGG_*nHnaT'
    'sC^O{J2n_R~l155>agD_e1n@+pm9{pE$Qj^EB-3%0{V=d9Ek&I*QWb#zh+Nb6wx>iH>Yiosr|@_jA9OvisI7-NZoO1=wPZ5gsfgW6rnk5oeLUKu|'
    'qipEtWm+oa^n+FeH>ujl9~>QX22jxtajH+OV*d5OCJNsqE2SE@bH$ZR>kF`{u5>Y9yvst=Y__G!c2`DE=inG%8H{ChgaG;OH^|H5vrnQ1#Em|J60'
    '4m_L|8Z%gq-L@qd8+N7Rw;)lX87+v%QDk3a4r~7@kd%8}fXDT3yCqoU1amGyIinM5w<zM}Fq;;c=DFnIFnyM&u1Ml&EJ@RJU(;s%E9K^1BlrZlH5'
    ')W>a%A=j{YM3I+g>$gnf(wB$ENH=ewm-oVy_`ENrG<dVm)8N=N|vyGS1e3o4TtphtL1>F)A#|W4VEK2~o^ch-F5@@YFbmqo$`&aEp`JJWlU&PL3&'
    'kF&d%yej;p2z^jG?!|Hw|8NTyT+9BDYfxRE-7{Df-^ymR155}$|g!_Kazl$u?R^N%uS)M5Zs5^6^6v!<XoABE`HW1Z;YqTl#Bf_=MLA(T(zj|->c'
    '&C~&YgItxaAGHJQU{})Q>_Hafn)!7>g#Wi`18|_Z6-9tXQ4&ga@$DddfASaC)!hHW52Pv`M5Mm1P80(!MZZ*pq9DDkH^ak^_>3(S#WmSn_Fp{0Uq'
    '+%7}`C+E=y?bKp|&B)-}epu@2l6Q5HzUcbQJ^v{w5}F37D^9kfiN4u{VHfpT&f{w9KUVjH)iXR|X?PK~FjAfz)ybct;!IoWLK2Q+$WVH#)u6n_l$'
    'vrvw|1-A(_+Ury5`JBu5i+6let&f*Xs|<Ka)8B}O^thxE*~nVjy1GV*W6xFOu2Ju|X~uM<&8KrMbXIQ^UIH3@$+Eomo05_2OT^xsUij?mIscS1?+'
    'DxT_hxwgyLesX3}Zy&svwvt89ZsPZdTz7WZT>jYpm8Qz0NoQPc2zNyIPNZ<-7bFykJ_$vFyHqqYGz|3DNoOP2)!h)<M}%U?Jz18O+V>vqF32cyp+'
    'Hp7_t(gw&1Y5V~;#H->E}49((M3REz0=~S0^hg_adRy@kjq?&I{ge%f|BdK843qxgSe>SsH9Htzm!AAqKjc0LPKjm6ih^4sHr{{2=Q|R$3t;G#8k'
    '&HJ%8Wd~0pnH)XftDwM#Pom!IGLQ_#t8m4_HC-R#=-SV{R*_9R1*gPetg000sd82y=Of&@FuF-ze>4#M%@|*ie@VT+?e-=$yWnfrZpU~(Z`yeanz'
    'DD(1l3cXE)!wF<q1eN}PfYqGtfKY0Bg4;YA6_9iQb>PNATyOo!kg<7AG|tdHkg_$*N+1Wx7bVSYJuXJtij-?zyvaRzeB2pUjmDCnqTe|r}|yT)gC'
    'C%EUX*^8~*AfO$qridZ+xy^+K%1z}zd=lSu84FS<n8<DY_<o8=a2x_-hh1|boefV&@3if3v_X`e%$Q5IIw6s7LCn7?hl|8``Vf6jk<O?$m(tYv%q'
    'Am!)gZvh@;nGM((|DnD)Vl<8sn3t6aL#$xhDr)f3hi{9QpBA9v6!7b43IS8W0BN(-5DlpxMj&RVu!rmA5=3f&O3kKJEuvN#@6>jUmpFP}o{1K<WB'
    '5E2!fT)_)l0Qj3@b776=V<&1LF2y4eWdb@`ZL;WDK#*k#Kzvt(kFjM<KodP8ZIY=zqB8?=X-EE{03fM!ijZ_`QxB8-SA9e4G_jE~52mX>R%l%ZZ-'
    'k$|NGX;Y-&&SByRxX_g2=#;gmk3*_hS6_;#MiZ#gNtuXihx{ZgLGTH$^4e$n8&a8BXvdL;LT7$?9mz23*2PBU01wQ%Y(`Y#%<r<&l(Fo0hl_3Yo8'
    's0{xJ=nyXt=p530@k(z@8E2uuXA>_s6QWhGs`FSn)_pr|xGiI`*}dil{IaG;^G7Oi4{i>)NbdcfmHE%`IXH+A2i=P?-gW=R^-?tWs+eo8Zv?97G!'
    'Ov!>S;;6$XH&ZaLp$sjogrjFRSctX{de}=&;?4rb5OTJwFSWbb4yl4DW5w>}H5z*t4$6WGaA^O3)>tB@?bAe%{HPifqNon~oL7;4l;UtfO1oF17Q'
    'I<WLaej#kdrnRnJfZl=|16!D#RWd&2p&7tLg2(6qa}1<{0fZycXl)&k|>v<3V1@n6npn?lE`q1!&U()RG+k@2?mg-T~?}QANv|^`6)~H7Tu_{DHS'
    'd*=59MPN*Zd7$fXgQ@FIcdpCXh?;p-YplYKSqtDOQ7;77$<hSsd+dxm(Q2s%=^1}PpsEkiag11aE$eb|r^eZ-J^;yn2-{Lqxsp0k<96!X-c)x;|Q'
    '2MNqeHY^k3*I?Du9a#dZ9{(dkEZ>esBN;9U+r=_EBAA5k2QrT>V(MJpM1=U=JMDk(w&7y64Ex6-^p?fNq7cQJV4teM=P)c(4U7$&KIiJrUW{C`D-'
    '?LS>!cyuI>9(7b+qtT-P2cchP_|3}T&1hHd-Tk{oAcN^M|jvnKMir(T7!eVKstE%UD7DnY@`;>vIGmXWE#f21yUl|{1_;D~OgZ+{^dJk(vE@4Ad2'
    'Q(i`1p%a(9<QcHVAg39y(+S@-cdw#^)a!1J{~Zay$8(k`gyYgGWiB8e1FyZ=R-o8iKMBAM#idz4V9Q70ZTk#&C09Sq1~m}Z@8(lX5Var)^7RibAM'
    '$3r3@LfWhrjwHp{+c6WgC&=C&pvIq31Vl*0$%gljk@#qZe3zyb%PdWHkzp6%1OI*zXnCknfU>buARs{-odi{Uj9>fpw4Y5H^=;_aWh9Cy4<%=k2B'
    'efu+<{>`=zt%_6xppalM-`Fm=B$it17a=GtnU*#Q-E&hhMfipf#_%#XCg^GS4(y<e+pCy(o;)9>wQUyh<ag+j+)<gd5C?Vb;k|Nm`SoPVEB1bf|R'
    '-<07MOGOM6KMa`Jro=T*U6^WG~+gE1%u1_`C`e<YMnq<-28YVu!p{%<k>PeAV2`+vKz0G($vJ*UjWqR=BYB1&YXq<gi}K(UQI(rl>T-qdF`)uR0E'
    '}Die`~8SGq+%%QD>|uI$p|oK=XcSgzb|sS%b>k^BF`WXXB%(n8jmf(A@=brCwuo8MNs<j8Vs-Ry~{!$zZion5GzKQKPKV%W*TdYPnhjF-a200_O<'
    'J^LapEw#Xs=pHl_BmByHuzX*fH&A`4K`N$gsm|3E*;2q5hgKk*49@8(pAuI8>v9U0>z6x8ElWpTjK$Hs&<XE7!bV9BM>DpaLx&_C!0$mz8}I3XlR'
    'ruaqeLcyzblNrjqUSW{Un(Z>MdP$sg2?wo^IJ*$j4NydALsE*lG$%<OCW8e>btd?6GQjP8=(yX=n!07VgA)ELo<*al6;r#CzfA7<z53*_}SCv{B9'
    '+R;WKWQ5Cdu$TSq$n+c3^fmY^Vg-T|rwIRO{b9a$0eZ0LoC9$X>AvJa*FK41&?;8@ZXrth}MVguxbe0lV++Fg30sr&@o*8?~gs2uDf1LG%2WJid!'
    'J6UP59({KVt6|lQZOn1UCB3hGiE4;2D1VgHP7_-*FpiFSo`GB_}%xDwe2Z$G=|Cy#dgP~S$5Cd{ukXj*JuwS$?SnW6WqJh+%o`3SQ&j;4`kWzhuh'
    'isnD?3YL!LtVOEcZcvO_72Hin$8qg<XditjkxF_Rs*u6Gm=X{~WEe>{r6vmtNGUf0hO?LQ9RU#-=rW4!<(28t6ITux)J^)8Ld&`5*9_B;AX8#|H)'
    'DP4c3pT?G`jjj8@8kx=AnHSKDEQV+tqx>eS3YQBHw-^D+%H^DSSf4ej(ME&y3OL)Hd_2Mkm8&Zl8If+~tqX9<Qal5RR1vL3(JRK4)MuNP`PnaZkl'
    'I%H!`4XnNDs1jlEzbmU#wA#|EHQkaBd*4Q6#hAv%-632dR&qrU-1c53dnkjJ5_%k`{n#hnj863^9>_0H1wh>oY-0M`z>%b(>9%#JlV5Hg7_C?f{('
    'Wt>yL(j;<ogte(MJdHz{ni16`}%II_uiJ9~rdBJ5ykMGp<N{#E_y}HYer~StZ#!I}<G~_a^#!I2ddx}mmt10v_R^6K*n<-w)*5C#5m2&u)7%~Ui`'
    'Aih@_hoUNIk+Cpz40vwt!()8B**}KU55?3k5TJ6V|X!#|25O($5ffEXBYn4ggEJy_RqMD{}h*^A7qAnb`;duU-hMQkCxduqev{*E56|8qcr$JV%H'
    'XVu{gulg?@jsb6|8Oj_bikFYtBDJJwzBNoN^jaSqH=h$H{X(I{bb0ZNIM%dyeD5}fCjq7U1!%3Lz_f4WW{+!*iqT_aZbO0C6-=c@=*pb<}g6`Er?'
    't9**(yPi}%#skgXm9qK-j6k~jnyJi1r!Qf98>0|{DKcS1XkE5U7n>9a{L-geIN^Z_5vAy7fk5G1)L7@}>6KW;B;hEG`@UXl1ai2jU59K7E&N|H#P'
    'RHJ`WRXa-k3epqDkbHLUwGj%;Uo1cRvk>z}CP0_HpfE?u_O$JnK-fS}uPTHO~Zv>*we=+$u&FW&24CMw2tyCI%tb%u}VE!CILCL;9dE*Y1LNQ62c'
    '2yPuV2yyjk#UxVu1;7n{8`?#N_Fx`CaLyCb&lm2ioj&J|0(R)31Ru7t-lQkPbO*?4}LO$%UYDdUw5>PIAWLCaF)BI7SIxeq!nI`Zz)C%%DdG6#&s'
    'pZ$V=m%$$igy%?lr!`@%HHu0hVwfJRR0&UJr{}oe?YnlWG1}SWcrC$B)GT8J%m?dS3`qFEw|3{OmueZyD34u4M#QLsDE`*J*j#BdbSrYe*P{jZ7A'
    'KDSY(V#3S<TVW)15)1OT5!hBVN45h_}^<X-Z`F2Mc%S5Mr!_L5msaO_itn_B~j>BJJa_L+@KV}va9@J5akJ3b~cFOVE1wZ=Nn-^t9kDIXKCaWk<b'
    '>Mz~b=a88+17la7sF%Cw*;1X+x`Iu}%6tOKCFe3JCJX(kXuY)j_9y^XUVG8tl>N`NjKqa7B_~yVytY4Q+FeNTMWXi9G9W8SJi{DzhyE<l=153x27'
    'YuII<k!S9Ue{F#_{kd%ESpTM;NZ`;g>hfwQ+il&LVn73!I(>ztto@yvAD6kRxh+OHnYH6*aRVofA{8^H!ulKMU0QtnuudzP0^zvP^n%Z@ZE6>)(6'
    'm>eH_;RO9a=XG?{IOEiXAp-6`xeLJdehBJ(lG`%t@VAJXp8r>b`2d{<Z;r@PBh5A1#6gC)p_INb*YA0$E_a8UT!z)72a1(BgSuy(pT->cEQe&tBV'
    'u>Oo%kIQ28lQJimc`w5)lK)^5{)|!wk9ZVi9AT<u;kJn&gJ{nqW*KKRe^#j=EuypV=%<%uyk=LubeAERz}!rL<%1v%_McHfcJ5Ik}wc!qJdS%6;s'
    'Npa@C_2xFS*=7(*Wtke+Jg($&|i`c$&6sOVm+p2Q83$VSq|PTgH7CRn6uTUPswEG^ri3z*0V6Ns}|mvXh#D+i`8kA@+!v)ToA->YX?m&X28<e7;h'
    '96-E}Lapf6lE0jvWxnN4;GPk8&#iCBJ9=fzJ>R-#?3hPfAGFo=?3k-VyKIqvdU40+SmZVPt@6vcD>RHyl9O9Xhd%ndmotLF@&r0dqYDWO7x1^O{C'
    '2T&aXV}d<<BJkB8bvsz#6^WgA@<cR<rvcm%<dP0miu(p|J?|FcJK{;z3e{ND(uQ*rq4s$?7@9Em>I<msjvQ`Sz)y4b}fnS>Hx(<G)eOM0h$Y?#K_'
    '!>nT#kXG?!V%nz(C3cw%C#a1fLj<_&RrQYZ1TMFZ^v-@T`1T$UpX`Hpl42X<1WFHHLZ^TJ~?B<2qPlfndr7BGM|J%rYEe?SqbY++x|K_+0v-pY#a'
    'mu3XXo+6Tiwg*(L@1peow4bn$JitNMu}UpJ#xQqfO=%?Y}>kbeC#X7!IqYH9NO+XOUe{5-On<s#jd6nNO}a7<8~z-x@+~3bjg*7fb2I55kX*3vze'
    '8_b3&rE_lZ1QM<Dfe8$CKih-&yn^FZo$dPsv5S$*EkaX15Z1vS<yzWz$N-fpS%2Bk2K&SWm?Q7JP&fZL33;7nI{mP>-LjvKn`(cyawA842JFvkoy'
    'yb+uZN1)rx&b$6J4Cz_F|JsxCdzY%SX910^DkVT1{q|X-{YQKFH_S7{cVKd!4M~AVO`&CKxlHQ|R=zp#=@v(`=LI(->2N|y#8E}QCEs!^^yzxxh-'
    'b&I9yv_&(xVD#0}n39XHDWG#FZ`Jct5MG7}Fxl-SilMmFs=xoWU2y#TZ}PH6>5u5qz$u%s-z0v3;1EkKgcSoFP^%*N7rjURd{QwDO5vF9*5)29a;'
    '56d@1xSEEH?cUNTE{r<e+GmdHNUhd>80oD#!;Kn7*4FaHLj$iUuQEl%S2SboBd4(^flxuu<Rt2L|ys{{09sAxm>lld8f9|aVpL__;@xi+7HYgU!H'
    'ont!j}!c}PMakKuA|+&@{QktqgZs^7M5uY7RHzUCghOi`NT9yB=ak@X$eaXD^dqV|5VeP)pNd7u(7O#m<iij*|XoJwMt^rK~cM3ouGsbe5=Rb((I'
    'l0Rs!U#pMt!`FEblog-5<b*{%jV^KEnhtmClo*Br5@sRdulaZS%W_MRt}{9X{&v})JDbq99#n>Yu2A+uU5>Rj@uzVyT}LO!cJYor7lrf)DBX~?@2'
    '%%wti0X+a&F-4dYT~C(=9gos9N5x4t)qrM=WJUw%+E#;njEcr~Pg?neko+k}JeA}&1<!N#O?*{K%vzkqXPwG56iCKl&26)YA8rLHB4?YhMjBoAo+'
    'j|A@=c|0o#V_}n#lz=7g~U(oqkHk?MJZePLW7}TSTyHm%n+tGqK<|e0}o%uSvm1i|@0>yEQ6p03-T6_^kAsG+}g;PR-yNh-2EJ&H%I`E@fm!EWHK'
    'NbGTV0G52BENDT7-Ji3%#cNut4h@3R!mHIUT%A)_dVIx*(bA3O?`ciVXT$7y;;}OrXW#mn(X2>oWbTcpC75Q%tpQ3jHSpk+&JIo19LZNY$+G~^Ov'
    '{FLbn2@qpw?ctoVhSKS&_(GEOatF=N8@h%Sz*z2{H|U-zvQ@wPZl5=nDASZwjZIMHxCbAt(|y0d_^$OonCv13w6S?A4MJ19S9;w4ao^YO;u_xA1;'
    'Hf+bs6i6DstasD8YV>+>mg>muv^WkO(3C_)#&;Wt?e%ANDs2o*TN8#6u(CJ88#jU(2F_}FnHb<ut^DA<^US(QTikO$jP*Xn%#ZER)lOgLfVyog&3'
    '=dJ{F%Vz^JGjum}TORMMuaX^L<r%%Fo(BfZJ7dy!WPHkgMSvBT7XkLb*K+b0-%-#fy^(CDuA-Wwe$88ZqFCb3yD-WXI;R`M=#ebWm-YFw%?#Oq3#'
    'D~+=lm{9y;PHzUCu=7f{-4MtS|7GBH}w;&a0S_W8X1IQey~n`zEAJjg5^Xx+m5cL`^l`O;?d!do6LSN9`*3q~7BC{SXyGq%XgD92Sj)r^NH{m)Tb'
    'g)qkjf7iM{p3LJ5=gH^|OqEX2`&MBQkx)@XSds_3rLtgqg9fM=)kt3pUV`Tr4660Q8F!DmMxPL!Jn<J$l)CRYS{zi=o%7C5J&qQi$;tQ3Wa$})Rd'
    'fTyupODIJoh)Yr@*@Np!Li~*lXMhQGn<&B@QL5!sV-vp4k*O5wHHk0^wH>8KlNG{F^|RXQ5KO8Pgz1^Vcp5jE$I@7?#Y@0=(nqYCV6%(KdvSzio{'
    'Vifk)Ck92^5Ma0l8s_~i<wjxAA^9GRdKglw<y(QmD0SlDkD436(Z%&NpX{5=PlN?AV~b^X`<(|A7+*hp;MV*Y5OiGlC7Qm)ZeCwGe{q}xXvrSM-T'
    'gpkw~(?ipGHxK189pBveGH4{1KenHBnYTq21#tj|_j}|b5`WY+ciJEogqGe<c1e9aSITJ)c*RLN_WGh_e3&OPhI6rN_FbkFZvln3Hr)7GnM2;vcx'
    'e5B7hVyDZD0u0`)83`k%}8=&8dS%MBq&)mEk4cTKTMej;<6Qe<2?ya+BH21%-615y!}7#E7gN`x?H$k@lf$%9=z3j-MFjbvy-P4(@vPCNIPh#KiY'
    '!iFU^z7@(*}x{H`-?FLM+7mmVF@W7FFBx<`i)&$txved7=u19n%4bcZzdU8J?UZ1Gs06F%W#74jHbqoSqxpS;w;I?NE=>Dgqu*$M2X~K0d#%t>w`'
    'I!+?a=2DQZ}JVAwXx6dp@aEX5>Fh-9DR(F?8CtefoWv)&hSO^Wc><Fb490(OykVXJM#|CDV;N}yJ;ZT_p`#nDaWG&SuZar+*}b8R2xejZzOQTIzy'
    'f*tP_7j$>2nInEw8eKP8il59?wM`|0f&o0S7zlM3*FgRKxBagh@4#>6byu_+$C^!r`jQ|=@A)#L;F-dz89trby5@e5tDx9u1EgrL7GA{B=mmq3}<'
    'zZDXr7fXPoH+d3M42d`re%j~d@9sn^_-?ix03%p2SZ|ls^RvizE;3Sww03B(p@~yCcs2<DgsI;@Srw_0BrzAisI800jH4%|y?aR>mm?vU?iDdr%x'
    'dkGrTyqp(C;?3$r%|3c%a}ls{`%oI8#51jLtsJ4-fSu(!PfF{}HyZ{5oV{EEF$@iJpD$ix(bs>1?bcb0O=W^IFp8bUBz(oYV5m^y)=3srR$Ut$uY'
    '$HvUb$BH!kC**QcHru)wuv(A*ta)F$^uhE2h`4=^2USwT|M}gp*W3^C=tymtWrP1_fd$lD}xo?zm#c__qgzFy=DL||cO?l(ayP}k&#Z$+cr?@z*-'
    '*37(92KmZ>?O5(>QuWl-Gn)#U%D5i252soalEl483Hq(@{t@kJ5$iS#=x|Mmgeqa;t>?0{-+VRXsa~tFZa}!rj!@TKPd3WUa{}T2a#-%XW#TBS6!'
    'vJr+gr41&li-X^c-$x$ql_0+T0pKL6y~S+=kLpo0!&Ob^T|nHkz854tM(uqL`s(RlE;yao)pm-L3^g4=losQfb$wm?Y>OahAEy2E4B7%?6wGH1b;'
    'E1}>|WE?4|kj;FSGDc<7Rgal>2SOM24WNK>w4h^q$#Djq!GemtEN1YkDNJ31@oE0{?u@^&cqP7HbSa+%JbYNtFGDo(Gcz3)iExCmW7qgGE`FMt$!'
    'i|3WBb{Gr6z6gU+ktF<9Khu3)iyFReR$N4}2-vPAbz=tUN&#^OOlLF*`Xi9p~{mq-SWa$%Pb9e5iLmK&JqsD(~jGMM1eA0CEz@B#mh|e`>k8K9qc'
    'f{9yDCqq}SQY`8)kyxK*Fv<7V%TVa4!;pYCuf^w(wV)-M3vqsg9Uwga@M#2y`J~N6mhN&$%)mUNTO^ER~SO={|WA#HsfR0ahL6REk=%d~Ksj%^8b'
    'n_Ka<5E+M;eOR_(LKE3qOuIH_z0ilrJEq%9pM2^nJ?CFMDFDzcKgtiy_yFfh6Z}%ljKRbrqobb6i|V#XuvbR;n9yWP4dU=$>LCO=!x;u2GRH_rsR'
    '@dGkOUb&TdBb_}#m^3XU(vrP@)ZlF7aQOE0?x{Lef*eh8n-XzYy*1YCTTkZ|)w5;<%7Z*Y^p;@q`3#;?20K!|7dm8+50LNbIcVA}@8A-PsIY42FV'
    'wSHG<W9(J1Yl@cGGuSSHIyf}OckT2XpJYHEOdOd)Qx(ZyxRtky0f3lQvYFw;UeI-)?ut5pMkatvZV~__@N3*XzN?d-|G#oW&L&mLj`jF1RK26y`D'
    '>EqF_GvoJ^~liaqftyR;iKHjxSIWofm13eB3<YCBmcIc*0+~9d3sEJnha6EgXq{Tse#8l__NWQ1|W~%SJm;5A<ku>3#b<^s-9wQ6~a(I506rdd<!'
    'il6*@VA2mSf=&jKB0g7E}{~?z{T*!_dN5Gmk@HM_3RTI5{1M|!TL}z6qH{$dY-5n5QrSbO8ZBo=0SqcD>*WYD!;i7T)C4Z3_aKk>8qNQwpw61j;$'
    'VJ>fp{mpn0dCFvi&c2Sv2D#OP&56{CW`=4ztWH{qlmIKU05jpjkt7|-b}5!M*bKm+voiz%;aK-e{T*VFQAxP<s5&HrZ>VXf#z!WBakDq9H?{Ar-V'
    '*R_vSY~_t|8~p5u{Y6-sDy$0X(CJUPRD%LjM0aMGxz7+JUO;`iS-XZ<(zf~68Zed^LDN=<krYuuCM>U*h3<Wf&{ms-UeiP_(MT|T=NipSdFE%akX'
    '$s@lDV|u%~>=59HdHl^p=O#Pm8Bew9qVXjR?hlsI3VXM?$oS;eLWbQuFiquTu^{olh!)WoUFAE9$~c0<{l<zjm%Z+~WdKoubDMg->KQo8r?{@QSj'
    's#}=j?;~_xM>zoT#eK{>1&q@nMbUT%*>UL4Uu-SmduHTr-;dYHy1~6*cpOF{mKDi+&vi;xbL`lf>>iq1QO>X}HrrvldxUthl61OR<lB6Lyg1$H^W'
    'ye}KbSZB+K3MP7Z?&^Wr(p4tCTBF#kS_7&uQJdETyJ4~kp(5_N1QqjeDK(kf6XLD+4jRb>F+EFl-v?j(cml=%;g}MWYq|3<cpq_Clz5eU@{U{SI6'
    'zTYOwCK+0Y1~DGNkYE4cRFpH(n!*i9d|Uil!IyHe$4|B8iGz$y*YOWP|AhhLk|l(%Z3E=XKP;@mCK5TH&l%vqkmqRhFy1FTmX&ngq-QUp=6rBOXt'
    'O~_<?3w<uJ-keiCt#9~r)qnmzyjuI?>4f+IMx=w9^QXsh>QG1~t&l;;*mqNEucv!O^J(Os395#fIQNiA9+$iKX5=lnQD0GM5(Mmy^SnsiL<&G=1?'
    '$)Sq0D<PMx2Jhv5Eiq95?Gem$fly(*sGP`dyhsql<LV<APnU))%{_;QO%%nc#ic|_STDI`!3H<yIx=35qQiKMw0(JcgY@9^&r`W}Vd}_W<55k<;j'
    'bmS`;b%!>qrY9g;z+&SA_Y@*W%QnQ(w%n=@hb=4qb=fRCCRx(9P}@TRPD3Vc(HCvyfU+(y;NAxI_PfmEpHV(LNyiN<>Bbq99ajzt*{ShZF>Tgmj3'
    '?5oFWoXeH4n=shvizA*#t*o}zB_?}n3(|ddL|5Ag`spC>5@rjgq#s8u<yJ$=FUZ~%b6rVDSi;o0dpXEkf@9(;z@C>aA+i{o4iq9~1BFOR`%Bs&1-'
    'pSii3Ha2fv8^C7y`pRVT(j+L!9p44pn=!*fSjD^NN%I6^+om9Fj~UvPJvYITZd4*?X|*Q>4}V<$daV5!=hlN{(rN?w<`Z=FFvK~%${XT0&vks!5!'
    '+WMnplPltl)M)?nM+NC7zhz%2#*#d)$8vU>2hu<*`z$~Jk1tfl8`nJJRSL;Xb+Vr+5``w_r9ev6Dc<LAr%82^}qO9rbV<YJT34hrnIJjR}1k)s=u'
    '$8#vmCyFh@d*B+lsa>Dgt}*%4Lb>UC9!AW`1{|ZWMc!pOspj{pkf}SD?ixr7>jSY}k=jhG)x=F#_s8VU_p`f{?b&gucPYjJxuGXUb#$j`WPv3Rvh'
    '$QHy)r|hbR$q*K9%S0QlskieLs0qb2x#ic7C}=?jibl<D=E&1bi8V1brepdt9*RBHRqkMvAo7b&{2aJ8Da5MwksXi@khArWlV#qr_!x8W71ulbaa'
    'GrE2j~zLq#QO}Lwzo3dyq*M~JsDweZMV8@KmjLC?>2B9&3Vciff=#re(5CR+JaVx-p$-GLmKC2h9_^CKWV;;t!jmWGe*UjIU$3$e3WOM(qn^|UJA'
    'C6ccYwg|NVcG=RXJ;GV04qHtAx5T0lB=G(Vz(c9^sh_^T_7*p5AFc&?sj(XwYSUg-pel8k|a5O&xnWKu%=_T)K_E4GgrxLnT@%GkRi~)Zyt%l+(d'
    '~}MHXl=erUf&-Em-IQ{0SKVw186u)ox=X_aO(X&(tH^vN!I`Xj*|+R2pC9KxOOnF4qX&E6@VOHq~ay8JwX&65UA8w=eky9p>9CxH^RP*G|3-{1fn'
    'De=)9V3TSW>h&s?S`{l;0sMfmg4kv$=`35pzBbMzNF;n%HS_@BSQWZ4L-{C#Un{gr7RIG>liRUlznoSAh3M|L6>=ygGR1~4Vp2JfP>s`O{e2!a44'
    '87A#wRaJBVkubH@z!AYC#LdWUdr+rtOXyJH{h?f345<i2fr5fm@%+e3U6?VaGzS4h{SdT7!F8+A$Z6aiW<B(19#PL_IkWU{6a?Ob`*DrIy`g6OG4'
    'ZOctK~PL;Vmsj%UxQtBv+|LlxQqb-KtnuzWba+dceI05XGOv|nA<9ntvKv*j2{uye^!701rj9=fkjjCuTgLXD^$h}36b1Q*0_V5Zl-GudSD~K7Iy'
    's=%lEM7|#t?7zcQ@GstHBK=2gr=RyZk<P!fX!=~;@hIR?xNY{QBoJ4ZHqA=@Oz9_5UzxH-JEwWn2{Z3sA3X90;zT-)1UqzgE%|<Yk}nqJMkfeZB$'
    'fSBN|%ECRj$22PS#CX8MZy9A`szM-s*OMYNGAb`Fe<yx}CNu;jKkhM;Fz)}oqST}~oAx8w1%xuQpRWOujMdws32kYU1JPsyeYsH(ykTCl?opT<!K'
    '9Aya2V>5gEka&rB_RipB*hS^1$~SRCg17%X<6gNLq6<T-9xSIcn{GV>Vjnp>qX45bjpy)w3ZIZ+rseHHZ!O`O&nZ0rQji*mb(zu4&cY9e3%=?%@D'
    'VFI9=Vs<*!Y;^UR~SM?q9!8b5)Say*+daNh^~n*VG~nC6bOwdGkNlYnA3&{b2}d`2w^(&&Vj_xVT%@xW$~vzVu%bWFAh?q3XbhbIm`SB*330_aR`'
    '=3I4FfZ1hSJZfDU$WCR{7S(o^}6r1B|mHToQPjPIo6&h;sft1#hNe`)|Yj_++7M#tfL%U0-l|(@sV0b*YZ-7%4#%(Hf>LQTcseVYC#dh;8eTicru'
    '!M(OA~Ck)wmIezV1=C)hTA)Jy}s!tT4{A_zOehiJ(>3lmX<)GO`yiwfyp?$M9E|>!cY)cLQ}RFRMB=Y%<n868ds0_IbL^xd!7VKq`n%@KcPlI{5_'
    'VK2Vi<K0$yvhCt^spPu#Q)9M&|1+-mdPgmJdSJ*sO%Q1dk-^V?LY%)G0~{=wurmt_=A=Rt#b2ko4|aWVC1tjDS&FN_0XF=yc}mgn#noN0Y!4F{eQ'
    'zI_G*H0RK0bw*Am7S=Z_etCGN$KJmd@ZmKjk)I!BxH<=bXnmYU8%)l`J6tNZCY5V3u$o8m>Kxg;^tF-_-BA?0R_J{;)%zN-TtOc1M?YjjsQKZ<Ff'
    'Afy35v0YHyHUE7*m{LoaLGQ(JW<mBleJhC8BT&$2CtEIF4!>UvKl2zC3HqMW;t@HFgRVfY+YbKnr5rm_yH^J5xKoSyNtremP%raeidf+0&pT)lE7'
    'CA=#ffKZ(L#9MncVHo!K%Ww?(inEbA%O3+$37{7;KH+C;9I+tHJi1!y=cDVbGT|3aw^>`mhs4wavoid-IpgG%-XLZjCmqFf4Y3g1{yfbEQwqAq2l'
    'zMnx%UbKrkXf)h8!Y}Rv7Fh=nf_emOBSc%c%9894KsazO?k(SDRYhp6QN)NhRAl@9&-IS>1sdr+}T@#c5|^ba@(>wou07?LT}H6sY&euLC2}-8+('
    'CMmS|f5%<0$NvZN9T?5`Et63;McYg|F%hEKS9_M|&Aybo1Jd;)#OT=nQ8V<~EOPja&K3s%jYm&5SEUby-R0a(;D;_Jt;;Hf_`c&H;H^BkeygqYpO'
    '-^Ceaam+6*PDA!Sireb!As8xl{^RV66kal-sw6cw7_YZ45zi0(uFj3&Pt+3-B*wWVP>;~H;ePZp<z3Ss={h}`&kL1==iAfqUZTs5WFwq>hAHfGC}'
    '*Pp=q=#iu|6X^uZcS!!0x!g;#6-Sz2>Spsc4^wIdF8VamM!vOGK@MO9J$5-!hrjM;IF|D%+>h?Rui<O^l}3ey!e<1$szoMly!6jeO{+JoK2vs*XF'
    '-DE!@BkiN!m)SsS10MKB=CM}Quz||W{#9mIx#bD*`TW}MW2RfhD=a-S67kJ5QdezEu&$<akme+%*+@oGXAR`PI4=Tlcy>>v*xpU6vc+LBJk-aUZz'
    'hs31JWsynBYx`ZKjnLM*EgwCFMd$J>j;|#FMsJVR&@N&uPeuArX#sOy8f}XW<AVzJ@<oe_ZNKg%b?}1wq1m0DOMy-XDBkeGa(8rFt(K;Jm!O%`Q&'
    '_^D;L;(<)<4T?)>;oY4$Gj4@c{`GrYFx<#~VE&9qt-i5W*2Zj7A;6@dj=B)?5y*rg;QbXEBxkB!1Ae;+{<fmmM}eHL(rXPitSQ&l_gKrf6SsyvUU'
    'G1{)r-#N$N`@2qKH4<`D3E{kufzO|2B8pFmywY(}fXtn4hdYZ;(44><FiyIq&<Rf@%f4aMH9e6ofC;7)cNjRDr?Rl-!e}iiIxS%S_C(*Ve|<65=w'
    'hR7Y%1Y7Cb|zpmFH+Fa@`f@Ln%&vFrM@?#brQF!V2Kdah=)|;MsfRyn;ZFGqg+Nlxu$&J!o|IdZ>(G8Yrg7a<u(Q_8n~q>bB+IlX)Q4=cnLX$|Vu'
    '#VmqtjtXm2$hPrbo&rk$r1#sqj{ncnX-=(O4HFaNICgi2@^^)Rcf;~lO7;%mNa@-tM0_N*c{Dn~Nk9g~J3IWJD{m$1Cz0B_9qB!W=v<<<^kom_=q'
    '+9Lyh%|P`Wt=T@r)U#H-zNsYQ^I03sjiGafZ^F(paN7TymkhxLKgHd;m+iXMi~XJlVj_YbxnRP&ztRP`z=R@<?n{lpW-3W+OgP<QVCD&fox*FVe-'
    'hfP?F9<(&u=JRRUvAGS;NHu4J!I#U+z|!DZXYObon7(#J336(Qp^l-a1(|Fxq(t8~3l3DX}yj`~`eFc%#_5V1K~`6r7XRGmTI@n}Eg=&IRW{JR^4'
    'nb@-n73fAg{Nb$m77--hs1%Z016?78F~enJo2PWX$f$6=_tzrbaAcNg9uXn%(i(N1S;W!ZLqYS+T~neJ{p2%f@)iP4|3^G?m$30QOWWPUk6sP-oX'
    '3<%b3w%F@F_#h7|*dtPdq0Wq2zN0|K{+oC5|l;O3_@4;ifH{#fo+<NF&b4PWFV8?&HBudi=lBS#JGW*zFz`jG7C0BO+Hla40+yT_g>e6JEV>f}JS'
    '|&8RQM&)6xzg_dVnGMW&77pZ8;=`#AZa>+r5h}VDy@c->{w?%HnJMirQ`9le5bsT^+<Ds1chuIbMUo*cmxPyT2-vnRa2LSTpCq1K9XDuuBB?nFQd'
    '~ked0sIRTc~&@XI^|)QDw+^-h%0&Zz1;6k1l8CjR|1P2!GT!(vj=j!AYbp=3qIK!k!V~osc3UX%mh-H1J$GDbU8l>wh@h8h6&1;v^CUD8_)4t;kf'
    '8TViZ5Ill$+bv@yq%N+~1s)IK)$XC9(@9B)Dw+8yu}uYtLVH_;H|4)kbI0J~bP`k|vqz)EKLqA79U0Z(%zRP#HTIoy56pFzvB!rcnBUm^AE4T2BX'
    'm^dXYxX@DIB~9l4;~l1hwI{<%X=>27XZtQ4_ogkUN5queN|S|D)BLtR(F+ra7a<z`fLw#|&2L4ia@@QYc=Qn-x)>B`6F=1O&r0;*Ow(JX9Xq9#e~'
    'k*pKPP*$LUtHcb5k(QFjwtw?J61}3OEDA(R<y4433vf5)OLegvLGIFbzk|dvB(zjCuZRecokpJ48g~RBpiqHexSxA=MI4o*vTqP+BnK&=?+e)0R`'
    '{8Qqa}k2QlFF~*Gt?ylr>QDO4tFBV3>ileo^9dQE2bKy$I!lN+zyFODY_pJj;YeO*;jZ1Z7W4$15%<##H_iWVu1qxI?h>1Vj`w1{LK0Rgqsph(LZ'
    '-T+IFRYp)!pO&mrW1kzWrPK-$ag_#V2C~g-5ty8?qEd4Kb814(t&x2z+5(ZwEUxH(n;P11c8A4<R9(;^LiqZO&rxflx1I%mqSH!myLF5&8l$d0gR'
    'cAypoBDBtn1vc*j!eT#-D__XoKAPx^tUKdI|6*V=a_Atg$_=r#SI!R?ScTOliZ_tfc^uYx~vJf18DIDUC;2Nz7`FxIZh7LIIOAB+wSt&IT)K^m$r'
    'MT8x7{I2=^DUPl1>;^_@1oDd8v*d{oA(FT*;)d8g{BH_rfd4rLu(;<E=kG|oHX43tL@6u^;E?YbN5=`AcLAMpEz(Q)$1DU~8r9woKR*4}R;_Y5>G'
    'Z$uM~g3Z6P@C^4^KV`+cgbgjvJS4EK@goXMnne`{AIO1n?HT>EQ$z#v#}mL$S?(jLp@~AG9aXJt?!!HZw>f1f3BL<1y!aEpS|I&LfQ~<kDg{vCXK'
    ')CgzGG)xqd0B8r<hjw3VJ$rG-#_aMkqj!w066mpkHRWsc9xO!rsl6QlAc@7h2)7W5d5zeb1IG`jwNXmR&Y>HS`N@Q~x|67D$yt^qafkDe|0Mwt3H'
    '66dr=krkjbED6+R1GaQ;MVbR-P-0EpIp4efx#iIhUcGnRAl?QG|F_P;6dG>qNQ<oT^I^hHt*DknWZY9p1~kt?x64_W6K}2v<UO)8l1)QD@dH^$V*'
    '0t1d-D^@rgRr`B6Hd7npoICI-ZcpMHlUxXL}95L0paBYYVUh+Mi}2eP8C=c<4Xi79HgyXmwxzEq#RQ))BT-YS6^Qh$PuyX<j*6^-#D;RlXYn&<j3'
    '^1zCk%5gZ4N|AB4jFg)0>r-#_l)1>A4aEO^h|laSU6Ku5!6*A>Ji0?ppyk^>w<chqrVrk&4OkTN;rgVro`&H8xtZvQm@p*dY)y3sni6Almd3ieem'
    'v0Haf>pq_4$kZL+fGir;|Bft90{1S^GXfMez>6$NX=FUNX6IXp9deZ4#fixT6n=_?Kgz>W2`mEA9CDNp`s;>vJvzQ}BM#z%jQHfZzhIU`XRHM1vH'
    'nG7fgy@$0`Z#Akt<O<Uiw@ZBR)(Z_bHAvo)8kvHOB-2je@v8Hnr?_XRVmd&^(pZQUp;`AFkUk8e=cb-3(nszmfp5su6{WH7{C_1(`|A06fu<B+^e'
    'JyeCGc?wbFLNp-Xm`e+$uYQhTC=aCRdM6;xctsU-6R-NZqk_LA<#+^BnzE<BICd~1K;(ly#@}xNsc{g7SzajlnXYCG)J<gf;y+=wM2I>wU|^=61z'
    'up#c(z@jkvuajpI{{S^;RnV#5x4mCk&{Xm*3n=qY1eNG-Zp+^(VIGzCS9HQFL?BxTmyF@q&f?H4vNf*iUO0`E0;|2o2e3I(hBHVPuAhB9~3j$0ks'
    '`8PAo1&ZTbVIVy;Lzyw2;nZF<ASFo&XUS-#4mq(deJP(}!zjTy*nd&OvKa_CM3#I#h{k76Z$>r)!XjH4iesepwHEGJ&_~Vjq|37&tPlQaT<f%q<$'
    'Y(F5IRJG1$kj~3R(MwxmF14noQIKbS>VLGjuz`+>s#TaG{)D2j4S0JCY!T+><yW#FiDz?8u3)(fvue=l+4ns$BGVe>tsA<j!S(<Y{9)jvS4qDTGT'
    '%T?Ts;6eEDKz}?nz)XHeIAZFI9q|*VT^?FaLL$#{-+ays=4^&kplofzW_;e#-H>QfbNrmb;h=ni>FpQYihlH**L>`vI%;y$XpT-SX(nI8mJ@!ibt'
    '%)gVa-4}P9>kp;csbu+a~mSWoJ3Dgnd;(478}2kIm#6UZj@iS*ly;sdQfWP9-#3EV%o3yC66wgGT1z2MUvxE?l2obVRyAu!k)Io1-m3D3#TC^m<Y'
    'Z3d>t;;QnyI}T+1>&qbDK_s=m?CaY|gJ39TZK9?>TR!{eqEH0Id^R;hD&H~`FtJOlM=AW~M73Ku!m)Dj*|%CW)YIK~l*lPt`E?vL&DI-8&gsvj3w'
    '`x>B>Mzxyr%aDCwDNUUSzG+T&^&dk|o=(x@-j&rnr4N!PjnIHRFg=7ZwTb=(5LC`VE%~p!6CKujEm)6R2T_}9&s+Ff<z>^+g4~kH#8YUDPVX%j1&'
    '|1>*_XqIv(#6rpX6kpLWG%;wCb`44an6@?pda4e5z|Y4<t#JzYrZpJ5Tl_Rk25%Wz2x${o33;ku#--g!-a#^!E%8ybmGHpC@wMRqu4T8W3Wir$kB'
    '!2!&caLvfsjixifV=q!|`yy49q<7`H^o{bn}`ohaT?0uJyEojfVyk440Q)oiJOw7U9tdjlRsihy$E1|_F3K4EE!XTeus$S0M+W=s@@5hOHnzHFKr'
    '4`>(^vOnX3DD754G&H|Cjzj!Jmx2lbvFjk-rk6eUaRzKrwIsvxylD+Ov|x3zSyJOn?-Vw+%Pgob@F*V^3esR4b-S4)<u@Pj^}}Gv|VLNoJq9-y(e'
    'K@V}%E=*p#99BrkiS_E4@g;uun2-?e$#akd{v8k!%9!=eTm3-qCl{t^8P&y2UnCN{b=_hZgfnPId%xmRU^&C$_oLXyw8QC!q);w(<`e3z6Ob4Vyl'
    '!P%VL`8AL2wYn}kMCvP?{Hk;qkDVY7v+E;~>==FdCIv+=4T{H|O5E$W>iRBB<&lI=_0HU`sZded?igut1ie=uil(X5FOSB!<-0;*UUS(;&3|2H)u'
    '!~2C@5$zz*v-~5t^<$brI-vL;{Wp-)8|aV=yspeB&z}>(Kf;15uNa9R<8S(q{5Y%KjDbV$YWb=EgXTr<x$FofA_K_)<0e{shlj5NE(j>h3%3iY>}'
    'si36-ZCyjDEfk!?ZPvPgTD}7#%$>L~=e;U~{!{!#O6AS(4(7xA)0xb@D{Ix|@p=Zxe7X0|LH2;rB(f8j1*Q%DWQG@x8$IVb~PQWZkOwx}w^msW(M'
    'Ona8jY**+^C#6WX7VUAx*sK|yRV0vy&_Fb#dAWl-vMb0yKr@1DHKN2V7G_dkBWirfYam_Ot!z)=slhxgor?n*d?Vm*=#v9*;}+JKV?UU_g)$UHt{'
    '*SaL8SHCWAUadypo|YiFS*4f4a6|5%2oGg-B{ke|}j=!I*eHPjZc1V6C;y)dqi@=sU$+mH}HH~kFOhEN&3os)0)iXOMe%0mJ@Q#TM9aHsKi1niTf'
    'ap)jwPe}C3ElYWP?iEf(is^;MZsC0n17cvj87p7qXkB$sUd!B*G_B8U%;gu(L6Wg}TpfaO>%YxBp#X?gS7l(GW3*~{TH^nFRa0ueh4qcRR4biuw@'
    '@eGNil+ITf0bsOpR4M)Ltz&t|W(?xBL8BWQrm2pW0|O8<M3lZn}TbhMZ_VAFbLbQ7CEKUiS57f`hL_WKFR21E8pa5g8&b@5yxY?b2pRlRa{5fx{?'
    '!4w+ilFFzzYt%C0Uf6rxB2SK)Jcc(MQcD+pKu7w5lVC@+e6^%;*aCx^n$WU)}sstSDNhw1vvKF2GY5E9H3{aJp3Z@U>4~x{~4LCKCe$J%l!N8}28'
    '~&~^CzxTdY*5!*j$VIymfX3%iO+8b?pZM{cpx_?EVU;Swpp%@SwvK~=<!{XY>BZi#>h2kR48bkf&dFqzPVW$)#<Fx-?G<b58(Lv<5zoD`7nPp=Qu'
    'GPsDzvZ{@+K4?n4|MsXHh<y`YxwTKnl+ww&BAeb6Iw`goJgZnZ?u*iP1S@7>;ZZnsaW)^A-$5jmp?usmOyd<DXi*Q{WD%wSFw&Pc4W@prM06NPh6'
    '!LDXK{0Qzbn>0QpGCQBmv>b36h%+jb%4cy2qXX@B8^-V2ng$UWAu*%2>w)V4nLdx10^k6{#jTmSY_B(aeu!v0sW`RreUxA}9<1Iyb);iW$$F^@b`'
    'e4JDrI_)E?<dhW6e|6R7Io-!6ln&3;DCg04iNOA@6VMC=MaS_uRik=XdvlHpx3*OBB7~1RMW`2bOG7fQqYr9@^Wz-5x0ukCb+Lpb%+fz5EHTP`ZL'
    '{j&pt@+2c1fw=l6RIn>oHGR6+Htb5yuYidyb2opM_W+e6iny+P+zhUez6SA2OY1HXFU<uAX59c`^XNA)lhI4Gm{a|ymf<wh;MAb#Y3Zvt$Gn?NkZ'
    'HR00LD<K3QiE?L@*Py2Ws2<dD!|dxbG?=r>d;qo_HP)#u{2IJDLV4wKfiS8h?B8#O=Ffv(J5khE@I^>8q>kTEJ5A@eTL?yN^l8CEvc#gk)Jib*=>'
    'sa;LyD2Mo>(huBMi+1qx5vYy~^csGpKLyI}*{5AxKJ>M<!3z2~CimAc7!cqF-Z^4WV<LcnjEYCWtBWDi-u!{BIu;F9RLifn+GFPn!*#>9qpVcB0h'
    'j|#!a+8)ouPQq3@N#gc58E4!Qj6Std@r4IlLS0*cRI(WJ?W1_S4dhe<e=b?Z`FNTKck);|r?iab=zf}#2zx0;Sx3%#Q^D+B%iJvU0_&xOiRkU3i`'
    'u7M4py60gSlWF7=|kh5Q{mygWis%YBm2E0|eHa(tO6-bqlY=8To=g88#_gPK$hgzScsjg>f8x=O>qLw3^rDdOwhgg_7dGp+tls9c5R5!cZ85@HK3'
    'cHHG#^sa-PD68Q6U^!R)uqkEUBxh#FXGs~Ow+Bd{mTk7bn9D6&Y*NB^;jSR2%^0mMs+8JJ}92?Ngf=i0w3tS$=hbG6j+`V1DX#1@zX!ywo=5Dw1l'
    '*;EIy`77D`5~S5@EX+BJ`Y1y!K87*tFfxM%hVW;f*E%lVDWX|u(D9{^Ys2KPpe#`>)FcXhxOYqw7O?R&|#4>UvC15d#C?#H*kPR9HT^SfT-S}^0Q'
    'Q@NmV8A$VrB~aTFm*ZDa%x_-mQsiKvly%pU*8zP+v%<}m~YU}X#aA!)zQaoid%3K{j~3~3f^cu$w4%Q}A7RP@q9mZ9As8rJaRbdN!v29xafm(}78'
    'r*o(-$HP7)8&A|fz7^56&f69L&wcInebeDVe1A>yABT8@KjyPdts$HO>G8M1up7>RF2pAqN#oW~xA6*|0yVU=>1^c+-c{bvRk(0xv9CtHT0w<zOB'
    '2dR8>Uz&&(2o|g!<nqx2$2@g=2TEkhH0?!y2f%BG=!L-OPG}dU&B8loz`^BL<CISJ|n^G;u_o)Vr|xWISf@&K6s;u}@kSJL6VX(Rc=jq~bz9+oaa'
    'rMLE9KIIcj-r~)Ey!YF|BwWvAqd_s~AvUZPjA$w<J^(C3Bnt3!0GSu#U;p-FdP4=B63&o80bS&3i<(05Ey8tf_5!m>LVZtbQTuOa=#`>>C+R$lnz'
    'EPAfQ|3>Cct$$8SY?p-vJ-Dg^|j46EAk1krkY*^pZiHChDMPZj_!A%hSMU8O-R-~9_O=+Ty-N|)wrkL_3m6#l~ddMyB<5xooR{1Ti^aMl4Ct<gBj'
    '0yjxCbf|GmgY)QfB9c#bxr)E+^f%KJa=ER)fpG!fAA&+nl9r4G=;*j@{>=@1={Q5b=uDXb~}Uc`FMOq93<LUF2dBRKjtfo>Nz1_*(M)+O3vuPoh6'
    'YF!@}pQSPJ)Rt$WxzK@s;abNRO{uZ^b@o$HbyLY=uO*I451}O3p8ErB&*(Nalwo2X8?#O$y)G~TvIYFPLKOVNv%2mNGjgft^Kjr&(t+PHDb6X`1f'
    'T89Uk1~xKLFzBmm+ZXU*I2ojrM_3^0mxy^%+MyHaZw6ISYHHZL!HchSfNEB4yJlOjn7cF`lQ;9+LcIUDrUA`Hp=hMrM4NqcCUiK7B?!I>e>2Pv=+'
    '#kGh{GPr5PNhI|JxO0O41W$0FiuQyJCISRq}Q^zY@6b)F}{v3pKnqAKtn$59*@i~q8iUoIkQpsV<+>IzRNhzI$Ji%+5Gkjbb;${+6Uub0Y*ASB3S'
    'JGs>);L~fQeA!<0wpPPC5sbeTxO_8q+|Fx-LqNR8z36V$fkT)5Tz*DgSW5yb68To5mp|u&4EphtJoG}dt~f!3?Mlb{<7`KmB!=l$mJIuH<{zL%A)'
    '2m#;GJ^|AfROrEvo21)q$5P)H)o=0X5RZNPsp{(AES4Y;cjtwbdWd3+$EQ4D0kCTBTj5|74351*M$-j=fwKZ^a6Z|&7Sk<{1y^*lzw7wW|KrkUy1'
    'qAWfNbQ?Px2FG}29-F2pGI0-5B6lNX@TWiyTw~)%odUK>#v@mv#)iQfO(Pw4UW=iOa%xfF>?Z0aHXqK`Z{cQ-3+(+VDhzjUrc85`qlTGB?ys@p@#'
    'QA2%->eFWRZ8HDU~}{jWG7_8#Gh9fsAIVRlb#V<{iGxuJ+{-8jk140G<wmIKA=0B89K()>-<(_Fkcy(9FI;;WlU7ja{gX#7@0r0DHg|OW<e+r4|5'
    'Jd8+)a|6@+*8&kO|3cTwYn%vdKsb2L}i+y~IHky;kgoHnc>Fm^|kN~s!+osr>%dG3u#zsep+Ddfw)!xu)(r@xb>oRM%aAs-P|L~@G3#c5K7MGkjE'
    'U<fev#|M8U*1-C%SD52lL6tm8XZbc(wXgJYIkB`vZ?I*YY%@?vkwojG#$Sq#nSbydZ1399u$L~88`Xu)6Sh3s(Mcj?2cIEh4h698%_O!TUnGFU<`'
    '#6aSL$I*Pv(JyVrcw5l22Z*^1Ng`kqV64tLzuFlp%=aE!U@s2Ug8NV{M{Mc2cYF;}#Fg?&NPxPK^qqQ9HOHeKbZJG5CS3cEG~nG=HDky)8pWDV-_'
    'IN-!nj0)_vzOmnojnoX!X$-%o82r(YLC<lrsf_1O`c>5A@nR6rETy!;SulGr9$sD9nd%fQr$4KjtsGos7Kbm12Ira*g7^uYaqF5nc8JcUm~Jn5@>'
    'U76Jbt?=_uCkcr38a{Ft3>1mO<N1m>5F#C%xS8C^6(|W{&)wRRTHoE92CwarC#Svo1y-eVy%*qZRMh@CGMu`zHn(0U;CK`z}V9x0tYejqR5nV%L('
    '3sOf@UI3I_xR5a97AzG2;*QZNxFhNb#E9zGt=J<Qqy?vXL+OOzu7o|~=F35yq|1RmOoRuxMs^jX*lQbT?8)#>F9fb6Grc0*ZDl2ZswK>W+;*kPJL'
    ';~1h);xjRvhvi7cg=Y7dUA05{U0g>G#Y_XH0#V8e5GFUAdH)6Je`ihZ_hd3$3YHL6w`8E&v5<kA{B(i(@=yHNsJwOK5Nm63O8)-jWi=4r^C1S_OQ'
    'Jl$#%(Z!R5s$cUbQP%1bay_P9?ip<)URSvSX^no+8o-m|LV!;r2Pa@Wgz``-%p1~gPb#>!k7T&}+zOU<6Kf^kfZrfHE8w`<|S$!AGQ0j65q=J0aQ'
    'rDrRn6qI{y81Vy$Zg+O$-2<EwP#QIc#+yrr`*8s!T2K@dZ(Ofcj>Ebi$s)^z0SW($!7D7@PG>M@l`ITr&hfqGN1-MLr4r2c^|4CAa?<&8G+s&rnh'
    '9+T`Ioz??T1gv6Y*4oT3OHeT>%T^a{X<P?iDfi3o-^pJ)#y4b<<@71xju1q2mZLGm3Yvq_b;)wz6zfBJR7F+eqLmP+^H_eP<l|##cKXn5TK8O{As'
    ';vD+2W+WfBxg?NzTZ%w^F8OnxLf#5JQXL7P7>d~$}K2eR?CeK2?Rk1KYAJv2?-nsnr#PUAxRWn5F8jbk=B*GekPTK4&Pam((E_9G;*9VCNr9HgH{'
    'EJ2XS|W_;cvub9a6Q4_@`h|)k1c~5h7|iDbRq4*R_!S*TXr}3C~>Jr*eWNz#-secsfm`+<5dRk<|`g=$dp8N0q}Omkl3=i&v$bc4{^NKXgITkTGi'
    'BJo-rG%p`efJvZj$%JJP|AaXR@P@9T-eOJGeSfnN0EJ#ok5g7vXEA{T?0`-$3J=Ymz|;=>NxFWE+=YyM%{IIH<9nb{l9n73BCHj^+_)_(dDM9?jB'
    '8q=(Bm%$-3{9-FoaY_R=!zftw2gHF)5Es<lP|u+Zx+yM{o{>AnV#LbDxTB7bLyOz&e#l&GUa$47*2nAB5NT#0Di!IJf(04{Uk7gBeUSivh>9XZ*R'
    'P$vVL;@5qh}u3d`y`Z1_SnIN@&k4=!tI?e$m~LJZ$_#H8(x1hnUQX-n^8S*CG`%{ap)L7C3LdzJt^~)&-eUAXC#0bsO*J)X=2{#33+ll#-t0?q)<'
    'pK7V7zjk`V(lN0gG>ND14OeJQFV_JbN9L%w53yUz1Ciz;XV#Zktzf3y0u$=%R&-t;h(tGB>I1FaO#OU0X64KD~M2f>RfvR;y+nUc$fdR|!y35cx%'
    'jD!8^Ky(%47(YmqdiOZ!`BS}zs4Q+crEhkFdX-|DaYn`;`Wk;83op|CP`0L+s_j2<TUxqy`~O`GY)(pqvkZT@g4FqvzH%6Q{AQX_UBr0#)LvoN9*'
    '8A_5#_5G4p#($<DA-4o=H!rAx=QVH-ID)%O!Fu6j406Md9v;fNC7*SF^iD<(z%;7p)bez%WU{bG_?*Y3ewFC7c68Y(5rM2SGsx4|tYd2woP2O<aO'
    'eZ3d?Y(foE5K|^C<gHGhT6xWBpjo3qI=ADR_yK*Rxzc6p4`-N~E55=`t<MW|J{il2Nmf_DfHryo7tr~`nN+gGncd-R%&E50G+)ac+mKfOiUsF<Te'
    '~`(4=!$o7*JX-cmh#gR~kTV1<0tc_K0JVBr;O{G#7CMyU51Klq3Xz$prn?EPu7+Z(fIGeyE}XKfj(9T`v;l*Am?>ibtSEwQ1?=d}5Qj){lD%>#=u'
    'yo>K|`yTtW+EP}uP6L5QDbfV@Xv>)I3XsL*Y`1c2m&sK8^D?!6gt!I?;n`PVv;aUBwN!pvy7`cE$k}PT$6l=%BVo1n3M*c5%W$vL^Y1#pN$BAn{C'
    'lv4#0>7ATN~ue^>ODz4k6>TC{9NI~t#l$fY$-D4(jBx}%$~*@gn#_EUwYKECxQWFk#;U3a(|w!uOL9orS-Y*viUjQboS4RH6AgYDJZ@D5mKldDe7'
    '4aue2RwOB{b3FvgZ3e<km}j<nK6K5yYtu=&H{NxcdO{#vGoazau~{UWbY2XlLTajR5Uf_bwAlS(r|G710qV<a)&DwKG^u}2@~M3M(U7INHkkusHZ'
    '81L*E9!l<AQB`n*#`(0x%jW|38sC5Go1?D%@}#|)Ny;f4AJTZ5mE3}T#n5xTCnm%AG|gRqtqG(2=f$N#JW74qo+dWgsLQn;5pEihEI~^C1yQtf$R'
    'zx|v7eAj;t~kO8n@HqRn8ty?clm(1gg<~$bGUO%o+EzndQJ+Bz28smG}vxo@LzIo`~m}>aUNH*7?iQOzFGTRu!;z-$k`Eh@D>Ujn)!JF=o;BfUen'
    '!i71qQ@9(;|yQl=hi34>fH29i!_}+p8^0Xf76IbP(8P9_f|3_clW>?xQ5RejQtT#cXE|m$CFk}Y4%4X6uD#QyRiU{(q7JDXFo3u%)-0N>6?daAkZ'
    'If(>i+=6F{-NphF&V4ep=^+bleQT$<ao_xbA2oCE-mDP-pcuE+;wcp;<t(ypmBi~g#*~V5PiNuUG_4EpM22;Zqpk+lMc?Wb*j33ep!*@G+=nB;WE'
    'gSUAhJL(|(@$DLMZrj!;@}f8D%4>Eb#AjsdMyPQukkDRXAkUWZLQ9~<Fml5uIAT@+2EA}p^O=75Oj_1c4K<NMR3%#EkwO1kxPJ>1OT<U8%zr@|q{'
    't6|6%?-tkUo7G=H(cZmsH%>g$s?&{%qSxMYGsl*fZ|Ww?o68%HWVv+jP`|kdK{byL_q|HTH&<9Y0Fe(mRoy5z6K|ZL_JhcT*$$w?onvaYpbYE5SY'
    '7M)w(lLUQ11Y#+N&~?Puxi^Tq2oVW~Us3Qz<EX(QmqpsC^Ie^sg5CsEF|J$<W;%o-~+usk9B@N@ZWxg32W+YXh3X1}@fz*maAvM+lxZ@Ft2`<Q6w'
    '6lko3Sf*PUpp@YHU4sI_|l<{y4N%e3go6-HncpTk*odE7m2whhY>CY3Siby8MeS0GO6!fLW>y3`rdo)hst{4%laKm+YpCr=6C2N#gDtDf@tGq_8A'
    'Hg_A^DLCcIkr=O$;sz94iHL-*CLO`HfwV=WQn`xx{a&NRgz=8G4k7S4T(W8sSaSI+!9m4BEEXLZ3o&z=3Voz@fjj?(;L#&1g?;Gi3mk?e?9V93DK'
    'B86rZkm?=_wkWTQ19@`2)!!|9_oqA%3II?aic0xNwd4z?}*aNK3MI_R0z&h*3www+yk0h}N(^iE30u;=4l>!3QOy+(+(W22DH0-HXLMzGc8d_Rsg'
    '2R0HXplN%4L5$A-i*Rn&ja*8BM1a<np3qay$J;;Ve*hK}u-Doe*UBn4KG{2Jaot-v=X<dWG~IYy(f@E<t}J{7!0pR(GvD89r12VGtjH0r%%%T}-!'
    'S$=LlWI9%#n{Cz)?Wn9&M}6tizM!h>%%jt&Rj~l>Nx`8kK~{UvG$com!K9?UXZ>@<|ekKUm)dao0g%c>Q;kUCTS~6<7f_y1bg)*k>775iA99%0tk'
    'Gobm+X!3q|T_oBdA>ZpnW2D<{O8TX_sQ;V6{T(99$=^FqBWlPRDk-YwXcbixjv=DZ|*D~FL?(&u037K6^N8TzPW!8H<=x@C_QsYz<2Z(tFs-?KDo'
    'zWhMr~eRcfPiGv0FaUxfagT|1YTMgg$+D`xIN9X3FsVkKy;CyW3$@5&+i5hW__A1YPzesPn5|)E|$)eeg;`){Jn6cwOrcqVHM_oKToYbyW{#mqCn'
    '$>ADg!lkE2Qug?-R@W}`vWuKSVI?_5<%WZdTEeqGlpbH<NEf~9sjaV{VIgbcB<1uAUF&G_SjSt7M_RaE=Bdg`knO|18`$_T9)N_U}XYkSkTfr}^_'
    'z4uQgjkf8;CQY)Ew-P-h#vc01yNamT8L#G+Ny5>3#{j#D5{do;JUG4tt#P!TnMh21FV-S~A-eINvL``c$)$MUW)zSPqgx8rWSx5l_yN6HLLc#kDF'
    'Np5V$6uVw7quA<_~&<($;8*FJS@Z(X}8+{i0q$km20i^2GTu1lw}knM5vq0o>axtqhWS%L^p-SGu=?!iW1w@21yDh9eKx@#+aJbJ?tIdabU(!=bC'
    'G|9U);J4GIh190s!NyaH1iF_+W+~k^xiZmH9&RaP~L=fvERv)6Dnf&91YHO>?Q9<2xB!|{1^Tut0Gakz3lYM*f0ON`LHHVxPi>wf%>ma#soMuKy3'
    '$eu|pjC9mnxYz0@7q9eu027nOIzRuPEY>qKvR#|y><Y@E{Jg>aN+}neY$<i&Ae5o<7d}?C85Pz8vD@{UwM5kvNkGXIdHrR`VP82n^w@0_0jko;ev'
    'P+#)~}{qjc4Kg_7a6*2Vte0U|~vqB`v*qwXhYBJmp^lknX|JNih7e`C|Ji+rLJfDB!p<>_U<KdTkj@}P<%x#x6_uBmkqeXvL4``;M3+s<36Sot!9'
    'G*CHb0EzH;D<ge4hOw9h^=1?=?z?R54BOhhOX-U$+(NV}{AoGtb-d4iZC_{O5r20$C#_EteRtIPn@I=LI3Mt?&Xz5OY?_MOt2m={^3s@36=kG6kh'
    'mu*VSnH%F$!1W?h7fA%6+Yb#>7yF&`WL=tj1V&)9ZSF-7Z1L6GaIg4S&P=>2m9yV6tT@3br^m#}(MQIw;vnwe+~#xkR6VY!b$tL9W4Eq(m^riK3#'
    'VOtE@}qsrUJ4(IoA&r3bQ2wIl+C+QYM7fv(2JP20T1Q;-R$+8s8{THcSHZ@kMi6xC*c{}3WdE?U6XCi=nN7PotUz{LcAtOYV5}0=)^6n*UbwUa63'
    '^QJdac%Q<jaTlqM3<n0>u}DXf|op`5;}*vVk8yg;}Z!12T%)ltg<ZIFzgda?)`+>=94_4<Y#F%fN~xOg;|6k-B{l~MS~2N9mV<67((y8imI>)zTO'
    '&jA{01rb{<0#7K6w&V+_Uzet=h7-EehVaCx7f$>)+%-QaZ&{J{bXZ)ez|h4o?1Te=m)9!c6H+g-EIkx-8}ONYUQ`=sZDe;NIP*BaLz$MI3aO|cbf'
    'eA5ePB=>FH#nUllj}Q0J-G@DbdNvalGQ!T$5=Z6bdWUpIq_k46s=w7Xq{ZV32$?YcxLp9iSmXMH!_SFuj_x@0RIf!ElE5#!$AJ#tQvu~BIR^{KzJ'
    'wJOy;wBH<pdEBez|bM+i`TGkuDFVkoaTL@)kq$$jwOU3i?EC9Vg<pH48Uf0VAh)Yy-CY+^Cp;C-LX6$%YF~&@zA9{-h5x<(dtx&y8WVw=Y={?dS&'
    'Zy8OKG{v86>HH?P>B^VcQv0Ptu!Ez<MzeDDb+-<cKi$rG2Kg$~@&Qb8t`V^D#T4<80ai#l~fx8=9twk=c@M_8sF7d4>fK-5sa&GA+x7pmsKO$6HL'
    '(Ttm(fB@%nUva2N}ZsZ`a&K^?^jTHxWK)AD%*yJMhXi-IQQQP==q4MP;Gm7HqB<xtOJx+0>*4yAe4;-8Kj06#~HuJ6*$B}rG}m#m+>-{QQ3J;gfL'
    'noq`xMOeD0KFvijKZCQLW%=gl2aVZit`KlvlCH4;@pIKfN%FUCnBMe=q6nWJtF@8x+aw`7^S8wYjkCI-o&4M(!1#z$JXSbN;iXaZB-v(f83r%SR='
    'a?ujYo$eq9D65KUDHVRUoxP5mgjJ_J9k!ZFa>!Z3TsAWOo@V2C@*pd&wsKQ#2}wR9#JIh%6Bw-dL2XQ016v-2B_m7T?+FVR-0e|>NKeI{IRkP$h('
    'rmW+;uEL%j=@13R4-#>zL9W6nN*6agxk(#T~;R+a9{0SIot$zl8XMVP-U;Vl_PP4jYH7h1BLIx|%*B9LB72OQ-QM{8a)lktr$es>KdZ&FGWD>h+S'
    'V!ceY!c`^oQU9BOu-lEZa=+%(4?%swNr*;bQv6>!`SnD14x)U`Gr<rs3qCC;0u8vv%tZTolp{UwqSp?2=<r}Jynd|sk#&3UroM$euLqC*pN*l;7d'
    'uNBei7y*wZ90FWK`04oP9i?eqK>pJu@l!zxkjf{_uc&h@xhOx+LZ9%&!L$ZZx2P*HyHx_fFkTk?IJ^&yZO(rMY?2AEPnTgeyJ&9M&P>mXg(@=qUl'
    '}cqSCzp&=mDq#f$<0ZtgXqJxubqXv&2c>85SHlCP0)uO-`KPY@qF5O)E>1lw!&pdH@yeeYF+jZKAV2x?1YndY|?vTqq?G~8bB<AFy!@RKv$|8qBB'
    'q*jbyPpE^b1eoDYziuKT4bDeOnmpT!h0V!OK9!bUg?f)qe!ZK{eZTHO>adm33`h;lPxA^%hYVd~1e6$Bi1xAbT0qcv(zb#RPLwJ>(mP@$a;^l!t0'
    '@ABWl0o-OGGLL4b((MeJCl$Um>820qWGkA|NIvTDI3_&JV<3!lLU}orJ8OD^h{&f%xp=U<4171qci~8o`fmEr71<H#C+a>IFKWy_)op&J-Is8IWx'
    'Rys~S?ZH)q@6V1$|ZDH{(v6~w{2nG9Zk=Ik>H0XmS8y8naY)6Lx4}mgZuXC#mOAf%<ft@fxymOp*klyV{1H%jih?8Quw0Vx5O@8k!zti=_5SUgA5'
    '$x$MKO>UOj}v$}POx#TtgnT38|rf62tBXle1r{l;YA~0T#w{XA9oQ=v;_zGjvgVw$Q9BUM6vJOh4c(tV1pqA8t<<s-c6|HotriHY!Y?PqXCqzj>B'
    '92fv4cT$g*#<$%EwqsjAnLDe(6#<(ob;lGAiw&^mjs`X!v5VZcNPT9V{`WLGtQBg{T^C}*m0%E_rxYW#G8%Ht1~G{Gx-dEXhcwbQ7^w$~C1f75){'
    'k^zt;tzNT5ZoNFZ`=_8Xt#5Z8sltqVbDoA8gL+kYZ1p$nN#iAu&eaIev41e^@UeK;KQ&mtDA!vF*5<wT@(`>mJ<4&uAIoAg=>oZ0Lhw9e@+C(vSF'
    'lmTxn^CLzH$Zn8vpx8tpXQPfSKc}nV+vK>c$p#%t<@G+e{<)gtpELpdNPFSxxuDy}bbT8UIO$r)qzFoKFDm-Ec35l)jS59nrM<ZEb{OaEHQm3pNW'
    'PTu9jB>cGdOX;pYiSFxf2#y=;C!d#?_UUZ2NyfjJfyDkkBk{hyZ{Oj=FwPT@mUrUq^keWn*wL1DVq?~(moF(kW!t_SPnX_bn>@Cn?s9XTY?y+~Mw'
    'uyw<d?A=d^xXJOwg(q0gaXkHSFb#7lq0ag?bqgBTUk%^wM5wfYWnXpRZ(e<sr*44y@!&((q>}X5@NG{QYXq|TlWmUC01PVljf4|#>HZSrve8}Q;+'
    'eA9w<7@`APupX295lJidoKh(U}9zP!*dS2d$<Y;MjY-b9|MMhilngqA7+FQ24tw0()brpfU*i@&9DvY#$<VH=a1t_R~V@QrgR?FjPi8Qz*6wDHcR'
    '7tM^VPuF-aKMdYRCHU8fdOaz)?vL)CTI9=KIUpwrQs7emtar9QuQYa`7{hJ`PG(&dcHcv_;dRp65sSri1Ou2NUW3VFRrhJuk_E|etz7I<f#&*%y#'
    'SD0p0w9GB?U|+B8Kh17;@<j+<T}9VbphTFY}_Dl<}5^L)cRhjs2e6395h1aBzwxcaxM`FQ>~LoqR~K&Yk9h3iy2&T|yDY#-UR=C9@>FIkxwQ`NYG'
    'a(e&`}gEW>ja~rQ~jr3i5u^mHJpiLF-&eY4**N(1MuWr2MNtNCpXEK?M?UH=oC9}L*R_pUgdU61abK@~IUAZA=lDOryL}4n8u1#`r4)?ZZJ^ZEE`'
    'tj)ayT<no<7Bww?^uFp9I;mU4~ReZ9J~s&GKZ+^Nq(QyEn|V=73}M-EzKvDE)1x(>TcIqNqe*}e;2tt5pfe%u#aSvY@<m=80#CtJ>21hS$diARex'
    ')$O(r*47IOx?(vr137~b?UO?;Pn!hi(6MHF4lHums2F1Fu>Jv}w=;%G6wd%fMNTHOW2Xiqw!!v37V5^@MDyr&F_YRK{P5Eh=^pP6;CO#a&Sfh=LQ'
    'eeuT7@ooO<5YZZUfjm2}%?FN=^31%MB(#>$Rk#heI(*pmdSjL+@$rzDB11ze9fi-h@RPVjJIrWsJkG^gSv?-A@ojB5h~8BwuzFiR`TRy>_eiC(bb'
    'm9PA!;=_73Tga{ahL5X&C+dD<vN8)ZvQ1F^IfQ#N|nSr29|gs`jI_1wuR?*s46+ahNny#vOJbOwlmNO!JXXj^LxwX<<sR8z+sAL20r#6E1Nn2yUO'
    '83YwI1U*oXM#2sLinE$SV??r+>X`Hw?tTv1xkDX(Dj<3vw9JH=PPCbm#*SiXCFG$Id9)i!d0s?Lbu9XN$hYZS1?$u{S*2Gxd-^UQb321U$MAEbI6'
    'wo?0{*C(oeX~%`a+7-67?iQBYLMO91;GrK?4EX>O*+pKsp)v|&7jjxAfgdd7>h)r)=OZ2zJpNdO)Yv_J}yC4cJ(D@o=dPdfO8WAWC1&x-_^s?=D4'
    'P+z7v2RQxEY@EPI#eZD2tIc9&NhY=fBP^ZPGx?i<pl-epjj^~moiP)~d>Srosf??{HU%kxG`5**A0vWz*}Ov72Dg?)W(kR>FI&pfyFcIGYaqR`mH'
    'OhM0vhm&)bnfCf;p`JF?#PrYqbkYbz{$k$9goc;skSsj>pPzMVGprz${N_44$NJn&L?qoR+9O5Fe+#`E-D#*X*arJwOJ)4Ar{V7tcbD{fb?vPMG+'
    't7Ii)QKk4aR1$rblhcM|W0&I!Di#LIA=bT1`$0T?0+-^g13|fkAU)KgvRHpi*yn$!71Scf6dVPsanmNw7w09-n9WGH;6rIno>7^A;Sy)lP$p%^H|'
    'S+1zW^7Y|&xE}{CF!3+GLYuD3vy6oB_`mic>t(+(M(t9P!)33W<i^Qk`5c<}!xN<9eOknZhUBn6T+$!gYEE~?D0}1SR15q;MfT(!{UCe!Z@|?*X@'
    'kxV$ja|yLK8oT_)`O#=XN1YBy&}iZQL59ry+6q3s%BPuqD5#v9G?JkGJgrf;!q#T{sx@NG2j2#M(osuQ)_wu7TQ!kd#T<>CU2pXP)|$XfBk%aK^c'
    'v_qkHC%S>FUkpMr;2dVj4^E#|EIYPt~`mJP7wTs*@8;Xd?|?_o6zqOOpPmov{7%1AjqCP`JmmwfI->dvi?oKA;h$m`NW&(YObiMn!yQejN7mAO#P'
    'zj*Jv$=xWzh&lzLDCe%{D`UzaI-a&iV`KVDzcj%|U=`KHESGOwP`9}D@pn&6deLq?wyZiFVF0#b@J<B6Hs`4<LT^_X8?_vL2jh@8{jas)ejt9oZ}'
    '$x;psfvCT`v^pEVt$dWnEkY!~)pZ#{}9lH<53v*v^0Q409i8|5DZ;NIT;@;+j{gn($K&aNh@NrE?{q>NPLJ^z|@S%U<f;g?U7xg32Z9oj_@@ld-d'
    '0O4p)xK;#7rJtQI*nVa@$<CN)}CXPOy9nw|6!GtVzftte=nCK-<gKJ$&X}R>eBwA%*PX%~f?`6*Q2F@*Cqao;ghv$_?<bnX+Du`<wjF<vf*#|{#{'
    'c3scGVB1Zo)+p+RGwb#T9)$qdk@o%)*Z#X8rvi#fyR<LV?3Top@>mAO-wFu8D9(aa_@#mB%pmP9Xm#|nG{tF=WoF{#i<*w?NwapkH!->CI^MlVd|'
    'cO67EF6OW6PY5x=>QyCIAL@*eVv?sKVSbv}_M$FKIAKk5BxYFX7am#4ZZ=<cV^KUrDm0c9{OZIk0N5-!IH>%?)RITBUjqTI{=bPh$7t;N1P;7HyE'
    '!+<(RsIwn30@StA-Jd)ednRU!#xsZM`d(&jE5c5+9&qUmZpZ018_CUP_GbK;i+ZB4i6<8}x|J5eRR%q!RpKnSTJE*GJ#ZA14kbvsqM6f)@BP8L59'
    'eHQhLbnCC|luL22<N>8&c2ubC`lk2L?FGsy|MN%~WpI8)Fx9V>dRYe!tZHT_~n{mpgiTji;*wGRYVEkZpm-^<Y|i<fh^@bcMCKCPSA--@9~517f$'
    'RdwGA5SIvEce}{y}@A%Eiyp+Q$ogtac(TtUJFr8Sunur3XF|$gJV)uuE{BYOY{n3v`RcqDmM&v>LHyWusyr_%;8*#HZS>>}+V$RWeytc}Qj2fulh'
    '7Vew+)dPNJljYJ+u3zZT{FkSqSv0G^7e)e-dTqCy9M>6B0dVlnAe9K5&6Xb9JO+IT<!-ybDHbpDLC*Fz>;XcR=5O3+|_gp(_2}2FcP>w#JQKd;=y'
    'YeBmP7JLAFgkhP5aii~pyIn3HpS(MsAB9K=H36drnab-&`HkIzh=I*TT0kj-VY!CevnkJkmrd{L~Kydd}cDQG1YEw`FtTxJ*xI&9h`iK|g9Jl9_S'
    ')0}0)Sy{AH!Q1Y#dwS3EmMLV{La$O{Xq{3b#WwNGhE|sfObE7~ubYK(_IJ!DglLsfQTqX;LQm9hGg%!K4r+Q}bwJE=?!21R7Fm|&pm|0sbSdW%Af'
    '_w+c&uGS8@j0%Z6hYE0-xsPQCAYgea&@3I=Oqlmbs_8S>q_Mi`->{yBU9bdJG7*cw}P|^d&FQo`h4^8n`qjP2)6m9}TL>ab!Qx_aGRfDmWteu!T<'
    'WCyX94cHwfjVsEF&wuB&zJM;g09=+FfhD$IrkvuzZ*LEx{p7FXfGI4D4S#VU5W-NVjKBt&UtiVLJBsN<ScQkI!@PLO=%eTGcG~iORrTZ7b(nMe>W'
    'osPI$5gN3btE=gG;{g7YjeM$3(#(=m699WWk{LVebZ&$Mq;UAp#`o6hSJ|nwn#}7E?fEVOk-ZiiE2ito5lD8!IQrfeO>nV$9&XnUIquz!)xbhw20'
    'Znvj<1Yg~0NxN9JdYAp_OV@x*G-AdQP`J;@vB9jY&pRim$^@KUHr<EQa8_OMY791a@2fsI!gscRgX;~?L;weC-9e4sA1w;%W4mpxC_iR)AKn~&hJ'
    'Ojp)uGUNkdBNxr_YZ$^z%|5c^yn%8Djfl3SLzsBw;$5A83$WCR6NDHcEMY$gMsr(=GS0q+yUX#Mw$5-*-}YYTW0q&fINd0Ps^J!s0jfw1D1CBA0d'
    'oX(jiwGv-bRgMt9ICSa}**yb|w3b@D>%KjlO6!Fs#}0XddWI<KrA3Bk7Q*{z#cRo{HD%9#)lBI?s}EQPmKesqJXg-@>%+@Ob(;g#ZU$O2zPy_8r#'
    'r?I|LJT6`N6C(xT65$TH+C1ZC%Hd1zMuA2p>=LgL;>^v{<^4i|ZOk*|WV+Blc=QplwXlpp9WR05a?$7xklp}n-6@9fI0ILa{Vsw(kL)Vyv>K#;pk'
    '?)i-AGZ;JiJ|r@4D|>)d}J&6;#ER(+hJa-{8f048_QY5Fi_U^*iXrJsVXM_?<c@ts{**W7-(#NrWpnW@|&JBdm@)a12B^x|Dm4yu0wtX*F2YGsMM'
    'KTK=4b&xr>6|2t!eT{wPGOG4Vwlzzmx{Yu-2zcX<U&Wlk=A@>Q0hb`((YD5S<qG&WIQ%QCRi98bE%%BcB_28QIq9RbRrgmUkhxm>OgH4^BAt4K3G'
    'Fq9W`wf73`j}UPh)y&{y2b9(XDrL%krYNiB7Y!3n)FodG@L6Dy&NOjfdSm85-4U=Wwk4@8X1bsdF3K3W+k=(eJ83u*y`XdsYs=;}oc}H{0EXevmj'
    '=ObkQ0^_VCNlmdtW7sxJ<9W|K*KNzi*7-o$*=mwC~9Uyy>3ROC2yeh2R?=@zXQA1tvR+GG(%6PpGE?j!j=i$S#WawLp>vo@Hb(H${-EfkYJN;;%L'
    '{kYHff@yls>#!Y^6h45FQE`(3p5TfXSX0NA*jL%3`2TbK%#A^5%YNfO=cR4?Eh&Rvp0*mYNqfUOUaeGc{Sr|fIro9>#FBt&SU;wW)!zY6B=@`BUi'
    'j{N72#d;P?f#zqW<$vUJ;2CXif<4aH?AVmREWHal>CAPXm=bBW?c&$9E$sW{X0jrNc*FU%D3}ASV^Nl-cnSLgI-XxeH3PHm2M0~=SPQ(A)X47S(+'
    '-c!{SWezeMDQ8aiOivWbs73(f7XG-OFvdAql{#4WsQXI_`Ap|54yA6;+sRVDP1xYknqPWhH~`zGpALnSH8Szqp~vJySW`}ur<{o{L7vyXk?^zlf7'
    '(KVcE)r%8_v?5`V#6CU(a%R`pDld*FLKk9@{dygSIoSA)Fh}<#;nkz^Fl?jUMu7uAWvQIA@o!nC>Mw5@2#uk*pR2PD?KLcMCg@TV2r*>bh2=5YUI'
    '9h45|$w0@h^&7FyN)Ir%Cy{$|R^|RPssYUm;KVp^Gr20^_+v4w+t!ixj}5M){N;N&pxF%N-Qzcv{zRq3)Sm{DsHW7*<_7)BuBubKJjywf%RhL+TJ'
    'nLp{YXNUt@z{>(g%mV-*P)t6uK3dz527=O>av(){I9L~#MH=i75PVBiPJo2?qIs{X6p*hpMt{_oEGk2<G2d*X!ulcV2ek@RxmEw{G6uzFzUoYUqz'
    'w52@v7ArZRL!$1*0_`iL$(r?9CWA^VDg5yY!TyL3$kCL<9HzS*Wtrtgf#T#AQ`PB6NWqU)_Rn~cJ472<2WL}YAt1~J^sv@EOQM4HW%;ZFd&eze^g'
    'jg#cmW4TKUK0R&r0{J}}R?$x|3ZmNoGbX)4m!Bdnl$+pY!!y47cTOV9b@O%O1lYgWsL7~9{na!zY;&3}qn%Vb__4&MxoF{()`z1&wXK+n^!`?SY@'
    'VLC^-eLMjZ{(<-V6xU6v6wdL`O^ca2KV8g)lnCdPrcj*ChlF3`t)tM0w>vK|Kn$nVv9a#2*E|}bcIj6+DNp*>#bQ`u)7A*5`P>0QRPQ*LG3HerY('
    '<I_*0ESTJVWF25g$%dW)h#rjtbdrQJx5yg%?rSf!n9=ywsseVm9^H=@X04e??%qJ(E@<RCN&@FGG!~&*8z;_vXd#RTE03GhlIunnA2gf{NQRd7mD'
    '>1LY9C@?A#r9OR$<<%l1?1^HQZnjzi#UeERQSgONj9cYGJS8f=Z{g?Vq958o-T$OX<E&T?z=biFa7?8W&h<TLS8k^3<!X8xU;T6rDZfT+_@VF7a!'
    '*`E48Op}EfB55$n84bnbH~_!Yn&@@W@OkeJrF$e{J>PsdN*`Cmv@Fij13a$Z5&VIlS&(-yuQ1bQHC}@D|vzI;K^p_Ut^L0RGp;puH`4GS@>Zz9>('
    'GM#NLpxb6aZj{*mlkXW0`PKTCmZls>QpXv;!39yCpwW52Cwp7QVH(f4_5ZU@{}YcWh+^BIKn&)mATx5xL5enDKO>pZ?`*FA$=QTA>QD?p<`?@e6k'
    'MEc+V{Xbodfrt'
)

def apply_excel_gold_archive(records,today):
    rows=json.loads(zlib.decompress(base64.b85decode(EXCEL_GOLD_ARCHIVE)))
    cutoff=rows[-1][0]
    specs=(
        ('BFRC.NETFX.TREASURYEX','Swap ve Hazine Hariç Net Döviz Pozisyonu'),
        ('BFRC.NETFX.TREASURYEX.CHANGE','Swap ve Hazine Hariç Net Döviz Pozisyonu Günlük Değişimi'),
        (GOLD_LEVEL_CODE,'Net Altın Varlığı'),
        (GOLD_ADJUSTED_CODE,GOLD_CHART_TITLE),
        (FX_EX_GOLD_LEVEL,FX_EX_GOLD_LEVEL_TITLE),
        (FX_EX_GOLD_CODE,FX_EX_GOLD_TITLE),
        (FX_LEVEL_CODE,'Swap Hariç Net Döviz Pozisyonu'),
        (FX_CHANGE_CODE,'Swap Hariç Net Döviz Pozisyonu Günlük Değişimi'),
    )
    for i,(code,title) in enumerate(specs,1):
        points=[{'date':r[0],'value':r[i]} for r in rows if r[0]<=today.isoformat() and r[i] is not None]
        live=records.get(code,{})
        tail=[r for r in live.get('observations',[]) if cutoff<r['date']<=today.isoformat() and r['value'] is not None]
        if points or tail:
            records[code]={'provider':'derived','unit':'Milyon ABD doları','frequency':'İŞ GÜNÜ','audit':[],
                **live,'id':code,'code':code,'name':title,
                'source':'Excel arşivi; yeni gözlemler: TCMB / EVDS / Westmetall' if tail else 'Excel arşivi',
                'observations':points+tail}

CURRENT_ACCOUNT_CODE='TP.ODANA6.Q01'
CURATED_SPECS[CURRENT_ACCOUNT_CODE]={
    'id':CURRENT_ACCOUNT_CODE,'code':CURRENT_ACCOUNT_CODE,'name':'Cari İşlemler Hesabı',
    'group_code':'bie_odana6','group':'Ödemeler Dengesi - Analitik Sunum',
    'unit':'Milyon ABD doları','frequency':'AYLIK','provider':'evds3','source':'TCMB / EVDS',
}
CURATED_CHARTS.extend([
    {'id':'cari-denge-aylik-sutun','name':'Cari Açık / Fazla – Aylık (Sütun)',
     'unit':'Milyar ABD doları','daily_bars':True,'monthly_bars':True,'default_years':3,
     'lines':[line(CURRENT_ACCOUNT_CODE,'Aylık cari denge',.001,color='#432454')]},
    {'id':'cari-denge-aylik-cizgi','name':'Cari Açık / Fazla – Aylık (Çizgi)',
     'unit':'Milyar ABD doları','default_years':3,
     'lines':[line(CURRENT_ACCOUNT_CODE,'Aylık cari denge',.001,color='#432454')]},
    {'id':'cari-denge-12-ay','name':'Cari Açık / Fazla – 12 Aylık Kümülatif',
     'unit':'Milyar ABD doları','default_years':5,
     'lines':[line(CURRENT_ACCOUNT_CODE,'12 aylık cari denge',.001,'sum12','#432454')]},
])

CONSTRUCTION_SERIES=(
    ('BFRC.CONSTRUCTION.MOM','PCH_PRE','SCA'),
    ('BFRC.CONSTRUCTION.YOY','PCH_SM','CA'),
)
CURATED_CHARTS.append({
    'id':'insaat-uretimi-degisim','name':'İnşaat Üretimi Aylık-Yıllık Değişim',
    'unit':'%','source_label':'Kaynak: TÜİK / Eurostat','legend_top':True,
    'default_years':5,'lines':[line(CONSTRUCTION_SERIES[0][0],'Aylık değişim',color='#432454'),
                              line(CONSTRUCTION_SERIES[1][0],'Yıllık değişim',color='#468698')],
})

def construction_points(payload,today):
    ids=payload['id']
    sizes=payload['size']
    pos=ids.index('time')
    if len(ids)!=len(sizes) or any(size!=1 for i,size in enumerate(sizes) if i!=pos):
        raise DataError('İnşaat üretimi: tek zaman serisi bekleniyor')
    index=payload['dimension']['time']['category']['index']
    if isinstance(index,list):index={key:i for i,key in enumerate(index)}
    values=payload['value']
    if isinstance(values,list):values={str(i):v for i,v in enumerate(values)}
    return validate([{'date':month+'-01','value':values.get(str(i))}
                     for month,i in index.items() if re.fullmatch(r'\d{4}-\d{2}',month)
                     and month<=today.isoformat()[:7]],today)

def attach_construction(records,previous,errors,today):
    url='https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/sts_copr_m'
    with session() as client:
        for code,unit,adjustment in CONSTRUCTION_SERIES:
            try:
                def pull(request_unit):
                    response=client.get(url,params={'lang':'en','freq':'M','geo':'TR',
                        'nace_r2':'F','indic_bt':'PRD','unit':request_unit,'s_adj':adjustment},timeout=(10,30))
                    response.raise_for_status()
                    return construction_points(response.json(),today)
                try:
                    points=pull(unit)
                except (DataError,KeyError,ValueError,requests.HTTPError):
                    levels=pull('I21')
                    monthly={r['date'][:7]:r['value'] for r in levels}
                    lag=1 if unit=='PCH_PRE' else 12
                    points=validate([{'date':m+'-01','value':100*(v/base-1)}
                        for m,v in monthly.items()
                        if (base:=monthly.get(previous_month(m,lag))) not in (None,0)],today)
                records[code]={'id':code,'code':code,'provider':'eurostat','unit':'%',
                               'source':'TÜİK / Eurostat','observations':points}
            except (DataError,requests.RequestException,ValueError,KeyError,TypeError) as error:
                errors.append('İnşaat üretimi '+unit+': '+str(error))
                if previous.get(code):records[code]={**previous[code],'fallback':True}


# Values transcribed from the six user-supplied TÜİK 2026 Q3 charts.
# These are dated snapshots, not API-updated time series.
LABOUR_SNAPSHOTS=[
 {'id':'isgucu-issizlik-atil','name':'İşsizlik ve Atıl İşgücü',
  'categories':['İşsizlik oranı','Atıl işgücü oranı'],'unit':'%',
  'traces':[('2026 II. Çeyrek',[8.0,29.9],'#5290a5'),('2026 III. Çeyrek',[8.1,30.9],'#432454')]},
 {'id':'isgucu-sektorel-degisim','name':'Sektörel İstihdam Değişimi',
  'categories':['Sanayi','Tarım','İnşaat','Hizmetler'],'unit':'Bin kişi','horizontal':True,
  'period':'2026 III. çeyrek · II. çeyreğe göre değişim',
  'traces':[('Değişim',[-133,-58,9,120],['#aa6070','#aa6070','#438f8a','#438f8a'])]},
 {'id':'isgucu-cinsiyet','name':'İstihdam ve Katılımda Cinsiyet Farkı',
  'categories':['İşgücüne katılım','İstihdam oranı'],'unit':'%','horizontal':True,
  'traces':[('Erkek',[70.5,65.7],'#432454'),('Kadın',[35.1,31.4],'#5290a5')]},
 {'id':'isgucu-genc-cinsiyet','name':'Genç İşsizliğinde Cinsiyet Ayrışması',
  'categories':['Toplam','Erkek','Kadın'],'unit':'%',
  'traces':[('15+ yaş',[8.1,6.8,10.7],'#5290a5'),('15–24 yaş',[14.1,10.4,21.3],'#432454')]},
 {'id':'isgucu-sektorel-paylar','name':'İstihdamın Sektörel Dağılımı',
  'categories':['Hizmetler','Sanayi','Tarım','İnşaat'],'unit':'%','horizontal':True,
  'traces':[('Pay',[60.3,19.3,13.6,6.8],['#432454','#5290a5','#438f8a','#a9bac5'])]},
 {'id':'isgucu-oran-degisim','name':'İşgücü Piyasasında Çeyreklik Değişim',
  'categories':['Atıl işgücü','İşsizlik','Genç işsizliği','İşgücüne katılım','İstihdam oranı'],
  'unit':'Yüzde puan','horizontal':True,'period':'2026 III. çeyrek · II. çeyreğe göre değişim',
  'traces':[('Değişim',[1.0,0.1,0.1,-0.2,-0.3],['#aa6070']*3+['#5290a5']*2)]},
]
for snapshot in LABOUR_SNAPSHOTS:
    CURATED_CHARTS.append({**snapshot,'name':snapshot['name']+' (2026 III. Çeyrek)',
                           'labour_snapshot':True,'lines':[],'default_years':0})

def labour_snapshot_figure(chart):
    fig=go.Figure()
    horizontal=chart.get('horizontal',False)
    for label,values,color in chart['traces']:
        text=[(f'{v:+.1f}' if chart['unit']=='Yüzde puan' else f'{v:.1f}' if chart['unit']=='%' else f'{v:+.0f}').replace('.',',') for v in values]
        axes={'y':chart['categories'],'x':values} if horizontal else {'x':chart['categories'],'y':values}
        fig.add_trace(go.Bar(**axes,name=label,orientation='h' if horizontal else 'v',
            marker_color=color,text=text,textposition='outside',cliponaxis=False,
            hovertemplate='%{text} '+chart['unit']+'<extra>%{fullData.name}</extra>'))
    fig.update_layout(template='none',height=760,barmode='group',bargap=.35,
        paper_bgcolor='#eff6fa',plot_bgcolor='#eff6fa',
        font={'family':'Courier New, monospace','size':18,'color':'#102536'},
        title={'text':'<b>'+chart['name']+'</b>','x':.025,'font':{'size':27}},
        margin={'l':260 if horizontal else 70,'r':110,'t':145,'b':150},
        legend={'orientation':'h','y':1.08,'x':0},showlegend=len(chart['traces'])>1,
        meta={'labour_snapshot':True,'reference_period':'2026-Q3','auto_updated':False})
    axis={'showgrid':True,'gridcolor':'rgba(44,66,83,0.2)','griddash':'dot','zeroline':True}
    fig.update_xaxes(**axis if horizontal else {'showgrid':False,'type':'category'})
    fig.update_yaxes(**{'type':'category','autorange':'reversed'} if horizontal else axis)
    all_values=[v for _,values,_ in chart['traces'] for v in values]
    lo=min(0,min(all_values));hi=max(all_values);pad=(hi-lo or 1)*.18
    if horizontal:fig.update_xaxes(range=[lo-pad if lo<0 else 0,hi+pad],title=chart['unit'])
    else:fig.update_yaxes(range=[lo,hi+pad],title=chart['unit'])
    fig.add_layout_image(source=CURATED_LOGO,xref='paper',yref='paper',x=.5,y=.5,
        xanchor='center',yanchor='middle',sizex=.65,sizey=.8,opacity=.15,layer='below')
    for text,y in [(chart.get('period','2026 III. çeyrek · Mevsim etkisinden arındırılmış'),-.14),
                   ('Kaynak: TÜİK',-.22),('Grafik: Mehmet Faruk Bekir',-.29)]:
        fig.add_annotation(text=text,x=0,y=y,xref='paper',yref='paper',
            xanchor='left',showarrow=False,font={'size':17})
    return fig

if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--demo", action="store_true")
    options = args.parse_args()
    try:
        build_curated(options.demo)
    except DataError as error:
        print("Yayın üretilmedi:", error, file=sys.stderr)
        sys.exit(1)
