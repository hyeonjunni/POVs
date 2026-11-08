import httpx
from fastapi.testclient import TestClient

from scripts.serve_web_preview import create_app


def test_proxy_preserves_browser_origin_for_protected_media(tmp_path, monkeypatch):
    class Upstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, url, **kwargs):
            assert url == "http://127.0.0.1:8002/api/v1/uploads/room"
            assert kwargs["headers"]["host"] == "127.0.0.1:8083"
            assert kwargs["headers"]["authorization"] == "Bearer test-token"
            return httpx.Response(200, json={"file_url": "http://" + kwargs["headers"]["host"] + "/api/v1/uploads/image/content"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Upstream())
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8083")
    response = client.get("/api/v1/uploads/room", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json()["file_url"].startswith("http://127.0.0.1:8083/")
