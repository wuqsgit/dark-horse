import subprocess
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "start.sh"


class StartScriptTest(unittest.TestCase):
    def test_script_restarts_every_service_and_cleans_stale_processes(self):
        script = START_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("stop_process_tree()", script)
        self.assertIn("stop_matching_processes()", script)
        self.assertIn("stop_port()", script)
        self.assertIn(".venv/Scripts/python.exe", script)
        self.assertIn(".venv/bin/python", script)

        for service in (
            "Pipeline",
            "Alpha Pipeline",
            "Engine",
            "Alpha Engine",
            "AI Entry Quality",
            "Trader",
            "API",
            "Frontend",
        ):
            self.assertIn(f'"{service}"', script)

    def test_script_has_valid_bash_syntax(self):
        bash = "bash"
        if os.name == "nt":
            git_bash = Path("C:/Program Files/Git/bin/bash.exe")
            if git_bash.exists():
                bash = str(git_bash)
        result = subprocess.run(
            [bash, "-n", str(START_SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_git_bash_uses_msys_pid_check_and_powershell_cleanup_helper(self):
        script = START_SCRIPT.read_text(encoding="utf-8")
        helper = ROOT / "scripts" / "stop_dark_horse_processes.ps1"

        process_check = script[script.index("process_is_running()") : script.index("stop_process_tree()")]
        self.assertLess(process_check.index('kill -0 "$pid"'), process_check.index("tasklist.exe"))
        self.assertIn("stop_dark_horse_processes.ps1", script)
        self.assertTrue(helper.exists())

    def test_api_initializes_database_before_writer_services_start(self):
        script = START_SCRIPT.read_text(encoding="utf-8")
        api_start = script.index(
            'start_service "API" "$RUNTIME_DIR/alphadog_api.pid"'
        )
        pipeline_start = script.index(
            'start_service "Pipeline" "$RUNTIME_DIR/alphadog_pipeline.pid"'
        )

        self.assertLess(api_start, pipeline_start)
        self.assertIn("wait_for_port 8000", script)
        self.assertIn("wait_for_port 8010", script)


if __name__ == "__main__":
    unittest.main()
