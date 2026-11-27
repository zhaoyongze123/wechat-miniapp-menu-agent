"""Unit tests of the release policy; no network, GitHub runner or app dependencies."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import change_plan as cp


class PolicyTests(unittest.TestCase):
    def test_docs_and_test_only(self):
        doc = cp.classify(["README.md", "docs/updates/README.md"])
        self.assertTrue(doc["docs_only"])
        self.assertFalse(doc["build"])
        test = cp.classify(["app/tests/unit/test_delivery.py"])
        self.assertTrue(test["backend"])
        self.assertFalse(test["release_needed"])
        self.assertFalse(test["build"])
        contract = cp.classify(["app/tests/contract/test_windows.py"])
        self.assertTrue(contract["packaging"])
        self.assertFalse(contract["release_needed"])

    def test_prompt_and_ui(self):
        prompt = cp.classify(["app/ui_tars/prompts/menu.md"])
        self.assertEqual(prompt["risk"], "L1")
        self.assertTrue(prompt["prompts"] and prompt["backend"] and prompt["build"])
        self.assertTrue(prompt["build_windows"])
        self.assertFalse(prompt["build_hub"])
        self.assertEqual(prompt["components"], "windows")
        self.assertFalse(prompt["hub_regression"] or prompt["frontend"])
        ui = cp.classify(["app/web-react/src/features/tasks/Panel.tsx", "app/web/react/index.html"])
        self.assertEqual(ui["risk"], "L2")
        self.assertTrue(ui["frontend"] and ui["build_windows"] and ui["build_hub"])
        self.assertEqual(ui["components"], "both")
        self.assertFalse(ui["hub_regression"])

    def test_component_independent_classification(self):
        hub_only = cp.classify(["deploy/hub/Dockerfile.hub", "app/menu_capture/shared/delivery.py"])
        self.assertTrue(hub_only["build_hub"])
        self.assertFalse(hub_only["build_windows"])
        self.assertEqual(hub_only["components"], "hub")

        win_only = cp.classify(["deploy/windows/MenuCaptureLauncher.py", "app/menu_capture/updates/recovery.py"])
        self.assertTrue(win_only["build_windows"])
        self.assertFalse(win_only["build_hub"])
        self.assertEqual(win_only["components"], "windows")

        shared = cp.classify(["app/menu_capture/api/server.py"])
        self.assertTrue(shared["build_windows"] and shared["build_hub"])
        self.assertEqual(shared["components"], "both")

    def test_reviewed_business_path(self):
        p = cp.classify(["app/menu_capture/vision/purpose_classifier.py"])
        self.assertEqual(p["risk"], "L2")
        self.assertTrue(p["backend"])
        self.assertFalse(p["hub_regression"])

    def test_high_risk_fails_closed(self):
        for path in [
            "app/menu_capture/storage/store.py", "app/menu_capture/api/server.py",
            "app/menu_capture/shared/delivery.py", "app/menu_capture/updates/velopack_backend.py",
            "app/menu_capture/unknown/new.py", "app/uv.lock", "app/ui_tars/adapter.mjs",
            "deploy/hub/compose.yaml", "deploy/hub-update/hub_updater.py",
            "deploy/windows/build.ps1", ".github/workflows/windows-release.yml",
            "unknown.md", "app/web-react/package-lock.json",
        ]:
            with self.subTest(path=path):
                p = cp.classify([path])
                self.assertEqual(p["risk"], "L3")
                self.assertTrue(all(p[f] for f in cp.FLAGS))
                self.assertTrue(p["build"] and p["release_needed"])

    def test_mixed_and_force_and_uncertain(self):
        p = cp.classify(["docs/a.md", "app/ui_tars/prompts/a.md", "app/menu_capture/storage/store.py"])
        self.assertEqual(p["risk"], "L3")
        self.assertTrue(p["hub_regression"])
        force = cp.classify(["README.md"], force_full=True)
        self.assertTrue(force["build"])
        self.assertFalse(force["release_needed"])
        self.assertTrue(all(force[f] for f in cp.FLAGS))
        fail = cp.classify([], uncertain=True)
        self.assertTrue(fail["build"] and fail["release_needed"])
        self.assertTrue(all(fail[f] for f in cp.FLAGS))
        self.assertFalse(cp.classify([])["build"])

    def test_stable_baseline_rejects_preview_and_draft(self):
        with self.assertRaises(ValueError):
            cp.stable_baseline("1.2.0-rc.1")
        with (
            patch.object(cp, "command", return_value=b'{"tagName":"v1.0.0","isDraft":true,"isPrerelease":false}'),
            self.assertRaises(ValueError),
        ):
            cp.stable_baseline("1.1.0")
        with patch.object(cp, "command", return_value=b'{"tagName":"v1.0.0","isDraft":false,"isPrerelease":false}') as cmd:
            with patch.object(cp, "git", return_value="abc"):
                self.assertEqual(cp.stable_baseline("1.1.0"), "abc")
            cmd.assert_any_call("git", "merge-base", "--is-ancestor", "abc", "HEAD")

    def test_failed_baseline_never_becomes_docs_only(self):
        with (
            patch.object(cp, "git", return_value="abc"),
            patch.object(cp, "stable_baseline", side_effect=ValueError),
        ):
            plan = cp.plan_for("release", "1.1.0")
        self.assertEqual(plan["risk"], "L3")
        self.assertTrue(plan["build"])
        self.assertFalse(plan["docs_only"])

    def test_real_git_cumulative_diff_and_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", "-C", tmp, *args], stderr=subprocess.PIPE)
            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            old = root / "app/menu_capture/storage/store.py"
            old.parent.mkdir(parents=True)
            old.write_text("x = 1\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD").decode().strip()
            git("update-ref", "refs/remotes/origin/main", base)
            new = root / "docs/store.md"
            new.parent.mkdir()
            git("mv", str(old.relative_to(root)), str(new.relative_to(root)))
            git("commit", "-qm", "rename")
            (root / "README.md").write_text("docs\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "docs only last commit")
            def command(*args):
                self.assertEqual(args[0], "git")
                return git(*args[1:])
            with patch.object(cp, "command", side_effect=command):
                p = cp.plan_for("pr")
            self.assertIn("app/menu_capture/storage/store.py", p["files"])
            self.assertIn("docs/store.md", p["files"])
            self.assertEqual(p["risk"], "L3")


if __name__ == "__main__":
    unittest.main()
