def test_health_and_ready(ctx):
    assert ctx.get("/health").json() == {"status": "ok"}
    assert ctx.get("/ready").json() == {"status": "ready"}


def test_bootstrap_and_seed(cafe):
    assert cafe.ids["table"]
    assert cafe.get("/menu").json()["categories"][0]["name"] == "Coffee"
