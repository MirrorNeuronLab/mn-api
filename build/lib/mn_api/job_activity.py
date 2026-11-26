"""Compatibility facade for SDK-owned activity projection."""

from mn_sdk.activity_projection import (
    MAX_ACTIVITY_EVENTS,
    activity_message as _activity_message,
    compact_activity_event as _compact_activity_event,
    compact_activity_value as compact_value,
    compact_event,
    enrich_workflow_progress_activity,
)

__all__ = [
    "MAX_ACTIVITY_EVENTS",
    "_activity_message",
    "_compact_activity_event",
    "compact_event",
    "compact_value",
    "enrich_workflow_progress_activity",
]
