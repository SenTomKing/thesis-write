from __future__ import annotations

import asyncio
import hashlib
import os
import urllib.request
from pathlib import Path

try:
    from vercel.blob import AsyncBlobClient, BlobClient
except ImportError:  # pragma: no cover - optional until deployment deps are installed
    AsyncBlobClient = None
    BlobClient = None


def is_remote_storage_ref(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized.startswith("http://") or normalized.startswith("https://")


def blob_token() -> str:
    return (os.getenv("BLOB_READ_WRITE_TOKEN") or "").strip()


def blob_enabled() -> bool:
    return bool(blob_token() and BlobClient is not None)


def is_vercel_blob_ref(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return ".blob.vercel-storage.com/" in normalized


def upload_bytes_to_blob(*, pathname: str, body: bytes, content_type: str, add_random_suffix: bool = False) -> dict[str, str]:
    if not blob_enabled() or BlobClient is None:
        raise RuntimeError("Blob storage is not configured.")
    client = BlobClient()
    uploaded = client.put(
        pathname,
        body,
        access="private",
        content_type=content_type or None,
        add_random_suffix=add_random_suffix,
        overwrite=not add_random_suffix,
        multipart=len(body) >= 5 * 1024 * 1024,
    )
    return {
        "pathname": uploaded.pathname,
        "url": uploaded.url,
        "downloadUrl": uploaded.download_url,
        "contentType": uploaded.content_type or content_type or "application/octet-stream",
    }


async def _download_private_blob_async(storage_ref: str) -> bytes:
    if AsyncBlobClient is None:
        raise FileNotFoundError(storage_ref)
    client = AsyncBlobClient()
    result = await client.get(storage_ref, access="private")
    if result is None or result.status_code != 200 or result.stream is None:
        raise FileNotFoundError(storage_ref)
    chunks: list[bytes] = []
    async for chunk in result.stream:
        chunks.append(chunk)
    return b"".join(chunks)


def materialize_storage_ref(*, storage_ref: str, file_name: str, temp_dir: Path) -> Path:
    normalized_ref = (storage_ref or "").strip()
    if not normalized_ref:
        raise FileNotFoundError("Storage reference is empty.")
    if not is_remote_storage_ref(normalized_ref):
        path = Path(normalized_ref)
        if not path.exists():
            raise FileNotFoundError(normalized_ref)
        return path.resolve()

    temp_dir.mkdir(parents=True, exist_ok=True)
    extension = Path(file_name or "").suffix or Path(normalized_ref).suffix or ".bin"
    digest = hashlib.sha1(f"{normalized_ref}|{file_name}".encode("utf-8")).hexdigest()[:16]
    cached_path = temp_dir / f"{digest}{extension}"
    if cached_path.exists():
        return cached_path

    if is_vercel_blob_ref(normalized_ref) and blob_token():
        cached_path.write_bytes(asyncio.run(_download_private_blob_async(normalized_ref)))
        return cached_path

    request = urllib.request.Request(normalized_ref, headers={"User-Agent": "DraftRefine/1.0 storage bridge"})
    with urllib.request.urlopen(request, timeout=30) as response:
        cached_path.write_bytes(response.read())
    return cached_path
