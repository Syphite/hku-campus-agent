"""Upload filled files to a public host for Teams-compatible download links."""

import logging
import os

import requests

logger = logging.getLogger(__name__)

FALLBACK_DOWNLOAD_URL = "https://example.com/mock-filled-application.docx"


def upload_to_public_host(file_path: str) -> str:
    """Upload a file to 0x0.st and return the public URL."""
    try:
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as handle:
            response = requests.post(
                "https://0x0.st",
                files={"file": (filename, handle)},
                timeout=30,
            )
        if response.status_code == 200:
            public_url = response.text.strip()
            if public_url.startswith("http"):
                logger.info("Uploaded filled form to public host: %s", public_url)
                return public_url
        logger.error(
            "Public host upload failed: status=%s body=%s",
            response.status_code,
            response.text[:200],
        )
    except Exception as exc:
        logger.error(f"Failed to upload file to public host: {exc}")

    return FALLBACK_DOWNLOAD_URL
