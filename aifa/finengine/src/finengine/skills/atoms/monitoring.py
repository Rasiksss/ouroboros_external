"""Graph executors for Slice-11 capabilities.

These are thin adapters around :mod:`subject_history`; the arithmetic remains
usable and testable without a graph, while every product execution still goes
through ``Target -> ProducerResolver -> GraphExecutor``.
"""
from __future__ import annotations

import json

from finengine.contracts import (
    ApproachPolicy,
    ApproachReport,
    MonitorResult,
    MonitorRunTrace,
    PeerCandidate,
    PeerPolicy,
    SeriesFact,
    SubjectDiff,
    SubjectKey,
    SubjectProfile,
    WatchSubscription,
)
from finengine.skills.atoms.subject_history import (
    approach_profiles,
    decompose_change,
    diff_profiles,
    select_peers,
    series_from_profiles,
)

DEFAULT_APPROACH_POLICY = ApproachPolicy(
    policy_id="monitor-relative-headroom",
    version="1.0.0",
    distance_kind="relative_to_threshold",
    approach_boundary=0.10,
)


def _profiles(ctx) -> tuple[SubjectProfile, SubjectProfile]:
    profiles = sorted(
        (x for x in ctx.dependencies() if isinstance(x, SubjectProfile)),
        key=lambda x: (x.version, x.created_at),
    )
    if len(profiles) != 2:
        raise ValueError(f"monitoring atom requires exactly two compatible SubjectProfile snapshots, got {len(profiles)}")
    if profiles[0].subject_key.value != profiles[1].subject_key.value:
        raise ValueError("monitoring dependencies contain different SubjectKey values")
    return profiles[0], profiles[1]


def calc_series_executor(args, ctx):  # noqa: ARG001
    previous, current = _profiles(ctx)
    return series_from_profiles((previous, current))


def watch_diff_executor(args, ctx):  # noqa: ARG001
    previous, current = _profiles(ctx)
    return [diff_profiles(previous, current)]


def bind_watch_approach(policy: ApproachPolicy = DEFAULT_APPROACH_POLICY):
    def executor(args, ctx):  # noqa: ARG001
        previous, current = _profiles(ctx)
        return [approach_profiles(previous, current, policy)]
    return executor


def monitor_compose_executor(args, ctx):  # noqa: ARG001
    diff = next((x for x in ctx.dependencies() if isinstance(x, SubjectDiff)), None)
    approach = next((x for x in ctx.dependencies() if isinstance(x, ApproachReport)), None)
    series = [x for x in ctx.dependencies() if isinstance(x, SeriesFact)]
    if diff is None or approach is None:
        raise ValueError("monitor.compose requires SubjectDiff and ApproachReport")
    return [MonitorResult(
        subject_key=diff.subject_key,
        diff=diff,
        series=series,
        approaches=approach,
        trace=MonitorRunTrace(
            monitor_plan_run_id=ctx.run_id,
            source_snapshot_ids=[diff.previous_snapshot_id, diff.current_snapshot_id],
            source_plan_run_ids=[diff.previous_plan_run_id, diff.current_plan_run_id],
        ),
    )]


def calc_decompose_executor(args, ctx):  # noqa: ARG001
    raw = json.loads(args.extra.get("decomposition_json") or "{}")
    return [decompose_change(
        concept=str(raw["concept"]),
        target_change=float(raw["target_change"]),
        target_input_evidence_ids=raw.get("target_input_evidence_ids", ()),
        components=raw.get("components", ()),
        methodology_id=str(raw["methodology_id"]),
        methodology_version=str(raw["methodology_version"]),
        required_component_ids=raw.get("required_component_ids", ()),
        tolerance=float(raw.get("tolerance", 1e-9)),
    )]


def ref_peer_executor(args, ctx):  # noqa: ARG001
    raw = json.loads(args.extra.get("peer_request_json") or "{}")
    return [select_peers(
        subject_key=SubjectKey.model_validate(raw["subject_key"]),
        scenario_id=str(raw["scenario_id"]),
        policy=PeerPolicy.model_validate(raw["policy"]),
        candidates=[PeerCandidate.model_validate(x) for x in raw.get("candidates", ())],
    )]


def watch_subscribe_executor(args, ctx):  # noqa: ARG001
    raw = json.loads(args.extra.get("subscription_json") or "{}")
    return [WatchSubscription.model_validate(raw)]


__all__ = [
    "DEFAULT_APPROACH_POLICY", "calc_series_executor", "watch_diff_executor",
    "bind_watch_approach", "monitor_compose_executor", "calc_decompose_executor",
    "ref_peer_executor", "watch_subscribe_executor",
]
