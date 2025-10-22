# kas_exporter.py
#
# Export a kas project configuration supporting format versions 1..20.
# Adds:
#   - path_prefix: prepend a directory (e.g., "sources") to each repo path.
#   - path_dedup:  handle path collisions. Values:
#         "off"   -> raise ValueError on first collision (default)
#         "suffix"-> append "~1", "~2", ... to conflicting paths
#     Emits warnings for every collision.
# Also prepends a comment header with source metadata (git/file + timestamp).
#
# Usage:
#   exporter = KASExporter(md, version=14, path_prefix="sources", path_dedup="suffix")
#   print(exporter.generate_kas_configuration())

from typing import Any, Dict, Union, Optional
import copy
import yaml
import warnings


_DEFAULT_VERSION = 14


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
        path_apply_mode: str = "always"
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
        
        # Build remote->fetch map for URL resolution
        self._remote_fetch = {
            r["name"]: r.get("fetch", "") for r in self.manifest_data.get("remote", [])
        }

    # ----------------------------- public API -----------------------------

    def generate_kas_configuration(self) -> str:
        data = self._base_header()

        # Optional top-level fields
        if self.manifest_data.get("machine"):
            data["machine"] = self.manifest_data["machine"]
        if self.manifest_data.get("distro"):
            data["distro"] = self.manifest_data["distro"]

        # v3+: task
        if self.version >= 3 and self.manifest_data.get("task"):
            data["task"] = str(self.manifest_data["task"])

        # v4+: target can be list
        targets = self.manifest_data.get("targets")
        if targets:
            data["target"] = list(targets) if isinstance(targets, (list, tuple)) else [str(targets)]

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
        defaults = self._build_defaults()
        if defaults:
            data["defaults"] = defaults

        repos = self._build_repos()
        if repos:
            data["repos"] = repos

        yaml_text = yaml.safe_dump(data, sort_keys=False)

        # Prepend source comment header
        header_comment = self._render_source_comment(self.manifest_data.get("__source"))
        if header_comment:
            return header_comment + "\n" + yaml_text
        return yaml_text

    # --------------------------- internal helpers -------------------------

    def _render_source_comment(self, src: Optional[Dict[str, Any]]) -> str:
        lines = []
        lines.append("# -----------------------------------------------------------------------------")
        lines.append("# KAS Exporter Generated Configuration")
        lines.append(f"#   kas format: v{self.version}")
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

    def _base_header(self) -> Dict[str, Any]:
        header: Dict[str, Any] = {"version": self.version}
        includes = self.manifest_data.get("includes")
        if includes:
            header["includes"] = includes
        return {"header": header}

    def _build_defaults(self) -> Dict[str, Any]:
        defs = {}
        defaults_list = self.manifest_data.get("default", []) or []
        if not defaults_list:
            return defs
        d = defaults_list[0]

        if d.get("remote") is not None:
            defs["remote"] = d["remote"]

        rev = d.get("revision")
        commit = None
        branch = d.get("branch")
        tag = d.get("tag")
        refspec = None

        if rev:
            if rev.startswith("refs/tags/"):
                tag = tag or rev.split("/", 2)[-1]
            elif rev.startswith("refs/heads/") or rev.startswith("origin/"):
                branch = branch or rev.split("/", 2)[-1]
            elif len(rev) in (40, 64) and all(c in "0123456789abcdef" for c in rev.lower()):
                commit = rev
            else:
                if self.version < 14:
                    refspec = rev
                else:
                    branch = branch or rev

        commit = d.get("commit", commit)
        branch = d.get("branch", branch)
        tag = d.get("tag", tag)
        refspec = d.get("refspec", refspec)

        if self.version >= 14:
            if commit is not None:
                defs["commit"] = commit
            if branch is not None or (self.version >= 18 and "branch" in d):
                defs["branch"] = branch
            if self.version >= 16 and ("tag" in d or tag is not None):
                defs["tag"] = tag
        else:
            if refspec is not None:
                defs["refspec"] = refspec

        if self.version >= 7 and d.get("type"):
            defs["type"] = d["type"]
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

            if self.version >= 14:
                if commit is not None:
                    repo_entry["commit"] = commit
                if branch is not None or (self.version >= 18 and "branch" in proj):
                    repo_entry["branch"] = branch
                if self.version >= 15 and (tag is not None or (self.version >= 18 and "tag" in proj)):
                    repo_entry["tag"] = tag
            else:
                if refspec is not None:
                    repo_entry["refspec"] = refspec

            # layers
            if proj.get("layers") is not None:
                repo_entry["layers"] = self._normalize_layers(proj["layers"])

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

    def _repo_id(self, proj: Dict[str, Any]) -> str:
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
    def _normalize_layers(layers: Any) -> Dict[str, Any]:
        if isinstance(layers, dict):
            return layers
        if isinstance(layers, str):
            return {layers: None}
        if isinstance(layers, (list, tuple)):
            return {str(x): None for x in layers}
        return layers

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
