"""Address <-> GPS lookup used by the "Use my current location" button (Swiggy / Zomato / Zepto style).

The browser or phone gives exact coordinates; this turns them into a readable street address so the customer only has to
add a flat / house number, and it can search an address when GPS is poor (desktop, indoors).

Works with any Nominatim-compatible API (see GEOCODER_* settings). Results are cached because the same spot is looked up
repeatedly and the public OpenStreetMap server asks for light usage. A geocoder outage never blocks an order: the
coordinates are what matter for the rider, the address text is a convenience.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from app.core.config import get_settings

log = logging.getLogger("crescent.geo")

_TTL_SECONDS = 6 * 3600
_MAX_CACHE = 2000
_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
        return hit[1]
    _cache.pop(key, None)
    return None


def _cache_put(key: str, value: object) -> None:
    if len(_cache) >= _MAX_CACHE:
        for k in list(_cache)[: _MAX_CACHE // 4]:  # drop the oldest quarter
            _cache.pop(k, None)
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    _cache.clear()


def _first(a: dict, *keys: str) -> str:
    for k in keys:
        v = a.get(k)
        if v:
            return str(v).strip()
    return ""


def shape(item: dict) -> dict:
    """One geocoder result -> the fields of our address form. Missing parts are empty strings, never invented."""
    a = item.get("address") or {}
    road = _first(a, "road", "pedestrian", "footway", "residential", "path", "street")
    house = _first(a, "house_number", "house_name")
    building = _first(a, "building", "amenity", "shop", "office")
    street = " ".join(p for p in (house, road) if p)
    line1 = ", ".join(p for p in (building, street) if p) or str(item.get("display_name", "")).split(",")[0].strip()
    line2 = _first(a, "neighbourhood", "suburb", "quarter", "city_district", "hamlet")
    city = _first(a, "city", "town", "village", "municipality", "county", "state_district")
    postcode = re.sub(r"\s+", "", _first(a, "postcode"))
    try:
        lat, lng = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        lat = lng = None
    return {"line1": line1[:200], "line2": line2[:200], "landmark": "", "city": city[:80], "postal_code": postcode[:12], "state": _first(a, "state"),
            "display_name": str(item.get("display_name", ""))[:300], "latitude": lat, "longitude": lng}


async def _get(path: str, params: dict) -> object | None:
    s = get_settings()
    params = {**params, "format": "jsonv2", "addressdetails": 1, "accept-language": "en"}
    if s.geocoder_api_key:
        params["key"] = s.geocoder_api_key
    try:
        async with httpx.AsyncClient(timeout=s.geocoder_timeout_s, headers={"User-Agent": s.geocoder_user_agent}) as client:
            r = await client.get(f"{s.geocoder_url.rstrip('/')}{path}", params=params)
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # network, timeout, bad JSON, 4xx/5xx: all mean "no address text right now"
        log.warning("geocoder %s failed: %s", path, exc)
        return None


async def reverse(lat: float, lng: float) -> dict:
    """GPS point -> street address. Always returns the coordinates back; `found` says whether address text was available."""
    key = f"r:{lat:.5f},{lng:.5f}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = await _get("/reverse", {"lat": lat, "lon": lng, "zoom": 18})
    if not isinstance(data, dict) or "address" not in data:
        return {"found": False, "latitude": lat, "longitude": lng}  # not cached, so a retry can succeed
    out = {"found": True, **shape(data), "latitude": lat, "longitude": lng}
    _cache_put(key, out)
    return out


async def search(query: str, limit: int = 5) -> list[dict]:
    q = " ".join(query.split())[:120]
    key = f"s:{q.lower()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    params: dict = {"q": q, "limit": limit}
    countries = get_settings().geocoder_country_codes.strip()
    if countries:
        params["countrycodes"] = countries
    data = await _get("/search", params)
    if not isinstance(data, list):
        return []
    out = [r for r in (shape(i) for i in data) if r["latitude"] is not None]
    _cache_put(key, out)
    return out
