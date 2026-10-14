# ADR-0007. Атом графа исполняется `sealed_room.py` в Workstation контура ouroboros

Статус: принято (владелец, решение №7 и «Workstation — свой, в контуре ouroboros»)

## Контекст
Клиент ouroboros (`ouroboros/egress/workstation/client.py`) и `sealed_room.py` AIFA ходят в один
API WorkStation (`/v1/sessions`, `/exec`, `files:*`). Ouroboros посылает `X-Workstation-Protocol: 1`,
`X-Session-Create-Secret` при создании сессии, `X-Trace-Id`, `X-Agent-Id`; `sealed_room.py` — только
`X-Session-Id` и `Authorization`. Комната AIFA собиралась поверх образа `workstation-svc:local` со
слоем пакетов (`deploy/workstation-aifa/Dockerfile` в AIFA-CLAW).

## Решение
- В контуре ouroboros — свой экземпляр Workstation (не `workstation-svc` AIFA). Образ: базовый
  `workstation-svc:local` + слой пакетов комнаты (`numpy`, `pandas`, `openpyxl`, `python-pptx`,
  `python-docx`), без кода finengine и без моста к моделям.
- Ouroboros ходит в него своим клиентом (сессия на владельца + чат + агента), служба — своим
  `sealed_room.py` (разовая комната на запуск).
- `sealed_room.py` дополняется заголовками протокола: `X-Workstation-Protocol: 1`,
  `X-Session-Create-Secret` (если задан), `X-Trace-Id` хода; имя сессии — `aifa-eph-<hex>`.
  Остальное поведение (пределы, порт выхода `out/`, разбор ответа) не меняется.

## Следствия
- Имена сессий не пересекаются: у ouroboros — 24 знака дайджеста (`session.py`, `session_id_for`),
  у службы — префикс `aifa-eph-`.
- Предел `code_exec` ouroboros — 300 с; предел комнаты службы — 60 с (как сейчас).

## Чем проверяется
C5.
