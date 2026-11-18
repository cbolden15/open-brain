"""Explicit owner-only setup actions; this CLI is separate from the native wire."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from open_brain_engine.engine import LocalEngineContext
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages
from open_brain_engine.engine.saved_item_identity import SavedItemSource

from open_brain.local_data import select_local_root
from open_brain.profile import open_existing_single_user_local
from open_brain.services.local_bootstrap import initialize_local_brain
from open_brain.services.t03_adapters import owner_authority_for_principal
from open_brain_connectors.runtime.saved_item import SavedItemRefusal, identity_url, item_key

from .policy import (
    Policy,
    Refused,
    binding_value,
    extension_origin,
    install,
    lock_setup,
    read,
    save,
)

OWNER_COMMANDS = frozenset({"setup", "sources", "revoke", "install"})


def _owner(profile: LocalEngineContext) -> BrowserOwnerSetup:
    return BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(
            principal_id=profile.owner_actor_id, session_id="browser-owner"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-brain-browser-host")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Bind an installed extension to a local Brain and source.")
    setup.add_argument("--data-dir")
    setup.add_argument("--initialize-brain", action="store_true")
    setup.add_argument("--extension-id", required=True)
    selected = setup.add_mutually_exclusive_group(required=True)
    selected.add_argument("--new-source", action="store_true")
    selected.add_argument("--source", nargs=2, metavar=("CONNECTION_ID", "RESOURCE_ID"))
    setup.add_argument("--recover-url", help="Prove a legacy source with one previously saved URL.")
    setup.add_argument(
        "--operation", action="append", choices=("lookup", "save", "status"), required=True
    )
    setup.add_argument(
        "--tier", action="append", choices=("public", "work", "personal"), required=True
    )
    setup.add_argument("--space", action="append", default=[])
    setup.add_argument("--unassigned", action="store_true")
    setup.add_argument("--workflow-principal", help="Rebind an original saved operation principal.")
    setup.add_argument(
        "--complete-domain",
        action="store_true",
        help="Grant all current and future spaces, including unassigned, in all three tiers.",
    )
    sources = sub.add_parser("sources", help="List saved-item source IDs available to the owner.")
    sources.add_argument("--data-dir")
    sources.add_argument("--offset", type=int, default=0)
    sub.add_parser("revoke", help="Revoke the configured grant in the Brain.")
    register = sub.add_parser(
        "install", help="Register this executable for the configured Chrome extension."
    )
    register.add_argument(
        "--chrome-user-data-dir",
        type=Path,
        help="An absolute Chrome user data directory under HOME.",
    )
    return parser


def _revoke(policy: Policy) -> None:
    profile = open_existing_single_user_local(policy.brain_root)
    try:
        binding = BrowserPages(profile).binding(policy.token)
    except PermissionError as error:
        if str(error) != "browser_unbound":
            raise
        return
    policy.require(binding)
    _owner(profile).revoke(policy.token)


def _configure(args: argparse.Namespace, directory: int) -> dict[str, object]:
    extension_origin(args.extension_id)
    if args.recover_url and not args.source:
        raise ValueError("--recover-url requires --source")
    if args.new_source and args.workflow_principal:
        raise ValueError("--workflow-principal requires the original --source")
    spaces: list[str | None] = [*args.space]
    if args.unassigned:
        spaces.append(None)
    if not spaces:
        raise ValueError("select an allowed --space or --unassigned")
    if args.complete_domain and (
        None not in spaces or set(args.tier) != {"public", "work", "personal"}
    ):
        raise ValueError("--complete-domain requires --unassigned and all three tiers")
    try:
        previous = read(directory)
    except FileNotFoundError:
        previous = None

    selection = select_local_root(data_dir=args.data_dir, environment=os.environ)
    if args.initialize_brain:
        initialize_local_brain(selection)
    profile = open_existing_single_user_local(selection.brain_root)
    owner = _owner(profile)
    source = (
        owner.create_source() if args.new_source else SavedItemSource("saved_item", *args.source)
    )
    if args.recover_url:
        owner.register_legacy(source, item_key(identity_url(args.recover_url)[0]))
    token = owner.bind(
        source,
        tiers=args.tier,
        spaces=spaces,
        operations=args.operation,
        workflow_principal=args.workflow_principal,
        complete_domain=args.complete_domain,
    )
    try:
        binding = BrowserPages(profile).binding(token)
        policy = Policy(args.extension_id, profile.root, token, binding_value(binding))
        # Publish only after the old authority is withdrawn. If this process dies
        # first, the existing policy contains a revoked token and the new token
        # has not been published.
        if previous is not None:
            _revoke(previous)
        save(directory, policy)
    except Exception:
        owner.revoke(token)
        raise
    return {"state": "configured", **policy.binding}


def main(argv: Sequence[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result: dict[str, object]
        if args.command == "sources":
            selection = select_local_root(data_dir=args.data_dir, environment=os.environ)
            owner = _owner(open_existing_single_user_local(selection.brain_root))
            result = {"sources": [source.values() for source in owner.sources(offset=args.offset)]}
        else:
            with lock_setup(create=args.command == "setup") as directory:
                if args.command == "setup":
                    result = _configure(args, directory)
                else:
                    policy = read(directory)
                    if args.command == "revoke":
                        _revoke(policy)
                        result = {"state": "revoked"}
                    else:
                        profile = open_existing_single_user_local(policy.brain_root)
                        policy.require(BrowserPages(profile).binding(policy.token))
                        manifest = install(
                            policy, Path(sys.argv[0]), user_data_dir=args.chrome_user_data_dir
                        )
                        result = {"manifest": str(manifest)}
    except Refused, OSError, RuntimeError, SavedItemRefusal, ValueError:
        print("Browser setup refused. Check the local Brain, source and grant.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
