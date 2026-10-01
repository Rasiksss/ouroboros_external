---
name: agent-delegation
description: Delegate work to a configured remote A2A agent with an a2a__* tool, handle long tasks without blocking, and answer a remote agent's clarifying question.
version: 1.1.0
type: instruction
when_to_use: A configured remote agent (an a2a__* tool) can do the work better than this agent — it holds data, permissions, or skills unavailable here — or a remote task started earlier needs to be collected, continued, or cancelled; Russian equivalents include делегируй агенту, спроси у агента, передай другому агенту.
---

# Agent Delegation

Delegation here means one thing: sending a request to another *agent* through an `a2a__*` tool
and using its answer. It is not the same as assigning work to a person.

## When to delegate

Delegate when the remote agent has something this agent does not: access to a system, a
dataset, a permission, or a specialised skill. Its tool description says what it publishes —
read it before deciding.

Do not delegate:

- work an available local tool can do directly;
- a question already answered in the conversation;
- a task whose result cannot be checked here. A remote answer is evidence from a source, not
  a fact; say where it came from when reporting it.

If no configured agent covers the request, say so plainly and continue with local tools.
Never claim a request was delegated when no `a2a__*` tool exists.

## Choosing between agents

Several `a2a__*` tools can be configured at once. Choose in this order and stop at the first
answer:

1. **Capability.** A tool's description is built from the remote agent's card — its name, its
   description, its published skills. Take the agent whose published skills name the work.
2. **Health.** A description saying the agent card could not be read means that agent answered
   nothing when the connection was made: its skills are unknown, so nothing but the request
   itself says whether it can do the work. Prefer an agent whose card was read.
3. **Fallback.** If no agent matches, or the matching one fails, use local tools and say which
   agent was tried. Do not send the same request to a second agent hoping for a better answer:
   the descriptions, not trial and error, say who can do what.

## How to phrase a request

The remote agent receives only `message`. It sees no conversation, no files, no earlier tool
results.

- State the goal and the constraints in one self-contained request.
- Include the identifiers it needs — repository, period, account, document name.
- Say what form the answer should take when the form matters.
- Do not invent details the user did not give. If a detail is missing and the request would
  be ambiguous or risky without it, ask the user first.
- Send identifiers, never secrets. The `message` leaves this process for someone else's agent
  and is logged on their side: no tokens, passwords, API keys, or personal data the task does
  not require. The credential this agent authenticates with is the transport's business and
  belongs in settings, not in a message.

## Long work: submit, record, continue, retrieve

Work expected to take more than a moment goes in this order, and the order is not optional:

1. Call the agent tool with `wait: false`. It returns a `task_id` and returns quickly.
2. **Record the `task_id` with `update_scratchpad` before doing anything else**, together with
   the `agent_id` and what was asked. It is the only handle on that work, and the scratchpad
   is the note that survives context compaction — a reply may be summarised away long before
   the remote agent finishes.
3. Continue with other work — the remote agent runs on its own.
4. Retrieve the result with `a2a_task_status(agent_id, task_id, wait_sec)`. Use `wait_sec: 0`
   to check the state, or a bounded wait to sit on it until it finishes. The wait is clamped
   to the configured tool timeout (`A2A_TOOL_TIMEOUT_SEC`, 300s by default), so a larger
   number buys nothing; for work measured in minutes, check back with `wait_sec: 0` between
   other work rather than holding one long wait.
5. Use `a2a_task_cancel(agent_id, task_id)` when the result is no longer needed.

A call with `wait: true` (the default) is for work that answers in seconds. If such a call
comes back `⚠️ A2A_TIMEOUT`, the task id in that message is still valid: collect it with
`a2a_task_status` rather than sending the request again.

## When the send itself goes unanswered

`⚠️ A2A_SUBMISSION_UNKNOWN` means the request was sent and nothing came back — not that it was
refused. The remote agent may be running the work already, so **never resend it**: a blind
retry is how one request becomes two remote tasks, and the second one is invisible from here.

- With a `task_id` in the message, the message was an answer to an existing task: read that
  task with `a2a_task_status` and continue from what it says.
- Without one, nothing here can name the work. Say the outcome is unknown, do not send the
  same request to that agent or to another one, and continue with what can be done locally.
  An identical retry is blocked automatically, and that block is the correct answer.

## When the remote agent asks a question

A result starting with `⚠️ A2A_INPUT_REQUIRED` is not a failure. The remote task is alive and
waiting, and its `task_id` is in the message.

- If the answer is already known here, call the same agent tool again with that `task_id` and
  the answer as the `message`.
- If only the user can answer, ask the user, then continue the task the same way.
- **Accept at most three clarification rounds with one remote agent for one request.** After
  the third, stop, cancel the task, and tell the user what remains unclear. Two agents can
  clarify each other indefinitely, and each round costs a model call on both sides.

## When it refuses or fails

- `⚠️ A2A_ERROR` naming credentials (`auth-required`) is an operator problem, not a task
  problem. Report it and do not retry: no phrasing of the message will fix a wrong token.
- `⚠️ A2A_ERROR` for an unreachable agent means the agent is down. Report it and fall back to
  local tools if the request can be met that way. Do not retry more than once — if the retry
  fails too, stop calling that agent for this request and say it is unreachable.
- Everything else the far side gets wrong arrives in that same `⚠️ A2A_ERROR` envelope —
  including a malformed or unusable request — so the reason is the text after the colon, and
  reading it is the first step. Reword and resend only when that text points at the request
  itself; never resend the same request unchanged.
- A remote task that ends `failed` or `rejected` has been refused by the other agent, and
  reaches here as an `⚠️ A2A_ERROR` naming that state. Say so, with its reason, rather than
  resending the same request.

## Precedence against assignment-delegation

`assignment-delegation` routes work to **people** through `send_assignment`. This skill routes
work to **agents** through `a2a__*` tools. They never both apply to the same step:

1. If a configured remote agent can complete the request itself, delegate to the agent.
2. If the request needs a human — a decision, a physical action, an approval, anything
   outside a machine's reach — use `send_assignment`, even when a remote agent exists.
3. If both could apply, prefer the agent: it returns a result to this conversation, while an
   assignment leaves the loop and needs a person's time.
4. Never do both for one request. Two channels for one task means the work is done twice.

`send_assignment` comes from the `assignment-delegation` extension skill and exists at runtime
only while that extension is loaded, so a deployment can offer this pack without it. When the
tool is absent, assignment delivery is unavailable: say so, and do not send the request to a
remote agent as a substitute — an agent cannot do the human part.
