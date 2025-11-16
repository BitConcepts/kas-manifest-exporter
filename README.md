# kas-manifest-exporter

Export Android/Yocto repo manifests to KAS YAML — supports all format versions and Git-based sources.

## Usage

```
python xml_to_kas.py --repo-url https://github.com/example/manifest.git \
    --branch scarthgap --manifest-filename default.xml --path-prefix sources \
    --include-all-layers -o kas.yml
```

Or convert a local manifest file:

```
python xml_to_kas.py --manifest-file ./default.xml --version 14 > kas.yml
```

Layers are always detected for every repo. By default, none of them are written
to the resulting kas file. Use `--include-layer [repo-id:]path/to/layer` multiple
times to cherry-pick layers, or `--include-all-layers` to add every detected
layer.
