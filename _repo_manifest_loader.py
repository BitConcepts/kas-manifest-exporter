import os
import shutil
import tempfile
import warnings
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

import pygit2 as git
from pygit2.enums import ResetMode, CheckoutStrategy
from pygit2 import Repository, Commit
from _repo_manifest_parser import RepoManifestParser


class LibGitError(RuntimeError):
    pass


_DEFAULT_MANIFESTS: List[str] = [
    "default.xml",
    ".repo/manifests/default.xml",
    "manifests/default.xml",
]


def _repo_key_from_path_or_name(path_or_name: str) -> str:
    """Return a stable repo key (kas 'repos:' key) from manifest project path/name."""
    base = os.path.basename(path_or_name.rstrip("/"))
    return base or path_or_name


def _maybe_add_defaults_from_env(manifest_data: dict) -> None:
    """Allow the caller to set MACHINE/DISTRO/TARGETS via env without hardcoding."""
    machine = os.environ.get("KAS_MACHINE")
    distro = os.environ.get("KAS_DISTRO")
    targets = os.environ.get("KAS_TARGETS")  # space or comma separated
    if distro and not manifest_data.get("distro"):
        manifest_data["distro"] = distro
    if machine and not manifest_data.get("machine"):
        manifest_data["machine"] = machine
    if targets and not manifest_data.get("target"):
        sep = "," if "," in targets else " "
        manifest_data["target"] = [t for t in (x.strip() for x in targets.split(sep)) if t]


def _discover_default_branch(repo: Repository) -> Optional[str]:
    """
    Best-effort default-branch detection:
      1) If HEAD is attached to a local branch, return that name.
      2) Else try 'refs/remotes/origin/HEAD' -> 'origin/<branch>' and return the short branch.
    """
    try:
        if not repo.head_is_unborn and not repo.head_is_detached:
            return repo.head.shorthand  # e.g., 'main' or 'scarthgap'
    except Exception:  # noqa
        pass

    try:
        # origin/HEAD -> origin/<default>
        origin_head = repo.lookup_reference("refs/remotes/origin/HEAD").resolve()
        shorthand = origin_head.shorthand  # e.g., 'origin/main'
        if shorthand and "/" in shorthand:
            return shorthand.split("/", 1)[1]
    except Exception:  # noqa
        pass

    return None


def _checkout_branch(repo: Repository, branch_name: str) -> None:
    """
    Ensure a local branch exists for 'branch_name' from origin/<branch_name>,
    set HEAD to it, reset the working tree, and checkout files.
    """
    try:
        origin = repo.remotes["origin"]
    except KeyError:
        raise LibGitError("Remote 'origin' not found in repository")
    origin.fetch([f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}"])

    local_ref_name = f"refs/heads/{branch_name}"

    try:
        repo.lookup_reference(local_ref_name)
    except KeyError:
        try:
            remote_ref = repo.lookup_reference(f"refs/remotes/origin/{branch_name}")
        except KeyError:
            raise LibGitError(f"Cannot find remote branch 'origin/{branch_name}' after fetch")
        commit: Optional[Commit] = repo.get(remote_ref.target)
        if commit:
            repo.create_branch(branch_name, commit)

    repo.set_head(local_ref_name)

    target = repo.revparse_single(local_ref_name)
    repo.reset(target.id, ResetMode.HARD)

    repo.checkout_head(strategy=CheckoutStrategy.SAFE)


def _resolve_manifest_path(repo_root: str, manifest_filename: Optional[str]) -> str:
    """
    Try the provided filename; if not given or not found, try common defaults.
    Returns absolute path to a found manifest; raises FileNotFoundError otherwise.
    """
    candidates: List[str] = []
    if manifest_filename:
        candidates.append(manifest_filename)
    for c in _DEFAULT_MANIFESTS:
        if c not in candidates:
            candidates.append(c)

    for cand in candidates:
        p = os.path.normpath(os.path.join(repo_root, cand))
        if os.path.isfile(p):
            return p

    # If they passed an absolute path, try that as-is
    if manifest_filename and os.path.isabs(manifest_filename) and os.path.isfile(manifest_filename):
        return manifest_filename

    raise FileNotFoundError(
        f"Manifest not found. Tried: {', '.join(candidates)} inside {repo_root}"
    )


def load_manifest_from_git(
        repo_url: str,
        branch: Optional[str] = None,
        manifest_filename: Optional[str] = None,
        *,
        workdir: Optional[str] = None,
        keep_checkout: bool = False,
) -> Dict[str, Any]:
    """
    Clone 'repo_url' using libgit2 and parse the chosen manifest.

    Args:
      repo_url: HTTPS/SSH URL for the repo.
      branch:   Optional branch/ref name. If None, uses repo's default branch.
      manifest_filename: Optional path relative to repo root. If None, uses 'default.xml' (w/ fallbacks).
      workdir:  Optional parent directory for the clone; default is a new temp dir.
      keep_checkout: If True, keep the cloned checkout on disk and return its path in '__checkout_dir'.

    Returns:
      manifest_data dict for KASExporter; includes '__source' meta.
    """
    if not repo_url:
        raise ValueError("repo_url is required")

    parent_dir = workdir or tempfile.mkdtemp(prefix="repo-manifests-libgit2-")
    created_parent = workdir is None
    clone_dir = os.path.join(parent_dir, "repo")

    try:
        # Clone without specifying checkout_branch so we land on provider's default branch.
        # If 'branch' is provided, we’ll check it out explicitly after cloning.
        repo = git.clone_repository(repo_url, clone_dir)

        # Decide which branch to use
        active_branch = branch or _discover_default_branch(repo)  # may still be None (detached or unborn)
        if branch:
            # User specified branch: checkout it
            _checkout_branch(repo, branch)
            active_branch = branch
        else:
            # If we didn't learn an active branch (detached HEAD), try to make one from origin/HEAD.
            if not active_branch:
                default_branch = _discover_default_branch(repo)
                if default_branch:
                    _checkout_branch(repo, default_branch)
                    active_branch = default_branch
                else:
                    # Leave as-is; we'll still read files from current tree
                    pass

        # Record commit
        try:
            head_commit = repo.revparse_single("HEAD").id.__str__()
        except Exception:  # noqa
            head_commit = None

        # Locate manifest
        manifest_path = _resolve_manifest_path(clone_dir, manifest_filename)

        # Parse
        parser = RepoManifestParser()
        manifest_data = parser.parse_file(manifest_path)

        # Stamp source metadata (overwrite parser's local-file stamp)
        manifest_data["__source"] = {
            "type": "git",
            "repo_url": repo_url,
            "branch": active_branch,  # may be None (detached)
            "manifest_filename": os.path.relpath(manifest_path, clone_dir),
            "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "commit": head_commit,
        }

        if keep_checkout:
            manifest_data["__checkout_dir"] = clone_dir
        else:
            shutil.rmtree(clone_dir, ignore_errors=True)
            if created_parent:
                shutil.rmtree(parent_dir, ignore_errors=True)

        return manifest_data

    except Exception as e:
        # Cleanup on failure unless kept
        if not keep_checkout:
            try:
                shutil.rmtree(clone_dir, ignore_errors=True)
            except Exception:  # noqa
                pass
            if created_parent:
                try:
                    shutil.rmtree(parent_dir, ignore_errors=True)
                except Exception:  # noqa
                    pass
        raise LibGitError(str(e)) from e


def _discover_repo_root(start_dir: str) -> Optional[str]:
    """
    Find the enclosing git repo workdir (not .git path). Returns None if not in a repo.
    """
    try:
        git_dir = git.discover_repository(start_dir)
        if not git_dir:
            return None
        repo = git.Repository(git_dir)
        return repo.workdir or None
    except Exception:  # noqa
        return None


def _is_within(path: str, root: str) -> bool:
    """
    Safe containment check using real paths.
    """
    try:
        path_real = os.path.realpath(path)
        root_real = os.path.realpath(root)
        return os.path.commonpath([path_real, root_real]) == root_real
    except Exception:  # noqa
        return False


def load_manifest_from_file(
        manifest_path: str,
        *,
        warn_if_outside_repo: bool = True,
) -> Dict[str, Any]:
    """
    Load a local repo-XML manifest file without cloning.

    - Parses the manifest using RepoManifestParser (includes resolved from disk).
    - If the file is in a git repo, verifies that includes also reside inside that repo.
    - Emits warnings when the main file or its includes are outside the repo (or missing).

    Returns:
        manifest_data dict suitable for KASExporter (parser stamps '__source': {'type': 'file', ...}).
    """
    if not manifest_path:
        raise ValueError("manifest_path is required")

    manifest_path = os.path.abspath(manifest_path)
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    manifest_dir = os.path.dirname(manifest_path)
    repo_root = _discover_repo_root(manifest_dir)

    # Parse (this also stamps __source = {'type':'file', 'filename':..., 'parsed_at': ...})
    parser = RepoManifestParser()
    manifest_data = parser.parse_file(manifest_path)

    # Repo membership checks & include validation
    if warn_if_outside_repo:
        if not repo_root:
            warnings.warn(
                f"Manifest '{manifest_path}' is not inside a Git repository; "
                f"includes may not be under version control.",
                UserWarning,
            )
        else:
            if not _is_within(manifest_path, repo_root):
                warnings.warn(
                    f"Manifest '{manifest_path}' is outside the repository root '{repo_root}'.",
                    UserWarning,
                )

            # Validate each include
            for inc in (manifest_data.get("includes") or []):
                inc_name = inc.get("name")
                if not inc_name:
                    continue
                inc_full = os.path.normpath(os.path.join(manifest_dir, inc_name))
                if not os.path.isfile(inc_full):
                    warnings.warn(
                        f"Included file not found on disk: '{inc_full}' (from include '{inc_name}').",
                        UserWarning,
                    )
                    continue
                if not _is_within(inc_full, repo_root):
                    warnings.warn(
                        f"Included file '{inc_full}' is outside the repository root '{repo_root}'.",
                        UserWarning,
                    )

    return manifest_data
