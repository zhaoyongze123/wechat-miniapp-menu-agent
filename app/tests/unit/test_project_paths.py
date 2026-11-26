import pytest

from menu_capture.projects.paths import project_batch_dir, project_store_dir


B1, B2 = 'a' * 32, 'b' * 32


def test_paths_isolate_identity(tmp_path):
    paths = {project_store_dir(tmp_path, p, b, s, flat=flat)
             for p in ('Q3', 'Q4') for b in (B1, B2) for s in ('ABC', 'abc') for flat in (True, False)}
    assert len(paths) == 16
    assert all(path.is_relative_to(tmp_path) for path in paths)
    assert project_store_dir(tmp_path, 'Q4', B1, '../CON').parent == tmp_path / 'Q4' / B1


@pytest.mark.parametrize('project', ['../x', 'CON', 'LPT1', 'x/y', ''])
def test_invalid_project(tmp_path, project):
    with pytest.raises(ValueError):
        project_batch_dir(tmp_path, project, B1)


def test_existing_symlink_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (tmp_path / 'Q4').symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Current platform cannot create directory symlinks: {exc}')
    with pytest.raises(ValueError):
        project_batch_dir(tmp_path, 'Q4', B1)


def test_existing_output_api_scoped_and_legacy(tmp_path):
    from menu_capture.capture.capture_outputs import (
        archive_unqualified, batch_output_dir, store_output_dir, unqualified_store_dir,
    )

    root = tmp_path / 'output'
    legacy = store_output_dir(root, 'same', B1, 'batch')
    assert legacy == root / '清洗批次' / 'batch' / 'same'
    q3 = store_output_dir(root, 'same', B1, 'batch', project_code='Q3', store_id='s1')
    q4 = store_output_dir(root, 'same', B2, 'batch', project_code='Q4', store_id='s1')
    assert q3 != q4
    assert q4.parent == batch_output_dir(root, B2, 'renamed', project_code='Q4')
    assert q4 == store_output_dir(root, 'renamed', B2, 'renamed', project_code='Q4', store_id='s1')
    with pytest.raises(ValueError):
        store_output_dir(root, 'same', B2, project_code='Q4')
    original = tmp_path / 'source'
    original.mkdir()
    (original / 'image.png').write_bytes(b'q3')
    kwargs = {'status': 'rejected', 'stage': 'test', 'message': 'test', 'store_id': 's1'}
    reject_root = tmp_path / 'rejected'
    p3 = archive_unqualified(reject_root, original, 'same', project_code='Q3', batch_id=B1, **kwargs)
    (original / 'image.png').write_bytes(b'q4')
    p4 = archive_unqualified(reject_root, original, 'same', project_code='Q4', batch_id=B2, **kwargs)
    assert p4 == unqualified_store_dir(reject_root, 'same', project_code='Q4', batch_id=B2, store_id='s1')
    assert (p3 / 'image.png').read_bytes() == b'q3'
    assert (p4 / 'image.png').read_bytes() == b'q4'
