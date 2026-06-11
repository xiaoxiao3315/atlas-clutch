"""Backfill the learning -> playbook compounding chain from existing retros.

The workbench has retros but an empty playbook layer. This script harvests
Candidate Improvements / Lessons Learned lines from APPROVED retros, dedupes
them globally (auto-retros repeat the same boilerplate, so 63 retros collapse
to a handful of unique lessons with occurrence counts), and drives the
bridge's own chain so every artifact stays canonical:

  proposal (build_learning_proposal_markdown) -> /learn review heuristic
  -> approve (registry) -> /apply plan global -> /apply enact (playbook entry)

Modes:
  default            dry-run report only; writes NOTHING
  --propose          create proposals (status proposed), stop there
  --enact            propose + approve + apply to the global playbook,
                     but only candidates whose /learn review says "approve"
  --enact --force    enact even when review says defer (note records override)

Options: --top N (default 8 unique candidates), --min-count N (default 1).

Boundary: writes only under workbench/learning, workbench/applications, and
workbench/playbooks via bridge functions. Does not modify Hermes, Memory,
SkillRepo, project code, or any task/dispatch/exec ledger entry. Does not
execute commands. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402

APPLY_ID = re.compile(r"\b(APPLY-\d{8}-\d{6}(?:-\d{2})?)\b")
REVIEW_DECISION = re.compile(r"建议决策：(approve|defer|reject)")


def harvest() -> tuple[list[dict], int, int]:
    """Collect unique candidate lines from approved retros."""
    bridge.ensure_workbench_dirs()
    counts: Counter[str] = Counter()
    sources: dict[str, list[dict]] = {}
    total = 0
    skipped_unapproved = 0
    for path in sorted(bridge.RETROS_DIR.glob("OHB-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        total += 1
        meta = bridge.task_metadata(text)
        if meta.get("status") != "approved":
            skipped_unapproved += 1
            continue
        clean = (
            "- live_skipped: true" not in text
            and not re.search(r"- missing_count:\s*[1-9]", text)
        )
        for candidate in bridge.candidate_lines_from_retro(text):
            counts[candidate] += 1
            sources.setdefault(candidate, []).append(
                {"task_id": path.stem, "project_id": meta.get("project_id", ""), "clean": clean}
            )
    unique = []
    for candidate, count in counts.most_common():
        retros = sources[candidate]
        # Prefer a gap-free source retro so the /learn review heuristic can approve.
        best = next((r for r in retros if r["clean"]), retros[0])
        unique.append(
            {
                "text": candidate,
                "count": count,
                "source_task_id": best["task_id"],
                "source_project_id": best["project_id"],
                "clean_source": best["clean"],
                "retro_ids": [r["task_id"] for r in retros],
            }
        )
    return unique, total, skipped_unapproved


def existing_titles() -> set[str]:
    titles: set[str] = set()
    for records in (bridge.learning_records(), bridge.registry_records()):
        for record in records:
            title = str(record.get("title", "")).strip()
            if title:
                titles.add(title)
    return titles


def make_proposal(item: dict) -> str:
    learn_id = bridge.generate_learn_id()
    evidence_lines = [
        f"- occurrence_count: {item['count']} retro(s) 重复出现此改进点。",
        f"- source_retro: workbench/retros/{item['source_task_id']}.md",
        "- retro 已 approved。",
    ]
    evidence_lines += [
        f"- also_seen_in: workbench/retros/{rid}.md" for rid in item["retro_ids"][1:5]
    ]
    proposal = bridge.build_learning_proposal_markdown(
        learn_id,
        item["text"],
        source="retro",
        source_task_id=item["source_task_id"],
        source_project_id=item["source_project_id"],
        source_retro_id=f"RETRO-{item['source_task_id']}",
        problem=f"- 该改进点在 {item['count']} 个已确认 retro 中重复出现，但从未沉淀到 playbook。",
        evidence="\n".join(evidence_lines),
        lesson=item["text"],
        proposed_change=item["text"],
        risks="- 若与现有 playbook 条目重复或过时，应 defer 并在下次复盘合并。",
    )
    bridge.write_proposal(learn_id, proposal)
    return learn_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill learning/playbook chain from approved retros")
    parser.add_argument("--propose", action="store_true", help="create proposals, stop before approve")
    parser.add_argument("--enact", action="store_true", help="propose + approve + apply to global playbook")
    parser.add_argument("--force", action="store_true", help="with --enact: enact even if review says defer")
    parser.add_argument("--top", type=int, default=8, help="max unique candidates to process (default 8)")
    parser.add_argument("--min-count", type=int, default=1, help="min occurrence count (default 1)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    unique, total_retros, skipped = harvest()
    known = existing_titles()
    fresh = [
        item for item in unique
        if item["count"] >= args.min_count and bridge.sanitize_title(item["text"]) not in known
    ][: args.top]

    print(f"retros scanned: {total_retros} (skipped unapproved: {skipped})")
    print(f"unique candidates: {len(unique)} | new after dedupe vs existing proposals: {len(fresh)}")
    print()
    for item in fresh:
        marker = "clean-source" if item["clean_source"] else "gapped-source(review will defer)"
        print(f"[{item['count']:>2}x] {marker:<34} {item['text'][:90]}")
    if not fresh:
        print("nothing to do.")
        return 0
    if not (args.propose or args.enact):
        print("\ndry-run only. use --propose to create proposals, --enact to run the full chain.")
        return 0

    print()
    enacted = 0
    for item in fresh:
        learn_id = make_proposal(item)
        print(f"proposal created: {learn_id}  ({item['count']}x) {item['text'][:60]}")
        if not args.enact:
            print(f"  next: /learn review {learn_id}")
            continue
        review = bridge.build_learn_review_reply(learn_id)
        match = REVIEW_DECISION.search(review)
        decision = match.group(1) if match else "unknown"
        print(f"  review decision: {decision}")
        if decision != "approve" and not args.force:
            print(f"  skipped enact (review says {decision}); use --force to override or /learn review {learn_id} in chat")
            continue
        note = f"backfill_learning: occurrence_count={item['count']}; review={decision}" + (
            "; forced by operator" if decision != "approve" else ""
        )
        bridge.build_learn_approve_reply(learn_id, note)
        plan_reply = bridge.build_apply_plan_reply(f"{learn_id} global")
        apply_match = APPLY_ID.search(plan_reply)
        if not apply_match:
            print(f"  ERROR: could not parse apply_id from plan reply; stopping at registry for {learn_id}")
            continue
        apply_id = apply_match.group(1)
        bridge.build_apply_enact_reply(apply_id, note)
        enacted += 1
        print(f"  enacted: {apply_id} -> {bridge.playbook_display_path(bridge.global_playbook_path())}")

    if args.enact:
        entries = bridge.playbook_entry_count(bridge.global_playbook_path())
        print(f"\nglobal playbook entries now: {entries} (enacted this run: {enacted})")
        print("verify in chat: /playbook show global  and  /context pack <task_id> (advisory should be non-empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
