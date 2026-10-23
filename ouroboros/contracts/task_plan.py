"""Root-owned plan proposals and recorded acceptance decisions."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(PlanModel):
    id: Identifier
    description: Text
    source: Text


class Assumption(PlanModel):
    id: Identifier
    description: Text
    reason: Text


class PlanInput(PlanModel):
    source: Literal["artifact", "step"]
    ref: Text
    description: Text


class Execution(PlanModel):
    kind: Literal["scenario", "atom", "subtask", "root"]
    instruction: Text


class Criterion(PlanModel):
    id: Identifier
    description: Text
    verification_method: Text


def unique_ids(values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise ValueError("duplicate identifiers")


class PlanStep(PlanModel):
    id: Identifier
    objective: Text
    requirement_ids: list[Identifier] = Field(min_length=1, max_length=100)
    depends_on: list[Identifier] = Field(default_factory=list, max_length=100)
    inputs: list[PlanInput] = Field(default_factory=list, max_length=100)
    execution: Execution
    expected_outputs: list[Text] = Field(min_length=1, max_length=100)
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def references(self) -> Self:
        unique_ids(self.depends_on)
        unique_ids(self.requirement_ids)
        unique_ids([item.id for item in self.acceptance_criteria])
        if any(item.source == "step" and item.ref not in self.depends_on for item in self.inputs):
            raise ValueError("step inputs must name a declared dependency")
        return self


class PlanProposal(PlanModel):
    schema_version: Literal["1"] = "1"
    base_revision: int = Field(ge=0)
    requirements: list[Requirement] = Field(min_length=1, max_length=100)
    assumptions: list[Assumption] = Field(default_factory=list, max_length=100)
    questions: list[Text] = Field(default_factory=list, max_length=100)
    steps: list[PlanStep] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def graph(self) -> Self:
        unique_ids([item.id for item in self.requirements])
        unique_ids([item.id for item in self.assumptions])
        unique_ids([item.id for item in self.steps])
        requirements = {item.id for item in self.requirements}
        steps = {item.id for item in self.steps}
        covered: set[str] = set()
        pending = {}
        for step in self.steps:
            if not set(step.requirement_ids) <= requirements or not set(step.depends_on) <= steps:
                raise ValueError("unknown requirement or dependency")
            covered.update(step.requirement_ids)
            pending[step.id] = set(step.depends_on)
        if covered != requirements:
            raise ValueError("every requirement must be covered by a step")
        while pending:
            ready = {key for key, dependencies in pending.items() if not dependencies}
            if not ready:
                raise ValueError("cyclic step dependencies")
            pending = {key: dependencies - ready for key, dependencies in pending.items() if key not in ready}
        return self


class ExpectedVersion(PlanModel):
    expected_version: int = Field(ge=0)


class AcceptPlan(ExpectedVersion):
    proposal_task_id: Text
    reason: Text


StepStatus = Literal["pending", "running", "awaiting_review", "accepted", "blocked", "failed"]


class StepUpdate(ExpectedVersion):
    step_id: Identifier
    status: Literal["running", "awaiting_review", "blocked", "failed"]
    attempt_id: Text
    result_refs: list[Text] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=12000)


class CriterionCheck(PlanModel):
    criterion_id: Identifier
    verdict: Literal["pass", "fail", "inconclusive"]
    observation_refs: list[Text] = Field(min_length=1, max_length=100)
    note: Text


class StepReview(ExpectedVersion):
    step_id: Identifier
    attempt_id: Text
    result_refs: list[Text] = Field(min_length=1, max_length=100)
    checks: list[CriterionCheck] = Field(min_length=1, max_length=100)
    decision: Literal["accept", "revise", "blocked"]
    explanation: Text


class AcceptStep(ExpectedVersion):
    """Accept a step by the result its executor returned: one write instead of `step` plus `review`."""

    step_id: Identifier
    attempt_id: str = Field(default="", max_length=200)
    explanation: str = Field(default="", max_length=12000)


class FinishPlan(ExpectedVersion):
    status: Literal["complete", "partial"]
    reason: Text


class StepProgress(PlanModel):
    status: StepStatus = "pending"
    attempt_id: str = ""
    result_refs: list[str] = Field(default_factory=list)
    reason: str = ""


class ExecutionBinding(PlanModel):
    revision: int = Field(ge=1, strict=True)
    step_id: Identifier
    attempt_id: Text


class PlanExecution(ExecutionBinding):
    call_id: Text
    tool_name: Text
    status: Literal["running", "returned", "failed"] = "running"
    child_task_id: str = ""
    child_result_ref: str = ""
    result_ref: str = ""


class AcceptedRevision(PlanModel):
    revision: int
    proposal_task_id: str
    proposal: PlanProposal
    reason: str


class RecordedReview(PlanModel):
    revision: int
    review: StepReview


class StepEvent(PlanModel):
    revision: int
    update: StepUpdate


class PlanState(PlanModel):
    version: int = 0
    revision: int = 0
    status: Literal["active", "complete", "partial"] = "active"
    reason: str = ""
    revisions: list[AcceptedRevision] = Field(default_factory=list)
    steps: dict[str, StepProgress] = Field(default_factory=dict)
    reviews: list[RecordedReview] = Field(default_factory=list)
    events: list[StepEvent] = Field(default_factory=list)
    calls: list[PlanExecution] = Field(default_factory=list)
    closing_call_id: str = ""

    @property
    def proposal(self) -> PlanProposal:
        if not self.revisions:
            raise ValueError("no accepted plan")
        return self.revisions[-1].proposal

    def accept(self, proposal: PlanProposal, command: AcceptPlan) -> None:
        self.ensure_idle()
        if any(step.status in {"running", "awaiting_review"} for step in self.steps.values()):
            raise ValueError("resolve running attempts and review their results before replacing the plan")
        if proposal.base_revision != self.revision:
            raise ValueError("stale base_revision; read the current plan and request a revised proposal")
        if proposal.questions:
            raise ValueError("resolve planning questions before accepting the proposal")
        previous = {step.id: step for step in self.proposal.steps} if self.revisions else {}
        retained: dict[str, StepProgress] = {}
        pending = list(proposal.steps)
        while pending:
            ready = [step for step in pending if all(dep in retained for dep in step.depends_on)]
            for step in ready:
                old = self.steps.get(step.id)
                unchanged = previous.get(step.id) == step and all(
                    retained[dep].status == "accepted" for dep in step.depends_on
                )
                requirements_unchanged = not self.revisions or self.proposal.requirements == proposal.requirements
                retained[step.id] = (
                    old if old and old.status == "accepted" and unchanged and requirements_unchanged else StepProgress()
                )
            pending = [step for step in pending if step not in ready]
        self.revision += 1
        self.revisions.append(
            AcceptedRevision(
                revision=self.revision,
                proposal_task_id=command.proposal_task_id,
                proposal=proposal,
                reason=command.reason,
            )
        )
        self.steps = retained
        self.status, self.reason = "active", ""

    def update_step(self, command: StepUpdate) -> None:
        self._active()
        step = next((step for step in self.proposal.steps if step.id == command.step_id), None)
        if step is None:
            raise ValueError("unknown step")
        old = self.steps[step.id]
        self.ensure_idle(step.id)
        if old.status == "accepted":
            raise ValueError("accepted step changes require a new plan revision")
        if command.status == "running":
            if old.status not in {"pending", "failed", "blocked"} or command.attempt_id == old.attempt_id:
                raise ValueError("a new attempt is required for a pending, failed or blocked step")
            if any(self.steps[dep].status != "accepted" for dep in step.depends_on):
                raise ValueError("step dependencies have not been accepted by the root")
        elif old.status != "running" or command.attempt_id != old.attempt_id:
            raise ValueError("step update must match the running attempt")
        if command.status == "awaiting_review" and not command.result_refs:
            raise ValueError("review requires result references with versions or content hashes")
        if command.status in {"failed", "blocked"} and not command.reason.strip():
            raise ValueError("failed or blocked step needs a reason")
        if command.status == "awaiting_review":
            receipts = [
                call.child_result_ref or call.result_ref
                for call in self.calls
                if call.revision == self.revision and call.step_id == step.id and call.attempt_id == command.attempt_id
            ]
            command = command.model_copy(update={"result_refs": list(dict.fromkeys([*command.result_refs, *receipts]))})
        self.steps[step.id] = StepProgress(
            status=command.status, attempt_id=command.attempt_id, result_refs=command.result_refs, reason=command.reason
        )
        self.events.append(StepEvent(revision=self.revision, update=command))

    def review(self, command: StepReview) -> None:
        self._active()
        progress = self.steps.get(command.step_id)
        if progress is None or progress.status != "awaiting_review" or progress.attempt_id != command.attempt_id:
            raise ValueError("review must name the current awaiting_review attempt")
        if command.result_refs != progress.result_refs:
            raise ValueError("review must refer to the exact returned result versions")
        step = next(step for step in self.proposal.steps if step.id == command.step_id)
        unique_ids([check.criterion_id for check in command.checks])
        if {check.criterion_id for check in command.checks} != {criterion.id for criterion in step.acceptance_criteria}:
            raise ValueError("review must cover every acceptance criterion")
        if command.decision == "accept" and any(check.verdict != "pass" for check in command.checks):
            raise ValueError("failed or inconclusive criteria cannot be accepted")
        progress.status = (
            "accepted" if command.decision == "accept" else "failed" if command.decision == "revise" else "blocked"
        )
        progress.reason = command.explanation
        self.reviews.append(RecordedReview(revision=self.revision, review=command))

    def accept_step(self, command: AcceptStep) -> None:
        """Take the returned result of the running attempt as the observation of every criterion.

        Executor calls of the attempt must have returned: a failed call or no call at all is not a result.
        """
        self._active()
        progress = self.steps.get(command.step_id)
        step = next((step for step in self.proposal.steps if step.id == command.step_id), None)
        if progress is None or step is None:
            raise ValueError("unknown step")
        attempt_id = command.attempt_id or progress.attempt_id
        if progress.status == "running":
            calls = [
                call
                for call in self.calls
                if call.revision == self.revision and call.step_id == step.id and call.attempt_id == attempt_id
            ]
            if any(call.status == "failed" for call in calls):
                raise ValueError("an executor call of this attempt failed; review it explicitly or start a new attempt")
            refs = [call.child_result_ref or call.result_ref for call in calls if call.status == "returned"]
            if not any(refs):
                raise ValueError("no returned executor result to accept; run the step first")
            self.update_step(
                StepUpdate(
                    expected_version=self.version,
                    step_id=step.id,
                    status="awaiting_review",
                    attempt_id=attempt_id,
                    result_refs=[ref for ref in refs if ref],
                )
            )
            progress = self.steps[step.id]
        if progress.status != "awaiting_review" or progress.attempt_id != attempt_id:
            raise ValueError("accept_step needs a running or awaiting_review attempt")
        refs = list(progress.result_refs)
        self.review(
            StepReview(
                expected_version=self.version,
                step_id=step.id,
                attempt_id=attempt_id,
                result_refs=refs,
                checks=[
                    CriterionCheck(
                        criterion_id=criterion.id,
                        verdict="pass",
                        observation_refs=refs,
                        note="Принято по результату исполнителя",
                    )
                    for criterion in step.acceptance_criteria
                ],
                decision="accept",
                explanation=command.explanation or "Принято по результату исполнителя",
            )
        )

    def finish(self, command: FinishPlan) -> None:
        self._active()
        self.ensure_idle()
        if command.status == "complete" and any(step.status != "accepted" for step in self.steps.values()):
            raise ValueError("complete requires root acceptance of every step")
        if any(step.status == "running" for step in self.steps.values()):
            raise ValueError("record the outcome of running steps before finishing")
        self.status, self.reason = command.status, command.reason

    def ensure_idle(self, step_id: str = "") -> None:
        if self.closing_call_id:
            raise ValueError("a lifecycle close is still executing")
        if any(call.status == "running" and (not step_id or call.step_id == step_id) for call in self.calls):
            raise ValueError("record the outcome of active executor calls first")

    def begin_execution(self, binding: ExecutionBinding, call_id: str, tool_name: str) -> None:
        self._active()
        if self.closing_call_id:
            raise ValueError("a lifecycle close is still executing")
        if binding.revision != self.revision:
            raise ValueError("stale execution revision")
        if any(call.call_id == call_id for call in self.calls):
            raise ValueError("executor call already recorded; inspect its result instead of replaying it")
        step = next((step for step in self.proposal.steps if step.id == binding.step_id), None)
        if step is None:
            raise ValueError("unknown execution step")
        progress = self.steps[step.id]
        if progress.status != "running":
            self.update_step(
                StepUpdate(
                    expected_version=self.version, step_id=step.id, status="running", attempt_id=binding.attempt_id
                )
            )
        elif progress.attempt_id != binding.attempt_id:
            raise ValueError("executor must name the current running attempt")
        self.ensure_idle(step.id)
        self.calls.append(PlanExecution(**binding.model_dump(), call_id=call_id, tool_name=tool_name))

    def execution(self, call_id: str) -> PlanExecution:
        call = next((call for call in self.calls if call.call_id == call_id), None)
        if call is None:
            raise ValueError("unknown executor call")
        return call

    def _active(self) -> None:
        if not self.revisions or self.status != "active":
            raise ValueError("an active accepted plan is required")
