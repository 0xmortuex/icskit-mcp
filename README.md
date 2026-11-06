# icskit-mcp

**Let your AI work with the `.ics` calendar files on your own disk: agenda, search, free time, conflicts and safe edits. No account, no OAuth, no network.**

<!-- mcp-name: io.github.0xmortuex/icskit-mcp -->

icskit-mcp is an [MCP](https://modelcontextprotocol.io) server for local iCalendar files: the
`.ics` export from Google Calendar, Apple Calendar or Outlook, or a folder of them.

**What is different about it.** Other `.ics` MCP servers lean on a calendar library or only read
published feeds. This one ships its own RFC 5545 parser and recurrence engine, and the part that is
usually wrong in calendar tools is the part that is tested hardest:

- **Daylight saving is exact.** A 09:00 Berlin meeting stays at 09:00 Berlin across the clock change;
  a time that falls in the spring-forward gap or the fall-back overlap is resolved the way RFC 5545
  says; `DTEND` lengths are real elapsed time; free-time search keeps your wall-clock working hours
  on 23 and 25 hour days. Tested against `America/New_York`, `Europe/Berlin` and `Europe/Istanbul`
  (which has no DST), for IANA zones *and* for custom `VTIMEZONE` blocks such as Outlook's
  `W. Europe Standard Time`.
- **Recurrence is checked against the spec.** 30 worked examples from RFC 5545 section 3.8.5.3
  (the ones that use supported parts) are test cases, and about a thousand randomized rules are
  compared against `python-dateutil` as an independent oracle.
- **Writes are safe by construction.** Editing works by line surgery: everything you did not ask to
  change (other events, `VTIMEZONE`, `VALARM`, unknown `X-` properties, folding, line endings, a
  BOM) stays byte for byte. By default the original is never touched; you get an edited copy or a
  patch.
- **Local only.** Nothing is sent anywhere. Attendee email addresses are just text in the file.
- Several calendars at once: free time and conflicts work across all of them.

It is free and MIT licensed, with no paid tier.

## Install

You need [uv](https://docs.astral.sh/uv/). Pick the folder that holds your `.ics` files; the server
can see **only** that folder (searched recursively).

**Claude Code**

```bash
claude mcp add icskit -- uvx icskit-mcp --dir "/path/to/calendars"
```

**Claude Desktop** - add this to `claude_desktop_config.json` (Settings, Developer, Edit config):

```json
{
  "mcpServers": {
    "icskit": {
      "command": "uvx",
      "args": ["icskit-mcp", "--dir", "C:\\Users\\you\\Calendars"]
    }
  }
}
```

(`--dir` can also be given as the `ICSKIT_MCP_DIR` environment variable.)

**Getting your calendars out**

- Google: Settings, Import & export, Export. You get a zip of one `.ics` per calendar.
- Apple Calendar: File, Export, Export...
- Outlook: File, Save Calendar (or "Export" in the new Outlook), choose iCalendar format.

## Try asking

- "What does my week of 2026-03-02 look like? Use Europe/Istanbul."
- "When am I free for 90 minutes next Tuesday and Wednesday, between 10:00 and 16:00, with 15 minutes of buffer around meetings?"
- "Do any of my calendars have meetings that overlap this month?"
- "What are the next 6 times my 'Board sync' happens?"
- "Find every event with anna@example.com that is not cancelled."
- "Add a dentist appointment on 2026-03-10 at 15:00 for 45 minutes (Europe/Istanbul). Show me the patch first."
- "Cancel just the 2026-03-11 occurrence of the Monday/Wednesday/Friday standup."
- "Write my week as Markdown notes."

## Tools

| Tool | What it does |
|------|--------------|
| `calendar_overview` | Files, calendar names, event and recurring-series counts, date span, time zones used, and every problem found (files that failed to load, events skipped and why). Call first. |
| `agenda` | Every occurrence in a window, sorted and grouped by day, recurring events expanded (RRULE, EXDATE, RDATE, modified instances), recurrence marked. |
| `search_events` | Text, attendee, location or regex search; lists events/series, or expanded occurrences when you give a window. |
| `find_free_time` | Free blocks of at least N minutes inside working hours and working days, across one or several calendars, with buffers. |
| `find_conflicts` | Overlapping events, including across calendars. All-day and "free" (TRANSPARENT) events are ignored unless you ask. |
| `next_occurrences` | The next N occurrences of one event by UID. |
| `export_agenda_markdown` | A seven day agenda as Markdown, optionally saved to a new `.md` file. |
| `create_event` / `update_event` / `delete_event` | Safe writes (below). `delete_event` can remove a single occurrence of a recurring event (adds an `EXDATE`). |

**Time zones.** Every tool takes `tz` (an IANA name such as `Europe/Istanbul`; default `UTC`) and
every time in every answer is printed with its zone, never silently local. Floating times (no zone
in the file) are read in the `tz` you pass. All-day events are dates and are not shifted by `tz`.
A date-only `end` such as `2026-03-08` includes that whole day.

## Safe writing

`create_event`, `update_event` and `delete_event` never modify your calendar unless you say so:

| You pass | What happens |
|----------|--------------|
| nothing | The edited calendar is written next to the original as `<file>.new`. The original is untouched. An existing `.new` file is never overwritten. |
| `dry_run=true` | Nothing is written; you get the patch (a unified diff). |
| `output_file="x.ics"` | The edited copy goes to that new file (it must not exist). |
| `in_place=true` | The original is replaced atomically (and refused if the file changed on disk since it was read). |

Every result shows the patch. Edited files are parsed again before they are written, and a
create-then-delete round trip is tested to reproduce the original file byte for byte.
`update_event` bumps `SEQUENCE` and refreshes `DTSTAMP`; new events get a generated UID.

## Limits (read these)

- Recurrence supports `DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY` with `INTERVAL`, `COUNT`, `UNTIL`,
  `BYDAY` (with ordinals like `2MO`, `-1FR`), `BYMONTHDAY`, `BYMONTH`, `BYSETPOS`, `WKST`, plus
  `EXDATE`, `RDATE` and `RECURRENCE-ID` overrides. `HOURLY` and finer, `BYWEEKNO`, `BYYEARDAY` and
  `BYHOUR`-style parts are **rejected with a message** (the event is skipped and listed under
  problems), never silently mis-expanded.
- `DTSTART` is always treated as the first instance, as RFC 5545 defines it, even if the rule
  would not generate it.
- `RECURRENCE-ID;RANGE=THISANDFUTURE` is reported and applied to one instance only.
- Only `VEVENT` is read (not `VTODO`/`VJOURNAL`). Updates change the whole series; editing a single
  instance is not supported (delete one occurrence is).
- New events reference an IANA `TZID` without embedding a `VTIMEZONE`; Google and Apple accept that,
  some Outlook versions may not.
- A `TZID` that is neither an IANA name nor defined by a `VTIMEZONE` in the same file (for example a
  bare Windows name without its definition) skips that event with an explanation.
- Files must be UTF-8. Attendee changes are recorded in the file only; no invitation is sent.

## Security

- The server reads and writes **only** `.ics` files under `--dir`. Paths that escape it (`..`,
  absolute paths elsewhere, symlinks pointing outside) are refused.
- It makes no network connections and needs no credentials.
- Text you pass to write tools is escaped; newlines cannot inject extra properties, and control
  characters are refused.
- The only file other than `.ics` it can create is the Markdown export, and only as a new file.
- Calendar contents (titles, descriptions) come from your files and may contain text written by other
  people. Treat them as data, not instructions.

## Development

```bash
uv sync --extra test
uv run pytest
uv run ruff check src tests && uv run mypy --strict src
```

A companion tutorial that builds a minimal version from scratch is in
[icskit-mcp-the-hard-way](https://github.com/0xmortuex/icskit-mcp-the-hard-way).

MIT licensed.
