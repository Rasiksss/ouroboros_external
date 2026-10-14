from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from config_registry.agent_registry.infra.repositories.repo import SqlAgentRegistryRepo
from config_registry.agent_registry.interfaces import IAgentRegistryRepo
from config_registry.user_settings.api.http.a2a_agents import (
    A2aAgentsPayload,
    A2aPersonalTokenClearedPayload,
    A2aPersonalTokenPayload,
    a2a_agents_payload,
)
from config_registry.user_settings.api.http.mcp_servers import (
    McpServersPayload,
    PersonalTokenClearedPayload,
    PersonalTokenPayload,
    mcp_servers_payload,
)
from config_registry.user_settings.api.http.personal_tokens import PERSONAL_TOKEN_MAX_LENGTH, SetPersonalTokenRequest
from config_registry.user_settings.api.http.skills import SkillsPayload, skills_payload
from config_registry.user_settings.infra.cache import settings_cache_key
from config_registry.user_settings.infra.repositories.repo import SqlUserSettingsRepo
from config_registry.user_settings.interfaces import IUserSettingsRepo
from config_registry.user_settings.reader import (
    REASONING_PROGRESS_KEY,
    USER_ENABLED_SKILL_IDS_KEY,
    ResolvedUserSettings,
    UserSettings,
    user_settings_factory,
)
from infra.async_engine import async_engine_factory
from infra.cache import cache_factory
from infra.cache.interface import IAsyncCache
from infra.user_tokens.interface import IUserTokenRepo, UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenRepo
from infra.utils.http.fastapi import is_configured_owner, resolve_owner
from new_core.skills.models import SkillId
from new_core.skills.surface import SkillSurface
from ouroboros.app.egress_auth_composition import EgressAuthComposition
from ouroboros.skills.presentation import SkillPresentation
from ouroboros.storage.settings.schema import (
    FORWARDABLE_HEADER_VALUE_RE,
    A2aRemoteAgentEntry,
    McpServerEntry,
    connectable_a2a_agents,
)
from ouroboros.storage.settings.store import load_settings

OWNER_SETTING_KEYS = (
    "OUROBOROS_USER_ENABLED_MCP_IDS",
    "OUROBOROS_EFFORT_TASK",
    "OUROBOROS_LOCALE",
    "OUROBOROS_MEMORY_DISTILL",
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_FALLBACK",
    "OUROBOROS_MODEL_LIGHT",
    REASONING_PROGRESS_KEY,
    "OUROBOROS_TIMEZONE",
    "OUROBOROS_TOOL_CONFIRMATION_ENABLED",
    USER_ENABLED_SKILL_IDS_KEY,
)

router = APIRouter(tags=["owner-settings"])

_PERSONAL_TOKEN_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": SetPersonalTokenRequest.model_json_schema()}},
    }
}


def get_settings_repo() -> IUserSettingsRepo:
    return SqlUserSettingsRepo(async_engine_factory.engine)


def get_registry() -> IAgentRegistryRepo:
    return SqlAgentRegistryRepo(async_engine_factory.engine)


def get_settings_cache() -> IAsyncCache:
    return cache_factory.async_cache()


def get_settings() -> UserSettings:
    return user_settings_factory.settings()


def get_skill_surface(request: Request) -> SkillSurface:
    return request.app.state.skill_surface


def get_skill_presentations(request: Request) -> Mapping[SkillId, SkillPresentation]:
    return request.app.state.skill_presentations


def get_mcp_servers() -> Sequence[McpServerEntry]:
    if not load_settings().MCP_ENABLED:
        return []
    from security_config.mcp import mcp_servers

    return mcp_servers


def get_a2a_agents() -> Sequence[A2aRemoteAgentEntry]:
    if not load_settings().A2A_EGRESS_ENABLED:
        return []
    from security_config.a2a import a2a_remote_agents

    connectable, _ = connectable_a2a_agents(a2a_remote_agents)
    return connectable


_UNSEALABLE_TOKENS = {
    UserTokenTargetKind.MCP_SERVER: "personal MCP tokens",
    UserTokenTargetKind.A2A_AGENT: "personal A2A tokens",
}


def _user_token_repo(kind: UserTokenTargetKind) -> IUserTokenRepo:
    encryption_key = load_settings().OUROBOROS_CREDENTIALS_ENCRYPTION_KEY
    try:
        return SqlUserTokenRepo(async_engine_factory.engine, encryption_key=encryption_key)
    except ValueError as refused:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_UNSEALABLE_TOKENS[kind]} cannot be sealed: "
            "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY is not 64 bytes of base64",
        ) from refused


def get_user_token_repo() -> IUserTokenRepo:
    return _user_token_repo(UserTokenTargetKind.MCP_SERVER)


def get_a2a_user_token_repo() -> IUserTokenRepo:
    return _user_token_repo(UserTokenTargetKind.A2A_AGENT)


class SettingPayload(BaseModel):
    key: str
    value: bool | str

    overridden: bool


class ClearedPayload(BaseModel):
    key: str
    cleared: bool


class SetOverrideRequest(BaseModel):
    model_config = {"extra": "forbid"}
    value: str


def _known_key(key: str) -> None:
    if key not in OWNER_SETTING_KEYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"no such setting: {key}")


async def _agent_of(owner_id: str, agent_id: str, registry: IAgentRegistryRepo) -> str:
    agent = await registry.get_agent(owner_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such agent")
    return agent.agent_id


def _enabled(servers: Sequence[McpServerEntry]) -> list[McpServerEntry]:
    return [entry for entry in servers if entry.enabled]


def _personal_token_server(servers: Sequence[McpServerEntry], server_id: str) -> None:
    enabled = _enabled(servers)
    if server_id not in {entry.id for entry in enabled}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such MCP server")
    if server_id not in EgressAuthComposition.personal_token_targets(enabled):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"MCP server {server_id} takes no personal token"
        )


def _personal_token_agent(agents: Sequence[A2aRemoteAgentEntry], remote_agent_id: str) -> None:
    agent = next((entry for entry in agents if entry.id == remote_agent_id), None)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such A2A agent")
    if not agent.takes_personal_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"A2A agent {remote_agent_id} takes no personal token"
        )


def _refuse_configured_owner(owner_id: str) -> None:
    if is_configured_owner(owner_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the configured owner holds no personal token: every peer served as that owner would send it",
        )


def _holdable_token(token: str) -> None:
    if len(token) > PERSONAL_TOKEN_MAX_LENGTH or not FORWARDABLE_HEADER_VALUE_RE.fullmatch(token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"the token must be 1 to {PERSONAL_TOKEN_MAX_LENGTH} characters a request header can hold, "
            "with no leading or trailing whitespace",
        )


async def _token_request(request: Request) -> SetPersonalTokenRequest:
    try:
        return SetPersonalTokenRequest.model_validate_json(await request.body())
    except ValidationError as invalid:
        errors = invalid.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{**error, "loc": ("body", *error["loc"])} for error in errors],
        ) from invalid


async def _invalidate(cache: IAsyncCache, owner_id: str, agent_id: str, key: str) -> None:
    try:
        await cache.delete_value(settings_cache_key(owner_id, agent_id, key))
    except Exception as failure:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the row was written but the settings cache could not be invalidated; retry",
        ) from failure


@router.get(
    "/{agent_id}",
    response_model=ResolvedUserSettings,
    summary="The settings in force for one agent, typed as the environment types them",
)
async def read_in_force(
    agent_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    settings: UserSettings = Depends(get_settings),
) -> ResolvedUserSettings:
    resolved = await _agent_of(owner_id, agent_id, registry)
    return await settings.user_settings(owner_id, resolved)


@router.get("/{agent_id}/skills", response_model=SkillsPayload)
async def read_skills(
    agent_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    settings: UserSettings = Depends(get_settings),
    surface: SkillSurface = Depends(get_skill_surface),
    presentations: Mapping[SkillId, SkillPresentation] = Depends(get_skill_presentations),
) -> SkillsPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    selected = await settings.enabled_skill_ids(owner_id, resolved)
    return skills_payload(surface, selected, resolved, presentations)


@router.put("/{agent_id}/{key}", response_model=SettingPayload, summary="Set an override for one agent")
async def set_override(
    agent_id: str,
    key: str,
    body: SetOverrideRequest,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    repo: IUserSettingsRepo = Depends(get_settings_repo),
    cache: IAsyncCache = Depends(get_settings_cache),
) -> SettingPayload:
    _known_key(key)
    resolved = await _agent_of(owner_id, agent_id, registry)
    await repo.set_override(owner_id, resolved, key, body.value)
    await _invalidate(cache, owner_id, resolved, key)
    return SettingPayload(key=key, value=body.value, overridden=True)


@router.delete("/{agent_id}/{key}", response_model=ClearedPayload, summary="Clear an override for one agent")
async def clear_override(
    agent_id: str,
    key: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    repo: IUserSettingsRepo = Depends(get_settings_repo),
    cache: IAsyncCache = Depends(get_settings_cache),
) -> ClearedPayload:
    _known_key(key)
    resolved = await _agent_of(owner_id, agent_id, registry)
    cleared = await repo.clear_override(owner_id, resolved, key)
    await _invalidate(cache, owner_id, resolved, key)
    return ClearedPayload(key=key, cleared=cleared)


@router.get(
    "/{agent_id}/mcp-servers",
    response_model=McpServersPayload,
    summary="The deployment's enabled MCP servers and the personal token each holds for one agent",
)
async def list_mcp_servers(
    agent_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    settings: UserSettings = Depends(get_settings),
    servers: Sequence[McpServerEntry] = Depends(get_mcp_servers),
    tokens: IUserTokenRepo = Depends(get_user_token_repo),
) -> McpServersPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    enabled = _enabled(servers)
    selected = await settings.mcp_user_server_ids(owner_id, resolved)
    return mcp_servers_payload(
        enabled,
        selected=frozenset(selected),
        supported=frozenset(EgressAuthComposition.personal_token_targets(enabled)),
        writable=not is_configured_owner(owner_id),
        expirations=await tokens.expirations(UserTokenTargetKind.MCP_SERVER, owner_id, resolved),
    )


@router.put(
    "/{agent_id}/mcp-servers/{server_id}/token",
    response_model=PersonalTokenPayload,
    summary="Store a personal token for one MCP server and agent until its TTL",
    openapi_extra=_PERSONAL_TOKEN_REQUEST_BODY,
)
async def set_personal_token(
    agent_id: str,
    server_id: str,
    owner_id: str = Depends(resolve_owner),
    body: SetPersonalTokenRequest = Depends(_token_request),
    registry: IAgentRegistryRepo = Depends(get_registry),
    servers: Sequence[McpServerEntry] = Depends(get_mcp_servers),
    tokens: IUserTokenRepo = Depends(get_user_token_repo),
) -> PersonalTokenPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    _personal_token_server(servers, server_id)
    _refuse_configured_owner(owner_id)
    _holdable_token(body.token)
    expires_at = datetime.now(UTC) + timedelta(seconds=body.ttl_sec)
    await tokens.save(
        UserTokenTarget(UserTokenTargetKind.MCP_SERVER, server_id), owner_id, resolved, body.token, expires_at
    )
    return PersonalTokenPayload(server_id=server_id, expires_at=expires_at)


@router.delete(
    "/{agent_id}/mcp-servers/{server_id}/token",
    response_model=PersonalTokenClearedPayload,
    summary="Clear the personal token for one MCP server and agent",
)
async def clear_personal_token(
    agent_id: str,
    server_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    tokens: IUserTokenRepo = Depends(get_user_token_repo),
) -> PersonalTokenClearedPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    cleared = await tokens.clear(UserTokenTarget(UserTokenTargetKind.MCP_SERVER, server_id), owner_id, resolved)
    return PersonalTokenClearedPayload(server_id=server_id, cleared=cleared)


@router.get(
    "/{agent_id}/a2a-agents",
    response_model=A2aAgentsPayload,
    summary="The deployment's connectable A2A agents and the personal token each holds for one agent",
)
async def list_a2a_agents(
    agent_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    agents: Sequence[A2aRemoteAgentEntry] = Depends(get_a2a_agents),
    tokens: IUserTokenRepo = Depends(get_a2a_user_token_repo),
) -> A2aAgentsPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    return a2a_agents_payload(
        agents,
        writable=not is_configured_owner(owner_id),
        expirations=await tokens.expirations(UserTokenTargetKind.A2A_AGENT, owner_id, resolved),
    )


@router.put(
    "/{agent_id}/a2a-agents/{remote_agent_id}/token",
    response_model=A2aPersonalTokenPayload,
    summary="Store a personal token for one A2A agent and agent until its TTL",
    openapi_extra=_PERSONAL_TOKEN_REQUEST_BODY,
)
async def set_a2a_personal_token(
    agent_id: str,
    remote_agent_id: str,
    owner_id: str = Depends(resolve_owner),
    body: SetPersonalTokenRequest = Depends(_token_request),
    registry: IAgentRegistryRepo = Depends(get_registry),
    agents: Sequence[A2aRemoteAgentEntry] = Depends(get_a2a_agents),
    tokens: IUserTokenRepo = Depends(get_a2a_user_token_repo),
) -> A2aPersonalTokenPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    _personal_token_agent(agents, remote_agent_id)
    _refuse_configured_owner(owner_id)
    _holdable_token(body.token)
    expires_at = datetime.now(UTC) + timedelta(seconds=body.ttl_sec)
    await tokens.save(
        UserTokenTarget(UserTokenTargetKind.A2A_AGENT, remote_agent_id), owner_id, resolved, body.token, expires_at
    )
    return A2aPersonalTokenPayload(remote_agent_id=remote_agent_id, expires_at=expires_at)


@router.delete(
    "/{agent_id}/a2a-agents/{remote_agent_id}/token",
    response_model=A2aPersonalTokenClearedPayload,
    summary="Clear the personal token for one A2A agent and agent",
)
async def clear_a2a_personal_token(
    agent_id: str,
    remote_agent_id: str,
    owner_id: str = Depends(resolve_owner),
    registry: IAgentRegistryRepo = Depends(get_registry),
    tokens: IUserTokenRepo = Depends(get_a2a_user_token_repo),
) -> A2aPersonalTokenClearedPayload:
    resolved = await _agent_of(owner_id, agent_id, registry)
    cleared = await tokens.clear(UserTokenTarget(UserTokenTargetKind.A2A_AGENT, remote_agent_id), owner_id, resolved)
    return A2aPersonalTokenClearedPayload(remote_agent_id=remote_agent_id, cleared=cleared)


__all__ = [
    "OWNER_SETTING_KEYS",
    "ClearedPayload",
    "SetOverrideRequest",
    "SettingPayload",
    "get_a2a_agents",
    "get_a2a_user_token_repo",
    "get_mcp_servers",
    "get_registry",
    "get_settings",
    "get_settings_cache",
    "get_settings_repo",
    "get_user_token_repo",
    "router",
]
