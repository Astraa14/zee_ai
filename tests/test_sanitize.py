"""sanitize_input: the shared input guard for zee_core and win_control.

Rejects empty/oversized input, control characters and shell metacharacters
so untrusted strings never reach a subprocess or URL.
"""

import pytest

import win_control
import zee_core

DANGEROUS = [
    "a;b",
    "a&b",
    "a|b",
    "a`b",
    "a$b",
    "a\nb",
    "a\rb",
    "a\x00b",
    "rm -rf /; echo hi",
    "&&calc",
    "whoami$()",
]
CLEAN = ["hello world", "John Doe", "notepad", "youtube.com", "file-name_v2.pdf"]


@pytest.mark.parametrize("module", [zee_core, win_control])
@pytest.mark.parametrize("bad", DANGEROUS)
def test_sanitize_rejects_dangerous(module, bad):
    assert module.sanitize_input(bad) is None, repr(bad)


@pytest.mark.parametrize("module", [zee_core, win_control])
@pytest.mark.parametrize("good", CLEAN)
def test_sanitize_accepts_clean(module, good):
    assert module.sanitize_input(good) == good


@pytest.mark.parametrize("module", [zee_core, win_control])
def test_sanitize_edge_cases(module):
    assert module.sanitize_input(None) is None
    assert module.sanitize_input("") is None
    assert module.sanitize_input("   ") is None
    assert module.sanitize_input("x" * 121) is None  # over default 120 limit
    assert module.sanitize_input("x" * 120) == "x" * 120  # exactly at the limit
    trimmed = module.sanitize_input("  padded  ")
    assert trimmed == "padded"


@pytest.mark.parametrize("module", [zee_core, win_control])
def test_sanitize_custom_maxlen(module):
    assert module.sanitize_input("short-fine", 10) == "short-fine"
    assert module.sanitize_input("too-long-for-ten", 10) is None
