"""F9 (образы) — Dockerfile dev: публичная база, контексты, доставка файлов (ADR-0007, 0018, 0020).

Вторая половина F9 (`test_dev_config.py` — окружение и compose). Docker не нужен: проверяется
текст Dockerfile и наличие файлов, которые он копирует. Что образы действительно собираются —
`docker build` (README.md каталога); проверка ловит то, что собрать без docker не проверить:
«объявлено, но в образ не доехало» (P15, тот же разрыв, что стоил AIFA #252 и #253).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from arch_support import DEV_DIR, REPO_ROOT

CORPORATE = ("nexus", "genai.int", "sbrf.ru", "sberosc")
MIN_WHITELISTED_TOOLS = 15  # AIFA держит у себя 18: меньше 15 — читаем не тот файл
# Код службы ищет deploy/ рядом с src/ (llm/tools.py, net/ru_gov_tls.py): и в чекауте, и в образе.
ENGINE_DEPLOY = REPO_ROOT / "aifa" / "finengine" / "deploy"
# ADR-0007: пакеты комнаты; без версии вычисления атомов расходятся с комнатой старого dev (P30).
ROOM_PACKAGES = ("numpy", "pandas", "openpyxl", "python-pptx", "python-docx")


def _instructions(dockerfile: Path) -> list[str]:
    """Строки Dockerfile без комментариев и пустых."""
    return [
        line.strip()
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _bases(lines: list[str]) -> list[str]:
    return [line.split()[1] for line in lines if line.upper().startswith("FROM ")]


def _ignore_entries(path: Path) -> set[str]:
    entries = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {e.rstrip("/") for e in entries if e and not e.startswith("#")}


def image_violations(dev: Path, engine_deploy: Path = ENGINE_DEPLOY) -> list[str]:
    problems: list[str] = []
    dockerfiles = {
        "ouroboros": dev / "ouroboros.Dockerfile",
        "finengine": dev / "finengine.Dockerfile",
        "frontend": dev / "frontend.Dockerfile",
        "workstation": dev / "workstation" / "Dockerfile",
    }
    lines: dict[str, list[str]] = {}
    for name, path in dockerfiles.items():
        if not path.is_file():
            problems.append(f"F9 (ADR-0020): нет {path.relative_to(dev)}")
            continue
        lines[name] = _instructions(path)
        problems.extend(
            f"F9 (ADR-0020): {name}: корпоративный реестр в {line!r} — образы на публичных базах"
            for line in lines[name]
            if any(marker in line.lower() for marker in CORPORATE)
        )

    if "ouroboros" in lines:
        if _bases(lines["ouroboros"])[:1] != ["python:3.12-slim"]:
            problems.append("F9 (ADR-0020): база образа ouroboros — не python:3.12-slim")
        problems.extend(
            f"F9 (F1): ouroboros.Dockerfile копирует всё дерево или aifa/: {line!r}"
            for line in lines["ouroboros"]
            if line.upper().startswith(("COPY", "ADD"))
            and (line.split()[1] in {".", "./"} or line.split()[1].startswith("aifa"))
        )
        ignore = dev / "ouroboros.Dockerfile.dockerignore"
        if not ignore.is_file():
            problems.append(
                "F9 (F1): нет ouroboros.Dockerfile.dockerignore рядом с Dockerfile — "
                "корневой .dockerignore ouroboros не правим"
            )
        elif not {"aifa", "aifa-symbiosis"} <= _ignore_entries(ignore):
            problems.append("F9 (ADR-0020): dockerignore образа ouroboros не исключает aifa/")

    if "finengine" in lines:
        text = "\n".join(lines["finengine"])
        for target in ("/app/deploy/tools_whitelist.yaml", "/app/deploy/certs/"):
            if target not in text:
                problems.append(f"F9 (P15): finengine.Dockerfile не доставляет {target} в образ")
        whitelist = engine_deploy / "tools_whitelist.yaml"
        enabled = (yaml.safe_load(whitelist.read_text(encoding="utf-8")) or {}).get("enabled") or []
        if len(enabled) < MIN_WHITELISTED_TOOLS:
            problems.append(
                f"F9 (P15): в tools_whitelist.yaml {len(enabled)} имён — каталог инструментов "
                "в образе пуст или разобран не тот файл"
            )
        if not list((engine_deploy / "certs").glob("*.pem")):
            problems.append(
                "F9 (P15): в finengine/certs нет сертификатов — Росстат останется закрыт"
            )

    if "workstation" in lines:
        if _bases(lines["workstation"])[:1] != ["workstation-svc:local"]:
            problems.append("F9 (ADR-0007): база Workstation контура — не workstation-svc:local")
        text = "\n".join(lines["workstation"]).lower()
        problems.extend(
            f"F9 (ADR-0007): в слое Workstation есть `{word}` — слой без кода службы "
            "и моста моделей"
            for word in ("finengine", "bridge", "infproxy")
            if word in text
        )
        # P4: в базе entrypoint.sh с CRLF (`#!/bin/sh\r`), ядро отвечает «no such file»;
        # старый AIFA обходит это своим start.sh, у контура его нет.
        if "entrypoint.sh" not in text:
            problems.append(
                "F9 (P4): workstation/Dockerfile не нормализует /app/entrypoint.sh (CRLF базы)"
            )
        tokens = {t.rstrip("\\") for line in lines["workstation"] for t in line.split()}
        problems.extend(
            f"F9 (P30): пакет комнаты {name} без версии в workstation/Dockerfile"
            for name in ROOM_PACKAGES
            if name in tokens
        )
    return problems


def test_dev_images_are_public_and_deliver_what_the_service_reads() -> None:
    """F9: образы на публичных базах; белый список и сертификаты доезжают в образ службы."""
    problems = image_violations(DEV_DIR)
    assert not problems, "\n".join(problems)


def _copy_dev(tmp_path: Path) -> Path:
    target = tmp_path / "dev"
    shutil.copytree(DEV_DIR, target)
    return target


def test_f9_catches_a_missing_whitelist_delivery(tmp_path: Path) -> None:
    """F9: нарушитель — Dockerfile службы не копирует белый список (все инструменты открыты)."""
    dev = _copy_dev(tmp_path)
    path = dev / "finengine.Dockerfile"
    kept = [
        ln for ln in path.read_text(encoding="utf-8").splitlines() if "tools_whitelist" not in ln
    ]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    assert any("tools_whitelist.yaml" in p for p in image_violations(dev))


def test_f9_catches_ouroboros_context_copying_aifa(tmp_path: Path) -> None:
    """F9: нарушитель — образ ouroboros копирует aifa/ и теряет dockerignore рядом."""
    dev = _copy_dev(tmp_path)
    with (dev / "ouroboros.Dockerfile").open("a", encoding="utf-8") as dockerfile:
        dockerfile.write("COPY aifa ./aifa\n")
    (dev / "ouroboros.Dockerfile.dockerignore").unlink()
    problems = image_violations(dev)
    assert any("копирует всё дерево или aifa/" in p for p in problems)
    assert any("dockerignore рядом с Dockerfile" in p for p in problems)


def test_f9_catches_finengine_in_the_workstation_layer(tmp_path: Path) -> None:
    """F9: нарушитель — в слой Workstation вернулся код службы (ADR-0007)."""
    dev = _copy_dev(tmp_path)
    with (dev / "workstation" / "Dockerfile").open("a", encoding="utf-8") as dockerfile:
        dockerfile.write("COPY finengine /usr/lib64/python3.12/site-packages/finengine\n")
    assert any("finengine" in p and "ADR-0007" in p for p in image_violations(dev))


def test_f9_catches_an_unpinned_room_package(tmp_path: Path) -> None:
    """F9: нарушитель — пакет комнаты без `==` (P30): версия поплывёт при пересборке."""
    dev = _copy_dev(tmp_path)
    path = dev / "workstation" / "Dockerfile"
    path.write_text(
        path.read_text(encoding="utf-8").replace("numpy==", "numpy #", 1), encoding="utf-8"
    )
    assert any("numpy" in p and "P30" in p for p in image_violations(dev))


def test_f9_catches_a_workstation_entrypoint_left_with_crlf(tmp_path: Path) -> None:
    """F9: нарушитель — слой Workstation не чинит CRLF в entrypoint.sh базы (P4)."""
    dev = _copy_dev(tmp_path)
    path = dev / "workstation" / "Dockerfile"
    kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "entrypoint.sh" not in ln]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    assert any("entrypoint.sh" in p and "P4" in p for p in image_violations(dev))


def test_f9_catches_a_corporate_registry(tmp_path: Path) -> None:
    """F9: нарушитель — база из корпоративного реестра (ADR-0020)."""
    dev = _copy_dev(tmp_path)
    path = dev / "frontend.Dockerfile"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "node:20-alpine", "registry.genai.int/ci/node:20-alpine", 1
        ),
        encoding="utf-8",
    )
    assert any("корпоративный реестр" in p for p in image_violations(dev))
