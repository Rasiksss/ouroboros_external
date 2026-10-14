"""A2.1.8: атомы с сетевым доступом, свободным кодом и без сети запускаются
только в разрешённых им профилях песочницы; намеренное нарушение
блокируется ДО исполнения — `executor` не вызывается ни разу."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor, validate_sandbox_dispatch, SandboxViolation
import pytest


def test_deterministic_atom_under_free_code_is_rejected_before_execution(atom, target, build_plan):
    calls = {"n": 0}

    def suspicious_executor(args, ctx):  # noqa: ARG001
        calls["n"] += 1
        return ()

    # Нарушение внесено НАРОЧНО: детерминированный расчёт, но объявлен под
    # профилем со свободным кодом и сетью — решение №9 запрещает это по умолчанию.
    registry = AtomRegistry([
        atom("calc.suspicious", ("X",), execution_kind="deterministic",
             sandbox_profile="free_code", executor=suspicious_executor),
    ])
    result, node_args = build_plan(registry, target("X"))

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    assert calls["n"] == 0, "executor не имел права вызываться — нарушение обязано ловиться до диспетчеризации"
    assert exec_result.node_runs[result.graph.root].status == "blocked"
    assert "детерминированный" in (exec_result.node_runs[result.graph.root].error or "")


def test_io_atom_under_no_network_is_rejected(atom, target, build_plan):
    registry = AtomRegistry([
        atom("fetch.report", ("URL_DOC",), execution_kind="io", sandbox_profile="compute_no_network"),
    ])
    result, node_args = build_plan(registry, target("URL_DOC"))

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    assert exec_result.node_runs[result.graph.root].status == "blocked"


def test_matched_profiles_pass_validation_directly():
    """Положительный контроль: когда требования и профиль реально совпадают,
    `validate_sandbox_dispatch` не поднимает `SandboxViolation`."""
    from finengine.skills.spec.atom_registry import AtomDef

    deterministic = AtomDef(
        name="calc.ok", version="1.0.0", produces=("X",), sandbox_profile="compute_no_network",
        execution_kind="deterministic", cache_policy="run_scoped", cost_class="free", executor=lambda *_: (),
    )
    validate_sandbox_dispatch(deterministic)  # не должно поднять исключение

    io_atom = AtomDef(
        name="fetch.ok", version="1.0.0", produces=("Y",), sandbox_profile="free_code",
        execution_kind="io", cache_policy="none", cost_class="cheap", executor=lambda *_: (),
    )
    validate_sandbox_dispatch(io_atom)


def test_unknown_sandbox_profile_name_is_rejected():
    from finengine.skills.spec.atom_registry import AtomDef

    bogus = AtomDef(
        name="calc.bogus", version="1.0.0", produces=("X",), sandbox_profile="does_not_exist",
        execution_kind="deterministic", cache_policy="run_scoped", cost_class="free", executor=lambda *_: (),
    )
    with pytest.raises(SandboxViolation):
        validate_sandbox_dispatch(bogus)


# --- execution_kind="generated" (гибридный планировщик, часть 2): код, написанный моделью ---
def _generated_atom(profile: str, calls: dict | None = None):
    from finengine.skills.spec.atom_registry import AtomDef

    def executor(args, ctx):  # noqa: ARG001
        if calls is not None:
            calls["n"] += 1
        return ()

    return AtomDef(
        name="eph.generated", version="1.0.0", produces=("dyn:x",), sandbox_profile=profile,
        execution_kind="generated", cache_policy="run_scoped", cost_class="cheap", executor=executor,
    )


def _room_profile(name: str, *, network, hosts: tuple[str, ...] = (), code: bool = True) -> str:
    from finengine.policy.sandbox_profiles import SandboxProfile, register_dynamic_profile

    return register_dynamic_profile(name, SandboxProfile(
        name=name, network_access=network, allowed_hosts=hosts,
        code_execution=code, rare_explicit_grant=code, description="тестовый профиль комнаты",
    ))


def test_generated_atom_under_code_execution_without_network_passes():
    from finengine.policy.sandbox_profiles import NetworkAccess

    profile = _room_profile("test_room_ok", network=NetworkAccess.NONE)
    assert validate_sandbox_dispatch(_generated_atom(profile)).code_execution


def test_generated_atom_under_profile_without_code_execution_is_rejected():
    with pytest.raises(SandboxViolation, match="сгенерированный код"):
        validate_sandbox_dispatch(_generated_atom("compute_no_network"))


@pytest.mark.parametrize("profile_name", ["free_code", "test_room_with_network"])
def test_generated_atom_with_network_is_rejected_before_execution(profile_name):
    from finengine.policy.sandbox_profiles import NetworkAccess

    if profile_name == "test_room_with_network":
        _room_profile(profile_name, network=NetworkAccess.ALLOWLIST, hosts=("example.com",))
    calls = {"n": 0}
    atom = _generated_atom(profile_name, calls)

    with pytest.raises(SandboxViolation, match="не давать сети"):
        validate_sandbox_dispatch(atom)
    assert calls["n"] == 0


def test_generated_atom_is_not_retried(atom, target, build_plan):
    """Тот же код на тех же входах упадёт так же: чинит автор, а не повтор."""
    from finengine.policy.sandbox_profiles import NetworkAccess
    from finengine.skills.spec.graph_executor import ExecutionBudget

    calls = {"n": 0}
    profile = _room_profile("test_room_retry", network=NetworkAccess.NONE)

    def failing(args, ctx):  # noqa: ARG001
        calls["n"] += 1
        raise RuntimeError("код упал")

    registry = AtomRegistry([
        atom("eph.generated", ("X",), execution_kind="generated", sandbox_profile=profile,
             executor=failing, cost_class="cheap"),
    ])
    result, node_args = build_plan(registry, target("X"))

    exec_result = GraphExecutor(registry=registry, budget=ExecutionBudget(max_retries=3)).run(
        result.graph, node_args, run_id="run-1")

    assert exec_result.node_runs[result.graph.root].status == "failed"
    assert calls["n"] == 1
