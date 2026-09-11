# Contributing to QobuzProxy

Thanks for helping! This is a small project maintained in spare time, and
most testing means sitting in front of a real speaker with the Qobuz app
open. The guidelines below are here so a change can be understood, tested,
and released quickly. None of them are meant to be gatekeeping, they just
make the review a lot easier for everyone.

## One PR, one change

A pull request fixes one bug or adds one feature. If you find yourself
writing "also", "additionally", or "while I was there" in the description,
split it.

Concretely:

- A PR that adds a feature does not also fix unrelated bugs, even small ones.
  Open a second PR for the fix. Small fixes are cheap to review on their own
  and can be merged and released immediately.
- A PR that fixes a bug does not also refactor, rename, or reformat code it
  did not need to touch.
- A PR that changes the Web UI does not also change the playback engine,
  unless the UI change is impossible without it. If it is, say so in the
  description and keep the engine change minimal.
- If two changes are independent, they are two PRs. If one depends on the
  other, open the dependency first and mention it in the second PR.

A good rule of thumb: if a reviewer could reasonably want to merge half of
the PR and reject the other half, it should have been two PRs. See Google's
[Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)
guide for the reasoning.

Large changes that touch the player state machine, the Connect protocol, or
the backends are welcome, but **open an issue first** describing the problem
and the approach. This avoids you spending a weekend on something that
conflicts with work in progress or with how the project is meant to evolve.

## What the maintainer owns

Please leave these out of your PR. They are handled by the maintainer at
release time, and including them creates conflicts with every other open PR:

- The `version` field in `pyproject.toml` and the matching line in `uv.lock`
- Release tags and GitHub releases
- Changes to `.github/workflows/`
- `uv.lock` changes, unless the PR adds or updates a dependency (use
  `uv add` / `uv remove` so the lock file and `pyproject.toml` change
  together, and say why the dependency is needed)

Releases are cut by tagging `main`. A merged PR ships in the next release;
there is nothing you need to do to make that happen.

## Describe the problem, not just the code

The PR description should answer, in this order:

1. **What was wrong** from the user's point of view. "Skipping a track in the
   Qobuz app snaps the UI back to the previous song for two seconds" is a
   problem. "Refactor SET_STATE handling" is not.
2. **Why it happened**, briefly, if you know.
3. **What changed** in behavior. Not a list of functions edited.
4. **How you tested it**, with the actual hardware. Name the renderer model
   or audio device, the Qobuz app platform, and what you did. "Tests pass" is
   necessary but not sufficient for anything touching playback, Connect, or
   DLNA. If you could not test on hardware, say so explicitly.

Link the issue the PR addresses. If there is no issue, the first paragraph of
the description is the issue.

## Commits

Follow the existing convention: `fix(module):`, `feat(module):`,
`refactor(module):`, `test(module):`, `docs:`. Subject under 60 characters.
The body explains the visible behavior or business rule, not the diff.

A PR does not need to be a single commit, but each commit should build and
pass tests on its own, and the commits should tell the story of the change.
Fixup and "address review" commits are fine during review; they will be
squashed on merge if needed.

## Before you open the PR

```bash
uv run ruff format qobuz_proxy/ tests/
uv run ruff check qobuz_proxy/ tests/
uv run mypy qobuz_proxy/
uv run pytest
```

mypy currently reports some pre-existing errors on `main`. That is fine, just
make sure your change does not add new ones.

New behavior comes with tests. Changed behavior comes with changed tests.
Tests live under `tests/` mirroring the source tree.

## Using AI tools

**You are welcome to use AI coding tools.** Most of this project was written
with them! Two things to keep in mind:

1. **Disclose it.** Add a line to the PR description saying which tool was
   used and roughly how much of the change it produced. This is not a
   judgment; it tells the reviewer where to look harder.
2. **You are the author.** You must be able to explain why each change is there, and
   have run the result on real hardware.

AI tools are especially prone to the problems this document is about: they
happily bump versions, fix nearby unrelated things, add defensive code that
was never needed, and write long confident descriptions of changes that were
not actually verified. Review the output with that in mind before you submit
it.

## Review

Expect a few questions, and possibly a request to split things up. Reviews can take a while since testing
usually means sitting in front of a speaker. A PR that follows the guidelines above gets through much
faster because it can be understood and tested in one sitting. Thanks for your patience, and for
contributing!
