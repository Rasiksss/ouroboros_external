"""Характеризация: какие файлы сегодня видят гварды, сканирующие движок целиком.

ЗАЧЕМ ЭТОТ ФАЙЛ ЗАВЕДЁН (13.09.2026). Минимум пять гвард-тестов независимо
делают `rglob("*.py")` + `ast.parse()` по одному и тому же (или вложенному)
поддереву `src/finengine/` — измерено прогоном `pytest --durations`:

    test_registry_external_dir.py::test_no_call_site_swallows_a_broken_registry   1.25с
    test_bg_extract_no_duplicate.py (два теста)                                   0.73с
    test_document_text_lives_in_one_place.py                                      0.44с
    test_calculated_rows_all_carry_inputs.py                                      0.43с
    test_station_has_what_the_code_imports.py (два теста)                        0.41с

~3.3с из 71с прогона — один и тот же файл `finengine/skills/two_stage_extractor.py`
парсится с нуля пятью разными процессами теста. Кандидат на общий
session-scoped кэш `ast.parse` по пути (план — не здесь, ждёт «да»).

ЧТО СТОРОЖИТ ЭТОТ ФАЙЛ. Не логику существующих гвардов — только их ВХОД:
точный набор файлов, который каждый корень отдаёт сегодня. Общий кэш обязан
отдавать РОВНО ТЕ ЖЕ файлы каждому потребителю, что и его сегодняшний личный
`rglob`; отпечаток (sha256 отсортированных путей) плюс количество — самая
дешёвая пломба против того, чтобы кэш тихо сузил или расширил чей-то корень
(например, перепутал `src/finengine` и `src/finengine/skills`).

Красный здесь после рефакторинга кэша — не обязательно баг: он может значить
«состав файлов правда поменялся» (кто-то добавил модуль). Тогда пломбу просто
пересчитывают. Но красный БЕЗ рефакторинга кэша — всегда баг: кто-то отредактировал
не тот `.py` файл так, что дерево `rglob` не заметило (например, переименование
директории).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (относительный корень, точное число .py-файлов, sha256[:16] отсортированных
# относительных путей). Пересчитать одной командой при осознанном изменении
# состава: см. docs/... — или скриптом в теле теста ниже (test_recompute).
ROOTS = {
    # 203 → 216 (16.09.2026): добавлены src/finengine/contracts/ (12 файлов,
    # A0) и src/finengine/policy/sandbox_profiles.py (A4) — осознанное
    # изменение состава, Фаза 0 build-plan, см. docs/build-plan/10-faza-0-fundament.md.
    # 216 → 222 (16.09.2026, Срез 1): добавлены src/finengine/skills/atoms/
    # (6 файлов, включая __init__.py — doc.probe/id.entity/id.frame/id.periods
    # + сборка run_passport), см. docs/build-plan/11-srez-01-run-passport.md.
    # 222 → 223 (16.09.2026, Срез 2): добавлен src/finengine/skills/
    # evidence_bundle.py (адаптер two_stage_extractor/formula_engine в
    # EvidenceBundleV2), см. docs/build-plan/12-srez-02-evidence-bundle-v2.md.
    # 223 → 228 (16.09.2026, Срез 3): добавлены src/finengine/skills/spec/
    # atom_registry.py, graph_planner.py, graph_executor.py, feasibility.py
    # (AtomRegistry/GraphPlanner/GraphExecutor/intent.contract, A1/A2) и
    # src/finengine/skills/atoms/graph_atoms.py (адаптеры существующих крупных
    # компонентов для B3), см. docs/build-plan/13-srez-03-graph-planner-executor.md.
    # 228 → 236 (19.09.2026, Срез 4 + Срез 5a + Срез 5b): пломба не была
    # пересчитана после Среза 4 (добавлены src/finengine/contracts/intent.py,
    # src/finengine/skills/atoms/intent.py — GoalIR/TargetNode/intent.parse,
    # docs/build-plan/14-srez-04-graf-istochnik-istiny.md) и Среза 5a
    # (добавлены src/finengine/skills/atoms/document.py, document_index.py,
    # locate.py, read_value.py, docs/build-plan/15a-srez-05a-locate-read-value-
    # yadro.md) — обнаружено этой сессией при первом полном прогоне `pytest
    # tests/` (не только `tests/atoms`/`tests/planner`/`tests/integration`),
    # не регрессия Среза 5b. Пересчитано ЗАРАЗ вместе с Срезом 5b (добавлены
    # src/finengine/skills/atoms/read_structure.py, read_quote.py,
    # read_entities.py, docs/build-plan/15b-srez-05b-read-structure-quote-
    # entities.md) — итоговая дельта 228 → 236 покрывает все три среза сразу.
    # 236 → 247 (20.09.2026, Срез 6a): добавлены src/finengine/contracts/
    # basis.py, definitions.py (Basis/BasisComparison, MetricDefinition/
    # FormulaDefinition, A0-расширение) и 9 файлов src/finengine/skills/
    # atoms/ — norm_unit.py, norm_time.py, norm_basis.py, verify_bounds.py,
    # verify_identity_cross.py (verify.identity/verify.cross), verify_
    # citation.py, verify_grounding.py, calc_formula.py, definitions.py
    # (definition resolver) — см. docs/build-plan/16a-srez-06a-normalizaciya-
    # proverki-raschety.md. Итоговая дельта 11 файлов, из них 9 под skills/.
    # Значения посчитаны на Linux dev-стенде (rasul@85.198.84.50) — на Windows
    # Path.relative_to() отдаёт обратный слэш и даёт другой (ложный) хэш при
    # том же наборе файлов; см. известную находку в PHASE-0-STATUS.md.
    # 247 → 254 (20.09.2026, Срез 7): добавлен src/finengine/skills/
    # document_acquisition/ (7 файлов — __init__.py, url_safety.py, policy.py,
    # downloader.py, search.py, registration.py, acquire.py; A5 — поиск и
    # безопасное скачивание отчётности из интернета), см. docs/build-plan/
    # 17-srez-07-otchetnost-iz-interneta.md. Все 7 — под skills/, поэтому обе
    # строки сдвинулись на одну и ту же дельту.
    # 278 → 279 (23.09.2026, Срез 13.5): добавлен src/finengine/skills/
    # production_runtime.py (единственный production ingress в CaseRuntime),
    # см. docs/build-plan/23a-srez-13-5-production-cutover.md. Предыдущее
    # значение 278 было посчитано ПОСЛЕ удаления spec/runner.py и spec/steps.py
    # (см. запись про Срез 14 ниже), но ДО появления production_runtime.py —
    # обе правки вошли в одну незакоммиченную сессию.
    # 279 → 283 (24.09.2026, Срез 31, Ingress v2): добавлены
    # src/finengine/orchestrator/live_stream.py (живая передача событий из
    # конвейера в SSE), skills/atoms/ad_hoc_concepts.py (ad-hoc понятия и формулы
    # от модели), skills/spec/atom_catalog.py и одноходовый агент-планировщик
    # — см. docs/build-plan/31-vozvrat-k-zamyslu-
    # vladeltsa-ingress-v2.md. Значения посчитаны на Linux dev-стенде.
    # 283 → 284 (25.09.2026, Срез 32): добавлен skills/atoms/out_changes.py
    # (атом `out.changes` — таблица изменений между периодами одного отчёта для
    # MONITOR_CHANGES), см. docs/build-plan/32-monitor-changes-odin-dokument.md.
    # Под skills/, поэтому обе строки сдвинулись на одну дельту. Значения
    # посчитаны на Linux dev-стенде.
    # 284 → 287 (30.09.2026, Срез 34, часть 1): добавлены contracts/dynamic.py
    # (DynamicArtifact), skills/spec/sealed_room.py и skills/spec/_room_entry.py
    # (исполнение кода временных атомов в комнате станции), см.
    # docs/build-plan/34-gibridnyy-planirovshchik-dynamic-run.md. Под skills/ два
    # файла из трёх. Значения посчитаны на Windows: пути POSIX, набор тот же.
    # 287 → 288, 176 → 177 (01.10.2026, часть 2): src/finengine/skills/spec/ephemeral.py
    # (временные атомы с кодом) — осознанное изменение состава.
    # 288 → 289, 177 → 178 (01.10.2026, часть 3): src/finengine/skills/spec/atom_author.py
    # (автор временных атомов) — осознанное изменение состава. atom_author.md не .py и не считается.
    # 289 → 290, 178 → 179 (01.10.2026, часть 4): src/finengine/skills/spec/dynamic_run.py
    # (цикл гибридного планировщика) — осознанное изменение состава.
    # 290 → 289, 179 → 178 (01.10.2026, часть 5): удалён заменённый
    # одноходовый агент-планировщик; остаётся один путь через DynamicRun.
    "src/finengine": (289, "0ab17bf61a511b1e"),
    "src/finengine/skills": (178, "da53fccc370ff724"),
    # Срез 14 удалил два самостоятельных runner'а: manual spec и model plan;
    # Срез 27 добавил один canonical UI ingress для семи GoalType.
    "station-skills": (4, "64ac3c71d8ae35ba"),
}


def _fingerprint(root: Path) -> tuple[int, str]:
    # POSIX separators make this guard identical on Windows and Linux.
    rels = sorted(p.relative_to(REPO).as_posix() for p in root.rglob("*.py"))
    digest = hashlib.sha256("\n".join(rels).encode()).hexdigest()[:16]
    return len(rels), digest


def test_engine_scan_roots_have_not_silently_drifted():
    """Набор файлов под каждым корнем — тот же, что видели пять гвардов 13.09.2026.

    Не о том, ПРАВИЛЬНЫ ли эти файлы (это дело самих гвардов) — только о том,
    что общий кэш, если его заведут, унаследует ровно этот же вход, а не
    случайно более узкий или более широкий.
    """
    mismatched = {}
    for rel, expected in ROOTS.items():
        actual = _fingerprint(REPO / rel)
        if actual != expected:
            mismatched[rel] = {"expected": expected, "actual": actual}
    assert not mismatched, (
        "состав .py-файлов под корнем поменялся с 13.09.2026 — если это "
        "осознанно (добавлен/удалён модуль), пересчитайте ROOTS; если нет — "
        "почему rglob('*.py') вдруг видит другой набор:\n"
        f"  {mismatched}")


def test_the_skills_root_is_a_subset_of_the_finengine_root():
    """`src/finengine/skills` — не отдельное дерево, а поддерево `src/finengine`.

    Общий кэш по пути опирается ровно на это: один парс `src/finengine`
    обязан покрывать и запрос `src/finengine/skills` без отдельного прохода.
    """
    finengine_files = {p.relative_to(REPO) for p in (REPO / "src" / "finengine").rglob("*.py")}
    skills_files = {p.relative_to(REPO) for p in (REPO / "src" / "finengine" / "skills").rglob("*.py")}
    assert skills_files <= finengine_files, (
        "skills-поддерево содержит файлы вне src/finengine — предположение "
        "общего кэша (один проход накрывает оба корня) неверно")
