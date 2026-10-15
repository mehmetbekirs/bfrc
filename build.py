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
    title="<br>".join(escape(x) for x in textwrap.wrap(title,70))
    last=min(latest_dates);latest=date.fromisoformat(last)
    earliest=(latest-timedelta(days=365*5)).isoformat()
    fig.update_layout(template="none",height=760,paper_bgcolor="#eff6fa",plot_bgcolor="#eff6fa",
        font={"family":"Arial, sans-serif","size":14,"color":"#172b3b"},
        title={"text":"<b>"+title+"</b><br><span style='font-size:16px'>"+escape(chart["unit"])+" · Son gözlem: "+latest.strftime("%d.%m.%Y")+"</span>",
               "font":{"family":"Courier New, monospace","size":23},"x":.025,"y":.97,"xanchor":"left"},
        margin={"l":30,"r":85,"t":110,"b":190},hovermode="x unified",bargap=.32,
        legend={"orientation":"h","x":.5,"xanchor":"center","y":-.17,
                "font":{"size":14}},
        xaxis={"type":"date","range":[earliest,last],"showgrid":False,"showline":True,
               "linecolor":"#344654","linewidth":1,"tickformat":"%Y-%m","dtick":"M6",
               "tickangle":-45,"ticks":"outside","fixedrange":False},
        yaxis={"side":"right","showgrid":True,"gridcolor":"rgba(44,66,83,0.35)",
               "griddash":"dot","zeroline":True,"zerolinecolor":"#7d8f9b",
               "tickformat":",.1f","showline":False,"fixedrange":False})
    if chart.get("daily_bars"):
        fig.update_xaxes(range=[(date.fromisoformat(earliest)-timedelta(days=1)).isoformat(),
                                (latest+timedelta(days=1)).isoformat()])
    fig.add_layout_image(source=CURATED_LOGO,xref="paper",yref="paper",x=.5,y=.53,
        xanchor="center",yanchor="middle",sizex=.75,sizey=.90,sizing="contain",opacity=.15,layer="below")
    labels=[("Kaynak: TCMB / EVDS",-109),
            ("Grafik: Mehmet Faruk Bekir",-129)]
    footer_shift=-105
    for text,_ in labels:
        wrapped=textwrap.wrap(text,width=62) or [text]
        fig.add_annotation(text="<b>"+"<br>".join(escape(part) for part in wrapped)+"</b>",
            x=0,y=0,xref="paper",yref="paper",yshift=footer_shift,
            xanchor="left",yanchor="top",align="left",showarrow=False,
            font={"family":"Courier New, monospace","size":14,"color":"#172b3b"})
        footer_shift-=len(wrapped)*18+10
    fig.update_layout(margin={"b":175})
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
        item={"default_years":chart.get("default_years",5),"id":chart["id"],"name":chart["name"],"unit":chart["unit"],"file":"charts/"+chart["id"]+".json"}
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
            if isinstance(v,(int,float)) and 36000<v<70000 and v==int(v):
                d=date(1899,12,30)+timedelta(days=int(v))
                if d<=today:dates[col]=d.isoformat()
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


def daily_fx_records(records,weekly,now):
    if not all(c in records for c in FX_RAW):raise DataError('Günlük net döviz hesabının bazı girdileri eksik')
    values={c:{r['date']:r['value'] for r in records[c]['observations']} for c in FX_RAW}
    # Preserve the submitted historical archive's input precision without changing raw API records.
    for d,archived in FX_HISTORICAL_INPUTS.items():
        if all(d in values[c] for c in FX_RAW):
            for c,v in zip(FX_RAW,archived):values[c][d]=v
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
    if not reusable_after_release(pseudo,spec,now):
        try:
            fetched=fetch_weekly_liquidity(now.date())
            for r in fetched:
                by_date[r['date']]={**by_date.get(r['date'],{}),**r}
            cache['_fx_weekly']={'fetched_at':now.isoformat(timespec='seconds'),'rows':list(by_date.values())}
        except (DataError,ValueError,zipfile.BadZipFile,ET.ParseError) as error:
            errors.append('Haftalık swap düzeltmesi: '+str(error))
    try:
        derived,details=daily_fx_records(records,list(by_date.values()),now)
        records.update(derived)
        cache['_fx_calculation_last']=details[-1]
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
    return records


# Daily analytical balance sheet definitions; independent of swap availability.
CURATED_SPECS.pop('TP.AB.N15',None)
CURATED_SPECS.pop('TP.AB.N05',None)
CURATED_SPECS['TP.AB.A15']={**CURATED_SPECS['TP.AB.A02'],'id':'TP.AB.A15','code':'TP.AB.A15','name':'Merkez Bankası Parası'}
CURATED_CHARTS.insert(0,{'id':'netfx-bilanco','name':'Net Rezerv — Swap Dahil (Günlük)',
    'title':'TCMB Net Rezervi (Swap Dahil)','unit':'Milyar ABD doları',
    'area':True,'default_years':3,
    'note':'Net dış varlıklar / USD alış kuru. Swap hariç tahmin ayrı göstergedir.',
    'lines':[line('BFRC.DAILY.NETFX','Swap dahil net rezerv',.001,color='#432454')]})
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
    calculate('BFRC.DAILY.NETFX',['BFRC.DAILY.NDV','TP.DK.USD.A.YTL'],lambda n,u:n/(1000*u) if u>0 else None,'Milyon ABD doları')


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--demo", action="store_true")
    options = args.parse_args()
    try:
        build_curated(options.demo)
    except DataError as error:
        print("Yayın üretilmedi:", error, file=sys.stderr)
        sys.exit(1)
