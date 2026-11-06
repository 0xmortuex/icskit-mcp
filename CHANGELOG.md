# Changelog

## 0.1.0

First release.

- Hand-written RFC 5545 parser: line folding, parameter quoting, TEXT escaping, BOM, CR/LF/CRLF,
  tolerant of damaged lines (reported, not swallowed).
- Time zones: IANA via zoneinfo, custom `VTIMEZONE` blocks (Outlook style) with the same
  gap/overlap policy, floating times, UTC, all-day dates.
- Recurrence engine: DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY (ordinals),
  BYMONTHDAY, BYMONTH, BYSETPOS, WKST, EXDATE, RDATE, RECURRENCE-ID overrides.
- Tools: calendar_overview, agenda, search_events, find_free_time, find_conflicts,
  next_occurrences, export_agenda_markdown, create_event, update_event, delete_event.
- Safe writes: edited copy by default, dry-run patch, `in_place` opt-in, byte-identical round trip.
