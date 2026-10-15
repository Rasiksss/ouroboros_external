"""Дерево навыка анализа: призмы, ветки и доставка не расходятся между собой.

ЗАЧЕМ ЭТОТ ФАЙЛ. Навык анализа перестал быть одним файлом (канон
`docs/SKILLS_ARCHITECTURE.md` §3): корень несёт порядок, ветка — отрасль,
призма — вид разбора. Части лежат в РАЗНЫХ местах и читаются РАЗНЫМИ читателями:
тела веток читает гейтвейная модель (значит, они обязаны доехать в её воркспейс),
элементы методики читает код станции (они едут полкой). Расхождение любой пары
даёт молчаливый отказ: модель включает призму, которой нет; методолог пишет
призму, которую никто не включит; ветка едет на стенд, а её элементов в
методике нет.

САМОЕ ВАЖНОЕ ЗДЕСЬ — ПУСТОТА ЗАКОННА. На 14.08 не написано ни одной призмы:
механизм есть, методики нет. Сторож на пустых множествах тривиально зелёный и
краснеет ровно в тот момент, когда появляется первая половина пары. Объявленная
призма с пустой методикой опаснее отсутствующей — она обещает разбор, которого
не будет.

ПОЧЕМУ НЕ В СТАНЦИИ. Тот же паритет можно проверять на старте `analyze.py`, но
это стоит подъёма комнаты, распаковки полки и потраченного вызова модели ради
текстового расхождения, которое видно здесь бесплатно. Побеждает самое раннее и
дешёвое место.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: положить `station-skills/aifa-analyze/method/lens/foo.md`
и не добавить строку в таблицу призм корневого SKILL.md — тест обязан покраснеть.
(Проделано 21.08.2026: `test_the_three_lens_sets_match` краснеет.)

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (ревизия 21.08.2026). Он сверяет ИМЕНА в трёх
текстах и не исполняет ничего. Значит зелёный цвет здесь НЕ означает, что
призма работает, что модель её включит, что правило доехало до промпта или что
`sync_workspace_skills.sh` действительно отработал на гейтвее: про доставку
доказывается только согласие правила отбора (`-maxdepth`/`-name`) с глубиной
ссылок, а не факт копирования. И половина файла сегодня ВЫРОЖДЕНА: призм ноль,
`test_the_three_lens_sets_match` сверяет три пустых множества, а
`test_declared_lens_is_not_empty` уходит в `skip` — это ловушка на будущее, а
не действующая проверка.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "station-skills"
ANALYZE = SKILLS_DIR / "aifa-analyze"
ORDER = ANALYZE / "method" / "_order.md"
LENS_DIR = ANALYZE / "method" / "lens"
SYNC = ROOT / "deploy" / "sync_workspace_skills.sh"

# Ниже этого объёма файл призмы — обещание, а не методика.
MIN_LENS_CHARS = 200


def _lens_files() -> set[str]:
    return {p.stem for p in LENS_DIR.glob("*.md")} if LENS_DIR.is_dir() else set()


def _lens_groups() -> set[str]:
    """Группы `## lens: <имя>` в порядке сборки методики."""
    return {m.group(1).strip() for m in
            re.finditer(r"^##\s+lens:\s*(\S+)\s*$", ORDER.read_text(encoding="utf-8"), re.M)}


def _lens_rows() -> set[str]:
    """Строки таблицы призм корневого тела — единственное место триггера."""
    lines = (ANALYZE / "SKILL.md").read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if re.match(r"^\s*\|\s*Призма\s*\|", ln)), None)
    assert start is not None, (
        "в aifa-analyze/SKILL.md нет таблицы призм. Она обязана быть даже пустой: "
        "иначе решение «что подключить» размажется по телу навыка."
    )
    out = set()
    for ln in lines[start + 1:]:
        s = ln.strip()
        if not s.startswith("|"):
            break
        cell = s.strip("|").split("|")[0].strip()
        if set(cell) <= {"-", ":", " "}:
            continue  # разделитель шапки
        out.add(cell.strip("`"))
    return out


def _branches() -> list[Path]:
    return sorted(p.parent for p in ANALYZE.glob("*/SKILL.md"))


def test_the_three_lens_sets_match():
    """Файл призмы, группа в методике и строка в теле — либо все три, либо ни одной."""
    files, groups, rows = _lens_files(), _lens_groups(), _lens_rows()
    assert files == groups == rows, (
        f"призмы разошлись:\n"
        f"  файлы method/lens/*.md : {sorted(files)}\n"
        f"  группы lens: в _order.md: {sorted(groups)}\n"
        f"  строки таблицы в SKILL.md: {sorted(rows)}\n"
        f"Файл без строки в таблице не включит никто; строка без файла обещает "
        f"разбор, которого не будет; группа без файла уронит сборку методики."
    )


@pytest.mark.parametrize("name", sorted(_lens_rows()) or [""])
def test_declared_lens_is_not_empty(name: str):
    """Объявленная призма с пустой методикой опаснее отсутствующей."""
    if not name:
        pytest.skip("призм не объявлено — проверять нечего")
    path = LENS_DIR / f"{name}.md"
    assert len(path.read_text(encoding="utf-8").strip()) >= MIN_LENS_CHARS, (
        f"{path}: призма объявлена в теле навыка, но методики в ней нет"
    )


def test_every_branch_has_a_body_and_a_family_of_elements():
    """У ветки два конца: тело для модели и семейство элементов для промпта."""
    catalogue = ORDER.read_text(encoding="utf-8")
    assert _branches(), "у aifa-analyze не осталось ни одной ветки — тест устарел"
    for branch in _branches():
        name = branch.name
        assert (branch / "SKILL.md").is_file(), f"{name}: нет тела ветки"
        assert re.search(rf"^-\s+{re.escape(name)}\.", catalogue, re.M), (
            f"ветка {name}: в method/_order.md нет ни одного элемента «{name}.*». "
            f"Тело ветки говорит модели «здесь своя специфика», а в методике "
            f"этой специфики нет — разбор уйдёт по общим правилам молча."
        )


def _delivery_rule() -> tuple[int, str]:
    """Правило доставки тел на гейтвей: (максимальная глубина, имя файла)."""
    m = re.search(r"-maxdepth\s+(\d+)\s+-name\s+(\S+)", SYNC.read_text(encoding="utf-8"))
    assert m, (
        f"{SYNC}: не найдено правило отбора файлов навыка. Пока его нет, "
        f"нельзя сказать, доезжает ли тело ветки до воркспейса гейтвея."
    )
    return int(m.group(1)), m.group(2)


def test_every_file_the_model_reads_is_delivered():
    """Тело ссылается на файл — значит файл обязан существовать И доезжать.

    Болезнь проекта: сделанное не доезжает до места работы. Ссылка на файл,
    который остался в репозитории, — тот же молчаливый отказ, что и ссылка на
    несуществующий инструмент.
    """
    maxdepth, pattern = _delivery_rule()
    for body in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = body.read_text(encoding="utf-8")
        refs = {r for r in re.findall(r"`([\w./-]+\.md)`", text) if not r.startswith("/")}
        for rel in sorted(refs):
            # ДВА ТРАКТА, ДВА ПРАВИЛА (29.08.2026).
            #
            # Модель читает файлы навыка ДВУМЯ способами, и доставка у них разная:
            #
            #   тул `read`  → воркспейс гейтвея → едет только `SKILL.md` глубиной
            #                 до двух (`sync_workspace_skills.sh`);
            #   `exec` в комнате → ПОЛКА → едет ВЕСЬ `station-skills/` целиком
            #                 (`deploy/skill-hub/pack.sh`), включая `method/`.
            #
            # Ссылка вида `.shelf/aifa_station_skills/<навык>/…` — это второй
            # тракт, и мерить её правилом первого неверно: сторож требовал бы
            # положить методику в `SKILL.md`, хотя полка её везёт (проверено на
            # стенде: 23 файла `method/` внутри shelf.zip).
            if rel.startswith(".shelf/aifa_station_skills/"):
                на_полке = ROOT / "station-skills" / Path(
                    rel[len(".shelf/aifa_station_skills/"):])
                assert на_полке.is_file(), (
                    f"{body.parent.name}/SKILL.md велит прочитать с полки «{rel}», "
                    f"а в репозитории такого файла нет: {на_полке}"
                )
                continue
            target = (body.parent / rel).resolve()
            assert target.is_file(), (
                f"{body.parent.name}/SKILL.md ссылается на «{rel}» — файла нет"
            )
            depth = len(Path(rel).parts)
            assert depth <= maxdepth and target.name == pattern, (
                f"{body.parent.name}/{rel}: тело велит модели прочитать этот файл, "
                f"а доставка на гейтвей берёт только «{pattern}» глубиной до "
                f"{maxdepth}. Файл останется в репозитории, модель прочитает пустоту."
            )
