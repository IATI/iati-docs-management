# AGENTS.md

Orientation for humans and AI agents working on the IATI documentation estate.

## What this repo is

The control plane for **the IATI documentation estate** — every IATI org repo
on GitHub tagged with the custom property `Documentation=true`. The tooling
under `scripts/` discovers, inspects, syncs, and now builds the estate. The
template `iati-docs-base` is the authoritative source of truth for shared
files (conf.py, requirements, RTD config, CI). Downstream repos converge on
the template; the template never converges on a downstream.

We call it the **estate**, not the fleet.

## Repos

`python scripts/repo_manager.py list` returns the current set. The template
(`iati-docs-base`) is also tagged `Documentation=true` but is treated specially
in the tooling.

There's a per-repo migration spectrum for `conf.py`:

- **Shared-conf**: thin `conf.py` + `project_info.py`, matches the template.
- **Inline**: ~81-line `conf.py` close to the template's shape but with values
  baked in. Mechanical migration to shared-conf.
- **Legacy**: 396-line sphinx-quickstart leftover from 2016. Needs real work
  before it can fit the shared-conf model.

Run `python scripts/repo_manager.py check` for the current snapshot. Don't
encode the spectrum into memory — it changes as repos migrate.

## Understanding the surface area of a change

Static checks (`check`) tell you whether files match the template. They don't
tell you what changes in the rendered docs when you roll a template change
through downstream repos. **Use `build-compare` to see the surface area of a
change before publishing.**

```bash
# Single-repo health check, fresh clone of main
python scripts/repo_manager.py build <repo-name>

# Estate-wide health snapshot — every repo on its default branch
python scripts/repo_manager.py build-all

# What changes in the rendered output for this repo?
python scripts/repo_manager.py build-compare --dir <path-to-checkout>
```

`build-compare` runs the candidate's working tree as-is (uncommitted edits
included) and clones a fresh baseline from `main`. The candidate's tree is
never modified. The report describes **what differs**:

- pages added / removed / modified (with per-page normalised HTML diffs)
- Sphinx warnings added / removed

It does not adjudicate whether each difference is good or bad. **That's your
call as the operator** — you know what change you intended, and you eyeball
the diff against that intent.

Exit code is mechanical: `1` only when the candidate failed to build while
the baseline succeeded. Content differences, however large, exit `0`. The
verb composes into a "see the surface area" loop, not a pass/fail gate.

HTML diffs are normalised to strip Sphinx's `?v=<hash>` asset cache-busters
so they don't appear as noise. If a future Sphinx/theme version introduces
another build-volatile pattern, extend `_HTML_NORMALISERS` in `repo_manager.py`.

### Don't go down build rabbit holes

If a build fails after a change, **establish a baseline first**. Build the
upstream `main` of the same repo before debugging anything about your branch.
If `main` also fails locally, the problem is environmental, not yours. If
`main` builds and your branch doesn't, the diff between them is small and
findable.

This is the lesson from the session where we lost an hour to a `conf.py`
`sys.path` trail before realising the deployed site was already building
fine; we just hadn't established what "fine" looked like.

## Architectural decisions

### Template as source of truth

`iati-docs-base` defines the canonical shared files. `sync` and `check`
both treat any divergence as drift the downstream needs to converge on. If
a downstream needs to permanently differ from the template, that's a signal
to update the template, not to special-case the downstream.

### Three flavours of cross-repo change

- **Template sync** (`sync`) — files identical across repos. PR per repo, all
  at once.
- **Scripted change** (`run-script`) — same modification, parameterised by
  repo. Script gets `cwd=<repo>` and `argv=[<repo-name>]`. Any non-zero exit
  aborts the whole run; no partial publishes.
- **Manual refactor** (`checkout-all` + `make-prs`) — when the change is
  bespoke per repo. Clone everything to a persistent dir, edit by hand,
  then PR the lot. The clones outlive the command on purpose; macOS prunes
  `/tmp` after ~3 days.

Picked over more elaborate "branch coordination" schemes because it stays
linear: edit, validate, publish. No mid-flight state to recover from.

### Build isolation = fresh clone

All build operations happen in a fresh `/tmp/iati-docs-<rand>/<repo>` clone
with a freshly-created `.venv-build`. There's no concept of "use my existing
.ve/" because that introduces machine state into the result. The fresh-clone
property is what makes the tool trustable for AI agents that can't inspect
the user's working environment.

### Builds run from `docs/`

`sphinx-build` is invoked with `cwd=<repo>/docs` and `python -m sphinx -b
dirhtml . _build/html`. This mirrors the Makefile/make.bat convention and
ensures `from project_info import ...` resolves (Sphinx 7+ `chdir`s to the
conf.py dir but doesn't add it to `sys.path`; running from `docs/` puts
`""` in `sys.path` resolving to `docs/` at import time).

If you ever invoke `sphinx-build` from the repo root instead, the
`project_info` import will fail. That's not a bug in any repo — it's how
Python imports work. Use the tooling here or the repo's Makefile.

## When in doubt

- `python scripts/repo_manager.py --help` for verb-level guidance.
- `<verb> --help` for command-specific options.
- `build-all` for a one-shot health check of the whole estate.

If something doesn't fit any of the existing verbs, that's a signal — add a
verb. The CLI is the surface area; bespoke per-repo shell loops are not.
