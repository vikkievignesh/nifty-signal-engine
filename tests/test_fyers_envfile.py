import logging
import os
import stat
import subprocess

import pytest

from nifty_signal_engine.fyers import EnvFileError, load_credentials
from nifty_signal_engine.fyers import envfile
from nifty_signal_engine.fyers.envfile import preflight, set_env_value

from fyers_auth_helpers import ACCESS_TOKEN, APP_ID, APP_SECRET, OLD_TOKEN

NAME = "FYERS_ACCESS_TOKEN"


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def write(path, text, perms=0o600):
    path.write_text(text, newline="")
    path.chmod(perms)
    return path


def test_updates_only_the_token_and_preserves_everything_else_byte_for_byte(tmp_path):
    original = (
        "# local settings\n"
        f"FYERS_APP_ID={APP_ID}\n"
        f"FYERS_APP_SECRET={APP_SECRET}\n"
        "\n"
        "NIFTY_LOG_LEVEL=DEBUG   # keep me\n"
        f"FYERS_ACCESS_TOKEN={OLD_TOKEN}\n"
        "# FYERS_ACCESS_TOKEN=commented_out_must_stay_0123456789\n"
        "FYERS_ACCESS_TOKEN_OLD=do_not_touch_0123456789\n"
        "WEIRD = spaced value\n"
    )
    path = write(tmp_path / ".env", original)
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_text() == original.replace(f"FYERS_ACCESS_TOKEN={OLD_TOKEN}", f"FYERS_ACCESS_TOKEN={ACCESS_TOKEN}")
    assert mode(path) == 0o600


def test_every_active_definition_is_updated_because_the_loader_uses_the_last(tmp_path):
    path = write(tmp_path / ".env", f"FYERS_APP_ID={APP_ID}\nFYERS_ACCESS_TOKEN=first_0123456789abcdef\n"
                                    "FYERS_ACCESS_TOKEN=second_0123456789abcdef\n")
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_text().count(f"FYERS_ACCESS_TOKEN={ACCESS_TOKEN}") == 2
    assert load_credentials({}, path).access_token == ACCESS_TOKEN


def test_export_prefix_indentation_and_crlf_are_preserved(tmp_path):
    path = write(tmp_path / ".env", f"A=1\r\n  export FYERS_ACCESS_TOKEN={OLD_TOKEN}\r\nB=2\r\n")
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_bytes() == f"A=1\r\n  export FYERS_ACCESS_TOKEN={ACCESS_TOKEN}\r\nB=2\r\n".encode()


@pytest.mark.parametrize("before, after", [
    ("A=1\n", f"A=1\n{NAME}={ACCESS_TOKEN}\n"),
    ("A=1", f"A=1\n{NAME}={ACCESS_TOKEN}\n"),            # no trailing newline in the original
    ("A=1\r\n", f"A=1\r\n{NAME}={ACCESS_TOKEN}\r\n"),    # CRLF file keeps CRLF
    ("", f"{NAME}={ACCESS_TOKEN}\n"),
    ("# FYERS_ACCESS_TOKEN=\n", f"# FYERS_ACCESS_TOKEN=\n{NAME}={ACCESS_TOKEN}\n"),
])
def test_missing_variable_is_appended(tmp_path, before, after):
    path = write(tmp_path / ".env", before)
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_bytes().decode() == after


def test_new_file_is_created_with_mode_0600(tmp_path):
    path = tmp_path / ".env"
    old_umask = os.umask(0o000)  # a permissive umask must not leak into the file mode
    try:
        set_env_value(path, NAME, ACCESS_TOKEN)
    finally:
        os.umask(old_umask)
    assert path.read_text() == f"{NAME}={ACCESS_TOKEN}\n" and mode(path) == 0o600


@pytest.mark.parametrize("perms", [0o644, 0o666, 0o640, 0o400])
def test_existing_permissions_are_tightened_to_0600(tmp_path, perms):
    path = write(tmp_path / ".env", f"A=1\n{NAME}={OLD_TOKEN}\n", perms=perms)
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert mode(path) == 0o600


def test_symlinked_env_is_refused_and_target_untouched(tmp_path):
    target = write(tmp_path / "real_secrets.txt", f"{NAME}={OLD_TOKEN}\n")
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises(EnvFileError, match="symbolic link"):
        set_env_value(link, NAME, ACCESS_TOKEN)
    assert target.read_text() == f"{NAME}={OLD_TOKEN}\n" and link.is_symlink()


def test_dangling_symlink_is_refused_and_nothing_is_created(tmp_path):
    victim = tmp_path / "victim.txt"
    link = tmp_path / ".env"
    link.symlink_to(victim)
    with pytest.raises(EnvFileError, match="symbolic link"):
        set_env_value(link, NAME, ACCESS_TOKEN)
    assert not victim.exists()


def test_symlink_swapped_in_after_the_checks_is_replaced_not_followed(tmp_path, monkeypatch):
    """TOCTOU: os.replace swaps the directory entry, so a late symlink never receives the token."""
    victim = write(tmp_path / "victim.txt", "untouched\n")
    path = write(tmp_path / ".env", f"{NAME}={OLD_TOKEN}\n")
    real_write = envfile._write_private

    def swap_then_write(p, text):
        p.unlink()
        p.symlink_to(victim)  # attacker acts between the checks and the write
        real_write(p, text)

    monkeypatch.setattr(envfile, "_write_private", swap_then_write)
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert victim.read_text() == "untouched\n"
    assert not path.is_symlink() and ACCESS_TOKEN in path.read_text()


@pytest.mark.parametrize("make", ["directory", "fifo"])
def test_non_regular_files_are_refused(tmp_path, make):
    path = tmp_path / ".env"
    if make == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(EnvFileError, match="not a regular file"):
        set_env_value(path, NAME, ACCESS_TOKEN)


def test_hard_linked_env_is_refused(tmp_path):
    path = write(tmp_path / ".env", f"{NAME}={OLD_TOKEN}\n")
    other = tmp_path / "other_name"
    os.link(path, other)
    with pytest.raises(EnvFileError, match="hard links"):
        set_env_value(path, NAME, ACCESS_TOKEN)
    assert other.read_text() == f"{NAME}={OLD_TOKEN}\n"


def test_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    path = write(tmp_path / ".env", f"{NAME}={OLD_TOKEN}\n")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(EnvFileError, match="not owned"):
        set_env_value(path, NAME, ACCESS_TOKEN)


def test_missing_directory_and_non_utf8_are_refused(tmp_path):
    with pytest.raises(EnvFileError, match="directory"):
        set_env_value(tmp_path / "nope" / ".env", NAME, ACCESS_TOKEN)
    path = tmp_path / ".env"
    path.write_bytes(b"\xff\xfe\x00bad")
    path.chmod(0o600)
    with pytest.raises(EnvFileError, match="UTF-8"):
        set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_bytes() == b"\xff\xfe\x00bad"


@pytest.mark.parametrize("value", ["", "short", "has space 0123456789abcdef", "line\nbreak0123456789abc",
                                   "semi;colon0123456789abcd", "quote'0123456789abcdefg", "#hash0123456789abcdef",
                                   "x" * 5000])
def test_unsafe_values_are_refused_and_file_unchanged(tmp_path, value):
    path = write(tmp_path / ".env", f"A=1\n{NAME}={OLD_TOKEN}\n")
    with pytest.raises(EnvFileError, match="unexpected format"):
        set_env_value(path, NAME, value)
    assert path.read_text() == f"A=1\n{NAME}={OLD_TOKEN}\n"


def test_invalid_variable_name_is_refused(tmp_path):
    with pytest.raises(EnvFileError):
        set_env_value(tmp_path / ".env", "BAD NAME", ACCESS_TOKEN)


def test_failed_write_leaves_original_intact_and_no_temp_file(tmp_path, monkeypatch):
    original = f"A=1\n{NAME}={OLD_TOKEN}\n"
    path = write(tmp_path / ".env", original)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(EnvFileError, match="could not write"):
        set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_token_is_never_logged(tmp_path, caplog):
    path = write(tmp_path / ".env", f"{NAME}={OLD_TOKEN}\n")
    with caplog.at_level(logging.DEBUG):
        set_env_value(path, NAME, ACCESS_TOKEN)
    assert ACCESS_TOKEN not in caplog.text and OLD_TOKEN not in caplog.text
    assert "updated FYERS_ACCESS_TOKEN" in caplog.text


def test_error_messages_never_contain_the_value(tmp_path):
    secretish = "bad value with spaces " + ACCESS_TOKEN
    with pytest.raises(EnvFileError) as info:
        set_env_value(tmp_path / ".env", NAME, secretish)
    assert ACCESS_TOKEN not in str(info.value)


# --- git guard ---------------------------------------------------------------------------------

def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    try:
        git(tmp_path, "init", "-q")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    return tmp_path


def test_env_not_ignored_by_git_is_refused(repo):
    path = write(repo / ".env", f"{NAME}={OLD_TOKEN}\n")
    with pytest.raises(EnvFileError, match="not ignored by git"):
        set_env_value(path, NAME, ACCESS_TOKEN)
    assert path.read_text() == f"{NAME}={OLD_TOKEN}\n"
    with pytest.raises(EnvFileError, match="not ignored by git"):
        preflight(repo / ".env.new")


def test_env_ignored_by_git_is_accepted(repo):
    (repo / ".gitignore").write_text(".env\n")
    path = write(repo / ".env", f"{NAME}={OLD_TOKEN}\n")
    set_env_value(path, NAME, ACCESS_TOKEN)
    assert ACCESS_TOKEN in path.read_text()


def test_tracked_env_is_refused_even_if_listed_in_gitignore(repo):
    path = write(repo / ".env", f"{NAME}={OLD_TOKEN}\n")
    git(repo, "add", "-f", ".env")
    (repo / ".gitignore").write_text(".env\n")
    with pytest.raises(EnvFileError, match="not ignored by git"):
        set_env_value(path, NAME, ACCESS_TOKEN)


def test_this_repositorys_gitignore_covers_env_and_temp_files():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in (".env", ".env.abc123.tmp"):
        outcome = subprocess.run(["git", "check-ignore", "-q", name], cwd=root, capture_output=True)
        if outcome.returncode == 128:
            pytest.skip("not a git checkout")
        assert outcome.returncode == 0, f"{name} is not ignored"
