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
    fig.add_layout_image(source=CURATED_LOGO,xref="paper",yref="paper",x=.5,y=.53,
        xanchor="center",yanchor="middle",sizex=.75,sizey=.90,sizing="contain",opacity=.15,layer="below")
    sources=list(dict.fromkeys(records[c]["source"] for c in requested))
    fetched=min(records[c]["fetched_at"] for c in requested)
    labels=[("Kaynak: "+"; ".join(sources),-109),
            ("Grafik: Mehmet Faruk Bekir",-129),
            (f"Son veri: {last} · API kontrolü: {fetched[:16].replace('T',' ')}",-149)]
    if chart.get("note"):
        labels.append((chart["note"],-169))
    anchors=[records[c].get("weekly_anchor") for c in requested if records[c].get("weekly_anchor")]
    if anchors:labels.append(("Haftalık swap düzeltmesi referansı: "+min(anchors),-189))
    footer_shift=-105
    for text,_ in labels:
        wrapped=textwrap.wrap(text,width=62) or [text]
        fig.add_annotation(text="<b>"+"<br>".join(escape(part) for part in wrapped)+"</b>",
            x=0,y=0,xref="paper",yref="paper",yshift=footer_shift,
            xanchor="left",yanchor="top",align="left",showarrow=False,
            font={"family":"Courier New, monospace","size":14,"color":"#172b3b"})
        footer_shift-=len(wrapped)*18+10
    fig.update_layout(margin={"b":max(210,abs(footer_shift)+12)})
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
const update={'xaxis.autorange':false,'xaxis.range':[begin,end],'xaxis.dtick':currentYears<=1?'M1':currentYears===3?'M3':'M6'};
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
        if all(reusable_after_release(r,s,datetime.now(TR)) for r,s in zip(prior,items)):
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

FX_LEVEL_CODE='BFRC.NETFX.SWAPEX'
FX_CHANGE_CODE='BFRC.NETFX.SWAPEX.CHANGE'
FX_RAW=['TP.AB.A02','TP.AB.A10','TP.AB.A13','TP.DK.USD.A.YTL',
        'TP.SWAPTEKTAR.TOTALSTOKALIMYONLU','TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU']
CURATED_SPECS.update(FX_SPECS)
CURATED_CHARTS[:0]=[
 {'id':'netfx-gunluk','name':'Net Döviz Pozisyonu — Excel Hesabı (Günlük)',
  'title':'TCMB Swap Hariç Net Döviz Pozisyonu','unit':'Milyar ABD doları',
  'note':'Excel U = F + R + H. Hazine mevduatı eklenir; swap düzeltmesi son haftalık referanstan taşınır.',
  'default_years':3,'area':True,'lines':[line(FX_LEVEL_CODE,'Swap hariç net döviz pozisyonu',.001,color='#432454')]},
 {'id':'netfx-degisim','name':'Net Döviz Pozisyonu Günlük Değişim',
  'title':'TCMB Swap Hariç Net Döviz Pozisyonu Günlük Değişimi','unit':'Milyon ABD doları',
  'note':'Excel W = U(t) − U(önceki iş günü). Kur ve değerleme etkilerini içerir.',
  'default_years':.5,'daily_bars':True,'lines':[line(FX_CHANGE_CODE,'Günlük değişim',color='#432454')]}
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
    assets,liabilities,treasury,usd,buy,sell=[values[c] for c in FX_RAW]
    domestic={d:sell[d]-buy[d] for d in buy.keys()&sell.keys()}
    anchors=[]
    for item in sorted(weekly,key=lambda r:r['date']):
        d=item['date'];m=domestic.get(d,item.get('domestic'))
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
    fetched=min(records[c]['fetched_at'] for c in FX_RAW)
    base={'provider':'derived','source':'TCMB / EVDS ve haftalık rezerv tablosu; hesaplama: BFRC',
          'fetched_at':fetched,'frequency':'Günlük tahmin','group':'Günlük net döviz pozisyonu',
          'weekly_anchor':details[-1]['anchor_date'],'estimated':True}
    if any(records[c].get('fallback') for c in FX_RAW):base['fallback']=True
    return {
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
CURATED_CHARTS.sort(key=lambda c: {'netfx-gunluk':0,'netfx-degisim':1,'netfx-bilanco':2}.get(c['id'],3))
for chart in CURATED_CHARTS:
    if chart['id'] in ('net-ic','net-dis'):
        chart['name']+=' (Günlük Analitik Bilanço)'
        chart['area']=True
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
