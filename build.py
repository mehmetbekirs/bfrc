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


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--demo", action="store_true")
    options = args.parse_args()
    try:
        build(options.demo)
    except DataError as error:
        print("Yayın üretilmedi:", error, file=sys.stderr)
        sys.exit(1)
