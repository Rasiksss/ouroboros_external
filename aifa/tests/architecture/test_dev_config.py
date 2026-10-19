"""F9 — конфигурация dev (ADR-0010, 0013, 0016, 0017, 0018, 0027, 0028).

Разбор `aifa/deploy/dev/`: `compose.yml`, `ouroboros.env.example`, `engine.env.example`.
Обязательные значения и списки отключённых инструментов и навыков — снимок
`aifa/contracts/dev_config.snapshot.json` (изменение набора — ADR-0027, F13). Образы и доставка
файлов в них — `test_dev_images.py`.

Проверка не зависит от docker и сети: читает файлы. Что `docker compose config` принимает файл —
отдельный прогон (README.md каталога).
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from arch_support import DEV_DIR, REPO_ROOT

SNAPSHOT_FILE = REPO_ROOT / "aifa" / "contracts" / "dev_config.snapshot.json"

PROJECT = "aifa-sym"
DEFAULT_SERVICES = frozenset({"ouroboros", "ouro-postgres", "s3", "workstation"})
APP_SERVICES = frozenset({"engine", "engine-postgres", "frontend"})
# Контейнеры prod и общий вход: цели изменений быть не могут (ADR-0018).
PROD_TARGETS = ("finengine-prod", "openclaw-prod-gw", "workstation-svc-prod", "finengine-caddy-1")
LOOPBACK = ("127.0.0.1:", "[::1]:")
# ouroboros — только /ready (ADR-0028); фронтенд — вход пользователей по SSH-туннелю (шаг 12):
# оба строго на loopback, остальным сервисам порты запрещены.
PUBLISHING_SERVICES = frozenset({"ouroboros", "frontend"})
# ADR-0050: модель разбора запроса и извлечения службы — DeepSeek-V4-Pro (ADR-0017).
SERVICE_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
MIN_S3_VOLUMES = 30  # две корзины (`ouroboros`, `finengine`) по семь томов роста, с запасом
SECRET_KEY = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN)$")
PLACEHOLDER = "CHANGE_ME"
DB_URL_KEY = re.compile(r"^OUROBOROS_RUNTIME_DB_URL(_\d+)?$")

# Опорная запись (шаг 05): ходит на старый dev и loopback; адрес prod нигде не появляется.
GOLDEN_DIR = REPO_ROOT / "aifa" / "tests" / "golden"
GOLDEN_HOSTS = frozenset({"finagent.homyakagent.ru", "127.0.0.1", "localhost"})
RESERVED_HOSTS = frozenset({"example.org", "example.com"})  # RFC 2606: «чужой адрес» в тестах
GOLDEN_SUFFIXES = frozenset({".py", ".json", ".md", ".txt", ".sh", ".sse"})
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
SECRET_SHAPE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|Bearer\s+\S{8,}|Set-Cookie|Authorization",
    re.IGNORECASE,
)


def parse_env(path: Path) -> dict[str, str]:
    """`KEY=VALUE` по строке; пустые строки и `#` пропускаются, строчных комментариев нет."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_snapshot(path: Path = SNAPSHOT_FILE) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _items(value: str) -> list[str]:
    return [part for part in re.split(r"[,\s]+", value) if part]


def _strings(node: Any) -> Iterator[str]:
    """Все ключи и строковые значения разобранного YAML (комментарии в разбор не попадают)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _endpoint(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url)
    return (parts.hostname or "", parts.port or 5432, parts.path.strip("/"))


def secret_violations(name: str, env: Mapping[str, str]) -> list[str]:
    """Секреты в примерах — только заглушки: настоящие значения в git не попадают."""
    problems: list[str] = []
    for key, value in env.items():
        if SECRET_KEY.search(key) and value and PLACEHOLDER not in value:
            problems.append(f"F9: {name}: {key} похож на настоящий секрет, а не на заглушку")
        if "@" in value and "://" in value:
            password = urlsplit(value).password or ""
            if password and PLACEHOLDER not in password:
                problems.append(f"F9: {name}: {key} содержит настоящий пароль в адресе")
    return problems


def env_violations(
    ouro: Mapping[str, str], engine: Mapping[str, str], snapshot: Mapping[str, Any]
) -> list[str]:
    problems: list[str] = []
    for key, expected in snapshot["required"].items():
        if ouro.get(key) != expected:
            problems.append(
                f"F9 (ADR-0027): в ouroboros.env.example {key}={ouro.get(key)!r}, "
                f"ждали {expected!r}"
            )
    for key, expected_list in snapshot["lists"].items():
        actual = _items(ouro.get(key, ""))
        if sorted(actual) != sorted(expected_list) or len(set(actual)) != len(actual):
            drift = sorted(set(actual) ^ set(expected_list))
            problems.append(
                f"F9 (ADR-0027): список {key} расходится со снимком dev_config.snapshot.json "
                f"(разница: {drift or 'повтор имени'}). Изменение набора — новая ADR и снимок."
            )
    for key in snapshot.get("shared_secrets", []):
        problems.extend(
            f"F9 (ADR-0028, ADR-0047): {key} нет в {name} — "
            "секрет внутреннего API один у ouroboros и службы"
            for name, env in (("ouroboros.env.example", ouro), ("engine.env.example", engine))
            if key not in env
        )
    service_model = engine.get("FINENGINE_OPENROUTER_MODEL")
    if service_model != SERVICE_MODEL:
        problems.append(
            f"F9 (ADR-0050): FINENGINE_OPENROUTER_MODEL={service_model!r}, нужна {SERVICE_MODEL}: "
            "разбор запроса и извлечение службы идут на этой модели"
        )
    if int(ouro.get("OUROBOROS_WORKER_SLOTS", "0") or 0) < 2:
        problems.append(
            "F9 (ADR-0008, ADR-0027): OUROBOROS_WORKER_SLOTS < 2 — автору не хватит слота"
        )
    for prefix in snapshot["absent_prefixes"]:
        problems.extend(
            f"F9 (ADR-0016): {key} задан в ouroboros.env.example — канал не включается"
            for key in ouro
            if key.startswith(prefix)
        )
    workstation = ouro.get("WORKSTATION_BASE_URL", "")
    if urlsplit(workstation).hostname != "workstation":
        problems.append(
            f"F9 (ADR-0007): WORKSTATION_BASE_URL={workstation!r} — не Workstation контура "
            "(сервис `workstation` compose-проекта)"
        )
    if engine.get("FINENGINE_WORKSTATION_URL") != workstation:
        problems.append(
            "F9 (ADR-0007): FINENGINE_WORKSTATION_URL службы не равен WORKSTATION_BASE_URL "
            "ouroboros — комната службы должна быть в том же Workstation контура"
        )
    return problems


def database_violations(ouro: Mapping[str, str], engine: Mapping[str, str]) -> list[str]:
    ouro_urls = [value for key, value in ouro.items() if DB_URL_KEY.match(key) and value]
    engine_urls = [v for k, v in engine.items() if k.endswith("_DATABASE_URL") and v]
    if not ouro_urls or not engine_urls:
        # Пустое множество сравнения зеленело бы на любом файле.
        return ["F9 (ADR-0013): в примерах окружения нет адресов баз ouroboros или службы"]
    shared = {_endpoint(u) for u in ouro_urls} & {_endpoint(u) for u in engine_urls}
    problems = [
        f"F9 (ADR-0013): база {host}:{port}/{name} указана и у ouroboros, и у службы — "
        "доступа к базе ouroboros у службы быть не должно"
        for host, port, name in sorted(shared)
    ]
    same_endpoint = ouro.get("OUROBOROS_S3_ENDPOINT") == engine.get("FINENGINE_S3_ENDPOINT")
    if same_endpoint and ouro.get("OUROBOROS_S3_BUCKET") == engine.get("FINENGINE_S3_BUCKET"):
        problems.append("F9 (ADR-0013): у ouroboros и службы одна корзина S3 на одном адресе")
    return problems


def _published(service: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for entry in service.get("ports") or []:
        if isinstance(entry, Mapping):
            result.append(f"{entry.get('host_ip', '')}:{entry.get('published', '')}")
        else:
            result.append(str(entry))
    return result


def _env_files(service: Mapping[str, Any]) -> list[str]:
    declared = service.get("env_file") or []
    entries = [declared] if isinstance(declared, (str, Mapping)) else declared
    return [str(e["path"] if isinstance(e, Mapping) else e) for e in entries]


def compose_violations(compose: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    services: Mapping[str, Mapping[str, Any]] = compose.get("services") or {}
    networks: Mapping[str, Any] = compose.get("networks") or {}
    volumes: Mapping[str, Any] = compose.get("volumes") or {}
    if compose.get("name") != PROJECT:
        problems.append(
            f"F9 (ADR-0018): имя compose-проекта {compose.get('name')!r}, ждали {PROJECT!r}"
        )
    for missing in sorted((DEFAULT_SERVICES | APP_SERVICES) - set(services)):
        problems.append(f"F9: в compose нет сервиса `{missing}`")
    names = [str(compose.get("name", "")), *services, *networks, *volumes]
    for service in services.values():
        names += [str(service.get("container_name", "")), str(service.get("image", ""))]
    names += [str(v.get("name", "")) for v in (*networks.values(), *volumes.values()) if v]
    problems.extend(
        f"F9 (ADR-0018): `prod` в имени {name!r}" for name in names if "prod" in name.lower()
    )
    for text in _strings(compose):
        if any(target in text for target in (*PROD_TARGETS, "infproxy")):
            problems.append(f"F9 (ADR-0018): compose ссылается на общий с prod ресурс: {text!r}")
    for name, service in services.items():
        profiles = service.get("profiles") or []
        if name in APP_SERVICES and "app" not in profiles:
            problems.append(f"F9: `{name}` без профиля `app` — поднимется до шага 12")
        if name in DEFAULT_SERVICES and profiles:
            problems.append(f"F9: `{name}` под профилем {profiles} — контур не поднимется целиком")
        ports = _published(service)
        if ports and name not in PUBLISHING_SERVICES:
            problems.append(f"F9 (ADR-0028): `{name}` публикует порты {ports}")
        problems.extend(
            f"F9 (ADR-0028): `{name}` публикует {p!r} не на 127.0.0.1"
            for p in ports
            if not p.startswith(LOOPBACK)
        )
        if service.get("network_mode"):
            problems.append(f"F9 (ADR-0028): `{name}` с network_mode — вне внутренней сети")
        if list(service.get("networks") or []) != list(networks):
            problems.append(f"F9 (ADR-0028): `{name}` не только в единственной внутренней сети")
        for mount in service.get("volumes") or []:
            source = str(mount.get("source", "")) if isinstance(mount, Mapping) else str(mount)
            if source.split(":", 1)[0] not in volumes:
                problems.append(
                    f"F9 (ADR-0018): `{name}` монтирует {source!r} — не именованный том"
                )
    if len(networks) != 1:
        problems.append(f"F9 (ADR-0028): сетей {len(networks)}, ждали одну внутреннюю")
    problems.extend(
        f"F9 (ADR-0018): {kind} {name!r} внешний — это путь к чужим контейнерам"
        for kind, group in (("сеть", networks), ("том", volumes))
        for name, spec in group.items()
        if isinstance(spec, Mapping) and spec.get("external")
    )
    problems.extend(
        f"F9 (ADR-0018): том {name!r} без имени `{PROJECT}-*`"
        for name, spec in volumes.items()
        if not str((spec or {}).get("name", "")).startswith(f"{PROJECT}-")
    )
    problems.extend(_s3_volume_violations(services.get("s3", {})))
    for service_name, env_file in (("ouroboros", "ouroboros.env"), ("engine", "engine.env")):
        files = _env_files(services.get(service_name, {}))
        if env_file not in files:
            problems.append(f"F9: `{service_name}` не читает {env_file} (env_file: {files})")
    return problems


def _s3_volume_violations(service: Mapping[str, Any]) -> list[str]:
    """SeaweedFS сам выводит потолок томов из свободного диска (на dev — 9); корзина растёт семью
    томами, поэтому вторая корзина (`finengine`) не получала ни одного и PutObject отвечал
    InternalError (шаг 12)."""
    command = [str(item) for item in service.get("command") or []]
    limits = [item.split("=", 1)[1] for item in command if item.startswith("-volume.max=")]
    if not limits or not limits[0].isdigit() or int(limits[0]) < MIN_S3_VOLUMES:
        return [
            f"F9 (шаг 12): у `s3` нет -volume.max>={MIN_S3_VOLUMES}: две корзины не поместятся "
            "в потолок томов, выведенный из диска"
        ]
    return []


def workstation_limit_violations(
    compose: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> list[str]:
    """Пределы Workstation контура (ADR-0039): контейнер держит память, CPU и процессы (cgroup
    песочницы в контейнере не действует), окружение — время и rlimit одного exec."""
    service: Mapping[str, Any] = (compose.get("services") or {}).get("workstation") or {}
    environment: Mapping[str, Any] = service.get("environment") or {}
    limits = snapshot["workstation_limits"]
    problems = [
        f"F9 (ADR-0039): у `workstation` {key}={service.get(key)!r}, ждали {expected!r}"
        for key, expected in limits["container"].items()
        if str(service.get(key)) != str(expected)
    ]
    problems += [
        f"F9 (ADR-0039): у `workstation` {key}={environment.get(key)!r}, ждали {expected!r}"
        for key, expected in limits["environment"].items()
        if str(environment.get(key)) != str(expected)
    ]
    return problems


def dev_config_violations(dev: Path, snapshot: Mapping[str, Any]) -> list[str]:
    ouro = parse_env(dev / "ouroboros.env.example")
    engine = parse_env(dev / "engine.env.example")
    infra = parse_env(dev / "infra.env.example")
    compose = yaml.safe_load((dev / "compose.yml").read_text(encoding="utf-8"))
    return [
        *secret_violations("ouroboros.env.example", ouro),
        *secret_violations("engine.env.example", engine),
        *secret_violations("infra.env.example", infra),
        *env_violations(ouro, engine, snapshot),
        *database_violations(ouro, engine),
        *compose_violations(compose),
        *workstation_limit_violations(compose, snapshot),
    ]


def golden_violations(root: Path) -> list[str]:
    """Адреса вне списка стендов и цели prod — в любом файле; токены и cookie — в записях
    (`*.json`), где им не место (код называет эти заголовки, но не хранит их значения)."""
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in GOLDEN_SUFFIXES or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(root).as_posix()
        for host in sorted(set(URL_HOST.findall(text)) - GOLDEN_HOSTS - RESERVED_HOSTS):
            problems.append(f"F9 (ADR-0018): {name} называет адрес {host!r} вне списка стендов")
        problems.extend(
            f"F9 (ADR-0018): {name} ссылается на {target!r}"
            for target in (*PROD_TARGETS, "infproxy")
            if target in text
        )
        if path.suffix == ".json" and SECRET_SHAPE.search(text):
            problems.append(f"F9: в записи {name} секрет: токен, Authorization или cookie")
    return problems


def _copy_dev(tmp_path: Path) -> Path:
    target = tmp_path / "dev"
    shutil.copytree(DEV_DIR, target)
    return target


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"в {path.name} нет {old!r}: тест-нарушитель устарел"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_dev_config_matches_the_snapshot() -> None:
    """F9: конфигурация dev равна снимку и держит сеть, имена и разделение баз."""
    problems = dev_config_violations(DEV_DIR, load_snapshot())
    assert not problems, "\n".join(problems)


def test_f9_catches_a_changed_required_value(tmp_path: Path) -> None:
    """F9: нарушитель — таймаут задачи не 2700 с (ADR-0027)."""
    dev = _copy_dev(tmp_path)
    _edit(
        dev / "ouroboros.env.example",
        "OUROBOROS_HARD_TIMEOUT_SEC=2700",
        "OUROBOROS_HARD_TIMEOUT_SEC=1800",
    )
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_HARD_TIMEOUT_SEC" in p and "ADR-0027" in p for p in problems)


def test_f9_catches_a_drifted_tool_list(tmp_path: Path) -> None:
    """F9: нарушитель — инструмент чтения вложений убран из отключённых без ADR."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "ouroboros.env.example", "attachment_read,", "")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_DISABLED_TOOLS" in p and "attachment_read" in p for p in problems)


def test_f9_catches_a_missing_skill_admission(tmp_path: Path) -> None:
    """F9: нарушитель — навык workstation не допущен владельцам (ADR-0036): в multiuser
    встроенные code_exec и file_* без допуска недоступны, а список по умолчанию пуст."""
    dev = _copy_dev(tmp_path)
    _edit(
        dev / "ouroboros.env.example",
        "OUROBOROS_USER_ENABLED_SKILL_IDS=distribution:workstation",
        "OUROBOROS_USER_ENABLED_SKILL_IDS=",
    )
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_USER_ENABLED_SKILL_IDS" in p for p in problems)


def test_f9_catches_a_planning_skill_the_owner_cannot_read(tmp_path: Path) -> None:
    """F9: нарушитель — `aifa-planning` не допущен владельцам (ADR-0036, ADR-0047): в multiuser
    навык-инструкция
    без допуска не виден задаче, и корень не прочитает порядок работы с `aifa_goal` (найдено
    живым прогоном шага 10)."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "ouroboros.env.example", "distribution:aifa-planning,", "")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_USER_ENABLED_SKILL_IDS" in p and "aifa-planning" in p for p in problems)


def test_f9_catches_an_engine_extension_the_owner_cannot_call(tmp_path: Path) -> None:
    """F9: нарушитель — `aifa-engine` не допущен владельцам: доверенный список ADR-0038 делает его
    видимым
    ядру, но задача без допуска получает `TOOL_UNAVAILABLE` на каждый вызов `aifa_goal`."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "ouroboros.env.example", ",distribution:aifa-engine\n", "\n")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_USER_ENABLED_SKILL_IDS" in p and "aifa-engine" in p for p in problems)


def test_f9_catches_a_drifted_engine_address(tmp_path: Path) -> None:
    """F9: нарушитель — плагин `aifa-engine` смотрит не на службу `engine` контура (ADR-0022,
    ADR-0028)."""
    dev = _copy_dev(tmp_path)
    _edit(
        dev / "ouroboros.env.example",
        "AIFA_ENGINE_URL=http://engine:8000",
        "AIFA_ENGINE_URL=http://elsewhere:8000",
    )
    problems = dev_config_violations(dev, load_snapshot())
    assert any("AIFA_ENGINE_URL" in p for p in problems)


def test_f9_catches_an_engine_token_that_one_side_does_not_have(tmp_path: Path) -> None:
    """F9: нарушитель — у службы нет `AIFA_ENGINE_TOKEN`: её внутренний API закрыт целиком (503,
    ADR-0028),
    и ouroboros получает отказ на каждый вызов."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "engine.env.example", "AIFA_ENGINE_TOKEN=CHANGE_ME\n", "")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("AIFA_ENGINE_TOKEN" in p and "engine.env.example" in p for p in problems)


def test_f9_catches_a_missing_trusted_executable_skill(tmp_path: Path) -> None:
    """F9: нарушитель — доверенный навык aifa-engine убран из списка (ADR-0038): в multiuser
    без него инструменты плагина не доходят до задачи."""
    dev = _copy_dev(tmp_path)
    _edit(
        dev / "ouroboros.env.example",
        "OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=distribution:aifa-engine",
        "OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=",
    )
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS" in p for p in problems)


def test_f9_catches_a_port_published_beyond_loopback(tmp_path: Path) -> None:
    """F9: нарушитель — порт ouroboros опубликован на все интерфейсы (ADR-0028)."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "compose.yml", '"127.0.0.1:18180:8080"', '"18180:8080"')
    problems = dev_config_violations(dev, load_snapshot())
    assert any("ADR-0028" in p and "127.0.0.1" in p for p in problems)


def test_f9_catches_a_frontend_published_beyond_loopback(tmp_path: Path) -> None:
    """F9: фронтенд доступен только через туннель, на все интерфейсы не публикуется (ADR-0028)."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "compose.yml", '"127.0.0.1:13000:3000"', '"13000:3000"')
    problems = dev_config_violations(dev, load_snapshot())
    assert any("`frontend` публикует" in p and "127.0.0.1" in p for p in problems)


def test_f9_catches_a_port_on_the_engine(tmp_path: Path) -> None:
    """F9: нарушитель — у службы опубликован порт, даже на loopback (её API не для входа)."""
    dev = _copy_dev(tmp_path)
    compose = yaml.safe_load((dev / "compose.yml").read_text(encoding="utf-8"))
    compose["services"]["engine"]["ports"] = ["127.0.0.1:18000:8000"]
    (dev / "compose.yml").write_text(yaml.safe_dump(compose, allow_unicode=True), encoding="utf-8")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("`engine` публикует" in p for p in problems)


def test_f9_catches_an_s3_left_with_the_disk_derived_volume_limit(tmp_path: Path) -> None:
    """F9: нарушитель — у `s3` нет явного потолка томов, вторая корзина не получает томов."""
    dev = _copy_dev(tmp_path)
    _edit(dev / "compose.yml", ", -volume.max=40", "")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("-volume.max" in p for p in problems)


def test_f9_catches_the_service_model_drifting_from_adr_0050(tmp_path: Path) -> None:
    """F9: нарушитель — модель службы сменили с DeepSeek-V4-Pro (ADR-0050)."""
    dev = _copy_dev(tmp_path)
    _edit(
        dev / "engine.env.example",
        "FINENGINE_OPENROUTER_MODEL=deepseek-ai/DeepSeek-V4-Pro",
        "FINENGINE_OPENROUTER_MODEL=zai-org/GLM-5.2",
    )
    problems = dev_config_violations(dev, load_snapshot())
    assert any("ADR-0050" in p and "FINENGINE_OPENROUTER_MODEL" in p for p in problems)


def test_f9_catches_a_port_on_the_workstation(tmp_path: Path) -> None:
    """F9: нарушитель — у Workstation контура опубликован порт, даже на loopback."""
    dev = _copy_dev(tmp_path)
    compose = yaml.safe_load((dev / "compose.yml").read_text(encoding="utf-8"))
    compose["services"]["workstation"]["ports"] = ["127.0.0.1:18181:8080"]
    (dev / "compose.yml").write_text(yaml.safe_dump(compose, allow_unicode=True), encoding="utf-8")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("`workstation` публикует" in p for p in problems)


def test_f9_catches_an_unlimited_workstation(tmp_path: Path) -> None:
    """F9: нарушитель — у Workstation контура снят предел памяти контейнера (ADR-0039): код
    модели забрал бы память хоста, общего с prod."""
    dev = _copy_dev(tmp_path)
    compose = yaml.safe_load((dev / "compose.yml").read_text(encoding="utf-8"))
    del compose["services"]["workstation"]["mem_limit"]
    (dev / "compose.yml").write_text(yaml.safe_dump(compose, allow_unicode=True), encoding="utf-8")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("ADR-0039" in p and "mem_limit" in p for p in problems)


def test_f9_catches_a_raised_exec_cpu_limit(tmp_path: Path) -> None:
    """F9: нарушитель — предел процессорного времени exec поднят без ADR (ADR-0039)."""
    dev = _copy_dev(tmp_path)
    compose = yaml.safe_load((dev / "compose.yml").read_text(encoding="utf-8"))
    compose["services"]["workstation"]["environment"]["SESSION_RLIMIT_CPU_SEC"] = "3600"
    (dev / "compose.yml").write_text(yaml.safe_dump(compose, allow_unicode=True), encoding="utf-8")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("ADR-0039" in p and "SESSION_RLIMIT_CPU_SEC" in p for p in problems)


def test_f9_catches_prod_in_a_name_and_a_prod_target(tmp_path: Path) -> None:
    """F9: нарушитель — `prod` в имени сервиса и внешняя сеть общего Caddy."""
    dev = _copy_dev(tmp_path)
    compose = yaml.safe_load((dev / "compose.yml").read_text(encoding="utf-8"))
    compose["services"]["ouro-prod-postgres"] = compose["services"].pop("ouro-postgres")
    compose["networks"]["edge"] = {"external": True, "name": "finengine-caddy-1"}
    (dev / "compose.yml").write_text(yaml.safe_dump(compose, allow_unicode=True), encoding="utf-8")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("`prod` в имени" in p for p in problems)
    assert any("finengine-caddy-1" in p for p in problems)
    assert any("внешний" in p for p in problems)


def test_f9_catches_the_same_database_for_both_products(tmp_path: Path) -> None:
    """F9: нарушитель — служба смотрит в базу ouroboros (ADR-0013)."""
    dev = _copy_dev(tmp_path)
    ouro_url = parse_env(dev / "ouroboros.env.example")["OUROBOROS_RUNTIME_DB_URL"]
    with (dev / "engine.env.example").open("a", encoding="utf-8") as engine_env:
        engine_env.write(f"\nFINENGINE_DIALOGS_DATABASE_URL={ouro_url}\n")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("ADR-0013" in p and "доступа к базе ouroboros" in p for p in problems)


def test_f9_catches_a_real_looking_secret(tmp_path: Path) -> None:
    """F9: нарушитель — в примере ключ шифрования вместо заглушки."""
    dev = _copy_dev(tmp_path)
    env = dev / "ouroboros.env.example"
    text = re.sub(
        r"^OUROBOROS_CREDENTIALS_ENCRYPTION_KEY=.*$",
        "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY=" + "QUJD" * 21,
        env.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    env.write_text(text, encoding="utf-8")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY" in p and "секрет" in p for p in problems)


def test_f9_catches_an_enabled_channel(tmp_path: Path) -> None:
    """F9: нарушитель — включён вход A2A, которого ADR-0016 не допускает."""
    dev = _copy_dev(tmp_path)
    with (dev / "ouroboros.env.example").open("a", encoding="utf-8") as env:
        env.write("\nA2A_ENABLED=true\n")
    problems = dev_config_violations(dev, load_snapshot())
    assert any("A2A_ENABLED" in p and "ADR-0016" in p for p in problems)


def test_f9_golden_has_no_foreign_address_and_no_secret() -> None:
    """F9: записывающий клиент ходит только на dev и loopback; в `aifa/tests/golden` нет адреса
    prod и учётных данных (шаг 05)."""
    assert golden_violations(GOLDEN_DIR) == []


def test_f9_catches_a_foreign_address_in_golden(tmp_path: Path) -> None:
    """F9: нарушитель — запись или пример с адресом, которого нет в списке стендов."""
    (tmp_path / "scenario.json").write_text(
        '{"url": "https://app.corp-bank.example.net/api"}', encoding="utf-8"
    )
    problems = golden_violations(tmp_path)
    assert any("app.corp-bank.example.net" in p and "ADR-0018" in p for p in problems)


def test_f9_catches_a_prod_container_name_in_golden(tmp_path: Path) -> None:
    """F9: нарушитель — цель prod названа в коде записи."""
    (tmp_path / "golden_x.py").write_text("TARGET = 'finengine-prod-backend-1'\n", encoding="utf-8")
    assert any("finengine-prod" in p for p in golden_violations(tmp_path))


def test_f9_catches_a_token_and_a_cookie_in_a_recording(tmp_path: Path) -> None:
    """F9: нарушитель — в записи остался JWT или заголовок Set-Cookie."""
    jwt = "eyJhbGciOiJIUzI1NiJ9." + "a" * 20 + "." + "b" * 20
    (tmp_path / "token.json").write_text(f'{{"h": "{jwt}"}}', encoding="utf-8")
    (tmp_path / "cookie.json").write_text('{"c": "Set-Cookie: tok=1"}', encoding="utf-8")
    problems = golden_violations(tmp_path)
    assert any("token.json" in p and "секрет" in p for p in problems)
    assert any("cookie.json" in p and "секрет" in p for p in problems)


def test_f9_allows_the_dev_stand_loopback_and_reserved_example_hosts(tmp_path: Path) -> None:
    (tmp_path / "scenario.json").write_text(
        '["https://finagent.homyakagent.ru/api", "http://127.0.0.1:18180", "https://example.org"]',
        encoding="utf-8",
    )
    assert golden_violations(tmp_path) == []
