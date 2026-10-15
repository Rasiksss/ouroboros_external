"""A4: профили песочницы для исполнения атомов (build-plan, Фаза 0).

Нерушимое решение №9: выполнение, инициированное моделью, идёт через
ограниченный профиль песочницы, и доступ к сети/файлам/коду не выдаётся
атому по умолчанию. Здесь заведён сам механизм — привязка профиля к
конкретному атому живёт в `AtomRegistry` (Срез 1/3), которого пока нет.

Три профиля фиксированы и живут в `SANDBOX_PROFILES` под своим именем.
Четвёртый, `external_enrichment`, обязан получать список разрешённых
узлов от вызывающего атома — единого списка "на все атомы" не существует,
поэтому это функция-конструктор, а не готовый экземпляр в реестре.
"""
from __future__ import annotations

from enum import Enum

from pydantic import ConfigDict, model_validator

from finengine.contracts.base import Contract, NonEmptyStr


class NetworkAccess(str, Enum):
    NONE = "none"
    ALLOWLIST = "allowlist"
    ANY = "any"


class SandboxProfile(Contract):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)

    name: NonEmptyStr
    network_access: NetworkAccess
    allowed_hosts: tuple[str, ...] = ()
    filesystem_read: bool = False
    filesystem_write_scratch_only: bool = True
    code_execution: bool = False
    rare_explicit_grant: bool = False
    description: NonEmptyStr

    @model_validator(mode="after")
    def _hosts_match_access_mode(self):
        if self.network_access is NetworkAccess.NONE and self.allowed_hosts:
            raise ValueError("network_access=none cannot carry an allowlist")
        if self.network_access is NetworkAccess.ALLOWLIST and not self.allowed_hosts:
            raise ValueError("network_access=allowlist requires at least one allowed host")
        return self

    @model_validator(mode="after")
    def _code_execution_is_never_silent(self):
        if self.code_execution and not self.rare_explicit_grant:
            raise ValueError(
                "code_execution=True requires rare_explicit_grant=True — "
                "free code execution is never a silent default (A4, решение №9)"
            )
        return self


SANDBOX_PROFILES: dict[str, SandboxProfile] = {
    "compute_no_network": SandboxProfile(
        name="compute_no_network",
        network_access=NetworkAccess.NONE,
        filesystem_read=False,
        code_execution=False,
        description="Детерминированные вычисления (формулы, проверки, сравнения). Ни сети, ни чтения файлов.",
    ),
    "document_read": SandboxProfile(
        name="document_read",
        network_access=NetworkAccess.NONE,
        filesystem_read=True,
        code_execution=False,
        description="Извлечение из уже загруженного документа. Чтение файла разрешено, сеть — нет.",
    ),
    "free_code": SandboxProfile(
        name="free_code",
        network_access=NetworkAccess.ANY,
        filesystem_read=True,
        filesystem_write_scratch_only=False,
        code_execution=True,
        rare_explicit_grant=True,
        description="Редкая, явно запрашиваемая возможность выполнить произвольный код. Не выдаётся по умолчанию.",
    ),
}


def external_enrichment(*, allowed_hosts: tuple[str, ...], description: str = "") -> SandboxProfile:
    """Профиль внешнего обогащения: сеть разрешена только на явно перечисленные узлы.

    Список узлов — обязанность вызывающего атома, а не общий реестр: разные
    атомы обогащения ходят к разным источникам (регулятор, биржа, IR-страница
    компании), и общий на всех список нарушил бы принцип минимальных прав.
    """
    return SandboxProfile(
        name="external_enrichment",
        network_access=NetworkAccess.ALLOWLIST,
        allowed_hosts=tuple(allowed_hosts),
        filesystem_read=False,
        code_execution=False,
        description=description or f"Внешнее обогащение, разрешённые узлы: {', '.join(allowed_hosts)}.",
    )


# Профили, сконструированные `external_enrichment(...)` конкретным атомом —
# НЕ четвёртый фиксированный профиль (`test_registry_has_the_three_fixed_
# profiles` держит `SANDBOX_PROFILES` ровно тройкой): у каждого вызова
# `external_enrichment` одно и то же поле `.name == "external_enrichment"`
# независимо от вызывающего атома, поэтому ключом здесь служит СТРОКА,
# которую задаёт САМ атом (`AtomDef.sandbox_profile`), а не `.name` профиля —
# два разных атома с разными `allowed_hosts` иначе столкнулись бы в одном
# ключе. `GraphExecutor._sandbox_profile_for` ищет здесь ровно так же, как
# в `SANDBOX_PROFILES`, просто во ВТОРУЮ очередь.
DYNAMIC_SANDBOX_PROFILES: dict[str, SandboxProfile] = {}


def register_dynamic_profile(key: str, profile: SandboxProfile) -> str:
    """Идемпотентная регистрация: повторный вызов с ТЕМИ ЖЕ `key`/`profile`
    (например, при повторном импорте модуля атома в тестах) не считается
    конфликтом; тот же `key` с ДРУГИМИ настройками — считается, чтобы два
    атома молча не переопределили профиль друг друга."""
    existing = DYNAMIC_SANDBOX_PROFILES.get(key)
    if existing is not None and existing != profile:
        raise ValueError(
            f"динамический профиль песочницы {key!r} уже зарегистрирован с другими настройками"
        )
    DYNAMIC_SANDBOX_PROFILES[key] = profile
    return key


__all__ = [
    "NetworkAccess", "SandboxProfile", "SANDBOX_PROFILES", "external_enrichment",
    "DYNAMIC_SANDBOX_PROFILES", "register_dynamic_profile",
]
