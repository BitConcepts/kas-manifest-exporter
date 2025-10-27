#!/usr/bin/env python3

from _repo_manifest_loader import load_manifest_from_git as load_manifest_from_git
from _kas_exporter import KASExporter


md = load_manifest_from_git(
    repo_url="https://github.com/varigit/variscite-bsp-platform",
    branch="scarthgap",
    manifest_filename="imx-6.6.52-2.2.0.xml"
)

exporter = KASExporter(md, version=14, path_prefix="sources")
print(exporter.generate_kas_configuration())
