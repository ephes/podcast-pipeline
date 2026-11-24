from __future__ import annotations

from collections.abc import Hashable
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class DuplicateYamlKeyError(ConstructorError):
    pass


class UniqueKeySafeLoader(yaml.SafeLoader):
    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._checked_mappings: set[MappingNode] = set()

    def flatten_mapping(self, node: MappingNode) -> None:
        if node in self._checked_mappings:
            return
        self._checked_mappings.add(node)

        # Check each mapping's own keys before SafeLoader applies YAML merge precedence.
        merge_key = object()
        keys: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                key = merge_key
            else:
                if key_node.tag == "tag:yaml.org,2002:value":
                    key_node.tag = "tag:yaml.org,2002:str"
                key = self.construct_object(key_node)
            if not isinstance(key, Hashable):
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark, "unhashable key", key_node.start_mark
                )
            if key in keys:
                raise DuplicateYamlKeyError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key_node.value!r}",
                    key_node.start_mark,
                )
            keys.add(key)
        super().flatten_mapping(node)
