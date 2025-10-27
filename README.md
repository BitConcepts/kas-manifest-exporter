# kas-manifest-exporter

`xml-to-kas` converts [Android repo manifests](https://android.googlesource.com/tools/repo/+/refs/tags/v2.59/docs/manifest-format.md)
into [kas project configuration files](https://kas.readthedocs.io/en/latest/userguide/project-configuration.html#).
It understands the kas [format changelog](https://kas.readthedocs.io/en/latest/format-changelog.html) so you can select
the exact version that matches your build environment.

## Requirements

* Python 3.10+
* [PyYAML](https://pyyaml.org/) (`pip install pyyaml`)
* [pygit2](https://www.pygit2.org/) (`pip install pygit2`)

The tool only uses HTTPS/SSH git access, so ensure the corresponding credentials are configured before invoking the
CLI.

## Command line interface

```
usage: xml-to-kas [-h] [--kas-version KAS_VERSION] [-o OUTPUT] [--path-prefix PATH_PREFIX]
                  [--path-dedup {off,suffix}] [--path-apply-mode {always,missing-only}]
                  [--include-layer [REPO:]LAYER] [--exclude-layer [REPO:]LAYER]
                  [--layer-hint REPO:layer[,layer]] [--machine MACHINE] [--distro DISTRO]
                  [--target TARGET] [--task TASK] [--build-system BUILD_SYSTEM]
                  [--env KEY=VALUE]
                  {from-git,from-file} ...
```

### Sub-commands

* `from-git` – clone a manifest repository and export a kas file from that checkout. Useful when includes
  reference other files in the same repository.
* `from-file` – read a manifest XML from the local filesystem (includes are resolved relative to the manifest).

### Core options

| Option | Description |
| ------ | ----------- |
| `--kas-version` | kas format version to emit. The exporter clamps values to the range supported by kas (1–20). |
| `--path-prefix` | Prepend a directory prefix to repository checkouts inside the generated YAML. |
| `--path-dedup`  | Control how duplicate paths are handled (`off` = error, `suffix` = append `~1`, `~2`, …). |
| `--path-apply-mode` | Apply `--path-prefix` to all repos (`always`) or only when the manifest omits a path (`missing-only`). |
| `-o/--output` | Write YAML to a file. If omitted the configuration is written to STDOUT. |

### Layer controls

The exporter discovers layers by scanning each repository. You can adjust the result:

* `--include-layer [REPO:]LAYER` – keep only matching layers. Match either the full path (`meta-openembedded/meta-oe`)
  or the basename (`meta-oe`). Prefix with `repo-id:` (kas `repos` key) to restrict to a single repository. Repeat to
  keep multiple layers.
* `--exclude-layer [REPO:]LAYER` – remove matching layers using the same matching rules as `--include-layer`.
* `--layer-hint REPO:layer[,layer]` – add layers when remote scanning is incomplete or disabled. Provide a kas repo id
  followed by a comma-separated list of layers.

### Build context overrides

* `--machine`, `--distro`, `--target` – override machine/distro/targets when the manifest does not define them.
* `--task` – set the kas task (requires kas v3+).
* `--build-system` – force the kas `build_system` (requires kas v10+).
* `--env KEY=VALUE` – inject environment variables into the generated file (requires kas v6+).

### Git source options

`from-git` accepts the following additional flags:

| Option | Description |
| ------ | ----------- |
| `repo_url` | Git URL of the manifest repository (HTTPS or SSH). |
| `--branch` | Branch/ref to check out. Defaults to the repository’s default branch. |
| `--manifest` | Specific manifest XML inside the repository. Defaults to `default.xml` and other common filenames. |
| `--workdir` | Parent directory to reuse for cloning (useful for caching). Defaults to a temporary directory. |
| `--keep-checkout` | Preserve the cloned checkout on disk. When omitted, temporary directories are cleaned up automatically. |

`from-file` takes a single positional argument `manifest_path` pointing to a local XML manifest.

### Examples

Convert the default manifest from a remote repository using kas format version 14 and write the YAML to `kas.yml`:

```
python xml_to_kas.py from-git https://github.com/example/manifest-repo.git \
    --branch main --kas-version 14 --output kas.yml
```

Load a local manifest, override the machine, and explicitly list the layers to keep:

```
python xml_to_kas.py from-file ./manifests/default.xml \
    --machine my-machine --include-layer meta-oe --include-layer meta-python
```

## Specification compliance

* **Repo manifest** – The loader understands repo manifest v2.59 including includes, remotes, and revision handling.
* **kas project configuration** – The exporter supports kas format versions 1 through 20. Feature flags (such as
  environment variables, build system, and tasks) are validated against the format changelog so you cannot accidentally
  request fields that are not available in the chosen version.

Refer to the upstream documentation for the complete schema:

* [Repo manifest format (v2.59)](https://android.googlesource.com/tools/repo/+/refs/tags/v2.59/docs/manifest-format.md)
* [kas project configuration guide](https://kas.readthedocs.io/en/latest/userguide/project-configuration.html#)
* [kas format changelog](https://kas.readthedocs.io/en/latest/format-changelog.html)

## Working with Git host rate limits

Public Git hosts such as GitHub apply API throttling to anonymous requests. When cloning manifests from a hosted
repository you may need to authenticate with a personal access token (PAT):

1. Create a PAT in your Git provider account (for GitHub, visit
   https://github.com/settings/tokens and grant at least the `repo` scope for private manifests or `read:packages`/`public_repo` for
   public data).
2. Use the token in the repository URL, e.g. `https://USERNAME:TOKEN@github.com/org/manifest.git`, or rely on the
   standard Git credential helper: `git config --global credential.helper store` and run `git ls-remote` once to cache
   credentials.
3. For CI environments, export the token via `GIT_ASKPASS` or `PYGIT2_SSH_KEY_PASSWORD` as appropriate so pygit2 can
   authenticate without prompting.

Tokens count as credentials, so keep them secret and rotate them regularly. Authenticated requests receive higher rate
limits which keeps `xml-to-kas` from failing mid-export due to HTTP 429 responses.

## Contributing

Issues and pull requests are welcome. Please include the manifest you are working with (or a redacted example) so that
layer discovery and kas version validation can be reproduced.
