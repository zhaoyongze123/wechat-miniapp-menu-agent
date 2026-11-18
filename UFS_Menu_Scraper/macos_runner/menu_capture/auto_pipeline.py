"""串行自动流水线：分析完成 → 接受推荐 → 发布 → 激活 → 采集。

只通过本机 HTTP API 操作，不直接改数据库；顺序执行传入的批次，
前一批采集未结束前不会启动下一批。任何一批没有可推荐门店或采集中断时停止并报错，
避免在无人值守时把错误状态继续放大。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8765"
VERSION = "v1.0.0"


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)


class Client:
    def __init__(self) -> None:
        self._token = ""

    def _refresh(self) -> str:
        with urllib.request.urlopen(BASE + "/api/session", timeout=30) as response:
            self._token = json.loads(response.read().decode("utf-8"))["token"]
        return self._token

    def call(self, method: str, path: str, payload=None):
        token = self._token or self._refresh()
        for attempt in range(2):
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = urllib.request.Request(
                BASE + path, data=data, method=method,
                headers={"Content-Type": "application/json", "x-capture-token": token, "Origin": BASE},
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 403 and attempt == 0:
                    token = self._refresh()
                    continue
                raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:300]}") from None
        raise RuntimeError(f"{method} {path} 认证失败")


def get_batch(client: Client, batch_id: str) -> dict:
    return client.call("GET", f"/api/materials/batches/{batch_id}")


def wait_analyzed(client: Client, batch_id: str, name: str, interval: int) -> dict:
    while True:
        batch = get_batch(client, batch_id)
        progress = batch.get("progress") or {}
        log(f"{name} 分析状态 {batch.get('status')} {progress.get('done')}/{progress.get('total')}")
        if batch.get("status") == "completed":
            return batch
        if batch.get("status") in {"deleted", "error"}:
            raise RuntimeError(f"{name} 分析状态异常：{batch.get('status')}")
        time.sleep(interval)


def ensure_release(client: Client, batch: dict, interval: int) -> dict:
    name, batch_id = batch["name"], batch["batch_id"]
    result = client.call("POST", f"/api/materials/batches/{batch_id}/accept-recommendations")
    log(f"{name} 自动接受推荐：{json.dumps(result, ensure_ascii=False)}")
    batch = get_batch(client, batch_id)
    for release in batch.get("releases") or []:
        if release.get("version") == VERSION:
            log(f"{name} 复用已发布版本 {VERSION}（{release.get('store_count')} 家）")
            return release
    published = client.call("POST", f"/api/materials/batches/{batch_id}/publish", {
        "name": name, "version": VERSION, "description": "自动流水线",
    })
    release = published.get("release") or {}
    log(f"{name} 自动发布 {VERSION}：{release.get('store_count')} 家")
    if not release.get("store_count"):
        raise RuntimeError(f"{name} 发布后没有可采集门店")
    return release


def wait_idle(client: Client, interval: int) -> None:
    while True:
        status = client.call("GET", "/api/status")
        if not status.get("running"):
            return
        run = status.get("run") or {}
        task = (status.get("current_task") or {}).get("source_name", "")
        log(f"等待当前采集结束：{run.get('status')} 第 {run.get('current_index')}/{len(run.get('task_ids') or [])} 家 {task[:24]}")
        time.sleep(interval)


def pending_tasks(client: Client, snapshot: str) -> list[dict]:
    """当前激活快照下仍待采集的任务；门店数超过单次上限时用于续跑。"""
    status = client.call("GET", "/api/status")
    return [task for task in status.get("tasks", [])
            if snapshot in str(task.get("source_path", ""))
            and task.get("status") == "pending" and not task.get("reject_reason")]


def activate_batch(client: Client, batch: dict, release: dict) -> None:
    client.call("POST", f"/api/materials/batches/{batch['batch_id']}/activate", {"version": VERSION})
    log(f"{batch['name']} 已激活版本 {VERSION}")


def start_capture(client: Client, batch: dict, tasks: list[dict], round_no: int) -> dict:
    selected = tasks[:100]
    profile = client.call("GET", "/api/robot/profile")
    profile["selected_task_ids"] = [task["id"] for task in selected]
    profile["max_tasks"] = len(selected)
    client.call("POST", "/api/robot/profile", profile)
    client.call("POST", "/api/start", {})
    log(f"{batch['name']} 第 {round_no} 轮采集已启动，共 {len(selected)} 家")
    return client.call("GET", "/api/status")


def wait_run_finished(client: Client, name: str, interval: int) -> dict:
    while True:
        status = client.call("GET", "/api/status")
        if not status.get("running"):
            run = status.get("run") or {}
            log(f"{name} 采集结束：{run.get('status')}")
            return run
        run = status.get("run") or {}
        counters = status.get("counts") or {}
        log(f"{name} 采集中：第 {run.get('current_index')}/{len(run.get('task_ids') or [])} 家，"
            f"成功 {counters.get('captured', 0)}，待处理 {counters.get('pending', 0)}")
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batches", nargs="+", required=True, help="按顺序执行的批次 ID")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    client = Client()
    for batch_id in args.batches:
        batch = get_batch(client, batch_id)
        name = batch["name"]
        log(f"===== {name} 开始 =====")
        if args.dry_run:
            log(f"{name} 状态 {batch.get('status')}，推荐 {batch.get('summary', {}).get('selected_stores')} 家，"
                f"已确认 {batch.get('summary', {}).get('confirmed_stores')} 家")
            continue
        batch = wait_analyzed(client, batch_id, name, args.interval)
        release = ensure_release(client, batch, args.interval)
        wait_idle(client, args.interval)
        activate_batch(client, batch, release)
        for _ in range(10):
            tasks = pending_tasks(client, release["snapshot_id"])
            if tasks:
                break
            time.sleep(3)
        else:
            raise RuntimeError(f"{name} 激活后没有找到待采集任务")
        round_no = 0
        while tasks:
            round_no += 1
            if round_no > 20:
                raise RuntimeError(f"{name} 续跑轮次过多，流水线停止")
            start_capture(client, batch, tasks, round_no)
            run = wait_run_finished(client, name, args.interval)
            if run.get("status") != "completed":
                raise RuntimeError(f"{name} 采集未正常完成：{run.get('status')}，流水线停止")
            tasks = pending_tasks(client, release["snapshot_id"])
            if tasks:
                log(f"{name} 仍有 {len(tasks)} 家待采集，继续下一轮")
    log("全部批次处理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
