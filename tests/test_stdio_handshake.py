"""MCP stdio test: launches the installed server as a subprocess, lists the
tools and calls them over real JSON-RPC, including error paths (mcp>=2 only
forwards ToolError messages to the client)."""

import hashlib
import shutil
import sys

import pytest
from conftest import FIX

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "calendar_overview",
    "agenda",
    "search_events",
    "find_free_time",
    "find_conflicts",
    "next_occurrences",
    "export_agenda_markdown",
    "create_event",
    "update_event",
    "delete_event",
}


@pytest.fixture
def cal_dir(tmp_path):
    for name in ("google.ics", "apple.ics", "outlook.ics"):
        shutil.copy(FIX / name, tmp_path / name)
    return tmp_path


def params(cal_dir):
    return StdioServerParameters(command=sys.executable, args=["-m", "icskit_mcp", "--dir", str(cal_dir)])


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(cal_dir):
    async with stdio_client(params(cal_dir)) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "icskit"
        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS, (names - EXPECTED_TOOLS, EXPECTED_TOOLS - names)
        by_name = {t.name: t for t in tools}
        assert "never modified unless in_place=true" in by_name["create_event"].description
        assert "tz" in by_name["agenda"].input_schema["properties"]

        res = await session.call_tool("calendar_overview", {})
        assert not res.is_error and "google.ics" in res.content[0].text and "Europe/Istanbul" in res.content[0].text

        res = await session.call_tool(
            "agenda", {"start": "2026-03-27", "end": "2026-03-31", "tz": "Europe/Berlin"})
        text = res.content[0].text
        assert not res.is_error and "zone Europe/Berlin" in text
        assert "01:30-04:30 CEST  Night shift" in text  # DST-correct end over the wire

        res = await session.call_tool(
            "find_free_time", {"start": "2026-03-09", "end": "2026-03-09", "duration_minutes": 60,
                               "tz": "Europe/Istanbul", "days": "Mon"})
        assert not res.is_error and "09:00-14:30 +03" in res.content[0].text


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(cal_dir):
    async with stdio_client(params(cal_dir)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("agenda", {"start": "2026-03-01", "end": "2026-03-02", "tz": "CEST"})
        assert res.is_error and "IANA" in res.content[0].text
        res = await session.call_tool("agenda", {"start": "tomorrow", "end": "2026-03-02"})
        assert res.is_error and "cannot read start" in res.content[0].text
        res = await session.call_tool("update_event", {"event_uid": "nope", "summary": "x"})
        assert res.is_error and "no event with UID" in res.content[0].text
        res = await session.call_tool("create_event", {"calendar": "../x.ics", "summary": "x", "start": "2026-03-01"})
        assert res.is_error and "outside the allowed directory" in res.content[0].text


@pytest.mark.anyio
async def test_write_tools_over_stdio_leave_the_original_alone(cal_dir):
    before = hashlib.sha256((cal_dir / "google.ics").read_bytes()).hexdigest()
    async with stdio_client(params(cal_dir)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("create_event", {
            "calendar": "google.ics", "summary": "Over the wire", "start": "2026-03-10T15:00",
            "duration_minutes": 30, "tz": "Europe/Istanbul"})
        assert not res.is_error and "google.ics.new" in res.content[0].text
        assert hashlib.sha256((cal_dir / "google.ics").read_bytes()).hexdigest() == before
        assert b"Over the wire" in (cal_dir / "google.ics.new").read_bytes()

        res = await session.call_tool("create_event", {
            "calendar": "google.ics", "summary": "Real one", "start": "2026-03-10T16:00",
            "duration_minutes": 30, "tz": "Europe/Istanbul", "in_place": True})
        assert not res.is_error and "IN PLACE" in res.content[0].text
        res = await session.call_tool("agenda", {"start": "2026-03-10", "end": "2026-03-10", "tz": "Europe/Istanbul"})
        assert "16:00-16:30 +03  Real one" in res.content[0].text


def test_missing_dir_flag_exits_with_a_message(monkeypatch):
    import subprocess

    env = {k: v for k, v in __import__("os").environ.items() if k != "ICSKIT_MCP_DIR"}
    p = subprocess.run([sys.executable, "-m", "icskit_mcp"], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 2 and "--dir is required" in p.stderr
    p = subprocess.run([sys.executable, "-m", "icskit_mcp", "--dir", "Z:/definitely/not/here"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "not a directory" in p.stderr
