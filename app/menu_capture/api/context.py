"""路由层的显式依赖容器。

各路由模块不再读取 create_app 的闭包变量，而是通过 ApiContext 拿到自己真正需要的服务。
新增域时按需扩展字段，避免把无关内部变量塞进上下文。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from menu_capture.annotation.annotation import AnnotationSession
from menu_capture.capture.uipath_runner import UiPathRunner
from menu_capture.core.connections import ConnectionManager
from menu_capture.core.environment import EnvironmentDiagnostics
from menu_capture.materials.materials import MaterialService
from menu_capture.materials.rejections import RejectionService
from menu_capture.shared.hub_update import HubInstallProxy
from menu_capture.shared.delivery import DeliveryService
from menu_capture.shared.shared import SharedHub
from menu_capture.shared.shared_worker import SharedWorker
from menu_capture.storage.store import TaskStore
from menu_capture.updates.update_install import InstallController
from menu_capture.updates.updates import UpdateService
from menu_capture.vision.model_usage import UsageLedger


@dataclass
class ApiContext:
    """跨路由共享的服务引用，只承载路由实际使用的依赖。"""

    updates: UpdateService
    install: InstallController | HubInstallProxy
    model_usage: UsageLedger
    environment: EnvironmentDiagnostics
    connections: ConnectionManager
    runner: UiPathRunner
    annotations: AnnotationSession
    rejections: RejectionService
    store: TaskStore
    materials: MaterialService
    hub: SharedHub | None
    worker: SharedWorker | None
    delivery: DeliveryService | None
    control_lock: threading.RLock
    # 采集/标注运行中禁止修改配置、素材与环境的统一闸门，由 create_app 注入。
    idle: Callable[[], None]
    # 所有业务路由共享同一组项目归属校验，避免各域自行猜测资源归属。
    project_scope: Callable[..., str]
    batch_scope: Callable[..., str]
    task_scope: Callable[..., str]
    role: str
