"""Auth token persistence: OS credential store (python-keyring) with a file
fallback.

The browser-only web token lives in the environment (``ZEE_TOKEN``) when set,
otherwise in the Windows Credential Locker / keyring, and always mirrored to
``.zee_token`` in the writable data dir so non-keyring callers (e.g. requests
from the CLI) can find it.
"""
import logging
import secrets

import apppaths

log = logging.getLogger("zee")
_SERVICE = "ZEE"
_ACCOUNT = "web-ui-token"
TOKEN_FILE = apppaths.data_path(".zee_token")


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def read_token():
    """Return the persisted token (keyring first), or None if unset."""
    kr = _keyring()
    if kr is not None:
        try:
            tok = kr.get_password(_SERVICE, _ACCOUNT)
            if tok:
                return tok
        except Exception as e:  # noqa: BLE001 — backend glitches must not block
            log.debug(f"keyring read failed: {e}")
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
        return tok or None
    except OSError:
        return None


def write_token(token=None):
    """Store ``token`` (or generate a fresh one) in keyring and the file."""
    tok = token or secrets.token_urlsafe(24)
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_SERVICE, _ACCOUNT, tok)
        except Exception as e:  # noqa: BLE001
            log.debug(f"keyring write failed: {e}")
    try:
        apppaths.ensure_data_dir()
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError as e:
        log.error(f"Could not persist auth token: {e}")
    return tok
