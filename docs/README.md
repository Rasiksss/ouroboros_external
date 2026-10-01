# Ouroboros documentation

This directory describes the **headless production runtime** in this repository. It is the operational map for engineers deploying and extending the agent today, with explicit notes on what is implemented versus what the solution architecture targets.

## Documents

| File | Audience | Contents |
|------|----------|----------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architects, reviewers, agent context | Current module layout, runtime flows, security boundaries, gap vs target |
| [DEPLOYMENT.md](DEPLOYMENT.md) | SRE / platform | Docker, env configuration, network auth, volumes |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Contributors | Naming, module rules, platform abstraction, governance artifacts |
| [CREATING_SKILLS.md](CREATING_SKILLS.md) | Skill authors | Manifest schema, lifecycle, PluginAPI (extensions) |
| [A2A_DOCKER.md](A2A_DOCKER.md) | Integrators | Agent-to-Agent protocol in containers |

## Related repo artifacts

- `prompts/SYSTEM.md`, `prompts/SAFETY.md` — immutable agent instructions loaded into LLM context
- `tests_headless/` — production-profile and minimal-route guards
