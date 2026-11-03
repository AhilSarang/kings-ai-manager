from fastapi.testclient import TestClient


def test_health_is_ok_without_db():
    from app.main import create_app

    r = TestClient(create_app()).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_reports_database_and_worker(db):
    from app.main import create_app
    from app.services import health

    client = TestClient(create_app())
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "database": True, "worker": False}
    health.beat(db)
    db.commit()
    assert client.get("/health/ready").json() == {"status": "ok", "database": True, "worker": True}


def test_ready_degraded_when_db_down(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "db_ping", lambda: False)
    r = TestClient(main.create_app()).get("/health/ready")
    assert r.status_code == 503
    assert r.json()["database"] is False
