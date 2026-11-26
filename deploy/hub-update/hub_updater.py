"""Hub 宿主机镜像更新执行器。

为什么不放进 Hub 容器：容器会被更新替换，且业务容器不能挂载 Docker Socket。
本进程运行在宿主机，仅通过受限 HTTP 接口接收「应用某个已校验镜像」的意图，
自己完成下载/校验/加载/切换/健康核对/回滚，所有动作写入状态文件与审计日志。

安全边界：
- 只接受固定镜像仓库 `<IMAGE_REPO>:<version>`，拒绝任意镜像、命令、路径；
- 归档只允许来自受信任 GitHub 域名，或位于 `ARCHIVE_DIR` 内且名称不含路径分隔符；
- 完整性以 SHA-256 为准，大小不得超过上限，下载前检查磁盘余量；
- 只对固定的 compose 文件与 project 执行 `up -d`，从不执行 `down -v`、prune；
- 同一时刻只允许一个更新/回滚事务，事务状态落盘，进程退出后可恢复判定。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
# OCI 分层交付：固定 GHCR 仓库、固定 linux/amd64 平台与不可变 digest。
REGISTRY_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OCI_PLATFORM = "linux/amd64"
ALLOWED_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
ASSET_PATH_RE = re.compile(r"/releases/download/[^/]+/[^/]+$")
ACTIVE_STATUSES = {"pulling", "downloading", "loading", "switching", "verifying", "rolling_back"}
# 单个镜像归档上限 2 GiB；超过说明不是本项目发布物。
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
# 下载/加载额外需要的磁盘余量，避免写到一半把宿主机写满。
DISK_MARGIN_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 1800
DOCKER_TIMEOUT_SECONDS = 900
SCHEMA_PROBE = r'''import json, pathlib, sqlite3, sys
def q(value): return '"' + value.replace('"', '""') + '"'
root = pathlib.Path(sys.argv[1])
data = root / 'data'
path = data / 'tasks.sqlite3'
if data.is_symlink() or path.is_symlink(): raise RuntimeError('Hub 数据库路径不能是符号链接')
if not path.exists(): print(json.dumps({'exists': False, 'tables': [], 'objects': []}))
else:
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    tables = []
    for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name"):
        indexes = []
        for row in db.execute('PRAGMA index_list(' + q(name) + ')'):
            index_name = row[1]
            index_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)).fetchone()[0]
            columns = [item[2] for item in db.execute('PRAGMA index_info(' + q(index_name) + ')')]
            indexes.append({'name': index_name, 'unique': int(row[2]), 'origin': row[3], 'partial': int(row[4]), 'columns': columns, 'sql': index_sql or ''})
        tables.append({'name': name, 'sql': sql or '', 'columns': [list(row) for row in db.execute('PRAGMA table_xinfo(' + q(name) + ')')], 'indexes': indexes, 'foreign_keys': [list(row) for row in db.execute('PRAGMA foreign_key_list(' + q(name) + ')')]})
    objects = [list(row) for row in db.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('view','trigger') ORDER BY type,name")]
    print(json.dumps({'exists': True, 'tables': tables, 'objects': objects}))
    db.close()
'''


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _normalize_sql(sql: str) -> str:
    """只折叠 SQL 引号之外的空白，不能改写字符串/标识符内部内容。"""
    result = []
    quote = ""
    pending_space = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            result.append(char)
            if quote == "]":
                if char == "]":
                    quote = ""
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                    result.append(sql[index])
                else:
                    quote = ""
        elif char in "'\"`[":
            if pending_space and result:
                result.append(" ")
            pending_space = False
            quote = "]" if char == "[" else char
            result.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and result:
                result.append(" ")
            pending_space = False
            result.append(char)
        index += 1
    return "".join(result).strip()


def _table_definitions(sql: str) -> tuple[dict[str, str], list[str], str]:
    """拆分 CREATE TABLE 顶层定义，区分原有列定义与表级约束。"""
    opening = sql.find("(")
    if opening < 0:
        return {}, [], ""
    chunks: list[str] = []
    start = opening + 1
    depth = 0
    quote = ""
    index = start
    closing = -1
    while index < len(sql):
        char = sql[index]
        if quote:
            if quote == "]":
                if char == "]":
                    quote = ""
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
        elif char in "'\"`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                closing = index
                chunks.append(sql[start:index].strip())
                break
            depth -= 1
        elif char == "," and depth == 0:
            chunks.append(sql[start:index].strip())
            start = index + 1
        index += 1

    columns: dict[str, str] = {}
    constraints = []
    for chunk in chunks:
        if not chunk:
            continue
        if re.match(r"(?is)^(?:CONSTRAINT\s+.+?\s+)?(?:PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY)\b", chunk):
            constraints.append(_normalize_sql(chunk))
            continue
        match = re.match(r'^(?:"((?:[^"]|"")*)"|`([^`]*)`|\[([^\]]+)\]|([^\s]+))\s*(.*)$', chunk, re.DOTALL)
        if match:
            name = next(value for value in match.groups()[:4] if value is not None)
            name = name.replace('""', '"')
            columns[name] = _normalize_sql(chunk)
    table_options = _normalize_sql(sql[closing + 1:]) if closing >= 0 else ""
    return columns, constraints, table_options


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """只读查询 Windows 进程状态；无法确认时保守认为事务仍在运行。"""
    import ctypes
    from ctypes import wintypes

    # 零超时等待只需要 SYNCHRONIZE 权限，无需查询进程详情或终止权限。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        # ERROR_INVALID_PARAMETER 表示 PID 不存在。拒绝访问或其他未知错误时保守阻止并发事务。
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == 258:  # WAIT_TIMEOUT: 进程仍在运行
            return True
        # WAIT_OBJECT_0 表示已退出；WAIT_FAILED 或未知结果均保守保留活动状态。
        return result != 0
    finally:
        kernel32.CloseHandle(handle)


class UpdateError(ValueError):
    pass


class ValidationError(UpdateError):
    """请求体本身不合法（字段、格式、路径穿越）；与事务状态冲突区分，按 400 返回。"""


class Config:
    """执行器配置全部来自环境变量；缺省值只适用于默认部署。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.token = str(env.get("HUB_UPDATE_TOKEN") or "")
        self.compose_file = Path(env.get("HUB_UPDATE_COMPOSE_FILE") or "/opt/menu-capture/deploy/hub/compose.yaml")
        self.project = str(env.get("HUB_UPDATE_PROJECT") or "menu-capture")
        self.service = str(env.get("HUB_UPDATE_SERVICE") or "hub")
        self.container = str(env.get("HUB_UPDATE_CONTAINER") or "")
        self.image_repo = str(env.get("HUB_UPDATE_IMAGE_REPO") or "menu-capture-hub")
        # 留空即关闭 OCI 分层拉取；配置后只允许该固定仓库，不接受请求方任意指定镜像。
        self.registry_repo = str(env.get("HUB_UPDATE_REGISTRY_REPO") or "").strip().lower()
        self.data_volume = str(env.get("HUB_UPDATE_DATA_VOLUME") or "")
        self.state_dir = Path(env.get("HUB_UPDATE_STATE_DIR") or "/var/lib/menu-capture-updater")
        self.archive_dir = Path(env.get("HUB_UPDATE_ARCHIVE_DIR") or str(self.state_dir / "archives"))
        self.health_url = str(env.get("HUB_UPDATE_HEALTH_URL") or "http://127.0.0.1:8766")
        # Hub 的 /api/version 需要管理员 Basic 认证；Worker 不需要，凭据为空时自动省略。
        self.health_user = str(env.get("HUB_UPDATE_HEALTH_USER") or "")
        self.health_password = str(env.get("HUB_UPDATE_HEALTH_PASSWORD") or "")
        self.bind = str(env.get("HUB_UPDATE_BIND") or "127.0.0.1")
        self.port = int(env.get("HUB_UPDATE_PORT") or "8767")
        self.allowed_clients = [
            item.strip() for item in str(env.get("HUB_UPDATE_ALLOWED_CLIENTS") or "127.0.0.1/32").split(",")
            if item.strip()
        ]
        self.health_timeout = float(env.get("HUB_UPDATE_HEALTH_TIMEOUT") or "240")

    def image_ref(self, version: str) -> str:
        return f"{self.image_repo}:{version}"

    @property
    def oci_enabled(self) -> bool:
        return bool(self.registry_repo)

    def validate(self) -> None:
        if not self.token or len(self.token) < 16:
            raise UpdateError("HUB_UPDATE_TOKEN 必须配置且不少于 16 个字符")
        if not self.compose_file.is_file():
            raise UpdateError(f"找不到 compose 文件：{self.compose_file}")
        if ":" in self.image_repo.rsplit("/", 1)[-1]:
            raise UpdateError("HUB_UPDATE_IMAGE_REPO 不能自带版本标签")
        if self.registry_repo and not REGISTRY_RE.fullmatch(self.registry_repo):
            raise UpdateError("HUB_UPDATE_REGISTRY_REPO 必须是 ghcr.io/<org>/<repo> 形式")


def _client_allowed(ip: str, networks: list[str]) -> bool:
    """仅允许配置的网段访问，避免把宿主机执行器暴露给任意网络。"""
    import ipaddress

    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in networks:
        try:
            if address in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def validate_payload(payload: Any, config: Config) -> dict:
    """把外部请求收敛成唯一契约；任何不认识的字段直接拒绝。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    allowed = {"version", "confirm", "sha256", "size", "url", "archive", "image", "digest"}
    extra = set(payload) - allowed
    if extra:
        raise ValidationError(f"不支持字段：{', '.join(sorted(extra))}")
    version = str(payload.get("version") or "").strip()
    if not VERSION_RE.match(version):
        raise ValidationError("版本号不合法")
    if str(payload.get("confirm") or "") != version:
        raise ValidationError("confirm 必须与目标版本一致")
    sha256 = str(payload.get("sha256") or "").strip().lower()
    if not SHA256_RE.match(sha256):
        raise ValidationError("sha256 必须是 64 位小写十六进制")
    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError):
        raise ValidationError("size 必须是字节数") from None
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise ValidationError("归档大小超出允许范围")
    image = str(payload.get("image") or "").strip().lower()
    digest = str(payload.get("digest") or "").strip().lower()
    # image/digest 是可选的 OCI 加速通道；url/archive 仍然是必需的归档回退依据。
    if bool(image) != bool(digest):
        raise ValidationError("image 与 digest 必须同时提供")
    if image:
        if not config.oci_enabled:
            raise ValidationError("宿主执行器未启用镜像分层拉取")
        if not REGISTRY_RE.fullmatch(image) or image != config.registry_repo:
            raise ValidationError("镜像仓库不在允许清单内")
        if not DIGEST_RE.fullmatch(digest):
            raise ValidationError("digest 必须是 sha256:<64 位小写十六进制>")
    url = str(payload.get("url") or "").strip()
    archive = str(payload.get("archive") or "").strip()
    if bool(url) == bool(archive):
        raise ValidationError("url 与 archive 必须二选一")
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
            raise ValidationError("只允许从受信任的 GitHub 域名下载归档")
        if not ASSET_PATH_RE.search(parsed.path):
            raise ValidationError("下载地址必须指向 Release 资产路径")
    else:
        # 只接受归档目录中的文件名，禁止绝对路径、子目录和 .. 穿越。
        if not ARCHIVE_RE.match(archive):
            raise ValidationError("archive 只能是归档目录内的文件名")
        candidate = (config.archive_dir / archive).resolve()
        if candidate.parent != config.archive_dir.resolve():
            raise ValidationError("archive 越出归档目录")
    return {
        "version": version, "sha256": sha256, "size": size, "url": url, "archive": archive,
        "image": image, "digest": digest,
    }


def validate_rollback(payload: Any) -> str:
    """回滚只需要版本与确认，不接受任何镜像/路径字段。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    extra = set(payload) - {"version", "confirm"}
    if extra:
        raise ValidationError(f"不支持字段：{', '.join(sorted(extra))}")
    version = str(payload.get("version") or "").strip()
    if not VERSION_RE.match(version):
        raise ValidationError("版本号不合法")
    if str(payload.get("confirm") or "") != version:
        raise ValidationError("confirm 必须与目标版本一致")
    return version


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _allowed_download(url: str, timeout: float = 60) -> urllib.request.http.client.HTTPResponse:
    """逐跳校验重定向目标，避免受信任域名把人带到任意主机。"""
    current = url
    for _ in range(5):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
            raise UpdateError(f"重定向目标不受信任：{parsed.hostname or current}")
        request = urllib.request.Request(current, headers={"User-Agent": "menu-capture-hub-updater"})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location") or ""
                current = urllib.parse.urljoin(current, location)
                continue
            raise UpdateError(f"下载失败：HTTP {exc.code}") from None
    raise UpdateError("重定向次数过多")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HubUpdater:
    """单个更新事务：下载 → 校验 → 加载 → 切换 → 健康核对，失败尝试回滚。"""

    def __init__(
        self,
        config: Config,
        *,
        run: Callable[[list[str], float], tuple[int, str]] | None = None,
        run_env: Callable[[list[str], float, dict[str, str]], tuple[int, str]] | None = None,
        download: Callable[[str, Path], None] | None = None,
        health: Callable[[str], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = config.state_dir / "hub-update.json"
        self.audit_path = config.state_dir / "hub-update-audit.jsonl"
        self._lock = threading.Lock()
        self.run = run or self._run
        self.run_env = run_env or self._run_with_env
        self.download = download or self._download
        self.health = health or self._health
        self.sleep = sleep

    # ---------- 基础动作 ----------

    @staticmethod
    def _run(args: list[str], timeout: float) -> tuple[int, str]:
        try:
            completed = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, str(exc)
        return completed.returncode, "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        ).strip()

    def _download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        written = 0
        with _allowed_download(url) as response, part.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ARCHIVE_BYTES:
                    raise UpdateError("归档超过允许大小，已中止")
                digest.update(chunk)
                handle.write(chunk)
        part.replace(target)

    def _health(self, version: str) -> dict:
        """同时核对容器健康、服务健康与真实运行版本，避免只看容器 State。"""
        result: dict = {"ok": False, "version": "", "detail": ""}
        container = self._container_id()
        if not container:
            result["detail"] = "找不到 hub 容器"
            return result
        code, output = self.run(
            ["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container],
            30,
        )
        state = output.strip() if code == 0 else ""
        if state not in {"healthy", "running"}:
            result["detail"] = f"容器状态 {state or '未知'}"
            return result
        try:
            request = urllib.request.Request(f"{self.config.health_url}/api/version")
            if self.config.health_password:
                credential = f"{self.config.health_user or 'admin'}:{self.config.health_password}"
                request.add_header("Authorization", "Basic " + base64.b64encode(credential.encode()).decode())
            with urllib.request.urlopen(request, timeout=5) as response:
                info = json.loads(response.read(64 * 1024).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败都按未就绪处理
            result["detail"] = f"版本接口不可用：{exc}"
            return result
        if not isinstance(info, dict) or str(info.get("version") or "") != version:
            result["detail"] = f"运行版本 {info.get('version') if isinstance(info, dict) else '未知'}，期望 {version}"
            return result
        result.update({"ok": True, "version": version})
        return result

    def _container_id(self) -> str:
        if self.config.container:
            return self.config.container
        code, output = self.run(
            ["docker", "compose", "-f", str(self.config.compose_file), "-p", self.config.project,
             "ps", "-q", self.config.service],
            60,
        )
        if code != 0:
            return ""
        ids = [line.strip() for line in output.splitlines() if line.strip()]
        return ids[0] if ids else ""

    def _switch(self, version: str) -> tuple[bool, str]:
        # compose 只认环境里的镜像标签；绝不拼接 shell 命令，避免注入。
        env = dict(os.environ, MENU_CAPTURE_IMAGE=self.config.image_ref(version))
        code, output = self.run_env(
            ["docker", "compose", "-f", str(self.config.compose_file), "-p", self.config.project,
             "up", "-d", "--no-build", self.config.service],
            DOCKER_TIMEOUT_SECONDS,
            env,
        )
        return code == 0, (output or "")[:800]

    def _run_with_env(self, args: list[str], timeout: float, env: dict[str, str]) -> tuple[int, str]:
        try:
            completed = subprocess.run(args, capture_output=True, text=True, check=False,
                                       timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, str(exc)
        return completed.returncode, "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        ).strip()

    def _wait_healthy(self, version: str) -> tuple[bool, str]:
        deadline = time.monotonic() + self.config.health_timeout
        detail = ""
        while time.monotonic() < deadline:
            state = self.health(version)
            if state.get("ok"):
                return True, ""
            detail = str(state.get("detail") or "")
            self.sleep(3)
        return False, detail or "健康检查超时"

    def _disk_ok(self, payload: dict) -> bool:
        usage = shutil.disk_usage(self.config.state_dir)
        return usage.free > payload["size"] + DISK_MARGIN_BYTES

    def _schema_snapshot(self) -> dict:
        """只读记录 Hub 唯一 SQLite 状态库的架构，用于判断旧镜像能否读新库。"""
        if not self.config.data_volume or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.config.data_volume):
            raise UpdateError("未配置 Hub 数据卷，无法建立数据库回滚基线")
        code, _ = self.run(["docker", "volume", "inspect", self.config.data_volume], 30)
        if code != 0:
            raise UpdateError("Hub 数据卷不存在，已拒绝架构检查")
        image = self._schema_probe_image()
        mount = f"type=volume,source={self.config.data_volume},target=/hub-data,volume-nocopy,readonly"
        code, output = self.run([
            "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--mount", mount,
            "--entrypoint", "python", image, "-c", SCHEMA_PROBE, "/hub-data",
        ], 30)
        if code != 0:
            raise UpdateError("无法通过只读容器读取 Hub 数据库架构，已拒绝回滚判定")
        try:
            payload = json.loads(output)
            tables: dict[str, dict] = {}
            for table in payload["tables"]:
                name = table["name"]
                columns = {
                    row[1]: {"type": row[2] or "", "notnull": int(row[3]), "default": row[4],
                             "pk": int(row[5]), "hidden": int(row[6])}
                    for row in table["columns"]
                }
                indexes = {
                    item["name"]: {key: item[key] for key in ("unique", "origin", "partial", "columns", "sql")}
                    for item in table["indexes"]
                }
                column_defs, constraints, table_options = _table_definitions(table["sql"])
                tables[name] = {
                    "columns": columns, "column_defs": column_defs,
                    "constraints": sorted(constraints), "indexes": indexes,
                    "foreign_keys": table["foreign_keys"], "table_options": table_options,
                }
            objects = sorted(
                (str(row[0]), str(row[1]), _normalize_sql(row[2] or ""))
                for row in payload["objects"]
            )
            return {"exists": bool(payload["exists"]), "tables": tables, "objects": [list(row) for row in objects]}
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateError(f"只读容器返回了无效的数据库架构：{exc}") from None

    def _schema_probe_image(self) -> str:
        current = self._current_image_ref()
        prefix = f"{self.config.image_repo}:"
        if current.startswith(prefix) and VERSION_RE.fullmatch(current[len(prefix):]):
            return current
        journal = self.journal()
        for key in ("target_version", "previous_version"):
            version = str(journal.get(key) or "")
            if not VERSION_RE.fullmatch(version):
                continue
            image = self.config.image_ref(version)
            code, _ = self.run(["docker", "image", "inspect", image], 30)
            if code == 0:
                return image
        raise UpdateError("没有可用的 Hub 应用镜像读取数据库架构")

    @staticmethod
    def _schema_compatibility(before: dict, after: dict) -> tuple[bool, str]:
        """自动回滚只接受旧代码可证明忽略的扩展；既有表加列默认不兼容。"""
        if before.get("objects") != after.get("objects"):
            return False, "数据库视图或触发器发生变化，无法证明旧版本兼容"
        old_tables = before.get("tables", {})
        new_tables = after.get("tables", {})
        for table_name, old in old_tables.items():
            current = new_tables.get(table_name)
            if current is None:
                return False, f"数据库表 {table_name} 已删除"
            old_columns = old["columns"]
            new_columns = current["columns"]
            missing = set(old_columns) - set(new_columns)
            if missing:
                return False, f"数据库表 {table_name} 删除了列：{', '.join(sorted(missing))}"
            old_defs = old.get("column_defs", {})
            new_defs = current.get("column_defs", {})
            for column_name, definition in old_defs.items():
                if new_defs.get(column_name) != definition:
                    return False, f"数据库列 {table_name}.{column_name} 的定义发生变化"
            if old.get("constraints", []) != current.get("constraints", []):
                return False, f"数据库表 {table_name} 的约束发生变化"
            if old.get("table_options") != current.get("table_options"):
                return False, f"数据库表 {table_name} 的存储选项发生变化"
            old_foreign_keys = {tuple(item) for item in old.get("foreign_keys", [])}
            current_foreign_keys = {tuple(item) for item in current.get("foreign_keys", [])}
            if old_foreign_keys != current_foreign_keys:
                return False, f"数据库表 {table_name} 的外键发生变化"
            for column_name in set(new_columns) - set(old_columns):
                # SQLite 会把新增列追加到表尾。旧版若使用 INSERT ... VALUES(位置参数)，
                # 即使新列有 DEFAULT，写入值个数也会不匹配；仅凭 PRAGMA 无法证明旧代码安全。
                return False, f"新增列 {table_name}.{column_name} 的旧版本写入兼容性无法证明"
            for index_name, index in old.get("indexes", {}).items():
                if current.get("indexes", {}).get(index_name) != index:
                    return False, f"数据库表 {table_name} 的索引 {index_name} 被删除或修改"
            for index_name in set(current.get("indexes", {})) - set(old.get("indexes", {})):
                if current["indexes"][index_name]["unique"]:
                    return False, f"数据库表 {table_name} 新增了唯一索引 {index_name}，旧版本写入兼容性未知"
        if before.get("exists") and not after.get("exists"):
            return False, "Hub 状态数据库已丢失"
        return True, "数据库仅有向后兼容的扩展"

    def _require_rollback_compatible(self, journal: dict, *, target: str) -> None:
        baseline = journal.get("schema_before")
        if not isinstance(baseline, dict):
            reason = "事务没有保存数据库架构基线"
        else:
            try:
                current = self._schema_snapshot()
                compatible, reason = self._schema_compatibility(baseline, current)
            except UpdateError as exc:
                compatible, reason = False, str(exc)
        if not compatible:
            message = f"数据库架构不适用于回滚到 {target}：{reason}；已停止自动回退，请人工恢复"
            self._write("needs_manual_recovery", message, error=message, target_version=target)
            self._audit("rollback", "blocked_schema", message, target)
            raise UpdateError(message)

    # ---------- 状态与审计 ----------

    def journal(self) -> dict:
        return _read_json(self.journal_path, {})

    def status(self) -> dict:
        journal = self.journal()
        status = str(journal.get("status") or "idle")
        alive = _pid_alive(int(journal.get("pid") or 0))
        if status in ACTIVE_STATUSES and not alive:
            journal = self._resolve_stale(journal)
            status = str(journal.get("status") or "failed")
        return {
            "supported": True,
            "active": status in ACTIVE_STATUSES,
            "running": alive,
            "current_image": self._current_image_ref(),
            # 最小能力声明：Hub 只在 oci=true 时才发送 image/digest，旧执行器保持归档路径。
            "capabilities": {"oci": self.config.oci_enabled},
            **journal,
            "status": status,
        }

    def _current_image_ref(self) -> str:
        container = self._container_id()
        if not container:
            return ""
        code, output = self.run(["docker", "inspect", "--format", "{{.Config.Image}}", container], 30)
        return output.strip() if code == 0 else ""

    def _resolve_stale(self, journal: dict) -> dict:
        """执行器进程已退出：按真实运行版本收尾，不假装仍在安装。"""
        target = str(journal.get("target_version") or "")
        current = self._current_image_ref()
        version = current.rsplit(":", 1)[-1] if current else ""
        if target and version == target:
            journal.update({"status": "completed", "message": f"执行器已退出，服务已运行 {target}"})
        else:
            journal.update({
                "status": "failed",
                "message": f"执行器异常退出：运行镜像 {current or '未知'}，目标 {target or '未知'}",
            })
        journal.update({"error": "" if journal["status"] == "completed" else journal["message"],
                        "finished_at": _utc_now(), "updated_at": _utc_now()})
        _write_json(self.journal_path, journal)
        return journal

    def _write(self, status: str, message: str, *, error: str = "", **extra: Any) -> None:
        journal = self.journal()
        journal.update(extra)
        journal.update({
            "status": status,
            "message": message,
            "error": error or (message if status in {"failed", "rolled_back"} else ""),
            "pid": os.getpid(),
            "updated_at": _utc_now(),
        })
        if status in {"completed", "failed", "rolled_back"}:
            journal["finished_at"] = _utc_now()
        _write_json(self.journal_path, journal)

    def _audit(self, action: str, result: str, message: str, version: str = "") -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"time": _utc_now(), "action": action, "result": result,
               "version": version, "message": message[:500]}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---------- 事务 ----------

    def active(self) -> bool:
        status = str(self.journal().get("status") or "")
        return status in ACTIVE_STATUSES and _pid_alive(int(self.journal().get("pid") or 0))

    def apply(self, payload: Any) -> dict:
        with self._lock:
            request = validate_payload(payload, self.config)
            if self.active():
                raise UpdateError("已有更新事务正在执行")
            version = request["version"]
            previous = self._current_image_ref().rsplit(":", 1)[-1]
            if previous and VERSION_RE.match(previous) and self._compare(version, previous) <= 0:
                raise UpdateError("目标版本必须高于当前运行版本，禁止降级")
            # OCI 模式不下载完整归档，因此不能先用归档体积挡住更新；
            # 只有真正回退到归档时才检查磁盘余量。
            if not request["image"] and not self._disk_ok(request):
                raise UpdateError("磁盘余量不足，已拒绝开始更新")
            schema_before = self._schema_snapshot()
            self._write("pulling" if request["image"] else "downloading",
                        f"正在准备 {version} 更新", target_version=version,
                        previous_version=previous, schema_before=schema_before,
                        started_at=_utc_now(), finished_at="", error="")
            pulled = False
            if request["image"]:
                self._write("pulling", f"正在分层拉取 {request['image']}@{request['digest']}")
                pulled, output = self._pull_image(request["image"], request["digest"], version)
                if not pulled:
                    # 拉取失败不改变事务身份：审计后在同一事务内改用完整归档。
                    self._audit("pull", "failed", output, version)
                    if not self._disk_ok(request):
                        self._fail(f"镜像分层拉取失败且磁盘余量不足：{output}", version)
                        raise UpdateError(f"镜像分层拉取失败且磁盘余量不足：{output}")
                    self._write("downloading", "镜像分层拉取失败，改用完整归档", error="")
            if not pulled:
                if not request["image"]:
                    self._write("downloading", f"正在准备 {version} 镜像归档")
                archive = self._prepare_archive(request)
                self._write("loading", f"正在加载镜像 {self.config.image_ref(version)}")
                ok, output = self._load_image(archive, version)
                if not ok:
                    self._fail(f"镜像加载失败：{output}", version)
                    raise UpdateError(f"镜像加载失败：{output}")
            self._write("switching", f"正在切换到 {version}",
                        delivery="oci" if pulled else "archive")
            switched, output = self._switch(version)
            if not switched:
                return self._rollback_after_failure(previous, f"compose 切换失败：{output}", version)
            self._write("verifying", "正在核对运行版本与健康状态")
            healthy, detail = self._wait_healthy(version)
            if not healthy:
                return self._rollback_after_failure(previous, f"健康检查未通过：{detail}", version)
            self._write("completed", f"已切换到 {version}")
            self._audit("install", "ok", f"已切换到 {version}", version)
            return self.status()

    def rollback(self, payload: Any) -> dict:
        with self._lock:
            target = validate_rollback(payload)
            if self.active():
                raise UpdateError("已有更新事务正在执行")
            journal = self.journal()
            previous = str(journal.get("previous_version") or "")
            if not previous or not VERSION_RE.match(previous):
                raise UpdateError("没有可回滚的上一版本记录")
            if target != previous:
                raise UpdateError(f"回滚目标必须是上一版本 {previous}")
            current = self._current_image_ref().rsplit(":", 1)[-1]
            if current == target:
                raise UpdateError("当前已经运行上一版本")
            self._require_rollback_compatible(journal, target=target)
            code, _ = self.run(["docker", "image", "inspect", self.config.image_ref(target)], 30)
            if code != 0:
                raise UpdateError(f"上一版本镜像 {self.config.image_ref(target)} 已不存在，无法自动回滚")
            self._write("rolling_back", f"正在回滚到 {target}", target_version=target,
                        started_at=_utc_now(), error="")
            switched, output = self._switch(target)
            if not switched:
                self._fail(f"回滚切换失败：{output}", target)
                raise UpdateError(f"回滚切换失败：{output}")
            healthy, detail = self._wait_healthy(target)
            if not healthy:
                # 回滚目标不可用时绝不能把采集服务留在停机状态：优先切回回滚前仍在运行的版本。
                restored, restore_output = self._restore_after_failed_rollback(current)
                message = f"回滚后健康检查未通过：{detail}"
                message += (f"；已恢复运行 {current}" if restored
                            else f"；恢复运行 {current or '原版本'} 也失败：{restore_output or '未知原因'}")
                self._fail(message, target)
                raise UpdateError(message)
            self._write("rolled_back", f"已回滚到 {target}")
            self._audit("rollback", "ok", f"已回滚到 {target}", target)
            return self.status()

    def _prepare_archive(self, request: dict) -> Path:
        if request["archive"]:
            archive = self.config.archive_dir / request["archive"]
        else:
            archive = self.config.archive_dir / f"hub-image-{request['version']}.tar.gz"
            try:
                self.download(request["url"], archive)
            except UpdateError:
                raise
            except Exception as exc:  # noqa: BLE001 - 下载错误统一转成可审计的失败原因
                self._fail(f"下载失败：{exc}", request["version"])
                raise UpdateError(f"下载失败：{exc}") from None
        if not archive.is_file():
            self._fail("归档不存在", request["version"])
            raise UpdateError("归档不存在")
        if archive.stat().st_size != request["size"]:
            self._fail("归档大小与清单不一致", request["version"])
            raise UpdateError("归档大小与清单不一致")
        if _sha256_file(archive) != request["sha256"]:
            self._fail("归档 SHA-256 校验失败", request["version"])
            raise UpdateError("归档 SHA-256 校验失败")
        return archive

    def _load_image(self, archive: Path, version: str) -> tuple[bool, str]:
        code, output = self.run(["docker", "load", "-i", str(archive)], DOCKER_TIMEOUT_SECONDS)
        if code != 0:
            return False, output[:800]
        expected = self.config.image_ref(version)
        loaded = re.findall(r"Loaded image(?: ID)?:\s*(\S+)", output)
        if expected not in loaded:
            return False, f"归档未包含 {expected}（实际：{', '.join(loaded) or '未识别'}）"
        return True, output

    def _pull_image(self, image: str, digest: str, version: str) -> tuple[bool, str]:
        """按不可变摘要拉取固定仓库镜像，核对摘要/平台后再打成 compose 使用的本地标签。

        只接受 validate_payload 已校验过的仓库与 digest；任何一步不一致都返回失败，
        由调用方在同一事务内回退到完整归档，绝不使用来源不明的镜像。
        """
        ref = f"{image}@{digest}"
        code, output = self.run(
            ["docker", "pull", "--platform", OCI_PLATFORM, ref], DOCKER_TIMEOUT_SECONDS
        )
        if code != 0:
            return False, f"拉取失败：{output[:600]}"
        code, output = self.run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref], 60
        )
        if code != 0:
            return False, f"无法读取镜像摘要：{output[:400]}"
        try:
            digests = json.loads(output.strip() or "[]")
        except ValueError:
            digests = []
        if not isinstance(digests, list) or ref not in digests:
            actual = ", ".join(str(item) for item in digests) if isinstance(digests, list) else "无法解析"
            return False, f"拉取到的镜像摘要与清单不一致：{actual or '未知'}"
        code, output = self.run(
            ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", ref], 60
        )
        if code != 0 or output.strip() != OCI_PLATFORM:
            return False, f"镜像平台不是 {OCI_PLATFORM}：{output.strip() or '未知'}"
        target = self.config.image_ref(version)
        code, output = self.run(["docker", "tag", ref, target], 60)
        if code != 0:
            return False, f"镜像打标签失败：{output[:400]}"
        code, output = self.run(["docker", "image", "inspect", target], 30)
        if code != 0:
            return False, f"打标签后找不到 {target}"
        return True, ""

    def _rollback_after_failure(self, previous: str, message: str, version: str) -> dict:
        if not previous or not VERSION_RE.match(previous):
            self._fail(message, version)
            raise UpdateError(message)
        self._require_rollback_compatible(self.journal(), target=previous)
        self._write("rolling_back", f"{message}；正在回滚到 {previous}", error=message)
        switched, output = self._switch(previous)
        healthy = False
        detail = ""
        if switched:
            healthy, detail = self._wait_healthy(previous)
        if not healthy:
            self._fail(f"{message}；回滚失败：{output or detail}", version)
            raise UpdateError(f"{message}；回滚失败：{output or detail}")
        self._write("rolled_back", f"{message}；已回滚到 {previous}", target_version=version)
        self._audit("install", "rolled_back", message, version)
        return self.status()

    def _restore_after_failed_rollback(self, version: str) -> tuple[bool, str]:
        """回滚目标不可用时切回回滚前运行的版本，保证采集服务不因回滚失败而停机。"""
        if not version or not VERSION_RE.match(version):
            return False, "没有可恢复的原版本"
        code, _ = self.run(["docker", "image", "inspect", self.config.image_ref(version)], 30)
        if code != 0:
            return False, f"原版本镜像 {self.config.image_ref(version)} 不存在"
        self._write("rolling_back", f"回滚未通过健康检查，正在恢复运行 {version}", target_version=version)
        switched, output = self._switch(version)
        if not switched:
            return False, output
        healthy, detail = self._wait_healthy(version)
        return healthy, detail

    def _fail(self, message: str, version: str) -> None:
        self._write("failed", message, target_version=version)
        self._audit("install", "failed", message, version)

    @staticmethod
    def _compare(left: str, right: str) -> int:
        """语义化版本比较，只用数字段与预发布标记，避免引入额外依赖。"""
        def parse(value: str) -> tuple:
            core, _, pre = value.partition("-")
            numbers = tuple(int(item) for item in core.split("."))
            # 无预发布标记的正式版高于同号预发布版。
            return numbers + ((1,) if not pre else (0, pre))

        try:
            a, b = parse(left), parse(right)
        except ValueError:
            return 0
        return (a > b) - (a < b)


class Handler(BaseHTTPRequestHandler):
    server_version = "MenuCaptureHubUpdater/1"
    updater: HubUpdater
    token: str
    allowed: list[str]

    def log_message(self, fmt: str, *args: Any) -> None:  # 只写审计，不写访问日志
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not _client_allowed(self.client_address[0], self.allowed):
            self._send(403, {"error": "来源地址不在允许范围"})
            return False
        header = self.headers.get("Authorization") or ""
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, self.token):
            self._send(401, {"error": "未授权"})
            return False
        return True

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            raise UpdateError("请求体为空或过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path != "/status":
            self._send(404, {"error": "未知路径"})
            return
        self._send(200, self.updater.status())

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            payload = self._body()
        except (UpdateError, ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        try:
            if self.path == "/apply":
                self._send(200, self.updater.apply(payload))
            elif self.path == "/rollback":
                self._send(200, self.updater.rollback(payload))
            else:
                self._send(404, {"error": "未知路径"})
        except ValidationError as exc:
            # 请求格式非法用 400；事务状态冲突（进行中、禁止降级、无回滚目标）用 409。
            self._send(400, {"error": str(exc)})
        except UpdateError as exc:
            self._send(409, {"error": str(exc), **self.updater.journal()})
        except Exception as exc:  # noqa: BLE001 - 执行器不能把内部异常直接暴露为 500 堆栈
            self._send(500, {"error": f"执行器内部错误：{exc}"})


def serve(config: Config) -> None:
    config.validate()
    updater = HubUpdater(config)
    # 进程重启后把遗留的活动事务收尾，避免界面永久停在切换中。
    updater.status()
    handler = type("BoundHandler", (Handler,), {
        "updater": updater,
        "token": config.token,
        "allowed": config.allowed_clients,
    })
    server = ThreadingHTTPServer((config.bind, config.port), handler)
    print(f"hub updater listening on {config.bind}:{config.port}", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hub 宿主机镜像更新执行器")
    parser.add_argument("--status", action="store_true", help="只打印当前事务状态")
    args = parser.parse_args(argv)
    config = Config()
    if args.status:
        print(json.dumps(HubUpdater(config).status(), ensure_ascii=False, indent=2))
        return 0
    try:
        serve(config)
    except UpdateError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
