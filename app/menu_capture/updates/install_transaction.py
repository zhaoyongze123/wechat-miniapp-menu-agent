"""Short cross-process lock and atomic installation journal transitions.

事务是唯一状态源：只有持有 owner_token 的协调器可以推进阶段；
旧事务、损坏事务或未验证恢复包一律进入 recovery_required，不自动丢弃。
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from menu_capture.updates.velopack_identity import validate_local_asset

ACTIVE = frozenset({'prepared', 'stopping', 'applying', 'verifying', 'rolling_back', 'recovery_required'})
TERMINAL = frozenset({'completed', 'failed', 'rolled_back'})
TRANSITIONS = {
    'prepared': {'stopping', 'failed'},
    'stopping': {'applying', 'failed', 'recovery_required'},
    'applying': {'verifying', 'rolling_back', 'recovery_required'},
    'verifying': {'completed', 'rolling_back', 'recovery_required'},
    'rolling_back': {'rolled_back', 'recovery_required'},
    'recovery_required': set(),
}

REQUIRED_FIELDS = (
    'schema_version', 'transaction_id', 'status', 'previous_version', 'previous_commit',
    'target_version', 'target_commit', 'channel', 'schema_baseline', 'target_asset',
    'recovery_asset', 'coordinator', 'authorization', 'evidence', 'rollback_attempts',
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TransactionError(ValueError):
    pass


def _sha(value):
    import re
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) is not None


class InstallTransaction:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / 'update-install.json'
        self.lock_path = self.path.with_suffix('.lock')

    def read(self):
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise TransactionError('安装事务无法读取，禁止继续更新') from exc
        if not isinstance(payload, dict):
            raise TransactionError('安装事务格式无效或为旧格式，需人工恢复')
        if payload.get('schema_version') != 3:
            raise TransactionError('旧版安装事务仅作迁移诊断，需人工恢复')
        if payload.get('status') not in ACTIVE | TERMINAL:
            raise TransactionError('安装事务状态无效，需人工恢复')
        missing = [key for key in REQUIRED_FIELDS if key not in payload]
        if missing or not isinstance(payload.get('transaction_id'), str) or not payload['transaction_id']:
            raise TransactionError('安装事务身份或必填字段缺失，需人工恢复')
        if (not isinstance(payload['target_asset'], dict) or not isinstance(payload['recovery_asset'], dict)
                or not isinstance(payload['schema_baseline'], dict) or not isinstance(payload['coordinator'], dict)
                or not isinstance(payload['authorization'], dict) or not isinstance(payload['evidence'], dict)
                or type(payload['rollback_attempts']) is not int):
            raise TransactionError('安装事务字段类型无效，需人工恢复')
        return payload

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open('a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b'\0')
                    stream.flush()
                stream.seek(0)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise TransactionError('安装事务锁繁忙') from exc
                        time.sleep(.05)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise TransactionError('安装事务锁繁忙') from exc
                        time.sleep(.05)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write(self, payload):
        temp = self.path.with_name(f'{self.path.name}.{uuid.uuid4().hex}.tmp')
        try:
            with temp.open('x', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def create(self, *, previous_version, previous_commit, target_version, target_commit,
               channel, schema_baseline, target_asset, recovery_asset, authorization=None):
        with self._lock():
            old = self.read()
            if old and old['status'] not in TERMINAL:
                raise TransactionError('已有安装事务正在执行或等待恢复')
            if channel not in {'stable', 'preview'} or not previous_version or not target_version:
                raise TransactionError('安装事务目标无效')
            if previous_version == target_version or not _sha(previous_commit) or not _sha(target_commit):
                raise TransactionError('安装事务版本或 commit 无效')
            if (not isinstance(target_asset, dict) or target_asset.get('version') != target_version
                    or target_asset.get('git_commit') != target_commit
                    or not isinstance(target_asset.get('full'), dict)):
                raise TransactionError('安装事务目标发布身份无效')
            if (not isinstance(schema_baseline, dict)
                    or schema_baseline.get('version') != previous_version
                    or not isinstance(schema_baseline.get('schema'), dict)):
                raise TransactionError('安装事务数据库基线无效')
            try:
                validate_local_asset(recovery_asset, version=previous_version, commit=previous_commit)
            except ValueError as exc:
                raise TransactionError(f'安装事务恢复包身份无效：{exc}') from exc
            stamp = now()
            payload = {
                'schema_version': 3, 'transaction_id': uuid.uuid4().hex, 'status': 'prepared',
                'previous_version': previous_version, 'previous_commit': previous_commit,
                'target_version': target_version, 'target_commit': target_commit,
                'channel': channel, 'schema_baseline': schema_baseline,
                'target_asset': target_asset, 'recovery_asset': recovery_asset,
                'coordinator': {}, 'owner_token': '',
                'authorization': dict(authorization or {}), 'evidence': {},
                'started_at': stamp, 'updated_at': stamp, 'finished_at': '', 'error': '',
                'message': '已登记安装事务，等待恢复协调器接手', 'rollback_attempts': 0,
            }
            self._write(payload)
            return payload

    def claim(self, transaction_id, expected, *, owner_token, pid, process_started_at, phase,
              takeover_evidence=None):
        """取得事务所有权；不同 owner 只有在提供实测接管证据时才允许覆盖。"""
        with self._lock():
            payload = self.read()
            if payload.get('transaction_id') != transaction_id or payload.get('status') != expected:
                raise TransactionError('安装事务身份或前置状态不一致')
            try:
                started = float(process_started_at)
            except (TypeError, ValueError) as exc:
                raise TransactionError('安装事务协调器创建时间无效') from exc
            if (not isinstance(owner_token, str) or not owner_token
                    or type(pid) is not int or pid <= 0
                    or not math.isfinite(started) or started <= 0
                    or not isinstance(phase, str) or not phase):
                raise TransactionError('安装事务协调器身份无效')
            current = payload.get('owner_token') or ''
            if current and current != owner_token:
                previous = payload.get('coordinator') or {}
                valid_takeover = (
                    isinstance(takeover_evidence, dict)
                    and takeover_evidence.get('observed_state') == 'dead'
                    and takeover_evidence.get('previous_owner_token') == current
                    and type(takeover_evidence.get('previous_pid')) is int
                    and takeover_evidence.get('previous_pid') == previous.get('pid')
                    and isinstance(takeover_evidence.get('previous_process_started_at'), (int, float))
                    and float(takeover_evidence.get('previous_process_started_at') or 0)
                        == float(previous.get('process_started_at') or 0)
                    and bool(takeover_evidence.get('detected_at'))
                )
                if not valid_takeover:
                    raise TransactionError('安装事务已由其他协调器持有')
                payload['evidence'].setdefault('takeovers', []).append(takeover_evidence)
            elif current == owner_token:
                previous = payload.get('coordinator') or {}
                if (previous.get('pid') != pid
                        or float(previous.get('process_started_at') or 0) != started):
                    raise TransactionError('协调器令牌不能用于接管其他进程')
            elif takeover_evidence is not None:
                raise TransactionError('没有旧协调器时不接受接管证据')
            stamp = now()
            payload.update(owner_token=owner_token, updated_at=stamp, coordinator={
                'pid': pid, 'process_started_at': started, 'phase': phase,
                'claimed_at': stamp,
            })
            self._write(payload)
            return payload

    def transition(self, transaction_id, expected, status, *, owner_token, message='',
                   error='', evidence=None):
        with self._lock():
            payload = self.read()
            if payload.get('transaction_id') != transaction_id or payload.get('status') != expected:
                raise TransactionError('安装事务身份或前置状态不一致')
            if not owner_token or payload.get('owner_token') != owner_token:
                raise TransactionError('安装事务所有权不匹配')
            if status not in TRANSITIONS.get(expected, ()):
                raise TransactionError('禁止的安装事务状态转换')
            stamp = now()
            payload.update(status=status, message=message, error=error, updated_at=stamp,
                           finished_at=stamp if status in TERMINAL else '')
            if status == 'rolling_back':
                payload['rollback_attempts'] += 1
            if evidence is not None:
                if not isinstance(evidence, dict):
                    raise TransactionError('安装事务证据格式无效')
                payload['evidence'].update(evidence)
            self._write(payload)
            return payload

    def add_evidence(self, transaction_id, *, owner_token, patch):
        with self._lock():
            payload = self.read()
            if payload.get('transaction_id') != transaction_id:
                raise TransactionError('安装事务身份不一致')
            if not owner_token or payload.get('owner_token') != owner_token:
                raise TransactionError('安装事务所有权不匹配')
            if payload.get('status') not in ACTIVE:
                raise TransactionError('安装事务已结束')
            if not isinstance(patch, dict):
                raise TransactionError('安装事务证据格式无效')
            payload['evidence'].update(patch)
            payload['updated_at'] = now()
            self._write(payload)
            return payload
