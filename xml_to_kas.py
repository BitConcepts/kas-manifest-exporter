from repo_manifest_loader import load_manifest_from_git_libgit2 as load_manifest_from_git
from kas_exporter import KASExporter

md = load_manifest_from_git(
    repo_url="https://github.com/varigit/variscite-bsp-platform",
    branch="scarthgap",
    manifest_filename="imx-6.6.52-2.2.0.xml"
)

exporter = KASExporter(md, version=14, path_prefix="sources")
print(exporter.generate_kas_configuration())
