from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector.control import (
    _receive,
    control_endpoint,
    control_request,
    control_server,
)
from open_brain_collector.sources_cli import main
from open_brain_connectors.runtime.live_common import LiveSourceError


def test_real_private_socket_roundtrip_cli_and_service_ownership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = brain / "collector" / "state.json"
    endpoint = control_endpoint(state, brain)
    assert len(str(endpoint).encode()) < 104
    with control_server(state, brain, background=True):
        assert endpoint.stat().st_mode & 0o777 == 0o600
        assert endpoint.parent.stat().st_mode & 0o777 == 0o700
        result = control_request(state, brain, "sources.status", {})
        assert result["collector"] == {"connected": True, "background": True}
        assert result["sources"] == []
        assert main(["--state", str(state), "--brain-root", str(brain), "status"]) == 0
        assert json.loads(capsys.readouterr().out) == result
        with pytest.raises(LiveSourceError, match="source_invalid_operation"):
            control_request(state, brain, "system.execute", {})
        with pytest.raises(LiveSourceError, match="source_busy"), control_server(state, brain):
            pytest.fail("second collector acquired ownership")
    assert not endpoint.exists()
    with control_server(state, brain):
        assert control_request(state, brain, "sources.status", {})["collector"] == {
            "connected": True,
            "background": False,
        }


def test_socket_symlink_is_rejected(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = brain / "collector" / "state.json"
    endpoint = control_endpoint(state, brain)
    endpoint.parent.mkdir(mode=0o700)
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    endpoint.symlink_to(target)
    try:
        with pytest.raises(LiveSourceError, match="collector_unsafe_socket"):
            control_request(state, brain, "sources.status", {})
        with (
            pytest.raises(LiveSourceError, match="collector_unsafe_socket"),
            control_server(state, brain),
        ):
            pytest.fail("accepted socket symlink")
        assert target.read_text() == "preserve"
    finally:
        endpoint.unlink()


@pytest.mark.parametrize("frame", [b"{}\n{}\n", b"[]\n", b"x" * 20 + b"\n"])
def test_control_rejects_invalid_or_oversized_frames(frame: bytes) -> None:
    left, right = socket.socketpair()
    with left, right:
        right.sendall(frame)
        with pytest.raises(LiveSourceError, match="collector_invalid_frame"):
            _receive(left, 16)


def test_control_deadline_is_total_even_when_peer_drips_bytes() -> None:
    left, right = socket.socketpair()
    stop = threading.Event()

    def drip() -> None:
        while not stop.wait(0.015):
            try:
                right.sendall(b" ")
            except OSError:
                return

    thread = threading.Thread(target=drip)
    with left, right:
        thread.start()
        started = time.monotonic()
        try:
            with pytest.raises((LiveSourceError, TimeoutError)):
                _receive(left, 1024, timeout_seconds=0.08)
            assert time.monotonic() - started < 0.5
        finally:
            stop.set()
            thread.join(timeout=1)


@pytest.mark.parametrize("cleanup_stuck", [False, True])
def test_failed_background_enable_removes_only_its_job_and_reports_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_stuck: bool
) -> None:
    from itertools import count
    from types import SimpleNamespace

    from open_brain_collector import sources_cli

    class Manager:
        installed = False
        removed = False

        def install(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["keep_alive"] is True
            self.installed = True
            return {"status": "loaded"}

        def remove_owned(self, *, label: str, owner_token: str) -> dict[str, object]:
            assert label.startswith("open-brain.collector.")
            assert owner_token == "collector-owned:" + label.removeprefix("open-brain.collector.")
            self.removed = True
            return {"status": "loaded"}

        def status(self, label: str) -> str:
            return "loaded" if cleanup_stuck else "not_loaded"

    manager = Manager()
    ticks = count(0, 10)
    monkeypatch.setattr(
        sources_cli, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sources_cli, "CollectorLaunchdServiceManager", lambda **_: manager)
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    expected = (
        "collector_service_cleanup_unconfirmed"
        if cleanup_stuck
        else "collector_service_start_failed"
    )
    with pytest.raises(LiveSourceError, match=expected):
        sources_cli.background_service(brain / "collector/state.json", brain, enable=True)
    assert manager.installed and manager.removed
