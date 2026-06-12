"""Ledger -> AgentMemory connector (owner-approved 2026-06-11, contract-gated).

Implements the approved scope from tools-lab/contracts/agentmemory-adapter-contract.md:
ingest ONLY Octo work-order artifacts (approved retro summaries + user decisions
+ registry lessons) into a loopback AgentMemory server, with the bridge's
sensitive scan as the gate (the tool's built-in privacy filter is a backstop,
not the gate). MCP `connect` / agent-config wiring stays deferred.

Usage:
  python -B scripts/memory_sync.py                  # dry-run: show what would be sent
  python -B scripts/memory_sync.py --ingest         # actually POST to the server
  python -B scripts/memory_sync.py --recall "timeout fix"   # prove the read path
  python -B scripts/memory_sync.py --health         # server health only

Hard rules enforced here (from the contract):
- loopback only: base URL is pinned to http://127.0.0.1:3111
- AGENTMEMORY_SECRET must be set (bearer auth); --allow-no-secret to override
  for first single-user smoke only
- every payload must pass bridge.detect_sensitive_findings with zero hits;
  failing items are skipped and reported, never stored
- reads the ledger read-only; never writes ledger files
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402

BASE_URL = "http://127.0.0.1:3111"  # pinned loopback per contract
AGENT_ID = "atlas-bridge"
TIMEOUT = 15


def auth_headers(allow_no_secret: bool) -> dict:
    secret = os.environ.get("AGENTMEMORY_SECRET", "").strip()
    if not secret and not allow_no_secret:
        raise SystemExit(
            "AGENTMEMORY_SECRET is not set. Set it before ingestion (contract auth rule), "
            "or pass --allow-no-secret for a first single-user smoke."
        )
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def call(path: str, body: dict | None, headers: dict) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", errors="replace") or "{}")


def server_health(headers: dict) -> dict:
    try:
        return call("/agentmemory/health", None, headers)
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(
            f"AgentMemory server unreachable at {BASE_URL}: {exc}\n"
            "Start it per contract (Docker engine path):\n"
            "  $env:AGENTMEMORY_USE_DOCKER='1'; node E:\\ai\\tools\\agentmemory\\dist\\cli.mjs"
        )


def harvest_items() -> list[dict]:
    """Approved retro summaries, decisions, and registry lessons - compact, ledger-traceable."""
    items: list[dict] = []
    for path in sorted(bridge.RETROS_DIR.glob("OHB-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = bridge.task_metadata(text)
        if meta.get("status") != "approved":
            continue
        task_id = path.stem
        project = meta.get("project_id", "") or "unassigned"
        lessons = [
            line.strip()[2:]
            for line in bridge.task_section(text, "Lessons Learned").splitlines()
            if line.strip().startswith("- ")
        ]
        decision = " ".join(bridge.task_section(text, "Final Decision").split())[:300]
        outcome = " ".join(bridge.task_section(text, "Task Summary").split())[:300]
        content = (
            f"{task_id} [{project}] retro: {outcome} | decision: {decision} | "
            f"lessons: {'; '.join(lessons[:4]) or 'none recorded'}"
        )
        items.append(
            {
                "task_id": task_id,
                "op": "remember",
                "content": content,
                "concepts": ["work-order", "retro", project],
                "agent_id": AGENT_ID,
            }
        )
    for record in bridge.registry_records():
        content = (
            f"{record['learn_id']} approved lesson [{record.get('source_project_id') or 'unassigned'}]: "
            f"{record.get('title', '')} (application_status={record.get('application_status')})"
        )
        items.append(
            {
                "task_id": record.get("source_task_id", "") or record["learn_id"],
                "op": "remember",
                "content": content,
                "concepts": ["learning", "playbook", record.get("source_project_id") or "unassigned"],
                "agent_id": AGENT_ID,
            }
        )
    return items


def sensitive_gate(items: list[dict]) -> tuple[list[dict], list[dict]]:
    clean, rejected = [], []
    for item in items:
        findings = bridge.detect_sensitive_findings(item["content"])
        (rejected if findings else clean).append(
            {**item, "findings": [f.get("name", "?") for f in findings]} if findings else item
        )
    return clean, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="Ledger -> AgentMemory connector (contract-gated)")
    parser.add_argument("--ingest", action="store_true", help="actually POST items (default: dry-run)")
    parser.add_argument("--recall", default="", help="smart-search query to prove the read path")
    parser.add_argument("--health", action="store_true", help="health check only")
    parser.add_argument("--limit", type=int, default=200, help="max items to send")
    parser.add_argument("--allow-no-secret", action="store_true", help="permit missing AGENTMEMORY_SECRET (first smoke only)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    headers = auth_headers(args.allow_no_secret)

    if args.health or args.recall or args.ingest:
        health = server_health(headers)
        print(f"server health: {json.dumps(health, ensure_ascii=False)[:200]}")
        if args.health:
            return 0

    if args.recall:
        result = call("/agentmemory/smart-search", {"query": args.recall, "agent_id": AGENT_ID}, headers)
        print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
        return 0

    items = harvest_items()
    clean, rejected = sensitive_gate(items)
    clean = clean[: args.limit]
    print(f"harvested: {len(items)} | clean: {len(clean)} | rejected by sensitive gate: {len(rejected)}")
    for item in rejected:
        print(f"  REJECTED {item['task_id']}: findings={item['findings']}")
    if not args.ingest:
        for item in clean[:20]:
            print(f"  would send {item['task_id']}: {item['content'][:110]}")
        if len(clean) > 20:
            print(f"  ... and {len(clean) - 20} more")
        print("\ndry-run only. add --ingest to store these in AgentMemory.")
        return 0

    sent = 0
    for item in clean:
        body = {k: v for k, v in item.items() if k != "op"}
        try:
            call("/agentmemory/remember", body, headers)
            sent += 1
        except (urllib.error.URLError, OSError) as exc:
            print(f"  send failed for {item['task_id']}: {exc}")
            break
    print(f"ingested: {sent}/{len(clean)} (server deduplicates by SHA-256)")
    print("verify recall: python -B scripts/memory_sync.py --recall \"<keyword>\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
