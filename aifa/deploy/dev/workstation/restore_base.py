#!/usr/bin/env python3
"""Восстановить базу Workstation `workstation-svc:local` из образа комнаты AIFA на dev (P34).

На dev нет тега `workstation-svc:local`: есть `workstation-svc-aifa:local` — база плюс слой AIFA
с кодом finengine, мостом и навыками станции. ADR-0007 требует базу без них. Нижние слои образа
комнаты — и есть база, она кончается `ENTRYPOINT ["./entrypoint.sh"]`; скрипт режет по нему
OCI-архив `docker save` и пишет новый архив. Стандартная библиотека, docker не вызывает.

    docker save workstation-svc-aifa:local -o room.tar
    python3 restore_base.py room.tar base.tar
    docker load -i base.tar                       # Loaded image ID: sha256:<id>
    docker tag sha256:<id> workstation-svc:local
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

BASE_ENTRYPOINT = 'ENTRYPOINT ["./entrypoint.sh"]'
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"


def _blob_path(digest: str) -> str:
    return "blobs/sha256/" + digest.split(":", 1)[1]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_json(tar: tarfile.TarFile, name: str) -> dict[str, Any]:
    member = tar.extractfile(name)
    if member is None:
        raise ValueError(f"в архиве нет {name}")
    loaded: dict[str, Any] = json.load(member)
    return loaded


def _image_manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    """Манифест образа linux/amd64: `docker save` на containerd кладёт индекс, а в нём манифест."""
    document = _read_json(tar, "index.json")
    while "layers" not in document:
        entries = document["manifests"]
        chosen = next(
            (e for e in entries if e.get("platform", {}).get("architecture") == "amd64"),
            entries[0],
        )
        document = _read_json(tar, _blob_path(chosen["digest"]))
    return document


def _base_cut(history: list[dict[str, Any]]) -> tuple[int, int]:
    """(число записей истории, число слоёв) базы — вместе с её `ENTRYPOINT`."""
    for position, entry in enumerate(history):
        if BASE_ENTRYPOINT in entry.get("created_by", ""):
            layers = sum(1 for e in history[: position + 1] if not e.get("empty_layer"))
            return position + 1, layers
    raise ValueError(f"в истории образа нет {BASE_ENTRYPOINT}: это не образ комнаты AIFA")


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def build_base_archive(source: Path, target: Path) -> None:
    """Записать в `target` OCI-архив базы: слои образа `source` до `ENTRYPOINT` включительно."""
    with tarfile.open(source) as src:
        manifest = _image_manifest(src)
        config = _read_json(src, _blob_path(manifest["config"]["digest"]))
        entries, layers = _base_cut(config["history"])
        kept = manifest["layers"][:layers]
        config["history"] = config["history"][:entries]
        config["rootfs"]["diff_ids"] = config["rootfs"]["diff_ids"][:layers]

        config_bytes = json.dumps(config, separators=(",", ":")).encode()
        new_manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": OCI_CONFIG,
                "digest": _digest(config_bytes),
                "size": len(config_bytes),
            },
            "layers": kept,
        }
        manifest_bytes = json.dumps(new_manifest, separators=(",", ":")).encode()
        index = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [
                {
                    "mediaType": OCI_MANIFEST,
                    "digest": _digest(manifest_bytes),
                    "size": len(manifest_bytes),
                }
            ],
        }
        with tarfile.open(target, "w") as dst:
            for layer in kept:
                name = _blob_path(layer["digest"])
                member = src.getmember(name)
                dst.addfile(member, src.extractfile(member))
            _add_bytes(dst, _blob_path(_digest(config_bytes)), config_bytes)
            _add_bytes(dst, _blob_path(_digest(manifest_bytes)), manifest_bytes)
            _add_bytes(dst, "index.json", json.dumps(index).encode())
            _add_bytes(dst, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: restore_base.py <docker-save.tar> <base.tar>", file=sys.stderr)
        return 2
    build_base_archive(Path(argv[1]), Path(argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
