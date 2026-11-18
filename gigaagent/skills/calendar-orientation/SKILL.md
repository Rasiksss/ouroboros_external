---
name: calendar-orientation
description: Verify relative dates, weekdays, and calendar offsets with calendar_resolve.
type: instruction
when_to_use: The user mentions relative dates, weekdays, future dates, schedules, deadlines, or date consistency.
---

# Calendar Orientation

Use `calendar_resolve` whenever an answer depends on calendar arithmetic.

- For today-relative expressions such as "завтра", "через неделю", or "через месяц", call `date_offset` with `reference_datetime` from runtime context.
- For "следующий вторник" and similar expressions, call `date_for_weekday`.
- Before stating a weekday for a known date, call `weekday_for_date`.
- If an answer contains both a date and weekday, verify that they match.
- Treat the returned timezone and date as authoritative. Do not recalculate them mentally.

The tool does not know holidays, business calendars, room availability, or meeting participants.
