from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_prompts_and_default_identity_do_not_name_specific_humans(tmp_path):
    from ouroboros.storage.core_memory.memory import DEFAULT_IDENTITY

    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    identity = DEFAULT_IDENTITY

    assert "my human" in system
    assert "I learn my human through direct conversation and remembered context" in identity
    assert "Anton" not in system
    assert "Razzhigaev" not in system


def test_the_prompt_defers_the_rewrite_question_to_the_identity_block():
    """Whether identity may be rewritten is a setting, so the prompt must not answer it here."""
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")

    assert "I cannot write or delete it" not in system


def test_live_task_message_marker_uses_my_human_wording():
    from ouroboros.agent.loop_adapters import OwnerInboxAdapter

    marked = OwnerInboxAdapter._owner_marked_content("Please adjust this")

    assert marked == "[Message from my human]: Please adjust this"
    assert "[Owner message during task]" not in marked


def test_public_publishing_requires_explicit_permission():
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    assert "Do not publish, expose, or change external/public-facing systems without explicit permission" in system


def test_prompt_forbids_writing_code():
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    # Top IMPORTANT block carries the full unconditional ban.
    assert "Ты никогда не пишешь код, скрипты, SQL, патчи или конфиги" in system
    # The canonical refusal phrase is repeated at every rule site.
    assert system.count("Я не пишу код и не создаю скрипты") >= 4
    # A dedicated recency section near the end of the prompt.
    assert "## Код и скрипты" in system
    # The closing IMPORTANT key prohibitions include the no-code rule.
    assert "не писать код, скрипты, SQL, патчи или конфиги" in system
    # The forbidden-topics table row is unconditional (no user-request qualifier).
    assert "Написание кода, скриптов, SQL, патчей или конфигов в любой форме и объеме" in system
    # Talking about systems in words stays allowed (no over-blocking).
    assert "Обсуждать системы, данные и подходы словами можно" in system
    # Internal tool parameters are explicitly out of the ban's scope.
    assert "внутренние параметры инструментов" in system


def test_prompts_do_not_name_specific_humans():
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    default_identity = (
        REPO_ROOT / "ouroboros" / "storage" / "core_memory" / "memory" / "domain" / "models.py"
    ).read_text(encoding="utf-8")

    assert "my human" in system
    assert "I learn my human through direct conversation and remembered context" in default_identity
    assert "Anton" not in system
    assert "Razzhigaev" not in system
