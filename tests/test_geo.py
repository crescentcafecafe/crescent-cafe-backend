"""Current-location support: GPS -> address lookup, address search, and coordinate validation on addresses."""
import pytest
from pydantic import ValidationError

from app.schemas.orders import AddressIn
from app.services import geo as geo_svc

NOMINATIM = {
    "lat": "12.9716", "lon": "77.5946",
    "display_name": "12, MG Road, Shivajinagar, Bengaluru, Karnataka, 560 001, India",
    "address": {"house_number": "12", "road": "MG Road", "suburb": "Shivajinagar", "city": "Bengaluru", "state": "Karnataka", "postcode": "560 001"},
}


@pytest.fixture(autouse=True)
def _fresh_geo_cache():
    geo_svc.clear_cache()
    yield
    geo_svc.clear_cache()


def test_shape_maps_geocoder_fields_without_inventing_any():
    out = geo_svc.shape(NOMINATIM)
    assert out["line1"] == "12 MG Road" and out["line2"] == "Shivajinagar" and out["city"] == "Bengaluru"
    assert out["postal_code"] == "560001"  # spaces removed
    assert out["latitude"] == pytest.approx(12.9716) and out["longitude"] == pytest.approx(77.5946)
    sparse = geo_svc.shape({"lat": "1", "lon": "2", "display_name": "Some Place, Somewhere", "address": {}})
    assert sparse["line1"] == "Some Place" and sparse["city"] == "" and sparse["postal_code"] == ""


def test_reverse_endpoint_returns_address_and_is_cached(cafe, monkeypatch):
    calls = []

    async def fake(path, params):
        calls.append(path)
        return NOMINATIM

    monkeypatch.setattr(geo_svc, "_get", fake)
    r = cafe.get("/geo/reverse?lat=12.9716&lng=77.5946")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True and body["line1"] == "12 MG Road" and body["postal_code"] == "560001"
    assert body["latitude"] == pytest.approx(12.9716)
    assert cafe.get("/geo/reverse?lat=12.9716&lng=77.5946").status_code == 200
    assert calls == ["/reverse"]  # second lookup came from the cache


def test_reverse_endpoint_still_returns_the_pin_when_the_geocoder_is_down(cafe, monkeypatch):
    async def down(path, params):
        return None

    monkeypatch.setattr(geo_svc, "_get", down)
    r = cafe.get("/geo/reverse?lat=12.9716&lng=77.5946")
    assert r.status_code == 200
    assert r.json() == {"found": False, "latitude": 12.9716, "longitude": 77.5946}


def test_reverse_and_search_validate_input(cafe):
    assert cafe.get("/geo/reverse?lat=95&lng=77").status_code == 422
    assert cafe.get("/geo/reverse?lat=12&lng=200").status_code == 422
    assert cafe.get("/geo/search?q=ab").status_code == 422


def test_search_endpoint(cafe, monkeypatch):
    async def fake(path, params):
        assert path == "/search" and params["q"] == "MG Road" and params["countrycodes"] == "in"
        return [NOMINATIM, {"display_name": "broken", "address": {}}]  # a result without coordinates is dropped

    monkeypatch.setattr(geo_svc, "_get", fake)
    r = cafe.get("/geo/search?q=MG Road")
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1 and results[0]["city"] == "Bengaluru"


def test_saved_address_needs_both_coordinates_and_rejects_null_island(cafe):
    h = cafe.customer("Asha")
    base = {"label": "home", "line1": "12 MG Road", "postal_code": "560001"}
    assert cafe.post("/customers/me/addresses", {**base, "latitude": 12.97}, h=h).status_code == 422
    assert cafe.post("/customers/me/addresses", {**base, "latitude": 0, "longitude": 0, "location_source": "gps"}, h=h).status_code == 422
    ok = cafe.post("/customers/me/addresses", {**base, "latitude": 12.9716, "longitude": 77.5946, "location_source": "gps"}, h=h)
    assert ok.status_code == 201 and ok.json()["location_source"] == "gps" and ok.json()["latitude"] == pytest.approx(12.9716)
    typed = cafe.post("/customers/me/addresses", {**base, "line1": "Typed only", "location_source": "gps"}, h=h).json()
    assert typed["location_source"] == "manual" and typed["latitude"] is None  # a "gps" label without coordinates is not trusted


def test_order_address_keeps_gps_details_and_validates_pairs():
    a = AddressIn(line1="12 MG Road", postal_code="560001", latitude=12.9716, longitude=77.5946, accuracy_m=8.5, location_source="gps")
    assert a.model_dump()["accuracy_m"] == 8.5 and a.location_source == "gps"
    typed = AddressIn(line1="12 MG Road", postal_code="560001", accuracy_m=30, location_source="gps")
    assert typed.location_source == "manual" and typed.accuracy_m is None
    with pytest.raises(ValidationError):
        AddressIn(line1="12 MG Road", postal_code="560001", longitude=77.5)
    with pytest.raises(ValidationError):
        AddressIn(line1="12 MG Road", postal_code="560001", latitude=0, longitude=0)
