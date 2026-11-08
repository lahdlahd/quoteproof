"""Staging resists symlinks, hard links, FIFOs, path tricks, size abuse and hash mismatch."""
import hashlib
import os
import uuid

import pytest

from sandboxd import staging
from sandboxd.staging import RunFolder, StagingError


@pytest.fixture
def dirs(tmp_path):
    uploads = tmp_path / "uploads"
    stage = tmp_path / "staging"
    uploads.mkdir(mode=0o750)
    stage.mkdir()
    os.chmod(stage, 0o711)
    return str(uploads), str(stage)


def put(uploads, data=b"Item,Qty\nChair,2\n"):
    doc = str(uuid.uuid4())
    with open(os.path.join(uploads, doc), "wb") as f:
        f.write(data)
    return doc, hashlib.sha256(data).hexdigest()


def run_id():
    return os.urandom(16).hex()


def test_happy_path_copies_hashes_and_seals(dirs):
    uploads, stage = dirs
    doc, sha = put(uploads)
    f = RunFolder(stage, run_id())
    got, size = f.copy_upload(uploads, doc, sha)
    f.write_bytes("meta.json", staging.meta_json("csv"))
    f.seal()
    assert got == sha and size == 17
    st = os.stat(os.path.join(f.in_path, "document"))
    assert oct(st.st_mode & 0o777) == "0o444"
    assert oct(os.stat(f.in_path).st_mode & 0o777) == "0o511"
    assert oct(os.stat(f.run_path).st_mode & 0o777) == "0o711"
    assert f.remove() is True
    assert not os.path.exists(f.run_path)


def test_hash_mismatch_is_rejected(dirs):
    uploads, stage = dirs
    doc, _ = put(uploads)
    f = RunFolder(stage, run_id())
    with pytest.raises(StagingError) as e:
        f.copy_upload(uploads, doc, "0" * 64)
    assert e.value.code == "hash_mismatch"
    assert f.remove() is True


def test_symlink_upload_is_refused(dirs, tmp_path):
    uploads, stage = dirs
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOPSECRET")
    doc = str(uuid.uuid4())
    os.symlink(secret, os.path.join(uploads, doc))
    f = RunFolder(stage, run_id())
    with pytest.raises(StagingError) as e:
        f.copy_upload(uploads, doc, hashlib.sha256(b"TOPSECRET").hexdigest())
    assert e.value.code == "document_open_refused"
    f.remove()


def test_hard_link_is_refused(dirs, tmp_path):
    uploads, stage = dirs
    other = tmp_path / "elsewhere.csv"
    other.write_bytes(b"a,b\n")
    doc = str(uuid.uuid4())
    os.link(other, os.path.join(uploads, doc))
    f = RunFolder(stage, run_id())
    with pytest.raises(StagingError) as e:
        f.copy_upload(uploads, doc, hashlib.sha256(b"a,b\n").hexdigest())
    assert e.value.code == "document_has_extra_hard_links"
    f.remove()


def test_fifo_is_refused_without_hanging(dirs):
    uploads, stage = dirs
    doc = str(uuid.uuid4())
    os.mkfifo(os.path.join(uploads, doc))
    f = RunFolder(stage, run_id())
    with pytest.raises(StagingError) as e:
        f.copy_upload(uploads, doc, "0" * 64)
    assert e.value.code == "document_not_regular_file"
    f.remove()


def test_too_large_is_refused(dirs):
    uploads, stage = dirs
    doc, sha = put(uploads, b"x" * 2048)
    f = RunFolder(stage, run_id())
    with pytest.raises(StagingError) as e:
        f.copy_upload(uploads, doc, sha, max_bytes=1024)
    assert e.value.code == "document_too_large"
    f.remove()


def test_wrong_owner_is_refused(dirs):
    uploads, stage = dirs
    doc, sha = put(uploads)
    f = RunFolder(stage, run_id())
    with pytest.raises(StagingError) as e:
        f.copy_upload(uploads, doc, sha, expected_owner_uid=os.getuid() + 12345)
    assert e.value.code == "document_wrong_owner"
    f.remove()


def test_symlinked_uploads_dir_is_refused(dirs, tmp_path):
    uploads, stage = dirs
    link = tmp_path / "uploads_link"
    os.symlink(uploads, link)
    with pytest.raises(StagingError) as e:
        staging.check_root(str(link), owner_uid=os.getuid())
    assert e.value.code == "root_not_real_directory"
    doc, sha = put(uploads)
    f = RunFolder(stage, run_id())
    with pytest.raises(OSError):  # O_NOFOLLOW on the folder handle -> ELOOP
        f.copy_upload(str(link), doc, sha)
    f.remove()


def test_staging_root_mode_is_enforced(dirs):
    _, stage = dirs
    staging.check_root(stage, owner_uid=os.getuid(), exact_mode=0o711)
    os.chmod(stage, 0o777)
    with pytest.raises(StagingError) as e:
        staging.check_root(stage, owner_uid=os.getuid(), exact_mode=0o711)
    assert e.value.code == "root_wrong_mode"


def test_run_folder_cannot_be_reused_or_preplanted(dirs, tmp_path):
    _, stage = dirs
    rid = run_id()
    os.symlink(tmp_path, os.path.join(stage, rid))  # attacker pre-plants the name
    with pytest.raises(FileExistsError):
        RunFolder(stage, rid)


def test_remove_does_not_follow_planted_symlink(dirs, tmp_path):
    uploads, stage = dirs
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    doc, sha = put(uploads)
    f = RunFolder(stage, run_id())
    f.copy_upload(uploads, doc, sha)
    os.symlink(victim, os.path.join(f.in_path, "evil_link"))
    f.seal()
    assert f.remove() is True
    assert (victim / "keep.txt").read_text() == "keep"


def test_clear_staging_root(dirs):
    _, stage = dirs
    for _ in range(3):
        RunFolder(stage, run_id()).seal()
    assert len(staging.clear_staging_root(stage)) == 3
    assert os.listdir(stage) == []
