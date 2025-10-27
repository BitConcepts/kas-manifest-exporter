from typing import Any
import yaml


class YamlDumper(yaml.SafeDumper):
    """YAML dumper that renders None as an empty scalar (no 'null')."""


def _represent_none(dumper: yaml.SafeDumper, _: Any):
    # Empty scalar for nulls -> prints as "key:" with no value
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


# Register the representer once for this dumper
YamlDumper.add_representer(type(None), _represent_none)
