from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Iterator
from importlib.metadata import Distribution
from typing import TYPE_CHECKING

from pdm import termui
from pdm.exceptions import InstallationError, UninstallError
from pdm.installers.installers import install_wheel
from pdm.installers.uninstallers import BaseRemovePaths, StashedRemovePaths

if TYPE_CHECKING:
    from pdm.environments import BaseEnvironment
    from pdm.models.candidates import Candidate, PreparedCandidate

#: Seconds to wait for another PDM process that is building the same local
#: directory before giving up with a clear error.
LOCAL_BUILD_LOCK_TIMEOUT = 300


class InstallManager:
    """The manager that performs the installation and uninstallation actions."""

    def __init__(
        self, environment: BaseEnvironment, *, use_install_cache: bool = False, rename_pth: bool = False
    ) -> None:
        self.environment = environment
        self.use_install_cache = use_install_cache
        self.rename_pth = rename_pth

    @contextlib.contextmanager
    def _local_build_lock(self, prepared: PreparedCandidate) -> Iterator[None]:
        """Serialize builds of the same local directory across threads and
        processes, so concurrent installations can never interleave writes
        into one environment."""
        source_dir = prepared.local_build_source_dir()
        if source_dir is None:
            # Only local directory requirements build in tree.
            yield
            return
        import filelock

        locks_dir = self.environment.project.cache("locks")
        source_key = hashlib.sha256(os.path.normcase(os.path.abspath(source_dir)).encode("utf-8")).hexdigest()
        lock_path = locks_dir / f"local-build-{source_key[:32]}.lock"
        # is_singleton makes the lock shared per path inside this process,
        # hence also coordinating parallel installer threads.
        lock = filelock.FileLock(lock_path, is_singleton=True)
        try:
            with lock.acquire(timeout=LOCAL_BUILD_LOCK_TIMEOUT):
                yield
        except filelock.Timeout:
            raise InstallationError(
                f"Timed out waiting for another PDM process that is building the local "
                f"package at {source_dir}. Make sure no other installation is still "
                f"running, or remove the stale lock file: {lock_path}"
            ) from None

    def install(self, candidate: Candidate) -> Distribution:
        """Install a candidate into the environment, return the distribution"""
        prepared = candidate.prepare(self.environment)
        extra_metadata = prepared.local_build_record()
        with self._local_build_lock(prepared):
            dist_info = install_wheel(
                prepared.build(),
                self.environment,
                direct_url=prepared.direct_url(),
                install_links=self.use_install_cache and not candidate.req.editable,
                rename_pth=self.rename_pth,
                requested=candidate.requested,
                extra_metadata=extra_metadata,
            )
        return Distribution.at(dist_info)

    def get_paths_to_remove(self, dist: Distribution) -> BaseRemovePaths:
        """Get the path collection to be removed from the disk"""
        return StashedRemovePaths.from_dist(dist, environment=self.environment)

    def uninstall(self, dist: Distribution) -> None:
        """Perform the uninstallation for a given distribution"""
        remove_path = self.get_paths_to_remove(dist)
        dist_name = dist.metadata.get("Name")
        termui.logger.info("Removing distribution %s", dist_name)
        try:
            remove_path.remove()
            remove_path.commit()
        except OSError as e:
            termui.logger.warning("Error occurred during uninstallation, roll back the changes now.")
            remove_path.rollback()
            raise UninstallError(e) from e

    def overwrite(self, dist: Distribution, candidate: Candidate) -> None:
        """An in-place update to overwrite the distribution with a new candidate"""
        paths_to_remove = self.get_paths_to_remove(dist)
        termui.logger.info("Overwriting distribution %s", dist.metadata.get("Name"))
        installed = self.install(candidate)
        installed_paths = self.get_paths_to_remove(installed)
        # Remove the paths that are in the new distribution
        paths_to_remove.difference_update(installed_paths)
        try:
            paths_to_remove.remove()
            paths_to_remove.commit()
        except OSError as e:
            termui.logger.warning("Error occurred during overwriting, roll back the changes now.")
            paths_to_remove.rollback()
            raise UninstallError(e) from e
