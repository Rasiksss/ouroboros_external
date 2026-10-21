"""Имена моделей доезжают до контейнера бэкенда — и умолчания не расходятся.

ЧТО СЛУЧИЛОСЬ (02.09.2026). Смена модели на DeepSeek-V4-Pro легла в
`deploy/.env` двумя переменными, бэкенд был пересоздан — и продолжал звать
qwen. У `backend` в compose закрытый список `environment:`, и переменная,
не перечисленная там, до контейнера не доезжает, сколько её ни пиши в .env.
Класс дефекта — «умеет, но не доехало», четвёртый раз на одном параметре.

Две проверки, и вторая важнее первой:
  1. обе переменные проброшены;
  2. умолчание в compose РАВНО умолчанию config.py. Пустое `:-` перекрыло бы
     умолчание пустым именем модели там, где .env её не задаёт (прод).

Отрицательный контроль: убрать строку из compose — красный; заменить умолчание
на пустое — красный.
"""
import re
from pathlib import Path

import yaml

from finengine.config import Settings

REPO = Path(__file__).resolve().parents[1]
VARS = {"FINENGINE_OPENROUTER_MODEL": "openrouter_model",
        "FINENGINE_NEWS_CLASSIFY_MODEL": "news_classify_model"}


def _backend_env() -> dict:
    d = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    env = d["services"]["backend"]["environment"]
    return env if isinstance(env, dict) else dict(kv.split("=", 1) for kv in env)


def test_model_names_reach_the_backend_container():
    env = _backend_env()
    for var in VARS:
        assert var in env, f"{var} не проброшена в backend.environment — .env её не донесёт"


def test_compose_defaults_equal_config_defaults():
    env = _backend_env()
    for var, field in VARS.items():
        m = re.fullmatch(r"\$\{" + var + r":-(.*)\}", str(env[var]))
        assert m, f"{var}: ожидалась форма ${{{var}:-<умолчание>}}, есть {env[var]!r}"
        expected = Settings.model_fields[field].default
        assert m.group(1) == expected, (
            f"{var}: умолчание compose {m.group(1)!r} ≠ config.py {expected!r} — "
            "разойдутся молча")
        assert m.group(1) != "", f"{var}: пустое умолчание перекроет config.py пустой моделью"


ROLE_VARS = {"FINENGINE_PLANNER_MODEL": "planner_model",
             "FINENGINE_LOCATE_MODEL": "locate_model",
             "FINENGINE_READ_VALUE_MODEL": "read_value_model"}


def test_role_model_names_reach_the_backend_container():
    """Модели планировщика и автора атомов (08.10.2026). Пустое умолчание здесь законно: пустое
    поле значит «модель бэкенда», и в config.py умолчание тоже пустое."""
    env = _backend_env()
    for var, field in ROLE_VARS.items():
        assert var in env, f"{var} не проброшена в backend.environment — .env её не донесёт"
        m = re.fullmatch(r"\$\{" + var + r":-(.*)\}", str(env[var]))
        assert m, f"{var}: ожидалась форма ${{{var}:-<умолчание>}}, есть {env[var]!r}"
        assert m.group(1) == Settings.model_fields[field].default
