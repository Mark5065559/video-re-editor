from pathlib import Path

from app.access import OwnerStore


def test_first_user_becomes_owner_and_is_remembered(tmp_path: Path):
    path = tmp_path / "data" / "owners.json"
    store = OwnerStore(path)
    assert store.claim(42)
    assert not store.claim(43)
    assert store.owners == {42}
    assert OwnerStore(path).owners == {42}  # survives a restart


def test_damaged_file_never_hands_out_the_bot(tmp_path: Path):
    path = tmp_path / "owners.json"
    path.write_text("{not json", encoding="utf-8")
    store = OwnerStore(path)
    assert store.broken and not store.claim(7) and store.owners == set()
