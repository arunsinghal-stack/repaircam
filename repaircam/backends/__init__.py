"""Capture backends.

The recorder talks only to the :class:`CaptureBackend` interface, so swapping
RTSP IP cameras for Pi cameras later is a new file here and nothing else.
"""

from __future__ import annotations

from ..config import CameraConfig
from .base import CaptureBackend, CaptureError, Segment
from .rtsp import RtspBackend

#: backend name in cameras.yaml -> implementation
REGISTRY: dict[str, type[CaptureBackend]] = {
    "rtsp": RtspBackend,
}


def build_backend(camera: CameraConfig) -> CaptureBackend:
    """Create the backend named by ``camera.backend``."""
    try:
        backend_cls = REGISTRY[camera.backend]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise CaptureError(
            f"Unknown backend '{camera.backend}' for {camera.work_center}. Available: {known}"
        ) from None
    return backend_cls(camera)


__all__ = [
    "CaptureBackend",
    "CaptureError",
    "RtspBackend",
    "Segment",
    "REGISTRY",
    "build_backend",
]
