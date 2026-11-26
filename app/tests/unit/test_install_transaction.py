import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from menu_capture.updates.install_transaction import InstallTransaction, TransactionError


def create(tx):
    return tx.create(previous_version='0.3.4', target_version='0.4.0', channel='stable',
                     schema_baseline={'objects': []}, target_asset={'sha256': 'a'*64})


def competing_create(path):
    try:
        create(InstallTransaction(Path(path)))
        return True
    except TransactionError:
        return False


def test_roundtrip(tmp_path):
    tx = InstallTransaction(tmp_path)
    assert tx.read() == {}
    row = create(tx)
    assert tx.read() == row
    assert row['status'] == 'prepared'


def test_concurrent_creation(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(competing_create, [str(tmp_path)] * 8)) == 1


def test_stale_transaction_cannot_write(tmp_path):
    tx = InstallTransaction(tmp_path)
    first = create(tx)
    tx.transition(first['transaction_id'], 'prepared', 'failed')
    second = create(tx)
    with pytest.raises(TransactionError):
        tx.transition(first['transaction_id'], 'prepared', 'stopping')
    assert tx.read() == second


@pytest.mark.parametrize('bad', ['{', '[]', '{"status":"restarting"}', '{"status":"prepared"}'])
def test_invalid_journal_blocks(tmp_path, bad):
    tx = InstallTransaction(tmp_path)
    tx.path.write_text(bad)
    with pytest.raises(TransactionError):
        create(tx)


def test_no_version_only_completion(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    with pytest.raises(TransactionError):
        tx.transition(row['transaction_id'], 'prepared', 'completed')


def test_unknown_apply_blocks_retry(tmp_path):
    tx = InstallTransaction(tmp_path)
    tid = create(tx)['transaction_id']
    tx.transition(tid, 'prepared', 'stopping')
    tx.transition(tid, 'stopping', 'applying')
    tx.transition(tid, 'applying', 'recovery_required')
    with pytest.raises(TransactionError):
        create(tx)
    with pytest.raises(TransactionError):
        tx.transition(tid, 'recovery_required', 'failed')


def test_successful_verified_path(tmp_path):
    tx = InstallTransaction(tmp_path)
    tid = create(tx)['transaction_id']
    for before, after in [('prepared','stopping'), ('stopping','applying'), ('applying','verifying'), ('verifying','completed')]:
        tx.transition(tid, before, after)
    assert tx.read()['finished_at']
    assert create(tx)['transaction_id'] != tid


def test_rollback_once(tmp_path):
    tx = InstallTransaction(tmp_path)
    tid = create(tx)['transaction_id']
    for before, after in [('prepared','stopping'), ('stopping','applying'), ('applying','verifying'), ('verifying','rolling_back'), ('rolling_back','rolled_back')]:
        tx.transition(tid, before, after)
    assert tx.read()['rollback_attempts'] == 1
