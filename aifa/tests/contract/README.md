# Контрактные тесты

Тесты границ с настоящими объектами по обе стороны (`PRINCIPLES.md` §Б.3). Здесь — граница служба ↔
ouroboros (`CONTRACTS.md` §2): живой ouroboros, модель подставная (`../stub_model/README.md`).

Шаг 10: `test_chat_turn_end_to_end.py` — `POST /chat/turns` → ouroboros → `aifa_goal` → конвейер на фикстурном
документе → SSE. Служба в нём — **процесс теста** на `127.0.0.1:18183` (`engine_host.py`: настоящее приложение,
подмены конвейера — `pipeline_fixture.py`), ouroboros-контейнер ходит к ней на хост через
`host.docker.internal`. На Linux-хосте служба должна слушать адрес моста: `AIFA_CONTRACT_ENGINE_BIND=0.0.0.0`.

Шаг 13: `test_loop_end_to_end.py` — цикл ведёт ouroboros (`loop_driver=ouroboros`): `aifa_goal` → `НУЖЕН_ЦИКЛ` →
`aifa_doc` → `aifa_move` (сценарий хода → конвейер) → `aifa_finish` → финал; сценарий подставной модели —
`scenarios/loop.json`, модель планировщика службы в нём вызвать нельзя. Сохранение цикла после хода проверяется там же.

Шаг 16: `test_subtasks_end_to_end.py` — корень → `schedule_task` → подзадача по навыку-фикстуре → `aifa_state`,
`aifa_report`, `aifa_register_atom` (отказ, затем `ГОТОВО`), `aifa_report done` → корень получает итог в `aifa_wait(task_id)` →
`aifa_move` с `eph`-атомом → `aifa_finish` (C4.1, C4.3, C4.4, C4.6, C4.7); молчащая подзадача и `cancel_task` (C4.9). Код атома
исполняет `room_stub.py` (локальный интерпретатор вместо Workstation: в профиле Workstation нет). Сценарии —
`scenarios/delegation.json`, `silent_subtask.json`; корень берёт `task_id` из ответа `schedule_task` полем
`arguments_from_last_tool_result` подставной модели. C4.2 (та же сессия Workstation, сокет наружу закрыт) — живой, на стенде
с Workstation; в профиле его нет, тест не написан (`PROGRESS.md` §4).

**Навык-фикстура C4.7.** `skills/contract-fixture-role/SKILL.md` (`type: instruction`) лежит здесь, а не в `skills/`, и
подключается только профилем `contract`: `compose.contract.yml` монтирует каталог в `/app/skills/contract-fixture-role:ro`
(каталог навыков контейнера) и добавляет `distribution:contract-fixture-role` к `OUROBOROS_USER_ENABLED_SKILL_IDS`
(допуск dev + фикстура). `test_contract_compose.py` держит оба значения; служба навыка не знает
(`finengine/tests/architecture/test_service_knows_no_subtask_skill.py`).

Шаг 11: `test_catalog_files.py` — C6.1–C6.4 настоящим `CatalogFiles` службы на каталоге ouroboros;
`test_files_end_to_end.py` — загрузка документа через ручку службы → каталог → распознавание → ход с этим
документом → файл-результат (атом `out.file`) в каталоге → выдача через ручку → история чата → удаление.
Служба в нём с настоящим реестром документов (временный SQLite): `pipeline_fixture.install(real_documents=True)`.

Сейчас: `test_turn_contract.py` — C2.1–C2.8 настоящим `ouro_client` и `turn_gateway` службы
(C2.3–C2.4 закрывают P1); `test_turn_to_journal.py` — сквозной ход `turn_gateway` → ouroboros →
журнал `run_orchestrator`; `test_smoke.py` — ход «привет»; `test_tool_round.py` — ход с вызовом
инструмента (круг 2 видит результат); `test_contract_compose.py` — границы самого профиля (идёт
всегда, без Docker). Общее для тестов хода — `turn_support.py`.

## Запуск

Профиль `contract` поднимается **локально** отдельным compose-проектом `aifa-sym-contract`
(контейнеры dev-контура `aifa-sym` не затрагиваются; на общий dev не выкатывается):

```bash
docker compose -f aifa/deploy/dev/compose.contract.yml --profile contract up -d --build --wait
export AIFA_CONTRACT_OURO_URL=ws://127.0.0.1:18181/ws AIFA_CONTRACT_STUB_URL=http://127.0.0.1:18182
uv run --no-config --frozen --directory aifa/finengine --extra dev pytest ../tests/contract -k smoke
docker compose -f aifa/deploy/dev/compose.contract.yml --profile contract down -v
```

Поднимаются `ouroboros` (образ `aifa-sym-ouroboros:dev`, настройки — `ouroboros.env.example` как
на dev), `ouro-postgres`, `s3` (+ разовый `s3-init`), `stub-model`. Подменено только то, что
перечислено в `ALLOWED_ENVIRONMENT` (`test_contract_compose.py`): адрес и имена моделей и заглушки
секретов. Workstation и службы в профиле нет — тесты с ними живые.

Без `AIFA_CONTRACT_OURO_URL` и `AIFA_CONTRACT_STUB_URL` тесты с меткой `contract` пропускаются
(так их видит `aifa/scripts/check.sh`); если адреса заданы, а стенд не отвечает, тест падает.

## Если что-то не так

- `docker logs aifa-sym-contract-stub-model-1` — по строке на запрос: номер, модель, круг, правило.
- `curl http://127.0.0.1:18182/_journal` — что модель увидела и какое правило сработало.
- Данные живут в томах `aifa-sym-contract-*`; чистый стенд — `down -v` и новый `up`.
