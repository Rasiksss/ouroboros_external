"""Configuration keys removed from the runtime contract."""

RETIRED_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "ASSISTANT_DIALOGS_BOT_GROUP_ID",
        "ASSISTANT_DIALOGS_BOT_TOKEN",
        "HEIMDALL_BASE_URL",
        "HEIMDALL_TOKEN",
        "OUROBOROS_ALLOW_FILE_STORAGE_IN_PRODUCTION",
        "OUROBOROS_LAUNCHER_PATH",
        "OUROBOROS_MANAGED_BY_LAUNCHER",
        "OUROBOROS_MODEL_CODE",
        "OUROBOROS_STORAGE_BACKEND",
        "PULSE_PEOPLE_API_LOG_REQUESTS",
        "PULSE_PEOPLE_BASE_URL",
        "PULSE_PEOPLE_EMPLOYEE_ACTUAL_PATH",
        "PULSE_PEOPLE_EMPLOYEE_COMPETENCE_PATH",
        "PULSE_PEOPLE_MAX_PARALLEL_REQUESTS",
        "PULSE_PEOPLE_MCP_DOCS_PATH",
        "PULSE_PEOPLE_MCP_MODELS_PATH",
        "PULSE_PEOPLE_MCP_QUERY_PATH",
        "PULSE_PEOPLE_TIMEOUT_SEC",
        "PULSE_PEOPLE_TOKEN",
        "PULSE_PEOPLE_TOTAL_TIMEOUT_SEC",
        "USE_LOCAL_CODE",
    }
)

RETIRED_SETTING_PREFIXES: tuple[str, ...] = (
    "ASSISTANT_ASSIGNMENT_",
    "HEIMDALL_",
    "NAVIGATOR_BI_",
    "PULSE_PEOPLE_",
)


def is_retired_setting_key(key: object) -> bool:
    """Return whether a setting belongs to a removed runtime contract."""
    text = str(key)
    return text in RETIRED_SETTING_KEYS or text.startswith(RETIRED_SETTING_PREFIXES)


__all__ = ["RETIRED_SETTING_KEYS", "RETIRED_SETTING_PREFIXES", "is_retired_setting_key"]
