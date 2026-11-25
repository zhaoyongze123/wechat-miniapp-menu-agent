"""比较两棵正式构建树，生成 Windows 文件级增量补丁。

仅使用标准库，供发布流水线在**同一次构建**的 staging bundle 上调用：
- 基线/目标目录必须各自带有 `release-manifest.json`，版本与 commit 从清单读取，
  不接受调用方凭空指定，避免补丁绑定到错误的构建；
- 版本、commit、平台、架构与两棵树的完整受管文件清单都会写入补丁；
- 运行环境/依赖锁/安装器/根级脚本存在差异时返回 `skipped`，该版本回退全量；
- 生成失败不允许阻断完整安装包与正式 Release。

退出码：0 = 已生成或明确跳过（可继续发布）；2 = 参数/输入错误（应检查流水线）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "app"))

from menu_capture.updates import update_patch

MANIFEST_NAME = "release-manifest.json"


def _read_manifest(root: Path) -> dict:
    path = root / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"缺少发布清单：{path}") from exc
    except ValueError as exc:
        raise ValueError(f"发布清单不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"发布清单结构无效：{path}")
    return payload


def _describe(root: Path, version: str | None, label: str) -> dict:
    if not root.is_dir():
        raise ValueError(f"{label}构建目录不存在：{root}")
    manifest = _read_manifest(root)
    actual_version = str(manifest.get("version") or "").strip()
    commit = str(manifest.get("git_commit") or "").strip()
    if not actual_version:
        raise ValueError(f"{label}发布清单缺少版本号")
    if version and version != actual_version:
        raise ValueError(f"{label}版本不匹配：参数 {version}，清单 {actual_version}")
    # 没有 commit 就无法建立可信绑定；宁可跳过增量，也不能产出无法审计的补丁。
    if not commit:
        raise ValueError(f"{label}发布清单缺少 git_commit，无法建立补丁绑定")
    return {"version": actual_version, "commit": commit}


def _emit(payload: dict, *, stream=sys.stdout) -> None:
    # Windows Runner 默认代码页可能是 cp1252；JSON 转义非 ASCII 字符，避免错误路径
    # 在输出中文诊断时再抛 UnicodeEncodeError 并遮蔽最初的补丁失败原因。
    json.dump(payload, stream, ensure_ascii=True, indent=2)
    stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Windows 文件级增量补丁")
    parser.add_argument("--from-dir", required=True, help="基线版本的完整构建树")
    parser.add_argument("--to-dir", required=True, help="目标版本的完整构建树")
    parser.add_argument("--from-version", default="", help="期望的基线版本（须与清单一致）")
    parser.add_argument("--to-version", default="", help="期望的目标版本（须与清单一致）")
    parser.add_argument("--output", required=True, help="补丁 ZIP 输出路径")
    parser.add_argument("--platform", default="windows")
    parser.add_argument("--arch", default="x64")
    args = parser.parse_args(argv)

    base_dir = Path(args.from_dir).resolve()
    target_dir = Path(args.to_dir).resolve()
    output = Path(args.output).resolve()

    try:
        base = _describe(base_dir, args.from_version or None, "基线")
        target = _describe(target_dir, args.to_version or None, "目标")
        if base["version"] == target["version"]:
            raise ValueError("基线版本与目标版本相同，不需要补丁")
        summary = update_patch.make_patch(
            base_dir,
            target_dir,
            from_version=base["version"],
            to_version=target["version"],
            output=output,
            from_commit=base["commit"],
            to_commit=target["commit"],
            platform=args.platform,
            arch=args.arch,
        )
    except update_patch.PatchError as exc:
        # 结构性问题（符号链接、超限、非法路径）说明构建树不可用于增量；回退全量。
        _emit({"status": "skipped", "reason": str(exc)})
        return 0
    except (ValueError, TypeError) as exc:
        _emit({"status": "error", "reason": str(exc)}, stream=sys.stderr)
        return 2

    if summary is None:
        _emit({
            "status": "skipped",
            "reason": "运行环境、依赖、安装器或根级脚本存在差异，该版本必须使用完整安装包",
        })
        return 0

    sidecar = output.with_suffix(".json")
    metadata = {
        "status": "ok",
        "format": summary["format"],
        "from": summary["from"],
        "to": summary["to"],
        "from_commit": base["commit"],
        "to_commit": target["commit"],
        "platform": args.platform,
        "arch": args.arch,
        "name": summary["name"],
        "size": summary["size"],
        "sha256": summary["sha256"],
        "changed": summary["changed"],
        "deleted": summary["deleted"],
        "total_files": summary["total_files"],
    }
    sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit(metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
