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
    fig.add_layout_image(source=CURATED_LOGO,xref="paper",yref="paper",x=.5,y=.53,
        xanchor="center",yanchor="middle",sizex=.75,sizey=.90,sizing="contain",opacity=.15,layer="below")
    labels=[("Kaynak: TCMB / EVDS"+(" / Excel / London Fix" if chart["id"] in ("netfx-altin-haric-degisim","netfx-altin-haric") else ""),-109),
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

FX_HISTORICAL_INPUTS={'2023-12-29': [4206687781.0000005, 3403832713.0, 276261256.0, 29.3973, 48746.0, 0], '2024-01-02': [4200663852.0, 3418713097.0, 277108732.0, 29.4382, 47723.0, 0], '2024-01-03': [4191890894.0, 3451522805.0, 284201620.0, 29.6675, 47630.0, 0], '2024-01-04': [4173788908.0, 3464036840.0, 285424749.0, 29.7394, 47583.0, 0], '2024-01-05': [4212903709.0, 3487581238.0, 286599166.0, 29.7452, 48366.0, 0], '2024-01-08': [4161313103.0, 3475742781.0, 286001749.0, 29.7714, 47717.0, 0], '2024-01-09': [4179590565.0, 3490794046.0, 287172941.0, 29.8342, 47648.0, 0], '2024-01-10': [4181482079.0, 3515477785.0, 287552110.0, 29.8823, 47150.0, 0], '2024-01-11': [4180044219.0, 3515018404.0, 270438865.0, 29.9136, 46976.0, 0], '2024-01-12': [4248504794.0, 3600253453.0, 272001880.0, 29.9581, 45386.0, 0], '2024-01-15': [4267188831.0, 3598152177.0, 272639482.0, 30.0163, 45159.0, 0], '2024-01-16': [4152026562.0, 3623666183.0, 272192586.0, 30.0449, 42638.0, 0], '2024-01-17': [4166701426.0, 3653438555.0, 268388346.00000003, 30.047, 42914.0, 0], '2024-01-18': [4159192224.0, 3660928864.0, 268181072.0, 30.0721, 42971.0, 0], '2024-01-19': [4215549859.0, 3721051241.0, 271169155.0, 30.0847, 42147.0, 0], '2024-01-22': [4183925488.0, 3667853848.0, 269581784.0, 30.1219, 43972.0, 0], '2024-01-23': [4157733510.0, 3585586855.0, 270178275.0, 30.1806, 46021.0, 0], '2024-01-24': [4182579151.0, 3501205506.0, 269389695.0, 30.2185, 50344.0, 0], '2024-01-25': [4205871007.0, 3456305116.0, 268877148.0, 30.2145, 53065.0, 0], '2024-01-26': [4205432887.0, 3490070498.0, 265843941.99999997, 30.2185, 51099.0, 0], '2024-01-29': [4140396411.0, 3466184560.0, 266322360.99999997, 30.2343, 50608.0, 0], '2024-01-30': [4145543655.0, 3475773231.0, 267001128.00000003, 30.2849, 50319.0, 0], '2024-01-31': [4132486324.0, 3479594091.0, 269401021.0, 30.3053, 50094.0, 0], '2024-02-01': [4105393389.0, 3440603525.0, 265685919.0, 30.2952, 49667.0, 0], '2024-02-02': [4169814104.0, 3532778507.0, 267101800.0, 30.3063, 49587.0, 0], '2024-02-05': [4096372545.0, 3522983577.0, 266684090.00000003, 30.3735, 49494.0, 0], '2024-02-06': [4106914386.0, 3529772017.0, 268837866.0, 30.477, 49390.0, 0], '2024-02-07': [4129439046.0, 3531676879.0, 300198954.0, 30.5077, 49448.0, 0], '2024-02-08': [4147181477.0, 3529203324.0, 301056555.0, 30.5349, 49419.0, 0], '2024-02-09': [4181323602.0, 3547508455.0, 298029536.0, 30.5357, 49259.0, 0], '2024-02-12': [4116126424.0, 3533074109.0, 295500654.0, 30.6005, 48218.0, 0], '2024-02-13': [4115242456.0, 3543627025.0, 291063852.0, 30.6318, 47951.0, 0], '2024-02-14': [4073736139.0, 3533877012.0, 287177685.0, 30.669, 47883.0, 0], '2024-02-15': [4125716828.0, 3590262129.0, 373553510.0, 30.6835, 47547.0, 0], '2024-02-16': [4180566171.0, 3632078713.0, 373246714.0, 30.6968, 47220.0, 0], '2024-02-19': [4208783705.0, 3627254009.0, 373914751.0, 30.7221, 47220.0, 0], '2024-02-20': [4112762036.0, 3588677106.0, 375387166.0, 30.804, 46631.0, 0], '2024-02-21': [4133508317.0, 3680243657.0, 366756762.0, 30.8388, 45653.0, 0], '2024-02-22': [4118799223.0, 3735530070.0, 367799644.0, 30.9096, 44911.0, 0], '2024-02-23': [4141040354.0, 3756875567.0, 365949085.0, 30.9582, 45418.0, 0], '2024-02-26': [4101706779.0, 3763528003.0, 366554599.0, 31.0047, 44620.0, 0], '2024-02-27': [4102797419.0, 3755285972.0, 366342740.0, 31.0441, 45505.0, 0], '2024-02-28': [4094925958.0, 3787468726.0, 365926787.0, 31.0737, 44518.0, 0], '2024-02-29': [4081102103.0, 3796432284.0, 367712417.0, 31.1201, 43976.0, 0], '2024-03-01': [4141298412.0, 3818599873.0, 368603909.0, 31.1597, 45055.0, 0], '2024-03-04': [4096857735.0, 3802131749.0, 371359617.0, 31.2392, 45038.0, 0], '2024-03-05': [4125097327.0, 3847102144.0, 374239378.0, 31.4069, 45703.0, 0], '2024-03-06': [4100328033.0, 3811652007.0, 374444239.0, 31.5457, 48038.0, 0], '2024-03-07': [4120558312.0, 3807918876.0, 375495356.0, 31.6612, 49542.0, 0], '2024-03-08': [4210811067.0, 3870986404.0, 376553796.0, 31.7768, 51356.0, 0], '2024-03-11': [4172500793.0, 3859083074.0, 375907735.0, 31.8618, 52284.0, 0], '2024-03-12': [4166918817.0, 3891849441.0, 373213873.0, 31.9378, 52235.0, 0], '2024-03-13': [4099796368.0, 3845016575.0, 371035346.0, 31.9856, 53100.0, 0], '2024-03-14': [4098855976.0, 3829094305.0, 368062475.0, 32.039, 54440.0, 0], '2024-03-15': [4164331672.0, 3850626152.0, 368403158.0, 32.0629, 55969.0, 0], '2024-03-18': [4068631628.0, 3829649576.0, 367930803.0, 32.1214, 55289.0, 0], '2024-03-19': [4092989201.0, 3859998127.0, 369130900.0, 32.2448, 56451.0, 0], '2024-03-20': [4057251155.0, 3844074413.0, 286571081.0, 32.2791, 57368.0, 0], '2024-03-21': [4151567797.0, 3893821440.0, 353413255.0, 32.3294, 58503.0, 0], '2024-03-22': [4064881852.0, 3875858930.0, 351588210.0, 32.3105, 57080.0, 0], '2024-03-25': [4001585852.0, 3805996665.0, 347232233.0, 31.9675, 56508.0, 0], '2024-03-26': [4030624616.0, 3828827186.0, 347970013.0, 32.0592, 56171.0, 0], '2024-03-27': [3991685846.0, 3788902097.0, 347317929.0, 32.1005, 57286.0, 0], '2024-03-28': [4006121094.0, 3776076763.0, 348713971.0, 32.165, 57923.0, 0], '2024-03-29': [4057007074.0, 3834312295.0, 350771051.0, 32.2596, 57898.0, 0], '2024-04-01': [4046883608.0, 3823146050.0, 350860931.0, 32.2854, 58457.0, 0], '2024-04-02': [4070049708.0, 3806873705.0, 354394452.0, 32.3568, 58224.0, 0], '2024-04-03': [4065641458.0, 3813106523.0, 352700582.0, 32.1302, 56543.0, 0], '2024-04-04': [4092293551.0, 3792821978.0, 350662571.0, 31.9392, 55162.0, 0], '2024-04-05': [4106324269.0, 3771028911.0, 341517673.0, 31.8396, 53569.0, 0], '2024-04-08': [4156794683.0, 3774428702.0, 343752920.0, 31.9209, 53065.0, 0], '2024-04-09': [4181838147.0, 3803776682.0, 345101858.0, 32.006, 52336.0, 0], '2024-04-15': [4130274237.0, 3810735873.0, 301960907.0, 32.006, 50453.0, 0], '2024-04-16': [4138551462.0, 3869122110.0, 309062781.0, 32.3271, 48143.0, 0], '2024-04-17': [4159783689.0, 3940482144.0, 357590425.0, 32.4049, 46452.0, 0], '2024-04-18': [4123280070.0, 3979404460.0, 355366414.0, 32.4626, 44693.0, 0], '2024-04-19': [4164445519.0, 3976396985.0, 352040413.0, 32.4551, 44495.0, 0], '2024-04-22': [4120947995.0, 3959263614.0, 354739288.0, 32.5096, 44481.0, 0], '2024-04-24': [4079580215.0, 3939977733.0, 346903346.0, 32.5, 43275.0, 0], '2024-04-25': [4116169222.0, 3963715431.0, 346300887.0, 32.4742, 41266.0, 0], '2024-04-26': [4096460628.0, 3932573879.0, 346370595.0, 32.4747, 39586.0, 0], '2024-04-29': [4081883253.0, 3830881656.0, 345371371.0, 32.4527, 40183.0, 0], '2024-04-30': [4107363595.0, 3743279647.0, 343337504.0, 32.3389, 40404.0, 0], '2024-05-02': [4109859826.0, 3711669103.0, 342921664.0, 32.3091, 39245.0, 0], '2024-05-03': [4168498709.0, 3770981621.0, 344022102.0, 32.3199, 36813.0, 0], '2024-05-06': [4205448200.0, 3699685854.0, 343704582.0, 32.2472, 37204.0, 0], '2024-05-07': [4229265195.0000005, 3655234189.0, 344302689.0, 32.2242, 36366.0, 0], '2024-05-08': [4277486065.0000005, 3663979078.0, 337886729.0, 32.2073, 34866.0, 0], '2024-05-09': [4306641221.0, 3680155233.0, 336894700.0, 32.207, 32988.0, 0], '2024-05-10': [4391643401.0, 3682932952.0, 336358976.0, 32.177, 31398.0, 0], '2024-05-13': [4359136264.0, 3665668996.0, 326970799.0, 32.1534, 29383.0, 0], '2024-05-14': [4382649874.0, 3667140264.0, 326620209.0, 32.1496, 28123.0, 0], '2024-05-15': [4430022461.0, 3703509736.0, 328644164.0, 32.1946, 26623.0, 0], '2024-05-16': [4504999896.0, 3737047759.0, 328477806.0, 32.2296, 25870.0, 0], '2024-05-17': [4540619192.0, 3721838317.0, 328080527.0, 32.1573, 25355.0, 0], '2024-05-20': [4608220957.0, 3737055668.0, 331222174.0, 32.194, 24471.0, 0], '2024-05-21': [4641039889.0, 3765432489.0, 329657961.0, 32.156, 23156.0, 0], '2024-05-22': [4710638204.0, 3756588954.0, 329275005.0, 32.1556, 22294.0, 0], '2024-05-23': [4631035207.0, 3635607690.0, 326323841.0, 32.1209, 22172.0, 0], '2024-05-24': [4639784615.0, 3574706968.0, 290395474.0, 32.1421, 22264.0, 0], '2024-05-27': [4662671768.0, 3573970094.0, 288914252.0, 32.1452, 22264.0, 0], '2024-05-28': [4636965791.0, 3503471955.0, 286400066.0, 32.1311, 21961.0, 0], '2024-05-29': [4671567843.0, 3490783054.0, 285307384.0, 32.1203, 21561.0, 0], '2024-05-30': [4675489675.0, 3476230575.0, 285317758.0, 32.1874, 21061.0, 0], '2024-05-31': [4699625810.0, 3464507319.0, 287728380.0, 32.2489, 20811.0, 0], '2024-06-03': [4713161590.0, 3465694138.0, 286011860.0, 32.1339, 20611.0, 0], '2024-06-04': [4777167066.0, 3483634016.0, 285446583.0, 32.1534, 19910.0, 0], '2024-06-05': [4777941057.0, 3469607198.0, 286529424.0, 32.2687, 19514.0, 0], '2024-06-06': [4794925410.0, 3484983591.0, 290162450.0, 32.4547, 19303.0, 0], '2024-06-07': [4768973356.0, 3469022712.0, 286636822.0, 32.1775, 18300.0, 0], '2024-06-10': [4718472436.0, 3464045579.0, 286325551.0, 32.1923, 17118.0, 0], '2024-06-11': [4763290315.0, 3493742370.0, 290226577.0, 32.3699, 16552.0, 0], '2024-06-12': [4803327545.0, 3536677865.0, 291494293.0, 32.3127, 15374.0, 0], '2024-06-13': [4814108735.0, 3537554132.0, 285594257.0, 32.3073, 13984.0, 0], '2024-06-14': [4782038548.0, 3495445642.0, 281786633.0, 32.2313, 12496.0, 0], '2024-06-20': [4793471320.0, 3539513227.0, 286826666.0, 32.4579, 10757.0, 0], '2024-06-21': [4872637439.0, 3647013846.0, 289869442.0, 32.5711, 9093.0, 0], '2024-06-24': [4851387612.0, 3705335879.0, 290318459.0, 32.781, 7122.0, 0], '2024-06-25': [4842567396.0, 3766427594.0, 290173408.0, 32.8078, 4698.0, 0], '2024-06-26': [4812506035.0, 3785943743.0, 288480498.0, 32.9015, 4979.0, 0], '2024-06-27': [4796282792.0, 3750839775.0, 288870117.0, 32.904, 5199.0, 0], '2024-06-28': [4762091401.0, 3700003784.0, 290508750.0, 32.8417, 4719.0, 0], '2024-07-01': [4803347411.0, 3722224217.0, 289938688.0, 32.8262, 4719.0, 0], '2024-07-02': [4795612159.0, 3696498224.0, 287707071.0, 32.6207, 4353.0, 0], '2024-07-03': [4799148558.0, 3699359851.0, 285740212.0, 32.629, 2994.0, 0], '2024-07-04': [4807262311.0, 3685718679.0, 283228310.0, 32.4929, 2994.0, 0], '2024-07-05': [4891246979.0, 3702907167.0, 283396659.0, 32.5011, 2322.0, 0], '2024-07-08': [4940773145.0, 3717271516.0, 284863700.0, 32.6301, 2322.0, 0], '2024-07-09': [4981135058.0, 3733111903.0, 284075701.0, 32.6401, 2330.0, 0], '2024-07-10': [5015817011.0, 3735668030.0, 280678429.0, 32.7362, 2330.0, 0], '2024-07-11': [5062480966.0, 3754259129.0, 263019726.00000003, 32.8849, 2353.0, 0], '2024-07-12': [5120440339.0, 3776756716.0, 262566493.00000003, 32.8473, 2351.0, 0], '2024-07-16': [5164116752.0, 3793569401.0, 262915934.0, 32.9069, 2342.0, 0], '2024-07-17': [5233216928.0, 3872057968.0, 318265365.0, 33.0223, 2042.0, 0], '2024-07-18': [5243810617.0, 3880482504.0, 316245456.0, 33.038, 2042.0, 0], '2024-07-19': [5152701184.0, 3816209032.0, 313508020.0, 33.0389, 2051.0, 0], '2024-07-22': [5062836743.0, 3806347553.0, 312408882.0, 33.0042, 144.0, 0], '2024-07-23': [4906927464.0, 3635964430.0, 312319913.0, 32.9441, 144.0, 0], '2024-07-24': [4906629170.0, 3613655992.0, 306964182.0, 32.8805, 144.0, 0], '2024-07-25': [4889241992.0, 3596409296.0, 305200599.0, 32.8106, 121.0, 0], '2024-07-26': [4950026180.0, 3627585048.0, 306596562.0, 32.9833, 55.0, 0], '2024-07-29': [4939054543.0, 3591343637.0, 306552551.0, 32.9586, 55.0, 0], '2024-07-30': [4943420378.0, 3577176098.0, 304942046.0, 32.9509, 52.0, 0], '2024-07-31': [4967794204.0, 3565009078.0, 309773555.0, 33.0304, 0.0, 0], '2024-08-01': [4990017367.0, 3574835856.0, 307498006.0, 33.0835, 0.0, 0], '2024-08-02': [5041545847.0, 3594239880.0, 308699205.0, 33.0782, 0.0, 180.0], '2024-08-05': [5002094583.0, 3631623965.0, 282347725.0, 33.0763, 0.0, 180.0], '2024-08-06': [4979907945.0, 3726984521.0, 282271385.0, 33.2573, 0.0, 180.0], '2024-08-07': [4979808472.0, 3740607831.0, 359570870.0, 33.3984, 0.0, 180.0], '2024-08-08': [5004882285.0, 3746393311.0, 280900098.0, 33.4965, 0.0, 180.0], '2024-08-09': [5092887103.0, 3821530427.0, 279147728.0, 33.4471, 0.0, 0.0], '2024-08-12': [5023879080.0, 3726778663.0, 278169581.0, 33.4596, 0.0, 0.0], '2024-08-13': [5072614899.0, 3762484242.0, 279108900.0, 33.4843, 0.0, 225.0], '2024-08-14': [5122054597.0, 3785861052.0, 279357562.0, 33.4928, 0.0, 225.0], '2024-08-15': [5116400163.0, 3757153180.0, 271630444.0, 33.4846, 0.0, 225.0], '2024-08-16': [5201567100.0, 3817003912.0, 272379677.0, 33.5746, 0.0, 225.0], '2024-08-19': [5189874957.0, 3787489315.0, 275166913.0, 33.6107, 0.0, 225.0], '2024-08-20': [5181984601.0, 3789581491.0, 274724885.0, 33.6524, 0.0, 0.0], '2024-08-21': [5170657447.0, 3853016278.0, 273611078.0, 33.7482, 0.0, 0.0], '2024-08-22': [5167396586.0, 3895102099.0, 270483610.0, 33.8465, 0.0, 0.0], '2024-08-23': [5159461415.0, 3893386217.0, 267785108.0, 33.8506, 0.0, 0.0], '2024-08-26': [5103765105.0, 3879393310.0, 267844352.0, 33.8863, 0.0, 0.0], '2024-08-27': [5108734680.0, 3891860982.0, 266471593.0, 33.9282, 0.0, 0.0], '2024-08-28': [5119793501.0, 3905540698.0, 268107697.0, 33.9577, 0.0, 0.0], '2024-08-29': [5142259219.0, 3967022626.0, 269492302.0, 33.9626, 0.0, 0.0], '2024-09-02': [5159677881.0, 3972368394.0, 270200551.0, 33.9233, 0.0, 0.0], '2024-09-03': [4964672352.0, 3941797283.0, 271097807.0, 33.9196, 0.0, 0.0], '2024-09-04': [4951916989.0, 3925896253.0, 269645355.0, 33.887, 0.0, 0.0], '2024-09-05': [5030475697.0, 3920015883.0, 271755501.0, 33.9725, 0.0, 0.0], '2024-09-06': [5045508726.0, 3911247949.0, 271098536.0, 33.9091, 0.0, 0.0], '2024-09-09': [5071624948.0, 3869588308.0, 269366015.0, 33.8937, 0.0, 0.0], '2024-09-10': [5115067772.0, 3872614181.0, 272846641.0, 33.9676, 0.0, 0.0], '2024-09-11': [5166095879.0, 3892231043.0, 272766972.0, 33.9904, 0.0, 0.0], '2024-09-12': [5194930694.0, 3826392254.0, 268527373.0, 33.9398, 0.0, 0.0], '2024-09-13': [5274745527.0, 3838014895.0, 267247023.0, 33.8984, 0.0, 0.0], '2024-09-16': [5294992018.0, 3824868357.0, 266869816.0, 33.8159, 0.0, 0.0], '2024-09-17': [5295924527.0, 3821153631.0, 265875884.00000003, 33.8895, 0.0, 500.0], '2024-09-18': [5354472609.0, 3890392525.0, 264178440.0, 33.9681, 0.0, 500.0], '2024-09-19': [5382278562.0, 3894541560.0, 265934521.0, 34.0283, 0.0, 500.0], '2024-09-20': [5382660868.0, 3854767690.0, 263469338.0, 33.9766, 0.0, 500.0], '2024-09-23': [5396432000.0, 3875169890.0, 259968209.0, 33.9531, 0.0, 750.0], '2024-09-24': [5348655367.0, 3826247053.0, 260834773.0, 34.0627, 0.0, 550.0], '2024-09-25': [5382396858.0, 3826102045.0, 261787440.0, 34.0815, 0.0, 705.0], '2024-09-26': [5414177533.0, 3839783347.0, 262212401.0, 34.0894, 0.0, 845.0], '2024-09-27': [5435757972.0, 3794960139.0, 260962024.0, 34.0982, 0.0, 1095.0], '2024-09-30': [5276985424.0, 3690124446.0, 259735294.0, 34.09, 0.0, 1195.0], '2024-10-01': [5413335325.0, 3784534695.0, 259814136.0, 34.121, 0.0, 1190.0], '2024-10-02': [5398081279.0, 3785568536.0, 260270527.0, 34.1247, 0.0, 1035.0], '2024-10-03': [5417177320.0, 3793012039.0, 314981488.0, 34.1538, 0.0, 1045.0], '2024-10-04': [5402574206.0, 3743754702.0, 315830525.0, 34.1126, 0.0, 970.0], '2024-10-07': [5393082744.0, 3734985807.0, 313587013.0, 34.1398, 0.0, 620.0], '2024-10-08': [5386895457.0, 3720862709.0, 313829381.0, 34.1881, 0.0, 725.0], '2024-10-09': [5368340324.0, 3711738056.0, 351771784.0, 34.2103, 0.0, 1075.0], '2024-10-10': [5418046446.0, 3745193839.0, 347619900.0, 34.1951, 0.0, 1175.0], '2024-10-11': [5446544316.0, 3746712439.0, 346283222.0, 34.1522, 0.0, 1300.0], '2024-10-14': [5481113982.0, 3754488130.0, 347350084.0, 34.1625, 0.0, 1300.0], '2024-10-15': [5422695169.0, 3705080265.0, 347294449.0, 34.2034, 0.0, 1595.0], '2024-10-16': [5464065389.0, 3720519450.0, 349389630.0, 34.2018, 0.0, 1770.0], '2024-10-17': [5453095355.0, 3708492769.0, 349683745.0, 34.1633, 0.0, 2072.0], '2024-10-18': [5507516316.0, 3729541040.0, 352310696.0, 34.1155, 0.0, 2262.0], '2024-10-21': [5519446515.0, 3734074957.0, 354450487.0, 34.1786, 0.0, 2832.0], '2024-10-22': [5527446266.0, 3731211498.0, 351880483.0, 34.1788, 0.0, 2887.0], '2024-10-23': [5589824884.0, 3783319149.0, 355998736.0, 34.1821, 0.0, 3362.0], '2024-10-24': [5556137061.0, 3765249102.0, 352306829.0, 34.2011, 0.0, 3810.0], '2024-10-25': [5520664677.0, 3735230024.0, 350965864.0, 34.1902, 0.0, 4570.0], '2024-10-28': [5510147662.0, 3719625737.0, 350729578.0, 34.189, 0.0, 4570.0], '2024-10-30': [5577048977.0, 3741665295.0, 353860542.0, 34.189, 0.0, 5000.0], '2024-10-31': [5563833663.0, 3731622343.0, 354222160.0, 34.2095, 0.0, 5750.0], '2024-11-01': [5526358223.0, 3728235499.0, 351722509.0, 34.1787, 0.0, 6913.0], '2024-11-04': [5420212248.0, 3748005969.0, 351768391.0, 34.2236, 0.0, 7457.0], '2024-11-05': [5426755705.0, 3763135204.0, 351360560.0, 34.2757, 0.0, 8045.0], '2024-11-06': [5405196405.0, 3823838629.0, 349897098.0, 34.282, 0.0, 7393.0], '2024-11-07': [5372462982.0, 3765279506.0, 336565288.0, 34.1841, 0.0, 6807.0], '2024-11-08': [5447759605.0, 3745832424.0, 339428217.0, 34.1764, 0.0, 6808.0], '2024-11-11': [5482120721.0, 3766566648.0, 425380057.0, 34.2614, 0.0, 6800.0], '2024-11-12': [5410197070.0, 3736832431.0, 353550455.0, 34.2869, 0.0, 6958.0], '2024-11-13': [5441777385.0, 3769940622.0, 378621138.0, 34.3128, 0.0, 7445.0], '2024-11-14': [5413000017.0, 3746570277.0, 375681758.0, 34.3118, 0.0, 7405.0], '2024-11-15': [5445510144.0, 3735736678.0, 377512895.0, 34.2996, 0.0, 7375.0], '2024-11-18': [5443246846.0, 3754784382.0, 380757321.0, 34.3333, 0.0, 7761.0], '2024-11-19': [5345845032.0, 3750962741.0, 384803288.0, 34.4321, 0.0, 7298.0], '2024-11-20': [5352666195.0, 3761122115.0, 349524109.0, 34.5162, 0.0, 6378.0], '2024-11-21': [5410430913.0, 3727512837.0, 351708336.0, 34.4372, 0.0, 4880.0], '2024-11-22': [5467397459.0, 3665249965.0, 355512105.0, 34.4495, 0.0, 3081.0], '2024-11-25': [5437936546.0, 3635873258.0, 353579716.0, 34.4642, 0.0, 3170.0], '2024-11-26': [5548182652.0, 3735794656.0, 438377982.0, 34.5184, 0.0, 3393.0], '2024-11-27': [5546180748.0, 3727278143.0, 441496722.0, 34.5726, 0.0, 3741.0], '2024-11-28': [5538941514.0, 3702768402.0, 437007609.0, 34.5728, 0.0, 3825.0], '2024-11-29': [5525197095.0, 3687874634.0, 439924907.0, 34.5697, 0.0, 3875.0], '2024-12-02': [5530653027.0, 3702298133.0, 438250725.0, 34.5794, 0.0, 4293.0], '2024-12-03': [5550444855.0, 3703953157.0, 439189225.0, 34.6432, 0.0, 4788.0], '2024-12-04': [5587345560.0, 3752406176.0, 440417501.0, 34.6766, 0.0, 5153.0], '2024-12-05': [5591146698.0, 3740301861.0, 441371635.0, 34.6873, 0.0, 5789.0], '2024-12-06': [5599233403.0, 3720413723.0, 439480064.0, 34.6753, 0.0, 5561.0], '2024-12-09': [5620539530.0, 3720148334.0, 440421396.0, 34.7007, 0.0, 5610.0], '2024-12-10': [5686415177.0, 3755085018.0, 441295362.0, 34.7499, 0.0, 5774.0], '2024-12-11': [5666188758.0, 3746113908.0, 444095578.0, 34.7797, 0.0, 5726.0], '2024-12-12': [5691681068.0, 3787171410.0, 446070231.0, 34.8028, 0.0, 6551.0], '2024-12-13': [5763490284.0, 3868671485.0, 446046652.0, 34.8177, 0.0, 6822.0], '2024-12-16': [5654958670.0, 3859919733.0, 445651947.0, 34.8621, 0.0, 7121.0], '2024-12-17': [5602575464.0, 3810718996.0, 446337796.0, 34.8985, 0.0, 7292.0], '2024-12-18': [5628313872.0, 3848881469.0, 449669943.0, 34.9219, 0.0, 7554.0], '2024-12-19': [5616189308.0, 3805462864.0, 447975073.0, 34.9529, 0.0, 7468.0], '2024-12-20': [5542381721.0, 3765782877.0, 446161170.0, 35.0067, 0.0, 6963.0], '2024-12-23': [5525043209.0, 3822250608.0, 442116044.0, 35.0619, 0.0, 6194.0], '2024-12-24': [5530473662.0, 3793335947.0, 441276743.0, 35.1065, 0.0, 5365.0], '2024-12-25': [5562162052.0, 3804598619.0, 440057044.0, 35.1814, 0.0, 5555.0], '2024-12-26': [5586368222.0, 3770353615.0, 436559573.0, 35.2162, 0.0, 2809.0], '2024-12-27': [5531555752.0, 3672640627.0, 435473999.0, 35.2033, 0.0, 2392.0], '2024-12-30': [5560898134.0, 3671328888.0, 432329489.0, 35.1368, 0.0, 2058.0], '2024-12-31': [5538776105.0, 3594647626.0, 435898091.0, 35.2233, 0.0, 1480.0], '2025-01-02': [5606141685.0, 3646735166.0, 437267044.0, 35.2803, 0.0, 1367.0], '2025-01-03': [5639697090.0, 3637193967.0, 443253658.0, 35.2525, 0.0, 1281.0], '2025-01-06': [5618011412.0, 3606297664.0, 443220072.0, 35.271, 0.0, 2038.0], '2025-01-07': [5649865377.0, 3606028452.0, 443793648.0, 35.2892, 0.0, 2887.0], '2025-01-08': [5677882946.0, 3629698633.0, 447444283.0, 35.2764, 0.0, 4547.0], '2025-01-09': [5722209951.0, 3654108232.0, 441622165.0, 35.3179, 0.0, 5571.0], '2025-01-10': [5735076675.0, 3652046657.0, 440032348.0, 35.2921, 0.0, 7199.0], '2025-01-13': [5750831963.0, 3644421806.0, 431219555.0, 35.3356, 0.0, 8119.0], '2025-01-14': [5713890463.0, 3634905640.0, 431980876.0, 35.4197, 0.0, 8888.0], '2025-01-15': [5777955430.0, 3658091715.0, 429405934.0, 35.4292, 0.0, 9288.0], '2025-01-16': [5802305099.0, 3659937561.0, 426404794.0, 35.4294, 0.0, 9891.0], '2025-01-17': [5853867855.0, 3692450993.0, 426809392.0, 35.3979, 0.0, 10223.0], '2025-01-20': [5832310419.0, 3663564596.0, 426474512.0, 35.4119, 0.0, 10490.0], '2025-01-21': [5881395379.0, 3674079303.0, 426400136.0, 35.5183, 0.0, 10551.0], '2025-01-22': [5958536565.0, 3705561253.0, 425391034.0, 35.5516, 0.0, 10574.0], '2025-01-23': [5970718505.0, 3730193581.0, 423115061.0, 35.5815, 0.0, 10983.0], '2025-01-24': [6037691261.0, 3777121641.0, 424790180.0, 35.5915, 0.0, 11588.0], '2025-01-27': [6042699107.0, 3810507191.0, 419953325.0, 35.5994, 0.0, 12723.0], '2025-01-28': [6030657453.0, 3818215735.0, 419127469.0, 35.6602, 0.0, 13256.0], '2025-01-29': [6079136082.0, 3892186921.0, 420203378.0, 35.6978, 0.0, 13985.0], '2025-01-30': [6100758126.0, 3852899590.0, 421272877.0, 35.7067, 0.0, 13440.0], '2025-01-31': [6004202789.0, 3804080543.0, 424608675.0, 35.721, 0.0, 13131.0], '2025-02-03': [6061483209.0, 3787620949.0, 324294596.0, 35.763, 0.0, 11784.0], '2025-02-04': [6046129510.0, 3823547355.0, 324871320.0, 35.9022, 0.0, 11952.0], '2025-02-05': [6034609047.0, 3747544074.0, 328215567.0, 35.9164, 0.0, 11769.0], '2025-02-06': [6069163324.0, 3762448914.0, 326956862.0, 35.8683, 0.0, 12251.0], '2025-02-07': [6077398149.0, 3746678834.0, 325494047.0, 35.8332, 0.0, 12295.0], '2025-02-10': [6109038602.0, 3748304938.0, 329086386.0, 35.9055, 0.0, 12701.0], '2025-02-11': [6127263700.0, 3731225161.0, 330298944.0, 35.9451, 0.0, 12547.0], '2025-02-12': [6232425430.0, 3826611963.0, 415954223.0, 35.9593, 0.0, 12609.0], '2025-02-13': [6280259867.0, 3867622469.0, 411247196.0, 36.0273, 0.0, 12741.0], '2025-02-14': [6318059785.0, 3862903722.0, 411585586.0, 36.0448, 0.0, 12788.0], '2025-02-17': [6295782551.0, 3847582707.0, 408562870.0, 36.106, 0.0, 12860.0], '2025-02-18': [6230299231.0, 3856823918.0, 409828865.0, 36.1523, 0.0, 13175.0], '2025-02-19': [6271793484.0, 3940605989.0, 409087590.0, 36.1964, 0.0, 13327.0], '2025-02-20': [6227718739.0, 3968434292.0, 410288476.0, 36.2267, 0.0, 13355.0], '2025-02-21': [6238290148.0, 3971584954.0, 405625579.0, 36.2472, 0.0, 13309.0], '2025-02-24': [6123867253.0, 4019433037.0, 405922199.0, 36.306, 0.0, 13105.0], '2025-02-25': [6173465685.0, 4034864949.0, 406965019.0, 36.3563, 0.0, 12823.0], '2025-02-26': [6165302487.0, 4057562062.0, 406382122.0, 36.3875, 0.0, 12614.0], '2025-02-27': [6120314117.0, 3980283365.0, 404537661.0, 36.3717, 0.0, 11899.0], '2025-02-28': [6091906671.0, 3944582247.0, 403087008.0, 36.3652, 0.0, 12012.0], '2025-03-03': [6085481852.0, 3979684360.0, 403209043.0, 36.3812, 0.0, 12454.0], '2025-03-04': [6138816808.0, 3985545338.0, 406653769.0, 36.4097, 0.0, 12514.0], '2025-03-05': [6168015033.0, 3981219793.0, 407375114.0, 36.4011, 0.0, 12450.0], '2025-03-06': [6203191482.0, 3918619160.0, 328213491.0, 36.3682, 0.0, 12452.0], '2025-03-07': [6256873833.0, 3893754227.0, 328163722.0, 36.3748, 0.0, 11156.0], '2025-03-10': [6261945958.0, 3866302253.0, 399969590.0, 36.3153, 0.0, 10701.0], '2025-03-11': [6307775268.0, 3886563263.0, 335486373.0, 36.4558, 0.0, 10425.0], '2025-03-12': [6290057315.0, 3882515640.0, 330835879.0, 36.5088, 0.0, 10234.0], '2025-03-13': [6243399600.0, 3857693777.0, 329093179.0, 36.5401, 0.0, 10789.0], '2025-03-14': [6325878571.0, 3901199445.0, 332409069.0, 36.5387, 0.0, 11164.0], '2025-03-17': [6363122732.0, 3946515879.0, 333158762.0, 36.535, 0.0, 11308.0], '2025-03-18': [6368731117.0, 3929446366.0, 333333852.0, 36.6139, 0.0, 11537.0], '2025-03-19': [6403041748.0, 3955647958.0, 334833443.0, 36.5922, 0.0, 11720.0], '2025-03-20': [6313473157.0, 4182508464.0, 343328561.0, 37.9839, 0.0, 11286.0], '2025-03-21': [6262527231.0, 4190883023.0, 339724019.0, 37.915, 0.0, 9775.0], '2025-03-24': [5894637157.0, 4201446446.0000005, 340317526.0, 37.8502, 0.0, 7293.0], '2025-03-25': [5948778416.0, 4217801329.0, 341867362.0, 37.86, 0.0, 5149.0], '2025-03-26': [6039973550.0, 4206882482.0, 341813336.0, 37.9086, 0.0, 2973.0], '2025-03-27': [6066994244.0, 4152244918.0, 260597178.0, 37.9287, 0.0, 1388.0], '2025-03-28': [6015928474.0, 4099918769.0, 221346411.0, 37.9323, 0.0, 1401.0], '2025-04-02': [5941345824.0, 4236492382.0, 224454266.0, 37.7656, 0.0, 1501.0], '2025-04-03': [5937032697.0, 4213469327.9999995, 225041789.0, 37.8559, 0.0, 1628.0], '2025-04-04': [5919852133.0, 4232801161.9999995, 219974228.0, 37.8637, 0.0, 2124.0], '2025-04-07': [5605061648.0, 4231596127.9999995, 213495645.0, 37.8465, 0.0, 2801.0], '2025-04-08': [5511902589.0, 4261169012.0, 213578394.0, 37.933, 0.0, 3022.0], '2025-04-09': [5520357083.0, 4270314648.0, 217519077.0, 37.93, 0.0, 2631.0], '2025-04-10': [5498966104.0, 4286993894.9999995, 214064079.0, 37.9327, 0.0, 2169.0], '2025-04-11': [5666484773.0, 4349130266.0, 216453454.0, 37.8646, 0.0, 1616.0], '2025-04-14': [5545565164.0, 4285941980.9999995, 213174921.0, 37.9073, 0.0, 1073.0], '2025-04-15': [5565273529.0, 4272933088.9999995, 213883927.0, 37.9649, 0.0, 850.0], '2025-04-16': [5678187263.0, 4305505897.0, 281859883.0, 38.0065, 0.0, 1165.0], '2025-04-17': [5548879501.0, 4303111464.0, 283563957.0, 38.0472, 0.0, 1490.0], '2025-04-18': [5671351668.0, 4407395497.0, 283764816.0, 38.0611, 0.0, 1508.0], '2025-04-21': [5586216283.0, 4315793443.0, 279916234.0, 38.0032, 0.0, 1625.0], '2025-04-22': [5707395371.0, 4343082288.0, 288314471.0, 38.114, 0.0, 1526.0], '2025-04-24': [5491598368.0, 4352940794.0, 276075389.0, 38.1314, 0.0, 1090.0], '2025-04-25': [5474469727.0, 4336491367.0, 263573257.99999997, 38.2289, 0.0, 1178.0], '2025-04-28': [5348211157.0, 4343717182.0, 263491003.00000003, 38.2455, 0.0, 1082.0], '2025-04-29': [5399540995.0, 4350027744.0, 267296914.99999997, 38.3665, 0.0, 1091.0], '2025-04-30': [5402442408.0, 4324451260.0, 266548058.00000003, 38.3672, 0.0, 736.0], '2025-05-02': [5398518459.0, 4336642676.0, 265632941.99999997, 38.3684, 0.0, 576.0], '2025-05-05': [5378806571.0, 4275015603.0, 267456778.0, 38.3695, 0.0, 303.0], '2025-05-06': [5477323444.0, 4275365705.0, 272581066.0, 38.5105, 0.0, 241.0], '2025-05-07': [5519741086.0, 4277159087.0000005, 273289637.0, 38.5259, 0.0, 165.0], '2025-05-08': [5528087603.0, 4298235129.0, 262615115.99999997, 38.556, 0.0, 165.0], '2025-05-09': [5648164183.0, 4398069006.0, 261139633.0, 38.5529, 0.0, 123.0], '2025-05-12': [5536018381.0, 4344125923.0, 259629308.0, 38.5725, 0.0, 63.0], '2025-05-13': [5610725396.0, 4365532175.0, 257772240.0, 38.6761, 0.0, 11.0], '2025-05-14': [5653367353.0, 4361703022.0, 256037208.0, 38.7123, 0.0, 11.0], '2025-05-15': [5657040588.0, 4344289275.0, 250456147.0, 38.6933, 0.0, 11.0], '2025-05-16': [5711389324.0, 4352961760.0, 251931090.0, 38.6349, 0.0, 10.0], '2025-05-20': [5777389319.0, 4333524288.0, 258403677.0, 38.5944, 0.0, 10.0], '2025-05-21': [5931469468.0, 4372476256.0, 278159224.0, 38.7709, 0.0, 10.0], '2025-05-22': [5987178153.0, 4371206100.0, 276902796.0, 38.7602, 0.0, 10.0], '2025-05-23': [6022665775.0, 4372667689.0, 279856689.0, 38.7965, 0.0, 23.0], '2025-05-26': [6023293966.0, 4364004709.0, 279471473.0, 38.8174, 0.0, 253.0], '2025-05-27': [5926667251.0, 4383246207.0, 277526578.0, 38.8907, 0.0, 359.0], '2025-05-28': [6008968591.0, 4412260207.0, 278065988.0, 38.9027, 0.0, 359.0], '2025-05-29': [6054417565.0, 4452899029.0, 352839198.0, 38.9808, 0.0, 359.0], '2025-05-30': [6064811844.0, 4420909151.0, 353816915.0, 39.0212, 0.0, 336.0], '2025-06-02': [6014253315.0, 4441563256.0, 357396870.0, 39.0626, 0.0, 107.0], '2025-06-03': [6087232050.0, 4433664583.0, 356432141.0, 39.1151, 0.0, 0.0], '2025-06-04': [6117104943.0, 4409707642.0, 356757875.0, 39.0798, 0.0, 0.0], '2025-06-05': [6176555414.0, 4421549187.0, 357623642.0, 39.0575, 0.0, 0.0], '2025-06-10': [6128306303.0, 4425898906.0, 354921341.0, 39.0575, 0.0, 0.0], '2025-06-11': [6199944523.0, 4463033611.0, 355347227.0, 39.1385, 0.0, 0.0], '2025-06-12': [6235815383.0, 4433112740.0, 308424790.0, 39.1192, 0.0, 55.0], '2025-06-13': [6332198538.0, 4471868941.0, 310626076.0, 39.2163, 0.0, 269.0], '2025-06-16': [6120150380.0, 4485313346.0, 310687502.0, 39.2546, 0.0, 652.0], '2025-06-17': [6211583467.0, 4545480781.0, 306828509.0, 39.321, 0.0, 915.0], '2025-06-18': [6217550470.0, 4546942355.0, 306976476.0, 39.3015, 0.0, 1429.0], '2025-06-19': [6237744420.0, 4560152486.0, 306942668.0, 39.4229, 0.0, 1698.0], '2025-06-20': [6230697484.0, 4568821174.0, 308096974.0, 39.4801, 0.0, 1654.0], '2025-06-23': [6248078985.0, 4600228838.0, 308403450.0, 39.541, 0.0, 1365.0], '2025-06-24': [6173313945.0, 4605895215.0, 306800662.0, 39.647, 0.0, 1415.0], '2025-06-25': [6205536623.0, 4589384635.0, 305292832.0, 39.5502, 0.0, 1446.0], '2025-06-26': [6290712482.0, 4632172624.0, 304616079.0, 39.6392, 0.0, 1362.0], '2025-06-27': [6219235374.0, 4611441484.0, 302658759.0, 39.6989, 0.0, 1559.0], '2025-06-30': [6067351143.0, 4541418601.0, 303553598.0, 39.7424, 0.0, 1692.0], '2025-07-01': [6480643347.0, 4644899847.0, 401186847.0, 39.7408, 0.0, 1469.0], '2025-07-02': [6559873220.0, 4658849095.0, 399986625.0, 39.7733, 0.0, 924.0], '2025-07-03': [6589333295.0, 4646540791.0, 401670348.0, 39.7292, 0.0, 683.0], '2025-07-04': [6627508475.0, 4659804334.0, 399880938.0, 39.677, 0.0, 316.0], '2025-07-07': [6585961412.0, 4589232945.0, 397293507.0, 39.6792, 0.0, 98.0], '2025-07-08': [6654959570.0, 4622463420.0, 400243130.0, 39.9019, 0.0, 121.0], '2025-07-09': [6638148028.0, 4622980781.0, 395046777.0, 39.9332, 0.0, 332.0], '2025-07-10': [6694718131.0, 4642267400.0, 389792587.0, 39.9621, 0.0, 545.0], '2025-07-11': [6739066441.0, 4671783397.0, 376060557.0, 39.9775, 0.0, 833.0], '2025-07-14': [6786490585.0, 4693765747.0, 376197464.0, 40.0076, 0.0, 1025.0], '2025-07-16': [6813958749.0, 4710290256.0, 376822573.0, 40.0883, 0.0, 1086.0], '2025-07-17': [6877969764.0, 4758481159.0, 441852190.0, 40.1639, 0.0, 1007.0], '2025-07-18': [6866018913.0, 4715945116.0, 444231081.0, 40.2014, 0.0, 1056.0], '2025-07-21': [6961813085.0, 4826213902.0, 444502618.0, 40.2282, 0.0, 1255.0], '2025-07-22': [7025643376.0, 4863265538.0, 445274618.0, 40.3108, 0.0, 1574.0], '2025-07-23': [7084595981.0, 4870797995.0, 515935765.0, 40.3356, 0.0, 1679.0], '2025-07-24': [7034971183.0, 4878034521.0, 509879410.0, 40.3683, 0.0, 1960.0], '2025-07-25': [7029254141.0, 4863391597.0, 509184109.0, 40.3929, 0.0, 1732.0], '2025-07-28': [7003587010.0, 4881685397.0, 510531810.0, 40.399, 0.0, 1590.0], '2025-07-29': [7020815575.0, 4863943308.0, 509893387.0, 40.4717, 0.0, 1699.0], '2025-07-30': [7003847555.0, 4838015899.0, 509342038.0, 40.493, 0.0, 1668.0], '2025-07-31': [6967428028.0, 4817702620.0, 510094257.0, 40.4994, 0.0, 1812.0], '2025-08-01': [6933587045.0, 4812061662.0, 510830772.0, 40.5128, 0.0, 2033.0], '2025-08-04': [7058681099.0, 4832412916.0, 512850855.0, 40.519, 0.0, 2031.0], '2025-08-05': [7106617842.0, 4844822005.0, 515087405.0, 40.594, 0.0, 1753.0], '2025-08-06': [7125464497.0, 4844670460.0, 514082279.0, 40.6026, 0.0, 1620.0], '2025-08-07': [7153061681.0, 4832083409.0, 513347140.0, 40.5932, 0.0, 1508.0], '2025-08-08': [7162731365.0, 4803568373.0, 449988620.0, 40.5808, 0.0, 1622.0], '2025-08-11': [7150940022.0, 4797531379.0, 445697060.0, 40.5543, 0.0, 1707.0], '2025-08-12': [7156575082.0, 4781922807.0, 443467675.0, 40.6325, 0.0, 1779.0], '2025-08-13': [7224847961.0, 4824396304.0, 436406308.0, 40.6384, 0.0, 1888.0], '2025-08-14': [7295338909.0, 4827616916.0, 427869786.0, 40.6573, 0.0, 2032.0], '2025-08-15': [7277328675.0, 4785145465.0, 427102940.0, 40.7094, 0.0, 1915.0], '2025-08-18': [7276281770.0, 4768930269.0, 427316025.0, 40.7313, 0.0, 2018.0], '2025-08-19': [7298406564.0, 4775037153.0, 427373851.0, 40.8084, 0.0, 2155.0], '2025-08-20': [7332786738.0, 4801015029.0, 426999482.0, 40.812, 0.0, 2368.0], '2025-08-21': [7341279427.0, 4789440103.0, 422926305.0, 40.841, 0.0, 2367.0], '2025-08-22': [7296904141.0, 4773538152.0, 482698001.0, 40.8586, 0.0, 2412.0], '2025-08-25': [7292560000.0, 4775035541.0, 483533569.0, 40.8507, 0.0, 2486.0], '2025-08-26': [7382851833.0, 4806699066.0, 479518759.0, 40.9205, 0.0, 2606.0], '2025-08-27': [7387137309.0, 4787645803.0, 478981461.0, 40.9413, 0.0, 2604.0], '2025-08-28': [7394727416.0, 4812529889.0, 480637531.0, 40.9629, 0.0, 2719.0], '2025-08-29': [7399763778.0, 4790572431.0, 482130324.0, 40.9735, 0.0, 2738.0], '2025-09-01': [7497251281.0, 4822962470.0, 484570183.0, 40.9441, 0.0, 2770.0], '2025-09-02': [7521103630.0, 4905508364.0, 488617297.0, 41.0379, 0.0, 2785.0], '2025-09-03': [7434318035.0, 4963235322.0, 488646106.0, 41.0768, 0.0, 2751.0], '2025-09-04': [7466945584.0, 4975259400.0, 489509327.0, 41.0851, 0.0, 2612.0], '2025-09-05': [7498022409.0, 4979987107.0, 490156373.0, 41.0908, 0.0, 2645.0], '2025-09-08': [7555340835.0, 4981993271.0, 492560249.0, 41.097, 0.0, 2442.0], '2025-09-09': [7541544762.0, 5003115494.0, 495965345.0, 41.1816, 0.0, 2363.0], '2025-09-10': [7574884031.0, 5072935394.0, 497964205.0, 41.192, 0.0, 2096.0], '2025-09-11': [7478441290.0, 5025797331.0, 485255028.0, 41.2013, 0.0, 1940.0], '2025-09-12': [7424777012.0, 4973433142.0, 486815462.0, 41.2114, 0.0, 1872.0], '2025-09-15': [7331276386.0, 4995648281.0, 482815874.0, 41.209, 0.0, 1947.0], '2025-09-16': [7479119371.0, 5081289121.0, 568411312.0, 41.255, 0.0, 1955.0], '2025-09-17': [7450624095.0, 5057071063.0, 564659876.0, 41.2295, 0.0, 2035.0], '2025-09-18': [7484054652.0, 5075201503.0, 562653184.0, 41.2161, 0.0, 2152.0], '2025-09-19': [7471058273.0, 5051895187.0, 561708985.0, 41.241, 0.0, 2365.0], '2025-09-22': [7567883806.0, 5084345602.0, 562120675.0, 41.2344, 0.0, 2465.0], '2025-09-23': [7673704788.0, 5126981164.0, 561029248.0, 41.2983, 0.0, 2642.0], '2025-09-24': [7670128535.0, 5130501053.0, 561450312.0, 41.3375, 0.0, 3030.0], '2025-09-25': [7674965002.0, 5143962427.0, 559159280.0, 41.3726, 0.0, 3680.0], '2025-09-26': [7672895536.0, 5139959554.0, 558907803.0, 41.395, 0.0, 4069.0], '2025-09-29': [7715311895.0, 5202428585.0, 563779713.0, 41.4179, 0.0, 4577.0], '2025-09-30': [7591754442.0, 5085621552.0, 565413551.0, 41.4984, 0.0, 5122.0], '2025-10-01': [7813376353.0, 5209179332.0, 569993845.0, 41.5068, 0.0, 4564.0], '2025-10-02': [7854245751.0, 5202884521.0, 568427467.0, 41.513, 0.0, 3885.0], '2025-10-03': [7831924771.0, 5191843973.0, 567684149.0, 41.5283, 0.0, 3817.0], '2025-10-06': [7946215157.0, 5272712687.0, 571960775.0, 41.5226, 0.0, 3540.0], '2025-10-07': [8002498273.0, 5270437417.0, 570071162.0, 41.6225, 0.0, 3142.0], '2025-10-08': [8090837903.0, 5304318275.0, 574447322.0, 41.6217, 0.0, 3103.0], '2025-10-09': [8099156600.0, 5296869288.0, 489077878.0, 41.633, 0.0, 2745.0], '2025-10-10': [8003967678.0, 5192568455.0, 487049020.0, 41.6421, 0.0, 2133.0], '2025-10-13': [8091032285.0, 5189879220.0, 492983609.0, 41.6388, 0.0, 1715.0], '2025-10-14': [7978905091.0, 5213623660.0, 496464617.0, 41.7196, 0.0, 1916.0], '2025-10-15': [8129863307.0, 5325914346.0, 497829649.0, 41.7496, 0.0, 2496.0], '2025-10-16': [8249483081.0, 5427612353.0, 500792828.0, 41.7611, 0.0, 3024.0], '2025-10-17': [8397492348.999999, 5491066786.0, 508191692.0, 41.7736, 0.0, 3227.0], '2025-10-20': [8230565018.0, 5520379313.0, 499947400.0, 41.7855, 0.0, 3692.0], '2025-10-21': [8286454395.0, 5551552321.0, 501472552.0, 41.8768, 0.0, 3970.0], '2025-10-22': [8020576475.0, 5498701431.0, 487519536.0, 41.8872, 0.0, 4630.0], '2025-10-23': [8019115127.0, 5468107817.0, 484712827.0, 41.8966, 0.0, 4247.0], '2025-10-24': [7879005097.0, 5437175632.0, 480146102.0, 41.9042, 0.0, 3982.0], '2025-10-27': [7827050693.0, 5402546832.0, 476596596.0, 41.8427, 0.0, 3445.0], '2025-10-28': [7675053867.0, 5329011304.0, 469349254.0, 41.8699, 0.0, 3445.0], '2025-10-30': [7784835777.0, 5316612084.0, 474491586.0, 41.8699, 0.0, 2748.0], '2025-10-31': [7806329686.0, 5285671111.0, 478302462.0, 41.9, 0.0, 3065.0], '2025-11-03': [7784504399.0, 5239365293.0, 476118321.0, 41.8922, 0.0, 2602.0], '2025-11-04': [7840835936.0, 5359257773.0, 572396618.0, 41.9751, 0.0, 3038.0], '2025-11-05': [7850910959.0, 5378413405.0, 571258494.0, 42.0042, 0.0, 3294.0], '2025-11-06': [7895127562.0, 5353612905.0, 572133630.0, 42.0362, 0.0, 2916.0], '2025-11-07': [7872609170.0, 5292526587.0, 457581888.0, 42.0426, 0.0, 2690.0], '2025-11-10': [7984469814.0, 5351591964.0, 461584190.0, 42.049, 0.0, 2543.0], '2025-11-11': [8084344700.0, 5389094527.0, 465981847.0, 42.1124, 0.0, 2626.0], '2025-11-12': [7866141340.0, 5250373821.0, 466515504.0, 42.1526, 0.0, 2820.0], '2025-11-13': [8060927514.0, 5346803241.0, 462096946.0, 42.1636, 0.0, 2985.0], '2025-11-14': [8012799127.0, 5349276497.0, 458020247.0, 42.1724, 0.0, 3145.0], '2025-11-17': [7824059820.0, 5260105865.0, 452517390.0, 42.1661, 0.0, 2806.0], '2025-11-18': [7817110252.0, 5257198106.0, 451137230.0, 42.2525, 0.0, 2952.0], '2025-11-19': [7908219392.0, 5278764482.0, 454900412.0, 42.2641, 0.0, 3111.0], '2025-11-20': [7839399070.0, 5235954730.0, 451978671.0, 42.2756, 0.0, 3135.0], '2025-11-21': [7740841435.0, 5202227484.0, 480142909.0, 42.2882, 0.0, 3326.0], '2025-11-24': [7720094861.0, 5263779296.0, 483388373.0, 42.2819, 0.0, 3356.0], '2025-11-25': [7838873269.0, 5315461504.0, 488508964.0, 42.3675, 0.0, 3594.0], '2025-11-26': [7899663845.0, 5325580014.0, 483300282.0, 42.3668, 0.0, 3637.0], '2025-11-27': [7883858626.0, 5282939980.0, 481563940.0, 42.3578, 0.0, 3576.0], '2025-11-28': [7865389513.0, 5213689833.0, 479995413.0, 42.3624, 0.0, 3198.0], '2025-12-01': [8046867625.0, 5297641852.0, 485155707.0, 42.3547, 0.0, 3241.0], '2025-12-02': [8024136522.0, 5270272498.0, 480991346.0, 42.416, 0.0, 2804.0], '2025-12-03': [8042992842.0, 5245197349.0, 481428571.0, 42.3574, 0.0, 2439.0], '2025-12-04': [8045852423.0, 5205140964.0, 480477922.0, 42.3702, 0.0, 2048.0], '2025-12-05': [8008863720.0, 5114294003.0, 482724484.0, 42.3771, 0.0, 2091.0], '2025-12-08': [8025532323.0, 5143453937.0, 481950643.0, 42.3606, 0.0, 2430.0], '2025-12-09': [8059983968.0, 5173653163.0, 482453540.0, 42.4735, 0.0, 3027.0], '2025-12-10': [8074939310.0, 5215529134.0, 482135345.0, 42.497, 0.0, 3963.0], '2025-12-11': [8130438077.0, 5251376664.0, 483122354.0, 42.5179, 0.0, 4340.0], '2025-12-12': [8221304792.0, 5239240445.0, 487889967.0, 42.5343, 0.0, 4312.0], '2025-12-15': [8199862656.0, 5222651778.0, 483056162.0, 42.5344, 0.0, 3682.0], '2025-12-16': [8200065339.0, 5217854699.0, 482037679.0, 42.6211, 0.0, 3171.0], '2025-12-17': [8242296356.0, 5232995829.0, 482336357.0, 42.6317, 0.0, 3270.0], '2025-12-18': [8293366915.0, 5273604714.0, 483414629.0, 42.6516, 0.0, 3517.0], '2025-12-19': [8306455689.0, 5275235098.0, 482916986.0, 42.6585, 0.0, 3533.0], '2025-12-22': [8340775118.0, 5316329114.0, 487049272.0, 42.6532, 0.0, 3970.0], '2025-12-23': [8474278301.999999, 5382529744.0, 489680065.0, 42.734, 0.0, 4397.0], '2025-12-24': [8476720150.0, 5396992784.0, 487607607.0, 42.7474, 0.0, 4367.0], '2025-12-25': [8501098418.0, 5392790000.0, 487129621.0, 42.7434, 0.0, 4353.0], '2025-12-26': [8414758198.000001, 5390312756.0, 485891332.0, 42.7641, 0.0, 4904.0], '2025-12-29': [8352311331.0, 5378331002.0, 482145950.0, 42.7656, 0.0, 4919.0], '2025-12-30': [8288819577.0, 5360510523.0, 477619490.0, 42.8542, 0.0, 4716.0], '2025-12-31': [7978781696.0, 5146521445.0, 475112545.0, 42.8623, 0.0, 4640.0], '2026-01-02': [8195716534.0, 5334196141.0, 500323594.0, 42.8457, 0.0, 3104.0], '2026-01-05': [8310828098.0, 5362298895.0, 510102191.0, 42.881, 0.0, 3294.0], '2026-01-06': [8386166306.0, 5396701203.0, 512017962.0, 42.9648, 0.0, 3878.0], '2026-01-07': [8438742128.000001, 5387287880.0, 513290764.0, 42.9663, 0.0, 4100.0], '2026-01-08': [8467962184.0, 5401537006.0, 512785632.0, 42.9743, 0.0, 4332.0], '2026-01-09': [8535106274.0, 5396287861.0, 505565141.0, 42.9747, 0.0, 4519.0], '2026-01-12': [8678373643.0, 5416290058.0, 506821864.0, 42.9863, 0.0, 4262.0], '2026-01-13': [8745029655.0, 5421154015.0, 485688470.0, 43.0609, 0.0, 4124.0], '2026-01-14': [8961877879.0, 5549576784.0, 639285552.0, 43.0765, 0.0, 4053.0], '2026-01-15': [8924058232.0, 5541990334.0, 629758544.0, 43.0925, 0.0, 4447.0], '2026-01-16': [8951444531.0, 5579968780.0, 628922094.0, 43.1147, 0.0, 4901.0], '2026-01-19': [9013930672.0, 5594972235.0, 631095335.0, 43.0883, 0.0, 5297.0], '2026-01-20': [9080459954.0, 5653359772.0, 635266822.0, 43.1943, 0.0, 5393.0], '2026-01-21': [9272153080.0, 5711902130.0, 636292329.0, 43.2061, 0.0, 5163.0], '2026-01-22': [9274713532.0, 5731660015.0, 556472230.0, 43.2165, 0.0, 5309.0], '2026-01-23': [9422034244.0, 5773245705.0, 562263896.0, 43.2247, 0.0, 5146.0], '2026-01-26': [9391779754.0, 5599582458.0, 378446670.0, 43.2036, 0.0, 5196.0], '2026-01-27': [9472574643.0, 5665298596.0, 377012413.0, 43.2978, 0.0, 5410.0], '2026-01-28': [9826822329.0, 5888043748.0, 527721334.99999994, 43.3158, 0.0, 5921.0], '2026-01-29': [10119576720.0, 6071131553.0, 538486647.0, 43.3327, 0.0, 5982.0], '2026-01-30': [9562620648.0, 5952280234.0, 518182907.0, 43.3443, 0.0, 5981.0], '2026-02-02': [8930692532.0, 5715156157.0, 499042310.0, 43.3414, 0.0, 6121.0], '2026-02-03': [9155250436.0, 5719026110.0, 512799644.0, 43.4168, 0.0, 5788.0], '2026-02-04': [9347030159.0, 5757177213.0, 551362565.0, 43.4142, 0.0, 4951.0], '2026-02-05': [9181826266.0, 5694866022.0, 542220549.0, 43.4245, 0.0, 4539.0], '2026-02-06': [9123784092.0, 5615014988.0, 542961121.0, 43.4555, 0.0, 3912.0], '2026-02-09': [9227722464.0, 5669070201.0, 548205194.0, 43.4542, 0.0, 3176.0], '2026-02-10': [9452895061.0, 5787905065.0, 649644758.0, 43.5182, 0.0, 2521.0], '2026-02-11': [9465879563.0, 5789094018.0, 651852222.0, 43.545, 0.0, 2551.0], '2026-02-12': [9463794051.0, 5785698319.0, 552416474.0, 43.5633, 0.0, 2598.0], '2026-02-13': [9336061268.0, 5722369694.0, 549175919.0, 43.5702, 0.0, 2989.0], '2026-02-16': [9388020968.0, 5727603941.0, 550769525.0, 43.5433, 0.0, 3090.0], '2026-02-17': [9165476112.0, 5642531287.0, 548410525.0, 43.6375, 0.0, 3260.0], '2026-02-18': [9102546649.0, 5559542542.0, 472939139.0, 43.6524, 0.0, 3316.0], '2026-02-19': [9185367970.0, 5583273709.0, 469774090.0, 43.6747, 0.0, 3424.0], '2026-02-20': [9106831174.0, 5602906340.0, 467403026.0, 43.6913, 0.0, 3658.0], '2026-02-23': [9199145202.0, 5723433759.0, 468929832.0, 43.6883, 0.0, 3805.0], '2026-02-24': [9331435448.0, 5790133342.0, 470080880.0, 43.7542, 0.0, 3654.0], '2026-02-25': [9336020077.0, 5742193320.0, 465747636.0, 43.7775, 0.0, 3401.0], '2026-02-26': [9384120567.0, 5766823550.0, 466888532.0, 43.7888, 0.0, 3552.0], '2026-02-27': [9324614981.0, 5683765634.0, 468174999.0, 43.8026, 0.0, 3417.0], '2026-03-02': [9468599211.0, 5817776087.0, 477151696.0, 43.8, 0.0, 3438.0], '2026-03-03': [9133535038.0, 5851066724.0, 471388699.0, 43.885, 0.0, 3344.0], '2026-03-04': [8928908209.0, 5877438191.0, 515429213.0, 43.8913, 0.0, 3231.0], '2026-03-05': [8912958243.0, 5833090205.0, 510400850.0, 43.8932, 0.0, 2827.0], '2026-03-06': [8788913941.0, 5744765237.0, 507648648.0, 43.9135, 0.0, 2646.0], '2026-03-09': [8534381582.0, 5824846946.0, 511650565.0, 43.9055, 0.0, 2319.0], '2026-03-10': [8520734889.0, 5929176863.0, 515998982.0, 44.0007, 0.0, 2175.0], '2026-03-11': [8625290089.0, 5895225745.0, 514779741.0, 43.9826, 0.0, 1972.0], '2026-03-12': [8644016312.0, 5843957123.0, 502221510.0, 43.9964, 0.0, 1683.0], '2026-03-13': [8457417033.0, 5845790772.0, 496612318.0, 44.031, 0.0, 1695.0], '2026-03-16': [8184052219.0, 5888360190.0, 492055191.0, 44.021, 0.0, 1863.0], '2026-03-17': [8329451083.0, 5971220870.0, 495485540.0, 44.106, 0.0, 2270.0], '2026-03-18': [8347963962.0, 5936424411.0, 465348087.0, 44.1207, 0.0, 2581.0], '2026-03-19': [7934307764.0, 5780880425.0, 455943597.0, 44.1325, 0.0, 2581.0], '2026-03-23': [7218576482.0, 5708033908.0, 436758074.0, 44.1325, 0.0, 2534.0], '2026-03-24': [7234315407.0, 5711076700.0, 434505711.0, 44.2435, 0.0, 2397.0], '2026-03-25': [7215307273.0, 5819587520.0, 440244618.0, 44.2636, 0.0, 2038.0], '2026-03-26': [7024794445.0, 5805232414.0, 434780395.0, 44.2737, 0.0, 2328.0], '2026-03-27': [6986856579.0, 5783013326.0, 435576680.0, 44.2828, 0.0, 2416.0], '2026-03-30': [6746084438.0, 5673976220.0, 438614227.0, 44.2887, 0.0, 1710.0], '2026-03-31': [6813670372.0, 5518856738.0, 446717267.0, 44.3841, 4901.0, 1313.0], '2026-04-01': [7218014578.0, 5721868340.0, 453113582.0, 44.3961, 8262.0, 1190.0], '2026-04-02': [7239079104.0, 5619807476.0, 448583948.0, 44.3938, 10757.0, 1272.0], '2026-04-03': [7307230115.0, 5620271374.0, 447438754.0, 44.4155, 12227.0, 1638.0], '2026-04-06': [7237312174.0, 5563117065.0, 447584040.0, 44.4306, 13517.0, 1803.0], '2026-04-07': [7304981200.0, 5621236418.0, 448512373.0, 44.5085, 11356.0, 2049.0], '2026-04-08': [7429363781.0, 5699598570.0, 455351360.0, 44.5255, 10840.0, 2170.0], '2026-04-09': [7651693838.0, 5619308369.0, 450071465.0, 44.4527, 9545.0, 2025.0], '2026-04-10': [7729110390.0, 5604917387.0, 383082400.0, 44.5102, 8575.0, 1706.0], '2026-04-13': [7787767563.0, 5585770725.0, 379143224.0, 44.5157, 7670.0, 1738.0], '2026-04-14': [7855548387.0, 5578095954.0, 378168954.0, 44.6355, 5710.0, 1761.0], '2026-04-15': [7913545613.0, 5562953745.0, 360589824.0, 44.6474, 4595.0, 1618.0], '2026-04-16': [8016519657.0, 5557924912.0, 360624526.0, 44.6681, 4280.0, 1555.0], '2026-04-17': [7913416864.0, 5568086449.0, 356575060.0, 44.6788, 4190.0, 1727.0], '2026-04-20': [7945521971.0, 5617218476.0, 355015722.0, 44.6989, 4150.0, 2028.0], '2026-04-21': [8009715582.0, 5659769505.0, 350649593.0, 44.7952, 3750.0, 2175.0], '2026-04-22': [8033142051.0, 5685496198.0, 434611435.0, 44.8101, 3700.0, 2326.0], '2026-04-24': [7789923157.0, 5694448020.0, 431717372.0, 44.8132, 3600.0, 2081.0], '2026-04-27': [7760077827.0, 5718462652.0, 433243959.0, 44.8541, 3600.0, 2066.0], '2026-04-28': [7698397567.0, 5657696695.0, 430708525.0, 44.9393, 3600.0, 2201.0], '2026-04-29': [7720734157.0, 5655379754.0, 425545181.0, 44.9705, 3600.0, 2498.0], '2026-04-30': [7567793128.0, 5485860610.0, 428447588.0, 44.985, 3600.0, 2722.0], '2026-05-04': [7470006436.0, 5634786090.0, 430015132.0, 44.9692, 2215.0, 2404.0], '2026-05-05': [7471057988.0, 5584200273.0, 429517764.0, 45.104, 1855.0, 2240.0], '2026-05-06': [7623053554.0, 5654268757.0, 431754448.0, 45.1339, 1760.0, 2063.0], '2026-05-07': [7824292743.0, 5736016166.0, 427708489.0, 45.1436, 1580.0, 1912.0], '2026-05-08': [7868563410.0, 5747964126.0, 427372200.0, 45.1566, 1535.0, 1979.0], '2026-05-11': [7799893060.0, 5742884738.0, 425406554.0, 45.19, 1655.0, 2169.0], '2026-05-12': [7978754846.0, 5780270855.0, 434158547.0, 45.2951, 1655.0, 2349.0], '2026-05-13': [7971338969.0, 5823410368.0, 427791739.0, 45.313, 1405.0, 2655.0], '2026-05-14': [7993097138.0, 5850962516.0, 428073561.0, 45.3307, 1405.0, 2965.0], '2026-05-15': [7768610384.0, 5725064752.0, 423733639.0, 45.35, 1405.0, 3048.0], '2026-05-18': [7674826744.0, 5684287902.0, 417443312.0, 45.39, 1405.0, 2938.0], '2026-05-20': [7689871002.0, 5650291922.0, 395907439.0, 45.45, 1405.0, 1779.0], '2026-05-21': [7679533564.0, 5574703227.0, 396134546.0, 45.51, 1405.0, 1476.0], '2026-05-22': [7415421158.0, 5553091137.0, 369991447.0, 45.5277, 3185.0, 1526.0], '2026-05-25': [7433191995.0, 5533670617.0, 370045468.0, 45.5532, 3185.0, 1145.35469], '2026-05-26': [7367691901.0, 5576913616.0, 370984256.0, 45.6312, 3715.0, 1145.35469], '2026-06-01': [7333995744.0, 5718932068.0, 372717698.0, 45.6312, 3455.0, 1288.34664], '2026-06-02': [7491946958.0, 5711366132.0, 374711189.0, 45.8248, 2925.0, 1447.60306], '2026-06-03': [7463198842.0, 5650278421.0, 370783749.0, 45.8402, 2975.0, 1640.28565], '2026-06-04': [7464507342.0, 5609618218.0, 371415280.0, 45.8696, 2730.0, 1567.64036], '2026-06-05': [7448355770.0, 5558510085.0, 372014628.0, 45.8873, 2640.0, 1850.31107], '2026-06-08': [7288474933.0, 5515264194.0, 368427517.0, 45.9084, 1840.0, 1988.33199], '2026-06-09': [7386787220.0, 5547227078.0, 371423194.0, 46.0144, 1840.0, 2376.04445], '2026-06-10': [7260545475.0, 5468374969.0, 429458995.0, 46.0328, 1820.0, 2518.45557], '2026-06-11': [7108116912.0, 5366545058.0, 424966124.0, 46.0491, 1820.0, 2517.6056], '2026-06-12': [7133328284.0, 5355049557.0, 405348970.0, 46.07, 1820.0, 2900.88879], '2026-06-15': [7333537069.0, 5436601226.0, 403813472.0, 46.09, 1820.0, 2590.58272], '2026-06-16': [7494509695.0, 5486221495.0, 285463711.0, 46.19, 1820.0, 2338.79691], '2026-06-17': [7714385045.0, 5429932351.0, 285232270.0, 46.21, 1830.0, 1792.87364], '2026-06-18': [7495454184.0, 5244202169.0, 283482558.0, 46.23, 1830.0, 1667.60006], '2026-06-19': [7393466637.0, 5164738018.0, 280440213.0, 46.25, 1830.0, 1398.52177], '2026-06-22': [7391714878.0, 5260388749.0, 283324447.0, 46.2724, 1110.0, 1880.23997], '2026-06-23': [7306212293.0, 5214303265.0, 282701621.0, 46.3758, 1110.0, 2530.54644], '2026-06-24': [7281872852.0, 5236175669.0, 278370818.0, 46.3925, 1100.0, 3064.81726], '2026-06-25': [7094405023.0, 5136841275.0, 278065452.0, 46.4099, 1100.0, 3324.8085], '2026-06-26': [7041306905.0, 5123643501.0, 278662706.0, 46.4302, 1100.0, 3223.94172], '2026-06-29': [7163722048.0, 5163762139.0, 277079022.0, 46.4482, 1100.0, 2562.46562], '2026-06-30': [7002875182.0, 5012463111.0, 276022929.0, 46.5551, 1100.0, 1985.38587], '2026-07-01': [7092276847.0, 5108117082.0, 269278278.0, 46.5747, 1100.0, 2158.17903], '2026-07-02': [7479183562.0, 5260028915.0, 398575514.0, 46.5867, 1100.0, 2541.01778], '2026-07-03': [7568962334.0, 5300707088.0, 401621188.0, 46.61, 1100.0, 2469.46284], '2026-07-06': [7697206611.0, 5397363803.0, 400079128.0, 46.63, 750.0, 2950.89], '2026-07-07': [7789610153.0, 5418900601.0, 400731880.0, 46.74, 750.0, 2921.81], '2026-07-08': [7779718138.0, 5432544520.0, 398928370.0, 46.75, 350.0, 2789.41], '2026-07-09': [7796284393.0, 5425902923.0, 390093830.0, 46.77, 350.0, 2752.99], '2026-07-10': [7767467595.0, 5428997122.0, 385385920.0, 46.79, 150.0, 2889.63], '2026-07-13': [7751577680.0, 5412545438.0, 307094229.0, 46.8084, 150.0, 3157.80285], '2026-07-14': [7721187803.0, 5309890672.0, 302699285.0, 46.9121, 150.0, 3303.49295], '2026-07-16': [7799910201.0, 5375757483.0, 296157854.0, 46.93, 150.0, 2819.58958], '2026-07-17': [7663478482.0, 5455984867.0, 295253731.0, 46.97, 150.0, 3347.14962], '2026-07-20': [7797885606.0, 5453392252.0, 295831679.0, 46.99, 150.0, 3074.37], '2026-07-21': [7941512504.0, 5513940048.0, 297921962.0, 47.09, 150.0, 3513.84], '2026-07-22': [8011004338.0, 5624398077.0, 300471598.0, 47.11, 150.0, 4011.9], '2026-07-23': [7971936256.0, 5621644669.0, 297973299.0, 47.13, 150.0, 3998.8], '2026-07-24': [7802053404.0, 5572816601.0, 296927790.0, 47.15, 150.0, 3739.82], '2026-07-27': [7845777004.0, 5622119019.0, 298301405.0, 47.1647, 150.0, 3774.96], '2026-07-28': [7957463675.0, 5579977452.0, 296882870.0, 47.2681, 150.0, 3594.48], '2026-07-29': [8025185457.0, 5536559721.0, 296995228.0, 47.2899, 150.0, 3752.62], '2026-07-30': [8002020972.0, 5544629984.0, 296400853.0, 47.3075, 150.0, 3475.8], '2026-07-31': [7907263946.0, 5543644076.0, 297919351.0, 47.3274, 150.0, 3395.19], '2026-08-03': [7885801180.0, 5610425047.0, 296993121.0, 47.3452, 150.0, 3554.61954], '2026-08-04': [8085093360.0, 5637267743.0, 297387697.0, 47.4497, 150.0, 3684.96202], '2026-08-05': [8360867413.0, 5718096643.0, 299186042.0, 47.4694, 0.0, 3778.10231], '2026-08-06': [8584468698.000001, 5767737842.0, 302200683.0, 47.4881, 0.0, 3684.10496], '2026-08-07': [8589660693.0, 5805488925.0, 302776439.0, 47.5055, 0.0, 3798.92639], '2026-08-10': [8723025794.0, 5853896004.0, 299245685.0, 47.5229, 0.0, 3744.71427], '2026-08-11': [8939036561.0, 5896182457.0, 301130495.0, 47.626, 0.0, 3400.18314], '2026-08-12': [8959327011.0, 5867885661.0, 307215410.0, 47.6505, 0.0, 3275.67593], '2026-08-13': [8905843375.0, 5877070497.0, 293586705.0, 47.6678, 0.0, 3556.02521], '2026-08-14': [8865684517.0, 5891006352.0, 292511506.0, 47.6858, 0.0, 3479.52857], '2026-08-17': [8947844701.0, 5930848698.0, 294510842.0, 47.72, 0.0, 3719.95], '2026-08-18': [9074065071.0, 5959273503.0, 294209617.0, 47.81, 0.0, 4012.24], '2026-08-19': [9012051157.0, 5980165023.0, 287791389.0, 47.83, 0.0, 4215.53], '2026-08-20': [9177788975.0, 6055414008.0, 290703699.0, 47.85, 0.0, 4694.93], '2026-08-21': [9150654657.0, 6133799052.0, 294651372.0, 47.86, 0.0, 5138.09], '2026-08-24': [9192091030.0, 6210207598.0, 297608411.0, 47.8799, 0.0, 5416.3001], '2026-08-25': [9266248402.0, 6226597639.0, 297431631.0, 47.9924, 0.0, 5546.93019], '2026-08-26': [9307618164.0, 6211098501.0, 292825804.0, 48.0117, 0.0, 5616.88902], '2026-08-27': [9274064789.0, 6177421354.0, 291669482.0, 48.0292, 0.0, 5471.2527], '2026-08-28': [9189313380.0, 6157810548.0, 292028078.0, 48.0466, 0.0, 5784.81961], '2026-08-31': [9107049689.0, 6120514645.0, 294712480.0, 48.0732, 0.0, 5642.84], '2026-09-01': [8884371826.0, 6008983042.0, 285452500.0, 48.1745, 0.0, 5431.33], '2026-09-02': [8843029815.0, 5980341741.0, 283021763.0, 48.1891, 0.0, 5416.55], '2026-09-03': [8978373345.0, 6042209337.0, 286421481.0, 48.2075, 0.0, 5064.38], '2026-09-04': [9013172795.0, 6031249184.0, 287516310.0, 48.22, 0.0, 4337.65], '2026-09-07': [8976633850.0, 6009781762.0, 284692140.0, 48.2326, 0.0, 4217.92082], '2026-09-08': [8888874326.0, 5979883473.0, 284780973.0, 48.3465, 0.0, 4330.50153], '2026-09-09': [8971883015.0, 6027999781.0, 284925836.0, 48.3727, 0.0, 4337.32894], '2026-09-10': [8922436124.0, 6000206478.0, 276918505.0, 48.3905, 0.0, 4402.36453], '2026-09-11': [8779468631.0, 5949595520.0, 275283731.0, 48.4069, 0.0, 4270.36841], '2026-09-14': [8718542957.0, 5920061121.0, 270628104.0, 48.4305, 0.0, 4208.24], '2026-09-15': [8623353324.0, 5859205431.0, 265291555.99999997, 48.5343, 0.0, 3971.48], '2026-09-16': [8721160377.0, 5863051512.0, 269469222.0, 48.5585, 0.0, 3766.99], '2026-09-17': [8555188447.000001, 5959852036.0, 266299617.00000003, 48.5779, 0.0, 3754.76], '2026-09-18': [8607148693.0, 6052874147.0, 268666861.0, 48.5873, 0.0, 3789.69], '2026-09-21': [8581557732.000001, 6062635982.0, 267322072.99999997, 48.6116, 0.0, 3776.09026], '2026-09-22': [8616386786.0, 6012261757.0, 262108752.0, 48.7131, 0.0, 3888.79842], '2026-09-23': [8696565860.0, 6043683215.0, 257087851.0, 48.7323, 0.0, 3823.92091], '2026-09-24': [8605220110.0, 5983924682.0, 255789738.0, 48.7499, 400.0, 3880.586], '2026-09-25': [8471752151.000001, 6027574810.0, 258081542.0, 48.7671, 800.0, 3814.106], '2026-09-28': [8325622406.0, 5967632162.0, 250773923.0, 48.7901, 1300.0, 3668.77401], '2026-09-29': [8367260343.0, 5930221912.0, 250828656.0, 48.9008, 2400.0, 3850.41883], '2026-09-30': [8314535676.0, 5810739875.0, 327764331.0, 48.9131, 3650.0, 3649.3933], '2026-10-01': [8264501364.0, 5902090341.0, 327281487.0, 48.9303, 3250.0, 3514.13924], '2026-10-02': [8303088944.0, 5909459571.0, 327653381.0, 48.9466, 2850.0, 3449.49993], '2026-10-05': [8308731939.0, 5923141438.0, 329791110.0, 48.9699, 2350.0, 3317.21272], '2026-10-06': [8449142960.000001, 5919241765.0, 329772784.0, 49.0732, 1250.0, 3243.9289], '2026-10-07': [8516848958.000001, 6081346345.0, 353457491.0, 49.0918, 0.0, 3404.88527], '2026-10-08': [8580546861.0, 6047360431.0, 352492593.0, 49.1091, 0.0, 2876.40475]}
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
    forward=other=None
    def norm(value):
        return unicodedata.normalize('NFKD',str(value).lower().replace('ı','i')).encode('ascii','ignore').decode()
    for row in rows:
        text=' '.join(norm(v) for v in row.values() if isinstance(v,str))
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
        if isinstance(a,(int,float)) and isinstance(b,(int,float)):
            result.append({'date':d,'total':a+b,'source':'TCMB haftalık rezerv/likidite tablosu'})
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


def daily_fx_records(records,weekly,now,*,use_workbook=True):
    if not all(c in records for c in FX_RAW):raise DataError('Günlük net döviz hesabının bazı girdileri eksik')
    values={c:{r['date']:r['value'] for r in records[c]['observations']} for c in FX_RAW}
    # Preserve the submitted historical archive's input precision without changing raw API records.
    for d,archived in FX_HISTORICAL_INPUTS.items():
        if all(d in values[c] for c in FX_RAW):
            for c,v in zip(FX_RAW,archived):values[c][d]=v
    assets,liabilities,treasury,usd,buy,sell=[values[c] for c in FX_RAW]
    workbook=WORKBOOK_FX if use_workbook else {}
    for d,(a,l,h,u,j,_) in workbook.items():
        if d>now.date().isoformat():continue
        assets[d],liabilities[d],treasury[d],usd[d]=a*1000,l*1000,h*1000,u
        buy[d],sell[d]=max(-j,0),max(j,0)
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
        if d in workbook:valid=True
        if not valid:
            # Never compute a daily change across an internal missing-input gap.
            level.append({'date':d,'value':None})
            last_value=None;last_date=None;continue
        anchor_day,q=anchors[anchor_index] if anchor_index>=0 else (d,0.)
        if d in workbook:anchor_day,q=d,workbook[d][5]-domestic[d]
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
    if previous.get('date_parser_version')!=2 or not reusable_after_release(pseudo,spec,now):
        try:
            fetched=fetch_weekly_liquidity(now.date())
            for r in fetched:
                by_date[r['date']]={**by_date.get(r['date'],{}),**r}
            cache['_fx_weekly']={'fetched_at':now.isoformat(timespec='seconds'),
                                 'date_parser_version':2,'rows':list(by_date.values())}
        except (DataError,ValueError,zipfile.BadZipFile,ET.ParseError) as error:
            errors.append('Haftalık swap düzeltmesi: '+str(error))
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
    derived,_=daily_fx_records(records,weekly,datetime.now(TR),use_workbook=False);records.update(derived)
    attach_analytic_daily(records,[])
    prices=[];stocks=[]
    for k in range(1100):
        d=today-timedelta(days=1099-k)
        if d.weekday()<5:
            prices.append({'date':d.isoformat(),'value':2500+k*.5})
            if d.weekday()==4:stocks.append({'date':d.isoformat(),'value':80000.})
    records['TP.AB.C1']['observations']=stocks
    for code,grams in [(NET_GOLD_ASSET,800e6),*[(c,500e6) for c in WORKBOOK_LIABILITY_CODES]]:
        records[code]={**CURATED_SPECS[code],'observations':[{'date':r['date'],'value':grams} for r in stocks],
            'fetched_at':datetime.now(TR).isoformat(timespec='seconds')}
    records['BFRC.GOLD.SWAP.USD']={'observations':[{'date':r['date'],'value':2500.} for r in prices]}
    records[GOLD_ADJUSTED_CODE]=gold_adjustment_records(records,prices,today,use_workbook=False)
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
GOLD_ADJUSTED_CODE='BFRC.NETFX.GOLD.ADJUSTED.CHANGE'
GOLD_LEVEL_CODE='BFRC.NETFX.GOLD.ADJUSTED.LEVEL'
GOLD_CHART_TITLE='Swap ve Hazine Hariç Net Döviz Pozisyonu - Değişimi (Altın hariç)'
# Source inputs transcribed from the supplied workbook, not frozen output values.
import zlib
_WORKBOOK_INPUTS=(
    'eNp8nWmSLLmRpO/Sv7NcsC9zlZY5yVx+9FNDRMApXd1NIR9ZLy3hgO2L2n//v/8qqeR/kv61/uv//PdYs471lD7/xihptWflvzVa708v+W8+Zd'
    'b690+rI+W/f0Zuuf/fvy+JnCGxcy3j2bmJxMgrP6W0vzVLTU+pTTRq25DoY0CirnSTKCIxU059Py3XvzF7r/up+nVrztGfkqdItFYzNOrwMfJ8'
    'HaOaRq0zPfySMfcs+5l8ysr6lNEGNEZa0EgcR38e9abR4hx5DJ23i0bdXRfDl/S5H50bEmV1SLTmY5T+OkbnNuberT2zcKFr7P2sNf9W36U+e3'
    'OMuj40JjTSetFYQaOs9ST9EtEoPT85d9GYsz+p+EZ1N6bRq2nsfNPYfti09cv34jpabuvJemN9y0hPLYtv2b2IRpk9cR061kWjJNMYPRVxxBaN'
    'OVcSZ+w5dLDmJ/F3lF0LP9/n/awlOEP/X56pFxxrlboe7m2NtfMzdceiUQffUdZuplHLTSNYo+o36OrK39ht1/rMUeDQpp+e3XdRBzTq4C5Smu'
    'Om0eNZt9iywaE76ZueUTlHbXwcJHZ8yrmK9uKMMiBR9Br7EZ2/mVsRia6HyGJEnWhZUqq4DiKpir36nv31LdNEWm96wiqOzrvnIvnoJlKKLrXG'
    'x3CSPLmXvttLVsoyEQmVbkECoovNqT09bYiIGXU+c0fXA0GkDBOp+yYCd8y8dJf69ZxEMjseSbyJ5Pyk4ZNIgCHSd4JIGT8WK6JjIjUnvWTRK6'
    'QlkXv64iSpi9dTQ2xrybxN9hP13dO8icT77ix2WBsiUg3p2bpNScWuTYLI55Ts19GNVYiM0m4i1Yy2kTT9vB6496IH3lVEpNuqbgOpy/qe607q'
    'fp0khF/Ps62sxl6zJcurTpKkB5ZfJ7e9fCdj+U6uixURi7+eVe8An8P7QyyT4JPKE/eWfZLi19Ebi4gkMt1ELP9b2rA+Qw8g5ZoK91h5HbEpYi'
    'ciaS50qt6rmMgaN5FQAFWa9JEak/DNJlaXQoDI0PnKhsZAYppuqUOjpvtrcoqDJGmOLt5A+JbONM31vaFa+JoE84uITANE8qVFyrEP0iJT2iwN'
    'vqZsRId7FcMsqQEeJ/Xsk/Tpk+iabyIliIi2zQJXIjZZUuZ64TX4EH9OS9xrygOGnWu+iPhxVp5pyKQ1X8niJNBoUrbFOlE/rPuUOlim0feLxr'
    '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'
    'UMZI3HhX6uRTt3T/GjHz4//+v//9//Eg38s='
)
WORKBOOK_FX,WORKBOOK_GOLD=json.loads(zlib.decompress(base64.b64decode(_WORKBOOK_INPUTS)))
WORKBOOK_LAST_DAY=max(WORKBOOK_GOLD)
NET_GOLD_ASSET='TP.BL0021'
WORKBOOK_LIABILITY_CODES=('TP.BL0822','TP.BL132','TP.BL136','TP.BL089','TP.BL141')
for code in (NET_GOLD_ASSET,*WORKBOOK_LIABILITY_CODES):
    CURATED_SPECS[code]={'id':code,'code':code,'name':code,
        'unit':'Safi Gram' if code==NET_GOLD_ASSET else 'Bin TL',
        'frequency':'HAFTALIK(CUMA)','group_code':'bie_mbblnch',
        'group':'Merkez Bankası Haftalık Vaziyeti (Yeni)','provider':'evds3','source':'TCMB / EVDS'}
GOLD_ADJUSTMENT_METHOD=('Excel: AE = Z + AB × Y − AD; seviye = P − AE; günlük değişim = ΔP − ΔAE. '
    'Arşiv sonrası: haftalık varlık ve pasiflerden ons miktarı, günlük AM fiyatı ve ayrı altın swap stoku kullanılır.')

CURATED_CHARTS.insert(0,{'id':'netfx-altin-haric-degisim','name':GOLD_CHART_TITLE,
    'title':GOLD_CHART_TITLE,'unit':'Milyon ABD doları','daily_bars':True,'default_years':.5,
    'method':'',
    'lines':[line(GOLD_ADJUSTED_CODE,'Altın hariç günlük değişim',color='#432454')]})

CURATED_CHARTS.insert(0,{'id':'netfx-altin-haric','name':'Swap ve Hazine Hariç Net Döviz Pozisyonu (Altın hariç)',
    'title':'Swap ve Hazine Hariç Net Döviz Pozisyonu (Altın hariç)',
    'unit':'Milyar ABD doları',
    'area':True,'default_years':.5,'method':'',
    'lines':[line(GOLD_LEVEL_CODE,'Altın hariç net pozisyon',.001,color='#432454')]})

def attach_gold_level(records):
    adjusted=records[GOLD_ADJUSTED_CODE]
    records[GOLD_LEVEL_CODE]={**adjusted,'id':GOLD_LEVEL_CODE,'code':GOLD_LEVEL_CODE,
        'name':'Altın hariç net döviz pozisyonu',
        'observations':[{'date':r['date'],'value':r['adjusted_level_million_usd']} for r in adjusted['level_audit']]}

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

def gold_adjustment_records(records,prices,today,*,use_workbook=True):
    def points(code):
        return {r['date']:float(r['value']) for r in records.get(code,{}).get('observations',[])
                if isinstance(r.get('value'),(int,float)) and math.isfinite(r['value']) and r['date']<=today.isoformat()}
    levels=points('BFRC.NETFX.TREASURYEX')
    am={r['date']:r['value'] for r in prices if r['date']<=today.isoformat() and r['value']>0}
    grams=points(NET_GOLD_ASSET)
    liabilities=[points(c) for c in WORKBOOK_LIABILITY_CODES]
    usd=points('TP.DK.USD.A.YTL')
    swaps=points('BFRC.GOLD.SWAP.USD')
    # Workbook liabilities are negative USD values; freeze the ounce amount at the weekly AM.
    anchors=[]
    for d,asset in sorted(grams.items()):
        if d not in am or usd.get(d,0)<=0 or any(d not in m or m[d]<0 for m in liabilities):continue
        ounces=round(asset/31.1034768/1e6,3)
        passive_ounces=-sum(m[d] for m in liabilities)/(1000*usd[d]*am[d])
        anchors.append((date.fromisoformat(d)+timedelta(days=7),d,ounces,passive_ounces))
    output=[];audit=[];level_audit=[];previous=None
    for d,p in sorted(levels.items()):
        old=WORKBOOK_GOLD.get(d) if use_workbook else None
        if old:
            ounces,price,gross,passive_ounces,swap=old
            ref='Excel X/AB';basis='Excel'
        else:
            eligible=[a for a in anchors if a[0]<=date.fromisoformat(d) and (date.fromisoformat(d)-date.fromisoformat(a[1])).days<=28]
            if not eligible or d not in am or d not in swaps:
                previous=None
                if level_audit:level_audit.append({'date':d,'adjusted_level_million_usd':None})
                continue
            _,ref,ounces,passive_ounces=eligible[-1]
            price=am[d];swap=swaps[d];gross=ounces*price;basis='EVDS'
        passive=passive_ounces*price
        net_gold=gross+passive-swap
        row={'date':d,'weekly_gold_reference':ref,'source':basis,'gold_million_ounces':ounces,
             'gold_price_usd':price,'gross_gold_million_usd':gross,
             'passive_million_ounces':passive_ounces,'gold_liabilities_signed_million_usd':passive,
             'gold_swap_million_usd':swap,'net_gold_million_usd':net_gold,
             'raw_level_million_usd':p,'adjusted_level_million_usd':p-net_gold}
        level_audit.append(row)
        if previous and (date.fromisoformat(d)-date.fromisoformat(previous['date'])).days<=5:
            raw_change=p-previous['raw_level_million_usd']
            gold_change=net_gold-previous['net_gold_million_usd']
            adjusted=raw_change-gold_change
            output.append({'date':d,'value':adjusted})
            audit.append({**row,'previous_date':previous['date'],'raw_change_million_usd':raw_change,
                'net_gold_change_million_usd':gold_change,'adjusted_change_million_usd':adjusted})
        previous=row
    if not output:raise DataError('Altın hariç hesap için iki ardışık ortak gözlem bulunamadı')
    return {'id':GOLD_ADJUSTED_CODE,'code':GOLD_ADJUSTED_CODE,'provider':'derived',
        'name':GOLD_CHART_TITLE,'unit':'Milyon ABD doları','frequency':'İŞ GÜNÜ',
        'source':'TCMB / EVDS; London AM — Westmetall; Excel arşivi','estimated':True,
        'method':GOLD_ADJUSTMENT_METHOD,'observations':output,'audit':audit,'level_audit':level_audit,
        'fetched_at':datetime.now(TR).isoformat(timespec='seconds')}

def attach_gold_swap_series(records,today):
    # Find a dedicated gold-swap sell stock in millions of USD. Never substitute total FX swaps.
    with session() as client:
        meta=evds3_json(client,'serieList/',{'type':'json','code':'bie_swaptektarf'})
        if not isinstance(meta,list):raise DataError('Altın swap seri listesi doğrulanamadı')
        choices=[]
        for item in meta:
            if not isinstance(item,dict):continue
            r={k.upper():v for k,v in item.items()}
            name=unicodedata.normalize('NFKD',str(r.get('SERIE_NAME','')).upper()).encode('ascii','ignore').decode()
            unit=unicodedata.normalize('NFKD',str(r.get('BIRIMI','')).upper()).encode('ascii','ignore').decode()
            if all(x in name for x in ('ALTIN','STOK','SATIM')) and 'NET ALIM' not in name and 'ALIM YONLU' not in name and 'MILYON' in (unit+' '+name) and any(x in (unit+' '+name) for x in ('USD','ABD DOLARI')):
                if r.get('SERIE_CODE'):choices.append(r['SERIE_CODE'])
        preferred=os.environ.get('EVDS_GOLD_SWAP_SERIES')
        if preferred:
            if preferred not in choices:raise DataError('EVDS_GOLD_SWAP_SERIES altın swap satım stoku ve milyon USD birimiyle eşleşmedi')
            choices=[preferred]
        if len(set(choices))!=1:raise DataError('Milyon USD cinsinden tekil altın swap satım stok serisi doğrulanamadı; Excel sonrası altın düzeltmesi bekliyor')
        code=choices[0]
        raw=evds3_json(client,'',{'series':code,'type':'json',
            'startDate':(today-timedelta(days=45)).strftime('%d-%m-%Y'),'endDate':today.strftime('%d-%m-%Y')})
        if not isinstance(raw,dict) or not isinstance(raw.get('items'),list):raise DataError('Altın swap verisi doğrulanamadı')
        values=validate([{'date':period_date(r['Tarih']),'value':r.get(code.replace('.','_'))} for r in raw['items']],today)
    records['BFRC.GOLD.SWAP.USD']={'observations':values,'source':code,
        'fetched_at':datetime.now(TR).isoformat(timespec='seconds')}

def attach_gold_adjustment(records,cache,errors,today):
    saved=cache.get('_lbma_am',{})
    prices={r['date']:r for r in saved.get('observations',[]) if isinstance(r.get('value'),(int,float)) and r['value']>0}
    failed=False
    try:
        fresh=fetch_lbma_am(today);prices.update({r['date']:r for r in fresh})
        # Explicit user-supplied AM value takes precedence over the conflicting feed.
        overrides={'2026-11-04':4022.85}
        for day,value in overrides.items():
            if day>today.isoformat():continue
            incoming=prices.get(day,{}).get('value')
            if incoming is not None and abs(incoming-value)>.01:
                errors.append('LBMA AM '+day+': kaynak '+str(incoming)+', kullanıcı kaydı '+str(value)+'. Kullanıcı kaydı esas alındı; fiyat uyuşmazlığı doğrulanmalı.')
            prices[day]={'date':day,'value':value,'source':'user supplied LBMA AM; external conflict awaiting verification','manual':True}
        cache['_lbma_am']={'source':'Westmetall London Fixing (AM, cross-checked with LBMA AM archive)',
            'fetched_at':datetime.now(TR).isoformat(timespec='seconds'),
            'observations':[prices[d] for d in sorted(prices)]}
    except DataError as error:errors.append('Altın fiyatı: '+str(error));failed=True
    ordered=[prices[d] for d in sorted(prices)]
    records[LBMA_CODE]={'id':LBMA_CODE,'code':LBMA_CODE,'provider':'public-table',
        'source':'Westmetall London Fixing','observations':ordered,'fallback':failed,
        'fetched_at':cache.get('_lbma_am',{}).get('fetched_at','')}
    if today.isoformat()>WORKBOOK_LAST_DAY:
        try:attach_gold_swap_series(records,today)
        except (DataError,KeyError,TypeError) as error:errors.append('Altın swap: '+str(error))
    try:
        records[GOLD_ADJUSTED_CODE]=gold_adjustment_records(records,ordered,today)
        attach_gold_level(records)
        levels=records['BFRC.NETFX.TREASURYEX']['observations']
        latest=max(r['date'] for r in levels if r.get('value') is not None)
        last=records[GOLD_ADJUSTED_CODE]['observations'][-1]['date']
        if last<latest:errors.append('Altın düzeltmesi '+last+' tarihinde: '+latest+' veya önceki karşılaştırma gününün LBMA AM fiyatı/stok girdisi eksik. Yayımlanınca otomatik tamamlanır.')
    except DataError as error:errors.append('Altın düzeltmesi: '+str(error))

if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--demo", action="store_true")
    options = args.parse_args()
    try:
        build_curated(options.demo)
    except DataError as error:
        print("Yayın üretilmedi:", error, file=sys.stderr)
        sys.exit(1)
