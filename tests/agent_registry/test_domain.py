from config_registry.agent_registry.domain import AgentEntity


def test_the_entity_carries_the_record_and_no_kind():
    agent = AgentEntity(
        agent_id="main",
        owner_id="owner-1",
        name="main",
        description="the owner's default agent",
        is_active=True,
    )
    assert agent.agent_id == "main"
    assert not hasattr(agent, "agent_type")
    assert not hasattr(agent, "is_communal")


def test_the_entity_is_frozen():
    agent = AgentEntity(agent_id="main", owner_id="owner-1", name="main", description="", is_active=True)
    try:
        agent.is_active = False
    except AttributeError:
        return
    raise AssertionError("the entity is handed across a boundary and must not be mutable")
