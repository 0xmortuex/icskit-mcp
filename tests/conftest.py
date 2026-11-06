import shutil
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def ics(*lines: str, nl: str = "\r\n") -> str:
    """Build an iCalendar document with explicit line endings."""
    return nl.join(lines) + nl


def vcal(*body: str, nl: str = "\r\n") -> str:
    return ics("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", *body, "END:VCALENDAR", nl=nl)


def write(path: Path, text: str) -> Path:
    """Write text byte-exactly (no newline translation)."""
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def caldir(tmp_path):
    """A sandbox directory holding copies of the hand-written exports."""
    for name in ("google.ics", "apple.ics", "outlook.ics"):
        shutil.copy(FIX / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def configured(caldir):
    from icskit_mcp import server

    lib = server.configure(caldir)
    yield lib
    server._lib = None
