"""Files API tests."""

from __future__ import annotations

from httpx import AsyncClient


def _auth_headers(session_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Session-Id": session_id,
    }


async def test_files_upload_download_and_list(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "files-api"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("files-api", token)

    uploaded = await client.post(
        "/v1/sessions/files-api/files:upload",
        headers=headers,
        files={"file": ("data.txt", b"hello world", "text/plain")},
        data={"path": "/workspace/data.txt"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["path"] == "/workspace/data.txt"
    assert uploaded.json()["bytes"] == 11

    listed = await client.post(
        "/v1/sessions/files-api/files:list",
        headers=headers,
        json={"path": "/workspace"},
    )
    assert listed.status_code == 200
    assert listed.json()["path"] == "/workspace"
    assert listed.json()["children"][0]["path"] == "/workspace/data.txt"

    downloaded = await client.post(
        "/v1/sessions/files-api/files:download",
        headers=headers,
        json={"path": "/workspace/data.txt"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"hello world"
    assert downloaded.headers["X-File-Bytes"] == "11"


async def test_files_list_can_disable_sha256(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "files-api-nohash"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("files-api-nohash", token)

    uploaded = await client.post(
        "/v1/sessions/files-api-nohash/files:upload",
        headers=headers,
        files={"file": ("data.txt", b"hello world", "text/plain")},
        data={"path": "/workspace/data.txt"},
    )
    assert uploaded.status_code == 200

    listed = await client.post(
        "/v1/sessions/files-api-nohash/files:list",
        headers=headers,
        json={"path": "/workspace", "includeSha256": False},
    )

    assert listed.status_code == 200
    child = listed.json()["children"][0]
    assert child["path"] == "/workspace/data.txt"
    assert child["bytes"] == 11
    assert child["sha256"] is None


async def test_files_errors_use_specific_messages(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "files-api-errors"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("files-api-errors", token)

    missing_file = await client.post(
        "/v1/sessions/files-api-errors/files:download",
        headers=headers,
        json={"path": "/workspace/missing.txt"},
    )
    assert missing_file.status_code == 404
    missing_file_body = missing_file.json()
    assert missing_file_body["code"] == "not-found"
    assert missing_file_body["message"] == "Файл не найден"
    assert missing_file_body["details"]["path"] == "/workspace/missing.txt"

    missing_path = await client.post(
        "/v1/sessions/files-api-errors/files:list",
        headers=headers,
        json={"path": "/workspace/missing"},
    )
    assert missing_path.status_code == 404
    missing_path_body = missing_path.json()
    assert missing_path_body["code"] == "not-found"
    assert missing_path_body["message"] == "Путь не найден"
    assert missing_path_body["details"]["path"] == "/workspace/missing"

    escaped_path = await client.post(
        "/v1/sessions/files-api-errors/files:list",
        headers=headers,
        json={"path": "../../etc/passwd"},
    )
    assert escaped_path.status_code == 403
    escaped_path_body = escaped_path.json()
    assert escaped_path_body["code"] == "policy-deny"
    assert escaped_path_body["message"] == "path escapes session rootfs"
    assert escaped_path_body["details"]["path"] == "../../etc/passwd"
