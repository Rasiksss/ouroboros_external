# Реализация · шаг 08 · Клиент ouroboros и шлюз хода

Ты — кодовый агент симбиоза AIFA и ouroboros (Claude Code, Sonnet 5.5, усилие High). Эта сессия —
ровно один шаг плана `aifa-symbiosis/plan/README.md`. Рабочая папка —
`C:\Users\magom\Desktop\ouroboros`.

## Прочитай до работы
1. `aifa/CLAUDE.md`; `aifa-symbiosis/plan/PRINCIPLES.md`.
2. `aifa-symbiosis/plan/PROGRESS.md` — запись 07, §4 (P1).
3. ADR-0011, ADR-0014, ADR-0015, ADR-0021, ADR-0024 (п. 1), ADR-0028, ADR-0040, ADR-0041 (п. 3), ADR-0043 (п. 3), ADR-0044 (п. 9).
4. `CONTRACTS.md` §2 целиком (C2.1–C2.8); `FITNESS.md` F6, F12 (а).
5. `ARCHITECTURE.md` §4.3 (компоненты `turn_gateway`, `ouro_client`), §5 (последовательность).
6. ouroboros (только чтение): `transport/browser/ws.py` — `_handle_inbound_chat`, `_frame`,
   `handle_outbound`, `ws_endpoint`; `transport/browser/contracts.py`; `transport/browser/chat.py`.
7. Служба: `run_orchestrator.py` — `RunState.emit`, `start_run`, `cancel_run`,
   `recover_orphan_runs`/`session_lost`.

## Постановка задачи
Сделать в службе два компонента: `ouro_client` — единственное место общения с ouroboros по сети
(WS хода, история) и `turn_gateway` — проведение одного хода через ouroboros с переводом кадров в
события журнала хода. В ручку `POST /chat/turns` пока не подключать.

## Описание
- `ouro_client` (`aifa/finengine/src/finengine/ouro/…` — пакет назови доменно):
  - открывает WS с заголовками `X-Ouroboros-Owner-Id`, `X-Ouroboros-Chat-Id`, `X-Trace-Id`; без
    `X-Ouroboros-Agent-Id` (ADR-0002);
  - отправляет кадр `chat` **только** по контракту §2.2; проверка запрещённых ключей
    (`attachment_ids`, `detached: true`) перед отправкой — исключение (F6, во время исполнения);
  - читает историю `GET /api/chat/history` (пагинация `before_ts`);
  - команда `/stop`;
  - адрес ouroboros, таймауты — поля `Settings` службы.
- `turn_gateway`:
  - один ход = одно соединение (ADR-0021 п. 1); `client_message_id = run_id`;
  - разбор кадров по таблице §2.3; опознание корня §2.4 (из привязки вызова инструмента — интерфейс
    для шага 09: метод «корень хода известен», который вызовет `tool_api`; до шага 09 — из первого
    непрогрессного кадра с `task_id`);
  - финальный ответ → `text_delta` (весь текст) + `text_segment_end` + `done`;
  - прогресс, подзадачи, уведомления → служебные события журнала (не на экран);
  - `confirmation_request` → `error` хода + запись о нарушении конфигурации;
  - крайний срок хода (`OUROBOROS_HARD_TIMEOUT_SEC` + 60 — настройкой службы);
  - обрыв → переподключение → восстановление по истории §2.7 → `session_lost`;
  - «Стоп» → `/stop` → ожидание финала ≤ 30 с → `interrupted`;
  - кадр итога подзадачи хода (непрогрессный, `task_id` не корень, `transport.source_message_id ==
    run_id` — кадры другого хода того же чата не учитываются) — служебное событие и
    точка расширения «подзадача `task_id` завершена, текст» для `aifa_wait(task_id)`
    (ADR-0041 п. 3; потребитель — шаг 16); сверки с каноническим текстом нет (ADR-0040).
- **Тексты для пользователя** при отказах (ouroboros недоступен, отказ рукопожатия, потеря хода) —
  по-русски, без подробностей ouroboros; список — в отчёт; каждый текст закреплён тестом.

## Образ целевого результата
- `turn_gateway` проводит ход через настоящий ouroboros (профиль `contract`, подставная модель) и
  выдаёт в журнал хода ту же последовательность событий, что ожидает экран для ответа без
  инструментов (`text_delta`, `text_segment_end`, `done`).
- Контрактные тесты C2.1–C2.8 зелёные на настоящем ouroboros (C2.3–C2.4 закрывают P1; расхождение
  с ADR-0021 — стоп и предложенная ADR).
- F6 и F12 (а) — в `aifa/tests/architecture` и тестах службы.

## Границы
Не делать: подключение к `POST /chat/turns` (шаг 10), внутренний API инструментов (шаг 09),
каталог файлов (шаг 11), изменение формата событий экрана.

## Правила шага
- Сквозной тест обязательно идёт через три настоящих объекта: `turn_gateway` → ouroboros
  (`contract`) → журнал хода `run_orchestrator` (события читаются его же реплеем).
- Модульные тесты кадров — на подставном WS-сервере; контрактные — только на настоящем ouroboros.
- Ни одна строка с заголовками владельца вне `ouro_client` (F12 а).

## Проверки готовности
| Что | Чем |
|---|---|
| Контракт хода | C2.1–C2.8 |
| Вложения не уходят | F6 |
| Один источник заголовков | F12 (а) |
| Журнал хода | сквозной тест `turn_gateway` ↔ ouroboros ↔ `run_orchestrator` |
| Ворота | `aifa/scripts/check.sh` |

## Конец сессии
По `PRINCIPLES.md` §Д. `PROGRESS.md` §1: шаг 08; P1 — решено или новая формулировка.
