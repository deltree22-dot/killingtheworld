#!/usr/bin/env python3
"""
Daily updater: fetches conflict-related items from
- International Crisis Group (CrisisWatch RSS)
- ReliefWeb reports (API)

Then:
- geocodes country names via Nominatim (cached) to get approx coordinates
- writes GeoJSON to data/conflicts.geojson
- persists cache in data/geocache.json

Notes:
- Geocoding is intentionally conservative: country-level points only.
- Nominatim Usage Policy: max 1 request/sec and identify with a custom User-Agent.
  Keep the list size modest and rely on caching.
"""
from __future__ import annotations

import json
import os
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(ROOT, "data")
OUT_GEOJSON = os.path.join(DATA_DIR, "conflicts.geojson")
GEO_CACHE_PATH = os.path.join(DATA_DIR, "geocache.json")

CRISISWATCH_RSS = "https://www.crisisgroup.org/rss/crisiswatch"
RELIEFWEB_ENDPOINT = "https://api.reliefweb.int/v1/reports"

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_EMAIL = os.environ.get("NOMINATIM_EMAIL", "").strip()
USER_AGENT = os.environ.get("USER_AGENT", "conflict-map-starter/0.1 (set USER_AGENT env var)")

MAX_ITEMS_PER_SOURCE = int(os.environ.get("MAX_ITEMS_PER_SOURCE", "20"))
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))  # pull last N days
GEOCODE_DELAY_SEC = float(os.environ.get("GEOCODE_DELAY_SEC", "1.1"))

@dataclass
class Item:
    source: str
    title: str
    url: str
    date: str  # ISO date
    country: Optional[str]
    summary: str

def http_get_json(url: str, headers: Dict[str, str] | None = None) -> Any:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)

def http_get_text(url: str, headers: Dict[str, str] | None = None) -> str:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")

def load_cache() -> Dict[str, Any]:
    try:
        with open(GEO_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def save_cache(cache: Dict[str, Any]) -> None:
    with open(GEO_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def iso_date(dt: datetime) -> str:
    return dt.date().isoformat()

def parse_crisiswatch_rss() -> List[Item]:
    xml = http_get_text(CRISISWATCH_RSS, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
    root = ET.fromstring(xml)
    channel = root.find("channel")
    if channel is None:
        return []

    items: List[Item] = []
    for it in channel.findall("item")[:MAX_ITEMS_PER_SOURCE]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        desc = (it.findtext("description") or "").strip()

        # Try to infer country from title patterns; often: "Country: something"
        country = None
        m = re.match(r"^([^:]{3,50}):\s+", title)
        if m:
            country = m.group(1).strip()

        # pubDate parsing: RFC822-ish
        dt_iso = iso_date(datetime.now(timezone.utc))
        try:
            # Example: "Tue, 09 Jan 2026 12:34:56 +0000"
            dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
            dt_iso = iso_date(dt.astimezone(timezone.utc))
        except Exception:
            pass

        # Strip HTML tags from description (very simple)
        desc_clean = re.sub(r"<[^>]+>", "", desc).strip()
        desc_clean = re.sub(r"\s+", " ", desc_clean)

        items.append(Item(
            source="CrisisWatch (International Crisis Group)",
            title=title,
            url=link,
            date=dt_iso,
            country=country,
            summary=desc_clean[:320]
        ))
    return items

def parse_reliefweb_reports() -> List[Item]:
    # ReliefWeb API quotas exist; keep calls low and cache results client-side by rebuilding daily.
    # Query: last DAYS_BACK days, keyword match on conflict-related terms.
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%S%z")
    # ReliefWeb expects timezone offset like +0000
    payload = {
        "appname": "conflict-map-starter",
        "query": {
            "value": "conflict OR war OR violence OR clashes",
            "operator": "AND"
        },
        "filter": {
            "operator": "AND",
            "conditions": [
                {"field": "date.created", "value": {"from": since}}
            ]
        },
        "fields": {
            "include": ["id", "title", "url", "date.created", "body", "primary_country.name", "country.name", "source.name"]
        },
        "sort": ["date.created:desc"],
        "limit": MAX_ITEMS_PER_SOURCE
    }

    req = Request(
        RELIEFWEB_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, "Accept": "application/json"},
        method="POST"
    )
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)

    items: List[Item] = []
    for e in (data.get("data") or [])[:MAX_ITEMS_PER_SOURCE]:
        f = e.get("fields") or {}
        title = (f.get("title") or "").strip()
        url = (f.get("url") or "").strip()

        created = (f.get("date", {}).get("created") or f.get("date.created") or "").strip()
        dt_iso = created[:10] if created else iso_date(datetime.now(timezone.utc))

        # choose primary_country if present, else first country
        country = None
        pc = f.get("primary_country") or {}
        if isinstance(pc, dict):
            country = pc.get("name")
        if not country:
            cs = f.get("country") or []
            if isinstance(cs, list) and cs:
                country = (cs[0] or {}).get("name")

        body = (f.get("body") or "")
        body_clean = re.sub(r"<[^>]+>", "", body).strip()
        body_clean = re.sub(r"\s+", " ", body_clean)

        src = "ReliefWeb"
        srcs = f.get("source")
        if isinstance(srcs, list) and srcs:
            s0 = srcs[0] or {}
            if isinstance(s0, dict) and s0.get("name"):
                src = f"ReliefWeb (Quelle: {s0['name']})"

        items.append(Item(
            source=src,
            title=title,
            url=url,
            date=dt_iso,
            country=country,
            summary=body_clean[:320]
        ))
    return items

def nominatim_geocode_country(country: str, cache: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    key = country.strip().lower()
    if not key:
        return None
    if key in cache:
        c = cache[key]
        if isinstance(c, dict) and "lat" in c and "lon" in c:
            return float(c["lat"]), float(c["lon"])

    params = {
        "q": country,
        "format": "jsonv2",
        "limit": 1
    }
    if NOMINATIM_EMAIL:
        params["email"] = NOMINATIM_EMAIL

    url = f"{NOMINATIM_SEARCH}?{urlencode(params)}"
    data = http_get_json(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    if isinstance(data, list) and data:
        hit = data[0]
        try:
            lat = float(hit.get("lat"))
            lon = float(hit.get("lon"))
            cache[key] = {"lat": lat, "lon": lon, "ts": datetime.now(timezone.utc).isoformat()}
            return lat, lon
        except Exception:
            return None
    return None

def dedupe(items: List[Item]) -> List[Item]:
    seen = set()
    out = []
    for it in items:
        k = (it.title.strip().lower(), it.date, (it.country or "").strip().lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out

def to_geojson(items: List[Item], cache: Dict[str, Any]) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    for it in items:
        country = it.country or ""
        coords = None
        if country:
            coords = nominatim_geocode_country(country, cache)
            # Respect Nominatim usage policy (max 1 req/sec); caching should keep this low.
            time.sleep(GEOCODE_DELAY_SEC)

        if not coords:
            # skip if no usable coordinates
            continue

        lat, lon = coords
        title = it.title if it.title else (country or "Unbekannt")
        props = {
            "title": title,
            "status": "aktiv",  # simple default; refine later
            "date": it.date,
            "summary": it.summary,
            "why_it_matters": f"Quelle: {it.source}",
            "sources": [it.url] if it.url else [],
            "country": country,
            "source": it.source
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props
        })

    return {"type": "FeatureCollection", "features": features}

def main() -> None:
    cache = load_cache()

    all_items: List[Item] = []
    try:
        all_items.extend(parse_crisiswatch_rss())
    except Exception as e:
        print(f"[warn] CrisisWatch RSS Fehler: {e}")

    try:
        all_items.extend(parse_reliefweb_reports())
    except Exception as e:
        print(f"[warn] ReliefWeb API Fehler: {e}")

    all_items = dedupe(all_items)

    geo = to_geojson(all_items, cache)

    with open(OUT_GEOJSON, "w", encoding="utf-8") as f:
        json.dump(geo, f, ensure_ascii=False, indent=2)

    save_cache(cache)

    print(f"[ok] wrote {OUT_GEOJSON} with {len(geo.get('features', []))} features")

if __name__ == "__main__":
    main()
