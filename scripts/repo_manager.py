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
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
                "--jq",
                '.[] | select(.properties[] | select(.property_name == "Documentation" and .value == "true")) | .repository_name',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        repos = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]
        return repos

    def checkout_repo(self, repo_name: str) -> RepoCheckout:
        """
        Clone a single repository.

        Args:
            repo_name: Name of the repository to clone.

        Returns:
            RepoCheckout object for the cloned repo.
        """
        repo_path = self.work_dir / repo_name
        clone_url = f"https://github.com/{GITHUB_ORG}/{repo_name}.git"

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
    def _default_branch(repo_path: Path) -> str:
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
        Refuse to operate if the working tree is on a branch that would
        cause changes to land on the repo's default (or main/master).

        This is defence in depth: checkouts already switch to the working
        branch, so this guard only fires if that switch silently failed.
        """
        current = self._current_branch(repo.path)
        default = self._default_branch(repo.path)
        if not current or current == default or current in ("main", "master"):
            raise RuntimeError(
                f"Refusing to operate on branch {current!r} in {repo.name} "
                f"(default branch is {default!r}). Expected to be on "
                f"{self.branch_name!r} - did the checkout fail?"
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

        default = self._default_branch(repo.path)
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

        Args:
            commit_message: Message for commits.
            pr_title: PR title. Defaults to the commit message.
            pr_body: PR body. Defaults to a generated description.
            dry_run: If True, only report what would be done.

        Returns:
            Dict mapping repo names to update results.
        """
        if pr_title is None:
            pr_title = commit_message
        if pr_body is None:
            pr_body = (
                f"Automated update from iati-docs-management.\n\n"
                f"Branch: `{self.branch_name}`\n"
                f"Commit: {commit_message}"
            )

        results = {}
        for repo in self.repos:
            commit_result = self.commit_changes(repo, commit_message, dry_run)

            if commit_result["action"] == "commit":
                push_result = self.push_changes(
                    repo, pr_title, pr_body, dry_run=dry_run
                )
            else:
                push_result = {"action": "skip", "reason": "No commit made"}

            results[repo.name] = {
                "commit": commit_result,
                "push": push_result,
            }

        return results

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
        "    published.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List repos command
    subparsers.add_parser("list", help="List all tagged documentation repos")

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

    args = parser.parse_args()

    if args.command == "list":
        repos = RepoManager.get_tagged_repos()
        print("Documentation repositories:")
        for repo in repos:
            marker = " (template)" if repo == TEMPLATE_REPO else ""
            print(f"  - {repo}{marker}")
        return

    if args.command is None:
        parser.print_help()
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
