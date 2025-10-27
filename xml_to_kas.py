#!/usr/bin/env python3
"""Command line tool to convert Repo manifests into kas configuration files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from _kas_exporter import KASExporter, _DEFAULT_VERSION
from _repo_manifest_loader import (
    LibGitError,
    load_manifest_from_file,
    load_manifest_from_git,
)


class CLIError(RuntimeError):
    """Raised when user supplied CLI arguments are invalid."""


def _coerce_version(value: Optional[str]) -> int:
    if value is None:
        return _DEFAULT_VERSION
    text = str(value).strip()
    if not text:
        return _DEFAULT_VERSION
    if text in {"0.10", "0", "1.0"}:
        return 1
    try:
        num = int(float(text))
    except ValueError as exc:  # pragma: no cover - defensive
        raise CLIError(f"Invalid kas version value: {value!r}") from exc
    return max(1, min(20, num))


def _parse_env(values: Iterable[str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for raw in values:
        key, sep, val = raw.partition("=")
        key = key.strip()
        if not sep:
            raise CLIError(f"--env requires KEY=VALUE pairs, got: {raw!r}")
        if not key:
            raise CLIError(f"Environment variable name missing in: {raw!r}")
        env[key] = val
    return env


def _parse_layer_hints(values: Iterable[str]) -> Dict[str, List[str]]:
    hints: Dict[str, List[str]] = {}
    for raw in values:
        repo, sep, layers = raw.partition(":")
        repo = repo.strip()
        if not sep or not repo:
            raise CLIError(
                "--layer-hint expects REPO:layer[,layer...] syntax (e.g. meta-openembedded:meta-oe)"
            )
        layer_names = [item.strip() for item in layers.split(",") if item.strip()]
        if not layer_names:
            raise CLIError(f"--layer-hint requires at least one layer name in: {raw!r}")
        hints.setdefault(repo, [])
        for layer in layer_names:
            if layer not in hints[repo]:
                hints[repo].append(layer)
    return hints


def _split_targets(target_args: Iterable[str]) -> List[str]:
    targets: List[str] = []
    for raw in target_args:
        if not raw:
            continue
        pieces = [token.strip() for token in raw.replace(",", " ").split()]  # allow comma or space
        targets.extend([token for token in pieces if token])
    return targets


def _apply_overrides(manifest: Dict[str, Any], args: argparse.Namespace, env: Dict[str, str]) -> None:
    if args.machine:
        manifest["machine"] = args.machine
    if args.distro:
        manifest["distro"] = args.distro
    targets = _split_targets(args.targets or [])
    if targets:
        manifest["targets"] = targets
    if args.task:
        manifest["task"] = args.task
    if args.build_system:
        manifest["build_system"] = args.build_system
    if env:
        merged_env = dict(manifest.get("env") or {})
        merged_env.update(env)
        manifest["env"] = merged_env


def _load_manifest(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "from-git":
        return load_manifest_from_git(
            repo_url=args.repo_url,
            branch=args.branch,
            manifest_filename=args.manifest_filename,
            workdir=args.workdir,
            keep_checkout=args.keep_checkout,
        )
    if args.command == "from-file":
        return load_manifest_from_file(args.manifest_path)
    raise CLIError("Unknown command")


def _build_parser() -> argparse.ArgumentParser:
    description = (
        "Convert Android repo manifests (XML) into kas project configuration files (YAML).\n"
        "\n"
        "Documentation: repo manifest format v2.59 and kas configuration schema v14+."
    )
    parser = argparse.ArgumentParser(
        prog="xml-to-kas",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--kas-version",
        default=str(_DEFAULT_VERSION),
        help="kas file format version to emit (default: %(default)s).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write output YAML to this file instead of stdout.",
    )
    parser.add_argument(
        "--path-prefix",
        help="Prefix to prepend to each repo path in the generated configuration.",
    )
    parser.add_argument(
        "--path-dedup",
        default="off",
        choices=["off", "suffix"],
        help="Handle duplicate repo paths by either failing ('off') or appending suffixes ('suffix').",
    )
    parser.add_argument(
        "--path-apply-mode",
        default="always",
        choices=["always", "missing-only"],
        help="Apply --path-prefix to all repos or only when the manifest omits explicit paths.",
    )
    parser.add_argument(
        "--include-layer",
        action="append",
        default=[],
        metavar="[REPO:]LAYER",
        help=(
            "Only keep matching layers. Names can be full paths (meta-openembedded/meta-oe) or basenames. "
            "Prefix with REPO: to target a single repo."
        ),
    )
    parser.add_argument(
        "--exclude-layer",
        action="append",
        default=[],
        metavar="[REPO:]LAYER",
        help="Exclude matching layers. Uses the same matching rules as --include-layer.",
    )
    parser.add_argument(
        "--layer-hint",
        action="append",
        default=[],
        metavar="REPO:layer[,layer]",
        help="Add extra layer names for a repo when discovery misses them.",
    )
    parser.add_argument("--machine", help="Override MACHINE in the resulting kas file.")
    parser.add_argument("--distro", help="Override DISTRO in the resulting kas file.")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        metavar="TARGET",
        help="Add a kas target. Repeat or supply comma separated values for multiple targets.",
    )
    parser.add_argument("--task", help="Set the kas task field (kas v3+).")
    parser.add_argument(
        "--build-system",
        help="Explicitly set the kas build_system field (kas v10+).",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Inject environment variables into the kas file (kas v6+).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    git_parser = subparsers.add_parser(
        "from-git",
        help="Clone a repo manifest from a Git repository before exporting.",
    )
    git_parser.add_argument("repo_url", help="Git URL containing a repo-style manifest repository.")
    git_parser.add_argument("--branch", help="Branch to check out (defaults to the repository's default branch).")
    git_parser.add_argument(
        "--manifest",
        dest="manifest_filename",
        help="Manifest file to parse inside the repository (default: autodetect).",
    )
    git_parser.add_argument(
        "--workdir",
        help="Optional directory to reuse for cloning (defaults to a temporary directory).",
    )
    git_parser.add_argument(
        "--keep-checkout",
        action="store_true",
        help="Keep the cloned checkout on disk instead of cleaning it up.",
    )

    file_parser = subparsers.add_parser(
        "from-file",
        help="Load a local manifest XML file directly from disk.",
    )
    file_parser.add_argument("manifest_path", help="Path to the repo manifest XML file.")

    return parser


def _validate_capabilities(version: int, args: argparse.Namespace, env: Dict[str, str]) -> None:
    if env and version < 6:
        raise CLIError("--env requires kas format version 6 or newer")
    if args.task and version < 3:
        raise CLIError("--task requires kas format version 3 or newer")
    if args.build_system and version < 10:
        raise CLIError("--build-system requires kas format version 10 or newer")


def _write_output(text: str, destination: Optional[Path]) -> None:
    if destination is None:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        env = _parse_env(args.env)
        layer_hints = _parse_layer_hints(args.layer_hint)
        kas_version = _coerce_version(args.kas_version)
        _validate_capabilities(kas_version, args, env)
    except CLIError as exc:
        parser.error(str(exc))

    try:
        manifest = _load_manifest(args)
        _apply_overrides(manifest, args, env)
        exporter = KASExporter(
            manifest,
            version=kas_version,
            path_prefix=args.path_prefix,
            path_dedup=args.path_dedup,
            path_apply_mode=args.path_apply_mode,
            include_layers=args.include_layer or None,
            exclude_layers=args.exclude_layer or None,
            layer_hints=layer_hints or None,
        )
        kas_text = exporter.generate_kas_configuration()
        _write_output(kas_text, args.output)
    except (CLIError, FileNotFoundError, LibGitError, ValueError) as exc:
        print(f"xml-to-kas: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - manual execution
    sys.exit(main())
