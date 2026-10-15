"""Вид анализа доезжает до ИСПОЛНИТЕЛЯ, а перечень видов живёт в одном месте (#240).

ЧТО БЫЛО. Реестр видов (`skills/analysis/ANALYSIS_SKILLS`) работал ВОРОТАМИ, но
не маршрутизатором. Тул `fe_analyze` принимал `analysis_type`, валидировал его
по реестру — и звал станцию, а в станции стояло жёсткое
`get_skill("general")  # единственный готовый вид`. Аргумента до исполнителя не
существовало вовсе.

Следствие ровно одно, и оно про обещание, а не про баг: пометь завтра
`comparative` как ready — поведение не изменится ни на йоту. Сценарий владельца
«завтра новый вид анализа — добавил и работает» был непроверяемым по
построению, и «работает» никто никогда не видел.

ХУЖЕ ТОГО, перечень видов был вписан РУКАМИ В ТРЁХ МЕСТАХ: схема тула
(`llm/tools.py`), плагин гейтвея (`fe-tools/index.ts` — TypeScript, отдельный
деплой) и сам реестр. Новый вид = три правки в трёх местах и два деплоя; забыл
одну — модель либо зовёт несуществующий вид, либо не видит готового.

ЧТО ЗДЕСЬ ЗАЩИЩАЕТСЯ:
  1. исполнитель выбирается ПО АРГУМЕНТУ, а не по литералу;
  2. отказы разные и внятные: «нет такого вида» ≠ «есть, но не готов»;
  3. флаг назван в теле навыка — иначе модель его не поставит.

🔴 ТРЁХ МЕСТ БОЛЬШЕ НЕТ, и половина этого файла ушла вместе с ними (aa13cb1).
Схемы тула `fe_analyze` не существует (`grep fe_analyze src/finengine/llm/
tools.py` — пусто), командную строку драйвер не собирает (`station_driver.py`
— 74 строки без `httpx`). Перечень видов остался в ОДНОМ месте — реестре
`skills/analysis/ANALYSIS_SKILLS`, — и читает его сам станционный скрипт.
Поэтому сторожем «перечень живёт в одном месте» служат теперь ЗЕЛЁНЫЕ
`test_unknown_type_is_refused_by_name` и `test_planned_type_is_refused_with_
its_status` ниже: они гоняют боевой `analyze.py` и требуют, чтобы меню
доступных видов и статус «не готов» пришли ИЗ РЕЕСТРА.

🔴 РЕВИЗИЯ 21.08: сняты два текстовых теста, оба доказанно дублирующие.
  * `test_the_executor_takes_the_argument` искал `get_skill(analysis_type)` в
    ИСХОДНИКЕ `analyze.py`. Мутация (вернуть литерал `get_skill("general")`)
    роняет `test_unknown_type_is_refused_by_name` и
    `test_planned_type_is_refused_with_its_status` — они гоняют скрипт и
    получают коды 0 вместо 2 и 3. Грепалка ловила ту же мутацию позже и
    хуже — по написанию, а не по поведению.
  * `test_the_body_names_the_flag` читал `SKILL.md` на подстроку. Мутация
    (переименовать флаг в теле навыка) роняет
    `test_skill_flags_are_documented.py::test_every_script_flag_is_named_in_
    the_body[aifa-analyze]`, который спрашивает список флагов у САМОГО
    argparse через `--help` — то есть строго сильнее.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в analyze.py `get_skill("general")` —
краснеют оба теста отказа ниже.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ANALYZE = REPO / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py"

REPORT_TEXT = "===PAGE 1===\nКонсолидированный отчёт по МСФО за 2024 год\nВыручка 500\n"


def _run(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    txt = tmp_path / "ocr.txt"
    txt.write_text(REPORT_TEXT, encoding="utf-8")
    mj = tmp_path / "metrics.json"
    mj.write_text(json.dumps({"metrics": [{"enum": "SALES_REVENUE",
                                           "name_ru": "Выручка"}]}), encoding="utf-8")
    # 🔴 cwd=tmp_path ОБЯЗАТЕЛЕН: замок стража дубля живёт в cwd, при выходе
    # не снимается и 90 секунд остаётся «свежим». Прогоны из общего cwd на
    # Linux напарываются друг на друга — второй честно отказывает кодом 5.
    # На маке страж выключен (нет /proc), поэтому локально дыры не видно.
    # Свой каталог на тест = своя комната, как в настоящей станции.
    return subprocess.run(
        [sys.executable, str(ANALYZE), "--text", str(txt), "--metrics", str(mj), *extra],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )


# ── ГЛАВНОЕ: аргумент доезжает до исполнителя, отказы разные и внятные ──────


def test_unknown_type_is_refused_by_name(tmp_path):
    """«Нет такого вида» — это опечатка или выдумка модели, код 2."""
    res = _run(tmp_path, "--analysis-type", "чего-то-нет")
    assert res.returncode == 2, f"неизвестный вид не отбит: {res.stderr[-500:]}"
    assert "неизвестный вид анализа" in res.stderr
    assert "general" in res.stderr, "меню доступных видов не показано"


def test_planned_type_is_refused_with_its_status(tmp_path):
    """«Есть, но не готов» — честный план, код 3 и что нужно на вход."""
    res = _run(tmp_path, "--analysis-type", "comparative")
    assert res.returncode == 3, f"неготовый вид не отбит отдельным кодом: {res.returncode}"
    assert "ещё не готов" in res.stderr
    assert "2 компании" in res.stderr, "не сказано, что потребуется на вход"


def test_default_stays_general(tmp_path):
    """Без флага — прежнее поведение: общий финанализ, а не отказ.

    ⚠️ Ловим и отказ СТРАЖА (код 5): на стенде этот тест был зелен вакуумно —
    скрипт отбивался стражем дубля из-за общего cwd, и «нет отказа по виду»
    было правдой при полностью не начатой работе.
    """
    res = _run(tmp_path)
    assert res.returncode not in (2, 3, 5), (
        f"скрипт отказал кодом {res.returncode}, до общего анализа не дошло: "
        f"{res.stderr[-300:]}")
    assert "ОТКАЗ: неизвестный вид" not in res.stderr
    assert "ОТКАЗ: вид анализа" not in res.stderr
