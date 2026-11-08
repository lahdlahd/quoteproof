"""Symlink- and path-safe staging of inputs for one sandbox run.

Rules (from the approved architecture, clarification 3):
  * folders are opened as handles with O_NOFOLLOW; files are opened relative
    to those handles, never by joining user-supplied strings into paths;
  * the upload must be a regular file with exactly one hard link, under the
    size cap, owned by the expected user;
  * the copy is hashed byte-for-byte while it is written, and compared with
    the expected SHA-256 before any container is started;
  * staged files end up 0444 inside a 0555 folder, which is bind-mounted
    read-only; the whole run folder is deleted after the run.
"""

import hashlib
import json
import os
import shutil
import stat

from . import limits

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
CHUNK = 64 * 1024


class StagingError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def check_root(path, *, owner_uid, exact_mode=None, forbid_other_write=True):
    """Fail closed unless `path` is a real directory with the expected owner/mode."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise StagingError("root_missing", path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise StagingError("root_not_real_directory", path)
    if owner_uid is not None and st.st_uid != owner_uid:
        raise StagingError("root_wrong_owner", f"{path} uid={st.st_uid} expected={owner_uid}")
    mode = stat.S_IMODE(st.st_mode)
    if exact_mode is not None and mode != exact_mode:
        raise StagingError("root_wrong_mode", f"{path} mode={oct(mode)} expected={oct(exact_mode)}")
    if forbid_other_write and mode & stat.S_IWOTH:
        raise StagingError("root_world_writable", path)


class RunFolder:
    """A fresh <staging_root>/<run_id>/in/ folder, created exclusively."""

    def __init__(self, staging_root, run_id):
        self.staging_root = staging_root
        self.run_id = run_id
        self.run_path = os.path.join(staging_root, run_id)
        self.in_path = os.path.join(self.run_path, "in")
        root_fd = os.open(staging_root, DIR_FLAGS)
        try:
            os.mkdir(run_id, 0o700, dir_fd=root_fd)  # fails if it already exists
            self._run_fd = os.open(run_id, DIR_FLAGS, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        os.mkdir("in", 0o700, dir_fd=self._run_fd)
        self._in_fd = os.open("in", DIR_FLAGS, dir_fd=self._run_fd)

    def write_bytes(self, name, data: bytes):
        """Create a new file in in/ (never overwrite, never follow links)."""
        fd = os.open(name, NEW_FILE_FLAGS, 0o600, dir_fd=self._in_fd)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
            os.fchmod(fd, 0o444)
        finally:
            os.close(fd)
        return hashlib.sha256(data).hexdigest()

    def copy_upload(self, uploads_dir, document_id, expected_sha256, *,
                    expected_owner_uid=None, max_bytes=limits.MAX_UPLOAD_BYTES):
        """Copy uploads_dir/document_id to in/document, hashing while copying."""
        dfd = os.open(uploads_dir, DIR_FLAGS)
        try:
            try:
                # O_NONBLOCK so a FIFO planted under that name cannot hang us.
                src = os.open(document_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                              dir_fd=dfd)
            except FileNotFoundError:
                raise StagingError("document_not_found", document_id)
            except OSError as e:
                # ELOOP = it was a symlink
                raise StagingError("document_open_refused", f"{document_id}: {e.strerror}")
        finally:
            os.close(dfd)

        try:
            st = os.fstat(src)
            if not stat.S_ISREG(st.st_mode):
                raise StagingError("document_not_regular_file")
            if st.st_nlink != 1:
                raise StagingError("document_has_extra_hard_links", f"nlink={st.st_nlink}")
            if st.st_size > max_bytes:
                raise StagingError("document_too_large", f"{st.st_size} > {max_bytes}")
            if expected_owner_uid is not None and st.st_uid != expected_owner_uid:
                raise StagingError("document_wrong_owner", f"uid={st.st_uid}")

            dst = os.open("document", NEW_FILE_FLAGS, 0o600, dir_fd=self._in_fd)
            digest = hashlib.sha256()
            total = 0
            try:
                while True:
                    chunk = os.read(src, CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise StagingError("document_too_large", "grew during copy")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        n = os.write(dst, view)
                        view = view[n:]
                os.fsync(dst)
                os.fchmod(dst, 0o444)
            finally:
                os.close(dst)
        finally:
            os.close(src)

        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise StagingError("hash_mismatch", f"expected={expected_sha256} actual={actual}")
        return actual, total

    def seal(self):
        """Make in/ read-only. Folders are traverse-only (no listing) for everyone else:
        the container (uid 65534) opens files by their fixed names, so it never needs to
        list them, and other local accounts cannot discover what is being processed."""
        os.fchmod(self._in_fd, 0o511)
        os.fchmod(self._run_fd, 0o711)
        os.close(self._in_fd)
        os.close(self._run_fd)
        self._in_fd = self._run_fd = None

    def remove(self):
        """Delete the run folder; return True only if it is really gone."""
        for fd in (getattr(self, "_in_fd", None), getattr(self, "_run_fd", None)):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._in_fd = self._run_fd = None
        return remove_run_folder(self.staging_root, self.run_id)


def remove_run_folder(staging_root, run_id):
    run_path = os.path.join(staging_root, run_id)
    try:
        st = os.lstat(run_path)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        os.unlink(run_path)
    else:
        in_path = os.path.join(run_path, "in")
        try:
            ist = os.lstat(in_path)
            if stat.S_ISDIR(ist.st_mode) and not stat.S_ISLNK(ist.st_mode):
                os.chmod(in_path, 0o700)  # needed to delete files inside a 0555 dir
        except FileNotFoundError:
            pass
        # shutil.rmtree uses fd-based traversal on Linux and does not follow symlinks
        shutil.rmtree(run_path)
    return not os.path.lexists(run_path)


def clear_staging_root(staging_root):
    """Remove leftovers from a previous crash (called at startup)."""
    removed = []
    for name in os.listdir(staging_root):
        remove_run_folder(staging_root, name)
        removed.append(name)
    return removed


def meta_json(declared_type):
    return json.dumps({"declared_type": declared_type}).encode("utf-8")
