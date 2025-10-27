from typing import Any, Dict, Optional, Tuple, Set
import os
import io
import xml.etree.ElementTree as ET
import copy
import warnings
from datetime import datetime, timezone


def _text_bool(val: Optional[str]) -> Optional[bool]:
    if val is None:
        return None
    v = val.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


class RepoManifestParser:
    def __init__(self) -> None:
        self._seen_includes: Set[Tuple[str, str]] = set()
        self._warned_missing_manifest_dir: bool = False

    def parse_file(self, path: str) -> Dict[str, Any]:
        manifest_dir = os.path.dirname(os.path.abspath(path))
        with io.open(path, "r", encoding="utf-8") as f:
            xml_text = f.read()
        # Reset per-parse flags
        self._warned_missing_manifest_dir = False
        self._seen_includes.clear()
        md = self.parse_string(xml_text, manifest_dir=manifest_dir)
        # Stamp source metadata (local file)
        md["__source"] = {
            "type": "file",
            "filename": os.path.abspath(path),
            "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return md

    def parse_string(self, xml_text: str, manifest_dir: Optional[str] = None) -> Dict[str, Any]:
        # Reset per-parse flags
        self._warned_missing_manifest_dir = False
        self._seen_includes.clear()

        root = ET.fromstring(xml_text)
        if root.tag != "manifest":
            raise ValueError("Root element must be <manifest>")
        state = self._empty_state()
        # recursively process this tree (and its includes/submanifests)
        self._process_manifest(root, state, manifest_dir=manifest_dir)
        # apply extend-project & remove-project after collecting all base projects
        self._apply_remove_project(state)
        self._apply_extend_project(state)
        # build the final manifest_data dict
        return self._state_to_manifest_data(state)

    # ------------------------------ INTERNALS ---------------------------------

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "remotes": {},  # name -> { fetch, review, pushurl, alias, revision, annotations[] }
            "default": {},  # { remote, revision, dest-branch, upstream, sync-j, sync-c, sync-s, sync-tags }
            "projects": [],  # list of project dicts (see _make_project)
            "extend": [],  # list of extend-project ops
            "remove": [],  # list of remove-project ops
            "includes": [],  # resolved (path, groups, revision) for info only
            "submanifests": [],  # resolved submanifest metadata (for info)
            "extras": {},  # top-level extra things: notice, repo-hooks, superproject, contactinfo
        }

    def _process_manifest(self, root: ET.Element, state: Dict[str, Any], manifest_dir: Optional[str]) -> None:
        # order matters a bit: collect remotes/default first
        for child in root:
            if child.tag == "remote":
                self._add_remote(child, state)
            elif child.tag == "default":
                self._merge_default(child, state)

        # then includes/submanifest to bring in more remotes/defaults before projects
        for child in root:
            if child.tag == "include":
                self._handle_include(child, state, manifest_dir)
            elif child.tag == "submanifest":
                self._handle_submanifest(child, state, manifest_dir)

        # now core project definitions + modifiers & metadata
        for child in root:
            tag = child.tag
            if tag == "project":
                self._add_project(child, state)
            elif tag == "extend-project":
                self._queue_extend(child, state)
            elif tag == "remove-project":
                self._queue_remove(child, state)
            elif tag == "notice":
                state["extras"]["notice"] = (child.text or "").strip()
            elif tag == "repo-hooks":
                state["extras"]["repo-hooks"] = {k: child.attrib.get(k) for k in ("in-project", "enabled-list")}
            elif tag == "superproject":
                state["extras"]["superproject"] = dict(child.attrib)
            elif tag == "contactinfo":
                state["extras"]["contactinfo"] = dict(child.attrib)
            elif tag in ("remote", "default", "include", "submanifest"):
                pass
            else:
                state["extras"].setdefault("unknown", []).append({tag: copy.deepcopy(child.attrib)})

    # ------------------------------ ELEMENT HANDLERS --------------------------

    @staticmethod
    def _add_remote(el: ET.Element, state: Dict[str, Any]) -> None:
        name = el.attrib["name"]
        r = {
            "name": name,
            "fetch": el.attrib.get("fetch", ""),
            "pushurl": el.attrib.get("pushurl"),
            "review": el.attrib.get("review"),
            "alias": el.attrib.get("alias"),
            "revision": el.attrib.get("revision"),  # branch-like
            "annotations": [],
        }
        for ann in el.findall("annotation"):
            r["annotations"].append(dict(ann.attrib))
        state["remotes"][name] = r

    @staticmethod
    def _merge_default(el: ET.Element, state: Dict[str, Any]) -> None:
        d = state["default"]
        for k in ("remote", "revision", "dest-branch", "upstream", "sync-j", "sync-c", "sync-s", "sync-tags"):
            if el.attrib.get(k) is not None:
                d[k] = el.attrib.get(k)

    def _handle_include(self, el: ET.Element, state: Dict[str, Any], manifest_dir: Optional[str]) -> None:
        name = el.attrib["name"]
        state["includes"].append(dict(el.attrib))

        # If we don't have a manifest_dir, we cannot resolve the include.
        if not manifest_dir:
            if not self._warned_missing_manifest_dir:
                warnings.warn(
                    "Repo manifest contains <include> elements, but no manifest_dir was provided. "
                    "Includes will NOT be resolved. Pass manifest_dir (usually '.repo/manifests') "
                    "to enable on-disk include resolution.",
                    UserWarning,
                )
                self._warned_missing_manifest_dir = True
            return

        include_key = (manifest_dir or "", name)
        if include_key in self._seen_includes:
            return
        self._seen_includes.add(include_key)

        inc_path = os.path.join(manifest_dir, name)
        if os.path.isfile(inc_path):
            sub = ET.parse(inc_path).getroot()
            self._process_manifest(sub, state, manifest_dir=os.path.dirname(inc_path))
        else:
            # We do have manifest_dir, but file isn't present—warn specifically for this case.
            warnings.warn(
                f"Include file not found on disk: {inc_path!r}. Skipping this include.",
                UserWarning,
            )

    @staticmethod
    def _handle_submanifest(el: ET.Element, state: Dict[str, Any], manifest_dir: Optional[str]) -> None:
        info = dict(el.attrib)
        state["submanifests"].append(info)
        # Keep as breadcrumb; environment-specific resolution can be added later.

    def _add_project(self, el: ET.Element, state: Dict[str, Any]) -> None:
        a = el.attrib
        name = a["name"]
        proj = self._make_project(name=name)
        for k in ("path", "remote", "revision", "dest-branch", "groups", "upstream", "clone-depth"):
            if a.get(k) is not None:
                proj[k] = a[k]
        for k in ("sync-c", "sync-s"):
            b = _text_bool(a.get(k))
            if b is not None:
                proj[k] = b
        proj["annotations"] = [dict(x.attrib) for x in el.findall("annotation")]
        proj["copyfiles"] = [dict(x.attrib) for x in el.findall("copyfile")]
        proj["linkfiles"] = [dict(x.attrib) for x in el.findall("linkfile")]
        nested = el.findall("project")
        if nested:
            proj["subprojects"] = []
            for sub in nested:
                subp = self._make_project(name=sub.attrib["name"])
                for k in ("path", "remote", "revision", "dest-branch", "groups", "upstream", "clone-depth"):
                    if sub.attrib.get(k) is not None:
                        subp[k] = sub.attrib[k]
                for k in ("sync-c", "sync-s"):
                    b = _text_bool(sub.attrib.get(k))
                    if b is not None:
                        subp[k] = b
                subp["annotations"] = [dict(x.attrib) for x in sub.findall("annotation")]
                subp["copyfiles"] = [dict(x.attrib) for x in sub.findall("copyfile")]
                subp["linkfiles"] = [dict(x.attrib) for x in sub.findall("linkfile")]
                proj["subprojects"].append(subp)
        state["projects"].append(proj)

    @staticmethod
    def _queue_extend(el: ET.Element, state: Dict[str, Any]) -> None:
        state["extend"].append(dict(el.attrib))

    @staticmethod
    def _queue_remove(el: ET.Element, state: Dict[str, Any]) -> None:
        op = dict(el.attrib)
        b = _text_bool(op.get("optional"))
        if b is not None:
            op["optional"] = str(b)
        state["remove"].append(op)

    # ------------------------------ TRANSFORMS --------------------------------

    @staticmethod
    def _apply_remove_project(state: Dict[str, Any]) -> None:
        if not state["remove"]:
            return
        keep = []
        for p in state["projects"]:
            removed = False
            for r in state["remove"]:
                by_name = (r.get("name") and r["name"] == p["name"])
                by_path = (r.get("path") and r["path"] == p.get("path"))

                def both_or_either() -> bool:
                    if r.get("name") and r.get("path"):
                        return by_name and by_path
                    if r.get("name"):
                        return by_name
                    if r.get("path"):
                        return by_path
                    return False

                if both_or_either():
                    removed = True
                    break
            if not removed:
                keep.append(p)
        state["projects"] = keep

    @staticmethod
    def _apply_extend_project(state: Dict[str, Any]) -> None:
        if not state["extend"]:
            return
        for e in state["extend"]:
            name = e.get("name")
            path = e.get("path")
            for p in state["projects"]:
                if p["name"] != name:
                    continue
                if path and p.get("path") != path:
                    continue
                for k in ("revision", "remote", "dest-branch", "upstream", "groups"):
                    if e.get(k) is not None:
                        p[k] = e[k]
                if e.get("base-rev") is not None:
                    p.setdefault("extras", {})["base-rev"] = e["base-rev"]
                if e.get("dest-path") is not None:
                    p["path"] = e["dest-path"]

    # ------------------------------ STATE -> EXPORTER -------------------------

    def _state_to_manifest_data(self, state: Dict[str, Any]) -> Dict[str, Any]:
        md: Dict[str, Any] = {
            "remote": [],
            "project": [],
            "default": [],
            "extras": state["extras"],
        }

        for name, r in state["remotes"].items():
            md["remote"].append({"name": name, "fetch": r.get("fetch", "")})

        d = {}
        if state["default"].get("remote"):
            d["remote"] = state["default"]["remote"]
        if state["default"].get("revision"):
            d["revision"] = state["default"]["revision"]
        for k in ("dest-branch", "upstream", "sync-j", "sync-c", "sync-s", "sync-tags"):
            if state["default"].get(k) is not None:
                d[k] = state["default"][k]
        if d:
            md["default"].append(d)

        for p in state["projects"]:
            md["project"].append(self._project_to_exporter_shape(p, state))

        if state["includes"]:
            md.setdefault("includes", [])
            md["includes"] += [x.get("name") for x in state["includes"] if x.get("name")]

        return md

    @staticmethod
    def _project_to_exporter_shape(p: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": p["name"],
            "path": p.get("path", p["name"]),
        }
        remote = p.get("remote") or state["default"].get("remote")
        if remote:
            out["remote"] = remote

        rev = p.get("revision")
        if rev is None and remote and state["remotes"].get(remote, {}).get("revision"):
            rev = state["remotes"][remote]["revision"]
        if rev is None and state["default"].get("revision"):
            rev = state["default"]["revision"]
        if rev is not None:
            out["revision"] = rev

        # Map repo-manifest's 'upstream' (and fallback 'dest-branch') to kas 'branch'
        upstream = p.get("upstream")
        dest_branch = p.get("dest-branch")
        if upstream:
            out["branch"] = upstream
        elif dest_branch:
            out["branch"] = dest_branch

        extras_fields = ("dest-branch", "upstream", "groups", "clone-depth", "sync-c", "sync-s")
        for k in extras_fields:
            if p.get(k) is not None:
                out.setdefault("extras", {})[k] = p[k]

        if p.get("annotations"):
            out.setdefault("extras", {})["annotations"] = p["annotations"]
        if p.get("copyfiles"):
            out.setdefault("extras", {})["copyfiles"] = p["copyfiles"]
        if p.get("linkfiles"):
            out.setdefault("extras", {})["linkfiles"] = p["linkfiles"]
        if p.get("subprojects"):
            out.setdefault("extras", {})["subprojects"] = p["subprojects"]

        return out

    # ------------------------------ HELPERS -----------------------------------

    @staticmethod
    def _make_project(name: str) -> Dict[str, Any]:
        return {"name": name}
