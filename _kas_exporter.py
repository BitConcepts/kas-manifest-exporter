import copy
import os
import shutil
import sys
import tempfile
import yaml
import warnings
from typing import Any, Dict, Union, Optional, Iterable
from _repo_remote_layer_scanner import RemoteLayerScanner
from _yaml_dumper import YamlDumper

_DEFAULT_VERSION = 14

# List of substrings to exclude from layers
_LAYER_FILTER_SUBSTRS = [
    "bitbake/lib/layerindexlib/tests/testdata",
    "tests/"
]

# Allowed by spec
_BUILD_SYSTEMS = {"openembedded", "oe", "isar"}


def _discover_layers(url: str, refspec: str | None) -> list[str] | None:
    """
    Populate the minimal fields the exporter expects, discovering layers from REMOTE repos:
      - bblayers_conf_header: {} (left empty)
      - local_conf_header: {}  (left empty)
      - distro, machine, target: left unset unless already present or provided via env
      - repos[<key>]: ensure 'url', 'refspec', and infer 'layers' by scanning the remote
    """

    scanner = RemoteLayerScanner(url, refspec)
    try:
        return scanner.scan()
    except Exception as exc:  # noqa: BLE001 - surface message via CLI
        fallback = _discover_layers_via_clone(url, refspec, exc)
        if fallback is not None:
            return fallback
        raise


def _discover_layers_via_clone(url: str, refspec: str | None, original_exc: Exception) -> list[str] | None:
    """Fallback: clone the repo locally (pygit2) and scan the checkout for layers."""
    try:
        import pygit2  # type: ignore
    except ImportError:
        print(
            f"  Layer discovery failed for {url}: {original_exc} (pygit2 unavailable)",
            file=sys.stderr,
        )
        return None

    tmp_root = tempfile.mkdtemp(prefix="kas-layer-scan-")
    repo_path = os.path.join(tmp_root, "repo")

    print(
        f"  Layer discovery failed for {url}: {original_exc}. "
        "Falling back to a temporary clone...",
        file=sys.stderr,
    )

    try:
        repo = pygit2.clone_repository(url, repo_path)
        _checkout_clone_to_ref(repo, refspec, pygit2)
        local_scanner = RemoteLayerScanner(repo_path, refspec)
        layers = local_scanner.scan()
        if layers:
            print("  Local fallback detected layers successfully.", file=sys.stderr)
        else:
            print("  Local fallback completed but found no layers.", file=sys.stderr)
        return layers
    except Exception as clone_exc:  # noqa: BLE001 - prefer original exception
        print(f"  Local fallback failed: {clone_exc}", file=sys.stderr)
        return None
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _checkout_clone_to_ref(repo, refspec: str | None, pygit2_mod) -> None:
    if not refspec:
        return

    try:
        obj = repo.revparse_single(refspec)
    except Exception:  # noqa: BLE001 - best effort checkout
        return

    if isinstance(obj, pygit2_mod.Tag):
        target = repo.get(obj.target)
        if isinstance(target, pygit2_mod.Commit):
            obj = target
        else:
            return

    if isinstance(obj, pygit2_mod.Commit):
        repo.checkout_tree(obj.tree)
        if hasattr(repo, "set_head_detached"):
            repo.set_head_detached(obj.id)
        else:  # pragma: no cover - older pygit2
            repo.set_head(obj.id)


def _coerce_version(version: Union[int, str]) -> int:
    if isinstance(version, int):
        v = version
    else:
        s = str(version).strip()
        if s in {"0.10", "0", "1.0"}:
            v = 1
        else:
            try:
                v = int(float(s))
            except ValueError:
                v = int(s)
    return max(1, min(20, v))


class KASExporter:
    def __init__(
            self,
            manifest_data: Dict[str, Any],
            version: Union[int, str] = _DEFAULT_VERSION,
            *,
            path_prefix: Optional[str] = None,
            path_dedup: str = "off",  # "off" | "suffix"
            path_apply_mode: str = "always",
            include_layers: Optional[Iterable[str]] = None,
            include_all_layers: bool = False,
    ):
        self.manifest_data = copy.deepcopy(manifest_data)
        self.version = _coerce_version(version)
        self.path_prefix = (path_prefix or "").strip().strip("/\\") or None
        if path_dedup not in {"off", "suffix"}:
            raise ValueError("path_dedup must be 'off' or 'suffix'")
        self.path_dedup = path_dedup
        if path_apply_mode not in {"always", "missing-only"}:
            raise ValueError("path_apply_mode must be 'always' or 'missing-only'")
        self.path_apply_mode = path_apply_mode
        self.include_all_layers = bool(include_all_layers)

        include_layers = include_layers or []
        self._requested_layer_tokens: set[str] = set()
        self._include_layers_by_repo: dict[tuple[str, str], str] = {}
        self._include_layers_any_repo: dict[str, str] = {}
        for entry in include_layers:
            raw = str(entry).strip()
            if not raw:
                continue
            if ":" in raw:
                repo, layer = raw.split(":", 1)
                repo = repo.strip()
                layer = layer.strip()
                if not repo or not layer:
                    raise ValueError(
                        "include_layer entries must be 'repo:layer' or 'layer'"
                    )
                token = f"{repo}:{layer}"
                self._requested_layer_tokens.add(token)
                self._include_layers_by_repo[(repo, layer)] = token
            else:
                token = raw
                self._requested_layer_tokens.add(token)
                self._include_layers_any_repo[token] = token

        self._detected_layers_by_repo: dict[str, list[str]] = {}
        self._matched_layer_tokens: set[str] = set()
        self._layer_detection_failures: dict[str, str] = {}

        # Build remote->fetch map for URL resolution
        self._remote_fetch = {
            r["name"]: r.get("fetch", "") for r in self.manifest_data.get("remote", [])
        }

    # ----------------------------- public API -----------------------------

    def generate_kas_configuration(self) -> str:
        self._reset_layer_tracking()

        # Build the kas configuration header
        data = self._build_header()

        # Build system
        build_system = self._build_system(self.manifest_data.get("build_system"))
        if build_system:
            data["build_system"] = build_system

        # Defaults
        defaults = self._build_defaults()
        if defaults:
            data["defaults"] = defaults

        # Machine
        if self.manifest_data.get("machine"):
            data["machine"] = self.manifest_data["machine"]

        # Distro
        if self.manifest_data.get("distro"):
            data["distro"] = self.manifest_data["distro"]

        # v4+: target can be list
        targets = self.manifest_data.get("targets")
        if targets:
            data["target"] = list(targets) if isinstance(targets, (list, tuple)) else [str(targets)]

        # v3+: task
        if self.version >= 3 and self.manifest_data.get("task"):
            data["task"] = str(self.manifest_data["task"])

        # v6+: env
        if self.version >= 6:
            env = self.manifest_data.get("env")
            if isinstance(env, dict) and env:
                if self.version < 13:
                    env = {k: ("" if v is None else v) for k, v in env.items()}
                data["env"] = env

        # v10+: build_system
        if self.version >= 10 and self.manifest_data.get("build_system"):
            data["build_system"] = self.manifest_data["build_system"]

        # headers (always accepted)
        if self.manifest_data.get("bblayers_conf_header"):
            data["bblayers_conf_header"] = self.manifest_data["bblayers_conf_header"]
        if self.manifest_data.get("local_conf_header"):
            data["local_conf_header"] = self.manifest_data["local_conf_header"]
        if self.manifest_data.get("menu_configuration"):
            data["menu_configuration"] = self.manifest_data["menu_configuration"]

        # v17+: artifacts
        if self.version >= 17 and self.manifest_data.get("artifacts"):
            data["artifacts"] = self.manifest_data["artifacts"]

        # v19+: signers
        if self.version >= 19 and self.manifest_data.get("signers"):
            data["signers"] = self.manifest_data["signers"]

        # defaults / repos


        repos = self._build_repos()
        if repos:
            data["repos"] = repos

        self._validate_layer_requests()

        yaml_text = yaml.dump(data, Dumper=YamlDumper, default_flow_style=False, sort_keys=False)

        # Prepend source comment header
        header_comment = self._render_source_comment(self.manifest_data.get("__source"))
        if header_comment:
            return header_comment + "\n" + yaml_text
        return yaml_text

    # --------------------------- internal helpers -------------------------

    def _build_header(self) -> Dict[str, Any]:
        """
        Build a spec-compliant header:
          header:
            version: <int>           # required
            includes: [ ... ]        # optional; strings or {repo, file} dicts
        """
        header: Dict[str, Any] = {"version": int(self.version)}

        raw_includes: Iterable[Any] = self.manifest_data.get("includes") or []
        norm: list[Union[str, Dict[str, str]]] = []

        seen: set = set()  # to keep order but avoid duplicates

        def _add(item: Union[str, Dict[str, str]]) -> None:
            key = item if isinstance(item, str) else ("dict", item.get("repo"), item.get("file"))
            if key not in seen:
                seen.add(key)
                norm.append(item)

        for it in raw_includes:
            if not it:
                continue

            # Case 1: already a string path
            if isinstance(it, str):
                _add(it.strip())
                continue

            # Case 2: tuple/list like (repo, file)
            if isinstance(it, (tuple, list)) and len(it) >= 2:
                repo, file = str(it[0]).strip(), str(it[1]).strip()
                if repo and file:
                    _add({"repo": repo, "file": file})
                continue

            # Case 3: dict variants -> normalize to {"repo": ..., "file": ...} or string
            if isinstance(it, dict):
                repo = (it.get("repo") or it.get("repository") or it.get("repo_id"))
                file = (it.get("file") or it.get("path") or it.get("kas"))
                # If both present, emit dict form (cross-repo include)
                if repo and file:
                    _add({"repo": str(repo).strip(), "file": str(file).strip()})
                    continue
                # If only file present, treat as same-repo string include
                if file and not repo:
                    _add(str(file).strip())
                    continue
                # otherwise skip invalid include
                continue

            # Unknown type -> skip
            continue

        if norm:
            header["includes"] = norm

        return {"header": header}

    @staticmethod
    def _build_system(value: Optional[str], *, strict: bool = False) -> str | None:
        if not value:
            return None
        v = value.strip().lower()
        if v not in _BUILD_SYSTEMS:
            if strict:
                raise ValueError(f"build_system must be one of {_BUILD_SYSTEMS}, got {value!r}")
            return None
        return "openembedded" if v in {"oe", "openembedded"} else "isar"

    def _render_source_comment(self, src: Optional[Dict[str, Any]]) -> str:
        lines = ["# -----------------------------------------------------------------------------",
                 "# KAS Exporter Generated Configuration", f"#   kas format: v{self.version}"]
        if not src:
            lines.append("#   source: (unspecified)")
        else:
            st = src.get("type")
            if st == "git":
                repo = src.get("repo_url", "(unknown)")
                branch = src.get("branch") or "(default branch)"
                mfile = src.get("manifest_filename") or "default.xml"
                when = src.get("pulled_at") or src.get("timestamp") or "(unknown time)"
                commit = src.get("commit") or "(HEAD unknown)"
                transport = src.get("transport") or "git"
                lines.append(f"#   source: git repo ({transport})")
                lines.append(f"#     repo:   {repo}")
                lines.append(f"#     branch: {branch}")
                lines.append(f"#     file:   {mfile}")
                lines.append(f"#     head:   {commit}")
                lines.append(f"#     pulled: {when} (UTC)")
            elif st == "file":
                fn = src.get("filename") or "(unknown file)"
                when = src.get("parsed_at") or src.get("timestamp") or "(unknown time)"
                lines.append(f"#   source: local file")
                lines.append(f"#     file:   {fn}")
                lines.append(f"#     parsed: {when} (UTC)")
            else:
                lines.append(f"#   source: {st or '(unknown)'}")
        if self.path_prefix:
            lines.append(f"#   path_prefix: {self.path_prefix}")
            lines.append(f"#   path_dedup:  {self.path_dedup}")
        lines.append("# -----------------------------------------------------------------------------")
        return "\n".join(lines)



    def _build_defaults(self) -> Dict[str, Any]:
        """
        Build 'defaults' that conform to the KAS spec:

        defaults:
          repos:
            branch: <str>   # optional
            tag:    <str>   # optional
          patches:
            repo:   <str>   # optional
        """
        defs: Dict[str, Any] = {}

        defaults_list = self.manifest_data.get("default", []) or []
        if not defaults_list:
            return defs

        d = defaults_list[0]

        # Derive branch/tag from 'revision' when present, preserving your logic.
        rev = d.get("revision")
        branch = d.get("branch")
        tag = d.get("tag")

        if rev:
            if rev.startswith("refs/tags/"):
                tag = tag or rev.split("/", 2)[-1]
            elif rev.startswith("refs/heads/") or rev.startswith("origin/"):
                branch = branch or rev.split("/", 2)[-1]
            else:
                # Treat non-SHA rev as a branch name
                _hex = "0123456789abcdef"
                is_sha = len(rev) in (40, 64) and all(c in _hex for c in rev.lower())
                if not is_sha:
                    branch = branch or rev

        # Build 'defaults.repos' per spec
        repos_defaults: Dict[str, Any] = {}
        if branch is not None:
            repos_defaults["branch"] = branch
        if tag is not None:
            repos_defaults["tag"] = tag
        if repos_defaults:
            defs["repos"] = repos_defaults

        # Build 'defaults.patches.repo' if provided by input
        # Accept either nested 'patches: {repo: ...}' or a top-level alias 'patch_repo'
        patch_repo = None
        if isinstance(d.get("patches"), dict):
            patch_repo = d["patches"].get("repo")
        if patch_repo is None:
            patch_repo = d.get("patch_repo")

        if patch_repo is not None:
            defs["patches"] = {"repo": patch_repo}

        return defs

    def _build_repos(self) -> Dict[str, Any]:
        repos: Dict[str, Any] = {}
        used_paths: set[str] = set()

        for proj in self.manifest_data.get("project", []):
            repo_id = self._repo_id(proj)
            repo_entry: Dict[str, Any] = {}

            # URL
            url = self._resolve_url(proj)
            if "url" in proj or url:
                repo_entry["url"] = proj.get("url", url)

            print(f"Scanning repo {repo_id}...")

            # PATH handling
            explicit_path = proj.get("path")
            if self.path_prefix:
                prefix = self.path_prefix.strip().rstrip('/\\')
                if self.path_apply_mode == "always" or not explicit_path:
                    desired_path = f"{prefix}/{repo_id}"
                else:
                    # missing-only and an explicit path exists -> keep the explicit one
                    desired_path = explicit_path
            else:
                desired_path = None  # do not emit a path at all when no prefix

            if desired_path:
                final_path = self._dedup_or_fail(desired_path, used_paths, repo_id)
                repo_entry["path"] = final_path

            # v7: type
            if self.version >= 7 and proj.get("type"):
                repo_entry["type"] = proj["type"]

            # revisions (v14+ commit/branch/tag; earlier refspec)
            commit, branch, tag, refspec = self._derive_revision_fields(proj)
            revision = None
            if self.version >= 14:
                if commit is not None:
                    repo_entry["commit"] = commit
                    revision = commit
                if branch is not None or (self.version >= 18 and "branch" in proj):
                    repo_entry["branch"] = branch
                    revision = branch
                if self.version >= 15 and (tag is not None or (self.version >= 18 and "tag" in proj)):
                    repo_entry["tag"] = tag
                    revision = tag
            else:
                if refspec is not None:
                    repo_entry["refspec"] = refspec
                    revision = refspec

            # layers
            print("  Discovering layers...")
            try:
                layers = _discover_layers(repo_entry["url"], revision)
            except Exception as exc:  # noqa: BLE001 - log repo context, continue
                print(
                    f"  Layer detection failed for {repo_id}: {exc}",
                    file=sys.stderr,
                )
                self._layer_detection_failures[repo_id] = str(exc)
                layers = None
            filtered_layers = self._filter_layer_list(layers or [])
            if filtered_layers:
                self._detected_layers_by_repo[repo_id] = filtered_layers
                selected_layers = self._select_layers_for_repo(repo_id, filtered_layers)
                if selected_layers:
                    repo_entry["layers"] = self._normalize_layers(selected_layers)
            elif self._layer_detection_failures.get(repo_id):
                manual_layers = self._fallback_manual_layers(repo_id)
                if manual_layers:
                    repo_entry["layers"] = self._normalize_layers(manual_layers)

            # v8: patches
            if self.version >= 8 and proj.get("patches"):
                repo_entry["patches"] = self._normalize_patches(proj["patches"])

            # v19: signing
            if self.version >= 19:
                if proj.get("signed") is not None:
                    repo_entry["signed"] = bool(proj["signed"])
                if proj.get("allowed_signers"):
                    repo_entry["allowed_signers"] = list(proj["allowed_signers"])

            # (No 'remote' key in repos)

            # Optional: keep explicit human name if it differs
            if proj.get("name") and proj["name"] != repo_id:
                repo_entry["name"] = proj["name"]

            repos[repo_id] = repo_entry

        return repos

    def _reset_layer_tracking(self) -> None:
        self._detected_layers_by_repo = {}
        self._matched_layer_tokens = set()
        self._layer_detection_failures = {}

    def _select_layers_for_repo(self, repo_id: str, available_layers: list[str]) -> list[str]:
        if not available_layers:
            return []

        if self.include_all_layers:
            self._mark_matching_layer_requests(repo_id, available_layers)
            return available_layers

        if not self._requested_layer_tokens:
            return []

        selected: list[str] = []
        for layer in available_layers:
            matched = False
            repo_key = (repo_id, layer)
            token = self._include_layers_by_repo.get(repo_key)
            if token:
                self._matched_layer_tokens.add(token)
                matched = True
            token = self._include_layers_any_repo.get(layer)
            if token:
                self._matched_layer_tokens.add(token)
                matched = True
            if matched:
                selected.append(layer)
        return selected

    def _fallback_manual_layers(self, repo_id: str) -> list[str]:
        manual: list[str] = []
        for (repo, layer), token in self._include_layers_by_repo.items():
            if repo != repo_id:
                continue
            manual.append(layer)
            self._matched_layer_tokens.add(token)
        if manual:
            warnings.warn(
                (
                    f"Layer detection unavailable for repo '{repo_id}'. "
                    f"Trusting requested layers: {', '.join(manual)}"
                ),
                UserWarning,
            )
        return manual

    def _mark_matching_layer_requests(self, repo_id: str, available_layers: list[str]) -> None:
        if not self._requested_layer_tokens:
            return
        for layer in available_layers:
            repo_key = (repo_id, layer)
            token = self._include_layers_by_repo.get(repo_key)
            if token:
                self._matched_layer_tokens.add(token)
            token = self._include_layers_any_repo.get(layer)
            if token:
                self._matched_layer_tokens.add(token)

    def _validate_layer_requests(self) -> None:
        if not self._requested_layer_tokens:
            return
        missing = sorted(self._requested_layer_tokens - self._matched_layer_tokens)
        if not missing:
            return

        available_lines: list[str] = []
        repo_ids = set(self._detected_layers_by_repo)
        repo_ids.update(self._layer_detection_failures)
        if not repo_ids:
            available_lines.append("  (no layers detected)")
        else:
            for repo_id in sorted(repo_ids):
                if repo_id in self._layer_detection_failures:
                    layers = f"(detection failed: {self._layer_detection_failures[repo_id]})"
                else:
                    layers = ", ".join(self._detected_layers_by_repo[repo_id]) or "(none)"
                available_lines.append(f"  {repo_id}: {layers}")
        available_msg = "\n".join(available_lines)
        raise ValueError(
            "Requested layers were not found: " + ", ".join(missing) +
            "\nAvailable layers:\n" + available_msg
        )

    # ------------- path de-dup / conflict handling -------------

    def _dedup_or_fail(self, desired_path: str, used_paths: set, repo_id: str) -> str:
        """
        Ensure unique paths among repos. If collision:
          - 'off'   -> raise ValueError
          - 'suffix'-> append ~1, ~2, ...; warn on each collision
        """
        if desired_path not in used_paths:
            used_paths.add(desired_path)
            return desired_path

        # Collision
        msg = f"Path collision for repo '{repo_id}': '{desired_path}' already used."
        if self.path_dedup == "off":
            warnings.warn(msg + " (path_dedup='off' -> raising)", UserWarning)
            raise ValueError(msg + " Enable path_dedup='suffix' to auto-resolve.")
        else:
            # Apply suffixing
            n = 1
            while True:
                candidate = f"{desired_path}~{n}"
                if candidate not in used_paths:
                    warnings.warn(
                        msg + f" Using de-duplicated path: '{candidate}'.",
                        UserWarning,
                    )
                    used_paths.add(candidate)
                    return candidate
                n += 1

    # ------------------------------ utilities -----------------------------

    @staticmethod
    def _repo_id(proj: Dict[str, Any]) -> str:
        if proj.get("id"):
            return proj["id"]
        if proj.get("name"):
            return proj["name"].split("/")[-1]
        if proj.get("path"):
            return proj["path"].rstrip("/").split("/")[-1]
        return "repo"

    def _resolve_url(self, proj: Dict[str, Any]):
        if proj.get("url") is not None:
            return proj.get("url")
        remote = proj.get("remote")
        name = proj.get("name")
        if remote and name and remote in self._remote_fetch:
            fetch = self._remote_fetch[remote]
            if fetch and not fetch.endswith("/"):
                fetch += "/"
            return f"{fetch}{name}"
        return None

    def _derive_revision_fields(self, proj: Dict[str, Any]):
        rev = proj.get("revision")
        commit = proj.get("commit")
        branch = proj.get("branch")  # may be set by parser from 'upstream'
        tag = proj.get("tag")
        refspec = proj.get("refspec")

        if rev:
            if rev.startswith("refs/tags/"):
                tag = tag or rev.split("/", 2)[-1]
            elif rev.startswith("refs/heads/") or rev.startswith("origin/"):
                branch = branch or rev.split("/", 2)[-1]
            elif len(rev) in (40, 64) and all(c in "0123456789abcdef" for c in rev.lower()):
                commit = commit or rev
            else:
                if self.version < 14:
                    refspec = refspec or rev
                else:
                    branch = branch or rev
        return commit, branch, tag, refspec

    @staticmethod
    def _filter_layer_list(layers: Iterable[str]) -> list[str]:
        seen, out = set(), []
        for layer in layers:
            if not layer:
                continue
            if any(s in layer for s in _LAYER_FILTER_SUBSTRS):
                continue
            if layer in seen:
                continue
            seen.add(layer)
            out.append(layer)
        out.sort()
        return out

    @classmethod
    def _normalize_layers(cls, layers: Iterable[str]) -> Dict[str, Any]:
        filtered = cls._filter_layer_list(layers)
        return {name: None for name in filtered}

    @staticmethod
    def _normalize_patches(patches: Any) -> Dict[str, Any]:
        if isinstance(patches, dict):
            return patches
        out = {}
        if isinstance(patches, str):
            out[patches] = {}
        elif isinstance(patches, (list, tuple)):
            for p in patches:
                out[str(p)] = {}
        return out
