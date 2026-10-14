# ADR-0028. Сеть и доверие: порт ouroboros только во внутренней сети

Статус: принято (14.10.2026, сессия архитектуры)

## Контекст
С доверенными заголовками любой, кто дотянется до порта ouroboros, станет любым владельцем
(`transport/browser/identity.py`: «nothing in a request is evidence»). Внутренний API службы для
инструментов (`/internal/ouro/v1/*`) меняет `Case`.

## Решение
- Ouroboros, служба, Workstation контура, PostgreSQL и S3 — в одной внутренней сети docker
  compose-проекта dev симбиоза. Порт ouroboros наружу не публикуется (допустим только
  `127.0.0.1:<port>` для `/ready`).
- Внутренний API службы требует `Authorization: Bearer <AIFA_ENGINE_TOKEN>` и не проксируется
  Caddy наружу; общий `finengine-caddy-1` не трогаем.
- Workstation контура требует `WORKSTATION_CREATE_SECRET`; секрет знают ouroboros и служба.

## Чем проверяется
F9 (compose: нет публикации портов ouroboros/Workstation кроме loopback), C3.0 (вызов без токена —
401).
