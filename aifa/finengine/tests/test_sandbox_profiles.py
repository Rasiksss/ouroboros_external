"""A4: ограниченный набор профилей песочницы, заводимый до `AtomRegistry`.

Критерий готовности (зафиксирован до кода): существуют ровно четыре вида
профиля — вычисления без сети, чтение документов, внешнее обогащение с
явным списком разрешённых узлов, и свободный код. Нерушимое решение №9
требует, чтобы доступ к сети/файлам/коду не выдавался атому по умолчанию —
здесь это проверяется как инвариант конструктора, а не как соглашение.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from finengine.policy.sandbox_profiles import (
    DYNAMIC_SANDBOX_PROFILES,
    SANDBOX_PROFILES,
    NetworkAccess,
    SandboxProfile,
    external_enrichment,
    register_dynamic_profile,
)


def test_registry_has_the_three_fixed_profiles():
    assert set(SANDBOX_PROFILES) == {"compute_no_network", "document_read", "free_code"}


def test_compute_no_network_forbids_network_filesystem_and_code():
    profile = SANDBOX_PROFILES["compute_no_network"]
    assert profile.network_access is NetworkAccess.NONE
    assert profile.allowed_hosts == ()
    assert profile.filesystem_read is False
    assert profile.code_execution is False


def test_document_read_grants_filesystem_read_but_nothing_else():
    profile = SANDBOX_PROFILES["document_read"]
    assert profile.network_access is NetworkAccess.NONE
    assert profile.filesystem_read is True
    assert profile.code_execution is False


def test_free_code_is_the_only_profile_with_code_execution_and_it_says_so():
    profile = SANDBOX_PROFILES["free_code"]
    assert profile.code_execution is True
    assert profile.rare_explicit_grant is True
    for name, other in SANDBOX_PROFILES.items():
        if name != "free_code":
            assert other.code_execution is False, f"{name} must not silently grant code execution"


def test_external_enrichment_requires_an_explicit_non_empty_allowlist():
    profile = external_enrichment(allowed_hosts=("sec.gov", "cbr.ru"))
    assert profile.network_access is NetworkAccess.ALLOWLIST
    assert profile.allowed_hosts == ("sec.gov", "cbr.ru")
    assert profile.code_execution is False
    with pytest.raises(ValidationError):
        external_enrichment(allowed_hosts=())


def test_none_access_cannot_carry_an_allowlist():
    with pytest.raises(ValidationError):
        SandboxProfile(
            name="broken",
            network_access=NetworkAccess.NONE,
            allowed_hosts=("example.com",),
            description="x",
        )


def test_allowlist_access_cannot_be_empty():
    with pytest.raises(ValidationError):
        SandboxProfile(
            name="broken",
            network_access=NetworkAccess.ALLOWLIST,
            allowed_hosts=(),
            description="x",
        )


def test_code_execution_always_requires_the_rare_explicit_flag():
    with pytest.raises(ValidationError):
        SandboxProfile(
            name="sneaky",
            network_access=NetworkAccess.NONE,
            code_execution=True,
            rare_explicit_grant=False,
            description="x",
        )


def test_profiles_are_immutable():
    profile = SANDBOX_PROFILES["compute_no_network"]
    with pytest.raises(ValidationError, match="frozen"):
        profile.code_execution = True


def test_profile_round_trips_through_json():
    profile = external_enrichment(allowed_hosts=("moex.com",))
    restored = SandboxProfile.model_validate_json(profile.model_dump_json())
    assert restored == profile


def test_dynamic_profiles_never_pollute_the_three_fixed_profiles():
    """`external_enrichment(...)` конструируется ЗА ПРЕДЕЛАМИ `SANDBOX_
    PROFILES` (Срез 5a, `doc.textify`) — регистрация в `DYNAMIC_SANDBOX_
    PROFILES` не расширяет фиксированную тройку."""
    register_dynamic_profile("test-dynamic-isolation", external_enrichment(allowed_hosts=("example.com",)))
    assert set(SANDBOX_PROFILES) == {"compute_no_network", "document_read", "free_code"}
    assert "test-dynamic-isolation" in DYNAMIC_SANDBOX_PROFILES


def test_register_dynamic_profile_is_idempotent_for_the_same_settings():
    profile = external_enrichment(allowed_hosts=("example.com",))
    register_dynamic_profile("test-dynamic-idempotent", profile)
    register_dynamic_profile("test-dynamic-idempotent", profile)  # повтор не должен упасть
    assert DYNAMIC_SANDBOX_PROFILES["test-dynamic-idempotent"] == profile


def test_register_dynamic_profile_rejects_a_conflicting_redefinition():
    """Ключ — строка САМОГО атома, не `.name` профиля (у `external_enrichment`
    оно одно и то же для всех вызовов) — два РАЗНЫХ набора хостов под одним
    ключом означали бы, что один атом молча переопределил профиль другого."""
    register_dynamic_profile("test-dynamic-conflict", external_enrichment(allowed_hosts=("a.example",)))
    with pytest.raises(ValueError):
        register_dynamic_profile("test-dynamic-conflict", external_enrichment(allowed_hosts=("b.example",)))
