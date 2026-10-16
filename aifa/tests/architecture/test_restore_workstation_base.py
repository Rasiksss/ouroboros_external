"""Восстановление базы Workstation `workstation-svc:local` из слоёв образа комнаты (P34, ADR-0007).

На dev тега `workstation-svc:local` нет: есть только `workstation-svc-aifa:local` (база + слой
AIFA с кодом finengine и мостом). Скрипт режет OCI-архив `docker save` по `ENTRYPOINT`
базы; тест строит архив из трёх слоёв — база, база, слой AIFA — и смотрит, что осталось.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from arch_support import DEV_DIR

SCRIPT = DEV_DIR / "workstation" / "restore_base.py"
ENTRYPOINT = '/bin/sh -c #(nop) ENTRYPOINT ["./entrypoint.sh"]'


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("restore_base", SCRIPT)
    assert spec and spec.loader, f"нет {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _archive(path: Path, history: list[dict[str, Any]], layers: list[bytes]) -> None:
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"User": "aef", "Entrypoint": ["./entrypoint.sh"]},
        "history": history,
        "rootfs": {"type": "layers", "diff_ids": [_digest(b) for b in layers]},
    }
    config_bytes = json.dumps(config).encode()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "digest": _digest(config_bytes),
            "size": len(config_bytes),
            "mediaType": "application/vnd.oci.image.config.v1+json",
        },
        "layers": [
            {
                "digest": _digest(b),
                "size": len(b),
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
            }
            for b in layers
        ],
    }
    manifest_bytes = json.dumps(manifest).encode()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "digest": _digest(manifest_bytes),
                "size": len(manifest_bytes),
                "mediaType": manifest["mediaType"],
            }
        ],
    }
    with tarfile.open(path, "w") as tar:
        for blob in (*layers, config_bytes, manifest_bytes):
            _add(tar, f"blobs/sha256/{_digest(blob).split(':')[1]}", blob)
        _add(tar, "index.json", json.dumps(index).encode())
        _add(tar, "oci-layout", b'{"imageLayoutVersion": "1.0.0"}')


def _read(path: Path) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    with tarfile.open(path) as tar:
        names = {m.name for m in tar.getmembers()}
        index = json.load(tar.extractfile("index.json"))  # type: ignore[arg-type]
        manifest_ref = index["manifests"][0]["digest"].split(":")[1]
        manifest = json.load(tar.extractfile(f"blobs/sha256/{manifest_ref}"))  # type: ignore[arg-type]
        config_ref = manifest["config"]["digest"].split(":")[1]
        config = json.load(tar.extractfile(f"blobs/sha256/{config_ref}"))  # type: ignore[arg-type]
    return manifest, config, names


def test_the_base_keeps_layers_up_to_entrypoint_and_drops_the_aifa_layer(tmp_path: Path) -> None:
    """P34: код finengine и мост (слой после ENTRYPOINT) в базе остаться не должны."""
    base_a, base_b, aifa = b"base-os", b"base-app", b"finengine-code"
    history: list[dict[str, Any]] = [
        {"created_by": "FROM ubi8"},
        {"created_by": "RUN groupadd aef"},
        {"created_by": ENTRYPOINT, "empty_layer": True},
        {"created_by": "COPY finengine /usr/lib64/python3.12/site-packages"},
    ]
    src, dst = tmp_path / "in.tar", tmp_path / "out.tar"
    _archive(src, history, [base_a, base_b, aifa])

    _load().build_base_archive(src, dst)

    manifest, config, names = _read(dst)
    kept = [layer["digest"] for layer in manifest["layers"]]
    assert kept == [_digest(base_a), _digest(base_b)]
    assert config["rootfs"]["diff_ids"] == kept
    assert [h["created_by"] for h in config["history"]][-1] == ENTRYPOINT
    assert f"blobs/sha256/{_digest(aifa).split(':')[1]}" not in names


def test_an_image_without_the_base_entrypoint_is_refused(tmp_path: Path) -> None:
    """Режем по ENTRYPOINT базы; чужой образ без него — отказ, а не молчаливая копия целого."""
    src, dst = tmp_path / "in.tar", tmp_path / "out.tar"
    _archive(src, [{"created_by": "FROM scratch"}], [b"layer"])

    with pytest.raises(ValueError, match="ENTRYPOINT"):
        _load().build_base_archive(src, dst)
