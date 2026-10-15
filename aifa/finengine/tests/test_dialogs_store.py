"""Диалоги в базе: строка на диалог, а не общий файл.

ДЕФЕКТ. Вся переписка пользователя лежала в одном json-файле, который
переписывался ЦЕЛИКОМ на каждое сохранение. Файл дорос до 2,27 МБ, рядом с ним
на стенде лежат `.bak-docfix`, `.bak-recover`, `.bak-sovcom` — следы уже
случившихся потерь. Пока единица записи «все диалоги сразу», один сбой уносит
всё; поэтому диалог обязан стать отдельной строкой.

ПУТЬ ТЕСТА — ПОЛЬЗОВАТЕЛЬСКИЙ. Всё, что здесь проверяется, идёт через реальные
ручки GET/PUT/DELETE /dialogs приложения FastAPI с реальной подстановкой
пользователя. Хранилище напрямую не дёргается: главный урок репозитория
(дефект #137) — тест, ходивший в обход движка, был зелёным при сломанном
проде. В базу тест заглядывает только КАК НАБЛЮДАТЕЛЬ (счётчик записей строки
`rev`), чтобы доказать, что соседнюю строку не писали.

ЧЕМ ЭТОТ ТЕСТ НЕ ЯВЛЯЕТСЯ. Постгреса на dev-VPS сейчас нет, поднять его в
прогоне негде, поэтому SQL-хранилище исполняется здесь на временной файловой
базе через тот же SQLAlchemy-код, что пойдёт в прод. Это проверяет НАШУ
логику — адресность записи, изоляцию владельца, транзакцию, идемпотентность
переноса — но НЕ проверяет: поведение самого Postgres (JSONB, типы, блокировки),
одновременную запись из НЕСКОЛЬКИХ ПРОЦЕССОВ (бэкенд сейчас однопроцессный,
uvicorn --workers 1), сетевые обрывы до базы и права доступа. Первый запуск на
живом Postgres обязан быть отдельным смоуком.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Приложение с диалогами в базе + подставленный вошедший пользователь.

    `who` — изменяемый словарь: тест меняет id прямо посреди сценария и тем
    самым «перелогинивается», не пересобирая клиент.
    """
    from finengine.api import main as m  # noqa: PLC0415
    from finengine.auth.db import User  # noqa: PLC0415
    from finengine.auth.dependencies import get_current_user  # noqa: PLC0415
    from finengine.config import settings  # noqa: PLC0415

    db_path = tmp_path / "dialogs.db"
    monkeypatch.setattr(settings, "dialogs_database_url", f"sqlite+pysqlite:///{db_path}")
    # Серверный tripwire дописывает строку в журнал client_events — уводим его
    # в tmp, чтобы тест не писал в рабочие данные. Журнал переехал в общее
    # хранилище (26.08), поэтому подменяем каталог данных, а не docs_dir.
    from finengine.store import journals as _journals  # noqa: PLC0415

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "journals_database_url", "")
    _journals._cached = None

    who = {"id": 1, "username": "u1"}
    m.app.dependency_overrides[get_current_user] = lambda: User(
        id=who["id"], username=who["username"]
    )
    try:
        yield TestClient(m.app), who, db_path
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)


def _chat(pid: str, *, messages: list[dict] | None = None, name: str = "чат", updated_at: int = 1000) -> dict:
    return {
        "id": pid,
        "name": name,
        "created_at": 1,
        "updated_at": updated_at,
        "doc_ids": [],
        "messages": messages if messages is not None else [
            {"id": f"{pid}-m1", "role": "user", "created_at": 1, "content": "привет"}
        ],
    }


def _rows(db_path) -> dict[str, dict]:
    """Строки таблицы диалогов глазами наблюдателя: id → (rev, payload)."""
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as conn:
        return {
            r.project_id: {"rev": r.rev, "payload": r.payload, "user_id": r.user_id}
            for r in conn.execute(text("SELECT user_id, project_id, rev, payload FROM dialog"))
        }


def test_saving_one_dialog_does_not_rewrite_the_others(api):
    """СУТЬ ПЕРЕЕЗДА: пуш одного чата не трогает строки остальных.

    Раньше сохранение любого чата переписывало файл со ВСЕЙ перепиской — этим
    и опасно было каждое сохранение. Здесь сосед должен остаться нетронутым
    физически: тот же rev, тот же payload.
    """
    client, _who, db = api
    client.put("/dialogs", json={"projects": [_chat("a"), _chat("b")], "last_project_id": "a"})
    before = _rows(db)

    client.put(
        "/dialogs",
        json={
            "projects": [_chat("a", updated_at=2000, messages=[
                {"id": "a-m1", "role": "user", "created_at": 1, "content": "привет"},
                {"id": "a-m2", "role": "assistant", "created_at": 2, "content": "ответ"},
            ])],
            "last_project_id": "a",
        },
    )
    after = _rows(db)

    assert after["a"]["rev"] > before["a"]["rev"], "изменённый чат обязан быть записан"
    assert after["b"] == before["b"], "соседнюю строку не писали вовсе"
    assert {p["id"] for p in client.get("/dialogs").json()["projects"]} == {"a", "b"}


def _push_together(client, payloads: list[dict]) -> None:
    """Отправить пуши из разных потоков одновременно и дождаться обоих."""
    start = threading.Barrier(len(payloads))
    errors: list[BaseException] = []

    def push(body: dict):
        try:
            start.wait(timeout=5)
            client.put("/dialogs", json=body)
        except BaseException as exc:  # noqa: BLE001 — падение потока должно всплыть наружу
            errors.append(exc)

    threads = [threading.Thread(target=push, args=(b,)) for b in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors, errors


def test_two_dialogs_written_at_once_lose_neither(api):
    """Две вкладки пушат РАЗНЫЕ чаты одновременно — ни один не пропадает."""
    client, _who, _db = api
    client.put("/dialogs", json={"projects": [_chat("a"), _chat("b")], "last_project_id": "a"})

    _push_together(client, [
        {
            "projects": [_chat(pid, updated_at=3000, messages=[
                {"id": f"{pid}-m1", "role": "user", "created_at": 1, "content": "привет"},
                {"id": f"{pid}-m2", "role": "user", "created_at": 2, "content": f"правка {pid}"},
            ])],
            "last_project_id": pid,
        }
        for pid in ("a", "b")
    ])

    saved = {p["id"]: p for p in client.get("/dialogs").json()["projects"]}
    assert set(saved) == {"a", "b"}
    for pid in ("a", "b"):
        assert [m["content"] for m in saved[pid]["messages"]][-1] == f"правка {pid}"


def test_two_writers_in_one_dialog_lose_no_message(api, monkeypatch):
    """Две вкладки дописывают ОДИН чат одновременно — оба сообщения выживают.

    Это тот случай, где «прочитал — слил — записал» действительно теряет
    данные: оба писателя видят одно и то же состояние, и второй затирает
    сообщение первого. Естественное окно между чтением и записью — доли
    миллисекунды, поэтому тест расширяет его задержкой в чтении: без такой
    подпорки гонка почти никогда не воспроизводится, и тест был бы зелёным
    просто по везению (проверено откатом — см. отчёт).
    """
    from finengine.store.dialogs import get_dialog_store  # noqa: PLC0415

    client, _who, _db = api
    client.put("/dialogs", json={"projects": [_chat("a")], "last_project_id": "a"})

    store = get_dialog_store()
    real_load = store.load

    def slow_load(user_id: int) -> dict:
        state = real_load(user_id)
        time.sleep(0.15)  # окно, в которое второй писатель успевает прочитать то же самое
        return state

    monkeypatch.setattr(store, "load", slow_load)
    _push_together(client, [
        {
            "projects": [_chat("a", updated_at=2000 + i, messages=[
                {"id": "a-m1", "role": "user", "created_at": 1, "content": "привет"},
                {"id": f"a-new-{i}", "role": "user", "created_at": 2 + i, "content": f"вкладка {i}"},
            ])],
            "last_project_id": "a",
        }
        for i in (1, 2)
    ])

    texts = [m["content"] for m in client.get("/dialogs").json()["projects"][0]["messages"]]
    assert "вкладка 1" in texts and "вкладка 2" in texts


@pytest.mark.parametrize("order", ["сначала заглушка", "сначала полная"])
def test_extraction_card_never_gets_poorer_on_the_db_path(api, order):
    """Merge-safe жив и на базе: слияние дополняет карточку, но не обедняет.

    Дефект #143. Карточка извлечения уезжает на сервер В МОМЕНТ СТАРТА прогона
    (`metrics: []`, `status: running`), а дозаполненная приходит позже. Порядок
    прихода — деталь сети, а не политика сохранности, поэтому проверяются ОБА
    порядка: и «заглушка, потом полная» (живой сценарий прогона), и «полная,
    потом заглушка» (вкладка со вчерашним снимком). В обоих должно остаться 75
    строк, иначе зона «Документ» снова покажет пустоту при готовой таблице.
    """
    client, _who, _db = api
    full = {
        "id": "extract-1", "role": "extraction", "created_at": 1000,
        "status": "done", "metrics": [{"enum": f"M_{i}"} for i in range(75)],
    }
    stub = {"id": "extract-1", "role": "extraction", "created_at": 1000, "status": "running", "metrics": []}
    first, second = (stub, full) if order == "сначала заглушка" else (full, stub)
    client.put("/dialogs", json={"projects": [_chat("a", messages=[first])], "last_project_id": "a"})
    client.put("/dialogs", json={"projects": [_chat("a", messages=[second])], "last_project_id": "a"})

    card = client.get("/dialogs").json()["projects"][0]["messages"][0]
    assert len(card["metrics"]) == 75
    assert card["status"] == "done"


def test_push_that_forgot_a_dialog_restores_it_and_trips_the_wire(api, tmp_path):
    """Пуш без чата, который сервер знает, — не удаление, а улика.

    Удалять сервер имеет право только по явному списку надгробий. Неполный пуш
    (вкладка со старым бандлом) обязан быть восстановлен и записан в журнал —
    иначе такие пропажи снова придётся ловить по .bak-файлам.
    """
    client, _who, _db = api
    client.put("/dialogs", json={"projects": [_chat("a"), _chat("b")], "last_project_id": "a"})

    res = client.put("/dialogs", json={"projects": [_chat("a")], "last_project_id": "a"}).json()
    assert res["restored_for_pusher"] == 1
    assert res["projects_total"] == 2
    assert {p["id"] for p in client.get("/dialogs").json()["projects"]} == {"a", "b"}

    from finengine.skills.run_logger import ЖУРНАЛ_КЛИЕНТА, прочитать

    виды = [з.get("kind") for з in прочитать(ЖУРНАЛ_КЛИЕНТА)]
    assert "dialogs_push_partial" in виды, f"в журнале клиента только {виды}"


def test_explicit_tombstone_removes_the_row(api):
    """Явное удаление всё-таки удаляет — иначе «неубиваемость» стала бы багом."""
    client, _who, db = api
    client.put("/dialogs", json={"projects": [_chat("a"), _chat("b")], "last_project_id": "a"})
    client.put(
        "/dialogs",
        json={"projects": [_chat("a")], "last_project_id": "a", "deleted_project_ids": ["b"]},
    )
    assert set(_rows(db)) == {"a"}
    assert {p["id"] for p in client.get("/dialogs").json()["projects"]} == {"a"}


def test_tombstone_of_one_user_does_not_delete_the_same_id_of_another(api):
    """IDOR на УДАЛЕНИИ: моё надгробие не сносит одноимённый чат соседа.

    Здесь единственное место, где сервер удаляет строку по списку, ПРИШЕДШЕМУ
    ОТ КЛИЕНТА (`deleted_project_ids`), — то есть ровно тот путь, где живёт
    IDOR. Соседний тест про owner-scoping проверяет чтение, запись и DELETE
    /dialogs (delete_all), но удаление по надгробию не покрывал ничем: скоуп
    можно было снять с `delete(DIALOG)` в `SqlDialogStore.save`, и все тесты
    оставались зелёными. Идентификаторы чатов клиент генерирует сам, совпадение
    у двух пользователей — вопрос времени, а не злого умысла.
    """
    client, who, _db = api
    client.put("/dialogs", json={"projects": [_chat("shared", name="мой")], "last_project_id": "shared"})

    who["id"], who["username"] = 2, "u2"
    client.put("/dialogs", json={"projects": [_chat("shared", name="чужой")], "last_project_id": "shared"})
    client.put("/dialogs", json={"projects": [], "deleted_project_ids": ["shared"]})
    assert client.get("/dialogs").json()["projects"] == [], "своё удаление обязано сработать"

    who["id"], who["username"] = 1, "u1"
    assert [p["name"] for p in client.get("/dialogs").json()["projects"]] == ["мой"]


def test_another_users_dialog_is_neither_visible_nor_overwritable(api):
    """Диалоги owner-scoped: чужой чат не виден, не перезаписывается, не стирается.

    «Не стирается» здесь — про DELETE /dialogs (сброс всей истории). Удаление
    по надгробию — отдельный путь и отдельный сторож выше.
    """
    client, who, _db = api
    client.put("/dialogs", json={"projects": [_chat("shared", name="мой")], "last_project_id": "shared"})

    who["id"], who["username"] = 2, "u2"
    assert client.get("/dialogs").json()["projects"] == []
    # Тот же id проекта у другого пользователя — своя строка, не чужая.
    client.put("/dialogs", json={"projects": [_chat("shared", name="чужой")], "last_project_id": "shared"})
    client.delete("/dialogs")

    who["id"], who["username"] = 1, "u1"
    mine = client.get("/dialogs").json()["projects"]
    assert [p["name"] for p in mine] == ["мой"]


def test_without_database_url_the_endpoints_still_write_files(tmp_path, monkeypatch):
    """ПЕРЕКЛЮЧАТЕЛЬ: база не настроена → работает файловое хранилище, как раньше.

    Дефолт ничего не переключает: момент переезда живых пользователей выбирает
    владелец.
    """
    from finengine.api import main as m  # noqa: PLC0415
    from finengine.auth.db import User  # noqa: PLC0415
    from finengine.auth.dependencies import get_current_user  # noqa: PLC0415
    from finengine.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "dialogs_database_url", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    m.app.dependency_overrides[get_current_user] = lambda: User(id=7, username="u7")
    try:
        client = TestClient(m.app)
        client.put("/dialogs", json={"projects": [_chat("a")], "last_project_id": "a"})
        blob = json.loads((tmp_path / "dialogs" / "7.json").read_text(encoding="utf-8"))
        assert [p["id"] for p in blob["projects"]] == ["a"]
        assert [p["id"] for p in client.get("/dialogs").json()["projects"]] == ["a"]
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)


def test_migration_is_idempotent_and_reports_counts(api, tmp_path, capsys):
    """Перенос файлов в базу: считает, не дублирует, файлы не удаляет.

    Идёт тем же путём, что и человек: через CLI `python -m
    finengine.store.migrate_dialogs`, включая сухой прогон. Второй запуск
    неизбежен (посчитали, перенесли, потом «а точно всё доехало?») и обязан не
    трогать уже перенесённое — иначе повторный перенос откатит пользователя,
    который уже успел поработать на базе, во вчерашний день.
    """
    from finengine.store.migrate_dialogs import main  # noqa: PLC0415

    client, who, db = api
    source = tmp_path / "dialogs_files"
    source.mkdir()
    (source / "1.json").write_text(
        json.dumps({"projects": [_chat("a"), _chat("b")], "folders": [], "last_project_id": "a"}),
        encoding="utf-8",
    )
    (source / "2.json").write_text(
        json.dumps({"projects": [_chat("c")], "folders": [], "last_project_id": "c"}),
        encoding="utf-8",
    )
    # Ручная копия рядом — след прошлой потери. В перенос попадать не должна.
    (source / "1.json.bak-recover").write_text(json.dumps({"projects": [_chat("z")]}), encoding="utf-8")

    assert main(["--source", str(source), "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "СУХОЙ ПРОГОН" in dry and "диалогов в файлах:      3" in dry
    assert _rows(db) == {}, "сухой прогон не имеет права писать"

    assert main(["--source", str(source)]) == 0
    first = capsys.readouterr().out
    assert "перенесено диалогов:    3" in first
    assert "уже было в базе:        0" in first

    assert main(["--source", str(source)]) == 0
    second = capsys.readouterr().out
    assert "перенесено диалогов:    0" in second
    assert "уже было в базе:        3" in second

    assert (source / "1.json").exists() and (source / "2.json").exists()
    assert "z" not in _rows(db)

    # Пользовательский путь: перенесённое видно в ручке.
    who["id"], who["username"] = 1, "u1"
    assert {p["id"] for p in client.get("/dialogs").json()["projects"]} == {"a", "b"}


def test_migration_never_destroys_a_dialog_that_lives_only_in_the_database(api, tmp_path):
    """Перенос ДОПОЛНЯЕТ и никогда не удаляет. Инвариант всей операции.

    ДЕФЕКТ, ОТКРЫТЫЙ САМОЙ ПОЧИНКОЙ предыдущего шага. Убрав гейт «шапку берём
    только в пустую базу», перенос заодно начал сливать из файла НАДГРОБИЯ — а
    надгробие это не данные, которые надо сберечь, это КОМАНДА НА УДАЛЕНИЕ:
    хранилище сносит по нему строку. Старая команда «удалить X», приехав из
    файла в более свежую базу, убивала чат X, которого в файле нет вовсе.

    Отягчающие, которые делали это хуже обычной работы приложения: живой путь
    гасит надгробия старше тридцати суток, а перенос никакого срока не знал (в
    файле на 2,27 МБ они копились всю файловую эпоху); ломалась заявленная
    идемпотентность — вычищенное по сроку надгробие заводилось из файла заново
    на каждом прогоне; и отчёт об уничтоженной строке молчал.

    Сценарий здесь ровно тот: файл помнит удаление чата, который пользователь
    успел завести заново уже в базе.
    """
    from finengine.store.migrate_dialogs import main  # noqa: PLC0415

    client, who, _db = api
    source = tmp_path / "dialogs_files"
    source.mkdir()
    (source / "1.json").write_text(
        json.dumps({
            "projects": [_chat("a")],
            # Файл помнит: чат «live» когда-то удаляли.
            "deleted": {"live": time.time()},
        }),
        encoding="utf-8",
    )

    who["id"], who["username"] = 1, "u1"
    client.put("/dialogs", json={"projects": [_chat("live")], "last_project_id": "live"})

    assert main(["--source", str(source)]) == 0

    got = {p["id"] for p in client.get("/dialogs").json()["projects"]}
    assert "live" in got, (
        "перенос уничтожил чат, который жил ТОЛЬКО в базе, по надгробию из "
        "старого файла — операция, спасающая данные, их потеряла")
    assert "a" in got, "и при этом обязан был донести чат из файла"


def test_migration_stays_idempotent_when_the_file_remembers_deletions(api, tmp_path):
    """Повторный прогон не должен ничего менять — это первая строка докстринга
    самого модуля, и надгробия её ломали: живой путь гасил протухшее надгробие
    по сроку, а следующий перенос заводил его из файла заново и снова убивал
    совпавшую строку. И так бесконечно."""
    from finengine.store.migrate_dialogs import main  # noqa: PLC0415

    client, who, _db = api
    source = tmp_path / "dialogs_files"
    source.mkdir()
    (source / "1.json").write_text(
        json.dumps({"projects": [_chat("a")], "deleted": {"b": time.time()}}),
        encoding="utf-8",
    )
    who["id"], who["username"] = 1, "u1"

    assert main(["--source", str(source)]) == 0
    client.put("/dialogs", json={"projects": [_chat("a"), _chat("b")]})
    before = {p["id"] for p in client.get("/dialogs").json()["projects"]}

    assert main(["--source", str(source)]) == 0
    after = {p["id"] for p in client.get("/dialogs").json()["projects"]}
    assert after == before, (
        f"повторный перенос изменил состав: было {sorted(before)}, стало {sorted(after)}")


def test_migration_carries_folders_even_after_the_client_pushed(api, tmp_path, capsys):
    """Папки доезжают, даже если клиент успел запушить снимок раньше переноса.

    ДЕФЕКТ. Порядок переезда (deploy/docker-compose.dev.yml): сначала поднять
    бэкенд с адресом базы, потом запустить перенос. В зазоре между шагами
    открытая вкладка пушит своё состояние по дебаунсу — в базе появляется
    строка с saved_at. Перенос считал это «пользователь уже живёт в базе» и
    забирал из файла ТОЛЬКО диалоги: чаты доезжали, а папка «Мои банки»
    исчезала навсегда, причём отчёт про папки молчал.

    Тест повторяет тот порядок ровно: файл с папкой → пуш клиента → перенос.
    Пуш приносит и чат из файла, и новый — то есть воспроизводит ровно ту
    картину, которой дефект и заканчивался: ЧАТЫ ДОЕХАЛИ, А ПАПКА ИСЧЕЗЛА.
    Заодно это ловит вторую половину дефекта: перенос уходил ни с чем, когда
    новых диалогов нет, — хотя папке доехать было надо.
    """
    from finengine.store.migrate_dialogs import main  # noqa: PLC0415

    client, who, _db = api
    source = tmp_path / "dialogs_files"
    source.mkdir()
    (source / "1.json").write_text(
        json.dumps({
            "projects": [_chat("a")],
            "folders": [{"id": "f1", "name": "Мои банки", "updated_at": 1}],
            "last_project_id": "a",
            "deleted": {"старый": time.time()},
        }),
        encoding="utf-8",
    )

    # Тот самый пуш из зазора: вкладка сохраняет своё состояние в уже поднятый
    # бэкенд — папок в этом снимке нет, они есть только в файле.
    who["id"], who["username"] = 1, "u1"
    client.put(
        "/dialogs",
        json={"projects": [_chat("a"), _chat("live")], "last_project_id": "live"},
    )

    assert main(["--source", str(source)]) == 0
    out = capsys.readouterr().out

    state = client.get("/dialogs").json()
    assert [f["name"] for f in state["folders"]] == ["Мои банки"], "папка обязана доехать"
    assert "старый" not in (state.get("deleted") or {}), (
        "надгробия из файла в базу НЕ едут — см. следующий тест: это команда на "
        "удаление, а не данные")
    assert {p["id"] for p in state["projects"]} == {"a", "live"}
    # Отчёт называет ВСЕ виды перенесённого: одно «перенесено: N» про диалоги
    # читалось как «переезд полный», хотя папки могли не доехать вовсе.
    assert "перенесено папок:       1" in out
    assert "надгроб" not in out, (
        "счётчика надгробий в отчёте больше нет: их не переносят, считать нечего")


def test_migration_report_counts_what_landed_not_what_it_intended(api, tmp_path, capsys):
    """Отчёт переноса не приписывает себе диалог, удалённый пользователем.

    ДЕФЕКТ. Счётчик увеличивали ДО записи, а тот же `save` тут же удаляет
    строку диалога, на который в базе стоит надгробие. Сценарий житейский:
    перенесли два чата, один пользователь удалил, перенос запустили ещё раз
    («а точно всё доехало?») — печаталось «перенесено: 1 / уже было: 1» при
    одном диалоге в таблице. Отчёт — единственный прибор, по которому судят об
    успехе переезда; врущий прибор хуже отсутствующего.
    """
    from finengine.store.migrate_dialogs import main  # noqa: PLC0415

    client, who, db = api
    source = tmp_path / "dialogs_files"
    source.mkdir()
    (source / "1.json").write_text(
        json.dumps({"projects": [_chat("a"), _chat("b")], "folders": [], "last_project_id": "a"}),
        encoding="utf-8",
    )
    assert main(["--source", str(source)]) == 0
    capsys.readouterr()

    who["id"], who["username"] = 1, "u1"
    client.put(
        "/dialogs",
        json={"projects": [_chat("a")], "last_project_id": "a", "deleted_project_ids": ["b"]},
    )
    assert set(_rows(db)) == {"a"}

    assert main(["--source", str(source)]) == 0
    out = capsys.readouterr().out
    assert set(_rows(db)) == {"a"}, "перенос не воскрешает удалённый чат"
    assert "перенесено диалогов:    0" in out
    assert "удалены пользователем:  1" in out


def test_migration_refuses_when_no_database_is_configured(tmp_path, monkeypatch, capsys):
    """Без настроенной базы перенос обязан отказаться, а не «перенести в никуда»."""
    from finengine.config import settings  # noqa: PLC0415
    from finengine.store.migrate_dialogs import main  # noqa: PLC0415

    monkeypatch.setattr(settings, "dialogs_database_url", "")
    assert main(["--source", str(tmp_path)]) == 2
    assert "База не настроена" in capsys.readouterr().out
