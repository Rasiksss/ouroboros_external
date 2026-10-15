"""Текст в комнате станции именуется документом, а не «ocr.txt» (Б8.2, 19.08.2026).

ЦЕНА ДЕФЕКТА. Демон писал текст в жёсткий `ocr.txt`, а обёртка при существующем
`ocr.txt` выходила с кодом 0, не заказав доставку. Два документа в одном ходе —
и второй прогон читал текст ПЕРВОГО, докладывая его числа под своим нонсом. Это
молчаливый неверный ответ: на экране всё выглядит удавшимся.

ДВА ПРАВИЛА, КОТОРЫЕ ЗДЕСЬ СТОРОЖАТСЯ.
1. Имя берётся из записки (`dst`), но только БЕЗОПАСНОЕ: записку пишет комната,
   а у демона есть и сеть, и корень сессии. `../`, вложенные каталоги и точечные
   имена обязаны падать в фолбэк, а не уводить запись из рабочей папки.
2. Фолбэк на `ocr.txt` ОБЯЗАТЕЛЕН в обе стороны: мост станции деплоится отдельно
   от полки навыков, и на стенде старый мост живёт с новой полкой и наоборот.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE = _ROOT / "deploy" / "station-bridge" / "bridge.py"
_WRAPPER = _ROOT / "station-skills" / "aifa-extract" / "scripts" / "extract_run.sh"
_STATION_EXTRACT = _ROOT / "station-skills" / "aifa-extract" / "scripts" / "extract.py"

_DOC_TEXT = "===PAGE 1===\nВыручка 1 000\n"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bridge():
    return _load(_BRIDGE, "station_bridge_daemon")


class _Text:
    def __init__(self, body: str, meta: str = "") -> None:
        self.text = body
        self.headers = {"x-doc-meta": meta} if meta else {}

    def raise_for_status(self) -> None:
        return None


class _Backend:
    def __init__(self, answer: _Text) -> None:
        self._answer = answer

    async def get(self, *_args, **_kw) -> _Text:
        return self._answer


def _deliver(tmp_path: Path, note: dict, meta: str = "") -> Path:
    """Один цикл доставки демоном: записка → текст в комнате. Возвращает workspace."""
    import asyncio

    bridge = _bridge()
    bridge.DOC_FETCH_URL = "http://backend/internal/doc-text"
    workspace = tmp_path / "workspace"
    rpc = workspace / ".model-rpc"
    rpc.mkdir(parents=True)
    (rpc / "rid1.fetch.json").write_text(json.dumps(note), encoding="utf-8")
    asyncio.run(bridge._service_fetch(str(rpc / "rid1.fetch.json"),
                                      _Backend(_Text(_DOC_TEXT, meta))))
    return workspace


# ── Демон: имя из записки, но только безопасное


@pytest.mark.parametrize("dst", [
    "../escape.txt", "a/b.txt", "/etc/passwd.txt", "..", ".hidden.txt",
    "ocr-x.json", "", None, 42,
])
def test_unsafe_names_fall_back_instead_of_escaping(dst):
    assert _bridge()._room_doc_name(dst) == "ocr.txt", (
        f"имя {dst!r} прошло — запись может уйти из рабочей папки комнаты")


def test_a_plain_document_name_is_honoured():
    assert _bridge()._room_doc_name("ocr-abc123.txt") == "ocr-abc123.txt"


def test_the_text_lands_under_the_requested_name(tmp_path):
    ws = _deliver(tmp_path, {"id": "rid1", "doc_id": "abc123", "nonce": "n",
                             "dst": "ocr-abc123.txt"})
    assert (ws / "ocr-abc123.txt").read_text(encoding="utf-8") == _DOC_TEXT
    assert not (ws / "ocr.txt").exists(), "старое имя тоже написано — путаница вернётся"


def test_a_note_without_a_name_still_works(tmp_path):
    """Старая полка `dst` не присылает. Отказ здесь означал бы, что первый же
    прогон после выкладки моста умрёт по FETCH-TIMEOUT."""
    ws = _deliver(tmp_path, {"id": "rid1", "doc_id": "abc123", "nonce": "n"})
    assert (ws / "ocr.txt").read_text(encoding="utf-8") == _DOC_TEXT


def test_an_escaping_name_writes_inside_the_workspace(tmp_path):
    """Не только «имя отвергнуто», но и «наружу ничего не легло»."""
    ws = _deliver(tmp_path, {"id": "rid1", "doc_id": "abc123", "nonce": "n",
                             "dst": "../../escape.txt"})
    assert (ws / "ocr.txt").read_text(encoding="utf-8") == _DOC_TEXT
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()


def test_the_meta_sidecar_follows_the_text(tmp_path):
    import base64

    meta = base64.b64encode(json.dumps({"company_name": "Ромашка"}).encode()).decode()
    ws = _deliver(tmp_path, {"id": "rid1", "doc_id": "abc123", "nonce": "n",
                             "dst": "ocr-abc123.txt"}, meta=meta)
    assert json.loads((ws / "ocr-abc123.meta.json").read_text(encoding="utf-8"))[
        "company_name"] == "Ромашка"


def test_one_rule_for_the_meta_name_in_both_places():
    """Демон кладёт мету, скрипт в комнате её ищет. Два правила разъехались бы
    при первой правке, и имя компании молча исчезло бы из карточки."""
    daemon = _bridge()._meta_name
    room = _load(_STATION_EXTRACT, "station_extract_under_test").meta_sidecar
    for name in ("ocr.txt", "ocr-abc123.txt"):
        assert daemon(name) == room(name).name
    assert daemon("ocr.txt") == "ocr.meta.json", "прежнее имя меты обязано сохраниться"


# ── Комната: обёртка заказывает свой файл и не берёт чужой


def _fetch_block(tmp_path: Path) -> Path:
    """Блок доставки из обёртки — отдельным файлом, чтобы прогнать как есть."""
    body = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", _WRAPPER.read_text(encoding="utf-8"), re.S)
    assert body, "блок доставки в обёртке не найден — сторож потерял предмет"
    path = tmp_path / "fetch_block.py"
    path.write_text(body.group(1), encoding="utf-8")
    return path


def _start(tmp_path: Path, doc: str = "abc123") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_fetch_block(tmp_path)), doc, "nonce-1"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for_note(tmp_path: Path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notes = list((tmp_path / ".model-rpc").glob("*.fetch.json")) if (
            tmp_path / ".model-rpc").exists() else []
        if notes:
            return json.loads(notes[0].read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError("записка о доставке не появилась")


def test_the_note_asks_for_a_document_named_file(tmp_path):
    proc = _start(tmp_path)
    try:
        note = _wait_for_note(tmp_path)
        assert note["dst"] == "ocr-abc123.txt", "обёртка снова просит общий файл"
        assert note["doc_id"] == "abc123" and note["nonce"] == "nonce-1"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_someone_elses_text_does_not_count_as_delivery(tmp_path):
    """ГЛАВНАЯ ПРОВЕРКА ФАЙЛА. Раньше готовый `ocr.txt` от ПЕРВОГО документа
    закрывал доставку второго, и прогон читал чужой отчёт."""
    (tmp_path / "ocr.txt").write_text("текст ПЕРВОГО документа", encoding="utf-8")
    proc = _start(tmp_path, doc="second")
    try:
        note = _wait_for_note(tmp_path)
        assert note["doc_id"] == "second", "доставка не заказана — прочитаем чужой текст"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_the_old_bridge_answer_is_accepted(tmp_path):
    """Разъезд версий: мост ответил `ok`, но положил текст в `ocr.txt`."""
    proc = _start(tmp_path)
    note = _wait_for_note(tmp_path)
    (tmp_path / "ocr.txt").write_text(_DOC_TEXT, encoding="utf-8")
    (tmp_path / ".model-rpc" / f"{note['id']}.fetchresp.json").write_text(
        json.dumps({"id": note["id"], "ok": True}), encoding="utf-8")
    assert proc.wait(timeout=20) == 0, "текст доставлен, а обёртка ждёт дальше"


def test_a_denial_is_still_fast(tmp_path):
    proc = _start(tmp_path)
    note = _wait_for_note(tmp_path)
    (tmp_path / ".model-rpc" / f"{note['id']}.fetchresp.json").write_text(
        json.dumps({"id": note["id"], "error": "403 nonce scope"}), encoding="utf-8")
    assert proc.wait(timeout=20) == 3
    assert "FETCH-DENIED" in proc.stderr.read()


def test_the_engine_is_given_the_file_that_actually_arrived():
    """Имя файла считается один раз и оттуда уходит в `--text`: захардкоженный
    `./ocr.txt` в команде обесценил бы всё, что выше."""
    lines = [ln for ln in _WRAPPER.read_text(encoding="utf-8").splitlines()
             if not ln.lstrip().startswith("#")]
    body = "\n".join(lines)
    assert 'TXT="./ocr-$DOC.txt"' in body
    assert '[ -s "$TXT" ] || TXT="./ocr.txt"' in body, "фолбэк на старый мост исчез"
    assert 'set -- --text "$TXT"' in body, "движку снова передают жёсткое имя"


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", ".hidden", "a b", "a;b"])
def test_the_wrapper_refuses_a_document_id_that_is_not_a_file_name(tmp_path, bad):
    """Идентификатор становится именем файла — форму проверяем на входе, иначе
    прогон умрёт по таймауту вместо внятного отказа."""
    proc = subprocess.run(["bash", str(_WRAPPER), "--doc-id", bad],
                          cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2, f"идентификатор {bad!r} принят как имя файла"
    assert "ОТКАЗ" in proc.stderr
