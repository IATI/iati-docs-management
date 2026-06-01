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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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

# Files that should be tracked across all repos. The pip-compile inputs
# (.in) are the canonical sources; the generated requirements.txt files
# are also tracked (see NOISY_DIFFS) so that template regenerations can
# be propagated, but their diffs are not flagged because they drift
# naturally as developers regenerate them locally.
FILES_TO_CHECK = [
    ".readthedocs.yaml",
    "requirements.in",
    "requirements_dev.in",
    "requirements.txt",
    ".github/workflows/ci.yml",
    ".vscode/launch.json",
]

# Files where a difference from the template is expected and not flagged
# in check output. The diff itself is suppressed; the result is shown as
# "DIFFERS (expected)" so the reader knows the file isn't identical but
# we knowingly chose not to treat that as a problem.
NOISY_DIFFS = {"requirements.txt"}


@dataclass
class RepoCheckout:
    """Represents a checked-out repository."""

    name: str
    path: Path
    is_template: bool = False


class RepoManager:
    """Manages IATI documentation repositories."""

    def __init__(
        self,
        work_dir: Path | None = None,
        branch_name: str | None = None,
    ):
        """
        Initialize the repo manager.

        Args:
            work_dir: Working directory for checkouts. If None, a session
                directory is created under /tmp. macOS prunes /tmp via the
                periodic daily job (files older than 3 days), so anything
                we fail to clean up is eventually reclaimed by the OS.
            branch_name: Working branch created in every checkout. All
                commits and pushes target this branch; the repo's default
                branch is never modified. Defaults to a timestamped name
                so multiple invocations on the same day don't collide.
        """
        if work_dir is None:
            self.work_dir = Path(tempfile.mkdtemp(prefix="iati-docs-", dir="/tmp"))
            self._owns_work_dir = True
        else:
            self.work_dir = work_dir
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self._owns_work_dir = False

        self.branch_name = branch_name or self.generate_branch_name()
        self.repos: list[RepoCheckout] = []
        self.template: RepoCheckout | None = None

    @staticmethod
    def generate_branch_name(prefix: str = "sync") -> str:
        """Build a timestamped working branch name with the given prefix."""
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"iati-docs-management/{prefix}-{ts}"

    @staticmethod
    def get_tagged_repos() -> list[str]:
        """
        Fetch all repos tagged with Documentation=true from GitHub.

        Returns:
            List of repository names.
        """
        result = subprocess.run(
            [
                "gh",
                "api",
                f"/orgs/{GITHUB_ORG}/properties/values",
                "--paginate",
                "--jq",
                '.[] | select(.properties[] | select(.property_name == "Documentation" and .value == "true")) | .repository_name',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        repos = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]
        return repos

    @staticmethod
    def get_branch_protection(repo_name: str, branch: str = "main") -> dict | None:
        """
        Fetch branch protection settings for ``branch`` in ``repo_name``.

        Returns the GitHub API response dict if the branch is protected,
        or ``None`` if it isn't (HTTP 404 "Branch not protected").
        """
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{GITHUB_ORG}/{repo_name}/branches/{branch}/protection",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            if "Branch not protected" in proc.stderr or "Not Found" in proc.stderr:
                return None
            raise RuntimeError(
                f"gh api failed for {repo_name}/{branch} protection: {proc.stderr.strip()}"
            )
        return json.loads(proc.stdout)

    @staticmethod
    def check_protection_all_repos(
        repo_names: list[str] | None = None, branch: str = "main"
    ) -> dict[str, dict | None]:
        """
        Fetch branch protection for ``branch`` on every tagged repo.

        Returns a dict mapping repo name to the protection dict (or
        ``None`` if the branch isn't protected).
        """
        if repo_names is None:
            repo_names = RepoManager.get_tagged_repos()
        return {
            name: RepoManager.get_branch_protection(name, branch) for name in repo_names
        }

    def checkout_repo(self, repo_name: str) -> RepoCheckout:
        """
        Clone a single repository.

        Args:
            repo_name: Name of the repository to clone.

        Returns:
            RepoCheckout object for the cloned repo.
        """
        repo_path = self.work_dir / repo_name
        clone_url = f"git@github.com:{GITHUB_ORG}/{repo_name}.git"

        print(f"Cloning {repo_name}...")
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(repo_path)],
            check=True,
            capture_output=True,
        )

        # Switch to the working branch immediately so any subsequent commit
        # or push lands there, never on the repo's default branch.
        subprocess.run(
            ["git", "switch", "-c", self.branch_name],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        is_template = repo_name == TEMPLATE_REPO
        checkout = RepoCheckout(name=repo_name, path=repo_path, is_template=is_template)

        if is_template:
            self.template = checkout
        else:
            self.repos.append(checkout)

        return checkout

    def checkout_all(self, include_template: bool = True) -> list[RepoCheckout]:
        """
        Clone all tagged documentation repos.

        Args:
            include_template: Whether to also clone the template repo.
                Honoured even if the template is itself tagged with
                Documentation=true.

        Returns:
            List of all checked-out repos.
        """
        repo_names = self.get_tagged_repos()

        if include_template:
            if TEMPLATE_REPO not in repo_names:
                self.checkout_repo(TEMPLATE_REPO)
        else:
            repo_names = [r for r in repo_names if r != TEMPLATE_REPO]

        for name in repo_names:
            self.checkout_repo(name)

        return [self.template, *self.repos] if self.template else self.repos

    # =========================================================================
    # CHECK FUNCTIONS
    # =========================================================================

    def check_file_matches_template(
        self, repo: RepoCheckout, relative_path: str
    ) -> dict:
        """
        Check if a file in a repo matches the template.

        Args:
            repo: The repo to check.
            relative_path: Path to the file relative to repo root.

        Returns:
            Dict with check results.
        """
        if self.template is None:
            raise ValueError("Template repo not checked out")

        template_file = self.template.path / relative_path
        repo_file = repo.path / relative_path

        result = {
            "repo": repo.name,
            "file": relative_path,
            "template_exists": template_file.exists(),
            "repo_exists": repo_file.exists(),
            "matches": False,
            "diff": None,
        }

        if not template_file.exists():
            result["error"] = "Template file does not exist"
            return result

        if not repo_file.exists():
            result["error"] = "File missing from repo"
            return result

        template_content = template_file.read_text()
        repo_content = repo_file.read_text()

        if template_content == repo_content:
            result["matches"] = True
        else:
            # Get diff showing what would change in the repo (repo -> template)
            diff_result = subprocess.run(
                ["diff", "-u", str(repo_file), str(template_file)],
                capture_output=True,
                text=True,
            )
            result["diff"] = diff_result.stdout

        return result

    def check_all_repos(self, files: list[str] | None = None) -> dict[str, list[dict]]:
        """
        Check all repos against the template.

        Args:
            files: List of files to check. Defaults to FILES_TO_CHECK.

        Returns:
            Dict mapping repo names to list of check results.
        """
        if files is None:
            files = FILES_TO_CHECK

        if self.template is None:
            raise ValueError("Template repo not checked out")

        results = {}
        for repo in self.repos:
            repo_results = []
            for file_path in files:
                check_result = self.check_file_matches_template(repo, file_path)
                repo_results.append(check_result)
            results[repo.name] = repo_results

        return results

    def extra_top_level_paths(self, repo: RepoCheckout) -> list[str]:
        """
        Top-level entries (files and directories) in the repo that don't
        exist at the same path in the template. ``.git`` is excluded.

        Reported for information only. The tooling never modifies these -
        they're a hint that a downstream repo has something the template
        doesn't, which may be deliberate (e.g. ``.yamllint.yaml`` for local
        linting, ``package.json`` for an auxiliary toolchain) or may be drift
        worth investigating. Use the human-decision verbs (``checkout-all``
        + ``make-prs``) if any of them turn out to need action.
        """
        if self.template is None:
            raise ValueError("Template repo not checked out")

        template_entries = {p.name for p in self.template.path.iterdir()}
        extras = []
        for entry in sorted(repo.path.iterdir(), key=lambda p: p.name):
            if entry.name == ".git":
                continue
            if entry.name in template_entries:
                continue
            extras.append(entry.name)
        return extras

    def extra_paths_all_repos(self) -> dict[str, list[str]]:
        """
        Run ``extra_top_level_paths`` for every checked-out repo.
        Returns a dict mapping repo name to its list of extras.
        """
        return {repo.name: self.extra_top_level_paths(repo) for repo in self.repos}

    def run_custom_check(
        self, check_func: Callable[[RepoCheckout, "RepoManager"], dict]
    ) -> dict[str, dict]:
        """
        Run a caller-supplied check function against every repo.

        Use this for checks that don't fit the file-by-file template
        comparison - e.g. parsing config files, validating workflow
        contents, or cross-referencing GitHub state.

        Args:
            check_func: Function taking ``(repo, manager)`` and returning a
                dict describing the check outcome.

        Returns:
            Dict mapping repo names to the dict returned by ``check_func``.
        """
        results = {}
        for repo in self.repos:
            results[repo.name] = check_func(repo, self)
        return results

    # =========================================================================
    # SYNC FUNCTIONS
    # =========================================================================

    def sync_file_from_template(
        self, repo: RepoCheckout, relative_path: str, dry_run: bool = True
    ) -> dict:
        """
        Copy a file from the template to a repo.

        Args:
            repo: Target repo.
            relative_path: Path to the file relative to repo root.
            dry_run: If True, only report what would be done.

        Returns:
            Dict with sync results.
        """
        if self.template is None:
            raise ValueError("Template repo not checked out")

        template_file = self.template.path / relative_path
        repo_file = repo.path / relative_path

        result = {
            "repo": repo.name,
            "file": relative_path,
            "action": None,
            "dry_run": dry_run,
        }

        if not template_file.exists():
            result["action"] = "skip"
            result["reason"] = "Template file does not exist"
            return result

        if not repo_file.exists():
            result["action"] = "create"
        else:
            template_content = template_file.read_text()
            repo_content = repo_file.read_text()
            if template_content == repo_content:
                result["action"] = "skip"
                result["reason"] = "Already matches template"
                return result
            result["action"] = "update"

        if not dry_run:
            repo_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template_file, repo_file)
            result["synced"] = True

        return result

    def sync_all_repos(
        self, files: list[str] | None = None, dry_run: bool = True
    ) -> dict[str, list[dict]]:
        """
        Sync specified files from template to all repos.

        Args:
            files: List of files to sync. Defaults to FILES_TO_CHECK.
            dry_run: If True, only report what would be done.

        Returns:
            Dict mapping repo names to list of sync results.
        """
        if files is None:
            files = FILES_TO_CHECK

        results = {}
        for repo in self.repos:
            repo_results = []
            for file_path in files:
                sync_result = self.sync_file_from_template(repo, file_path, dry_run)
                repo_results.append(sync_result)
            results[repo.name] = repo_results

        return results

    def run_custom_sync(
        self,
        sync_func: Callable[[RepoCheckout, "RepoManager"], dict],
    ) -> dict[str, dict]:
        """
        Run a caller-supplied sync function against every repo.

        Use this for syncs that don't fit copying a file from the template -
        e.g. editing a config in place, generating content, or updating
        only a subset of a file. The ``sync_func`` is responsible for its
        own dry-run logic; this method just dispatches.

        Args:
            sync_func: Function taking ``(repo, manager)`` and returning a
                dict describing the sync outcome.

        Returns:
            Dict mapping repo names to the dict returned by ``sync_func``.
        """
        results = {}
        for repo in self.repos:
            results[repo.name] = sync_func(repo, self)
        return results

    def run_script_on_all_repos(
        self,
        script_path: str | Path,
        include_template: bool = False,
    ) -> dict[str, dict]:
        """
        Run a script in each repo, aborting on first failure.

        The script is invoked as ``<script> <repo-name>`` with the working
        directory set to the repo's checkout. stdout, stderr, exit code,
        and the list of changed files (per ``git status --porcelain``) are
        captured and returned.

        Processing stops as soon as any repo fails - either a non-zero
        exit, a timeout, or an exception invoking the script. The failing
        repo's result will have ``failed=True`` and a ``failure_reason``
        string. Callers should treat any failure as a signal to abort the
        whole estate-wide operation rather than publish a partial run.

        This method does not commit or push anything - it only runs the
        script and reports. Callers decide what to do with the results.

        Args:
            script_path: Path to the script. Must be executable.
            include_template: If True, also run on the template repo.

        Returns:
            Dict mapping repo names to {exit, stdout, stderr, changed_files}
            or, on failure, with additional {failed, failure_reason}.
        """
        script_path = Path(script_path).resolve()

        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        results = {}
        repos_to_process = list(self.repos)
        if include_template and self.template:
            repos_to_process.append(self.template)

        for repo in repos_to_process:
            result = {"repo": repo.name, "script": str(script_path)}

            try:
                proc = subprocess.run(
                    [str(script_path), repo.name],
                    cwd=repo.path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                result["exit"] = proc.returncode
                result["stdout"] = proc.stdout
                result["stderr"] = proc.stderr

                status_proc = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=repo.path,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                result["changed_files"] = (
                    status_proc.stdout.strip().splitlines()
                    if status_proc.stdout.strip()
                    else []
                )

                if proc.returncode != 0:
                    result["failed"] = True
                    result["failure_reason"] = (
                        f"Script exited with code {proc.returncode}"
                    )
            except subprocess.TimeoutExpired:
                result["failed"] = True
                result["failure_reason"] = "Timed out after 300 seconds"
            except Exception as e:
                result["failed"] = True
                result["failure_reason"] = str(e)

            results[repo.name] = result

            if result.get("failed"):
                break

        return results

    # =========================================================================
    # UPDATE TO GITHUB FUNCTIONS
    # =========================================================================

    @staticmethod
    def _current_branch(repo_path: Path) -> str:
        """Return the currently checked-out branch (empty if detached HEAD)."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def default_branch(repo_path: Path) -> str:
        """Return the upstream default branch name (e.g. 'main')."""
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        # e.g. "origin/main" -> "main"
        return result.stdout.strip().removeprefix("origin/")

    def _ensure_safe_branch(self, repo: RepoCheckout) -> None:
        """
        Refuse to operate unless the working tree is on the expected
        working branch. Catches both the case where the checkout failed
        to switch off the default branch and the case where someone
        manually switched branches between checkout and publish.
        """
        current = self._current_branch(repo.path)
        default = self.default_branch(repo.path)
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

    def commit_changes(
        self, repo: RepoCheckout, message: str, dry_run: bool = True
    ) -> dict:
        """
        Commit any changes in a repo to the working branch.

        Args:
            repo: The repo to commit.
            message: Commit message.
            dry_run: If True, only report what would be done.

        Returns:
            Dict with commit results.
        """
        self._ensure_safe_branch(repo)

        result = {
            "repo": repo.name,
            "branch": self.branch_name,
            "action": None,
            "dry_run": dry_run,
        }

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo.path,
            capture_output=True,
            text=True,
            check=True,
        )

        if not status.stdout.strip():
            result["action"] = "skip"
            result["reason"] = "No changes to commit"
            return result

        result["action"] = "commit"
        result["changes"] = status.stdout.strip().split("\n")

        if not dry_run:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo.path,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=repo.path,
                check=True,
            )
            result["committed"] = True

        return result

    def push_changes(
        self,
        repo: RepoCheckout,
        pr_title: str,
        pr_body: str,
        dry_run: bool = True,
    ) -> dict:
        """
        Push the working branch to GitHub and open a pull request.

        Args:
            repo: The repo to push.
            pr_title: Title for the pull request.
            pr_body: Body for the pull request.
            dry_run: If True, only report what would be done.

        Returns:
            Dict with push results.
        """
        self._ensure_safe_branch(repo)

        default = self.default_branch(repo.path)
        result = {
            "repo": repo.name,
            "branch": self.branch_name,
            "base": default,
            "action": None,
            "dry_run": dry_run,
        }

        # Count commits ahead of the default branch. Using rev-list avoids
        # parsing git's English porcelain output and works correctly even
        # if upstream tracking isn't yet configured for the new branch.
        ahead_proc = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{default}..HEAD"],
            cwd=repo.path,
            capture_output=True,
            text=True,
            check=True,
        )
        commits_ahead = int(ahead_proc.stdout.strip())

        if commits_ahead == 0:
            result["action"] = "skip"
            result["reason"] = f"No commits ahead of origin/{default}"
            return result

        result["action"] = "push"
        result["commits_ahead"] = commits_ahead

        if not dry_run:
            subprocess.run(
                ["git", "push", "-u", "origin", self.branch_name],
                cwd=repo.path,
                check=True,
            )
            pr_proc = subprocess.run(
                [
                    "gh",
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
                ],
                cwd=repo.path,
                capture_output=True,
                text=True,
                check=True,
            )
            result["pushed"] = True
            result["pr_url"] = pr_proc.stdout.strip()

        return result

    @staticmethod
    def format_with_black(repo: RepoCheckout) -> dict:
        """
        Run black across a repo's tree, auto-fixing formatting.

        Reformatting changes are left in the working tree for a subsequent
        commit to pick up - same as if a contributor had run black before
        committing. Black's version is whichever one is installed for the
        Python running this script; that matches CI because every repo's
        ``requirements_dev.in`` pins the same black via template sync.

        Black exits non-zero only when it can't parse the source (i.e. a
        real syntax error). That's the one case where the caller should
        abort the publish rather than continue: silently auto-fixing
        formatting is fine, silently dropping unparseable code on the
        floor is not.

        Args:
            repo: The repo to format.

        Returns:
            Dict with:
                exit_code: black's exit code (0 = clean or reformatted OK)
                failed: True if black couldn't run or couldn't parse the
                  source
                failure_reason: human-readable explanation if failed
                changed_files: list of file paths black reformatted
                  (from black's stderr; relative to wherever black logged)
                stderr_tail: last ~20 lines of black stderr
        """
        result: dict = {"repo": repo.name, "failed": False, "changed_files": []}

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "black", str(repo.path)],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            result["failed"] = True
            result["failure_reason"] = (
                f"Python interpreter not found at {sys.executable!r}."
            )
            return result

        result["exit_code"] = proc.returncode
        result["stderr_tail"] = "\n".join(proc.stderr.splitlines()[-20:])

        # Detect missing black before treating the non-zero as a parse error.
        if "No module named" in proc.stderr and "black" in proc.stderr:
            result["failed"] = True
            result["failure_reason"] = (
                f"black is not installed for {sys.executable}. "
                "Install this repo's requirements_dev.txt and try again."
            )
            return result

        if proc.returncode != 0:
            result["failed"] = True
            result["failure_reason"] = (
                f"black exited {proc.returncode} - usually a syntax error "
                "in the source. See stderr_tail."
            )
            return result

        # Lines on stderr like "reformatted /path/to/file.py" tell us what
        # changed. The summary line "N files reformatted." also appears
        # but we don't need it.
        result["changed_files"] = [
            line.removeprefix("reformatted ").strip()
            for line in proc.stderr.splitlines()
            if line.startswith("reformatted ")
        ]
        return result

    def update_all_to_github(
        self,
        commit_message: str,
        pr_title: str | None = None,
        pr_body: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, dict]:
        """
        Commit changes and push the working branch in all repos, opening a
        pull request against the default branch.

        Before committing, runs black across each repo and auto-fixes any
        formatting. If black can't parse the source in any repo, the
        entire estate-wide publish is aborted - no PRs are opened. This
        prevents the common failure of opening PRs that immediately fail
        CI's ``black --check``.

        Args:
            commit_message: Message for commits.
            pr_title: PR title. Defaults to the commit message.
            pr_body: PR body. Defaults to a generated description.
            dry_run: If True, only report what would be done. Black is
                skipped on dry runs since it would modify files.

        Returns:
            Dict mapping repo names to update results.

        Raises:
            RuntimeError: If black fails (parse error or missing) in any
                repo. No commits or pushes happen.
        """
        if pr_title is None:
            pr_title = commit_message
        if pr_body is None:
            pr_body = (
                f"Automated update from iati-docs-management.\n\n"
                f"Branch: `{self.branch_name}`\n"
                f"Commit: {commit_message}"
            )

        # Format every repo with black before any commit. Run all repos
        # before checking failures so the operator gets a complete picture
        # of what's wrong rather than the first failure only.
        if not dry_run:
            format_results = {
                repo.name: self.format_with_black(repo) for repo in self.repos
            }
            failures = [r for r in format_results.values() if r["failed"]]
            if failures:
                for r in failures:
                    print(f"\n  {r['repo']}: {r['failure_reason']}")
                    for line in r.get("stderr_tail", "").splitlines():
                        print(f"    {line}")
                raise RuntimeError(
                    f"black failed in {len(failures)} repo(s); aborting publish. "
                    "No commits or PRs were created."
                )
            total_reformatted = sum(
                len(r["changed_files"]) for r in format_results.values()
            )
            if total_reformatted:
                print(f"\nblack reformatted {total_reformatted} file(s):")
                for name, r in format_results.items():
                    if r["changed_files"]:
                        print(f"  {name}: {len(r['changed_files'])} file(s)")

        results = {}
        for repo in self.repos:
            commit_result = self.commit_changes(repo, commit_message, dry_run)
            # Always attempt push; push_changes self-skips when there are no
            # commits ahead. This way pre-existing commits on the working
            # branch (e.g. from a manual commit before make-prs) still get
            # published.
            push_result = self.push_changes(repo, pr_title, pr_body, dry_run=dry_run)

            results[repo.name] = {
                "commit": commit_result,
                "push": push_result,
            }

        return results

    # =========================================================================
    # BUILD FUNCTIONS
    # =========================================================================

    @staticmethod
    def build_repo(repo: RepoCheckout, python_executable: str | None = None) -> dict:
        """
        Build a repo's Sphinx docs in a fresh venv in an external scratch dir.

        The venv and Sphinx output both live in a scratch directory under
        ``/tmp/iati-build-<repo>-<rand>/`` - never inside the repo. This
        keeps the candidate's working tree untouched even when the caller
        passes a local checkout (e.g. via ``build-compare --dir``).

        ``requirements.txt`` is installed from the repo (which pins the same
        Sphinx / theme versions ReadTheDocs uses). The build is invoked from
        the ``docs/`` directory so any conf.py imports of sibling modules
        (``from project_info import ...``) resolve. ``PYTHONDONTWRITEBYTECODE``
        is set on the build subprocess so importing conf.py doesn't leave
        ``__pycache__`` in the repo's ``docs/``.

        Scratch directories are NOT cleaned up. macOS prunes ``/tmp`` files
        older than ~3 days, which is plenty of time to inspect a failed
        build manually.

        Args:
            repo: A checked-out repo.
            python_executable: Python interpreter to bootstrap the venv from.
                Defaults to the interpreter running this script.

        Returns:
            Dict with structured build results:
                stage: "install" or "build" or "complete"
                failed: bool
                failure_reason: str (if failed)
                install_exit_code, install_stderr_tail
                build_exit_code, build_stderr_tail
                pages: sorted list of HTML pages produced
                  (paths relative to html_dir, empty if build failed)
                warnings: list of Sphinx warning lines from stderr
                scratch_dir: path to the build scratch dir
                html_dir: path to the directory containing the built HTML
                  (only set on a successful build)
        """
        if python_executable is None:
            python_executable = sys.executable

        result = {
            "repo": repo.name,
            "stage": "install",
            "failed": False,
            "pages": [],
            "warnings": [],
        }

        docs_dir = repo.path / "docs"
        if not docs_dir.is_dir():
            result["failed"] = True
            result["failure_reason"] = f"No docs/ directory in {repo.name}"
            return result

        requirements = repo.path / "requirements.txt"
        if not requirements.is_file():
            result["failed"] = True
            result["failure_reason"] = f"No requirements.txt in {repo.name}"
            return result

        scratch_dir = Path(
            tempfile.mkdtemp(prefix=f"iati-build-{repo.name}-", dir="/tmp")
        )
        result["scratch_dir"] = str(scratch_dir)
        venv_path = scratch_dir / "venv"
        html_dir = scratch_dir / "html"

        venv_proc = subprocess.run(
            [python_executable, "-m", "venv", str(venv_path)],
            capture_output=True,
            text=True,
        )
        if venv_proc.returncode != 0:
            result["failed"] = True
            result["failure_reason"] = "venv creation failed"
            result["install_stderr_tail"] = "\n".join(
                venv_proc.stderr.splitlines()[-30:]
            )
            return result

        pip_path = venv_path / "bin" / "pip"
        py_path = venv_path / "bin" / "python"

        install_proc = subprocess.run(
            [str(pip_path), "install", "-q", "-r", str(requirements)],
            capture_output=True,
            text=True,
        )
        result["install_exit_code"] = install_proc.returncode
        result["install_stderr_tail"] = "\n".join(
            install_proc.stderr.splitlines()[-30:]
        )
        if install_proc.returncode != 0:
            result["failed"] = True
            result["failure_reason"] = "pip install failed"
            return result

        result["stage"] = "build"
        # PYTHONDONTWRITEBYTECODE keeps Python from leaving __pycache__/
        # in the repo's docs/ when Sphinx imports conf.py and project_info.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        build_proc = subprocess.run(
            [
                str(py_path),
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
        result["build_exit_code"] = build_proc.returncode
        result["build_stderr_tail"] = "\n".join(build_proc.stderr.splitlines()[-30:])
        # Sphinx writes warnings to stderr in the form
        # "/path/file.rst:N: WARNING: ..." - keep just those lines, and
        # strip the absolute repo path so warnings produced from two
        # different checkouts of the same code compare equal.
        repo_prefix = str(repo.path.resolve()) + "/"
        result["warnings"] = [
            line.replace(repo_prefix, "")
            for line in build_proc.stderr.splitlines()
            if "WARNING:" in line or "ERROR:" in line
        ]

        if build_proc.returncode != 0:
            result["failed"] = True
            result["failure_reason"] = "sphinx build failed"
            return result

        if html_dir.is_dir():
            result["pages"] = sorted(
                str(p.relative_to(html_dir)) for p in html_dir.rglob("*.html")
            )
            result["html_dir"] = str(html_dir)

        result["stage"] = "complete"
        return result

    def build_all_repos(self, include_template: bool = True) -> dict[str, dict]:
        """
        Build every checked-out repo at its default branch.

        Precondition: ``checkout_all`` (or repeated ``checkout_repo``) must
        have been called first. This method reads from ``self.repos`` and
        ``self.template``; it does not clone anything itself.

        Each repo is switched to its default branch before building so the
        measured state reflects the upstream head rather than the working
        branch ``checkout_all`` creates. The working branch is not
        restored - the checkouts are intended to be thrown away after
        ``cleanup()``.

        Args:
            include_template: Whether to also build the template repo if
                one is checked out. Defaults to True - an estate-wide
                health snapshot normally wants to confirm the template
                itself builds.

        Returns:
            Dict mapping repo names to ``build_repo`` result dicts. Order
            puts the template (if included) first, then ``self.repos`` in
            the order they were checked out.

        Raises:
            ValueError: If no repos have been checked out yet.
        """
        if not self.repos and self.template is None:
            raise ValueError(
                "No repos checked out. Call checkout_all() before " "build_all_repos()."
            )

        repos_to_build = list(self.repos)
        if include_template and self.template is not None:
            repos_to_build.insert(0, self.template)

        results: dict[str, dict] = {}
        for repo in repos_to_build:
            default = self.default_branch(repo.path)
            subprocess.run(
                ["git", "checkout", default],
                cwd=repo.path,
                check=True,
                capture_output=True,
            )
            print(f"Building {repo.name}...")
            results[repo.name] = self.build_repo(repo)
        return results

    @staticmethod
    def _normalise_html(content: str) -> str:
        """Strip volatile patterns so two builds of the same code compare equal."""
        for pattern, replacement in _HTML_NORMALISERS:
            content = pattern.sub(replacement, content)
        return content

    @staticmethod
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
        baseline_norm = RepoManager._normalise_html(baseline_html).splitlines()
        candidate_norm = RepoManager._normalise_html(candidate_html).splitlines()
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

    @staticmethod
    def build_compare(
        candidate_repo: RepoCheckout,
        baseline_ref: str = "main",
        python_executable: str | None = None,
    ) -> dict:
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
            python_executable: Python interpreter for venvs.

        Returns:
            Dict with:
                baseline, candidate: per-side build results
                baseline_ref, candidate_label
                pages_added: pages in candidate not present in baseline
                pages_removed: pages in baseline not present in candidate
                pages_modified: {page_name: unified diff string} for pages
                    on both sides whose content differs post-normalisation
                warnings_added: warning lines new in candidate
                warnings_removed: warning lines gone from candidate
                baseline_scratch: where the baseline clone lives, for manual
                    deeper inspection
        """
        clone_url = f"git@github.com:{GITHUB_ORG}/{candidate_repo.name}.git"

        # Validate the baseline ref before doing any build work. ``git clone
        # --depth 1 --branch`` only accepts branch and tag refs, not commit
        # SHAs - and our error message is clearer than git's "Remote branch
        # <ref> not found in upstream origin". Doing this up front avoids
        # ~30s of wasted candidate build on a typo.
        ls_remote = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", clone_url, baseline_ref],
            capture_output=True,
            text=True,
            check=True,
        )
        if not ls_remote.stdout.strip():
            raise ValueError(
                f"Baseline ref {baseline_ref!r} is not a branch or tag in "
                f"{candidate_repo.name}. --baseline-ref accepts branch and "
                f"tag names only; commit SHAs are not supported."
            )

        print(f"Building candidate ({candidate_repo.name})...")
        candidate_result = RepoManager.build_repo(candidate_repo, python_executable)

        baseline_scratch = Path(
            tempfile.mkdtemp(prefix=f"iati-baseline-{candidate_repo.name}-", dir="/tmp")
        )
        baseline_path = baseline_scratch / candidate_repo.name
        print(f"Cloning baseline ({baseline_ref})...")
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                baseline_ref,
                clone_url,
                str(baseline_path),
            ],
            check=True,
            capture_output=True,
        )
        baseline_repo = RepoCheckout(name=candidate_repo.name, path=baseline_path)
        print(f"Building baseline ({baseline_ref})...")
        baseline_result = RepoManager.build_repo(baseline_repo, python_executable)

        candidate_label = (
            RepoManager._current_branch(candidate_repo.path) or "candidate"
        )

        baseline_pages = set(baseline_result.get("pages", []))
        candidate_pages = set(candidate_result.get("pages", []))
        pages_added = sorted(candidate_pages - baseline_pages)
        pages_removed = sorted(baseline_pages - candidate_pages)

        # Both sides only have html_dir set if the build actually succeeded.
        # If either failed, skip the per-page diff (the warnings/pages_added
        # sections still carry signal).
        baseline_html_dir = (
            Path(baseline_result["html_dir"])
            if baseline_result.get("html_dir")
            else None
        )
        candidate_html_dir = (
            Path(candidate_result["html_dir"])
            if candidate_result.get("html_dir")
            else None
        )
        pages_modified: dict[str, str] = {}
        for page in sorted(baseline_pages & candidate_pages):
            if baseline_html_dir is None or candidate_html_dir is None:
                break
            baseline_file = baseline_html_dir / page
            candidate_file = candidate_html_dir / page
            if not baseline_file.is_file() or not candidate_file.is_file():
                continue
            try:
                baseline_html = baseline_file.read_text(errors="replace")
                candidate_html = candidate_file.read_text(errors="replace")
            except OSError:
                continue
            diff = RepoManager._page_diff(baseline_html, candidate_html, page)
            if diff is not None:
                pages_modified[page] = diff

        baseline_warnings_set = set(baseline_result.get("warnings", []))
        candidate_warnings_set = set(candidate_result.get("warnings", []))
        warnings_added = [
            w
            for w in candidate_result.get("warnings", [])
            if w not in baseline_warnings_set
        ]
        warnings_removed = [
            w
            for w in baseline_result.get("warnings", [])
            if w not in candidate_warnings_set
        ]

        return {
            "repo": candidate_repo.name,
            "baseline_ref": baseline_ref,
            "candidate_label": candidate_label,
            "baseline": baseline_result,
            "candidate": candidate_result,
            "pages_added": pages_added,
            "pages_removed": pages_removed,
            "pages_modified": pages_modified,
            "warnings_added": warnings_added,
            "warnings_removed": warnings_removed,
            "baseline_scratch": str(baseline_scratch),
        }

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def cleanup(self) -> None:
        """
        Remove only the repositories this manager cloned.

        The list of tracked checkouts is the source of truth - we never
        rmtree the work_dir itself, so a user-supplied work_dir can't be
        accidentally wiped, and an unrelated file dropped into a session
        directory won't be touched either.
        """
        tracked = [*self.repos]
        if self.template is not None:
            tracked.append(self.template)

        for repo in tracked:
            if repo.path.exists():
                shutil.rmtree(repo.path)

        if self._owns_work_dir and self.work_dir.exists():
            try:
                self.work_dir.rmdir()
            except OSError:
                pass

        self.repos = []
        self.template = None

    def __enter__(self) -> "RepoManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


# =============================================================================
# EXAMPLE CUSTOM CHECK/SYNC FUNCTIONS
# =============================================================================


def example_check_python_version(repo: RepoCheckout, manager: RepoManager) -> dict:
    """
    Example custom check for ``run_custom_check``: verify the Python version
    pinned in ``.readthedocs.yaml``.
    """
    rtd_file = repo.path / ".readthedocs.yaml"
    result = {"check": "python_version", "passed": False}

    if not rtd_file.exists():
        result["error"] = ".readthedocs.yaml not found"
        return result

    content = rtd_file.read_text()
    # Simple check - in reality you'd parse the YAML
    if 'python: "3.13"' in content:
        result["passed"] = True
        result["version"] = "3.13"
    else:
        result["error"] = "Python version is not 3.13"

    return result


def example_sync_gitignore(repo: RepoCheckout, manager: RepoManager) -> dict:
    """
    Example custom sync for ``run_custom_sync``: ensure ``.gitignore``
    contains a set of required entries.
    """
    gitignore = repo.path / ".gitignore"
    required_entries = ["__pycache__/", "*.pyc", ".env"]

    result = {"sync": "gitignore", "action": None}

    if not gitignore.exists():
        result["action"] = "create"
        result["entries_to_add"] = required_entries
        return result

    content = gitignore.read_text()
    missing = [e for e in required_entries if e not in content]

    if not missing:
        result["action"] = "skip"
        result["reason"] = "All required entries present"
    else:
        result["action"] = "update"
        result["entries_to_add"] = missing

    return result


# =============================================================================
# CLI INTERFACE
# =============================================================================


def _file_result_status(r: dict) -> str:
    """Classify a per-file check or sync result for the operator."""
    file_name = r.get("file", "")
    if r.get("matches"):
        return "OK"
    if r.get("error"):
        return "NEEDS ATTENTION"
    if r.get("diff"):
        if file_name in NOISY_DIFFS:
            return "DIFFERS (expected)"
        return "NEEDS ATTENTION"
    if r.get("action") == "skip":
        # Skip is only OK if the file already matches the template; a skip
        # caused by a missing template file is a config problem.
        if r.get("reason") == "Template file does not exist":
            return "NEEDS ATTENTION"
        return "OK"
    return "NEEDS ATTENTION"


def print_results(results: dict, title: str, show_diff: bool = True) -> None:
    """Pretty print results."""
    print(f"\n{'=' * 60}")
    print(f" {title}")
    print("=" * 60)

    for repo_name, repo_results in results.items():
        print(f"\n{repo_name}:")
        if isinstance(repo_results, list):
            for r in repo_results:
                file_name = r.get("file", "check")
                print(f"  {file_name}: {_file_result_status(r)}")
                if r.get("error"):
                    print(f"    Error: {r['error']}")
                if r.get("action") and r["action"] != "skip":
                    print(f"    Action: {r['action']}")
                if show_diff and r.get("diff") and file_name not in NOISY_DIFFS:
                    print(f"\n    --- Diff for {file_name} ---")
                    # Indent each line of the diff for readability
                    for line in r["diff"].splitlines():
                        print(f"    {line}")
                    print(f"    --- End diff ---\n")
        elif isinstance(repo_results, dict):
            for key, value in repo_results.items():
                if isinstance(value, dict):
                    print(f"  {key}: {value.get('action', value)}")
                else:
                    print(f"  {key}: {value}")


def print_protection(protections: dict[str, dict | None], branch: str = "main") -> None:
    """
    Tabular dump of branch protection state per repo.

    Structural columns (reviews, force_push, deletions, dismiss_stale,
    enforce_admins, strict) come from a single canonical policy and
    should match across the estate. The CHECKS column (count of required
    status contexts) is inherently per-repo because each context name
    embeds the repo's RTD slug, so we report it but don't compare it.
    """
    print(f"\n{'=' * 60}")
    print(f" BRANCH PROTECTION ({branch})")
    print("=" * 60)
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

    print(f"\n{'=' * 60}")
    print(" EXTRA TOP-LEVEL PATHS (in repo, not in template)")
    print("=" * 60)
    print(
        "\nReported for information only - the tooling never touches these."
        "\nThey may be deliberate per-repo additions (local linter config,"
        "\nauxiliary toolchains) or drift worth investigating."
    )
    for repo_name, paths in repos_with_extras.items():
        print(f"\n{repo_name}:")
        for p in paths:
            print(f"  - {p}")


def print_run_script_results(results: dict, title: str) -> None:
    """Pretty print run-script results: repo, exit code, stdout, stderr."""
    print(f"\n{'=' * 60}")
    print(f" {title}")
    print("=" * 60)

    for repo_name, r in results.items():
        print(f"\n{repo_name}:")
        if r.get("failed"):
            print(f"  FAILED: {r['failure_reason']}")

        if "exit" in r:
            print(f"  exit: {r['exit']}")
        stdout = r.get("stdout", "").rstrip()
        stderr = r.get("stderr", "").rstrip()
        changed = r.get("changed_files") or []

        if stdout:
            print("  stdout:")
            for line in stdout.splitlines():
                print(f"    {line}")
        if stderr:
            print("  stderr:")
            for line in stderr.splitlines():
                print(f"    {line}")
        if changed:
            print(f"  changed: {len(changed)} file(s)")
            for line in changed:
                print(f"    {line}")
        if not r.get("failed") and not stdout and not stderr and not changed:
            print("  (no output, no changes)")


def print_build_result(result: dict) -> None:
    """Print a single build_repo result in human-readable form."""
    print(f"\n{'=' * 60}")
    print(f" BUILD: {result['repo']}")
    print("=" * 60)
    if result["failed"]:
        print(f"  FAILED at stage '{result['stage']}': {result['failure_reason']}")
    else:
        print("  OK")
    if "install_exit_code" in result:
        print(f"  install exit:    {result['install_exit_code']}")
    if "build_exit_code" in result:
        print(f"  sphinx exit:     {result['build_exit_code']}")
    print(f"  warnings:        {len(result.get('warnings', []))}")
    print(f"  pages produced:  {len(result.get('pages', []))}")
    if result.get("warnings"):
        print("  warning lines:")
        for w in result["warnings"][:20]:
            print(f"    {w}")
        if len(result["warnings"]) > 20:
            print(f"    ... ({len(result['warnings']) - 20} more)")
    if result.get("install_stderr_tail") and result["failed"]:
        print("  install stderr tail:")
        for line in result["install_stderr_tail"].splitlines():
            print(f"    {line}")
    if result.get("build_stderr_tail") and result["failed"]:
        print("  build stderr tail:")
        for line in result["build_stderr_tail"].splitlines():
            print(f"    {line}")


def print_build_all_results(results: dict[str, dict]) -> None:
    """One-line summary per repo for build-all."""
    print(f"\n{'=' * 72}")
    print(" BUILD-ALL SUMMARY")
    print("=" * 72)
    print(f"  {'repo':<40} {'status':<10} {'warns':>6} {'pages':>6}")
    print(f"  {'-' * 40} {'-' * 10} {'-' * 6} {'-' * 6}")
    for repo_name, r in results.items():
        status = "FAIL" if r["failed"] else "OK"
        warns = len(r.get("warnings", []))
        pages = len(r.get("pages", []))
        print(f"  {repo_name:<40} {status:<10} {warns:>6} {pages:>6}")
    failures = [n for n, r in results.items() if r["failed"]]
    if failures:
        print(f"\n  {len(failures)} repo(s) failed: {', '.join(failures)}")
        for name in failures:
            r = results[name]
            print(f"\n  --- {name} failure detail ---")
            print(f"    stage: {r['stage']}")
            print(f"    reason: {r.get('failure_reason', 'unknown')}")
            tail = r.get("build_stderr_tail") or r.get("install_stderr_tail") or ""
            for line in tail.splitlines()[-15:]:
                print(f"    {line}")


def print_build_compare(comparison: dict) -> None:
    """
    Print a build_compare change report.

    The report describes what differs between baseline and candidate. It
    doesn't categorise differences as good or bad - that judgement belongs
    to the operator rolling out the change.
    """
    print(f"\n{'=' * 72}")
    print(f" BUILD-COMPARE: {comparison['repo']}")
    print(f"   baseline:  {comparison['baseline_ref']}")
    print(f"   candidate: {comparison['candidate_label']}")
    print("=" * 72)

    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    print(
        f"  baseline:  {'FAIL' if baseline['failed'] else 'OK':<5}  "
        f"warns={len(baseline.get('warnings', []))}  "
        f"pages={len(baseline.get('pages', []))}"
    )
    print(
        f"  candidate: {'FAIL' if candidate['failed'] else 'OK':<5}  "
        f"warns={len(candidate.get('warnings', []))}  "
        f"pages={len(candidate.get('pages', []))}"
    )

    # If the candidate failed to build, surface that first - downstream
    # sections may be empty or misleading. Don't suppress them; the
    # operator may still want to see partial output.
    if candidate["failed"] and not baseline["failed"]:
        print(f"\n  Candidate build did not complete (stage: {candidate['stage']}).")
        print(f"  Reason: {candidate.get('failure_reason', 'unknown')}")
        tail = candidate.get("build_stderr_tail") or candidate.get(
            "install_stderr_tail"
        )
        if tail:
            print("  stderr tail:")
            for line in tail.splitlines()[-15:]:
                print(f"    {line}")

    pages_added = comparison["pages_added"]
    pages_removed = comparison["pages_removed"]
    pages_modified = comparison["pages_modified"]
    warnings_added = comparison["warnings_added"]
    warnings_removed = comparison["warnings_removed"]

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
    if nothing_changed and not candidate["failed"]:
        print("\n  No differences detected.")

    # Only advertise the scratch checkout when there's actually something
    # to inspect. macOS prunes /tmp eventually either way.
    if not nothing_changed or candidate["failed"]:
        print(
            f"\n  Baseline checkout (for further inspection): "
            f"{comparison['baseline_scratch']}"
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
        repos = RepoManager.get_tagged_repos()
        print("Documentation repositories:")
        for repo in repos:
            marker = " (template)" if repo == TEMPLATE_REPO else ""
            print(f"  - {repo}{marker}")
        return

    if args.command == "check-protection":
        protections = RepoManager.check_protection_all_repos()
        print_protection(protections)
        return

    if args.command == "checkout-all":
        branch_name = args.branch_name or RepoManager.generate_branch_name(
            prefix="refactor"
        )
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
            checkout = manager.checkout_repo(args.repo)
            # checkout_repo creates a working branch; switch back to the
            # default so we build the same code that's on main.
            default = RepoManager.default_branch(checkout.path)
            subprocess.run(
                ["git", "checkout", default],
                cwd=checkout.path,
                check=True,
                capture_output=True,
            )
            result = manager.build_repo(checkout)
            print_build_result(result)
            return 0 if not result["failed"] else 1

    if args.command == "build-all":
        with RepoManager() as manager:
            print(f"Working directory: {manager.work_dir}")
            print("Checking out repositories...")
            manager.checkout_all(include_template=True)
            results = manager.build_all_repos()
            print_build_all_results(results)
            failures = [r for r in results.values() if r["failed"]]
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
            origin = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=candidate_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # Match …/<repo>.git or …/<repo>
            repo_name = origin.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        except subprocess.CalledProcessError:
            repo_name = candidate_path.name

        # Cross-check against the tagged estate. The baseline is always
        # cloned from IATI/<repo>, so if the name isn't in the tagged set
        # the clone will 404. Failing fast here gives a clearer message
        # and surfaces the "repo exists but isn't tagged Documentation=true"
        # case explicitly.
        tagged = RepoManager.get_tagged_repos()
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
            comparison = RepoManager.build_compare(
                candidate, baseline_ref=args.baseline_ref
            )
        except ValueError as exc:
            parser.error(str(exc))
        print_build_compare(comparison)
        # Exit code is mechanical: only flag failure when the candidate
        # itself failed to build while the baseline succeeded. Content
        # differences are reported, not adjudicated - the operator judges
        # whether each diff matches their intent.
        candidate_failed = comparison["candidate"]["failed"]
        baseline_failed = comparison["baseline"]["failed"]
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
            detected = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=checkouts[0],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if not detected:
                parser.error(
                    f"Could not detect branch from {checkouts[0]} "
                    "(detached HEAD?). Pass --branch-name."
                )
            branch_name = detected

        # Reuse the existing RepoManager: point it at the already-populated
        # work_dir, register each subdir as a tracked checkout, and call the
        # standard publish flow. We never invoke cleanup() so the checkouts
        # persist for re-runs if needed.
        manager = RepoManager(work_dir=work_dir, branch_name=branch_name)
        for path in checkouts:
            manager.repos.append(RepoCheckout(name=path.name, path=path))

        print(f"Working directory: {manager.work_dir}")
        print(f"Working branch: {manager.branch_name}")
        print(f"Repos to process: {len(manager.repos)}")
        push_results = manager.update_all_to_github(
            args.message,
            pr_body=args.pr_body,
            dry_run=False,
        )
        print_results(push_results, "COMMIT + PUSH + PR")
        return

    # Pick a branch name. run-script uses a script-specific prefix so its
    # branches are distinguishable from sync's at a glance.
    branch_name = getattr(args, "branch_name", None)
    if branch_name is None and args.command == "run-script":
        script_stem = Path(args.script).stem
        branch_name = RepoManager.generate_branch_name(prefix=f"script-{script_stem}")

    with RepoManager(branch_name=branch_name) as manager:
        print(f"Working directory: {manager.work_dir}")
        print(f"Working branch: {manager.branch_name}")
        print("Checking out repositories...")
        manager.checkout_all()
        print(f"Checked out {len(manager.repos)} repos (+ template)")

        if args.command == "check":
            results = manager.check_all_repos(args.files)
            print_results(results, "CHECK RESULTS", show_diff=not args.no_diff)

            extras = manager.extra_paths_all_repos()
            print_extra_paths(extras)

            # Summary
            total_files = sum(len(r) for r in results.values())
            matching = sum(
                1
                for repo_results in results.values()
                for r in repo_results
                if r.get("matches")
            )
            print(f"\nSummary: {matching}/{total_files} files match template")

        elif args.command == "sync":
            copy_results = manager.sync_all_repos(args.files, dry_run=False)
            print_results(copy_results, "FILE COPY")

            push_results = manager.update_all_to_github(
                args.message,
                pr_body=args.pr_body,
                dry_run=False,
            )
            print_results(push_results, "COMMIT + PUSH + PR")

        elif args.command == "run-script":
            results = manager.run_script_on_all_repos(
                args.script,
                include_template=args.include_template,
            )
            print_run_script_results(results, "RUN-SCRIPT RESULTS")

            failed = next((r for r in results.values() if r.get("failed")), None)
            if failed:
                print(
                    f"\nERROR: Script failed in {failed['repo']} "
                    f"({failed['failure_reason']}). "
                    "Aborting; no changes published."
                )
                return 1

            repos_with_changes = [r for r in results.values() if r.get("changed_files")]
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
                dry_run=False,
            )
            print_results(push_results, "COMMIT + PUSH + PR")


if __name__ == "__main__":
    raise SystemExit(main())
