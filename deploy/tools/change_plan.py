"""Project-specific impact rules. Git and GitHub CLI own diff/release discovery."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

FLAGS = ("frontend", "backend", "prompts", "runtime_deps", "packaging", "hub_deploy", "hub_regression")
RUNTIME = {
    "app/pyproject.toml", "app/uv.lock",
    "app/ui_tars/package.json", "app/ui_tars/package-lock.json",
    "deploy/windows/runtimes.json",
}
# Only reviewed, isolated modules qualify for the reduced historical upgrade gate.
# New/unclassified business files fail closed; do not infer safety from .py.
LOWER_RISK_BACKEND = {
    "app/menu_capture/vision/purpose_classifier.py",
    "app/menu_capture/desktop/template_locator.py",
    "app/menu_capture/desktop/preview_locator.py",
    "app/menu_capture/desktop/chat_anchor.py",
    "app/menu_capture/desktop/chat_layout.py",
    "app/menu_capture/desktop/chat_image_locator.py",
    "app/menu_capture/desktop/checkbox_locator.py",
    "app/menu_capture/desktop/close_icon_locator.py",
}
DOCS = {"AGENTS.md", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE"}
STABLE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")


def is_doc(path: str) -> bool:
    return path.startswith("docs/") or path in DOCS


def is_test(path: str) -> bool:
    return path.startswith("app/tests/") or path == "deploy/tools/test_change_plan.py"


def classify(files: list[str], *, force_full: bool = False, uncertain: bool = False) -> dict:
    plan = dict.fromkeys(FLAGS, False)
    risk = 0
    release_needed = uncertain
    reasons = []
    for path in files:
        if is_doc(path):
            continue
        if is_test(path):
            # Changed tests still run; they are not a new runtime version.
            plan["backend"] = True
            if path.startswith(("app/tests/contract/", "app/tests/platform/")):
                plan["packaging"] = True
            continue
        release_needed = True
        if path in RUNTIME:
            plan["runtime_deps"] = True
            risk = 3
        elif path.startswith("app/ui_tars/prompts/"):
            plan["prompts"] = plan["backend"] = True
            risk = max(risk, 1)
        elif path.startswith(("app/web-react/src/", "app/web/react/")):
            plan["frontend"] = True
            risk = max(risk, 2)
        elif path in LOWER_RISK_BACKEND:
            plan["backend"] = True
            if path.startswith("app/menu_capture/desktop/"):
                plan["packaging"] = True
            risk = max(risk, 2)
        else:
            risk = 3
            if path.startswith(("deploy/hub/", "deploy/hub-update/")):
                plan["hub_deploy"] = True
            if path.startswith("deploy/windows/"):
                plan["packaging"] = True
    if uncertain:
        risk = 3
        reasons.append("baseline or diff uncertain; full gates required")
    if risk == 3 or force_full:
        plan.update(dict.fromkeys(FLAGS, True))
        reasons.append("high risk / explicit full verification")
    if not release_needed:
        reasons.append("no runtime changes; no software release")
    if not reasons:
        reasons.append("reviewed impact rules; unrelated historical upgrade checks omitted")
    return {
        **plan,
        "risk": f"L{max(risk, 3 if force_full else 0)}",
        "docs_only": bool(files) and all(is_doc(p) for p in files) and not uncertain,
        "release_needed": release_needed,
        "build": release_needed or force_full,
        "reasons": reasons,
    }


def command(*args: str) -> bytes:
    return subprocess.check_output(args, stderr=subprocess.PIPE)


def git(*args: str) -> str:
    return command("git", *args).decode().strip()


def stable_baseline(version: str) -> str:
    # A bare tag can be unpublished/draft. Ask GitHub for the official stable release.
    # Preview has different channel semantics; conservatively run full gates for now.
    if not STABLE_TAG.fullmatch(f"v{version}"):
        raise ValueError("preview or invalid version: no verified same-channel baseline")
    release = json.loads(command("gh", "release", "view", "--json", "tagName,isDraft,isPrerelease"))
    tag = release["tagName"]
    if release["isDraft"] or release["isPrerelease"] or not STABLE_TAG.fullmatch(tag):
        raise ValueError("latest release is not a trusted stable baseline")
    base = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    command("git", "merge-base", "--is-ancestor", base, "HEAD")
    return base


def plan_for(mode: str, version: str = "", *, force_full: bool = False) -> dict:
    head = git("rev-parse", "HEAD")
    base = "unknown"
    files = []
    try:
        base = git("merge-base", "origin/main", head) if mode == "pr" else stable_baseline(version)
        # Disable rename detection so both old and new paths participate; NUL handles filenames.
        raw = command("git", "diff", "--no-renames", "--name-only", "-z", base, head)
        files = [os.fsdecode(p) for p in raw.split(b"\0") if p]
        result = classify(files, force_full=force_full)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError) as exc:
        result = classify([], force_full=force_full, uncertain=True)
        result["reasons"].append(type(exc).__name__)
    return {**result, "base_ref": base, "head": head, "files": files}


def emit(plan: dict) -> None:
    print(json.dumps(plan, ensure_ascii=True, indent=2))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, val in plan.items():
                if isinstance(val, bool):
                    stream.write(f"{key}={str(val).lower()}\n")
                elif key in {"base_ref", "head", "risk"}:
                    stream.write(f"{key}={val}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("### Change plan\n\n")
            for key in ("base_ref", "head", "risk", *FLAGS, "docs_only", "release_needed", "build"):
                stream.write(f"- {key}: {plan[key]}\n")
            stream.write(f"- changed paths: {len(plan['files'])}\n")
            for reason in plan["reasons"]:
                stream.write(f"- {reason}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pr", "release"), required=True)
    parser.add_argument("--version", default="")
    parser.add_argument("--force-full", action="store_true")
    args = parser.parse_args()
    emit(plan_for(args.mode, args.version, force_full=args.force_full))


if __name__ == "__main__":
    main()
