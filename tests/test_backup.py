# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Sentinel backup pipeline (archive, encrypt, upload)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import tarfile
from pathlib import Path

import pytest

from sentinel.backup import (
    BackupError,
    _count_files,
    _sha256,
    build_archive,
    encrypt_stream,
    restore_object,
)


@pytest.fixture
def dek_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _make_archive(path: Path, contents: dict[str, str]) -> Path:
    """Write a small tar.gz with the given {relpath: content} mapping."""
    with tarfile.open(path, "w:gz") as tar:
        for rel, content in contents.items():
            data = content.encode()
            import io
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


class TestEncryptStream:
    def test_roundtrip_single_chunk(self, tmp_path, dek_b64):
        src = tmp_path / "plain"
        src.write_bytes(b"hello world" * 100)
        ct = tmp_path / "plain.enc"
        nonce_hex = encrypt_stream(src, ct, dek_b64)

        assert len(nonce_hex) == 24  # 12 bytes hex
        blob = ct.read_bytes()
        assert blob[:12].hex() == nonce_hex

        restored = tmp_path / "out"
        restore_object(ct, restored, dek_b64, nonce_hex)
        assert restored.read_bytes() == src.read_bytes()

    def test_roundtrip_multi_chunk(self, tmp_path, dek_b64):
        big = secrets.token_bytes(3 * 1024 * 1024 + 12345)  # > 3 chunks
        src = tmp_path / "big"
        src.write_bytes(big)
        ct = tmp_path / "big.enc"
        nonce_hex = encrypt_stream(src, ct, dek_b64)

        restored = tmp_path / "out"
        restore_object(ct, restored, dek_b64, nonce_hex)
        assert restored.read_bytes() == big

    def test_ciphertext_differs_from_plaintext(self, tmp_path, dek_b64):
        src = tmp_path / "p"
        src.write_bytes(b"secret payload that must not survive in the clear")
        ct = tmp_path / "p.enc"
        encrypt_stream(src, ct, dek_b64)
        blob = ct.read_bytes()
        assert b"secret payload" not in blob

    def test_wrong_key_fails(self, tmp_path, dek_b64):
        src = tmp_path / "p"
        src.write_bytes(b"data")
        ct = tmp_path / "p.enc"
        nonce_hex = encrypt_stream(src, ct, dek_b64)

        other_key = base64.b64encode(secrets.token_bytes(32)).decode()
        with pytest.raises(Exception):
            restore_object(ct, tmp_path / "out", other_key, nonce_hex)

    def test_nonce_mismatch_rejected(self, tmp_path, dek_b64):
        src = tmp_path / "p"
        src.write_bytes(b"data")
        ct = tmp_path / "p.enc"
        encrypt_stream(src, ct, dek_b64)
        bad_nonce = "0" * 24
        with pytest.raises(BackupError, match="nonce mismatch"):
            restore_object(ct, tmp_path / "out", dek_b64, bad_nonce)

    def test_invalid_key_length(self, tmp_path):
        src = tmp_path / "p"
        src.write_bytes(b"data")
        short_key = base64.b64encode(b"0123456789").decode()
        with pytest.raises(BackupError, match="invalid data key"):
            encrypt_stream(src, tmp_path / "x", short_key)


class TestBuildArchive:
    def test_archives_hermes_tree(self, tmp_path, monkeypatch):
        # Fake a home with a .hermes tree and sentinel state.
        fake_home = tmp_path / "home"
        (fake_home / ".hermes" / "profiles" / "default" / "skills").mkdir(parents=True)
        (fake_home / ".hermes" / "config.yaml").write_text("model: x")
        (fake_home / ".local" / "share" / "ilai-sentinel").mkdir(parents=True)
        (fake_home / ".local" / "share" / "ilai-sentinel" / "device.token").write_text("tok")

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        workdir = tmp_path / "work"
        archive, size = build_archive(workdir)
        assert archive.exists() and size > 0

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        # Arcnames are relative to the fake home, restorable 1:1.
        assert any(n.startswith(".hermes/") for n in names)
        assert any("device.token" in n for n in names)

    def test_nothing_to_backup_raises(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "empty"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        with pytest.raises(BackupError, match="nothing to back up"):
            build_archive(tmp_path / "work")

    def test_files_counted(self, tmp_path):
        a = tmp_path / "a.tar.gz"
        _make_archive(a, {"x.txt": "x", "y.txt": "y"})
        assert _count_files(a) == 2

    def test_sha256_matches_hashlib(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"abc123")
        assert _sha256(p) == hashlib.sha256(b"abc123").hexdigest()


class TestStorageConfig:
    def test_seeweb_preferred(self, monkeypatch):
        from sentinel.backup import _load_storage_config

        monkeypatch.setenv("SEEWEB_COS_BUCKET", "b1")
        monkeypatch.setenv("SEEWEB_COS_ENDPOINT_URL", "https://s3.it-north-1.s3seeweb.it")
        monkeypatch.setenv("SEEWEB_COS_REGION", "it-north-1")
        monkeypatch.setenv("SEEWEB_COS_ACCESS_KEY_ID", "k")
        monkeypatch.setenv("SEEWEB_COS_SECRET_ACCESS_KEY", "s")
        monkeypatch.setenv("DO_SPACES_BUCKET", "b2")
        monkeypatch.setenv("DO_SPACES_ENDPOINT_URL", "https://x")
        monkeypatch.setenv("DO_SPACES_ACCESS_KEY_ID", "k2")
        monkeypatch.setenv("DO_SPACES_SECRET_ACCESS_KEY", "s2")

        cfg = _load_storage_config()
        assert cfg["bucket"] == "b1"  # Seeweb wins
        assert cfg["region"] == "it-north-1"

    def test_do_spaces_fallback(self, monkeypatch):
        from sentinel.backup import _load_storage_config

        for var in (
            "SEEWEB_COS_BUCKET", "SEEWEB_COS_ENDPOINT_URL",
            "SEEWEB_COS_REGION", "SEEWEB_COS_ACCESS_KEY_ID",
            "SEEWEB_COS_SECRET_ACCESS_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DO_SPACES_BUCKET", "b2")
        monkeypatch.setenv("DO_SPACES_ENDPOINT_URL", "https://nyc3.digitaloceanspaces.com")
        monkeypatch.setenv("DO_SPACES_REGION", "nyc3")
        monkeypatch.setenv("DO_SPACES_ACCESS_KEY_ID", "k2")
        monkeypatch.setenv("DO_SPACES_SECRET_ACCESS_KEY", "s2")

        cfg = _load_storage_config()
        assert cfg["bucket"] == "b2"

    def test_unconfigured_returns_none(self, monkeypatch):
        from sentinel.backup import _load_storage_config

        for var in (
            "SEEWEB_COS_BUCKET", "SEEWEB_COS_ENDPOINT_URL",
            "SEEWEB_COS_REGION", "SEEWEB_COS_ACCESS_KEY_ID",
            "SEEWEB_COS_SECRET_ACCESS_KEY",
            "DO_SPACES_BUCKET", "DO_SPACES_ENDPOINT_URL",
            "DO_SPACES_ACCESS_KEY_ID", "DO_SPACES_SECRET_ACCESS_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        assert _load_storage_config() is None


class TestExecuteBackup:
    def test_missing_data_key_refuses_unencrypted(self, tmp_path, monkeypatch):
        from sentinel.backup import execute_backup
        from sentinel.config import Config

        fake_home = tmp_path / "home"
        (fake_home / ".hermes").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        class FakeClient:
            server_url = "https://mothership.example.com"
            device_token = "tok"

        with pytest.raises(BackupError, match="data key"):
            execute_backup(
                FakeClient(),
                Config(),
                {"id": "j1"},
                {"strategy": "presigned_post", "url": "https://x", "fields": {}, "object_key": "k"},
                {},  # no encryption payload
            )

    def test_full_pipeline_encrypts_and_uploads(self, tmp_path, monkeypatch, dek_b64):
        import base64 as _b64

        from sentinel.backup import execute_backup
        from sentinel.config import Config

        fake_home = tmp_path / "home"
        (fake_home / ".hermes").mkdir(parents=True)
        (fake_home / ".hermes" / "marker.txt").write_text("recovery-marker")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        config = Config()
        config.backup.workdir = str(tmp_path / "work")

        uploaded: dict = {}

        def fake_presigned(upload, file_path):
            uploaded["bytes"] = file_path.read_bytes()
            uploaded["key"] = upload["object_key"]

        monkeypatch.setattr("sentinel.backup._upload_presigned", fake_presigned)

        wrapped = _b64.b64encode(b"wrapped-dek").decode()
        payload = execute_backup(
            type("C", (), {"server_url": "https://m", "device_token": "t"}),
            config,
            {"id": "j2"},
            {
                "strategy": "presigned_post",
                "url": "https://bucket.example.com",
                "fields": {},
                "object_key": "ilai-backups/j2.tar.gz.enc",
            },
            {"plaintext_data_key": dek_b64, "wrapped_data_key": wrapped},
        )

        assert payload["status"] == "success"
        assert payload["encryption"] == "aes-256-gcm-envelope"
        assert payload["wrapped_data_key"] == wrapped
        assert payload["dek_nonce"]
        assert uploaded["key"] == "ilai-backups/j2.tar.gz.enc"
        assert payload["size_bytes"] == len(uploaded["bytes"])
        assert payload["checksum_sha256"] == hashlib.sha256(uploaded["bytes"]).hexdigest()

        # The uploaded blob is encrypted: decrypt it with the DEK and verify
        # the archive content is inside. Format: nonce(12) || ct || tag(16).
        import io

        blob = uploaded["bytes"]
        nonce = blob[:12]
        ct_with_tag = blob[12:]
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aead = AESGCM(_b64.b64decode(dek_b64))
        out = aead.decrypt(nonce, ct_with_tag, None)
        tar = tarfile.open(fileobj=io.BytesIO(out), mode="r:gz")
        names = tar.getnames()
        assert any("marker.txt" in n for n in names)

        # No plaintext or ciphertext left on disk.
        leftovers = list(Path(config.backup.workdir).glob("*.tar.gz*"))
        assert leftovers == []
