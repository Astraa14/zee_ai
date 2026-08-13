"""Updater unit tests: sha256 helpers, download/verify, apply strategies,
and the token-protected /update endpoint."""
import hashlib
import json
import sys
import threading

import pytest

import updater


@pytest.fixture
def payload_file(tmp_path):
    data = b"ZEE-RELEASE-BINARY" * 64
    f = tmp_path / "release.bin"
    f.write_bytes(data)
    return f, hashlib.sha256(data).hexdigest()


def test_sha256_file_matches_hashing_lib(payload_file):
    f, expected = payload_file
    assert updater.sha256_file(str(f)) == expected


def test_verify_sha256_ok_and_mismatch(payload_file):
    f, expected = payload_file
    assert updater.verify_sha256(str(f), expected) is True
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        updater.verify_sha256(str(f), "0" * 64)


def test_fetch_manifest_validates_fields(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"version": "1.0.1", "url": "https://x/Zee-Setup-1.0.1.exe",
                    "sha256": "ab" * 32}

    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: _Resp())
    m = updater.fetch_manifest("https://x/latest.json")
    assert m["version"] == "1.0.1"

    class _Bad:
        def raise_for_status(self):
            pass

        def json(self):
            return {"version": "1.0.1", "url": "file:///etc/passwd",
                    "sha256": "ab" * 32}

    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: _Bad())
    with pytest.raises(ValueError, match="must be http"):
        updater.fetch_manifest("https://x/latest.json")


def test_download_streams_to_dest(monkeypatch, tmp_path):
    chunks = iter([b"a" * 256, b"b" * 128])

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=None):
            return chunks

    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: _Resp())
    dest = tmp_path / "out.bin"
    updater.download("https://x/f.bin", str(dest))
    assert dest.read_bytes() == b"a" * 256 + b"b" * 128


def test_atomic_replace_swaps_exe(monkeypatch, tmp_path):
    import sys
    fake_exe = tmp_path / "Zee.exe"
    fake_exe.write_bytes(b"old")
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    new = tmp_path / "new.exe"
    new.write_bytes(b"new")
    replaced = updater.atomic_replace(str(new))
    assert replaced == str(fake_exe)
    assert fake_exe.read_bytes() == b"new"
    assert not (tmp_path / "Zee.exe.old").exists()


def test_atomic_replace_noop_from_source(monkeypatch, tmp_path):
    """Running under python.exe (no Zee exe) — nothing to swap."""
    import sys
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    new = tmp_path / "new.exe"
    new.write_bytes(b"new")
    assert updater.atomic_replace(str(new)) is None


def test_installer_apply_uses_list_no_shell(monkeypatch):
    calls = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    code, _, _ = updater.installer_apply(r"C:\Zee-Setup-1.0.1.exe")
    assert code == 0
    assert calls and calls[0][0].endswith("Zee-Setup-1.0.1.exe")
    assert "/VERYSILENT" in calls[0]


def test_run_update_direct_asset_requires_sha(monkeypatch):
    with pytest.raises(ValueError, match="sha256 is required"):
        updater.run_update("https://x/Zee.exe")


def test_run_update_flow(monkeypatch, tmp_path, payload_file):
    """Manifest URL: download + verify + 'apply' via the silent installer."""
    f, expected = payload_file

    def fake_download(url, dest, timeout=120):
        with open(dest, "wb") as out:
            out.write(f.read_bytes())
        return dest

    monkeypatch.setattr(updater, "fetch_manifest", lambda url: {
        "version": "9.9.9", "url": "https://x/Zee-Setup-9.9.9.exe", "sha256": expected})
    monkeypatch.setattr(updater, "download", fake_download)
    monkeypatch.setattr(updater, "installer_apply",
                        lambda p: (0, "installed", ""))
    monkeypatch.setattr(updater.tempfile, "gettempdir",
                        lambda: str(tmp_path))

    summary = updater.run_update("https://x/latest.json", apply=True)
    assert summary["version"] == "9.9.9"
    assert summary["applied"] is True


def test_run_update_atomic_calls_shutdown_and_restart(monkeypatch, tmp_path, payload_file):
    """Bare-EXE update: --shutdown stops the daemon, --restart relaunches it."""
    f, expected = payload_file
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Zee.exe"))
    (tmp_path / "Zee.exe").write_bytes(b"old")

    def fake_download(url, dest, timeout=120):
        with open(dest, "wb") as out:
            out.write(f.read_bytes())
        return dest

    monkeypatch.setattr(updater, "fetch_manifest", lambda url: {
        "version": "9.9.9", "url": "https://x/Zee.exe", "sha256": expected})
    monkeypatch.setattr(updater, "download", fake_download)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    stopped, restarted = [], []
    monkeypatch.setattr(updater, "stop_daemon",
                        lambda *a, **k: (stopped.append(True), True)[1])
    monkeypatch.setattr(updater, "start_daemon",
                        lambda exe=None: (restarted.append(exe), None)[1])

    summary = updater.run_update("https://x/latest.json", apply=True,
                                 shutdown=True, restart=True)
    assert summary["applied"] is True
    assert summary["replaced"] == str(tmp_path / "Zee.exe")
    assert stopped == [True]
    assert restarted == [str(tmp_path / "Zee.exe")]
    assert (tmp_path / "Zee.exe").read_bytes() == b"ZEE-RELEASE-BINARY" * 64


def test_stop_daemon_sends_shutdown_with_token(monkeypatch):
    calls = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    def fake_post(url, data=None, headers=None, timeout=10, verify=False):
        calls["url"] = url
        calls["headers"] = headers
        return _Resp()

    monkeypatch.setenv("ZEE_TOKEN", "tok123")
    monkeypatch.setattr(updater.requests, "post", fake_post)
    assert updater.stop_daemon() is True
    assert calls["url"].endswith("/shutdown")
    assert calls["headers"]["X-ZEE-TOKEN"] == "tok123"


def test_stop_daemon_returns_false_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(updater.requests, "post", boom)
    assert updater.stop_daemon() is False


# ---------------- /update endpoint ----------------
def test_update_requires_token(client):
    resp = client.post("/update", data=json.dumps({"manifest": "https://x/l.json"}),
                       content_type="application/json")
    assert resp.status_code == 401


def test_update_validates_urls(client):
    resp = client.post("/update", data=json.dumps({"manifest": "file:///etc/passwd"}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 400
    resp = client.post("/update", data=json.dumps({}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 400


def test_update_starts_background_job(client, monkeypatch):
    started = threading.Event()

    def fake_run(url, sha256=None, apply=True):
        started.set()

    monkeypatch.setattr(updater, "run_update", fake_run)
    resp = client.post("/update",
                       data=json.dumps({"url": "https://x/Zee.exe",
                                        "sha256": "ab" * 32}),
                       content_type="application/json",
                       headers={"X-ZEE-TOKEN": "ci-test-token"})
    assert resp.status_code == 202
    assert resp.get_json()["ok"] is True
    assert started.wait(2), "background update never started"
