"""Конфиг гейтвея живёт в репозитории, а не только на стенде (17.08.2026).

ЧЕМ ЭТО БЫЛО ПЛОХО. `openclaw.json` задаёт основную модель, цепочку фоллбэков,
песочницу и пути плагинов — то есть ПОВЕДЕНИЕ агента, — и не был ни в одной
ветке. За один день я столкнулся со всеми тремя последствиями сразу:
  * менял модель на dev — правку невозможно воспроизвести и некуда откатить,
    кроме случайного `.bak` рядом;
  * сравнивал dev с продом — сравнивать было нечего, пришлось читать оба файла
    по ssh;
  * искал, чем прод отличается, — узнал только тем, что залез на машину.

🔒 СЕКРЕТЫ В РЕПОЗИТОРИЙ НЕ ЕДУТ. В шаблонах вместо токена стоит подстановка;
настоящий токен берётся из работающего конфига на стенде.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_GW = Path(__file__).resolve().parents[1] / "deploy" / "gateway"
_ENVS = ("dev", "prod")


def _cfg(env: str) -> dict:
    return json.loads((_GW / f"openclaw.{env}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("env", _ENVS)
def test_template_exists_and_parses(env):
    """Отрицательный контроль: удалил шаблон — краснеет."""
    assert (_GW / f"openclaw.{env}.json").exists(), f"нет шаблона для {env}"
    assert _cfg(env)["agents"]["defaults"]["model"]["primary"]


@pytest.mark.parametrize("env", _ENVS)
def test_no_real_secret_leaked_into_the_repo(env):
    """ГЛАВНАЯ ПРОВЕРКА. Токен гейтвея — секрет; в файлах должна стоять
    подстановка, а не значение. Длинная шестнадцатеричная строка здесь —
    почти наверняка утёкший ключ."""
    raw = (_GW / f"openclaw.{env}.json").read_text(encoding="utf-8")
    assert "${OPENCLAW_GATEWAY_TOKEN}" in raw, "подстановка токена пропала"
    leaks = re.findall(r"\b[a-f0-9]{24,}\b", raw)
    assert not leaks, f"в шаблон {env} утекло похожее на секрет: {len(leaks)} шт."


@pytest.mark.parametrize("env", _ENVS)
def test_model_chain_is_explicit(env):
    """Цепочка моделей — то, ради чего конфиг и заводили в репозиторий:
    её меняют, и изменение должно быть видно в истории."""
    m = _cfg(env)["agents"]["defaults"]["model"]
    assert m.get("primary", "").startswith("openrouter/")
    assert isinstance(m.get("fallbacks"), list) and m["fallbacks"], (
        f"{env}: у гейтвея нет запасных моделей — один отказ провайдера "
        "останавливает работу")


def test_prod_and_dev_differ_only_where_intended():
    """Сравнение стало возможным — им и пользуемся. Разойдись окружения в
    неожиданном месте, это будет видно здесь, а не на живом прогоне."""
    d, p = _cfg("dev"), _cfg("prod")
    assert (d["agents"]["defaults"]["sandbox"]["backend"]
            == p["agents"]["defaults"]["sandbox"]["backend"]), (
        "песочницы разошлись: dev и прод исполняют навыки по-разному")


def _install_sh_code() -> str:
    """Текст install.sh БЕЗ строк-комментариев.

    ⚠️ Сторож, ищущий строку по всему файлу, зеленеет от шапки: режим `--check`
    там описан в примерах запуска. Проверять надо разбор аргументов, а не
    объяснительную записку.
    """
    src = (_GW / "install.sh").read_text(encoding="utf-8")
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def _code_reached_by_check() -> str:
    """ВЕСЬ код install.sh, до которого доходит режим `--check`.

    Это разбор аргументов, тело `compare_template_with_stand` вместе с её
    питон-heredoc'ом и сама ветка — то есть всё, что лежит ЛЕКСИЧЕСКИ до
    закрывающего `fi` ветки. Установка живёт ниже и в сверке не исполняется.
    """
    code = _install_sh_code()
    start = code.index('if [ "$MODE" = "--check" ]; then')
    return code[:code.index("\nfi\n", start)]


# Кто пишет на диск. Список куплен мутацией: прежний сторож знал только
# `cp`/`mv`/`>` и только внутри трёх строк ветки, поэтому `printf x >> "$TARGET"`
# и `sed -i "s/a/b/" "$TARGET"` ВНУТРИ сверяющей функции проходили мимо него
# зелёными — а `--check` на двойнике стенда при этом реально портил конфиг.
_WRITERS = (
    (r"\bcp\s", "cp"),
    (r"\bmv\s", "mv"),
    (r"\btee\b", "tee"),
    (r"\bsed\s+-i\b", "sed -i"),
    (r">>", ">>"),
    (r"(?<![0-9&>])>(?!&)\s*[\"']?\$", "перенаправление в файл"),
    (r"open\([^)]*[\"'][wax]", "open(..., 'w')"),
    (r"\.write_text\(|\.write_bytes\(", "Path.write_text"),
    (r"\bos\.(remove|replace|rename|truncate)\b", "os.replace/remove"),
    (r"\bshutil\.(copy|move)", "shutil.copy/move"),
)


def test_the_installer_has_a_check_mode_that_never_writes():
    """В режиме `--check` не исполняется НИ ОДНОЙ записи на диск.

    ЦЕНА РЕЖИМА (19.08.2026). Установка перезаписывает файл стенда ЦЕЛИКОМ, а
    `sync-tool-allowlist.py` правит `alsoAllow` прямо на VPS, мимо git — он
    только добавляет имена. На 19.08 статически доказано: манифест плагина
    объявляет `fe_news_lookup`, а в `alsoAllow` dev-шаблона его нет, то есть
    установка шаблона молча отняла бы у модели тул, заведённый 17.08 ради
    чтения собственного выпуска новостей. Поэтому сверка обязана быть
    отдельным режимом БЕЗ записи: сначала список расхождений, потом решение.

    ЦЕНА ЭТОЙ РЕДАКЦИИ. Прежний сторож смотрел ТРИ литерала (`cp `, `mv `, `>`)
    внутри лексической ветки `if` — а сверку ветка делегирует функции,
    объявленной выше. Мутация `printf x >> "$TARGET"` плюс `sed -i` в теле
    `compare_template_with_stand` оставляла его ЗЕЛЁНЫМ, хотя запуск `--check`
    на двойнике стенда после неё портил конфиг. Теперь область = весь код, до
    которого режим доходит, а писатели перечислены классами, а не тремя
    строками.

    Проверка сторожа откатом: вписать `sed -i`, `>>`, `tee` или `open(…, "w")`
    куда угодно выше закрывающего `fi` ветки — красное; убрать `exit 0` —
    красное.
    """
    code = _install_sh_code()
    assert "install|--check" in code, (
        "режим --check не объявлен в разборе аргументов — упоминания в шапке "
        "недостаточно, скрипт его не примет")

    reached = _code_reached_by_check()
    for pattern, name in _WRITERS:
        hit = re.search(pattern, reached)
        assert hit is None, (
            f"в коде, исполняемом в режиме --check, есть запись ({name}): "
            f"{reached[max(0, hit.start() - 40):hit.end() + 20]!r}. Режим сверки "
            f"обязан быть безопасным для стенда, иначе им не проверишь перед "
            f"установкой")

    start = code.index('if [ "$MODE" = "--check" ]; then')
    branch = code[start:code.index("\nfi\n", start)]
    assert "exit 0" in branch, (
        "ветка --check не завершает скрипт — сверка провалится в установку и "
        "перезапишет конфиг стенда")

    # Область выше считана ЛЕКСИЧЕСКИ. Функция, объявленная ниже ветки, но
    # вызванная из неё, в область не попадёт — проверяем, что таких нет.
    for fn in re.finditer(r"^([a-z_][a-z0-9_]*)\(\)\s*\{", code, re.M):
        if re.search(rf"^\s*{fn.group(1)}\b", branch, re.M):
            assert fn.start() < start, (
                f"ветка --check зовёт {fn.group(1)}(), объявленную ПОСЛЕ неё — "
                f"её тело не попало в проверенную область")

    for writer in ('cp "$TARGET"', 'mv "$TARGET.tmp"', '> "$TARGET.tmp"'):
        assert code.index(writer) > len(reached), (
            f"{writer} стоит ДО ветки --check — в режиме сверки он отработает")


# ── Режим сверки проверяем ИСПОЛНЕНИЕМ: двойник стенда во временном каталоге ─
#
# Сторож по тексту не видит недостижимости и не видит, ЧТО скрипт напечатал:
# подмени условие ветки на `if false` — литералы на месте, тест зелёный, а
# сверка проваливается в установку. Здесь скрипт запускается по-настоящему.

_STAND_PATH = "TARGET_DIR=/opt/openclaw-dev/config"
_STAND_TOKEN = "живой-токен-стенда"


def _stand_double(tmp: Path, mutate, *, stand_config: bool = True) -> Path:
    """Копия install.sh, у которой dev-стенд — каталог внутри `tmp`.

    `stand_config=False` — чистая машина: конфига на стенде ещё нет, значит
    и токен оттуда не достать.
    """
    src = (_GW / "install.sh").read_text(encoding="utf-8")
    assert src.count(_STAND_PATH) == 1, (
        f"путь dev-стенда в install.sh изменился — двойник собран бы не туда: "
        f"нет ровно одного {_STAND_PATH!r}")
    stand = tmp / "stand"
    stand.mkdir()
    script = tmp / "install.sh"
    script.write_text(src.replace(_STAND_PATH, f"TARGET_DIR={stand}"), encoding="utf-8")
    # Шаблон скрипт ищет рядом с собой — везём настоящий, байт в байт.
    (tmp / "openclaw.dev.json").write_bytes((_GW / "openclaw.dev.json").read_bytes())

    if stand_config:
        cfg = _cfg("dev")
        cfg["gateway"]["auth"]["token"] = _STAND_TOKEN
        mutate(cfg)
        (stand / "openclaw.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return script


def _run_check(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), "dev", "--check"],
                          capture_output=True, text=True)


def _run_install(script: Path) -> subprocess.CompletedProcess:
    """Установка на двойник стенда. Переменную с токеном снимаем: единственный
    его источник здесь — работающий конфиг стенда, как на живой машине."""
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_GATEWAY_TOKEN"}
    return subprocess.run(["bash", str(script), "dev"],
                          capture_output=True, text=True, env=env)


def test_check_mode_notices_a_whole_branch_vanished(tmp_path):
    """Пустой узел обязан быть ВИДИМЫМ отличием.

    `.mcp.servers` = {} не давал ни одного листа, поэтому исчезновение со
    стенда всего узла `mcp` сверка печатала как «стенд совпадает с шаблоном» —
    сторож доставки врал «доставлено» на классе расхождений, который строгое
    сравнение `a != b` до него ловило.

    Проверка сторожа откатом: вернуть `flat()` рекурсию без проверки на пустоту
    — красное.
    """
    script = _stand_double(tmp_path, lambda c: c.pop("mcp"))
    run = _run_check(script)
    assert run.returncode == 5, (
        f"снесённый со стенда узел mcp сверка не заметила:\n{run.stdout}")
    assert ".mcp" in run.stdout


def test_check_mode_survives_a_list_of_objects(tmp_path):
    """`.agents.list` — список СЛОВАРЕЙ, и `set()` на нём падал TypeError.

    Печать расхождений звала `set(x) - set(y)` на любых двух списках. В боевом
    конфиге список объектов есть, и отказ приходился ровно на типичный вид
    ручной правки стенда — завели второго агента: вместо списка расхождений
    трассировка и код выхода 1 вместо задокументированного 5. То есть режим
    ломался на сценарии, ради которого он и заведён.

    Проверка сторожа откатом: вернуть `set()` на любых списках — красное.
    """
    script = _stand_double(tmp_path, lambda c: c["agents"]["list"].append(
        {"id": "hand-made", "name": "hand-made", "default": False}))
    run = _run_check(script)
    assert "Traceback" not in run.stderr, f"сверка упала трассировкой:\n{run.stderr}"
    assert ".agents.list" in run.stdout, (
        f"расхождение по списку объектов не названо:\n{run.stdout}")
    assert run.returncode == 5, f"расхождение обязано давать код 5, дало {run.returncode}"


def test_check_mode_never_prints_a_secret_value(tmp_path):
    """🔒 security.md §1.3: значения секретов в вывод сверки не попадают.

    Маскировался ровно один путь, `gateway.auth.token`; все прочие
    различающиеся листья печатались `repr`'ом — включая
    `.agents.defaults.sandbox.docker.env.*`, то самое место, где живут ключи
    провайдеров. Маска течёт на любом другом имени, а имя у нового секрета
    всегда новое, поэтому маскируем по ИМЕНИ ключа.

    Вход здесь — секрет под именем, которого в конфиге раньше не было.

    Проверка сторожа откатом: вернуть маску по одному пути — красное.
    """
    secret = "sk-ant-ЭТО-УТЕЧКА-В-ЛОГ"
    script = _stand_double(
        tmp_path,
        lambda c: c["agents"]["defaults"]["sandbox"]["docker"]["env"].update(
            {"ANTHROPIC_AUTH_TOKEN": secret}))
    run = _run_check(script)
    assert run.returncode == 5
    assert secret not in run.stdout + run.stderr, (
        f"секрет напечатан в вывод сверки:\n{run.stdout}\n{run.stderr}")
    assert "ANTHROPIC_AUTH_TOKEN" in run.stdout, (
        "расхождение скрыто целиком — маскировать надо значение, а не факт")


def test_check_mode_on_a_stand_double_writes_nothing(tmp_path):
    """Тот же режим, но исполнением: файл стенда цел, .bak/.tmp не появились.

    Сторож по тексту не видит недостижимости: подмени условие ветки на
    `if false; then` — литералы на месте, тест зелёный, а сверка проваливается
    в установку и перезаписывает конфиг стенда. Здесь скрипт запускается
    по-настоящему, поэтому такая подмена краснеет.
    """
    script = _stand_double(tmp_path, lambda c: c["agents"]["list"].append(
        {"id": "hand-made", "name": "hand-made", "default": False}))
    target = tmp_path / "stand" / "openclaw.json"
    before = target.read_bytes()
    run = _run_check(script)
    assert target.read_bytes() == before, "режим сверки изменил конфиг стенда"
    assert [p.name for p in target.parent.iterdir()] == ["openclaw.json"], (
        f"сверка оставила на стенде мусор: "
        f"{sorted(p.name for p in target.parent.iterdir())}")
    assert run.returncode == 5, f"расхождение обязано давать код 5, дало {run.returncode}"


def test_check_mode_is_green_when_the_stand_matches(tmp_path):
    """Положительный контроль: без него проверки выше вакуумны — скрипт,
    который всегда отвечает «не совпало», прошёл бы их."""
    run = _run_check(_stand_double(tmp_path, lambda c: None))
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "совпадает с шаблоном" in run.stdout


def test_the_installer_refuses_without_a_token(tmp_path):
    """Молча поставить конфиг без токена = уронить гейтвей.

    Запускаем установку на чистом двойнике стенда: брать токен неоткуда.
    Текстовый сторож («в скрипте есть слова про токен и `exit 4`») зеленел от
    подмены условия на `if false` — то есть при возврате ровно того дефекта,
    ради которого он написан (мутация проверена 21.08).
    """
    run = _run_install(_stand_double(tmp_path, lambda c: None, stand_config=False))
    assert run.returncode == 4, (
        f"установка без токена не остановлена (код {run.returncode}):\n{run.stdout}")
    assert "Токен гейтвея не найден" in run.stdout, run.stdout
    assert not list((tmp_path / "stand").iterdir()), (
        f"на стенд что-то записано до отказа: "
        f"{sorted(p.name for p in (tmp_path / 'stand').iterdir())}")


def test_the_installer_proves_delivery_and_asks_for_a_restart(tmp_path):
    """Сверка «что поставили == что в репозитории» — И она исполняется.

    Без неё правка снова могла бы «не доехать»: локально всё зелено, в образе
    файлов нет. Строку «✅ конфиг установлен» печатает ТОЛЬКО сверка, поэтому
    её наличие в выводе и есть доказательство, что сверка отработала после
    записи, — снятый вызов краснеет здесь (мутация проверена 21.08).
    """
    script = _stand_double(tmp_path, lambda c: c["agents"]["list"].append(
        {"id": "hand-made", "name": "hand-made", "default": False}))
    run = _run_install(script)
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "конфиг установлен" in run.stdout, (
        f"после записи сверка не отработала:\n{run.stdout}")
    assert "перезапусти гейтвей" in run.stdout, (
        "без напоминания о перезапуске правку сочтут выкаченной, а ядро "
        "читает конфиг на старте")

    installed = json.loads((tmp_path / "stand" / "openclaw.json").read_text(encoding="utf-8"))
    assert installed["gateway"]["auth"]["token"] == _STAND_TOKEN, (
        "живой токен стенда не сохранён — гейтвей после установки не поднимется")
    installed["gateway"]["auth"]["token"] = "<token>"
    template = _cfg("dev")
    template["gateway"]["auth"]["token"] = "<token>"
    assert installed == template, "на стенд доехал не шаблон из репозитория"


# ── Шаблон конфига и манифест плагина держат ОДИН набор тулов ───────────────

_MANIFEST = (Path(__file__).resolve().parents[1] / "deploy" / "gateway-plugins"
             / "fe-tools" / "openclaw.plugin.json")
_ALLOWLIST_SYNC = (Path(__file__).resolve().parents[1] / "deploy" / "dev-gateway"
                   / "sync-tool-allowlist.py")


def _core_tools() -> set[str]:
    """Тулы ядра — из ЕДИНСТВЕННОГО места, где они уже перечислены.

    Список нельзя копировать сюда руками: он станет третьим источником истины
    и разъедется со своими двумя. Импортировать скрипт нельзя (имя с дефисом,
    и он читает sys.argv на верхнем уровне), поэтому достаём литерал разбором.
    """
    import ast

    tree = ast.parse(_ALLOWLIST_SYNC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "CORE_TOOLS" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"в {_ALLOWLIST_SYNC} пропал CORE_TOOLS")


def test_the_dev_template_offers_every_tool_of_the_plugin_manifest():
    """Манифест fe-tools ⊆ alsoAllow dev-шаблона, и лишнего в шаблоне нет.

    ЗАЧЕМ. Тул доходит до модели, только если он в ОБОИХ местах: в плагине
    (схема) и в `alsoAllow` (политика песочницы) — fe-тулы объявлены
    `optional: true`, а optional проходит лишь по прямому имени. Места
    правятся порознь: плагин генерируется в репозиторий, а `alsoAllow` до
    19.08 дописывался скриптом прямо на VPS. Так `fe_news_lookup` и оказался в
    манифесте, но не в шаблоне — то есть `install.sh dev` отнял бы его у
    модели.

    Обратная сторона так же важна: имя, оставшееся в шаблоне после сноса тула,
    выглядит живым разрешением. Поэтому «лишнее» = всё, чего нет ни в
    манифесте, ни среди тулов ядра.

    Проверка сторожа откатом: убрать `fe_news_lookup` из шаблона — красное;
    добавить в манифест имя, которого нет в шаблоне, — красное; вписать в
    шаблон имя снятого тула — красное на второй половине.
    """
    manifest = set(json.loads(_MANIFEST.read_text(encoding="utf-8"))["contracts"]["tools"])
    assert len(manifest) >= 15, f"манифест разобрался в {len(manifest)} имён — читаем не то"
    allow = set(_cfg("dev")["tools"]["sandbox"]["tools"]["alsoAllow"])

    missing = sorted(manifest - allow)
    assert not missing, (
        f"тулы есть в плагине, но не в политике песочницы: {missing}. Модель их "
        f"не увидит, а установка шаблона на стенд ОТНИМЕТ их, если там они уже есть")

    extra = sorted(allow - manifest - _core_tools())
    assert not extra, (
        f"в alsoAllow разрешены имена вне плагина и вне тулов ядра: {extra}. "
        f"Либо тул снесли и забыли здесь, либо кто-то правил шаблон руками")
