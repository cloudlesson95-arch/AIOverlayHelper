"""Screen capture utilities backed by ``mss``."""
from __future__ import annotations
import mss
from PIL import Image

from src.logger import get_logger

log = get_logger(__name__)


def capture_full_screen() -> Image.Image:
    """Capture the primary monitor as a PIL Image (RGB)."""
    with mss.mss() as sct:
        # sct.monitors[0] = virtual "all monitors" rectangle
        # sct.monitors[1] = primary monitor
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        log.debug("Full-screen capture: %dx%d", img.width, img.height)
        return img


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    """Capture an arbitrary region."""
    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        log.debug("Region capture @ (%d,%d) %dx%d", left, top, width, height)
        return img
