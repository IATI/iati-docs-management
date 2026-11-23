#!/usr/bin/env python3.13
"""
Manage IATI documentation repositories.

Provides a CLI and Python API for working across every repository in the
IATI GitHub org tagged with the Documentation custom property. Operations
include listing the estate, checking files against the iati-docs-base
template, syncing template files via pull request, and running arbitrary
scripts in each repo.
"""

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
        raise ValueError(
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


def _print_header(heading: str) -> None:
    """Print a standard 60-char ruled section header."""
    print(f"\n{'=' * 60}")
    print(f" {heading}")
    print("=" * 60)


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
                for line in check.diff.splitlines():
                    print(f"    {line}")
                print(f"    --- End diff ---\n")


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
    """Tabular dump of open PRs across the estate."""
    _print_header("OPEN PULL REQUESTS")

    if not prs:
        print("\nNo open PRs across the estate.")
        return

    rows = []
    for pr in sorted(prs, key=lambda p: (p["repo"], p["number"])):
        rows.append(
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
        )

    cols = [
        "repo",
        "pr",
        "age",
        "upd",
        "merge",
        "ci",
        "review",
        "author",
        "branch",
        "title",
    ]
    widths = {c: max(len(c.upper()), max(len(r[c]) for r in rows)) for c in cols}
    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    print()
    print(header)
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in cols))


def print_protection(protections: dict[str, dict | None]) -> None:
    """
    Tabular dump of branch protection state per repo.

    Structural columns (reviews, force_push, deletions, dismiss_stale,
    enforce_admins, strict) come from a single canonical policy and
    should match across the estate. The CHECKS column (count of required
    status contexts) is inherently per-repo because each context name
    embeds the repo's RTD slug, so we report it but don't compare it.
    """
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
            rows.append(
                {
                    "repo": repo_name,
                    "protected": "no",
                    "reviews": "-",
                    "checks": "-",
                    "strict": "-",
                    "dismiss_stale": "-",
                    "enforce_admins": "-",
                    "force_push": "-",
                    "deletions": "-",
                }
            )
            continue
        pr_reviews = p.get("required_pull_request_reviews") or {}
        rsc = p.get("required_status_checks") or {}
        rows.append(
            {
                "repo": repo_name,
                "protected": "yes",
                "reviews": str(pr_reviews.get("required_approving_review_count", 0)),
                "checks": str(len(rsc.get("contexts") or [])),
                "strict": "yes" if rsc.get("strict") else "no",
                "dismiss_stale": (
                    "yes" if pr_reviews.get("dismiss_stale_reviews") else "no"
                ),
                "enforce_admins": (
                    "yes" if (p.get("enforce_admins") or {}).get("enabled") else "no"
                ),
                "force_push": (
                    "allowed"
                    if (p.get("allow_force_pushes") or {}).get("enabled")
                    else "forbidden"
                ),
                "deletions": (
                    "allowed"
                    if (p.get("allow_deletions") or {}).get("enabled")
                    else "forbidden"
                ),
            }
        )

    cols = [
        "repo",
        "protected",
        "reviews",
        "checks",
        "strict",
        "dismiss_stale",
        "enforce_admins",
        "force_push",
        "deletions",
    ]
    widths = {c: max(len(c.upper()), max(len(r[c]) for r in rows)) for c in cols}
    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    print()
    print(header)
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in cols))


def print_extra_paths(extras: dict[str, list[str]]) -> None:
    """
    Print top-level paths found in each repo that don't exist in the
    template. Skips repos with nothing to report.
    """
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
        for p in paths:
            print(f"  - {p}")


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
            for line in stdout.splitlines():
                print(f"    {line}")
        if stderr:
            print("  stderr:")
            for line in stderr.splitlines():
                print(f"    {line}")
        if r.changed_files:
            print(f"  changed: {len(r.changed_files)} file(s)")
            for line in r.changed_files:
                print(f"    {line}")
        if not (r.failure or stdout or stderr or r.changed_files):
            print("  (no output, no changes)")


def print_build_result(result: BuildResult) -> None:
    """Print a single build_repo result in human-readable form."""
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
        for w in result.warnings[:20]:
            print(f"    {w}")
        if len(result.warnings) > 20:
            print(f"    ... ({len(result.warnings) - 20} more)")
    if result.install_stderr_tail and result.failed:
        print("  install stderr tail:")
        for line in result.install_stderr_tail.splitlines():
            print(f"    {line}")
    if result.build_stderr_tail and result.failed:
        print("  build stderr tail:")
        for line in result.build_stderr_tail.splitlines():
            print(f"    {line}")


def print_build_all_results(results: dict[str, BuildResult]) -> None:
    """One-line summary per repo for build-all."""
    print(f"\n{'=' * 72}")
    print(" BUILD-ALL SUMMARY")
    print("=" * 72)
    print(f"  {'repo':<40} {'status':<10} {'warns':>6} {'pages':>6}")
    print(f"  {'-' * 40} {'-' * 10} {'-' * 6} {'-' * 6}")
    for repo_name, r in results.items():
        status = "FAIL" if r.failed else "OK"
        warns = len(r.warnings)
        pages = len(r.pages)
        print(f"  {repo_name:<40} {status:<10} {warns:>6} {pages:>6}")
    failures = [n for n, r in results.items() if r.failed]
    if failures:
        print(f"\n  {len(failures)} repo(s) failed: {', '.join(failures)}")
        for name in failures:
            r = results[name]
            print(f"\n  --- {name} failure detail ---")
            print(f"    stage: {r.stage}")
            print(f"    reason: {r.failure}")
            tail = r.build_stderr_tail or r.install_stderr_tail
            for line in tail.splitlines()[-15:]:
                print(f"    {line}")


def print_build_compare(comparison: BuildComparison) -> None:
    """
    Print a build_compare change report.

    The report describes what differs between baseline and candidate. It
    doesn't categorise differences as good or bad - that judgement belongs
    to the operator rolling out the change.
    """
    print(f"\n{'=' * 72}")
    print(f" BUILD-COMPARE: {comparison.repo}")
    print(f"   baseline:  {comparison.baseline_ref}")
    print(f"   candidate: {comparison.candidate_label}")
    print("=" * 72)

    baseline = comparison.baseline
    candidate = comparison.candidate
    print(
        f"  baseline:  {'FAIL' if baseline.failed else 'OK':<5}  "
        f"warns={len(baseline.warnings)}  "
        f"pages={len(baseline.pages)}"
    )
    print(
        f"  candidate: {'FAIL' if candidate.failed else 'OK':<5}  "
        f"warns={len(candidate.warnings)}  "
        f"pages={len(candidate.pages)}"
    )

    # If the candidate failed to build, surface that first - downstream
    # sections may be empty or misleading. Don't suppress them; the
    # operator may still want to see partial output.
    if candidate.failed and not baseline.failed:
        print(f"\n  Candidate build did not complete (stage: {candidate.stage}).")
        print(f"  Reason: {candidate.failure}")
        tail = candidate.build_stderr_tail or candidate.install_stderr_tail
        if tail:
            print("  stderr tail:")
            for line in tail.splitlines()[-15:]:
                print(f"    {line}")

    pages_added = comparison.pages_added
    pages_removed = comparison.pages_removed
    pages_modified = comparison.pages_modified
    warnings_added = comparison.warnings_added
    warnings_removed = comparison.warnings_removed

    if pages_added:
        print(f"\n  Pages added ({len(pages_added)}):")
        for p in pages_added[:30]:
            print(f"    + {p}")
        if len(pages_added) > 30:
            print(f"    ... ({len(pages_added) - 30} more)")

    if pages_removed:
        print(f"\n  Pages removed ({len(pages_removed)}):")
        for p in pages_removed[:30]:
            print(f"    - {p}")
        if len(pages_removed) > 30:
            print(f"    ... ({len(pages_removed) - 30} more)")

    if pages_modified:
        print(f"\n  Pages modified ({len(pages_modified)}):")
        for p in sorted(pages_modified):
            line_count = pages_modified[p].count("\n") + 1
            print(f"    ~ {p}  ({line_count} diff line(s))")
        print()
        for page in sorted(pages_modified):
            print(f"  --- diff: {page} ---")
            for line in pages_modified[page].splitlines():
                print(f"    {line}")
            print()

    if warnings_added:
        print(f"  Warnings added ({len(warnings_added)}):")
        for w in warnings_added[:20]:
            print(f"    + {w}")
        if len(warnings_added) > 20:
            print(f"    ... ({len(warnings_added) - 20} more)")
        print()

    if warnings_removed:
        print(f"  Warnings removed ({len(warnings_removed)}):")
        for w in warnings_removed[:20]:
            print(f"    - {w}")
        if len(warnings_removed) > 20:
            print(f"    ... ({len(warnings_removed) - 20} more)")
        print()

    nothing_changed = not any(
        [pages_added, pages_removed, pages_modified, warnings_added, warnings_removed]
    )
    if nothing_changed and not candidate.failed:
        print("\n  No differences detected.")

    # Only advertise the scratch checkout when there's actually something
    # to inspect. macOS prunes /tmp eventually either way.
    if not nothing_changed or candidate.failed:
        print(
            f"\n  Baseline checkout (for further inspection): "
            f"{comparison.baseline_scratch}"
        )


def main():
    """Main CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage IATI documentation repositories.\n\n"
        "Verbs:\n"
        "  * 'list' and 'check' are read-only.\n"
        "  * 'sync' commits, pushes, and opens a pull request against each\n"
        "    repo's default branch. Use 'check' first to preview the diffs.\n"
        "  * 'run-script' publishes automatically if the script produces\n"
        "    filesystem changes; otherwise the run is informational. A\n"
        "    non-zero exit in any repo aborts the run with no changes\n"
        "    published.\n"
        "  * 'checkout-all' + 'make-prs' is the manual-edit flow: clone\n"
        "    every repo to a persistent dir, edit files by hand, then\n"
        "    publish the lot as PRs in one go. Use this for cross-repo\n"
        "    refactors that don't fit a template-sync or single script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List repos command
    subparsers.add_parser("list", help="List all tagged documentation repos")

    subparsers.add_parser(
        "list-prs",
        help=(
            "List open PRs across every Documentation-tagged repo, with "
            "mergeability, CI rollup, and review status. Includes PRs from "
            "anyone - useful for spotting in-flight work before stomping it."
        ),
    )

    subparsers.add_parser(
        "check-protection",
        help=(
            "Show branch protection state for the default branch of every "
            "Documentation-tagged repo. Read-only; never modifies protection."
        ),
    )

    # Check command
    check_parser = subparsers.add_parser("check", help="Check repos against template")
    check_parser.add_argument(
        "--files",
        nargs="+",
        default=FILES_TO_CHECK,
        help="Files to check",
    )
    check_parser.add_argument(
        "--no-diff",
        action="store_true",
        help="Hide diff output for mismatched files",
    )

    # Sync command
    sync_parser = subparsers.add_parser(
        "sync",
        help="Sync template files to repos and open PRs",
        description="For each Documentation-tagged repo: copy the specified files from\n"
        "iati-docs-base, commit them on a fresh working branch, push the\n"
        "branch, and open a pull request against the repo's default branch.\n"
        "Repos where every tracked file already matches the template are\n"
        "skipped without producing a PR.\n\n"
        "Use 'check' first if you want to preview the per-file diffs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync_parser.add_argument(
        "--message",
        "-m",
        required=True,
        help="Commit message (also used as PR title unless --pr-body overrides)",
    )
    sync_parser.add_argument(
        "--files",
        nargs="+",
        default=FILES_TO_CHECK,
        help="Files to sync from the template",
    )
    sync_parser.add_argument(
        "--branch-name",
        help="Override the auto-generated working branch name "
        "(default: iati-docs-management/sync-<timestamp>)",
    )
    sync_parser.add_argument(
        "--pr-body",
        help="Body for the pull requests (default: auto-generated)",
    )
    sync_parser.add_argument(
        "--push-delay",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="Seconds to wait between pushes, to spread downstream webhook\n"
        "fan-out (e.g. ReadTheDocs build queue) when publishing many PRs\n"
        "at once. Default: 20. Pass 0 to disable.",
    )

    # Sync-gitignore command
    sync_gitignore_parser = subparsers.add_parser(
        "sync-gitignore",
        help="Additively sync baseline .gitignore entries and open PRs",
        description="For each Documentation-tagged repo: ensure .gitignore carries the\n"
        "baseline entries (GITIGNORE_BASELINE), appending any that are missing\n"
        "without touching the repo's existing entries, then commit, push, and\n"
        "open a PR. Repos that already have every baseline entry are skipped\n"
        "without producing a PR.\n\n"
        ".gitignore is synced additively rather than verbatim because\n"
        "downstream repos legitimately carry their own extra ignores.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync_gitignore_parser.add_argument(
        "--message",
        "-m",
        required=True,
        help="Commit message (also used as PR title unless --pr-body overrides)",
    )
    sync_gitignore_parser.add_argument(
        "--branch-name",
        help="Override the auto-generated working branch name",
    )
    sync_gitignore_parser.add_argument(
        "--pr-body",
        help="Body for the pull requests (default: auto-generated)",
    )
    sync_gitignore_parser.add_argument(
        "--push-delay",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="Seconds to wait between pushes, to spread downstream webhook\n"
        "fan-out (e.g. ReadTheDocs build queue) when publishing many PRs\n"
        "at once. Default: 20. Pass 0 to disable.",
    )

    # Run-script command
    run_script_parser = subparsers.add_parser(
        "run-script",
        help="Run a script in each repo; PR any changes it produces",
        description="Run a script in each Documentation-tagged repo's checkout and\n"
        "collect its stdout, stderr, exit code, and any filesystem changes.\n\n"
        "Behaviour:\n"
        "  * If the script makes no filesystem changes anywhere, the run\n"
        "    is purely informational - nothing is committed or pushed.\n"
        "  * If the script makes changes in any repo, those changes are\n"
        "    committed on a fresh working branch and opened as PRs against\n"
        "    each repo's default branch. -m must be provided in this case.\n"
        "  * If the script exits non-zero in any repo (or times out), the\n"
        "    whole estate-wide run aborts and nothing is published.\n\n"
        "Branch names default to iati-docs-management/script-<stem>-<ts>,\n"
        "distinct from sync's branch naming.\n\n"
        "Script contract:\n"
        "  * Invoked as: <script> <repo-name>\n"
        "  * cwd is set to the repo's checkout\n"
        "  * Must be executable (chmod +x) and have a shebang\n"
        "  * Must exit 0 to indicate success; non-zero aborts the run\n"
        "  * Per-repo timeout: 5 minutes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_script_parser.add_argument(
        "script",
        help="Path to the script to run in each repo (must be executable)",
    )
    run_script_parser.add_argument(
        "--include-template",
        action="store_true",
        help="Also run on the template repo (iati-docs-base)",
    )
    run_script_parser.add_argument(
        "--message",
        "-m",
        help="Commit message and PR title - required if the script produces changes",
    )
    run_script_parser.add_argument(
        "--branch-name",
        help="Override the auto-generated working branch name",
    )
    run_script_parser.add_argument(
        "--pr-body",
        help="Body for the pull requests (default: auto-generated)",
    )
    run_script_parser.add_argument(
        "--push-delay",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="Seconds to wait between pushes, to spread downstream webhook\n"
        "fan-out (e.g. ReadTheDocs build queue) when publishing many PRs\n"
        "at once. Default: 20. Pass 0 to disable.",
    )

    # Checkout-all command
    checkout_parser = subparsers.add_parser(
        "checkout-all",
        help="Clone every tagged repo to a persistent dir for manual edits",
        description="Clone every Documentation-tagged repo (and the template) to a\n"
        "fresh directory under /tmp, create a working branch in each, and\n"
        "exit. The directory is NOT cleaned up - that's the whole point.\n\n"
        "Use this when the work doesn't fit a template-sync or a single\n"
        "scripted change: edit files by hand across the checkouts, then\n"
        "run 'make-prs' to commit and PR everything in one go.\n\n"
        "macOS prunes /tmp via the periodic daily job (files older than 3\n"
        "days), so anything left behind is reclaimed by the OS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    checkout_parser.add_argument(
        "--branch-name",
        help="Override the auto-generated working branch name "
        "(default: iati-docs-management/refactor-<timestamp>)",
    )
    checkout_parser.add_argument(
        "--no-template",
        action="store_true",
        help="Skip cloning the template repo (iati-docs-base)",
    )

    # Make-prs command
    make_prs_parser = subparsers.add_parser(
        "make-prs",
        help="Commit and PR changes from a checkout-all directory",
        description="For each repo checkout in --dir: stage uncommitted changes,\n"
        "commit with -m MSG, push the working branch, and open a pull\n"
        "request against the repo's default branch. Repos with nothing to\n"
        "publish (clean tree and no commits ahead) are skipped.\n\n"
        "Pair with 'checkout-all'.\n\n"
        "The working branch is detected from the first checkout unless\n"
        "--branch-name is given. All checkouts must be on the same branch;\n"
        "any repo on a different branch will abort with an error.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    make_prs_parser.add_argument(
        "--dir",
        required=True,
        help="Directory containing the repo checkouts (from checkout-all)",
    )
    make_prs_parser.add_argument(
        "--message",
        "-m",
        required=True,
        help="Commit message (also used as PR title unless --pr-body overrides)",
    )
    make_prs_parser.add_argument(
        "--branch-name",
        help="Working branch name (default: detected from first checkout)",
    )
    make_prs_parser.add_argument(
        "--pr-body",
        help="Body for the pull requests (default: auto-generated)",
    )
    make_prs_parser.add_argument(
        "--push-delay",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="Seconds to wait between pushes, to spread downstream webhook\n"
        "fan-out (e.g. ReadTheDocs build queue) when publishing many PRs\n"
        "at once. Default: 20. Pass 0 to disable.",
    )

    # Build command
    build_parser = subparsers.add_parser(
        "build",
        help="Build one repo's docs in a fresh venv inside a fresh clone",
        description="Clone the repo at its default branch, create a fresh venv,\n"
        "install requirements.txt, and run sphinx-build. Reports the\n"
        "build exit code, any Sphinx warnings, and the list of HTML\n"
        "pages produced. The checkout is removed when done.\n\n"
        "Use this to confirm the upstream main of a single repo builds\n"
        "cleanly on your machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    build_parser.add_argument(
        "repo",
        help="Name of the repo to build (e.g. iati-publisher-docs)",
    )

    # Build-all command
    build_all_parser = subparsers.add_parser(
        "build-all",
        help="Build every Documentation-tagged repo on main; report a health snapshot",
        description="Clone every Documentation-tagged repo at its default branch,\n"
        "build each in a fresh venv, and print a one-line summary per\n"
        "repo (exit code, warning count, page count). Use this to take\n"
        "a snapshot of estate-wide build health before/after a template\n"
        "change.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Build-compare command
    build_compare_parser = subparsers.add_parser(
        "build-compare",
        help="Build a candidate checkout and a baseline ref; report what differs",
        description="Build the candidate (a local checkout, including any uncommitted\n"
        "edits) and a fresh clone of the same repo at --baseline-ref, then\n"
        "report what differs between the two builds:\n"
        "  * pages added / removed / modified (with per-page diffs)\n"
        "  * Sphinx warnings added / removed\n\n"
        "The report describes the change surface area. It does not adjudicate\n"
        "whether each difference is intended or problematic - that's the\n"
        "operator's call when rolling out an estate-wide change.\n\n"
        "The candidate's working tree is never modified. Exit code is 1 only\n"
        "when the candidate failed to build while the baseline succeeded;\n"
        "otherwise 0, regardless of how much content changed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    build_compare_parser.add_argument(
        "--dir",
        required=True,
        help="Path to a local checkout to use as the candidate",
    )
    build_compare_parser.add_argument(
        "--baseline-ref",
        default="main",
        help="Git ref of the upstream repo to compare against (default: main)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    # Every command in this tool depends on gh - either to enumerate the
    # Documentation-tagged estate or to push branches and open PRs. Fail
    # fast with a clear message if it isn't installed or authenticated,
    # rather than letting the failure surface mid-run as a noisy
    # CalledProcessError from a subprocess deep in the call stack.
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

    if args.command == "list":
        repos = get_tagged_repos()
        print("Documentation repositories:")
        for repo in repos:
            marker = " (template)" if repo == TEMPLATE_REPO else ""
            print(f"  - {repo}{marker}")
        return

    if args.command == "list-prs":
        prs = list_open_prs()
        print_open_prs(prs)
        return

    if args.command == "check-protection":
        protections = {name: get_branch_protection(name) for name in get_tagged_repos()}
        print_protection(protections)
        return

    if args.command == "checkout-all":
        branch_name = args.branch_name or generate_branch_name(prefix="refactor")
        # Persistent: do NOT use the context manager - we want the
        # checkouts to outlive this invocation so the user can edit them
        # by hand and pass --dir to make-prs later.
        manager = RepoManager(branch_name=branch_name)
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
        return

    if args.command == "build":
        with RepoManager() as manager:
            print(f"Working directory: {manager.work_dir}")
            result = build_repo(manager.checkout_repo(args.repo))
            print_build_result(result)
            return 1 if result.failed else 0

    if args.command == "build-all":
        with RepoManager() as manager:
            print(f"Working directory: {manager.work_dir}")
            print("Checking out repositories...")
            manager.checkout_all()
            results = manager.build_all_repos()
            print_build_all_results(results)
            failures = [r for r in results.values() if r.failed]
            return 1 if failures else 0

    if args.command == "build-compare":
        candidate_path = Path(args.dir).resolve()
        if not candidate_path.is_dir() or not (candidate_path / ".git").is_dir():
            parser.error(f"--dir {candidate_path} is not a git checkout")

        # Infer the repo name from the origin remote so we know what to
        # clone for the baseline. Falls back to the directory name if the
        # remote can't be read.
        origin: str | None = None
        try:
            origin = git("remote", "get-url", "origin", cwd=candidate_path)
            # Match …/<repo>.git or …/<repo>
            repo_name = origin.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        except subprocess.CalledProcessError:
            repo_name = candidate_path.name

        # Cross-check against the tagged estate. The baseline is always
        # cloned from IATI/<repo>, so if the name isn't in the tagged set
        # the clone will 404. Failing fast here gives a clearer message
        # and surfaces the "repo exists but isn't tagged Documentation=true"
        # case explicitly.
        tagged = get_tagged_repos()
        if repo_name not in tagged:
            source = origin if origin else f"directory name {candidate_path.name!r}"
            parser.error(
                f"Repo {repo_name!r} (inferred from {source}) is not in the "
                f"Documentation-tagged estate. Either:\n"
                f"  * --dir points at a checkout that isn't an IATI docs repo, or\n"
                f"  * the upstream repo exists but isn't tagged Documentation=true "
                f"(use `list` to see what is).\n"
                f"Baselines are always cloned from IATI/<repo> on github.com."
            )

        candidate = RepoCheckout(name=repo_name, path=candidate_path)
        try:
            comparison = build_compare(candidate, baseline_ref=args.baseline_ref)
        except ValueError as exc:
            parser.error(str(exc))
        print_build_compare(comparison)
        # Exit code is mechanical: only flag failure when the candidate
        # itself failed to build while the baseline succeeded. Content
        # differences are reported, not adjudicated - the operator judges
        # whether each diff matches their intent.
        candidate_failed = comparison.candidate.failed
        baseline_failed = comparison.baseline.failed
        return 1 if candidate_failed and not baseline_failed else 0

    if args.command == "make-prs":
        work_dir = Path(args.dir)
        if not work_dir.is_dir():
            parser.error(f"--dir {work_dir} is not a directory")

        checkouts = sorted(
            p
            for p in work_dir.iterdir()
            if p.is_dir() and (p / ".git").exists() and p.name != TEMPLATE_REPO
        )
        if not checkouts:
            print(f"No repository checkouts found in {work_dir}")
            return

        branch_name = args.branch_name
        if branch_name is None:
            detected = current_branch(checkouts[0])
            if not detected:
                parser.error(
                    f"Could not detect branch from {checkouts[0]} "
                    "(detached HEAD?). Pass --branch-name."
                )
            branch_name = detected

        # No context manager: the checkouts must survive for re-runs.
        manager = RepoManager(
            branch_name, work_dir, [RepoCheckout(p.name, p) for p in checkouts]
        )

        print(f"Working directory: {manager.work_dir}")
        print(f"Working branch: {manager.branch_name}")
        print(f"Repos to process: {len(manager.repos)}")
        push_results = manager.update_all_to_github(
            args.message,
            pr_body=args.pr_body,
            push_delay=args.push_delay,
        )
        print_publish_results(push_results)
        return

    # Pick a branch name. run-script uses a script-specific prefix so its
    # branches are distinguishable from sync's at a glance.
    branch_name = getattr(args, "branch_name", None)
    if branch_name is None and args.command == "run-script":
        script_stem = Path(args.script).stem
        branch_name = generate_branch_name(prefix=f"script-{script_stem}")
    if branch_name is None and args.command == "sync-gitignore":
        branch_name = generate_branch_name(prefix="gitignore")
    if branch_name is None:
        branch_name = generate_branch_name()

    with RepoManager(branch_name=branch_name) as manager:
        print(f"Working directory: {manager.work_dir}")
        print(f"Working branch: {manager.branch_name}")
        print("Checking out repositories...")
        manager.checkout_all()
        print(f"Checked out {len(manager.repos)} repos (+ template)")

        if args.command == "check":
            results = {
                repo.name: [check_file(manager.template, repo, f) for f in args.files]
                for repo in manager.repos
            }
            print_file_results(
                results, "CHECK RESULTS", _CHECK_NOTES, show_diff=not args.no_diff
            )

            extras = {
                repo.name: extra_top_level_paths(manager.template, repo)
                for repo in manager.repos
            }
            print_extra_paths(extras)

            # Summary
            total_files = sum(len(r) for r in results.values())
            matching = sum(
                check.state is FileState.MATCH
                for checks in results.values()
                for check in checks
            )
            print(f"\nSummary: {matching}/{total_files} files match template")

        elif args.command == "sync":
            copy_results = {
                repo.name: [sync_file(manager.template, repo, f) for f in args.files]
                for repo in manager.repos
            }
            print_file_results(copy_results, "FILE COPY", _SYNC_NOTES)

            push_results = manager.update_all_to_github(
                args.message,
                pr_body=args.pr_body,
                push_delay=args.push_delay,
            )
            print_publish_results(push_results)

        elif args.command == "sync-gitignore":
            sync_results = {
                repo.name: [sync_gitignore_entries(repo)] for repo in manager.repos
            }
            print_file_results(sync_results, "GITIGNORE SYNC", _SYNC_NOTES)

            push_results = manager.update_all_to_github(
                args.message,
                pr_body=args.pr_body,
                push_delay=args.push_delay,
            )
            print_publish_results(push_results)

        elif args.command == "run-script":
            results = manager.run_script_on_all_repos(
                args.script,
                include_template=args.include_template,
            )
            print_run_script_results(results)

            failed = next((r for r in results.values() if r.failure), None)
            if failed:
                print(
                    f"\nERROR: Script failed in {failed.repo} "
                    f"({failed.failure}). "
                    "Aborting; no changes published."
                )
                return 1

            repos_with_changes = [r for r in results.values() if r.changed_files]
            if not repos_with_changes:
                print("\nNo file changes; nothing to commit or push.")
                return

            if not args.message:
                parser.error(
                    f"Script produced changes in {len(repos_with_changes)} "
                    "repo(s); pass -m to commit and open PRs."
                )

            push_results = manager.update_all_to_github(
                args.message,
                pr_body=args.pr_body,
                push_delay=args.push_delay,
            )
            print_publish_results(push_results)


if __name__ == "__main__":
    raise SystemExit(main())
