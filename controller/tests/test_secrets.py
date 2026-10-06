import json
import os
import stat
import warnings

import pytest

from rebuild_controller.providers import secrets as S


@pytest.fixture
def store(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = S.SecretStore(tmp_path / "data")
    yield s
    for ref in s.refs():
        s.delete(ref)


class FakeOsBackend:
    name = "fakeos"
    os_backed = True

    def protect(self, data: bytes) -> bytes:
        return data[::-1]

    def unprotect(self, data: bytes) -> bytes:
        return data[::-1]


def test_file_backend_warns_that_it_is_not_os_backed(tmp_path):
    if os.name == "nt":
        pytest.skip("posix-only expectation")
    with pytest.warns(S.SecretStoreWarning, match="NOT OS-backed"):
        s = S.SecretStore(tmp_path / "d")
    assert s.os_backed is False and s.backend_name == "file" and "NOT OS-backed" in s.warning
    assert s.describe()["os_backed"] is False


def test_default_backend_selection_matches_os():
    b = S._default_backend()
    assert (b.name == "dpapi") == (os.name == "nt")


def test_roundtrip_ref_and_permissions(store):
    ref = store.put("sk-test-1234567890abcdef")
    assert ref.startswith("secret:")
    assert store.get(ref) == "sk-test-1234567890abcdef"
    assert store.get("secret:missing") is None
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    # no stray temp files after atomic write
    assert [p.name for p in store.dir.iterdir()] == [store.FILE_NAME]
    assert store.delete(ref) is True and store.get(ref) is None and store.delete(ref) is False


def test_corrupt_file_is_not_silently_overwritten(store):
    store.put("abcdefgh-secret")
    store.path.write_text("{not json")
    with pytest.raises(S.SecretError):
        store.put("another-secret-value")
    assert store.path.read_text() == "{not json"
    store.path.write_text('{"version": 1, "entries": {}}')  # leave a readable file for fixture teardown


def test_entries_record_their_backend_and_os_backed_store_does_not_warn(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # would raise if a warning were emitted
        s = S.SecretStore(tmp_path / "d", backend=FakeOsBackend())
    ref = s.put("hunter2-hunter2")
    raw = json.loads(s.path.read_text())["entries"][ref]
    assert raw["b"] == "fakeos" and "hunter2" not in s.path.read_text()
    assert s.get(ref) == "hunter2-hunter2" and s.os_backed
    # an entry from a backend this host cannot open is reported, not mangled
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        other = S.SecretStore(tmp_path / "d")
    with pytest.raises(S.SecretError, match="unavailable"):
        other.get(ref)


def test_redact_masks_stored_secret_in_any_text(store):
    secret = "zzTopSecretValue-9876543210"
    store.put(secret)
    text = f"upstream said: invalid key {secret}; header x-api-key: {secret}"
    out = S.redact(text)
    assert secret not in out and S.REDACTED in out
    # the JSON-escaped spelling is masked too
    weird = 'pa"ss-word-with-quote-123'
    store.put(weird)
    assert weird not in S.redact(json.dumps({"k": weird}))


@pytest.mark.parametrize("raw", [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
    "sk-proj-abcdefghijklmnopqrstuvwx",
    "sk-or-v1-abcdefghijklmnop",
    "AIzaSyA1234567890abcdefghijklmnopqrstu",
    "Authorization: Bearer abcDEF123456789xyz",
    "https://x.test/v1?key=SUPERSECRETKEY12345&alt=sse",
    '{"api_key": "abcdef123456"}',
])
def test_redact_masks_known_key_shapes_without_registration(raw):
    out = S.redact(raw)
    for frag in ("AbCdEfGhIjKlMnOpQrStUv", "abcdefghijklmnopqrstuvwx", "abcdefghijklmnop", "A1234567890abcdef",
                 "abcDEF123456789xyz", "SUPERSECRETKEY12345", "abcdef123456"):
        assert frag not in out


def test_redact_leaves_ordinary_text_and_handles_none():
    assert S.redact("hello world, 42 tokens used") == "hello world, 42 tokens used"
    assert S.redact(None) == ""


def test_redact_obj_masks_sensitive_keys_and_nested_values(store):
    store.put("nested-secret-value-xyz")
    obj = {"headers": {"Authorization": "Bearer abc", "X-Api-Key": "k", "ok": "nested-secret-value-xyz in text"}, "n": [1, "nested-secret-value-xyz"]}
    out = S.redact_obj(obj)
    assert out["headers"]["Authorization"] == S.REDACTED and out["headers"]["X-Api-Key"] == S.REDACTED
    assert "nested-secret-value-xyz" not in json.dumps(out)


def test_logging_filter_redacts_records(store, caplog):
    import logging
    secret = "log-leak-secret-abcdef"
    store.put(secret)
    lg = logging.getLogger("test.redact")
    S.install_redacting_filter(lg)
    with caplog.at_level(logging.INFO, logger="test.redact"):
        lg.info("calling with key %s", secret)
    assert secret not in caplog.text and S.REDACTED in caplog.text


def test_secrets_are_re_registered_when_store_reopens(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = S.SecretStore(tmp_path / "d")
        ref = s.put("persisted-secret-123456")
        S.unregister_secret("persisted-secret-123456")
        assert S.redact("x persisted-secret-123456 y") == "x persisted-secret-123456 y"  # unregistered: not masked
        S.SecretStore(tmp_path / "d")  # reopening registers every stored secret again
    assert "persisted-secret-123456" not in S.redact("x persisted-secret-123456 y")
    s.delete(ref)


def test_isolated_env_drops_keys_and_inherits_nothing_else():
    base = {"PATH": "/bin", "HOME": "/h", "OPENAI_API_KEY": "k1", "ANTHROPIC_API_KEY": "k2", "JEV_API_KEY": "k3",
            "GITHUB_TOKEN": "t", "REBUILD_STUDIO_DATA": "/d", "RANDOM_VAR": "x", "CODEX_HOME": "/c", "HTTPS_PROXY": "http://p"}
    env = S.isolated_env(base=base)
    assert env == {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/c", "HTTPS_PROXY": "http://p"}
    # explicit allow cannot smuggle a secret-looking name; `extra` is the deliberate channel
    assert "GITHUB_TOKEN" not in S.isolated_env(base=base, allow=["GITHUB_TOKEN"])
    assert S.isolated_env({"X": "1"}, base=base)["X"] == "1"
