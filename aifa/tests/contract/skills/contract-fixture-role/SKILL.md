---
name: contract-fixture-role
description: "Фикстура контрактных тестов C4.7: роль подзадачи, не связанная с автором кода. Читай, если задание начинается с «Навык: contract-fixture-role.»"
version: 1.0.0
type: instruction
---

# Роль подзадачи для контрактных тестов

Только профиль `contract`: навык лежит в `aifa/tests/contract/skills/` и в поставку не входит.

Если задание начинается с «Навык: contract-fixture-role.» — ты подзадача:

1. Прочитай состояние хода: `aifa_state`.
2. Запиши попытку в ленту: `aifa_report` со статусом `ok` или `error`.
3. Сдай атом по заданию: `aifa_register_atom`; на отказ исправь код и сдай снова.
4. Закончи работу: `aifa_report` со статусом `done` и итогом в `note`.
