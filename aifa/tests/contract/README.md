# Контрактные тесты

Тесты границ с настоящими объектами по обе стороны (`PRINCIPLES.md` §Б.3). Здесь — граница служба ↔
ouroboros (`CONTRACTS.md` §2): живой ouroboros, модель подставная (`../stub_model/README.md`).

Шаг 10: `test_chat_turn_end_to_end.py` — `POST /chat/turns` → ouroboros → `aifa_goal` → конвейер на фикстурном
документе → SSE. Служба в нём — **процесс теста** на `127.0.0.1:18183` (`engine_host.py`: настоящее приложение,
подмены конвейера — `pipeline_fixture.py`), ouroboros-контейнер ходит к ней на хост через
`host.docker.internal`. На Linux-хосте служба должна слушать адрес моста: `AIFA_CONTRACT_ENGINE_BIND=0.0.0.0`.

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
