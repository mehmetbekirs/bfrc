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
function applyRange(){if(!currentFigure)return;const dates=currentFigure.data.flatMap(t=>t.x.filter((d,i)=>d&&typeof t.y[i]==='number'&&Number.isFinite(t.y[i]))).sort();if(!dates.length)return;const end=dates[dates.length-1];
const start=new Date(end+'T12:00:00Z');start.setUTCMonth(start.getUTCMonth()-Math.round(currentYears*12));const begin=currentYears===0?dates[0]:start.toISOString().slice(0,10);
const values=[];for(const t of currentFigure.data){for(let i=0;i<t.x.length;i++){if(t.x[i]>=begin&&t.x[i]<=end&&typeof t.y[i]==='number'&&Number.isFinite(t.y[i]))values.push(t.y[i]);}
if(t.type==='bar'||t.fill==='tozeroy')values.push(0);}
const hasBars=currentFigure.data.some(t=>t.type==='bar');
const padded=(day,offset)=>new Date(new Date(day+'T00:00:00Z').getTime()+offset*86400000).toISOString().slice(0,10);
const axisRange=hasBars?[padded(begin,-1),padded(end,1)]:[begin,end];
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
    apply_excel_gold_archive(records,today)
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
# Snapshot of published workbook cells: date, P, Q, AE, AF, Sheet3 G, AG.
EXCEL_GOLD_ARCHIVE=(
    'c-oY`SCTZ#lAO7hevS%pPSygM9YE%7_CNZmW&M>N7wF7xpvgTavOI{pQB}47^}qhBq>_J<{(t+g>;L`#{`dd>zy6Q^=X$%i|4%L+|C5X7|K#HJ'
    'U)N8cyRLigJ=ZE@jP?B0|K*DJKa_L)p}y|gOYUR#k?Qr6_uYG`W3{zwTeHr`-~If9W0~S*W*c`+BelHhZ22m=+^LP6+s<ng{i}18&(|M|R4+1i'
    'yKAj;<~(QZSI_sJrOY%&N`1|IuJQ5mV~OS^`Y!rSFQcrw)4X!hkM`<m)w;@Dshw+lzW!LGv&inF-f863%bL3_Uhz!#ETiv~n*QHD7Wv}(W04h$'
    '?5Q`JYEGqfz2To)_4upxS@pXz^L#Aw@%m$t?M2$Gw?3dQ$vx+K<x+25(x>jJm8rLVF7o;M_eDzbB6Y8O=aEXS+AyhKv)#Monak8GFa2EN)78fU'
    'ITp~CUnB3_dS7c@E!|14Agz_Mc7A&-@ZsuX0iTYwTh-^<+7n~-HpbO*zjG~VC7q|1M>#*nH$OO*>9I`BcTX}0yJ?5>c5iL>JX$He)z)i&tn%^t'
    'W04Vy<Z)--Zkwx?ap}gIeq7s_Lr1Nq^;qQN^~WO9i%fmQsx3F$PP%Iy;Ct58`_{jigPn7%@$vFwi4{wfaqC~{QFqnOyJo)U(1%<)TeHmG9!q?>'
    '{8(bYKKs(6=cZksb}yH<L%wy3`d?KKFH`sY>YuLvTcl{6e|t->bM(4oKgZQ&DyF<c8?sA7|HpsstpE1%V~N<lYtB3C==g-n0*=lB(0?VF?$RG0'
    '{L|IP0yP%M+K6MNDsMJY<|jJ4JvIJg>5td+>FQ&F79TsyD99cx`I#llC#yYb-dzTKVz|rWV}H8*SmJf~lJ<Gi|I663E+f6BY+J8oX^(XF<Ks(w'
    '{=u<KY}?gz(HMQDEwileCu20*s`6;E*7NZopRPU@h)uit)=%`*t?ZKK(0$1@%e3vP<D2u4H+n4c#V_6$$|)9F?Ov_vx~wCmK%?|q?_tRmq?~0@'
    'AMfk)^~WOlwX^1}FIV%_BiEtbIPYHC*h_DFjsJL*PnRD{#Oc7N$plnRl>Wz6Tf6y*c8q*>9j7z@eEG3N?5!p{AX_%N?B$3^CM|v=+as&@cu}9O'
    'J{B0S1=_7}uT@>7|EW({WZv}1I>sp}AZxol7y0~)W1%qYP3EWXtS_dQnTB`0A_Doiy!C#I%--{9zWBwlP@I#phMrpcWGSQ^MQ%r5GVuv})+?{+'
    '@#Q~Xe=PDkD+*t9kpeRX%UieB>%3d(L$N^_I>?XR_385WC0?P!(tXH<PQ|PWG4+~jlmDDM6Lq#U+I$|yPuCxdyzH-zQmcwBb&3`EU$xxw)+;M+'
    'l1uoHr}%F#KK?w`ci?VrKVff*`}u+j{%$$Cv3S8}xckNB#}Yl3NL#K`j!X_g=jbXr$4!P#cd1Xe`x(T2bN#W%@FI$hv`6&ibWWGdxiT<apdFf('
    'UMN{A=Nca`|68JlH`mSVENdWRqr^oX?wZ>+Zz`imN!N~ZiI0~bOB64Wv_ll*uU@ml<92C#--?fQxzdWZ`?<oWtB(b|oAw^u#md@jJp8qY2Nb?2'
    'OO>m8q#YkFKK^`*Kd)1iw)USsT+@GDN+oBPdFZkQ%I(<o=MtZPa4h2;r8g_rS;``|>ukcUbTSu$4A}_1it}rKas9E#iba-8H956@C~v8MB&(_r'
    'Gim47rJ!s-7WsJnvB>r!@-w&Gf0F6ha>x3FG7);{m8%>npYdGe^Y!nG*qbYooJ;Ystf*ozogu~f3d^V?tWuOJJyXH2esC<4W0|_|p$)FUvUhEG'
    'ohjw?x!G##e%Iq!K3{(<;uDg!EpB<vtbka#_Y4<32{oqEzgEvhK3{(<68lRT&^`2!<c=6pS?YbuETut_LOH<0oP6_xV;S2lZTW>?8*G=plpBq4'
    '<0UfDZ5d?x$10z%KNgv<MUvu5{V$~{l^ThTf<pbGfSDIR&+5-N*B^_-SxKWP?3b}txRH|#I!T}MBn4DtWT`%$<n#5%B5_udHVD>;5{OLqr6@-^'
    'w<4*equi8Y%*P@huYX^pgay{FyQ|KU@@%E_<?4+@fIC_QZRKHtKVE(;QDX@?28Dq7U^-8EUq6aw6k8T7o<8ZL;PB1$$099W)*Mk_n^j4StdVY1'
    'U$1A<CZgNN4*q=ou}FCKl@#vj9^`bU4s5#$F}dte?}{{YUF&?3&(|M|glAW5FEcS#<AW;UE9F*3F;|kM<#3$`{`2L>5<9*~FSo2iQFKv?OzsaS'
    'OZ%}7o?T|6JVKN&e(=6bP4T)EbTk=NouQ%>pk4A9atnIxe9A>Ub-u2T*B^_771ricOfEB5u+>ws|0bMbHxBZ%_B`(&um4-5y+R?Ksl=UWn^&bv'
    '@((rLt|2I<QuZ&y@z`A-uRj)vqd4W;@|N*bI;I_RDfqi}i`pxDCpn4o{PG{KKNg8CJi8MzzFA+{5v|<zF7oCwfHF`kJvQ+-zc?0(Z9H1JWw~X5'
    'lu;-exU$0Bvh`gvu2ymAxy<M5k40uYjq<P5WT(rNp;Lx2?YB#%U(hIlKAz>{^~WMBwy`{f(mUOhyz8Q*pj*{@Zwd{R#%%u3xsN~p;8-ROrJ~xq'
    'RP4`G_@q>3D>B4bRKDq6kHhlu^7kbom>@E{t8zSf2t`!m>f_!?;Xfg}Zmc~F@yE-LCBoP@S~NA&3R0Z5aS*hJ@Io@(dp@4x<K@Q^uM|$E8qX;^'
    '(G)~0#L?TP7*o2A15x6cUVXa!SmKq!t<Iw=$jC*WM_*9e#N{?c2nwY5XOBfb{@_?9&PZFbMX6H8UHawqqcFV_-cEf&9ftAPzMro@7Kwe;<a3k='
    'q$C&3@y)UlMe%|5qP8OS_QxV0uYX^pgsJc3u-e*sPWb{kh$bISnGc(%jQ{bvK3;w-kzePdQxB5?SEermF3UZXpUKc^_vmbqhdhtS=U*HPl~+>7'
    'TUKC@WOnqcA$zAFx8j*}vL1EK&zB!dxW=^=(cT<f!kVS+Ldvh4dAl$|@8DeG<K@Q^GoB(TS=J5cVYNT(vn%<UE?*?&`sJfr_02Dig`&B)6lc~='
    'OE43rT}yUcXIL(KRf<-R4EW>a#}d)nn|s^}pS1rJ73Mv70iC2@_e3kA@5iHj@q_neYBcwj;tl+d!W!L>L&BX2dDEClF-d+r$*0Tzmgv9r2%f$T'
    '>PbqXX1+SL9_1Q(!CM<rZs<SX)_;Hfu}D-U6mr}I<%PCNp2MABr3LsuB{nkNkGJ*l`eTt-<Cv%4ayH6dxx-6&z%2)=Oll7WZ@WEK`Qiu1GI3Jm'
    'cWy<ptJ71Xs?=}A19IN_FWJHI>>Pi({8%DRN;axMzCb2!Q0lJ~ewMOXr+?c1=Ng}{e_tdz4BB#o-2r-G%fTsO)|c#rh{`qhBY^zk@?(kodXAy5'
    ')^xp2@Yd;85Gtc88zfKH<g=fT@!|4giKqZ;7s{e8XUdA2u8zMhLy>gVvp?GSAFn<Zh?=}?%uTzAMiGCn>T}Zep9%_7U*%CL|9JVaL{#G|9keo*'
    'd<~5!`8VoVBU9K{l(5F5Z}s{5_eBCH0FdNX^p9VVAJ~`TPHoPLhm!?w`MJiY%a0`jP2<|PoZM`RIGl>>M>F9R<l`Q#o{v`_3k3dTbtN%u51D@g'
    'Cz@OebSAAL{TT(C4<OAKKRA}@0oPIfwT3%%O7{AtKQfe;D=g?fVh@zd$IFi;Mm)(d4x=V*>$dQ0V*`|!m}{jn<FT*4`N6Twj3?=|YOCQLZAS(p'
    '3(jfQDb6~|k4O1<{jo@(u}Z`FD-KwC$(fFttf+DeUClpw^w>XN{=P(woz-+-bJ2K6xfa^NMZanZG)|FEC{L64)i3@Q8ZW$n{7zDer^q!s9J>^c'
    'X#**iF3KuvtIu!$`TAp#fP=|k-et5(N<m@nhnD2vm=(R}Io4y1PnRD{1Q;ygbvC%Pq1Z|BS<_*gg~ulYd^q*bmmf>`FjC*S)1oR_<u9iz)H=Qd'
    'n#%k8^Sgg>{jtc5MX;YBFfvK8T(m+p<pbK0x=}DV{jtc$>yJgef$5sjt(8-t2)3@Gx1lSFQUTpwTMuN=7uO$)IAR=1T9shvFm3IMy{|Fuk%soW'
    'oVOhHqvQJd^7kdeqmQ2C7vwH;DvA3V;A5KdjY1)OKEj{Rmmf>ySOQ2|9Ye2FiM_U>G7Y{U{Z#%;ek}3n@?(kidQs~JU0JE)=(kG6XsIr3!`2ox'
    '*ztT-AAfKxV<Ru~Gk^_l1+fZZmK-&4KOyZTPCpB$UtE4HVIyB<c@!e;qBLB-0N}@@egs;n>olxK@A!-Bk41b!h-T!{%92~qBlD|S^*U;A_-Prt'
    '^OJmW{jo@xc%7je+$m)+pjQnLSqFP16EPm3gpXIhFYpQ-a!PVMlSZIUlfH0Cx+~@QN@jIa>$$||>yJgk?j`_ZNlB7?!f1<rus&>Q-ift70B4^s'
    'KbElFRo<ZA==qbrn{4ORw_&0=CmPR9f9Kr0pRYd_i7-f^#!aymV=!F)+>5^O)|;>&%?}Uq#Se~UA`Hq30S8HCq5(0;z7;(w{c`_C4z54(zTf=f'
    'SZKYTXWRv3R|AJb8(mH`4WPX`4QW5YPT&0CSZ2p22KXpjB(n{CiPxaF-xWQ}idPr-9{c$758juFoPARF)Mu4F(w-#10q~V~&qhj>_MhJOn_v7b'
    'G=F0i4lZXvn^9SxLiCOs;sjHzRDe^TU+2^H$0FtB8z+5w?Uk;>t2jz8;$9gfta5Yu(NEv_>H1@l9wuM=Q@4j>#nSd`PGC8}9u$u3^z<s9E<cvA'
    'E#}`SBd!3L^kx$<*mBE2m8GCs5$}EgHNW}6u}s7(#A-TR`f7>-Dm!z#xU1L}6HYpHF7w3?j%6ZZ(RL)u)Em%cU7u3tS$|5#eYHJhtR4^Z`TAoK'
    'pBE?scMTY_(wlbaxan32J7s1Y#j<0OFRp)IB=UDEi8pDnTwGCTK#Hayp#V=F+3>kX^!3#*j)iJGP0E2J1dvx0w5<g<L1_(`E!qc<r}=pOv4|~m'
    'S6)d>J7u{+|6K|`ZVEq8Lk7sU$1D4M{jrF*vO*g9K4oLB<&tnx-_`D?#nJ&gok#ME>yJhJ@tbmsn=dzYWRoqqJT9;Y*;|6r{0Q&A`N8`#kq2v|'
    'XphX6I!$&UrBudra{;Mvh4~C$zWBkhOaw{b)0@J<DWgHZ8qm%F4XA@QsFt34?2GG<MSLhLmJV}DLz?cPPMP+lf``Rz*T=X2eEG4&el0P5i5_+~'
    'l3Kp3ZWLaJ60Gsa=fAlAeUWfr@(_4eIxDhtJ$mc=PMOA$EQNOeqsIBwFa8!<FW;iK44iGrD=09j&JL79$>DCm@$iP8U**&F$09!WxPzOxCxcgQ'
    'eMG>!<)icx<YOOp|BLI7MSSk{v4%~ZKBHpA1gxS_?x%mS8r6Wu2K(X%$1?VlRVn#RB~*7=;790fYg@93<Zy=$-{XaSzW!JwjwE=kt@KPGTXola'
    'f^ixZR4Y*2hoSu9`eTvU$oh{rR<17K2=&_ao>78@kdjYq%omrxFA>>rP1^hNdmuL|4RPuX3gzigRC30L0sG?mV-d$-O4Z6ut#6w(Fbn{dW0KF%'
    'ThIFmK>p(TV-cSdZSm~#zCznlKwncAFLA4Lw?`EI`SN3lI3*cgBitz>&}Q_U!nQ((6;x1tWE5Xqe=PFKhjo6pY^WZ3f}1i3r3E%XcJ@Emmf!r~'
    'SSE};A=M@tRA!!(sw+NF7LxVPo4nA@{ZVWA<_E_zVeZo^G8Fn>GT2E`^#zV+=?&8pQJnpJmQR--OYGQP3J;q5-dj6!=}UsF;zKH)QUvk{jlQ`4'
    'eUWGk0Gk6qmo@>YPV6<6N-F`~gjD(q4+Htd^~WOKUnPTDUlR`CESD^XOkAS9nz!EjV}%bFAAi2ZpC1Eq-j>5w&`e0E458_D7vOXCS+@P+`eTu('
    '80od%g?k`@lJ&^S5a0&ZiU&&V1l@dd{jrG8h=R1DXJ2q&hCH4lLhvVzbiF^@0-rBGmhkz=ixwovsr&*@6>v^dnNx#yTGF|5zqtN=k*L^b*~`1N'
    'O_P-<L>YQ_r!99XzQzn3i+sNRZ;}1VfaRDgT!<zs4dnj%Y3)uljawU(XP>#lH$OO*vHb;3dh46XK-2S<8LBiY_14##kVYPE_lxU~MSMbZoU|qB'
    'icnGpsokmqk*NbUK!L4C)$@z%k40j4rQt>~%*>7-{aN)Ui5k1nR~pYJ`F#Dci1(L*KE<(0I(E$fwuUyPDR4^92twE6Fn)3Uv52j&Zmff!;9EBD'
    'm!zC#$)y6?sdGK@hA*x^7P0+RX5sc%1=}tAMh!rJg%fS2N-Jl${l)d~i$t74PY3g<l$k;hVJ*;_MF*S?Oxx@ED4&0DEED?+MggxH_;r>)xPG91'
    '58Yecdi+rzD57tEa4cg>taGJIDJ#Q-DWOBI+~96>N;cW-gN*m_>SF<0VG77H6gosH)5g6TcnaOtPypJ;esuglUw$m%Q<6aW^AQt(Sp_${-SR&Q'
    'n`@TeQ{H(l@$vFwiLk+P#@h4R7Rp##l>A(dgK9pYy^crI|EpiTFBIOL4|l^5Q{1OhEK`(j`fXXZbz>(U=j4m)k43@`Yct+p!Eke9x*prX?<i19'
    'fT!1^3GvMjj%C6QE3k3?m-HnI`Uu)Ry?4rt^!T1{?BnIf5^+kF^$-m3UNRDe6kXY+-mhMb{?@~CeYpBqAkN2Zik><RVuff6##n);+ZI^)3_$Z-'
    ';`8;#A}<SEAbnR{c2iy)X1ncKD4TWg(~sx)c>S?RSm553jYB9<CMgpkt4-@0)D+INKCw4nTz@PQ7Pu~5q^Q49+cOPn-ISIqdxW0V)1Z93{(X`9'
    'vcMZu;m}u9_(#L4CdzPzwBktX12Oo`5B@Ea(<@p@ay<8jimiRWXX;mJvamE|Pym1-{&-_w{NPyTWr3CCXZThL#&!kHs3@^0e%8IBE%JC{pDsU^'
    '2pe3<kK`4wL!`=ZH+KfZT#MX7e}oZVTz@R`vceSLmGW;|N2JPg-nJG*&{<pPVTC_kek>8^L^0s4@D>Ws9G3VPH%_!D0Z=&I$76SWzW!K*bJF0v'
    '-Nrb8VAZ~tU(n0I$8B=VbZ+5qu76*|bqKTdZvAVyYXzQgq`)tODTZOUoBqW-SNY-x$1)|J1qRm~3jHlob0IoVh)703OQbxW<ipj+0@Vw^Zsv|;'
    '?8wLU85jJBgk4oSKA$h^^YzCf&5IOJ2?cyeDVydotsSzAu?k$<^n8#nesC<qDN!(hPKN$<rBx;Wpx{7OwdIZXneC)cd~x})g!k4$`v?>%(m@38'
    'z2CH1W$kKW1GRIBkCz`y>{uf08y$h(A=Qp%f}N=mO}kSG+kUL^@%r~g9Hq2{rGt8bIc=+4LmLN`9c_5HgXQrYA1^<a@Ci{KPJ)Sa#n7WG$!J4%'
    'uQa`4RCro{e2&l8AB*^WteTrbjHHL8kXsz1;q;WC4MqRs0g(CX2gfq8vt%*v3RS+%NgJ+j-5Mb@rTC48!nw-l>yJgoYY~9n@?~W-qR}K@q=czZ'
    '2~VWv<GIME>yJgSz)D%lx)*n#??=h3hHQ!cTGhWg<CM==9}BElVA6=7h+%;%tT_E?B_3e?8@S)c8lQjgw@eP--U!7kIX0(qz{Gb1hz92Z4LS;$'
    '^Rc}?|KM0A_Loj6<zwB7LUA2nMa{J>Fe$x*wW;SKA1^<asPV#fap7gq0oi)T%Fue24a8Av56#Ex`gHlRgzfJfG=||%@2$73tS<pBmetnZs5a#1'
    '8lSH}7V#OGW+o90fJc(^0U*@3{!}Y+tOKREW0B9-AB%+jEp(&`<Zu0flA!N3x*w?M&1Ol6^Nf7F{#e8oSi2AnwT}8qqQm#IAn%o3Kp_>yf#;Ka'
    'asB%uuPOwsAw{n`ugd!rEa;!pWI?`bAtm$F_WR}s$1)KEquHgyHI-dXWk|qMOy~m(Ji*96qSMdUAB*^u0QkEbJjaRP*`}%h%wI=Z&Q3;YpL_U='
    '>yJgezo<;!s2G-+iTkxF-8V!|VLnhEA1^-sysht$DYzAs7Yu^FoWrRbO_7Cq4f+1L!sqLcMQnX#r4>EV!jZ*MG-LaqL#6x}^_q3|Nxr!JSYpL<'
    'C^Om>hE2&CxFIDn7;yS4$UvUOz;CWU7V!zuA(gY~O<7;(rx;Tu9E#Nx-RieUL!PUA@q_neUfw-{F+@xP^y=I=>rrkbQh>|u(Vxftiys`zL=3E5'
    'eXm)wsd~-7(g9ZBY|>yxZtFboU;N-$CZgbkzs_(1bwB#lJKqYP8X+xgsK+7seEG3N#3{Sk;WUyy;1lK($UGHZ^3YA5pXH0|k3~j!6VPCj)DP?l'
    'N!cPQplF#D==S#XCLgaq7TK`~5F?Os3OJjr!d15myZYyaHuqUP{pR}jMO=r_Kg)8SV953C6F^b}p)Iq$n;X#igphpogTH0UZ>)kU2e2>gLPTri'
    'A7o@@XchXT-O__e{LS^pB5_t)H(*^sMn{*2^l}242i6qrsYgiv@%m$t*GVBpF9y+c!;pU{m+77B<N?~sr!>V^KRA{duQEZQSCFw6a(KJ|fZ7PU'
    'p#=J1`F?Z#v50N4B1>?8NXM;BfOd7o3UfCE2gq>GUc)!nAB#jSLV?Up&!Yo2SjY|NnJLXA1Yu?ai*_#a%`c9HB4$wngQ^tw0X@dm>Plj+XiY!i'
    'a6dlHH@|pa=oPo<v)u?1&iEg|bvhbc+wvw5tsl&`uYPeX6di1&2Xa-~w9)3#DIh1%)@e}U^hY0P$S<xx7HI+P%L>>tVh~xol$T9`BTsFdNo#sM'
    'ga*I)!Ldx>Z4q)*tT2plusR_GAcZH$C+SV3N3i$J4~}I5bE~&WC5r$WK`T1PReKhqaF(NzgMTnNzWBkhOfWQ4Gij|UxRv7|5~b3=bc+cg*Zsr~'
    'fAfR)WkOIuHkHb#9(C$CL&K_&;7w?<63GLl{?!kTWh^S7xP9I9?%NuuJ?U54FktCALWS~M=PF-Ze=K6zjgipnm5Wd?hngw|ST8`vYCvJ66m%@|'
    '#r4M`7T)NJdT(HwN^LfHa~<V7qj&*OrHqF}+~>=WB`mx_i|tlWSgkNp0fWklC{aMIr{yQo=bP(~MJ(yk$Uz2zRRwjEb?Lmx{UQH`;T`ELt$uU;'
    'v4|yI>NfLIMi2!XicvH!P0Lw&1?!=+{mu2qA{O2tAH4U50wPzAq*6vnhXOW=9WsdRvC0=ecwZ(|mKr@O9j=VxPeO~9I!$Y!P<3H{?yfJcKNfkN'
    'lSRFRE+vAh9O8x<U+(iDrU3wba1_v5Ms=Ru@PGJ6KhU=<&sr4aQ2M5yqh*16XLugeOJ8?TJcS_kH(-*|(a}QY#&@7xzEMp@p^-Wj7<dH(3YZ6_'
    'cEG0ERTPu!K(7#7rbScS%3Vl+Pt2e@3pxiIpgV}ie{z52cXR`%(k02<fv8oAhuUtpQrM;chPA6iv!KO(*G;)sk#(Jlcl81~I-V#Z5-_m-0C?Wf'
    'nCr<kuT3p)7gUy%;<sb<9*++tQBmihVx|43M6Kxw=oNP=vqOt3s#X$;HiU%<w0JLo_u(~c^g_t+z^1L68vf+iDExF^1>FL0acTW=(#r36i64qg'
    '+1h(WpC@T+4w_60mZu)AxWl->7tlN-3EO;Y-33DBiz2R7ihdB@(oxbgQi0!PhEaf5Bu`NLIG=sSvv&wb89K<I2Z@%Ve7l}Kp+l6l_HH;c@*`lK'
    'o&IpvhLJB=O4HE9V$*JLn%Xo=3l)^hfE#!8z$gl#wL0dp_d-xoUkWB=O4RZ@`>amGFBwt5SJppq*Mjb?Re;cX`ypQ-B<0*@?6%+jM3Av2&J5Yz'
    ')u<NJTvX6a^?My}AfzT4d7-4BJ1eIssCRwK`XtUJTBv%D#WAX~#Z2jjj>Bcq*BSsB+5ye$Bebt0n2}qf#jV3Ahcj_xa?^j%ukvrC#^^3KK{uFw'
    'RaZv29Wm+<I>${GRK4$_(*cyfQx(bn{2c-D4iTn!^G`Z#8;AqYt!;vJN7`69!UlgjjvMgl*ISb-p+%}Qghp<)Wz32p6kincU);(YAPkY@-W+s!'
    'hLR@VrT~}r@~G{~$8F?9Wk@b-Is@)8TT<WQeE;G37R<Vg7Fnacszio1E?5TyB(*EH)?XmkH~cj&LXAT9yz2S65Vu|Y0l?n5u46p<MB0P13@cbx'
    'OdIUnfiJHpqn$9=yf>MZt_`3K+Zv1+Z8(;vCIxWX9#&B!E{X_p5t#NF+t2a%3apHNa}An1R(Y3!XmcNHuh-E>Vu8+8Hj4U3w)VzcK*+7j-=2>u'
    'myRi>*6PZk*6aX3WTqD1c~f#wjsfCuQp;AVhI#{tTv<MOgK_S?{crD8B5Z9vi}<4>59@TIQsRw9T@1dE3>vj8l*|WM5o<Qfkx`oA^dm|Db~E6|'
    'xIR+)WqEl0y~pE6B<c+V?GcGfZKO+?Iaw{IU*BEZjpeNXL$?^9Rxj5eiKl9=(9H^fdU~aIGEwN0;P}Qh=|<^O$zam4`6sbo{*43CcTeuQFwsTN'
    'vWAg#)XkBwnkdY%yopRQK#EAIPUsfzDl0t!_33nUZ~TTez&$K{DY(BI^csvY<S_er@FV1-4}L31FyUZYJZ&(6M_X?p`Zz5+2$crqS=iIfUKgZ}'
    'zT0e&I-rQg){~)^=R_h&{t9XM#ulQOrg-3de0x28DIgdw>5XY+BBb@S|0*&|NXejjI6)ebmsiwcL9+rBFb$5nKLlNtpB?db0Q9=;ZgrB~>1pb9'
    'Y`CBGxC8sWirS0vBBG3B0j7bow0$KSSNv2F`1FYeRR^pYlcu%wCli!87!G*8f{GVqFtXp3^_3Ke$ua40BSZQR$zA>p9~maX4@UUN))ISOdKa+m'
    '6~3bkM7M^c&!iv456NoY8>Mx<r)2gdkWOT4e)O4_&MnMyaP~EN-lz{(qO<nbV=ezilOUbHAhqy+bRVn*ml>s_uD`Dv!SGjd{G>yqcbq&v0w54_'
    'MQgZcfjQcRcez}rOHfzz==zU}Jdnj@U%oFNac9!=VACh(CIckZc|mc1=j3u9Zv@J~O&u8w+AgRi$-^tPT@zf5)pcZQwl@APH*Y#lLT4S_jm>AO'
    '`b2xb8UEjiCh}%rP>^qx(NI=t8J!Iv7s|Lo7(74Qe-fMNa6<CWR{!kg$XPIfqLjW)k_+1uSG0yK1TKZ$v6&mukHtfX4%fs~g9)oo=BlB8ToH<='
    '{EPuDglsZ=i7l)YSU0Hsf(#2PcRhPAKnY89C#L?XoTyGQ+U%>5(CAzK@zTmM2W8wA`c+mS1Fe8>QsnN|ib4XI#WRBbC}vEAgzR!q^n<A~ESUBK'
    '$|)&!fd7PGXuotSw1cL0KXGcx=aa{b6hpXC|0Pe=r&88&>$kGxxC%KFzz2)Wg0j2z#|BD80H!?vdI$I~i<70$%Th>2nMJuyMck14L2Is;WATvj'
    'VYmnT0nekoVW_ukX|c-gK;ptCmx3v2jQ%z5B|~ZePSR26qPXpchcU1x9A>c3Zbfl)AfFNN&13zS!jEST==4}aRGUZf2Vm5+?8u0KtEC`Om)xjJ'
    'EJS27gJIwY$d_k^-e`Sg1O$%Yrznt=3kvK2N=eA6zjQu-Xs37-Q^s65atD<vbFPbN6KF-!2pYkPfgzpipAccvi^lkY@|MxqcJFxg+hXMG$okSh'
    'uGzttj=~=A>%W9m5TJy}N=OI{;A&=}Rz}BAVnYg5mOfd)8L^p!V}voRKP~&GtpQw<<x8k-a5533(#Ju89rz70sefhe@;9c19uD)a5HlyPo%|+S'
    'BZPQg|D=-9+)&8n5U6kH(E(RBYME1}EVK0p?r(}u(7*-?t+;=A#~Nv8DaW^el_X`%?&>tG0wA$1;X`K!@!M26_M0Nx*?36<-lqPWuZ(~Yvo92&'
    'Ykk^bcqJNR+Kfb}9S91;cHSgZ%HM1k2Ik0+;@$|XwR$O@^@Y2sO*;a}LDr_caiY2gO<T^ZVua<yj}B{rj|_Ft10W{4t5knVanMYpv)K;E`Xj3H'
    'Wiht^5DV>k{UJq>6!IINzM*m}kE-ZTuTY5}U2@ssn+!vqm?0fxxQGJj1Pw-ZFiVA{iF(l~0)WM9JQfdd46e2_4lc=}v+clw>OmVO6DpK)ezcPi'
    '_mJNK11X2Z6oksnVDPGq;L;x$s2M06)G^<R8BDn_ITyjF^U(v_AY*hR%|=@+1B98LpFw~EFGn%_#as>kF2JIWi18~NQ_6!^Rcb^<=|^_8BbI|h'
    'mRqnpCdJpw)%KkT^^{}ZzaU$sqDVyRw}OL);-X`(uq1KYkY6ssZNzkWA=q459i;*ayAwmzCb&6nK!)2mK}#M0i__NCZE7R(O%$Y@t~UC;1at`T'
    'dzvI#%tR3iz4;FqN^QO*yI&d7p`%2tW&^E8I*$%=LbE3?XWok`7dKgt6%w{5M=tVitC(=|_p$$9y<p1h^j^8e3R6v|@aAtJ)y96;;nTKjPKS`#'
    '2Id2{2#?1TXlLjaxB*3-iW^Jt=9Op7jN&bNx1KM!jy>>;R?o~_Tg8RTovaqbNzZ_z2DCP$khJT%J+p@*OtLRI*bxLu?G!mbCsvyQKlp*YaalAG'
    'V8bFxV-e?Fk6Q?=u|5wHUJP<XZW0hQW?qrPLJ^HXgm0j?3n_(6SNAktl^$gmsKp}DzpSWVX<os^viP-ZseF|2)$D`$hpmk}SV1nN95g!qCR<iD'
    '^XfpfR_*gjw_0XE(HLJHmq5>#4~zqZ)I=;tUyH~`Cr%D0V+`SE55vik05U0p2J2g`>oxeMBc4S;G^2}|VW<+gY71m0cEZi$9q9bsct7+PkbUA0'
    'LwXr-f)M%OBXp}SE?_9KP@9ATR^kROR7~(VmJb3lSSTfpoL^L`hGz&;;iO~<$Y`>D!r!q|3g#yVmQhDmFKQ+uez}3dOi=hZ3mF|bV1??Lqlu>X'
    '8u{H4{tnHN2he(0_wIpe-I8m1_~ce+^IWw`!Qmz{)Qej7LO=9%v{Ni1+g)9A>cRd27(<~_?t^j*Kt-w~Z?w$vH&!PTpO%4(RPI-ls5rZ?HY~FJ'
    'vM6B(HpL*6b`+T_yyqJ+6md}n^8443LG__J`R<!qOXAal1S0(Zqcq3zK^LO9YSmcn)odkt)cfHx;#@%z=@iPdFgAc;e^WjNxZeV8W+Y=ry96i9'
    'E0MdG70~z0=*@fZl3XIgGRio&--_jB^@yny$uQ;dGCyzPmla=DRMKUJ71!!a=zjQD`LcyC)^0{+W_WeRXYvmdP6zGOA6Qt3`J?KLPdT4Hv`7+M'
    'P>;}J-Uh~`z)7`$q%ueaoC>X(MCQUab44&Gn0-3pljs8CL5-xRX#c^6R>;$FYj)r=8qk697wNc#hyZCnl4W72q3-e`={|H?Iv6-YF3@UZlN9r*'
    'Iz|UuA9Rz{xTUTCgK0Q)OkEhJB1$#G>90IP8|UIa{W1<pof85JK<Qt?a4<L65DKVavTF5Y2CaUp)38XhkdvDPN}W@yRS1B0jq4ndh8S3G*7DZ='
    'rRcC9_Yh&ACBznitgJlj^+w})(_l=9y?X}^iVQLbIwsml9JgwwOtEXGf9n*405&go<4~TuI#aV`q4_D1%5eZA5X4s8=sgj=k(_b_I^`m?-jp%x'
    '6mtX>F6xKC;rNLgWj9ai5fmthcoWiyukP_wX0$Q*3X&DxK~H)VeqN)3+=FCns-q?Jz=%X|S-^w{uTBErfRI>6-Weu3yq?ZRZ4UQBW{tgI?`Z4k'
    'MQuV<(um6rlui$I1s#KZ+<<L>_Uk5wLg;r>Iae%0MWK`LXzM!;Z&Y-egB4`h_J#LEI)OSM!MIo73|U0&lZt72^#u?+pS<4jHj=6Qn=sTV4im1>'
    'ydV}s-zVL}qHddkbOgibM^gAe5mgEGP@;%b7jLx?&TecV6Ra8Rhj?beoBZ%MFsB#~P^#^H`QR9850hbF?DbbjLuBnbQgCwQbz=%I57EWCF6Z2q'
    'pW9YOZopBg(YW})tSDB}t3x;_5;cNM29n8NA8$Xnhsqi#K2Yve<OvgcGvZM<Oj)C9Pi_WVAra<*y9y5T%ecd51k})&<Kx=LqTi&D6&f6sP*kXu'
    'G#5JG%AZaX9K=Lqw{HZF^d69N=k+(wg=#KDDbm&c16BZWPsxn7Ltd9p&2}Kl$xiNf$WT!C;rp;Lcuj?J9esqsgxWtPxbknF5rKoO8<ST(gLTPr'
    'C4|k9)a?in-~>q5=;0u7f+EiqjYI|wXK3UYFV@)#6d3fEfD`~)feuQ#wzlg$=lZq)vaX~b)M>IunqKs`^wp4Y*9QPO$_8X)fJ`|FiZgzchzMQD'
    '?x()5-7q2WO)D6z2PfAkc75aqpB7(n+<*@N-xWz#?Go8JpJPf1o>odJA{Bfvz}`V#!PEFN^_%IXFba6V+!DrLXGwRh{iBaRDNoni`v*b3dWIms'
    '+E3(c%6^!t+-UGsMt$l(c0x1$iS=>xy1||evVuU->BJHNG96&r344AvkrtdaIyEz9O2RXuW6+QD_fm!GbZ#5(FChYd$+e=<G1$&T{VXqIq(id&'
    '^+8!TM5`;G9a?1BuD&(bZlF#&^C42tR5Yniz@ANq^$j@*J#u2IF*jY@&B@gRBlW6uGujZvA79@Zxm2=oz><Wi+8~UAr${#Z>6wK?>#JMtr5QNj'
    '33Zk`*Z=*VhP>u2b@C|rJK1mqniBoaPV>Kc&b)5QiP%O2hqG7E*6jA~3jfL8NnpFnATqhdn0C?*{SO)P#egjOIkE}mz2($i@h2tCn_(x35sK7T'
    'W@Z?+=fhEj<M0t%F<WM!z3OHs)0p&-IA$gPE#eQV6;vC28uf=c__H-CkY)B`Z67?s$=h8d!XKh9hV-)Mt?XOSX4T@ELYDsUa!O13ZrFiM+U|@+'
    '=FvUT9+7;GBGRx0O@Tx=Jg@}qxPcJ7!>VZKB?@rRE-8OPey3(efJya_3L^51xUr3{`47=BHF>&)gnbcy4FF~MQnoVW$_=wm4uv{dn&-Q>;{|4W'
    'gAFanF|vmU7nUoH%y|VcfqZpASbz=U<cDZQCpmHUfUifrUlAF!YQ+g21^<J4WV)1g2BfLS&RY?AF`35rM<u|ti3aMRm2L}Gg)s(|4`X#9{beet'
    '<fx2JVOEx+EffY>KUqV7k)I5jnV0s4J_j%!{@wBHuh4U8uNJ10Q?9nQGTdkSz5wz(+wWp4P-*UjdGx%|bf?b_=z_Z8VC!vA+?KE>)HW<u)(-r#'
    'pPZ3PbscvQj<*{^l9>a{{H4o^>onU?{zB}Lw?Ap53{M78=qFR8qyk(FnmGDsj@aRLC`E_RuGK1MG1G99@52l;kKp|L0sL&#O{NkZdN~LgQ0E5q'
    'L0Ez429dw+=0@2GO5NNSmYTud9UX1-y2Is62)3Y%WPC3rtvAW$@+^?QdAH@?Tq%pL@LmU{|7HaaHyan}g&wpz?w83XFw)tgkvHP0NP?UzlbhS='
    'cMdItonlobMaDEA>3f5)-R}js_8WAEzJUxkVy;RkwT*5h5E~O66)N$f7vQb!K?isk*<4y`7<cS0)m<K5FYqQ!r@{`#Mjs2wTWD})p4&dcO-PZ%'
    'SLk%ea#~W*ZJR~h-q3^M!!x}WIv~S(piDwbs7x9pDW;|Z#wIrh9Q<@t$2*gmgs^?+6y`rtN%=RWifQ(;@LHdVJre;x1wxzlIQb|Y48Dl=KIK=k'
    'u32oHz_QDo(R!yqKz){52w7Lo5WNva@6|vg5KbK4mEZ3mv^xmTnFXhBWX0JY=92v&7li6H5f2(=Hyjg;tdhzrGtaRByWw?b>lT}Ma2tVMQE0>K'
    '!kwguhHg4jYK|MoFNuGvNfiX7ZLVDt#+))QNr5-}4~!LXFW83eF#&TzI|BF&Y{TMcnM8=;E{cPcVNUC=5gJmf*g#A7aRcQg3=Ck&SuN(nk$Q2z'
    'jhQfx@tK27nL{ad1&8;itY)qzVq43-=0u|jm(}T#6$b~7$x)3ZrJ2VK^w+2c!;A=(E3B>`;DZIV-j-uHk<(s+z7&El3_(+ZccWv1G&yiSe*u(B'
    'mmZoi0m+Ty1YNFigVTHESpPLAWHYBv(Ntwj<c@?kGryW%TxIM9+IWC*WknNOH1#41sq`yAdSv=eI*Rmq@$(hwa;o`y9UDCvt6m+w{@;mR3JakT'
    'VYv|%Ql6^U3!sbfaoJ=Bt?meSLU9^sRu50!#;lT))1o%mffS7NkvdxjFNks&ByLK#8Ro~aehkK0IC>IJKxh=J{8uQ7(+TXZGtt>qYP7)%@z3@}'
    ')u%v^(0QPzIKBU<Pc2%QjRcrKMPr&-f{J>u+vg3$2z#&voFPQzadpkSDh_agoxSXP4anHOfua!RqBeuP1*UhfB+CQXLj-*%XUcRkv<;nZpeg*0'
    'GW;EA<q0`#y}}Sm<($y>2O+PvoH-HH{&$#hXeG_92DW*!5G!-|no~ZCR<trI661vYvW7Am+u`<3hv)q_=n4oCxEdhF*~r8?_^|FM>U8H?UsX?='
    'I}EU;U+L_SB7kPi)cSy$0C26;Zo!`QfcB$}MQ^!Ln_K0$g?RsRaSCZEl=DiBO;k)fLca$n2OXfw&I0%dv?w`7VLAZ)Gum*(l|KJOz8>2Fg37|d'
    'M2JeM29VX`0|d%ar&M7Cm=Z=aS_*amxuxn!yHEE#sC(;-g1aN@m{`fe&jvGS>1%r4IWhQ((eNM@j25oci?w@b7SadH#~tL?z*@AuhztwUtIX>h'
    ')(6iVWH%AGg$9CJ;R$-hG(VW@jRmHzT`Y|8!=N)jzKAJqb1#0-ICr0DQVLl|^|AlX`yR$CBNohtB<*$a83YR0TjYKju)_cuFnonX4rjUC*>1Ku'
    '0F;gR;(KNWbI4Q`YvmbnMe{%rMmb+WjZxsV$rM>=hfR+f<V=zfp_b2Jy$diHQosr}z%-!p1^0$b6rcgHUSq6D+2~mrfVK==qIp46k{oUyhad)G'
    '7SocPvEb+&wRW?E=|@p^g3YV5&Yz|}xfrcMupP?YsHk<a1LN%$2z6>q$*p1hDacEdinKeam>hQyfGe^cG+AnOw<pfP0Q>|eUS<fDE|Mw;8O)zS'
    'ixV{}Om%XVn+Z<akTWn6+R9Ky0SjEPg{ZoqB$e`?u66l0o(z6rGx=xxj~U8Em9qkX1nZILz-77}IC3b*#nJV)`9ScHvZewgV{I2TazaC0YD_Xn'
    '!D*;HK*V&M!vH)|nFCFx2Tn|raAy*4RuCg4m8JGdV<VBMfGbvVm5smzUAH}P$j6Z24MtI}(<<v6X<Fl?k9h04-u*y-QCV~V*<aub0WrxI4u%<V'
    ';ufWRn1q-i%Bq9yRVYVysB?>M5YP>9?OMO0I7t*TT_i)gM1G3#^efXb-N)*NJQhAB{1k;vG(Ik!9;8Uns9#{oFoMfXXQd1@pb98z(v}YJ8Q2Un'
    'IKZ1KlD`>03CIufLu~-rL98=FZ1uwk2jr1*_YKxW--oCh`$}(hk%20a_GhdVUjUPV8=(I|?Q<$uAttHG<uD!}rUg(TAp3ROOc+jBP<Cb;9vd(a'
    'kGW*FvKfaoSzw{)X({s%Nose1M1W_(oCc6podXZ{S&^zvw%h>YRSb1jY^u1Vdy0=0u^YpKYPEDZV5wvM85W4N9c~!4rQ*pvN{dNO$O1rc_#`1x'
    '&4hKDRF&2$a|v``)Ve^Q(K+)G99WdeGlO4rFSYQNwQ&K%92@X=a5;q&8rw{>!o)W)YdYst(iDtQ$bua23>)oN8p>oDR4A8~b7?P*pu1qLaAJGU'
    'C_p8K!cQt3uqlos_<}v^ywarx&aLlpA)t%eA^uA{th))1JQ?U-a0tW1N-IKCngPo#35Nn3fDB(`qG%`MH9&JyHz7#=6D}?P=CUck&Q85HMG$n-'
    'ej2i6Xq{?%pkl)oF!4%%iKse8zUiLvN<c`!_}C3p@+AT<*-W#I6oKEI#|1EZ9;oN>I|<!m9og?3p3R(|Zn1D$n-sA*{`&lYErwmcHUxM$`r{M|'
    '2%+tRj5|@_fLj15OQA&Di0wPh!tZOAn`c^Rvw5&KPhk|9LM{$VVsuZo^@A)+aTa)Yhd+&*q$m#Dq*%j_&6hSUQ2^Ei9&i%z?gU5pi2Tkq7%!#`'
    'yx*Jp0U!`EC&h+rY{**6ro}iM5Exo-q67i_4RqN6BNpdsIS%YV&Os=eaKmmyuZt4Uw#fH;Ao>~tH>kASbX3foB$$I+>)?ihocw~}2ZM8zsj@QV'
    '#2rGau`>hKA_3<Gz<WSjI+OVTRsUfK?7+w^7(e;F7V#Por~5b62^JB}{N|a|gbp4PD|1WlrBWs|l}zWKes=)l6$VX8#vY5r4NUwboqpx3B+x&T'
    '`X0jzCa~mp7|v^UoGWCEEzNvgLJ=2_Dj7IQZ_C>TP=9#QL!+X_rwwGq9@h3+Aoy4xrHgDp1Aa4KVCR@3Nkl_Vb3A{<!Sv+r1`N`>v>4)s{4Bl`'
    'Ch(Y!K_{AMnI4k%5!DKuWGSY@5VD%^u#6q>;ae1f(5@h(T#M@2J`TVu0>o(3z6S6ve{rE*N61)k;iU5fpwO)w#4jq9GiY|i{$M?+l?CFz1^NM;'
    'KB&5Dp~{{>78!J(kL@2+8%3GU!I_o;--@R8zVZ7&EA;vbI#dH3oXqZW9}iRkbdy2P6V!&`M`i#}s%Q+KEe<9(W5yXo3Pd*l4dN>QM%4kI;Uv-0'
    'k~2esn}!ARuc$cSI^aAQfYckfQu#b>PLG9k&vVgdVBWlq{{$R{=)F=j(34cq;380}$08m#5R?O2e~KlE5jRslh?*!35`k_s0t`UGKT#|meDV$f'
    '3BNrcG=Mr^-4OtM3Ci)r1s=%?eB!1#1Evz=ec0bu5UgWH|Hnx^arZ@yA+y%%?jn+i;cggz4YJo*(dFD2EwAPwooqlhTm|u#I5F~?mT0DhGW9jJ'
    'iH`#q0Vq-CZCit!VUxKlz{Zxc1DVook}nqUCLF{`C?~uak-+5})o)rOwtr)p0S6|F#+Vstq+m-gQ|aV7y-#p%Dss6L^O<huMuw6#@*PUIof`6V'
    '8qBn0P&p9%WNis_yJs)yUFYQR)AbV)Rr<NOXg&08C@EcheaQ3a?fmEwDzK#k3!Hnpt=P!p8rAlUZ|lB+Nt_5g6Fra1Kv4RAfNh(<HEj&&ju|xZ'
    'GO83)#uQ@(pe}6Y;|6NjUvmgRxYHbM#*8znkV(PR^1Zq#KeQW|R12=`&aVA;OIbVnP@L`KPq&C}#YMwjhJwx$O6vXC{4c^0r=8K-M9HRz!Lmj9'
    '2Et8)uUZ{?5!Jz}tDcQy#XZ#Za0yU8{P3tx>oJ{hk`M68b|Fn=CPZ(?>Hj6%(0<xhhO5^3n8rU&K=W0*&EFoRA@gYp!fQsVR_J)tiP@*w1pok>'
    'x+@0Qx&431?BDbs2HzIpvt#-2eR@qdng7~}3W4>q0BmL8#xPE%V_#%cNYa5XnAb3kP+J76g-zlcpnh18qcW+QRFdfq3-d_ZfA@3y1#6uIZE{!{'
    '0pk3VsFqQG2qgmbxJ({rnzW%viH*%*MeS8vp8*TaA3z5wqfMFVm@tr(B&FDnX=C+vtp7`AFHF%X7W;JckF*y2s73n%dL!Nsi$JrgnfGnvNy@H4'
    'CL$H*!+K$G5cfjQh(-kI_jHPX3^*_Uru^ybFAzLb0Qu(oLL+0DVgX*E`{)l!=`IPA@LzzGiL@4(kC90{dX8vd1~<OKe4-KoK$9Y6JJxQm%pX>>'
    'F|-W4)2|lomJRe^!=Glh0c~$^82V4CyZp`G!{z4Che<8~qX|m=J99oJ;-fA_3{H|&q}Fi*!M20di~0m82SX=-mlJW*wB#+g_(N$G)-N~{O5^}M'
    '7x)@W6uJJb8^bqj+NG9wgT30wdx_WQ!Ek@g45Z1X<e2IleOF4>jl2mkHZRo**A*KChmfo{a}nxd-Y$%cqz%XFGB}h5JkKwK9#vkXw*$$NvQ#-1'
    'pTTq7Sn3Yd5m1yHm?m)*qD~mTUfuY6FcDybK(m5|UnE5X(89~=;t7?8Lx-e^Ocur<^y=<RUI*giHDnNY9+(5>7~qy`v<xX8$q<8-0&ztW55-z3'
    'KJU0(vXx{z!%R1A6nmXb{|#9r`tD|B0k|8OD*!ut^!Mdo1Z#kyM2hsgAkQ=lIy=qgrii*oZz->B1M>j;bUOyci33i3%JKBt#$-==ekK!MW(T6W'
    '2`&(T?y-7U{bBh>f=Ty}8WGT>_D%|#sBltl&BwFLErLmN6iu4HUHb1d?T|L3nMf2Y+j$%8RV|kAUUZ$Wnj7?FW{w$`o?j(p2vTsV=zg2?gEK>v'
    'w38XU-l`%mApcm{?0`GDVvggcTdLnd6GNhr9*I2gaqNQNOx^xQim6k!W($#9xsIan0=&EO9e6xw8YUnR6rK!a_25eSOq=}75%=KEDX?n;<b8F6'
    '&l;ft4Dhwz*MEgu@b*zLoJM1|;b;#!If5CRA=`+m&`?y=R=Nw}wz&ps@J0}SP7Zm1*f8UTgKMG|#PCWuL*W9*=5YgIbayjCj)0e3k#5Bzx?)!L'
    '0P{@I8*(|EKxlbraiDMQok@BRBGQH@;jUrCZ@T@_MbUmBkzEd*kL4ranlUfg-Qf)8f$9%A3~m=?L?~DCsYnNN8s>JBm2wAkd%PlB)yg79h0D-w'
    'C;yCg-^SV%_N=Qg#|`v2dwSXp*l)q-MpQ`V)3i@&%#F@4C0}_{9@S%%a1YZh78DCe%4-hGyU3Pou|0ApGenmtnt7xOdXEkG;yAm$2T;`Wopp-M'
    'Wzb*h;3~86-qu&Llmy$9`ZmLTGV9=QDX+3TZU%eQAv$YEFcWWL_jK}@bADL92)US(N^;YwyWyZfFx)mDXh!ND;`mq4Z6Kax?W|R)$m2$}LEpB`'
    ';D`M<Zob`r1fo81BNjR%oT{J(=6MS*Js_D(P6usO>`N5N(Fjn$45ttz79+hBKr|AZVIfu>uVBZ}e*`k+uvaG;X5=*CA-h2hmNV0fqDh#Kz~dEm'
    'QNECEC4kV&)0SIN1A|hiZ|<sZ0ZKZFFBe@VG_K5GX242w<=Iu2Zqz$y<3)|nHBWpQ)P(|c;yv<NugIr3%}<~!8zIg`g}ETx)lq;j>wqK}-=95S'
    '7D94^{5C+w5qN~ci1i}Sv7zp~X=78qpl(8SR))AAtCtA7CwlnEe_J)1_YbrhwOINK#=F9fv-8I*H?U!ZeWaD>dy<J1t31%S1q9A0unhCv#cFU@'
    'U@j3lI&L6(4nFh%{+oqR%(?A3YH3t!yMvEq46y!i1BppQ$x=L;GOEZ|P>4}53?-z~tXZZAqRLV1EB530BlfGygPa?*m>H}CLMWzEK<FsT<i|yt'
    'K}Xy6ej|lJX-)<dG8S?oA#*{9)=lpj=po?YsC!moaZ8XNcMxR{y8i^LfKQMm=~O)$1BpgJ_xS*UDgn4Zr~rcP@eHN}F90TlPXJ&)5-lD?3B$fX'
    'KT!5WPvXEntXK4dLck4?pS%Z9#SaJ`rc6&}WmLd6l$|KL0I#-K4>B|y8p@AQa~(C>23JF;C+z`$Bx1XPZjuY8=ll2Z%gCb4fDY1V);B{riH3(&'
    'Pe?j|SfK($adp#Y2V=A?5kN}6t!@O;nGhKREoXL_f40s2*5N;(PwThuU5uMWTntQ^exr8*K-P3dI{PF%XobMONW^g$NfL<;<$r>YVc%=WU!|`>'
    'lWSZED-%mjQ0&xzkHw?YU_ce1UAWqm`+iK>;gNex1tiR6)XGgbz_B*B8n^;1n`!*^b`pqi96f*quoiUMkK<z~;g!-^JGXv*sT|~Dzg`hkQCLau'
    '65Sq41|o37(chraCN%QQoZSoCuh&|^_ikX+hTT*&0Im8#6sxZYRwZU36s8N$V|l|q7P&N_B4mtEpwEV|o6D*%mb|(=J`_(n?mgxPXiL$eqtt38'
    'EyF&54a0^&Tu8Aq^D~!w;FQE+%Zy`v<31MnK`aM?-e8_+P%&K!B@HIvHrHF}zrbQpv<Cp+BNXA@K@WaW(van0YPOa0wN{Ublr-`gzvr>X$NKZN'
    'zNZu;t;ONIfYTvsM-Q@OBfY_!-N=iPn9^bgoQDhw<XTj2iVq&lL^eMX1hi2v{SKIHcEb!X+PMKk?EtYB8kzK?5gxoYWs~hL8;$lbJaDq8wdi{='
    'JMaqqE2cqVpN)3_5j5_>Z0ZGbvt6MsL=$aVI_Z4;z`WpR5+F~-JH-qJ@Xy-j$-P{N6-GQT1sxrS#P@6$9KCLB6ZZVHyb;@n%CX`o>Wv2H=#G->'
    'F~CXEvHb&dN7_V$js!Pzhv*UA(7V@U)#+BFY50`eh&rZ89DN)1$KOVUDVyj(^wrEz14OvOhSPkZ=BCtZAM1zC2PBq+28id$TGW(LyxjmN#&;R;'
    'T_E^26o=^YW|-e{kWl-XAC}D-jUxbw91v4Y#@&E9Nf0?Hlj@_Kw_xBchMH;;wX%-5J<<-O(@aUCNrvesAC;vbv(bw|wQSQPkOd$hD6aS=>#`Xc'
    'N{eYKLI%RcrUgrp#u4`f=5W!YcV`6(iPL48A84Y{pKVb#rzTHt*MtQ?AoX(nnV1Sjd$SNYBilgOFnt!>(R%HGjwT#bZf02>w-C@TxOH@c=ym~_'
    'ViTgR2oji?pICY}Ar;@s1Z(QPpb&uF5L8gIiJ})O`XoTQ$%^C{CfGB&f&wi+bezE#@Gfv(6fF5e+BXP}gwz@-gOmS-Y74Vm8=9epiviA)joQFY'
    'tuUl)zz`>>Nz{vh9Wts52n=}tv=84OY!j0-*=~c{HqF$jC)0BQ@S>L74S2ESUSpPbjv-(k)ke*g6HkfRz+Z|Dkq=eNP14RKh9MfGqW$Iqeg~W4'
    'p1~cG{E*JI7(!kxpGAL%>`Ax4Xed-$=w)^nJBV}S6cNm&q(4$vVH_w94=fwGbG|*LxzZD_xg2*8Mu03CXpb4o`T$Tn5!0vuR|suf|M2V&9WK4A'
    'LK6^TFwy=bCGFAlO2)w?BPS-U8!R>LCmJQLh#kwnd?4s0TEsyAWpIe|2UQCI?S$G!9MP5lQ%ntOMeq%<3TC1^j6Q)tHQ+_UngXm3oQ;Yj{1LEd'
    '$Lhb^A5J!5CFySt>5GY+5zzZ&w1Lvcc3a*cdC<`e8YSAR^r;Y)%HDuv`+#DM-C%;nAZ4lalNE!>g&#LC<NMo8lJD~}seASoD!-eFhJ^Jza^*cH'
    'DuNVyz&mmN6<Nq_BN52{gIkA<Y{|ncg1y75l?7Ky&>s8GE|B3|!R<3RlYNz}TF?x)09noV_m~XGpBu?V3nL)Ihpr-xVEtDO<S%`pY?yHdhNPo='
    '<DyYTX4>aF?Cp2<Dkq31=72AQXtO=MTY>Pd>0W_lXV!_n)I`x8`6(s{P*-Vi8uo}CsK2!!YLk}e<m_$@ivj+29-O27iUEP=fF?u#Jex&;1`_ty'
    'B?-hMy#>*_(t}ZLp~cR#G2a0zV~BXS#vxiEE}!5TU8*aH4p6D!o=k*1?jal@OY##yswjQ<l_Xbx;pEVF%66!unTD&&dgPX1l86PnA*uu-oBnAB'
    '=853kW#a{W98HaJIKkghu9WlTyDbag@efpiHnK+zIP9|MlEr|~o!N-3IZbkwW;h9;SRV`yl>XMTH%72NUVzRma!MVpc0Kk$M8Zgn0HC6l4whHX'
    '*5Ub}Rt0zoTxUgq>PDocLdNo;XI>EFTu91MGj#oCPJ>t!F0<mg*o2MYA7H5`vWUkGgdu=W49YTa;<5uJLx>n|VL&>e4he#R&=8E|bT^FF7a4-3'
    'r>y&#D8nJrjU94T26l=On}F}>35{d@9f=s`yqmz(%pTT9MyejTCX-_*L=~GWtxSeQKxkzZI*kByh_SM1-%QA&^$ZJ~`Viz`zJVo?AuyK14+IBA'
    '?p|R%2$XDC$2GyE(`2N;Pd^DfE|WYld}N}A$5mKkI=D*7srQ)tr=U+Z#KUWekQg5WQeI~n$q@MU$MP@SS3<?5@F9SF8-CkRGGvN61qMWbXx(l^'
    'mhpVcng$C*89Il|Afv!!1{M|@CJo#J>oauVuOQ$Rw-3Ud;}!z{OzEGfkCGFzw(|26Gz@XrVqi@1JFqii!V*F%lx%M%7&k&31isvVB!g5S3s8<;'
    '(TEc@0a&3uqhN2GcTivWtDv<O^h}GXGuD-*o;#PBS#b9!`4<o>5PnxSd5dCwqQVk&5Z=C9mmsDmISM9?ks<6Qylp#-VU%O3umZN2mL3=u;6U@p'
    '*hfnK(3(&Hrb%KjS+)!;e;q-`>=JR?9wN=5&J6!`1)d^#@$j7nf@(l9t>-(KA-f2=XgZ(Qi(<64;!vf)l)9k60eMAUA>HQpn`lx)ujroEVnDFe'
    'BDg<vFcHlMSMRxdjEOB~8It$pGcSD&yakKDJdklZ?Q@?K6c$(qGDX<{%YE<A*`#=jk0L9P!A5Ijq2w`2h;SfHf{lj#+Cp-dhhQ*0WzzE<L`evs'
    '4BZvIs5Y#dkoYrbIg<$C3&6Y})<Sh*qX|+kV2B{+jBeQm-9miBkSb3av}~PZ0>m3s8)4bDecV8F!g)IDWFYuueS;0F1Ba{!wuT+bd>tli+RRz7'
    '#GUK(=!{`#T0r3|bEZ200#s0jC0#ZOooI9cS*dSG6lSSLO%wn_M}sF@FX1xt$y{%WB&Y`i_$G9m$czFQldnr?;eo?O2WJn1Q0d`=HzNqMKB{e~'
    '=i<?q7;l-`?`U0Wq9)|@Ne`lgVrx=vth0-#6p}iZ=SSTL%ok7ctQ0PFm&>9zDC>smwO$t^CyW*}G!Se8Ses4o+us%8eh2TuW}2=apv9zveo@~T'
    '9{B}^!b&0t!c&rm)(0z#<_#e*T?F7TBL@_fk~)*otybHp9U^FH><mLS`#zQrJCK(L+e}8$x-<b0hg$xk+cC^YLOzAFg%-GO39(4aB2DXn1rY^@'
    '0o|P<r|aR6JuO2DJ5M%o9E%4EyICY65q;~8U8t5PvMm}?iQ<-rt!OFamD`Ku%Dd24X$Z4hM#(i%j~`DA<&u}sL7KP^LpO1t=}mxxIjB(+m5+e%'
    '&t%gHyo>Z(iT@<VVQ@Br*HGc0b0F?v3_LddM$)2NW;K|&R89-AKk`{<a#L8YW{)(KY>vgl4Wb?^??5M_I?|>=R?%Hh;8(1C3a$=_L93zv0s<+a'
    ')4PB@(S5S*rK()vj*vGuqa1EP8qIB;@&*L>o%)N!3&AP|ZJ=xZgAxs{1mLMhle2t=syFE};_%G>-_<(%bmAayI~$>~SK!a)9X;zxrx8xNw(v;D'
    't_vUlgI#ZGQKTKDqPCUvjlez7f3*Llp#=12Aq-5~E8lY?l1r+OAXkrMgt)WO6ovtT;j8+t)RYk`Ff#49g%<>LCuoO8^RS1oaTJ0O+eBimzWTA?'
    '13{oeMn&V1t_PLV>HOGx52+P8Aw-YUM12bKOg|uxeg~qk{4WJ)BHM1IKdMNE!8A?;MHM1Ql-(ONG*{cx{s%+>p$%dnJRg3COvI8b%sg}5UOUzu'
    'R1W63e6&IqT`oEoaB!=`Z~|=0+AW{+(0?^F2ZAN|obgHpDsedQ422unb_Z!vqtu;UQaGm7Npm7iVBY(@hy9`eSw0a*PaoDPDULOhkxdibLijs_'
    'gaU@&L-%@9$pU792$98OZT$g#Z*)^SiqTf;B5+|!#YDjC*oZG05^O}Yc91#T(>y!<6NXIF%+;v?(u+Mc{TK+63ovw~d~nGcGg`l90;FsvPNsK*'
    '4K!*Com#3Qv~Q0)2=Vo{tN~R)B{c#p)Z1{Yr1=n~`3`^wXtSa$4aO1dB)VO2RCA7bAIRh|rUCGm<Bx@`JCPgmPpFX}tH;a%)VlRop*KyB#$|Se'
    'p2wmN6#YoY2J<YKu-4GiLt&R@qfwf=eH6Fd5rkRmW~i$?<}09S%-|4@Ty5#t1ut9zf)!v^pj61vhYFQ~3rlJvB<c_V^zm(;{Q%<NIJDhPMy44i'
    '21RuwSQi|82M;THcipm<NCW5d2F44QW2MX5EiJ3q0wfnjWTu4z6(kb3qA@A$K%f%FqO6BSfgpYlt49TCoUDm|049k!bd;rBqL_yjdchLrFdZ5%'
    'z;zeGZN)_I49c0vrn2L10unYTm<KAp1}__q#>RDJngY~qel*-um?$2@z^DYmFW5rI9jq4=l4AP8<C89hsyoozf)_JBkuVT`59Mk^LUxSPxy{}n'
    '1_GbsmMoI2)VClg!BXxn3nTP`Qttce!9fD!>s}tTe)vSzt4|QEk#(>p8%!@?$BF_Oi16~1Xdgj6ra0`_C&9af8cZw$9sy|(%C^Sk<F@{m&%a1a'
    '56^HjWA6Z*Bf~;%*%hJWi5utxSDLL9X=Za#j<gZ&^z5pU$<Ph`ZsS?0-2W^OVn@A({_T9j1N_c@2G2kNBU|i}ke!APJ_KwNtZL*DJVnn~|7LN3'
    'ay3v46(s0b)`K+{9`(XuO+-aJ!%9KjP)G)sjQ4>jzk{I(Cjl=*66J+w$}9*mBVQq7gkZiWo*-*M(k-6~D9p2*KwF_x0h1)O(=5fD$WXG$0|gbx'
    'K0GgcSrg*8gZ84Kc(^nGFbnwT^r87f@x`v0Yzu8Te_GIoR7`#PU?Bs&)RwRiC2Vk{RV;_;{DVl^s&RH{d7fV&+%VEJux=-Hw1k2KvJZ5uab7dM'
    'l(<1jz!j+;D0*3`YC=+SJhUSO1wr39$mFF~Z9(I8dN3K95aq2X`a5rWFiOb7q&Ag!HXzX|60{8@gUJlY^DYb@l9I+~I7Nb*jJTkw&@$h~l7RoT'
    'Oxq9X7#u%;ptK$vFw#%@8#MHQ8RN@gGh=j%ZQpi56qVy_egg?fAuwGCztHZW?QBgn^HFcp*OD2_9=w2*43Ikt)BAn-2*Aw)r*%&Q2n7Txoy3Lc'
    '857=HhYE!WA1(e28n^&Yj9&&EKSw-$Ml2orLba5jOY(>d0m(%LtQ^a~dP1m-=oM4|ncebp2=~ITs?(#Sz=e?dD7q+C!8cFTX)Yad8AHczI}y!O'
    'kYqj;b&JZz+OEj6kJI_|QH+&~LP-$pQ-blAsf~JI6C!5KJz4$D+Y((|oha~GI3D@|P@e!JWzvuAUr|u1RFiO_lszhPC~xYQztgT!z}E5z>oB|)'
    '*RxPBqWnar%J9h-B!h6A##(b&1=3XsA)0T{<X**aW-^coz!@iZ=^0W0El&>_eba#borevsexY(+9sd!rz@tm>zO>_uX99pWyJ?O&10wK(w@V@1'
    'WtQpguQiKrZy0BK^1L&YcoOU}7!1ei0ZDM2w_tt)#)>W~oJ1NO!wPe}f4Ds$>IsbjjFI+3d61cf>q8rlJgE@N(+Nn{!}K0OV`RddfGT+0KwuKk'
    '8-{I`tt73pM-7dl9>RX$oi8g=V4!s#w}-KK<PnnskyTwQ6o}CPqaSPf>1GP64%CeZ%^V)(^@hjH`Bw;tFw+(G1#7rmXz0O6K`<Hy43LyWT#s52'
    'TxJXWpdJ*W!Ag_N+qY;pfHA~uuu|yALJL446|hz7hvyAM0!fW5O@p?>paQ~m3%#<jGkPBGM$_p7no6$%&&z97C}>*MOOO{Wmw7Xmk%{OnL~@BB'
    '$yk^G443Y>f%+Plir8mItulvp5b9l>!#5DWV)th|qwWG6wV5A7&Jbw=kPg($mB;GI+{-=r%OGkG!t3;PM>!V{;Z2G@KzraWkTNnU%0{iBC`UMG'
    '96snY`EJkpn20)B;SvY!sVd9Trwq;WygjhUdtVbKZ-O?6%y22^#}DDn>AL+k$Q(%a#j|G*%O!WAkbsvV9AYjFf{;PULjHQ1w7-*ONzj2#)`O*J'
    'Llxx*5lci%c(IFu(E0sC3(WnxiO3+mO^#ME+jEe!6IinSJ2XP7rww`SZiyepFrm`cAdkBEgX@}>1Ws15Cx-$LLGq_qdd+tM?%(;}ofbGMXUNwe'
    'wz%mF;jH!5UB=@0p?JtJ2W<fuH5X2WXaUg#sJ6U-=|O?W0Z?pLl8i7<vuLyp+djO0NN;v4X@fmR)hNhCRF5mvG7sfm1|-qeC97e=uzR*p%c*_g'
    '<mo^(&tEB!gP=5n-l~!M!U)5x(!Y9R;Qh`FC%{FiAK)zlq1OiNh7)1N7D!WQK<s}g!Ifl0HiAlvgJlm@xAG|JRg4dvAfaK4AZ)GkotJSPZ{3gI'
    'N#YcQ3(AsUI^h*tx@h|T(GTn<^TYsx0a=52%MK0l?BMjtFAW3MVPJL_6nsxYvWCZI9;@d<rFuShDBzi#&3)5LO%_n6wmZ7{@U)yR_-Q~P@%Uu*'
    'u0|Oy4iXJrtdRek41XL(Wzc{8VOYdX<6t{ad($5LH+dZJFLZr?=+IgM3B?2lS{)l?x>jfG4G2)w7+(w|^l3ISU3kN&I_3?7g&y$m)62Az%`@$o'
    'tm9pF^>-*aBw@x3Fm9w8bTE<J1v`hk=oSE^i*nQSV)`J_J?I`bOlaGI7Sz-CY&A|eK@(CCPPfxCH1(^!2fM#hhbq-}&~3)@H-nI5V{jD^n>IyF'
    ')(3|*jkb1m>|&%IgM?^`&|S<S`dVSoQY#AvEjldQK}RPfe<L3L4l(EHH#5T&=Q5rE1Ok#q<bg~TW0;9$AJ9asj1L$-V)Ro@jYgf*$TQF0F$kjY'
    'O{&9Ugjhg%PTqd&#|wB#UwVcgyju3OUw|w-EqPC1;STuzXlGCN+<<s`HWFhe$Yin^vTi@q@9!SJwkXQ{&?%$<13afW?>%lHq%VOg;qO3JBN0oR'
    'WP6Gmr<EC72yjiexEkecZBL9EBm~gbNCXuyBUT`!22<eq%N%<U7sG`o+HW2?_=dv%8;H(mN{Z;sm^^P=^ha$f6^PN<l&6^<qtv^j{6tVS;ltYZ'
    'GedD&dcg{OXr@x+pCQizF`ZrxA^cm@@876&zC5J626uu68kQE>zHOKnsQ?zBTO6M8Zj9ie{7p3$`WrxJ%8dR9aOGkk9`LJcTbddj-9_p(&h^6r'
    'P|lH4=P$|ZtP532k_kmx05pr0C-RF^-q7@?=m|3oSoTDnkIKN0=WE@dBR#chSv8fcKZcs5{@8&&0iZ>0fJ_DLoqjF!j5{H0V@uGy7@ipjrh`F;'
    'Br^(Bpy3oN)3`=QH3R{t#v60B90$%I2n_$?O!mY21@-_jGX+N|UN>OxKrFkR5Op}Hl^Zk{;_c{j+J!?EgY1XkAxUZ}5%qPCH%0)D0%m|!saq1h'
    '03SbI{Y!CvyF*7Yg_=aOrzHKxT|pyQNW9QC?3N`UM6)(8qHSo*f!p!E+n#F3ma#58HUDP*KEmmALdvoABj{zqmX4ZI4=4!E9bgRtfSScqW_fyR'
    'N%P9Bo?xcdcGZ8PK)pis31%vY?u;7-t6Ue*QP111Zl@p1*D!v}d|}KYA-g^qo)D5RJar&;NPlW4fq2xdEX<EN60U~={D$x3+7>)6iVMi(7lh-7'
    'Cl|m|-=0*`j^$(A0-ViREt#zJAro?7Es)A0qe8Zyb_oU<RvrvH)M(Khb4_dqXNiYLlzf>sCm*qlU7#gy?L6iJ&%^eI^kuasIC>+bt38I<(4WP`'
    'Fe1~#r^w++-I#g`rThYxu44dcclJ=q*d!Hb0dNc1)5Txn89Fe<G_ZBPfb|j_Z_Flh$ZgmO0F>L47NeAbsYjR6^Q;+pKIq7HJOo8LPX{y#oGC?~'
    'N<sUt#2j8E1<;e89#eJh{)l)7>LCPodRA){Fl`Zm<pihGv}mmlM|fDeDjDv{<R|oCkmf*PE{2;AD!pkN@r`^0rl>GSr4T#MeMDX_z4?XID1}uw'
    'SC#Q5ciJ;GPC7-FT~<^s`=cOhkRF+*2-S@uihYJX1|8ykdwLUWYeE!AsSJ#(qnhOLTEBY4cN{;EE3V@2ZgqL|a@ppVH=)~s@q_dMql}pe@I;N%'
    'M#v5tO6spMpB>c!&uIj5SS-y}TnD#8^!P_Xt^cOWJ18;eA0R_9zopZD!CFs?zirk>2EJDFZK#0^C;SaK8@R?|2)Kt{gR*oZHpXo*RSXsz2<KMM'
    '_3e3w_4Svf>5z*f_@Y083Jltd;6w;MJWL)U;fg{Y+S3T|u(d;}A64>pyL;qZz)T(Deu8xh0r_9Eqy8H&kG3mgnt%u*ZU<BfAp{t25M(^N$UX?Q'
    'V`HZVjo=>wO%(UBedzj9adKfSCV<K;^ea09>_`_eiZ}l=K=t2v`R+tb7H*Fm1!5qCH*r9r+(1e|>}fog<!dQ;UN%S>=!-{Dzhoa11Kllw)6#4D'
    'UH9NY#@Z=FY4iL5K7eQ$1CL|ZFjtmzl6F5x_SuZSY{jtL#o%S+hap!_5a9!&25o;z&7`c1dto?rF%k~p471kI#ae$;hU>op^FUKKY&T*a4toez'
    'n{Az#8X>#M^5}=40oNm&ynt3`M&AJ-3PS1+nNK2!LtYF+X$kk`vBRF`wa@kQ3;SB3Y-V78^Q9n6<O!v2kn8Id7cewTp+Z=^1XBi`7}$r}=*=Xk'
    'Y?i_8*#+o^rxlTB-7Xz1YoH%D5H%+cw4c-SGNE@~yP1VW*W6=JujaWKwS*1~qi&mpssMyQxdo{hV{%^Fzo<*nfWk|7YOROK{)xWTf8*teaj4SL'
    'r!5R7^0dCeSZ$b}V1OAjm<dHUq#UIZ%VUzZYveGJNHnbUuxURcE{Hi)_<?{D_K2c}1GQuM5hF(z2t)lL&>R#AO+ljQ4BAu9<)W2Ng^5n>Ky~b*'
    '#e(3aMIe`bCKWoW_A)Zshag3M0QlT=!ac7|zUx>$bf@{&bXj2tPuEQg0~;Y(bGALF>_SnAULTD!MtS&L;a3_(P=vs<@yMx|&Q0Q*A&PhycCtgL'
    'sKPBO#K%p%#HU?Tz)TPVhIpL3_Vj1m0L-`rG9;>L9PGMjws#{Gfx5v2GAUdqm<k&6Uy6kZ4cuc^8Hh93tmj*Ji62wMNT??wuRUob-DF-c%qVtW'
    '5ZI6yrT>zx^Lxd>BRm8>VuazmzG6Zof3VcU{ctb==vinlAXIQ{fr!4L@l*3`(6*hy$ao`Yh`^3{Au_;=hA<E^&NdyuAG%Qhai+Pp)7?H)x0$&H'
    'Be_*igD+^+0XP9DzK#R%Vu3Mf)dNF4lBwE{Z+{Txz<9ndRfz7X_GpAdu2}FS=seOYXb^pkv$ixSY;We6Sqz$ivB|xEpvA}X(QjckJyS_2V9ykT'
    'dfl*sx(_=KU<GjnJTwrnX<4R0iz;TP0&KvIhxDPYNqKm5GZjIy>MSAY4s#%t!~MKSU<+BoN^3tG#rcD+m0PVALfp)jznm2zi-x!WqoJ7%Obv3q'
    '27SNofG`~)Zz?|)uxn^KS*F*iQQGGnys+=Xx>EqC((mA)8|8Nx_;i66^L;8!`(`x@%pFRN7CXZutrtRLP_n}(Fehu~fWPqmAtoU_J&ph%B5-#K'
    'Ja7+i(hBOVhv?Yb@U<Bq#ykRu^rQty)DX7r;c>#4Tsy!7RB#WEnNPq5AWcl>d4PRP<ybw!&mf>ts2CRTw3?^U3)5OzRl5%@(i=_@<_&Oz@?*rg'
    '0Vx^(;8=&UKHfG!rPe~C<9%E7AE;-l$J_s<ph2fa181{M*ftmusGM#ufv?gHAl_yIKQn=40cnw<8Y9;W!`8`0^2qItTpVL-@sp@^H@Af7e(dpP'
    'eSRyx6pAhNPf-=M6L)AqXoA@_jsnc_xb4N@Y|2SxXls_-&9IY<1I!TbHcI=jeJY}#)U}LZtyDF7uE+Ahzd<&ITZA@<01Ty*iC-Be-A|sLsBU>E'
    'YPQNg#;<!01MH!(U%PL(h)T)~Nxr*py-+}88W=2sWBD4v7hIzTgos(uo_oMFYi7QpF|CJZg6d8G(LEyzc^0EeJokQh)P?0u(OaCB$g$=d*)hF$'
    '<draH<ybvpPlRj<R`Be(n(TQ8wHl2|<Pa{ZS_Le$sfM}+ogPj(IAF#wy^IfieNa~gCQl1NF+>vCpc(&qEI*^$#awVCF_El6>jw_06;l}+2oUo^'
    '2OBgzh3n-!69WmV5=bq;g4Ycs%Pmg2z}v&aLEbtR15G&BJl0>YZX1Dx30vCj>+--InIp<z%)(%Gg;ejW{}gZawhiNOpN7uU1=9EU?Xg@0+Ra0b'
    'saFr+Z!HE`m@f;e54gbP^3Cl{t<PPp@(=#lqjAtAN|u|^14Az^v><83U;!wz(I{i=I!MI#J9y2PXBtl7Y#59N9HPXm3Yg=}*1Nzof>EGr$)EuT'
    'v5ji@@Z?n1v(gqqO;pBYCI@`;&DaI9BZ!ORSUYz5V8#%FO(<-mFK9_f1@&-zp=2z^-6Jp106|9thy*Lh>g`h)G8Y(AMoe#r7B*5u1F$U(^BE>r'
    '95)bLYa)Hl3%%OStIqBldfJ!u1X&-z7OW?{7{>R<2y!wEbaRm;@eLSMAANLqS(hDMp_s&N=X|aoiDoxtmg!S~fwD(d@=2I^v8Wnc&<(-LFN>DD'
    '7RO|WX89y;eWk#9peD$ml7?>~(M%pY-Yv*VU;40g5o#j~aX0I-0V?Q*Lr`HvpOipSXS;zrzK5F^2xl5rbz2StWYzX?Q|<uF54>pzWzbC_q_}gs'
    'JOq_9aNI$h_d$UQA{_G%Ew~}8L3)U?crmx`BBx6J1dsu<Qwt9$n#_?z_A;<-!zYfm&GzgOrY>6-17+N_j^!iJM28mE=ps35Xy_ym8BDn%!J^U!'
    'oPHrLps<;_E&8*F{1KZ&)8;coOr5jSVeAXh02F?Ct%>#^bku+C_4;qhJoB8;gtSRnQ9~#ms=@?A?IXtM5NQt&)AdxL9?HGl0&z$oR4egEuE8zf'
    'kDwu7lZaXwiHI#h@awpPNHuw7h}<VASc>(~_xA`ZIs;TmDOe%7oEtcKl+P%2fyo$FUb33BzyI{eaYnsP6U<Di@yyUNOiVkLe^~$vF}edhV=9Lg'
    'sXY^#S{GAqOd`sr39wJte~ro)GhYzZ4p*C9Bbn%Kk~$K7uc$p)Dmp>+Ic^{T4#V6?{H9yVmuE@HvI0_~$xBy(_IHT669EvzDxhJLZeWJFhq66='
    '`DSSZ#NsXlmlVZik2hr4>#=xj{_c%r$$U>JC(TcMu;oc6SieREHyas<c-h9R6zftprl3XTA%UPquBTIYGO2|w%<SIBi9w9V>f!3B%<J#!$BbZi'
    'kte%J!92e1X&<dw%9K+GWGK55Lx+d5B%sR3Crw4B^G`F$)&Q0GL8eiSl9_MD=XbEYjve5a!ekz+Dhwbyw8R)(*WlR^USsNsXolT791f<sph?d_'
    'xme%iG*rHciIlA}h;8Jc?k?nJkJV$m7cdT@XNEZV?;zq`L69g|Sp-n6S=JhJLcB;np|UrORL1Z}m0=h14q_bU<Sh?DTA<=&z6$f&QahH98VgE8'
    'aEX~G<}Nv+CXFITg3@QG5fU794Jk-j6UbyGWS96inb~BPwN7kwLlO`?CSV{EG3{j1N65$eVfhQ)3Xg6xL`=aE5gmNWX;3S177m)?5g1n}Q*@Y;'
    ';t6*-^v450mb7muL@`Xu1UI@GO7YzR1@ylAvHpyKwv_n6uTru`G2GI==wf6r8wePH4!4bmGMUj!BF^=&YOvNWrw0)cy|fS{Hc8{ku5x>*xLbVf'
    '+yDV<a|kmnkmT`>0P8HtOj*)fnQ@=Mr63$3dIyfl+-B7JISxSzPQ~+}k$9w~;b;*29p|Q7ZTUzte@DxM)3LM<lyK%%UJMD!rZ&T0)RwaLC#WS4'
    'wqU|nb*zh|AxyA_ylC7(rmV*IOq`X=ViYLUO*D-jpWqewfvsY?S?A=sG;LBVBO5aR7D5R~euh9z&&~1p8@F8;Lfm8KkF~A4L#+{LFL_8hAq^IP'
    '<|Q4c|5aq9eo6NcSf8gB!9g7i&j2aF7C<f$)mtV+BA1gO%jr28DGkH`ENaAlXv(?&9A=3mGbXTRrtOe695)b624k_hLB|C845x)cq~|kEh56;B'
    '8&Mw0kGe<7(%aqcQhx!I4m>dsbVsSXX#1E_$GjwvVf%dgz_ijiW?n>bGaX(qND8KtTKY;+#RX>-;Y+$P9(Li`15lS*AmFU21OY0$H4XOGA|I62'
    '5nO<LMWtt)HxTE47y!gf2Sk+fh4ZGSf#?YA4Xfz^;ni*+q;DQRFyRe#QVFx$?R;7w`1fRl{6>rnEz)c>mt4}{K==ALP@We44K;vqEr1arcikS~'
    '1&vCn;bjUBIuVgPyPPTC!f-WZW(%}YjP#~OhWV?8J^~Oeo_3f#9+=Scef?M5hqD1$MD4PVh~O%tOcT+f^&>9uq-aosxrb8ijLwq_3*xXj#r$$w'
    'A|;0O^3DU+wraq4D5KMsIBp@wO!G`wnXJ-EvkrMOy%;zaR*YkUIihwqEfpk#w~=IgCk07>SSe#T_#bFRfJCD}PMdoZvVt)Ocsu?kThza)@}v&n'
    'D<~CFu-Bi4zS7M@*wCCT`hh72^G`<3gjHtT!#DuhJAjvjMwqRH|6y^zDx~KKSSZ-_!vno=t!OMWH56tD1w@1s5^{sgt7do_-3s2F`JPmzYV$Wh'
    '4+({9>SNBoGr==>e6|uk(>I0E59D_m?-a$3TL^>?x*R54ZTM@zcBg?3@=Mh{3IzceWTo-wbTGET=9am$B!??%XjYdeNwV5HRo#7o&ZK5W;Rthh'
    'nxKgPNDdHEgPXLiHn<_<G!W9=^D!4qO|5U6gp9EgBX%LMVH9%Vodn#Jp$4|pD1pQjY|pwhAC3Y<uZItMp<7L>n?w`H;A_i)6ol1)xH2stZ9>bD'
    'GT1vbfs8S{v!@nU)cmIH;NlH*aP>JRz(6t~vtkYe8}%>)(dZu|<_FId+M=ds06GL2nL(6k4}xZ3UL>tW6Y5L^1J(*183A`Y0f;3)Dzj(98F8E~'
    'k-!<s3`BijzrH$+C~Y^!%V4W4-%=2HK`$tK{4ZQ<YIuY*(7QpR6^qd_0-^CQO*ciS(TPK1o_7xIA1d9<$Roz+$Lo(uE9|J-?Rp#3;aT^$Ax@M`'
    '5z=cOKZWcm($2|E6}VhP;UKJm!SmiQkv9wq>homj$aTO!!6UkE_O^a|@v4$%+EQ&m^N!S>fDn*phMQ(h0qtBOGxUb=w8RNud8o)BPXZst7SBUY'
    '`TCuSS3ZISH|RP*?MSP?hXKFC<z;4k;-EvJ?IiD+L(37NTL=Uf9be#r+cQOJ2b0#*Wu?vUri9l|bSZ(h1A)={0QZe?LQxu>>SOf~`Qac>r20Vx'
    'r<X!@y-jq%D9QXS^uJ)}ffV$)tN{cx>ycqMMwuI-m5`f85-&sZkESr=xRLsCY4p7a^cpb_Z99>Idvs29HVf3U7NdH3Lcaun_o#Y+f#F4w-NQ#M'
    'G)oGD4FV-bdn@tTt*#dGabPrzIVgX6yY+9%Jk!Z<;4UZ*SCldpc!A;5A!ncyC1*2B5%xtr(Fs2aTDSF2K)@O8gGT!eY!GQ`B(buU^a0Iz7ScYS'
    'KjKiZi8n1-iYUz?CYhMN22B#0`{ZKD)mtY%%1+V$paDUPu-l1d<4wQ}-GCC2;6h`dTO;`OsYnoy^+WU(M>qo`P+!9JezPd-7=p&kDGKh^)EXWG'
    'g`=>n3L{6*x`;#+JaDq6)r?ofU*1Tyg1nr^;^j45k9i&mt;7k-1HHG2tT5xFk&!Q6zS}Gs#yJY}Whl!r@YBO*qW(e!&|MOb^XM@wRVg@lsKJb~'
    '^X1pjn}xKFWo3pf<GnIELDlp;uqB|(>0D8ND{!bMER1eZ9zaZ{j#>bp;S`9+B40)cSJ67{2XkzZ7dVoT-!b#(L&>;S2tbgTJQNZM8cGlV-blHU'
    '2~VC_SPh0J58RZgI@&JGCF9#O>!8CwVQQU+>`trlGLeX}o9nOuA#VTz%qgz5<a>z`;^x{??M5`(ST_*t#un86n#9Vkbtps^dBlWK3jYiuB{8G8'
    'i#??$;PHfJkgf9_tPq+)JDGL~bx#V+$cioCR~;k?6is9kAY6k%VCW*1kj#u*pl$^fniYg}#uqRQY{FS`dB7EQgl8MT)qQS)5IBGtL!_!O)Hv3k'
    ';xUZ{U&4qrGD?@J0iiH;j#uSctv1;3koc9%Y|oz4#OOPVS0@`P9O!V%o3EGBaSndRE-WT)ET#+rJJjAGA3z}p=;#LUIxD34V;fLmVDQ{vtj$26'
    'IY2K~L7S9n3`#aZ1wk6uOzV5kvAqNax}Kj(WMVuUA})M8TAT}BJX65wg()xr8UyWRd4dm7{`MG3kF|<QJpBqLEHI(ggdxP_nR%9(&^6cLi9=mr'
    'EDw4Erb`Q|8Yt(Xwgc@-j45v*dl+8#m|H~1nZki_*$DH4{f%^Z!pEXq!RWyd@MKOpUScr;=-h=NFW{6E;c(3705(sj8aWf5qtQgT+>!w|5Byzb'
    'vlB5ZkD}>8jE-()gx-`tcv{}&3z~thE|e2!TW2T`5(BirZqOB;+Qas;bbP5s-DXq9fVCOVE7MdwGnj`%N6m;A__M1-@6Y4HsZf+q;{{U*9)i&?'
    '6zR`R=!2*iQ><z^JX;prLt#WT2?0J9$^#0tp2x`4rfr4^<y9J8=vo4f4D;Zjw0Hz2#3={67*1%WEdySVqWHUk{^HqFJV8}~7uwO!a6HHaUtAMp'
    'LvY3@4Q1rdp@b?R^mn2k2AyWkNXc3^&I0m^j&OwNlttm*!j6{RJ=S+T2vh7%F)5%2WtV0kz&i$yc;tLv#LRS5XwxRo`lBWbKNzWx_1Xf^7O64<'
    'MW6gB3fatoK@FRp;5^p%A*902AWy4iLM;7Ze+}q@6~Oa??8!(>c~ve#MTPksu>Y8@2OIWP6_Nkc=0$(p)RPeVSxD%RZL@Wb<$VYtmAD3rve2@M'
    'b~6wdjkn4`Eru3Tlq4!5)hWhw!$JZy@c)x_hgodv%yO>hlHarzL4qJK|A_@pLEaX=lXY{G>|>jhL;{WOs<#q#7%_kr4aqtMt!GwywXN`YDg-W~'
    '?eZQ!C9u*{j<?Y09?zm~qwa^8u7oXX0>S5}+Z7hg9SifN6NMj9*7q-K<VuCkoM0ejuq*A-ET8zSZnt13a7-HjYShaf0Y89F2?3qB0u(`ctNByx'
    'V;86oRSCl89sPxWz5l2EVdFZ9Xc9XQZ*E{rUq&fXes3Y=;H#+rJdw;GU0^*Jq(zOWmcKj$^WUSsH*&HtdAu@^b|@nE)1O@e3|$tHqajsm;VWJ8'
    'ryM2DXL|}$5GvD)PRzB?da)L~i5Zi6iZ}cwddysk#aia?YK;<GusrIDtEW;X=tU35<Fwn~{A+VJQaB6PLuT9V!Ac-$1@`}qX(jDRnoyvv`2~UY'
    'B>9svWp3LJFoL0O^Yb)kQO;N@S9fSGU33uk=5!5>73V9gPiSSmx0NUj$`{5kxz~4q&<OLeF@o^k)LZs9Ws(_V_rWZ1K~&fr$&0@A0CN2&jLqk}'
    'rY!wC&m1G_HMZw4?MZtPCK4IdoAL5|)W}w2m&`;f82S<j_gqS}|2zZW<!arzdQ!KSrNMDr3}{9<<xPl~kmY)Q8m+ed3$Pm{cWv(CU0foBRvf+F'
    's55$da*CEL0Db-;dCxLsHK5%^*x~rQzK`J>5V_d$i=8xYLb#=RI=H3pwi>4{oX?@`^<-7a%R1WqEu)~<%)A9*&5yD_0fk)_4+<6R{i-8$ri^z|'
    'EL2$`0H{GwG*go6+`fWz2wy@3b5_>IXQ|nZ=zI?Z0#}IEVL_M+JHl7r?ov-<4sw=dY?dPut0JokR}trP%;-;C$-Vv0_3vE2MnFXLq~0)_m3-=y'
    'n3IWdHBTqUBV9NKl1{q&kxm<JeqYakXc)AWnn=fST<LRF26^g;R<?IACE=@iDaTWIUFaPz-Y_YLB*Qyzff0k~cikr}C%!C#=X$0afY_5&z@Y>g'
    '`Ah)lnR9vEjJQ`l8SvW;<R$dG2XmtyC1-_K=JX9`0TTX8d@8n!1TwGZkNEZpfw?3ynha9-@JnQ)W5!bDh*6?VdG3EUn05KQgmRWXc|{UGKmSgu'
    'flj(e(sqSJZi~{NCa0E;ZtM4hB$q13*!BA(KAyqBGlvCF=(9CP2e|Y72C^GDwSVSEi@W)b4+!=KON(&#Vd;XMk<Ao&9ok09pN=q{LxL2Qx*Ggm'
    'icd~k09$_0D^_V$L5)0lyN9$0xqg(~8V}@A^C9ep3q4NiD==u^LbTBLD*m6kw9ulGo~WVcZdbqG0v|^SY0bKZPsA`I?&VGYe!GKgM-KrYMjZ)b'
    'W<`7^&sY*}5UC^rB;{)0@hCkH@r0KkOe@Z)>n(g)WY`u7^b@Us+^dT~GyeQUpx;RV&-Gi3^Yan(I6mnsqr7sOwaomuk!*R{GfhE9KoRE4cT#A%'
    'mR3m+UAZXl`Rp<vrkVR9>6<iSg0$@h);c6uD=m!q&n552tS-?P^-c&37C0!0-UZZhq|Tgnx}lnR@XG5K=&GQEfl<`VUsLl5T}5zk-$X)fm4&qR'
    'b4fWh{IeC;k(4oeju^8M<LOAu7BIDTC8ivW;{1oXDX`9$NUD*N`2TsiURH(?urH4-m)tw{8(6zx{CF8O5rxh#fJ_eMcI4>wBA+SCEe3PV>1#3v'
    'kEQ|IIcjEQ_pZ7SeUJ8Zz-?JngOoN@C}dLSwcWtGRziJhuT9ab4qrxhHR(ED;Su<t#2M2Qa-)nzXen6;9l}EVUBAJ&RtRANwN(k(Mq5nX0>iFf'
    'rBeG=$JU9Df8wOa$*S@=4Qq%RZvbBT9aN8yGYl&|!Kpy@fF*0zy#htx#y%t>9+xZKYPOSr+*7>2zm3xNEsvX2ahpxm4JrQZjH7*A8r3HZMBNUZ'
    'OiD~EJvp~?N%Jf8wx(0=<zrYRuf-UzX!@Q;OThL{uD{n`RpE;-K;?s0y$mKN7><Qdyxy4|aUM(}NEy5vIsYfXp$5e%Rj_^cr0`Fj7=?2rEbM_;'
    'Hbr)txc58Ap6rhTOs;G^TasKn6B}+tRuNx;;?XRZS{~6ZoRg^8w1Ins@L7%^l!qblNl1MtR1I2zH+XNZ?O7~qSEh64I_AI=#TuZm6CIvGiMu?A'
    '={u)*hu`~P!!=LmS_3{<zW@*?r%Fq<)Is_y`Of5tf2(X4&r(2!{fQ4s*$nyzipi<xL0;iJ_=k1~vEo-mg@2O!qGO0-17W$2CXKHtd<r_mxI9bC'
    'UCtHZ{ym|#e|2$N{p=uP+F`Qt@h?$~2><xjgf<OpHYM|sC@!Uc5Vdz8M%qKOTz}p8+&qe;F)FHCzvbJo->#n*peBv1g+l!VV51{7mqis?i~e0r'
    'q$sr#l|Y*6f_3&4i9R^5uj@Z^OpU4*7XC27dXtNJg|D#v`qw%ga<ovIvj-R&r;z2pWGI$eW+_wx8PDoCg<Qfb*bL%L>ognFp_zNtC&z<M>h(Pr'
    '89HJrAcAeX;liNucVK5~w*>Qi>GMdDaAuo3L_nBs>a^?V=Vc-;F|JuJ3*Un;@@o%7;v5lbNST;r8_J1$ZK+qcJ4lo04P!$N66gg>`yC_h@WR+0'
    'Z_T9-Q#N}GI$R~!>=`qgMXYqb9ucQ&;iZ`>Q0>in_)3E8{oA8!-#7q6vWObz1_H4%msfPQ*FFelP~8x710Im2L}1<gW-;%b_9Cw+i$XfTFTpmv'
    '5FD*4$DpxwQDogdV_Sj05LEHi4lOUOAN>^)g@Qjqbe?VpV~snkmj(~_x=jmpOMqDkqBFX=`J=#NC85T^;1+wC!LHnhtM9-6`|XwI8<^|h?F@1N'
    'J%QaCmy4WBGAh3A@8|!cul9TS^7W=+DOEP0<bzQfvwhyMCt(EtyMg!Hqr<NLBnvp}6jwvoQ~G*^CLEBfY6<C&;p3}3)B<uQfPozsqgV~~aUR=%'
    'D~(=_b?GsI#^G$Xi+?HfT;2>i<*UO^ROh^fG&*{6VQ)>dCMyhIY5o~)0Za<6cQBt^{7q9liAJV*!o<0DU$Z+C<n7{%K#x}?;`U0$UF4z2jgtA2'
    'H6Ik$)fOue)BMaW#;an912ni+DtXQM`*uE&*xL`nGQVDe_U`N5F5eaaf7~Lcl;JngLCNEz2`sPgOfuxD{3uCIRte-dobW0tE}n`j^W8hR!m3FW'
    'QZAGcA-HI=hamG;E&ex_-IqCQ{GL7~#jJAT6C-^UB$^bg3MOh~R6@J1W*_=eM7@T%e?w6nNAbfX@+`a&gtin4Hn~~ZZNL6^Qu{cMI{nV2=?n<+'
    'uL*iYggN&b#(@%U3q5X&UPVXbVn1eqf791Z9+vr)W%&Z9?~yq;3&BL*zboJS*O(n{kiVM5nUQF2GJMOagzvfg*0|8Ks3oPSB<Y4^4M<R1EVg9n'
    'Gvd*Fk9RcopOxZt`wi;;;g>5cNWoC}yPqu{^jTKfXomj}kV?+F!H50R&#6xq8j#sLRr7)pkVh3FKaiM*rpb#KgdUaEW{P|Nnq|5J^BMa)cNsbn'
    '&M>Vkq4B&p_%SNr-IDBah`;Q{V0QU812g%Pb1xhT(C!`v;7G|Lj;^h2V!`*XP3FPuh$x$%ylQ?@WfM1ZB|UgPrUC$k(Pl?FDl&mI)-66Ol2zdk'
    '>7I5*9ATdzJS~NRh-?>MhHRokLnt_*kg&*s67^~ej^5i@R<f+IiS>I~6i{yFL08Oi8;eGACi-{&Yi;cc+R@k;)FlReZM*)`34?t;IBYFSUsIuX'
    'QO2H=L~I1c5q~uKn}@tc%}h!EKw<Qie`yF_5|AAv-w2fO#u7ArzIhw=-+g@Dh)%zWl=drz@)atbedft;C@EW;yC;0JWIfduigrZPz4rSXeld6{'
    'URhLb-~pJr(%~^Ry|>SQech;Fr$R2gMG;ojoa;2&tykediD+2Fy-?HY8Xak}tGGt|hgo(7b*dER_TE&1cZT+=Du?TLfAW`75^?vl6k(9B3MU&?'
    '(he~YEwmZT&8;b^pW3N+Y`n<UXcjFiYxujA6oBNK2gM$O)~5>h{`7l${J(zeC8=>X^R%a}?PF-TYA?O466wW!9BnU5r#OIOeeFrIjh!(+0cG-='
    '&=!rEW!#R7OJXaM%eM6_NA_GIZ6d|*+Y@6qtG$3@y-nf{2`2#Z3h|nBxjQjX7E5B|7G3jN`B*SL5dy&iGBzu3IdihoZ|m;Yj(sIEyeDa%xY6S$'
    'D?v&elnmmg3FIdx5ubQIrZ3OpDx?78>cWv=IeKyBBxuP1GKiLNH1HPhPvDX#3E!)Fv<&%yEzD2)kSTlG!CZ0!$v%6MLS{G@7~x3A9tAbA$N2$X'
    'N4O_C?mG^nC-yeq9RI_!ZN1<8CC&gzuVk?IV)#Q3jv&Kp0#D_+TV1>*_ztTT9W!HB7+xzCHI^DUFl~lVDdJK)o!*&wueM!(;pflfeyMrq8W6qR'
    '3Rh;dbDFlz3TC)JVIh9CoPp-FgvP22m%%8>Bs8~Q`%VNfRj;xVVgGtW`(Hoy2*rP%xGFY)niE-mR+e+a0{F578i&S9N&foYc}9`uRmwtgR`kSO'
    'BFqOPdxOKFXMQ9h00SA*ZI}Q0vE4HrPxna=xz5~>D#@C-nS_&kTzS65o|F))J{ErXRrtQJ_t86uJUaRGI&m1wNEJA9G_Tv$7Yw2!sLcg1)WHm+'
    '^s<d{FqRT(;L{7KHC9b~YUu2~;U~%(Cl?R;fhP>dw`U&4j*o_>;{}AN@ZN5qe&ZlfEs)3F5$`?BhT#ljgejWU2g=x)EToUx6Gll_>A){@tgP@M'
    't%;oRnMsmdO-ULT!DTP|wdwZ1@<5RRv%;UxFF%Pj<8+)5AE5tt<`<|};O{k>Om1rhE=kChq}NsUdiRh~rMi{1LSnce#<AT#`}e={K;<*dFUBJ~'
    '0#d#o87K2nLmXPAd@>%dpF8d~jJj`w>w}J!pe^9ddllZ`;i~9)$r6<h)X@rGX_U9yEsP}-rT)|6z|m;gwXRRxn>k{VSHw4A9xR+td6BU(Bfpag'
    'S=D?zR}o;nd5s6b(=uYWxX)6`e0vtD`)}}#fGP|VX)~TVz20!7h63|E^60BYn&AA{SBA?x*wo}&1ZiW{!s!bCA_2FR=%2J8I;Ob-0XxlC?8dKp'
    'w>wx%kYb4@Q?Dzlx$;+(A1+wDxt2gW{Kpgkrtg)n_er#o<9vKISpo$+av2FOYB?m)9)Z*$&6Qs$SpViA#bC?G8Sc5SSG@GVa7s!P1>P+}i3!^('
    'URLCE@}u|+Mu30o20R51=R?m>AG<B!NIeB}iMFkRWgS1U()n5K>B%qTS&{xAcLw0~&JD1%>qCRCQ7<U)K*1J4IwVy|Jf6*#7b;Y`Gd!a!(AHTR'
    '_3be%>i{|nH9!T{Fb<^1-*EWJu9)$&5S%niEcNIWNnjRCU8EsaCbIAGFY?vX3zPorEY<)9=D#g#X={G>uT&C(`vX41lCwhBMd_-V=?dAi?qt-E'
    'xg>w(S&VbQGw`+R{M!(awjoXiCK*78rPi)E|4^`gz~<Wx^zRMKe~S2QAWvE3rN(sOx1B91DGbE2pGX!5RoFa<l$;tzuyi<Z`32@qVUkGd)l-PV'
    '+1dX~Xl~bLm+0T-A6Y6xMPFJJ?Z79Qnp)Y0gkFI&d?#|s7INsHg!e8D5$s%etW8$NTy)Gv*ah^~=C^5hs;f0N$87(0{_d4tKDV8cG^ZGCAO}TU'
    'ScVYRq<g$E|HvIQ5jVV<6|o9G1#7$sZqGngL+T2^Z1kkHE8bt-_3al}?LWs@hULNx(9*<@4E0v!+mEjN0uDcd8sk%9NX1;djbag<2uNDcI2g+V'
    ';I4B&d?|yYYaEEtnD@Bd!qRCr!m2_K1Pq*iXcl>ig>phFX#(h!2tpAw22E*qeiBeN>)tn!6}qn_Y4;q6F`a%#n>rNpfDl33wiJJ*nxGBy3rg^K'
    'rSm3>&_HBkH<9%eNXv@aN`EEv^C&PWexzW$%RC$0fRfeVX+hdOJoCe|Q}_MqeaSS12oM4(G?H_{5u6RwHamy{Di~a5(^^9<DI*Z-A$2jwo^3hy'
    'U#3G11JwGG_V$O7q6J-iUfVLbl0fZlA4Z@m=`ho!Gtn0$-&9&XT`11>oEBp5Jg`D_+PnuQ5BH*@nMVhm5bb?F43JES+DU`@z5XhN5l~3KOdaEO'
    'yEPIWOl#JcR0BxGLo#|CBh<tnffamSk51QeE*q4zgec$9G+_h6+P(}y`i1|lKE6Rr_Ie8(%P4{;Jjh&by_%{q7uJIsY5zyD3<7@Dgx*ugr%^PP'
    'aXM?Ik(yaeJuJ$VcS{Y~&i;E47~cjgvgilQivUy@hwLssn+tv=LO%-p%B5`%ToOcQ&LY2lJ~nd)dcUJX-9K(hW(B2m_|8{Mqxj$B7g;dAJy_>E'
    'Y$$BRxm}}PmC%u7tZQ$UbrL<XKs7-}j;olHF@1{6y`QC37rH>HG|%s6OkDPW*Ce~K!M1z<{7a>*=^Es@Az1<;N#GzrO_|SN;VXUONI0<Y!9479'
    '8-%>H6RiYjH-JToH_%Lm?kBMw)HuP9WJB5RAlqOtH%$?(C1eB$ORlr=P!VWu90>^%X&1zrh>jjoZjb!PbR$oIck@GV|B!Df+Rp?sFb~-l-(PaT'
    '_~tMfb7s6pf0CB35inI#xX+0}tEhu#-VcoPkq<V1u31X!SH53?rq~BP#2%eI$TiTMm=oC1cN0gBZy6|&5qA>=L0nfdo~Wvw(q5&Jk1r19U6jc)'
    't9?A1IrfzRTi5#QI0PHYI2~~<Snq{=r`do1q5;R(Fe8Z+oJ-j$k84dJpC@r6kBl;MkSfv7Kq6v<ND;zBS3JvLxSlnIr>maFq!&yCq4Z#1l3T*N'
    'O?I~3z`d0EARVZa8Ml<x4+MZONU-xqFWCauz*BQ^`HL?r0wF{ZB2brUvxJD1)n4J#9j(yh=hH3A7))?`1Xd~B;^{!pBl7C2d(U?@9~fitCoh4X'
    'b{*joxpDydA3;HLt7$noe3yYLh`a=MJRh!QU#52LA>@X8yxqgv1+DO9vCf=kNG}9#zPg*4wWcNJbP2!-uj5?-zw0?li=sWh=$0Eg#XYs6OMg5e'
    '9(8UBvikq|8P;nc#_M$&(pA0QMgO%vi(pI$jzyi1PIN~D$<=PS7TIpTiEI<!Z$N|LTtGs*zT@O4K^xxa>)L;U?1K77jNpn8@v={ruBPri^HyXJ'
    'pheYJnI9%42(mYZ7fRLA!XT7T);K=T9pr@@0-A)qb&9<D_6{zyjCt|LGV!FPJU{#D{)$4R9MFCD9Db;Hou<S-W2$E)z5)WzrAIoWag4Rt4EIQs'
    '55qwWi|tp0GQLHy*NlLdFj;3w6r>*N8^jPrYrc9bfI+>H(hAO<Yi-F$2(LYKUQ3NLbArQYkto}!*yn$682TMSkAFol&=qTx{I&Vvowyj4l(fP('
    '{C)zhQLY&u7ZM_{8?FtW@*54=4HP?o@S#k+%aI#^B^4q0^lb-R<=-7RAy-!aizP4!0-BKVSy|CDhU;-j@I|D)k8q)a=D};N33R7g<DliBk0z3G'
    'qeV%mQvDr3kM9JWv1cwV`l{wv%Y>8!F)M=SG2I|e0*a^5fzvUqE1p__HQroI!y(O!K^=+pOm2@!8rP1)(7SEHYXVN0f+-)nNB<JlpFET>YUf?+'
    '-%}VFzu0d*BPo|k6g7IYwye)T>%W_9P?vB%{b!IIXIdj&R^@j6zBCAP!RLL!-d#Cbev<j%oh><uJ}(u=QcJ@CUIeEonk==>@6LjYkSMVfHlz`d'
    '_*49ln7R_z!{6TSV5}%uJXBTt<)|@ln`>R&XazA?IO3oD$mg?&v%;FyYEl&Q-rSJhg>9PMez3ZxTVl!KK@uYK@9`H!eyt-I%2wxd3VZv&OGI29'
    'kg%k=2aQ#-_dt{+zeY;?Ngj)|;47&>gM^*eUn~T(@B!5y?A<)n)UscD#c*PGhrY|%qF_8pFVvn2QgoU-qMA3(Nyd{XzY-8{tlilIf(mqEGKezn'
    '6{tsPO!;8VQ!{QCUkSiu1(i6f#_H;|n`gBeOG5BObf`Ch)*7K3#`Q(K9eKmdj><~Hdo3cQ&6UXbjE(`tiB!_D<s5yxgOvcBP`v`jC~*{Wb$Km_'
    '3oUfy2lFR@%7w@>&d(yt;mgvqMsaC?ptKn%G)X-p)EFKzz<d@d{=)4B+V}gns3KO#g1xg(DWb$_D48;v70?AYJ{A-v@urE0PJqUpN?@)8+8t2^'
    '%;y2;QU<O41|)f|NMpPF_%Dn5al)dKBc46!Wy*@?0?bGz(FpJsFNXwwZjH(d+1&jOGH&?@qJF%>Cxl8A-k8#~@l^l7GNf>S6R@f8CEjD%#rbEE'
    'M++a`3ivyDG~-;soa_5PMy51Ox<c==66t;LtzzKM8XzGp0-0#QV5Q3cTz>6(FGV+ZG3{trG^_kD^N!P1`->A$N&{<DYA#B2QI`mFY#V78cp98f'
    '23mLN@3ILKPu)qp({SGI;QHo20~g~n#mTBpvlFNxo`@E*^8voc5@RkJl|llTMX;!*qJKcTBZzYxUt!u$IG9@6Xkhi;p1xK1dM<UJY_(75P)rmW'
    '>3nEUw2!*zEF)MEp0z{T$K^{)QzZkqPMSW*!nLE)N3F;&!ov+Ke`WYCz8-z4HRVbi-y3ISA=wayVXJ5<8#9ei2gh!OgZD;uN2slmrt|qjZi;}6'
    ')sMOHBpXgQ-vO11_9v3T^_T1ms&xKEJaP`3dc@tyO<Uv36vOz){`OKkf9b~3G~J;o(w-#yi@GH`Up*S<m&8f~&KQEU|5@`_t?TPVivo$Ttl4tW'
    'vYzB$UbbS!Oaf`lTF$_gbwKUUbS!Kc5^H(XpR`R!#u|adR{qV{x+U4P{o}74Pkqa{Vt7z8H&y`4KHv$^L>@<y=`Hqib(;TJFBFA>bgaMVG4E<S'
    'DQ;(Jit_Y&sJ+OqW+=M9HRahBpH=flvSV<@BF{`HmDKS`MWr1;g(0SQUe~*>0&7`V-fz+dT+J8sbb0-VLkXEnz-QHb<cta89NYW<ZGBe5Rc!$h'
    'Vkb3J68xEDl(>XUjy$EbHAA@3!4yz|JTt(#pYO4^I%X!2{thvZs4-?RLKfh#?fPr`Gj!BXwONa<SNP>kS5eM2`5Ov@N<f)s-jV9;(pR?fT~2&U'
    '2H+kXpXiMH>X}j~hE{6pdh96MJ!AuXvbmm=f2Po74bLcEEI&wE`4z-Vciv2ZODdD4USP7oEGpOc)R&1NwsSy^4${o!_V+UcqIatTzSXbw9hRQ`'
    'qYHFOHZdHY7njN`xnSbS8)C0x663xRsu800quyBV;ECTC(t;o0n{AowGHX*G`|t3#`n{v@YmxWXgRyt{x#u!OmjqPcd=?+62H@QASy&&T>vl$V'
    '^40&+mF*fS+CtaY%FfLAz`Pkq|K7m84u2(?xwjVbAaILGj*E$$aDMG`A;P0~L`yj}+*2_^*NKIYWzG8{TVoobT%AAH{F`$dNc{}-47#xW0;~VZ'
    'D)h0HBBa}V<^c@9CLxGgA~;63+y7wj(?015{ZW!C(Ph67q<o>lNfCO5nck^i2%=d^<$Pa1*hEp_-;KC1j#VHUPM?;{NgbWOdUg=a^Cy7FjsYXZ'
    'iSPDJI@lZLf4R$lC;#hOLV<Zkk0GMMc!C}Mh69!Iw*Ws$y((+++zsA)oqzC$*tcM+UYE~lpSgnoE*%;G3ihlkt-F=b?fGEk(ldb}GYI6>^{?4)'
    '-(N#iDSzv5eKOPy3Mlzd)Ni(wK$$7r{Qx~;lqXPxxj4a7!6USE7FXxYGC+GGgAiSI-nqX;Pqg~UKj6$#{#KzqHPxB6?VVHO6XnhPTXAIN`+uA`'
    '-UQTK`K|B3ur6dE3XSvo4$$zTwAIA4*RXY+rI<)^4*NNPxrOx-nuG%V3r%-ui(%X3GW|iT5YMD1-mdrr=i&U|aY*$z%GaJ8DGT>VUm~po4;c7)'
    '0M(CQL6Xb&|HfcL>aaY3S{5i``P`(1d&|K((uD?+?ihHcJ7?JP08!Y~3soUghfysP?(_vwxuNJ-<R9&dy4}NSK<VcUZ0Q#-#5gvHjrr@!%^B3l'
    '5>!5v8FpT#*D5VhlW0D&G)s#e$_L;kOEL%U%-TPa79b5ekn8W|S2b=|3mQ|L^(i4OD+lk+8?l9VfF=`G=e6>coNkxJ>_vD!*A!;1c~F?B?Q<EL'
    'OOOSg_k!*Zm}k5A+KUs<!5`71)@T05O!D(ni8y22MD^@E$(;)iZ*I29n+`5qR{Fw5LzQ-ij`~$es#%^z1tmt<uD|wT42D^HXn2!`%?(b*%$1tI'
    '#`l^R5RcEoK7VgY{YM7`o86z7Z|1_~SifuVErY|8wq%}ajB32kYMbo_)=Bi#*aVrB=&J`dw^HL>4^RR{k4TdO?MKTAD1zvq(n{EaKzi2@g07jM'
    'yoMBxLErPoI@@pmRb-X&Up4Nt%{7j+#20Y0;wR?wcJv^{AS35GixyuTqu2^r2SQ+h>c5wVis=!gtDnE88Vq$ZzLywdTZ2|Pu48ZP=G@i6IS(yS'
    'G07?&z0dgsgor3~{niYqc(V25sXt+^&$i&fGG4$d5JkmYrv=C+9Smo=x99I$i_UNOgU$zJL2svTCh%PrA|zi+I<Cj_6E;EN*rr?pbcJ}E)ojTe'
    'c#UC5Fp~y(Euu(|fx?&TpnGnw|5u6N5wv7ANH#<&AoCVqKB{s{LTHU&pEYgP?@5v%dYBb}YGqu@8op-G;+XvYow{!sN`SyMd+L7u?1Hj$0_%Z~'
    'jrvh{t!50AE~BkPoh_rAs9=PaOXfo}Djg3T`5V(kL=hqf%5$t3w0*ES1S<RGmkH)gtkU2_y!rba;F)+#fcZq^j9v*55<wTSRYrxfi;`4ue!-oZ'
    'f!O7sNV6iHSI<nmtzniU*LL+KMM{&Wb5qGSs|U-2de0mQ49vraNxZ;xr)1vajd^7(E=7rsdvG5x0JR)S`Wqv_6I&M*l=kp;^{)*CzCzVKzx@{$'
    '3J>QY%91w|J2)9K!LTJvk>A)rI37lxh6QcOB$SL<$F<}IS>*Dbp$8Sz_U`|c!j8OEWbQ0Q$1GhG7W#PexHYo(4bzzxU>@xeDeTB(RWm+3pnUwA'
    '@Mg!51i+{Im}?amQ)09C%P%e78zt<k`%7(pJTSJm^!P-P@;{+DerP@Cx(ksny_ZJ9p~RI|4jS9a71uE<SH;;Avx#;@-Qslqd--oR6K;78H9^b)'
    ')Vy!QO!LAkoyCBWj3dG+g1QA!ZaAczhesXE0%?AWhMLvBB-sJ%37gJ4;Y_TN`BVJ=O-71rD5=W+Kt%$ak5z?`K;<Z2M9>MI!nr(!hkIyS>PJ1&'
    'l;(zfMgV1T3T{4qx+55OS`E{GmCSbWm6ec{66k}1qX9pKK}V}l5(M?;!)uIHKn4LxF?WBg?E@|-^rhH;aO$oE6skuLyliT|k}Q(v-mky%u!qQ1'
    'nrpDW5?xnYO3JxYi8DA%&ctxg=FlL9eU`?N4A`F3;43<G^*PhD(xAn*q`OBEIP!46+x6RmRui>Q)jSc3*VHPg5U`WdlEm^ZLfZUy;tx4i0Po{9'
    '7)izWLmaOsevxNzB_G)qNHmEf`tutKSN;Yg>FYXs(+?OPa`nz%><VED-L}SagZ>dTl{Qsz?h8!SMxwtFCXw>KYh*u0h9P!qTbPP78t7$j*Y7J7'
    '`HXN}l4EU|XPVs*p$LI$v7rpL1D!IAtZx-X&{_~P_ZqpC%+JlYsDb7QUXo&GUQ|2GKehex%lLIgTiyvr9dPFzO9xD_M>IE1ShOA_*dsY>B}7)e'
    '5AFcYQ~_!hJwodbr>2Cx4t5&9w$bmC?4X~SlN*Qx-5mTA8mo?*#?lD;bgnD#=yHmT6d!;B+T!fL+8~<pQcB;nw-X0mR?J4lY84ao2IX)Gd@~Xs'
    '+tnjM58>pzA}sa<`0hW_h;Gpc%D(Kjy(D2?*6UxBL}8Q_*Pta-5@SdTB?^||WF8wB2}C-g_Xwl^J#znzNwS#aFx~ylJbHc)ea3)QuXb5>42R~m'
    '5?g#R2XeZU1T5fV5er3xYc6b8<QPBnu!P94gmBVrSNGZzVo9tHc40)%Id>7a(-hp48+RgF6qlwK1dLQ^Z|LSD=j~8hIW6oK{#dw~R<QZos}Ibi'
    '6BxQ(-;)nfm1&<uuGf+uwX|&nx~M9XZTBqpBv+pAM0Pq@n!fQF;nnLii+|%GloXL#8%LN#q{JD~wihq0*V<#=ej-jsGdZG^&FgGX$HQ?u3S%HD'
    'zOWo-?fJ&@i7E~-5tbc=z40PTh>nNEz6AWv4~9|t+xGkhBpGwx^K;paB!%b@iNol6@U&H4a)X5rpiDeYxH^pFhCedi%@;%C=IoyDCr2J~{dIV!'
    '=)sa?WmDTOUvl}JD?7cb?zoNoG{~d?*F|C**H>1s(pKl}|4<RBjb=d)EaACleJ4mvHzNBlAzdf*)X|-PZCmhD<bt?9VLhtyFaBs!r&k+Mi(hC7'
    '!O>^l8ej1&80R^dn}&SPoL5-hA2xj7JN)Tu{)xyuKdmG;&d~i^&m%8FJW*<@O9GFU6m@okC+X@Y<t^(d#dNOhMZTm2-DggM8EoVe49Y(<JDCO)'
    'SMq^J{-I~PcKd%Twu-1+B@RGk%=-8%*dg-l#~C1T2(=toGDNRM{gMoU(D2rfUV7T$o>?12B;yEPYti!4=xJ{^@aDsZsRnvhHE5GU@DV_hD3w>Y'
    'p)LYGM{3UYmOq^iWe_=G5}B;~Z;WcQm>Wc>+Ix~thCYEg$#(I)>`F$c9SMF)qN9HihPQJh3cqjOa$M72ex7@UVEo??UxM?{l12Fb(#z&ojsY?M'
    '%&)S*<@2&@yt2A!ThlEav3e@wnPb!1)E!UuB&o%-0B<e5J~L+`*NoYG>GJ4a78OHpGo%D!5Fat5f3IHu%bl-U7XT1or(An$r4u_2{`6!^*JNh#'
    'c&pA2sY4i637Inpe*&DDYvAcX%&kFmsP{D3%l&-tarM8ZQvEM?-e?!el=yqD&^5=?M=pqVYiMY97CY2K<D(*8;Z9FF0B@#?ZzPQ>3bg6T95P+4'
    'E(;M42H;|Do!j+mRvVP>tJfvj_~xrr(piqiH}qbIn}lN2xJqa)nb-h1P-7~#=#{npWS6)S+WbwVyrPNnEz^O`hj_b%XWfFdPk0T%HHk~c1px9S'
    'b(1>q28b<2R6Qry@y=38#ll5<3NOpYEzgS9TgtLOvjR5wv!TI_{Kt1$mtT9G6S}_An`9dP%rkh4XKDGRLUB#lehrZ?#B7JH`F1*A+5gh$?BQDR'
    '{_-IN5gI*=Xjp2Oe=f`VU+nzcBv0|A?56e_t9?%+<e>$<y8S2lQ1caG+UP5cu7pr}A%sb9GkBdhaGgn^!B$i$AH?2PPjV~vzw7t<X=;XVWy0;b'
    '1^_l_JrjJJ^f)4!D=2y}5q!Tl5Hv@ekLf#*&9k2Pd<Ff41Tz-7V+FbdH!8N{bY}P5?xCz-AaRcg`JOe2Iy>5RqgpA~b$tg<Xnu541RR;(iM|sX'
    '_VSQs3F@43isYAO058JPY`zp1g;@EQMo|CDo)<L17m`{?XXr>=cqElq1T&}XV=(e&Vk0Ss?B|Srq37vN%Og|uC$e`MBtz9hWc8aT;Kc&y+w;GQ'
    'Jt7Sp4zkKb(0FU-p||pxhtnjA0LT~~UW{3gBl6irjA(9U+F*W@<OMqIqN|sDrLo3zKg#O*U+_F3d@u)&$(mufK@3*p1tTG0Z??YqxM^2NUMUHM'
    'yFwT<S5inN{#FIvMD!Xum*qzcz@d<trrY@Lw~)P0y0TiAn6G5GL>WajTw^swHt^$|#KkP+q?EokvOOtLJbi6d+MGEZ2*bp)WJ=;OMcdL|x$FK0'
    '<o&0}L!~^$<7pu$D9wLq(eVOvxB*$CUG_@w|3^{}swmL8+rMoU`oNe3j{;is=vSzE#IKs9WB>KD0F2<(3qW|h=7)@NXVTl)?@jJFvh;@sj>68j'
    'Vnre@$9X(Cfpd9X?OOyKz-cCOAH6|kGsoMxE&nSK2~Q``SxTqa%R)nTfb^OxwT=XYg<u$i$lxxsu5b_=I?lC0e*qINFaSh^l~S0&9!jTprTagB'
    'Tm2V1PXI&Dgr#st6|^D>5nkeL5xq=R69x-~JV)trvkM!%owd^3Da0`?fpP4y@fUioo^ahR7S!$XIpyUq%}v8`#Z0(;8LghGU|R5XJpS0v$s{Zp'
    '?9NBSQMi*!>hsc5`2+KSC}LYXt$wZeHO4?3s<u6Ri*kTv7!4l%MlEd_LJ)!CbWpma;*N&9EpwmbLe_jEZ42_1Fbqv7e*Gtw)`T)X@&w33t00p3'
    'uW#2+f5(hWp-DP_Vx7}z@S<6J1WO3QfDBVho6Nkk5}hyEaOQQ0y8G%WYf;QU^(M<wJrp;9em()hzcj*i?XiydPc$fwg3p@F=?&KeupmIUWKlWz'
    'CycY=71V>&FLshAg|DxkA_V+PLl)sDmSfOSBUQK4wvWErnyQ3M3i)rtRw`*`5LQOCkgvV~G4NC0=a(>30ykpxM5T5h&m9N?wlu+6kU-v&p}?17'
    'Li{J|uKz;kLs#<nNT767xYtx)HFyQKDG^Q^7ipQ8t)Tci!=>;>Giz52*48;eZV`Ld3@g~Y>Qb)!Yc1A)iSta&^DOKJ6WA?7?nCCLGw_(j3j(_t'
    'B+Zx<JI?{eI1d~i4sjnLSz5r)WWLBQo=@S?B5{sBR}lJd)%7=UzCg?%tjy;`?JJ6{7zq~yW5QA&-cDF8-bU9WWJ{3|7AMCUA3M!O)esU|Osa7w'
    'I-$zYAdCEc`2IFl>8UK>a;h{}IYnx69>PIn_VMi9#6LDk{con_7w8MPj1D7x5P*r-u|pF~;>yU%N0jFenRHygRnOnn>K&>cc!Gx=JirU|#6B^r'
    'G9}$Sqlbf_bCG4e9qWA8m_Zd4xhDgc?hrwH#NQfDsI{LQCsD4yb@zYm)tK@0D8$}Baa82O#F}g>-infkKQ5l0P45mFdOn`y2DF7W2Y2ILy-YJ-'
    'Ff0Y5Z*o3_`=wWB6)o<0lN8qsMq2i&c$$-ZXmAQWj+DhFnXO60V_C66L=Ab(Wu{m^4Z=)(yE8e7`k+Vbb${^Jx&wlV#fCgWflu15K_<8lquv+q'
    'cgBi!0|Yk)`^+J%&hxt&!K%=}^9vc{UGZRDU?i34RsmmsFTT2}7=xdnd*@SLQ#H4?mfU9{d3vVCEqx+kawQ;T--ce(T&YVWZ~5>n{myMYeSMUq'
    '^eE@sYbaL6_Smh)>b2k}V@X7!taNHOCrD*WBbW%bt^+}hp_#gJXA;IEMA2(=t-ikMWtJF?8G|s2PG&@oRuN{lS8uge@9c|WdLgdwHI}mQa&ORv'
    'IdG)is`m+5lZ+d}Qh_8_vMQDddA=@`3nfy{b6NkxKDfe-noszC{q^c!J{n?k(IkF@xAsq{b`?0jfAhuk73R`hIf*i*)3d+iwO--5JtW2R+?XVe'
    'KqV?@bBX(@hAO#TzI}gy_F1aSmFS~?hKgse$;x`ryBQTJn=-1*n8N_nnQ!hef|7!kat=-C?|gBYGAIKnIh<Q(*|R@@>*?3Vx<JXtGW6qMtsNz_'
    '4|E{m$M)fX5s}*&tI%{VUZyZe|L!?@??%b9PV3%JV==LbKR%ZFFMHk$%YJf_otLn%og^r8fR6Ci=7-S2rmdP~>+19Q_T`DAom1ZiY*XXO5>=U0'
    '@+=T{s6t}h;p1!D?jXCKGEdT47|Iz5mpjTD^|tzwm)l`r(D6hW9sHJTQ`T+Xp7}ovSVcC3C_`Jw-Et%~7*1C0YvOy3ZRcAhw>D_7<$RK7Bm$39'
    'Qcr9sTmFTM3)_ZVDpLAJ_env48pimq<xaL^4^a}AU>);s5uS?90?uKx-0on-kX~^UVesC*0Sh=3816q)N1mfu@}CT`8GiAK=t-r8smYxh3wO`>'
    'r6Y~|O%lUE-WI6C`41+$F1}KD!-8p1^*#Yp5M+A(Zh0M2MQ!}R)QKPn$ji*vw&#pif_DnM!D8=c3(o({lH72;-^e5wfwt=}w%$`$kC5CUAwl*%'
    '3ZYsP<S~nRT&H<``ya!V6VDm9B$<x@^RZ;*Pu71OGP<#_F}i?<*M!pab-R534{2!L7=9gq%q#{;W|kV&(|ta<fw_+va^xhf%(0>S^vtXG>UK~w'
    'IP`GunZP^no~X0&GK+N_b^rapc)g5MWYj~rB>aq+WC&?;d?!#Ayue97W?+N601a;GT%Hu%#8ho?Qu@VjoaRaE4hEjaAdwIzjQ?)o{uZBa5V{u$'
    't*2_awVl*pP9Y63pDxQ8#-`7s`H-Ax`{vNiQ2#7#JHEaBTmFLX#N3mhFeyE|$??72z!z|5XzmR`Y787WBuV8L{~U$QtOnBc)RPL6@UZoMp73Z1'
    '_1<q5IzC6jn~dB-E5x}L-OJK#&bi&dvmzK#_(xW`+Y#I9!4$$EfLU&5A~`f}pO12&&5NAZC4%Y1_4*AynoAjX0iTH9D#0x@#-#2{I9Iv0JBXs~'
    'iuFH_p~E>Zrcg{Aa*U$Wn~fFNdHpH2sAYSSaF^suLRJ4}3=qJd0TRn-_Cx^-zZrd*#Ef_U5tgKFAb7#T4GuWYxqyD}5cN#(=k@P2W5fxX{n;)*'
    '0Lt?pBHDQB!m2wKCEOa=Io+rh(TW2Oi{RLQg8sFYI!4Qj9_ywHjttJmn0KuM;Rfb<JRgk5FbN9rc-RO6i*O<xD*;a#VC%(|?1>8y8xTI_NK((g'
    'w3FPya#)$x>DV=?zE21R@{2{NOT?YOuegLK&3@E~m=I?fZa~l-Kd-2TBOs*oW0DM)D#0OjM755;E1_!p0m_%FUC1z;t{f-=ZFVS7m+RV=<d8mj'
    '@tthDP0H?n=eWy|t|Fl4P3ws7Zr<3?gb|^X4!9#I#h}?*-|iv0Hd)EZIa+~22{|ZwFpr}+lH=<;kW+UiL0f-q5q>Z$I4BolKT)t|?K<wSF?UU)'
    '+=yP3YRbL7{|YODjV0MRF^Vw6!p^F<+^`HHPdw@l=WyO+8)rar^KH?oNHS5YQQSvi0_Y#uV;&B7z=*+%c$V!{aDT<E!ztf+0?go!N?Ui^{8^bY'
    'RtCphcxoU4Zy0yR;y$cyIEm}^#rZtJPj#)Tt_I0+n~5G8j(Na#14){2#>R%ac%~MtC6#>=spHM}9NZ`CGzAZpTUu<m5P~1<NN0||T+O;Tg62nN'
    'MX~^9HBlFZhc0DXjTUBq-WNG_S`9{up=h<709!brwj@nblLB#D=}z%MfPN_obWH*oVS7S64eGf^2tR8UU%_%aN~+o(#KqYsqWSb>jHk1bf)dh%'
    'nsGjA0j(fZWTJ{)SrQ>(LOyss)u$!Lg+C?Y2D48Yya`9~c(1dH1>tPZb_Zoe9q_sM#%mV*ls)->Bo0%j&EusdaOTmUPkU77nDQik3|2`gmH5No'
    '7!u^U+Ug0?%psc`+R1mjgQRUx&4w%hZ2qJ=BD;={xh*qq)+23gfIq%Cf$g7=ESey}GirznK9|j6mc*wHM*M6Ek$H4@sGPUUuL!p00F%s)c{s1{'
    'A&d}K%#Ns?JaGMy3Hj{rz!~QWW(56CpvGK##b=TrCITugeB#Ln3N+|&$<w6UuD`M-<{^@+ewN&sADYA_Lsnh!5^w~Q^kMa`30KX&{gNR&@Sb^J'
    'vyOX0b@wdU-3otbJM@&a?96>H3R0YkE+I#K88z!SIwtY~LJz@~nsPoNuL2SeKI$^1nu-^r&s7P_1}`I0<aAUL!pB1s73&%F=d<kysZX6ak<^UU'
    'm&R*8JI)G81q9ETOQB&R{cv4k{Iw1#l0f)<v+Du4!};Vgsj=Re^PHmfhDq?fmn7FWYoq67@+syPv^2Cd<+_~-;hMjIMM`EuldY1;r?I~dj7$b&'
    'gzpz{n^98AoUsIOCJL&$BKlXM|Lg_zoBXtJZ}2G!_%}u$5qLz`S5EnJ4G`S?;W}_Y<gk3GG;3r>w&iz)Jh91{MAPymmt1wkbe&-))Wz6tV5J~J'
    '$IIGb6PY{65mwD(#ZXfhTl@Sr=HhH@H{TSd4skp43?x7@TFvzX@EI|WgCLc;HgjsPoCzVh*L`nSPu2~j39_6h7rva5=%klCTT!B4KpXLZU$huB'
    '4QRySJ=kSV)oYGpqP3P&3le(3;ovvdm02HlN|gWk_OlZ!#y%~P@C{H{)=WSf*OIHzyXNfl`LgYeMuOon{GJ<lo`5_Ox_pvErKB^r5p6K0tA%CL'
    '-`9TqMfjm)W$U+k2|6*J(Qa<TIoMlNlVPJ9K^e3npV)a`R=!&p*y1de#6_78gf4jHI3cI{5{AM{x&Qd9BE3r<EXJI4#WglQ;R0sC)A!4CNr0Ii'
    'oPFUKim^b5{f{Diqg3@v5(L2@5X11sya7qhdTHFxcKH>Gjr(h$M#t?Y4%%yh)cNp!Uxo@?mESKw_HwrYsT=Vn^6_QHqT`3PwNPhJ&5vMtF|y}x'
    'AymJG5ECW&)R`xgH<Iw-Nq9suAR`_f==h1b=Dc#?^!^(rsDjGkgqCDwX;1=jTc~T0K@x1Vo5r(!17DX<PGy%kds4oB%vHvK@D3aBp`@ObF;U4q'
    'B8b-;Hu$@sgcJZ*{g!X8<~Uhwfxb5pud(KcwYz)SuKqm_VT3#^G4lr2q4-RB+ODJ>x%lHyJ{O5>z7}6u=i;;++EkF=<G`#a(JLHTXWbIJ|0^;t'
    '#`t@C_`jBW-&bJgEkq7=qqL^ev;nwvF5<PP#mrr=SCp+@XIx0qz`-idxxW0)S1N_?jT;Z;D>(!L?I;IY-@pE`aHkZCfFS}n6QmL08PVHwmt_L4'
    '_zj{5=dL#IIb1cCXvL6_J2@^Z@C>$Yyen~16V-Hy?@J$rDRBD|z9t{ggC`iQM4~Y!>8V0!+695ll3>bb2Izm2W7Xtw&CeCtx9v8I{Ti;%hst)o'
    'HiZa>o+OehM6c}*>VL;_-i2UR@)Bb4v!7Ev`zf*pV5yLg)zm<9`(`~ukSYv5kzvwsWj6u4#3*>vZ053*9Y@@^-@qd8a~q?Y%{Ld7^|<_W#oR;('
    'l&E_CsH~XiFla88MFsxS3x_0^*3f$fzc%b!e$j(3N}$J^M9z8A*sk8b!5c5sxh>b>9Ym-vmjnm#F#hJbp<gErO_zwOS>>c8pf+C(S{f*JEbBHZ'
    '-+ZJqakaQ=<Nm;|)(xaP28p4stdLrPmAWetda+#O>&KqPu(M00@p~udoSmD<bmb+ScSNv5PX@ns{!6K@0VLFg_1?bxHI?QG{P0HkRxvji8Sz3v'
    'zO=n;0B_7S=zGHm37JAl%`2V@?KGWW4}nH=de~x87pRyxhjpbtzk-Nw8oM}kv005bh#KyoT@a8oY^P!u4h(|wiFW#tu?vRgY!&l*puCAac;AGz'
    'P;Lt019~D^HlT0oh*P)AM-2(9lU{(rzESjF;r&1r<`DXjW8$TJyr^BlVGVko6-zUJ{Psq7Hfne;7G=69bb6&kM;+5DRbOt`j~vR{E8I}t!C9de'
    '?5#173LK>8<#KtFO{%#D2$y{&jYReUUOe4I0}WxXrY2^i)S&efe7|G;Xxr7Jgfc|?=o09b(B2(X;~`584YErgLQnsL!Z4^c=h<sM_Rh5Qt)fw)'
    'fi@g5+kfnahV*mg%iz8yrO1S2yS(*Z&Ypfzi&|%XbB-0}_)LpTcIUeM6`2j+XokK<N~U=d0aZ1aD!Ol4;X33X@N?<r#b2lSfbhz1_5V#`pYKp-'
    'i&nzMK6(%)30+TyqtllChrd%FAiVYYU*_8?KW&*^M$(Wg(sk%yDDd;IWnbaHS3KflZjXMHkm)ait>2kkThdA*H!N}U!Jl;oq!3w&))+7d6ZDg_'
    'cLJN^ZBMY6S5Zf@2e9C4weuo{yT$F7PY*1kD1!5}SraA8I88vItsFtZyt*K|urp-art{!<`oxef^)D(^fCVW)xOg-f4saMb;Owk2znza~HNY+r'
    '_8_1tYSq1D&e)^uyFfE`Jq?DM1Kc|ok&q>65e%)luZ0%O_iol^b{s?dGqU6q0yX`HkOIrQUEhww#0N-#T&nd;w%JQ|SgPb#C*SpD8PC$efV;$T'
    '>ezMJlhtdQThR_K@GzV&*qS1WpmA=x8fQ3%+YNZ{=axq2l6-d7nGkLZjoU11$Rt3P4q7Zau&Aj`cobE}Xy|}`fKgXmrUks4=5jko3xt7i4v9Vc'
    '_t5uEUY{F>+@2?IK2x~wzlBOTd}Ao3KL1rRy3~uxNYK}&2f-U)daLw%8HCnjA}&$BqHM~`N=8(Dm+je4>uY`*8HX}tsOezVuF5M(unH%@b0WKj'
    '-NMmyuFiAU>sK~&XbI~KL53)S)1#y-h{=7J0Fb#<|JbF4xLw|%+r-mvMhfcj_xoGGNP(`f#%z13Kaxq5L>K5x=i9j#nvY)QNKXXc5(@lsax=}0'
    '%y)x_>;&j<-(G-JeVXPZ2?{onRg!xuXq!GUV(biu16Zdy4Kj%FJ3+}GWr0JM)_DspS3GC1#Piu)ju~og%5fIHes4Em0WdQqqNx}Ex<dVsoA!xK'
    'cJ8Y&27}HpVT%Ifx{CHlC+^KT9C<w?i}`QyY%+9e!s`z?dCG3Ig$Xd)Zom?t8}KHJjyeKY6uQDZ-^-Sc<y;U+CO$BizQ<>UsbrTI1B9Jy4^hIB'
    'Qt_pXETzX4hshOm&b;cT3VBxR3(pbNS|CgnGE<K?L`OaUh#q~Bb6;v1^GK@HQ&Ue{9Y$u;@MqxMVz~6<1*uVoD!ZEBkcgB)Y`eV8PlJ&NN2AS{'
    'K;HE7m&pmrmTUol1k+ZhJEHP?<Q~%w$S|-k80-Ozn!-tfN(W6pUI~QtrWW15ep_E$0PRhq25(G&plw9VLk-li+kRkA9|{$Xko#Now=~`zDu`A8'
    'Xpav53Gshdn4b)2i3Qj7Vj$mM!Ik+gLPNknAXFd?`hZ$KBo~($gM;^mP{zV$LhL6ZiI)Yb(d(Cu=(J(Yp)v$+d1QW8G7FXvx&8J_2sz)KboD9u'
    'dCD2P&M<mpf)bj+9n2yKx}6@0mE$0HmN%?69OUyci@yK}#H=B|&w`((Gd^Rnu)mXs_ANRGsF{|CE<U^CVWHLjcp30`b}hclm5F1V_w#(|+Ci{i'
    'fuJ*l{>kW{|7yN>FgXGVo8GLD5y6MQyKh@fFA-$IjqF*vem9XNyg6YDvZZ>O-c4VUMhQIY3N^?b5tN~jC0;<erO6JjZ}+(|5E2ph3dStu-LIeB'
    'm*P84{i3vC?jVRGX>HLuY4JQLl(9bJZe}nmm6;NPMT)){Rqx6Xqd0Cx6m%s0P*ygn#ogQQv;U2HXNdnGTHnm(EU$P()CDlpS?M8i)`SW$NJ~Y)'
    'L-mZH1Ju=eDofjyc<lt#T*++`i`(1vP0uac_1DTnduV?3gl$X&ol_&?0VUAQN^!y}#=uGd5~`gTg!@jO<{8_qQkZgzXOFOTKGwFMc}Mf-;Q`Z{'
    'cz$mnl_$BF*;GP8v@}CS7OW@YA;FO>u+&N)XS*;ZLRn=mkgG!{+jSnvdY?1#a%5l*-cH617oK3h3GUl&VC5VoROnd+1HG4EQgS}>jS_*q5&3ID'
    '8cDi)V>r1YSWSBmhY}PD90=G&m(ZN)Ie<gV4)D%Mx9;uwbNgNM^!a?fNf>#^+d=7cwxP{Cymj?!k~A}No=PoUP%GRMUtZ1F%P!<#?$~vk=g#*p'
    'rYr<klAwNW3xMhy16~S_@wQ+Fvt|DZ4Q5z9e1`|h82BRjo(}`SWEL6HO4_hJi9Szu?V@wQvuYb{(qb0B!Me78{<#8#Ty3wsjl6)nSzlmw<F|8Q'
    '*Yv7vGR1mKc93z7$FxWy;(iKpB%Ayv`YFD!JL$CAvoVWMnm_vusQ;*20N73fcKX62P)}V?=DJ6Nt4lM{TOqQii-_>nN9;HcVa<r7XyFz6)k<V@'
    'pq)j;o^40RV7vHy`kwG3-)InsZ|6SK)7ybGXaN5@9`gnMB;zvJsyFardH`RMpQxq7NHP2IAHbL3Vi`c^htqI#{6DwVPvKJoB74ks2<sFt+4oF*'
    'Ii;oa2JXYXEYItDA{3z>0_JQ{VJGgoUItvRy?DM%UL~U&I0SZ-F?RP+OS5ghV2@UEtbLSMhrtdvs^GBsdz2^BwZPGgRtB{Es=8_bGTK$+?TOyh'
    'w|fm%%+B~J9wXpO*i*?2u5Q<#i?2{2pk##!+fMqH+B=u#W(lyY8M)!5okodz;L({8uUlsUZzNm#`KgnfO<O38Q6m~3HK7sqfc|WYkHQbaS851@'
    '%xQ+(#F{c=OXeK3#I8U1xClsK>eNC{@@O@T1hFh0nksXEnU=CBg>&&nc%F9;j>?$1U49mRBHKQV|0NFn`B^3OXaT*UvspzCCi{8v_5>I+YwrX='
    '54YnmpkeLzr=w2Z{C!*Nc&~ZX@QXdOKac*KY2y=2w!P98!h887TWfx9>(z467<OC>gdoF>Bi|H_?WWru(l_!Q7CS7jU1VdQFrR2)@81{xO`i|k'
    '06wXET1;vfifY>Wa+(#drkrD;09f`#ozA12RLHbN#qZqO&U1zKH?J#22b}!e0Y1iuai7fMrfgTA3od84$w<+$x^vnG)iLQd_``!SX483#C$c6$'
    '{fJAq#9}JaY$d{)M4RL!58HHzgE*W*uGsm;^!@Vlm3J~H5udjoe{DPT8fd8su*oR#E+xw{6Ysg<E9-kgi>Y%BqT<%+OCA7?Mi@2sntAZ*jJpYk'
    'f3Iu1_I%%CHDWut@oRmp2mUyVqAUeXMqj)rFCMfK>r3Rf4_(}&zZnK4YNlzNGEs`jSaBpTC3)?MH?!kx*Po9&mnv1yTq|(6WP!{|1uh4UbfmfB'
    'aqkiT60!6UbqrXa2pI@5<2Snzyp)>S3Y?Ft9gy7R9LWCXu^X-~P7i6_2u5wQ&UgXG7wu2R53lM+B%CRvG_mie2TGp*y6^f))-$8@85lW(h`XFy'
    '0`_wR^1JkP#kr?=0Ig&u<J2QCkudHN!gp=&n(hw+n54>H{>$s@xJm$)nK=Jh$oasw%r{xWliAG^==?TM1LyX>E3`Z+J+m@ivU&gIXj(|Ie<`-F'
    'jMwLxpZ5c7tjp(lL;c3Q)Rz~)g_fzSb|%-NFG2o$eZG0~$=LX9Gp#)LA~4{S=%Gl{U->NR933yzKmB&95`cXSPE)W17aF6}&c#<8`fCujW&?op'
    '1UGX97iZAT!|<W&cJ;Z{=%Abbk}S4(CyyjFG#G`&Uogvr{UJP($>13N?Z4ll&%V(aa!T;fXcsbY+=0ns2DBvE&syWzuKv3E$#>J2gaUayV*R|W'
    'j+8@yu`hqFcRG<|67!qpCxVhk{nNTmpzm!On`kFs_%ohqBO1Z)72$q=?s~31k7Zl}7nYNmiPKbLbxD5T&3alhGARMz+SNq`%T>fgq{C>5TX1=z'
    'n7I11V$aYZnCu;mi0bcazXPvcWNW7b?K~lk6w94HXYQ%CXOa(n5)F<l%qrL?ivBkh)syU-z)^^-il7%kJB|ew-LB;TiY=yPTlaL>_=v@9#V6Gq'
    '>cf4Yb)$)i(4~tMITP|k+W@(itlqg^f$tdW*F>-~p?-3hq~!wRRoc$u(xBMwuCQJI&c_FQPr-H)$U4#WIDA0Zv<`!{W?kE^|19YE^)A+`q+3}('
    'nl$_0-mX|(aU`-iQCw1<-$VAusR1i)<Vj`8N$2b#y3>KPB{q+}kLD&(FCK0Mot8=ubLY8}|09$YSWg)1{)Ip5i930d;xlvhQP?bPA*txyBJS1`'
    'p|5(J6cTWoLO>^;YTyji;PTt^)nxEq7Fv*;U{H}%-1Tjfz4EpjBtqI=3%@%zS@EwSiZ~yD7#UOX%jINzUh!2YrNR#jRhZZPijriQoGDjCjXgmj'
    'r=!f|wqM=T4uY%)=hQA8>UakQHO=^SGFQ}K^AZ#BroA>71bRi0Y&yjZdNnV$2J2i3@u!*ilmEg?Z@-crvAt?~X%s%pB6rLL;9-aT{A99Bl<>KL'
    'iOo^P2vduFWYYSn2_X`3RTg2$CJ-@znCT9*PM)Aq_Ea%srELqXb{fTQpT$CB(>-AZMI<AI>ulyHz>h*xVL0)JASFSj<fB9Aw|iC3aY&!AB=c#Q'
    'zacs7Me?6ahU*t+uyWI>2JMwvpHB>9A$4NWrkiK9g5?6Zab>ZvvmhLBWSSaS=(EF`+85~qx5MpRcw%__<S)@YKZcvz_2)WsE&Sb!=r2!-UAtu-'
    '0F!2bUxO2xpL5x{(xVaxW$EcWKs+->5_T&&Y;%ERyR*nW2|=X9N=nkTUENEkFC0b))jh@CpT(*a_+ls@M+01B086|J*UCq5aJr;}JjvqCTX%Yl'
    '$l2u1lsGU?Eg0u-c$=~De0rK`93b-!?v_j&$2KRmxib%D5uE0GAxF(*664)Q{6Cm*m*Kyrp%BP8I3yMWJa?7-ozxNt?Cc`?vhTa!fsNMPBcn;T'
    '0!tZxaT>}FV|}xDGpcY`1@wrfj5I{VSGs#Gps_#fkj7#q45h#MH_}Z~inkcQw;Qn6YLJAcF0_asB=S@s69rvedo7CVSOoAEsqzYot1(6rt>b#9'
    'tRTjT{;8x&nFsV+z6U0v7SGskAX{zY)z!(Pbz(+|!;GxJJidtw9SfEbOIus+Yte-lCVzD<3q>^kPHGwprU&9MC+**+e1g`t%csg~h)zn^SL<oM'
    '0U}OIW=jATjAOn`Hd*u^o->Yp4Ieuz)<oP$*p)JVy8xQTWR^q)8W&)?bc?s2Uo)-5QDUC!oSq+6?9l`3CQK@Ny)b(}5k|a%fXl&t2DH*+m+5)d'
    'VD!QI<_{JF&pi9WJ#scReGi?;?eaAXKQ0yRM-3lwe)^^8^m`H&2Ib?7Xt8_kH%TD4gVz$xW&D{wh`t~#qD$!>KdW4p<7D}t)#2|6XC<bW0FZED'
    '<$BLW?}#ICJ~)*#7KbgXk2$8MRLa_81u#1s&i9Ns(QiMO(q!Ig9(ZsL;s`Ae17N4?{k!+_&%y@F>GDMqo3hmxE#pelL>6uBrCpo9h94RK`-tm5'
    'PBj9fAnRBOHJBhpAnkiU2xIWwov&mZwC(C6OMh%9Ck9IxWjZ?}N$CbeJ{X`u8cw&c&sO4orjbKI;MF@r$`#eu63Os*f5x@?494;5op>_0rC$@X'
    '1f+AC`oGAVZShZp(s{?)QtwAs(SVj`{R5Lo<I#AwM{vGtszxpGiB<$^z*L<Iaj1jl#@TrGw#(c3WW)=*D^{7@vN{sGS5EYkrz${)n+hgrQBV7F'
    '^dz5Vq$=p_PBhjmMAMP<%TbZ2#4)Ewv;ya*B3W7K4AVN)zU=t|wFLjvco47}%}mbx1mIT63;)%Z^2atGq4b%g>-F@jP1q+Shm6DoE-KcgNUou)'
    'ZrlGVvr~0CPM!KGk%IQ5&gnk#(^e8HeNDTfbe2R=#$HcR1S3g1;vtlrtT)#+0!k=9M1EKUB`K5FCSmKV@SIEYiNQ|^cD3gl0${>7^0gq~25s>q'
    'KU7Sb{~C4!ua5E*lQ!jFQp=!&I78l)gu0ngd%e6;@7ecTum#?syH{lSzI@?COV0NZhjszM?x1r`=&U`yhy$riHE3)vnkk?mB1ic=5mtwAa8dQW'
    '>bn+G<h})HYI+86OTgtNOP8gF)o4C@4t!^zZY2{mQ+p1!Gj$I?6{?_H7@9fhbtW+NdJ}jQdUFr=!V<zPbzS>Cq}QXs6%+=dQ1hgR88{bHbP;JQ'
    'ZsBR?_9V~}hC4v{P_2kQ@s;2Gz5@(Nga~<t%@e^GIqBc^M=C)zyND4yKK7>;h@nj<K_Qo3FQfTQBW}cN+WO=y4~E&{j!coFWR>Qs|JE}A34b<-'
    'n-CztFP?jBH=xToD+_S(P#inw6586_KOTKXuJHv|GJ8uVA2_c^{0=aWiIrg?&A-Tl(6HK-q;Rt*RaPylA6<X@>MJ%Kmj4qSrJ%I|&3xt5{6?te'
    'rOu1kcog3G@00wMAoI^<uJYKQ=`}@eIX^^Orieru6dnDpQ^DM{UEMx-Cf^S5xJ5{M5fP6Mt#C8fz3=*^+t7stv?j8fy}Ntsd|}GW^qOYLhXP|L'
    '0w(38B!;X<gQKFY+x1)etR)}@837C*^W0_)pnqp29O?3Y2PRgS`7_QJ5Jf89W0H;ZS|Bm3;Z9$c0fS)OT5<wP<-T9qgAW4yK?vy42;TS7l5vpJ'
    'y}S7B!7G3=@YgUB_L!Su@(f{9NRfFqUV4o#NHf^nUxVN_G?3Hd<i3CSbUE2VJ|2t=e&&-WH0Wc#2YN#GoShKEEcs?|%M@Rp==n#IC%eLeGw=Rn'
    '=YWElZ^Y4gP6+^M8O=1e%a@!%h%FsbKu*|`C^r}g?}UUy5!dw4UdhqWylmCcNxab;&4v1jv0SuFNuW;XICBe_1;~7>=Sxp4c5ere6_;MLo<CIT'
    'oOUteiDAPnO3)-bgoH@u-3X)%e(s}*@<zrFP4SL{J>O%f5LX2BvEL_H^om7uQ!}irbQ)YzE4DK`5%nE7epuRbi!H0`bN)SSqazm%s%l7m%5u3<'
    'mp}+8)OdgO*feLN4{iaa&~@FvVvdm#P>(t!07$e@SQVEVL33AcwBWcoP6Ak(v6Lt}j^+N%d10ihks>T1rgIay1DvS`wocFNUtq%TG;^fM3Zj8w'
    '1*yn*O14q2<U24&7)qi#B&m)yKhbPD%uzA2hB9!iAKs08pat2>)h)h_cg>a^Pw)NeJ!k(nk0$#Pm!WaEI)ilvd)|;T6^|oSBV$S;G_?{Aiy$Y-'
    'j`{3J*$N1Dni_wF1x<p{NCkag`)fB`9}We*84$!QGsYmnLm4Hkvj@zguv~<ma9x7%5mty}q0M|PNsLU<gspLo@i)=t#Z07u{CjcLuWkqH{j4~6'
    'eoT^0Z;AB^0XC6~2J00q$P=aprNzCLH|uQ=iNkjfVippZQkn;{bAfpk>s&OhU)$(N-4|4CqInz~=DR&#d9NuAmF!b98umAtKN0lxY;5)7_@;<e'
    'QJ#^mBelwT^4}@-5;jMgNbks$Fn_lF(^nFX?nI|dwYkQLkJ$Sxf(nEzy)v4{6{A0E{{R*uB9Qr0w6j+6&p9ZJN!khAz?<+j@Re>Sr0U$BJ?+*@'
    '43DVqH|w&D0gOy$w9K#;(yU%}8CO+zqpT&Ds4_Wtz)Y*}<4Nz-%9oOf9LccRK$Ohze_lLAuea$<H^YvgmQ?&`J70cQ@)5!h3#Ws-9><Z<58D`{'
    'uuup^;ykYDdR0K3+kDq?$?2J|T^+Yy+B+|C{o}1QFfo|CFg023UZaTgQTc6(sca2WxF&{NN10Kl<!s7DIq#9%8ts7Rka#SyRALt9X1A?&I;|X*'
    '^W@T0C~EGM=VUUt6eXB8A3e{Vg1IY}b1=NYQJ$XOc@4P>%(7xg-ZlY8>;;h&TXpgp)F0ijc<AD@$C)KX<O&nb99^b1IYGoUfl29>O8Tx@Md!tw'
    '|8*8kSIx8Y;ZdQaWM;3W!Fffuv5PAQe!RNhSoBK+glE9SDWwVbq$vS>q|4`>1IaoQ`Y~6WmgyBwno3fDr#M4bX8?GJ+UFDW21+GNQiQ8jm5~4E'
    '#QzOPH{&easztQO<(MG?lGO{JO4iSvmoDrk11<7;hKSX|wukk$4pkZ&pAy<OorV-?$K6<WZ4y+W+x5NmrbeH5ljp|=0B3O}y*RD@`j1C)pJst='
    'DPvCTQ4Av}%Mq3v@aFPmiI4;V)VXQ4M9OHD!;rKe!K`cNu}_4HemYBdHWCFc=9VUxc9l1twlP+>^!jq9R6xE!U)_HUZ#cqW{6o@ibc?kPh>LH*'
    '%JTy5pWlWjUrDI0lj#*y4EypUttet4hAjS=D?6v!tB$bqApnVLIiVkgbZ{T14v(s8$w?3K)A-lLY)apByMd7#2&^`!zu0k)Z1)za9Lr2Pl<20K'
    'VBx10Q#-=&;6vvZD&JFevW;mA&~uVl4=<<9ER{=~^pBk3n~<)Ad97*@z|B?lFg_M_uG$Gz$SZKoU!3aT3|y>a;vdHfx>5@)z;OAK$%78p<!s~^'
    '@o>+#>-4B>7x(s4X?+r{tF^3+o(>5bNtGG2q*EFZI7qBXPF*dy3XJr2X1BT*z~BiO&r9>nMRe}bpLy!0nVsL`*k3-KPD0B1iTVo`nhpPreT#OD'
    '7o)<4V=|)oyz*>L&MZ`>8YFdaRf}KYfA#@j<EqKJQCDnS^uXdSw#z@cJU3sn#b^i{bp&EA14@_jsew0<;=f)p{@M97T*g<g&bd*;2*!{nN%@(H'
    'y8*<~waY)yfkW`w-amz|bVmyYuYPhzu49-m3+I^EDQSAdd+ZU`374F5P~SXpDk~<QC}z_scIf3jzcsGn5*_5CtMOmTc6q8b9Vv71*q-(01Toz!'
    'WtV;15f7_e?$9Oq;v9x|+V{=?U^Umu>>*X8HXk-(&QS-`WPC-9{j^`dz&9RUo+!D#e^;1ibGj!!e3K4hJvPk0qen9fVn*k}07r+1(7&p|6x?|r'
    'P#f=+SZ+>?zVAEK!TjZ<NQ5pf&xr$nF-v1d{lnOn$BuUlCxwDkK<??>F8$Krgcy!NnK-gjWa`Yp@Y*a*@3ER!*BI6NCZtrrZI_=rAHB*vs<sRQ'
    ';bbJ=ex$URCM6^YB&CE+sQUv2Vd~X9pn&ai%3IPY=3(<gD)|Hx9)3|>XcI#gkt<{S@m0G<R(R;TWSUv?m5$MuR^ExeQ>;4Ds=(x$nBedRnAv+O'
    'k^>gT5;xEX9N!ufz}=mYN6mvbkK?xJs&%1<vsAswS&SNp-RBXzF_(S}JUoyGD6Yk8ZpJ8P@YP6*BiB~A7#;eOkALmC0${@|k!5cepKtNmw}(%y'
    '%hgrFUyq*q#8s&|?`yevWL-bG0&>z2KziViOv_G{2^z1965+J`B3x;7TQ7N*M^1fg*L7?~m*yr-JhFi{(oN;9>l|Ttrc{V*hlN}p%fJpK;y#||'
    'n-tnf<H@3-x?-;FQdg2xzZeC~#*7R?jJHQ$$;o(B6Kqry2hJx%MzEkhP3y+x=XOg97uJU;Bd&9?VS>`iyFyqK!hCa_I8?k&>Pa^O-bhF2-JX1-'
    '{TmhHlYDDlJ#{{@YJe&_IZ<jiIe&7YF{Cc@-ajuWvR0O*H+V7rJS$<elsay@);*#_Q$W)BpEZBwkju;V*#mHxn@o=&bSO>-8Z}>nC(n@*ou?YQ'
    '<upAX1&A;%ElO@S*(QjKf=QU}+~AJDzDT_76|z*G?fQB0=MnmolyHv*dCr|hWq$HY(WkGPH_!W_1T;*DFgZekxjrOd<|>QR_Ns4hMUgwwb>y?x'
    'L6KGQ?$^(Q&nZIhHWG2xGYj^yuxxvxlLM+7x+F%}Y#)v>pXfgBhza@v0RdJ}8W~m1-QNjPx(%S`sen88uRmICT|CbjqI}c^cYa0e!35yW+C{zL'
    'N4fG`gBHdopvUZ($xs&gWTDsnga#r4dWFT?Kt)c><A1_yw>z-wmFVlQbYbWE)b4~JOICXvEZ-zkP-E+W@l|nC9?5(6-NS&j7%W^rc(6A#qMXkE'
    'ppO7NYnP*qwe9+O^)IV)Nsgd71of<Zm5xTudv&Qd9Y~%0`E7al|G+dyq=nuvLfT(8m7tr?z<%;{MZG^FI2u0<p0a{!OxAqecX;eo$|#-|pF~~k'
    'Kzp|e<f9M4krJ4=hs_UH;=nSb6;y)_;;<l{NeZ#ydL|De^^{6^yMb(cSeU+@Gv@m*Wkf9>@Xq{6f@3m4o)4F?O-Up3i6uwH?NJH4`b>GN@!!@H'
    '!L*ulBC!2@`}dEZZSPH%57_A4RxHKG2&yODiE_rJTcYJ3KllsPk-o$O&j<C4t*Ce)=;QV_0za%gcaorIxk|meT{>_6ER0PGkU77Y!{Oxb)m8p_'
    'j6rk5;4Hxbmn(LpI|gYQdlLG2byFZ#4a0s%vLh|AmLM#w_p|PwJTHER)R-7ggg=`6wrNML=7$Zi-exKedp?F^A$u>Rz*`iYA9|sAa%R%vy=@bQ'
    '+~kw!_lym1&JFu7pC?~``iUXli997T*IIW#J>hLi+PY7sIq#iJ-B}W2@bq{w&!uSV31lLXO~-NJ-@Vm-7Vaw>;dc4q#b>L-`YPfw-g`-qi--CN'
    '5Px*t#WQDO@aS~N6KRkXH;%?TLq~Qy8Lqx9%2&#&KQVzAb&w6|oBZB@_9nkuCSVkRU%=rt99dW%9#hnV+9t!S`A$a}I9J{tH{v|dYA;hZTMY}V'
    'b(|dlHAzdbt|^?l>SBBRQtwh*eWF{${YAdSK9%=z$FXau?4TP?OfmRQARX#uH)NGY3d|U#m%KT-4O-dr5jdQ^<mp<WuFb9SO&+IBQz~50GB>AH'
    '6u|n@>Ua=mC%pv8P-fA$&fhg4J7N`|icND-xg_a`=0ppVI)QXfdUqRG{8|>u%BZ<XggzmS6WIHQ4eeql6_A#Xeo6io7R+`bQTz+sJZ<hg9(S_8'
    'JGe}<wMg48e7=KOQEM@R*Zt}?J+Ez8w|8JjBi<H6mJ$8Joq>Y*<%AWHqxV&6@@+6d5HKXFK!e<59m}hWqzs<Uph-jjy7uoq)+Am!^RoHWDPLlX'
    'gU+d<euITLB7Zd)z)MV>uN>9a@c$wLOEO;^FHR@+WYeY^d{D@(1|QqC*9)IPP7moA=j4fc{u$b4mte5(VoTJY6s8r*h}&<3%CKsx3Be0bcB+YO'
    '`m=7(-OA*i7We#B5`81b<6U*WtjQwbktr_)>xh8UVAFCV>^9ch9uiLiMk)Ou8cx(fCrkskx%+rI=zx1zg{Ndav2os(e^+1OFwV8?Kp;+?y*EC('
    '@GY6GvilhzHe(|SnG(NJI{w%wFDHq}p%%KZV<1zQZK!gw*VJqYik-)Wd%L`S?nwj`c_R>!xfkvRBjy={MbIz=L9-4CC)LKnjAH8cM6=`nrtWYC'
    'e6{qfo89Tgx%t(rCUg8z(0mielfq#Uxl>{!cYycONe^+=6vO%clL=oyK|OO68*Xyi&{;!)xDuU1gbR#R7JpAa1tY3iZ?=nj)nx{-$LQATo~_X1'
    '%PF;JVJ-<5Gzl1M`fG8*^Ar&<Ja=+`<Ag8S4H-DuiD<?t$Q%@27>gex_BU)C^FhPW68}ap-!go_ob{m)cuZt*8i3zUscM<W6R*$P>7mQ8Kr2i{'
    'f5VRJIUYt5H{8BVwRx96=)baJVKf-otFJide5=josj9cb+$Dt-g64b%IZQ`JCXaYa+hZXa8aYFnb`#&8rd6Jw2pAsHbp_|%QMuB{lvLl?`^{^t'
    '8pr(EkQfVj#F^$|KJw>@5S>+Le)cP3oniTg!PkSXb$sXLc))AwWtfNTIX6MExZA~TZ+-E_<EN2gt`tu~3)Fv2&bwCj6V9Jj`aGqOz&vm~X}CpJ'
    ')YIe85Z1nVlbjL?oUNe-FH}hWw(EP}$!ydKCeRo>M|yh>)>$)e9$23Gc-u10Im22YT#P=?`mE3g>QX`{k#oMH5grWq6m!E_Uu-XXidsRJpICiJ'
    '9W38^0`o+z`Nr5yuQvb38iJ@B_$2Rfhb%17*%-Ag8URByD>U|13ymQ1KOQB)-@mS;vch0=6VMq#n|q$fk6NOL(E^exp@F@4n;0{9|1Jt6B0_;='
    's7O806H(`l_&Lwyp<D?l2Hwjy`kMvf2HxC209H)@Y1|&tSp47n0uJBV;s(*8Se6>u;uk!^=%<jCy_xZopD2Y$Obk7SM7j^2G{fL~fAnl~St47k'
    '@$~$0RnAiDO-=WT|8t}o;oYIwa~!`%536ao(I-FzA75U(?%P}(j39hVuW)t)XgZnmhj;x=8h73%x^pt@Bd{j!UmF@bm>#*e{&FybA#sLg()si1'
    '<=xSg_H)F#V&bTgYPpj*C?)Ytu*?~f;rnjieyUn3ToVMy>NCl=JRP5;PrxLf`k~0W9^%^!#oLKB=*;H5e^w{YS<p9h$5nVm@TVUh694=u)0W#V'
    '@7XstW<-!t!CVw=Vc=m*0T-oF#3F=SAciAT7*hSZj!;IAL7jwl2-G)^XtO}zXODDCR74GbPn-IGZozhU#iEYltl2yN-qGWr&<K*`7p;`Tn!IJ<'
    'J=CYh$Hx7RHnOeHg#ThSa5Fv$0lDK_eNFPcKP&}xe*ji-8@ZoT*B#Qsk(o{=0j4@Vk`0Q4{ql2JHy*b4SPM=4=l^l^=YRh4cD<Iyy#YHE6k%W('
    'UM0>rwd$jM*S=(0`Xr6%O8m}qkRSI{x{N7j{$iBe{BM{($PU3cnXjCozk6HuZ`>PySre&b6HD=~Q4{2%Ca}}(S0t^{0oA$Is(3D_&wC}zcg3x?'
    '4Dv+Nx~VQmi=IOC?iucW-yZq&PqpE|E}oheUBI0>#ZlwHAE&XP(MJjEc%3Lqq?3e@NMxRWZ6r2rScNNMo8RR~W>Pgq(&nGHSGUWjg`>?lZ*~4J'
    'n^uWVC84370o)GKJS~Ejc)AQuYBYGrvT1DG!6gmd8*|xt@rYFHPd&e*6uI(ut$Y4vjo)#`P&JENw14yP&Id)jUO}&~x(Iy*dt3i{<{?~A^R(ub'
    'N(H(oYuG1oo+I_9$$GN4X8j|t|L<LU{$`CM6_{D^Rp~~u|2`<<M!xCirIG{#3fmK7FWeOwBDaVsm=nwV>aR*ANM6<<eNz*QpS|AyoX6|Z+2>5l'
    'hhfYcdgrXEvoz>WMdGhhlod;pfLJTY;8`wylCm&R4F$3&EOV%Ls-X+HEVNi4v#1LXfBs5kyS|6MXW~tx5b!6$3-d2%Kbdi2=WE_ge7qYkSb9AT'
    'tAj!UiLP_0iUg);(gjJZMt(xxrR#jzdj8X?JbyFBS9bX<uHpen=2}nR+F%6SJ8-ou{YZw|z+nk-KG*mIu~6<cuS%?E79(~9(yLjpXl4m-8h!uS'
    '=N?x%gefb*4>Ode&`i&ezv~$>otHO3Vpy?lp*Kklb+p$(&73LE+!g5pv`}USO9g#FYDeAwdv%3v^!yCnL!8P9ZKG^kPm9$p`M$ab20nV<FJRpt'
    'sK=H$Vav-sKj^?cb-yp)xq+e<wh8?!%8cx4yZVjpAqFY<Qji24DYjp`TVObV6Gs}SG}vX$-1_o!i$n?aeipWIro%c?lLzt*QWPa2LS>RSdfUGG'
    'fPMJAA?$im%9pynlf8hutnj~jMxIgP{(TZ&3jV`f4mV36i@GY<UE)(o-=rK~cEeMy{P`-(|6{xO+}aSkY4W%cM#6ZKX5#b=FYdGQA`LOUiI+w7'
    'n~Rj~Fe#k#N7h4poLMzb5OH3wdkK(vK5Y6|6s2vub^{dBh)^+QO<<jp*)VUHtXz`8n~+PkVxK5Rs6J1C)KnUS(H%NsD74<_Os?SL5;Djn*gZSX'
    'KMUCNH&9$;jxF|qd^Rd;$G9_7r6a+(d6x(4UFr$sNT<dH?s}|rNincBK%7<i_R9c^Wt#BL%QrT6>Ft3FNlDN^@Jke7u)*v2H6Q)?nkUMK|16N7'
    '5~^&iX`FDI5@UfQS5nzm*Wr8OF~GOX`WS>A(xg#(x2xmH#IQ?`1Ra*E4`hHAk);^oxZl8%85&-Q!WKAi0*Y`o=b4LTc=Vt)Guje}b0?tHOUnd7'
    'xwbuTx|Hyo&&S}5y8!GNd39s~MHYo$%>(BwQSRkixQQZ$onZA2G+Aa1ZlwCAk3IA_4<feL+|l%!pnALb?c(X?Ssez1?|iWmX==oq-10;?ozTgM'
    'I!|htATvDn0)JSq+8u@nLkKw;&oRR{Ahw~so{28(cJcX|E4gEBh{Bq|34%my0oyG3-0MB>5aHArctA__QzHYs6DipMaoAsQDw3V6$E>L0C*Uh<'
    'yyA6VW0iE86J|+`F?0TJ^wKLH0UC9i>9s=S=>`Bun|xu3l8vMA#KgdBn}_HlXo3Kd=m6<Ej*M&UdvNJuiMH!^*AK<Y7}iQ&gVuN{E{7DzafYn1'
    's&jHS@uH^x+Vyu5x{a^duI+C|RRTg~NU}Syk7wqAy4U}q4xYb3;tv3BB5FLlAr=VSLN8(w1wCXr6-R_;EDkRTJ&9UU4GBHeTXuO^lODl2X(qCS'
    'de%=l0Km83UOoHJ8WB{{(mlb=azBvJvRih_ZeKk&iX^(E&s>gPpZRQ)#E~Q5TF9AH#k3q3rEMG@#jrS@#n;|mc;#$XO8l4x);gm0FJaGGYEn47'
    'M_Tfq#IfKT!E!z5^%9-MddPkcAS0H?mf(es7a^YgK4k*S%zS=+_thxHsrA`K);{s3(MiL<Gj5slc;~Lwhn!m;#!AUFm>dfUm^{d}MRM|?@JU*y'
    'Vb?#z++yD4ci-E!y>p&yO0p>Hu61-~4M*Jdo(O-i)?xWW_$I-qB`QX)CC{CPVa1-(MuQWP2p_f)Wf43}I!Uan&vt#_Hr;4M7t@8J?Rvz|*B#F0'
    '{lvg_<ZH<M6Gx4`tiN@ovnHD?iZnfQ-{D*n{6ufk{lw?ZgO!fh*e^b}SE{0x$j~IB&Ue;|^Kklxx(Pnc;X_oK9GNQ+NubA1U#UULM;|qPGELVz'
    '?vVNIQ?A?go|W}#Y`b0Gf&)GrsQ}F0g-#h~+i98?Q=acW=oU{HA?=;x2E6^N6aFfFt!<AP*_(U@4UNP^rHwDq4#Y~P(eL#Yl}ZviKCxi7EB|y^'
    'sgQ7#H6}6nE$2r!M3~cDO5=lPLdR|gtj1CU`OPq^&>yxnvkT64@a@%m`|@>5<GcD8u3N`VeBSjDw8m@^BnXvx1$Sls@ChwaxRYnyE%W_0!b1wo'
    'nF#Derhy%UoW3J8(X!cIxu7x!9>}|5)y%lt>an@|i?1M58Q3!i))tj=jkW@USS)sr;+NwoaULI&hHjr}HGrDPm*Z9%wQrZ7Z@jf>fW2v7sEOng'
    'IWmnL;)Kr`1J|Bb9L>299WA6;2E3Ax;QNaYZ?VI-QsqPocBQ&`t|0&DskX;XIo(RaZEdkA&c^U1|FS=R2D%sh3uE1PzH8=6ho~zX)XTi7k;5NZ'
    'EaoV0=v?gPgUb*lohZ}${h51RTpxQR)lpMm0Se4glS^THSu^M}g5)b<fT=O4u@WUof2-V*vBd<Bt)u~|zQ;{8megVRt>ssnREI9PCGcZW9w`|6'
    'hnH6u?&X}VN^ATxntA|$=NR6SJ0kUW#aO1l{xJ$*(kEF=rx54BLGZ=I+gCe%(s(>S*}7xWx)zLtVCzYLuqV8f^d@OtzzcmXfW}8y8TiIhqp7r+'
    'JUu{SVA8pJN{DYJ;5F8)-y2X@lNpN%w{pT*ESC}#$s^jy=~koKmTI3h`eZz^<One05^W_Y1GXq2*K_1R*hAT2jV0nR1IH)XF7KV!40Rk1T6E0X'
    'NjSX;<FH}AVUuT0UOXP$NN<v@pC|oC^4f$bLqg69M}%6G&BTFS6Nb|(gv{}u`ej|+URQF4(Ajj_jr_$grQR(GMbgX>g`{;%JUYLW?l{G|sm=!;'
    'a$bLP$f0f`;YvvCykE&NHzlc0u>JNyl(MaBnJq<ZMKjVJtWmzsW4B$NJyL6l>)}i|s<j?N3To^$B$!paph|DzOUF>ldhUH)?<|(O-F{o}G)&Ej'
    'hlK)reZIpBQI)WafdF`4`j{4b(>vYoYek@N^IR^3Ry((fh6FNqS`4<B8k$JcC?nO#Pfmev*?)R_U21u8O(M&(b_qz#)+2OWuQn^LvrjZMBvgS&'
    'Ft@cQHE^03M{>7ixxrEnr4ON&45OYfY3=Oo`d<AKW?)}@_mhW_o>JPit-eC<diF2^i>~KMTmOJrdCBq64N7LA)2-bIaiL?!q4qI>7?61e`#;yd'
    'SAlo7#9=0BR0&#>(`w4$8`PyX9b;1dn0QDEOHIYG{z1%vnDP8$stYz|23av#ePsy0>E<*c%V*uLp9dd0J}xfAz~pZi1K1}!G=sX`=6Q+mc&lvz'
    'hnP_-bOcYlVHl52yh6-bh2U=FAzY~yL_U=LqvxsDfztFfUeNRH@XJmiJ#q^ese3VdAX!FK=T1s0-sG5R<sL$MKC*u~0fp-M9>j+NuF&89R#z*S'
    'Thk(un{(?dIv)p9ROmb@#feDsz<2SkXSkt}*(%28brEY4`H8`x5Qxi9KuA5-NzD>U6f<Al?cyGHpc-JN7Jxbydh|CVrA2A$X=}FF(&NY`6962u'
    '!(bU15y!oCCx(ME^NQ8f7>lpo)&t?P{oE<*GQ;CnJpH~Z2jDrSs4X9EAo5N&E5zo!>vKn$-+GBO4cmbTHIIpwo;J}5*ILcm^qSaZus_f3YfoM0'
    'B$2G@!lW>g05NZvNbOAv$_$>o(?_il4s#dIr;lQkFw7>EGc@G6a5$AwFA0QaJ_xO_qg9Ok!>jCCP{~Zw;BL#9TmtHGifu`7C9-s$;L)~IiN3vK'
    'JkZ3rC@~>knRo1Xsj{zVI~kQEezmClp6%*)es-nRd_cXAf_~(O`47B@BE1yQ)mHkUe0r0iD;I$?wkLT|Z2V<enfLP{Ji)<Q$BrtEnHjEUJlnU+'
    '*BmHXNXgh)VX|==e@q-qB+)X2jCw7o4^?~xj>$3h2~jWde@hxNCj+S%j5b%;J28l*#(sTW`}dx+Ow2x=G%2YnhC3f%Q{(oFK9HLOB^PEsgrp#n'
    'vcGjvA-*DwWI~nG{0}@l{`(_$XgrQIM{c=a-}>IAw9|8mgp@o4UD(OZ9wa43yt#@ccMA1KoUSOhC-_M+la}VJx~Av6)3TF@aloGAC%fhA5DRP<'
    'PhB^h+q~NPU`E?Qlg(WrmULY;YVjG^sPwRipBeqhB_q6YMq+#gTSQLP9lpYp&dHxI!Ea{r@O$}mOF_&&1`}^UF-z82Vgl}DG;x<$!{@#oh6@r`'
    'P4SmGh{+AoL(dRMSvS7cQ$5g?w<|G)Y;z<4XuE}iy>D$TZ-Oqgl+D1qR1+ycE{Q(gbCA}NypoS$elq`?wkk4^Y2+1=Puy0DG7YAi)NK9<SBA>`'
    'pD!Te@0&brXwl)qMi?(ILXuD-u!r8&doPRu(Hful6JA7+&D;{bNBYOx3BC_<EBC3cc~PX_YFi(F|NKu(1w<PWx|p!Vlw<mvyqYuch4#IJ0^t@R'
    '(Y)Mz)NlwS8d0uf-XA@05_X68?=0i{ulb2i2avPwcMw~HF|vQ!&dem^b+h!u|0rY9!Ah8rIVf5s@hH?9PfN5F38Zl*foA&z3C!DvcXkm^@aVpL'
    '3!_}y^)nE^<d3uxHNpH$3tDk!t~0N$sUdS1dhYJwlwsHz`gJ36PO^I>avU<iDRQX4zXlbm?c4ZEwfO!E-+KN=Zr@4gp)AAvytQSMuI&4|G|uFl'
    '&8?`*PFIzrPeZxmpm}DQxWknBV{~%M0}2Pg&_ItD1PzY^zWYbl|NM=dzA!C60cw`0Y!l>ynm3;c`sR^S^c4YCdbe~hk~LsXJ94npcnWaTu+!i`'
    '^56)iL>^Nm@7D>LU(EQO&aTvHIJsDcg2Ae3R<HUoe0?rq<pcSD(4JZyeJRW4Pb;TJXJ4l(Np9`JnN?!<*dONd_mQU#$QPx;c70ab25dq@rg|Am'
    '#22VN=okLzN?*e2zNPb-%_ZP;?kgWL0hjs38W6|+=yASuDO7GpV0t~30KDsc?ccz=kgX>Kn|275ze4*-EX_Pl5ohCod(&}jW>Ir3y<1s&O+n(4'
    'U^QI-1X%dN$ropKmGCz^2mY0@)LcKG?fT(UUn;m5AkGo#zD&XruMNzX8b8^;=+N3Q&Psk;U)<a*OIS*YKw`<FU`@v(iX|kS9wum7@>hHI8wfKq'
    '01uGuFfx;8q4O`QH-O<9iNIj9%>>1KO}Gtd{M`SYa9_Vi<_h#H^B{d^E*-kVQ-UNKq4;hs#rkC?Nv%T7RD^icGOY#s0Ry}hhgVLcrv(lvkk(1x'
    '+^m6AnPy`p!PXl5ZgWCSe<7)5$`Vx0_PYkvg-mL5A1A^AB0v{JG%@1ki=5?!r)~ay^CH2@7Lrr!IUbKOg?TQ&rem)H6>Fd^O_>uL)7o5K1YC@N'
    'p~vg$pqVwne!_!QGv!=zk}I(}XP7Y-fGQ1Ig?Of)KDR--4_y={*TRP5M?-s{HNT@Ch_S9E!@IxPQpU62z!PHBz2a1OidiHMqt_9sIh3dzW{%P)'
    'a09>xl60pIi|sN3-@yaI8dC$8{Me%JfE{lAn>*sGwqG49%%F<vGYA-S{v&%Z=>Z`qZOlBB&6?AH(n<E8O<J17w1H;Btei|5*%DnhKW)~wk}aMD'
    'n^0N4zbCGh`kmMtpTkJmc`m*_LlW~4@m4TrT2P7bw%JNU;YNcMK%k{`DoKFpwlcf+Ja?{?1m_@!h!836|M~8{`jwp95Q82bhni$UcZ@iKgKU1W'
    'L>PB`mXu{}>5b;L!mM{ER5sy94om42nV0j%GI?tK@CR1k?C;e>%>*7K=A`C)MPR`y%-7y)O8@oPYM+E*{$3p)1rik23_wdAqz5MY>i6$%*9#o%'
    'i6QN6N#*qmU4SSiJ<d}U{Q`*Yll+83>{bOHsnZY`J>}>Yi%1F_ooust3#6id%)bbkU$SW^r0x3-7d|;=Z$$0QpnN2GU77G01(-a%M^1pSnVM3z'
    'rwEf7h2F#-lL@(;Gr|0uhQ3h`tOTl$fvV3SO=Y`*6+z-;u0}<-i;}t`U%muiLlT4^oK9|-(w_-GT^x3bEh&@6Hdcw6si9{jg9fJ%pi6qw6$JhN'
    'ySl?<hizs#!+XhZ9TrIt0I`_6|7`?ML;fuIR^3$M*oP8D0)v_Ex37JvSMMY{J+J{WQsLVdln6q&UKSHuzxN8QP>k)fpgkCxr$gtR_i5PdU?r|#'
    'JFzOso?}cC3+jbsqb_BC>gH~PZ+-}@EvA#1u_J5K^egDp18zZ2vM^kxX36y-%A8*{Cq=Oh-Z;H)`Z!Ju&9tObV`jfRyi8NuR%2xB59qj>(uFkQ'
    'Fh}q<Uvxm3xn>K0a*kzQDx?6}>JLVWR4G?(lDKT`OU#8!fh`bMcF}xpV9T^@|GoH1`1Y~PkIrCAV1@q><w3a3v#fV+e_zOb^4<h3NIdtP#rmWp'
    '{3dLzw5Wx=f%AD_bOP1$yv!d>s>8Kie?EQC%#cp=Z|Q!`B1ZGDnKesb90nPVRQGoE2rNnae2Dq4En0g5Ah-W?yIL@i8R!GDRWixT%1*3@3G)`~'
    '^ha6Z_Xr%!U>c0}OlUlSYus76)mo7{uurn=+Oj?i!_tM-UEHqT6L(X>YY*ut`M84yM3Mq2WT^qx1j35f!w|{O7AV1d0&auvGpTL~Wig%t6{wnW'
    'sM6Q7XczadE}?#det;9BYxRkHGU~&**Y^WyL3^lB&EkMwv)p^7r-nMs#@bEb6KY4OrL%A*k5LQSa`=X<Avfr1`-2w#>oDl3QPI_>E)^RE-MA9>'
    '<puo^aIt#nGhDXE59{8@jA?EdBcn^_85KNGTi6J}1!rDOn<Vn){=IxCkmM9WrVc8;kku5KN1EXXZMgOxHIPGVxV`pFO>F@MF{5h;`+BZ(a@gnL'
    'flTg6K&jWLq{R%e`u=XLE7bc+@+OhsGXKT`1)Pdcm5S20n<qo03Z?a#x$7r!1v(CkMwC!RLY}!m0z`GY-NHVQ2soLSYk&4q(}5(?g*q15Q_>tG'
    '^nOUg3oNMjT&tPRXV&Ek_Z62}H27)k#@g4-={&YDj^KXvAFTsnrBUo$G`0^=Q&8I)Q1)yCDI&Bk@A<aiYx^riY+a)Dom>vhFG6$D0>>_7s8Yiu'
    'v1MJ+l4O`8)d9`6MlQ-&GWzxb5{2LZC3Ubqi4RI_iKm?#9fNk(TONRX1e?Xp;DK_<U}^<_!VvANu)-Fv0hAi?M<zW|)5Kfew-+Eha!%kQ@~(G9'
    '^UiM>THX9xvI$y(L1=T{ii+pCEJ>O@l5uKzz3T#t0p8REN)nrq)^$?%D0RGIXW72L(#k>Rl{>D^F<fp7mXNHhGls<G1-yvBa<nrSiv~*(<&=h7'
    'kfbD?BN(|7waJUrTjLt=CO@(B?cZNpfhhq_7ve+@oz^8ylxh}9tdQ5=dCK@kfoLE%zrErS<<nG3Rd12WWHsXGOtEVesJaQ+kG8ijL`v<4151yI'
    'l$`QS`VQ-H<+)`YBmh31D5C@u)x?k_BUwyx&FM)+BGgU5JE=)9j`7u?D%0ETcOH!qXB)^xGtTJ;2y3pF^&e0KuA;=H?b`EWpVsKN!-ar&&0ngm'
    'gs|WLbh{c7opm4!mJINg$~ZP2()L*~Kz6sJFepxWPNeWAFpPiV)D*B0n^_i+&BHOR67Sv5*F$E8%0a}Owts+dS9Q=q%HT00GLV!~Q@PF`5fQ~N'
    'eHt7R%|ABd&y(b%<OK+kdayT@mbPaNM5^Ff9SnM@K;av`@BWxA>S_^8ic7$59=34E5a3~bAs#9RZ9GgZyVhLPBEh(0Pwgf|`Ki-#V49>?om;%P'
    'y7{j=Mdb|n{$4!<O48iJfXcw_$(xA;s<uy1%-M)b-qfylRDJV>Q-=6gjMW%wD4~<*r@mq3B6Y2w0UrkXsoSC={P*f@b?(k6b|f4b{Vd`HU-HX^'
    '2Kb{S9MPbm1>X?+nb6GRlv#<DXywSGqc!SKQ`J};0U&X#rHzZB+PXF?I-B`g9z1QTNgqAg@a;=--?i_u#K?I|pjA9%hB8kQw~>z|Ap{6odZMdS'
    '&N&TRV;b$JPI6C7$=E)?6YOf!x&##`>h$bE{-UdwJgmMyrA!TBEJ6ehGa65P`!vQ=8g&umra%$h+b%UF?LX2z8EoI}>NWV$D?>%1Xy6Z3R|c5E'
    'mQG^f_G$8^%%fQ>g8&Xd+kQvoK9V_GG<USW=70HQWLLsF;VU-%c>3?)XvIcr&?i-<B7s7Af-cNome9obirmQ8XXT%dgAK)I7^@zul6`63{N&{R'
    '@ts@dC{WbF2@X1gQx4Hf3v2A3!5-si#t<FKY}dE!adY)Jk(e)Kno_tB=b{ms^aVF9JR$bYvYK{Pq#BkTuP_CPk{3C|_z`nq`}MChuB$Sb=A`gb'
    'y^i-NXPiOS1t#uOcb~$EKe454^sJOtAyVJPpd{%iZzK9HCg`OQf<iT|CC<3F%X|8*FGu=pR!5U)jU=mxe)Ox|?dNY#=$BOBoE!6%5VRU*WGLm{'
    'G-Jz3Mr$Z9V0fgpAlzArx`~3?B7NWd>0;Z3@+JV7KM`nEj%Ux9kZ3zJ@*ESA?}i%5%bBhBjZN0ub_CkOfs$kG5GHAE#W_3nH$SBBB>}k8cP4CQ'
    '=rWz<7MjmehY%09H>-D;&!%L~Ew4q<%iddfw0w3JaCPE{20m+u!8<vp6>_`0Ehl4m4Pax%-U20YK9;d28nXwOp;^muVPVdr_-BaJts1%$RJ6%4'
    'eONYR=_yq8l+gmr_c}01d-tzz(+PvF18|s8X}%ed2^6b(@-$G|NK7V<l}QIrL^9FxTrXJ34J0C!G5c;g$6zj&7KuoYtk|nxpY8jH_U-&A-!3UA'
    'BOwvs$sj6P_OItw#T`H9!%29nlj1L~L}AQe{z<aG2$T(N9Mi7sBcbb<N>W5yfVZcAA-^9L=zMPm5#ssA6*(8$n%19Lz$_Et|L4JJ;PkZU``Brd'
    'FmhTBG&EM5lI{5((JtPLkOptcj^Jr2o9AOicUu_@mIADD1kGHSu=89UDbfk8G(UR8EEauzQWzTIchk#RZU>=QgKfEj{Jy@3DdP@4*7OES-Jio?'
    'zv+RF`4#8+j)#%KOfPTn{(!VKFJ{d;6{yDBZD(8sqQsZxSP8bCgDcE#?F8I(F)sdr(WdY4zWa}mN>h}w=wM}*2%^L(%};~TQiIbXt05@A!jEXc'
    '0Bn_&QW;&`i9SC;f$75wF|QYqlX+i?=a90ti(3P3>1+g1@suquvp{72q`Rzsb{gj_T&$V#eJl=;Bua2sQsy_QCxi!43XGgkS`b3%ONJaHwc_^^'
    'r1~=cJ%aI8rltjdjU>}{L_SXw^dnostwQM6f}<?Mlcp7nY(~eZzhOXGiOkoGvtJ5RdiL&B{#A*s0Pi$t(Mp5+*9iX*jq9=?;RB86r6C)$R`ijS'
    'Er;`(^1P?L8aImB+pNk6->)}BS?x&n>iQ%$<oD~Z6mOs>k}t0ZA?>@89;>$E!-S#d5Ct9zl`Y5?O;2&;d16MLU${q}M6p)_GronLS-drNLvCUy'
    '*ssn`Oe%E<$|XvyrE9BlB^t?`7|f2hgONQ)z#Bj&DEK1^k#Nc0Yvl`FXN<R`17`vc2y#^`Y5AY;e&;DC&O2pIw`ScEFO_2=fSor?np#RvB{fBw'
    '|EX8@^${G(9f4b5Q6hXuqPuxM5wKVP3EoEv8#&#tSgkKhvBTieBW!;1^h=UAEM#jj#>(Lu;J<QWmhaePF70T|JxGk85RssJmIq!FZotF<^IW52'
    '|M%9vi$`V@?aR}Y2EqESl+9|vppmir!aQjt5@wdb!I5N)Mi^<6uiI(vW|fd^p;J&R#-M|O#p03sShwp32RHXg=9d7#QZ4^E&b~dx(-mw&qO}f*'
    'k0kR?0*Cz=LSW{y?-ZJZEvHwk1W;J~W5XuRrg&s<%C=h{SkCQY14@-+xU6KpP|OrC<6R>Zr}J6rEUo|$xfUJ?1v28h&RjG#4;e)R;4L(q#A-Fm'
    '5Qf2c_sfTVoJ4FPn~r>r=;(kx&R8u;1Ei3-8o-O<9*Am0;1+UFKNprU&9<=3pcot=IN3$_<yX}3*{;6uzoJPnO&GW4OYdnB&j23o0Pd{cgv+{r'
    'm`HQq$2d&Jg^Dw1z&|r?To2L_&yxo2NQ|`}TXm+pwqHKL8pPcrpuBE0mDpN()=2KU4P0OTMTEk5aO|L00!1?6vZB~>>x%4-OwypZM1%IBazO+^'
    'W8}}?uOB3vXcD4(<4l8DQnV}b1Fjkj?@uJ}5Nx6_%3=`F<zXmlAd)0&w`jf5Ph}3n`<#JpuxRtix_-6b{YzmKgr=!fKw*eb^aZISqm(gpb=a3y'
    '|0K0kpDPJ3Y(1A45@o?1w@Vb`a9D;R9j}iTZiI!bh}vHJU<ftDp${;^#F>6Em_(@#8-*yOr`gd$D^wGyg#VuCZ|090#o9=$`|ar=Z>TK3W5s_+'
    'LlIs;OUfX!eS|xBvK}c(`(ln_XqyjJhIFF|9O5t`sk3&U<O?}*BO}BmqDE+mH$tPBug5Eod4BZyAk#n?tl62ab9?gwtO<5QUQErqWhXVfzEPrT'
    'cqU$Z4#>DK6HnfPyOU9ItBvcpR=TDb9>FUVL7W*)Y3?){cfh&-`Vs5RdVc=pD5LE-f$Z>nXkR}`y(qehnG{)+`}I9unz3u&W7RYzqYp5*t~7(K'
    '&w-&+s;*p4t+aomR_Z^Q-o`QP-FPQ~{k9NBl{XBl=(J`7kq2aaR!z9fGXGk7Qu8%=lMQ?=g@=X1QlYtcF3p8p*~>8A-`YOF7rmX}zu?xc#SRq-'
    '?UpD~_=<9U1Vn1Id*vxrOP@~CwN8|aO*Ox7f)dPN^GM2RJEXCxo+MUmA7BKS6fU7n_C8Z{I4a$lfz9<h9et_Bj0VlDAv2BC(P$e!cU|f8A(9@^'
    '?dygrov#?~jos2p-L4*X&HV8)ZD;kNZ!v{9I0_*DB*dlIw}`#Ep9YK;Kl-=8cPR(w2_qF~?*9BlK#$aNx~56{x@pd?JZ0MlumCKyHUIXRknXkZ'
    'q8)+W5OEr+7?%mP>D^G*u?Ri4=yf92KO+y|spDnS=LIVF`(dr1{U+6M^8We$E8M%#>neF>fk^dM5f0OyMOJwLVF+bN)Ds~jf+XolkrK#qp`U)!'
    '>y{GhjH?HA1uFH1=tS;<e$#(TzIT;;1Of+6RgF|h#n%^O5DnC*HotyC7anElyg5|a4E^sn5Uu8z0(fN*@%X=tCxE~jyvI2LL219+{tEX7Je@_4'
    'L(@svHw%gpv8%kGf$p0}U*LoeL}Yw7-@zq(#Pq^E{|dV{V-L(TLj_TSR6um=)U|Ve(eK`_Z|i+8?NGku8Zui<{+Hs@5KuFqJ8a0er93w14TUWE'
    '4yqneCp9mvX6a!9sM4_Y5dj<6n=qYz-2LwNMJOZ-e$&X3iYb1`GMKaxLq%r|`Xih1+~xtYWA?^2V9AD#h;i}Bdi>7&e?>@2laqq{UX^0qE*^l6'
    'Saa*iL)Moc73fq>y*sU4spznRx76R#&OEK;>1EN{SI*HOu9fSMz+J8A6p6u=($LiHhga_j-?1*7oxe*x?Muv4H!}ux=3}ABm%kG^3D}Wb-Sm{{'
    'j%~SD&A~#v^BRlRWQWd=nMf3X35lO)+dshV1GF5P1`a{2&EXWiQJ!(1W{#XEh!v!~i$i-We|aA{e+bomZa4Yhl=lL^+Mzw*`B4%@Q#UKriu6W-'
    '!%9IQyjxr!`bB1*c=EHZ$gby1t$AT`r~8#5gTlgb<X$;595D|0T<0eor6!%}tGa-<sqbGNLr345^2ID6F}b9td^t2MvaVXr*hDOjw0D>yf0?v*'
    'X-<P;B~)O^@5>c;CqP^>B!7UA28pyLw%g@Vbe!ZITDo0c+3tN35dO%8LVnX{Q#_<}Ou)#OVyKZY$i-!P)?}?n{&{OQU3P+bW=s${U#m=>YkTXJ'
    'oK^(CD+nBO&>IgD^kPVA^LRm)+Mr=B8@p;w$kAT}mBQ7n&XO+zpB^!8A()bJECZ&p=^BA~wR>CiwK~Ne`1Oqe$V^87X&E0e_`q6Tnr!gXWyM)&'
    'sU(}>QqOiHCJ$DEu267wBRm<A*5+9{y&Vy3-uv?xmk5;`1a!c|C8V$-JE4E~ovz7*er?87R$O^-Y*dl<AS>Zwh7*PG{JjLjD`#nipmHUX%52|%'
    '8ALR;i6IB^q2$#0VAD@<)IcP^aVKY#s>@D~@JS`noWM)m7c2hwN{k@3W`u4r$e10_Nc<+v)PEwq6(TgRO7--a)2B+_L7JzJ{B~pu0WeQ|rZEDD'
    'edo*_ONm%R3w&q21y;^`Cx(<e=nB!$l6j5&tJmE4t^>tBp#`W-7IyR8R^~A<gqZp62}a#7zm8mnr%6nQeq?kO$U_%T9m-g>E%Vx$khvsxmKv!d'
    '*%o^OzYQ2O#<x2XNcd@GM&BRS<9;T|)+lHNl?$v)d=f9=yh$;6oyu(?s`Wp&8a9etG&KQxdnCw_|LGZ$nxCodk~%<^g;^Fp49|@(Zst$ZAX`Bm'
    'jtB?&6A|-g0VR{WXtkehkyBVRrV6~$w|_*)t(sMp6Be@I1GSByghh@Ht}FE?EU0E!4a_bW%)&GY{6T=Gzi!c2c9)V0<J{k|5m*=q3f0Yer9bV='
    'gE#LOJmGwZD3G6c@_@^QQ^l=Cq<+V>mj`=(`>^r}z-_9)+~}Qe(!NOPub)xYNL{HU74$9{Mw61dM@Ou%PuQyF0%C&^+_%m!C}L2pX_M6wme~oN'
    'rhR*~xbMxJ@H{of?xkgOq^;Z4y<<{>rNgHyRpaZZ+9$d>CCMtbH{Z%erUlS+YMuq~D?BQ5k<25%#ElQ)x4y@SXcwm8iVCgnhuW1BF4PsL)|4|0'
    'EJB2fhW-)k$ZbrJYt3*rLS4}p)axx_Em%^W*H)4me2Kiyli~P`Aef6`E?{6bdH~x8$PU}m8RMij?4N}t^n8l^hky^uoW|UvKkwCjg`WBOBFm`Y'
    '%tuSkxi4KJTvE>Teo4c2wUYE&GN<kGS!L{%pke)rvVN8%v|X%gb3@b(GQ|dU0}YyM&n<-9D`lXoQ6{b{&b5s4UT*(+*kwqLO1|jJz$?^^%{W$R'
    'sVj1XxdMR`BoDP2S}pRcu4o3SmxwV!`_3c*XD+#m+t#$mLJZtz+aKY96K&<5cxuTp1TN0|e{G*2JFG)(w2gBiae{_+jZ_a=gs<n3H=n8G{JGy2'
    'zsm<Gbe;*!3g)Np$rFP}fJCH)fb2}M4=c4`LgBS<PvBxS-YNgBut8^gvGt1IB*Cy?<O2xHRl9V2-#%h)1<e)GrZ`wCW&3*)=k10G7bTP}Vg=`7'
    'Ny4{1fvK>`HE{K`j7N+`Ozq*c-my2f`7ViDGa^D|HWYf+;P60k<OJZkPa@7h2I+b@3FgeNfMM~{dzs{4+h(_7!yQF>#LO_7!p?I_4Nq8Hg;tuF'
    'aB(xqX}wDrMr}<L6mcGpJ0g67mHE=W0UwnBEth&ftqleH`)&(SQf3IQ=0cE=?kli`o13okA!yRgmFHulfInsL5MP>3+RHE-wOAbqq52cZY{q6D'
    'u2@aO=jqQ}ornY8SoOCLVEg5?r%XQ+HO$>)SPKN3ey}qyO-<oetZ&MX2%da^xptmuuZHCVW`$Ri@utJ^2aLNIyUQ>j$J)Pt&*fVZNN07T2g}mz'
    '@Mzec<WMm59MO*#R4RVXr!ATp3PTt3P!p`j9+q6e;Dg_PwBly0J+#;TCCCwTBnai~$t}CD2r*9--nlOsDa_SGCWB_eTw+Fag8KS|(85hiSfQ)6'
    'm!aS*-%Qb_O+oCEV2ZhSZ%aUmY#!Rfqv@mFb90Nqn}rn#T+{Re&D<#g=P%82?NJYPi`t=;K(^A?i9Rt_a;J|t!wvK4*Ni(M#DA}!I+Nh$pu?ik'
    '^1E8N&lQKv1Z{!WpyoF5+|BPW5`%Nj-$=y)Gj17)3$?$P@-{SWE<pirI<nCfn;mS|FRRW~uFq%My``HH$fs+rsmi?S(K8?y)(J&2cUV%pQ;Z4{'
    'g)qX{pfzIgpE8O=3{8N{sT8w!)vf%{*QGNj)96kG#Qq67@Q%)1$LP)vpUBC_Tn5E2Y@Y{}eZbBNhsa86!lrvCU0v<O>p@MQheMNXd*@PP8-!WL'
    '9WGB|+mae$oJpFu)4K7O6HwzSV_ZTjy;UeZN(JCROD5kj6R8nGXSBq@V9#k+{O|MO-d?(t+y>$pegts$o#SPbbzUSeMwUGnozq;|K6Dx}jum74'
    'hV@4{+!hsJ?sc)vQk~efbQk6hy7=l%?|8T0ja7PT-$**13%l<O?ry^4Ga>hJyUb6(AUX8ZDO@!AUZpTqn*{SaO&N`!5H4$e{uTX}h#GBzH<{1B'
    'm)F$I$BM0(Xzfa<+gyb$ZE79hT(eYzAGUIXig0`Xq+d`@IqgbdNR%u2tRbJC6!*`^fg3Shais$IcklaVh(DBCNAfZ?_U}OJGZnm*-|^*;0(gk&'
    'i5;a?$V*8j3{7_E$lZrf)A;2G;7f4cmNKaIPAc!Pyj}kes`pOBi*qhBiub|rjAs4Lhg!SdlakDn1feED{ngZyg(B82LAJ0Ew)^ZJ+8{jo-ov)$'
    'lRxFxH$!}xdO#c=Q0+)jiEdkT+l>pwrngR1^YGFWQuZ+g1h=6rlqw#ISYm@D)x*0ihtmAj=4zcx-hxe0x9f*gOt0hgGaqj2+Gi0Xliq&c(agnI'
    'CT9soMC<*N;mwm_y)VD)dF13r^32Zwm(y>EbeurQA#Q9}535-8nv_>=al`Mxz)XbBbZ_Q&ulla@n;EdkKU}fg4fOe?vHfBXK;7n7Y_y*VoS{dt'
    'Q8;q`xFf#V;ezO)slx?vBUgZMGch5{ov%HvyH+i874=h!ULzvhy08S=_u^G<;ic=x){%3_8tDVU1p~9+_pz=YPm!bq-qiY0>DW|!W?Z&e(a*c9'
    '0OBu)ECiI3<0>Umi(kf#buZexCxcXHQ3KD=W1PCqypG#*7uL{R(mWdre34+Izqvf95jCeC?j1W954%UmW9OYfZm;PF0&PWVYvG4fUUDyOvbuEh'
    'FY3Agj^fSPKEXm3hdF2wA1kba-Wqx-Rkzu?MC58NFh9opp)R(gp7b;N%QPl(bpj(vsy*1{Qo*7sM`CCd+m&s2uW=xTmf`aov%hjtiX=-{34;{H'
    'N37R;s;l0ygo1Dtt$>BbaMgKpu_$4@&~NfeA+h1?n|I8gYJ2F_xixN<xka^vCuH_QEWv|Z@yzD;wxt{~;4s(?x#1&;6!YnvNA8vZwqX*p2x|ep'
    'ma)K)#YWmIcjf%gcL&#!lceFFZpeIPJ|pI`8p82qDE^5T4u2Y8<>bJE+G6F1)qU_JEAgqQEwdvBk{UEQFS*1kto8k&3#i$+j?7YuNS`&$0(6}q'
    'sqd`Q=PUC`&`&~PP~zt%{OMV2G*wGl3vVV^PKb1stmjv<3hd*e#d3P6+tnAFTvu#Gj0g$LZ&iU0Q{<!z4n73G$|gvNzNTeafZiOA)nFkea>W>7'
    'fN>PZyjf%xYRqItKjqgf@r?}^7xqg2M10qX`;a~3qL<>e1t>hDFF@ZJ@fMl%WQ_3OOF8MK#x-Umgn6<=y>o|3gykyM5%;#IE+sCls{si+t-GD9'
    'tV>4{JY;G0S{mt(&XtbO0vbGUP}CKkhd964(VDZtCG=Gv{V&cT;Z^{hrtI5Bn;LiCEH%Dm(>4ZjjJc7_QL{yA_1>8i1n3AkKHG}u=mf;{ESghu'
    '$)-7kgLB?VoG`vu0uWfV&=0nYd*$;`b;3^WlE}W#7>lUP1QP>B&Uc1Kj};EIaLxD0b#Ecn*c>Ctqoz#yV<lw#+>#3-se&E^h4QPF^bzcJS_Oxm'
    'Xwg&gs7sLX%sQ7>J1`Z7RM}qgM3w+e($X5n(s#1R;zf7bKAvDLR7suq7@3wF@N^>GU;U^TEKuA5*Nf!+u3o_<66KEIC_WCmy@$(@?|zb0zRvT?'
    'IO;JPN=JOOqFE?6fn*LAA5+3aXIs_UZ;Sbx9i9QpVIGDzH4C5l`X{eUPQrXIrP?T^@i^xP?1={X+~L_i3HY9y&z+OJLqd|-PAvis=~GuC!i5Uj'
    'us#2@11yJFfYOlyn+o$=nLsnBg@ZgnLvVSoO_k{YKCkASVk0qD3w2czE)S$mrlB(~K#o;8+=c6Bcz#{qCMUn9IQFWY6tzR9o<d0dYWns~Y^`M2'
    'HCm;UzDK^5MKzMITpa4M@b);b&Fv0m6Z7S6DFuIgQs3Zkfyj{BERR6xDl@r;5^N|Isqa4a5ttrKV7#(Ky2ah}Xu47Fj>zVoHyn<%IfuFdC*TcM'
    '69D5K`?s&_`i{va7R(YB)#FO!SrV7u#9ZP3!^Kw3I)W}|OjC=wsh`WTu=ql1GG}@rid;u)x0*lW!%jC>&h7dwcb_Lt=Y>S(e^M8?6oqGyOY^5s'
    '{0?jZkn=YhR+!kM$e#z&P@3nGpDGE@Z$204f6Y&A#zz4~cC%eSvz2gwM;5xC;B?|y?P8>Mp({OWpQRq(Wto$ow2o&DRm~ULT6no<iZxfSGj9-3'
    '!AY`KlY;eo@pLzPMB=PLt*In^F&#Uz1|V_{o)3T5h42gP{zhV8f~d1?g!?We;tkYu5S#XEiQVRlt!3tr+WGv?b9?&I-9$Rqxh(-eI=ujWn`cr@'
    'YUkVdaCjZi41Dc!LpjcuhC>6uAI(y--7$1@H(NCNGF&gO)%lAz{6>a9J>h{@iO49>4kR?Wcq*;=73MNXB_s`fn59HZEkhGXSu}hI%h`CIDC_1;'
    '8UY$aCr!MNsqFmW)HK@%DB1J@=387ZF(igeps6Uf0>D<WITBpW&@=yh;BI&aZ=}`ZS&FIN)1|gBCoC@%k2j`Q6WpKS{Wmt8>5ADTsqR5tGuzL%'
    '5VWT<dFH$GP<rT6)~<Rwu1{QnM9rt$q1_)XIu_Rzm-zcGx1^0>d+E~7^s6lmfS|CfT>_%l|4UHYmGea`W4_zGLW0)r&_H^^edwRvd4MK1e<)B6'
    '-n9B{i_T&5vp?s4{mc~SV{tG6(l^>$|4_ySIRjt&>Am!UoDgY3vg??<)RNACZ%%?sD)=4J+ZsIv?<bjnmpKJn+xXAdA7MK>FVxg$XSUHxaf1wU'
    '>n9uC%<oTsU6YP;FR`3f6D6UvviU&etxKS*5w!EcT)%~P36rPZHU`|@xkZ+|;@vpsJ2w^!(Lv6cy2;Ng$+4bQ7*sIuP)x3?ASr>sJcz>Y63Wr<'
    '(MZzxJyd7M?W}ONQ17<oE_H5=Ae@m1=jYB8=+$Bz$$y5svfe?LNd|!qd!A&oBQFg&_qjBu{<;%QuID=+lEi%A!rE+YHzf|9?YU32XX@+KNbc#k'
    'EqV%n@utovm;ZsW996(i&k8|3R=wYInVf%>4l8^8>#c(T1@d@$$`7j$j(-jGZCi9!Q}bbff7yr_i-XH}!Z@^QIZMw+?_B|BAT%a#^O+I{Wqa%7'
    'wP*VHAVx=~_9i(fA_ASA)pQuOR_b>3s0g}&uFP<r&q(0-jahKi(bml-IO2#^<Dn!GLS~-W1<0iL6_p}%f@v0Jk)ikJP1Qoz!9-Dhn#teH@I}gW'
    'P>UcKM`&TM4N!t462CnN86#@oozZesbD3?<HLrYsct;T`@nqCf*QJYY$G2<0zdenW!=qVy<6rSkJjv1&#GoT5ct(%u!_8us%`DdLlPGdl2D9yg'
    '$)9bOo02Q_iiPH5^hYLo>f%d&{`cB<+L+YkIicHTu-)roX5lp$j!Me@xb-F>vrg&>jaXfNxV`|=dYDT20T|!C#K0_CUJ+*+&JCsdyImeVnFTIA'
    'kmNL!Fhe+RoSSj`9qScQx`r#pDzT|mU;-|&`FYRV9+wPD9y`!HYZRQ#!DO!%n7`R}?NHL~1!y9Sz=owCL5to!c6tLB&IYm-(;cKToK^N+l&mBT'
    '!wx3WAT%0|BCA@;WEKf+Ff*|GKUWVUow&n~@-RQDBUu84g9|aXr|9J{1#%!gdZ-`6RbY9K9|Mr}0kc{djC~Py$?qqmtR#iAzgt~<{{u-Ue}9(W'
    '^bTRAd($pj3+w8ewc3Ou$!{!=Bd|xMPD|EUx!0&wC-jxTqac1(Q>q-RFL5WsuI*p`P4ZT1-}y~hBJ>M-Ojwe@a>2QM3Co;CTV=?;m$NS!>ukvL'
    'F3)Epp?fU3>GZ)V!LoJHX~vFV;B)KSA38nEqIV1&7*Q<4S4y2Xj!w2weKhA%v~H7>#HP-dfu2siWL*hq@+G;bMtrm@`2F;a$D4SgtX%u$)57$&'
    '9yxIpCv=I37G^U;LDfmS=yPv>B82N0d66OVqaeSXiBfzc3#FeUuf;O#ed$|*!3jFnud`p@E6=B&S4D<qC&o7~CnLRl1(R?i%)lo~g2%uon*J?S'
    'U{5Ei1RLxaJj+2IOqE<bO;57=gzWs1y}p^@!XT0`=zbCLYo+Nx81e|bw%qUzYF{^IkTXPPbQ90>i<7-nJIX-D!Lme&52M^ku^x)Pb7$SIp7f{T'
    'z{_GAzx`O_K+RkSBP*FUqGaE=ew--g6E)=Z@)PS*KXG9|2H^Pf<C(%{kOP|-&`;+l`w`-QW5cCwL49`(xNM4l5v$D`8W%=oy%sW&sKh2|kV^K?'
    '4E!PQidL_zsfAI>yq58p+3fSloo7`#!$kT?L-~e=gY(S;O^L=VNh8T;eoWjHao76t^IkJS=h>(csm9-+-FT)bFt960?Ev|_Gr#6&y-N<tI`sA2'
    'zx*3eeiwc<7wlT1_k)WD=|nU%sk6~UNN9D<AwNH}7c58|3cEB>AGs=KdgjMTyLHQSXY4if2K#sS&i8nCOZ?(SET1ryk|Y^7gPWnvb0#{`kM5Vj'
    'h#$$S9!nGS^7^VqXm*#`8HE#H>Q?(*+xJesB9lw5tY0yo6SEji2deZlaSk5~ZYVpU?<(LTtby~mbh%9K{xCNk+UEYWyjI+aG_7~6c<80&-mY$w'
    '14~3|l2ah8<U<`Km_>a740g|N&P{kw@aZ1TYBu<u2k1lhx~`ZuaFQdujueEmI^`)eq2ri>(%IhjB;Y`=0mUPjpk>6bemI9ZJ9dA2Uj5A$#>qPW'
    'm@(ZIf5w$Ocrv!!dj$`dlV!*}mDW;K63inr(zCtq$-tR~0Pn2W7KwrSbXr4Qhdc|bFP|Xo-^=H*(>*s!hhy%f@-0w^pZ|2bTKj)rt)Fk|NMDy6'
    'YAy-U>CW@!Nh#$qvy0*Wyjpc50)!j+QelT>9|58Xp#GurEp?0h4x@7-)=tX!pWD42^0@#)K?sxVk+u=?>Bz$$vikV|tTv%k@VEw4msq$Z8S@Fg'
    'zc>G5%&gAc3e2uDtb!kVG&0)_Ppl#yotaCa07+3AAWM*BSMr&yaAD<*%<5<`NpE$mJV>%*LJ#xn4*@Z+g=aDD7*JkD|JaW2{qnXp?Gmjok3(H?'
    'Q%EX3ISp8a7=R+{%}Qj|M;Z`hL2`><eiGOKuUj%HaC{!LzwGxXG~g|&+ex?nPma|72@B86GY)iT)#VyGrOnvwMjWF<T}*$&@8@No|Eu3Kf0K}?'
    '_!aM14If^|Dzy2DX^Vk^gqa1{$!5E_$K41L2+d^J37yOKnRmL)zpl`j-)lZP_z={|)hzhc%kVWB>_YQRH9c?m;iFN{YoV!s4{;0*NB94dgWEqz'
    ';k-F%y514diqK%Ip{ypLZE<^d{~t$k*hY+I+Hj#WOJgRH2naS=cZdEm{mi4&m2)VOUM4MSo9zRvJdh{h&oC73GcmFVbn|B$-=(UOn#A?2>Dre1'
    ';3V0-7yBnWW85}#I~!2x<bP+lku9qai<e%^wOv1XAe<Dh2u%Ru8r`>EsrnzLm<o}a8803}=1z}<ON8gx84w^oyX(C1hc;613-3zhLCZy_2v_~x'
    '@BfhScVj9?%$H?`n$p-e#(Bo7YC;JQ<XiD8F44JY`=0*nm41EPoK4DJlme@{gNjAl!7L;rVQjZwT^Fg{QHf0B1~mBP?7*1v`VQBBLiC~u1dCz>'
    '#8(p{A_M-4G1iOoBrciH%u(nwmyIK1R~f~SNZZB58<wme3bfOw*8|Uq)t20aX~Zv;Y-IjSWdn$4jKi<wl97{2!tw4qnriZkNWm_XOeT+6f3(0h'
    'x68{ntoe^FNoObG;3r|gmQJyIzT|xL=-B6hq63`>XGnoDYd6aY0gYmG9_Hr#7;)dbVqdwi8S_blvy*LCudc3BfqL*HqstacYasF=EHAU^h*2nO'
    'lNEO@iyMv!<`4N_3;+R$&ri)3NXCFUmBPzLY}mD3yXBjUCZ*dhl7&DhkkyjdE%5<ZMFzp4e>H^CJ0NB-P6|jl*@Mbd)EqgD680D$>-df{a0DKs'
    'd%LzL9u?47Fu>v`o(=&@L-SCv$W!f%*q80=H1?rLujbTGM25W1+N+GmLcYQfm^;l#HES~y!7$qPy6IEeN*hz@gVI~6x<}5l^H|F4HQ!?zk{ac`'
    'e}T$@^y@^v@EoHI%&+Yf(bb%<6I>uiWL?D}s?g}~ubMVxvg<<6;U3X|!wlzy0B>qJEOz7JxvKN<^@ZLQd;+*&LYH;_&;0f<5NoPF0)Ei_v8q5b'
    'j{ZO0Rr@C`+`MC*_Z^O|be-%<285Z%dz?R=LzuDd34yMrbU43;E#9cMl$fNdOb?DQDuYMT-OyIy3Z3ice&gq>-m||_zJpIxLDLa}A+=@l9M8~W'
    '_mavo{~?yt6<P?qe3BhTz#iT;<^nEFwj9K%#9hVZ`LzAUHw1LStM<R!y`DQoN#TU!bHa|#XW=8OY8E$e4Qiy<uXAU5Oe<OlZ#6QQwpH<R04~}q'
    'U|vTdraRUAcJaBt^%fn(ZD8h&@~X`fx+4)Qdbn(#7kfu5SP1aBj?QOXfM=pcdhDDvNqe3h%07#=vH|CT8i$p2|JlLcRa}wV&A$yM!XJHR0HSkO'
    'bqmGyy$~?54f|R`e7KR`75ov<KJJ9jug*!U-PjWXo-87&!Md&Pp}uP~JBkU@v7FCmxkeHa0|orh=Y2y*g3TjcZ$dQRjQLw44P=pFWS_Lr_6Xv+'
    'EciEilLdisW)?u(<(0Jg6$4Czlv9}^nBZHUQU?-=C}iFzN?A@{{TDosjhdn}JhS<acp0R*$}f!ojE^v#*E6{@|6Y1^M>9q|R<Z_x2Ia+QhFmSL'
    'aLG)C7%nIrr|uIFwVCy53YEZEVj$+*0}s>UO=@&<aQm@n3l|dh?{{A<Qj|DI^%e;@kJS@Zb}-0uLvt{k_qk`5cqLOi`HE~Qs);+P#j9|hKgWDi'
    'FF8Bu7~DaQ0gD<V-*vzEW2w>H3W>bMM)<KvIDTJFq|%SrKnHXEe9+!l$N{DRV)@!;J>Mg+wjr1oRZ2BLC$9ySIzfg6<Z|uzzUHfI0FIt!Tc!#^'
    'I=Z1xmVJH=)=oKPq;}svwEc$sdzhcy&nmLYQYZ+PpLOmhc|?JOj}>sdUEIE^&;*W7<Y5M&l%W!XScG`z=U0zEhnI?mj{y1&I$l7<#UPz?>?0we'
    'n;|=dx6p#k^Mr<gYqF4Um(SMPf<pi^SM2i{kXBzUu}VNVy2OqV*8M&docZSJfNH!`S9`LCb89LU)@WoH0EiaQ*MW2U>bj%ra0buC{VtE~;$UkT'
    'yYO4gt9{gFE7O4}t1L%l6%fz%U;ujM>V_02hmUSGQpN#4lNfGi$nA@3jfw*buOB(Rg#At1IX`)^E0pAkww3$k5atLqcYy}T;Z#1W&AYB4Z@I!n'
    'bc#G<4GX1%UxEh<u4TLUor|-X<A34(gbJY;j7}<Xsbt0k?Z)`SEHDQc*e?w#y|1$@AW(Y}0NW!O^?ieTGAvFgQ2Cd8-~N{oE+f#<JwK`%><`Se'
    'Cen<jvA`xhJ%ZDD#xPSFfxq}EX6gOv>Id_!GptI0IyGHM@&Fn@<v!<z9a}NEx}!3gJA?X||3O-h=0axL^GIjeLLf18rOb~4fq7-=zCk}d2uxc}'
    'P>(=IBH#QdL4C1k742g?^t0c6IpNK}!Y9ggb!x#R7@AC97_fD{^X_T8ii(A$%5jODA%atfsJK}ni>p<jn2u*mqnmGHAqjexgztX)U+)}w;FD2V'
    'tSA&1wDY8@6v{G6oC!OjUqauWoE7a4XKb)$#rZq|<(d#Y61i#^tA*&gWU%x<A5r@!C!CzY(CBi=V7%fW)5`Kph4Zt1d*LD^_L6cg$oVtlMFx|='
    '$kt4N!{sHqCqnEWTDhRrAA3;m&+pY!OtY2yJ78FIf6Ul>ME$Z<6XrUfxCCK)S;*;dqxd+It_X%T#!^*xtyAYmoHGZ%Kj4=mj#vk}a_;+_s<LI0'
    'fm>)zylFn3Xq4xjmK3RXG++E#CyE6RUapgrcY`ic(iWYL#t9b$q%!y6^M)8^)`rxk#GN-}zq<2d&k7PN;1xw3oxF*RBhq|aKBi<(B8n}4oB~gZ'
    'L4*ogmwd~CP%Jqvh@c$!6X?e1Z!-Lp?RUSk$<YB7D5UwY&*I<%FoW&K@CI`@uMNI)FzdwBS*}lshs0e7a&D;<&K!7G@*z-@u`&bi$jRV*_iKCI'
    'h1N+&hlMiY`*5x~mgzwSJv+bmN}}@Yq>$4Do2QL=m*+Lw75vj5CDwJcKvLt^jdFl6O0K-K=w4c-oCeJaIQ-R*HQ=JmwhEL)`G$TrPp3$XZ!%}D'
    'k7$DVbG}PA)-s;xW)XDTk>jRa!31vUpATXfE<f*jNnO{4vl$OFx~dPuz~r<7gCZ**^RO5U<SA`NbmH}iI0O}Chyr;+g%+y0ST%)1Gf8q)BA=nf'
    '$^vP9yS%6UV)QdiYOzlR@o0@8N+z^(+y|t6YG^rkdbeo{w(Lj{j;o#P^oPF?*t8Lv#K%90Sh6b%F0@3^?Gu;_=-3<-$IOQ%(=)cA`Nwmv>y+tl'
    'JjpfQYCwdZVN24>=A+6s8}oL#GL(@{0m2uh%M<m<eEZWyl25mNf|@yGS#3`TtB32zI|s>(IX+7*N)mRAk?^)wSKkCh1}am`1Lf73dAH1X{k)eL'
    '0vojeUGn=29afHS*I!}b%oJw?T;7qyVgmUpV8ef@egj70ZnY!rWt}9jqXr*wn9Aq=V}V!Wp|qjJE;g#{KC@ED?so1EL3Cb7;L&(Ui_ULp^mT+w'
    'rE3bkG5}r<@@2qq{S#K>oIkO!>b){I=kbj)QDps@X^r2I!_$eYmUp}T5qCZ`OtZpPZV;9y`+_q3T7jcn?M!%ui-$b;k&a7(fJvt(?u;Ai1MhDU'
    'gXsJ{%YfL35Z<NNag)?Kw(BqVo`W*^v6Y#)06_W382a+n=P`58ZL;&@qIv=58+e4jir1-^&|*fI5BsSbBN8FjNx|k~vV8e=|L5DsyT@K=bSwi>'
    'TFNITeWFR!i0F1dV1k;(E0vbcE2~7}0Ssh%JeLYX37ooBcZ!Ypy5A@LLX*RLd;P9u+pXj~;0Z(gd49y8N69-Zp^EHRX(eikwm9*!ML0ZAoh;ud'
    't(C^wRWCT@6YpM$nJ;L5^z`k?3yaiZIV=46xQZH1(71XjYVW}x=H-WkrO7y#RVHE!??+~zT&ZCc%<bYd(uRm8^3y>M5Zb03Cs6JGx&L9J!L=d_'
    'r{_1H7`l1{kSC}<Kf(M2f#mgY3!X$ZYgdg0gig+i$r*2GOmlX+OZdFfk47U(uu`Ax>T#^QXR}s+WdO58IU@@N0%z=+H#y;WASfK4d&vugCSF0&'
    'y<sO?=cvWZkr;H@i4|LkD7mwa{1(|OIeadxgVv1KnMO}9N<QyJKFF_$cD$@iFZIFgh?;SvrPnO%3Jts^UFcfqYx<>8W4r6i-Qnl4p8Z~4p*0Vx'
    'K@+hg7y{>%_%H!QQxNME+jdPnOX(yZ&y}7=0}f&DMEnUI_Ca&dZ@L24vlMqt^{j+G)xVDQ_D^!R?jqp-lStK)vV)IXBQ4o~<UsUSbNSMoqyL(R'
    'j61u9*HxYUC`2AH^M_5A6kgi=A2Za!R;dw*9<@FD32|%*NF8VaBkDrDt;#pn;vX2gr&yD-hL-gxk;IFSg3v94?n%%<9(Hq8A4&|?!bP%;54?OI'
    'Vteq_To(zrd5^n!FAowE#SxlpT|rW-Fe8I9?YL5ATxW)R^7mvYrVwjcXS7&r+Cj0>rkZ4ara&wE_fNE5iPPHBgXft)#88?&C}uezT2qCzf%&BM'
    'm`B=PeC}n?UX}21q?=v9kEiTgY{lfzfaU%`h;wnBJhPAkQtV7TNJ1>;y*ysCGZT(0F>a6}LMFV>z!1jK-Wa1F_CWY;&9!c`o^L0r@0-drEIW_C'
    'kxbv-J=1BZoWodRIRVn0r4>yuB$OCJ!?!x~e$PkGU;l_JMOVmCp8iR(M0t;I9tiHp0f5ny8`*;g_W1MgKM{K-ty+2oQ<zYdxmRdbCxx;z${=-E'
    'ZzLh`l7z^8uO^pfhJxY@nH2Oe>%5ezWu!K8nEzPki`C=ZE<d+Dg=6@_gEtBusD#4`Ig7H`0DGvX65-p!6}qFLU^|_sk_f+R?tfX-f8w0XMY(YC'
    '(8aoGbYK6uytT*d?Fc00)pq|xY(&orbg5Rn-FyoV01FanN%zp#BD8)Ir;8zwF)U+F=Ezhe$z9g&kNbR<bbY)0eD_Awj1tN;mzHM$rp1BPtegGc'
    '2cVMLKR|l+w*q0-CX(yc&wZQH{{F|?6?;-NGvG05Cn7S7kTKp8<};X%B@*g$7B?0<9frFESzvj?R;qE8(EgZf>ww$lZ#O_^-7k6SCvR7mWGS}d'
    'Si6dR)_HJDCociE(&uxI)#8qS2mB|=8(cODnz~&2caC_=dpz%*VRn!0{EDBc(ti$;^8M>4VXPtp!66uK(Ib1)GkrL1MEwqz#u(+~9dM_FjGRf8'
    '83`ZEv?h5f$LSC<%mC)WfaJd?DcCMr>hHyq@}%eQ-WMl;xk%5gxl2f_V7wAcvee4h$wrHQ$&|%z$NpUssV(*E57Az&YzTTK5lHV`5(-f;_S$bt'
    '<yjd=&J-G65(I%xh$9VoUY%);QD7F%F2rjZMmM4*hB$rIIMPa(M5V6f*s$2>zsWJ+rb-3{Zt}g|`_S%TS2d@7VA`V>16t=0uXCQ|JZSLERL80Z'
    'XmDCA8F~VYSohNSPTJDZlKP7(r_IgUe<6v&DfV-F)k0)~c|7zahRo&D#K@R{o=wD2sD`tyXi+R^p_4Lxs-8P1WsEO`pL5whgP8z=HE$%TnM;wJ'
    '1AQWrvawxVeOoylBUfcM&_r$#aLx~t9Cdz8>eE1pgLs`MOG=0oR;L_}5g^fPo_3Cvy*FZ;n@=D`c)iy^{n}o;)V8J$Y2!XubmK9dI}FYW(JFw$'
    '9RH4&tY<ZLw%{S)7a{L<a<Vx-(o;)9FT4!;1T^`W4c>*KQS^I#&Ad`pGltzN90|kDLnN;AqEn-Lub<-hMS!WL^hY`8X)>uN^Sz_)s|4to;RfSm'
    '+KO|i3b-ACx95JL_8Z#^>Yx9*_6*%qbrA-g%fRAH%Q-OIqo{d<j;Rrc*v&&IgWZy&qq0qId<dw#Wrmk++h4-i562|?cmxYqyt2fZEtw#2z(y;Y'
    'k*0T)WmgKhBY8TU;R)L+p);htotE`TmSl~&zNL6++qGpX&6;g4V^(wyY-R1A!xw{`B~)R4eWHp0M3G--E)lpX^A5pUNs_7Gotjl0*L~#Fs3C|a'
    'hUsyxD*KHWDzsFt()ysRdq%5_FS=ltf&`C9zwzJ)iuwQ5O3X07n+)LRL=pyNA=o*OSj(qO&^Ym6<M{gT&3|)!r<i$<=B?00=6x}66ftMnj(>vn'
    'XC!#Bg@;tu@k6Sj9eys!itI$IHDVoUT+sK_Z2WHB&M~WG`M29Au-%xJiBh@3?}WE~p|GqVD!;~7(8!H(V6QV^SoQBnuZXdSR;MS_33ODcILkd^'
    'A^J1oX|I6}sWboY^%JTD({v;idv38ObX@5xRZr(V53Nzavx1_BCSAfBK_=ywKbe6)S@A0o${i>G<?1JgzkqZbX3Ec(uirpN(hUGMX5};04av<s'
    '*3Ccu%3)q4uwfT(5LK?GlA`y<T8UA4?RP6BM{IdX8|)7Y=n&A!3dTgdT|beIQ8jiDz&BZT%PO6m25+#&s_TQt^tc16-{UV>xSl9c0o@@mGtFb#'
    'NBt{iQl~bdqj1PoCw#lUKritmPK@o2!yFZcyo4+~mRVZE*7WHTN~L@EB!k6q>;*U_7xU<`92_q}a|kmdy3Zqy-=w1UdTm!vrjgd6COnS7Wj=ey'
    'Y*<$aBKqYgZ%!DiEmppdVho!6N(w@>NA6OQO9E+1x>FZvTkBcEox#@r-IHo0J?sp@yjsGvpq^w`MLWuGSw9hcALb%Ys$_(HEPsjxdInF64{$ob'
    'w{U0$_eP!C-eAKYf#A>Y<&$L@JcI+a0NBP8=qRG4VGDNF%^}|r7QI1Od>`+^zD62jG{ao@=V`NZQ*isTmf~Ar`B>m_X_edM@1zQq6xj((V;6Re'
    'Jd9z8QmEDqxM)K$PDsoJDAN|8o+B{^Xf4aqhxWB2@vU;7zz)uMEgccJYs*!MQZwDei`Fnu#SUu8WXa>qE8?*`kNoI-Vm+xo=MjsN41l?+tR%Ot'
    '-8jp@>ORmM5Q9C;R(AT)x92WZ-d&BQOhUmsqLZBq1-=XG(OYAjq7XQfd3~M9H(;oqR}yJA-={n^E66VKwm1IN5RLQ64ARQmqa6J0X05RDgudeD'
    'n&(S$3gqMxRp~_WE(RR}iYE=6N-AX{QfViy1RiphzS(u33?NP_PuvFhIA`o0_VOnDtSip))o?i@NS;4#4Jo}iWCl)bMkT<CHnw4dJRzx;lu?23'
    'u#lkc^~5=H?i@^sIn%xYzsLA=<+v=4cl!uQl!WGxr*@O!tN;Or1Fcom#3;5LE5^vZtPyj<;pceLGoTL0-(6AHX&pO+KZ`bVXBD#vsUY|3ueUE9'
    'H`c3Dr{u{wTfttUvT`)jQ=p3Py2TYG2iuaEhUF}B$;7EX+HNSkp25D*?-@&p2<)-6{T?;`$t&+<9*n5C?lzUa%*Piyhx-2J-3V`@u&caDSHvEZ'
    '+Y5R)&mf^HomUu&2yw)|REsE6c$i3jrR`U@@fg?~L=CdQoZ~*BEfgN89_OMr;JP9a`QO#cyQH??{|VDZoRa=j^sBFotR$@c?+?v8vaLP|k~|&7'
    'Ec40TPBT5eO`Th6Px>e@x+}j@p&vpLnj5C|$AH6M2xnF^8Msx4GY?zT`AAC|^HPtW^QZ7^X(^e3adinQfE2FlXOu`xQo6z&t%BMP%8JIFkOt0?'
    'm^hZ}x`sAtV7eqiT?3?yvF68S_jw{~DrLKVqO?&TGWtsVrGWCKP@Pxq)<Kk;M_X8%)<niX{^*3wCsWW54tLA<pEa3?4~?++p6T)D^GGzprMvZW'
    'ze(EUBuB^}Ai49K&3aSK4_q$x;c>{<QF$&;Z#+OzWj!){ZcK+be*OIE){rQG*E$k%^3qx`v6uF%1pdZo!}cDm8N>O)F>bEsSLCt!DW|v)>iDX9'
    'hNc_#i4S?~vzYRiiFdv|1<OGgPYd#-7Xe->?_@qvzgfoZe?i)Uc9Dn@i-0!xQ7zMrj3>5l8thgUnbLDNf1qaOpf%)(7<%`W&=t{-V+DBv4S?O$'
    'klI>md*j{x23UaM*yhQN?}4s3rquQ2bm2xEY#^jX_ey&!0_|w|B(go0W>I^lpB8Z~P5Clp!bIqEk!+CQ3q5sS+Xom4^N!G}T`{q699=Z8RCwV^'
    'KQVUevs@@jQbvpjQ6+|t?Pvscog8)P@;xOdHr3+m$tJrw-T#Hawtu6<MU)O*1*<&Kn{2!c#ga**4o<-p$%OXW6Q)KIU}L&SN_e*as>`b;sKKC%'
    'eb7Wv_MGfFeSi5=deLtlSvS1rEiBp0ObgYH;Dkn@)g}Ds0%|{`r={l1uT<1bHET`fL3bvBiUE}qAY%d^!&!NMWdz?KafDnqa`9XMaKi6S`*g6;'
    '+$8}7;O@@f8rngX(56+V!uiT}3YPiw%$j5_hFo7ZPlJHt4wLj=VL{u~J^dmYb(y*~6$AdW=+&&VVilf+prwC0=Z6UWtH^AE>Y^>;V4bgIdL0WS'
    'o$-{ZJ|U}-=sdcF0leu2ze(ba*qhScW5%B+@&L&+nL8!ESh9>2{e_C#EPT$Sqeeneu65k>t~dtjXkK5%d?A^2228g4?=)jYiTmw(GL+5cDH=b-'
    'YUbt6pk2RE1y!gFs{SjMtTi<POOdh8){ty;5(i_o@pHyokZI^Cp?*NlY*$}Vsz!}qFU&I``^w{fjg+esKG&Lf88R+%U2$d0UD1V35B{d=#6>yK'
    '=m+fI=L%J3{WbpP4~7!jE^mxF?{rP*^FVydFg9o`Z8s-S#wUT6Vh*na77N8l*aXG|;RL!5U$ajj9p=dhuE!cRR|wz5fm<j3+<yF2JtJvKiIFX4'
    '#?N9)8wZkEbN_=aqhmSR`*Fp#OXnVmzEkzxX+U6|^MCTL&E4^hv`*ji$|2@;%-DYZ<cN<@-)-J=uM;hyT)<jNQXf%9OQ>B(=5fLTVj1xkI+^l9'
    'FxBNh#8fbc67Gu3M_V{c5c~t_QuA8pwci4}pB}7}m*VtI0So-rz(6BoFgxQmn=4{Vj0S796^a$*`rkcp#P-gY<*npKhHXdcBj4e&F?ZcA@Aa3^'
    'b1XQztmbo#YF^{C4kStC6)drlD{-3FGGiYCI-0IKfs|u?fRH4jjIOApC9ma?_8s0{+vI!S81ebwKGAv=qwF|Yhxs}cRNOA~L9nGX*F8G7`F#`k'
    'OuWwwz~7t=QHUsEZ{z(*@(@NxAeYj3dfk@)bi4o(4q8DoOLQq6`JCwux8nj$*0596u##Mh|4r+vJACJ+k@9y^52M)xu&EAYw5i10klzcZ)z!2_'
    '^upRg0S131883T29l?Ckl$ImT<5M-jlX&X;O8c1mnXyb+g8h4z0!e~r6J8lek7JNYYIH6Cet>U&ICsx6k__1SRzLDQDWGexVucb{)TwkY0qr@`'
    'kZ>4^C~)X`*1N~<$m|7hNEXM9FY?Hn^D<P9?doYcoaob5)MQ==Nl2QmS!}+KFVD?KjU_%Nyu~+t3a&EJ`xRkLRnDbp=neS%d>->hW9>^=Z~r}S'
    'ee=VEgbcV%LX?u9ijES(y=Xo5)0)j`;2>F!THtwRX#8+JMw(!v=D#dW6S8iu8z|~eIg}iX38ee`x6gRRaHqxE7G0n^;c~V+!!98$r#_W$TMff|'
    'bT;~|7g_;@o(5&8h{`G`S>UX?+*&c27xJ4~kH4FC?VBLpo=}yro;lp$`c)V<5M4_xXh!%e{ct4a@~l|;%Tby;femLhwlk8Clu#{#pjVUYN8TMu'
    '+piPsn<7q>lPvZHhe%{#IT8gXCD*iKzC(`dOo4bYPx3ZqK@x+s$JU&FX{wNmkZK$u=I6Fp$>CDepIg7{n<3uFbgyXR08hH4IYLI%=OmCOTa0i|'
    'D`S3u(7pp4l07?%uHe2S8g4-}y=t(QV5$fdsECtN_PpEEmkjZC`@kcbTthB`vjqFCn7Jo$o*IS6n~g+ubge$K)57XtU-2YSXtP6W-8qJSsABKb'
    'CXk=w+HQRsZWQWzjUK+CYh%WtSwij`KI~(?b=qG_rC1HS?w^2FLz1@4Df7|b4h4%xSbPe3gz{g-M@mw+RVVE>=oCb0IrQU9=74rzOIF$)ZbHLA'
    'c;f+dx|Fb_FyMtI?T1^qm&u%ePgom72R-rpD#pygH;dJNw>NJI_lQqH-^ohr!ll!KYYvvRn-q&t0*5juFj&xjVrB5}5J!me&B(39Y}&B(=Bl9K'
    'c>5>zC62iM>;C-4h}-NbSbX5Icwy31TNEUmHSWJLj@fsjg756hkHx47$1p}tihH)8W1Z3>r?D72@1@s-<aM>}C&+ZOB@3{dXFj+BAB~nHxp$Wa'
    'mBcfOb~ZKYK08E>m7}3SM%QvJ(Yh3M_!YZ>smVM}k%MUcaw)XUkM~Uy=Y<3ist&CQy_h@4L6K(3hTvQez!P;qwG~N`mr17}Hy?21t;><}N|&+Y'
    '8ijvw0(l^~#G0e)_U>oG*`faE^j;{oo^Fps2V-mJ_xT$$<h)sVbo6DX`DVPwHDow8&}(^wgo1~-AY<6XFb04VSA&(fj{WwpAaQ3WcFFlNh2skA'
    'Z%w<TtXRiCUS+xEo>Tj1KBVyx6VW_L#?`VOKvFc@4t#w2;&@1xCwx8tnFnp3z&rS`7SUIE!A4|nu~y$29&;DNUII<3pxMqrm}GL#yt}vlTPQPG'
    'wIe0alaHY=()nsCQ$t%^W!3fpdIX%Zq$7-U-m|ceNVErkNE($wZLj3T>bsut6NLK?HqXef!I2bzS$k0RSj*7th(YH%J+Nbp;pS)EuKy;@nYC>Z'
    '0vwSjr|s-(Xp}lIR%+=iH{K>YZh}{+Jd=8d6_7A*Z>`4M4XmHwJ?zxXehF=enz!rQf5tr1%=gA_+u;3TE%Q|Js=E1dU6+3bAe41BH#^^--riy0'
    'UA!)hFb4E_*OSuaAa*2b+x~5d`MrFy#7RJzcKNiOLAm4-Mavr_wS6V2x2*V}gc8V-j3?1pcq^^4=TBVY5fWKKWUfUJGia1;gLm5ZS1?iHyl6)P'
    '>*ld5`9U{a$n`AmowSpL`Wj^*dA1A}ifjqqNz*oix)eV>aT2$tAWn8jx2G29L)ih&_p|@LtNMF#%J!yp!rK*%43`F&hd*)U0|10w7zJ7L%StsS'
    'X}U=KdwCY7lNlTrAL2NP^B@+u<pY%2?pC%>kX<mt>1W0E8eto=ST}B`jJYj}U2G#LM+1>Y4dQri<m5C(tbN8RhgZm{+|hDwFbIyOBbiF<IOhf%'
    'dB0HbkW8z-gD@7!JH{pivv)w#tLZjh_X}YnP<?e>{LTqwp+<P(3T)9?C;i(D#JT^X-ZA4pQ(`{b^@B;(WzkoW#xU0@%Bp$9dh!fvK6PwR?Hk)2'
    'PCBE&6%kjC<qn-uemfa?Cx>thx^6w!xpy>LKyiEbHgb5sP;iJ|syb1ff)w&gDo8~#=y7#I#uE>R&Hyg;zScC0e<{RJt7qD^kWBFkJ+aDK(uY)-'
    'EK{t_vnu}m2n!9a@5nqN)58>pfL~raVOtu0NgYHGd6V51eplfMosSGzqtj>LattPC_)-FoG_XaiGjll;&gXK!`jse>Ae`^i^0GQXqR68)-efAt'
    'F6%Y=x4PtY@8?a5ZUr8BlQElC&_F**{OuROVFvnmee9_0Aluaerjohn@sjL0H}83xCv>Ut(TFWg>{IkICD~b`X_D4l1D|kcidXRp`t_9g`0`@Q'
    '2_ijsfFMjgp6&3nLdEqaB3W)h>qZ`#mX;LXqOYuj<oj#8cqz@+(qHc)_pbPlbFXp-)Es`Prh6V#fH)G?;?)W7?|(i6OtRfxY=JJ%{1w9Qs_FX@'
    'WY$E@$=+%VJzW5hW^ll$c2yZ`5CpM{CLs&$l2noXd&Qm4YSGV}!{)vQgrs<ezAL2%hjb<C+3<K{rTf&Cz9sB5{`{CqYDR-|fueqdgi5yeeCq{i'
    'F*R@_nin{eYAI970Q78^U-(ouwERjGYeq%sm!5J|m`+gE@0IZv_Z%m*wx$xq3gozuDtN$1hT8S46mBOP(N<DZe2=a7x}wEvy7!R5Jn1zni5AnH'
    'Oo-mBorLs^kUD`rqjo}kK8Mv91LBtO9WunzLNz2@YRO}J?G+Epn-%@Kwi|FSgz7o}lAmk-xXema6>b`vdhLmP^v2<-8mRGb(4m5lT~WUV?C!`!'
    'ld4akgMOz;!u^p5Ss>%>(%ya%D^N;R_vMX&Mfyr#;9-w45wEIP`r;Y=LMHxrAT=!Pw=6d!SMb3ii_~PnQp(|+jqn5KLUwOA|6bhS<Xz&K;jtRs'
    '&624muN2uV=dC67y;S$}H=K*FgyWF&`CD^L{fmJ1DAd3s8-fHsL5Rv`cH8?`X|$rn^`}j)OT5Stjwm=2^$i4xcQQWdzJra>I-N235HiFlAaLB_'
    '!nN8$J;UkJ?61^tWRo+|Tm9XCc(zIBDZ5JXx;z&hfks2qjiBTZMMGLmhiO@V&V^w#gy6mQQuTy?$&^0OqeDZOr$kzJ;p6-S!W6c*{!R?kEH})t'
    'AP1=n;`DbMi7n)gtl4`f03)+q5u7Xg!y(zx<}3+had|WtXB9-HksuA=vD^7nkjZxSyz~lxOP4yXCFBSCm^cX8uhL$yy~`IoV-I@1DP8_0b_yei'
    'vhu(2sVV>Cp<Li{lT6xW_Z6o8_X7m3IN$3??w_PGUfDy^i_^`T?@O%dRNaK$?>`^G&#DwV|M{P+df4rP2#w^$u_W^_Ca3@XtzT99KY`=Tc&`Kz'
    'HSc!#jx9`fDLE}H?Kh=I&U<pG^}*w_actM$J|p+b!mEhr(cn0;3bKiC00vN${jbLIn>j8v{2+$zR8=K3m3ap;p7pdLs+zjF=k7ijO46Ski4EH4'
    's>b{Fos<F#&0Qb}D1Lw%Mfyt|=n33!Kteg3K6B1_Bg*>dX5$wrb1qyS2BJ8+9%gjQBKF6_qsUj!l~!}JZ&uDs<`P4);CHAMx+H{jzw_pA;`m70'
    'yR*tTu(VH%$0E|rUxR@WvwEeatoHPC+~VGd`i1(@c&{>_eWM2U-y-xE+Jx3}hbQ%xYrA?v7m0zItp>^=3e!k@du)62KZ@8vN4tcEG~O8EUrVYR'
    'OiXO~Nk8c9a(bEmI@0qeh#(U<4fFZ?we9k6icZP`ggAxiyKW+38JWrXtoa(Tpo!y|z~uyUXc5B8&Kl)$)>$auE>R5+mGqnbkNp@O1M#)jfAO;I'
    'e|h8Ir+;v87~Uhsq<xS`$$rODL082{s2dLrM~gpy-;>5hf!hB;=|-%m7ecetSm%V_&D+Q^(|<3Xl6#&Ttr-4_(65?=HDh#Bf{+H^r0v21!W>~1'
    'E=%Ctn>fq*mB7{9eM*<M-g`Sq;+aTYKz<lR^kcVAaOK?5xlVANJnM<~LOQf{AQquz&efNT;|&UnyWXj~M(7F#!uPov4=q?&Q=t;%`^x&ziZ?1u'
    'TKc)Y{wX}GJ|d-8zDrg>jqV6~m0SH=`=0SPcPYdxb9k(PC~C2y){9Q1e?uScSTQwiGCx-E*kIb&uJ7oX-Vc<uz}h<28KTqCCG~_=Chz~;rs9En'
    'gi{Q|F^p3)P8KW~b~&{Jg!1(oqdUnD+R@SP91`n)Ucn@dHwpvhI%t=Nht48;X{m~5n<sFNGAip4XM0bE%XR1ULBhfmU03QcoCRGesToNc9lnqk'
    'vo(Y7vOfYz8gE>GJ%sc5yDb~6upV92%h{FOQ$3`=PFnOjQe>|=(-WvDK_*Y)Tbzi_HHP<?%%5v<B=AT!1L2AqF98%~#qvY9FA(`n#;M7<DO-#D'
    'Oj=+jvT>@#zQK-msayNX;)4Ny<Dc@NCYcWNTXvK7Hz~mH2T1!@%i&1ZOS6E|5n`5TpQGgI*?yIJS4^OUH5co2l1+FEbbcH?X;C~A?Dk+~I_YUg'
    'X^$to+R<*i{vGFfRi2X81Lfjk&-=k3W?*1v7f=a8ragEi`ZV%&2Kg7@a-60ybCI%2&wH=(&dA6p@W@EX`PwmdySmMfdP{Br$6>7gAQ#l6jstL='
    '90uIG!g&&7uK1wybmWiv%&P3i*Nqa>?MF!h1R8uHS`FHA8~uFK#u2U#9}kXjRS>gKJu=Alr+C9=?FSJQiV%=Mkgf!*COi=_UU5_F*cnUq79Uv('
    'srei!9@1Y`{%#+jX7Fb5p@&G$P9%fjh6kuO3%l57Tdm1(ck+i0a_)GY`L$GaIhNQ#qdjJB?4lTa>U^@|&ZB)k_h-+;&l7nB3Q9uXvu0yxJX-(W'
    'iITi;05u3LSutRMzOC;T`y%-{orWQs;N2m1Cvki$H3Y-wNzTi*{ig4$Nw;#22PNhmCBa;`qWv`TuOP@Rp|BK{^bW4X%JHU~D~(>?(_Ct92s-1U'
    '?psfK^J%=Mr$g2Ld-b$^IUgxuOJW7Dh~u4Poxa5Zy#!<CED+*zM?7=hA7DlN*_nH$!Fh)*&Bm`6IXAVuc?KV{C%co!^TV6{rj3_%sL*v|>pI_v'
    '=#!zs85E6P`w9)`F}g?SmMHDeh`46rY;;7=oa?2&zP^=b2Jt6jEWtEcRNp?pvN_O}b^E{kKzI^EaK*DY8Q-=Avufm=9%TdZNE3K2FMxR2=U?{3'
    'JM>WqJ2kJxi5Air>$>*uzTBMijwLHw>yXPq2U4SD?g6F??}%~Q@hz<LcpNieS%@b~J`GWZHFFWFM-odi#J;R*d?{5~cgru|zkCLthP5XPC}RUj'
    'b4eRqcU6TQ@`>_^+6xXXY$X>vu>PQ83@6VOL|F|3WaOzBT53;2yyQB>J^!Ci@YNj5?WzZo&L;XS2{mDdY0l~Y{KVLuEAqF=v<P+=<4O{*+>5pL'
    '3mv_fG|V05f2ezuXty3R!H?GP8#_+4-2_I@EhyQ}&g$hzY7^Q#mW+;%%);t$iAj<qNc~^hW8r&CaUwm<UL{t6&y^>PD2&5jfw_NquRfnKsToh{'
    'e7Q>!#{{+;=xL|s)E$-phQd5>g?8RUK-7gX;Evpc@b)!Ef3>GBps-V)oO?#yGJein!k<0)nuQ`hUd>xlZAz(mzgalp2o=6`qG(bzaEb4u2MRd9'
    'A=pRFxWoJ4$$06$=4m}ySN;o6$n$Ha{br5N)!uTBjB!GGmggg!ulcMxRw7HhcK)t`-7Lu-JB$wuD-t*F!)R&PcX{WLiHQ_J(y##LDx@&2!MASL'
    'f7YT8R0d{u*3{gvCkO(SXy;wIRbyqsj?7VPOzy^DUhCNf*JR0L<q;F)b#UUt1JpdIG_TBN+vUCa_NXHpLfld5&C*EEH6{65=`FQ0A=0)KBA}K<'
    'PcK$kOkQuGpM|$aBz1>#!y(u>Z(|yeSa$7?eH0INwxgsIDuBG?)P7LJS-o{mB;<_XzOIPyM#S7WYDd!5iNNwQ*msn{#o^v~lXB_Gv`If3XklFY'
    '6ELE(HEogmSb^%-*ht5e+J@N}%Yn-Hio=4A*eoge8PvI2k`G99HhyPe=pEdTz_XH;dJ|}#nTwU}6GZY5Ofl_nlyqiUe}>y+$$WM%sN~#?ooM;^'
    '^?VW*!gJAhxkkK|Z8d8u?K3yIOzLaNfddcHck27gpPKjLm2)LF!6)6L`J165I;WQrm!ilFyQ;~tw<A3#W4S8|1Wx_Lc+$D5Uw8tq*d7vLFZ%@W'
    'g=l5_0843m(vR~O`7(VGC3DVw1Kt7ol0Omrq7_v_Dyo&9I7QejGQgdJ2cAEXb`e+Xd&Q@HMe;TekOU1BWcyE0;%>b{-h9-#Pn51_s{Avto;9(*'
    'OxBc)Hel*3BmQBSMap)U%pPeWSXh-3h9wBG>0xQn^oRvKRojaYrD-M(vxdNgSy^;{2Fr&thn9iA^vSYxfEdiezb!zRf8Z6Eugt|$wXF5c(Kj53'
    'YxN0k)53Y^8s_TrS=yh29<4uo#@&)B{}t5f#aU$ZbxIO92Dz~wivPtId2^&KNlv`dcZ*xlJ}p6@Cdchfwjvp~^st|{_cwz47Jp8>_pI(U(SrsK'
    'u?N-Ot+(V6(8J-B$n*Mf>{tGhJ~}bQXTTtYA7Hw0E;EeLAi2s2G=6^vRG*iQ3pu#Dh7qlP$wg)xSN8jP%Ac1P9P4y`%UcJ{f*PoBHyUlhvoX;b'
    'pELIK*xoo@EwY9piMjaq@+;FOdBcZ+AHL(YoLN1`wXW6bxuf`$PRLMP<uoU=DOLW+R!ggpc4+`yWLf4%EARv?5Y~X+G|8jeCkQsoQSVkWHlsYy'
    'W#kl?v*k^T=EUDgTQ*5#EEEjj54Hh`WeKb?l@xiQqwd^62wL^0*?UZLXYufV|M&j^;l@-7'
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

if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--demo", action="store_true")
    options = args.parse_args()
    try:
        build_curated(options.demo)
    except DataError as error:
        print("Yayın üretilmedi:", error, file=sys.stderr)
        sys.exit(1)
