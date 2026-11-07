from __future__ import annotations

import os

import pytest

from pdm.exceptions import UninstallError
from pdm.installers.manager import InstallManager


def test_install_passes_candidate_options(mocker):
    environment = mocker.sentinel.environment
    candidate = mocker.Mock()
    candidate.req.editable = False
    candidate.requested = True
    prepared = candidate.prepare.return_value
    prepared.build.return_value = mocker.sentinel.wheel
    prepared.direct_url.return_value = mocker.sentinel.direct_url
    prepared.local_build_source_dir.return_value = None
    prepared.local_build_record.return_value = None
    install_wheel = mocker.patch(
        "pdm.installers.manager.install_wheel",
        return_value=mocker.sentinel.dist_info,
    )
    distribution_at = mocker.patch("pdm.installers.manager.Distribution.at")
    manager = InstallManager(environment, use_install_cache=True, rename_pth=True)

    result = manager.install(candidate)

    assert result is distribution_at.return_value
    candidate.prepare.assert_called_once_with(environment)
    install_wheel.assert_called_once_with(
        mocker.sentinel.wheel,
        environment,
        direct_url=mocker.sentinel.direct_url,
        install_links=True,
        rename_pth=True,
        requested=True,
        extra_metadata=None,
    )
    distribution_at.assert_called_once_with(mocker.sentinel.dist_info)


def test_install_does_not_cache_editable_candidate(mocker):
    candidate = mocker.Mock()
    candidate.req.editable = True
    prepared = candidate.prepare.return_value
    prepared.local_build_source_dir.return_value = None
    prepared.local_build_record.return_value = None
    install_wheel = mocker.patch("pdm.installers.manager.install_wheel")
    mocker.patch("pdm.installers.manager.Distribution.at")
    manager = InstallManager(mocker.sentinel.environment, use_install_cache=True)

    manager.install(candidate)

    assert install_wheel.call_args.kwargs["install_links"] is False
    prepared.build.assert_called_once_with()


def _local_build_manager(mocker, tmp_path):
    environment = mocker.Mock()
    environment.project.cache.return_value = tmp_path
    candidate = mocker.Mock()
    candidate.req.editable = False
    candidate.requested = False
    prepared = candidate.prepare.return_value
    prepared.build.return_value = mocker.sentinel.wheel
    prepared.direct_url.return_value = None
    source_dir = tmp_path / "localpkg"
    source_dir.mkdir()
    prepared.local_build_source_dir.return_value = source_dir
    mocker.patch(
        "pdm.installers.manager.install_wheel",
        return_value=mocker.sentinel.dist_info,
    )
    mocker.patch("pdm.installers.manager.Distribution.at")
    return environment, candidate, prepared, InstallManager(environment)


def test_install_records_local_build_digest_under_lock(mocker, tmp_path):
    import sys
    import types

    from pdm.models.candidates import LOCAL_BUILD_RECORD_FILE

    _environment, candidate, prepared, manager = _local_build_manager(mocker, tmp_path)
    record = {LOCAL_BUILD_RECORD_FILE: b'{"version": 1}'}
    prepared.local_build_record.return_value = record

    created_locks: list[str] = []

    class FakeFileLock:
        def __init__(self, path, is_singleton=False):
            created_locks.append(os.path.abspath(path))

        def acquire(self, timeout):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_filelock = types.ModuleType("filelock")
    fake_filelock.FileLock = FakeFileLock
    mocker.patch.dict(sys.modules, {"filelock": fake_filelock})

    manager.install(candidate)

    from pdm.installers.manager import install_wheel

    assert install_wheel.call_args.kwargs["extra_metadata"] is record
    assert len(created_locks) == 1 and "local-build-" in created_locks[0]


def test_install_lock_timeout_raises_installation_error(mocker, tmp_path):
    import sys
    import types

    from pdm.exceptions import InstallationError

    _environment, candidate, prepared, manager = _local_build_manager(mocker, tmp_path)
    prepared.local_build_record.return_value = None

    class FakeTimeout(Exception):
        pass

    fake_filelock = types.ModuleType("filelock")

    class FakeFileLock:
        def __init__(self, path, is_singleton=False):
            pass

        def acquire(self, timeout):
            raise FakeTimeout()

    fake_filelock.FileLock = FakeFileLock
    fake_filelock.Timeout = FakeTimeout
    mocker.patch.dict(sys.modules, {"filelock": fake_filelock})

    with pytest.raises(InstallationError):
        manager.install(candidate)


def test_get_paths_to_remove_uses_environment(mocker):
    environment = mocker.sentinel.environment
    dist = mocker.sentinel.dist
    from_dist = mocker.patch("pdm.installers.manager.StashedRemovePaths.from_dist")
    manager = InstallManager(environment)

    result = manager.get_paths_to_remove(dist)

    assert result is from_dist.return_value
    from_dist.assert_called_once_with(dist, environment=environment)


def test_uninstall_commits_removed_paths(mocker):
    dist = mocker.Mock()
    dist.metadata.get.return_value = "demo"
    manager = InstallManager(mocker.sentinel.environment)
    remove_paths = mocker.patch.object(manager, "get_paths_to_remove").return_value

    manager.uninstall(dist)

    remove_paths.remove.assert_called_once_with()
    remove_paths.commit.assert_called_once_with()
    remove_paths.rollback.assert_not_called()


def test_uninstall_rolls_back_after_remove_error(mocker):
    dist = mocker.Mock()
    manager = InstallManager(mocker.sentinel.environment)
    remove_paths = mocker.patch.object(manager, "get_paths_to_remove").return_value
    error = OSError("cannot remove")
    remove_paths.remove.side_effect = error

    with pytest.raises(UninstallError) as exc_info:
        manager.uninstall(dist)

    assert exc_info.value.__cause__ is error
    remove_paths.commit.assert_not_called()
    remove_paths.rollback.assert_called_once_with()


def test_overwrite_preserves_new_distribution_paths(mocker):
    dist = mocker.Mock()
    dist.metadata.get.return_value = "demo"
    candidate = mocker.sentinel.candidate
    installed = mocker.sentinel.installed
    old_paths = mocker.Mock()
    new_paths = mocker.Mock()
    manager = InstallManager(mocker.sentinel.environment)
    mocker.patch.object(manager, "install", return_value=installed)
    mocker.patch.object(manager, "get_paths_to_remove", side_effect=[old_paths, new_paths])

    manager.overwrite(dist, candidate)

    old_paths.difference_update.assert_called_once_with(new_paths)
    old_paths.remove.assert_called_once_with()
    old_paths.commit.assert_called_once_with()
    old_paths.rollback.assert_not_called()


def test_overwrite_rolls_back_after_remove_error(mocker):
    old_paths = mocker.Mock()
    old_paths.remove.side_effect = OSError("cannot remove")
    manager = InstallManager(mocker.sentinel.environment)
    mocker.patch.object(manager, "install")
    mocker.patch.object(manager, "get_paths_to_remove", side_effect=[old_paths, mocker.Mock()])

    with pytest.raises(UninstallError):
        manager.overwrite(mocker.Mock(), mocker.sentinel.candidate)

    old_paths.commit.assert_not_called()
    old_paths.rollback.assert_called_once_with()
