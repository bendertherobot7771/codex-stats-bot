"""PyInstaller entry point that preserves package-relative imports."""

from agent.codex_stats_agent.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())

