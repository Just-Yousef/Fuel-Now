"""Secure file-upload validation for station logos.

Rules
-----
* **Extension allow-list** — only .png/.jpg/.jpeg/.webp.
* **Magic-byte check** — the declared extension must match the real file header,
  so `evil.png` containing a PHP/HTML payload is rejected.
* **Size cap** — `MAX_UPLOAD_BYTES` (2 MB default).
* **Random filename** — the client-supplied name is discarded entirely, which
  removes path-traversal and double-extension tricks.
* **Re-encode via Pillow** when available, stripping EXIF/metadata; falls back to
  raw storage of a byte-verified image if Pillow is absent.
"""
from __future__ import annotations

import io
import secrets
from pathlib import Path

ALLOWED_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

# Magic-byte signatures per declared kind.
SIGNATURES = {
    "image/png": [bytes.fromhex("89504e470d0a1a0a")],
    "image/jpeg": [bytes.fromhex("ffd8ff")],
    "image/webp": [b"RIFF"],  # plus "WEBP" at offset 8, checked separately
}


class UploadError(ValueError):
    """Raised when an upload fails validation."""


def _sniff_matches(header: bytes, mime: str) -> bool:
    for signature in SIGNATURES.get(mime, []):
        if header.startswith(signature):
            if mime == "image/webp":
                return len(header) >= 12 and header[8:12] == b"WEBP"
            return True
    return False


def validate_image_upload(file_storage, *, max_bytes: int) -> bytes:
    """Validate an uploaded image and return its (possibly re-encoded) bytes.

    Raises UploadError with a user-safe message on any problem.
    """
    if file_storage is None or not getattr(file_storage, "filename", ""):
        raise UploadError("Choose an image file to upload.")

    original = Path(file_storage.filename)
    extension = original.suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadError("Only PNG, JPG or WEBP images are allowed.")

    # Reject double extensions like logo.png.php early.
    if original.name.count(".") > 1 and Path(original.stem).suffix.lower() in {
        ".php", ".py", ".js", ".html", ".htm", ".exe", ".sh"
    }:
        raise UploadError("That filename is not allowed.")

    payload = file_storage.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise UploadError(f"Image must be smaller than {max_bytes // (1024 * 1024)} MB.")
    if not payload:
        raise UploadError("The uploaded file is empty.")

    declared_mime = ALLOWED_EXTENSIONS[extension]
    if not _sniff_matches(payload[:16], declared_mime):
        raise UploadError("That file is not a real image of the type it claims to be.")

    # Re-encode with Pillow when available: strips EXIF and normalises the file.
    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(payload)) as image:
            image.verify()  # raises on malformed data
        with Image.open(io.BytesIO(payload)) as image:
            image = image.convert("RGB") if declared_mime == "image/jpeg" else image
            out = io.BytesIO()
            fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[extension]
            image.save(out, format=fmt, optimize=True)
            return out.getvalue()
    except ImportError:
        return payload
    except Exception:
        raise UploadError("That image could not be read. Try another file.")


def safe_stored_name(extension: str) -> str:
    """A random, server-controlled filename — client input never reaches disk."""
    return f"{secrets.token_hex(16)}{extension.lower()}"


def image_dimensions(payload: bytes) -> tuple[int, int] | None:
    """Read width/height without trusting client metadata."""
    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(payload)) as image:
            return image.size
    except Exception:
        return None
