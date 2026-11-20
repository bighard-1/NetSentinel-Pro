from pathlib import Path

import pytest

from services import migrations


def test_backup_retention_only_prunes_matching_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "DB_DIR", tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for index in range(4):
        (backup_dir / f"monitor-pre-upgrade-2026010{index}T000000Z.db").write_bytes(b"test")
    other = backup_dir / "monitor-pre-restore-20250101T000000Z.db"
    other.write_bytes(b"other")

    migrations.prune_database_backups("monitor-pre-upgrade", 2)

    assert len(list(backup_dir.glob("monitor-pre-upgrade-*.db"))) == 2
    assert other.read_bytes() == b"other"
    with pytest.raises(ValueError):
        migrations.prune_database_backups("../monitor", 1)
