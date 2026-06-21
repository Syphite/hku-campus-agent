#!/usr/bin/env python3
"""
scripts/diagnose_graph.py

Staged Microsoft Graph diagnostics for inbox 401 / access failures.
Usage:
  python3 scripts/diagnose_graph.py
  python3 scripts/diagnose_graph.py --user-id "<object-id-or-upn>"
  python3 scripts/diagnose_graph.py --profile-email user@gmail.com
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph_diagnostics import (  # noqa: E402
    compare_mailbox_identifiers,
    diagnose_mailbox_access,
    format_probe_line,
    get_config_report,
)


def _print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def _print_report(report: dict, label: str) -> bool:
    print(f"\n--- {label} ---")
    token = report.get("token") or {}
    if token.get("ok") is False or report.get("token", {}).get("error"):
        print(f"Token: FAIL — {report.get('token', {}).get('error', 'unknown')}")
        return False

    print(f"Token: OK")
    print(f"  app_id:     {token.get('app_id', '')}")
    print(f"  tenant_id:  {token.get('tenant_id', '')}")
    print(f"  roles:      {token.get('roles', [])}")
    print(f"  expires_at: {token.get('expires_at', '')}")
    print(format_probe_line(report.get("user_lookup"), "User lookup"))
    print(format_probe_line(report.get("inbox_read"), "Inbox read"))

    hints = report.get("hints") or []
    if hints:
        print("Hints:")
        for hint in hints:
            print(f"  - {hint}")
    return bool(report.get("ok"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Microsoft Graph mailbox access")
    parser.add_argument(
        "--user-id",
        default=os.getenv("GRAPH_USER_ID", ""),
        help="Graph user id (Object ID or UPN). Defaults to GRAPH_USER_ID env var.",
    )
    parser.add_argument(
        "--profile-email",
        default="",
        help="Optional profile email to compare against GRAPH_USER_ID",
    )
    args = parser.parse_args()

    _print_section("Graph 401 Diagnostics")
    config = get_config_report()
    print("Configuration (secrets redacted):")
    for key, value in config.items():
        print(f"  {key}: {value}")

    user_id = (args.user_id or "").strip()
    if not user_id:
        print("\nERROR: No user id. Set GRAPH_USER_ID or pass --user-id.")
        return 1

    _print_section(f"Primary user: {user_id}")
    if args.profile_email:
        comparison = compare_mailbox_identifiers(user_id, args.profile_email.strip())
        primary_ok = _print_report(comparison["primary"], f"GRAPH_USER_ID ({user_id})")
        profile_report = comparison.get("profile_email")
        if profile_report:
            _print_report(profile_report, f"Profile email ({args.profile_email})")
        mismatch = comparison.get("mismatch_hint")
        if mismatch:
            print(f"\nMismatch: {mismatch}")
        return 0 if primary_ok else 1

    report = diagnose_mailbox_access(user_id)
    ok = _print_report(report, user_id)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
