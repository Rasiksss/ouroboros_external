"""Тул правки методики: то, чем агент пользуется из чата (#123).

Проверяем поведение на границе агент↔методика, а не внутренности резолвера
(они в test_methodology.py): различие «сейчас/всегда», отказ вместо тихой
подмены, изоляция пользователей и то, что методика реально доезжает до анализа
(путь один — станционный, инлайн-ветка снята в Фазе D).
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from finengine.config import settings
from finengine.llm import tools as T
from finengine.skills.analysis import methodology as md
from finengine.skills.analysis import personal

REPO = Path(__file__).resolve().parents[1]


def _code(path: Path) -> str:
    """Исходник без комментариев И БЕЗ ДОКСТРОК.

    Докстроки переживают `ast.unparse` — это обычные строковые выражения, — а
    докстрока `station_turn` про методику как раз есть (строка 627). Сторож,
    ищущий подстроку в сыром тексте, зеленел бы от объяснительной записки о
    дефекте, который сторожит.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    personal._turn_deltas.clear()
    yield
    personal._turn_deltas.clear()


def call(action: str, uid: int = 7, **kw) -> dict:
    ctx = T.ToolContext(user_id=uid, project_id="p1")
    out = asyncio.run(T._h_fe_methodology({"action": action, **kw}, ctx))
    return json.loads(out)


def test_list_shows_what_can_be_tuned():
    got = call("list", query="nim")
    ids = [e["id"] for e in got["elements"]]
    assert "bank.formula.nim" in ids
    assert got["elements"][ids.index("bank.formula.nim")]["title"]


def test_show_gives_base_and_effective_text():
    got = call("show", id="bank.formula.nim")
    assert got["changed_by_user"] is False
    assert got["base_text"] == got["effective_text"]


def test_always_scope_persists_and_is_announced_as_permanent():
    got = call("set", id="bank.formula.nim", scope="always",
               text="NIM = NII / средние работающие активы за квартал.")
    assert got["ok"] and got["scope"] == "always"
    assert "всегда" in got["message"], "пользователь обязан понять, что правка постоянная"
    assert "Отменить" in got["message"]
    assert personal.load(7), "правка не легла на диск"
    assert call("show", id="bank.formula.nim")["changed_by_user"] is True


def test_turn_scope_does_not_persist():
    got = call("set", id="nf.rule.profit_quality", scope="turn",
               text="В этот раз исключи продажу оборудования из прибыли.")
    assert got["scope"] == "turn"
    assert "этом анализе" in got["message"]
    assert personal.load(7) == [], "разовая правка не должна оставаться навсегда"


def test_bad_id_is_refused_with_a_hint_not_silently_accepted():
    """Иначе агент решит, что правку учли, и не станет её уточнять."""
    got = call("set", id="bank.formula.nim_v2", text="что-то")
    assert "error" in got and "action=list" in got["hint"]
    assert personal.load(7) == []


def test_reset_returns_the_base_rule():
    call("set", id="bank.formula.nim", text="своё")
    assert call("reset", id="bank.formula.nim")["removed"] == 1
    assert call("show", id="bank.formula.nim")["changed_by_user"] is False


def test_mine_reports_changes():
    call("set", id="bank.formula.nim", text="своё", why="правила нашего банка")
    got = call("mine")
    assert [c["id"] for c in got["changes"]] == ["bank.formula.nim"]
    assert got["changes"][0]["why"] == "правила нашего банка"


def test_one_users_methodology_does_not_reach_another():
    call("set", id="bank.formula.nim", uid=7, text="только моё")
    assert call("show", id="bank.formula.nim", uid=8)["changed_by_user"] is False
    assert call("mine", uid=8)["changes"] == []


def test_add_requires_a_group_and_lands_in_the_analysis():
    got = call("add", id="user.def.working_assets", title="Работающие активы",
               group="analytical.bank",
               text="Работающие активы = кредиты клиентам + долговые ЦБ.")
    assert got["ok"]
    assert "кредиты клиентам + долговые ЦБ" in md.analytical_block(
        personal.effective(7, consume_turn=False), "bank")


# ───────────── методика обязана доезжать до анализа ─────────────


def test_analysis_path_passes_methodology_to_the_station():
    """Путь один (Фаза D) — и он обязан нести личную методику пользователя.

    ⚠️ ПЕРЕСЕЛЁН 20.08.2026 С МЁРТВОЙ ДВЕРИ. Прошлая редакция искала строку в
    `llm/tools.py`, где хендлера анализа больше нет (aa13cb1), и падала на
    отсутствии подстроки — о самом дефекте не сообщая ничего.

    ⚠️ ЭТО СТОРОЖ ОДНОЙ СТРОКИ, А НЕ ЦЕПОЧКИ. Он краснеет ровно на возврате
    `methodology=None` в `station_turn` — то есть на самом дешёвом способе
    отменить починку. Что правка ДОЕЗЖАЕТ до разбора, доказывает прогоном
    `tests/test_personal_methodology_reaches_the_room.py`: там взводится ход,
    зовётся роут, исполняется демон и запускается обёртка комнаты.
    """
    code = _code(REPO / "src" / "finengine" / "api" / "station_turn.py")
    assert "methodology=None" not in code, (
        "живой путь анализа прибил методику к None — личные правки пользователя "
        "не доезжают до станции ни при каких условиях")


def test_station_script_accepts_and_applies_deltas():
    """⚠️ Срез 13.5.4: канонический `analyze.py` больше не зовёт скилл
    `methodology=_methodology,`-параметром напрямую — правки применяются
    один раз (`effective_methodology = methodology.apply(deltas, strict=False)`),
    а в граф уходит уже готовый текстовый блок методики
    (`methodology_text=`, root_extra) плюс отпечаток `methodology_version`
    (по которому слушатель станции определяет «личная»/«базовая»,
    `station_turn._methodology_personal`)."""
    src = (REPO / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py").read_text(
        encoding="utf-8")
    assert '"--methodology"' in src
    assert "strict=False" in src, "потерянный якорь не должен ронять анализ в станции"
    assert "effective_methodology = methodology.apply(deltas, strict=False)" in src
    assert "methodology_version" in src, (
        "станция перестала ставить отпечаток применённой методики")


def test_provenance_records_the_rules_used():
    # Паспорт переехал из хендлера тула в общий модуль сборки (#244): им теперь
    # пользуются оба пути анализа, и отпечаток обязан быть один на оба.
    src = (REPO / "src" / "finengine" / "skills" / "analysis_result.py").read_text(
        encoding="utf-8")
    assert '"methodology_changes"' in src, "паспорт не отвечает, по каким ПРАВИЛАМ считали"
    assert '"methodology_lost"' in src, "потерянная правка обязана быть видна в паспорте"
