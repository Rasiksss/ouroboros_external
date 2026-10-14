"""Thin command-line entrypoint for source and packaged runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict


def _server_command(args: argparse.Namespace) -> int:
    os.environ["OUROBOROS_ENV"] = "production"
    if args.host:
        os.environ["OUROBOROS_SERVER_HOST"] = args.host
    if args.port:
        os.environ["OUROBOROS_SERVER_PORT"] = str(args.port)
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    import server

    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0]]
        return int(server.main())
    finally:
        sys.argv = old_argv


def _keyspace_record_command(args: argparse.Namespace) -> int:
    """Show, or deliberately replace, the object keyspace this deployment agreed on."""
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    # Load keyspace dependencies after configuration, only for this CLI subcommand.
    from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
    from ouroboros.app.multiuser_validation import resolved_keyspace
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    repository = SqlDeploymentKeyspaceRepository(store.engine)
    found = resolved_keyspace()
    if args.replace:
        # Printed from what came back, not from what was asked for: a concurrent write
        # would otherwise be reported as this command's own success.
        stored = repository.replace_deployment_keyspace(found)
        print(f"recorded keyspace is now: {stored.describe()}")
        return 0 if not found.differs_from(stored) else 1
    recorded = repository.get_deployment_keyspace()
    print(f"this process: {found.describe()}")
    if recorded is None:
        # Deliberately not recorded here: this shell may resolve a different backend from
        # the fleet's, and recording it would lock every instance out.
        print("recorded: nothing yet — the first instance to start under the mode records it")
        return 0
    print(f"recorded: {recorded.describe()} (at {recorded.recorded_at})")
    return 0 if not found.differs_from(recorded) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ouroboros")
    subparsers = parser.add_subparsers(dest="command")

    files = subparsers.add_parser("file-catalog-backfill", help="preview legacy file cutover; writes only with --apply")
    files.add_argument("--owner-id", required=True)
    files.add_argument("--agent-id", required=True)
    files.add_argument("--apply", action="store_true")
    files.set_defaults(func=_file_backfill_command)

    server_parser = subparsers.add_parser("server", help="run the Ouroboros web server")
    server_parser.add_argument("--host", default="", help="host/interface to bind")
    server_parser.add_argument("--port", type=int, default=0, help="port to bind")
    server_parser.add_argument(
        "--no-ui", action="store_true", help="accepted for CLI parity; server mode has no desktop UI"
    )
    server_parser.set_defaults(func=_server_command)

    keyspace_parser = subparsers.add_parser(
        "keyspace-record",
        help="show or replace the object keyspace this deployment recorded (multi-user mode)",
    )
    keyspace_parser.add_argument(
        "--replace",
        action="store_true",
        help="record this process's keyspace, overwriting what is stored — a deliberate fleet-wide move",
    )
    keyspace_parser.set_defaults(func=_keyspace_record_command)
    return parser


def _file_backfill_command(args: argparse.Namespace) -> int:
    # Defer storage startup so other subcommands can apply their configuration first.
    from new_core.identity import OwnerScope, SurfaceId
    from ouroboros.observability.aef_tracing import accept_trace_id, trace_scope
    from ouroboros.storage.blob.s3_backend import S3StorageProvider
    from ouroboros.storage.file_backfill import backfill
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.bootstrap import bootstrap_config
    from ouroboros.storage.settings.store import load_settings

    bootstrap_config(force=True)
    runtime_persistence.initialize()
    scope = OwnerScope(owner_id=args.owner_id, agent_id=args.agent_id, surface_id=SurfaceId.BROWSER, chat_id="1")
    provider = S3StorageProvider(load_settings())
    try:
        with trace_scope(accept_trace_id("", source="file_catalog_backfill")):
            result = backfill(scope, provider, apply=args.apply, report=lambda issue: print(issue.model_dump_json()))
    finally:
        provider.close()
    print(json.dumps(asdict(result)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
