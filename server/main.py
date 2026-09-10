"""Retired entry point: legacy unauthenticated endpoints must not be published."""

raise RuntimeError(
    "The unauthenticated legacy server has been retired. "
    "Run start.cmd, start.sh, or 'uv run --locked translator start'. "
    "The new runtime separates authenticated local management from the participant hub."
)
