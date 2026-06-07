from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DESIGN_FILE = ROOT / "workbench" / "designs" / "OHB-WEB-017A-web-workbench-design.md"
PROBE = ROOT / "web_workbench_design_probe.py"


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise AssertionError(f"unexpected text: {needle}")


def assert_no_secret_in_tree(root: Path) -> None:
    forbidden = ("bf_", "sk-", "Authorization: Bearer", "Cookie:")
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in forbidden:
            assert_not_contains(text, needle)


def run_probe(root: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(PROBE), "--root", str(root)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        shell=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or "probe failed")
    return result.stdout


def main() -> int:
    if not DESIGN_FILE.exists():
        raise AssertionError("design document missing")
    if not (ROOT / "workbench" / "designs").exists():
        raise AssertionError("workbench/designs missing")

    design = DESIGN_FILE.read_text(encoding="utf-8", errors="replace")
    for needle in (
        "## Current Workbench Data",
        "## Current OCTO Surface",
        "## Official OCTO Architecture Notes",
        "## Visual Workbench Requirements",
        "### Option A: Bridge Local Dashboard",
        "### Option B: Octo Bot Message Dashboard",
        "### Option C: octo-web Native Workbench Page",
        "## Recommended Route",
        "OHB-WEB-017B",
        "octo_web_modified: false",
        "octo_server_modified: false",
        "octo_deployment_modified: false",
        "ui_implemented: false",
        "auto_execute_enabled: false",
    ):
        assert_contains(design, needle)

    with tempfile.TemporaryDirectory(prefix="ohb-web-design-", ignore_cleanup_errors=True) as tmp:
        temp_root = Path(tmp)
        (temp_root / "README.md").write_text(
            "# Probe Test\n\n## Atlas Workbench\n\n## Web Workbench Roadmap\n",
            encoding="utf-8",
        )
        (temp_root / ".env").write_text("TOKEN=bf_probe_secret_123456\n", encoding="utf-8")
        (temp_root / "logs").mkdir()
        (temp_root / "logs" / "bridge.log").write_text(
            "Authorization: Bearer bf_log_secret_123456\nCookie: session=secret\n",
            encoding="utf-8",
        )
        for name in (
            "projects",
            "tasks",
            "evidence",
            "retros",
            "learning",
            "applications",
            "playbooks",
            "context_packs",
            "dispatches",
            "pilots",
            "collections",
            "executions",
            "designs",
        ):
            directory = temp_root / "workbench" / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "sample.md").write_text(f"# {name}\n", encoding="utf-8")

        output = run_probe(temp_root)
        for needle in (
            "OHB-WEB-017A read-only design probe",
            "env_file_read: false",
            "user_task_executed: false",
            "octo_web_modified: false",
            "octo_server_modified: false",
            "docker_modified: false",
            "- projects: exists=true files=1",
            "- tasks: exists=true files=1",
            "- executions: exists=true files=1",
            "- log_content_read: false",
        ):
            assert_contains(output, needle)
        for secret in ("bf_probe_secret", "bf_log_secret", "Authorization: Bearer", "Cookie:"):
            assert_not_contains(output, secret)

    readme = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
    assert_contains(readme, "## Web Workbench Roadmap")
    assert_contains(readme, "017A")
    assert_contains(readme, "017B")
    assert_contains(readme, "017D")

    assert_no_secret_in_tree(ROOT / "workbench" / "designs")
    print("smoke_web_design passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
