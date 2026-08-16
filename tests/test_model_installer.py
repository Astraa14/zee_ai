"""Model installer helper (tools/download_vosk_model.py): safe tar handling."""

import io
import os
import tarfile

import pytest

from tools import download_vosk_model as m


def _make_tgz(members):
    """Build an in-memory .tar.gz from ``{name: bytes}`` members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_extract_model_unpacks_flat(monkeypatch, tmp_path):
    tgz = _make_tgz(
        {
            "vosk-model-test/am/final.mdl": b"amdata",
            "vosk-model-test/conf/model.conf": b"confdata",
        }
    )
    tarball = tmp_path / "model.tar.gz"
    tarball.write_bytes(tgz.read())
    m.extract_model(str(tarball), str(tmp_path / "out"))
    assert (tmp_path / "out" / "vosk-model-test" / "am" / "final.mdl").read_bytes() == b"amdata"
    assert (
        tmp_path / "out" / "vosk-model-test" / "conf" / "model.conf"
    ).read_bytes() == b"confdata"


def test_extract_model_rejects_traversal(monkeypatch, tmp_path):
    tgz = _make_tgz({"../evil.txt": b"pwned"})
    tarball = tmp_path / "evil.tar.gz"
    tarball.write_bytes(tgz.read())
    with pytest.raises(ValueError, match="unsafe"):
        m.extract_model(str(tarball), str(tmp_path / "out"))


def test_model_dir_created_under_data_dir(monkeypatch, tmp_path):
    import apppaths

    monkeypatch.setattr(apppaths, "data_dir", lambda: str(tmp_path / "data"))
    out = m.model_dir()
    assert out == str(tmp_path / "data" / "model")
    assert os.path.isdir(out)
