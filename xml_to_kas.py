#!/usr/bin/env python3
"""Simple CLI for exporting repo manifests to kas configuration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from _kas_exporter import KASExporter
from _repo_manifest_loader import (
    LibGitError,
    load_manifest_from_file,
    load_manifest_from_git,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export a repo XML manifest (local file or remote git) into a kas YAML file."
        )
    )

    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--manifest-file",
        help="Path to a local repo manifest XML file.",
    )
    src.add_argument(
        "--repo-url",
        help="Git URL pointing to a repo manifest repository.",
    )

    parser.add_argument(
        "--branch",
        help="Branch or ref to checkout when using --repo-url.",
    )
    parser.add_argument(
        "--manifest-filename",
        help="Manifest filename inside the repo (defaults to standard manifest names).",
    )
    parser.add_argument(
        "--workdir",
        help="Optional working directory for remote git cloning.",
    )
    parser.add_argument(
        "--keep-checkout",
        action="store_true",
        help="Keep the temporary git checkout on disk when using --repo-url.",
    )
    parser.add_argument(
        "--version",
        dest="kas_version",
        type=int,
        default=14,
        help="kas format version to emit (default: 14).",
    )
    parser.add_argument(
        "--path-prefix",
        help="Optional path prefix applied to repo paths.",
    )
    parser.add_argument(
        "--path-dedup",
        choices=["off", "suffix"],
        default="off",
        help="Handle duplicate paths by failing ('off') or suffixing ('suffix').",
    )
    parser.add_argument(
        "--path-apply-mode",
        choices=["always", "missing-only"],
        default="always",
        help="Apply the prefix to all repos or only ones missing explicit paths.",
    )
    parser.add_argument(
        "--include-layer",
        action="append",
        default=[],
        metavar="LAYER",
        help=(
            "Add a detected layer to the kas file. "
            "Use multiple times. Accepts either 'layer-name' or 'repo-id:layer-path'."
        ),
    )
    parser.add_argument(
        "--include-all-layers",
        action="store_true",
        help="Include every detected layer for each repo.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write kas YAML to a file instead of stdout.",
    )

    return parser


def _load_manifest(args: argparse.Namespace) -> dict:
    if args.manifest_file:
        if args.manifest_filename:
            raise ValueError("--manifest-filename cannot be used with --manifest-file")
        return load_manifest_from_file(args.manifest_file)

    return load_manifest_from_git(
        repo_url=args.repo_url,
        branch=args.branch,
        manifest_filename=args.manifest_filename,
        workdir=args.workdir,
        keep_checkout=args.keep_checkout,
    )


def _write_output(path: str, content: str) -> None:
    out_path = Path(path)
    out_path.write_text(content)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        manifest = _load_manifest(args)
    except (ValueError, FileNotFoundError, LibGitError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    exporter = KASExporter(
        manifest,
        version=args.kas_version,
        path_prefix=args.path_prefix,
        path_dedup=args.path_dedup,
        path_apply_mode=args.path_apply_mode,
        include_layers=args.include_layer,
        include_all_layers=args.include_all_layers,
    )

    try:
        kas_yaml = exporter.generate_kas_configuration()
    except Exception as exc:  # noqa: BLE001 - surfacing message to CLI
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        _write_output(args.output, kas_yaml)
    else:
        sys.stdout.write(kas_yaml)
        if not kas_yaml.endswith("\n"):
            sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
