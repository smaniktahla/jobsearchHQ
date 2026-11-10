import json

import pytest

from network.models import Contact, Relationship
from network.resolver import resolve
from network.store import NetworkStore


def person(**overrides):
    return Contact(**({"first_name": "Ada", "last_name": "Example", "company": "Example Inc", "linkedin_url": "https://www.linkedin.com/in/ada-example/"} | overrides))


def test_preview_duplicate_notes_relationships(isolated_data_dir):
    store = NetworkStore("alice")
    row = person()
    assert store.import_contacts([row]) == {"created": 1, "updated": 0, "review": 0}
    assert not store.root.exists()
    store.import_contacts([row, row], dry_run=False)
    assert len(store.list_contacts()) == 1
    contact = store.list_contacts()[0]
    store.update_contact(contact.id, notes="Met at synthetic conference", verified=True)
    relation = Relationship(contact_id=contact.id, verified=True, notes="Human confirmed")
    store.save_relationship(relation)
    store.import_contacts([person(position="Engineer", notes="do not replace")], dry_run=False)
    saved = store.list_contacts()[0]
    assert saved.notes == "Met at synthetic conference"
    assert saved.verified and saved.position == "Engineer"
    assert store.export()["relationships"][relation.id] == relation.model_dump(mode="json")
    assert (store.root / "companies.json").is_file()
    assert (store.root / "matches.json").is_file()
    assert (store.root / "imports").is_dir()


def test_identity_precedence_and_conflicts():
    original = person(email="ada@example.test")
    assert resolve(person(linkedin_url="http://linkedin.com/in/ada-example?x=1"), [original]).reason == "url"
    assert resolve(person(linkedin_url="", email="ADA@example.test"), [original]).reason == "email"
    assert resolve(person(linkedin_url=""), [original]).reason == "name_employer"
    assert resolve(person(linkedin_url="https://linkedin.com/in/other"), [original]).action == "review"
    assert resolve(person(linkedin_url="", company="Other LLC"), [original]).action == "review"
    assert resolve(person(), [original, person()]).action == "review"
    assert resolve(Contact(first_name="Ada"), [Contact(first_name="Ada")]).action == "new"


def test_false_positive_and_review_dedup():
    store = NetworkStore("alice")
    store.import_contacts([person()], dry_run=False)
    other = person(linkedin_url="https://linkedin.com/in/different", company="Different LLC")
    for _ in range(2):
        store.import_contacts([other], dry_run=False)
    assert len(store.list_contacts()) == 1
    queue = store.export()["review_queue"]
    assert len(queue) == 1
    store.resolve_review(next(iter(queue)))
    assert len(store.list_contacts()) == 2
    assert not store.export()["review_queue"]


def test_reversible_merge():
    store = NetworkStore("alice")
    store.import_contacts([person(notes="one"), person(first_name="Bob", linkedin_url="https://linkedin.com/in/bob", notes="two")], dry_run=False)
    first, second = store.list_contacts()
    relationship = Relationship(contact_id=second.id, verified=True)
    store.save_relationship(relationship)
    before = store.export()
    merge_id = store.merge(first.id, second.id)
    assert len(store.list_contacts()) == 1
    assert store.export()["relationships"][relationship.id]["contact_id"] == first.id
    store.unmerge(merge_id)
    after = store.export()
    assert after["contacts"] == before["contacts"]
    assert after["relationships"] == before["relationships"]
    assert after["merge_log"][0]["active"] is False
    with pytest.raises(ValueError):
        store.unmerge(merge_id)


def test_isolation_export_delete_cache(isolated_data_dir):
    alice, bob = NetworkStore("alice"), NetworkStore("bob")
    alice.import_contacts([person()], dry_run=False)
    bob.import_contacts([person()], dry_run=False)
    exported = alice.export()
    exported["contacts"].clear()
    assert len(alice.list_contacts()) == 1
    # A separate writer invalidates cached reads by mtime/size.
    state = json.loads(alice.path.read_text())
    cid = next(iter(state["contacts"]))
    state["contacts"][cid]["notes"] = "external edit"
    alice.path.write_text(json.dumps(state))
    assert alice.list_contacts()[0].notes == "external edit"
    alice.delete_contact(cid)
    assert not alice.list_contacts()
    assert len(bob.list_contacts()) == 1
    alice.delete_all()
    assert not alice.root.exists()
    assert len(bob.list_contacts()) == 1
    with pytest.raises(ValueError):
        NetworkStore("../bob")


def test_unmerge_preserves_later_edits():
    store = NetworkStore("alice")
    store.import_contacts([person(), person(first_name="Bob", linkedin_url="https://linkedin.com/in/bob")], dry_run=False)
    first, second = store.list_contacts()
    mid = store.merge(first.id, second.id)
    store.update_contact(first.id, notes="later user edit")
    store.unmerge(mid)
    assert len(store.list_contacts()) == 2
    assert store.list_contacts()[0].notes == "later user edit"


def test_reimport_merged_source_is_not_duplicated():
    store = NetworkStore("alice")
    rows = [person(), person(first_name="Bob", linkedin_url="https://linkedin.com/in/bob")]
    store.import_contacts(rows, dry_run=False)
    first, second = store.list_contacts()
    mid = store.merge(first.id, second.id)
    assert store.import_contacts([rows[1]], dry_run=False)["updated"] == 1
    assert len(store.list_contacts()) == 1
    store.unmerge(mid)
    assert len(store.list_contacts()) == 2
    assert {c.linkedin_url for c in store.list_contacts()} == {c.linkedin_url for c in rows}


def test_store_has_no_model_dependency():
    import ast
    from pathlib import Path
    for filename in ("resolver.py", "store.py"):
        tree = ast.parse((Path(__file__).parents[1] / "network" / filename).read_text())
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(name.split(".")[0] in {"ai_router", "openai", "anthropic"} for name in imports)
