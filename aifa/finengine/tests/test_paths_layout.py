"""Раскладка файлов службы: один поиск на все точки чтения.

ЗАЧЕМ. В репозитории `Path(__file__).parents[N]` указывает туда же, куда и в
контейнере только потому, что каталоги совпадают. В собранном образе это не так:
пакет лежит в `site-packages` или внутри PyInstaller-бандла (распаковывается в
`/tmp/_MEIxxxx` и исчезает при выходе), и любой `parents[N]` ведёт в никуда, а
отказ при этом тихий — сервис поднимается, функция просто не находит файл.

Поэтому тест проверяет не «пути красивые», а поведение: каждая точка чтения
находит файл в раскладке контейнера, даже когда `__file__` лежит в бандле.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

FINENGINE_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture()
def finengine_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пакет импортируется из репозитория, каталог — первым в `sys.path`."""
    monkeypatch.syspath_prepend(str(FINENGINE_SRC))
    for name in [n for n in sys.modules if n == "finengine" or n.startswith("finengine.")]:
        monkeypatch.delitem(sys.modules, name)


def test_repo_layout_is_found(finengine_importable: None) -> None:
    """Разработка: раскладка репозитория видна как есть."""
    paths = importlib.import_module("finengine.paths")

    assert paths.project_root() is not None
    assert (paths.project_root() / "src" / "finengine").is_dir()
    assert paths.find_dir("station-skills") is not None
    assert paths.find_dir("deploy", "certs") is not None
    assert paths.find_package_file("policy/POLICY.md").is_file()


def test_layout_without_project_root_still_resolves(
    finengine_importable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контейнер: `project_root()` пуст, но файлы находятся по `/app/...`.

    Каталог `/app` подменяется на настоящий `aifa/finengine`: проверяем порядок
    поиска и то, что отсутствие корня проекта не превращается в падение.
    """
    paths = importlib.import_module("finengine.paths")
    project = paths.project_root()

    monkeypatch.setattr(paths, "project_root", lambda: None)
    monkeypatch.setattr(paths, "repo_root", lambda: project)

    assert paths.find_dir("station-skills") == project / "station-skills"
    assert paths.find_file("deploy", "tools_whitelist.yaml") is not None


def test_package_file_prefers_external_copy(
    finengine_importable: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Внешняя копия данных старше встроенной: правку без пересборки видно."""
    paths = importlib.import_module("finengine.paths")
    external = tmp_path / "finengine-data"
    (external / "policy").mkdir(parents=True)
    (external / "policy" / "POLICY.md").write_text("внешняя редакция", encoding="utf-8")

    monkeypatch.setattr(paths, "project_root", lambda: None)
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path)

    assert paths.find_package_file("policy/POLICY.md").read_text(encoding="utf-8") == "внешняя редакция"


def test_missing_package_file_names_every_candidate(
    finengine_importable: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Нет файла — ошибка с перечнем, где искали, а не молчаливый `None`."""
    paths = importlib.import_module("finengine.paths")

    monkeypatch.setattr(paths, "project_root", lambda: None)
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path)

    with pytest.raises(FileNotFoundError) as excinfo:
        paths.find_package_file("policy/НЕТ-ТАКОГО.md")
    assert "НЕТ-ТАКОГО.md" in str(excinfo.value)


def test_package_file_found_in_repo_copy_inside_bundle(
    finengine_importable: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Бандл обёртки AEF: проект лежит внутри бандла как `finengine/` целиком.

    `--add-data /app:.` укладывает контекст сборки в корень бандла, и пакет
    оказывается в `<бандл>/finengine`, а данные — в `<бандл>/finengine/src/
    finengine/...` (регресс на `sources.yaml` не найден в под 16.11.2026).
    """
    paths = importlib.import_module("finengine.paths")
    bundle_pkg = tmp_path / "finengine"
    (bundle_pkg / "src" / "finengine" / "policy").mkdir(parents=True)
    (bundle_pkg / "src" / "finengine" / "policy" / "POLICY.md").write_text(
        "копия проекта в бандле", encoding="utf-8"
    )
    (bundle_pkg / "deploy").mkdir()

    monkeypatch.setattr(paths, "PACKAGE_DIR", bundle_pkg)
    monkeypatch.setattr(paths, "project_root", lambda: None)
    monkeypatch.setattr(paths, "repo_root", lambda: bundle_pkg)

    found = paths.find_package_file("policy/POLICY.md")
    assert found.read_text(encoding="utf-8") == "копия проекта в бандле"
    assert found == bundle_pkg / "src" / "finengine" / "policy" / "POLICY.md"


def test_station_shelf_contains_both_roots(finengine_importable: None) -> None:
    """Полка станции: пакет и `station-skills`, иначе станция исполняет пустоту."""
    driver = importlib.import_module("finengine.skills.station_driver")

    roots = dict(driver._shelf_roots())
    assert set(roots) == {"finengine", "aifa_station_skills"}
    assert all(root.is_dir() for root in roots.values())
    assert roots["aifa_station_skills"].name == "station-skills"


def test_shelf_zip_is_not_empty(finengine_importable: None) -> None:
    """Собранная полка содержит файлы обоих каталогов."""
    import io
    import zipfile

    driver = importlib.import_module("finengine.skills.station_driver")
    driver._shelf_cache = None

    with zipfile.ZipFile(io.BytesIO(driver._build_shelf())) as archive:
        names = archive.namelist()

    assert any(name.startswith("finengine/") for name in names)
    assert any(name.startswith("aifa_station_skills/") for name in names)


def test_registry_data_resolves_from_package(finengine_importable: None) -> None:
    """Данные реестра читаются через поиск, а не через `__file__` модуля."""
    paths = importlib.import_module("finengine.paths")

    assert paths.find_package_dir("data/registry_layers").is_dir()
    assert paths.find_package_file("data/metrics.json").is_file()
    assert paths.find_package_file("roles/layouts.json").is_file()


def test_metric_schema_uses_layers_without_legacy_registry(
    finengine_importable: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    """Импорт не требует старого реестра; warning указывает на действующие слои."""
    registry = importlib.import_module("finengine.skills.metric_schema")
    config = importlib.import_module("finengine.config")
    monkeypatch.setattr(config.settings, "registry_dir", str(tmp_path / "missing"))

    manifest = registry.registry_path_for_profile("ifrs_nf_ru")

    assert manifest.name == "manifest.json"
    assert manifest.parent.name == "registry_layers"
    assert manifest.is_file()
    assert str(manifest.parent) in caplog.text


def test_roles_scope_loads_layouts(finengine_importable: None) -> None:
    """`roles/scope.py` находит `layouts.json` при импорте."""
    scope = importlib.import_module("finengine.roles.scope")

    assert scope.LAYOUTS_PATH.is_file()


def test_cert_dir_is_found(finengine_importable: None) -> None:
    """Сертификаты Минцифры: каталог находится без литерала `parents[3]`."""
    tls = importlib.import_module("finengine.net.ru_gov_tls")

    cert_dir = tls._cert_dir()
    assert cert_dir is not None
    assert (cert_dir / "russian_trusted_root_ca.pem").is_file()
