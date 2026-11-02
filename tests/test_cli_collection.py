"""CLI integration tests against an isolated Anki collection."""

import json

import pytest
from click.testing import CliRunner

from ankiweb_cli import cli
from ankiweb_cli.collection import open_collection


@pytest.fixture
def collection_cli(monkeypatch, tmp_path):
    collection = tmp_path / "collection.anki2"
    backups = tmp_path / "backups"
    monkeypatch.setattr(cli, "COLLECTION_FILE", collection)
    monkeypatch.setattr(cli, "BACKUPS_DIR", backups)
    monkeypatch.delenv("ANKIWEB_CLI_YES", raising=False)
    return CliRunner(), collection, backups


def invoke_json(runner, arguments, **kwargs):
    result = runner.invoke(cli.main, arguments, **kwargs)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_card_add_list_move_and_delete_with_confirmation(collection_cli):
    runner, collection, backups = collection_cli
    created = invoke_json(runner, [
        "cards", "add", "--deck", "Source", "--field", "Front=apple",
        "--field", "Back=fruit", "--tag", "food",
    ])
    card_id = created["card_ids"][0]
    cards = invoke_json(runner, ["cards", "list", "--deck", "Source", "--query", "apple"])
    assert len(cards) == 1
    assert cards[0]["fields"]["Back"] == "fruit"

    declined = runner.invoke(cli.main, ["decks", "move", "--ids", str(card_id), "--to", "Dest"],
                             input="n\n")
    assert declined.exit_code != 0
    assert len(invoke_json(runner, ["cards", "list", "--deck", "Source"])) == 1

    invoke_json(runner, ["decks", "move", "--ids", str(card_id), "--to", "Dest", "--yes"])
    assert len(invoke_json(runner, ["cards", "list", "--deck", "Dest"])) == 1
    invoke_json(runner, ["cards", "delete", "--ids", str(card_id), "--yes"])
    with open_collection(collection, backups_dir=backups, write=False) as col:
        assert col.db.scalar("select count() from cards") == 0
    assert list(backups.glob("*-delete.anki2"))


def test_bulk_card_import_validation_and_confirmation(collection_cli, tmp_path):
    runner, _, _ = collection_cli
    source = tmp_path / "notes.tsv"
    source.write_text("one\tfirst\ntwo\tsecond\n")
    args = ["cards", "add", "--deck", "Imported", "--from-file", str(source)]
    missing_order = runner.invoke(cli.main, args)
    assert missing_order.exit_code != 0
    assert "requires --fields-order" in missing_order.output
    declined = runner.invoke(cli.main, [*args, "--fields-order", "Front,Back"], input="n\n")
    assert declined.exit_code != 0
    result = invoke_json(runner, [*args, "--fields-order", "Front,Back", "--yes"])
    assert result["added"] == 2 and result["errors"] == []
    assert len(invoke_json(runner, ["cards", "list", "--deck", "Imported"])) == 2


def test_rename_dry_run_and_delete_deck_preserve_cards(collection_cli):
    runner, collection, backups = collection_cli
    invoke_json(runner, ["cards", "add", "--deck", "Parent::Child", "--field", "Front=hello"])
    preview = invoke_json(runner, ["decks", "rename", "Parent", "Renamed", "--dry-run"])
    assert preview["renames"] == [
        {"from": "Parent", "to": "Renamed"},
        {"from": "Parent::Child", "to": "Renamed::Child"},
    ]
    with open_collection(collection, backups_dir=backups, write=False) as col:
        assert col.decks.id_for_name("Parent::Child") is not None
        assert col.decks.id_for_name("Renamed::Child") is None
    invoke_json(runner, ["decks", "rename", "Parent", "Renamed"])
    invoke_json(runner, ["decks", "delete", "Renamed", "--recursive", "--yes"])
    cards = invoke_json(runner, ["cards", "list", "--deck", "Default"])
    assert len(cards) == 1 and cards[0]["fields"]["Front"] == "hello"


def test_large_delete_requires_explicit_bulk_confirmation(collection_cli):
    runner, _, _ = collection_cli
    ids = ",".join(str(n) for n in range(1, 52))
    result = runner.invoke(cli.main, ["cards", "delete", "--ids", ids, "--yes"])
    assert result.exit_code != 0
    assert "--yes-really" in result.output


@pytest.mark.parametrize("command", ["delete", "retag"])
def test_invalid_card_ids_are_rejected(collection_cli, command):
    runner, _, _ = collection_cli
    args = ["cards", command, "--ids", "1,not-a-number"]
    if command == "retag":
        args.extend(["--add", "tag"])
    result = runner.invoke(cli.main, args)
    assert result.exit_code != 0
    assert "Invalid card IDs" in result.output
