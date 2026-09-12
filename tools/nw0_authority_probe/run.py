"""Run the fixed synthetic native authority proof."""

from tools.nw0_api_probe.run import AUTHORITY_PROFILE, run_proof


def main() -> int:
    return run_proof(AUTHORITY_PROFILE)


if __name__ == "__main__":
    raise SystemExit(main())
