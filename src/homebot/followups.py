"""One-off follow-ups: "check that torrent again in 30 minutes".

A follow-up is a message the bot sends to itself later. It is stored on disk so
a container restart does not lose it, and it fires through the same path the
scheduled routines use, so the bot treats it as an ordinary incoming message.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

Approver = Callable[[str], Awaitable[bool]]

MAX_PENDING = 50
MAX_DELAY_DAYS = 30
MIN_DELAY_SECONDS = 30


class FollowupError(RuntimeError):
    """Reported back to Claude as a failed tool result."""


@dataclass(frozen=True)
class Followup:
    id: str
    due: str  # ISO 8601, UTC
    message: str
    asked_by: str
    created: str

    @property
    def due_at(self) -> datetime.datetime:
        return datetime.datetime.fromisoformat(self.due)

    def describe(self, tz: ZoneInfo) -> str:
        local = self.due_at.astimezone(tz)
        delta = self.due_at - datetime.datetime.now(datetime.timezone.utc)
        mins = delta.total_seconds() / 60
        when = "overdue" if mins < 0 else (f"in {mins:.0f}min" if mins < 90 else f"in {mins / 60:.1f}h")
        return f"{self.id} | {local.strftime('%a %H:%M')} ({when}) | asked by {self.asked_by} | {self.message}"


class FollowupStore:
    """Pending follow-ups, persisted as JSON. Small enough to rewrite whole."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: list[Followup] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("could not read %s, starting empty: %s", self._path, exc)
            return
        for entry in raw if isinstance(raw, list) else []:
            try:
                item = Followup(**entry)
                item.due_at  # validate the timestamp now, not when it fires
            except (TypeError, ValueError) as exc:
                log.warning("dropping malformed follow-up %r: %s", entry, exc)
                continue
            self._items.append(item)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling then rename, so a crash mid-write cannot leave a
        # truncated file that loses every pending follow-up.
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, prefix=".followups-", delete=False
        )
        try:
            json.dump([asdict(i) for i in self._items], handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self._path)

    def all(self) -> list[Followup]:
        return sorted(self._items, key=lambda i: i.due)

    def add(self, due: datetime.datetime, message: str, asked_by: str) -> Followup:
        if len(self._items) >= MAX_PENDING:
            raise FollowupError(f"There are already {MAX_PENDING} follow-ups pending. Cancel some first.")
        item = Followup(
            id=secrets.token_hex(2),
            due=due.astimezone(datetime.timezone.utc).isoformat(),
            message=message,
            asked_by=asked_by,
            created=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self._items.append(item)
        self._save()
        return item

    def cancel(self, followup_id: str) -> Followup | None:
        for index, item in enumerate(self._items):
            if item.id == followup_id:
                self._items.pop(index)
                self._save()
                return item
        return None

    def pop_due(self, now: datetime.datetime | None = None) -> list[Followup]:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        due = [i for i in self._items if i.due_at <= now]
        if due:
            self._items = [i for i in self._items if i.due_at > now]
            self._save()
        return due


def parse_when(
    delay_minutes: Any, at_time: Any, tz: ZoneInfo, now: datetime.datetime | None = None
) -> datetime.datetime:
    """Turn 'in 30 minutes' or '14:30' / '2026-10-02 14:30' into an absolute time."""
    now = now or datetime.datetime.now(tz)
    if delay_minutes not in (None, ""):
        try:
            minutes = float(delay_minutes)
        except (TypeError, ValueError) as exc:
            raise FollowupError("delay_minutes must be a number") from exc
        if minutes * 60 < MIN_DELAY_SECONDS:
            raise FollowupError(f"The soonest a follow-up can be is {MIN_DELAY_SECONDS} seconds away.")
        due = now + datetime.timedelta(minutes=minutes)
    elif isinstance(at_time, str) and at_time.strip():
        due = _parse_at(at_time.strip(), tz, now)
    else:
        raise FollowupError("Give either delay_minutes or at_time.")
    if due - now > datetime.timedelta(days=MAX_DELAY_DAYS):
        raise FollowupError(f"Follow-ups cannot be more than {MAX_DELAY_DAYS} days out.")
    return due


def _parse_at(text: str, tz: ZoneInfo, now: datetime.datetime) -> datetime.datetime:
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = (int(p) for p in text.split(":"))
        try:
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError as exc:
            raise FollowupError(f"{text!r} is not a valid time of day") from exc
        # A time that has already passed today means tomorrow.
        return due + datetime.timedelta(days=1) if due <= now else due
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise FollowupError(
            f"Could not read {text!r} as a time. Use HH:MM or YYYY-MM-DD HH:MM."
        ) from exc
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


class FollowupTools:
    def __init__(self, store: FollowupStore, tz: ZoneInfo) -> None:
        self._store = store
        self._tz = tz

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "schedule_followup",
                "description": (
                    "Send yourself a message later, to check back on something. Use when an action needs "
                    "time to take effect, e.g. after a search or a grab: 'check whether that torrent "
                    "started in 30 minutes'. Write the message as an instruction to your future self, "
                    "including the ids or names needed, because you will not remember this conversation. "
                    "Give either delay_minutes or at_time, not both."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "delay_minutes": {"type": "number", "description": "How long from now, in minutes."},
                        "at_time": {
                            "type": "string",
                            "description": "Clock time 'HH:MM' (next occurrence) or 'YYYY-MM-DD HH:MM', house time.",
                        },
                        "message": {"type": "string", "description": "What your future self should do."},
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_followups",
                "description": "List the follow-ups you have scheduled and not yet delivered.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "cancel_followup",
                "description": "Cancel one pending follow-up by the id shown in list_followups.",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        ]

    @property
    def names(self) -> set[str]:
        return {d["name"] for d in self.definitions}

    async def run(self, name: str, tool_input: dict[str, Any], _approve: Approver) -> str:
        if name == "schedule_followup":
            message = tool_input.get("message")
            if not isinstance(message, str) or not message.strip():
                raise FollowupError("message is required")
            due = parse_when(tool_input.get("delay_minutes"), tool_input.get("at_time"), self._tz)
            item = self._store.add(due, message.strip(), tool_input.get("_sender", "you"))
            local = due.astimezone(self._tz)
            return (
                f"Follow-up {item.id} saved for {local.strftime('%a %H:%M')}. "
                "It survives a restart. Tell the user when you will check back."
            )
        if name == "list_followups":
            items = self._store.all()
            if not items:
                return "No follow-ups pending."
            return "\n".join(i.describe(self._tz) for i in items)
        if name == "cancel_followup":
            followup_id = str(tool_input.get("id") or "").strip()
            item = self._store.cancel(followup_id)
            if item is None:
                raise FollowupError(f"No pending follow-up with id {followup_id!r}.")
            return f"Cancelled follow-up {item.id} ({item.message})."
        raise FollowupError(f"Unknown tool {name!r}")
