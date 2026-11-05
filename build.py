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
CURATED_LOGO = 'data:image/png;base64,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'

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
    earliest=(latest-timedelta(days=365*5)).isoformat()
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
    if chart.get("daily_bars"):
        fig.update_xaxes(range=[(date.fromisoformat(earliest)-timedelta(days=1)).isoformat(),
                                (latest+timedelta(days=1)).isoformat()])
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
    labels=[("Kaynak: TCMB / EVDS"+(" / London Fix" if chart["id"] in ("net-altin-degisim","net-altin") else ""),-109),
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
function applyRange(){if(!currentFigure)return;const dates=currentFigure.data.flatMap(t=>t.x).filter(Boolean).sort(),end=dates[dates.length-1];
const start=new Date(end+'T12:00:00Z');start.setUTCMonth(start.getUTCMonth()-Math.round(currentYears*12));const begin=currentYears===0?dates[0]:start.toISOString().slice(0,10);
const values=[];for(const t of currentFigure.data){for(let i=0;i<t.x.length;i++){if(t.x[i]>=begin&&t.x[i]<=end&&typeof t.y[i]==='number'&&Number.isFinite(t.y[i]))values.push(t.y[i]);}
if(t.type==='bar'||t.fill==='tozeroy')values.push(0);}
const hasBars=currentFigure.data.some(t=>t.type==='bar');
const padded=(day,offset)=>new Date(new Date(day+'T00:00:00Z').getTime()+offset*86400000).toISOString().slice(0,10);
const axisRange=hasBars?[padded(begin,-1),padded(end,1)]:[begin,end];
const update={'xaxis.autorange':false,'xaxis.range':axisRange,'xaxis.dtick':currentYears<=1?'M1':currentYears===3?'M3':'M6'};
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
        n=write_curated(ROOT/'demo',demo_with_fx(today),[],stamp,True)
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
     'area':True,'default_years':5,'lines':[line(FX_EX_GOLD_LEVEL,'Net döviz pozisyonu',.001,color='#432454')]},
    {'id':'netfx-degisim-altin-haric','observation_days_only':True,'name':FX_EX_GOLD_TITLE,'unit':'Milyon ABD doları',
     'daily_bars':True,'default_years':5,'lines':[line(FX_EX_GOLD_CODE,'Düzeltilmiş günlük değişim',color='#432454')]},
    {'id':'net-altin','observation_days_only':True,'name':'Net Altın Varlığı','unit':'Milyar ABD doları','area':True,
     'default_years':5,'lines':[line(GOLD_LEVEL_CODE,'Net altın',.001,color='#432454')]},
    {'id':'net-altin-degisim','observation_days_only':True,'name':GOLD_CHART_TITLE,'unit':'Milyon ABD doları','daily_bars':True,
     'default_years':5,'lines':[line(GOLD_ADJUSTED_CODE,'Günlük net altın değişimi',color='#432454')]}]

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
    levels=[r['date'] for r in records.get('BFRC.NETFX.TREASURYEX',{}).get('observations',[])
            if r['value'] is not None]
    previous=dict(zip(levels[1:],levels))
    fx_changes={r['date']:r['value'] for r in fx.get('observations',[]) if r['value'] is not None}
    points=[{'date':r['date'],'value':fx_changes[r['date']]-r['net_gold_change_million_usd']}
            for r in adjusted['audit']
            if r['date'] in fx_changes and previous.get(r['date'])==r['previous_date']]
    if points:
        records[FX_EX_GOLD_CODE]={**fx,'id':FX_EX_GOLD_CODE,'code':FX_EX_GOLD_CODE,
            'name':FX_EX_GOLD_TITLE,'source':fx['source']+'; '+adjusted['source'],
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

def fetch_lbma_am(today,client=None):
    client=client or session()
    # Public alternative feed. Do not retry GoldSilver's access-denied endpoint.
    url='https://www.westmetall.com/en/markdaten.php?action=table&field=USD_ozt_London'
    try:
        reply=client.get(url,timeout=(10,30),allow_redirects=False)
        if reply.status_code!=200:raise DataError('London AM fiyat kaynağı HTTP '+str(reply.status_code))
        if len(reply.content)>3_000_000:raise DataError('London AM fiyat tablosu beklenenden büyük')
        return parse_lbma_am_table(reply.text,today)
    except requests.RequestException:raise DataError('London AM fiyat kaynağına erişilemedi') from None

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
            previous=None
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
    failed=False
    try:
        fresh=fetch_lbma_am(today);prices={r['date']:r for r in fresh}
        cache['_lbma_am_source_only']={'source':'Westmetall London Fixing (AM, cross-checked with LBMA AM archive)',
            'fetched_at':datetime.now(TR).isoformat(timespec='seconds'),
            'observations':[prices[d] for d in sorted(prices)]}
    except DataError as error:errors.append('Altın fiyatı: '+str(error));failed=True
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
# Historical workbook inputs: B,C,G,J,N and Z,AC,AD, with availability flags
# for Q and AF. The original workbook is not needed at runtime.
EXCEL_GOLD_INPUTS_B85=(
    'c-oZx$&zHbvSkVWON{Lbp>MwBQ4DfWH44f>X666aUYl`5L|^C;m2|&k*W6tVd;l=G=gt51U;m?|lK+$Rzx6-<{nuEtj`b*g{u`sD)gCMVn~ySU'
    '{r8_%N6P>Hljq9qfBySF|DXT$zyAGy|BwImfB%2t*Z=wNwN}oowR0`wv2)W}Bd`1@rD?I+Hf!}Uey;WZ`@jC5-<B#`YNnj}ezaWwjoEwMkDB<d'
    ')@o+0I`UU*Rj*a+Ob^J9xo6pr={>fQ7HcIw?~Aosd{-VrJL+1i?yi$#hpnv6QmuXWS)J3Gd$;zOg_B=n@5fq`(^}VBleI=&pVwNkR$1$jL|$u^'
    'o*y}PuQjLF>h-I&wmBkgeav1QSIccZa_76+?nP^r+0!>`mE^TXPem-Xwbq;~`3k+ivTONjo!~6}T^=**tkx=ZJveEtRR{YkYqzh~3f8Kk{n)$w'
    '8@ugVA9EC+RUNEVhtF!}`gyG$?6Ae=4UXSQdwq<`chwzxJZJUiT7$KUu5UlI;J=w$(ONxTk9d}~Qr%yzHN95rtv^!L#pT^|d6XLbQ28#M)ck6#'
    '<+Vy}Iwam-o;kHgPn)%RYhJ62uhxRMW?njReGK0v2{9h6`@3dA+~mH4w`TsZR?%`^tLBvTezZVZde@J>{9Pq;kJ;Dx#ah7=JMa8ho3-jn>9LE&'
    'VD>%g2<umCRrBiRF>4v|MDImBv8&d~+T`1L{kc|%8(rkK03CbHrtjK=wc1`jud%*b%koBi(7PWFk7<b2QuB8e-=mgctv=i5wJdM8q;q`?-I|1c'
    'Dx5ldOmiXXbIIkawL;!h3HXPksowT%tvn=uSZnYQTbi)!-)rR*-<2e=x|oW$z4VEbJ*HJq8)@YB)mr(;t|^(C274@<_sH3&p8Z{Ye)nA^)>?U4'
    'hjCuwuO;21wOZorQyxc7?9XfY9#@`eNJBTD*W$ZaDocv}M}lX4x74uO=!mb5@?SMK2~?#1LyxJk)|l(7wWilf;>O34WEm2u(z30xl=P5Rl5qTL'
    'rFAwaGop$g$I=yS?bQsC{84%bUoB<cl9EO*@)jqTAfERIyR<HcwJlTo`22pXH&Cn<A%k8N&sIGpuQhV^TCIMyR*i$tlFwy}AC_LYBu!hawPTC<'
    'yQNyF>zO-}N}@}@)wvhcbt=d#tyR`nYaJ0$chgrOo=X`L^&?wEbX1Uzv3<3cc}w>sUD2=|WJF*zq1mM0EN-;c4wc<MtW_^jU!)!NG5=(}VVpEC'
    'C94i8n2xmhbE%{Khs>j-%bF!OwEyB&*Hrc!cm286F)X(%(~in7`ZLXTq1KWt4et7Gt&sF9vqc$2vIx8EaMY{Ts@vQ(jyC&it+>XCC6NYuTuWLb'
    'HAF<~=jf9qmlplJmbHIM(lQe4K6`|INP5`-!xA-&`ajp&p=FWu1C?J_k=EE;=|ih-uQkgzYgy8Zr{ML9YHebHCS}B0L)&AmV?F=5R<KvzlJw{s'
    '4Bk3rXm)>>EnYb0tF=m4s!bAytxQbGh7E43xB0vzyIHHv{dujhE_;_u#(JJLONNT?d5&EN8Q)dET5ANaO2Uh&^jS^Loj%J;)oGU4d3?3h4E9=G'
    'Mm<(wTJk&|tC%Bjep<oj`(~*X_F&cvg1N<L^6I4f!`I+p>-^VOOC9D)5~(-@GF3~?dOE@M6NYGW#U-ZpajkMLaU`r6nGtK~k36=`=VEU|tiQfm'
    '$}*zLB#~<e%hx0$bq_?>w%(UFyT4k=5<=9c^ggUvDpF8}mzI(x0hh|?`tGZGe3kUZV<QFe4toeOeH_nOE^*CYt!2)ta_Fi|tjz1t(aRm;uy!+-'
    'mN~sg>CVq<`4Y=+EP%MG@*HK#!48t7jz*-szFDfKa9etjtD#ew_=nk6v<v_#AiIq1tIw(y5LM@bgwYOXlWj7kTV>7_JB*`^zgKEUlgZ8&YfdTB'
    '*~A%=6l;4aSY2NghTxxTS(WKL#tiSZV%^KmDeQ12JB+jV66IxmUaN;VT~iB8RGGFUgJP|JSWd&IRYR5ixz=%HB^w^t+8kW2V$sHT)on+k=l<1N'
    ';mA&cEUCk&6LH!lWwe$fEga_fYAp*J-Pe>Z#8Q$=oZ(r2*I0I@TKew0%&5{5Ixn4z){;XYmZ-3VHs`f=#HXaNKdu$AE?K(L(W`5%CYMU0w8wd^'
    'YJu9yS8LVauZcB@oR`R(sEiSNv~cbzeYIAL?~;%GD9J)Xev3Hk?X%i!Uo92#hJd1+gT+$45z67SBoOT{iub-+E98xyjGoYl&(bY)T^u|Tv7mb7'
    'V9uY{+J_}%@MdIJmA2n}k=L?XEH!<#mSbJ|DSghPp@3wW&MH?S*2-S1?eEsg!S;1XViAw1O$$Hp$nairju_SWYOTXwv%~fktE-76WM29S)wsuq'
    '!?lz?ujR9EOTu0P-jWcbzHp5kqPZ)=5q~b_m)+&|OL{fGM%}zV$bxW3<ZRckmh!`HU51f-S2!Zc<FU=9&vLA|>|m}xmkNeh5%ILiMWC@JYmH^S'
    'SlU-hg#xN_arAt!=V7vG`GDqNp=^1ooBXg)KW>Js*^#Wi#0&%?mgT&*=UBL8&R1*sP3x#TZk-rQPMH*}JPXzer8q(a{<)Ojbe~!JE5QrBZ`dO7'
    'Svs(=10yE;=UV2gK8n1bxnKr~2AL+XXtln$?(eUK%vI7i6G5W~JuprmzP_45jg_!P#JV4s3J*gTM3ec>yBVF3UYyiC(l33rl3(-aZR`j7SRX~S'
    'Y<s0<uUZGSzFNwUc`n&za_;#k>8((WT5FB>wY2rmYn`)7;ssks(h^vybH1&$Blq*?T7H64A0rECzzd^d)^uOsaOy{<?>9?@9F@{p(lufqJ(ffy'
    'sDxx#=Q)43mQQ}k&dms^DgR>N2z#mFFlFfx4%F>_Udk_L_bHL=_`VFvn)Loy3SI4N#{O!le!fcEY8<>IgMPcLS+(s@(*3*7^2=HJtR3l)flQUd'
    '!&1A2G7PWh&!x-{OKWukN7`ZTMZOA$b<{*I->qbB*j8Uov)R)p0PCe3=9l1#5MC)C*Rt&`+cC+?ZG-@I2ctL)E61Siu)b6Md#!P-@3nb-;z=3J'
    'GPTe)eAYHMjM~0hsl*Pq-XRiC#+#f(%p$%D6WqzW^wnC(ce%haV~}_pRwjnZ21g2C><Z}qwG>Rc3ND1y7i(>zBE$t3f%<HJGt9rS)91COKMS$`'
    '*qD+sD@non@r>hcw)NFg%S*|_*E=RoFca~SoQppz`Ljv{GX7j^do8^lIpi?Be16!h`Q+D*Eq=F_Mf}2#P7j|}-sr_(Eh76(pP9c}$y};Oi-y2Y'
    'nK?H$%VG8EWI-<}cq@Ir#IU=!qzm~ql%b5b=Aa0o#%~&h_|;n7-=)K<kBMKW7ri9otq&vYqr)m+Erq0!lTZE#bvRSku~iGL-eRevrT<)N#!}Mv'
    'k<ym{)E$;Mtzz$~pX~mhYniuXMUJY&K|V;u@^FIXn@`^9qaqP9KdxnUp|whudF4GyM9*{b!!Ca7{mZqG5KDI_o+k#Z3&nsVPq4(TPUp&JY+tQq'
    '2_d@=s|EvNchUtCIjp6hGI!<gzRMzF<bon32)nvGoCgtR?J+wD+p%}QS<BqD#GbO%!~rYm?U3n5`;6{*P4CF&B3EdAUd#8WgOQb-<QGnv@Ulj_'
    '<2Cva?=cgL{bH>hYsn1|Q%T6kRMKOTRP=e}?U#+7{d+B&@iK;R>qw)_JvM21KOwyV%R!qGf2}n`|184G&G}84aZ5f=FCkJLv~C39zFEusCF>ne'
    'MS4-&A@vrv4S6odJJB8zJM)jV;vSLE57HJZ7k$j5n^#Aa9%!xh)mj!4t7Ee{6qO=nJ-pViP@S`WwbYEI9KpaUO^pnHDpnhgAc-YP|7tDRYs+jN'
    'l*PzsD+Oa$esO%3x9AAO7i+;@;!auX4?I;pfStOI%eQE|xpjgD`jP#4Dckc~AJms6&Z?E7j~%aZdW(|v`>VC8*OG#r`WA77Tti91X(L=FtGV8F'
    'H(#vPd|XnDvTt<UWpsU^JT%)RHU?$KeYe!`XKjjXut4;A4dE36I+qB{$H~uCKd<GJPpAlK9&vge)Cy!<;XT=eyVmM|tYyo2CrRZdpGOO1d1aEX'
    'eByI02Z+B|Yo0x><oXeP*VZ0G2}SNf>^i<$%8V+nM8}m7q<P^d44ZI$IAzd@34OCxFlv*qlJU)2y^}ACwIUfMPy4GaS`O~qNc9Zjb~)9aCH9An'
    '=NXSNWQ4f*!&)n3*B0mWBDV~^Aw4apqwyHq(LpWr?4N4|*Y}n%OOQ9Xjxu(Johg6T#(Mr@sbG2;rx;5e6|~F7Q^4_bkg|^uU4JgskBHdI!Gk3m'
    'f*7OYKYGySh*iEjEi<ZiA?~zCUFG6uvIVx59MlkFjChRs^IGNzY(B|8Jd9c?h?qOLD~I+k`<u0FJ<Adi>%aj27AXT6VLa>RtVl0MU;njI_E)Ve'
    'MS%^ss&$5*X7~qTiKp+@GH>C6iJ?#$RgX#D&<=C3y}jx`mojTrITQVXtt>k_+oJHnN41*P3bQMJzQmA%MPDQ5oXC$}WonPH!$IhlHk6fov(^me'
    'YSipjp0#$O@7nfbBgo%eb@;_vA#TJF^TAf6gpfRx0>h|Iuyo4)zFEu6Re358d}>Lq+~v|{f0lh>?eMGPau8@!Ad)aZwALO=0@Vsgc8Ndh63(N2'
    'Udz`g4+HNL?wZu55w!99ap*=;uza<aMZ}&Gp=?QOZIYeCfsGzUYTx;*r7R-$(BX!3pE8w(xGO|N99Qr4tF_Es{KXdaF|x4aeol*<Q#&eg)4y5^'
    '?vgIl_Qu0I9HYA83Gb6)F!?dZM&0w{QjXndDGB8L*aU{<tT(>MpXDg{AXoC|T03lK$>YAMrqFT8Xrsm=yy9d(PtH~PYONASUkRg6mcqJEwK?!f'
    '38Xfk@>fgMvqfS`nC@b5y&&<Zqw~Y2lFZ9cKrMY<Y@DN)HWN>YKjl~~(l*)Q`5tSPuhu%(m^TFxUQ0f*oHK{0ii>}B__=+x*z#hTY{OwNxrocJ'
    'T)$Z248QO%*4kc6=J%52CZ9)s$3heqKCC4de2yJn<6nze2Wb%s<>LY=7HpDVHcO~7`cIqXlJ}qQGRIvK<s?k0O?G)bj~d@))iF!?id$_z!xN)X'
    '&E0uMl_D97=*U~oG}c#Z^;iqTIeh*pJ9#+g&v!brIBWWDv0yQ|`+7W6!g$H)(d(=ZH&QRaV(rVtg2&{%B+9t-l5}mDIo-Q-Tw-1p)B1dqC2mqu'
    'UREXch!mVe#A7?9-d%-?<5<30ECkOSeu|Rt(!5e8k8o&5^I9d`sz28%@nJoa+mLjY9Y#?j1!4GAj$Vd0`o&`PEJjvwi!Zi3(kgWsq0nfO=|?2~'
    'xmYlnL_J$eQ(xA}HYV?~Q>WP|Me|y@e_kuDl7K>Wug8|f!vl2^)s||Tw#>g+E2PCvq!x!(T6oI#t0?pKSDkzOYo(AC@)IcfkTpj=5dk8L>eW{)'
    '7C!A?i`8_tSSpcvGex1e+aabEDGq77`R8Ieg*wugi6gs{db>9{eId1_RKoFCt3@oQd|s?w2euu<);9N)Eih7ine%6|1@>2qg}B%l%=(<dm-QCh'
    '#+j88-qrkSF^dZcpJW457a;S2Jhp}PY8HyJ4Ziy_^O#oCdh*%zWm5jF9G9Ec8GAA%eYMy+i^=JxWQ{MAS>_fMvL~)Pll>Wrx6F@=*#XgF9gBb-'
    '2r{7wdy{7=9bCLvt6wcvuEi+-kb%GDTa(y!ZNrXkrBNbE@#kU|7+K<(JdTX8P2FE|e;s$~{MVqE<D11=EQT>msV0j}5_96LO<b+S+x+g!%x8JX'
    '-xv-9?6qm;(XO?aJcnT&H|^5wPYn_*7PmPi^Sd6pK1XX2X^6$P9p;(C^!anK9gFFV<jLqeFoSRcd8V?-;sTB;`D?MzOSJQKv4+@Qi*>fVq(yKB'
    'TMvu4ezjUK+9doen<D?Cm`7Wc_T&memp43~Zx$<OpWNZ_q3QX^n>b(QpysUMajj1oEEe;17MTUozk1U8G6~(@sFl=ec1@*!!;<=QF^i2-sljyv'
    '8WzXk1`r{S83l!7hcWf%Y9TiWf#ipbMXsR8hos9(tF=hgkv9EmwP3ZKhYaH;Pl9s)L|q%7)|PX1*J6^Q9~R3;4M;jJ+Khrbwk&PuBVVhvkZFIe'
    'R^kqbKvQ)IcSy9+0?TT>EZ-=q{+rcmeA|%M=5Q-U_a@&U!%VB?8D8|RU#-^SN}V!KepI#V26d%+`j3d9&m3;_pNj>v)r!KCde>qy7iUBRwb%;V'
    'C2tE4G4kit)>&=JUc}bVspD4?qk-GJ%g}d!E@jbCT)XMWxz4&HlVmse!)!YCcAmi>tA%$`nk;`YZu^JvA!k^Rz4KiuLW*_vuNDhdYuX7u7@}iK'
    'RSoxbxShHDzgaC<jXceCe-;YMd8y8u+SxL)Mg>Dne=HW)NpF=jUa4M8m#7OEUMwPbHEN{)TrGshN_|wlAT1`dmbMra)?=@zn%`-EwU}jxoTEmq'
    'JYr4ne(`%aSikxa$bGC|t!7S>w<$aM!CHIK7F#Ba@~EMvN-y7int846+K7_z+KV2Lmg~CEOvk8QVSEcMN963|a-j`cE2LnlsmO(1==Pv5I#Vv@'
    'QWAN4<*Vf^J?Ivyzyav%s;;qbmqlj8W$>Gw|1Xxa_|QGhl<c`?Nu)%dXVuxs9PHg+*WK;$_j2aALCY{r2Kpj>2`!Z*cL~TvKg5g@%fFX1%k@e~'
    'QNm42NS17^gId=AY31<pTpB5tdwgEbH`{4t#^+pYcDoL-W-m9bO{Ep~H_L_NGBQT-gN$=__PPf@7c%YPZMZ@CkL6C|w9bxl?gx#?S#QHF%H3aw'
    'zZS(k{j257chUriOpdP84AEUcBUx8#1awCEZZ)&rSd%DptJ+`JXU^n?F)c?OScK=ioOH+M<$SRd|7A)<$`I)^;g$#0C7FCmiU>Cm<u8`AG?}_u'
    'Jln)(C3eeln^7ifWPiwx!WXNp__#3$0Vj#g&ih}6IN;;LQtf?y_i;Ox>s`y?UTCj7iv!7>vs^jReD}Nlxm+kO*I`EKp1w|gMtacs#9rG8QO9!i'
    'qrxf2k&$zeawD3i<>VJhUr=~Qdq|CRBqem;EN6K_k4&aYN@q9ZY%~ln@w2Rea3+^)-Tq#!pQA(kQlXY*tV;g6+9s2?Yq8w??&E^%8a+qA8vLV|'
    'Sq$v5IW0E~z);=j`^9QzI;q2oG?A+<i@y*?-#$f&JQqxj>gP{A>&NB7q1nJ0&>pHKHj2Jbmact^m3xHRXo&e@IiyJesf8yYgT7FFj~7??QZb*a'
    '4(D#)Ef-pB;AyxZof^~(wm|m#m*Eea?20}2#d0-ttghm)251iBfdNO4nTxr4+Sb^AuhzntD2qJ5B}l^HdJq-PxJ|@OdzzMYI>-Haxv))SoTMId'
    'y_PK#(S6`Fh?N=3xnTY8<-!bJi|jCkE&9D|3u4BZ<p|zsF^c@ZSS*Ya9X)loe3@RvbODE!TGg@LJza+V=VGD2s_a9&Q&`UJD0*sKu&k9R$=a)Y'
    'x11GNBVRTNwdq#Z9_U7HSPE>~A>2eF^t81J{b^Z3ZZ@Z}O%{V(1L^eUs$YtJ|L6Zss~8{23?$Q2C+UQ=tYp3D<(fX!n{D{*CiEAn1h7W4Bok{4'
    'N&xbrJcKSTkxPdhzx<#UR#E~y;jt}qG0_PRRBOLt2#YYNU*vG`WKzhq@?S`mf^wI@(1%N{I}Ihe`HpUn6D5)IpsAY^#VIBhB)WOIhDHBwzSEUC'
    'N2${Av0}?fMQnYrZg733X91~w)D13&w&{$7oevIm*aBI01E1vPCd}RZ2}OTt%QgC%$ZyK(op;n`Sa+2Mp$_|X;1*=Jw@{4LReWo>S8^1Jtl<|d'
    'Qb&Fsl)ch)(ZQnn56QC|QlT`vjz&%zWl6GY3*9<67%0td@wJ>ac#$eN5y{=^*_C$$A~%&xL?PyCcjR)A8!+7J-r3TheB@Bk*@odt79dY*)H9T7'
    'uL2;32Sq_Nibl5b6FCd#E>pcx)Qb5dE00VD&MojQj$cu!`c7|U0xfm<ILT+`MWlc%r=x?~{vE-32)bX^rztg>oB)QN4d}L-n@L~$gDq)mF}>;<'
    'P~srdLE%s2;`x=G?Jr?LRcU@b1E{%BfZj@I&2{z`mo@?Wk;94`0x*clj5{)iA2Wd>axJiUHA(YhPrs6yF7I4!uM;;q<TuIK=@qQc1lQRY708RT'
    '>k_t>OEz1Zb$uJFwI2k<iP9a#^yG^tB(xrPArm8d)NuSx@Ay9z+!bmWb#F^sd{D;g<hbum*Eini?IK>3w2Nm)$b^6dNNC9!km8)ouLR3^c4fH*'
    'u=qq}OFw!Tmwmdnc6PV!Jqh}gIv8wGJ7K!I?mmfhR!J1g>ko9}{gJ86#%+;y$g)}hIPt*-Qr&DL+Tiju-DqLQJjXU4TMxnY9-?$7SdRa!Zzn(J'
    'rzf$8r8|ya6cN=xsW^lrujyTLN=xLHOd!1i{b(cy%c8dacXB-x(FC0iVWr;_Zvy230liw}9ZrMh*_RxQ2dxr*c!U0N!rOQu!!IJ;>4W2cmnr@6'
    '==S;&ph^;oe&~+xIvV)4H?Ym5-pbqM^$C0qZ>iIrU}?7RUVAyuaMHAPE)U2ww=DS?TDQ-ztDkgfEtIM~JcEGExwc8-Y3h9p-cZ@|?k$FuM*c0Q'
    'h?RAHtT6yg)R|FnOpl)S-a^w_>&a@LU<Jqt)>&@~(2A1;zfuY_iON8&?zo6{q)QAlsb0NB^R&2Jr1Atuib%-YqmY`G*i1m<q)0aH`TJ#F@r6#i'
    '<#zrHloq2ygm!N!$9<lb_Cr^HKZr@#deNW7{jF3E_3YY4*&}xZ%L%t&JHxB;HfW|=A>B^GZKwH84tr%3L*Zr8y`&~a9@ydtLtqu|$Q`AW<fgk>'
    '2-t*8FJLfhi!i;E>H4w1POF*qevz^N(4(LaRF{AX5xss#&rg5opWu(mdFqKRyvgw|>KmfSs5xaJ;S%q|&4@BP>64PY4<*=5s@;*AK}zCiI;5|y'
    'b%RQtcUbFqx|&Z?vfQluZ3yNiqkPk=BFVbSimNMk1miVq-9(|Ag1aL%`WQ!WH@`uy<(;6ko{Tr2uOs#PNLf8YM#bo#&oVR<{g$#Kk~fIg_6Wr^'
    'oV0B8vb<62WczAZ6I4>m_mqi{Ms}KR?>oLc#^5$e1o=5Gu2OEELJf}SF-!CP;x@%4MGC%!H6IKrVJ9(F2-{Q#=Je~jqZ<J}sq2~yqawPsxMH!#'
    'EjrO#r=_yUwxDYa0KHTiO2nMfMiGIkh8XyrRPbhY*$-ta*yqx(k6l77?SPQo?Tu7R01)U|rumXFB@yJ49ole0CVVEkcYG58o8e=65ZB5?-GD7S'
    'J9N2FYjLs{S+NMvJ0lJ@>|c-->4}ph@5b$rVSM-UBd#MyIDv*$I9Qp&jhDDWqDJHA&g-45?*Bm!W5rDt-DDB-bYd_NoaOF4hNUQN4W2>TCfJBQ'
    '_Rtn1N8|B~MCu`!!RI8MW>H$S!-Z!i>#aK$oA=%GIELcZsWvxXi{D@a*O9~q4TWh#u}I5q(<>1^(FM@^rN>B+(O>2q$giYIzC=yOAT%kNX|(r*'
    'c8aK0b~Nfmsu_iJ+Hfp@l8N~?m5DB<`$R94gv<fJOL1%EOp{ikh1m35&z%hR`JxAMtzKL`+TJIM1%Nl%2rRcSo!gxdhzOME(&B^06<tdHBe+c%'
    ')#86g!ud&eQ0o@~eYrK&9_S`?WwfaFidg(_^x{PV!!cdGrCS-$CL;|!S50F+z0>3P9=2sl1<kG#|I}%hsBUO?FXH53q<cCiSx$JQhA!08G_EMV'
    '3-Vi@N;b7^M?>zk>ocm8H}2e(uD_#nv1Xyg76B?D!HXZ(_>#(eKF;lYqa@~{g272qifDiVz>bx}+QaEDzrTfgnK2-Cr3YYZxk=tod=|PebX%QK'
    'efHcMKwq=Cd4<q9jpkZHZfG+ss?6weM=&^4-t&~Ohf%Z+uJechIsW~BfCnh=8(ag=(_X0cj9;T9`;3Z>z4INpU`+C<l>UdVZ-fKnzWP98f5W-%'
    '*ZySG)gFEZfeSKsQ-)VQFc0&_@~u~JYa4yLS$dAKXt*|)x*c5mYdg*pcopc`IxSR~CLRuMZiI)%zNIvemLHXVtPyg$SHmkgoR8%zt|S=qBvm;G'
    '$(OO>twWanpUN4Cw{s1%HQiA?9GbEL64{e|d^Z4PyHRI@>5ZyXq1>`njvM1hFXoMOeNNQ7hJ3j{)qRxxB4mg~BVh_~UG<@ogMcX7>TRnM(K5$V'
    '#HlH~Ag-X-M{AHB$#wTE!l;oxSPjr6C+GrW%Jk$^WOL1rP13Q175ctO6d?xHk|CleWxKN7y^UkiNIbL?ZHMVfch3sQNPcjYaEQs~cD9Mmb<-#I'
    '`kwfPt{;#by(1L@lsH5(4u-T0xM29sklNH+_+7L&QiWRA<^EQ24X%vj(lpGX@k#HzmwK5pUG~s$C23?l8(8b!+BhP#jW>EEZDf@0_U48b>R<Ho'
    'WrZwm(4oKEdkkA<P_+@(HZTKND)IcM`?l*&>-|J^l7j@XABW7>2O3-=NZQ(_kL17|y)b3;gEZ=Ml(gAH&un9-642ZHey9Z94}6JCv<_%hlI@YA'
    '0chP=zz0{7!s;T64>)6@C5W14tu*Kw0h`qxv`!#ITX7JduL4gowVDIRQiSf9<ybMDHVb`S1c1oXjd=5JnwAaa!o?>codJ+ZhH$Xe1bDXuV;RO?'
    'YIk9vgG@=&tBgE1gpeSh%c@!<x=;h4*f_>RkJtr}@_KWx{6K2p*lcWUU}r<L-EOm<k^u=|-Z9J~13`~IME5f6C*1ghE~tHS-3(+*RU2>?O_qnQ'
    'wERKvJVL7DjTA2}1D@Ksz~wFxU9bD@pXW*rNIc_Ptb|v994T1GBdXKd9l3aeI85zi2nalo|FyypqZpGJ8|ptSID$PxNl=O?rIsd*ELU*j?%jnQ'
    'GrE3UkJX`33Y;Fqd}C+v5bU`;DS=2q;4YlON$gjSfV}Z!c&ssQ4>HsPt^E@6mSzMtml6HEvj9c6Px++ho&;+I`$#fr<UEbGr1-N7{d6-o1W&Pw'
    'q4y(%ASF?0kKew!X^)WE2=(U%ZCJgzX0CRTu~zt@9Q&NN#?uLQftuaI`D{o17oY2BAz*4sKgc3fGBBh5D;!b0MCpnIZg`zx)ch{i+OwEOFK!+K'
    'xNO4SN9ju(`(;GOoZf2C%@HGAZkE{&;-?*LDQn)5YUdF)@tZsW%Nm<}dxum@Mz8M9;yg*o_9X3OS0a1TMW)G08hS>_)m(Q3&#UOg@V*w=lQQVx'
    'pcnBcqs<Qod7cCdfi9v(ZvoQH&4UPv>E)8U4`h#*U8y!p7IC;EVvjgcswb@~+7GG(HVNOpQ%x8)n-bbIW6;Z7w>NnyDET99NuV2m<WD{(hG!!X'
    'yqrJi(&yh(glDna>;Z4=@5phKJ9<YDG*ED;RHf2kgB3k3Q>TTS-koz~5$A<-Le(ey(uCsGn+U_Z)Ei2d@kCnIaHj-k4PG{LP>Nv&*{v^6O0X7S'
    '-!lTTG}TG|7suTD$E`)4P-ePJn9xkr!VM0p#ZkKcTR&aj2+Cxp6yO*aMIu8V3}zHIe~Aph9l?46Ge9K}4y6b!+4v8uH2N@8a>xCXAYzH`nDiEg'
    'LJ;8?04Y!m@=Q_U9o<V!VaygMD2vSdaYA7iUPFq4{wH0k;-%U`nrE6aDJa8ub^VwuzB_vHB0=EXQHw!xd<R;U(_OL8?qGtftQ`kyK;c54p9G!c'
    'vFw!?YVv{9I(MiGr<;R2G^j-lQjB);WDZR17b)O8WyG92Trd}g0+`SdxafE9FOr6o4$4~J%Y}ps6v%PJTL~ccNoD7gV2QHIHIC>ehXBXOA=nZ3'
    '@9l<QBn>eV0R#?<bePX`Sc%nf_2gXFJ3*(V-t#|&%orhsgia1QG5Q^|9-9&C1x$T=P`Kfbonxad5c*gBu~GNGr?7<+Kx|Yp?x8>U2f28RwE}-F'
    'OUOK`+kezij@syfqHG&=(|5%-OYn@I$imeEJ7KunXpY+xJyBV#Sptw2X{!bZt2Mxgm|lcT4yg>fnXyMB!KEMWpy|;v5=q-Ta^vhQ$vbnA6De*A'
    '<K{R{p@D^WxG)#Fjf{wq1v)?Q1t=1b&z3`uw!0@03By5|cMDObhc7cMBo4;@jG060ot#W$c+$CwWh9AWbW1#TgFf7D?{Z>*157S9ZHscpbXih?'
    'XPUy*zUWbhw`-K!QfPtsT)@~TIO?GI?f&8gW->63Jwox0am8omjCn)O@5+QC)XM%MyNvI-+y~DnB8LszO;EI7mW7@hUhJ|zv3R`X;~=&C)^iGt'
    'bdds*JRsX87rOM39xO$vP7vzp^*eHhQ6!J(tBFF}O58q$(H#JNY4mr05F_@|>WdN&1KW?lIKHEF9U&rKSK>PZuQIoww4InpuIYbLieAGN!_a(^'
    '^fx5ZzMk9y*7FU)6OKuR6mm6_5P<LDr8UE{p%C`FqvSgNk|U*MFTO77AV?AgORYO{Cu-lx?b0(&97as?z-H-`J=jL-VP#+BfNak-Di1SdwGsqr'
    ')3DQAex1|Z%^gc3d$gB(Bs$T4sv5FuUza~PMmxO-g6T9P|4Lo34x{LhC*US_{BIfa7!nEl)$lQxEs{n@KVa8-u2_jEbK600y5Kv5yp@1&Cvoje'
    '<XX2o8^>YkhJE)*uH@nq2(`#yiR;pfkcR?2s6(sII2yw<c%j5fkh{}C@LQ@O!pP`f<@FTNwNq>;xfmkTuEA@6Xn}03JJFlYbW=yWmTN4;r}>WF'
    'kzusyhOUze1C7`9_XIvI1oI3_@VQOuIF@InAv6o995TaG4lADQ{SCoOsmtWAp@LkK)jcYUKAS)jDmi?Xo+3q8Jfja23DZL_u*imYGC0k<r}qqs'
    '6!CwvF}4v%l(z+`7Lc;(2PvveJTm2Vg=Iy)t1|__2a;B~qk4rW6YP&v8Hij8AIvzCk1c>~`y174Ky$b812#wNB=gJ?&1A>exT6=MTKuu9(S|Uy'
    'X;L5vJ04*j$y{*F+jEn1Iq_i~OXuw(Z=nb2oW1n=?U_d9yu5T7)BuxaU^nM6sK*(;Qe0sDqBbaouod+xS)nTfq=$-8ozov=qpw4%cGi%h*dIi6'
    'F$D(&sRm_Ek;dx8104*_QEJOvEA--~2-gy(-$~Gjk4Ru7Y-vJGgTNdk<-Eh%9C7&Xq~IEHsq<!%WirVnKnKR5&4^C~b9721r%s12a8Zr}6axgi'
    '%?Q2g{onTXAlr6=X|&nu4yfZXe1-`AewYVkIWw|=x(Yl8u^PkN9Et%?#RhWDv%eFhn7D_Gaqw;-70Q-p95c*%x}#U(M0fK@iGO(}(#DY*7$n3z'
    'w=8P;E^_WE$Tdor0hL^srTS+3q%;iNQL90Xsy3jNL{7RrQA|8c?iyodBXE>3%E}6Bn(Fl2J&m)t9$l1f&+&AT&}880b<`5k_d!MuO4B_q>DXfz'
    'DUe}jGnAViIF{VpROMu#Moci@5uAtMG^Xfq+X|&oq^L!(yM&=d_u7l#^jzKAPHfWscrxTGNm^pG{f^u^_ttatW>B7qqS>Z4(`wIQSubi5;4-F4'
    '6jRvBqDDqf4-?Y#>K(o4kCF@`b|P{s`o6IwFsh5gN;6E(=V{WxvmNN-G|`a61wix7pSKs5*^<{v;-A<6RUZ`kb?9a1jm6fXX-AE~YqXA(VR21v'
    'tg%FV+1<T`(XwdH2G2^%uQVNcp#Toa5}alxe32r;LWZ(LR|M0Q=_>*S`x=9HN3callAAf5=~$R|Ck%nd(IRDa?CBs#+8qRi$dS=;Bvzzk3$U^h'
    '10wGT_8=%NJB_cc-|t3z2UTl=wnIilv9BG*Y0vjk#*m%a0=UuX=@ET*WJi!qpi?eF604Fkk;Au4*0WAz{?zw<=H~E-nDeb>r0qfsog`$Bt`oTG'
    'j_&FB!lijo<|!W7<l60MPLE}>l@}7No>GU9F^sv+5)*w>-4U|-rlNlwbIF+@3a2wWEclyPCQ#a`|3ZpEjN_E1QP+rG00LvgkAI*Sx?xir{$Qq2'
    'W{@+GZrJvon&^w%aiULplp~fI<Rsn*mk=IUJGOWCG9vb|0OHD?N}3H;U87$Ua*=ImMpSr_W559T(?9xYsMW7j8!<vRUgRCWhG~d75BfJra>q@X'
    'FzE}d?*GsR_D{OI8<ruxRsP9GdeGVIYEd5dN4oJa%UoA0^y&=RWE6zyMy-(xMhhb*j>d?xCQlaLRHTZPMg-aKglSkz-~s~!hs7iorT%-3L&&Nk'
    '=l)KqkwzbY<Gx~NG%`!*Qq)D@AriB9k|$y!b8uGSQ%ea_pf*D%21a|jjNe0+9{rrccA{sDn5*2wy3RYi)AbP$nfUwT^l>ug3lM^8YB@sm|Lr^r'
    'EShtARx-;_tw2ZY5pwSK;h*H0B=3rW>5RjoAlpi6khnIzNG4tMz^7O-NG3|PDQWLr^0s_<jPq%zZ3A4GLk1l-DEgq5o%O6y0n}CssTz$n#P#$@'
    'YI0V|5tw?Dx1J|=CATVJDIsSjeb}h0Vr8G>Mg2EYiPXq(BN$;S&!f=InW{$>eW2G*d+?;X5U+&^ZS+NN+n|)=SwqDGJoJ(i^G3*`mw*miaY+T='
    '#r-4Y#!H${Ta7Sj0GcoutCB4DQSQpk5dujsde!|nrAHiwXR4>cc&=z@61jXwE-W?uuy1bg`k1l!*cNMfxsY{v%HfMzw|i*IiDFB#A~H?@ZL-f<'
    'Zam$2S!#nJ@Kho~u1i7~R2nTYEY4(b2fY^UHFKRt5FB24Dl$}4<WuiSg)PI>2PzGcCH5$UyT^JXHQjmgB4t1(cN$Xyjx)SLpx-fbL+<U}NuECP'
    'Wz=*~MA@1S?g5quf^3IEno()sp<i5F+BaKE^NuGY7QzgdsJFsua;Z@9AvPy%L@#O#E=q~i`PKEs0II@F^F}*qaPaNUj=3w=TLzxC&4i~M257U8'
    '#H>s|=y~8pY0+f{YJ#Mj?z}Zuov)i**gU2LL22>~HYJkPe6t**lt1Xz@kUR&pSCd1j??}Y`hl6udOh1kI`hUToNu;-M`zoX@NA%C(f1L?h&kqc'
    'gM(SK<4wtWpTxbX&kya02)&~l{^p*2oz015h&U$L6+>l}@FY&5X7qnVumBVlqPyGkEvD7o%C}n#F(zR}3SfAPlWhEqGo5GZcuH@SHm!to29}6}'
    ';4kS+>7!3!m*0xb6Mhj>GRcjQ7URw->rUpGl`ewq2f+@2P2|8Ck4|^eY+*vIo22c$Oo&F~hNL{3Bbsq)hefOggN`UFdJ;=Co9G{3)-@&B66p=*'
    '@&sJO{vB1n!qw?1c@Qhp?=kd@*bCJeA?d)pK2+(?LmOPx1%CX^)#qedh}l@Y_d8V^mV}&eqCPI{BT^*GnIL@jDBu=x<#1s7p*o&%Wnz2<R&aMw'
    'y~|0jqt&VJ?lA`<%W7m=(yEqd>kQgH6-F1S9<x$%q>YS_iE}Dl<Y9Nzi`)?`0VpwC%|dC&W?VRW(C=^u#5O>s?*wUntu+GYgtZA5Qq$e{DdkLe'
    '^sFM}qs~Qck_`gsjSb`*+|>b}92lcddQ{BGe2B^rn!51-yX0Ao!sA)i9YN~|-gA!G2U=R+IwL6nK_g}Wo%co%_}PaG&AmgiEE7XQ9AB-%YPqA='
    '15%@Rk7r&Wbcjc@2T%vS7$14!XZf6Gv_mzjHu`|fa8m*4zdK6Hlq$_kQs&~1EoKJPEgak(Kz&H4H&WUeDZ2DtC@ogS5TR!hf$Pfgddo4}WJ&cO'
    ')kK1(5INsLcE^Mf!w^5o7P4f`z8Ap@nIEIq>A0)Y9``KcSPXxJdQh--F<Hsnx^V}BXFAFl?*wVwDJ#bKx_)u!;uke!D~gTp$OR?}Kz%^0WcDXh'
    'jV$2dP}d^0BeySdIS@zl6>4G(Pcx)rRN}Y0C+{qoK9FvjM-E7r4lb&3`phsf>LZH4M?UmUQ|XqzrelE#RnF#qB4yqLcNKJ5QVswGgPPheju|mQ'
    's=tv+o6YxfI?U)0WPmDlAvKQj?(I1Q0~BWQ{28$a5b!kbjS!qIW&xz}zO}R%a+Z(dK%pgdJ>&u%@zDq0k((yBVoE!ZGg4*638>&qSI0MFTD_=I'
    '^iumt-WpVg{1Vti&F|oLbMgE(XjjpjK#UfYi}k)a8A<5}IpQ;Axl<M=0PP`h2o_C^27^0#@$Q(GMr{(8CrLqXZ^Z3(48bnj-QhCvXC!k*TpJ-G'
    'P(cUa#C?1uWh03>4-;<-i<6#94u@?L5IuvMBPMx~N{c>_oMUT1>d4e4S5^#4R_`jY&y)we?MWE{=BI2y%9R@)CMc6J;CuJ+PLOhu7HP2+>?^}y'
    '=d{9jxoNXHgCLA4K|jgzQ5I$`GWjHXNVCZ~pZZ2_p=IaE+!m&-%It5{Rqika?|7wzQWy)X(=`lIDLkug)1J9q&Z;=-{vwEkEv-lO=j!glkocz6'
    'E>gw$c8Brumd8IB^lxK3R|k<GXkkfpO*nmX$IFYujHG}>3bRprxQF+of-mU-quPYKW9ECim<4OX^LB$TFH(TJr_>HW#ip2<gml;G6icyjC!lKx'
    '=p_$5M;q`aHJLs32Z8|+Ut$u@Ay_F95^G&<ZP)ivd{luebufR9-xbiAUO{I;Cl7`NVIXaqz)f$a=$35ronB?|)4IZeQ8Fw^2gYgXvVJ0tJZin8'
    'dh`N+du)v~w5$fX4<*iZ1u$pI$D9ushY@1Bxn8VFO6yeYTZ>e=Bh}9aLD$+=L4DKOQ0(ZErP;rbnxvwqYY~Y_F#aUF<duS(&jb&9ifdL>gKTP%'
    'WQC*>1iiHqdFhKD(Mp#y*cD^MbYJp1q^UjlRYH8;(VIsRF%k_lS!&0Ihfd|7XO_WG`>{ddly#N2)(a^jf_qhO1SqlTmg8HpEA*}GDmg|NE|T61'
    'iz8tMtb@T&-JbAvfx2QTq07gO@Dgw@9ao126|<`Z{T)?5ND`tcM{};@!Z^usxB)Cg)wStHvP`5Ra+Wb$rCEyD+U4&(-BCKGi&QclPrk-YL`81O'
    'q2v%N4Os7#JOFM*C~H^@6lz(C)j_Y$JGOCriJqGY+vw_aAj~=TP^O?a-5VcrJLvHwC@4B?+GwkTvjEX?0ZhFqxH*GB=y7#(di#g5joexzwij$#'
    'd(;d*$>DPM<Ex;eaL7BjAAS`&qdRi(3~N*rXb<`iiy}Ltu2G2Tb`PLD-sr7KEN_Oh>Fz~Wc!6z)?_Kjvxy>0TLLWBPru2ao@Wh>u({w-HnW}uX'
    'b)C|E2r*wG(LW$($kCxRZYc#zN>L7;aQ{aqJq^SLIDpiQx|i@74yg%<yu|8(F)3Jr9w3bwg9Vf79l?^0aYoJ&z#?(VU|fMaXPE6{udkHxzBfuc'
    'k$+L{3Bb=L)`Mg1{gqpCp^_L>(%g<KG7?AbG&i}xj4i2-ALL-&x{qBAjObd4f_?`Q?oylkvd?5hR?k4vX(k52Q)aLnW3(|5F8h<%a`i?KGxWDE'
    'Y{&+(`YLy6n|&E)o1mp9x^Q`ghTPD9HR=G%JZxF#36Jk9>M3Chz-*#K>NBa(3mrqmMXZ}zOpKABG!2wQO6QFdlLg4p2jClBBR$jl$naW2YppvK'
    'TS6^>O;-cZe=|T~2Ldy;<%rtU(ckES(*!tDeMg=eb<#FWIi=jiKuqelbi;L6xf}k(=3vS|JrqB5ni{N*qN?ZGC&^lZ7e9iZKw!{ILV>FdkabgT'
    'x-Uy@V#!K;ViY2|lLy8@)|C?OmVPJO06;0R%}M<y^Vam%9pVLq=Z0K8etYd6B*$&K!amS)J!#!|?DG_q_XB+sl$f!wv@?u;d%$#N{YHs#BK5e?'
    'U<A@~15jN<h~}UadZF)kN<0}lwZlIeMz#k1BC}W1OYM$c=mkXQ@GKPcUN)YW#aXW&+BS~fyXet$UH!HrDk#32q6w0IF};3AFBF6H=xkALN~%}y'
    '*-*Tm1{wpwqPyv$NB`4oE1xJiFA9{DDj(d{TWjt+g5waBaDR~2L(w+RGVnv}{UA%McX|w}S%&0!<4*~BIs?U@GdqIe!AQST^>d`4q0_R4T%#eB'
    'SW0X&y0S*pU(38d<%;Er6_|)C?mnY&zT6b>GG!zMsYWCaZ<ZJssC5MTP0BgSpQLI`YHU|2G+jLMCkV5;;P(@~a3K&5+o=XhzFiG9>e-qx%BLeQ'
    'FM5CmJ0rn(74X(6?xh4RAmG;B1s=<6R?JmqXi4=*g!ibq@wCvmL0i}_dX>orkv$@H#Z7j=u#EKhdvffWa|6QFCIYZw7ecHp5ZIwMqS4iz)K6*v'
    'tfz>uV%qB;r6eklpyrd`(~Q?79AIY+=wxk6%c>)MK`weD+v#p1+KvP0*dx{)(6tr2k2yJz+IK9O4@*`<=@o3G3aSVN_=itM%xU>Ssxb@lm8J@('
    'T9PkQ5DB@Mg}&D9iH7H(BPSP@xIgL8PQYND6Rp87<(+OJU{H2g@*bJFc<vIsE|B}gpkbk{0}-<RQ6-*?fbveTkNQq<5tJYc6s|)vtZSFlZ98zW'
    '*6*dz*JvKS2y6mZ^w7G?a)uW$L61T;kjZO@RE6zUBRw|qb{M(JnWjhvdijoCJ};iKEYQ}_zW|9*nq+!0pCv_Ms|)tL!ASg(Z=%&ci+OVhmw9)V'
    '5niHFln``*)BuO%F@n-Gl>2Zp|49ivDLV2;KoTOXD`<Vp%`uO*ySE;}B{1!JBB%km&0yel^z4dJoe^8Q$W;PEMu0Tr0Wh8NXsh!@rQPyLIX**e'
    '3=BwI8{NQ+1q#6u-~c1$s6MI1@XCCGAo8jwNH9Qt8XUCUv&lSy%$TVKdUbl<^wC6)^|h7Dc&7&xrPiPCIFFI?b6IV5-8JJ_?#N#6kFkvudI7lr'
    '$eBx(AiIo}Yb;f{Ws#k(5t9eD$i2lnm=Z6vjk^uj!<YIhfPHy*67?-czrQGLXB#d`43T_DAs_=^_?g+AO0)&&&${ur{SGZ~%|0qbyg`-q^OHv7'
    'W=bf)Z3RL0@ty8wQYQv+K%kPYufVa?5pX;o2!=-5#5iT(TDM1cPdo~TcD2v^PLDn+_l~}bdNSyV61OqSv;FS7UjvlrbJ-LTEgm5^lA@Ocg4Vbz'
    '&=8*+?2el~Lui9GRwJtQ&(zxC7w`wNU3WRBBSyWMnrCzF96?Z<vQUe(d&b*ouD}FKpnpEd^CEz<abXP!aNPEN1lcys<60}dk=jJQYM}Gx0X_{='
    '@&<E@eQ&>)%wO}F*cp{<$mD`aa02fkT@WujU5%4r3s#T+S?S$ZAqvJM1O`JVn*`;ipYa;zRA`l_BB<M90tq2fAJBDG*Li|Zx_G%S_{CSqE>Zlq'
    '*#@84e)o~CsZV-W%wu^_O3l0I*cRys?;RlSv%HZ7@%BNuKE0qQ9NN-LoJ4zdTR!G`cg$fRmVq#J^8-U0y9a0PLpP3jhe5jL={=0vb}SO?nVBib'
    'n4d|7`2^@iaytSiTMY7Xyrp>hz3n$r#chWr>M$v_1Kp}KIxTVtt>3+k2x~Ebhy<r4oZJvH-R<Zn=Wmw-AuoEZIl&UG;Z#1Qv}<P`e4Za9>KQ>L'
    '`;NLEzh%%V8As(2Z&n#@)OX(bZGU4w!pqh6K&b<1nLxOMiS5QjJb>VCdzPC^xeK|FN)5)|N0Qn^beGzT90!g8yWdeX$-~PCnl$FSItFLsj;<eO'
    'b!()jPBHozDD!>^IM1c;)41u<cmcB$Rxm2?LHM15>KWZb{l?!tNZAG7L-rX_YoZ^s#UxRaik9JNrSl}ks2&tZdn=`P;SP1+6U=%Wr61^8XOJ3Q'
    '5uag;^fv8d+3XqdWk<4~bZb<A&uGSQ|1KSY&F!BecXy%@=%SOnR2L%?L?=ryL)68b$X1Syy2$BeFcdKgqKBb`sVxN4wtT$7uoG)Fk|jl|#W0$L'
    '=62AF9`We^ebO6%xHRVXJqtMK`H}4aWH`K&Ka-93$f;GM-Z1oq<V%|)$i}>!<I~h0Jy!vuE3x6mx<VO2He{Ooi*-jTcvMUZxKsDrpl<R(ouo;{'
    '7{g))e3D`k9i0p%2)ew8Mj9zp<F?&^yk5h*=wOK>owoA3k<Aam7{}}Gs~16vk<yNKx#5~d!-8j9tdWlWm0X0lz%Zc7pqU!Ef^?|mGf`^(AV#DX'
    '0N>yj>R5OYy&0dCJn6604mLMoIl9CE=EBgQ5^^~QzhrlhcDEco>ERVpYv`y2<K7?O!jk`zezqk2J>POo2O}}HLG-S}`(q%)bX&^>yBc(T%_caa'
    'ziTc(8J(h3en8~68=t<M5EsM9K^AnbWr8?J|5lK)y6^6He3BaAGtB0M2vBI_;i)jd_t?<7Gw{!Bu>uevlMdNf2k_*i$A=*WM)X`giOGI=P|Xl*'
    '>4I$<WL>Ve%qO?I3mq|W#UIo%;Lc0CJm|a)Mu|CV4m`egN5Gew@;87hI`9WT5mflOv$fpdu3kgR3bjyl%@LT<F=o#XK6FQn0x0vHE<rnB$KzC~'
    'gp&fLl(Tyz-zS28e-4Sc!V8H#h~OVzl-$jWAnkF%qV7s4%VU0J1JUM}$45#L;S#e=Fr2XOohBUt>TXKwIRM~EX~^N!CCj>aM_z110f@|!5pnZU'
    '`yFNZ8iiI+D8ndg9O-6odms5c!WcZ@>Vu1}<2W_#K!Dj8BO?(XsIK(=M6U(C0UqBZ`{FWWabk~cwg^D=ALOWS$o+Kd#lUEwu~Qg~`ZMc}oZlf3'
    'g?(xTAL~pmVU2fq22mO`;dQJyR;fiPfZw6^y4_e7MX~Mo0@Q2V8tx7Oe<>G~u1t2?iQ_z@0F^E`vuFt!a|RbNUzh1ovUMo5`{bmR6XR-r4l*Hv'
    'Wj0Bzc}N-IW`-QTNDV5STixgR(fKG^AdQunjM4htVMdHg3Y?BE`YEZgAnUMZh#B{9>Vx|`L55e7?XRzLW$MKUtra>HcB-F33fELOD!~X>=k6mV'
    'l3&8NkIGV!o#_Wzf`_@r!2%?&Pmmo`TaWJA^g9~yHTSJh;zNX_*iK7Sobja)jA?@-hI%{+>UR`l%y)S1X`~?{ZB$lb7Swbr#KT|%;%)^V8f}~8'
    'aKl9$J&?yzDEl@|wj07%KWMZjI{$MDbmk9)f=PigWC)cHRQK?R-9@h8zmeJ0xbDcel7b{nD(U0i+3}g!451CAjOUpW872HRBTY99>I}a04|-JN'
    '(0csl`BthvBttziW;u%giSAJrGKRWQ0t-bpbOiJeHS1`_V{Kpi1cBNt$v(j<)gNnwObw=x=I!g)M>*{kBN|A2Q@@;6u<4GhL3X6zNsq8hUV-*X'
    'zCrdpLwgx8|CwGGYYjjV3?k?e=xk95YkDVbU}e~+UHfi~4BGff`l1Kv#SX^&ja2o}vUm`PDe&=m_#k1@))JQ5c1)H=sW(#O3+e1}!VV!Q(YCQd'
    'z`9?b3C2rEl*7B+wK)F2F4ORgs~)Zddl4+I7Vn23vm>Y|rt!|juj#iKLVeD<D-wzzb%51Y3_wnB(04auYIKiS#6?e%75qRKNfYgQ053WQ<(3Bc'
    'Oux;XtIrvCl#y;ZKhq*jtm$lAkPFM#ow={=(XK-|P7NGsKy%3IO3%B3;1j)g@M$<sQMf=%Q?e^Tu%2n|fz>z21)v6vL{kAK<-zSr$Z16}N4g^w'
    'F3ZZvzyjIAg#mXdQiF_sFsboI%IJCzQ1ZqYSNy;eyYw=MHtiTFb5Aw`U9v5)naGb5_0W+;BL?N#H71Gf|3(%RO3)$GWk1o1Yn-xy+1A_vw|@)h'
    '*XPtb+Q#J{NXz-vbA4Cfc}8w|?r?f9x}XEEm10Ua1v16*Xc9t=5rQ8`h8x59c5HScCDcO`-CaR4LcuB2(;-Rxh#tv`(FLyi2I`rTXlc0~qjBy@'
    ')=MKA*V*qwCQH9cxh_D{8t-ujqOI=E=<3qU6qf;k>H&}@)$d4+W7QB%PgcINCex{-cHMmmQvF3rhdgPk2ASEr0}zF=g7oe!!ytbmIS<Q%b-zO2'
    '>%vkD-(q(wCZ|P2?;_dwPqL$0@J@@Zl1nw?oDR40YtM6ij>047PCAY`JOpUAk%fY1-EAhGbw$&Q?xb}%M|CxWBnIsa;16I#c?!^-95^PUBx*Ib'
    '<3Do2o);LL6a8j$Md0fqDDnQF#`sLih|GO+rGnnmXK`?B#@?BaLiaR9lvMfjD3zOr!eK~O?6HSd4A4$0SRWK9rL@3yXdkcfm`~ubP_@S9^j(Db'
    'N)J*8?6-~=sjfGWKp)#@j+#e#x8mt%Qu&_wMN3xstvI1R$Kh#<m07od#sl6?uXhb^czQxj)4%yacYEe<iCM(B`44%~odTh4@h^(wj$(K<)o4^q'
    'Q-EUK<*6R0yRt?U2t6rcQZ1^AhAGnTUBQCrJ3tC*yT8VGm6;LS1|b$jPZS}amm|ApS0nv6TBMvdD1Cn{e{6^cq6yg77AQF*-93<FIx@$O>M1i%'
    '2PbEo8#Qn@-ka?XR~-FUPX_GBO&;(AR7Tl<rMXr^r0y($V@)x?M|SS;%gDO@$BLTqg90fH`A%w=WJ%wKi&{XnWUe;igV{)bkUV~kC-$KTI8)}W'
    'MA3M>LRKE{6ssqSuK;yoli-ya!LU=bZi>H~RaeAK4x9&C+c}kvH0DJ<BtticC-o#bw&5k#8HcXVWhS<JsCr-t)P6_uG9A-SQJKW!*q|XFl2Kb`'
    'G>R8Vss}0XN%fg;YsE9r^%0ACAFX|+8^U1X@frWT0JMQY5ii88^le5L2WAaq^N6RB4Qh{ti+$~ew_k@>1f_Tym4Q}(tHL=8c;goJt1)d{J1qT;'
    ')T)j<_V6^CA@PVXP#*jTz2i^KX^4Joxq&u#<j@)`MXi@dRKDm@+sBC(JH)_h(qcuSXLlVo)nIoN!;>Pco*8u~iqp^|C9=UQMLlAV>Z|W5{+1N3'
    '&(V17k--G4PJU_Np+>Wa^>K?)voCsjt>ChnHK2#&Z$#JIFb2i1Eiv^u9S?uQQ<)ql($#M}i9yd5tUbU^0D-Weu}jD-aS9<9U(koqBY$sm4N{Sn'
    'A<Dz&@&*@^p8_BYQVya-!Tpoe09KfeR%@l>4ugA&TirpiJP+cYZac;XV`;-ZB`hKR5qIcRK24|VjjVi2u$f6&_3Is0=e!(=#Jq}j&~kT^CtE6;'
    '6zE~Hmqy8{0Z6+r&!g!e(uo({#=URE%Q05zm!?v}eOW%?e0!NE7M=jlX99-{2PeOet-1oS)yvtrNY)PcL+fFZ>rTXZ#Z7qKrJi-B+rteDQqsh2'
    'JbsguA3cb@LDB)EGU869k8I?WZ1<|sJ-irimcVRu^Hv!*l)|2(+e0FpCJ=k7)P40hPghG6SASiUC?_KfRoRbcoe^)XbbwJt;5fqvidVLD*7Fv@'
    '+@)tds@MZZvy~R+%|+4{_}nY!*C?sxRA=I9+K$OZx!sWsF9%p`<0e9ufjy5Fq4(uE#`+KMX9_Ij1X#S-k_BvOd2F<Vr3?`RY}IkQ)2RET78#tu'
    'e=ZZD4=mb$WU?60DMI;Ailw=`B{Yg$xoQZq;c#)U^E1VGl%$c9vuvab3ZrVn>R851W5ljsM>nfF$mb9M*>U<N^d{ZfRLAXU&e$3)TgEpl#@5bm'
    'A~qrSTJIfRc@XN&81{O2p1V*B$zZ9hP8sGp%|^Ertu(j_<{Ab?N->Ugvs`&e+f;ogNF`2ZN>A=riU<&Ud~|Ax^&E{fQ|s@K|4hmwz{GH2fWL@p'
    'JIcL+tWn372k^egE;8g5v?WWphCfy*01*3n)S7NJ-%%`w;xN_>j4J!3&KMO0$sXX0VWeIpr$MA??v9`X!=aemk^m)~e~>(xeNWz94myZois9pf'
    '<cwhfp+e}GLR;BltN_#|`qAYcWXCj1FL#7S?2SGi3iKS^U_|YyT3X$jpVs3yN|LBFPZDd~;V{xg4(R^^s<eM58KP8|zvx`{-ZHiN^N7-A9~H0V'
    'jU*8jYF?vTn|erGbF%sY{793)9l>B!#{F~;!A5rbkaoPm9Voi47=95X7?)_AozKi$1^frxAbPHHaB7*i8;y#vg^6pR1mr4d*cBVy!FbXpKQ>Cu'
    '7~V`xA<COaJo3sWpKZUf-;g{(GM-d5q5$?kF<no?rjg&@OibFWYAgtus5anewgT4*yJ32WW&IU{EGX5Z_B&dY6hmRoK#k#*3<a1*|G6|`Py+7}'
    'qqt+eo+x1Rq-o||%7iG!QK6&oj&3_ac&1W-wWf0dj8v-9VrLtX`a;Qk(M4EPy0!adUD;lxcSG`y5ns4DxwkvIVdiX)86g$N)DK>GR2J+AKu<$P'
    'Kgn*eOBifr#JWu0m|(Y*Mkg$1itCnQSUI%Y&?-LIsn$LH_>f#-CPmQd+9^t$m<Cl<kTi-6mVq3dVUeE3-Af58M<Ticoron+Q3G^j_@%@UCpgu+'
    'G>FZAVaVF^cXN?pTJ)*|>+;vJ(I$1w9Hu@~c=->e-F-44Cn&jTxR>ZJ2}(>cDb66<G(cz&tV}X^0UB=6J$kpflVwbk`VtzE=wqVTHb($?yG*Jw'
    'MC6U6V@+~rB0W#QicM19vuN2HX4Y{>@&s~eF0jU@{H>GBH7d*_xj|jR%th>PB&iEoG^Rh96}r8-7#$?p{`qjv<0Mx}g*&IaKu0r@1EuJ3xFL8B'
    '*+zOZGtncS<%zQ|Z~{hz$UYqI^(<X|fzv~6K4ro?NCwjNFoqa;5pzy8mXSzxr2j6StKMxl=35z8PB6EUupN|VI+nXUgOu?+GXRew-Vl`ZW0KsR'
    '4x8{*B?b6N30A2Y=W_D8y~T*)O1WL(__AAdOJX-gv-*sw@fX2HAwEzNE#6@V<BXoi*{SkzWA&|YJyl`b6M1P<<uuohR%9ry8bO(r-${~=leZHX'
    '-9+-h3jiUC@<~7aJnl~QREFvG(#wW5Ug1BwNK7&eiFxvI7e)GcC!)T#XB1}?^5{CJ>Y;DtGue|}X&_EvM^TtsXxv*tw#8WBa9TYX@4qPO331xc'
    '=gT1CVt`LU+;*8%vND13=Jh8JUn|25%51CGq`*UohKvei1jWS94YmMYXTA%ihy63zZ~$a57LX$>=(1reNX)sh#M~3585dpob6`uPY>a6T_0S_U'
    '>tef(_L=0Vms}o#Nw~TKeFg@ZUr6!D!+OOXFOrnMVV{Dwc}X2xyxX-MV53{=+_>fKDp6yY?c!ek*%qd{p<3D%m*VI$-AneQOBYtmPPpF5lvoBf'
    'gE$QY^Buucr3Oev6mM0Wwc%+Ir1)Ywa6ZzthPE0m)AUfYG$i1tqA5<^?OAUq9lHwjJseYxlrl*Yv^_|fdqy;RU!?MK574?&0E`r+pR3aTuw1I?'
    'A#J~tjF*wHs~?<Y^|16!RCT-YMv5awQ#`j;rc*FOe#YfoS!fMrxRfpFYoYinxO;uh(koCpGvm1?N;zo8f~=ALsR+sMbO{(!(iBBbB$8=YVUCy9'
    'a3jHPxBILoQ%;=`Q)<D^+*DOyNCN2+mO#w3yhx&Ssem2JWukK2@jw!l^QCzD=I`y-^*Kln@j82e@Dxv$^F$#?8eCEr9HpD<SUK544w&gkS5~6<'
    'ae5BHr_BY2_HI~Z{J2Il$p+ogl#K0FTaM7mAtfa!Ct=0bn0yFCLIT34>_f2M5j;iq1H=(5qGBMjM4rS1L%TLl2Y(VIAWmb`jF-WwP`D{)B}G}_'
    '+P7!9PffVo2BV4*<(kE=h6zp!$ZDjy7eOX#15CHi9vcH6S7G{L@=TJlZv3h<N)H{Jbrrk^)E2RFMyf&5EQh7#(7*N&!8pTG{dRvJNJZ-j^GuQp'
    'HA~}>Eq5lpNY$}_kfJYwjEAF&-~?gz59THAm^WK~9Y$QAW9<m!;Dr<3kU7p^!e(F`B7)V=A(~yD6af<Gl+#<%#P~j{M&*tVqZiNH=Mbqs-b<${'
    'pDP4Xz{s&C>8YGG@)mFBNgz%?7KvO+a4S79$s;yQ@Qz7OtZFbx<3TKy1mJenJoK7te=4XAKei}q5(JSN29<7);%2fWd)Zh1MDfI`ruz_^xs+f<'
    '6gC1AVDpI4nkJcN)H6HcN0XNJmgM&&W+L)cbN;S<`xPxJ?ieB%Rj3uveHa>U1K?oMJ&rsJQq$3_R!$;}oFB>lMwcx61)%JXU<5TK%t+Ww^XMe`'
    'l%}sw_KH!TQk2161R05uk${ob)I}_6e*i5sGtZltW<Ms4o@=wYjZnk<0v0(4cH{O^`uR?98z^L6!z?33?Y7YEdJ6gsc#LD-(e0<@tk8V#$JmXY'
    'JwrLQ2>p#3KKPvGn?5~TvSIcyNkzWC!NuL8obN~lzYtoaGNSuTjbmu-D{eQXD0O*Ks>B1<s3~&AgR)|<gZ1W5aA~!2M=|=@$@NSRDI^!4uu;w}'
    'hR-llL~bsU4NghUn1<~=u)V9hZHj%ZS?)G@FeL`FU?03Hd>omj26~3%PAB!-f9#UNLdmaM>Xq{cj0BMZDq3`Z&w5KW8hLA{i4^PER6(t`M(iFG'
    '2%6qcqMms)N7yu^mliN*8L?%0<Cb1v4*{wrP9aas+2k7SZp4rrhY{aLekuyYTTH3}LXLq7U<<T;NZoj*{ltg>DeBS|fH+lJ{z1Q7Cgnm{cg?<&'
    'YTGU|MeZ+mpM1{LHxK5+*`E18H&BRXB~y_dOd-9z4z~_N65xCPAnUe@wv9cO0r-Unur`>l-~zRICjky{DLjT}2ti|t1ctsSy6R({0>INVF=erl'
    '+B+F|T+2mZbJr-j-{VfC9^}$^U<B&0Pm=+4t%YY)3J)jtu(ne4EscJ2h5ME}4^maF?iJ@55=kK1oqUCJgl!n>JV^}?lg7R_Nv13UgWIT~a3Buw'
    'symVaS5+62@>=&Z1gY-;-aeLLk}89;-bpe#gEFd6za;$FUFR^Jart#tRDBNh;~(mtjznRUrV&0H1dV4|+m&}a^r#U9A>;&5sP@I~rc2RtF>ej-'
    'e!+<*$1^}bCQ{V#W~z-E^JhLL;zcl#5}5OZ`j|ZdUeMH*iznGIMd*%RJP4pU(*yeul#g<``Z`@FMMgfUupYC%YQ)<q?q+7V>riaGwt6SE4?-2f'
    'ut`!AzSxY1der8pca@fnb9aXns8r0B2o&u~7-A1w;kvfcK9W2rRm>$ljb|XY6P2uV4+2o*NY4(beMlD13@5_otm&lcI-&1@lnr>syT^XuON+sF'
    '3}VO_VM%<W20W@q&r~?#ogN1wX+0cj^Z;T^U>V;V+-^IO^LGY4GyTB*qE*UCsgBA9+Y*W+GK*4N?}u7Q35^JfGxK@6W)g3Ngq!KrJ9+{2P=KC!'
    'pxR3(d>1Img-#e7q`@H~Sfek}olGccaJxZX62loLUBBLvI>@DD$D>+OuNE|XW{@*b&7t*L0{-SAWmGEWP0s$Tma4{DqEAx(U2~c7C%Ok9YqhB>'
    '<H-X6CL0mqO^4+OFYO%ZMRs|<=zIf3058>`$*EI7mjh?;_9g@SK_5s@C(U5pK;+RTB9YZclqJsfgDyoWWxs<lFs2p!7BFQ!vek6g9o=w)U2Yqw'
    '2(wdpVRWEUU3Lr*YDA(gvcTpuy(O-70#!I=$uQ?3+6(UK?JFBrKp@hiq*JbkNlbF8SPt})Vl-S01YL>ZLQQ7-&6#8h+56o$hT?b@hl?5Itr0O@'
    '9!ZFBhE#J8a2==k<st=^n;SNwNRwH0Sq8u1D(pP{I+&_HC;p)$^zI+L4k9}a7?|KV;JH0i;zf|Kg?xxP*Nb=RaQEw`c`=&Je}z`8&#`}nA}`9B'
    'U{i^Hz|omx>1@8iYzY<q*e5+C;P$oj7O<u2o7wIJmO>r3JC;2Sg_OT}68AqmA<75Phprl4F@3{E*=xI$%@XpCGEw@wXx2h(Mk?}Gq^J5E{D%*P'
    'o#D7CAZ>#&?EUO?N8B`;UQ4vq8}pkQF;GddDa8^FjPr2rNJZJ*c8mvq-gvF@Q-HPzQm2ltzL7G%wzOun-E=ZMW4N`NB+*BL5vxIJAxC)^!?Q8w'
    'I9rQk&8k1>83Zn-^w7<RgEx6B#ltLa&9^i0uZR<214^C=dl-xZbUs~PltLZL^djdOH?DINIlgl+o}BjF)01+?D`71X>YUL_0AMWhqntm%x{h&9'
    '5mk8Yj*agSYrK6u-wKLq;O*9N_w>V3?4Arm-^o8)UeyyNe3~WFLMK~NBXE+VKZ`PHs#hrorXs->^G$KqI2P0d+|u)u{}m%wcf!09;uzH3Uenm`'
    'q+IC+=<tlz@97FLSsQDf1Yo~CORI`CXj}Kd?xL4%BX>Yh`;4ST5G>JLms|Al{kFFoO7jFv7E=Y%qoEu;L$+~#@1#l;*I%T1GKg1obU7mxxPyrS'
    'WzaQx5AP^imVql0gMI!<l@L0#V`!H>NBUr-pA<<U5>I<ghJ>sfb6^{!BW&t@pleySDRWOzBfVFF+eutQ*PX*U?1+>;4;9#W86s|mquso?KMp{m'
    '?Lr{zggcUE9F!AHCsc*8X`HEIicW{t5yRV_6tjmDppOExqcgL;%iwBc=poo|3C<u0%68yp$)#Z^Dsg=Xx;Tp$YJvxLDaM!T;b8}JJQ8Szz3De4'
    '#PftpHU$AKT*I}gAakO^ml*SJ#yQLj;0bdBkU@5qXjr3s3eFB|i!{R>MN6~YnLzr$U}9j{f?zg54=NzX|6^;?aE7Y2ZNX4bYU>!|wxZAI6G6)}'
    'y8Ly1WI;&C6U+x?4~p{?lBaiySlbVx9<DtFI*<<B#Jru<-3;H`-CMM6!HboYq|AcbqbHhbzCsGrLkHINFfR0}m?-B?dl3XGej+_*VwSd%h;Iot'
    '6XYhgF|s&RMlBHZDuyPBL1LjI+BKk@yLB@>HHWSZUG_rTyfF&riIQxo4{Duhsgbt@tRhX;#n2ya1c)ITFH!)uGiSjHBGF<a<BOi*+4cyM^N!>S'
    'k~<K<n#DHtnHH$Sm^&sJq5MOVadocTsjt*;a@ujZfQx)a{!DlKRAuVz<(Rr2WCaZc!`{Q#T_26t`cBvL$(WyE4V9+#MFWGyz~aVszB^r87L^W?'
    'VTT<lE=RisTsx5d1^`VivwEHG2187ipxwkYfhicC%}R*CvwiN~zikV$(aC4abEM)rJNyj6=&eZsGA#QuW_lllOmbC~k%;y!M+`K$liMYjbm>9t'
    'C~?`~Nf4!tt+^F|Y~T+{lyFko*e-%e22(ZA#2HF|N3WY6uoTQ=oz@e?6lne`)GD0u<GKGJSdEQU>a|7Z+}$SP1o0<|!-r|;@dp_Z>Iz^vda6uu'
    'oYBnhhsk^ah9Cyg3*)sP47ma9Y}A-fe^V@Q1^`wRD7gHR^)-<&pz=5fx|(jS{^Ry&Q{cL4jQ&e*!INI>{9sIcY<G{sNhj*t{gkvWMR=4gG6@)x'
    '9?XTP3G*<Tz_TY;LW()nOdaTY64k>x@7_WU)}$?+Om7w?#C8o403{bi&Kv=Zi_&5=NVGms1x>_}y1^Vln_GHk>YvQY>BdnR&Xu|`58I8~tI;Eq'
    '%MW@WhLW2)gAI9rGGfS-N<_+48YO=wTY~Ijawm)e#0PVI)3r^<X=WZj$a0brW4r7Z+2#?;y#GLT2VeG%YQ3o9&nGrZGlyp~1l1K2{i5;wqKZkg'
    '8L<`6Jr-~@+ZZau*zc)*rfVTUga==3!#=c*S2rv|H~Q>zKM0HcjFB@wd?EnG<Ou*iXVj<PgiBYLP}QchY+9SA$-tD_rzv#<tX(0#7bW8gj5tz{'
    'v=znDC$_g|Fx_;Uo>71R*8%wsSSA{I0DrjRX&A@6756;7C>BB~ZfddcUKDML3Ijdnte+|FpxA2<j4p{&hWR~e9Hu*lXd!v0$0Q@>!^*jS-XUPx'
    'QvU!{kdN?~usfnQ83|O*=-K69x66)84XW<sqnY82tnQdx3~pt+7?yx-XHY#&Xl?yLHl8QLafk7Ah+LKEB{SJ=O;vL}4o-wHiUSII6ZCFR@U-X%'
    'bv1Qqccg+zw<pI_z%Ldm;A%J#jj3u-$H3uykxFh!rAaQ%lmLzFAdsoj@I1$r@3_?g8sghHiUigvi>}?{)J!sa3QWY&UKEq-g>t-(Ngjf(XsK9+'
    'd6{6jBWPU(NFp^y6V{Q6!^)P*fS^%(fJJ;Gm^@`v%0KSU%~F|hx)PeO-_5My`kV_`3;9)YTpTJ@*z<A7*`@{w<DK3F+8~if53{j3RaO>PZ@jrr'
    '1XmEGLdDa5$*$3wpXukFRJa=Uuts<|mkYbrM${l$OO0Rvp4N{t{8&*7*k@x_R_Y4H-P2LiM{<gnM$GhsRN}5l_`fXFQ9bM)aEltjc!rqW(Y5XX'
    '9E?88#1?C|Ie{@gT?*l5Y^SoGbSWEl!`OJEy9_<i8HEvO!k_5Ypqs$6qJq+{zij$FeVf@1HQD|CMi)pHstj5SGu3b-K!j3fafa95U1k`MWVt(G'
    'WrR+z9v0~M8zWG8AYs$JC{oqRJT(w&o<0C+`C`RMvc1Nj={veT=w_ZjHC#w)g4EM-IJ!M&KuSB-=bB)4+JY1=h+b#&m^|O*x}_A_qk1NGwR~g7'
    'u6`eemAmA@_F6v(QA67a2>ML7(;h$?WvVEG;_aD+_MlCbKgHg3Eabq7Z2(3pLUKgu$wd-ix#iLDw&>h+MynrSUh^ZtW6|jVJy)4aq)NVH;G3XF'
    '_HdZ_ouC;j>my@^3fLHoE=JUc4A?g%QfFu&0T~L{tL@{#<adlhC=O4c>Of~$?4%JC9SuvKngnXTyoTl!zm9p|kqQn4YN4|eI5f!_jO-#~2e_i1'
    'FF@x*YF7FTdpQGz;w{oq(}`$jx-9Bvy1}80M{38;q1lt-JgZ}aYJh(FV~32zVzOR&C8I1|rHf~$AgtB?nQZ8f4iuOk#<cz*#LYQD)}!N3pYDrp'
    'abrv?>xGY&h$M7|EJP=n>E2|D&JaO)Vw7|}aG5Ig&MBtHLf0*{J)ioXC*6fq>Ygy`HelJv%$Y*Y+N!Jdj$o*fwHnd)*6i@C*<3t<-|DG_U8b=&'
    'n%1};rb*R?OO%NgGY;#6-La2}Lt!R**joe3<*{=RA~)3W)!zvLb0w=gwI@AstCRo{)|gd4Zs~>kAcWh=ez3LK%zVB>@G78~qEGUonB^0RVM|oj'
    'x|?&#Db7HM92kgulA$?hGa_FC*D%MEP6bn>ujfC#oFKqO5f4?*XYGq(A<r`n2owW*ZvvcWdZ5#;t2qrGK-Sw62rwHHr3sXK)E^WFoh?zV$w)84'
    '${nv(@*rNuJw-prO1HZUx=<N>y!%^C5y>fv!J)Z7vkbn#Z6Gy0!buLj-;D?kQszy{hEf!oa-59Q-TioRBE-XdCq!X}ba0|k0(wNTD@Z)xr1>q}'
    'v|ybHg0;$+HLx>pftT2swH2nn8K#|nlZzq^Tl7fB>!apD2hwD&O6$Ay{+?n4#B^aXWriK5ipn7AWvd@Yx655zZxk63Ns_@e63A1=<n7ILmFR`}'
    'z@cHwwI)NJ>LZ7Zm}TdyG)3ggT#^2I-X6}oi~wEg)2WiVXN*DW6F?Gl=;wB$CCK&$l8^0iH0_SLiTE5m45mexY!1~JnOU1so{%Z)dPb?tzHv47'
    'F=-l=P1IAPFi0L!>t(x`nR*;)j&$io3T~E3n_Ag#ZX@3cK4}?|?yPqi;WJi{u7FJWX@_}09XxP*wj()N@G9Qv7LbE<Ybjg~=?~hzj9#B&f_A?X'
    '-4PvQG(x6K7J<5!r}|_YjTo?giJ|=u#l$F2Qr{yastiALvE15OZ6`#%|L+w2APH<wr!pb}*p8FqIhR;f?{}Cy{)pz59Lbby$9?ai9eN0$=-r;_'
    '4to0BMXttYYS^jeyR43q&?gf*qsG|hlho*iKO{lA^g2(@`d7WnShx}oaW3E3bfl4Zj1ga<gq0}nJ>$M=%wHz3kfyD!3vCW=1Jq=@o;+Hk=bU$R'
    'YwWNQ@S;(VY0(vgF0zdC5sFVweW!Q2qY$zu$M{CKXU}oN&6vgx{Xn(_Sq8U~QM6Ts)^91ub_7mu4p+U%g6G%sGb-mt!W4=z7#T@{rE81kOm}q8'
    '<D@ach4$4W|EdSV*(!i}dJOJ;(QOkf>neNbjV>cQ7l2SHxvrz$bW2>L1}io$ug$*l#utzJ5FUV8x1J2AXR!Kqoj_tZ44v5Gh!V0NxQ07Y!Lsr*'
    'nO&O*l4}6~2)BJ!e`<$6UX%zh0|Pldq(oa=^x$S@5)G#TKG2JBl|GIzvKT?+<m0y(V>L<YA?;`;e5Z$fyckOvfd^cM3WP`^eZTW(ilISf_vjPC'
    'M;TCEVuC)*VKmQPPdEQX62NH+g91n2-4$SbZYtKNBUa=0%)>QlkO9esVm}=uXbH(cDb^mtF+i%q^E=5!wKOx4BVt9pZFN^|W_+W|y65dK{V?4Y'
    '731P6^GKH`-$HV_Qv!}Ww!n!X?_e3GgD_2xW~qZ7^n0Bjq(+b`lt)v^hE$c6%v2YXYSvioz%pE<h&bv%qI{WR8(JgD<<wyWeG+na6s<Xk{xgR{'
    '970&Jm3~P(DSAsCkHC9UER2axV4p`Ums}zDK%68+Irj&WD@Zn`SkQT?RkzdL%UrVvVZE;KB3V6-%fgp;K_0NJDvxLc0^GQj=wZxt{2;d{i6Etc'
    'nV1Fy_wdW0`s|$`eP0_m_Sj?NRV^7Hg)z_3b+GO(GOW2yT!`k62}z1J-MT!D68vs?ENiRU!y8{~(`%fuH)1RbNs&8B!JXI$*@GmA;GnMDyfguu'
    '=K}jN`!7<`G9<DkS5EGhlBDF$lfVvHw<0tmR7^)5+f~z*!NNEi|4_5uQ;z{_FM7#Cj3w9RE1yEs9}?T%pNE%xqT0`iE@0-oPY~VDD`aLDY}e12'
    '!zW#t)5Iv&IQwLP2S|(zL9vhedlS($dJqd-T>ij_%cTIX>yjf-iLu`LYW3&2f@6kF8uXZ6LL(nTo~A3FWftBtjo;z`7-Jc`4<Hqs4qBoLXghc^'
    'eYF^|l;=L3Ai*{Cs50)8%BYy7p3B{nsKK9mdD`;GD2t81Lh27v#mZ^9!0$!M=$F<1lAmV;xB&x8a+`z*-t8?SVxxM#hqRLAHZ5;;`Zy&~V4Ic0'
    'x_J_;9^^=gY_wjIg=1*C^j7O5yzpDHXYL?~CxCL$z>XioE}SM`kC^N(^D`107g<a&8rYjFDoj>-1>8|ec&K-d-S#`O)>5?6&*~ilh!I1&Ox1m-'
    '`fmY`@}z2We5Qb$0$7`7j!FY({>VB9ke%{XKy9d?tBk{vp6%QFte|`U2Z+W<<akIsw(z*pxD`)u1GMd_$N6_Gy?n6BspMn!6wV$2fW=tHZPux7'
    '00!$|zmuYJCgrGc%+8TXnbA|iWWn*7V!R8vwe%o~vukvwKXUL+Wy-K9p5r_e!QUW#1`OlXu}BvY4*CRVXn7*rC%P+kx|sKXlT_VX58NHqKtwY+'
    ')Y+mO=c2o)^k5R?@mTSf2l9M0qwwb6vn$sSLKGS89^e#I&#^m*a3nkIddwIj{31xV8Hj@?+hCxP4p3sX7yW@^*o@Qj)gMfqqy>U;pq|2Q_z*60'
    '?RvI&W<1SwrH(vlj>({Ao7C?(G_0tZY`9@;R!l%&k>xi!OO&<?O<$A<nNmm-W5o&jc#Mf;{|qQkx-9QVT2X=74xTL)9PWxwf#nlZ>_oB6{zj2t'
    'yBL)s2cIVN8RR%X{+XZYSw$I9v5;gIJ=BAA#q=ZWhV0;4eh?%;NJwBr{x-TOCoR&^cFM@TJBogbYXlG=rj!IMxlL*@iU%%LQ_Sy&D(8hft3EEm'
    ';fz7{FcoTM4O#Muq#q-Iwupa%DsHT<F^I)eP|Pv?j691OPYk-hB+ZsG1_EmL$lCv2vXu`uIbr(EJewgYN>)rCOJ1#9lThdg-fyHD5lB+5@#2|d'
    'B#vP`J#_$nfa#7&BP2#0E6+iWFQGzuZl)hRHWtU3$NQizumT@^V+?9%kcO-rN@Xc-_=BJo#hwVG2Z+GrRcMf=2pozoxE%pB@phU_BHv)BlP2=)'
    '<-t7A>fqT2krvkCCRXSAXVGiR+O7pXBk@XKo*MLvp8TanQCEvPD*MX}NhpRk`b^TVaKm2m(l8gPhomi_n_=d}?AFo|?QaG`V{Fd2qQnf*SVV}H'
    '2Nb5?tC#b^Bd5iQ%oHO*?Bi_)ew$$!Rv}Vl^!-i>L=Og5&j?I=P#Ty90C08B2~O)y(>o5<5~U~G7_Y>`xy%+e+j5og0QT#R<X{GNF<j;=YJ+Vm'
    '*}l%;Sa<!m+f&`9TM06TVN<*Bh3W){gt$SS-kD}|=ysCI0KU|t4%>rTH9Y9y$(>Yp-H^1+*ziqjHy9BU%&5l!NYh*0Hcxw~;ftgWf(ELw@^kD`'
    'qMs1VW-{bs*kA1ZcSHQ3qI>QrO;8vdl?j2v)m+dme7J15s1a`?=mwfi=K8nulk|<}Xmz@y)J#c_mSLmb;ef*#J{vo<!XI<}padEjDEaf0iBXCN'
    'q;=z$9eoq?j$}7UhUd^vYYCnij6HX3&S)Hc9{>9y37jUe`uT3#F#mMWB}nm)M@)U7J7TMKyL$~u=i2Q@KbJY3)tu?Hyj@!*s|oj(kge|LqITcU'
    'RrJ;!JwHtvKB{{{DlpXyd)hn5?XXXyD&|Q}su8&Ux2d(<qaN~<Of6xDf1<jAs${`1%q6=V?ix6+#D?R(raYdZed91GlaoM>=qXrq-!V=;EK1Su'
    'qYy<Qc$=t$k6YY9D9`yus8he3ByxK&&ajy>CUDUD27@8zy9;z6tkH@Jau3;B8C0FF*I}``oyI3Jw||giv@I6qs8<4B_5+Q?-2;(7QLO(XDwQZa'
    '-KHq>f}yU-EVEt2*IPa9Sr<k3@}}l~9F&#euW-Lz;b4;Oj^ufe8=HplfR$KLb$iLb0s>=?AaNaUB*hzas~KEADmOqzlw!5!+NOAWlYWnSfpolq'
    'j7eymjA}c_>ayWpR#c}1Y-fv7S@y#~jjUI)^q+?DW$$=12X9VOVz7a}stngMwV>1-R@`B197+R76Rse<3nr4WKW6Bpa^Pjt9YyO5rs!fm6dz_n'
    '_feGM!}R@?Ie%=8I<14P!wI55$Z?bvPz4_d+JFNqf(eL}OC!16@kTh4Z`e){p9!+p7U5_E#e{jo{9&MMJ%rQ}y!?(eam^y+=BokotfD#ysPttz'
    'uj(^BGv%IJWau(z2<0l>mS!Y^wYt9$fT%lyeuf}yfuGM7rJ*PkXl^lhB+t*#GH1$Lbi2<0>9nm_NbnJXb$7-8pR+Sdav;f(>|N%Y>0<kGmb(8v'
    '22Wv5IwJ$oUEL!xRh3DG(%fx<0SZ;@pCTwtO%fQkqLfo?9NIPk$n=qbRe|US6#B(1I9&n$TSOfa^m%kFReg_<Rs!nl`(0K%xxRaFG{@U6q{_{C'
    'oD@uO4_3&J3qyqH#{?%Z30@tC_SZY|G5|^=sM7}U9Aayta(SjELq*CozuTsU5SjqP3FE79!Opw3PKaqYTKH~ysIFJlcAHOq1nE)GhCBPa43)Fo'
    'OV&;pF}cu4|D9NPyAh(2fz}tjr|dIJ+VoYp5Da0#HP<N0nyKE~n6vyA>_^+?Kyq<wP5iRaw6{B;L8+t+HKD>Z<Cg0~Y+}G8A6LJi2Ih(ux$2Ur'
    '{yga7xO3tR8LYzgwg*H@Xvs*%<_)HQ#b!m)YaFVB&^(eJh*K?q$Z1p|B7(KJiP?2)y5`4k2Ss}u1i~>Bt6a-)U>9;NP1LCliZ+h}$_+T>-0lMK'
    'bWotmEP>}yU2i|{5KxPe&?LENHYx~R)Kcv<Qw2nuD0i#YcB5Qb^xy_`NlQm5_~CkKDT4ATIuz=YU8mxvu1>W};vTNS9f6=X6wAPNv2iZ>Qv2#S'
    '!%WZW`~~SJ#iV)c;wE23qG#djs94oq!G>$*-;mWQJ-s{R#0ag>(TJ~PtI^7DZ-YRZT4d}&$`l6SoU*P0aw*%xHvI*Onjbo7wcj0KRI*Vc-146x'
    'We`DThfGx4R0}r9gw9#gtl83aM8oOp9acoAX;?joiOnu^g`eJq^ar9ZMcX)R+lDfY(r2;12bBfHelxWl(OC@ko|C7bhk~^<xee*C|Mk}7P(DG&'
    ')V2hjz-D@e6fO#*dZFbOf9}E#g~#3nE8SDjJK+d2NhR7rqgDS0qD~7siD0<eS&F!U-0xNuqAK0?lTK6E861T;s%$<>6Z}y`LSWlX?;Vb2X`UyZ'
    '!Ey|nTdsCEis0!eKyM|;?TMGcyL0SAo&Qx<M()b{2b{0NzVnuIk-_2XCDRJQslArgh5W=T9?hgvfr8>dnT?PZGt$2y{glWZ$pGYR5E`J+M>-A2'
    '<wMMVZK}PUM|!3v2J*c#JMG}o+sn}@;ekxXrp|&FJGw=eA)alJZE#WJ?bYq{+8c_>|IXYz+TC7*EMQ_5h>rI=!?nE)0dfB3TLkyrF4*Wd`RmK7'
    'lGsV=?KL;zkOfIXarjN>#W}A^*8~Ri2>WZ=+Yb;c@jka-Ds0gg%q6IsP+a7uLNT;GaUs>%ns24u*O<yBmVN>i5@=G75PdBsi%9Z?DHZCp)ev(&'
    'h$LO9gZ3w)>czVbzidlL7H5fW@r8H{e<7a8jl#VsQf%PJTr~Ag-DC6yZPcEs$BHt{V!_*1G;I^0<7Ru)c#WX^tbn;PJUwH-E+f0|W2-9crQsK*'
    '8_B{Y+{O^HeAA<+*o&B?C)(5ghV^U|WYVUFbw3s9wia~?f0M1Zn7>9yZj~F31y1kEp0vqi>oSwK_01KX`_<2kD!r=8CP3PUlW_~S`X$P)RqY82'
    'Wj)bG<jo5f*)(cl_z}BNPlR}<zqnemb1WhagiK*3YnfPl%vroo^TLg|)tyq;X_uHo49x}G-i5jUK=iEu|C#3G*UpK+vYS3e?_TI`VpUuiW$hDa'
    'BzoNz?%cMqWoMuD2+Gr6n;P;(pCSeh_B+LT1<IW=-4^5nm1MwflgiurIkIg<AU09my7~`9Kc#pVl&=Sf8?zr8&uCGYYICVamEiz3Zp_$+m6IAm'
    'BAc`WBwd%hj1m2qqQQMDfWnz=NJs$s8BbM^Ze{$2v}b5bsJ5fPbh`Rcyyn_b({zuHKX6a-3)g6GT;Oh)s!v<PtvZi)J1gYiBFn$w)GXh2=9E>r'
    'J&*x`L)%U`bM^`1Dtb1g7;uWikIuJruC8ic@}T38CRRW15rOqC&n5J91hr-Wb@E>p7Sl3RG)bWlibh;%5|btPD*4F=!7v8LEDS8~=1lpPvleN<'
    '7B#p2s)*Fy_JA09*fK)@j1ku1eSLz(Bmlo$Z?-+KSb^B()n46xu%-t@S$q)e(2{T*$+ZaAGhtIYoHT+nTqqw%frHB>0uz8I8wH^Rn?t1cKDqa<'
    'f+TGovm#8O^2Ul_JsnnPE613RC{mO*5?;W%?1BN!C+?EM$B-E-HJnL33z?Q&PCX@jt>eVxvZJv`isKIj=cz6#rlOwj$>3z?_pPjCKiNH4O#?D)'
    'Qw&RKGJ-l+a7<FXpUB>$T)2G(5xM;WRUsXx>{Kn{cr;^d2A8d#)jt4FI!q!464$YSXJLq!J@63Io(x}p;!)|~m*wn}My8#cv%RFWqJngLTLZ!x'
    'VF_0DH=Bn==cV?#B|Q;T{OS{tn)`%<Sq{6u1BvHXZ^<8O40)C%Gmb>lO9I4-jzQc`NN78MjS&|rTLt?t7zrxr?6+8%Bdm#75Ut1)MUo0na(J&6'
    '4K64?!y&!Q486}qXm0&;Wm054*HCO+R0gyfN;EmkIXl!D2(cQARB_9>l@r>ze9b87358ZoCn#511AERjn|3`K`u;}rvjZKviFC$GB897aAvJTS'
    '(uMKV+-^jzIP9GTh-#HZuX`eH-F=AR`}2AxO47uDw&0=cAr-*=O3ekX(DVH?$rEt79}Y;z!Nlo%&g#678ooX+WKs+$p4}ieBj+rVm~AwADfL{!'
    'Z$KP_Y=O5UMxiG=i76{}LOUNNZ5QI!JFIG|w&Pz}m$HCZs0p`AAE=+6BG)eJaO07<q91)ra+ICM%hI_=Q0)Xc&@$C!Z>uni0R=M*(SFU)RM!K_'
    '#$71YB)J@9yKrt?U@fZR0=oZ3yti!?zhO*4^y2|HJ=CVoEacql6S2tTuu3>Q)0RrB6+5@M_%>^Dgm)GOH8~la!GamR_+=*(+3_&fj}g+#L48=q'
    'KXEBeWr|ss>&@*xBE%C^LHL(7FP9-S>JjXz;xsBM?x)}NAll(|ku<8>+OQF@0ox?OKD-<s$QeN{zl<|iy+dnF9AT9%a+9~^4Vhky+U|BF^K*1g'
    '3imt<r%=(>g>laa>SU&%R>`5splpRLI!pw$MAN|QtUaLQs)C-5WY+T<w4LK%Wx;_f`uu7K>WC-iA9{uy6|kN}3WFSqA)q5Vcj+G7p!J_pf{H`4'
    'sZX2>h8<Lrj;M1*h+a<Q)Z~C$%+ZeZ2Apybm)G{PtCih|*oGD9E#3iKwy5fom@Q#QCE;(d(Yil@)aG^~316FQTWUSxi(K9lM%&;MwP}->`bh>@'
    'G6I)H7C}n)`~~Q1ai}Dvk_JY#2#Ar2H$e3$*`@vzYL7#X`~T!2u4<-n(T<DmdL!XW^v!!LIv)qo8i!ij-la{@=3MZyo=}Axon7|QX*(C=*dCDG'
    'h8L;7zaf45whf88u5H!K)Qzr?c$!zytp-+E{({Wh&{4TsequoLCGVb^=lUrR2ap~{rEaT(ZJO=0p;dL^_W`7Loy>YTOgAJlce!WR*W#&gduw=x'
    '$qxb%*a*>=W6xc#C0ZQxs$oJ*4}+R`A-=Td8#D}T!bsQL<ONA1BMf2hRUy^iu*F|zThJhwEtD8Kr@3JBp=hjgRh<X6-iIL!SNe~WG(_949(j)B'
    '*KnfRevoKamTZ95qSCWSQmiKwQm~0d(8ryM!#x~ff9P6C0Z9ARD9>S?go5Zyb5Sq5VyM)=G+&bw{6_YS>Kitpbv4rQuJ)}*4|{Ja%sSPefkm)R'
    'VYGdi(Q+Zjb=~%73S#Qj@&{Wy3xC5-H@YsxXV7X@&&96FETQh!(-EYvOfEpQoe-qrMSyyM-lZhF=z^>d#3qxM4&D4_ze@lQO=6m-+V;SsL|e~^'
    'tvZx)KRQ;Gr6@kKl3x)=#P(0`vD<EatgSwy`%U_>5`^2CuB%4*eIIByNecxQZvNVODjpTSs}?2K`)V$Ne2_ZEng9u}Owk@2WWUMoMT8UL`P!J='
    '^wcuQ?GpWr>43HCFkaH*{{SKa#vdP_w#Cv!|HJrvpqA*d+=y`f)S|@#{c~_Fl0yr&_=~LGzATNqH}0^5oY}QLB4&2cv_pi*s$;rW6_)22!JbnK'
    '!xWc@o7`BkH&83=EovxoCesazM^dK$@BzC;)$ItA?IuQZZasqSCs{_(9~~5I|B&6`{ze?)B%|7otS`~F!H#2N#Y~$7DVQ+p-d=(g((Y^(3O1-F'
    'aSzZ9Li&u1&{=+Wj{HDT`=lCai!;CNZ0Hb9A{^R%RrjNkY6N>{tY8gOM!_+c9u?AG)q5%afq3Fu!E%@0=H3Cn46ua*JaIskLJO41y1cJk7mgdr'
    '{U1I^w(gOjaGxjen!G5nd)2kJg(Ewe6g!V8Tu_c%7TgsS5dF|{&70i5@a$qGe}Lu&?-F2N)R86ye5g5U)`(8$b{FM3ikx&~zSyd$>1IDHo2<{;'
    'TS9mptK}u>FB%HZR;QL71ZoO2z^qzO>Cw78#X0_<=Nc@mG$>IGxkf1}Z0@D@9xMZ+T0HYdZGNc<s@NTC(O0QS+oNT9Gpdl=q8%Do%K8Ma!j+O9'
    '9zovd!$Pyc7G2D-wZ>A%UOC~B_>;kPwcU9YTLcaIl>K$Ks)2sL9&0~|m39<`9BI+iy?giST&IN*Z-RxCzm73d+7aT(IPOmF5}BrrbHkJo1c*ev'
    'Wj+VSUDeLS?Bq1NdT~!0c#H#A{k2DM{m$p&gN8aJMF9#WRrA%NCQ;jq6Dr<_%5=aGzmG`HFDWPB^ta>-+eVAdkrRSgl#Dn5Au}?%blsEQDYsYH'
    'R*z|M!vlMRS%sXAb9uHPKz%AAAu^XDs5{fa5sCIUnQlV6(S4G31-{7@wr(g=MdUOgX$q1@j4)iK*TZQo;i-IzWQiN^1<Mq#U;CCf+)D&Wi3C->'
    '-jGw}kU=xg$L1X)?P~i2+<jGJ;3oYF+wTIB-61-DL<Cs$i)_#D&_u18BEj(!7R^1`SgyM^*ETH7jGx%tOM3kFLskUqnLrd6mUD8j<lq4p-}17P'
    '`IdLXq+WcqDtdNu<&*wRoY2K~q$MYUde>L&TQD8i{t2C%UwWo%iu#4%08wy8jML(sem_>O9@kFDCXs?r?XU)Eoqm!YNWjx6$m^sXrZy=<R*@2l'
    'Tj-4^F1K5YY^K{2lJudQrqG4hrI&3lG)0h}^suuSK~Zi4l!E4VB}JECmj%oZND0=><ZZAPi?xf+2AUg-3Z8#KYMn^m1Lj0l);g1f_Kl|`_t-hY'
    '5C?IjjCu+Da_y}xac0qD*qdHBInn^pxknW%9@xx6v_Pvs2UAcd--%rBFd0O{Q@8<32`WB4a%iVWEMta9nxgngV@fu}A)!EX?d>;o6%$dcsUIi{'
    '*|2W0jZLyle&cBn7VU*>w8d=)(*2=F^VPE+D6x}8oWh+Lj_WsYI7@D4<?@Bgd)rEmy=#zm1*A98{+~_S&Gq{qWMd;-?doduETT~32ij?Gs6O5;'
    'e&ZNVTssj-LEv`d(+B0@MAe+;TMs$^fYj3Ll0!j8oC?IGGe+tLED{aXHrDintb;%j*8claNdvGQE(yB8F6zL5@EPH1aazQQfD<PmNh<ZCWxrL5'
    '&Q$uX*S5QGyK?4q-J^2cJ6$wY<ct~IoBPRt*e$Dwq&|I!wnFF{^QUSM?Qm8P61&bNKcCfn=b_>blMHH!vU?-QefE<9!DSHv?4`<wxFyYZo_xCf'
    '{XcF*>8jAax8LQ*u6|e|I$Msey^NJY1gnE&asg3`xN18T#<y>@S?yWnR#Cg3DU6OHBZT|f(;<N;{IBP%E)w$>tXAo$DCuas5h0YUgAZ0$kwO#d'
    '2eq@oO3=)usyZ}eyH##(itG(SjMk?ITSLVQ3+doN*vUYV;k?l%5|rm2XynxGXM6v4*Hsc(bqfibk}iGD@KtJ_4qAv1rlX{bNmlZ-1W=01e?rkD'
    'YJY!vmm>-TwhN$>Lpocd`vY?M=*Aw1wuo^hrG%{x#LG3q(A3LckQ?H@Bm)H5Va`EiU*eX~G#Ksz9&FhVN!igEEe0zY^fPmcN*}p@LpJ(TyL!_7'
    '!BQVUH4s%qW({KvAU)WUI{|99VkF@e;t6g1DCJ$1(>EmTNYqG^3#vKpbOUG@7p|jeLya~brz8=9;C>_i6(-(tYE&?yY%kd|*BW)$NrGD`O;UJr'
    '97itLfP&n|IPnz<Z-{C@M1pf;(MR<;TBT2)#y1c;&$fXsKbH%M-$IL>Z8oyIrne&k)T*#~WxtG@1SD%)iVgDW=d`r_+FM6&M2b+h?B+ZHGkYW+'
    '7=|vB2e_CKVn2wx9c6Pw5hMiW;m&{olTn;}A`)aaY43l|No-I!O=Om;Zs@;ob!bg5yHas!1hG}RKe`$;!PcV}BiKRm8pBwx_jFuy5`CYW4mKV)'
    '@`Vwgb^$(&LIoEPQec?A<vZlz=%u6BWvZ*s8jo5tDGa;Pp=pyv0v|buuEvS3kg%`~lHVn6J{M-cVr6ALNm#E36fPN%B(`1B@0%TZoKyza)IYDv'
    '%<Kp@`v{T5beUmP$>f!+%YaLJyHe_BfEg-kck&>s!#=pU_zr0#k)R2ehnLW}n4x7PE*nu6=enmPC7fz=cD5%Xxe;Wz+#H!k`Ab#v&gG~750F}s'
    '3-W^tH5(*qT(%aAU$m5IAZ&U<l6JLdfF!-I5Ym@YKA<u!CFB|*`jT8Y-r68(w6I^=#Gupy@jj#C@nlWeuXb*g3Q6&j`BmXm)YZI?=mmO|Gq-E%'
    'z$LS5INi=(S!zrlaewCnX(929Zj1s;#>Qn=JB}1X$&t)xa31AAAgVaq*zB}>azYW)(@0$;oSpVjP(8T-f2{*0V!G$3y{Nuy=W&jZ^ca`@JUImu'
    'EGC+%@3aH09skViO}__dNkGPK3w{7oDcx0l$F9zWczgt>ZEcZ1o6;?&n9-P~e&(+vOM!7NI0uhZ!-9qV2;VSP9@<VZITkG-j}*ZUpPr>7-_P{4'
    'tHefVtm!7Vlj>4O>;J$`;wWU{$EWF6ddm#dj+>?&cgs6EH9WwfMSK;Ph=YQtQ>Z(t?)Li$iYYNA%TrbMkP#V%hEV|vOf|_C?QtIA*l};S6u&{r'
    'b+x+Gk;GhN{p3{3>_`6+@slW{OBa)P6`r@`6n;mVr_jPKLvT$l*P{0#9ThE$CHninkPwpX1a;$mKy_O8r|qsbhipgpOlJt|k@$w_1~-P3#~{z5'
    'MhKe|x|O>k&IcCJQ<^+;c_&ykgQ^>Pk1m>rz0w@PddOja0VX5mmVr$+%@EmPWq#`6|BcEnLnvIlJI`+&1&Q09Q8*Drz=FsWf%@XKH^EQMN(XdE'
    '#^dA!_iv~ap9)@Ed*A2P94I;v&Y`s6J{2*luTkvpEbJG#%pP5m$AD*e^QnRgH94x9zG#pmu%_2%sTJ9dtQO&Ds#5x~9}MBHPSkWPqVu3?)btC^'
    'mOB;gV|RX@)(1`zoP=%mDDxC?%I8VySeyg_H;Gq9GcWB$+6qQ<)Z-g*P$@9O3vyIHAnBp7GY<7!<ZT4IqiL(yqf^(;)EBboh~!%wb=Gk#gdeCV'
    'xm@r=oFX_#Y9rzby%alZRO+H(D(HaT(IgqgMvI8pcD^jF7;XOqgoU81>}e4~t%js0EehGi^yv(Z5LuX#D~A49mGq=4Evo_TJW4y%{vcfTEjmWl'
    '0^V?s<rlwQOd(o}O02-2$aqbV8MCt)2{-L`<;Z=PdNhWGH0V{kFsom#*vcz}(?$+}>L(%&s(g%ST_~^8<-aPQFIW)h&H2?BGic?G(l;!cSOw5G'
    '&zqFqMO02KP^C%!yXa?oTk(-kv_0$^;zKQ!vAWuzFK4p-0NvVU_O71;Q#EM>L*dnr<l?5`P`942O}vzG?xsY@kz#1&fa->I-lcPswn#n5F)3EH'
    'o7P6WwQ7fokd#<q!r6VwPh&ggIZ?VKY+$m=3AsIhoTW=K`I8OAHvJM+#1M=6piq^GP_>&gPY@PX_OqfseVuSXWY+%^s700$W;>X2peL1+dHK{('
    'd_f+Q>E{z|>Uprq0|`NXaTW(vC9olwaNCK{d=rO+mmPxf*&8gL-(9L>=JI@jl#US^m9Es;<!~2;{+`HXZQ|_kO)AF1^#w}V5{Vra4}wlSQ;C%y'
    'UHmG%;Wn+DOXPnR$$f}$A%bt+SROYwqTrg;WdAG()y956V9}8gk-3-Ol(@{G5rJY*4&gkFvYWh|p`ui({HO=|6?{t|-EVC-T*DluV%GXDSkWlS'
    '4c=}Z_-gMN11h3I=M`~zwL1;zVyMKys<VHV9&iL)oLlJcyy56OwT%Vwr)jgT1#riG4+3+_Q_7yC`p8%=sSn6)_8V@YxnW1|5!=?wL%y2aaYhB8'
    'jTp@rdZYNuS-{IfJpWogHoM8avIt~l;0*+vXfdekU$lyI2*rQC>(3*v)MThd`#zrLScB>B+b#Y3?++BqX{2(-#(8@7vT&rc;{sE^Kc1dXcYE%r'
    '>nM6DELGdtQHQyx-Tnm3WMb{UBKo9AwHJzNWXT}zhfwoCAp}H4&z?pi7o=`#?22rM{sDy{-oWHZ95K>*w!~!rI@mZusO68bq2Yv3PMxk1rsxy%'
    'dV8EdplH&>&AP02OVMmiF25;bK1Q`J1k_1G_wt%vm0d_>;>$Q+-ou+U3Cm$K&MmR-#U4=Uc|%;%VwwoR!xEWgA%ddUG)3^z`O;vXJ^#MS+(;|W'
    'eh2LfDhEA;O(*_{AD_}72z0Y5u;-*;4EKq8Xq697eM#W_jYo^=g(Pns_YSoX+lSeoDRS38#@0OKD%ud?3?eo%NEkXCQOY+Q%3(GWEbdruI2yxk'
    '*5Y)Vxc9|)T4Zom6Gp#Kqlk#BwtJ0k{Dec^gC2UR{~HuMyQ~&XOub?xg?Ki9$P>WjMV<&-9*qm7(>GA|vg;gYEU>3K$r^ilB?j;aXKfq?ymGZr'
    'PVfXBY!<Ynz93-K@UMr@aY6y!E551(;_+6Y{E=bhEyrJC|AoMUy&o+qyM4AZ2M-cSUxe_S&YFd`DsHxGx0M(1TZpE}pUxoEc|;_$^<*Jy@l#}C'
    '`}l>148A?x;nJpdPD5J}IcLLp34uJ|9}}JtM=!a;EcPn8W8|v(H&B*b<6!jnuqgdj2-F;!ut-9*>LKa2?OM9GRW_Ex(+%j!q<bM}51y1nvf!0i'
    'nVk>KcKA;rf?q3Al^Y4{O~l}|_X&VJ?*l-B85iY#?>AoP3(lnLsEi?Epy@ysk;5?b4u_Fs9$I<JpBHq15Y}WXNpH>arF$I)_2OdYKhTyh+D%t`'
    'XZCGGdFf!(e4xMn`@yX~T&i3v%APgeZCDEqRBK}(UsCAmZdWnc)U3&kD4swn@!o3pxo+0~;tlN+Lu3-h7=1Dni?k44+0CC^txp8|VDEDAa^8S1'
    'B&5?}y;p+6)nYWiB+wUB<aK5!S>EDHN5{^4e4}O5#V9HJgvdBr5uBxkKVb~yH-m0rxV&6nNME3_Qv42tY{D0g@^p#%dAW}#idUNr6D!yd$b%$#'
    '0J?b<ml@QJ&WO<K_(ED3&RQgI0o#4)!HD4^G0%xX4K+j?9`SXWKGn5{t|K&~ru|_<zB*_(k;Ui3WL47+&Ir>V<MH5O;`fLOmYAhf<z3<pA@KP`'
    '=;}dP>d0cBqqm1<JKG_{&~l&DO-7at&bGQAbiiYpsChld_c)Tg!EBRW?`)%_!DhwRB&!27MDy(%MVFI=Ur15YVd?K^r}hI4J4d#G=DU!EIT;gq'
    'yGD;%V<2Bl_A=DX%pJer^h)hOK(LIyfcB&x$3kS0IBoZm@`$})GPnDW2dJ%^Dl4@%H36a%NQBh=HLZI{p1;V|_c&_07KZheZ0lQpJTs3LXm3G2'
    'L7ab^p%}>c*+8E73qX!&^;Clv#W*<4>=VSDA&zH&AiTCSkdldi#(hNC@9yafsV&*iA&~c8LAYEFf;PA?>5m7et=o<g3YIdPwZN&0jL?1N*$n)F'
    'rvr<o6vrFFo2vb99Is1?HzE&;=y^nALriTHT(cBZ?3%~M_XCa|Ep*s$w?22M1(h(<?2~XvTk-Ssz=(M#?JC||M_OL=7Pg}?-grZEut?a7u#Fr;'
    'Q{yYd*bqTeud0z(T}zO9m;ZHI9cKUf>oio7xf)*1r-lM`&kp)b|5^;AKpgU6casdp#8yY$lqWZAvV4!kwofiR)GXPJNS?vJgq2H~`Hvy>^Uw&p'
    'BqonS9$^=kP%7^h97#0ygGF+bQwcfIGbwg0YICNOOVR@|pjH<h%E*hWr?!ezUkNS&w9R9+FF>5Qw~Fh6B8bOv)o!rz?G2%{z(iu6-dPS4VI&ut'
    'K--2aLE4T*;Z1|{@sNn%PTkb$A7Uu2gZt}798>C9IO&pF_gTJQIe#G(?T}=%MOdQ&<U%>bPM6YBbjiSYEBlkH6jZ6iQkRfl=+>(u{g&Nm<T09w'
    ';dT?5A**!&C&p#JqN9}*Vilk13l;yOs?EY|2O&epvbP0~><Oj>x(oK$um;w(R7R*$M)AsR5NE@hOo-~~cY4H=za-lb*DI&?g;e2s+sn@%_uGy1'
    'yG;^z7?^)NMy)PZp*&!R$m3JqyG4L#Yr5z*d2)Y@6gD!%Zmhsb!l^P8LQH>p6;kP>LBwKnCdiUV)+NfVt0}x8ARjS}rk7YXBupKaI@VF=i;gS+'
    'eQUQ(4~cf&KPd;4BjIS9n{DmW@1F;-kX$U;7Srceh%TCiX4Egpt^I-LTl=t!YI5Bj=mDe(eQz)1Ua~|rK7gH7nBgOT&XR=$ip@nRc#THa2+_kP'
    '6jTt=P;(>3SCw8ZjSyuT8d0B!Z2lJez(LnGkHiV;3KM&}6wiK-nhXWObh%DXICsU#6U@nC8;5g<Y|T#Jq;&nTnKuaODFo9&7tec7w@5Oq`ekiG'
    'yI)t<CXYGZzcBU|xlnX$yDy;Vj~}9)I~K2w7)qObqtOYUVAWu|#HOTyd0~_Ga=Gp|7M58|hV4x@N>|K@`rSgR?T<I*8~l*(kA%IbYvp~lgJtsy'
    '#{3hj4|HjCm$<_kdm@ADJ(k9UynGDk+kCHtuJR|D7U|<SJp64Dw-yl)-iRG*4|>7tN9k5{H`o}9hDLaKA)=O>T@@?<IP=5iFx0x{I|x33A}rXJ'
    'rM#w{<f2C<OlWL=PFj*v-Z6$JUWlmi_#;Dsrre!gGLR_^_WA`f)!R%xx(JL8c+_|oOC?Iw0;cVrpt>DDsfim+c3nMzAuKOpv>R-A78ubojf_fx'
    '-Sx2^rvWHlT^>bs)Xy80W6l=aa<*I8vso$U{p0cBRBC_SZO{{1m5b&SSZSsGF*2<(gp4X&%PfXBnj_hF`=aW(lt@1#%ZSR_40PgWMW?x<3Z5}|'
    'iJ`hbL^?b;;8m1|D4U8;i*1mIlL)FSY0>5DXb&&JP+&KJvylD(@u`;+*)KR#HkHJXm9K-h%>2N>@q;$SK_jq|v=n&J+}#||YqUS6y2m$?5R?=?'
    'TOm^G-fQi6q~9KRv#DR%-tJT}dBPR8fD~7=73J1%FSRa_`RWPvNztXCE9$R``_LU@-(#bd&YqY=op_G61=w@8U$mu4{5#ljP_vh%ilhrVv6EsN'
    'zR!%GFBdjs)6jV`WacNqZ|@-Q<lF6_`(1rB=R4N0|Cz;D=IPljUIvUB5^X)xyT{)sRra**P>G=djgj_>Y-MpmyaIA~KH|+Caa>j=DaR?*X|Mi4'
    'Z8e~ELa7@Ko6-rTNHt7nI^)nroND8>v!=dU4j-d$_U!Kn#&q?^(;WhX2}nEFraOq<h~od#`a16-s;xogtt*WpXvx@&3ZJNlR<`CxIj82}D&Q)o'
    'P1m^Bkcr_N5rU<~<}`=ig-uR8Y%I2&M7D?FH8_5!+}?(1T$^#dl;QT|{S=S)nJf0HhFd2fdL&F~3Kn%jV<U%65CZS7pgkk!JL*8~HOY306lvEM'
    'u1%OM5iscigFLsW@E<D?4p>fs!R;3jzE{xyl8<n6NBWDdEA3da^;L5!Z_lPFIj|=pQv0^a_u(s!Y_fFi?0XiU7+5$y5A?bCS)i|KpzTMbj*GTp'
    '+g_I~hC9+fX!I`)4+}gjMjnn349-z;@7a-~C2@i*;;7_Om=kwbWHzT`W-!we(~f6d)a9vbQ9H*vo+fsor=&1aI6z;2bVn!e3!gNhApPtwkoPm|'
    'JSDB<QL!HY9*>Yonl@)P`&@)_W@Cj==Q1DmqG@AY{uptEni8LSqp)6(gm(?Bf)v2<n<ITRU+s71N6vQ=r=fA-d>w*KZWf~R+e|6kEcSY!HJeV#'
    'p0Cm02abAgcd->=v)azBwxcC}@;ENwqjpgE0P679ks~#TAvKQ=q@5c)W?SiEP0{f<Wgcp?E!)!!!Oo@H_Hni3at^?!Byr>8vw*{*^7H^xub^F%'
    '@(HuZ^K4tSbVX1j?JL?YJ<g(+3Eex<O+6af9*C=>-hwA@idt&>>TYhClNhQ5Dde$j*7Oy<G>=3YH)-2ynIar>l!3b@ViT)9dz5fs<kv@UbQvBX'
    'D78)fR~>!<yDJ|}X4;G`Zln0<v!32uzYBITzRnr&1|vg4fZ9F$$%v&l2$u<Mf%UsS^6iC&{g3TA6d(1zNHAs(61qwQOwI=y4g*vphF84AHA_-1'
    '#tmgks4kZlK2DFhsxFYU$UtGWQ^QS{g{iz*e`4ZgN78)zF^(NPE+ca5!$kuweT26?HtE`x)ekL);fq8fcq+E6vyXJetaMc{OPUJ769O$A-*&Bw'
    '4(}s~ET7^I-*9SN=mzF`o~6w)n+WxZgM1!+PffWW8wD<clUBefZ~GkdJ%Q(ezdd?-|NWxIf~AKPJf&3;d;P*g+doS-7ZY|;>l;Besp)`LBQ%fv'
    'DT{WcoJq$g)K@hvhtk2hehrS^LHeESVVrxYJyCjHk-{LbsYH7@g!CK0&L{+_AUctOlCYgH(L{lX^ff>n_9p#jK0=B7p?)P-m>g*5-d<<WN(yWV'
    'Nldbm(7~xbLfZ?wcLLHK>}YCANLxJ@Opr~Z!+PztBN)v_3JlevEJuO1UVSnm!t{cS^g!w8b0D}VI$PFK9SFcvr?GMF)dMh3^U&y(yIVS7QznTs'
    '?OfPw+Y{7X`i(YZ!!#9tW%TUrT92^^<+0Za-<HQ)Lwde8hxN9Gk}=7#kVMmd^1*V&Al}UIHcN?Zqb4omiIJk0<&hTdeIyx%3y|6x^{%+f-nK=c'
    'l2!SP3zbClp4VH|31b%eW1y@M%g#6)IhxKExX>c%vZD(Hvy3OLFnPXqBlEUcy0QtC6*(saQP9O*c9tXSGJYqb=A&$kFq9lgrwirf?Q$wj&V8?$'
    'Pn)lUpnHxfVD~1qWI?zD7X~Jg-Ga2aqb<a9J%i+JcdG4jp>zoiPy@8FSvc|>6Rv^qYHbbY`#GkGAwz~WWFMpA5I@G{^B=1z6id+MqaT5d2*?dN'
    'VV3)RJCI$TKgrK-xh1cPWL|@&Q2v7R<(BbS^vW~jv{BrSgY=Kxtfm{8-f*5hS;VM{?x<UGsNw8RFqosq>7iC<Ys>eQ10QD6q%S%epr_fp_pxmU'
    'lYa#%y+yVRo=;}t&TyffgpZ0T+~PSai|C3BkCeJS!QtKg9~3SLxI1X12@)IW3S0V$vBvKfMxeEGs*#5949Ql>WQ!(>Ds_MLI{FihQtu}4z9s3{'
    'NqJ{zr&ANO8MQ|A&Q9iMU@PHlId0kXS;>8e@}-4qz5mycs8mOozWc-ZUoI>((V(cO&Hm!Iyvp*H{)5@zPL#2lAKtL$F0~E7mqirB?n%)xa00JN'
    'gO#^QNNb7Pn;vUHAJ-*+!CNO-*xHlnreK}Z7r5-woORXu73U<J2JVP>zkx$capAkhv#hcZ&iR2u907BABOdtPDY<#)wmEfpC-%dmWpFZ2%(>xQ'
    'Er?+x*a#*OUEthrD}9fRy+2W1yu*a8kPByS0-^TXHK4z(%WQ~tp|{hX-I&mW!41blONP?S6K}aVcTS2k>EEFq+)*{7aL>mhIITYfycADz8<><%'
    '_V71H{on51`B91QQfIdNp_H%a6>5t3@~;YMj^YPFMsU@Yr)dnAMt&2F-NTCam)s~#T`0(l(?@pRTgNXLxxBopyvecLh%o&)XqcT{l|9Qa5%NRq'
    'K@W15Pu}_r4c`dt^oHNWX-AO%rQL@M7Sb<fX$0tp!JIIfw{BJiY9P#OAEXV|+uA>X@LQ>rxtckpLz>?(Xh_oJ<aQk4=`?_uPnDSFz|qweUDP>@'
    '2#du-eSW>WNfU#7Onj=alAwJIvYil~?Ldm&q6c6$7g6z)+QWe4F<e`pw6D(d?c2&p!n&0a`c`mcwmOE8{<M>EM>rmwba{Vpz<7v_2Z5FVp6D->'
    'P#-vm0ugT2>6I#&mA2sQJeURLd)t0Sh^JG*c9JEb7BTwmbfH7_rH#U3y|w)6al<K5nR4~fv3M+WFX)%b&9H{k(i1_*Euf1F7Okozm9#ZC;uARP'
    'U(XQ6I?gMe|Dvm_kl&g~`X{dvuL2LjEQG{+Np#sEI>ANk?|6*>KR@y82)^X5E-@lHhPj6&rm2NRnTNT<Nup&3(+w38E^>p~xGkftRe+L~on79+'
    '#z`;4o{xd9y#OkHiv*25$NAe8PB<Q(kXW=ODw_bRPJ>j|@VA!1Y0i0g<4kHp%a0-pjTTgLG!!W!y?*Z=b2uFXaw=yx0dArcNM`_W9kNMIca-jM'
    'lN-Y--m;0^3SwvmHBnGyo~0f=M$ZOq9^`O4Le!4n0d&WMM`(EGwsY{Nx_4!0bK-`oqr(FAf)eb2JDq+LvFmSlG6Zjkn!A$O6{;jDd4WRsLF4ZE'
    '?j{*c3nHekJdP_HGff(}bz{xGSNIZHdDJfK@<R@rK*inangebC$>%;I+Ew&88<`z@03`P4mB8R9Tn36zBrmfm@3|6bm&nNhvSpCVEYC=NqE=~-'
    'T_oFL6AEju${jrqS$vjwb}k>7Q{yFkNa?a4#R~Q2D9tmTc*s@R8%=LzM@T<3o7!~`LX#9WT6iERH#G1BsAx`zR98s&voBZia2Mg#>Q7L*io~j{'
    '8f2wIOA>o@HL^d=5#T8tL#hN*1DG*(zq3<yODKmb(;$`Qvhwej$JGEHxx^)H`y$(eN~_Ipd404HE&spMIE;D_+<}EPMLPX?x}xA0nk81_vRG><'
    '!`4uIu}|+c(aun7Yx*7MWTF&*t#c!>+xS3rtog8L@5+aub#UnRw>jIq?$N1-!e!-9Ldpo4{|L=j0G!Y*LqH0R7F~*~VE2*)n<UHc?1Ki$4g!{9'
    '6zL0Xs24COb?yDF3fSM?V?=B?rQ+NoV^maUrj9z4Z5W*R_y>;m4hl@ub!rhyAzKPR7~Orll_OGFe=U*qxBb|_eQX>fE?SkELSjH^SA4NS<tM0p'
    '=-OWA#EkxGlpJTms+MmBEALp3k#)hDRjdKbASP`Mj%M!#G^cfR5|aNxd4uLQNZWVau~{^IA(GJ$Az`!rRenR5`7DHuLX14XXm3mw9@ph>u@fH8'
    'lpmOx%H-^R-Re9WyOg4wRA!a+b2vkloEq#_@NS%sQMe#zm8;Pmt?l^)6T!*`<1=Pp%g{)9!z<v<lu`u7K{SXTi^nT)X!D87@XOh}ypi_vaiW&V'
    'UhAttTVS%2n*WZa{D@wjBEMn3hQ^Mij1ktT#q%kHL^Nnh;eW<P>wv9v-P-X!M*#($#vvNap5bfL+N)G3w>)<%m2DJExH1o%h8AefsOn@D{sW<e'
    'yTkI6UV?ydRMk){+t3m%bwx&yD@meA&>Ytum5aEX$Px>k>-0cFH;CH&F1N0qJ;SALD;oN3k`n9N^Nui=zdaO5+$M5$4p3WrOPFZ5+nzc56BNed'
    'UZ|D4K{KpM;$o^k<mTuwjzDz|*`1C)Ao^a=!yYgjmfm8f?m@nOKA-E5;`uI0)#3m^W}QyooMoj~OEgG@f;LO$R1b;Tf$T;~QC07e22$|)bC855'
    '7@#bFk4Us|#N+stlw7VodXb6+X?sRy3wnS}0OVI)qH(wuuHpHbusewGB#x#6Ja`jlB2umpIH?qc8=A*R7NQbR6a7&_ZEDiiw5d!RH^%sYK@C9J'
    'v*=;cN+fM?q}CPvlpO8W%2y38_j0x9FI*S!)zR^X$_(dUku~)P4@JL(aJeQ6SND!e8}~0t9l1tpP^l`K6Dl@Eb{WUJftQiwrhKWCl|^s(!0FaH'
    'IncMiyF}i);1_NeH!RS4IK1gkGm)anL;{=qI-Lw%cti&|ul;;?86pr<AMv8ntM_}*0Ye3iJ*Kt@&jY~?Vb}It8p#?R%uEvDQ5D7TO9%W^{|hJX'
    '<^rP-smsUIP7^5g#nFDBEA@_D78O@FH9@|P`v35UnN`f}?FnX)IyVf(xTC6X20;&UxAXzc9)bD9w5u*giKJ^SvLv65aYlvmvq{kO{6yo)Ude4^'
    'IvyQl7&rBNrO#XNAdkP-$kIi2zb%-I6cZGEpvaOnw)8-wwu-ZYRsoD@TNrjM5!k9E+){+5X~`@>+=@_|#z`{i{vfeR0POkJva5A!Ke25aiMxVX'
    'R;6Rt{&|V8{9NeLO<C%C*dm=v^^GaXF_a}Vi}HHb$k3Kp<1Fw>HvwkAc3JB(<{LRc^H>F0*mm6R7TVv%FP2bNyXe^-s~oh9Pw9{^j}C*KRj=e&'
    'p!#Z&&4}Q%S7L9<v8l!zgfK0#9{$=;xH=TQ51bu7C`(Auv0!0#*?292fcep0c5R5>+lf|erS$0y)^?2b?5P|wHVd@kO*d~e(l5Kn%M3IM`D~M0'
    'hxMzJAhfM_yn^<g%`*<yiP`y7wvkTL%%fcU`QjjoC6dv__bw<z1$z>@d`XK%E8v7H$T%-FwrH=#4v372;Ans%<LiS{E(a+K_$oUeC1&i4pz^}x'
    'LmLBC!U#|E5_JnSGOM~$2hUQyg5YsCuW3Et>ZZe=E6KdCuwvjXT6drV*|V_OvLeXkkT$pS*`13>&d9q%mZCd{KDhZDB5ofTL#XydvGGzP`d*dH'
    'orR1uf>WSE86E{n9ps%Py{`+3sk(j5lAdt5sL`P7?~i=KKqb+lm9C6cG&em=v9nN|Tbo!iWJhR&$qD07gOdf*2W`M*B$KbfbwoIj)1*O|$_0CM'
    'gy&((;0-ADQ=2P#z@oHX<W{z8o^DZi{)0L#_c1kz->;A7(QJC=roO&7*;r3`&E7P`RE-ciz`H4f&iQi6p%2Io#i=>Z4u<cA?-Z>xwgaF55^XJB'
    'S=*33aH<=I21f|t%q=uH9Bn_bXhxT)&-As2bvjw^f-=P`j}h(B9;PJRKoCoP`T8k-G+<}oH;u1>*)pIRIrjr5f8Z*$bw~=B%tUB@hlP@jphvSe'
    '*Gs|Igs7d194oz^P7jA8^Fm`tQbv$YJKnQR5<7V6`FI59F-o@Sj_QlSp<Yfbt#%N~@szmyM3|(X{xg+t&lcE7Myhb7v3Cuh+`<juesu-vZyzgr'
    'ZyE9p6+h^=+;gfw%n3Ox_b<3vWwDNk;ybfR9;joxgXI-b0N;r)J+nzMu&5v1<_hw^7kXEEqK)@2@v9Fet-Eu41OTOAeW{c>=)AJ{i0?$0S`E1B'
    'bSXtMPI)=z;gg06XvxL5Yw<cJx~-hVk{PAPk)4f%AWhIzF>+}U+@U{U5ieSBL!5rwb?Xcft0(Xua1B@Y#kxMCZZ^3a4YnlYw>}-Aon8YP7wpAs'
    'LmN4(Bb>Y=Zd4>>OL?LdM9AzP`kdJ)4MPs&{mC*V1&OQUSw;VZ#ofq7y)Y0bgctuA7Q9_E%c#%6(WY(ZB*qO#2^dppi`mIoXMH+AI}K%9vYPa1'
    'HQ3HHltH-D;OtF!lI(tQv?SRbaJb#;q~VbDP|zt`T_2CoZmtpXJvQrJ(7urH_GTNFpPdUM`>~*k?{o=e_XXP=fAH%Dg(wo{BaaBn2@p_XNf>El'
    'EJgUm!kVnTbT+bsMO>TZ{>@ZGA`8a`#39Hhr&%MgLw)ExdZ;PHIa_*PTaOXS-130^#!@b)D9H%n^y0XzX!<mpG^?iw$_vcXzCIONVuI3>?JuH3'
    'UbGwL?5u5vqMJrMQw<9{dpqfpRH&1GHi9}VQyk`{a)O&KvYwuM^(U28cY0%CoWNG6VL>F|HiAdU`Wxz0lto;_=mOJEH7xk3a^Z&6Jpz*p6Hr}N'
    'y;H*?^dg;02|E>StI>9Kn#v$$sY}tug@`P|SoH(udY%7Euu-Csd3kI4Xl%D*&Z{Gq$|qn;Kemhq7!CWkqky6kn^GIS@x}}1!x5Mh{cUMb?B+36'
    '_6A%f^~O*pp{$jjBWql0?|xLT#Y|H4ok9*CDlkmbjwa-xS<^JP>7g-94!L}K_AjhDR7Mb{Vm)-H6Y9x+6)#e+Thf0XRdui#zPoi)0qDz9;DtXR'
    'ZU3qQ$?@$)J?Dv5M5fqE3EPQZaS+?8H8geTu4prM4zk5Ugi|mx8t1aDAhk0ulxXy9@&m1~Fgw0zD*Gs715?7TkLuyx(O&GFA4uUyXCz);!HfM;'
    'y$Nys`kHk-2ZqY`@Z3;SixaAE7OOxgw~zh`)IV+QRH)$JHwoqy*uZ**7TCPpx@L=B{~s!Hh+Vl6z1p2q*4dq5%J&&Uxv+u_9~J?#-)@EEHHKvA'
    '5bB5OU;o!oHPr~po3IFGSjkOLbS<g}%wla4hvnqc9SWaK(TDW>?7X#&4$tbj$dUa@h3{{xF1k`D-L8vx91BIX@HFLn>6~Nz3rZF%)1kO1)W-j*'
    '!7bVs`yG4~^p^C@P|<_aCCOn_v&Ro0$Ud^B+3x~RC5cEjyWmo$@IS<J-ep^K9A#~d8;y7wSPG95+B2eE@c{jxS==x`qNy~~-vZYIj$4V|S3bOZ'
    '3&$7vI#x2S_VmmMiY~DS4h(?H-A9S`;gRD+Mt&i`Mu_69F9_sa;`oi4uRJg`D+URt2Z~*8F#>VNs)ikTRu-CS3DwXDraAw&BFljQbW;sa0>;41'
    '{0$?w8XQnRWw?sh8wv*-Z{^L44bi%z*_DUN-O{7uR#eWQ;Ddb7&=w^PbsD@_ikLyD)Imo|P7%&<x)6LJ)iZURU>K^&z**#HKZNi9P|KpL#xE{@'
    'k`z+;6<YJMqhV+R48WY`GG&h;dz%Jd6{5Kxl+cEM;7MJ0xlBrBmZI3jbkEpu#7Nn%NSPC>>6E0{MXaK6VJ!HKq*Rs&*YA<lE&`o>WWnT^f57=I'
    '5Tg4+$OAZtchX?AM+raT3vBdsQK=J({dZi9i)vBh4-`*`V*=1+hQOc(!Y3Yr65PH{DI@9hz>JcHP^I3Ps!8%37K8G^2*%R`7-LmwpxZ>}*5(=;'
    'i?GP9C;fko(?cNbu?VQB7{vCl)a)nAE(6+0dbbZC3mxcsC>~T`hf3^9a}4KU4Jsx5&I%;Fb!5uJK2*q|JJeQJ&^MI5pUX?@c1GJOL@;h*O!q%P'
    'IdMh(pCUWi0WTs+^8I|Py6^=>@mrRj9304MDD|Ds2Ll`@tdPY<eTl;m=CNIYGu2q&r26R>EnYTHPttn%v3lY05{X#kL%SVEb`Vqy4=QXgw+Amp'
    'Pea7;x%#5oRJnBDdLTB`9jOSZjL??kAT#DA%9PIt>6CtEk&FDRdHCWA#hY7+B=9~k6a}|JLI^7)J9z<{X5?3jK)mDwEtY~{eH}ABx57a_+Lhcs'
    'DCC226Y;_|3au;*o9OFz8Poo@q#AVDcJZ`%Pk4e6{R@VOA$<yT3k8j<Ih#syY_>Jog&n#`iD3?H14R~9OMyXp0$KlGVVY!>S9YsGLPPFT7DCC|'
    'IfQY#1~S%7p6SH%Ny@L9b@)|(!_d&M+*lAHn@ciN!)5P7l*7}2qlYNICICeum_5AzU0%IEQFfL4YyN~pw(FAsV*7>?Uin4@r|E`60Z~-tYD&Mb'
    'J-ensL3(_ZlOh=clwyuZlW#y0NZbmrnzZ+3QA`QaB*TwgD!2B`F9F(anW?zxWfgi^hZp2-K)EP&FSMYz3lVjpI7tF{1O>Tq6E(QVW7;qwZFf{q'
    'r-VQ+NWVMx2a@av)ebB^7Gh3vDz;~+k48Yw&dBt!T}?Q4A1hA&+6wVswsq_;B(i%htJ0BEwqx0T9cd0e838#XL;mu%PF_%KG5SIns6y3IhH0-S'
    'P*EvhJl_@)x@`<u%q#PoN5F|1Hz}h^5rY(*VJ^D{UfCxkW~zkTp8ZUU(ru5;H1@a~D1s_bG|&_(O1S9r5#A}q5k$(1oRABRW~#q*CpPs#^e3EM'
    '{2_<nCsT&1Bt>eLG-avDB6@L$lYyU0l_a}?b@a8|G+YN0e)116+bCsk;4ZCtp16ggH3L~)jskooLhN3U=4x;ei1W@XD$iAjoze=mmM0`VgjjJZ'
    'nLG%t@B+11pkdIbk4JFh)KR5uWrswQj35fTdV51{(bD1hz(LfaL?lx?S!-0zPYj9q6S<CP#g((-HkUY}RGKXtYic<h%pvTl6uztAfh-VuL>98o'
    'yK}pv8q~QzBuE9h={o`D9Sls8YjlV=DGx5!&EB?C6;1H+#7w6!J)L@KS7~($;sQ^Jl8^ricTB9*sncA~5QlbR961yKmv|%NIYj7T<bWi{#~b8*'
    'atpWHIf0cvAK~ddKo5-GeqNwdF}!D-x`ticT*&o}M#t9XV&h=kr_lwo*IHPRJ19LI%PRDWDs}`Ugjl*tu=%5eh&5Jyg5fpYbxAMOv?;k;m^DcQ'
    '(s^1VD6KpUKjcsVlnoK-!s;`H()&r3x8lQsA1fa{MF;i_)7}{|!nQ<}h@hsA3J*OsEwSW^Oj~@B(}j2TOo!4vf4N_U9j1hd3}?b_qRS!z(lMYm'
    'S@_>C7E*%`s+elSi&m5DXGnC`S6m)Te<vqxyB>CzWPd?f&At)A=`fJR+d^qoc>8m_?JDb?@*G$rPorXPKNMgz)hwDB+?5+Dny*@;x$l9N9c>Ww'
    'U}z&UQ7DKCk2c`$`knUxo0J6TTGd@*p}1*N66y6$bk+kx^|7NuL?aFA&R}uHgx;%lri!;5Vn_}xpCKf?YHQ;V2uMyY8vSXST{W7EPROtWgKu;l'
    'Ia`wM=)~dym6Rhc{ejghoIxx<J{9&!FL$oCF|!Dqp%M;H8?)L_79H;|1g=Dov2YR0r9PB3p5sGQC#{>kgD@RYs~ro_Si3$R!D$WHU4E0sre0)Q'
    '667*;j+?9*NDBA`=7#H#;UT*C<5cq_l)dejo!|;{45;Ajb2Ub9rI9{twhd*Cjrm8<i;t}}Kf_UA%kC*&IxDCuKq=msnR+xdr$^Y@wui~{cr}!C'
    ')#6`-R>bi-q|t*0y=|f=s1r32(5rQv`zlHE(xVrre8)^wE~uc{fCx0~$18Zou9f{?{XB2DK^4$~ruzUd;tFFXDvw@$Jc6r&vzM{EihGUT2d;c<'
    '&&hk}@|gPpN6#+F@+ILFv=eA{5Z{~CppQpzO>nR;vu8*xkita{j~d{_@!yBrd_PR2^tjn6UP!RPD$<4A;;F&yks`S650j2NJEXE6W`(%5wep}1'
    'z4G)uth1PyDFmPLYa88dPwd)@-3LXu8E#(6gWw55^C{&%_>N1BYz{?a;<ck230|YUu1B&NQAvu07|#`pEcBHKaQonpSC)}BgTq1;sudBq@@nqo'
    'dqYZ{Bo~jG6Hx+k)zt60f|%&NDF|Y4OGap5RWU3fz=A~_JjQxLEs>>?W6{5}Z068s%yYEQMo?$H+33%)gUAHhhQ*QSz*_nF%34VhGEAQ$+_3HP'
    'LQb|o_(FA^@B9jRe5Z!=8K*?6F=iD#!ZGp2e5pBI8H?$0`s6aX$^#Q_x1?_e)FxZ|d?&*6m0;ogQlP;TB}GMVjbm=Uo*$5yXkT_Y+aE|abv4yB'
    '7)Kmgd?$j`YRCy~m<#P;dK%TknY7lC5kt=62}utC#W7*Q$)}&z0t-=vW)DX<1jS{$sS4ecui{z=(l}+`^aYj3V2JjPkviUcP_JeksJ)JWFL%tS'
    'F#XXR(-u}S6@*yq!=jVtRKE|{aN`$s<<3Gjz9;Dl^qgy4{XTY7*gL>H<eG@zETYyGLI%fs(vLm20naZGo}*029I;F5#hS%ePW9+fqG_oRg`41('
    'Q6#!X8-XPCm4Zb_8<g7zR;RTSLlUrtw3so9po?hfvk_Kttbv{b***&tGvs7s5S!dQ*!|8qaIkFElgd-<h2K3*73A8XOh}-2M`${A3Px0@vh&JH'
    'Lp`4OS7`DNI}J4lZK?N4etsvm98lKD_%{wc4!X6X7pEXhGSSLOv)*v{9rt=;uV?X;uZLMIyd1g^xQV20w-}7+qY;$WS)rD?)c;P<om&?XhucnK'
    'p`Faol0N-{FQB%%&siXUDRcsmXTUZoM;W5!ArUpAf(VPfG3@I_4bn`RSdU9J`GGi)uTirL94WdEibZhH0GmD=L2ZAK3n8qoP|q$|B64a4*aEQc'
    '=l(T1u_UA-HJ~9ie2g+1CfUE-qhTpIG->HsD8X|U*x3lNzi3<>1K906p`;sCbnhNWI@pAnH0Gfpr4L6~T5Ywd-ww%3duwRz$O8^63m-~#Vfy#5'
    'Al<4qGvwc1GHF9Q+<1hMn8xv`nutE66P8Sgv(siJD3Uy-oinbUu*6=G1!d#0`P;DUA5liLbq5)X??jkBDY2JN=aqFOyM%|eSUZ=2{|Ya-5ghL-'
    '&R@hYxkr)GpsN~DY!V0sLcvH+%t13J*XRf4loKfoDa@F7E+d7Q(=n&IlUd__H6+a{OoYSAaBc7#6Wc<&dp|L$@me^I1VYM8G5bmcsfAY|txqQx'
    '<LQp@;nEDHH{exdIsS&6%;1i;`3I6DdqOp`mVz*+z7t_?$2@xKi*q5NSb*dzm^6<UmWztmO1e!c0t2=1Cxvl=SUIa*AN>_-dt^k*A_T6Yvz3ju'
    'Tz+$vEp3qy`x|Ld8KDIPEZhL(rEE78f+;>5L1|HRHE>WIM_Gg94HOTEA{=_fRrqfxyjEof{13bhN-mq;V>pgepN*h&{J?!ef65*fsbr-TIb<`|'
    'oVHcWyU`e|t$f_jRiR)6WjAruTxt5)RUeMPbnsv(frcOnZu^n+t@^O&D=gzI^#e8Ox-PAM&Vub#QaqZ`y(;=j1lawZt)$+f{gNlB%q*uM(Wdhj'
    '-+M~hd041djQW6-x)yW_s{3$?M++aQRspoFCXq6fSu;df1bYO@{?Z=%*;cm=RjUW$#B#1srYQ2h<|`4R)&R6_ibXcF?7i2_pWJ$`N@kaOI4};$'
    '>3tETzy84;gu681UPTVgKOBH5FE#O^wRnS=97D}5krD?(TP5xB#A^I&-T!G7MP-*>uLSPWha)Usag*GZMfL{eeVMe0`h()1C!*Bd(({2LHG_7@'
    '<AD;~)9PqjXCbuWD-o`iUn`yjB8$06vZDH~HDT+LkZhJu6m1tKIeRW;o$V~^*94M!8bzBmqqpY~lAV+mv7p9dU>n|)Z!uYN3h=|8SXjH@KB~oz'
    'g%x{POIUL0vk}zr=Lus`3{u-uvU(tjLy_3pr{d{QWF6VRn=d@1Vo3hT+=7orNIHF*Ebf&)r=vZQMrJq5PIxHtyZMYt2As=9_3w3u1u+-oHf`!O'
    '|4xLdg@?pqZxYVMP{L-%MR%Xx+JJ)IpKlGB5K?8LUu=5(X>vwLw;+hjrz5oO&oU<j@6jPe!4E%>rJbdCi1es<dV`{^1Kl9m;NZqXEEJ!rM5VKR'
    'VkD^At4h$x-dn4(8T9!`w`6HwU>#2%F7aI}9A3@dFdr#`41GF6(}^%J&ap4qy~D2j122#4rw`gRY=DolF$`c5z%s5UXL?cXM5A<dZI;nf^c<L7'
    'hBFPd_F(X1>?zBd%dlvrg{heySSTvW%uORhaYTaah?`;e#HS;)?r8LCTwI$v_Bov3MenRi?0Bp%G}f90P8t~=E(_l@G?b)LJXQ!c91Q12**u}_'
    'qiRLhW2{Olo@&2xH~a@0hK#FZ)pw>$5fd$#d^Up8T;$rHHK$%3B|e@gDOw6KQ!W<sfi=|+UdQKPwIgrZ71Cx1;oj04u<rzzo(02I6RKWgwV`3J'
    'PDWwUU4r}Z6BF@vNU;Jd+#|3J?-Ygo!@q&uR$UYEP4-A+c%^cR!=b=Bs+O&vE=~tC9C{6O>QuEtq0Y5za@y^^1udMnRhPv}B9jyM@G#f04wqmx'
    'q|()&P#vw-w`l;+vZ{=ZAP(MVRXs4j$6&W**QE8b(9L3ycD=O~PV_2Bd77^5$&I0%YoljMZj@)~R#DXMZPBGEsiql$X<pjK4(9^idq?m)7rK)j'
    'Od^fvJQWyPSlmY7{@c*tirZ2^XKTYC*QX;i4uWOB%;tk{lTu(K{ubK6#kuAa4&7*T-@W`5Lm{N5Hf9!f=kZTRXiI1o)TzPH&}@Ssu;mUcH2P!n'
    'VTm8KioYS+#Q)T$S_a+ql&k%t5!7}lG*d2H+!i+3@hgfcK!|tF6IR|hFzAwGi7M?E7aLY5-N5cWTQoZqaoHopuvgXxHaqBirw?3M_N!kdpYohM'
    '7iibsau4GX+6u$KVq#536K>b1Bed${qIFyz^KQevFTV~9Iw+xJJi#=YMJW9b`d$bRb&hjNC44r5(&o=jB%M808hi&-W`V+phdj@CLJhK^N%bt-'
    'sOT)T6CP~@ma9G;fwA{nw6m2uLt`7QDKtl$qc!5*w!hF&^iXmBA9U}iwIovcgSr!+jj&i^OH4${WK?R&uNCKWLwbhAmJXBPU|F!par@kmk<XA_'
    'Y+4AENhv4VwiIz8>!z;IX2CLQOqd2PA0G`LwH*Bz*6V*Hbvp4Cpa@biG_yMr>i!7YPJKE+Q!;8HkT_gRTz!UY$k|7Ds~A4elHEw{=VT8_+7uzX'
    'BX*&dCVf1D%Ys{~o$y(^Ucq7(txPU-Ly9u#3AbTCVnz7QW}r|>jl3fbGPkSHL_3#)u7yF{9AZs$X`yA$EM$D2Me+H<Z1@{D38C7dQ-(yp2NcQS'
    'ACAC$)!F=IQ^t;zG8@bY-zJv`(xpA$9%&8&YQ4Nl29s+CZdv#n>C+LK79K4sT6mz*`P9j5gSHh%?bG_CE0qmxh=8wt;`?!CMfd3RaCsG}aE0$s'
    'SAs=94tHtWoAHc9_|QQYL0E0RXWpPlq+N^Lz726MadHv&NBv$Oj=+`&U$A~8iq&C$*#4`eItBu=rTPYAySymp=txzPpz}A#YV^O*JQkvB7Ogw{'
    'mdJbJxw$@QWx8kRjfSL{ZK{6AwIb;(l(0%h<Bd;8XdVpNO-gAV&Ysm(%gmaXHn23lpJGum!1rhS4<c<y^R!5Jefn3Zqjb!`u=k*9+wEMbax1Go'
    'EtP&rUlXO!AOnR#ajd$P>{|^zXc%<mS5g1|w)Zl$#B<z)%rH^=^bX%lr!D(t{eF(amRu223Af>-H;9DEV9oLA2(5dd$61Fa+l%fp{2sWCH7bFu'
    '0<>NqXf!(#!e$K)W1EB@CY}-9KOLbd?6TlIt8KP0w#ppY2MuCiIj7xwY&a<sW9zh&gek-u3z)eIM~B_fah{M9AnFt1ZB4gMh=&((A$P0-yj^X4'
    'H>qg-s8Y+omhzShVk!CX@q6VxAAw{1OPXAa8{^8{ZMQn54a=s!7VrnE(OH&}G2mPsRMG2#NQl@CAC9ne4CwA_N979#nY7*OyWwhr&dpH&>l<oF'
    '^~-liJZW)M<kX?!#AhQUzXS{^igP!?Omj+@R!}t1lvTbSV3%bvouKax(jG;X9yo-$!A{MWB1}I6?3FP?^d#*NNL8r>KnGQHDUp6anR)bJmB%61'
    '^x=+YD#GOYPK4R~+(8n>&mA&k9C+KG>4dwQwNKhcI8Rh0MyGOzlI6$s%O)vm*!pk;c20nXcAy<D9fv3S%}4j~k)M$p!UxO#Q<Tb+jAaL-?unAw'
    'Kmm-Xs04Z^x4D;H9H)e6$q5w%pO%CJTXC)S;!>e+%tn`Qr3`i7KTvYq%Wjdj!}93}P4{pbM98nvJWy10aW*jBfDrw&`uE;wBdLMFBAa*I*XZv`'
    'w4R|VOdC-Cy@T&|_d?GXIVHkc)LEG6RfrJEsyGXe@h$$1*AHOOiJqGg2O(d;rz0$#Jg{y>-S<30DTk}BMs%?teFbvE22C6zX7XoQZ0tO2XJ`Y-'
    '7ttW2H|Vj7Mft3B%BFZsa<wN<ElebEYyZNu&oK7pS1pRZjaFogu&X{Cf%%bO^Bc0$;7G`<U4HCKHSg(W22Zd7J(kM&Cvb1PbQJC+IVmCN@L(wC'
    'V6X_!_LtUmG3g6?y@N??J0#!q#v~{~nL`64H`kEp?OiId`gDZmYtV*B%AzQEuphdwy#&^=*7J_-4;U`W`|TO%fi!VHjpU@*B_=$>hw5u^uw5&A'
    'qH7*@Ueb4wz=)J$HAhwUQSOkoP^CBE#gPsmZ_xDv16f(zmHdck$V6UjxRi-Tb7emb5bC%$Z{J|hD<OI3Pkk4oceo>^aqC#n-D=RqeH(w#LJTVi'
    'FKdz<g(9RsRcfI{$<faMSJlx>4XvV7eQi=jEKU|YpN_!%n6M2A&t4L5YIu^n%Xx*8d)r@VQ&N%IvPtl8xLfSjM{{ks9fA4zVAmQtUy2fMYH2yA'
    'I}T^~EquK@bY(BuHCKFUNj(tQcHT3ejj+1sIq2Q9YyGQdx^u9V^Jy|=cp-%Zrn%qo1`GAskX6OrME`H#gMoHhAd~>4=ZlK^xKV5(A>8cBu<P`x'
    'iFRbG=?jyL4?-yqOqaB^s~MtwloKPpz1skcu3S4&>^YLpArDJ&SOaa+J5ObPgU*JfcYB`mQW!ycb>lN7F`yZMo+Q$2to5o?3my1!gaK1T?@^zS'
    'r~ul=x1Zyra=!68usfebrT6cJ!rKmvT6dF{3@4K<{wYIkAiI*%wS7O^CSJGiYu!n3v{gZ&?)r3urX^_Csj5mx(M_eeHG<F(ue6)H4*J=A-J{TU'
    'sUrLyA(?)Dp+)kuARWecB1}&yZ748uXS9`7hB}AW!DKI9-<Sinp7zWYOMnfT#!du0lWGtlsOOeak{vwGrYN{k@!DGc^xz>hoS&c>4;T3*QR0};'
    'hib=XjaQgwv^+egRA_aRh%FBl{g`VN$Eu`Li_$Ki0VW&()TD+cU9%}>2Vp`;o6fm1!&j)qh}aR-n#|Tc@$a~%AA|$@2Kkvi(df{!AjHw2APpqT'
    '`b5uy4@Y2Hgw(2fS7UqUriyu%Z`nhkP*+kFp-BhZq<?x=LU6+QQoBDJ6CVwrJduRiJfSSPvYK^w&-b{b6X{R!hJxBhI~DVT<L9a>sAYM0$7!o$'
    '4rc2`Ru7dLR~1)6LNg6fO2ilTe1h3gU{8FhfJkc?JTJ=DYWdNpBe3RRc39eX2wnEzSehOWP6^{VPwWHcZU?NyD$Po`3b>9+VlE(x-KJnzL2+0%'
    'Jw_zUlLKi~z5W+KJ$zqUVJ)kZjIf0MlS)@`SHwv;0V7ApdG757<K?OV%01;$5J%@?_76v3J`Zi`k(^><w(X!Q$6Xu@&@Il?2Mo!C$-aVCF_o=}'
    '<wT>ggwF<8)swcE?a?#EWpCir;SJV>JR4kBr6Ar|jdtU$e~6whWPn85fPU8uG7!fCjNHCmzApMM>|`CbRo(mXQa|SK-T%g-T&yxRZ;XYBm|vrt'
    '4l>#JZSx;*|E~pAH6yXg&6$v3IG>OxsjHTwH&zK$HLxPu^I6PT#U-4oYNDBvPe*8)i1eO7%Gdl*Gngu`VoT&ZZS)4i+6BGU_1yJk9<fnwu~|kF'
    '?629Q@iqgqpnc@w>gB97lF`M3qb*IpC26cTC}bsQX-W{=yC_oZwJF)NYu5axjWymrV5_kg6lo!%lgq7mEk_fJBc(4inH@~EUE<?LZ(LE|53Eo='
    '9f4`zM+$8eG1$-mwq+otAke0cZn2R+4|FEkQ;->>(+Z&?+rTH1iu!N_ru9aSDj|T;^dL2|kq;O><-Xo(J39#~W_E5rZg?BHR=m$CUQKR?{kL@|'
    'Opi^l#=$|ck@;o`C+I@LTnhX~d18`dM77bt?4X&pND)J;D5MsBI0DoD5A}xM+$meom2Y7*N*Wc9Dri%seStyfL-<l)ly^6(_mEWl!x5PFfBM0u'
    'B(GkbhD@qF8~_mF#q#!j`G8qCj@<;^92{xPuZo;Jwp0G$2u$mapsu@L8wvt@C$+HxhVxcys*!xaaN^wd^WJfD!^K=JjhJ>#iM0GyDH(4oFp{>S'
    'KCwM__9v9|fz0TG+V57dCBc4!r1*LjGM;uyImO*BHMC&*XCo+Prv2**R~>%?>`hs;rTUAbOOe~`6RUJF_$l*B5%z*0Zx^6M4x0}LV9FZpBwI~c'
    'Fi3F<OAm@#HZbn=T_)onutq2Mlo<mmR_e<^-~e@PEu`qpDQ~nT*v&Xz^h-+%mvw#7UG&1t<%xwjD8xmBvXhwef?5uwtTy!F2&y;~mU0(?RVQE?'
    '>7M@b(d%wA@dm@E=R$b!GfD6i;_yc*IpwBWM2Z%;Vy<mrGTnP5U^-Oo=7Mp6Qn=#)1T;16U=U|BjEK)g08dD{w{|EoGmD=zB1~rHMx@*=ahNWs'
    '`d&!h=&6cww4a!j4tV4kZHsO)I(0Exe42}YIzpQop<O^{^DS)HX^xDEgwT*?Tx@IU3vI~G`7Aw(M2IqSPIBZyVsH9Rgvq(EspG)hg#Kxf3zoMO'
    '(wd}!3{!neA53%xM3MUkHX3YqyHD6EO;nxva0JHc%V@)E`zkNH9A&r$T{2sjoL9P+L~k$@m)K+1=M{Ea>9*B&j)aASOw*SFPz7Wgdj(lxF`AC5'
    '8W|0NLVoXbqS{6@J$nlyPN6I{W>}FG;tuTu(E9lBuV8nQY9lUE&dR`PcpU9ZsvN^x=5MINjS7n-7mFZ4*HFUXE`m$opfELZ2@$m{vYd)9EuqP5'
    'vp>lZO{;!)pP={HlD2kxT^~T3)l#P9-P!M%`c4Gcv?lCr*Q9RiF6Xjoe^M_|aeM!MnoV^@713?a*i6{kF=Us--|peyx(0``>5%F$vY<<iFXfDm'
    '(C7I$7d}gL`Y^0&P=kXRDztw#Lefgl+WR=<aEIp?VmBlt?t`A#XzCJ{A=1VOc0D#UTFSimv0Srj=Mo|6D8d{_Hd34pgjeb&7h;!{(F4(zHP5lJ'
    'i8YuAl?c3~6}ldcw+WaU!M8R(UhWhdXlUa^tiSsLzkw>kQ-xBPL6P}Ruw>}CChOM2$*6P`W(<-bw!;QbLTU<0Ngf31i~Ok`P!`!xx?xnvq+J-X'
    'Q1{vdxqLdpDvo7AH=BWm)rSgYTUf)s?fLFE7OuH+V^DOCoT=?b11DB}HbT-8Lm(fUOLB?!%CLuq2@Vnlo$}I{?pd8(eX6XxbGGP};a%C?R8X9J'
    '`%Z-D=MIx7dG?YUHP&`bbAxJ?%L2a<*{H=b0%|%pnof4o21B2YuypDmV1$Y>=OLGjO81SnEX1SB_`<R`%AIHI$IRzC-VhKQU8EMi6Cvu%$xcie'
    'u&6rKG(_{Ys-YjcRLTd@o`gLJ8g%aG=@m-sW;<!<qVGhA+xL-<-X~NX{8{aufC{3zZ1A~iUoQ|bY%&t;BTW<n^6PkjFOps@2-lD47pyQ$g3**h'
    'h!RSaA2;%g>zbD3Y>&82+6vo>!_ZQ|cC>TIE*YrwXkvOuL<XthWzJS#)Iu@)zOk14x87LP2<^rodVEtZE{`PhqLTg75teWJcFf~p%P~5MqdC&_'
    '(%f~qePM}MftLta4FhP4qcBpVdQyLt%o=a2FD*FCBFLfGl@q(y>QHtIchiSA)NtLuHXVxa1Ra+$Ng8#1Ji<Cljg6EX59-J=qU+tU<lyeL>(4_('
    'vR`#dJw!68sE}4stod{VrnQDHrcM^yvuu_i%rbm(EZWqmOmm{uzI<%hDod^MJ@+vWfZRJ8@&;R9`4zHyL}F`oQDP@;-BCA~L*mCk;%&7ac$gt1'
    'd=dS$W=CCZEs8*xXz1e+md{6<T$<1}N2EbWsOQ7uRjAzf86D-~OX>S2`$qhjY$Hc)qv^vDmfwmCj99hrV1lM%&DIu4uigt2<|i1QVr*P`Z!n2j'
    'T(tF|^uvcEFuxUZS6{kyc6bVi+xXpM(eYna`F?L?AGR|w(Y0&x;9AY^!+bhI(|wq-TJ{~^rhj9Wl&V+8w}<XIqosod`*>?y$kp46E~`Oz;1nFe'
    '{iePXHH`~=hb^*bksor2KS)(+nh(-KVG1HxBaeJhtx6s7%%RT)NM0K2NDDW$s&=*4(xi<d<du->DQ@@p##*|Wa~Zek*rnDpXy>jpR_ECxEEUCu'
    '`*UIGfC3&yq!79ol{EZiE%L$I_Tw)xlN+~ftc?Z@0+j#xY=q@$C$(5yRKEIZVqT!S4VEk=U1IEmMOTHq{5}!5q_l@JLYqz^eJKL<@`Fu_JY_l8'
    '`5!h|B^JD6(A%w;jmptfm|yfNYU8hH%i5VlwPn++#it`M%|?<e-Cjoa8zMUSgjuLx(Rio4!W#?w9qsi%CBnzVGq-zCn)+}A<@9R&f|ZuDNir!u'
    'U2IiNbfV&gus2je`ILmRhJ_Uo;xhQ7+3V~2cm%e*Vl)xR*l-1L^~HLFn^QyS78kOWzeWZbkpi^f<bgdG*3FyrWa%r~K@Mw4Dss}RvK`K%+=Mu+'
    '(2%>h(d0``N)*~2m$R-kp2!w3VP?V__AfYZFDrS8SIxq<tq*Be_^BT_+0ET>@Nn%ekE|d0Ui82$@}W<uFOS7*v>7{ph}Z)TECeaIVBGNmBf9ot'
    '*c*!WXWigG$=*eN$bRvrnTvMnx}y#9NDH9AGQHo0x&%HBn*O@D6lbu$pJnAT*ET&e@ANW8Kifhd8y)KJ`c4EnT>A({yUHtlktr)j1^A*}xt1?P'
    'E`YdUtI(nbIA-gZ8}C?sI>M5+nFUX)s~?ED0;!6AX<^Y9amUBbHcL2rqwMnGjW4m4Cl2AW+Zmwm1c)v<iSaUV1qU}mbx5rH16>l|$>|A+03!=q'
    'g%2!`8DC8{$#8kq$0M|Sq~gf9S0!QdVyqI}AUV*)4qEFAZ*}fMT6*@|x>CjT>vVK`<gorLUOSC#Hno@nP@CZNBMDsf>MD9L|I9SY6K-@~H2p6B'
    '5E^E>6YL2=)kT;l2luJtA~t2Mpx!Yu37eE4!IR*an7cW}^oFwoo~vp9OwVSMkril%g&pS7(JjF-bZo@zf?~J{`o4kMzd~hrYK+b0QP6^AijG+i'
    'XPq>-d-Td+glsU4c8zCZ+un-sh7S^lZ@=W&ZJ#T(ath2LF--H6$LjwUZ>8`Ry3j^aGJ-3rtu18PvL=j-tp3?m!bEQ{dvrACDvV845~AEPZH^c9'
    'IJX^vd5?3Ue_z^@NOnWP?b^<|r1xxjr6(HtceEiX?Mqq~ma@HND5JQk&2YZxWPo>pkC)b-U3H{Gp#zo@s!h*czThBpTlBeB|CC&~^)q)MxHM?K'
    'zp6Ekw~^R{d_zWLb4O*Oagu6?sOtSguN|0oZygQd?n7)NoQ)0#H0LLIDHePh5!&f+u+3<CTF5R2c8bBZayYCAC7bnx%QBJn2g>qbXQgz73woa6'
    '-1Lq&1SH$ifS@xb)CRlO!jJ?J>4PU4MNf>cA2lpy=7r~6l0F{NS0hKDI+8Yb!>;fcCZ1|=O+y?__E1~x!5`4Ad_*pv@&r2*JK(nG1gi_vv~Z-u'
    'iRA;q!a<iQCu+ftQTz57%k{?k6O^-rczb=KPdo!*p-o!l8wOClhf`;sk-Ef^mcE5DXqsxiO1cr5STIXEq#hVw8xidH(^c~|&%2TJL|$SBH==g<'
    '*R;13@g!Iy?BVyGDjDW~iZ?=kK&xcmp9$I`i4xd@jRmJQ5<Ok-hqDuIqMkzxWoLrjhp#RdgKIjYz5AWz14=cr_xyOJ^h9lvkDDZFj_)q*Zipk#'
    'Qar0UkUbmX$QbC#N_<pFRlGs7)UZ4G>6Vqbf?2d+HE)F)(aJo;Z<`9mtzSv7H_7ihnwF$j>X9G!z$SBL?`=GmifE3STT{}7D_SVdjh^;hs@X-M'
    'A`0CSlK`@dHhe<UHzWy@-X0woHcnK%$KdMA{T<rt2*>HIYB<?W&+-f$kzq9GU2d<!*8Lo4LZFt@)Q^zO4Uc96X$8n|Hie_yhfvRT*Pm;bSj6-_'
    'Xv;7wYn%Px(Tcz`&yPAf%`G$<b4}wAr^h=0J!%1CVvZ+5Nh8|~*Hf76q|RO$;!%ix3fah}QCXkoiWqmwoUx<@aSr}H?C7`D5PRnE;uGrXwil%B'
    'qeLL@>V1FyuRr0j!X(>SAMk_-)V-GS`P@62Wa*-x(BBdvQ^gvh3F+t4oY575AxGa&vRXLbKLQd{l}|GG2wYZrVQjy_83)0otM3Tj1t@6JtrDH+'
    '5(iut!qdRR9z_^ojR*b%o~d21I<*+@y-T(16m#7h@SU-#&7X*V#NG2#wrG0P<H_pM?V}&TtAXz}q92hZyu(q#GmwYPf}>ba3D2~A^W_Ns^d2ny'
    'V?13o@p_`%4LS|-O;wAZ^FWs7zJ<vhiff_jTkKsrGkaoJL5dJ>@gDpF*DykK3W3O>d@*Vgx=?g76+Lq={EWVQ!zW!Iw{0*z-P^XULxOg@tfA8h'
    '1s@yP5<La)W`m4}^ekm!i#p0g><%dmI^6SXsP_5eB|M+PkXHj2X)VR7thODmSTP`9)5vy2zo7ar$Wol7v1Y;APi`&Q)iw^5{U@eHS5O6$-*xO?'
    'T{Jsb5W1>b7~{w8+5#KXN!(({_ogj9P`&v|L{8S{N;7=)6uEixcs~fg%QznIb5-qMMQt!M`VHOBlZBdol@p>oOl<)c{RTNah<c?7kF&)I_|WX*'
    '=`IDBkZT88a4_7cm~o?yKu_m0%G~1o+D4^@Hc50N(`Cc1nu_0;C?TQ6zVl<2L3id1fK6$qn3VaVS>}<@KrO=QtF}8N9_ChkTUFyp*7)<hQ~&-E'
    's$Oh3@a=PTAS9fWw`f(8@G7BG^dqtlA8k`9a$<9YeICjOh|cBL{PDC0e6BW8ZPg}^-Vcm16xVFmd0BuT4%vmmG90^0bd(NF$o_kEwCYe-_kohf'
    '?Wb83Yc}TOJ1atpRYKLo@_66LpY>|d6!%mUjzv6Ui6T_xEih*R9GR&^O8ox@PUWtK;4W!7EK)%)K*YE~QDFG`BE~Mr;Qj*6Hy}p$H#j1HoTwIF'
    'WXbsa1wKyJq*l^oKF24X*z{trH#kh$e}h}7OZrz`&YKvjS}0Meh{*2Pw&p)z{cYO<p$yTT$40XPp{RV>4UNCqexTyGN71R3&-3Pvxw&a>gxs*c'
    'u^a4&kX@v4(D>#(Uo0)U;^;@l^Ogt^*T?(=y5XX{%W_Ey>CzR{2n%+Da$iBVe@D1SuhI6rscNcin6N!bh6<M}qwSbi9Y1mBY;3K?^C?)lkt#$f'
    'U)~%D<&PG@`pia70X+p)71cU-96)FN^$%JQo&q5Ju4nt;6ofCZ%lkRA8KKXP-jQ4*$<+nbv~l7E4(Lhb&0U#A*dRm#ZPzj!Kx%n}3-!mZK!(p='
    '43g30It#@O+e)zXq4EL&=c+aO0j_5XTNrC4IIfcp+XUfsprwzQQEERhQ5mw=obPy6NmkKp%i1U=qoPV0Aw|#J(=G)^!G7dUC;&H|)*#mx^;WO#'
    'y^;5#RT*X5Ht+uIiD^I~Dk7hZ?YrE8qq%*Qz3o~i0csjn@OA0Vjvh4cV~2GM;l4wY&u<vnT^x76k1iyY=%CK1%WUl^h$dVkdhbW_8_gETF;X$0'
    'LHX+PmZ7oQej%IY<Hd^#I8edRlc2OlKe<Q+6)#VCVYEfkElZoZh5jpAYPSqz74w}433ntI{)DQ!wy<Tq%M$<k0nmlCmT718B&J$u?f<VtZ&XR-'
    'xf1;V7l+(xyW{Q&cZFBkWKQIfsy*lQ9WH(S06LsfSrWc7eEO1k6?P6xsX+6dB6z=iXLLj4JJ9KHrL7Xj&T+v4Ls+{$ra}IRk7I<L+dFyYU#{`w'
    '56I2pOhvQIn$a)t-0H@-&ifD)0TdZv@C!U`bUovj?_a=XAcM%g+PjjIX&=WVl8i@jzlV6I&qvR=Hjb|fZP@M_bvaE91|QWuRKvfKC-xeU{BnL*'
    'S&ACD@E_f@J=ypX1q&D;;{-EQfC;*(MPM+pj%5zr5}JWdMtaP9wuMQNxPR(96`7@((FYK#0u8IHM0-y;B?40Cl9r)u7-lP6$wtocr6BlFtbUn$'
    '=(Uk8&YnP>5?$z<h+<<qO@%N;m$Y#g2^TW0>J*1LBS9p8^#sja0&j<lUw(ki8g}WXS&31@qzS?%TvkSmPQz=3kKVmmP&Q@((T9v)L3Z6biV1et'
    'FC@LbL96maV~<ZDhp#m)h4k)G^F&*LFGk2-JZxxEONyqGo8(5EwCp%H?4Mlw$6Hr_V&f*EUM3D+3t(*4BJ+w(deN66)a}j@oD~<{<1qShYsgj4'
    'yXj^`>a96%@C$mWw`g`r_ECi=;vJJ#Dqge{xdY(ayT$e>lv+`(%<>JBb`ovfw%aV)EmwaN*MB^8g;W-^k=L{5O_~bYw_TwoE%<rM=;qfSa4hZY'
    'O=Dmv8@sNPZ@SL}HC4<FV3gQqmCA>P@%7qkQTZ0K&Ofjk?ZL4EEZ8YjfUpO;g4F!H!&v(=!Xv|VSZENY=a!#<UY48$+0fVT522H@v7=5|8ES1z'
    'Iu?P&SNaLKd!`cogzD)))t;sOY1=xUxDq<GDUESRm8UkwgpX67gn<5`>xt5yW=C80BbU<Fl+70kkd&hJC!!zW`LR7|Ax4ppCrVvS3i<55w*4Yg'
    'EyC9N7y6(^Z|B>Vdj&mAG|}3lpbe2OkfLi=I1}c{Z<8IsFT$SKB19Oz@}>l#u%pCu?%#vQjSjE8IlO~$Cu($vhuP&|keetuTKC64&$j68_Hra-'
    'D`NZNDGV|c(lFS^@a6|Lmk(}>vO?x;QRxH6tDaD=AUba$v?rixyHm-TlfKgqq&`6o4)Ut@Kli`KT<E&%G?O0_KkQkt>p&o((>Cyu;@FO!`l&8L'
    'D417Ql!I$Fq$nRae?VrP0$<dGj|{M#a;Y_ms4WODg{y2;g`Xn$6>x{<U1Ow_h>1Ecx+u!7n(VZT;(oQh5$$ErOt572Wy4e(1C{`d7CspGcHl&7'
    'JRj8@dnZ%#nY4t>E;W{MdqpDvN1Xi)N--WTAX1_C1BOgd#V;so<*S^`EquV^<U3JSE4r`HjgLl<-Yy0KgG$v;<Sw!#UDJ1SJt<0yW-!!z74<BK'
    'pjt~q8Gh#!HC#NKr2hb={g;>Ay--c&*XJ>5;VJciwM3h@T@jAYhP2)0m3si`*)Ec*m`J+4q8)~kloBMmTXyo+`2&)4v+ZeW6u05h9EM1l98@iS'
    'xay1$y`{Q|=!GCrM^PF(B>JJ~*+d~!8(8h*$4H$s(of!FOY<Nz^;P|qPT6V^uowP_Eb$D52Ef+fsvbNe`{ipOJop7^2aKJ}W3&fvga)~>$mdS!'
    '5vcqa!QMGz3v*=ENZH5bIksTYM8@m=2P^`#+hP&t;!G+HmBs+%R9|Wah*r(f#UyEg3ZRmQ7zz58tm#wx+MeivGO37;D}N!w+od-};B`T-KoAwr'
    'Hu!2$=r?acCLRbK&*W<@vcTmp`zTQZp>95YV6xp_YOsMrLIcLcBi_abVR-819${+pxBDuQ4^(ln-AG(9cj2v}cG>)r@xessgdkf-{^s|tjjon~'
    'Lx|Q43m@%1N;_F1dE|5|PP=wp)1xi)9=NgHHzcXSF5MP`%%o;{w`6DS^|RHF=*iZjn_SeXA}*J0sRgMkjG9ovskrE_pD(jfWyBw<VNrnyfyD(G'
    '%T_4nHqoL%9?^p|(L;u(j4K&47I7XBp?!PNhius<t>lngnBapjeZ(3XxF=a;0?{00iQgB=x~0Q$jDxWR`ykPXpPH7YbW3g3`eS_}l4++m?!(HH'
    '%Jb&+l6;a57_u@YLUw5=vdH!F`l@To(5*j}?eF!Tw@+l;*w;EsMOikMtO%xDdZKB&Zs7$B4&k0cWU=5SaRWM#^CdOA`~wjiA>`_&*UD)*Z;(Vv'
    'SyW~GI}xUMEFF2v1($;!4E(^|GY|m@|It4&@z{b#-5&%Gg^~-mbz7xro-ang(}6e5%aY4osY{3rViE$8_QR14^Q=VX)!mxaPzh<b7Vnsz7`q9R'
    '%%j`#d?`S6E<98{IO%k+uC6vXcBUp4tDVhvp80_)IkS|NSZ@;XCJoXwoAUjg2sC?V>c)Ak(*-C=fuenatT<A?iT1l6prpEBl;2)G-g?F1h)E`{'
    'Z2A6X1no_+@Vjt&0ebj}KC~^-?^VbH{D4Lg-!6@5-=YxeIe_>%k%{hDvESc}pv9E8EBBJQZC95FO_q}0oa+4QU5VEpqpoOX5-`=9B!!YP@@!L1'
    'q*T=Xn-R2@?i!Q^(c$8OvkAc=5j1T(v`IR3zCx!vZ<+HO6*1<PMGt6_B_zVwPwp>8sG8ARc0LwLXISlt1Ds`LsFS(HF#8WwI_qIbHm4zITC;JF'
    'rtlkor2Cr@wA%W}&Zu4sbl-H@$r=V?3!zBy+}0O#_59y*4!?LoIQ4MZU{CiiMW{#70m(dP+H5i6l;Hst)sNhNdCY{xl?=mY^U!58VM=6!mW73X'
    'HNZZtev>tiZt{x;Meb55>0!I`c01e02RqZbg~8Zq9`?pR!|oe8iUqoGSNxk1boRFIG~~lWi+DvQ@=89tmaI^a&H);#U2>*JQ{ey)<l4@}#<|gU'
    'yh+Q<Qq1e$jG&u$9U}RsiUndENFMkMTe^GMH2(qIyDjEPlC<B8S&qACmSXunVQT7&5w5xd5h`lvCKm%PF!C-bGGQQ1C@{{`ej?Y5-6quib|Y6@'
    'v~a((IJbzdsc%Nm-Xw@N*pfpJ0&qxVDS@|CF~Zt&{e0TUm{};qPN8l(NiA|67^QpvpS~5r&ZDy-K7;*nd~&FQS6E~?$=MU5*4lr*<nGJ|W#!L^'
    'l{o>|Mu7nJ`DO&|J-Q2D6;i{)mKIJBoof)L7ccc7dXP@x(&IWztI;2AI-+crv5DH-?Z_5OoM43++U21gOMs%#(0HYUc@0@4OL<pTS?#|>^)LO3'
    'dMpQ*#SE0KX$_;NAKAzx$6`r)Qw^x~2P)1EwxP;~0kMJq-`ANXJC5r>bVIit96@p|9dG~JXq*IP%LCtEMO2}Zlq?csrYR(`gsa5`8%8HoU5XPn'
    'dMLfS&aEnq4E?021o1WOriLdf{WSwQQWGDId&&%lGI9koYPpO~sBs?#OA?`)AxO^%6}=Rcod4836PdW%UdyCN#V3CiAbg_+CrPJAj9c!qJVCcj'
    'y_3a}1+*XX{-UV@0nKc09alTSpsOJ!ci{K&_&vsm2?7V&Aey?OY!^l+)VL2xTU5?tqn{VaTGP8N8mWo$eVQM@1y=mZ(AJAQ2J)9o9<ad(nqRgj'
    '{24zky6H5(;|JD4VT`MsBDW-ZD|M5rE%CMhbUP{N-4@W?Z(3?0=?a)H(-Z!R{bw*i@ZSk}Bh58{KWN!eD^!Wo>kXgGv1FK<)Cr7oBk^`g_Od!a'
    'hnu^K2a)!mGDx<IG;kX_L#n8#NJ7@ohP*>(2r3vU={EueI8>Y1XW(Ia*`5}l4|^cPM2!}Q1d5Zc^sA`%A=n)1kBbwfoBq7Vw<twhctX@X3E4~<'
    'N%LiPf|m0qUU9rCyb^ceJSLL}Afr+eBp@5tc6a^_d$aR06pm9uNPwCk2^LXgZ?hA2kMUQk+>ojhULbxOnkq9`T8Z-?6f?fDQB<LcgFevOrO2r~'
    'DIvyXb%Kt`WxpKBVbE0K8^JN7HN6G>#h(ao)@uFxk#iQ3^t$m%&=TEvarB~+Gr6o!ONfw8BV|;}<b^`43{?|LU@TQ4fy2J`?w5u-JXHRLp?K<5'
    'KxYvaCs2Lmuo=nIt~i2J=pUjkLn02xJ=$ratpouf6JzDSA`@f$BBHvE$O2<+Nhzjna{!JR1voGf@Tf2<A@YvTp$Bg0dsU75uZVHpfKwyA1<BC!'
    'fRT)e%2p-$VY6P=rzOM$G<`pzgXG6jk;r4$-bW?}G%0bf`L*|?eA7zzB@`w1ApPO#lOwsTPfO6PX8z0=U}Xi$n32lY3Lu>p*BCzQ_X;K~Ybs(t'
    '=V9O$p(8e_a*<!wrxmpL0d%I7iEx=TC`!X>1x-pg6CC?m0MxO#!P+~C1vnT``r%!$d6}J%d$7Z1(sY>?V^p1o%Ge+qytxcM=CqNe%dlMur08m}'
    'W<XI44~74eZc3ETusQ+9{4}Ltf+lV@qm!R8R3X!_s1cko+9xtP*M1#W4Xl`CF(~iS<_r&i7@dHF%@tl7KV(GSr7$k0o?z;uu-Q~P59X^kWHhX)'
    'SOBak9_nIb0on03Iv_`~6Y&ksML$3Kl&lu*`$FcY{tv~)r@rc%Kh`jiLeC|bAxd;%y7#(@UzQw@Mwj>c?K)mUjpAbRhse|dg+16PYPgR6IHVm)'
    'p})MfbKMtVMM*uBqMoxCQ^^%eG*c<pfb-4&a#~5SE~g4<;c8mRg6*)6SV@Dk3!On)^KW%_mDBNR_VXZ>%ecvHDaa*Pi48C2fh!5)u=u@{$Xh0n'
    '5U(nNi7L*6DsO`W=8ti^l7pt>Z3QR@Lx>Q=hLj)D^vh#pQkaZRgxv`{$c)<(aRfU?WD+M6VQSoSI3yHg&%1!-=OUn0Z>f$LsDcOyWhN?6%4y5~'
    'xc|8KvXSheZ`RVxDC^<nHz_a7NK>Pc{)OGEs8)keGgB1xqYIrHR3o2eC+r!wP?FM^aRxOeP{QL_O5yz>uuZ*Z{=hc(NAsQB2{K>lFlAn;N|W3A'
    'w1mxMGO3;rxOM!r+Y*Y2rHdlP*cai_pZ6~?#fkO0g)(CY<PCX_Qa!q?4@-zNCrn+;D8s;sX6dLgx+mqI7DebRa>$P*sJd4A3`$eljPb;V2ppeQ'
    'C+N5UP4Sd(6uFy%9%a(IFO5qeay0n|vWVgONcYWC&Y7@+^}(8+)~6K&Njb#iNzC<@Thgge^!zE&kr@+B`8#}<v%6Fa@(WC|50e@^jcMEHgc_G$'
    '*DNW%791n=Gs0ERAu6pr`VGGJOxjq&J=_Cu)fUJT{FhjtMkmngYo`&@KNx!0eED!)5Jw|sV9~S<8NXT~H2^`1Du<KDfLMljA353dG&upTAc{s)'
    '=z$=jQB+Zbf9#SFmZ76hNZiVz8DIn!aE%I;V)yIEwm2Y$@?>koVGn5E#gWIY+=K`_5WO8Sq<Mqva2EK(Z!fN@qR88C7LqZ`H2)~m6Lt>lu34!L'
    'wnu!v4me(P2S8xY#y+u|l7LFa&=dTCWV~n5TWyOIWL#sNlBc^b14!c55Dy@;<ZFf!tbRcHvn0+5ua)+DQYMVEa>Q<v6Q(}dtZ@T1KpPVibo`JA'
    'Sr0uYs4OEgg>NUkMTk}@y_fh*@gt4%0aBjJ<b)aX;&oATq_fV@+c8xZfuw#4X~_D3<OR_C9g}&9bc;J`=wZ#L$q9079Tuwnel=3&HT6mglKKk@'
    '01=zqA$cst#K*+JM=b^!iG;JdO-_(opO~uH13qWprAiqRIP~Aq0TM1SXu=hAd#H^TWA)SD)L!ua5ToI7F?dUxWCxL;%pc1L#6mme{Ct)E*No&0'
    'ZL$TYBBaD2eH$h}A@e1m%~hSncxf;tFd9_!mC)Zs%4farQjH7H9*Ts#Go6_HxzZ9>pB5*`-6~QD?#WvCyJRdND2x_~|FYUA9+lt;<y)g%`zR<N'
    '4wCL&|N4&gzyAPyhI5q'
)

def apply_excel_gold_history(records,today):
    rows=json.loads(zlib.decompress(base64.b85decode(EXCEL_GOLD_INPUTS_B85)))
    specs=(
        ('BFRC.NETFX.TREASURYEX','Swap ve Hazine Hariç Net Döviz Pozisyonu'),
        ('BFRC.NETFX.TREASURYEX.CHANGE','Swap ve Hazine Hariç Net Döviz Pozisyonu Günlük Değişimi'),
        (GOLD_LEVEL_CODE,'Net Altın Varlığı'),
        (GOLD_ADJUSTED_CODE,GOLD_CHART_TITLE),
        (FX_EX_GOLD_LEVEL,FX_EX_GOLD_LEVEL_TITLE),
        (FX_EX_GOLD_CODE,FX_EX_GOLD_TITLE),
    )
    series=[[] for _ in specs]
    prev_fx=prev_gold=None
    cutoff=min(today.isoformat(),rows[-1][0])
    for day,fx_inputs,gold_inputs,has_q,has_af in rows:
        if day>cutoff:break
        fx=gold=q=af=None
        if fx_inputs is not None:
            assets,liabilities,usd,domestic,foreign=fx_inputs
            fx=(assets-liabilities)/usd+domestic+foreign
        if gold_inputs is not None:
            gross,passives,swap=gold_inputs
            gold=gross+passives-swap
        if has_q and fx is not None and prev_fx is not None:q=fx-prev_fx
        if has_af and gold is not None and prev_gold is not None:af=gold-prev_gold
        values=(fx,q,gold,af,fx-gold if fx is not None and gold is not None else None,
                q-af if q is not None and af is not None else None)
        for points,value in zip(series,values):
            if value is not None:points.append({'date':day,'value':value})
        prev_fx,prev_gold=fx,gold
    for (code,title),points in zip(specs,series):
        if not points:continue
        live=records.get(code,{})
        first=points[0]['date']
        before=[r for r in live.get('observations',[]) if r['date']<first]
        after=[r for r in live.get('observations',[]) if cutoff<r['date']<=today.isoformat()]
        records[code]={'provider':'derived','unit':'Milyon ABD doları','frequency':'İŞ GÜNÜ','audit':[],
                       **live,'id':code,'code':code,'name':title,
                       'source':'Excel arşivi; yeni gözlemler: '+live['source'] if live.get('source') else 'Excel arşivi',
                       'observations':before+points+after}

if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--demo", action="store_true")
    options = args.parse_args()
    try:
        build_curated(options.demo)
    except DataError as error:
        print("Yayın üretilmedi:", error, file=sys.stderr)
        sys.exit(1)
