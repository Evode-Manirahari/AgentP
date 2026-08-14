"""Break-glass workspace and API-key administration from the command line.

The HTTP API supports the normal lifecycle: platform administrators create workspaces and
each workspace manages its own keys. This CLI is for operators with direct database access,
including recovery when no usable platform-administrator key remains.

    python -m app.provision create-workspace "Acme Lending"
    python -m app.provision create-key ws_... --name "production"
    python -m app.provision list-keys ws_...
    python -m app.provision rotate-key ws_... key_...
    python -m app.provision revoke-key ws_... key_...
"""

from __future__ import annotations

import argparse
import json
import sys

from app.db import SessionLocal
from app.operations.base import KnownOperationError
from app.schemas import WorkspaceCreate
from app.services.workspaces import (
    create_workspace,
    create_workspace_api_key,
    list_workspace_api_keys,
    revoke_workspace_api_key,
    rotate_workspace_api_key,
)


def _create_workspace(args: argparse.Namespace) -> dict:
    with SessionLocal() as session:
        response = create_workspace(
            session,
            request=WorkspaceCreate(name=args.name, initial_key_name="default"),
        )
    return response.model_dump(mode="json")


def _create_key(args: argparse.Namespace) -> dict:
    with SessionLocal() as session:
        response = create_workspace_api_key(
            session,
            workspace_id=args.workspace_id,
            name=args.name,
        )
    return response.model_dump(mode="json")


def _list_keys(args: argparse.Namespace) -> dict:
    with SessionLocal() as session:
        response = list_workspace_api_keys(
            session,
            workspace_id=args.workspace_id,
        )
    return response.model_dump(mode="json")


def _rotate_key(args: argparse.Namespace) -> dict:
    with SessionLocal() as session:
        response = rotate_workspace_api_key(
            session,
            workspace_id=args.workspace_id,
            api_key_id=args.api_key_id,
            allow_platform_admin=True,
        )
    return response.model_dump(mode="json")


def _revoke_key(args: argparse.Namespace) -> dict:
    with SessionLocal() as session:
        response = revoke_workspace_api_key(
            session,
            workspace_id=args.workspace_id,
            api_key_id=args.api_key_id,
            allow_platform_admin=True,
        )
    return response.model_dump(mode="json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.provision")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_workspace_parser = subparsers.add_parser(
        "create-workspace",
        help="Create a workspace and its first key.",
    )
    create_workspace_parser.add_argument("name")
    create_workspace_parser.set_defaults(handler=_create_workspace)

    create_key = subparsers.add_parser("create-key", help="Mint another workspace key.")
    create_key.add_argument("workspace_id")
    create_key.add_argument("--name", default="api key")
    create_key.set_defaults(handler=_create_key)

    list_keys = subparsers.add_parser("list-keys", help="List a workspace's keys.")
    list_keys.add_argument("workspace_id")
    list_keys.set_defaults(handler=_list_keys)

    rotate_key = subparsers.add_parser("rotate-key", help="Rotate a workspace key.")
    rotate_key.add_argument("workspace_id")
    rotate_key.add_argument("api_key_id")
    rotate_key.set_defaults(handler=_rotate_key)

    revoke_key = subparsers.add_parser("revoke-key", help="Revoke a key.")
    revoke_key.add_argument("workspace_id")
    revoke_key.add_argument("api_key_id")
    revoke_key.set_defaults(handler=_revoke_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(args.handler(args), indent=2))
    except KnownOperationError as exc:
        print(json.dumps(exc.to_dict(), indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
