"""Gzip is limited to /api/management/ and /ui/ and to textual, complete bodies."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from src.management_compression import ManagementCompressionMiddleware

BIG = {"items": [{"id": index, "name": f"model-{index}", "note": "x" * 40} for index in range(400)]}


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/management/v1/big")
    def big():
        return JSONResponse(BIG)

    @app.get("/api/management/v1/small")
    def small():
        return {"ok": True}

    @app.get("/api/management/v1/image")
    def image():
        return Response(b"\x89PNG" + b"\x00" * 50_000, media_type="image/png")

    @app.get("/api/management/v1/events")
    def events():
        return StreamingResponse(iter([b"data: 1\n\n"] * 400), media_type="text/event-stream")

    @app.get("/api/management/v1/partial")
    def partial():
        return Response(b"a" * 5000, status_code=206, media_type="text/plain")

    @app.get("/v1/models")
    def inference():
        return JSONResponse(BIG)

    app.add_middleware(ManagementCompressionMiddleware)
    return app


def test_management_json_is_gzipped_and_identical():
    with TestClient(_app()) as client:
        res = client.get("/api/management/v1/big", headers={"Accept-Encoding": "gzip"})
        assert res.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in res.headers["vary"]
        assert res.json() == BIG
        # The client inflates the body; the header still carries the compressed size.
        assert int(res.headers["content-length"]) * 5 < len(res.content)


def test_passthrough_cases():
    with TestClient(_app()) as client:
        gz = {"Accept-Encoding": "gzip"}
        assert "content-encoding" not in client.get("/api/management/v1/small", headers=gz).headers
        assert "content-encoding" not in client.get("/api/management/v1/image", headers=gz).headers
        assert "content-encoding" not in client.get("/api/management/v1/events", headers=gz).headers
        assert "content-encoding" not in client.get("/api/management/v1/partial", headers=gz).headers
        assert "content-encoding" not in client.get("/api/management/v1/big", headers={**gz, "Range": "bytes=0-9"}).headers
        assert "content-encoding" not in client.get("/api/management/v1/big", headers={"Accept-Encoding": "identity"}).headers
        # Inference endpoints keep their exact responses.
        assert "content-encoding" not in client.get("/v1/models", headers=gz).headers


def test_server_installs_the_middleware():
    import server

    assert any(item.cls is ManagementCompressionMiddleware for item in server.app.user_middleware)
