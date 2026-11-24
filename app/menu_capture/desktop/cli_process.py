"""启动并回收本轮的 UiPath CLI 进程，保存启动证据与构建缓存。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from ..core.runtime_files import atomic_write_json as _atomic_write_json

from .uipath_cli import cli_entry


class RobotCliProcess:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.process_factory = subprocess.Popen
        self.cli_entry = cli_entry
        self.debug_log = None
        self.run_id = None
        self.skip_build_requested = False
        self.source_fingerprint = None
        self.build_cache_path = None
        self.launch_params = None
        self.reset()

    def reset(self) -> None:
        """清除已回收进程的记录，下一次启动使用自己的 IPC 目录。"""
        self.process = None
        self.metadata_path = self.log_path = self.args_path = None
        self.info = None
        self._reader_thread = None


    def launch(self, run_directory: Path, settings, profile: dict, *, task_count: int | None = None,
                    timeout_seconds: float | None = None, allow_skip_build: bool = True, run_id: str | None = None) -> None:
        """只启动当前这轮自己的 Main.xaml 进程，并把 CLI 工件留在私有 run 目录。"""
        self.run_id = run_id
        entry = self.cli_entry()
        if not entry:
            raise ValueError("未找到 UiPath CLI（uip）；请确认已安装并位于 PATH")
        args_path = run_directory / "args.json"
        log_path = self.debug_log.cli_log_path(self.run_id or run_directory.name) if self.debug_log else run_directory / "cli.log"
        metadata_path = run_directory / "cli-process.json"
        _atomic_write_json(args_path, {"in_RunDirectory": str(run_directory)})

        environment = os.environ.copy()
        dotnet_root = Path.home() / ".dotnet"
        if sys.platform != "win32" and dotnet_root.is_dir():
            environment["DOTNET_ROOT"] = str(dotnet_root)
            environment["PATH"] = str(dotnet_root) + os.pathsep + environment.get("PATH", "")
        else:
            dotnet_path = shutil.which("dotnet", path=environment.get("PATH"))
            if dotnet_path:
                environment.setdefault("DOTNET_ROOT", str(Path(dotnet_path).resolve().parent))
        environment["UIPATH_CLI_DISABLE_VERSION_SYNC"] = "true"

        startup_timeout = 300
        max_tasks = task_count if task_count is not None else len(profile.get("selected_task_ids", []))
        # 人工标注按人工节奏进行，不能用自动采集的单店上限计算 CLI 生命周期。
        cli_timeout = (int(timeout_seconds) if timeout_seconds else
                       settings.capture.max_task_seconds * max_tasks + startup_timeout + 60)
        command = [
            *entry, "rpa", "run",
            "--project-dir", str(self.project_dir),
            "--file-path", "Main.xaml",
            "--input-arguments-file", str(args_path),
            "--timeout", str(cli_timeout),
            "--output", "json",
        ]
        self.source_fingerprint = self.build_fingerprint()
        self.build_cache_path = Path(settings.output_dir).expanduser().resolve() / ".uipath-build" / "state.json"
        self.skip_build_requested = allow_skip_build and self.build_cache_matches()
        if self.skip_build_requested:
            # 源文件与上次成功构建一致，直接复用 .local/install 产物。
            command.append("--skip-build")
        self.launch_params = (run_directory, settings, profile, task_count, timeout_seconds)
        log_stream = None
        try:
            if self.debug_log:
                process = self.process_factory(
                    command, cwd=str(self.project_dir), env=environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    shell=False, close_fds=True,
                )
            else:
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                log_stream = os.fdopen(fd, "ab")
                process = self.process_factory(
                    command, cwd=str(self.project_dir), env=environment,
                    stdin=subprocess.DEVNULL, stdout=log_stream, stderr=subprocess.STDOUT,
                    shell=False, close_fds=True,
                )
        except Exception as exc:
            if log_stream:
                log_stream.close()
            _atomic_write_json(metadata_path, {
                "pid": None, "exit_code": None, "status": "start_failed",
                "error_type": type(exc).__name__, "log": str(log_path),
            })
            raise ValueError(f"UiPath CLI 启动失败（{type(exc).__name__}）") from None
        finally:
            if log_stream:
                log_stream.close()

        self.process = process
        self.args_path, self.log_path, self.metadata_path = args_path, log_path, metadata_path
        self.info = {"pid": int(process.pid), "exit_code": None, "status": "running"}
        if self.debug_log and getattr(process, "stdout", None) is not None:
            self._reader_thread = threading.Thread(
                target=self._drain_output, args=(process.stdout, str(self.run_id or run_directory.name)),
                name="uipath-cli-log-reader", daemon=True,
            )
            self._reader_thread.start()
        self.write_metadata()

    def _drain_output(self, stream, run_key: str) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                self.debug_log.add_cli_output(run_key, chunk)
        except (OSError, ValueError):
            self.debug_log.emit("uipath_cli_output_read_error", run_id=run_key)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def write_metadata(self) -> None:
        if self.metadata_path is None or self.info is None:
            return
        value = dict(self.info)
        value.update({
            "args_file": str(self.args_path),
            "log_file": str(self.log_path),
            "project_dir": str(self.project_dir),
        })
        _atomic_write_json(self.metadata_path, value)

    def build_fingerprint(self) -> str:
        """源文件内容指纹：Main.xaml 等任一改动都必须重新构建。"""
        digest = hashlib.sha256()
        sources = sorted(self.project_dir.glob("*.xaml")) + [self.project_dir / "project.json"]
        for path in sources:
            if not path.is_file():
                continue
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                return ""
            digest.update(b"\0")
        return digest.hexdigest()

    def build_cache_matches(self) -> bool:
        if not self.source_fingerprint or self.build_cache_path is None:
            return False
        if not (self.project_dir / ".local" / "install").is_dir():
            return False
        try:
            cached = json.loads(self.build_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(cached, dict) and cached.get("fingerprint") == self.source_fingerprint

    def record_build_cache(self) -> None:
        """握手成功后确认产物可用，供下一轮复用。"""
        if not self.source_fingerprint or self.build_cache_path is None:
            return
        try:
            _atomic_write_json(self.build_cache_path, {
                "fingerprint": self.source_fingerprint,
                "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        except OSError:
            return

    def invalidate_build_cache(self) -> None:
        if self.build_cache_path is None:
            return
        try:
            self.build_cache_path.unlink(missing_ok=True)
        except OSError:
            return

    def terminate(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except (OSError, subprocess.SubprocessError):
                return
        finally:
            reader = self._reader_thread
            if reader is not None:
                reader.join(timeout=1)
            self._reader_thread = None
            self.process = None

    def reap(self) -> None:
        """有界回收本控制器创建的 CLI PID；不查找或终止 Studio/其他进程。"""
        process = self.process
        if process is None:
            return
        status = "exited"
        try:
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                status = "terminated"
                process.terminate()
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    status = "killed"
                    process.kill()
                    code = process.wait(timeout=5)
            self.info.update(exit_code=int(code) if code is not None else process.poll(), status=status)
        except Exception as exc:
            self.info.update(exit_code=process.poll(), status=f"reap_error:{type(exc).__name__}")
        finally:
            reader = self._reader_thread
            if reader is not None:
                reader.join(timeout=2)
            if self.debug_log and self.run_id:
                self.debug_log.finish_cli_output(self.run_id)
            self.write_metadata()
