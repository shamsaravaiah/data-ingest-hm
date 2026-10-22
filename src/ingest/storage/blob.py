"""Optional Azure Blob uploads for product images."""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


class BlobStore:
    def __init__(self, connection_string: str, container: str):
        self.enabled = bool(connection_string.strip() and container.strip())
        self._container = None
        if not self.enabled:
            return
        try:
            from azure.storage.blob import BlobServiceClient

            service = BlobServiceClient.from_connection_string(connection_string.strip())
            self._container = service.get_container_client(container.strip())
            if not self._container.exists():
                self._container.create_container()
        except Exception as exc:
            logger.warning("Azure Blob is disabled because the client could not be created: %s", exc)
            self.enabled = False
            self._container = None

    def upload(self, data: bytes, content_type: str, blob_name: str) -> str | None:
        if not self.enabled or self._container is None:
            return None
        try:
            from azure.storage.blob import ContentSettings

            self._container.upload_blob(
                name=blob_name,
                data=data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
            return self._container.get_blob_client(blob_name).url
        except Exception as exc:
            logger.warning("Blob upload failed for %s: %s", blob_name, exc)
            return None


def download_image(
    client: httpx.Client,
    url: str,
    max_bytes: int,
    user_agent: str,
) -> tuple[bytes, str] | None:
    try:
        with client.stream(
            "GET",
            url,
            headers={"User-Agent": user_agent},
            timeout=30,
            follow_redirects=True,
        ) as response:
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/") or content_type == "image/svg+xml":
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except httpx.HTTPError:
        logger.warning("Image download failed for %s", url)
        return None
    if not chunks:
        return None
    return b"".join(chunks), content_type


def blob_name(host: str, sku: str | None, digest: str, position: int, content_type: str) -> str:
    safe_host = re.sub(r"[^a-zA-Z0-9._-]+", "_", host)[:80]
    key = re.sub(r"[^a-zA-Z0-9._-]+", "_", sku or "item")[:80]
    extension = EXTENSIONS.get(content_type.split(";")[0].strip().lower(), "")
    return f"{safe_host}/{key}/{position}-{digest}{extension}"
