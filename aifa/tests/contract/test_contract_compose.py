"""Профиль `contract` держит границы: подменяется только модель, остальное — настоящее (шаг 07).

Читает `aifa/deploy/dev/compose.contract.yml`, без Docker и сети. PRINCIPLES.md §Б.4: подставляется
лишь внешнее и недетерминированное — модель; набор настроек ouroboros — тот же, что на dev (F9).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

DEV = Path(__file__).resolve().parents[2] / "deploy" / "dev"
CONTRACT_COMPOSE = DEV / "compose.contract.yml"
ENV_EXAMPLE = DEV / "ouroboros.env.example"
PROJECT = "aifa-sym-contract"
PROD_MARKERS = ("prod", "finengine-caddy", "openclaw", "infproxy")
LOOPBACK = "127.0.0.1:"
# Что профиль вправе задать поверх `ouroboros.env.example`: адрес и имена моделей, адрес службы
# расчётов и
# заглушки секретов (в примере там CHANGE_ME), и список отключённых инструментов без `schedule_task`
# (C2.4:
# подзадача; на dev он включается шагом 16, ADR-0044 п. 3). Остальное из примера — как на dev.
ALLOWED_ENVIRONMENT = frozenset(
    {
        "OUROBOROS_ENV",
        "OUROBOROS_DATA_DIR",
        "OUROBOROS_SERVER_HOST",
        "OUROBOROS_MODEL",
        "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK",
        "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "OUROBOROS_RUNTIME_DB_URL",
        "OUROBOROS_S3_ACCESS_KEY",
        "OUROBOROS_S3_SECRET_KEY",
        "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY",
        "WORKSTATION_CREATE_SECRET",
        "AIFA_ENGINE_URL",
        "AIFA_ENGINE_TOKEN",
        "OUROBOROS_DISABLED_TOOLS",
    }
)
SUBTASK_TOOL = "schedule_task"


def _load() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT_COMPOSE.read_text(encoding="utf-8"))
    return loaded


def _example_value(name: str) -> str:
    prefix = f"{name}="
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    return next(line[len(prefix) :] for line in lines if line.startswith(prefix))


def _published(service: Mapping[str, Any]) -> list[str]:
    return [str(entry) for entry in service.get("ports") or []]


def contract_violations(compose: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    services: Mapping[str, Mapping[str, Any]] = compose.get("services") or {}
    ouroboros: Mapping[str, Any] = services.get("ouroboros") or {}
    environment: Mapping[str, Any] = ouroboros.get("environment") or {}
    if compose.get("name") != PROJECT:
        problems.append(
            f"имя проекта {compose.get('name')!r}, ждали {PROJECT!r}: свой проект, не dev"
        )
    if "workstation" in services or "engine" in services:
        problems.append("в профиле contract нет Workstation и службы (тесты с ними — живые)")
    for extra in sorted(set(environment) - ALLOWED_ENVIRONMENT):
        problems.append(
            f"ouroboros: {extra} переопределён — профиль меняет только модель и заглушки"
        )
    if ouroboros.get("env_file") != "ouroboros.env.example":
        problems.append(
            "ouroboros читает не ouroboros.env.example — набор настроек разошёлся с dev"
        )
    example_tools = _example_value("OUROBOROS_DISABLED_TOOLS").split(",")
    expected_tools = [tool for tool in example_tools if tool != SUBTASK_TOOL]
    if str(environment.get("OUROBOROS_DISABLED_TOOLS", "")).split(",") != expected_tools:
        problems.append(
            f"OUROBOROS_DISABLED_TOOLS профиля — не список dev без {SUBTASK_TOOL}: "
            "профиль меняет ровно одно имя"
        )
    if environment.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL") != "http://stub-model:8000/v1":
        problems.append("модель ouroboros не направлена на сервис stub-model")
    if not all(
        str(environment.get(k, "")).startswith("cloudru::stub-")
        for k in ("OUROBOROS_MODEL", "OUROBOROS_MODEL_LIGHT", "OUROBOROS_MODEL_FALLBACK")
    ):
        problems.append(
            "модели ouroboros называются не cloudru::stub-*: правила сценария их не различат"
        )
    for name, service in services.items():
        if "contract" not in (service.get("profiles") or []):
            problems.append(f"{name}: нет профиля contract")
        problems.extend(
            f"{name}: порт {port!r} не на 127.0.0.1"
            for port in _published(service)
            if not port.startswith(LOOPBACK)
        )
    named = [
        *services,
        *(str(s.get("image", "")) for s in services.values()),
        *(str(v.get("name", "")) for v in (compose.get("volumes") or {}).values()),
        *(str(n.get("name", "")) for n in (compose.get("networks") or {}).values()),
    ]
    problems.extend(
        f"в имени {n!r} есть {m!r}" for n in named for m in PROD_MARKERS if m in n.lower()
    )
    for group in ("volumes", "networks"):
        for key, spec in (compose.get(group) or {}).items():
            if not str((spec or {}).get("name", "")).startswith(f"{PROJECT}-"):
                problems.append(f"{group} {key}: имя без префикса {PROJECT}-")
            if (spec or {}).get("external"):
                problems.append(f"{group} {key}: внешний — путь к чужим контейнерам")
    return problems


def test_contract_profile_keeps_its_boundaries() -> None:
    """Профиль contract: свой проект, loopback, без prod; из настоящего — всё, кроме модели."""
    problems = contract_violations(_load())
    assert not problems, "\n".join(problems)


def test_contract_check_catches_a_changed_dev_setting() -> None:
    """Нарушитель: профиль «заодно» выключает инструменты — это уже не настоящий ouroboros."""
    compose = copy.deepcopy(_load())
    compose["services"]["ouroboros"]["environment"]["OUROBOROS_DISABLED_TOOLS"] = "code_exec"
    assert any("OUROBOROS_DISABLED_TOOLS" in p for p in contract_violations(compose))


def test_contract_profile_enables_only_the_subtask_tool() -> None:
    """C2.4: от списка dev профиль отличается одним именем: подзадачи включены."""
    environment = _load()["services"]["ouroboros"]["environment"]
    profile = set(str(environment["OUROBOROS_DISABLED_TOOLS"]).split(","))
    dev = set(_example_value("OUROBOROS_DISABLED_TOOLS").split(","))
    assert dev - profile == {SUBTASK_TOOL}
    assert profile - dev == set()


def test_contract_check_catches_a_port_outside_loopback() -> None:
    compose = copy.deepcopy(_load())
    compose["services"]["stub-model"]["ports"] = ["0.0.0.0:18182:8000"]
    assert any("не на 127.0.0.1" in p for p in contract_violations(compose))


def test_contract_check_catches_a_model_not_pointed_at_the_stub() -> None:
    compose = copy.deepcopy(_load())
    compose["services"]["ouroboros"]["environment"]["CLOUDRU_FOUNDATION_MODELS_BASE_URL"] = (
        "https://foundation-models.api.cloud.ru/v1"
    )
    assert any("stub-model" in p for p in contract_violations(compose))
