## MODIFIED Requirements

### Requirement: No skill that executes code is exposed in the mode

With the mode on, the common skill surface SHALL withhold every skill whose manifest declares a type
this runtime executes — `script`, `extension`, `tool` — except a skill whose canonical id the deployment
names in `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS`. A withheld skill SHALL NOT be loaded or exposed:
it reaches no prompt index, no skill read, no tool provider, and no execution path. Filtering SHALL
happen in the common surface rather than in one provider, so every current or future source inherits
the property without repeating it.

`OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS` is a deployment setting, empty by default. Every entry SHALL be a
canonical `distribution:<skill>` id; an entry of any other source, an empty entry, or a value that is not a
list SHALL be refused when settings load. Naming an id does not create a skill and does not expose a skill
that is absent from the distribution tree.

The filter SHALL name the types it withholds rather than the types it keeps: a package delivered by
an external hub declares a type this runtime does not know, executes nothing, and SHALL be eligible
for the documentation-skill surface like any other non-executable package.

The filter is what makes the mode's support for documentation skills usable on a deployment whose
image also ships executable ones. A deployment SHALL NOT have to mask its skills tree to start,
because masking the tree removes the supported skills together with the unsupported ones.

An executable skill withheld by this filter is withheld for the whole process lifetime, not per task:
the mode and the trusted list are captured in the immutable deployment availability at composition, and no
task-level input or `OUROBOROS_USER_ENABLED_SKILL_IDS` value can widen it. The owner-and-agent allowlist SHALL
be applied only below this deployment ceiling and SHALL expose to one task only the skills it names: a
trusted executable skill reaches a task only when that allowlist also names it.

The agent-runtime composition root SHALL initialize the process-wide skill catalog before the tool
catalog and SHALL give its deployment `SkillSurface` to both skill-backed tool providers. Both
catalogs SHALL remain fixed after composition, and the task path SHALL NOT refresh either one. The
tier SHALL NOT add extension loading or a `SkillBridge`; those remain absent from this tier.

#### Scenario: Only enabled documentation skills in a mixed tree are exposed

- **WHEN** the mode is on, `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS` is empty, and the discovery root holds
  both documentation skills and skills whose type executes code
- **AND** the owner allowlist names one documentation skill
- **THEN** the task surface returns only that permitted documentation skill
- **AND** the executable ones appear in no prompt index, skill read, tool provider, or summary

#### Scenario: A package whose type this runtime does not know can be selected

- **WHEN** the mode is on and the discovery root holds a package whose manifest declares a type
  outside this runtime's own set
- **AND** its canonical id is in the owner allowlist
- **THEN** the common task surface returns it, and the task can read it
- **AND** no execution path accepts it, in this mode or outside it

#### Scenario: Naming an executable skill in the owner allowlist grants nothing

- **WHEN** an owner allowlist names a script, tool, or extension skill in multi-user mode and the
  deployment's trusted list does not name it
- **THEN** the deployment and task surfaces omit it
- **AND** direct lookup answers as if that skill does not exist

#### Scenario: A named distribution extension is visible under the mode

- **WHEN** the mode is on and `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS` names `distribution:<skill>`
  for an extension skill that is eligible
- **THEN** the deployment surface returns it and its tools reach the tool catalog
- **AND** every other executable skill remains omitted

#### Scenario: A trusted executable skill still needs the owner allowlist

- **WHEN** the mode is on, a distribution extension is named as trusted, and the owner allowlist does not
  name it
- **THEN** the task surface omits it and its tools are refused for that task

#### Scenario: A trusted id of another source is refused at settings load

- **WHEN** `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS` names an id whose source is not `distribution`
- **THEN** settings validation fails naming the setting
- **AND** no skill of that source becomes visible

#### Scenario: Skill availability outside the mode is unchanged

- **WHEN** the mode is off
- **THEN** this owner-selection change does not alter the existing executable-skill authorization
- **AND** every skill the single-user deployment previously exposed remains governed by its existing
  deployment and execution paths

### Requirement: The skill-execution tools are not composed in the mode

With multi-user mode on, the repo-defined provider SHALL omit `skill_exec`. Runtime provisioning
and repair tools do not exist. Executable packages from every skill provider SHALL be withheld by
the common skill surface until multi-user sandbox execution is implemented, except a distribution skill the
deployment names in `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS`; naming one adds no execution path for `script`
or `tool` skills.

#### Scenario: A future provider publishes an executable package

- **WHEN** multi-user mode is on and any provider publishes a script, tool, or extension skill that the
  deployment's trusted list does not name
- **THEN** the deployment and task surfaces omit it
- **AND** no provider-specific omission is required for safety
