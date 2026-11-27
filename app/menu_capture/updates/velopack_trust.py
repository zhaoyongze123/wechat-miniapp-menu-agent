"""发布者签名：Ed25519 覆盖 Windows 包身份和外层更新安全策略。

私钥仅由受控发布环境从 MENU_CAPTURE_RELEASE_SIGNING_KEY 注入；客户端固定公钥白名单。
清单缺少签名、信任根或策略绑定时一律拒绝，不回退到 unsigned。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

# 正式发布信任根。配置正式公钥后方可验证/发布签名版本；
# 轮换时只追加新 key_id，不删除仍在使用的旧公钥。
PUBLISHER_KEYS: dict[str, str] = {
    "4a5db37517bc5ae3": "1ZMCqx0hQBmpN2Tfp455USDipSVgtzXwGprBO8UBFxQ=",
}

DOMAIN = b"menu-capture-release-v1\x00"
SIGNATURE_SCHEMA = 1
_POLICY_FIELDS = ("version", "channel", "git_commit", "min_supported_version", "rollback", "assets")


def release_policy_from_manifest(manifest: dict, *, allow_empty_assets: bool = False) -> dict:
    """提取需由发布者签名保护的清单外层策略，允许传完整清单或已提取策略。"""
    if not isinstance(manifest, dict):
        raise ValueError("发布清单安全策略格式无效")
    if "assets" not in manifest or any(field not in manifest for field in _POLICY_FIELDS[:-1]):
        raise ValueError("发布清单缺少签名保护的外层安全策略")
    if any(not isinstance(manifest.get(field), str) for field in _POLICY_FIELDS[:-1]):
        raise ValueError("发布清单外层安全策略字段无效")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or any(not isinstance(item, dict) for item in assets):
        raise ValueError("发布清单外层资产策略无效")
    if not assets and not allow_empty_assets:
        raise ValueError("发布清单外层资产策略无效")
    policy = {field: manifest[field] for field in _POLICY_FIELDS[:-1]}
    # JSON 规范化既拒绝非 JSON 值，也切断调用方可变对象的别名。
    try:
        policy["assets"] = json.loads(json.dumps(assets, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("发布清单外层资产策略不可规范化") from exc
    return policy


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("签名载荷无法规范化") from exc


def canonical_payload(extension: dict) -> bytes:
    """规范化签名载荷；trust 自身不签，内嵌外层策略必须完整。"""
    if not isinstance(extension, dict):
        raise ValueError("发布扩展格式无效")
    assets = {}
    for key in ("setup", "full", "index"):
        item = extension.get(key)
        if not isinstance(item, dict):
            raise ValueError(f"发布扩展缺少 {key}")
        assets[key] = {name: item.get(name) for name in ("name", "size", "sha256", "url")}
    deltas = extension.get("deltas")
    if not isinstance(deltas, list):
        raise ValueError("发布扩展缺少差分资产列表")
    assets["deltas"] = [
        {name: item.get(name) for name in ("name", "size", "sha256", "url")}
        for item in deltas if isinstance(item, dict)
    ]
    if len(assets["deltas"]) != len(deltas):
        raise ValueError("发布扩展差分资产格式无效")
    payload = {
        "schema_version": extension.get("schema_version"),
        "pack_id": extension.get("pack_id"),
        "platform": extension.get("platform"),
        "arch": extension.get("arch"),
        "channel": extension.get("channel"),
        "version": extension.get("version"),
        "git_commit": extension.get("git_commit"),
        "tag": extension.get("tag"),
        "assets": assets,
        "release_policy": release_policy_from_manifest(extension.get("release_policy"), allow_empty_assets=True),
    }
    return DOMAIN + _canonical_json(payload)


def canonical_policy_payload(policy: dict) -> bytes:
    """仅 Hub 独立发布（不含 velopack 扩展）时的外层策略规范化签名载荷。"""
    return DOMAIN + _canonical_json({"release_policy": release_policy_from_manifest(policy)})


def verify_manifest_policy(manifest: dict, *, keys: dict[str, str] | None = None) -> None:
    """校验不含 velopack 扩展的独立发布清单（如仅 Hub 发布）的顶层 Ed25519 签名。"""
    trust = manifest.get("trust") if isinstance(manifest, dict) else None
    if not isinstance(trust, dict) or trust.get("mode") != "ed25519":
        raise ValueError("发布者签名模式无效")
    if trust.get("schema") != SIGNATURE_SCHEMA:
        raise ValueError("发布者签名版本无效")
    key_id, signature = trust.get("key_id"), trust.get("signature")
    allowlist = PUBLISHER_KEYS if keys is None else keys
    if not allowlist or not isinstance(key_id, str) or key_id not in allowlist:
        raise ValueError("发布者公钥未知或信任根未配置")
    if not isinstance(signature, str):
        raise ValueError("发布者签名缺失")
    try:
        raw = base64.b64decode(signature.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("发布者签名编码无效") from exc
    if len(raw) != 64:
        raise ValueError("发布者签名长度无效")
    from cryptography.exceptions import InvalidSignature

    try:
        _load_ed25519_public(allowlist[key_id]).verify(raw, canonical_policy_payload(manifest))
    except InvalidSignature as exc:
        raise ValueError("发布者签名验证失败") from exc


def _public_key_bytes(public_key: str) -> bytes:
    try:
        raw = base64.b64decode(public_key.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("发布者公钥编码无效") from exc
    if len(raw) != 32:
        raise ValueError("发布者公钥长度无效")
    return raw


def key_id_for(public_key: str) -> str:
    return hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:16]


def _load_ed25519_public(public_key: str):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - 运行时依赖缺失必须拒绝
        raise ValueError("缺少发布者签名验证依赖") from exc
    return Ed25519PublicKey.from_public_bytes(_public_key_bytes(public_key))


def verify_release(extension: dict, *, keys: dict[str, str] | None = None,
                   release_policy: dict | None = None) -> None:
    """校验签名、固定信任根，以及清单外层策略与签名内策略的一致性。"""
    trust = extension.get("trust") if isinstance(extension, dict) else None
    if not isinstance(trust, dict) or trust.get("mode") != "ed25519":
        raise ValueError("发布者签名模式无效")
    if trust.get("schema") != SIGNATURE_SCHEMA:
        raise ValueError("发布者签名版本无效")
    key_id, signature = trust.get("key_id"), trust.get("signature")
    allowlist = PUBLISHER_KEYS if keys is None else keys
    if not allowlist or not isinstance(key_id, str) or key_id not in allowlist:
        raise ValueError("发布者公钥未知或信任根未配置")
    if not isinstance(signature, str):
        raise ValueError("发布者签名缺失")
    embedded_policy = release_policy_from_manifest(extension.get("release_policy"), allow_empty_assets=True)
    if release_policy is not None and embedded_policy != release_policy_from_manifest(release_policy, allow_empty_assets=True):
        raise ValueError("发布清单外层安全策略与签名内容不一致")
    try:
        raw = base64.b64decode(signature.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("发布者签名编码无效") from exc
    if len(raw) != 64:
        raise ValueError("发布者签名长度无效")
    from cryptography.exceptions import InvalidSignature

    try:
        _load_ed25519_public(allowlist[key_id]).verify(raw, canonical_payload(extension))
    except InvalidSignature as exc:
        raise ValueError("发布者签名验证失败") from exc


def sign_release(extension: dict, private_key_seed: bytes) -> dict:
    """构建端使用：仅在当前客户端信任根包含对应公钥时返回签名扩展。"""
    if not isinstance(private_key_seed, bytes) or len(private_key_seed) != 32:
        raise ValueError("签名私钥长度无效")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    # 先确认策略完整，避免对不完整或无安全约束的扩展签名。
    canonical_payload(extension)
    private = Ed25519PrivateKey.from_private_bytes(private_key_seed)
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    public_b64 = base64.b64encode(public).decode("ascii")
    key_id = key_id_for(public_b64)
    trusted = PUBLISHER_KEYS.get(key_id)
    if trusted is None or _public_key_bytes(trusted) != public:
        raise ValueError("签名公钥未配置为客户端固定信任根")
    signature = private.sign(canonical_payload(extension))
    signed = dict(extension)
    signed["trust"] = {
        "mode": "ed25519", "schema": SIGNATURE_SCHEMA, "key_id": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def signing_key_from_env() -> bytes:
    value = os.environ.get("MENU_CAPTURE_RELEASE_SIGNING_KEY", "")
    if not value:
        raise ValueError("缺少 MENU_CAPTURE_RELEASE_SIGNING_KEY，不能生成正式签名")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("签名私钥编码无效") from exc
    if len(raw) != 32:
        raise ValueError("签名私钥长度无效")
    return raw


def sign_manifest(manifest: dict, private_key_seed: bytes) -> dict:
    """签署完整发布清单；支持双组件、仅 Windows（含 velopack）与仅 Hub（顶层 trust）。"""
    if not isinstance(manifest, dict):
        raise ValueError("签名输入必须是完整发布清单")
    has_velopack = isinstance(manifest.get("velopack"), dict)
    if not has_velopack and "assets" not in manifest:
        raise ValueError("签名输入必须是包含资产策略或 velopack 扩展的完整发布清单")
    if has_velopack:
        policy = release_policy_from_manifest(manifest, allow_empty_assets=True)
        extension = dict(manifest["velopack"])
        if "trust" in extension:
            raise ValueError("拒绝覆盖已签名发布扩展")
        extension["release_policy"] = policy
        signed = sign_release(extension, private_key_seed)
        verify_release(signed, release_policy=policy)
        result = dict(manifest)
        result["velopack"] = signed
        return result
    # 仅 Hub 发布：assets 必须非空，使用顶层 Ed25519 trust 保护外层策略与 Hub 镜像摘要。
    if "trust" in manifest:
        raise ValueError("拒绝覆盖已签名发布清单")
    if not isinstance(private_key_seed, bytes) or len(private_key_seed) != 32:
        raise ValueError("签名私钥长度无效")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    payload_bytes = canonical_policy_payload(manifest)
    private = Ed25519PrivateKey.from_private_bytes(private_key_seed)
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    public_b64 = base64.b64encode(public).decode("ascii")
    key_id = key_id_for(public_b64)
    trusted = PUBLISHER_KEYS.get(key_id)
    if trusted is None or _public_key_bytes(trusted) != public:
        raise ValueError("签名公钥未配置为客户端固定信任根")
    result = dict(manifest)
    result["trust"] = {
        "mode": "ed25519", "schema": SIGNATURE_SCHEMA, "key_id": key_id,
        "signature": base64.b64encode(private.sign(payload_bytes)).decode("ascii"),
    }
    verify_manifest_policy(result)
    return result


def main(argv: list[str] | None = None) -> int:
    """发布清单签名/验证入口；密钥只从环境变量读取且绝不写入日志。"""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Velopack 完整发布清单 Ed25519 签名")
    parser.add_argument("command", choices=("sign", "verify"))
    parser.add_argument("path")
    args = parser.parse_args(argv)
    path = Path(args.path)
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("发布清单格式无效")
    if args.command == "sign":
        payload = sign_manifest(payload, signing_key_from_env())
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    extension = payload.get("velopack")
    if not isinstance(extension, dict):
        raise ValueError("发布清单缺少 velopack 扩展")
    verify_release(extension, release_policy=release_policy_from_manifest(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover - 构建脚本入口
    import sys

    sys.exit(main())
