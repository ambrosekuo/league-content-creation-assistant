"""BT.709 limited-range tagging for HD social-media exports.

Tagging tells TikTok/players "this is BT.709 limited." It does not change
pixels. An explicit full→limited scale *does* change luma, and will darken
footage that is already decoded into TV-range.

Rule:
  Twitch / game video  → VIDEO_TO_BT709 (format + tags only)
  PNG / JPEG stills    → IMAGE_TO_BT709 (full→limited, then tags)

Never run scale=in_range=full:out_range=tv on Twitch/game video.
"""

from __future__ import annotations

# Video sources: preserve decoded levels, then tag BT.709 limited.
VIDEO_TO_BT709 = (
    "format=yuv420p,"
    "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
)

# Image sources only (RGB/JPEG, genuinely full-range) before they enter YUV.
IMAGE_TO_BT709 = (
    "scale=in_range=full:out_range=tv:out_color_matrix=bt709,"
    "format=yuv420p,"
    "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
)

# libx264 VUI / container. -pix_fmt keeps the encoder off yuvj420p.
X264_BT709 = [
    "-pix_fmt",
    "yuv420p",
    "-colorspace",
    "bt709",
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
    "-color_range",
    "tv",
]
