"""Recomputes the README badges, or checks that they are still true.

    .venv/Scripts/python.exe scripts/badges.py            # rewrite the README
    .venv/Scripts/python.exe scripts/badges.py --check    # fail if stale

A badge with a number in it is a claim, and a hand-typed one is wrong as soon as
anyone adds a test -- the first version of these said "621" and was stale one
commit later, in the commit that added the tests guarding the README. So the
numbers are measured here and CI runs --check, which makes them the same kind of
thing as an assertion rather than decoration.

Counts use the same `-m "not desktop"` filter CI does, so the number in the badge
is the number a reader would get.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
README = REPO / "README.md"


def collected_tests() -> int:
    out = subprocess.run(
        [str(PY), "-m", "pytest", "--collect-only", "-q", "-m", "not desktop"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    # The last line is "N tests collected in ...s"; anything else means pytest
    # changed its summary and the number below would be silently invented.
    match = re.search(r"(\d+) tests? collected", out)
    if not match:
        raise SystemExit("could not read the collected test count from pytest")
    return int(match.group(1))


def coverage_percent() -> int:
    subprocess.run(
        [str(PY), "-m", "pytest", "-q", "-m", "not desktop",
         "--cov=cufast", "--cov-report=json:.coverage.json"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    report = REPO / ".coverage.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    report.unlink(missing_ok=True)
    return int(data["totals"]["percent_covered"])


def mutation_count() -> int:
    sys.path.insert(0, str(REPO / "scripts"))
    import mutate

    return len(mutate.PYTHON_MUTATIONS) + len(mutate.NATIVE_MUTATIONS)


def colour(percent: int) -> str:
    if percent >= 90:
        return "brightgreen"
    return "green" if percent >= 75 else "orange"


def render(tests: int, cov: int, mutations: int) -> dict[str, str]:
    """The badge lines, keyed by the marker that identifies each one."""
    return {
        "tests-": f"[![tests {tests} passing](https://img.shields.io/badge/"
                  f"tests-{tests}%20passing-brightgreen)](tests/)",
        "coverage-": f"[![coverage {cov}%](https://img.shields.io/badge/"
                     f"coverage-{cov}%25-{colour(cov)})](tests/)",
        "mutations-": f"[![mutations {mutations} caught](https://img.shields.io/badge/"
                      f"mutations-{mutations}%20caught-brightgreen)](scripts/mutate.py)",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="badges.py", description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the README is out of date, changing nothing")
    args = parser.parse_args(argv)

    wanted = render(collected_tests(), coverage_percent(), mutation_count())
    lines = README.read_text(encoding="utf-8").split("\n")

    stale = []
    for marker, badge in wanted.items():
        for i, line in enumerate(lines):
            if marker in line and line.startswith("[!["):
                if line != badge:
                    stale.append((line, badge))
                    lines[i] = badge
                break
        else:
            raise SystemExit(f"no badge line in the README matches {marker!r}")

    if not stale:
        print("badges are current")
        return 0
    if args.check:
        for old, new in stale:
            print(f"stale:\n  is:     {old}\n  should: {new}")
        return 1
    README.write_text("\n".join(lines), encoding="utf-8", newline="")
    print(f"updated {len(stale)} badge(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
