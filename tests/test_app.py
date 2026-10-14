import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bgremover.app import create_app
from bgremover.config import Settings
from tests.test_remover import CircleModel, jpeg


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_path=tmp_path / "t.db", admin_password="pw", secret_key="k")
    with TestClient(create_app(settings, model=CircleModel())) as c:
        yield c


def admin(client):
    assert client.post("/admin/api/login", json={"password": "pw"}).status_code == 200
    return client


def test_unlisted_origin_is_blocked(client):
    r = client.post("/v1/remove", files={"image": jpeg()}, headers={"Origin": "https://evil.io"})
    assert r.status_code == 403 and "evil.io" in r.json()["error"]
    assert "access-control-allow-origin" not in r.headers


def test_full_flow_allowed_domain(client):
    admin(client).post("/admin/api/domains", json={"pattern": "*.shop.com"})

    pre = client.options("/v1/remove", headers={"Origin": "https://www.shop.com", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "https://www.shop.com"

    r = client.post("/v1/remove", files={"image": jpeg()}, data={"format": "webp"}, headers={"Origin": "https://www.shop.com"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert r.headers["access-control-allow-origin"] == "https://www.shop.com"
    assert Image.open(io.BytesIO(r.content)).size == (200, 100)

    usage = client.get("/admin/api/usage").json()
    assert usage["totals"]["succeeded"] == 1
    assert usage["by_host"][0]["host"] == "www.shop.com"


def test_raw_body_and_query_options(client):
    admin(client).put("/admin/api/settings", json={"allow_no_origin": True})
    r = client.post("/v1/remove?format=mask", content=jpeg(), headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).mode == "L"


def test_bad_image_is_422(client):
    admin(client).put("/admin/api/settings", json={"allow_no_origin": True})
    r = client.post("/v1/remove", files={"image": b"nope"})
    assert r.status_code == 422


def test_upload_limit(tmp_path):
    settings = Settings(database_path=tmp_path / "t.db", admin_password="pw", max_upload_bytes=1000)
    with TestClient(create_app(settings, model=CircleModel())) as c:
        admin(c).put("/admin/api/settings", json={"allow_no_origin": True})
        assert c.post("/v1/remove", content=b"x" * 200_000).status_code == 413


def test_admin_requires_login(client):
    assert client.get("/admin/api/domains").status_code == 401
    assert client.post("/admin/api/login", json={"password": "wrong"}).status_code == 401
    assert admin(client).get("/admin/api/domains").status_code == 200


def test_domain_crud_errors(client):
    admin(client)
    assert client.post("/admin/api/domains", json={"pattern": "not a domain"}).status_code == 422
    rule = client.post("/admin/api/domains", json={"pattern": "a.com"}).json()
    assert client.post("/admin/api/domains", json={"pattern": "A.com"}).status_code == 409
    assert client.patch(f"/admin/api/domains/{rule['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/admin/api/domains/{rule['id']}").status_code == 204
    assert client.delete(f"/admin/api/domains/{rule['id']}").status_code == 404


def test_admin_page_served(client):
    assert "BG Remover" in client.get("/admin").text
    assert client.get("/admin/static/app.js").status_code == 200
