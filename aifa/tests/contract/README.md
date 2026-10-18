# Контрактные тесты

Тесты границ с настоящими объектами по обе стороны (`PRINCIPLES.md` §Б.3). Здесь — граница служба ↔
ouroboros (`CONTRACTS.md` §2): живой ouroboros, модель подставная (`../stub_model/README.md`).

Сейчас: `test_smoke.py` — ход «привет», эхо и финал (C2.2, C2.3); `test_tool_round.py` — ход с
вызовом инструмента (круг 2 видит результат); `test_contract_compose.py` — границы
самого профиля (идёт всегда, без Docker). `ouro_ws.py` — минимальный клиент WS для этих тестов;
настоящий `ouro_client` службы (шаг 08) заменит его.

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
