"""Optional Vosk model installer.

The wake-word voice loop needs a Vosk model, but the model is **not bundled**
with the app (models range from ~40 MB to ~1.8 GB). This helper downloads a
model tarball from the official mirror and unpacks it into the writable data
dir where :func:`zee_core.find_vosk_model` looks for it.

Usage::

    python tools/download_vosk_model.py                  # default small model
    python tools/download_vosk_model.py --model vosk-model-small-en-us-0.15
    python tools/download_vosk_model.py --url https://.../vosk-model.tar.gz

After install, ``zee daemon`` / the GUI voice loop picks the model up on the
next start (no rebuild or uninstall needed).
"""
import argparse
import os
import sys
import tarfile
import tempfile
import urllib.request

BASE = "https://alphacephei.com/vosk/models"
DEFAULT_MODEL = "vosk-model-small-en-us-0.15"


def model_dir():
    """Return the data-dir model folder, creating it if needed."""
    import apppaths
    path = os.path.join(apppaths.data_dir(), "model")
    os.makedirs(path, exist_ok=True)
    return path


def download(url, dest):
    """Stream ``url`` to ``dest`` (raises on error)."""
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1024 * 256):
            f.write(chunk)


def extract_model(tarball, dest):
    """Unpack a Vosk model tar.gz so the model folder lands under ``dest``."""
    with tarfile.open(tarball, "r:gz") as tf:
        members = [m for m in tf.getmembers() if m.name and not m.name.startswith("/")]

        def is_safe(member):
            # Reject absolute paths / parent traversal.
            norm = os.path.normpath(member.name)
            return not norm.startswith("..") and not os.path.isabs(norm)

        safe = [m for m in members if is_safe(m)]
        if len(safe) != len(members):
            raise ValueError("model archive contains unsafe paths")
        tf.extractall(dest, members=safe)
    return dest


def install(model=DEFAULT_MODEL, url=None):
    """Download + unpack a model into the data dir. Returns its folder path."""
    src = url or f"{BASE}/{model}.tar.gz"
    print(f"Downloading {src} ...")
    tmp = os.path.join(tempfile.gettempdir(), f"{model}.tar.gz")
    download(src, tmp)
    dest = model_dir()
    print(f"Unpacking into {dest} ...")
    extract_model(tmp, dest)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return os.path.join(dest, model)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="zee-model-installer",
        description="Download and unpack a Vosk speech model for ZEE (optional).")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"model folder name on alphacephei.com (default: {DEFAULT_MODEL})")
    parser.add_argument("--url", help="direct URL of a .tar.gz model (overrides --model)")
    args = parser.parse_args(argv)

    path = install(model=args.model, url=args.url)
    print(f"[OK] Model installed at: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
