#!/usr/bin/env python3
"""Exercise recorded continuity offline; this does not run or evaluate Claude.

Run from any directory: python3 /path/to/recortex/examples/continuity/run.py
Only the existing fake gh is invoked. All writes stay in a temporary directory.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPTS = ROOT / "skills" / "recortex" / "scripts"
EXAMPLE = ROOT / "examples" / "octo-repo"
REPO = "octo-org/octo-repo"
UNPOSTED = "UNPOSTED-continuity-note-6f19"

sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
import names  # noqa: E402


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def session_environment(folder, remote):
    """Do not inherit a real configuration, gh override, trust list or fake fault."""
    folder.mkdir()
    config = folder / ".recortex.json"
    config.write_text(json.dumps({"repo": REPO, "hub": 4,
                                 "profiles": ["software", "research"],
                                 "topics": ["reader", "speed"]}), encoding="utf-8")
    trusted = folder / "trusted-repos"
    trusted.write_text(REPO + " octocat\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith((names.ENV_PREFIX, "FAKE_GH_", "CLAUDE_"))}
    env.update({"RECORTEX_GH": shlex.join([sys.executable, str(ROOT / "tests" / "fake_gh.py")]),
                "FAKE_GH_STATE": str(remote), "RECORTEX_CONFIG": str(config),
                "RECORTEX_TRUSTED_FILE": str(trusted), "RECORTEX_LINK_PAUSE": "0",
                "XDG_CACHE_HOME": str(folder / "cache"), "XDG_STATE_HOME": str(folder / "state"),
                "XDG_CONFIG_HOME": str(folder / "config"), "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def command(folder, env, script, *arguments, stdin=None):
    done = subprocess.run([sys.executable, str(SCRIPTS / script), *arguments],
                          cwd=str(folder), env=env, input=stdin, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    require(done.returncode == 0,
            "%s failed (%s):\n%s%s" % (script, done.returncode, done.stdout, done.stderr))
    return done.stdout


def run_demo():
    started = time.monotonic()
    checks = []

    def check(condition, description):
        require(condition, description)
        checks.append(description)

    with tempfile.TemporaryDirectory(prefix="recortex-continuity-") as temporary:
        base = Path(temporary)
        remote = base / "fake-github.json"
        shutil.copyfile(ROOT / "tests" / "fixtures" / "repo-state.json", remote)
        first = base / "session-a"
        first_env = session_environment(first, remote)
        drafts = first / "drafts"
        shutil.copytree(EXAMPLE, drafts)
        command(first, first_env, "setup_labels.py")
        command(first, first_env, "create_issues.py", "--manifest", str(drafts / "manifest.json"))
        created = json.loads((drafts / "manifest.json.state.json").read_text(encoding="utf-8"))
        require({key: item["number"] for key, item in created["issues"].items()} ==
                {"hub": 4, "reader": 5, "speed": 6, "task-newlines": 7},
                "The example issue ids changed; update the continuity fixtures with it.")

        chain = [("1-idea.md", "idea"), ("2-failure.md", "failure"),
                 ("3-decision.md", "decision"), ("4-question.md", "question"),
                 ("5-decision.md", "decision")]
        for expected, (name, kind) in enumerate(chain, 9103):
            output = command(first, first_env, "post_verified.py", "comment", "--issue", "5",
                             "--kind", kind, "--body-file", str(drafts / "chain" / name))
            match = re.search(r"memory id: ([0-9]+)", output)
            require(match is not None and int(match.group(1)) == expected,
                    "The posted memory ids no longer match the example's links.")
        command(first, first_env, "post_verified.py", "comment", "--issue", "7",
                "--kind", "checkpoint", "--body-file", str(HERE / "checkpoint.md"))
        command(first, first_env, "resume_block.py", "update", "--file", str(HERE / "resume.md"))
        command(first, first_env, "index.py", "sync")
        (first / "unposted-note.txt").write_text(UNPOSTED, encoding="utf-8")
        recorded_seconds = time.monotonic() - started

        # Lose the first session's drafts, note, index and state. Keep only the fake
        # GitHub record; the recovery environment is independently configured.
        shutil.rmtree(first)
        second = base / "session-b"
        second_env = session_environment(second, remote)
        check(not (second / "cache").exists() and not first.exists(),
              "Recovery starts without the earlier drafts, note, cache or session state")
        recovery_started = time.monotonic()
        command(second, second_env, "index.py", "sync", "--rebuild")
        recall = command(second, second_env, "index.py", "query", "recall", "--issue", "5", "--json")
        recalled = json.loads(recall)
        memories = {item["id"]: item for item in recalled["memories"] + recalled["others"]}
        check("4 of 6 failed" in memories[9104]["plain"],
              "The failed patch and its 4-of-6 result are recovered")
        check(["checks", 9103] in memories[9104]["links"] and
              ["because", 9104] in memories[9105]["links"] and
              ["instead", 9103] in memories[9105]["links"] and
              memories[9103]["state"] == "set aside",
              "The failed idea remains linked to the replacement approach")
        check(memories[9102]["state"] == "replaced" and
              memories[9107]["state"] == "stands" and
              "lenient mode warn" in memories[9107]["plain"],
              "The earlier decision is replaced; the current warning policy stands")
        check(memories[9106]["state"] == "open",
              "Pipe support remains an open question, not a successful result")
        checkpoint = command(second, second_env, "index.py", "query", "timeline",
                             "--issue", "7", "--type", "checkpoint", "--json")
        check(any("three pipe tests still need writing" in item["plain"]
                  for item in json.loads(checkpoint)), "The task checkpoint is recovered")
        resume = command(second, second_env, "resume_block.py", "show", "--active")
        check("Write the three pipe tests before reporting pipe support" in resume and
              "Do not tag 0.3.0 until the project lead says so" in resume,
              "The latest resume recovers the next action and release hold")
        payload = command(second, second_env, "hooks.py", "session-start",
                          stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": "continuity-b",
                                            "source": "startup", "cwd": str(second)}))
        check("BEGIN PROJECT STATUS DATA" in payload and
              "Write the three pipe tests" in payload and "Do not tag 0.3.0" in payload,
              "A fresh session hook receives the recorded focus and hold")
        check(UNPOSTED not in recall + checkpoint + resume + payload + remote.read_text(encoding="utf-8"),
              "The unposted note is absent from both the record and recovery outputs")
        recovery_seconds = time.monotonic() - recovery_started

    return {"kind": "offline recorded-continuity demonstration", "checks": checks,
            "record_seconds": round(recorded_seconds, 3),
            "recovery_seconds": round(recovery_seconds, 3),
            "claude_evaluated": False, "token_usage": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the measured run and checks as JSON")
    args = parser.parse_args()
    report = run_demo()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Offline continuity demonstration; no GitHub connection and no Claude run.")
        for check in report["checks"]:
            print("PASS: " + check)
        print("Record: %.3f s; fresh recovery: %.3f s (local wall time, not Claude latency)." %
              (report["record_seconds"], report["recovery_seconds"]))
        print("Token use: not measured. See README.md here for the separate live-session evaluation.")


if __name__ == "__main__":
    main()
