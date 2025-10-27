import os
import re
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from _http_client import HttpClient
from typing import Dict, List, Optional

_CONF_TARGET = "conf/layer.conf"

# cgit quick signals
_CGIT_META = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']cgit\b', re.I)
_CGIT_CSS = re.compile(r'href=["\'][^"\']*cgit\.css\b', re.I)
_CGIT_ID = re.compile(r'id=["\']cgit["\']', re.I)


def _strip_git_suffix(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def _parse_basic_auth(auth: Optional[str]) -> Optional[str]:
    if not auth or ":" not in auth:
        return None
    # RFC 7617 Basic base64(user:pass); HttpClient expects headers, so we pass Authorization directly
    import base64
    raw = auth.encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"Basic {b64}"


@dataclass
class RemoteLayerScanner:
    repo: str
    rev: Optional[str] = None
    timeout: float = 10.0  # fast default
    github_token: Optional[str] = None
    gitlab_token: Optional[str] = None
    extra_headers: Optional[Dict[str, str]] = None
    basic_auth: Optional[str] = None  # "user:pass" for HTML/cgit/basic-protected hosts

    _http_api: HttpClient = field(init=False, repr=False)
    _http_html: HttpClient = field(init=False, repr=False)
    _auth_html_header: Dict[str, str] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        # Merge env fallbacks
        self.github_token = self.github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GIT_TOKEN")
        self.gitlab_token = self.gitlab_token or os.environ.get("GITLAB_TOKEN") or os.environ.get("GIT_TOKEN")

        self._http_api = HttpClient(
            timeout=self.timeout,
            user_agent="RemoteLayerScanner/fast-auth-1.0",
            max_retries=1,  # fail fast on rate limit
            max_sleep=1.0,
        )
        self._http_html = HttpClient(
            timeout=min(self.timeout, 8.0),
            user_agent="RemoteLayerScanner/fast-auth-1.0",
            max_retries=0,  # no retries for HTML scraping
            max_sleep=0.0,
        )

        # Build HTML auth header (for cgit/basic) if provided
        self._auth_html_header = dict(self.extra_headers or {})
        ba = _parse_basic_auth(self.basic_auth)
        if ba:
            self._auth_html_header["Authorization"] = ba

    def scan(self) -> None | List[str]:
        """Return a sorted list of relative layer directories (no trailing slash)."""
        import os as _os
        layers = None
        if _os.path.isdir(self.repo):
            layers = self._scan_local(self.repo)
        else:
            parsed = urllib.parse.urlparse(self.repo)
            host = (parsed.netloc or "").lower()
            path = parsed.path or ""
            project = _strip_git_suffix(path.strip("/")) or None

            if "github.com" in host:
                layers = self._scan_github(parsed)
            elif "gitlab.com" in host:
                layers = self._scan_gitlab(parsed)
            elif self._looks_like_cgit(parsed, project):
                layers = self._scan_cgit(parsed)
            else:
                raise ValueError(
                    f"Unsupported remote host {host!r} without cloning. "
                    f"Only GitHub, GitLab, and cgit hosts are supported."
                )

        if not layers:
            return None
        return layers

    # ---------- LOCAL ----------
    @staticmethod
    def _scan_local(root: str) -> List[str]:
        import os
        layers: set[str] = set()
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            if "layer.conf" in filenames and os.path.basename(dirpath) == "conf":
                layer_dir = os.path.relpath(os.path.dirname(dirpath), root)
                if layer_dir == ".":
                    continue
                layers.add(layer_dir.replace("\\", "/"))
        return sorted(layers)

    # ---------- GITHUB ----------
    def _scan_github(self, parsed: urllib.parse.ParseResult) -> List[str]:
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError("GitHub URL must be like https://github.com/{owner}/{repo}[.git]")
        owner = path_parts[0]
        repo = _strip_git_suffix(path_parts[1])

        base_api = f"https://api.github.com/repos/{owner}/{repo}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "RemoteLayerScanner/fast-auth-1.0",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        if self.extra_headers:
            headers.update(self.extra_headers)

        # Resolve SHA
        if self.rev:
            info = self._http_api.get_json(f"{base_api}/commits/{urllib.parse.quote(self.rev)}", headers)
            if not (isinstance(info, dict) and "sha" in info):
                raise ValueError(f"Could not resolve rev {self.rev!r} on GitHub")
            sha = info["sha"]
        else:
            repo_info = self._http_api.get_json(base_api, headers)
            if not (isinstance(repo_info, dict) and "default_branch" in repo_info):
                raise ValueError("Could not determine default branch on GitHub")
            default_branch = repo_info["default_branch"]
            info = self._http_api.get_json(f"{base_api}/commits/{urllib.parse.quote(default_branch)}", headers)
            if not (isinstance(info, dict) and "sha" in info):
                raise ValueError(f"Could not resolve default branch {default_branch!r} on GitHub")
            sha = info["sha"]

        tree = self._http_api.get_json(f"{base_api}/git/trees/{sha}?recursive=1", headers)
        if not (isinstance(tree, dict) and "tree" in tree):
            raise ValueError("Unexpected response from GitHub trees API")

        layers: set[str] = set()
        for entry in tree.get("tree", []):
            p = entry.get("path", "")
            if p.endswith(_CONF_TARGET):
                layer_dir = p[: -len(_CONF_TARGET)].rstrip("/")
                if layer_dir:
                    layers.add(layer_dir)
        return sorted(layers)

    # ---------- GITLAB ----------
    def _scan_gitlab(self, parsed: urllib.parse.ParseResult) -> List[str]:
        full_path = _strip_git_suffix(parsed.path.strip("/"))
        if not full_path:
            raise ValueError("GitLab URL must be like https://gitlab.com/{namespace}/{repo}[.git]")

        project_id = urllib.parse.quote_plus(full_path)
        base_api = f"https://gitlab.com/api/v4/projects/{project_id}"

        headers = {
            "User-Agent": "RemoteLayerScanner/fast-auth-1.0",
            "Accept": "application/json",
        }
        if self.gitlab_token:
            headers["PRIVATE-TOKEN"] = self.gitlab_token
        if self.extra_headers:
            headers.update(self.extra_headers)

        ref = self.rev
        if not ref:
            for candidate in ("main", "master"):
                try:
                    _ = self._http_api.get_json(
                        f"{base_api}/repository/commits/{urllib.parse.quote(candidate)}",
                        headers,
                    )
                    ref = candidate
                    break
                except Exception:  # noqa
                    continue
            if not ref:
                raise ValueError("Could not resolve a default branch on GitLab (tried 'main' and 'master').")

        layers: set[str] = set()
        page = 1
        per_page = 100
        while True:
            url = (
                f"{base_api}/repository/tree"
                f"?ref={urllib.parse.quote(ref)}&recursive=true&per_page={per_page}&page={page}"
            )
            try:
                entries = self._http_api.get_json(url, headers)
            except urllib.error.HTTPError as e:
                if e.code in (400, 422):
                    return self._scan_gitlab_dfs(base_api, headers, ref)
                raise

            if not isinstance(entries, list):
                if isinstance(entries, dict) and entries.get("message"):
                    raise ValueError(f"GitLab API error: {entries.get('message')}")
                break

            for entry in entries:
                if entry.get("type") == "blob":
                    path = entry.get("path", "")
                    if path.endswith(_CONF_TARGET):
                        layer_dir = path[: -len(_CONF_TARGET)].rstrip("/")
                        if layer_dir:
                            layers.add(layer_dir)

            if len(entries) < per_page:
                break
            page += 1

        return sorted(layers)

    def _scan_gitlab_dfs(self, base_api: str, headers: Dict[str, str], ref: str) -> List[str]:
        layers: set[str] = set()
        stack = [""]
        while stack:
            prefix = stack.pop()
            url = (
                f"{base_api}/repository/tree"
                f"?ref={urllib.parse.quote(ref)}&path={urllib.parse.quote(prefix)}&per_page=100"
            )
            entries = self._http_api.get_json(url, headers)
            if not isinstance(entries, list):
                continue
            for e in entries:
                p = e.get("path", "")
                typ = e.get("type")
                if typ == "tree":
                    stack.append(p)
                elif typ == "blob" and p.endswith(_CONF_TARGET):
                    layer_dir = p[: -len(_CONF_TARGET)].rstrip("/")
                    if layer_dir:
                        layers.add(layer_dir)
        return sorted(layers)

    # ---------- CGIT ----------
    def _scan_cgit(self, parsed: urllib.parse.ParseResult) -> List[str]:
        host = parsed.netloc
        project = _strip_git_suffix(parsed.path.strip("/"))
        if not project:
            raise ValueError(f"cgit URL must be like https://{host}/{{project}}")

        base = f"https://{host}/{project}"
        rev = self.rev
        if not rev:
            for candidate in ("master", "main"):
                try:
                    _ = self._http_html.get_text(f"{base}/tree/?h={urllib.parse.quote(candidate)}",
                                                 self._auth_html_header)
                    rev = candidate
                    break
                except Exception:  # noqa
                    continue
            if not rev:
                rev = "master"

        def tree_url(path: str) -> str:
            path = path.strip("/")
            if path:
                return f"{base}/tree/{urllib.parse.quote(path)}?h={urllib.parse.quote(rev)}"
            return f"{base}/tree/?h={urllib.parse.quote(rev)}"

        layers: set[str] = set()
        visited: set[str] = set()
        stack: List[str] = [""]

        re_tree_href = re.compile(r'href="(?:/[^"]+)?/tree/([^"?#]+)\?h=[^"]*"')
        re_plain_href = re.compile(r'href="(?:/[^"]+)?/plain/([^"?#]+)\?h=[^"]*"')

        while stack:
            prefix = stack.pop()
            if prefix in visited:
                continue
            visited.add(prefix)

            url = tree_url(prefix)
            try:
                html = self._http_html.get_text(url, self._auth_html_header)
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    continue
                raise
            except Exception:  # noqa
                continue

            tree_paths = set(re_tree_href.findall(html))
            plain_paths = set(re_plain_href.findall(html))  # files

            for p in tree_paths:
                if p.endswith(_CONF_TARGET):
                    layer_dir = p[: -len(_CONF_TARGET)].rstrip("/")
                    if layer_dir:
                        layers.add(layer_dir)

            for p in tree_paths:
                if prefix and not p.startswith(prefix.rstrip("/") + "/"):
                    continue
                if p in plain_paths:
                    continue
                if p.endswith(_CONF_TARGET):
                    continue
                if "/" in p and p.rsplit("/", 1)[-1].count(".") > 0:
                    continue

                if prefix == "":
                    if "/" not in p:
                        stack.append(p)
                    else:
                        head = p.split("/", 1)[0]
                        if head and head not in visited:
                            stack.append(head)
                else:
                    if p.startswith(prefix.rstrip("/") + "/"):
                        tail = p[len(prefix.rstrip("/")) + 1:]
                        if "/" not in tail:
                            stack.append(p)

        return sorted(layers)

    # ---------- Fast cgit detector (auth-aware) ----------
    def _looks_like_cgit(self, parsed, project: Optional[str]) -> bool:
        base = f"{parsed.scheme}://{parsed.netloc}"
        hdrs = self._auth_html_header

        def ok(_html: str) -> bool:
            return bool(_CGIT_META.search(_html) or _CGIT_CSS.search(_html) or _CGIT_ID.search(_html))

        try:
            if project:
                for h in ("master", "main"):
                    try:
                        html = self._http_html.get_text(f"{base}/{project.strip('/')}/tree/?h={h}", hdrs)
                        if ok(html):
                            return True
                    except Exception:  # noqa
                        continue
                return False
            else:
                html = self._http_html.get_text(base + "/", hdrs)
                return ok(html)
        except Exception:  # noqa
            return False
