"""Личная методика пользователя доезжает до РАЗБОРА — вся цепочка (#123).

ЧТО ЧИНИТСЯ. Пользователь правит методику словами, тул `fe_methodology` кладёт
правку в его личный слой, агент отвечает «записал» — и разбор всё равно шёл по
БАЗОВОЙ методике. Молча: отчёт выглядел удавшимся, просто считан был не по тем
правилам. Оборван был ровно один стык — доставка правок в запечатанную комнату,
— и оба его конца жили: тул писал на диск, `analyze.py --methodology` умел
принять. Не было середины.

ЦЕПОЧКА, КОТОРУЮ ЭТОТ ФАЙЛ ПРОГОНЯЕТ ЗВЕНО ЗА ЗВЕНОМ:

  personal.save_delta / remember_for_turn        (что правил пользователь)
    → station_turn.arm                            (чей это нонс)
      → GET /internal/methodology?nonce=          (право — по нонсу)
        → bridge._service_method                  (у демона есть сеть)
          → analyze_run.sh                        (комната ждёт файл)
            → analyze.py --methodology            (правки применяются)
              → паспорт отчёта                    (по каким правилам считали)

КАЖДОЕ ЗВЕНО ПРОГОНЯЕТСЯ, А НЕ ЧИТАЕТСЯ. Ход взводится настоящим `arm`, роут
зовётся настоящий, демон исполняет свой настоящий обработчик, а обёртка
запускается ПОДПРОЦЕССОМ в поддельной комнате — иначе сторож доказывал бы
наличие текста в файле, а не достижимость кода. Ровно этой болезнью болел
прежний сторож: `test_analysis_path_passes_methodology_to_the_station` искал
подстроку `methodology=None` в исходнике.

ПРОВЕРКА СТОРОЖА ОТКАТОМ (каждая мутация обязана красить):
  • убрать `register_nonce_owner` из `station_turn.arm` → роут отвечает 403;
  • убрать проверку `_owner_of` в роуте → чужой нонс получает чужие правки;
  • убрать кэш `_served` → повторный заказ ТЕМ ЖЕ ходом теряет разовую правку;
  • вернуть `methodology=None` в `_finish_analysis` → паспорт снова врёт;
  • убрать `--methodology` из `analyze_run.sh` → навык запускается без флага;
  • убрать глоб `*.method.json` из петли демона → записка никем не разбирается;
  • `if fpath in _inflight or True` в петле демона → доставка мертва;
  • `if False` вместо проверки срока в `_owner_of` → просроченный нонс живёт;
  • имя файла из записки комнаты вместо `METHOD_NAME` → комната пишет куда хочет;
  • `[ -f ]` вместо `[ -s ]` в обёртке → пустой файл уедет в `analyze.py`;
  • `id` из записки без `_room_rid` → ответ ложится в соседнюю комнату.

⚠️ ЧЕТЫРЕ ИЗ ЭТИХ МУТАЦИЙ ПРОХОДИЛИ ЗЕЛЁНЫМИ 21.08 (18 из 18), и это причина,
по которой раздел «доказываем ДОСТИЖИМОСТЬ» ниже устроен так, как устроен.
Сторож, ищущий подстроку в исходнике, доказывает наличие текста; петлю демона
надо ЗАПУСТИТЬ, срок нонса — ПЕРЕВЕСТИ, файл — сделать ПУСТЫМ.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from finengine.api import (
    internal_methodology as meth,
    station_progress_ingest,
    station_turn,
)
from finengine.config import settings
from finengine.skills.analysis import personal

ROOT = Path(__file__).resolve().parents[1]
_BRIDGE = ROOT / "deploy" / "station-bridge" / "bridge.py"
_WRAPPER = ROOT / "station-skills" / "aifa-analyze" / "scripts" / "analyze_run.sh"

GW_KEY = "agent:aifa-station:openresponses:fe-" + "cd" * 16
NONCE = station_turn.analysis_nonce_of(station_turn.run_nonce_of(GW_KEY))
OWNER = 7
STRANGER = 8

# Настоящий элемент методики: правка на выдуманный id не пережила бы `md.apply`
# в `save_delta`, и тест мерил бы резолвер вместо доставки.
ELEMENT = "bank.formula.nim"
DELTA = {"id": ELEMENT, "op": "replace", "text": "NIM по средним за квартал."}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _wipe()
    yield
    _wipe()


def _wipe() -> None:
    personal._turn_deltas.clear()
    # ⚠️ СБРАСЫВАЕМ ВСЕ ТРИ РЕЕСТРА ХОДА, иначе тесты текут друг в друга: нонс
    # в файле один на всех, а «выдача унесла разовую правку» — признак нонса.
    # Первый же тест с разовой правкой красил бы следующему предупреждением,
    # которого тот не заказывал.
    meth.reset()
    station_progress_ingest._bus.clear()
    station_turn._detached.clear()


def _arm(cell: dict, *, nonce: str = NONCE, owner: int = OWNER) -> None:
    station_turn.arm(nonce=nonce, gw_key=GW_KEY, doc_ids=["doc-a"], owner_id=owner,
                     project_id="p", cell=cell, kind=station_turn.KIND_ANALYZE)


# ── 1. бэкенд: право по нонсу ────────────────────────────────────────────


async def test_the_turns_nonce_brings_back_the_users_own_edits():
    """Главное звено: ход взведён — и правка пользователя выдаётся по его нонсу."""
    personal.save_delta(OWNER, {"id": ELEMENT, "op": "replace",
                                "text": "NIM = NII / средние работающие активы."})
    cell: dict = {}
    _arm(cell)
    try:
        got = await meth.methodology(nonce=NONCE, authorization="")
    finally:
        station_turn.disarm(cell, stopped=True)
    assert [d["id"] for d in got["deltas"]] == [ELEMENT], (
        "правка не выдаётся по нонсу хода — разбор пойдёт по базовой методике")
    assert "средние работающие активы" in got["deltas"][0]["text"]


async def test_a_users_edits_never_travel_on_someone_elses_nonce():
    """Комната недоверенная: uid она назвать не может, и чужого не получит.

    `docs/rules/security.md` §3.1 — владелец ресурса проверяется на роуте.
    """
    personal.save_delta(OWNER, {"id": ELEMENT, "op": "replace", "text": "только моё"})
    mine: dict = {}
    theirs: dict = {}
    other_nonce = "ff" * 16
    _arm(mine)
    _arm(theirs, nonce=other_nonce, owner=STRANGER)
    try:
        got = await meth.methodology(nonce=other_nonce, authorization="")
    finally:
        station_turn.disarm(mine, stopped=True)
        station_turn.disarm(theirs, stopped=True)
    assert got["deltas"] == [], (
        "по нонсу чужого хода уехала методика другого пользователя")


async def test_an_unknown_nonce_gets_nothing_at_all():
    with pytest.raises(HTTPException) as denied:
        await meth.methodology(nonce="nonce-которого-не-было", authorization="")
    assert denied.value.status_code == 403


async def test_an_expired_nonce_stops_being_a_key():
    """Срок нонса — ПОЛЧАСА, и после него ключ мёртв.

    Свойство было, сторожа не было: `if False` вместо проверки срока в
    `_owner_of` проходил зелёным на всех 18 тестах (мутация 21.08). Комната
    живёт минуты, а ключ, живущий вечно, — это ключ, который однажды подберут.
    """
    personal.remember_for_turn(OWNER, dict(DELTA))
    cell: dict = {}
    _arm(cell)
    try:
        meth.expire_nonce(NONCE)  # как будто полчаса прошло
        with pytest.raises(HTTPException) as denied:
            await meth.methodology(nonce=NONCE, authorization="")
        assert denied.value.status_code == 403
    finally:
        station_turn.disarm(cell, stopped=True)
    assert personal.one_shot_pending(OWNER), (
        "просроченный нонс всё же унёс разовую правку")


async def test_the_daemon_token_is_required_when_it_is_set(monkeypatch):
    monkeypatch.setenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", "s3cret")
    cell: dict = {}
    _arm(cell)
    try:
        with pytest.raises(HTTPException) as denied:
            await meth.methodology(nonce=NONCE, authorization="Bearer wrong")
        assert denied.value.status_code == 401
        ok = await meth.methodology(nonce=NONCE, authorization="Bearer s3cret")
        assert ok == {"deltas": []}, "верный токен тоже перестал работать"
    finally:
        station_turn.disarm(cell, stopped=True)


# ── 2. разовая правка: съедается РОВНО одним ходом ───────────────────────


async def test_a_one_shot_edit_survives_a_retry_of_the_same_turn():
    """Обёртка идемпотентна, и её перезапуск не смеет получить методику БЕЗ
    разовой правки: это был бы тихий разбор по другим правилам."""
    personal.remember_for_turn(OWNER, {"id": ELEMENT, "op": "replace",
                                       "text": "в этот раз по средним за квартал"})
    cell: dict = {}
    _arm(cell)
    try:
        first = await meth.methodology(nonce=NONCE, authorization="")
        second = await meth.methodology(nonce=NONCE, authorization="")
    finally:
        station_turn.disarm(cell, stopped=True)
    assert len(first["deltas"]) == 1, "разовая правка не уехала в комнату"
    assert second == first, (
        "повторный заказ тем же ходом получил ДРУГУЮ методику — перезапуск "
        "обёртки посчитал бы отчёт по другим правилам, и молча")


async def test_the_next_turn_no_longer_gets_the_one_shot_edit():
    """«В этот раз» — это один ход, а не «пока не забудется»."""
    personal.remember_for_turn(OWNER, {"id": ELEMENT, "op": "replace",
                                       "text": "только этот разбор"})
    first_cell: dict = {}
    next_cell: dict = {}
    next_nonce = "ab" * 16
    _arm(first_cell)
    _arm(next_cell, nonce=next_nonce)
    try:
        assert (await meth.methodology(nonce=NONCE, authorization=""))["deltas"]
        after = await meth.methodology(nonce=next_nonce, authorization="")
    finally:
        station_turn.disarm(first_cell, stopped=True)
        station_turn.disarm(next_cell, stopped=True)
    assert after["deltas"] == [], "разовая правка пережила свой ход"


async def test_a_turn_that_never_asked_does_not_eat_the_one_shot_edit():
    """Анализ не состоялся — правка обязана дожить до следующего.

    Съедает её ФАКТ ВЫДАЧИ, а не взвод канала: ход, который до станции так и не
    дошёл, не должен уносить правку с собой.
    """
    personal.remember_for_turn(OWNER, {"id": ELEMENT, "op": "replace", "text": "жди меня"})
    cell: dict = {}
    _arm(cell)
    station_turn.disarm(cell, stopped=True)
    assert personal.deltas_for_run(OWNER), (
        "взвод хода съел разовую правку — пользователь её больше не увидит")


# ── 3. демон: записка → файл в комнате ───────────────────────────────────


def _bridge():
    spec = importlib.util.spec_from_file_location("station_bridge_daemon", _BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Answer:
    def __init__(self, body: str) -> None:
        self.text = body
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        return None


class _Backend:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[dict] = []

    async def get(self, url, params=None, headers=None):  # noqa: ANN001
        self.calls.append({"url": url, "params": params or {}})
        return _Answer(self._body)


class _Httpx:
    """Подмена модуля httpx для настоящего прогона петли демона."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def AsyncClient(self, **_kw):  # noqa: ANN001, N802 — имя из httpx
        return self

    async def __aenter__(self) -> _Backend:
        return self._backend

    async def __aexit__(self, *_exc) -> bool:
        return False


def _serve(tmp_path: Path, note: dict, body: str) -> tuple[Path, _Backend]:
    bridge = _bridge()
    bridge.DOC_FETCH_URL = "http://backend:8000/internal/doc-text"
    bridge.METHOD_URL = "http://backend:8000/internal/methodology"
    workspace = tmp_path / "workspace"
    rpc = workspace / ".model-rpc"
    rpc.mkdir(parents=True)
    (rpc / "rid1.method.json").write_text(json.dumps(note), encoding="utf-8")
    backend = _Backend(body)
    asyncio.run(bridge._service_method(str(rpc / "rid1.method.json"), backend))
    return workspace, backend


def test_the_daemon_needs_no_second_env_var(monkeypatch):
    """URL методики ВЫВОДИТСЯ из FE_DOC_FETCH_URL.

    Забытая переменная означала бы молчаливый разбор по базовой методике —
    ровно тот дефект, который эта доставка чинит.
    """
    monkeypatch.setenv("FE_DOC_FETCH_URL", "http://backend:8000/internal/doc-text")
    assert _bridge().METHOD_URL == "http://backend:8000/internal/methodology"


def test_the_daemon_leaves_the_answer_where_the_room_waits(tmp_path):
    body = json.dumps({"deltas": [{"id": ELEMENT, "op": "replace", "text": "моё"}]})
    ws, backend = _serve(tmp_path, {"id": "rid1", "nonce": "n-1"}, body)
    assert json.loads((ws / "methodology.json").read_text(encoding="utf-8"))["deltas"]
    assert backend.calls[0]["params"] == {"nonce": "n-1"}, "нонс до бэкенда не доехал"
    resp = json.loads((ws / ".model-rpc" / "rid1.methodresp.json").read_text(encoding="utf-8"))
    assert resp["ok"] and resp["name"] == "methodology.json"


def test_garbage_never_lands_in_the_room(tmp_path):
    """Скрипт разбирает файл как JSON: мусор превратил бы отказ доставки в
    падение разбора уже после того, как комната поднята."""
    ws, _ = _serve(tmp_path, {"id": "rid1", "nonce": "n-1"}, "<html>502</html>")
    assert not (ws / "methodology.json").exists()
    resp = json.loads((ws / ".model-rpc" / "rid1.methodresp.json").read_text(encoding="utf-8"))
    assert resp.get("error"), "мусор принят за методику"


def test_the_daemon_loop_actually_delivers_the_note(tmp_path):
    """ПЕТЛЯ ЗАПУСКАЕТСЯ ПО-НАСТОЯЩЕМУ, а не читается глазами.

    Прежний сторож искал подстроку `*.method.json` в тексте `main()` — и
    оставался зелёным при `if fpath in _inflight or True`, то есть при мёртвой
    доставке (проверено мутацией 21.08). Здесь петля крутится, записка кладётся
    в поддельную комнату, и зачёт даёт только появившийся файл.
    """
    bridge = _bridge()
    sessions = tmp_path / "ws" / "sessions"
    workspace = sessions / "s1" / "workspace"
    (workspace / ".model-rpc").mkdir(parents=True)
    bridge.SESSIONS_GLOB = str(sessions / "*" / "workspace" / ".model-rpc")
    bridge.METHOD_URL = "http://backend:8000/internal/methodology"
    bridge.PROGRESS_INGEST_URL = ""
    bridge.POLL = 0.01
    bridge.httpx = _Httpx(_Backend(json.dumps({"deltas": [dict(DELTA)]})))

    async def _spin() -> None:
        loop_task = asyncio.create_task(bridge.main())
        (workspace / ".model-rpc" / "rid9.method.json").write_text(
            json.dumps({"id": "rid9", "nonce": "n-1"}), encoding="utf-8")
        for _ in range(400):
            await asyncio.sleep(0.01)
            if (workspace / "methodology.json").exists():
                break
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    asyncio.run(_spin())
    assert (workspace / "methodology.json").exists(), (
        "петля демона не доставила методику — записка осталась лежать")


def test_the_room_cannot_choose_where_the_methodology_lands(tmp_path):
    """Имя файла решает ДЕМОН. Комната — недоверенная сторона.

    Докстрока `_service_method` это обещает; до 21.08 обещание не сторожил
    никто, и `_write_atomic(..., req.get("name") or METHOD_NAME)` проходил
    зелёным на всех 18 тестах.
    """
    body = json.dumps({"deltas": [dict(DELTA)]})
    ws, _ = _serve(tmp_path, {"id": "rid1", "nonce": "n-1", "name": "../ocr.txt"}, body)
    assert (ws / "methodology.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["workspace"], (
        "комната назвала имя файла — и демон написал за пределы рабочей папки")


@pytest.mark.parametrize("kind", ["method", "fetch"])
def test_the_room_cannot_aim_the_answer_at_a_neighbour(tmp_path, kind):
    """`rid` из записки становится ИМЕНЕМ ФАЙЛА ответа, а комнаты лежат рядом.

    Тело методики в соседнюю комнату не течёт (ответ несёт только id, ok и
    размер), но писать в чужой каталог по указанию комнаты нельзя и таким.
    Оба обработчика вместе: правило на одном из двух — это правило, которого нет.
    """
    bridge = _bridge()
    bridge.DOC_FETCH_URL = "http://backend:8000/internal/doc-text"
    bridge.METHOD_URL = "http://backend:8000/internal/methodology"
    rpc = tmp_path / "workspace" / ".model-rpc"
    rpc.mkdir(parents=True)
    note = rpc / f"rid1.{kind}.json"
    note.write_text(json.dumps({"id": "../pwn", "nonce": "n-1", "doc_id": "d1"}),
                    encoding="utf-8")
    handler = bridge._service_method if kind == "method" else bridge._service_fetch
    asyncio.run(handler(str(note), _Backend(json.dumps({"deltas": []}))))
    assert not (tmp_path / "workspace" / "pwn.methodresp.json").exists()
    assert not (tmp_path / "workspace" / "pwn.fetchresp.json").exists()
    assert list(rpc.glob(f"rid1.{kind}resp.json")), (
        "ответ не лёг в свою комнату — фолбэк на имя записки не сработал")


# ── 4. комната: обёртка заказывает и передаёт флаг ───────────────────────


def _room(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Поддельная комната: полка с обёрткой и подставным лаунчером навыка."""
    shelf = tmp_path / "shelf" / "aifa-analyze"
    (shelf / "scripts").mkdir(parents=True)
    wrapper = shelf / "scripts" / "analyze_run.sh"
    wrapper.write_text(_WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    argv = tmp_path / "argv.json"
    # Путь — через json.dumps (двойные кавычки): repr дал бы одинарные, а тело
    # `python3 -c '…'` само в одинарных, и скрипт развалился бы на кавычке.
    (shelf / "run.sh").write_text(
        "#!/bin/sh\npython3 -c 'import json,sys; open("
        + json.dumps(str(argv)) + ',"w").write(json.dumps(sys.argv[1:]))\' "$@"\n',
        encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return wrapper, workspace, argv


def _run_wrapper(wrapper: Path, workspace: Path, *, nonce: str | None = "n-1",
                 timeout: float = 60) -> subprocess.Popen:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if nonce:
        env["FE_PROGRESS_NONCE"] = nonce
    return subprocess.Popen(
        ["bash", str(wrapper), "--text", "./ocr.txt", "--metrics", "./metrics.json"],
        cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for_note(workspace: Path, timeout: float = 20.0) -> dict:
    rpc = workspace / ".model-rpc"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notes = list(rpc.glob("*.method.json")) if rpc.exists() else []
        if notes:
            return json.loads(notes[0].read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError("записка о методике не появилась — обёртка её не заказывает")


def test_the_wrapper_orders_the_methodology_and_hands_it_to_the_skill(tmp_path):
    """Полный оборот в комнате: записка → файл → флаг у навыка."""
    wrapper, workspace, argv = _room(tmp_path)
    proc = _run_wrapper(wrapper, workspace)
    try:
        note = _wait_for_note(workspace)
        assert note["nonce"] == "n-1", "обёртка не назвала нонс — бэкенд ответит 403"
        assert "doc_id" not in note and "user_id" not in note, (
            "комната называет что-то кроме нонса — это её нельзя дать называть")
        (workspace / "methodology.json").write_text(
            json.dumps({"deltas": [{"id": ELEMENT, "op": "replace", "text": "моё"}]}),
            encoding="utf-8")
        (workspace / ".model-rpc" / f"{note['id']}.methodresp.json").write_text(
            json.dumps({"id": note["id"], "ok": True}), encoding="utf-8")
        assert proc.wait(timeout=60) == 0, proc.stderr.read()
    finally:
        proc.kill()
    passed = json.loads(argv.read_text(encoding="utf-8"))
    assert "--methodology" in passed, (
        "методика доехала в комнату, а навык её не получил — флаг не добавлен")
    assert passed[passed.index("--methodology") + 1] == "./methodology.json"
    assert passed[:4] == ["--text", "./ocr.txt", "--metrics", "./metrics.json"], (
        "обёртка потеряла аргументы разбора")


def test_without_a_nonce_the_analysis_still_runs_on_the_base(tmp_path):
    """Нонса нет — доставка невозможна, но разбор обязан пойти по базе.

    Уронить готовый к работе разбор из-за необязательной доставки было бы хуже
    дефекта, который она чинит.
    """
    wrapper, workspace, argv = _room(tmp_path)
    proc = _run_wrapper(wrapper, workspace, nonce=None)
    assert proc.wait(timeout=60) == 0
    assert "--methodology" not in json.loads(argv.read_text(encoding="utf-8"))
    assert not (workspace / ".model-rpc").exists(), "записка без нонса бессмысленна"


def test_a_refused_delivery_does_not_kill_the_analysis(tmp_path):
    """Демон ответил отказом — разбор идёт по базовой, а не падает."""
    wrapper, workspace, argv = _room(tmp_path)
    proc = _run_wrapper(wrapper, workspace)
    try:
        note = _wait_for_note(workspace)
        (workspace / ".model-rpc" / f"{note['id']}.methodresp.json").write_text(
            json.dumps({"id": note["id"], "error": "403 unknown nonce"}), encoding="utf-8")
        assert proc.wait(timeout=60) == 0
        assert "БАЗОВОЙ" in proc.stderr.read(), "отказ доставки прошёл молча"
    finally:
        proc.kill()
    assert "--methodology" not in json.loads(argv.read_text(encoding="utf-8"))


def test_an_empty_methodology_file_never_reaches_the_skill(tmp_path):
    """Пустой файл — не доставка, а половина её.

    `analyze.py` разбирает файл `json.loads`, и на пустом падает — уже после
    того, как комната поднята и текст в неё привезён. `[ -f ]` вместо `[ -s ]`
    в обёртке проходил зелёным на всех 18 тестах (мутация 21.08).
    """
    wrapper, workspace, argv = _room(tmp_path)
    (workspace / "methodology.json").write_text("", encoding="utf-8")
    proc = _run_wrapper(wrapper, workspace)
    try:
        note = _wait_for_note(workspace)
        (workspace / ".model-rpc" / f"{note['id']}.methodresp.json").write_text(
            json.dumps({"id": note["id"], "error": "нет правок"}), encoding="utf-8")
        assert proc.wait(timeout=60) == 0, proc.stderr.read()
    finally:
        proc.kill()
    assert "--methodology" not in json.loads(argv.read_text(encoding="utf-8")), (
        "пустой methodology.json уехал в навык — разбор упадёт на json.loads")


def test_the_station_names_the_field_the_backend_reads():
    """СТЫК ДВУХ МЕСТ: станция ставит отпечаток, бэкенд его сравнивает.

    ⚠️ ЧТО ЭТОТ СТОРОЖ ДОКАЗЫВАЕТ, А ЧТО НЕТ — сказано прямо, потому что
    сторожа-на-подстроке в этом файле уже подводили. Он НЕ прогоняет `analyze.py`
    (для этого нужна комната станции) и достижимости не доказывает. Он ловит
    ровно одно: переименование поля на ОДНОМ из двух концов, после которого
    паспорт молча начнёт называть базовой каждую личную методику.

    ⚠️ Срез 13.5.4: подтверждение больше НЕ отдельное событие `plan_step` с
    `methodology_changes` (канонический движок его не эмитит вовсе — новый
    словарь событий это `node_run`/`typed_result`, не старый построчный).
    Станция ставит отпечаток `methodology_version` НА КАЖДЫЙ typed-результат
    (`OutputRunMetadata`, `contracts/results.py:66`, уже существующее поле,
    не изобретённое для этого среза); бэкенд сравнивает его с отпечатком
    базовой методики (`station_turn._methodology_personal`). Приём признака
    бэкендом прогоняется настоящим событием — см.
    `_finish_with(personal=True)`.
    """
    station = (ROOT / "station-skills" / "aifa-analyze" / "scripts"
               / "analyze.py").read_text(encoding="utf-8")
    backend = (ROOT / "src" / "finengine" / "api" / "station_turn.py").read_text(encoding="utf-8")
    assert "methodology_version" in station, (
        "станция перестала ставить отпечаток применённой методики")
    assert 'run_metadata.get("methodology_version")' in backend, (
        "бэкенд больше не читает отпечаток методики из typed-результата"
    )


def test_the_skill_body_launches_the_wrapper_not_the_bare_launcher():
    """Команда в теле навыка — единственное, чем обёртка попадает на боевой путь.

    Останется в `SKILL.md` голый `run.sh`, и вся цепочка выше окажется
    недостижимой: разбор пойдёт по базовой методике при зелёных тестах.
    """
    body = (ROOT / "station-skills" / "aifa-analyze" / "SKILL.md").read_text(encoding="utf-8")
    assert "aifa-analyze/scripts/analyze_run.sh" in body
    assert "aifa-analyze/run.sh --text" not in body, "модель зовёт лаунчер мимо доставки"


# ── 5. паспорт называет методику, по которой считали ─────────────────────



def _report_output(methodology_version: str) -> dict:
    return {
        "schema_version": "1.0", "output_type": "ReportOutput", "output_id": "ro-1",
        "run_id": "run-1", "status": "complete",
        "sections": [{"section_id": "s1", "title": "Вывод", "status": "supported",
                     "theses": [{"text": "текст"}]}],
        "gaps": [], "warnings": [],
        "publication_checks": {"grounding": "pass", "citation": "pass"},
        "run_metadata": {"run_id": "run-1", "methodology_version": methodology_version},
    }


def _canonical_analysis_done(methodology_version: str) -> dict:
    return {"type": "done", "result": {
        "schema_version": "1.0", "case_id": "case-1", "document_id": "doc-a",
        "document_version": "sha256:abc",
        "run_passport": {
            "run_id": "run-1", "goal_ir_id": "goal-1",
            "classification": {"profile_id": "ifrs_bank_ru", "standard": "ifrs",
                               "company_type": "bank", "language": "ru"},
            "entity": {"entity_id": "e-1", "canonical_name": "Банк", "raw_name": "Банк"},
            "registry_version": "v1", "methodology_version": methodology_version,
            "model_version": "m1", "prompt_version": "p1", "sandbox_image_version": "s1",
        },
        "report_output": _report_output(methodology_version),
    }}


def _version_for(served: list[dict] | None, *, personal: bool) -> str:
    """Тот же расчёт, что делает `analyze.py`, чтобы тест не выдумывал строку."""
    from finengine.skills import versioning as ver
    from finengine.skills.analysis import methodology as md

    if not personal or not served:
        return ver.methodology_fingerprint(None)["effective_sha256"]
    applied = md.apply(served, strict=False)
    return ver.methodology_fingerprint(applied)["effective_sha256"]


async def _finish_with(monkeypatch, served: list[dict] | None, *,
                       personal: bool = True, ask_route: bool = False,
                       fail: str = "", nonce: str = NONCE) -> dict:
    """Прогнать настоящий терминал анализа: что вышло в канон и что видит канал.

    `personal` — какую версию методики несёт `run_metadata.methodology_version`
    canonical typed-результата: отпечаток ПРИМЕНЁННЫХ `served`-дельт (станция
    реально применила личную методику) или отпечаток базовой (станция её не
    применила — «выдача не доставка», воспроизведено проверкой 21.08).
    `ask_route` — выдачу делает не подстановка в `_served`, а настоящий роут
    (тогда разовая правка реально расходуется). `fail` — вместо готового
    разбора приходит отказ станции.
    """
    seen: dict = {"said": []}

    def _watch(run_id, event):  # noqa: ANN001
        if event.get("type") == "text_delta":
            seen["said"].append(str(event.get("text") or ""))
        return True

    monkeypatch.setattr("finengine.skills.run_orchestrator.emit_to_run", _watch)
    cell: dict = {"run_id": "run-1"}
    _arm(cell)
    channel = station_turn.channels_of(cell)[station_turn.KIND_ANALYZE]
    seen["channel"] = channel
    if ask_route:
        await meth.methodology(nonce=nonce, authorization="")
    elif served is not None:
        meth.set_served(nonce, served)
    if fail:
        channel.queue.put_nowait({"type": "error", "message": fail})
    else:
        version = _version_for(served, personal=personal)
        channel.queue.put_nowait(_canonical_analysis_done(version))
    for _ in range(200):
        await asyncio.sleep(0)
        if channel.task is not None and channel.task.done():
            break
    station_turn.disarm(cell, stopped=True)
    return seen


async def test_the_passport_names_the_methodology_that_actually_travelled(monkeypatch):
    """Личная методика реально применилась — канал знает это, молчания нет.

    `channel.methodology_applied` было прибито `None` (не встроено вовсе) —
    паспорт врал на КАЖДОМ прогоне. Проверка идёт по `OutputRunMetadata.
    methodology_version` (сравнение с отпечатком базовой, `station_turn.
    _methodology_personal`), а не по списку правок: станционный слушатель
    видит только ИТОГ прогона (typed-результат), а не тело методики — список
    элементов остаётся заботой `analyze.py`/`methodology.apply` (проверено
    `test_fe_methodology_tool.py::test_station_script_accepts_and_applies_deltas`).
    """
    out = await _finish_with(monkeypatch, [DELTA], personal=True)
    assert out["channel"].methodology_applied == 1, (
        "паспорт снова не знает, что считали по личной методике")
    assert not any("не доехали" in s for s in out["said"]), (
        "методика реально применилась, а пользователю сказано обратное")


@pytest.mark.parametrize("served", [None, []], ids=["не заказывали", "правок нет"])
async def test_the_passport_says_base_when_nothing_travelled(monkeypatch, served):
    """Методика БАЗОВАЯ — и так и сказано, в обоих случаях.

    `None` — старая полка или модель мимо обёртки; `[]` — у пользователя просто
    нет ни одной правки. Второй случай — норма, и ломаться на нём нечему.
    """
    out = await _finish_with(monkeypatch, served, personal=False)
    assert out["channel"].methodology_applied is None
    assert not any("методик" in s for s in out["said"]), (
        "разбор по базовой без единой правки — говорить о методике не о чем")


async def test_the_passport_says_base_when_the_room_never_confirmed(monkeypatch):
    """ГЛАВНОЕ ЗДЕСЬ: выдача — не доставка (воспроизведено проверкой 21.08).

    Правки выданы роутом (`served` полон), а typed-результат несёт отпечаток
    БАЗОВОЙ методики — станция её не применила. Паспорт по одной выдаче назвал
    бы отчёт личным, и молча; здесь он обязан сказать «не доехали».
    """
    out = await _finish_with(monkeypatch, [DELTA], personal=False)
    assert out["channel"].methodology_applied is None, (
        "станция не подтвердила применение, а канал назвал методику личной")
    assert any("не доехали" in s for s in out["said"]), (
        "правки пропали, а пользователю об этом не сказано ни слова")


async def test_the_passport_finds_the_edits_served_on_the_turns_other_nonce(monkeypatch):
    """Комната назвала нонс ИЗВЛЕЧЕНИЯ того же хода — паспорт обязан их найти.

    Владелец регистрируется на КАЖДОМ виде канала намеренно, значит роут по
    такому нонсу правки отдаст. Ищи паспорт только по своему — и он скажет
    «базовая» о прогоне, который посчитан по личной методике.
    """
    extract_nonce = station_turn.run_nonce_of(GW_KEY)
    other: dict = {}
    _arm(other, nonce=extract_nonce)  # тот же ход, вид «извлечение»
    try:
        out = await _finish_with(monkeypatch, [DELTA], nonce=extract_nonce, personal=True)
    finally:
        station_turn.disarm(other, stopped=True)
    assert out["channel"].methodology_applied == 1, (
        "правки уехали на нонсе извлечения — паспорт их не нашёл")


async def test_the_passport_never_borrows_a_strangers_edits(monkeypatch):
    """Нонсы выводятся из ключа хода, а вывод — не доказательство владения."""
    extract_nonce = station_turn.run_nonce_of(GW_KEY)
    stranger: dict = {}
    _arm(stranger, nonce=extract_nonce, owner=STRANGER)
    try:
        out = await _finish_with(monkeypatch, [DELTA], nonce=extract_nonce, personal=True)
    finally:
        station_turn.disarm(stranger, stopped=True)
    assert out["channel"].methodology_applied is None, (
        "в паспорт уехали правки чужого пользователя")
    assert not any("не доехали" in s for s in out["said"]), (
        "чужие правки, о пропаже которых нам сообщать не за что, всё равно "
        "пожаловались пользователю")


async def test_a_spent_one_shot_edit_is_never_lost_in_silence(monkeypatch):
    """Ход сходил за методикой и упал до разбора — правки нет ни у кого.

    Семантику («терять» против «применить дважды») решает владелец, вопрос
    открыт. Что НЕ обсуждается — молчание: пользователь обязан узнать, что его
    «в этот раз» израсходовано впустую.
    """
    personal.remember_for_turn(OWNER, dict(DELTA))
    out = await _finish_with(monkeypatch, None, ask_route=True,
                             fail="станция не ответила")
    assert personal.deltas_for_run(OWNER) == [], "разовая правка не расходовалась — тест не о том"
    said = " ".join(out["said"])
    assert "израсходована" in said, "разовая правка потеряна МОЛЧА"
    assert "не доехали" in said
