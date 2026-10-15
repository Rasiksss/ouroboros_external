"""Паспорт прогона несёт версию ПРАВИЛ — реестра и методики (#155).

ЗАЧЕМ. За одни сутки реестр менялся трижды: развёрнут Cost/Income, у стоимости
риска снят ABS (сменился ЗНАК), добавлены три показателя. Отчёты, снятые
накануне, перестали сходиться с новыми — и объяснить пользователю «почему
поехало» было нечем: в выдаче стояла только версия КОДА, а формулы живут в
json-реестрах и элементах методики, на которые деплой не реагирует вовсе.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ:
  1. отпечаток ЧУВСТВИТЕЛЕН к смыслу — правка формулы в ячейке его меняет;
  2. отпечаток ГЛУХ к форматированию — перестановка ключей в json его не меняет
     (иначе он шумел бы на каждом переформатировании и ему перестали бы верить);
  3. личные правки методики ВИДНЫ в паспорте — и на живом пути анализа тоже;
  4. отпечаток реально ДОЕЗЖАЕТ до артефакта и до журнала прогонов — обе
     половины паспорта, и в извлечении, и в анализе;
  5. отпечаток ПОЛОН — покрывает и синонимы общего реестра, которые сливаются с
     ячейкой по дороге в промпт (#147), а не только файл ячейки;
  6. отпечаток снят С ТОГО ИСТОЧНИКА, который исполнялся, — с полки, уехавшей в
     станцию, а не с диска, который после её сборки мог поменяться;
  6b. отпечаток МОЖЕТ снять сам исполнитель — в станции, где полки нет вовсе;
  7. неснятый отпечаток НЕ выдаётся за базовую версию — ни в словаре, ни в листе;
  8. падение паспорта НЕ роняет уже посчитанные извлечение и анализ.

ПОЧЕМУ ЖИВОЙ ПУТЬ. Проверить `registry_fingerprint()` напрямую недостаточно:
именно так был потерян #137 — тест ходил своей дорогой, был зелёным, а на проде
не работало. Поэтому здесь дёргаются НАСТОЯЩИЕ обработчики `fe_extract_metrics`
и `fe_analyze`; подменены только внешние опоры (документ, OCR-текст, станция,
каталог артефактов и файлы журналов) — весь код паспорта исполняется боевой.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from finengine.config import settings
from finengine.skills import station_driver, versioning
from finengine.skills.analysis import personal
from finengine.skills.registry_composition import RegistrySelection, compile_registry

_DATA = Path(__file__).parent.parent / "src" / "finengine" / "data"
_REAL_METRICS = _DATA / "metrics.json"


def _write_effective_cell(path: Path) -> None:
    """Materialise the old external-profile format from the canonical layers.

    These tests deliberately exercise the still-supported legacy shelf/file
    consumer.  Built-in complete copies disappeared in Slice 13, so the
    compatibility fixture must be produced from the effective registry.
    """
    data = compile_registry(
        RegistrySelection.from_profile_id("ifrs_bank_ru")
    ).to_effective_data()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_stale_shelf(monkeypatch):
    """Полка — ГЛОБАЛЬНОЕ состояние процесса (`_shelf_cache` живёт до рестарта).

    Тест, оставивший её собранной, менял бы источник отпечатка у всех следующих:
    те читали бы полку там, где рассчитывают на диск. Поэтому по умолчанию
    «полки нет», а кому она нужна — собирает её сам (фикстура `_shelf`).
    """
    monkeypatch.setattr(station_driver, "_shelf_cache", None)


# ────────────────────── 1–2. свойства самого отпечатка ──────────────────────


@pytest.fixture()
def _cell_dir(tmp_path, monkeypatch):
    """Legacy external profile in tmp — правим его, не трогая репозиторий."""
    _write_effective_cell(tmp_path / "ifrs_bank_ru.json")
    monkeypatch.setattr(versioning, "_REGISTRY_DIR", tmp_path)
    return tmp_path


def _rewrite(path: Path, data: dict, **dump_kwargs) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, **dump_kwargs), encoding="utf-8")


def test_formula_edit_changes_the_fingerprint(_cell_dir):
    """Правка формулы — ровно тот случай, ради которого паспорт заводится.

    Стоимости риска на днях сняли ABS: знак показателя поменялся, а отличить
    вчерашний отчёт от сегодняшнего было нечем.
    """
    path = _cell_dir / "ifrs_bank_ru.json"
    before = versioning.registry_fingerprint("ifrs_bank_ru")["sha256"]

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["calculated"], "в ячейке нет расчётных показателей — тест потерял смысл"
    data["calculated"][0]["formula"] = data["calculated"][0]["formula"] + " * -1"
    _rewrite(path, data)

    after = versioning.registry_fingerprint("ifrs_bank_ru")["sha256"]
    assert before and after, "отпечаток обязан считаться"
    assert before != after, "правка формулы обязана менять отпечаток реестра"


def test_hint_edit_changes_the_fingerprint(_cell_dir):
    """Не только формулы: подсказка тоже уезжает в промпт и двигает результат."""
    path = _cell_dir / "ifrs_bank_ru.json"
    before = versioning.registry_fingerprint("ifrs_bank_ru")["sha256"]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["extractable"][0]["hint"] = "бери из примечания, а не из формы"
    _rewrite(path, data)
    assert versioning.registry_fingerprint("ifrs_bank_ru")["sha256"] != before


def test_key_reordering_and_reindent_do_not_change_the_fingerprint(_cell_dir):
    """Отпечаток обязан молчать на форматировании.

    Если он меняется от перестановки ключей или другого отступа, им перестанут
    пользоваться — «оно всё время разное», и признак версии превратится в шум.
    """
    path = _cell_dir / "ifrs_bank_ru.json"
    before = versioning.registry_fingerprint("ifrs_bank_ru")

    def _shuffle(obj):
        """Тот же смысл, другой порядок ключей (и другие отступы при записи)."""
        if isinstance(obj, dict):
            return {k: _shuffle(obj[k]) for k in reversed(list(obj))}
        if isinstance(obj, list):
            return [_shuffle(x) for x in obj]
        return obj

    _rewrite(path, _shuffle(json.loads(path.read_text(encoding="utf-8"))), indent=4)
    after = versioning.registry_fingerprint("ifrs_bank_ru")
    assert after["sha256"] == before["sha256"], (
        "перестановка ключей — форматирование, а не смысл; отпечаток не должен шуметь")
    assert after["extractable"] == before["extractable"]


def test_unreadable_registry_says_so_instead_of_staying_silent(tmp_path, monkeypatch):
    """Пустое поле читается как «правила не менялись» — то есть как утверждение,
    которого мы не делали. Молчать нельзя."""
    monkeypatch.setattr(versioning, "_REGISTRY_DIR", tmp_path)
    fp = versioning.registry_fingerprint("ifrs_bank_ru")
    assert fp["sha256"] == ""
    assert fp.get("problem"), "неизвестная версия обязана быть названа словами"


# ───────── 5–6. отпечаток полон и снят с того, что реально исполнялось ─────────
#
# Оба дефекта нашлись экспериментом на живом стенде, и оба делали паспорт ЛЖИВЫМ:
#   • подмена `aliases_en` у TOTAL_ASSETS в общем `data/metrics.json` меняла
#     строки, уходящие в промпт, а отпечаток оставался байт-в-байт;
#   • правка реестра «без деплоя» давала станцию, считающую по СТАРЫМ правилам, и
#     паспорт, уверенно называющий НОВЫЙ отпечаток.
# Ниже — ровно эти два эксперимента.

ALIEN_ALIAS = "Grand total of assets (проверочный синоним)"


@pytest.fixture()
def _shelf(tmp_path, monkeypatch):
    """Полка, СОБРАННАЯ из tmp-копии правил, — как в прогоне.

    Полка собирается один раз за жизнь процесса и дальше исполняется как есть;
    диск после этого живёт своей жизнью. Диск паспорта наводим на те же файлы,
    чтобы до правки полка и диск давали ОДИН отпечаток — иначе тест ниже показал
    бы разницу источников, а не разницу правил.
    """
    root = tmp_path / "shelf" / "finengine"
    (root / "data" / "registry").mkdir(parents=True)
    _write_effective_cell(root / "data" / "registry" / "ifrs_bank_ru.json")
    shutil.copy(_REAL_METRICS, root / "data" / "metrics.json")
    monkeypatch.setattr(station_driver, "_shelf_roots", lambda: [("finengine", root)])
    monkeypatch.setattr(versioning, "_REGISTRY_DIR", root / "data" / "registry")
    monkeypatch.setattr(versioning, "_METRICS_PATH", root / "data" / "metrics.json")
    station_driver._build_shelf()
    return root


def _rebuild_shelf() -> None:
    """«Деплой»: полка пересобирается с диска. До него станция считает старой."""
    station_driver._shelf_cache = None
    station_driver._build_shelf()


def test_shared_registry_aliases_are_covered_by_the_fingerprint(_shelf):
    """ЭКСПЕРИМЕНТ: подменить `aliases_en` у TOTAL_ASSETS в общем `metrics.json`.

    С #147 строки ячейки СЛИВАЮТСЯ с синонимами общего реестра — 169 латинских
    синонимов у 78 показателей `ifrs_bank_ru`, и они влияют на узнавание. Пока
    хешировался только файл ячейки, такая правка меняла промпт и не меняла
    отпечаток: паспорт предъявлял одну версию для двух разных наборов правил.
    """
    before = versioning.registry_fingerprint("ifrs_bank_ru")
    assert before["sha256"], "отпечаток обязан считаться"

    metrics = _shelf / "data" / "metrics.json"
    raw = json.loads(metrics.read_text(encoding="utf-8"))
    row = next(r for r in raw["extractable"]["bank"] if r["enum"] == "TOTAL_ASSETS")
    row["aliases_en"] = [ALIEN_ALIAS]
    metrics.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    _rebuild_shelf()

    assert versioning.registry_fingerprint("ifrs_bank_ru")["sha256"] != before["sha256"], (
        "синонимы общего реестра доезжают до модели, а отпечаток их не заметил")


def test_annualize_flag_is_covered_by_the_fingerprint(_shelf):
    """ЭКСПЕРИМЕНТ: перевернуть `annualize` у ROE в файле ячейки.

    ДЫРА, ОТКРЫТАЯ САМОЙ ПОЧИНКОЙ (волна 2). Переход с хеша ФАЙЛА на хеш
    РАЗОБРАННОЙ формы закрыл синонимы и молча потерял `annualize`/`unit_kind`:
    разобранный расчётный показатель — кортеж (enum, name_ru, formula, deps), а
    эти признаки едут отдельным `calculated_meta`, который в дайджест не попал.
    Старая, файловая формула их покрывала.

    Цена ошибки не косметическая: движок читает `annualize` и приводит значение
    к годовому эквиваленту. На промежуточном отчёте ROE меняется ВЧЕТВЕРО, а
    паспорт предъявляет ту же версию правил — ровно та ложь, ради которой
    паспорт и заводился. Таких строк в банковской ячейке девять, включая ROE,
    стоимость риска и NIM.
    """
    before = versioning.registry_fingerprint("ifrs_bank_ru")
    assert before["sha256"], "отпечаток обязан считаться"

    cell = _shelf / "data" / "registry" / "ifrs_bank_ru.json"
    raw = json.loads(cell.read_text(encoding="utf-8"))
    roe = next(m for m in raw["calculated"] if m["enum"] == "ROE")
    roe["annualize"] = not roe.get("annualize", False)
    cell.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    _rebuild_shelf()

    assert versioning.registry_fingerprint("ifrs_bank_ru")["sha256"] != before["sha256"], (
        "приведение к году поменялось, значение поедет вчетверо, "
        "а отпечаток остался прежним")


def test_unit_kind_is_covered_by_the_fingerprint(_shelf):
    """Второй признак из того же `calculated_meta`: единица измерения.

    Проверяется отдельно от `annualize`: они лежат рядом, но покрыть один и
    забыть другой — ровно то, что уже случилось однажды.
    """
    before = versioning.registry_fingerprint("ifrs_bank_ru")
    cell = _shelf / "data" / "registry" / "ifrs_bank_ru.json"
    raw = json.loads(cell.read_text(encoding="utf-8"))
    row = next(m for m in raw["calculated"] if m.get("unit_kind"))
    row["unit_kind"] = "ЗАВЕДОМО ДРУГАЯ ЕДИНИЦА"
    cell.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    _rebuild_shelf()

    assert versioning.registry_fingerprint("ifrs_bank_ru")["sha256"] != before["sha256"], (
        "единица измерения расчётного показателя не покрыта отпечатком")


def _prompt_with_base(base_rows: list) -> str:
    """Промпт извлечения, перехваченный на РЕАЛЬНОМ пути движка.

    Общий реестр подменяется целиком — это ровно то, что видит станция, читая
    `metrics.json` из своей полки (`metric_registry` загружает его при импорте).

    ⚠️ ПЕРЕХВАТЫВАЮТСЯ ОБА ВХОДА В МОДЕЛЬ (#257). Наш движок зовёт `llm.stream`,
    двухстадийная — `llm.complete_with_usage`. С 16.08.2026 боевая схема
    двухстадийная, и перехват только на `stream` означал бы две беды разом:
    промпт не ловится (проверять нечего) И уходит НАСТОЯЩИЙ вызов модели с
    повторами. Проверяемое правило — «синоним общего реестра доезжает до
    модели» — от выбора схемы не зависит и обязано держаться при любой.
    """
    from finengine.llm import openrouter as llm
    from finengine.skills import extract_text as et
    from finengine.skills import metric_registry as mr
    from finengine.skills.metric_schema import registry_for_profile

    seen: list[str] = []

    async def _fake_stream(prompt: str, **_kw):
        seen.append(prompt)
        yield "{}"

    async def _fake_complete_with_usage(prompt="", system=None, **_kw):
        seen.append(f"{system or ''}\n{prompt}")
        return "{}", {"prompt_tokens": 0, "completion_tokens": 0}

    async def _run() -> None:
        async for _ in et.extract_stream_from_text(
            "===PAGE 1===\nОтчёт о финансовом положении\nИтого активы 1 000\n",
            company_type="bank", role="other",
            include_segments=False, include_consolidated_group=False,
            registry_override=registry_for_profile("ifrs_bank_ru"),
        ):
            pass

    orig_stream, orig_base = llm.stream, mr.EXTRACTABLE_BANK
    orig_complete = llm.complete_with_usage
    llm.stream, mr.EXTRACTABLE_BANK = _fake_stream, base_rows
    llm.complete_with_usage = _fake_complete_with_usage
    try:
        asyncio.run(_run())
    finally:
        llm.stream, mr.EXTRACTABLE_BANK = orig_stream, orig_base
        llm.complete_with_usage = orig_complete
    # Склеиваем все промпты: у двухстадийной первый — сжатие контекста, и
    # синоним может уехать во второй.
    return "\n".join(seen)


def test_those_shared_aliases_really_do_reach_the_model():
    """Вторая половина того же эксперимента — почему предыдущий тест обязателен.

    Если бы синонимы общего реестра до модели не доезжали, покрывать их
    отпечатком было бы незачем. Доезжают: перехват промпта на живом пути.
    """
    raw = json.loads(_REAL_METRICS.read_text(encoding="utf-8"))
    rows = raw["extractable"]["bank"]
    next(r for r in rows if r["enum"] == "TOTAL_ASSETS")["aliases_en"] = [ALIEN_ALIAS]
    prompt = _prompt_with_base(rows)
    assert prompt, "движок не дошёл до вызова модели — перехват не сработал"
    assert ALIEN_ALIAS in prompt, "синоним общего реестра не доехал до модели"


# ────────────── 6b. отпечаток умеет снимать САМ ИСПОЛНИТЕЛЬ (#190, К6a) ──────────────
#
# ЧТО ЛОМАЕТСЯ БЕЗ ЭТОГО. Полку собирает бэкенд — чтобы отправить её в станцию. Пока
# прогон запускал наш тул, бэкендский процесс полку держал, и `registry_fingerprint`
# читал именно её. Комнату станции начинает поднимать гейтвей (#190): бэкенд в
# цепочке остаётся только приёмником результата, полку собирать некому — и тот же
# вызов ТИХО съезжает на `source="disk"`, то есть называет отпечаток файлов на диске
# бэкенда. Правка реестра «без деплоя» (#153) их разводит, и паспорт начинает врать
# ровно так, как до #155.
#
# Лечится тем, что отпечаток снимает исполнитель: в комнате на диске лежит
# РАСПАКОВАННАЯ полка — те самые правила, по которым и посчитано.
#
# Тесты этой секции — про обе половины: что в станции отпечаток вообще снимается
# (там нет даже модуля полки), и что ярлык источника не врёт ни в одну сторону.


@pytest.fixture()
def _no_shelf_module(monkeypatch):
    """Станция глазами `versioning`: модуля полки в процессе НЕТ.

    Это не выдумка ради теста, а физика образа. `station_driver` тянет
    `finengine.api.station_progress_ingest`, тот — FastAPI, а в
    `deploy/workstation-aifa/Dockerfile` ставятся httpx, openai, pydantic, pandas,
    numpy, openpyxl, simpleeval, fuzzywuzzy — и всё. До К6a этот ImportError улетал
    в общий `except` вокруг `registry_fingerprint`, и станция получала не отпечаток,
    а запись «правила не прочитаны»: паспорт со стороны исполнителя был невозможен.

    Отсутствие воспроизводится так же, как его видит импорт: атрибут снят с пакета,
    запись в `sys.modules` погашена — `from finengine.skills import station_driver`
    поднимает ImportError, как в образе без FastAPI.
    """
    import finengine.skills as _pkg

    monkeypatch.delattr(_pkg, "station_driver", raising=False)
    monkeypatch.setitem(sys.modules, "finengine.skills.station_driver", None)


def test_station_takes_the_fingerprint_even_without_the_shelf_module(_no_shelf_module):
    """В станции отпечаток обязан СНЯТЬСЯ, а не выродиться в «правила не прочитаны»."""
    fp = versioning.station_fingerprint("ifrs_nf_ru")
    assert fp["sha256"], f"исполнитель не снял отпечаток: {fp.get('problem')!r}"
    assert not fp.get("problem")
    assert fp["cell"] == "ifrs_nf_ru"


def test_the_station_label_is_not_a_lie_about_the_shelf(_no_shelf_module):
    """Ярлык называет ИСПОЛНИТЕЛЯ, а не полку: полки в комнате нет, и говорить
    «shelf» значило бы приписать отпечатку происхождение, которого у него нет."""
    assert versioning.station_fingerprint("ifrs_nf_ru")["source"] == "station"
    assert versioning.registry_fingerprint("ifrs_nf_ru")["source"] == "disk", (
        "базовый вызов перестал называть диск диском — ярлык переставили не там")


def test_station_fingerprint_does_not_relabel_a_real_shelf(_shelf):
    """Обратная сторона: там, где полка ЕСТЬ, ярлык трогать нельзя.

    Иначе достоверный «shelf» подменялся бы на «station» у любого вызывающего,
    и различать два источника снова стало бы нечем.
    """
    assert versioning.station_fingerprint("ifrs_bank_ru")["source"] == "shelf"


def test_unsnapped_fingerprint_is_not_relabelled_as_station(tmp_path, monkeypatch):
    """Неснятый отпечаток не смеет получить ярлык «снят исполнителем».

    Ярлык ставится по факту чтения, а не по факту вызова: пустой sha256 с гордым
    `source="station"` читался бы как «исполнитель отчитался», хотя он не прочёл
    ничего.
    """
    monkeypatch.setattr(versioning, "_REGISTRY_DIR", tmp_path)
    fp = versioning.station_fingerprint("ifrs_bank_ru")
    assert fp["sha256"] == "" and fp.get("problem")
    assert fp["source"] != "station"


def test_station_source_is_not_rendered_as_suspicious():
    """Лист паспорта: «станция» — самый достоверный источник, а не подозрительный.

    До 14.08 в листе было две ветки — полка и «всё остальное, оно же мог
    разойтись». Новый ярлык попал бы во вторую, и паспорт предупреждал бы о
    расхождении ровно там, где отпечаток снял тот, кто и считал.
    """
    rendered = dict(versioning.passport_rows(
        {"registry": {"cell": "ifrs_nf_ru", "sha256": "abc", "source": "station"}}))
    line = rendered.get("Источник правил", "")
    assert "станция" in line, "лист молчит о том, кто снял отпечаток"
    assert "мог разойтись" not in line, (
        "отпечаток исполнителя объявлен сомнительным — предупреждение не о том")


# ───────────────────────── 3. методика: своя и базовая ─────────────────────────


NIM_MINE = {"op": "replace", "id": "bank.formula.nim",
            "text": "NIM = NII / средние работающие активы ЗА КВАРТАЛ.",
            "why": "методика нашего банка"}


def test_personal_methodology_is_visible_in_the_fingerprint(tmp_path, monkeypatch):
    """«По базовой или по своей» — первый вопрос, когда цифры двух аналитиков
    по одному отчёту разошлись."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    personal._turn_deltas.clear()

    plain = versioning.methodology_fingerprint(personal.effective(7))
    assert plain["personal"] is False
    assert plain["effective_sha256"] == plain["base_sha256"]

    personal.save_delta(7, NIM_MINE)
    mine = versioning.methodology_fingerprint(personal.effective(7))
    assert mine["personal"] is True, "личная правка обязана быть видна в паспорте"
    assert mine["effective_sha256"] != mine["base_sha256"]
    assert mine["base_sha256"] == plain["base_sha256"], (
        "база не менялась — её отпечаток не должен ехать за личной правкой")


def test_unsnappable_methodology_reports_the_problem_instead_of_crashing():
    """К моменту сборки паспорта отчёт уже посчитан — ронять его из-за неснятого
    отпечатка нельзя. И `personal` при этом null: «неизвестно» ≠ «базовая»."""
    fp = versioning.methodology_fingerprint(SimpleNamespace())  # не Methodology
    assert fp["personal"] is None
    assert fp.get("problem"), "неснятый отпечаток обязан быть назван словами"


def test_lost_delta_cannot_pass_for_an_applied_one(tmp_path, monkeypatch):
    """Правка с потерянным якорем (мы переписали базу) НЕ применилась — значит
    и «личной» методика от неё не стала. `personal` считается сравнением хешей
    именно поэтому: список правок здесь соврал бы."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    personal._turn_deltas.clear()
    from finengine.store import state
    state.put(state.METHODOLOGY, owner=7, key="overlay", doc={"version": 1, "deltas": [
        {"op": "replace", "id": "bank.formula.nonexistent", "text": "x"}]})
    fp = versioning.methodology_fingerprint(personal.effective(7))
    assert fp["personal"] is False
    assert fp["effective_sha256"] == fp["base_sha256"]


# ───────────────── 4. живой путь: извлечение (Excel + журнал) ─────────────────


class _Index:
    def get_document(self, doc_id):  # noqa: D102, ANN001
        return None


# ────────────────── 4. живой путь: анализ (артефакт + журнал) ──────────────────


DOC_TEXT = ("Консолидированная финансовая отчётность по МСФО за 2024 год. "
            "Отчёт о финансовом положении. Итого активы 250 000 млн руб. ") * 20
# Длина заглушки — не предмет этих тестов, но она обязана быть выше порога
# «есть ли текст» в fe_analyze (80 знаков), иначе тул откажет до паспорта.
REPORT_MD = ("# Финансовый анализ\n\n## Вердикт\n\nУстойчиво: выручка растёт, "
             "долговая нагрузка снижается.\n\n## Выводы\n\n1. Рост опережает инфляцию.\n")


# ───────────── 7. лист паспорта не выдаёт «неизвестно» за «базовую» ─────────────


def test_unsnapped_methodology_is_not_rendered_as_the_base_one():
    """Проверка РЕНДЕРА, а не словаря.

    `methodology_fingerprint` при неснятом отпечатке честно отдаёт
    `personal=None` и текст `problem` — а лист Excel печатал на этом «базовая» и
    `problem` проглатывал. Пользователь читает лист, а не словарь: он увидел бы
    уверенное «посчитано по базовой методике» там, где мы не знаем ничего.
    """
    fp = versioning.methodology_fingerprint(SimpleNamespace())  # не Methodology
    assert fp["personal"] is None and fp.get("problem"), "предпосылка теста ушла"

    rows = versioning.passport_rows({"methodology": fp})
    rendered = dict(rows)
    assert "базовая" not in rendered["Методика базовая/личная"].lower(), (
        "неизвестную методику лист выдаёт за базовую — прямая ложь пользователю")
    assert "НЕИЗВЕСТНО" in rendered["Методика базовая/личная"]
    assert any(fp["problem"] in v for v in rendered.values()), (
        "текст проблемы проглочен — пользователю не с чем идти разбираться")


def test_unreadable_registry_problem_is_rendered_too():
    """То же и у реестра: пустой отпечаток в листе обязан быть назван словами."""
    rows = versioning.passport_rows(
        {"registry": {"cell": "ifrs_bank_ru", "sha256": "", "source": "",
                      "problem": "правила извлечения не прочитаны"}})
    rendered = dict(rows)
    assert rendered["Отпечаток реестра"] == "НЕИЗВЕСТЕН"
    assert "правила извлечения не прочитаны" in rendered["Проблема реестра"]


def test_fingerprint_taken_off_the_disk_says_so_in_the_sheet():
    """Отпечаток с диска мог разойтись с исполнявшимся — лист обязан предупредить.

    Полка — источник по умолчанию, о нём в листе молчим. А вот диск означает,
    что паспорт снят не с того, что уехало в станцию; молчание тут вернуло бы
    ровно ту подмену источника, ради которой всё и переделывалось.
    """
    quiet = dict(versioning.passport_rows(
        {"registry": {"cell": "ifrs_bank_ru", "sha256": "abc", "source": "shelf"}}))
    assert "Источник правил" not in quiet, "обычный случай комментировать незачем"

    loud = dict(versioning.passport_rows(
        {"registry": {"cell": "ifrs_bank_ru", "sha256": "abc", "source": "disk"}}))
    assert "disk" in loud.get("Источник правил", ""), (
        "отпечаток снят не с полки, а лист об этом молчит")


# ────────── 8. паспорт не смеет ронять уже посчитанные извлечение/анализ ──────────
#
# Контракт КОНСТРУКЦИЕЙ, а не дисциплиной: расчёт паспорта однажды уже вынесли из
# best-effort-блока «заодно», и с этого момента любое исключение в отпечатке
# убивало готовый отчёт — после отработавшей станции и потраченного вызова модели.


