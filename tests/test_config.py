"""Offline checks for literal, non-executing API-key configuration."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import redline


class KeyConfigurationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / ".env"

    def test_environment_takes_precedence_even_if_file_is_invalid(self):
        self.path.write_text("OPENAI_API_KEY=\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SYNTHETIC_ENV"}, clear=True):
            self.assertEqual(redline.read_api_key(self.path), "SYNTHETIC_ENV")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=True):
            self.assertEqual(redline.read_api_key(self.path), "")

    def test_plain_quoted_and_bom_files(self):
        for value in ("SYNTHETIC_FILE", "'SYNTHETIC_FILE'", '\"SYNTHETIC_FILE\"'):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                self.path.write_text("\ufeff# local config\nOTHER=value\n OPENAI_API_KEY = " + value + "\n", encoding="utf-8")
                self.assertEqual(redline.read_api_key(self.path), "SYNTHETIC_FILE")

    def test_missing_file_or_assignment_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(redline.read_api_key(self.path))
            self.path.write_text("# no key\nOTHER=value\n")
            self.assertIsNone(redline.read_api_key(self.path))

    def test_malformed_or_duplicate_assignments_fail_without_echoing_values(self):
        values = ["OPENAI_API_KEY", "OPENAI_API_KEY=", "OPENAI_API_KEY='SYNTHETIC_SECRET", "OPENAI_API_KEY=SYNTHETIC_SECRET\nOPENAI_API_KEY=OTHER"]
        for value in values:
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                self.path.write_text(value)
                with self.assertRaises(ValueError) as error:
                    redline.read_api_key(self.path)
                self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))

    def test_values_are_literal_and_do_not_execute_shell_commands(self):
        marker = self.path.parent / "unexpected"
        literal = "$(touch " + str(marker) + ")"
        self.path.write_text("OPENAI_API_KEY=" + literal + "\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(redline.read_api_key(self.path), literal)
        self.assertFalse(marker.exists())
