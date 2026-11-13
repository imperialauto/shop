"""Exercise the development worktree and evidence flow with a fixed fixture."""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

FIXTURE = "imperial-harness-fixture.txt"
EXPECTED = b"Imperial synthetic development check\n"


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()


def review(root, base, commit):
    """Check the committed artifact against a fixed contract in a fresh checkout."""
    if git(root, "rev-parse", f"{commit}^") != base:
        raise ValueError("Candidate parent differs from the recorded base.")
    if git(root, "diff", "--name-status", base, commit) != f"A\t{FIXTURE}":
        raise ValueError("Candidate must add exactly the synthetic fixture.")
    fixture = root / FIXTURE
    if fixture.is_symlink() or fixture.read_bytes().replace(b"\r\n", b"\n") != EXPECTED:
        raise ValueError("Fixture does not match the review contract.")


def run(root):
    base = git(root, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="imperial-smoke-") as directory:
        writer = Path(directory) / "writer"
        reviewer = Path(directory) / "reviewer"
        git(root, "worktree", "add", "--detach", str(writer), base)
        try:
            with (writer / FIXTURE).open("xb") as fixture:
                fixture.write(EXPECTED)
            git(writer, "add", "--", FIXTURE)
            git(writer, "-c", "user.name=Imperial Smoke", "-c",
                "user.email=imperial-smoke@example.invalid", "-c", "core.hooksPath=.git/no-hooks",
                "commit", "-m", "Add synthetic development fixture")
            commit = git(writer, "rev-parse", "HEAD")
            git(root, "worktree", "add", "--detach", str(reviewer), commit)
            try:
                review(reviewer, base, commit)
            finally:
                git(root, "worktree", "remove", str(reviewer))
        finally:
            git(root, "worktree", "remove", "--force", str(writer))
    return {
        "mode": "synthetic",
        "base": base,
        "candidate": commit,
        "fixtureReview": "passed",
        "coordinatorClaim": "not_run",
        "providerExecution": "not_run",
        "candidatePublished": False,
        "candidateCi": "not_run",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.repository)))
