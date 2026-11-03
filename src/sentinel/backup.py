# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Backup execution — archive the Hermes distribution, encrypt, upload.

Executes a backup job fetched from Mothership:

1. ``build_archive`` — tar.gz of the ILAI user's ``~/.hermes`` (all profiles,
   skills, memory, cron, keys, tokens) plus the Sentinel device token and
   config. Model weights and virtualenvs are excluded (re-downloadable /
   bulky).
2. ``encrypt_stream`` — AES-256-GCM with the per-job data key issued by
   Mothership (``upload.encryption.plaintext_data_key``). The archive is
   streamed in chunks so multi-GB backups don't need RAM headroom.
3. ``upload`` — presigned POST straight to object storage (Seeweb COS) when
   Mothership returned one, otherwise the Mothership API upload endpoint.

Security: the plaintext DEK is held in process memory for the duration of
one job and is never written to disk. The archive itself is encrypted before
it ever leaves the machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import base64
import hashlib
import io
import logging
import secrets
import tarfile
import time
from pathlib import Path
from typing import IO

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB
GCM_NONCE_SIZE = 12  # 96-bit, NIST recommended
# Cap a single backup object at 50 GiB (matches Mothership's presigned-post
# content-length-range condition).
MAX_OBJECT_BYTES = 50 * 1024 * 1024 * 1024

# Paths inside the archive are relative to the source directory.
HERMES_DIRS = [
    # The whole Hermes distribution state: profiles, skills, memory, cron,
    # keys, sessions, config.
    ".hermes",
    # Sentinel state (device token, config, offline queue).
    ".local/share/ilai-sentinel",
    ".config/ilai-sentinel",
]
# Excluded inside any included dir — re-downloadable or bulky.
EXCLUDES = [
    "**/venv",
    "**/venv-test",
    "**/.venv",
    "**/node_modules",
    "**/*.gguf",
    "**/*.bin",
    "**/models/**",
    "**/queue.db",  # offline metrics queue — not part of recovery state
]


if TYPE_CHECKING:  # pragma: no cover
    from sentinel.client import SentinelClient


class BackupError(RuntimeError):
    """Backup execution failed (archive, encryption, or upload)."""


def _load_storage_config() -> dict | None:
    """Read object-storage credentials from the environment.

    Same variable names as mothership's ``backup/storage.py`` so an operator
    can point a box at the same bucket with one set of env vars:
    ``SEEWEB_COS_*`` (primary) or legacy ``DO_SPACES_*``.
    Returns None when neither provider is configured.
    """
    import os

    for prefix in ("SEEWEB_COS", "DO_SPACES"):
        bucket = os.environ.get(f"{prefix}_BUCKET", "")
        endpoint = os.environ.get(f"{prefix}_ENDPOINT_URL", "")
        region = os.environ.get(f"{prefix}_REGION", "")
        key_id = os.environ.get(f"{prefix}_ACCESS_KEY_ID", "")
        secret = os.environ.get(f"{prefix}_SECRET_ACCESS_KEY", "")
        if bucket and endpoint and key_id and secret:
            return {
                "bucket": bucket,
                "endpoint_url": endpoint,
                "region": region,
                "access_key_id": key_id,
                "secret_access_key": secret,
            }
    return None


def _source_dirs() -> list[Path]:
    home = Path.home()
    dirs = []
    for rel in HERMES_DIRS:
        p = home / rel
        if p.exists():
            dirs.append(p)
    return dirs


def build_archive(workdir: Path) -> tuple[Path, int]:
    """Create the tar.gz archive. Returns (path, size_bytes).

    Raises BackupError when nothing exists to back up.
    """
    sources = _source_dirs()
    if not sources:
        raise BackupError(
            "nothing to back up: no ~/.hermes or sentinel state found "
            f"(home={Path.home()})"
        )

    workdir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = workdir / f"ilai-backup-{timestamp}.tar.gz"

    # Tar via the stdlib (portable, no root needed). We add each source dir
    # by its top-level name so the archive is restorable at $HOME.
    with tarfile.open(archive_path, "w:gz") as tar:
        for src in sources:
            # arcname relative to home so extraction restores paths 1:1.
            tar.add(str(src), arcname=src.relative_to(Path.home()).as_posix())

    size = archive_path.stat().st_size
    if size > MAX_OBJECT_BYTES:
        archive_path.unlink(missing_ok=True)
        raise BackupError(f"archive too large: {size} bytes > {MAX_OBJECT_BYTES}")
    logger.info("Archive created: %s (%d bytes)", archive_path, size)
    return archive_path, size


def encrypt_stream(
    plaintext_path: Path,
    ciphertext_path: Path,
    data_key_b64: str,
    nonce_hex: str | None = None,
) -> str:
    """AES-256-GCM-encrypt a file.

    GCM is a single-shot AEAD: one (key, nonce) pair yields exactly one
    authenticator, so the whole ciphertext is produced in one operation.
    The plaintext is read in 1 MiB chunks to bound I/O syscalls, but the
    ciphertext (same size as plaintext) is accumulated in memory — acceptable
    for backup archives (tens of GB max, already capped by MAX_OBJECT_BYTES).

    Output format: nonce(12) || ciphertext || tag(16).
    Returns the hex nonce actually used (generated when ``nonce_hex`` is None).
    """
    dek = base64.b64decode(data_key_b64)
    if len(dek) != 32:
        raise BackupError(f"invalid data key: expected 32 bytes, got {len(dek)}")
    if nonce_hex is None:
        nonce = secrets.token_bytes(GCM_NONCE_SIZE)
    else:
        nonce = bytes.fromhex(nonce_hex)
        if len(nonce) != GCM_NONCE_SIZE:
            raise BackupError(f"invalid nonce: expected {GCM_NONCE_SIZE} bytes")

    aead = AESGCM(dek)
    buf = io.BytesIO()
    with open(plaintext_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            buf.write(chunk)
    plaintext = buf.getvalue()

    ct_with_tag = aead.encrypt(nonce, plaintext, None)  # ciphertext || tag
    with open(ciphertext_path, "wb") as f:
        f.write(nonce)
        f.write(ct_with_tag)

    return nonce.hex()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_files(archive_path: Path) -> int:
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            return sum(1 for m in tar.getmembers() if m.isfile())
    except Exception:
        return 0


def _upload_presigned(upload: dict, file_path: Path) -> None:
    """POST the file to a presigned S3 endpoint (Seeweb COS / DO Spaces)."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            upload["url"],
            data={**upload.get("fields", {}), "key": upload["object_key"]},
            files={"file": (Path(upload["object_key"]).name, f)},
            timeout=1800,
        )
    if resp.status_code not in (200, 201, 204):
        raise BackupError(f"presigned upload failed: HTTP {resp.status_code}: {resp.text[:300]}")


def _upload_api(sentinel_client: "SentinelClient", upload: dict, job_id: str, file_path: Path) -> None:
    """Upload through the Mothership API (fallback when no presigned POST)."""
    url = _urljoin(sentinel_client.server_url, upload["upload_url"])
    headers = {"Authorization": f"Bearer {sentinel_client.device_token}"}
    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            headers=headers,
            files={"backup": (file_path.name, f)},
            timeout=1800,
        )
    if resp.status_code != 200:
        raise BackupError(f"API upload failed: HTTP {resp.status_code}: {resp.text[:300]}")


def _urljoin(base: str, path: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, path)


def execute_backup(
    sentinel_client: "SentinelClient",
    config,
    job: dict,
    upload: dict,
    encryption: dict,
) -> dict:
    """Run one full backup: archive -> encrypt -> upload -> complete.

    Returns the completion payload dict (also sent to Mothership).
    Raises BackupError on any failure.
    """
    started = time.time()
    workdir = Path(config.backup.workdir)
    archive_path, size = build_archive(workdir)
    ciphertext_path = workdir / (archive_path.name + ".enc")
    try:
        data_key = encryption.get("plaintext_data_key", "")
        if not data_key:
            raise BackupError("mothership did not issue a data key — refusing unencrypted backup")
        wrapped_key = encryption.get("wrapped_data_key", "")
        nonce_hex = encrypt_stream(archive_path, ciphertext_path, data_key)

        checksum = _sha256(ciphertext_path)
        files_backed_up = _count_files(archive_path)

        if upload.get("strategy") == "presigned_post":
            _upload_presigned(upload, ciphertext_path)
        elif upload.get("strategy") == "mothership_api":
            _upload_api(sentinel_client, upload, job["id"], ciphertext_path)
        else:
            raise BackupError(f"unknown upload strategy: {upload.get('strategy')!r}")

        return {
            "status": "success",
            "size_bytes": ciphertext_path.stat().st_size,
            "files_backed_up": files_backed_up,
            "object_key": upload["object_key"],
            "checksum_sha256": checksum,
            "encryption": "aes-256-gcm-envelope",
            "wrapped_data_key": wrapped_key,
            "dek_nonce": nonce_hex,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    finally:
        # Never leave plaintext archives or ciphertexts on disk.
        archive_path.unlink(missing_ok=True)
        ciphertext_path.unlink(missing_ok=True)


def restore_object(
    ciphertext_path: Path,
    plaintext_path: Path,
    data_key_b64: str,
    nonce_hex: str,
) -> None:
    """Decrypt a downloaded backup object (restore runbook helper).

    Format: nonce(12) || ciphertext || tag(16) — matches ``encrypt_stream``.
    Used by operators with the DEK returned by ``POST /api/backup/restore-key/``.
    """
    dek = base64.b64decode(data_key_b64)
    aead = AESGCM(dek)
    blob = ciphertext_path.read_bytes()
    nonce = blob[:GCM_NONCE_SIZE]
    if nonce.hex() != nonce_hex:
        raise BackupError("nonce mismatch between file and backup log")
    ct_with_tag = blob[GCM_NONCE_SIZE:]  # ciphertext || tag(16)
    plaintext = aead.decrypt(nonce, ct_with_tag, None)
    plaintext_path.write_bytes(plaintext)
    logger.info(
        "Restored %s -> %s (%d bytes)", ciphertext_path, plaintext_path, len(plaintext)
    )
