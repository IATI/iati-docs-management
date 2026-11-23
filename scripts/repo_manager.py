#!/usr/bin/env python3.13
"""
Manage IATI documentation repositories.

Provides a CLI and Python API for working across every repository in the
IATI GitHub org tagged with the Documentation custom property. Operations
include listing the estate, checking files against the iati-docs-base
template, syncing template files via pull request, and running arbitrary
scripts in each repo.
"""

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Patterns that vary between two builds of the same code and must be
# normalised out before HTML pages are compared. Today the only known
# offender is Sphinx's ``?v=<hash>`` asset cache-buster. Extend this list
# if a future Sphinx/theme version introduces another build-volatile
# value (e.g. timestamps in footers).
_HTML_NORMALISERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\?v=[0-9a-f]+"), "?v=NORM"),
]

# The authoritative template repo
TEMPLATE_REPO = "iati-docs-base"
GITHUB_ORG = "IATI"

# Files that should be tracked across all repos. requirements.txt is the
# file that actually controls the build (RTD and the build verb both
# install from it), so it's treated as canonical and synced from the
# template like everything else; the .in files are its pip-compile inputs.
# An estate-wide dependency change means regenerating requirements.txt in
# the template and syncing it out. A repo that legitimately needs extra
# packages should carry a requirements_local.in overlay rather than
# diverging silently.
#
# docs/conf.py is shared verbatim across the estate; all per-repo values live
# in docs/project_info.py, which it imports. project_info.py is deliberately
# NOT tracked here - it's the per-repo half and must never be overwritten from
# the template.
FILES_TO_CHECK = [
    ".readthedocs.yaml",
    "requirements.in",
    "requirements_dev.in",
    "requirements.txt",
    ".github/workflows/ci.yml",
    ".vscode/launch.json",
    "docs/conf.py",
]

# Baseline .gitignore entries every estate repo should carry. Synced
# additively by `sync-gitignore`: missing entries are appended, existing
# entries are never replaced or reordered. .gitignore is deliberately NOT
# in FILES_TO_CHECK - downstream repos legitimately carry their own extra
# ignores, so it must not be synced verbatim. __pycache__/ is un-anchored
# so it catches docs/ (Sphinx compiles conf.py and project_info.py there
# at build time) as well as scripts/.
GITIGNORE_BASELINE = [
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
]


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a command, raising on a non-zero exit, and return its stripped stdout."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def git(*args: str, cwd: Path | None = None) -> str:
    return _run(["git", *args], cwd)


def gh(*args: str, cwd: Path | None = None) -> str:
    return _run(["gh", *args], cwd)


def clone_url(repo_name: str) -> str:
    return f"git@github.com:{GITHUB_ORG}/{repo_name}.git"


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


def generate_branch_name(prefix: str = "sync") -> str:
    """Build a timestamped working branch name with the given prefix."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"iati-docs-management/{prefix}-{ts}"


def current_branch(repo_path: Path) -> str:
    """Return the currently checked-out branch (empty if detached HEAD)."""
    return git("branch", "--show-current", cwd=repo_path)


def default_branch(repo_path: Path) -> str:
    """Return the upstream default branch name (e.g. 'main')."""
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo_path)
    # e.g. "origin/main" -> "main"
    return head.removeprefix("origin/")


@dataclass
class RepoCheckout:
    name: str
    path: Path


class FileState(Enum):
    MATCH = "match"
    DIFFERS = "differs"
    MISSING_IN_REPO = "missing in repo"
    MISSING_IN_TEMPLATE = "missing in template"


@dataclass
class FileCheck:
    """How one tracked file in a repo compares with the template's copy."""

    path: str
    state: FileState
    diff: str = ""


# In the result classes below, an empty ``failure`` means success.


@dataclass
class ScriptResult:
    repo: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    changed_files: list[str] = field(default_factory=list)
    failure: str = ""


@dataclass
class BlackResult:
    reformatted: list[str] = field(default_factory=list)
    failure: str = ""
    stderr_tail: str = ""


@dataclass
class PublishResult:
    committed: bool
    pushed: bool


@dataclass
class BuildResult:
    repo: str
    stage: str = "install"
    failure: str = ""
    install_exit_code: int | None = None
    install_stderr_tail: str = ""
    build_exit_code: int | None = None
    build_stderr_tail: str = ""
    warnings: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    html_dir: Path | None = None

    @property
    def failed(self) -> bool:
        return bool(self.failure)

    @property
    def stderr_tail(self) -> str:
        return self.build_stderr_tail or self.install_stderr_tail


@dataclass
class BuildComparison:
    repo: str
    baseline_ref: str
    candidate_label: str
    baseline: BuildResult
    candidate: BuildResult
    pages_added: list[str]
    pages_removed: list[str]
    pages_modified: dict[str, str]
    warnings_added: list[str]
    warnings_removed: list[str]
    baseline_scratch: Path


class UsageError(Exception):
    """A problem with the invocation, reported the way argparse reports its own."""


def get_tagged_repos() -> list[str]:
    """
    Fetch all repos tagged with Documentation=true from GitHub.

    Returns:
        List of repository names.
    """
    names = gh(
        "api",
        f"/orgs/{GITHUB_ORG}/properties/values",
        "--paginate",
        "--jq",
        '.[] | select(.properties[] | select(.property_name == "Documentation" and .value == "true")) | .repository_name',
    )
    return [r.strip() for r in names.split("\n") if r.strip()]


def get_branch_protection(repo_name: str) -> dict | None:
    """
    Fetch branch protection settings for ``main`` in ``repo_name``.

    Returns the GitHub API response dict if the branch is protected,
    or ``None`` if it isn't (HTTP 404 "Branch not protected").
    """
    try:
        protection = gh(
            "api", f"repos/{GITHUB_ORG}/{repo_name}/branches/main/protection"
        )
    except subprocess.CalledProcessError as exc:
        if "Branch not protected" in exc.stderr or "Not Found" in exc.stderr:
            return None
        raise RuntimeError(
            f"gh api failed for {repo_name}/main protection: {exc.stderr.strip()}"
        ) from exc
    return json.loads(protection)


def list_open_prs() -> list[dict]:
    """
    List all open PRs across every Documentation-tagged repo.

    Returns one dict per PR with: ``repo``, ``number``, ``title``,
    ``branch``, ``author``, ``createdAt``, ``updatedAt``,
    ``mergeable``, ``reviewDecision``, ``statusCheckRollup``. PRs
    from other authors and unrelated branches show up too - the
    verb is meant to surface other in-flight work alongside our own.
    """
    fields = (
        "number,title,headRefName,author,createdAt,updatedAt,"
        "mergeable,reviewDecision,statusCheckRollup"
    )
    results = []
    for repo_name in get_tagged_repos():
        prs = gh(
            "pr",
            "list",
            "--repo",
            f"{GITHUB_ORG}/{repo_name}",
            "--state",
            "open",
            "--json",
            fields,
        )
        for pr in json.loads(prs):
            pr["repo"] = repo_name
            pr["branch"] = pr.pop("headRefName")
            results.append(pr)
    return results


def file_state(template: RepoCheckout, repo: RepoCheckout, path: str) -> FileState:
    template_file = template.path / path
    repo_file = repo.path / path
    if not template_file.exists():
        return FileState.MISSING_IN_TEMPLATE
    if not repo_file.exists():
        return FileState.MISSING_IN_REPO
    if template_file.read_text() == repo_file.read_text():
        return FileState.MATCH
    return FileState.DIFFERS


def check_file(template: RepoCheckout, repo: RepoCheckout, path: str) -> FileCheck:
    """Compare a repo's file with the template's, with a diff if they differ."""
    state = file_state(template, repo, path)
    diff = ""
    if state is FileState.DIFFERS:
        # repo -> template, i.e. what a sync would change in the repo
        diff = subprocess.run(
            ["diff", "-u", str(repo.path / path), str(template.path / path)],
            capture_output=True,
            text=True,
        ).stdout
    return FileCheck(path, state, diff)


def sync_file(template: RepoCheckout, repo: RepoCheckout, path: str) -> FileCheck:
    """Copy a file from the template into a repo; reports its state beforehand."""
    state = file_state(template, repo, path)
    if state in (FileState.DIFFERS, FileState.MISSING_IN_REPO):
        (repo.path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template.path / path, repo.path / path)
    return FileCheck(path, state)


def extra_top_level_paths(template: RepoCheckout, repo: RepoCheckout) -> list[str]:
    """
    Top-level entries in the repo that the template lacks, ``.git`` aside.

    Informational: an extra may be deliberate or may be drift, and the
    tooling never touches it.
    """
    template_entries = {p.name for p in template.path.iterdir()}
    return sorted(
        p.name
        for p in repo.path.iterdir()
        if p.name != ".git" and p.name not in template_entries
    )


def sync_gitignore_entries(repo: RepoCheckout) -> FileCheck:
    """
    Append any missing ``GITIGNORE_BASELINE`` entries to a repo's ``.gitignore``.

    Matching is by whole line, so a narrower entry such as
    ``scripts/__pycache__/`` does not count as covering ``__pycache__/``.
    """
    gitignore = repo.path / ".gitignore"
    existed = gitignore.exists()
    content = gitignore.read_text() if existed else ""
    existing = {line.strip() for line in content.splitlines()}

    missing = [e for e in GITIGNORE_BASELINE if e not in existing]
    if not missing:
        return FileCheck(".gitignore", FileState.MATCH)

    if content and not content.endswith("\n"):
        content += "\n"
    gitignore.write_text(content + "\n".join(missing) + "\n")
    state = FileState.DIFFERS if existed else FileState.MISSING_IN_REPO
    return FileCheck(".gitignore", state)


def format_with_black(repo: RepoCheckout) -> BlackResult:
    """
    Reformat a repo's tree with black, leaving the changes for the next commit.

    Uses the black installed for this interpreter, which matches each repo's
    CI because ``requirements_dev.in`` is synced from the template.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "black", str(repo.path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return BlackResult(
            failure=f"Python interpreter not found at {sys.executable!r}."
        )

    stderr_tail = _tail(proc.stderr, 20)
    if "No module named" in proc.stderr and "black" in proc.stderr:
        return BlackResult(
            failure=(
                f"black is not installed for {sys.executable}. "
                "Install this repo's requirements_dev.txt and try again."
            ),
            stderr_tail=stderr_tail,
        )
    if proc.returncode != 0:
        return BlackResult(
            failure=(
                f"black exited {proc.returncode} - usually a syntax error "
                "in the source. See stderr_tail."
            ),
            stderr_tail=stderr_tail,
        )

    # black logs "reformatted <path>" on stderr for each file it changes.
    return BlackResult(
        reformatted=[
            line.removeprefix("reformatted ").strip()
            for line in proc.stderr.splitlines()
            if line.startswith("reformatted ")
        ]
    )


def build_repo(repo: RepoCheckout) -> BuildResult:
    """
    Build a repo's Sphinx docs in a fresh venv from its ``requirements.txt``.

    The venv and HTML go in a scratch dir under ``/tmp``, never inside the
    repo, so a local checkout passed to ``build-compare`` stays untouched.
    The scratch dir is left behind for inspection.
    """
    result = BuildResult(repo.name)

    docs_dir = repo.path / "docs"
    if not docs_dir.is_dir():
        result.failure = f"No docs/ directory in {repo.name}"
        return result

    requirements = repo.path / "requirements.txt"
    if not requirements.is_file():
        result.failure = f"No requirements.txt in {repo.name}"
        return result

    scratch_dir = Path(tempfile.mkdtemp(prefix=f"iati-build-{repo.name}-", dir="/tmp"))
    venv_path = scratch_dir / "venv"
    html_dir = scratch_dir / "html"

    venv_proc = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_path)],
        capture_output=True,
        text=True,
    )
    if venv_proc.returncode != 0:
        result.failure = "venv creation failed"
        result.install_stderr_tail = _tail(venv_proc.stderr, 30)
        return result

    install_proc = subprocess.run(
        [str(venv_path / "bin" / "pip"), "install", "-q", "-r", str(requirements)],
        capture_output=True,
        text=True,
    )
    result.install_exit_code = install_proc.returncode
    result.install_stderr_tail = _tail(install_proc.stderr, 30)
    if install_proc.returncode != 0:
        result.failure = "pip install failed"
        return result

    result.stage = "build"
    # Stops Python leaving __pycache__/ in the repo's docs/ when Sphinx
    # imports conf.py and project_info.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    build_proc = subprocess.run(
        [
            str(venv_path / "bin" / "python"),
            "-m",
            "sphinx",
            "-b",
            "dirhtml",
            ".",
            str(html_dir),
        ],
        cwd=docs_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    result.build_exit_code = build_proc.returncode
    result.build_stderr_tail = _tail(build_proc.stderr, 30)
    # The absolute repo path is stripped so warnings from two checkouts of
    # the same code compare equal.
    repo_prefix = str(repo.path.resolve()) + "/"
    result.warnings = [
        line.replace(repo_prefix, "")
        for line in build_proc.stderr.splitlines()
        if "WARNING:" in line or "ERROR:" in line
    ]

    if build_proc.returncode != 0:
        result.failure = "sphinx build failed"
        return result

    if html_dir.is_dir():
        result.pages = sorted(
            str(p.relative_to(html_dir)) for p in html_dir.rglob("*.html")
        )
        result.html_dir = html_dir

    result.stage = "complete"
    return result


def _normalise_html(content: str) -> str:
    """Strip volatile patterns so two builds of the same code compare equal."""
    for pattern, replacement in _HTML_NORMALISERS:
        content = pattern.sub(replacement, content)
    return content


def _page_diff(
    baseline_html: str,
    candidate_html: str,
    page_name: str,
) -> str | None:
    """
    Unified diff of two HTML pages after normalisation, or None if they
    are identical post-normalisation. The diff is returned in full -
    the operator ran build-compare to see what changed, and truncating
    hides exactly the signal they asked for.
    """
    baseline_norm = _normalise_html(baseline_html).splitlines()
    candidate_norm = _normalise_html(candidate_html).splitlines()
    if baseline_norm == candidate_norm:
        return None
    diff_lines = difflib.unified_diff(
        baseline_norm,
        candidate_norm,
        fromfile=f"baseline/{page_name}",
        tofile=f"candidate/{page_name}",
        n=2,
        lineterm="",
    )
    return "\n".join(diff_lines)


def build_compare(
    candidate_repo: RepoCheckout,
    baseline_ref: str = "main",
) -> BuildComparison:
    """
    Build candidate and baseline, return a structured change report.

    Builds the candidate in place (using its current working tree, then
    clones a fresh copy of the same repo at ``baseline_ref`` into a
    scratch directory and builds that as the baseline. The candidate's
    working tree is never modified.

    The result describes **what changed** between the two builds; it
    does not adjudicate whether the change is good or bad. The operator
    rolling out an estate-wide change is the one who can judge whether
    each diff matches their intent.

    Args:
        candidate_repo: The checkout to test. Its working tree is built
            as-is (uncommitted edits included).
        baseline_ref: Git ref of the same repo to compare against
            (default: ``main``).

    ``pages_modified`` in the result maps each page that differs after
    normalisation to its unified diff.
    """
    url = clone_url(candidate_repo.name)

    # Validate the baseline ref before doing any build work. ``git clone
    # --depth 1 --branch`` only accepts branch and tag refs, not commit
    # SHAs - and our error message is clearer than git's "Remote branch
    # <ref> not found in upstream origin". Doing this up front avoids
    # ~30s of wasted candidate build on a typo.
    if not git("ls-remote", "--heads", "--tags", url, baseline_ref):
        raise UsageError(
            f"Baseline ref {baseline_ref!r} is not a branch or tag in "
            f"{candidate_repo.name}. --baseline-ref accepts branch and "
            f"tag names only; commit SHAs are not supported."
        )

    print(f"Building candidate ({candidate_repo.name})...")
    candidate = build_repo(candidate_repo)

    baseline_scratch = Path(
        tempfile.mkdtemp(prefix=f"iati-baseline-{candidate_repo.name}-", dir="/tmp")
    )
    baseline_path = baseline_scratch / candidate_repo.name
    print(f"Cloning baseline ({baseline_ref})...")
    git("clone", "--depth", "1", "--branch", baseline_ref, url, str(baseline_path))
    baseline_repo = RepoCheckout(name=candidate_repo.name, path=baseline_path)
    print(f"Building baseline ({baseline_ref})...")
    baseline = build_repo(baseline_repo)

    baseline_pages = set(baseline.pages)
    candidate_pages = set(candidate.pages)

    # Both sides only have html_dir set if the build actually succeeded.
    # If either failed, skip the per-page diff (the warnings/pages_added
    # sections still carry signal).
    pages_modified: dict[str, str] = {}
    for page in sorted(baseline_pages & candidate_pages):
        if baseline.html_dir is None or candidate.html_dir is None:
            break
        baseline_file = baseline.html_dir / page
        candidate_file = candidate.html_dir / page
        if not baseline_file.is_file() or not candidate_file.is_file():
            continue
        try:
            baseline_html = baseline_file.read_text(errors="replace")
            candidate_html = candidate_file.read_text(errors="replace")
        except OSError:
            continue
        diff = _page_diff(baseline_html, candidate_html, page)
        if diff is not None:
            pages_modified[page] = diff

    baseline_warnings = set(baseline.warnings)
    candidate_warnings = set(candidate.warnings)
    return BuildComparison(
        repo=candidate_repo.name,
        baseline_ref=baseline_ref,
        candidate_label=current_branch(candidate_repo.path) or "candidate",
        baseline=baseline,
        candidate=candidate,
        pages_added=sorted(candidate_pages - baseline_pages),
        pages_removed=sorted(baseline_pages - candidate_pages),
        pages_modified=pages_modified,
        warnings_added=[w for w in candidate.warnings if w not in baseline_warnings],
        warnings_removed=[w for w in baseline.warnings if w not in candidate_warnings],
        baseline_scratch=baseline_scratch,
    )


class RepoManager:
    """A directory of estate checkouts that are published together."""

    def __init__(
        self,
        branch_name: str | None = None,
        work_dir: Path | None = None,
        repos: list[RepoCheckout] | None = None,
    ):
        """
        ``branch_name`` is the working branch created in every clone, and the
        only branch commits and pushes go to; without one, clones stay on
        their default branch and cannot be published. ``work_dir`` and
        ``repos`` adopt existing checkouts in place of a fresh ``/tmp`` dir.
        """
        self.branch_name = branch_name
        self.work_dir = work_dir or Path(
            tempfile.mkdtemp(prefix="iati-docs-", dir="/tmp")
        )
        self.repos = repos or []
        self.template: RepoCheckout | None = None

    def checkout_repo(self, repo_name: str) -> RepoCheckout:
        """Clone a repo into the work dir and put it on the working branch."""
        checkout = RepoCheckout(repo_name, self.work_dir / repo_name)

        print(f"Cloning {repo_name}...")
        git("clone", "--depth", "1", clone_url(repo_name), str(checkout.path))
        if self.branch_name:
            git("switch", "-c", self.branch_name, cwd=checkout.path)

        if repo_name == TEMPLATE_REPO:
            self.template = checkout
        else:
            self.repos.append(checkout)
        return checkout

    def checkout_all(self, include_template: bool = True) -> None:
        """Clone every tagged repo, and the template unless excluded."""
        repo_names = get_tagged_repos()
        if not include_template:
            repo_names = [r for r in repo_names if r != TEMPLATE_REPO]
        elif TEMPLATE_REPO not in repo_names:
            self.checkout_repo(TEMPLATE_REPO)

        for name in repo_names:
            self.checkout_repo(name)

    def run_script_on_all_repos(
        self,
        script_path: str | Path,
        include_template: bool = False,
    ) -> dict[str, ScriptResult]:
        """
        Run ``<script> <repo-name>`` with cwd set to each checkout.

        Stops at the first failure: a non-zero exit, a timeout, or the script
        not starting. Nothing is committed or pushed.
        """
        script_path = Path(script_path).resolve()

        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        repos_to_process = list(self.repos)
        if include_template and self.template:
            repos_to_process.append(self.template)

        results = {}
        for repo in repos_to_process:
            result = results[repo.name] = ScriptResult(repo.name)
            try:
                proc = subprocess.run(
                    [str(script_path), repo.name],
                    cwd=repo.path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                result.exit_code = proc.returncode
                result.stdout = proc.stdout
                result.stderr = proc.stderr
                result.changed_files = git(
                    "status", "--porcelain", cwd=repo.path
                ).splitlines()
                if proc.returncode != 0:
                    result.failure = f"Script exited with code {proc.returncode}"
            except subprocess.TimeoutExpired:
                result.failure = "Timed out after 300 seconds"
            except (OSError, subprocess.CalledProcessError) as exc:
                result.failure = str(exc)

            if result.failure:
                break

        return results

    def _ensure_safe_branch(self, repo: RepoCheckout) -> None:
        """
        Refuse to operate unless the working tree is on the expected
        working branch. Catches both the case where the checkout failed
        to switch off the default branch and the case where someone
        manually switched branches between checkout and publish.
        """
        current = current_branch(repo.path)
        default = default_branch(repo.path)
        if not current or current == default or current in ("main", "master"):
            raise RuntimeError(
                f"Refusing to operate on branch {current!r} in {repo.name} "
                f"(default branch is {default!r}). Expected to be on "
                f"{self.branch_name!r} - did the checkout fail?"
            )
        if current != self.branch_name:
            raise RuntimeError(
                f"Refusing to operate on branch {current!r} in {repo.name}: "
                f"expected {self.branch_name!r}. Pass --branch-name to "
                "match the actual branch, or switch the checkout."
            )

    def commit_changes(self, repo: RepoCheckout, message: str) -> bool:
        """Commit the whole working tree; False if there was nothing to commit."""
        self._ensure_safe_branch(repo)

        if not git("status", "--porcelain", cwd=repo.path):
            return False

        # Uncaptured so git's own output reaches the operator.
        subprocess.run(["git", "add", "-A"], cwd=repo.path, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo.path, check=True)
        return True

    def push_changes(self, repo: RepoCheckout, pr_title: str, pr_body: str) -> bool:
        """Push the working branch and open a PR; False if it has nothing to publish."""
        self._ensure_safe_branch(repo)

        default = default_branch(repo.path)
        # rev-list works before upstream tracking exists for the new branch.
        ahead = git("rev-list", "--count", f"origin/{default}..HEAD", cwd=repo.path)
        if int(ahead) == 0:
            return False

        # Uncaptured so git's own output reaches the operator.
        subprocess.run(
            ["git", "push", "-u", "origin", self.branch_name],
            cwd=repo.path,
            check=True,
        )
        gh(
            "pr",
            "create",
            "--title",
            pr_title,
            "--body",
            pr_body,
            "--base",
            default,
            "--head",
            self.branch_name,
            cwd=repo.path,
        )
        return True

    def update_all_to_github(
        self,
        commit_message: str,
        pr_body: str | None = None,
        push_delay: float = 20.0,
    ) -> dict[str, PublishResult]:
        """
        Format, commit, push and open a PR in every repo.

        black runs over every repo first and any failure aborts before anything
        is committed, so no PR opens only to fail CI's ``black --check``. The
        commit message doubles as the PR title. ``push_delay`` is the pause in
        seconds between pushes, which spreads out the ReadTheDocs build queue.
        """
        if pr_body is None:
            pr_body = (
                f"Automated update from iati-docs-management.\n\n"
                f"Branch: `{self.branch_name}`\n"
                f"Commit: {commit_message}"
            )

        # Every repo is formatted before failures are reported, so the operator
        # sees all of them at once.
        format_results = {repo.name: format_with_black(repo) for repo in self.repos}
        failures = {name: r for name, r in format_results.items() if r.failure}
        if failures:
            for name, r in failures.items():
                print(f"\n  {name}: {r.failure}")
                for line in r.stderr_tail.splitlines():
                    print(f"    {line}")
            raise RuntimeError(
                f"black failed in {len(failures)} repo(s); aborting publish. "
                "No commits or PRs were created."
            )
        total_reformatted = sum(len(r.reformatted) for r in format_results.values())
        if total_reformatted:
            print(f"\nblack reformatted {total_reformatted} file(s):")
            for name, r in format_results.items():
                if r.reformatted:
                    print(f"  {name}: {len(r.reformatted)} file(s)")

        results = {}
        last_idx = len(self.repos) - 1
        for idx, repo in enumerate(self.repos):
            committed = self.commit_changes(repo, commit_message)
            # Push even without a new commit: the branch may carry commits made
            # by hand before make-prs.
            pushed = self.push_changes(repo, commit_message, pr_body)
            results[repo.name] = PublishResult(committed, pushed)

            if push_delay and pushed and idx < last_idx:
                print(f"  sleeping {push_delay:g}s before next push...")
                time.sleep(push_delay)

        return results

    def build_all_repos(self) -> dict[str, BuildResult]:
        """Build every checkout, template first."""
        checkouts = ([self.template] if self.template else []) + self.repos
        results = {}
        for repo in checkouts:
            print(f"Building {repo.name}...")
            results[repo.name] = build_repo(repo)
        return results

    def cleanup(self) -> None:
        """Remove the clones, then the work dir if that leaves it empty."""
        for repo in [*self.repos, self.template]:
            if repo and repo.path.exists():
                shutil.rmtree(repo.path)
        try:
            self.work_dir.rmdir()
        except OSError:
            pass

    def __enter__(self) -> "RepoManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


def _print_header(*lines: str, width: int = 60) -> None:
    """Print a ruled section header."""
    print(f"\n{'=' * width}")
    for line in lines:
        print(f" {line}")
    print("=" * width)


def _print_lines(
    lines: list[str], prefix: str = "    ", limit: int | None = None
) -> None:
    """Print lines under a prefix, summarising any beyond ``limit``."""
    for line in lines[:limit]:
        print(f"{prefix}{line}")
    if limit is not None and len(lines) > limit:
        print(f"    ... ({len(lines) - limit} more)")


def _print_table(rows: list[dict[str, str]]) -> None:
    """Print rows as left-aligned columns headed by their upper-cased keys."""
    cols = list(rows[0])
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in cols}
    print()
    for row in [{c: c.upper() for c in cols}, *rows]:
        print("  ".join(row[c].ljust(widths[c]) for c in cols))


_CHECK_NOTES = {
    FileState.MISSING_IN_TEMPLATE: "Error: Template file does not exist",
    FileState.MISSING_IN_REPO: "Error: File missing from repo",
}
_SYNC_NOTES = {
    FileState.MISSING_IN_REPO: "Action: create",
    FileState.DIFFERS: "Action: update",
}


def print_file_results(
    results: dict[str, list[FileCheck]],
    title: str,
    notes: dict[FileState, str],
    show_diff: bool = False,
) -> None:
    """Print each repo's per-file state, annotated from ``notes``."""
    _print_header(title)

    for repo_name, checks in results.items():
        print(f"\n{repo_name}:")
        for check in checks:
            status = "OK" if check.state is FileState.MATCH else "NEEDS ATTENTION"
            print(f"  {check.path}: {status}")
            if check.state in notes:
                print(f"    {notes[check.state]}")
            if show_diff and check.diff:
                print(f"\n    --- Diff for {check.path} ---")
                _print_lines(check.diff.splitlines())
                print("    --- End diff ---\n")


def print_publish_results(results: dict[str, PublishResult]) -> None:
    _print_header("COMMIT + PUSH + PR")

    for repo_name, r in results.items():
        print(f"\n{repo_name}:")
        print(f"  commit: {'commit' if r.committed else 'skip'}")
        print(f"  push: {'push' if r.pushed else 'skip'}")


def _age_str(iso_dt: str) -> str:
    """Compact human-readable duration since an ISO 8601 timestamp."""
    dt = datetime.datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
    delta = datetime.datetime.now(datetime.timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 604800:
        return f"{seconds // 86400}d"
    return f"{seconds // 604800}w"


def _pr_ci_status(rollup: list | None) -> str:
    """Roll up a PR's statusCheckRollup into a single ``ok|fail|pending|none``.

    statusCheckRollup mixes two shapes: ``CheckRun`` entries (GitHub
    Actions) use ``conclusion``; ``StatusContext`` entries (external
    statuses like Read the Docs) use ``state``. Either may be present.
    """
    if not rollup:
        return "none"
    bad = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "ERROR"}
    good = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    outcomes = [c.get("conclusion") or c.get("state") for c in rollup]
    if any(o in bad for o in outcomes):
        return "fail"
    if any(not o for o in outcomes):
        return "pending"
    if all(o in good for o in outcomes):
        return "ok"
    return "mixed"


_PR_MERGEABLE = {"MERGEABLE": "ok", "CONFLICTING": "conflict", "UNKNOWN": "unknown"}
_PR_REVIEW = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes",
    "REVIEW_REQUIRED": "required",
}


def print_open_prs(prs: list[dict]) -> None:
    _print_header("OPEN PULL REQUESTS")

    if not prs:
        print("\nNo open PRs across the estate.")
        return

    _print_table(
        [
            {
                "repo": pr["repo"],
                "pr": f"#{pr['number']}",
                "age": _age_str(pr["createdAt"]),
                "upd": _age_str(pr["updatedAt"]),
                "merge": _PR_MERGEABLE.get(pr.get("mergeable") or "", "?"),
                "ci": _pr_ci_status(pr.get("statusCheckRollup")),
                "review": _PR_REVIEW.get(pr.get("reviewDecision") or "", "n/a"),
                "author": (pr.get("author") or {}).get("login", "?"),
                "branch": pr["branch"],
                "title": pr["title"],
            }
            for pr in sorted(prs, key=lambda p: (p["repo"], p["number"]))
        ]
    )


_UNPROTECTED = dict.fromkeys(
    [
        "reviews",
        "checks",
        "strict",
        "dismiss_stale",
        "enforce_admins",
        "force_push",
        "deletions",
    ],
    "-",
)


def _enabled(protection: dict, setting: str) -> bool:
    return bool((protection.get(setting) or {}).get("enabled"))


def print_protection(protections: dict[str, dict | None]) -> None:
    _print_header("BRANCH PROTECTION (main)")
    print(
        "\nStructural settings (reviews, force_push, etc) should match across"
        "\nthe estate. CHECKS is the count of required status contexts and"
        "\nvaries per repo (each name embeds the repo's RTD slug)."
    )

    rows = []
    for repo_name in sorted(protections):
        p = protections[repo_name]
        if p is None:
            rows.append({"repo": repo_name, "protected": "no", **_UNPROTECTED})
            continue
        reviews = p.get("required_pull_request_reviews") or {}
        checks = p.get("required_status_checks") or {}
        rows.append(
            {
                "repo": repo_name,
                "protected": "yes",
                "reviews": str(reviews.get("required_approving_review_count", 0)),
                "checks": str(len(checks.get("contexts") or [])),
                "strict": "yes" if checks.get("strict") else "no",
                "dismiss_stale": (
                    "yes" if reviews.get("dismiss_stale_reviews") else "no"
                ),
                "enforce_admins": "yes" if _enabled(p, "enforce_admins") else "no",
                "force_push": (
                    "allowed" if _enabled(p, "allow_force_pushes") else "forbidden"
                ),
                "deletions": (
                    "allowed" if _enabled(p, "allow_deletions") else "forbidden"
                ),
            }
        )
    _print_table(rows)


def print_extra_paths(extras: dict[str, list[str]]) -> None:
    """Print each repo's extra top-level paths, skipping repos with none."""
    repos_with_extras = {name: paths for name, paths in extras.items() if paths}
    if not repos_with_extras:
        return

    _print_header("EXTRA TOP-LEVEL PATHS (in repo, not in template)")
    print(
        "\nReported for information only - the tooling never touches these."
        "\nThey may be deliberate per-repo additions (local linter config,"
        "\nauxiliary toolchains) or drift worth investigating."
    )
    for repo_name, paths in repos_with_extras.items():
        print(f"\n{repo_name}:")
        _print_lines(paths, prefix="  - ")


def print_run_script_results(results: dict[str, ScriptResult]) -> None:
    _print_header("RUN-SCRIPT RESULTS")

    for repo_name, r in results.items():
        print(f"\n{repo_name}:")
        if r.failure:
            print(f"  FAILED: {r.failure}")
        if r.exit_code is not None:
            print(f"  exit: {r.exit_code}")

        stdout = r.stdout.rstrip()
        stderr = r.stderr.rstrip()
        if stdout:
            print("  stdout:")
            _print_lines(stdout.splitlines())
        if stderr:
            print("  stderr:")
            _print_lines(stderr.splitlines())
        if r.changed_files:
            print(f"  changed: {len(r.changed_files)} file(s)")
            _print_lines(r.changed_files)
        if not (r.failure or stdout or stderr or r.changed_files):
            print("  (no output, no changes)")


def print_build_result(result: BuildResult) -> None:
    _print_header(f"BUILD: {result.repo}")
    if result.failed:
        print(f"  FAILED at stage '{result.stage}': {result.failure}")
    else:
        print("  OK")
    if result.install_exit_code is not None:
        print(f"  install exit:    {result.install_exit_code}")
    if result.build_exit_code is not None:
        print(f"  sphinx exit:     {result.build_exit_code}")
    print(f"  warnings:        {len(result.warnings)}")
    print(f"  pages produced:  {len(result.pages)}")
    if result.warnings:
        print("  warning lines:")
        _print_lines(result.warnings, limit=20)
    if result.failed and result.install_stderr_tail:
        print("  install stderr tail:")
        _print_lines(result.install_stderr_tail.splitlines())
    if result.failed and result.build_stderr_tail:
        print("  build stderr tail:")
        _print_lines(result.build_stderr_tail.splitlines())


def print_build_all_results(results: dict[str, BuildResult]) -> None:
    """One-line summary per repo, then detail for any that failed."""
    _print_header("BUILD-ALL SUMMARY", width=72)
    print(f"  {'repo':<40} {'status':<10} {'warns':>6} {'pages':>6}")
    print(f"  {'-' * 40} {'-' * 10} {'-' * 6} {'-' * 6}")
    for repo_name, r in results.items():
        status = "FAIL" if r.failed else "OK"
        warns = len(r.warnings)
        pages = len(r.pages)
        print(f"  {repo_name:<40} {status:<10} {warns:>6} {pages:>6}")

    failures = {name: r for name, r in results.items() if r.failed}
    if failures:
        print(f"\n  {len(failures)} repo(s) failed: {', '.join(failures)}")
        for name, r in failures.items():
            print(f"\n  --- {name} failure detail ---")
            print(f"    stage: {r.stage}")
            print(f"    reason: {r.failure}")
            _print_lines(r.stderr_tail.splitlines()[-15:])


def print_build_compare(comparison: BuildComparison) -> None:
    baseline, candidate = comparison.baseline, comparison.candidate
    _print_header(
        f"BUILD-COMPARE: {comparison.repo}",
        f"  baseline:  {comparison.baseline_ref}",
        f"  candidate: {comparison.candidate_label}",
        width=72,
    )
    for label, build in (("baseline:", baseline), ("candidate:", candidate)):
        print(
            f"  {label:<10} {'FAIL' if build.failed else 'OK':<5}  "
            f"warns={len(build.warnings)}  "
            f"pages={len(build.pages)}"
        )

    # Shown first because the sections below may be empty or misleading
    # after a failed build.
    if candidate.failed and not baseline.failed:
        print(f"\n  Candidate build did not complete (stage: {candidate.stage}).")
        print(f"  Reason: {candidate.failure}")
        if candidate.stderr_tail:
            print("  stderr tail:")
            _print_lines(candidate.stderr_tail.splitlines()[-15:])

    pages_added = comparison.pages_added
    pages_removed = comparison.pages_removed
    pages_modified = comparison.pages_modified
    warnings_added = comparison.warnings_added
    warnings_removed = comparison.warnings_removed

    if pages_added:
        print(f"\n  Pages added ({len(pages_added)}):")
        _print_lines(pages_added, "    + ", limit=30)

    if pages_removed:
        print(f"\n  Pages removed ({len(pages_removed)}):")
        _print_lines(pages_removed, "    - ", limit=30)

    if pages_modified:
        print(f"\n  Pages modified ({len(pages_modified)}):")
        for page, diff in sorted(pages_modified.items()):
            line_count = diff.count("\n") + 1
            print(f"    ~ {page}  ({line_count} diff line(s))")
        print()
        for page, diff in sorted(pages_modified.items()):
            print(f"  --- diff: {page} ---")
            _print_lines(diff.splitlines())
            print()

    if warnings_added:
        print(f"  Warnings added ({len(warnings_added)}):")
        _print_lines(warnings_added, "    + ", limit=20)
        print()

    if warnings_removed:
        print(f"  Warnings removed ({len(warnings_removed)}):")
        _print_lines(warnings_removed, "    - ", limit=20)
        print()

    changed = any(
        [pages_added, pages_removed, pages_modified, warnings_added, warnings_removed]
    )
    if changed or candidate.failed:
        print(
            f"\n  Baseline checkout (for further inspection): "
            f"{comparison.baseline_scratch}"
        )
    else:
        print("\n  No differences detected.")


@contextmanager
def checked_out_estate(branch_name: str):
    """Clone the whole estate onto a working branch; remove the clones on exit."""
    with RepoManager(branch_name) as manager:
        print(f"Working directory: {manager.work_dir}")
        print(f"Working branch: {manager.branch_name}")
        print("Checking out repositories...")
        manager.checkout_all()
        print(f"Checked out {len(manager.repos)} repos (+ template)")
        yield manager


def publish(manager: RepoManager, args: argparse.Namespace) -> None:
    results = manager.update_all_to_github(args.message, args.pr_body, args.push_delay)
    print_publish_results(results)


def cmd_list(args: argparse.Namespace) -> None:
    print("Documentation repositories:")
    for repo in get_tagged_repos():
        marker = " (template)" if repo == TEMPLATE_REPO else ""
        print(f"  - {repo}{marker}")


def cmd_list_prs(args: argparse.Namespace) -> None:
    print_open_prs(list_open_prs())


def cmd_check_protection(args: argparse.Namespace) -> None:
    print_protection({name: get_branch_protection(name) for name in get_tagged_repos()})


def cmd_check(args: argparse.Namespace) -> None:
    with checked_out_estate(generate_branch_name()) as manager:
        results = {
            repo.name: [check_file(manager.template, repo, f) for f in args.files]
            for repo in manager.repos
        }
        print_file_results(
            results, "CHECK RESULTS", _CHECK_NOTES, show_diff=not args.no_diff
        )
        print_extra_paths(
            {
                repo.name: extra_top_level_paths(manager.template, repo)
                for repo in manager.repos
            }
        )

        checks = [check for repo_checks in results.values() for check in repo_checks]
        matching = sum(check.state is FileState.MATCH for check in checks)
        print(f"\nSummary: {matching}/{len(checks)} files match template")


def cmd_sync(args: argparse.Namespace) -> None:
    with checked_out_estate(args.branch_name or generate_branch_name()) as manager:
        results = {
            repo.name: [sync_file(manager.template, repo, f) for f in args.files]
            for repo in manager.repos
        }
        print_file_results(results, "FILE COPY", _SYNC_NOTES)
        publish(manager, args)


def cmd_sync_gitignore(args: argparse.Namespace) -> None:
    branch_name = args.branch_name or generate_branch_name("gitignore")
    with checked_out_estate(branch_name) as manager:
        results = {repo.name: [sync_gitignore_entries(repo)] for repo in manager.repos}
        print_file_results(results, "GITIGNORE SYNC", _SYNC_NOTES)
        publish(manager, args)


def cmd_run_script(args: argparse.Namespace) -> int:
    branch_name = args.branch_name or generate_branch_name(
        f"script-{Path(args.script).stem}"
    )
    with checked_out_estate(branch_name) as manager:
        results = manager.run_script_on_all_repos(args.script, args.include_template)
        print_run_script_results(results)

        failed = next((r for r in results.values() if r.failure), None)
        if failed:
            print(
                f"\nERROR: Script failed in {failed.repo} "
                f"({failed.failure}). "
                "Aborting; no changes published."
            )
            return 1

        changed = [r for r in results.values() if r.changed_files]
        if not changed:
            print("\nNo file changes; nothing to commit or push.")
            return 0
        if not args.message:
            raise UsageError(
                f"Script produced changes in {len(changed)} "
                "repo(s); pass -m to commit and open PRs."
            )
        publish(manager, args)
        return 0


def cmd_checkout_all(args: argparse.Namespace) -> None:
    # No context manager: the checkouts must outlive this command for make-prs.
    manager = RepoManager(args.branch_name or generate_branch_name("refactor"))
    print(f"Working directory: {manager.work_dir}")
    print(f"Working branch: {manager.branch_name}")
    print("Checking out repositories...")
    manager.checkout_all(include_template=not args.no_template)
    print(f"\nCheckouts ready in: {manager.work_dir}")
    print(f"Branch in each repo:  {manager.branch_name}")
    print("\nNext: edit files in the checkouts, then run:")
    print(
        f"  python scripts/repo_manager.py make-prs "
        f"--dir {manager.work_dir} -m 'Your commit message'"
    )


def cmd_make_prs(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir)
    if not work_dir.is_dir():
        raise UsageError(f"--dir {work_dir} is not a directory")

    checkouts = sorted(
        p
        for p in work_dir.iterdir()
        if p.is_dir() and (p / ".git").exists() and p.name != TEMPLATE_REPO
    )
    if not checkouts:
        print(f"No repository checkouts found in {work_dir}")
        return

    branch_name = args.branch_name or current_branch(checkouts[0])
    if not branch_name:
        raise UsageError(
            f"Could not detect branch from {checkouts[0]} "
            "(detached HEAD?). Pass --branch-name."
        )

    # No context manager: the checkouts must survive for re-runs.
    manager = RepoManager(
        branch_name, work_dir, [RepoCheckout(p.name, p) for p in checkouts]
    )
    print(f"Working directory: {manager.work_dir}")
    print(f"Working branch: {manager.branch_name}")
    print(f"Repos to process: {len(manager.repos)}")
    publish(manager, args)


def cmd_build(args: argparse.Namespace) -> int:
    with RepoManager() as manager:
        print(f"Working directory: {manager.work_dir}")
        result = build_repo(manager.checkout_repo(args.repo))
        print_build_result(result)
        return 1 if result.failed else 0


def cmd_build_all(args: argparse.Namespace) -> int:
    with RepoManager() as manager:
        print(f"Working directory: {manager.work_dir}")
        print("Checking out repositories...")
        manager.checkout_all()
        results = manager.build_all_repos()
        print_build_all_results(results)
        return 1 if any(r.failed for r in results.values()) else 0


def cmd_build_compare(args: argparse.Namespace) -> int:
    candidate_path = Path(args.dir).resolve()
    if not candidate_path.is_dir() or not (candidate_path / ".git").is_dir():
        raise UsageError(f"--dir {candidate_path} is not a git checkout")

    # The baseline is cloned from IATI/<repo>, named by the candidate's origin
    # remote or, failing that, its directory.
    try:
        source = git("remote", "get-url", "origin", cwd=candidate_path)
        repo_name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except subprocess.CalledProcessError:
        source = f"directory name {candidate_path.name!r}"
        repo_name = candidate_path.name

    if repo_name not in get_tagged_repos():
        raise UsageError(
            f"Repo {repo_name!r} (inferred from {source}) is not in the "
            f"Documentation-tagged estate. Either:\n"
            f"  * --dir points at a checkout that isn't an IATI docs repo, or\n"
            f"  * the upstream repo exists but isn't tagged Documentation=true "
            f"(use `list` to see what is).\n"
            f"Baselines are always cloned from IATI/<repo> on github.com."
        )

    comparison = build_compare(
        RepoCheckout(repo_name, candidate_path), args.baseline_ref
    )
    print_build_compare(comparison)
    return 1 if comparison.candidate.failed and not comparison.baseline.failed else 0


def _add_publish_args(
    parser: argparse.ArgumentParser, message_required: bool = True
) -> None:
    message_help = "Commit message, also used as the PR title"
    if not message_required:
        message_help += " (required if the script changes files)"
    parser.add_argument("--message", "-m", required=message_required, help=message_help)
    parser.add_argument(
        "--branch-name",
        help="Working branch name (default: generated; make-prs detects it "
        "from the first checkout)",
    )
    parser.add_argument("--pr-body", help="Pull request body (default: generated)")
    parser.add_argument(
        "--push-delay",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="Pause between pushes, which spreads out the ReadTheDocs build "
        "queue (default: 20; 0 disables)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the IATI documentation estate. README.rst has the "
        "full guide.\n\n"
        "Read-only: list, list-prs, check-protection, check, build, build-all,\n"
        "build-compare. Opens pull requests: sync, sync-gitignore, make-prs,\n"
        "and run-script when the script changes files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    def add_verb(name, func, help, description=None):
        verb = subparsers.add_parser(
            name,
            help=help,
            description=description or help,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        verb.set_defaults(func=func)
        return verb

    add_verb("list", cmd_list, "List all tagged documentation repos")
    add_verb(
        "list-prs",
        cmd_list_prs,
        "List every open PR across the estate with merge, CI and review status",
    )
    add_verb(
        "check-protection",
        cmd_check_protection,
        "Show branch protection on main for every repo",
    )

    check = add_verb("check", cmd_check, "Check repos against template")
    check.add_argument(
        "--files", nargs="+", default=FILES_TO_CHECK, help="Files to check"
    )
    check.add_argument(
        "--no-diff", action="store_true", help="Hide diff output for mismatched files"
    )

    sync = add_verb(
        "sync",
        cmd_sync,
        "Sync template files to repos and open PRs",
        "Copy the tracked files from iati-docs-base into every repo and open a\n"
        "PR for each repo that changed. Run 'check' first to preview the diffs.",
    )
    sync.add_argument(
        "--files",
        nargs="+",
        default=FILES_TO_CHECK,
        help="Files to sync from the template",
    )
    _add_publish_args(sync)

    sync_gitignore = add_verb(
        "sync-gitignore",
        cmd_sync_gitignore,
        "Additively sync baseline .gitignore entries and open PRs",
        "Append any missing GITIGNORE_BASELINE entries to each repo's\n"
        ".gitignore, leaving its existing entries alone, and open a PR for each\n"
        "repo that changed.",
    )
    _add_publish_args(sync_gitignore)

    run_script = add_verb(
        "run-script",
        cmd_run_script,
        "Run a script in each repo; PR any changes it produces",
        "Run an executable script as '<script> <repo-name>' with cwd set to\n"
        "each repo's checkout, allowing five minutes per repo.\n\n"
        "A non-zero exit in any repo aborts the run and publishes nothing. If\n"
        "the script changes files, the changes are committed and opened as PRs,\n"
        "which needs -m. Otherwise nothing is published.",
    )
    run_script.add_argument("script", help="Path to the script to run in each repo")
    run_script.add_argument(
        "--include-template",
        action="store_true",
        help="Also run on the template repo (iati-docs-base)",
    )
    _add_publish_args(run_script, message_required=False)

    checkout_all = add_verb(
        "checkout-all",
        cmd_checkout_all,
        "Clone every tagged repo to a persistent dir for manual edits",
        "Clone every repo and the template into a fresh directory under /tmp,\n"
        "on a working branch, and leave it there. Edit the checkouts by hand,\n"
        "then run 'make-prs'.",
    )
    checkout_all.add_argument(
        "--branch-name",
        help="Working branch name "
        "(default: iati-docs-management/refactor-<timestamp>)",
    )
    checkout_all.add_argument(
        "--no-template",
        action="store_true",
        help="Skip cloning the template repo (iati-docs-base)",
    )

    make_prs = add_verb(
        "make-prs",
        cmd_make_prs,
        "Commit and PR changes from a checkout-all directory",
        "Commit any uncommitted changes in each checkout under --dir, push the\n"
        "working branch and open a PR. Repos with nothing to publish are\n"
        "skipped. Every checkout must be on the same branch.",
    )
    make_prs.add_argument(
        "--dir",
        required=True,
        help="Directory containing the repo checkouts (from checkout-all)",
    )
    _add_publish_args(make_prs)

    build = add_verb(
        "build",
        cmd_build,
        "Build one repo's docs in a fresh venv inside a fresh clone",
        "Clone the repo's default branch, install requirements.txt into a fresh\n"
        "venv and run Sphinx. Reports the exit code, warnings and page count.",
    )
    build.add_argument("repo", help="Name of the repo to build")

    add_verb(
        "build-all",
        cmd_build_all,
        "Build every repo's default branch and report a health snapshot",
    )

    build_compare_verb = add_verb(
        "build-compare",
        cmd_build_compare,
        "Build a candidate checkout and a baseline ref; report what differs",
        "Build a local checkout as it stands, uncommitted edits included, and a\n"
        "fresh clone of the same repo at --baseline-ref. Reports pages and\n"
        "Sphinx warnings added, removed or changed. The candidate's tree is not\n"
        "modified.\n\n"
        "Exits 1 only if the candidate fails to build and the baseline succeeds.",
    )
    build_compare_verb.add_argument(
        "--dir", required=True, help="Path to a local checkout to use as the candidate"
    )
    build_compare_verb.add_argument(
        "--baseline-ref",
        default="main",
        help="Branch or tag of the upstream repo to compare against (default: main)",
    )

    return parser


def main() -> int | None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return None

    # Every verb needs gh, so fail here rather than as a CalledProcessError
    # from deep inside a run.
    try:
        auth_check = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        parser.error(
            "gh CLI is not installed. Install from https://cli.github.com/ "
            "and run `gh auth login`."
        )
    if auth_check.returncode != 0:
        parser.error(
            "gh CLI is not authenticated for the IATI org. Run "
            "`gh auth login` and try again."
        )

    try:
        return args.func(args)
    except UsageError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
