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
    (tmp_path / 'Q4').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        project_batch_dir(tmp_path, 'Q4', B1)
