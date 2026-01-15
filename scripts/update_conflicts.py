#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
update_conflicts.py

Ziel:
- Taegliches Generieren von data/conflicts.geojson fuer deine Karte/Liste.

Quellen:
1) CrisisWatch RSS (International Crisis Group) – via feedparser (tolerant bei kaputtem XML)
2) ReliefWeb API (v2 oder v1) – Reports; appname wird mitgeschickt
3) GDELT GEO 2.0 – Fallback, damit nie 0 Features entstehen (liefert GeoJSON-Punkte)

Wichtig:
- Geocoding via Nominatim ist streng limitiert -> Cache + Delay.
- Viele Angaben sind nur approximativ (Land-Zentrum), das wird in properties markiert.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Optional dependency (empfohlen)
try:
    import feedparser  # type: ignore
except Exception:
    feedparser = None


# -----------------------------
# Konfiguration (ENV)
# -----------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(ROOT, "data")
OUT_GEOJSON = os.path.join(DATA_DIR, "conflicts.geojson")
GEO_CACHE_PATH = os.path.join(DATA_DIR, "geocache.json")

CRISISWATCH_RSS = os.environ.get("CRISISWATCH_RSS", "https://www.crisisgroup.org/rss/crisiswatch")

RELIEFWEB_BASE = os.environ.get("RELIEFWEB_BASE", "https://api.reliefweb.int")
RELIEFWEB_VERSION = os.environ.get("RELIEFWEB_VERSION", "v2").strip().lower()  # "v2" oder "v1"
RELIEFWEB_ENDPOINT = f"{RELIEFWEB_BASE.rstrip('/')}/{RELIEFWEB_VERSION}/reports"
# ReliefWeb erwartet appname; je nach Policy muss der Appname akzeptiert sein.
RW_APPNAME = os.environ.get("RW_APPNAME", os.environ.get("NOMINATIM_EMAIL", "killingtheworld")).strip() or "killingtheworld"

INCLUDE_GDELT = os.environ.get("INCLUDE_GDELT", "1").strip() != "0"
GDELT_TIMESPAN_MIN = int(os.environ.get("GDELT_TIMESPAN_MIN", "1440"))  # 24h
GDELT_MAX_FEATURES = int(os.environ.get("GDELT_MAX_FEATURES", "150"))

MAX_ITEMS_PER_SOURCE = int(os.environ.get("MAX_ITEMS_PER_SOURCE", "25"))
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))

GEOCODE_DELAY_SEC = float(os.environ.get("GEOCODE_DELAY_SEC", "1.1"))
NOMINATIM_EMAIL = os.environ.get("NOMINATIM_EMAIL", "").strip()
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "killingtheworld/0.2 (Kontakt in USER_AGENT setzen; siehe Workflow ENV)"
).strip()

# Minimal: damit der Script nicht abstuerzt, wenn data/ noch nicht existiert
os.makedirs(DATA_DIR, exist_ok=True)


# -----------------------------
# Datenmodell
# -----------------------------
@dataclass
class Item:
    source: str
    title: str
    url: str
    date_iso: str  # YYYY-MM-DD
    country: str
    region: str
    summary: str
    status: str  # aktiv/eskalierend/fruehwarnung/deeskalierend/unbekannt


# -----------------------------
# Helpers
# -----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_date(dt: datetime) -> str:
    return dt.date().isoformat()


def http_get_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> str:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Any:
    return json.loads(http_get_text(url, headers=headers, timeout=timeout))


def http_post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Any:
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def safe(s: Any) -> str:
    return str(s or "").strip()


def parse_country_from_title(title: str) -> str:
    # CrisisWatch ist oft "Country: ..."
    m = re.match(r"^([^:]{3,60}):\s+", title or "")
    if m:
        return m.group(1).strip()
    return ""


def guess_status(text: str) -> str:
    t = (text or "").lower()
    # sehr einfache Heuristik (kein Anspruch auf Genauigkeit)
    if any(k in t for k in ["ceasefire", "truce", "de-escalat", "talks resumed", "agreement reached"]):
        return "deeskalierend"
    if any(k in t for k in ["warning", "risk", "tension", "unrest", "protests", "mobiliz"]):
        return "fruehwarnung"
    if any(k in t for k in ["escalat", "intensif", "airstrike", "shell", "clashes", "offensive"]):
        return "eskalierend"
    if any(k in t for k in ["war", "conflict", "fighting", "attack", "killed", "violence"]):
        return "aktiv"
    return "unbekannt"


def load_cache() -> Dict[str, Any]:
    try:
        with open(GEO_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: Dict[str, Any]) -> None:
    with open(GEO_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# -----------------------------
# Nominatim (Country-level)
# -----------------------------
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"


def nominatim_geocode_country(country: str, cache: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    key = country.strip().lower()
    if not key:
        return None

    if key in cache:
        c = cache.get(key)
        try:
            return float(c["lat"]), float(c["lon"])
        except Exception:
            pass

    params = {"q": country, "format": "jsonv2", "limit": 1}
    if NOMINATIM_EMAIL:
        params["email"] = NOMINATIM_EMAIL

    url = f"{NOMINATIM_SEARCH}?{urlencode(params)}"

    try:
        data = http_get_json(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        if isinstance(data, list) and data:
            hit = data[0]
            lat = float(hit.get("lat"))
            lon = float(hit.get("lon"))
            cache[key] = {"lat": lat, "lon": lon, "ts": now_utc().isoformat()}
            return lat, lon
    except Exception:
        return None

    return None


# -----------------------------
# Quelle 1: CrisisWatch RSS
# -----------------------------
def fetch_crisiswatch_items() -> List[Item]:
    if feedparser is None:
        print("[warn] feedparser ist nicht installiert -> CrisisWatch RSS wird uebersprungen. (requirements.txt: feedparser)")
        return []

    try:
        d = feedparser.parse(CRISISWATCH_RSS)
        if getattr(d, "bozo", 0):
            # bozo heisst: Feed ist formal kaputt, feedparser liefert aber oft trotzdem Eintraege
            print(f"[warn] CrisisWatch RSS bozo: {getattr(d, 'bozo_exception', 'unbekannt')}")

        items: List[Item] = []
        for e in (d.entries or [])[:MAX_ITEMS_PER_SOURCE]:
            title = safe(getattr(e, "title", ""))
            url = safe(getattr(e, "link", ""))

            published = safe(getattr(e, "published", "")) or safe(getattr(e, "updated", ""))
            date_iso = iso_date(now_utc())
            # feedparser liefert oft struct_time
            if getattr(e, "published_parsed", None):
                try:
                    dt = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc)
                    date_iso = iso_date(dt)
                except Exception:
                    pass

            desc = safe(getattr(e, "summary", "")) or safe(getattr(e, "description", ""))
            desc_clean = strip_html(desc)[:400]

            country = parse_country_from_title(title)
            status = guess_status(title + " " + desc_clean)

            items.append(Item(
                source="CrisisWatch (International Crisis Group)",
                title=title,
                url=url,
                date_iso=date_iso,
                country=country,
                region="",
                summary=desc_clean,
                status=status
            ))
        return items

    except Exception as e:
        print(f"[warn] CrisisWatch RSS Fehler: {e}")
        return []


# -----------------------------
# Quelle 2: ReliefWeb API (v2 oder v1)
# -----------------------------
def reliefweb_payload(since_iso: str) -> Dict[str, Any]:
    # ReliefWeb akzeptiert unterschiedliche Query-Formen je nach Version.
    # Wir halten den Payload bewusst konservativ.
    return {
        "appname": RW_APPNAME,
        "query": {
            "value": "conflict OR war OR violence OR clashes OR airstrike OR shelling OR ceasefire",
        },
        "filter": {
            "operator": "AND",
            "conditions": [
                {"field": "date.created", "value": {"from": since_iso}}
            ]
        },
        "fields": {
            "include": [
                "title",
                "url",
                "date.created",
                "body",
                "primary_country.name",
                "country.name",
                "source.name"
            ]
        },
        "sort": ["date.created:desc"],
        "limit": MAX_ITEMS_PER_SOURCE
    }


def parse_reliefweb_response(data: Dict[str, Any]) -> List[Item]:
    out: List[Item] = []
    for row in (data.get("data") or [])[:MAX_ITEMS_PER_SOURCE]:
        fields = row.get("fields") or {}

        title = safe(fields.get("title"))
        url = safe(fields.get("url"))

        created = safe((fields.get("date") or {}).get("created")) or safe(fields.get("date.created"))
        date_iso = created[:10] if created else iso_date(now_utc())

        # primary_country bevorzugen
        country = ""
        pc = fields.get("primary_country") or {}
        if isinstance(pc, dict):
            country = safe(pc.get("name"))

        if not country:
            cs = fields.get("country") or []
            if isinstance(cs, list) and cs:
                c0 = cs[0] or {}
                if isinstance(c0, dict):
                    country = safe(c0.get("name"))

        body = strip_html(safe(fields.get("body")))[:400]

        src = "ReliefWeb"
        srcs = fields.get("source")
        if isinstance(srcs, list) and srcs:
            s0 = srcs[0] or {}
            if isinstance(s0, dict) and s0.get("name"):
                src = f"ReliefWeb (Quelle: {safe(s0.get('name'))})"

        status = guess_status(title + " " + body)

        out.append(Item(
            source=src,
            title=title or (country or "ReliefWeb Report"),
            url=url,
            date_iso=date_iso,
            country=country,
            region="",
            summary=body,
            status=status
        ))
    return out


def fetch_reliefweb_items() -> List[Item]:
    since = now_utc() - timedelta(days=DAYS_BACK)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+0000")

    payload = reliefweb_payload(since_iso)

    # ReliefWeb: appname kann auch als Query-Param sinnvoll sein.
    # Wir versuchen zuerst POST auf die konfigurierte Version, danach Fallback auf v1.
    def endpoint(ver: str) -> str:
        base = f"{RELIEFWEB_BASE.rstrip('/')}/{ver}/reports"
        # appname als Query-Param
        return f"{base}?{urlencode({'appname': RW_APPNAME})}"

    versions_to_try = [RELIEFWEB_VERSION]
    if RELIEFWEB_VERSION != "v1":
        versions_to_try.append("v1")
    if RELIEFWEB_VERSION != "v2":
        versions_to_try.append("v2")

    last_err = None
    for ver in versions_to_try:
        url = endpoint(ver)
        try:
            data = http_post_json(url, payload)
            return parse_reliefweb_response(data)
        except HTTPError as e:
            last_err = e
            # 400 ist bei Policy/Schema aenderungen haeufig
            print(f"[warn] ReliefWeb API ({ver}) Fehler: HTTP {e.code} {e.reason}")
        except URLError as e:
            last_err = e
            print(f"[warn] ReliefWeb API ({ver}) Netzwerkfehler: {e}")
        except Exception as e:
            last_err = e
            print(f"[warn] ReliefWeb API ({ver}) Fehler: {e}")

    if last_err:
        # letzter Hinweis bleibt im Log
        pass
    return []


# -----------------------------
# Quelle 3: GDELT GEO 2.0 (Fallback)
# -----------------------------
def fetch_gdelt_geo_features() -> List[Dict[str, Any]]:
    """
    Holt GeoJSON-Punkte aus News-Mentions (GDELT GEO 2.0).
    Das ist ein Fallback, damit die Karte nicht leer bleibt.
    """
    base = "https://api.gdeltproject.org/api/v2/geo/geo"

    query = "(war OR conflict OR fighting OR clashes OR shelling OR airstrike OR ceasefire OR insurgency OR militia OR invasion)"

    params = {
        "query": query,
        "mode": "pointdata",
        "format": "geojson",
        "timelinespan": str(GDELT_TIMESPAN_MIN),
        "geo": "1",
    }

    url = f"{base}?{urlencode(params)}"

    try:
        data = http_get_json(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        feats = data.get("features", [])
        if not isinstance(feats, list):
            return []

        out: List[Dict[str, Any]] = []
        for f in feats[:GDELT_MAX_FEATURES]:
            geom = f.get("geometry") or {}
            props = f.get("properties") or {}
            coords = geom.get("coordinates") or None
            if not (isinstance(coords, list) and len(coords) >= 2):
                continue

            lon, lat = coords[0], coords[1]
            if lon is None or lat is None:
                continue

            name = safe(props.get("name") or props.get("location") or "News-Ort")
            country = safe(props.get("country") or props.get("countryname") or "")
            region = safe(props.get("adm1") or props.get("adm1name") or "")

            # Quellen-URLs sind je nach GDELT-Ausgabe unterschiedlich. Wir bleiben defensiv.
            sources: List[str] = []
            for k in ["url", "shareimage", "articles"]:
                v = props.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    sources.append(v)

            status = "fruehwarnung"  # als Fallback eher vorsichtig
            title = f"{country}: {name}" if country else name

            out.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "title": title,
                    "status": status,
                    "date": iso_date(now_utc()),
                    "summary": "Automatisch extrahierte News-Ortsnennung (GDELT).",
                    "why_it_matters": "Fallback, damit die Karte nicht leer ist. Angaben koennen falsch/ungenau sein.",
                    "sources": sources[:3],
                    "country": country,
                    "region": region,
                    "source": "GDELT GEO 2.0",
                    "location_precision": "point",
                    "confidence": 0.35,
                }
            })
        return out

    except Exception as e:
        print(f"[warn] GDELT GEO Fehler: {e}")
        return []


# -----------------------------
# Dedupe + GeoJSON
# -----------------------------
def dedupe_items(items: List[Item]) -> List[Item]:
    seen = set()
    out: List[Item] = []
    for it in items:
        k = (
            (it.url or "").strip().lower(),
            (it.title or "").strip().lower(),
            it.date_iso,
            (it.country or "").strip().lower(),
        )
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def items_to_geojson(items: List[Item], cache: Dict[str, Any]) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []

    for it in items:
        country = it.country.strip()
        if not country:
            # Ohne Land koennen wir (in diesem einfachen Setup) nicht sinnvoll geokodieren
            continue

        coords = nominatim_geocode_country(country, cache)
        time.sleep(GEOCODE_DELAY_SEC)

        if not coords:
            continue

        lat, lon = coords
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "title": it.title or country,
                "status": it.status or "unbekannt",
                "date": it.date_iso,
                "summary": it.summary,
                "why_it_matters": f"Quelle: {it.source}",
                "sources": [it.url] if it.url else [],
                "country": country,
                "region": it.region,
                "source": it.source,
                "location_precision": "country",
                "confidence": 0.55,
            }
        })

    return {"type": "FeatureCollection", "features": features}


def merge_geojson_features(a: Dict[str, Any], b_features: List[Dict[str, Any]]) -> Dict[str, Any]:
    feats = list(a.get("features") or [])
    # sehr einfache Dedupe fuer GeoJSON-Fallback
    seen = set()
    for f in feats:
        p = f.get("properties") or {}
        k = (safe(p.get("title")).lower(), safe(p.get("country")).lower(), json.dumps(f.get("geometry") or {}, sort_keys=True))
        seen.add(k)

    for f in b_features:
        p = f.get("properties") or {}
        k = (safe(p.get("title")).lower(), safe(p.get("country")).lower(), json.dumps(f.get("geometry") or {}, sort_keys=True))
        if k in seen:
            continue
        seen.add(k)
        feats.append(f)

    return {"type": "FeatureCollection", "features": feats}


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    cache = load_cache()

    items: List[Item] = []

    # 1) CrisisWatch RSS
    try:
        items.extend(fetch_crisiswatch_items())
    except Exception as e:
        print(f"[warn] CrisisWatch RSS Fehler: {e}")

    # 2) ReliefWeb
    try:
        items.extend(fetch_reliefweb_items())
    except Exception as e:
        print(f"[warn] ReliefWeb API Fehler: {e}")

    items = dedupe_items(items)

    geo = items_to_geojson(items, cache)

    # 3) Fallback: GDELT, wenn leer oder sehr wenig
    if INCLUDE_GDELT and (len(geo.get("features", [])) == 0):
        gdelt_feats = fetch_gdelt_geo_features()
        if gdelt_feats:
            geo = merge_geojson_features(geo, gdelt_feats)

    # Schreiben
    with open(OUT_GEOJSON, "w", encoding="utf-8") as f:
        json.dump(geo, f, ensure_ascii=False, indent=2)

    save_cache(cache)

    print(f"[ok] wrote {OUT_GEOJSON} with {len(geo.get('features', []))} features")


if __name__ == "__main__":
    main()
