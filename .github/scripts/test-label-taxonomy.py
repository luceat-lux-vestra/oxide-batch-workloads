#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name("validate-label-taxonomy.py")
SPEC = importlib.util.spec_from_file_location("validate_label_taxonomy", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class LabelTaxonomyTests(unittest.TestCase):
    def load_valid(self):
        return json.loads(MODULE.TAXONOMY.read_text(encoding="utf-8"))

    def write_and_load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "labels.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return MODULE.load_taxonomy(path)

    def test_repository_taxonomy_is_valid(self):
        data = MODULE.load_taxonomy()
        MODULE.validate_references(data)

    def test_duplicate_label_is_rejected(self):
        data = self.load_valid()
        data["labels"].append(dict(data["labels"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate label name"):
            self.write_and_load(data)

    def test_invalid_color_is_rejected(self):
        data = self.load_valid()
        data["labels"][0]["color"] = "purple"
        with self.assertRaisesRegex(ValueError, "six-digit hex color"):
            self.write_and_load(data)

    def test_empty_managed_prefix_is_rejected(self):
        data = self.load_valid()
        data["managed_prefixes"].append("status:")
        with self.assertRaisesRegex(ValueError, "has no labels"):
            self.write_and_load(data)

    def test_singular_group_must_be_managed(self):
        data = self.load_valid()
        data["singular_groups"].append("status:")
        with self.assertRaisesRegex(ValueError, "subset of managed_prefixes"):
            self.write_and_load(data)

    def test_structural_type_must_reference_defined_type_label(self):
        data = self.load_valid()
        data["structural_types"].append("type:missing")
        with self.assertRaisesRegex(ValueError, "is not defined in labels"):
            self.write_and_load(data)


if __name__ == "__main__":
    unittest.main()
