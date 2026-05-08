#!/usr/bin/env python3.13
"""
Manage IATI documentation repositories.

This script provides functions for checking out, checking, syncing, and updating
IATI documentation repos that are tagged with the Documentation property.
iati-docs-base is the authoritative template for comparison.
"""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# The authoritative template repo
TEMPLATE_REPO = "iati-docs-base"
GITHUB_ORG = "IATI"

# Files that should be synced across all repos
FILES_TO_CHECK = [
    ".readthedocs.yaml",
    "requirements.txt",
    ".github/workflows/ci.yml",
    ".vscode/launch.json",
]


@dataclass
class RepoCheckout:
    """Represents a checked-out repository."""

    name: str
    path: Path
    is_template: bool = False


class RepoManager:
    """Manages IATI documentation repositories."""

    def __init__(self, work_dir: Path | None = None):
        """
        Initialize the repo manager.

        Args:
            work_dir: Working directory for checkouts. If None, a temp dir is created.
        """
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        if work_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="iati-docs-")
            self.work_dir = Path(self._temp_dir.name)
        else:
            self.work_dir = work_dir
            self.work_dir.mkdir(parents=True, exist_ok=True)

        self.repos: list[RepoCheckout] = []
        self.template: RepoCheckout | None = None

    def get_tagged_repos(self) -> list[str]:
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

        Returns:
            List of all checked-out repos.
        """
        repo_names = self.get_tagged_repos()

        # Always checkout template first if requested
        if include_template and TEMPLATE_REPO not in repo_names:
            self.checkout_repo(TEMPLATE_REPO)

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

    def check_all_repos(
        self, files: list[str] | None = None
    ) -> dict[str, list[dict]]:
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
            if repo.is_template:
                continue

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
        Run a custom check function on all repos.

        This is a placeholder for custom check logic.

        Args:
            check_func: Function that takes a RepoCheckout and RepoManager,
                       returns a dict with check results.

        Returns:
            Dict mapping repo names to check results.
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
            if repo.is_template:
                continue

            repo_results = []
            for file_path in files:
                sync_result = self.sync_file_from_template(repo, file_path, dry_run)
                repo_results.append(sync_result)
            results[repo.name] = repo_results

        return results

    def run_custom_sync(
        self,
        sync_func: Callable[[RepoCheckout, "RepoManager"], dict],
        dry_run: bool = True,
    ) -> dict[str, dict]:
        """
        Run a custom sync function on all repos.

        This is a placeholder for custom sync logic.

        Args:
            sync_func: Function that takes a RepoCheckout and RepoManager,
                      returns a dict with sync results.
            dry_run: Passed to sync_func for it to respect.

        Returns:
            Dict mapping repo names to sync results.
        """
        results = {}
        for repo in self.repos:
            results[repo.name] = sync_func(repo, self)
        return results

    def run_script_on_all_repos(
        self,
        script_path: str | Path,
        dry_run: bool = True,
        include_template: bool = False,
    ) -> dict[str, dict]:
        """
        Run a script on all repos.

        The script is executed with the repo path as the current working directory.
        The repo name is passed as the first argument to the script.

        Args:
            script_path: Path to the script to run.
            dry_run: If True, only report what would be done.
            include_template: If True, also run on the template repo.

        Returns:
            Dict mapping repo names to execution results.
        """
        script_path = Path(script_path).resolve()

        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        results = {}
        repos_to_process = list(self.repos)
        if include_template and self.template:
            repos_to_process.append(self.template)

        for repo in repos_to_process:
            result = {
                "repo": repo.name,
                "script": str(script_path),
                "dry_run": dry_run,
                "action": "run",
            }

            if dry_run:
                result["would_run"] = f"{script_path} (in {repo.path})"
                results[repo.name] = result
                continue

            try:
                proc = subprocess.run(
                    [str(script_path), repo.name],
                    cwd=repo.path,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout per repo
                )
                result["returncode"] = proc.returncode
                result["stdout"] = proc.stdout
                result["stderr"] = proc.stderr
                result["success"] = proc.returncode == 0
            except subprocess.TimeoutExpired:
                result["success"] = False
                result["error"] = "Script timed out after 300 seconds"
            except Exception as e:
                result["success"] = False
                result["error"] = str(e)

            results[repo.name] = result

        return results

    # =========================================================================
    # UPDATE TO GITHUB FUNCTIONS
    # =========================================================================

    def commit_changes(
        self, repo: RepoCheckout, message: str, dry_run: bool = True
    ) -> dict:
        """
        Commit any changes in a repo.

        Args:
            repo: The repo to commit.
            message: Commit message.
            dry_run: If True, only report what would be done.

        Returns:
            Dict with commit results.
        """
        result = {
            "repo": repo.name,
            "action": None,
            "dry_run": dry_run,
        }

        # Check for changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo.path,
            capture_output=True,
            text=True,
        )

        if not status.stdout.strip():
            result["action"] = "skip"
            result["reason"] = "No changes to commit"
            return result

        result["action"] = "commit"
        result["changes"] = status.stdout.strip().split("\n")

        if not dry_run:
            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo.path,
                check=True,
            )
            # Commit
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=repo.path,
                check=True,
            )
            result["committed"] = True

        return result

    def push_changes(self, repo: RepoCheckout, dry_run: bool = True) -> dict:
        """
        Push committed changes to GitHub.

        Args:
            repo: The repo to push.
            dry_run: If True, only report what would be done.

        Returns:
            Dict with push results.
        """
        result = {
            "repo": repo.name,
            "action": None,
            "dry_run": dry_run,
        }

        # Check if there are commits to push
        status = subprocess.run(
            ["git", "status", "-sb"],
            cwd=repo.path,
            capture_output=True,
            text=True,
        )

        # Check if ahead of remote
        if "[ahead" not in status.stdout:
            result["action"] = "skip"
            result["reason"] = "No commits to push"
            return result

        result["action"] = "push"

        if not dry_run:
            subprocess.run(
                ["git", "push"],
                cwd=repo.path,
                check=True,
            )
            result["pushed"] = True

        return result

    def update_all_to_github(
        self, commit_message: str, dry_run: bool = True
    ) -> dict[str, dict]:
        """
        Commit and push changes in all repos.

        Args:
            commit_message: Message for commits.
            dry_run: If True, only report what would be done.

        Returns:
            Dict mapping repo names to update results.
        """
        results = {}
        for repo in self.repos:
            if repo.is_template:
                continue

            commit_result = self.commit_changes(repo, commit_message, dry_run)
            push_result = {"action": "skip", "reason": "No commit made"}

            if commit_result.get("committed") or (
                not dry_run and commit_result["action"] == "commit"
            ):
                push_result = self.push_changes(repo, dry_run)
            elif commit_result["action"] == "commit" and dry_run:
                push_result = {"action": "push", "dry_run": True}

            results[repo.name] = {
                "commit": commit_result,
                "push": push_result,
            }

        return results

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def cleanup(self) -> None:
        """Remove all checked out repositories and clean up temp directory."""
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None
        elif self.work_dir.exists():
            shutil.rmtree(self.work_dir)

        self.repos = []
        self.template = None
        print("Cleanup complete.")

    def __enter__(self) -> "RepoManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


# =============================================================================
# EXAMPLE CUSTOM CHECK/SYNC FUNCTIONS
# =============================================================================


def example_check_python_version(repo: RepoCheckout, manager: RepoManager) -> dict:
    """
    Example custom check: verify Python version in .readthedocs.yaml.

    This is a placeholder showing how to implement custom checks.
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
    Example custom sync: ensure .gitignore has certain entries.

    This is a placeholder showing how to implement custom syncs.
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


def print_results(results: dict, title: str, show_diff: bool = True) -> None:
    """Pretty print results."""
    print(f"\n{'=' * 60}")
    print(f" {title}")
    print("=" * 60)

    for repo_name, repo_results in results.items():
        print(f"\n{repo_name}:")
        if isinstance(repo_results, list):
            for r in repo_results:
                status = "OK" if r.get("matches") or r.get("action") == "skip" else "NEEDS ATTENTION"
                print(f"  {r.get('file', 'check')}: {status}")
                if r.get("error"):
                    print(f"    Error: {r['error']}")
                if r.get("action") and r["action"] != "skip":
                    print(f"    Action: {r['action']}")
                if show_diff and r.get("diff") and r.get("file") != "requirements.txt":
                    print(f"\n    --- Diff for {r.get('file')} ---")
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


def main():
    """Main CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage IATI documentation repositories.\n\n"
        "IMPORTANT: The 'sync' and 'push' commands run in DRY-RUN mode by default.\n"
        "This means no changes will be made unless you pass the --apply flag.\n"
        "Always review the dry-run output before applying changes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List repos command
    subparsers.add_parser("list", help="List all tagged documentation repos")

    # Check command
    check_parser = subparsers.add_parser(
        "check", help="Check repos against template"
    )
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
        help="Sync files from template to repos (dry-run by default)",
        description="Copy files from the template repo to all documentation repos.\n"
        "By default, runs in DRY-RUN mode showing what would be changed.\n"
        "Use --apply to actually copy the files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync_parser.add_argument(
        "--files",
        nargs="+",
        default=FILES_TO_CHECK,
        help="Files to sync",
    )
    sync_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply changes (without this flag, only shows what would change)",
    )

    # Push command
    push_parser = subparsers.add_parser(
        "push",
        help="Commit and push changes to GitHub (dry-run by default)",
        description="Commit and push any local changes to GitHub.\n"
        "By default, runs in DRY-RUN mode showing what would be committed and pushed.\n"
        "Use --apply to actually commit and push the changes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    push_parser.add_argument(
        "--message",
        "-m",
        required=True,
        help="Commit message",
    )
    push_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit and push (without this flag, only shows what would change)",
    )

    # Run-script command
    run_script_parser = subparsers.add_parser(
        "run-script",
        help="Run a script on all repos (dry-run by default)",
        description="Execute a script in each documentation repo's directory.\n"
        "The script receives the repo name as its first argument.\n"
        "By default, runs in DRY-RUN mode showing what would be executed.\n"
        "Use --apply to actually run the script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_script_parser.add_argument(
        "script",
        help="Path to the script to run on each repo",
    )
    run_script_parser.add_argument(
        "--include-template",
        action="store_true",
        help="Also run on the template repo",
    )
    run_script_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the script (without this flag, only shows what would run)",
    )

    args = parser.parse_args()

    if args.command == "list":
        manager = RepoManager.__new__(RepoManager)
        manager._temp_dir = None
        manager.work_dir = Path(".")
        repos = manager.get_tagged_repos()
        print("Documentation repositories:")
        for repo in repos:
            marker = " (template)" if repo == TEMPLATE_REPO else ""
            print(f"  - {repo}{marker}")
        return

    if args.command is None:
        parser.print_help()
        return

    # Commands that need checkout
    with RepoManager() as manager:
        print(f"Working directory: {manager.work_dir}")
        print("Checking out repositories...")
        manager.checkout_all()
        print(f"Checked out {len(manager.repos)} repos (+ template)")

        if args.command == "check":
            results = manager.check_all_repos(args.files)
            print_results(results, "CHECK RESULTS", show_diff=not args.no_diff)

            # Summary
            total_files = sum(len(r) for r in results.values())
            matching = sum(
                1 for repo_results in results.values()
                for r in repo_results if r.get("matches")
            )
            print(f"\nSummary: {matching}/{total_files} files match template")

        elif args.command == "sync":
            dry_run = not args.apply
            results = manager.sync_all_repos(args.files, dry_run=dry_run)
            mode = "DRY RUN" if dry_run else "APPLIED"
            print_results(results, f"SYNC RESULTS ({mode})")

        elif args.command == "push":
            dry_run = not args.apply
            results = manager.update_all_to_github(args.message, dry_run=dry_run)
            mode = "DRY RUN" if dry_run else "APPLIED"
            print_results(results, f"PUSH RESULTS ({mode})")

        elif args.command == "run-script":
            dry_run = not args.apply
            results = manager.run_script_on_all_repos(
                args.script,
                dry_run=dry_run,
                include_template=args.include_template,
            )
            mode = "DRY RUN" if dry_run else "APPLIED"
            print_results(results, f"RUN-SCRIPT RESULTS ({mode})")

            if not dry_run:
                # Print summary
                success = sum(1 for r in results.values() if r.get("success"))
                failed = sum(1 for r in results.values() if not r.get("success"))
                print(f"\nSummary: {success} succeeded, {failed} failed")


if __name__ == "__main__":
    main()
