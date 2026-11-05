#!/usr/bin/env python3
"""Turn an "Add an event" issue into a page under `content/events`.

Reads the body of an issue created from `.github/ISSUE_TEMPLATE/new-event.yml`,
validates every field, and writes `content/events/<slug>.md` with the same TOML
frontmatter the hand-written event pages use.

The issue body is untrusted input. Values are validated against strict rules
and written as escaped TOML strings, never interpolated into a shell.

Environment:
  ISSUE_NUMBER     the issue number (required)
  ISSUE_BODY_FILE  path to a file holding the issue body (required)
  EVENTS_DIR       events section directory (default: content/events)

Pass --check to validate and report without writing the event file.

Writes these keys to $GITHUB_OUTPUT when it is set:
  path, branch, title, date, event_type   on success
  errors                                  on failure (Markdown list)
"""

import datetime
import json
import os
import pathlib
import re
import secrets
import sys
import unicodedata
import urllib.parse

# Headings in the issue body come from the `label` of each form field.
FIELD_NAME = "Event name"
FIELD_DATE = "Event date"
FIELD_TYPE = "Event type"
FIELD_LOCATION = "Location"
FIELD_LOCATION_URL = "Location map URL"
FIELD_EVENT_URL = "Event URL"

# Dropdown option text -> `event_type` value understood by templates/events.html.
EVENT_TYPES = {
    "Third-party event": "third-party",
    "Meetup or hackathon": "meetup-hackathon",
    "Valkey event (organized by the Valkey project)": "first-party",
}

NO_RESPONSE = "_No response_"
MAX_TITLE = 120
MAX_LOCATION = 200
MAX_URL = 500
MAX_SLUG = 80
MAX_BRANCH_NAME = 60
# Matches the time of day every existing event page uses.
EVENT_TIME = "01:01:01"


def parse_form(body):
    """Return {heading: value} for the `### Heading` sections of an issue form."""
    fields = {}
    parts = re.split(r"^###[ \t]+(.+?)[ \t]*$", body.replace("\r\n", "\n"), flags=re.M)
    # parts = [preamble, heading1, value1, heading2, value2, ...]
    for heading, value in zip(parts[1::2], parts[2::2]):
        value = value.strip()
        fields[heading.strip()] = "" if value == NO_RESPONSE else value
    return fields


def clean_line(value):
    """Collapse whitespace and drop control characters from a one-line value."""
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t\n")
    return " ".join(value.split())


def validate_url(value, field, errors, required):
    if not value:
        if required:
            errors.append(f"**{field}** is required.")
        return ""
    if len(value) > MAX_URL or any(ch.isspace() for ch in value):
        errors.append(f"**{field}** must be a single URL of at most {MAX_URL} characters.")
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        errors.append(f"**{field}** must be an http or https URL.")
        return ""
    return value


def ascii_words(value):
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return [word for word in re.split(r"[^A-Za-z0-9]+", ascii_text) if word]


def slugify(title, year):
    words = [word.lower() for word in ascii_words(title)]
    if year not in words:
        words.append(year)
    slug = "-".join(words)[:MAX_SLUG].strip("-")
    return slug or f"event-{year}"


def pascal_case(title):
    name = "".join(word[0].upper() + word[1:] for word in ascii_words(title))
    return name[:MAX_BRANCH_NAME] or "Event"


def toml_string(value):
    # JSON string escapes (\" \\ \n \uXXXX) are all valid TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def build_event(fields, issue_number, events_dir):
    errors = []

    title = clean_line(fields.get(FIELD_NAME, ""))
    if not title:
        errors.append(f"**{FIELD_NAME}** is required.")
    elif len(title) > MAX_TITLE:
        errors.append(f"**{FIELD_NAME}** must be at most {MAX_TITLE} characters.")

    raw_date = clean_line(fields.get(FIELD_DATE, ""))
    date = None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        errors.append(f"**{FIELD_DATE}** must use the YYYY-MM-DD format (got `{raw_date or 'nothing'}`).")
    else:
        try:
            date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            errors.append(f"**{FIELD_DATE}** `{raw_date}` is not a real calendar date.")

    type_label = clean_line(fields.get(FIELD_TYPE, ""))
    event_type = EVENT_TYPES.get(type_label)
    if not event_type:
        choices = ", ".join(f"`{label}`" for label in EVENT_TYPES)
        errors.append(f"**{FIELD_TYPE}** must be one of {choices}.")

    location = clean_line(fields.get(FIELD_LOCATION, ""))
    if not location:
        errors.append(f"**{FIELD_LOCATION}** is required.")
    elif len(location) > MAX_LOCATION:
        errors.append(f"**{FIELD_LOCATION}** must be at most {MAX_LOCATION} characters.")

    location_url = validate_url(
        fields.get(FIELD_LOCATION_URL, "").strip(), FIELD_LOCATION_URL, errors, required=False
    )
    event_url = validate_url(fields.get(FIELD_EVENT_URL, "").strip(), FIELD_EVENT_URL, errors, required=True)

    if errors:
        return None, errors

    slug = slugify(title, str(date.year))
    path = events_dir / f"{slug}.md"
    if path.exists() or (events_dir / slug).exists():
        return None, [
            f"An event page named `{slug}` already exists in `{events_dir}`. "
            f"Change the **{FIELD_NAME}** so it is unique, then re-run the workflow."
        ]

    lines = [
        "+++",
        f"title = {toml_string(title)}",
        f"date = {date.isoformat()} {EVENT_TIME}",
        "",
        "[extra]",
        f"event_type = {toml_string(event_type)}",
        f"location = {toml_string(location)}",
    ]
    if location_url:
        lines.append(f"location_url = {toml_string(location_url)}")
    lines += [f"external_url = {toml_string(event_url)}", "+++", ""]

    event = {
        "path": path.as_posix(),
        "branch": f"event/{issue_number}/{date.isoformat()}/{pascal_case(title)}",
        "title": title,
        "date": date.isoformat(),
        "event_type": event_type,
        "content": "\n".join(lines),
    }
    return event, []


def write_outputs(values):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            delimiter = f"EOF_{secrets.token_hex(16)}"
            handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main():
    check_only = "--check" in sys.argv[1:]

    issue_number = os.environ.get("ISSUE_NUMBER", "")
    if not issue_number.isdigit():
        sys.exit("ISSUE_NUMBER must be set to a positive integer.")
    body_file = os.environ.get("ISSUE_BODY_FILE")
    if not body_file:
        sys.exit("ISSUE_BODY_FILE must point to a file containing the issue body.")
    events_dir = pathlib.Path(os.environ.get("EVENTS_DIR", "content/events"))
    if not (events_dir / "_index.md").is_file():
        sys.exit(f"{events_dir} is not the events section; run this from the repository root.")

    body = pathlib.Path(body_file).read_text(encoding="utf-8")
    event, errors = build_event(parse_form(body), issue_number, events_dir)

    if errors:
        for error in errors:
            print(f"::error::{error}")
        write_outputs({"errors": "\n".join(f"- {error}" for error in errors)})
        sys.exit(1)

    if not check_only:
        pathlib.Path(event["path"]).write_text(event["content"], encoding="utf-8")
    print(f"{'Validated' if check_only else 'Wrote'} {event['path']}")
    print(f"Branch: {event['branch']}")
    print(event["content"])
    write_outputs({key: event[key] for key in ("path", "branch", "title", "date", "event_type")})


if __name__ == "__main__":
    main()
