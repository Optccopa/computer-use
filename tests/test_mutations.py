"""Every mutation in scripts/mutate.py has to still apply to real code.

The mutation count is on a badge, so it is read as a claim: this many deliberate
breakages, each one checked against the suite. A mutation whose pattern no longer
matches anything reports SKIP, which is honest in the run and invisible on the
badge -- the number stays the same while what it counts quietly drains away.

It has drained away before. Splitting session.py and actions.py into packages
invalidated thirty-six paths in one commit, and three more went stale when the
runner switched from text reads to bytes. The runner can follow a file that moved;
what it cannot do is notice that nobody noticed.

Written as loops rather than one parametrized case per mutation. Seventy tests
asserting one property is seventy on a badge that is meant to say how much of this
harness is covered, and the list is reported in full on failure either way.
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import mutate  # noqa: E402

ALL = [("python", *m) for m in mutate.PYTHON_MUTATIONS]
ALL += [("native", *m) for m in mutate.NATIVE_MUTATIONS]


class TestEveryMutationStillBites:
    def test_every_pattern_is_found_where_it_says_it_is(self):
        """Through the runner's own resolver, so a moved file is not a failure.

        resolve_target follows code that moved and refuses a pattern that now
        matches two files. Both of those are the answer this test wants: the thing
        being ruled out is a mutation that matches nothing at all.
        """
        stale = [
            f"{name} ({rel})"
            for _, name, rel, old, _new in ALL
            if mutate.resolve_target(rel, old) is None
        ]
        assert not stale, (
            f"{len(stale)} of {len(ALL)} mutations no longer match any file. They "
            f"report SKIP while still being counted:\n  " + "\n  ".join(stale)
        )

    def test_applying_each_one_actually_changes_the_file(self):
        # A mutation whose replacement equals its pattern passes the suite for the
        # least interesting reason there is.
        inert = []
        for _kind, name, rel, old, new in ALL:
            resolved, _ = mutate.resolve_target(rel, old)
            original = mutate.read_source(resolved)
            if mutate.mutate_bytes(original, old, new) in (None, original):
                inert.append(name)
        assert not inert, "these mutations change nothing:\n  " + "\n  ".join(inert)


class TestTheListIsWellFormed:
    def test_no_two_mutations_share_a_name(self):
        # The name is how a run reports which one survived, so a duplicate makes the
        # report ambiguous exactly when it matters.
        names = [name for _kind, name, _rel, _old, _new in ALL]
        assert len(names) == len(set(names))

    def test_the_python_and_native_lists_do_not_overlap(self):
        """Native mutations force a rebuild and python ones must not.

        One filed under the wrong list either rebuilds for nothing, which costs
        twenty minutes across a run, or edits C++ that is never recompiled -- and
        that second one reports SURVIVED for a mutation that was never in the build.
        """
        py = {m[1] for m in mutate.PYTHON_MUTATIONS}
        native = {m[1] for m in mutate.NATIVE_MUTATIONS}
        assert not py & native
        assert all(not m[1].endswith((".cpp", ".hpp")) for m in mutate.PYTHON_MUTATIONS)
        assert all(m[1].endswith((".cpp", ".hpp")) for m in mutate.NATIVE_MUTATIONS)
