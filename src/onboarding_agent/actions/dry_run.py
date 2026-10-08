"""DRY_RUN bookkeeping shared by the executor and the reminder batch.

A dry run records "dryrun:" ids where a real run records Calendar ids and
Slack ts values, so reruns are skipped the same way. A real run ignores
those ids. This module stays free of Calendar and Slack clients so the
reminder batch can import it with the small batch dependencies.
"""

from __future__ import annotations

from ..meeting.models import Meeting

DRY_RUN_PREFIX = "dryrun:"
# Public-dataset excerpts are for extraction and evaluation only (see DECISIONS.md).
EXTERNAL_SOURCE_PREFIXES = ("assembly_",)


def is_done(value: str | None, dry_run: bool) -> bool:
    """A recorded id counts as done; dry-run ids only count in dry-run mode."""
    if not value:
        return False
    if value.startswith(DRY_RUN_PREFIX):
        return dry_run
    return True


def is_external_source(meeting: Meeting) -> bool:
    return (meeting.source_filename or "").startswith(EXTERNAL_SOURCE_PREFIXES)
