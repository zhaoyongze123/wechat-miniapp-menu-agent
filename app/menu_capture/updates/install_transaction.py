"""Short cross-process lock and atomic installation journal transitions."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = frozenset({'prepared', 'stopping', 'applying', 'verifying', 'rolling_back', 'recovery_required'})
TERMINAL = frozenset({'completed', 'failed', 'rolled_back'})
TRANSITIONS = {
    'prepared': {'stopping', 'failed'},
    'stopping': {'applying', 'failed', 'recovery_required'},
    'applying': {'verifying', 'recovery_required'},
    'verifying': {'completed', 'rolling_back', 'recovery_required'},
    'rolling_back': {'rolled_back', 'recovery_required'},
    'recovery_required': set(),
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TransactionError(ValueError):
    pass


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
        if not isinstance(payload, dict) or payload.get('status') not in ACTIVE | TERMINAL:
            raise TransactionError('安装事务格式无效或为旧格式，需人工恢复')
        if payload.get('schema_version') != 3 or not isinstance(payload.get('transaction_id'), str) or not payload['transaction_id']:
            raise TransactionError('安装事务身份无效或为旧格式，需人工恢复')
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
                deadline = time.monotonic() + 3
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
                deadline = time.monotonic() + 3
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

    def create(self, *, previous_version, target_version, channel, schema_baseline, target_asset):
        with self._lock():
            old = self.read()
            if old and old['status'] not in TERMINAL:
                raise TransactionError('已有安装事务正在执行或等待恢复')
            if channel not in {'stable', 'preview'} or not previous_version or not target_version:
                raise TransactionError('安装事务目标无效')
            stamp = now()
            payload = {
                'schema_version': 3, 'transaction_id': uuid.uuid4().hex, 'status': 'prepared',
                'previous_version': previous_version, 'target_version': target_version,
                'channel': channel, 'schema_baseline': schema_baseline, 'target_asset': target_asset,
                'started_at': stamp, 'updated_at': stamp, 'finished_at': '', 'error': '',
                'message': '已登记安装事务，等待启动器接手', 'rollback_attempts': 0,
            }
            self._write(payload)
            return payload

    def transition(self, transaction_id, expected, status, *, message='', error=''):
        with self._lock():
            payload = self.read()
            if payload.get('transaction_id') != transaction_id or payload.get('status') != expected:
                raise TransactionError('安装事务身份或前置状态不一致')
            if status not in TRANSITIONS.get(expected, ()):
                raise TransactionError('禁止的安装事务状态转换')
            stamp = now()
            payload.update(status=status, message=message, error=error, updated_at=stamp,
                           finished_at=stamp if status in TERMINAL else '')
            if status == 'rolling_back':
                payload['rollback_attempts'] += 1
            self._write(payload)
            return payload
