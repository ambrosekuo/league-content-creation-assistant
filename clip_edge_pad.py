"""Shared edge pad (cut) / trim (stitch) for seek-cut open/close freezes.

Cut expands each window by PAD_LEAD / PAD_TRAIL; stitch drops the same
amounts from non-lobby gameplay clips so delivered pre/post-roll matches
snap intent (e.g. 8s / 10s buffers stay 8s / 10s after trim).
"""

from __future__ import annotations

# Keep cut --pad-* and stitch --trim-* defaults identical.
PAD_LEAD_S = 1.5
PAD_TRAIL_S = 1.5
