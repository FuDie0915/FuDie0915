#!/usr/bin/env python3
"""Render the last 31 UTC days of a viewer-visible GitHub contribution calendar.

Uses only the Python standard library. Online requests require GITHUB_TOKEN or
GH_TOKEN. --calendar-json accepts a saved response from the same GraphQL query
for offline validation without reading a token or making a network request.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.github.com/graphql"
DAY_COUNT = 31
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
USERNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
QUERY = """
query ActivityGraph($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    login
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""


class GraphError(Exception):
    """An expected input, API, or output failure safe to report to the caller."""


def decode_response(raw: bytes) -> object:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GraphError("The calendar response exceeds the size limit.")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GraphError("The calendar response is not valid UTF-8 JSON.") from error


def fetch_calendar(username: str, first_day: date, now: datetime) -> object:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not token.strip():
        raise GraphError("Set GITHUB_TOKEN or GH_TOKEN, or use --calendar-json.")
    token = token.strip()
    if any(character.isspace() for character in token):
        raise GraphError("The GitHub token contains invalid whitespace.")

    start = datetime.combine(first_day, time.min, tzinfo=timezone.utc)
    variables = {
        "login": username,
        "from": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "to": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    request = Request(
        API_URL,
        data=json.dumps({"query": QUERY, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "github-profile-activity-graph",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise GraphError(f"GitHub GraphQL returned HTTP {response.status}.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        # Do not print headers, request bodies, or credentials on API failures.
        raise GraphError(f"GitHub GraphQL returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise GraphError("Could not complete the GitHub GraphQL request.") from error
    return decode_response(raw)


def read_fixture(path: Path) -> object:
    try:
        with path.open("rb") as fixture:
            return decode_response(fixture.read(MAX_RESPONSE_BYTES + 1))
    except OSError as error:
        raise GraphError(f"Could not read the calendar fixture: {path}") from error


def require_object(value: object, location: str) -> dict:
    if not isinstance(value, dict):
        raise GraphError(f"Invalid calendar schema at {location}: expected an object.")
    return value


def parse_calendar(
    payload: object, username: str, first_day: date, last_day: date
) -> list[tuple[date, int]]:
    root = require_object(payload, "response")
    errors = root.get("errors")
    if errors is not None:
        if not isinstance(errors, list):
            raise GraphError("Invalid GraphQL errors schema.")
        if errors:
            messages = []
            for error in errors[:3]:
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    messages.append(" ".join(error["message"].split())[:250])
            detail = "; ".join(messages) or "The query was not successful."
            raise GraphError(f"GitHub GraphQL error: {detail}")

    data = require_object(root.get("data"), "data")
    if data.get("user") is None:
        raise GraphError(f"GitHub did not return the requested user: {username}")
    user = require_object(data.get("user"), "data.user")
    returned_login = user.get("login")
    if not isinstance(returned_login, str) or returned_login.casefold() != username.casefold():
        raise GraphError("The returned GitHub login does not match the requested user.")
    collection = require_object(user.get("contributionsCollection"), "contributionsCollection")
    calendar = require_object(collection.get("contributionCalendar"), "contributionCalendar")
    weeks = calendar.get("weeks")
    if not isinstance(weeks, list) or not weeks:
        raise GraphError("Invalid calendar schema: weeks must be a non-empty array.")

    selected = {
        first_day + timedelta(days=index): 0
        for index in range((last_day - first_day).days + 1)
    }
    seen = set()
    in_range = 0
    for week in weeks:
        week = require_object(week, "contributionCalendar.weeks[]")
        days = week.get("contributionDays")
        if not isinstance(days, list) or not days:
            raise GraphError("Invalid calendar schema: contributionDays must be a non-empty array.")
        for item in days:
            item = require_object(item, "contributionDays[]")
            raw_date = item.get("date")
            if not isinstance(raw_date, str):
                raise GraphError("Invalid calendar schema: a contribution date is missing.")
            try:
                day = date.fromisoformat(raw_date)
            except ValueError as error:
                raise GraphError("Invalid calendar schema: a contribution date is invalid.") from error
            if day.isoformat() != raw_date:
                raise GraphError("Invalid calendar schema: dates must use YYYY-MM-DD.")
            count = item.get("contributionCount")
            if type(count) is not int or not 0 <= count <= 2_147_483_647:
                raise GraphError("Invalid calendar schema: counts must be non-negative integers.")
            if day in seen:
                raise GraphError(f"Invalid calendar schema: duplicate date {day.isoformat()}.")
            seen.add(day)
            if day in selected:
                selected[day] = count
                in_range += 1
    if not in_range:
        raise GraphError("The calendar contains no dates in the requested UTC range.")
    return list(selected.items())


def axis_scale(maximum: int) -> tuple[int, int]:
    rough_step = max(1.0, maximum / 4)
    magnitude = 10 ** math.floor(math.log10(rough_step))
    fraction = rough_step / magnitude
    step = next(value for value in (1, 2, 5, 10) if fraction <= value) * magnitude
    upper = max(step * 4, math.ceil(maximum / step) * step)
    return int(step), int(upper)


def render_svg(username: str, days: list[tuple[date, int]]) -> str:
    if len(days) != DAY_COUNT:
        raise GraphError(f"The graph requires exactly {DAY_COUNT} days.")
    safe_username = escape(username, quote=True)
    total = sum(count for _, count in days)
    first_day, last_day = days[0][0], days[-1][0]
    step, upper = axis_scale(max(count for _, count in days))
    left, right, top, bottom = 64.0, 976.0, 88.0, 244.0
    points = [
        (left + index * (right - left) / (DAY_COUNT - 1), bottom - count * (bottom - top) / upper)
        for index, (_, count) in enumerate(days)
    ]
    point_text = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    daily_description = "; ".join(f"{day.isoformat()}: {count}" for day, count in days)
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="300" '
        'viewBox="0 0 1000 300" role="img" aria-labelledby="activity-title activity-desc" '
        'style="width:100%;max-width:1000px;height:auto">',
        f'  <title id="activity-title">{safe_username}: GitHub contribution activity</title>',
        '  <desc id="activity-desc">'
        f'{safe_username}, {first_day.isoformat()} through {last_day.isoformat()} UTC. '
        f'{total} contributions across {DAY_COUNT} days, as returned by the GitHub calendar '
        f'for the requesting viewer. Daily counts: {daily_description}.</desc>',
        '  <rect x="0.5" y="0.5" width="999" height="299" rx="8" fill="#0d1117" '
        'stroke="#ffffff" stroke-opacity="0.18"/>',
        '  <g font-family="sans-serif" fill="#5bcdec">',
        f'    <text x="24" y="32" font-size="18" font-weight="600">'
        f"{safe_username}'s contribution activity</text>",
        f'    <text x="24" y="53" font-size="12" fill-opacity="0.82">'
        f'{first_day.isoformat()} — {last_day.isoformat()} · UTC · {DAY_COUNT} days</text>',
        f'    <text x="976" y="33" text-anchor="end" font-size="15" font-weight="600">'
        f'{total:,} contributions</text>',
    ]
    for value in range(0, upper + 1, step):
        y = bottom - value * (bottom - top) / upper
        lines.extend([
            f'    <line x1="{left:.2f}" y1="{y:.2f}" x2="{right:.2f}" y2="{y:.2f}" '
            'stroke="#5bcdec" stroke-opacity="0.16" stroke-width="1"/>',
            f'    <text x="52" y="{y + 4:.2f}" text-anchor="end" font-size="11" '
            f'fill-opacity="0.82">{value:,}</text>',
        ])
    for index in (0, 5, 10, 15, 20, 25, 30):
        x = points[index][0]
        day = days[index][0]
        lines.extend([
            f'    <line x1="{x:.2f}" y1="{top:.2f}" x2="{x:.2f}" y2="{bottom:.2f}" '
            'stroke="#5bcdec" stroke-opacity="0.10" stroke-width="1"/>',
            f'    <text x="{x:.2f}" y="271" text-anchor="middle" font-size="11" '
            f'fill-opacity="0.82">{day.month:02d}-{day.day:02d}</text>',
        ])
    lines.extend([
        f'    <polygon points="{left:.2f},{bottom:.2f} {point_text} '
        f'{right:.2f},{bottom:.2f}" fill="#5bcdec" fill-opacity="0.10"/>',
        f'    <polyline points="{point_text}" fill="none" stroke="#5bcdec" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    ])
    for (day, count), (x, y) in zip(days, points):
        noun = "contribution" if count == 1 else "contributions"
        lines.append(
            f'    <circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#ffffff" '
            f'stroke="#0d1117" stroke-width="1"><title>{day.isoformat()}: '
            f'{count} {noun}</title></circle>'
        )
    lines.extend(['  </g>', '</svg>', ''])
    return "\n".join(lines)


def write_atomic(output: Path, svg: str) -> bool:
    encoded = svg.encode("utf-8")
    temporary = None
    try:
        try:
            if output.read_bytes() == encoded:
                return False
        except FileNotFoundError:
            pass
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False
        ) as destination:
            temporary = Path(destination.name)
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output)
        temporary = None
        return True
    except OSError as error:
        raise GraphError(f"Could not atomically write the SVG: {output}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="FuDie0915", help="GitHub login (default: FuDie0915)")
    parser.add_argument(
        "--output", type=Path, default=Path("assets/activity-graph.svg"), help="SVG output path"
    )
    parser.add_argument(
        "--calendar-json", type=Path, help="Read a saved GraphQL JSON response instead of the API"
    )
    args = parser.parse_args(argv)
    if not USERNAME_RE.fullmatch(args.username) or "--" in args.username:
        parser.error("--username must be a valid GitHub login")

    now = datetime.now(timezone.utc)
    last_day = now.date()
    first_day = last_day - timedelta(days=DAY_COUNT - 1)
    try:
        payload = (
            read_fixture(args.calendar_json)
            if args.calendar_json is not None
            else fetch_calendar(args.username, first_day, now)
        )
        days = parse_calendar(payload, args.username, first_day, last_day)
        updated = write_atomic(args.output, render_svg(args.username, days))
    except GraphError as error:
        print(f"activity graph: {error}", file=sys.stderr)
        return 1
    print(f"{'Updated' if updated else 'Unchanged'}: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
