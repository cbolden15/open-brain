from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_public_engine_opens_and_exercises_phase1_without_app_or_connector_modules(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parents[2] / "packages" / "engine" / "src"
    root = tmp_path / "engine-root"
    program = f"""
import json
import os
import sys
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, {str(source)!r})
from open_brain_engine.engine import (
    LocalEngineContext,
    ProposalDraft,
    ProviderMode,
    TextPayload,
    open_local_engine,
)

root = Path({str(root)!r})
root.mkdir(mode=0o700)
(root / '.open-brain').mkdir(mode=0o700)
metadata = root.stat()
profile = LocalEngineContext(
    root=root,
    root_identity=(metadata.st_dev, metadata.st_ino),
    tenant_id='tenant_123e4567-e89b-42d3-a456-426614174000',
    owner_actor_id='actor_123e4567-e89b-42d3-a456-426614174001',
    owner_role_claim=MappingProxyType({{
        'role_claim_id': 'role_claim_123e4567-e89b-42d3-a456-426614174003',
        'tenant_id': 'tenant_123e4567-e89b-42d3-a456-426614174000',
        'actor_id': 'actor_123e4567-e89b-42d3-a456-426614174001',
        'role_id': 'role_123e4567-e89b-42d3-a456-426614174002',
        'capabilities': ('canonical.publish', 'capture.accept', 'space.write'),
    }}),
    provider_mode=ProviderMode.NONE,
    starter_spaces=(),
)
tasks = open_local_engine(profile)
space = tasks.inbox.create_space('Isolation', delivery_id='isolation.space')
capture = tasks.capture.accept(
    TextPayload('isolation searchable text'),
    delivery_id='isolation.capture',
    space_id=space.space_id,
)
proposals = tasks.review.propose(
    capture.capture_id,
    (ProposalDraft('Isolation', 'isolation review text'),),
    delivery_id='isolation.propose',
)
result = tasks.retrieval.search('isolation searchable')[0]
rebuild = tasks.portability.rebuild_index()
print(json.dumps({{
    'capture_id': capture.capture_id,
    'proposal_id': proposals[0].proposal_id,
    'result_id': result.result_id,
    'portability': rebuild.status,
    'modules': sorted(name for name in sys.modules if name.startswith('open_brain')),
}}))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["capture_id"].startswith("capture_")
    assert result["proposal_id"].startswith("proposal_")
    assert result["result_id"] == result["capture_id"]
    assert result["portability"] == "rebuilt"
    modules = set(result["modules"])
    forbidden_prefixes = (
        "open_brain.cli",
        "open_brain.config",
        "open_brain.dev",
        "tools.open_brain_dev",
        "open_brain_legacy",
        "open_brain_engine.ledger",
        "open_brain_engine.protocol",
        "open_brain_engine.portability.secure_node",
        "open_brain.integrations",
        "open_brain.migrate",
        "open_brain.operations",
        "open_brain.parity",
        "open_brain.production",
        "open_brain.profile",
        "open_brain.release",
        "open_brain.services",
        "open_brain.capture.auth",
        "open_brain.capture.extractors",
        "open_brain.capture.http",
        "open_brain.capture.media",
        "open_brain.capture.poll",
        "open_brain.providers.local",
        "open_brain.providers.optional_cloud",
        "open_brain.providers.transcription",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in modules)
