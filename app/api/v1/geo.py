"""Location helpers for the apps: GPS point -> address, and address search. Public, because guest checkout must work too."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.ratelimit import limit
from app.services import geo

router = APIRouter(prefix="/geo", tags=["geo"])


@router.get("/reverse", dependencies=[Depends(limit("geo_reverse", 30, 60))])
async def reverse(lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180)):
    """Street address for a GPS point. `found` is false (HTTP 200) when the lookup service is unavailable; the coordinates still work."""
    return await geo.reverse(lat, lng)


@router.get("/search", dependencies=[Depends(limit("geo_search", 20, 60))])
async def search(q: str = Query(min_length=3, max_length=120)):
    """Up to 5 matching places, each with coordinates, for when the phone cannot get a good GPS fix."""
    return {"results": await geo.search(q)}
