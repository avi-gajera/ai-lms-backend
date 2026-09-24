"""HTTP-layer hardening: upload size limit, one error envelope everywhere, request-id handling."""

from pathlib import Path

import pytest

from app.config import get_settings

MB = 1024 * 1024


@pytest.fixture
def small_upload_limit(monkeypatch):
    """1 MB upload limit (the middleware allows 1 MB of multipart headroom on top: 2 MB total)."""
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)


def _stored_videos() -> list[Path]:
    d = Path(get_settings().video_storage_dir)
    return list(d.iterdir()) if d.exists() else []


def _multipart(size: int, boundary: str = "lmsboundary") -> tuple[bytes, dict[str, str]]:
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.mp4"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
    ).encode() + b"0" * size + f"\r\n--{boundary}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


def test_upload_over_declared_content_length_is_rejected_before_reading(client, small_upload_limit):
    before = _stored_videos()
    r = client.post("/videos", files={"file": ("big.mp4", b"0" * 3 * MB, "video/mp4")})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert r.headers["X-Request-ID"] == r.json()["error"]["request_id"]
    assert _stored_videos() == before  # the endpoint never ran, nothing was written


def test_chunked_upload_is_cut_off_at_the_limit(client, small_upload_limit):
    body, headers = _multipart(3 * MB)

    def stream():  # a generator body is sent chunked, without Content-Length
        for i in range(0, len(body), 256 * 1024):
            yield body[i : i + 256 * 1024]

    before = _stored_videos()
    r = client.post("/videos", content=stream(), headers=headers)
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    assert _stored_videos() == before


def test_upload_under_limit_still_accepted(client, small_upload_limit):
    r = client.post("/videos", files={"file": ("ok.mp4", b"recursion", "video/mp4")})
    assert r.status_code == 202


def test_request_validation_errors_use_the_envelope(client):
    r = client.post("/videos/any/progress", json={"learner_id": "", "progress": 2})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_failed" and err["request_id"]
    assert {tuple(d["loc"]) for d in err["details"]} == {("body", "learner_id"), ("body", "progress")}


def test_framework_http_errors_use_the_envelope(client):
    r = client.get("/no-such-route")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.delete("/videos")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_openapi_documents_the_envelope_for_422(client):
    spec = client.get("/openapi.json").json()
    ref = spec["paths"]["/assessments"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]
    assert ref["$ref"].endswith("/ErrorOut")
    assert "HTTPValidationError" not in spec["components"]["schemas"]


@pytest.mark.parametrize("rid", ["client-req-42", "0f8c:trace.id_1"])
def test_safe_request_id_is_echoed(client, rid):
    assert client.get("/health", headers={"X-Request-ID": rid}).headers["X-Request-ID"] == rid


@pytest.mark.parametrize("rid", ["A" * 500, "has spaces", "x\"}; drop"])
def test_unsafe_request_id_is_replaced(client, rid):
    echoed = client.get("/health", headers={"X-Request-ID": rid}).headers["X-Request-ID"]
    assert echoed != rid and len(echoed) == 36  # a fresh uuid4
