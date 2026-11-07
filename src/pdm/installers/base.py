from __future__ import annotations

import dataclasses
import json
from collections.abc import Collection, Iterable
from functools import cached_property
from importlib.metadata import Distribution
from itertools import chain
from pathlib import Path

from pdm import termui
from pdm.environments import BaseEnvironment
from pdm.exceptions import BuildError
from pdm.installers.manager import InstallManager
from pdm.models.candidates import (
    LOCAL_BUILD_DIGEST_VERSION,
    LOCAL_BUILD_RECORD_FILE,
    Candidate,
    compute_local_build_digest,
)
from pdm.models.repositories import Package
from pdm.models.requirements import FileRequirement, Requirement, parse_requirement, strip_extras
from pdm.utils import is_editable, normalize_name, url_to_path


def editables_candidate(environment: BaseEnvironment) -> Candidate | None:
    """Return a candidate for `editables` package"""
    with environment.get_finder() as finder:
        best = finder.find_best_match("editables").best
    return None if best is None else Candidate.from_installation_candidate(best, parse_requirement("editables"))


class BaseSynchronizer:
    """Synchronize the working set with given installation candidates

    :param candidates: a dict of candidates to be installed
    :param environment: the environment associated with the project
    :param clean: clean unneeded packages
    :param dry_run: only prints summary but do not install or uninstall
    :param retry_times: retry times when installation failed
    :param install_self: whether to install self project
    :param no_editable: if True, override all editable installations,
        if a list, override editables with the given names
    :param use_install_cache: whether to use install cache
    :param reinstall: whether to reinstall all packages
    :param only_keep: If true, only keep the selected candidates
    :param fail_fast: If true, stop the installation on first error
    """

    SEQUENTIAL_PACKAGES = ("pip", "setuptools", "wheel")

    def __init__(
        self,
        environment: BaseEnvironment,
        candidates: dict[str, Candidate] | None = None,
        clean: bool = False,
        dry_run: bool = False,
        retry_times: int = 1,
        install_self: bool = False,
        no_editable: bool | Collection[str] = False,
        reinstall: bool = False,
        only_keep: bool = False,
        fail_fast: bool = False,
        use_install_cache: bool | None = None,
        packages: Iterable[Package] = (),
        requirements: Iterable[Requirement] | None = None,
    ) -> None:
        if candidates:  # pragma: no cover
            self.requested_candidates = candidates
        else:
            self.requested_candidates = {entry.candidate.identify(): entry.candidate for entry in packages}
        self.environment = environment
        self.clean = clean
        self.dry_run = dry_run
        self.retry_times = retry_times
        self.no_editable = no_editable
        self.install_self = install_self
        if use_install_cache is None:
            use_install_cache = bool(environment.project.config["install.cache"])
        self.use_install_cache = use_install_cache
        self.reinstall = reinstall
        self.only_keep = only_keep
        self.parallel = environment.project.config["install.parallel"]
        self.fail_fast = fail_fast

        self.working_set = environment.get_working_set()
        self.ui = environment.project.core.ui
        self._manager: InstallManager | None = None
        self.packages = packages
        self.requirements = requirements

    @cached_property
    def self_candidate(self) -> Candidate:
        """Return the candidate for self project"""
        return self.environment.project.make_self_candidate(not self.no_editable)

    @cached_property
    def candidates(self) -> dict[str, Candidate]:
        """Return the candidates to be installed"""
        candidates = self.requested_candidates.copy()
        requested = {
            req.identify()
            for req in (self.requirements or chain.from_iterable(self.environment.project.all_dependencies.values()))
        }
        if isinstance(self.no_editable, Collection):
            keys = self.no_editable
        elif self.no_editable:
            keys = candidates.keys()
        else:
            keys = []
            if self.should_install_editables():
                # Install `editables` as well as required by self project
                editables = editables_candidate(self.environment)
                if editables is not None:
                    candidates["editables"] = editables
        for key in keys:
            if key in candidates and candidates[key].req.editable:
                candidate = candidates[key]
                # Create a new candidate with editable=False
                req = dataclasses.replace(candidate.req, editable=False)
                candidates[key] = candidate.copy_with(req)
        for key in requested:
            if key in candidates:
                candidates[key].requested = True
        return candidates

    def should_install_editables(self) -> bool:
        """Return whether to add editables"""
        if not self.install_self or "editables" in self.requested_candidates:
            return False
        # As editables may be added by the backend, we need to check the metadata
        try:
            metadata = self.self_candidate.prepare(self.environment).metadata
        except BuildError:
            return False
        return any(req.startswith("editables") for req in metadata.requires or [])

    @property
    def manager(self) -> InstallManager:
        if not self._manager:
            self._manager = self.get_manager(rename_pth=True)
        return self._manager

    def get_manager(self, rename_pth: bool = False) -> InstallManager:
        return self.environment.project.core.install_manager_class(
            self.environment, use_install_cache=self.use_install_cache, rename_pth=rename_pth
        )

    @property
    def self_key(self) -> str | None:
        if not self.install_self:
            return None
        name = self.environment.project.name
        if name:
            return normalize_name(name)
        return name

    def _resolve_local_source_dir(self, can: Candidate) -> Path | None:
        """Resolve the build directory of a local directory candidate
        (taking the subdirectory fragment into account)."""
        if not can.link or not can.link.is_file:
            return None
        expanded_url = self.environment.project.backend.expand_line(can.link.url_without_fragment)
        try:
            path = Path(url_to_path(expanded_url))
        except ValueError:
            return None
        if not path.is_absolute():
            return None
        if can.link.subdirectory:
            path = path / can.link.subdirectory
        return path if path.is_dir() else None

    def _artifacts_complete(self, dist: Distribution) -> bool:
        """Check that every file recorded in the installed RECORD exists."""
        files = list(dist.files or [])
        if not files:
            return False
        for package_path in files:
            if not dist.locate_file(package_path).exists():
                termui.logger.info(
                    "Installed artifact %s of %s is missing, the distribution will be rebuilt.",
                    package_path,
                    dist.metadata.get("Name"),
                )
                return False
        return True

    def _local_build_is_current(self, dist: Distribution, source_dir: Path, *, editable: bool) -> bool:
        """Whether a distribution built from a local directory is still up to
        date: the recorded build-input digest must match the current inputs
        and the installed artifacts must be complete."""
        record_text = dist.read_text(LOCAL_BUILD_RECORD_FILE)
        if not record_text:
            # Installed by an older PDM (or before the build finished):
            # rebuild once and record the digest afterwards.
            return False
        try:
            record = json.loads(record_text)
        except ValueError:
            termui.logger.warning(
                "Invalid %s found for %s, the distribution will be rebuilt.",
                LOCAL_BUILD_RECORD_FILE,
                dist.metadata.get("Name"),
            )
            return False
        if not isinstance(record, dict) or record.get("version") != LOCAL_BUILD_DIGEST_VERSION:
            # The digest algorithm/record format changed, rebuild is needed.
            return False
        try:
            digest = compute_local_build_digest(source_dir, self.environment, editable=editable)
        except OSError as e:
            termui.logger.warning(
                "Failed to compute the build digest of %s: %s; the distribution will be rebuilt.",
                source_dir,
                e,
            )
            return False
        if record.get("digest") != digest:
            return False
        return self._artifacts_complete(dist)

    def _should_update(self, dist: Distribution, can: Candidate) -> bool:
        """Check if the candidate should be updated"""
        backend = self.environment.project.backend
        if self.reinstall:  # Forced reinstallation always rebuilds everything.
            return True
        if can.req.editable:
            if not can.req.is_vcs and is_editable(dist):
                # Editable builds of local directories can be skipped when
                # their build inputs didn't change. Editable VCS requirements
                # are always updated below.
                source_dir = self._resolve_local_source_dir(can)
                if source_dir is not None and self._local_build_is_current(dist, source_dir, editable=True):
                    return False
            return True
        if is_editable(dist):  # only update editable if no_editable is True
            return bool(self.no_editable)
        if not can.req.is_named:
            dreq = Requirement.from_dist(dist)
            if not isinstance(dreq, FileRequirement):
                return True
            url = dreq.get_full_url()
            assert can.link is not None
            expanded_url = backend.expand_line(can.link.url_without_fragment)
            if url != expanded_url:
                return True
            if dreq.is_local_dir:
                # Decide from the digest of the local build inputs instead of
                # rebuilding unconditionally.
                source_dir = self._resolve_local_source_dir(can)
                if source_dir is None:
                    # The source directory is gone, force the update and let
                    # the build step report the proper error.
                    return True
                return not self._local_build_is_current(dist, source_dir, editable=False)
            direct_json = json.loads(content) if (content := dist.read_text("direct_url.json")) else None
            if not direct_json or "archive_info" not in direct_json:
                # We are not able to check, don't update
                return False
            dist_hash = direct_json["archive_info"]["hash"].replace("=", ":")
            return not any(dist_hash == file_hash["hash"] for file_hash in can.hashes)
        specifier = can.req.as_pinned_version(can.version).specifier
        return not specifier.contains(dist.version, prereleases=True)

    def compare_with_working_set(self) -> tuple[list[str], list[str], list[str]]:
        """Compares the candidates and return (to_add, to_update, to_remove)"""
        working_set = self.working_set
        candidates = self.candidates.copy()
        to_update: set[str] = set()
        to_remove: set[str] = set()
        to_add: set[str] = set()
        locked_repository = self.environment.project.get_locked_repository()
        all_candidate_keys = list(locked_repository.all_candidates)

        for key, dist in working_set.items():
            if key == self.self_key and self.install_self:
                continue
            if key in candidates:
                can = candidates.pop(key)
                if self._should_update(dist, can):
                    if working_set.is_owned(key):
                        to_update.add(key)
                    else:
                        to_add.add(key)
            elif (
                (self.only_keep or (self.clean and key not in all_candidate_keys))
                and key not in self.SEQUENTIAL_PACKAGES
                and working_set.is_owned(key)
            ):
                # Remove package only if it is not required by any group
                # Packages for packaging will never be removed
                to_remove.add(key)
        to_add.update(
            strip_extras(name)[0]
            for name, _ in candidates.items()
            if name != self.self_key and strip_extras(name)[0] not in working_set
        )
        return (sorted(to_add), sorted(to_update), sorted(to_remove))

    def synchronize(self) -> None:
        """Synchronize the working set with pinned candidates."""
        to_add, to_update, to_remove = self.compare_with_working_set()
        manager = self.get_manager()
        for key in to_add:
            can = self.candidates[key]
            termui.logger.info("Installing %s@%s...", key, can.version)
            manager.install(can)
        for key in to_update:
            can = self.candidates[key]
            dist = self.working_set[strip_extras(key)[0]]
            dist_version = dist.version
            termui.logger.info("Updating %s@%s -> %s...", key, dist_version, can.version)
            manager.overwrite(dist, can)
        for key in to_remove:
            dist = self.working_set[key]
            termui.logger.info("Removing %s@%s...", key, dist.version)
            manager.uninstall(dist)
        if self.install_self:
            self_key = self.self_key
            assert self_key
            word = "a" if self.no_editable else "an editable"
            termui.logger.info(f"Installing the project as {word} package...")
            if self_key in self.working_set:
                dist = self.working_set[strip_extras(self_key)[0]]
                manager.overwrite(dist, self.self_candidate)
            else:
                manager.install(self.self_candidate)
        termui.logger.info("Synchronization complete.")
