"""文件级增量补丁：生成、解析与应用。

边界：
- 只接受本模块生成的 `zip-sha256-v1` 格式，不使用 `zipfile.extractall`；
- 路径按 POSIX 相对路径校验，拒绝绝对路径、`..`、反斜杠、盘符、NTFS 数据流、
  Windows 保留名、尾随点/空格、控制字符以及大小写碰撞；
- 限制条目数、单文件与总解压大小，避免 zip bomb；
- 应用时只复制补丁声明的基线受管文件，重建结果必须与目标全量清单完全一致；
- 受限区域（运行环境、依赖锁、安装器、根级启动/回滚脚本）一旦变化即判定
  该版本不可增量，由调用方回退全量安装。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

PATCH_FORMAT = "zip-sha256-v1"
PATCH_MANIFEST_NAME = "patch-manifest.json"
PATCH_FILES_PREFIX = "files/"
# 解压上限：单个补丁远超这些值时说明不是本项目的应用层补丁，直接拒绝。
# 已发布的 v0.3.2/v0.3.3 bundle 各有 26,121 个文件；此上限约为实测基线的两倍，
# 允许正常完整构建树进入清单，同时仍限制异常膨胀的目录扫描和清单解析。
MAX_RELEASE_FILES = 50000
# 增量补丁本身仍限制变更文件数，避免单个 ZIP 和安装事务失控。
MAX_PATCH_FILES = 20000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_PATH_LENGTH = 240
_MAX_COMPONENT_LENGTH = 200
_CHUNK_BYTES = 256 * 1024
# 运行环境/安装契约不允许通过增量修改，必须走完整安装包。
RESTRICTED_PREFIXES = ("python/", "node/", "tesseract/", "vcruntime/")
RESTRICTED_NAMES = {
    "launch.ps1",
    "rollback.ps1",
    "setup.iss",
    "runtimes.json",
    "vc-runtime-version.txt",
    "requirements.txt",
    "requirements.lock",
    "poetry.lock",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
# 这些根级文件安装在安装根而不是 releases/<version>，补丁不能触碰。
ROOT_ONLY_FILES = ("launch.ps1", "rollback.ps1", "setup.iss")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class PatchError(ValueError):
    """补丁内容不合法或基线不可信；调用方应回退全量安装。"""


class PatchScopeError(PatchError):
    """受限区域存在差异，该版本不允许生成/应用增量补丁。"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    """符号链接与 Windows 重解析点都不能作为补丁基线。"""
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except OSError:
        return True
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _validate_relative(name: str) -> str:
    """校验并归一化补丁内的相对路径；任何可疑写法都直接拒绝。"""
    text = str(name or "")
    if not text or text != text.strip():
        raise PatchError(f"补丁路径为空或含首尾空白: {name!r}")
    if len(text) > MAX_PATH_LENGTH:
        raise PatchError(f"补丁路径过长: {name!r}")
    if "\\" in text or ":" in text:
        raise PatchError(f"补丁路径包含非法字符: {name!r}")
    if text.startswith("/"):
        raise PatchError(f"补丁路径不能是绝对路径: {name!r}")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PatchError(f"补丁路径包含空段或上跳: {name!r}")
    for part in parts:
        if len(part) > _MAX_COMPONENT_LENGTH:
            raise PatchError(f"补丁路径单段过长: {name!r}")
        if any(ord(char) < 32 for char in part):
            raise PatchError(f"补丁路径包含控制字符: {name!r}")
        if part.endswith(".") or part.endswith(" "):
            raise PatchError(f"补丁路径不能以点或空格结尾: {name!r}")
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise PatchError(f"补丁路径使用了 Windows 保留名: {name!r}")
    return text


def _is_restricted(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith(RESTRICTED_PREFIXES):
        return True
    name = lowered.rsplit("/", 1)[-1]
    return name in RESTRICTED_NAMES


def scan_tree(root: Path, *, exclude_root: tuple[str, ...] = ()) -> dict[str, str]:
    """返回受管文件清单（POSIX 相对路径 → SHA-256）；拒绝符号链接/重解析点。"""
    root = Path(root)
    if not root.is_dir():
        raise PatchError(f"目录不存在: {root}")
    excluded = {name.lower() for name in exclude_root}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        try:
            if path.is_dir():
                continue
            if _is_reparse(path):
                raise PatchError(f"目录中存在符号链接或重解析点: {path}")
            relative = path.relative_to(root).as_posix()
        except OSError as exc:
            raise PatchError(f"无法读取目录内容: {path}") from exc
        if "/" not in relative and relative.lower() in excluded:
            continue
        if not path.is_file():
            continue
        if len(files) >= MAX_RELEASE_FILES:
            raise PatchError("文件数量超出补丁允许范围")
        files[_validate_relative(relative)] = file_sha256(path)
    return files


def scope_fingerprint(root: Path, files: dict[str, str]) -> str:
    """受限区域的整体指纹：运行环境、依赖锁、安装器与根级脚本。"""
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or _is_reparse(root):
        raise PatchError(f"补丁受限区域目录不可用: {root}")
    entries: list[str] = []
    for name, sha in files.items():
        if _is_restricted(name):
            entries.append(f"{name}\0{sha}")
    for name in ROOT_ONLY_FILES:
        candidate = next(
            (path for path in Path(root).iterdir() if path.name.lower() == name),
            None,
        )
        if candidate is not None:
            if _is_reparse(candidate):
                raise PatchError(f"补丁受限区域包含符号链接或重解析点: {candidate}")
            if candidate.is_file():
                entries.append(f"{name}\0{file_sha256(candidate)}")
    digest = hashlib.sha256()
    for item in sorted(entries):
        digest.update(item.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _restricted_changes(paths: list[str]) -> list[str]:
    return sorted(path for path in paths if _is_restricted(path))


def make_patch(
    base_dir: Path,
    target_dir: Path,
    *,
    from_version: str,
    to_version: str,
    output: Path,
    from_commit: str = "",
    to_commit: str = "",
    platform: str = "windows",
    arch: str = "x64",
) -> dict | None:
    """比较两棵完整构建树并生成补丁；受限区域有差异时返回 None（该版本走全量）。"""
    base_files = scan_tree(base_dir, exclude_root=ROOT_ONLY_FILES)
    target_files = scan_tree(target_dir, exclude_root=ROOT_ONLY_FILES)
    base_scope = scope_fingerprint(base_dir, base_files)
    target_scope = scope_fingerprint(target_dir, target_files)
    if base_scope != target_scope:
        return None
    changed = sorted(
        name for name, sha in target_files.items() if base_files.get(name) != sha
    )
    deleted = sorted(name for name in base_files if name not in target_files)
    if len(changed) + len(deleted) > MAX_PATCH_FILES:
        raise PatchError("变更文件数量超出补丁允许范围")
    restricted = _restricted_changes(changed + deleted)
    if restricted:
        # 生成端再次兜底：即便指纹实现有遗漏，也不产出触碰受限区域的补丁。
        return None
    manifest = {
        "format": PATCH_FORMAT,
        "from": str(from_version),
        "to": str(to_version),
        "from_commit": str(from_commit),
        "to_commit": str(to_commit),
        "platform": str(platform),
        "arch": str(arch),
        "scope": {"base": base_scope, "target": target_scope},
        "base_files": base_files,
        "files": target_files,
        "changed": changed,
        "deleted": deleted,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise PatchError("补丁清单超出大小限制")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(PATCH_MANIFEST_NAME, manifest_bytes)
        for name in changed:
            source = Path(target_dir) / Path(name)
            if _is_reparse(source) or not source.is_file():
                raise PatchError(f"目标文件不可读取: {name}")
            archive.write(source, arcname=f"{PATCH_FILES_PREFIX}{name}")
    temp.replace(output)
    return {
        "format": PATCH_FORMAT,
        "from": str(from_version),
        "to": str(to_version),
        "name": output.name,
        "size": output.stat().st_size,
        "sha256": file_sha256(output),
        "changed": len(changed),
        "deleted": len(deleted),
        "total_files": len(target_files),
    }


def _read_manifest(archive: zipfile.ZipFile) -> dict:
    try:
        info = archive.getinfo(PATCH_MANIFEST_NAME)
    except KeyError as exc:
        raise PatchError("补丁缺少清单文件") from exc
    if info.file_size > MAX_MANIFEST_BYTES:
        raise PatchError("补丁清单超出大小限制")
    try:
        payload = json.loads(archive.read(info).decode("utf-8"))
    except (ValueError, OSError) as exc:
        raise PatchError("补丁清单格式无效") from exc
    if not isinstance(payload, dict) or payload.get("format") != PATCH_FORMAT:
        raise PatchError("补丁格式不受支持")
    for key in ("from", "to", "platform", "arch"):
        if not str(payload.get(key) or "").strip():
            raise PatchError(f"补丁清单缺少字段: {key}")
    for key in ("base_files", "files"):
        value = payload.get(key)
        if not isinstance(value, dict) or not value:
            raise PatchError(f"补丁清单缺少受管文件列表: {key}")
        normalized: dict[str, str] = {}
        folded: set[str] = set()
        for name, sha in value.items():
            relative = _validate_relative(str(name))
            if relative.lower() in folded:
                raise PatchError(f"补丁清单存在大小写碰撞: {name}")
            folded.add(relative.lower())
            if not isinstance(sha, str) or len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha.lower()):
                raise PatchError(f"补丁清单哈希无效: {name}")
            normalized[relative] = sha.lower()
        if len(normalized) > MAX_RELEASE_FILES:
            raise PatchError("补丁清单文件数量超限")
        payload[key] = normalized
    for key in ("changed", "deleted"):
        value = payload.get(key)
        if not isinstance(value, list):
            raise PatchError(f"补丁清单缺少字段: {key}")
        items: list[str] = []
        seen: set[str] = set()
        for name in value:
            relative = _validate_relative(str(name))
            if relative in seen:
                raise PatchError(f"补丁清单存在重复条目: {name}")
            seen.add(relative)
            items.append(relative)
        payload[key] = items
    if len(payload["changed"]) + len(payload["deleted"]) > MAX_PATCH_FILES:
        raise PatchError("补丁变更文件数量超限")
    if set(payload["changed"]) & set(payload["deleted"]):
        raise PatchError("补丁清单的新增/修改与删除存在冲突")
    if any(name not in payload["files"] for name in payload["changed"]):
        raise PatchError("补丁清单的修改文件不在目标清单中")
    if any(name not in payload["base_files"] for name in payload["deleted"]):
        raise PatchError("补丁清单的删除文件不在基线清单中")
    if _restricted_changes(payload["changed"] + payload["deleted"]):
        raise PatchScopeError("补丁包含受限区域变更，必须使用完整安装包")
    scope = payload.get("scope")
    if not isinstance(scope, dict) or scope.get("base") != scope.get("target"):
        raise PatchScopeError("补丁的受限区域指纹不一致")
    return payload


def load_patch(zip_path: Path) -> dict:
    """只解析并校验补丁清单，不写任何文件。"""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            payload = _read_manifest(archive)
            names = {info.filename for info in archive.infolist()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise PatchError("补丁文件损坏或不是有效 ZIP") from exc
    for name in payload["changed"]:
        if f"{PATCH_FILES_PREFIX}{name}" not in names:
            raise PatchError(f"补丁缺少修改文件内容: {name}")
    return payload


def _validate_binding(
    manifest: dict,
    *,
    from_version: str,
    to_version: str,
    platform: str,
    arch: str,
    from_commit: str = "",
    to_commit: str = "",
) -> None:
    if manifest["from"] != str(from_version) or manifest["to"] != str(to_version):
        raise PatchError("补丁版本与安装任务不一致")
    if manifest["platform"] != str(platform):
        raise PatchError("补丁平台与本机不一致")
    if manifest["arch"].lower() != str(arch).lower():
        raise PatchError("补丁架构与本机不一致")
    if from_commit and manifest.get("from_commit") != from_commit:
        raise PatchError("补丁基线提交与本地安装不一致")
    if to_commit and manifest.get("to_commit") != to_commit:
        raise PatchError("补丁目标提交与发布清单不一致")


def apply(
    base_dir: Path,
    stage_dir: Path,
    zip_path: Path,
    *,
    from_version: str,
    to_version: str,
    platform: str = "windows",
    arch: str = "x64",
    from_commit: str = "",
    to_commit: str = "",
    scope_root: Path | None = None,
) -> dict:
    """在 stage_dir 中重建目标版本；任何失败都会清理 stage 并抛出 PatchError。"""
    base_dir = Path(base_dir)
    stage_dir = Path(stage_dir)
    fingerprint_root = Path(scope_root) if scope_root is not None else base_dir
    if scope_root is not None:
        # 安装版把 Launch/rollback 放在安装根，业务文件则位于 releases/<version>。
        # 只允许对这层既定布局使用外部指纹根，避免调用方把任意目录引入信任范围。
        if (
            base_dir.parent.name.lower() != "releases"
            or base_dir.parent.parent != fingerprint_root
            or not fingerprint_root.is_dir()
            or fingerprint_root.is_symlink()
            or _is_reparse(fingerprint_root)
            or _is_reparse(base_dir.parent)
        ):
            raise PatchError("安装版补丁指纹目录与基线目录布局不匹配")
    if stage_dir.exists():
        raise PatchError("补丁临时目录已存在，拒绝覆盖")
    manifest = load_patch(zip_path)
    _validate_binding(
        manifest,
        from_version=from_version,
        to_version=to_version,
        platform=platform,
        arch=arch,
        from_commit=from_commit,
        to_commit=to_commit,
    )
    base_files = manifest["base_files"]
    target_files = manifest["files"]
    changed = set(manifest["changed"])
    deleted = set(manifest["deleted"])
    if manifest["scope"]["base"] != scope_fingerprint(fingerprint_root, base_files):
        raise PatchScopeError("本地基线的运行环境/依赖指纹与补丁不一致")
    try:
        stage_dir.mkdir(parents=True)
        total = 0
        # 先按基线清单复制未删除、未修改的文件；不整目录复制，避免运行期残留进入新版本。
        for name, sha in base_files.items():
            if name in deleted or name in changed:
                continue
            source = base_dir / Path(name)
            if not source.is_file() or _is_reparse(source):
                raise PatchError(f"基线文件缺失或被替换: {name}")
            if file_sha256(source) != sha:
                raise PatchError(f"基线文件哈希不一致: {name}")
            total = _copy_file(source, stage_dir / Path(name), total)
            if file_sha256(stage_dir / Path(name)) != sha:
                raise PatchError(f"基线文件复制后哈希不一致: {name}")
        with zipfile.ZipFile(zip_path) as archive:
            _verify_zip_entries(archive, changed)
            for name in manifest["changed"]:
                target = stage_dir / Path(name)
                total = _extract_member(archive, name, target, target_files[name], total)
        _verify_stage(stage_dir, target_files)
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return {
        "from": manifest["from"],
        "to": manifest["to"],
        "files": len(target_files),
        "changed": len(changed),
        "deleted": len(deleted),
    }


def _copy_file(source: Path, target: Path, total: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK_BYTES), b""):
            total += len(chunk)
            if total > MAX_TOTAL_BYTES:
                raise PatchError("重建内容超出允许大小")
            writer.write(chunk)
    return total


def _verify_zip_entries(archive: zipfile.ZipFile, changed: set[str]) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_PATCH_FILES + 1:
        raise PatchError("补丁条目数量超出限制")
    seen: set[str] = set()
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename
        if name == PATCH_MANIFEST_NAME:
            continue
        if not name.startswith(PATCH_FILES_PREFIX):
            raise PatchError(f"补丁包含未知条目: {name}")
        relative = _validate_relative(name[len(PATCH_FILES_PREFIX):])
        if relative.lower() in seen:
            raise PatchError(f"补丁存在重复或大小写碰撞条目: {name}")
        seen.add(relative.lower())
        if relative not in changed:
            raise PatchError(f"补丁包含未声明的修改文件: {relative}")
        if info.file_size > MAX_FILE_BYTES:
            raise PatchError(f"补丁单文件超出限制: {relative}")


def _extract_member(
    archive: zipfile.ZipFile, name: str, target: Path, expected_sha: str, total: int
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    digest = hashlib.sha256()
    with archive.open(f"{PATCH_FILES_PREFIX}{name}") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK_BYTES), b""):
            written += len(chunk)
            if written > MAX_FILE_BYTES:
                raise PatchError(f"补丁文件解压后超出限制: {name}")
            total += len(chunk)
            if total > MAX_TOTAL_BYTES:
                raise PatchError("补丁解压后总大小超出限制")
            digest.update(chunk)
            writer.write(chunk)
    if digest.hexdigest() != expected_sha:
        raise PatchError(f"补丁文件哈希与清单不一致: {name}")
    return total


def _verify_stage(stage_dir: Path, target_files: dict[str, str]) -> None:
    actual = scan_tree(stage_dir)
    if set(actual) != set(target_files):
        missing = sorted(set(target_files) - set(actual))[:5]
        extra = sorted(set(actual) - set(target_files))[:5]
        raise PatchError(f"重建结果与目标清单不一致（缺失 {missing}，多余 {extra}）")
    for name, sha in target_files.items():
        if actual[name] != sha:
            raise PatchError(f"重建文件哈希不一致: {name}")


def summarize(manifest: dict) -> dict:
    """供执行器写日志的简要信息，不包含文件内容。"""
    return {
        "from": manifest.get("from", ""),
        "to": manifest.get("to", ""),
        "changed": len(manifest.get("changed") or []),
        "deleted": len(manifest.get("deleted") or []),
        "files": len(manifest.get("files") or {}),
    }
