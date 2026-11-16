import _isolate  # noqa: F401
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_release", ROOT / "scripts" / "check-release.py"
)
check_release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_release)

FILES = (
    "pyproject.toml",
    "uv.lock",
    "npm/package.json",
    "server.json",
    "integrations/hermes/plugin.json",
    "integrations/hermes/mcp.json",
    "integrations/claude-code/.claude-plugin/plugin.json",
    "integrations/claude-code/.mcp.json",
    "mcpb/manifest.json",
    "mcpb/pyproject.toml",
)


class ReleasePinTests(unittest.TestCase):
    def test_every_version_pin_in_the_checkout_agrees(self) -> None:
        found = check_release.pins(ROOT)
        self.assertEqual(len(set(found.values())), 1, found)
        self.assertTrue(all(isinstance(value, str) and value for value in found.values()), found)

    def test_the_checkout_passes_its_own_tag(self) -> None:
        (version,) = set(check_release.pins(ROOT).values())
        self.assertEqual(check_release.check(f"v{version}", ROOT), [])

    def test_a_tag_that_is_not_a_plain_version_is_refused(self) -> None:
        for tag in ("1.5.7", "v1.5", "v1.5.7-rc1", "main", ""):
            with self.subTest(tag=tag):
                self.assertEqual(len(check_release.check(tag, ROOT)), 1)

    def test_a_tag_ahead_of_the_pins_names_every_stale_file(self) -> None:
        problems = check_release.check("v99.0.0", ROOT)
        stale = " ".join(problems)
        for name in ("pyproject.toml", "uv.lock", "npm/package.json", "server.json"):
            self.assertIn(name, stale)
        self.assertIn("integrations/hermes/mcp.json", stale)
        for name in (
            "integrations/claude-code/.claude-plugin/plugin.json",
            "integrations/claude-code/.mcp.json",
            "mcpb/manifest.json",
            "mcpb/pyproject.toml viajante[mcp,browser]",
        ):
            self.assertIn(name, stale)

    def test_one_forgotten_pin_is_the_only_one_named(self) -> None:
        (version,) = set(check_release.pins(ROOT).values())
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            for name in FILES:
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / name, tree / name)
            plugin = tree / "integrations/hermes/plugin.json"
            data = json.loads(plugin.read_text(encoding="utf-8"))
            data["version"] = "0.0.1"
            plugin.write_text(json.dumps(data), encoding="utf-8")
            problems = check_release.check(f"v{version}", tree)
        self.assertEqual(len(problems), 1)
        self.assertIn("integrations/hermes/plugin.json", problems[0])


if __name__ == "__main__":
    unittest.main()
