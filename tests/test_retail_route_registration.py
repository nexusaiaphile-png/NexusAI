from fastapi import FastAPI

from backend.src import retail_intelligence


def test_install_registers_retail_routes_without_touching_database(monkeypatch):
    # Route registration must not require a live database during this focused test.
    monkeypatch.setattr(retail_intelligence, "_init", lambda: None)
    app = FastAPI()

    retail_intelligence.install(app)

    routes = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert ("/api/retail/status", "GET") in routes
    assert ("/api/retail/pos/transactions", "POST") in routes
    assert ("/api/retail/camera-events", "POST") in routes
    assert ("/api/retail/vision-results", "POST") in routes
    assert ("/api/retail/incidents", "GET") in routes
    assert ("/api/retail/incidents/{incident_id}/status", "POST") in routes
