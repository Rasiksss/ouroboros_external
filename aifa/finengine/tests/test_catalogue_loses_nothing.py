"""Справочник не теряет НИЧЕГО из старого реестра (#250).

ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 15.08, дословно по смыслу: «Справочник, который выносится
из хардкодного реестра, должен полностью соответствовать старому реестру, ни
один алиас, хинт или ещё какое-либо описание не должны потеряться».

ЗАЧЕМ МАШИНОЙ, А НЕ ГЛАЗАМИ. Перенос делали три агента разными партиями, и
граница между партиями оказалась дырявой: семь показателей не взял никто —
внебалансовые обязательства, капитализированные проценты, амортизация права
пользования, мнение аудитора. Никто не соврал: каждый отчитался за свою часть,
а «между частями» не отвечал никто. Такую дыру видит только сплошная сверка.

ЭТОТ СТОРОЖ — ЕДИНСТВЕННОЕ, ЧТО ДЕЛАЕТ ПЕРЕЕЗД БЕЗОПАСНЫМ. Пока извлечение
работает по старому реестру, недостача в справочнике незаметна. В день, когда
источником станет справочник, она превратится в молча не извлечённые
показатели — и списывать будут на качество модели, а не на пропущенный перенос.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: удалить любой файл из
`station-skills/aifa-extract/method/catalogue/<ячейка>/` — тест обязан назвать
его поимённо.
"""
from __future__ import annotations

import pytest

from finengine.skills.extraction import catalogue as cat
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

# ВСЕ ячейки реестра, а не те, что успели перенести. Сверка по двум из восьми
# молчала о шести пустых — ровно та же дыра «между частями не отвечал никто»,
# что дала недостачу семи записей, только шире: 443 показателя из 600.
CELLS = PROFILE_IDS

# Поля, которые обязаны доехать. Пустое в реестре — не потеря: переносить
# нечего. Потеря — когда в реестре ЕСТЬ, а в справочнике НЕТ.
CARRIED = ("name_ru", "name_en", "aliases_ru", "aliases_en", "description", "hint")


def _registry(cell: str) -> dict:
    return {m["enum"]: m for m in registry_for_profile(cell).get("extractable", [])}


@pytest.mark.parametrize("cell", CELLS)
def test_every_registry_metric_has_a_record(cell: str):
    """Ни один показатель не потерялся между партиями переноса."""
    reg = _registry(cell)
    have = {m.enum for m in cat.load(cell).select()}
    missing = sorted(set(reg) - have)
    assert not missing, (
        f"{cell}: в справочнике нет {len(missing)} показателей из реестра — "
        f"{missing}.\nПосле переключения на справочник они просто не будут "
        f"извлекаться, и это будет выглядеть как провал качества модели."
    )


@pytest.mark.parametrize("cell", CELLS)
def test_no_field_is_lost(cell: str):
    """Ни один алиас, хинт или описание не потерялись при переносе."""
    reg = _registry(cell)
    records = {m.enum: m for m in cat.load(cell).select()}
    lost: list[str] = []
    for enum, src in reg.items():
        rec = records.get(enum)
        if rec is None:
            continue  # отсутствие записи ловит тест выше — не дублируем
        for field in CARRIED:
            want = src.get(field)
            if want and not getattr(rec, field, None):
                lost.append(f"{enum}.{field}")
    assert not lost, (
        f"{cell}: потеряно {len(lost)} полей при переносе — {lost[:15]}"
        f"{'…' if len(lost) > 15 else ''}.\n"
        "Подсказка «где искать» и синонимы — это то, чем модель узнаёт строку "
        "в отчёте; потерянный алиас = ненайденный показатель."
    )


@pytest.mark.parametrize("cell", CELLS)
def test_catalogue_adds_nothing_the_registry_does_not_know(cell: str):
    """Обратная сторона: в справочнике нет показателей, которых нет в реестре.

    Лишняя запись опаснее, чем кажется: она уедет в прогон как полноценный
    показатель, а сверить её будет не с чем — ни эталона, ни формулы.
    """
    reg = _registry(cell)
    extra = sorted({m.enum for m in cat.load(cell).select()} - set(reg))
    assert not extra, (
        f"{cell}: в справочнике {len(extra)} показателей, которых нет в "
        f"реестре — {extra}. Либо это опечатка в имени файла, либо показатель "
        "завели мимо реестра."
    )


@pytest.mark.parametrize("cell", CELLS)
def test_the_catalogue_is_readable_at_all(cell: str):
    """Отрицательный контроль: сверка выше бессмысленна на пустом справочнике."""
    c = cat.load(cell)
    assert not c.problems, f"{cell}: битые записи — {c.problems}"
    # Порог отрицательного контроля, а не состав: самая маленькая ячейка —
    # rsbu_bank_ru с 49 записями. Сверку «сколько именно» делает тест выше,
    # здесь только «справочник не пуст и не заглушка».
    assert len(c.select()) >= 40, (
        f"{cell}: записей {len(c.select())} — слишком мало, чтобы сверка выше "
        "что-то доказывала")
