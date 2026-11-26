from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from menu_capture.updates.install_transaction import InstallTransaction, TransactionError

PREVIOUS_VERSION = "0.3.4"
PREVIOUS_COMMIT = "a" * 40
TARGET_VERSION = "0.4.0"
TARGET_COMMIT = "b" * 40


def create(tx):
    recovery = {
        "version": PREVIOUS_VERSION, "commit": PREVIOUS_COMMIT, "channel": "stable",
        "name": "Yipu.MenuCapture.0.3.4-full.nupkg", "size": 1, "sha256": "c" * 64,
        "path": "/data/recovery/0.3.4/Yipu.MenuCapture.0.3.4-full.nupkg",
        "source": {"type": "official-release", "url": "https://github.com/owner/repo/releases/download/v0.3.4"},
        "inner_manifest": {"version": PREVIOUS_VERSION, "commit": PREVIOUS_COMMIT},
    }
    return tx.create(
        previous_version=PREVIOUS_VERSION, previous_commit=PREVIOUS_COMMIT,
        target_version=TARGET_VERSION, target_commit=TARGET_COMMIT, channel="stable",
        schema_baseline={"version": PREVIOUS_VERSION, "schema": {"objects": []}, "snapshot": {"sha256": "d" * 64}},
        target_asset={"version": TARGET_VERSION, "git_commit": TARGET_COMMIT, "full": {"name": "full.nupkg"}},
        recovery_asset=recovery,
    )


def claim(tx, row, *, owner="owner", pid=123, started=1.0):
    return tx.claim(row["transaction_id"], row["status"], owner_token=owner,
                    pid=pid, process_started_at=started, phase="test")


def move(tx, row, status, *, owner="owner"):
    return tx.transition(row["transaction_id"], row["status"], status, owner_token=owner)


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
    assert row["status"] == "prepared"


def test_concurrent_creation(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(competing_create, [str(tmp_path)] * 8)) == 1


def test_stale_transaction_cannot_write(tmp_path):
    tx = InstallTransaction(tmp_path)
    first = create(tx)
    claim(tx, first)
    move(tx, tx.read(), "failed")
    second = create(tx)
    with pytest.raises(TransactionError):
        tx.transition(first["transaction_id"], "prepared", "stopping", owner_token="owner")
    assert tx.read() == second


@pytest.mark.parametrize("bad", ["{", "[]", '{"status":"restarting"}', '{"status":"prepared"}'])
def test_invalid_journal_blocks(tmp_path, bad):
    tx = InstallTransaction(tmp_path)
    tx.path.write_text(bad)
    with pytest.raises(TransactionError):
        create(tx)


def test_no_version_only_completion(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    claim(tx, row)
    with pytest.raises(TransactionError):
        tx.transition(row["transaction_id"], "prepared", "completed", owner_token="owner")


def test_unknown_apply_blocks_retry(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    claim(tx, row)
    for status in ("stopping", "applying"):
        row = move(tx, tx.read(), status)
    row = move(tx, tx.read(), "recovery_required")
    with pytest.raises(TransactionError):
        create(tx)
    with pytest.raises(TransactionError):
        move(tx, tx.read(), "failed")


def test_successful_verified_path(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    claim(tx, row)
    for status in ("stopping", "applying", "verifying", "completed"):
        row = move(tx, tx.read(), status)
    assert tx.read()["finished_at"]
    assert create(tx)["transaction_id"] != row["transaction_id"]


def test_takeover_requires_dead_identity_evidence_and_cas(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    claim(tx, row, owner="first", pid=123, started=10.5)
    with pytest.raises(TransactionError):
        tx.claim(row["transaction_id"], "prepared", owner_token="second", pid=456,
                 process_started_at=20.5, phase="verifying", takeover_evidence={"reason": "unknown"})
    evidence = {
        "previous_owner_token": "first", "previous_pid": 123,
        "previous_process_started_at": 10.5, "observed_state": "dead",
        "detected_at": "2026-11-26T00:00:00+00:00",
    }
    claimed = tx.claim(row["transaction_id"], "prepared", owner_token="second", pid=456,
                       process_started_at=20.5, phase="verifying", takeover_evidence=evidence)
    assert claimed["owner_token"] == "second"
    assert claimed["evidence"]["takeovers"] == [evidence]


def test_same_owner_token_cannot_move_to_another_process(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    claim(tx, row, owner="same", pid=123, started=10.5)
    with pytest.raises(TransactionError, match="不能用于接管其他进程"):
        tx.claim(row["transaction_id"], "prepared", owner_token="same", pid=456,
                 process_started_at=20.5, phase="verifying")


def test_rollback_once(tmp_path):
    tx = InstallTransaction(tmp_path)
    row = create(tx)
    claim(tx, row)
    for status in ("stopping", "applying", "verifying", "rolling_back", "rolled_back"):
        row = move(tx, tx.read(), status)
    assert tx.read()["rollback_attempts"] == 1
