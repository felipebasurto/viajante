"""Sweep configuration defaults and strict validation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.runtime import get_runtime_info
from viajante.sweep_config import SweepConfig, get_sweep_config


class SweepConfigTests(unittest.TestCase):
    def test_default_mode_uses_standard_concurrency(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_sweep_config(), SweepConfig(mode="standard", concurrency=8))

    def test_conservative_mode_uses_lower_concurrency(self) -> None:
        with patch.dict(os.environ, {"VIAJANTE_SWEEP_MODE": "conservative"}):
            config = get_sweep_config()
        self.assertEqual((config.mode, config.concurrency), ("conservative", 2))

    def test_invalid_mode_is_rejected_without_normalizing(self) -> None:
        for value in ("", "Standard", "fast", " conservative"):
            with self.subTest(value=value), patch.dict(os.environ, {"VIAJANTE_SWEEP_MODE": value}):
                with self.assertRaisesRegex(ValueError, "VIAJANTE_SWEEP_MODE"):
                    get_sweep_config()

    def test_runtime_info_reports_sweep_configuration(self) -> None:
        with patch.dict(os.environ, {"VIAJANTE_SWEEP_MODE": "conservative"}):
            info = get_runtime_info()
        self.assertEqual(info["sweep_mode"], "conservative")
        self.assertEqual(info["sweep_concurrency"], 2)


if __name__ == "__main__":
    unittest.main()
