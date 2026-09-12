"""Process entry point for the canonical connector worker protocol module."""

from open_brain_connectors.runtime.connector_worker_v1 import _child_main

if __name__ == "__main__":
    raise SystemExit(_child_main())
