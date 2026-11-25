"""Print the reply draft for a saved thread, outside the database. Sends nothing.

    python scripts/draft_preview.py tests/fixtures/reply_threads/waiting_parts.json
    python scripts/draft_preview.py thread.json --rough "tell him its ready at 5"

The JSON holds what app/reply_context.build() returns; only "conversation" is required:
    {"customer": {"first_name": "Sam"},
     "vehicle": {"label": "2015 Ford F-250"},
     "conversation": [{"direction": "outbound", "body": "Hi Sam, ..."},
                      {"direction": "inbound", "body": "when will it be done?"}],
     "job": {"id": 12, "status": "waiting_parts", "total": 1234.5, "approvals": {"pending": 0},
             "parts": [{"description": "injector", "status": "ordered"}], "status_link": null}}
Clean-up uses the rule path unless --ai is given and GROQ_API_KEY is set (that call is billed).
Keep real customer threads out of the repository.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(path: str) -> dict:
    from app.reply_context import style_from
    data = json.loads(Path(path).read_text())
    data.setdefault("customer", {})
    data.setdefault("vehicle", None)
    data.setdefault("job", None)
    data.setdefault("conversation", [])
    if "style" not in data:
        data["style"] = style_from([m.get("body", "") for m in reversed(data["conversation"])
                                    if m.get("direction") == "outbound"])
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("thread", help="JSON file with the conversation and job")
    parser.add_argument("--rough", help="a rough reply to clean up instead of drafting")
    parser.add_argument("--ai", action="store_true", help="allow the AI clean-up (needs GROQ_API_KEY)")
    args = parser.parse_args(argv)
    if not args.ai:
        os.environ["REPLY_AI"] = "off"
    from app import reply_drafts
    context = load(args.thread)
    result = reply_drafts.clean_up(args.rough, context) if args.rough else reply_drafts.draft(context)
    print(result.text)
    print(f"\n[intent: {result.intent} | source: {result.source}]")
    for need in result.needs_info:
        print(f"NEEDS INFO: {need}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
