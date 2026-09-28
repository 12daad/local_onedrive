import pytest

from quarksync.sync.paths import UnmappableWindowsPath, validate_windows_relative_path


@pytest.mark.parametrize(
    "path", ["CON", "folder/NUL.txt", "bad. ", "a:b.txt", "../escape.txt"]
)
def test_rejects_unmappable_windows_names(path):
    with pytest.raises(UnmappableWindowsPath):
        validate_windows_relative_path(path)


def test_accepts_unicode_spaces_hash_percent_ampersand():
    assert validate_windows_relative_path("中文/hello # % & 😀.txt")

