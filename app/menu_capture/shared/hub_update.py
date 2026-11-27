"""Hub 端更新入口：把安装意图转交给宿主机执行器。

Hub 业务容器只负责授权、组装请求与展示状态；真正的镜像下载、切换与回滚
由宿主机上的 `deploy/hub-update/hub_updater.py` 完成，容器不接触 Docker Socket。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from typing import Callable

# 执行器可能正在下载数百 MB 镜像，提交线程必须比状态查询等更久。
APPLY_TIMEOUT_SECONDS = 1800.0
STATUS_TIMEOUT_SECONDS = 3.0
STATUS_CACHE_SECONDS = 2.0
# OCI 分层交付只允许固定 GHCR 仓库与不可变摘要；格式不合法就退回完整归档。
OCI_REGISTRY_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class HubInstallProxy:
    """与 Windows `InstallController` 保持同一组方法：status/start/active/gate/recover。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        latest_provider: Callable[[], dict],
        enabled: bool = True,
        request: Callable[[str, str, dict | None, float], dict] | None = None,
        timeout: float = STATUS_TIMEOUT_SECONDS,
        authorization_check: Callable[[str], bool] | None = None,
        authorization_release: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.latest_provider = latest_provider
        self._supported = bool(enabled and self.base_url and self.token)
        self._request = request or self._http
        self._timeout = timeout
        # 授权消费/归还在更新服务锁内线性化；调用它时只持有本代理锁，
        # 与取消路径共用同一把更新锁，保证“取消成功”与“已提交执行器”不会同时成立。
        self.authorization_check = authorization_check
        self.authorization_release = authorization_release
        self._lock = threading.RLock()
        self._cache: dict = {}
        self._cache_at = 0.0

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def gate(self) -> threading.RLock:
        """采集/标注启动前检查的闸门；与 Windows 安装共用同一套调用方式。"""
        return self._lock

    @staticmethod
    def _oci_capable(snapshot: dict) -> bool:
        capabilities = (snapshot or {}).get("capabilities")
        return isinstance(capabilities, dict) and capabilities.get("oci") is True

    @property
    def oci_supported(self) -> bool:
        """只读最近一次执行器快照；旧执行器没有 capabilities 字段，一律按不支持处理。

        该属性不得发起网络请求：更新服务可能在状态锁内读取它。
        """
        return self._oci_capable(self._cache)

    @property
    def reason(self) -> str:
        if self._supported:
            return ""
        return "Hub 镜像更新执行器未配置（需要 HUB_UPDATE_URL 与 HUB_UPDATE_TOKEN，并在宿主机部署执行器）"

    def _http(self, method: str, path: str, payload: dict | None, timeout: float) -> dict:
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            parsed = json.loads(response.read(1024 * 1024).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("执行器返回格式无效")
        return parsed

    def status(self, *, force: bool = False) -> dict:
        if not self._supported:
            return {
                "supported": False, "reason": self.reason, "active": False,
                "status": "idle", "target_version": "", "previous_version": "",
                "message": "", "error": "", "started_at": "", "finished_at": "",
                "capabilities": {"oci": False},
            }
        with self._lock:
            now = time.monotonic()
            if not force and self._cache and now - self._cache_at < STATUS_CACHE_SECONDS:
                return dict(self._cache)
            try:
                data = self._request("GET", "/status", None, self._timeout)
                data = {"supported": True, "reason": "", **data}
            except Exception as exc:  # noqa: BLE001 - 执行器不可达时如实降级，不能假装在线
                # 读不到状态时不能让采集永久被挡：active 按 False 处理，并通过 reason 暴露异常。
                data = {
                    **(self._cache or {}),
                    "supported": True,
                    "reason": f"更新执行器暂不可达：{exc}",
                    "active": False,
                    "status": (self._cache or {}).get("status") or "idle",
                }
            self._cache, self._cache_at = dict(data), now
            return dict(data)

    def active(self) -> bool:
        return bool(self.status().get("active"))

    def active_nonblocking(self) -> bool:
        """事件循环等不能阻塞的调用方使用：只读最近一次状态快照，绝不发网络请求。

        与 Windows 执行器保持一致的方法名；真正的采集闸门仍由 gate + active() 闭合，
        这里只用于中间件提前返回 409，读不到快照时按未安装处理，不能把整个站点拖住。
        """
        return bool(self._cache.get("active"))

    def recover(self) -> None:
        """执行器自己会在启动/查询时收尾遗留事务，这里只刷新一次快照。"""
        if self._supported:
            self.status(force=True)

    def start(self, version: str, confirm: str, *, require_authorization: bool = False) -> dict:
        """提交一次 Hub 镜像更新。

        require_authorization 用于后台调度：真正提交给宿主机执行器前消费一次更新授权，
        使用户在锁外校验期间取消更新时镜像不会继续切换。授权消费与取消共用更新服务锁，
        先到者生效；latest_provider 会进入更新服务锁，必须在取本地锁之前调用，
        否则与状态查询路径交叉持锁会形成 ABBA 死锁。
        """
        version = str(version or "").strip()
        if not self._supported:
            raise ValueError(self.reason)
        if not version or str(confirm or "").strip() != version:
            raise ValueError("confirm 必须与目标版本一致")
        latest = self.latest_provider() or {}
        if str(latest.get("version") or "") != version:
            raise ValueError("目标版本不是当前最新版本")
        asset = next(
            (item for item in (latest.get("assets") or []) if item.get("kind") == "hub_image"),
            None,
        )
        if not asset:
            raise ValueError("最新版本没有可用的 Hub 镜像资产")
        payload = {
            "version": version,
            "confirm": version,
            "sha256": str(asset.get("sha256") or ""),
            "size": int(asset.get("size") or 0),
            "url": str(asset.get("url") or ""),
        }
        if not payload["sha256"] or payload["size"] <= 0 or not payload["url"]:
            raise ValueError("镜像资产缺少校验信息，已拒绝安装")
        with self._lock:
            # 能力必须以最终刷新结果为准：缓存可能已过期，授权或升级期间宿主能力也可能变化。
            fresh = self.status(force=True)
            if fresh.get("active"):
                raise ValueError("已有更新任务正在执行")
            # 只有宿主执行器明确声明 OCI 能力时才发送新字段；旧执行器继续只收完整归档。
            # url/sha256/size 始终保留，作为拉取失败时的归档回退依据。
            if self._oci_capable(fresh):
                registry = str(asset.get("registry") or "").strip().lower()
                digest = str(asset.get("digest") or "").strip().lower()
                if OCI_REGISTRY_RE.fullmatch(registry) and OCI_DIGEST_RE.fullmatch(digest):
                    payload["image"] = registry
                    payload["digest"] = digest
            claimed = False
            if require_authorization and self.authorization_check is not None:
                if not self.authorization_check(version):
                    raise ValueError("更新授权已取消，安装未启动")
                claimed = True
            self._cache = {
                "supported": True,
                "reason": "",
                "active": True,
                "status": "starting",
                "target_version": version,
                "previous_version": "",
                "message": "已提交给宿主机执行器，切换期间页面会短暂断开",
                "error": "",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "finished_at": "",
                # 保留能力声明，避免提交后能力缓存丢失导致调度误判为“仍需下载归档”。
                "capabilities": self._cache.get("capabilities") or {"oci": False},
            }
            self._cache_at = time.monotonic()
            # 返回提交瞬间的快照：线程可能在返回前就把状态写回缓存。
            result = dict(self._cache)
        # 执行器同步执行完整事务；Hub 请求必须尽快返回，否则页面会在切换中一直挂起。
        try:
            threading.Thread(target=self._submit, args=(payload, claimed), daemon=True).start()
        except BaseException:
            # 提交线程未能启动时归还授权，后台调度才有机会重试。
            self._release_claim(claimed, version)
            raise
        return result

    def _release_claim(self, claimed: bool, version: str) -> None:
        if claimed and self.authorization_release is not None:
            self.authorization_release(version)

    def _submit(self, payload: dict, claimed: bool = False) -> None:
        try:
            data = self._request("POST", "/apply", payload, APPLY_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 提交失败要转成可展示的状态
            # 执行器没有接手这次更新：归还授权，避免一次可重试失败永久占住安排。
            self._release_claim(claimed, str(payload.get("version") or ""))
            self._cache = {
                **self._cache,
                "active": False,
                "status": "failed",
                "message": f"提交更新执行器失败：{exc}",
                "error": str(exc),
            }
            self._cache_at = time.monotonic()
            return
        self._cache = {
            "supported": True,
            "reason": "",
            **data,
            # 执行器的 /apply 响应不重复能力声明；沿用提交前快照，避免能力缓存丢失。
            "capabilities": data.get("capabilities") or self._cache.get("capabilities") or {"oci": False},
        }
        self._cache_at = time.monotonic()
