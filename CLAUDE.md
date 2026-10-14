@AGENTS.md

## Claude Code

- `.claude/rules` points to `.rules/` for path-scoped loading. Use `Read` for source files;
  shell reads do not establish that the matching rules were delivered. If their content is absent,
  run the resolver and read the rule files explicitly using `AGENTS.md` §Edit Map before editing.
- Definition of Done requires an independent review: `/code-review` on the diff, in a clean
  context. The session that wrote the code cannot be the one that signs it off.
- Use plan mode for changes under `ouroboros/policy/`, `security_config/`, or `new_core/`.
