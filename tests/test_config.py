from __future__ import annotations

from server.config import migrate_legacy_data_dir


def test_legacy_data_directory_migration_is_safe_and_idempotent(tmp_path):
    old_dir = tmp_path / ".macpilot"
    new_dir = tmp_path / ".bamai"
    old_dir.mkdir()
    (old_dir / "data.db").write_bytes(b"legacy-data")
    nested = old_dir / "captures"
    nested.mkdir()
    (nested / "sample.txt").write_text("kept")

    assert migrate_legacy_data_dir(old_dir, new_dir) is True
    assert (new_dir / "data.db").read_bytes() == b"legacy-data"
    assert (new_dir / "captures" / "sample.txt").read_text() == "kept"
    assert (old_dir / "data.db").read_bytes() == b"legacy-data"

    assert migrate_legacy_data_dir(old_dir, new_dir) is False
    assert (new_dir / "data.db").read_bytes() == b"legacy-data"
