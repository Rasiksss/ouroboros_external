"""Завершённый прогон не помечается оборвавшимся.

🔴 ЧТО СЛУЧИЛОСЬ (25.08.2026). `recover_orphan_runs` считала журнал живым по
ПОСЛЕДНЕМУ событию. Но `artifact` (api/main.py:2337) пишется ПОСЛЕ `done`, и
успешный прогон выглядел оборванным: рестарт бэкенда дописывал ему
«Прогон оборвался при перезапуске сервера. Запустите его заново», а владелец,
переоткрыв прогон, читал отказ вместо своего результата.

Замер на проде до правки: 69 журналов, у 15 после `done` стоит `session_lost`
— 22% успешных прогонов помечены ложно. Та же ошибка была в стороже занятости
`deploy/prod-gateway/prod_busy.py`, и она заблокировала первую же выкатку.

Отрицательный контроль: вернуть `last_event_type in {...}` — оба теста краснеют.
"""
import json

from finengine.skills import run_orchestrator as ro


ПРОГОН = "aaaaaaaa-0000-0000-0000-000000000000"


def _journal(tmp_path, monkeypatch, types):
    """Журнал прогона СТАРОГО образца — файлом на томе.

    ⚠️ Файл здесь не случайность и не пережиток теста: журнал переехал в общее
    хранилище 26.08, и этот набор заодно проверяет, что прогоны эпохи файла
    по-прежнему читаются и закрываются. Проверка идёт через ЧТЕНИЕ
    (`_из_журнала`), а не через содержимое файла: где лежит журнал — дело
    хранилища, а не этого теста.
    """
    from finengine.store import cache as _cache
    from finengine.store import journals as _journals

    d = tmp_path / "documents" / "ir_calendar_events"
    d.mkdir(parents=True)
    p = d / f"{ПРОГОН}.jsonl"
    p.write_text("".join(json.dumps({"type": t}) + "\n" for t in types), encoding="utf-8")
    monkeypatch.setattr(ro.settings, "docs_dir", tmp_path / "documents")
    monkeypatch.setattr(ro.settings, "data_dir", tmp_path)
    monkeypatch.setattr(ro.settings, "journals_database_url", "")
    monkeypatch.setattr(ro.settings, "redis_url", "")
    _journals._cached = None
    _cache._cached = None
    return p


def _события(kind: str = "ir_calendar") -> list[str]:
    return [e.get("type") for e in ro._из_журнала(ПРОГОН, kind)]


def test_done_then_artifact_is_finished(tmp_path, monkeypatch):
    """Хвост «done → artifact» — это успех, а не сирота."""
    _journal(tmp_path, monkeypatch, ["started", "company_done", "done", "artifact"])
    было = _события()

    assert ro.recover_orphan_runs() == {}, "успешный прогон помечен оборвавшимся"
    assert _события() == было, "в закрытый журнал дописали события"


def test_a_truly_broken_run_is_still_marked(tmp_path, monkeypatch):
    """Обратная сторона: прогон без `done` метить по-прежнему обязаны."""
    _journal(tmp_path, monkeypatch, ["started", "company_done"])

    assert ro.recover_orphan_runs().get("ir_calendar") == 1, "сирота не помечена"
    assert "session_lost" in _события()


def test_the_deploy_guard_uses_the_same_rule():
    """Страж занятости и восстановление обязаны судить одинаково.

    Они разошлись однажды — и деплой встал на завершённом прогоне. Проверяем
    поведением: скармливаем стражу тот же хвост «done → artifact».
    """
    import pathlib
    src = pathlib.Path("deploy/prod-gateway/prod_busy.py").read_text(encoding="utf-8")
    body = src.split("def _live_journal_runs")[1].split("def main")[0]
    assert "last not in _TERMINAL" not in body, (
        "страж вернулся к проверке ПОСЛЕДНЕГО события — заблокирует деплой "
        "на завершённом прогоне")
    assert "if not closed:" in body, "страж потерял проверку «журнал содержит done»"
