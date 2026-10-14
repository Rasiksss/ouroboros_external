"""Замерщик A/B ходит той же дорогой, что и пользователь.

ЦЕНА, ЗАПЛАЧЕННАЯ 20.08.2026. В этот день включили шардирование второй стадии:
на стенде встали настройки (размер батча 10, потоков 4), боевой путь их получил.
Первый же замер показал в разрезе стадий ОДИНОКИЙ `main_extract_1` — то есть
ровно то, что было до правки.

Дефекта в правке не было. Дефект был в ЗАМЕРЩИКЕ: `benchmarks/ab_extract.py`
зовёт движок напрямую, а параметры батчей боевому пути передаёт
`skills/extract_text.py` — звено, которое замерщик обходит. Замер мерил СТАРОЕ
поведение и, не заметь я стадию, был бы прочитан как «шарды не дали выигрыша».

Это тот же класс, что три незваных доставщика и `docker-compose.dev.yml`,
который стенд не читает: ДВА МЕСТА ДЛЯ ОДНОГО ЗНАНИЯ, и второе тихо отстаёт.
Здесь оно опаснее прочих: расходится не поведение, а ИЗМЕРИТЕЛЬ поведения —
ошибка приезжает под видом числа.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать любой ключ из `extra` в `ab_extract.py` —
тест обязан покраснеть и назвать его.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEASURER = ROOT / "benchmarks" / "ab_extract.py"
LIVE = ROOT / "src" / "finengine" / "skills" / "extract_text.py"


def _code(path: Path) -> str:
    """Без комментариев: сторож читает код, а не пояснения к нему."""
    return re.sub(r"#.*", "", path.read_text(encoding="utf-8"))


def _settings_used(text: str) -> set[str]:
    """Какие настройки темпа названы в файле."""
    return set(re.findall(r"settings\.(extract_\w+)", text))


def _pace_settings(text: str) -> set[str]:
    """Настройки ВТОРОЙ СТАДИИ — те, что боевой путь передаёт движку отдельно.

    ⚠️ Отбор по слову «batch» был бы уже, чем нужно: 20.08 к ним добавилась
    `extract_ask_without_evidence` (спрашивать ли безулиочные показатели) — не
    темп, но такая же ручка сверх умолчаний движка, и разъедься она с
    замерщиком, замер снова показал бы поведение, которого у пользователя нет.
    В выборку входят только настройки темпа и полноты батчей.
    """
    return {s for s in _settings_used(text) if "batch" in s or "evidence" in s}


def test_the_measurer_passes_every_pace_setting_the_live_path_passes():
    live, meas = _pace_settings(_code(LIVE)), _pace_settings(_code(MEASURER))
    assert live, (
        "боевой путь перестал называть настройки темпа — либо их убрали, либо "
        "переименовали; сторож смотрит не туда")
    missing = sorted(live - meas)
    assert not missing, (
        f"замерщик не передаёт движку {missing}, а боевой путь передаёт. "
        f"Замер покажет поведение, которого у пользователя нет — и это будет "
        f"выглядеть как честное число")


def test_the_measurer_actually_hands_them_to_the_engine():
    """Мало объявить словарь — его надо отдать движку."""
    code = _code(MEASURER)
    assert "**extra" in code, (
        "настройки собраны в словарь и никуда не переданы — замерщик снова "
        "меряет умолчания движка")


def test_the_guard_is_not_empty():
    live = _pace_settings(_code(LIVE))
    assert len(live) >= 2, (
        f"в боевом пути найдено {len(live)} настроек темпа — для сравнения "
        f"этого мало, проверка выродилась")
