from __future__ import annotations

import hashlib

import pytest

from exleasingcar_archive.r2_storage import CloudflareR2Storage, R2Settings


class FakeR2Client:
    def __init__(self):
        self.put_calls = []
        self.head_calls = []
        self.presign_calls = []
        self.present = set()

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.present.add(kwargs["Key"])

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        if kwargs["Key"] not in self.present:
            error = RuntimeError("missing")
            error.response = {
                "Error": {"Code": "404"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
            raise error
        return {}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presign_calls.append((operation, Params, ExpiresIn))
        return "https://example.r2.cloudflarestorage.com/signed-object"


def _settings():
    return R2Settings(
        account_id="a" * 32,
        bucket_name="private-archive",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
    )


def test_r2_settings_builds_account_endpoint_and_reads_environment():
    settings = R2Settings.from_env(
        {
            "EXLEASINGCAR_R2_ACCOUNT_ID": "b" * 32,
            "EXLEASINGCAR_R2_BUCKET": "archive-bucket",
            "EXLEASINGCAR_R2_ACCESS_KEY_ID": "access",
            "EXLEASINGCAR_R2_SECRET_ACCESS_KEY": "secret",
        }
    )

    assert settings.endpoint_url == f"https://{'b' * 32}.r2.cloudflarestorage.com"
    assert "secret" not in repr(settings)


def test_r2_settings_reports_missing_environment_variable_names_only():
    with pytest.raises(RuntimeError, match="EXLEASINGCAR_R2_ACCOUNT_ID"):
        R2Settings.from_env({})


def test_r2_settings_rejects_invalid_account_and_bucket():
    with pytest.raises(ValueError, match="account ID"):
        R2Settings("not-an-account-id", "private-bucket", "access", "secret")
    with pytest.raises(ValueError, match="bucket name"):
        R2Settings("a" * 32, "Bad_Bucket", "access", "secret")


def test_object_key_is_content_addressed_and_media_type_scoped():
    digest = hashlib.sha256(b"image").hexdigest()

    assert CloudflareR2Storage.object_key_for(digest, "photo") == (
        f"archive/photo/{digest[:2]}/{digest}"
    )
    with pytest.raises(ValueError, match="media_type"):
        CloudflareR2Storage.object_key_for(digest, "unknown")


def test_put_bytes_uploads_private_content_without_public_acl():
    client = FakeR2Client()
    storage = CloudflareR2Storage(_settings(), client=client)
    body = b"%PDF-1.7 synthetic report"
    digest = hashlib.sha256(body).hexdigest()
    key = storage.object_key_for(digest, "report")

    stored_key = storage.put_bytes(
        body,
        object_key=key,
        content_type="application/pdf",
        expected_sha256=digest,
    )

    assert stored_key == key
    assert len(client.put_calls) == 1
    upload = client.put_calls[0]
    assert upload["Body"] == body
    assert upload["ContentType"] == "application/pdf"
    assert upload["Metadata"] == {"sha256": digest}
    assert "ACL" not in upload
    assert "GrantRead" not in upload


def test_put_bytes_rejects_hash_mismatch_and_unknown_mime():
    client = FakeR2Client()
    storage = CloudflareR2Storage(_settings(), client=client)
    digest = hashlib.sha256(b"image").hexdigest()
    key = storage.object_key_for(digest, "photo")

    with pytest.raises(ValueError, match="does not match"):
        storage.put_bytes(b"different", object_key=key, content_type="image/jpeg")
    with pytest.raises(ValueError, match="Unsupported media"):
        storage.put_bytes(b"image", object_key=key, content_type="text/html")
    assert client.put_calls == []


def test_put_bytes_requires_digest_inside_key_and_accepts_mime_parameters():
    client = FakeR2Client()
    storage = CloudflareR2Storage(_settings(), client=client)
    body = b"image"
    digest = hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError, match="does not match"):
        storage.put_bytes(
            body,
            object_key="archive/photo/other-object",
            content_type="image/jpeg",
        )
    storage.put_bytes(
        body,
        object_key=storage.object_key_for(digest, "photo"),
        content_type="image/jpeg; charset=binary",
    )
    assert len(client.put_calls) == 1


def test_object_key_rejects_absolute_and_traversal_paths():
    for key in ("/absolute/key", "archive/../secret", "archive//empty"):
        with pytest.raises(ValueError, match="safe relative path"):
            CloudflareR2Storage.validate_object_key(key)


def test_object_exists_checks_head_without_downloading():
    client = FakeR2Client()
    storage = CloudflareR2Storage(_settings(), client=client)

    assert storage.object_exists("archive/photo/a1/image") is False
    digest = hashlib.sha256(b"photo").hexdigest()
    key = storage.object_key_for(digest, "photo")
    client.present.add(key)
    assert storage.object_exists(key) is True
    assert len(client.head_calls) == 2


def test_presigned_get_url_is_short_lived_and_object_specific():
    client = FakeR2Client()
    storage = CloudflareR2Storage(_settings(), client=client)
    digest = hashlib.sha256(b"photo").hexdigest()
    key = storage.object_key_for(digest, "photo")

    assert storage.presign_get_url(key, expires_in_seconds=120).startswith("https://")
    assert client.presign_calls == [
        ("get_object", {"Bucket": "private-archive", "Key": key}, 120)
    ]


@pytest.mark.parametrize("expiry", [0, -1, 3601, True, 1.5])
def test_presigned_get_url_rejects_invalid_expiry(expiry):
    storage = CloudflareR2Storage(_settings(), client=FakeR2Client())
    with pytest.raises(ValueError, match="expiry"):
        storage.presign_get_url("archive/photo/a1/image", expires_in_seconds=expiry)
