"""Тело навыка — семь секций в одном порядке, и инструменты только в одной из них.

ЗАЧЕМ ЭТОТ ФАЙЛ. Тело `SKILL.md` читает гейтвейная модель, и читает его ЦЕЛИКОМ
и на ходу. Пока структура свободная, тело расползается: в него утекает методика
(которая на самом деле едет в станцию), имена инструментов рассыпаются по всему
тексту, а «когда НЕ я» не пишется вовсе — и навык начинает перехватывать чужие
запросы. Структура из семи секций — из docs/INSTRUCTION_CONTRACT.md §6.3.

САМОЕ ВАЖНОЕ ЗДЕСЬ — проверка «инструмент существует». 09.08 выяснилось, что
`aifa-analyze/SKILL.md` полтора месяца велел модели запускать станцию командой
`exec bash …/run.sh`, хотя живой путь — тул `fe_analyze`. Никто не заметил,
потому что тело ни разу не открывали (#165). Ссылка на несуществующий способ
вызова — это молчаливо неисполнимая инструкция.

ПРО PENDING. Список исключений умеет только СОКРАЩАТЬСЯ: новый навык попадает
под проверку сразу, добавить его в исключения молча не выйдет. 14.08 список
опустел: `aifa-extract/SKILL.md` переписан по §6.3 в окне переключения, и все
операционные уроки, купленные инцидентами, перенесены в него дословно.

ПРО ВЕТКИ (`<навык>/<ветка>/SKILL.md`). Навык анализа ветвится по отрасли
(канон `docs/SKILLS_ARCHITECTURE.md` §3), и у ветки СВОЙ контракт — короче
корневого. Ветка это ДЕЛЬТА: она не навык, её не выбирают по описанию, её
читают по имени из корня. Поэтому у неё четыре секции вместо семи, потолок
60 строк вместо 150 и запрет называть инструменты вовсе: команда запуска живёт
в корне в единственном экземпляре, а две копии команды разъезжаются.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: переставить местами две секции в aifa-analyze/SKILL.md
или упомянуть `fe_calc` в разделе «Правила» — тесты обязаны покраснеть
(проверено мутацией 21.08.2026: перестановка секций → красный).

⚠️ ЧЕГО ЭТИ СТОРОЖА НЕ ЛОВЯТ (дописано ревизией 21.08.2026).

РАЗ. Они проверяют ФОРМУ текста, который читает гейтвейная модель, и ни один
из них не спрашивает, СРАБОТАЛО ли тело. Семь секций в правильном порядке,
уложенные в бюджет строк, — это не доказательство, что модель выбрала навык,
поняла порядок и дошла до конца. Единственное здесь, что опирается на живой
код, — сверка названных инструментов с `llm.tools.REGISTRY`.

ДВА, И ЭТО ХУЖЕ. Весь файл собран ГЛОБОМ по `station-skills/*/SKILL.md`, и
поэтому ИСЧЕЗНОВЕНИЕ НАВЫКА для него не событие. Проверено мутацией
21.08.2026: убрал каталог `station-skills/aifa-calc` целиком — 27 passed,
ни одного красного. `test_there_is_something_to_check` требует лишь, чтобы
навык остался ХОТЬ ОДИН. Кто сторожит комплект навыков — здесь не сторожат.

ТРИ. `METHODOLOGY_LEAKS` — список из шести подстрок, а не понимание методики:
порог, записанный словами («выше трёх» вместо «> 3,0»), проедет зелёным.

🔴 СНЯТО РЕВИЗИЕЙ 21.08.2026: `test_pending_list_has_no_ghosts`. Он считал
`PENDING - set(ALL_SKILLS)`, а `PENDING` — пустое множество, объявленное
двадцатью строками выше в ЭТОМ ЖЕ файле. Пустое множество минус что угодно
пусто всегда: тест не мог покраснеть ни от одной правки продукта. Мутация:
удалил каталог навыка целиком — тест зелёный.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from finengine.llm.tools import REGISTRY

ROOT = Path(__file__).resolve().parents[1]

SECTIONS = ["Когда", "Что нужно на входе", "Если входа нет", "Порядок работы",
            "Правила", "Вывод", "Границы навыка"]
TOOLS_SECTION = "Порядок работы"
MAX_LINES = 150

# Навыки, написанные до §6.3. Список только сокращается — см. шапку файла.
PENDING: set[str] = set()

BRANCH_SECTIONS = ["Когда эта ветка", "Порядок работы", "Правила ветки", "Вывод"]
BRANCH_MAX_LINES = 60

METHODOLOGY_LEAKS = ("ebitda /", "чистый долг /", "коэффициент текущей",
                     "норматив н1", "> 3,0", "< 1,0")

ALL_SKILLS = sorted(p.parent.name for p in (ROOT / "station-skills").glob("*/SKILL.md"))
MIGRATED = [s for s in ALL_SKILLS if s not in PENDING]
BRANCHES = sorted((ROOT / "station-skills").glob("*/*/SKILL.md"))


def _body(skill: str) -> str:
    text = (ROOT / "station-skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    return text.split("---", 2)[2] if text.startswith("---") else text


def _headings(skill: str) -> list[str]:
    return [ln[3:].strip() for ln in _body(skill).splitlines() if ln.startswith("## ")]


def _split_sections(skill: str) -> dict[str, str]:
    out, cur = {}, None
    for ln in _body(skill).splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            out[cur] = ""
        elif cur:
            out[cur] += ln + "\n"
    return out


def test_there_is_something_to_check():
    assert MIGRATED, "под проверкой не осталось ни одного навыка — тест устарел"


@pytest.mark.parametrize("skill", MIGRATED)
def test_seven_sections_in_order(skill):
    got = _headings(skill)
    assert got == SECTIONS, (
        f"{skill}/SKILL.md: секции\n  есть:  {got}\n  надо:  {SECTIONS}\n"
        f"Порядок держит чтение модели: «когда» → «что нужно» → «как добыть» → "
        f"«что делать» → «чего нельзя» → «что показать» → «когда НЕ я»."
    )


@pytest.mark.parametrize("skill", MIGRATED)
def test_tool_names_only_in_the_order_section(skill):
    for name, body in _split_sections(skill).items():
        if name == TOOLS_SECTION:
            continue
        found = [t for t in REGISTRY if re.search(rf"\b{re.escape(t)}\b", body)]
        assert not found, (
            f"{skill}/SKILL.md: инструменты {found} упомянуты в «{name}». "
            f"Единственное место для имён инструментов — «{TOOLS_SECTION}»: "
            f"иначе порядок вызова оказывается размазан по файлу."
        )


@pytest.mark.parametrize("skill", MIGRATED)
def test_every_named_tool_exists(skill):
    """Ссылка на несуществующий инструмент — молча неисполнимая инструкция."""
    order = _split_sections(skill).get(TOOLS_SECTION, "")
    named = set(re.findall(r"`(fe_[a-z_]+)`", order))
    missing = sorted(named - set(REGISTRY))
    assert not missing, (
        f"{skill}/SKILL.md: в «{TOOLS_SECTION}» названы несуществующие инструменты "
        f"{missing}. Модель попробует их позвать и не сможет."
    )


# ⚠️ УДАЛЁН Срезом 13.5.4: `test_the_check_above_is_not_vacuous`. Держал
# инвариант «хоть один из трёх навыков (`aifa-extract`/`aifa-analyze`/
# `aifa-calc`) называет `fe_*`-инструмент, иначе проверка `test_every_named_
# tool_exists` тавтология». Инвариант перестал держаться АРХИТЕКТУРНО, не
# из-за недосмотра в тексте: ни один из трёх навыков сегодня не вызывает ни
# одного `fe_*`-инструмента вообще — все трое запускаются только `exec`
# станционных shell-скриптов суб-агентом (`sessions_spawn`), тем же путём,
# которым уже работал `aifa-calc` (обоснование `test_the_check_above_is_not_
# vacuous`'s собственный докстринг). Проверять здесь больше нечего — не
# потому что текст SKILL.md не дописан, а потому что предмет проверки
# (вызов тула из тела навыка) в этом наборе из трёх навыков не существует.
# `test_tool_names_only_in_the_order_section`/`test_every_named_tool_exists`
# остаются: они по-прежнему поймают попытку назвать несуществующий тул, если
# он когда-нибудь появится в теле любого из навыков, просто честно ничего не
# находят сегодня.


@pytest.mark.parametrize("skill", MIGRATED)
def test_body_fits_the_reading_budget(skill):
    n = len(_body(skill).splitlines())
    assert n <= MAX_LINES, (
        f"{skill}/SKILL.md: {n} строк при потолке {MAX_LINES}. Длиннее — выносить "
        f"в соседний файл каталога навыка со ссылкой относительным путём."
    )


@pytest.mark.parametrize("skill", MIGRATED)
def test_methodology_did_not_leak_into_the_body(skill):
    """Методика едет в станцию файлами и правится методологом — копии здесь быть не должно.

    Признак утечки — формулы и пороги коэффициентов в теле навыка.
    """
    body = _body(skill).lower()
    leaks = [w for w in METHODOLOGY_LEAKS if w in body]
    assert not leaks, (
        f"{skill}/SKILL.md: похоже на методику в теле навыка ({leaks}). "
        f"Тело — это ПОРЯДОК действий; правила счёта живут в method/."
    )


# ── ветки навыка: свой контракт, короче корневого ───────────────────────


def _branch_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _branch_id(path: Path) -> str:
    return f"{path.parent.parent.name}/{path.parent.name}"


@pytest.mark.parametrize("path", BRANCHES, ids=_branch_id)
def test_branch_has_its_own_four_sections(path: Path):
    got = [ln[3:].strip() for ln in _branch_lines(path) if ln.startswith("## ")]
    assert got == BRANCH_SECTIONS, (
        f"{_branch_id(path)}/SKILL.md: секции\n  есть:  {got}\n  надо:  {BRANCH_SECTIONS}\n"
        f"Ветка — ДЕЛЬТА к корню: «когда именно эта» → «что обязательно» → "
        f"«чем правила отличаются» → «что на выходе». Семь секций корня здесь "
        f"означали бы вторую копию порядка."
    )


@pytest.mark.parametrize("path", BRANCHES, ids=_branch_id)
def test_branch_names_no_tools(path: Path):
    """Команда запуска живёт в корне в одном экземпляре — иначе копии разъедутся."""
    text = path.read_text(encoding="utf-8")
    found = [t for t in REGISTRY if re.search(rf"\b{re.escape(t)}\b", text)]
    assert not found, (
        f"{_branch_id(path)}/SKILL.md: названы инструменты {found}. Ветка говорит "
        f"ЧТО учесть, корень — ЧЕМ запускать."
    )


@pytest.mark.parametrize("path", BRANCHES, ids=_branch_id)
def test_branch_fits_the_reading_budget(path: Path):
    n = len(_branch_lines(path))
    assert n <= BRANCH_MAX_LINES, (
        f"{_branch_id(path)}/SKILL.md: {n} строк при потолке {BRANCH_MAX_LINES}. "
        f"Ветка читается ВМЕСТЕ с корнем: разрастаясь, она съедает бюджет обоих."
    )


@pytest.mark.parametrize("path", BRANCHES, ids=_branch_id)
def test_branch_did_not_copy_the_methodology(path: Path):
    text = path.read_text(encoding="utf-8").lower()
    leaks = [w for w in METHODOLOGY_LEAKS if w in text]
    assert not leaks, (
        f"{_branch_id(path)}/SKILL.md: похоже на методику в ветке ({leaks}). "
        f"Ветка называет ОСИ разбора; формулы и пороги живут в method/."
    )


@pytest.mark.parametrize("path", BRANCHES, ids=_branch_id)
def test_branch_did_not_copy_the_root(path: Path):
    """Дословный повтор корня — это не дельта, а вторая редакция того же текста.

    Сверяем содержательные строки (короткие заголовки и разметка совпадают
    законно). Первая же разошедшаяся копия дала бы модели два разных порядка.
    """
    root = (path.parent.parent / "SKILL.md").read_text(encoding="utf-8")
    root_lines = {ln.strip() for ln in root.splitlines() if len(ln.strip()) >= 40}
    dupes = [ln.strip() for ln in _branch_lines(path)
             if len(ln.strip()) >= 40 and ln.strip() in root_lines]
    assert not dupes, (
        f"{_branch_id(path)}/SKILL.md: строки списаны с корня — {dupes[:2]}"
    )
