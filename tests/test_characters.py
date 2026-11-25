import base64
import json
import struct
import zlib

import pytest

from jarvis.characters import (
    BUILTIN_IDS,
    CharacterStore,
    handle_command,
    render_persona,
)


def _chunk(kind, body):
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def make_png(**texts):
    """Build a 1x1 PNG with tEXt chunks (keyword -> json-able card)."""
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for key, card in texts.items():
        payload = base64.b64encode(json.dumps(card).encode())
        out += _chunk(b"tEXt", key.encode() + b"\x00" + payload)
    out += _chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    out += _chunk(b"IEND", b"")
    return out


def v2(name="Nova", **extra):
    data = {"name": name, "description": "A {{char}} for {{user}}", "personality": "bright",
            "scenario": "space", "first_mes": "Hi {{user}}", "mes_example": "<USER>: yo\n<BOT>: hey",
            "system_prompt": "be brief", "tags": ["scifi"], "alternate_greetings": ["Hello"]}
    data.update(extra)
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


@pytest.fixture
def store(tmp_path):
    return CharacterStore(tmp_path)


def test_v2_json(store):
    s = store.import_bytes(json.dumps(v2()).encode(), "nova.json")
    assert s["id"] == "nova" and not s["builtin"] and not s["has_avatar"]
    card = store.get("nova")
    assert card["description"] == "A {{char}} for {{user}}\n\nScenario: space"
    assert card["personality"] == "bright" and card["greeting"] == "Hi {{user}}"
    assert card["example_dialogue"].startswith("<USER>: yo")
    assert card["instructions"] == "be brief" and card["tags"] == ["scifi"]
    assert not set(card) - {"id", "name", "description", "personality", "greeting",
                            "example_dialogue", "instructions", "tags", "builtin", "overridden"}


def test_import_maps_post_history_into_instructions(store):
    raw = v2(post_history_instructions="stay in voice")
    store.import_bytes(json.dumps(raw).encode(), "a")
    assert store.get("nova")["instructions"] == "be brief\n\nstay in voice"


def test_v3_json_nickname_fallback(store):
    raw = {"spec": "chara_card_v3", "data": {"name": "", "nickname": "Nick", "description": "d"}}
    s = store.import_bytes(json.dumps(raw).encode(), "x.json")
    assert s["name"] == "Nick"


def test_v1_flat_json(store):
    raw = {"name": "Old", "description": "d", "personality": "p", "first_mes": "hi"}
    s = store.import_bytes(json.dumps(raw).encode(), "old.json")
    assert store.get(s["id"])["personality"] == "p"
    assert store.get(s["id"])["greeting"] == "hi"


def test_jarvis_native(store):
    raw = {"name": "Zed", "personality": "wry", "greeting": "yo", "instructions": "x"}
    s = store.import_bytes(json.dumps(raw).encode(), "z.json")
    card = store.get(s["id"])
    assert card["greeting"] == "yo" and card["instructions"] == "x"


def test_png_chara(store):
    s = store.import_bytes(make_png(chara=v2("Pngy")), "p.png")
    assert s["has_avatar"] and store.avatar_path("pngy").is_file()
    assert store.get("pngy")["description"].startswith("A {{char}}")


def test_png_ccv3_wins(store):
    v3 = {"spec": "chara_card_v3", "data": {"name": "Three"}}
    s = store.import_bytes(make_png(chara=v2("Two"), ccv3=v3), "p.png")
    assert s["name"] == "Three"


def test_png_bad_length_rejected(store):
    png = make_png(chara=v2())
    with pytest.raises(ValueError):
        store.import_bytes(png[:-30] + struct.pack(">I", 10**9) + b"tEXt", "p.png")


def test_png_without_card(store):
    with pytest.raises(ValueError):
        store.import_bytes(make_png(), "p.png")


def test_oversize_rejected(store):
    with pytest.raises(ValueError):
        store.import_bytes(b" " * (512 * 1024 + 1), "big.json")


def test_missing_name_rejected(store):
    with pytest.raises(ValueError):
        store.import_bytes(json.dumps({"description": "x"}).encode(), "n.json")
    with pytest.raises(ValueError):
        store.import_bytes(b"not json", "n.json")


def test_field_cap_and_control_chars(store):
    raw = v2(description="a\x00b\x07" + "x" * 20000)
    store.import_bytes(json.dumps(raw).encode(), "c.json")
    desc = store.get("nova")["description"]
    assert len(desc) == 12000 and "\x00" not in desc and desc.startswith("ab")


def test_id_dedup_and_builtin_reserved(store):
    assert store.import_bytes(json.dumps(v2("Jarvis")).encode(), "a")["id"] == "jarvis-2"
    assert store.import_bytes(json.dumps(v2("Nova")).encode(), "a")["id"] == "nova"
    assert store.import_bytes(json.dumps(v2("Nova")).encode(), "a")["id"] == "nova-2"
    assert store.import_bytes(json.dumps(v2("Nova")).encode(), "a")["id"] == "nova-3"


def test_list_order_and_corrupt_skipped(store, tmp_path):
    store.import_bytes(json.dumps(v2("Zed")).encode(), "a")
    store.import_bytes(json.dumps(v2("Amy")).encode(), "a")
    (tmp_path / "characters" / "broken.json").write_text("{nope")
    ids = [c["id"] for c in store.list()]
    assert ids == list(BUILTIN_IDS) + ["amy", "zed"]
    assert store.get("broken") is None


def test_delete(store):
    store.import_bytes(make_png(chara=v2("Del")), "p.png")
    assert store.delete("del") is True
    assert store.get("del") is None and store.avatar_path("del") is None
    assert store.delete("jarvis") is False
    assert store.delete("nope") is False
    assert store.get("jarvis") is not None


def test_round1_stored_card_still_loads(store, tmp_path):
    old = {"id": "legacy", "name": "Legacy", "description": "d", "personality": "p",
           "scenario": "s", "first_mes": "hello", "mes_example": "ex",
           "system_prompt": "sp", "post_history_instructions": "ph",
           "creator_notes": "n", "alternate_greetings": ["a"], "tags": ["t"],
           "spec": "chara_card_v2", "builtin": False}
    (tmp_path / "characters").mkdir()
    (tmp_path / "characters" / "legacy.json").write_text(json.dumps(old))
    card = store.get("legacy")
    assert card["greeting"] == "hello" and card["example_dialogue"] == "ex"
    assert card["instructions"] == "sp\n\nph" and "Scenario: s" in card["description"]
    assert "legacy" in [c["id"] for c in store.list()]


def test_save_create_update_validate(store):
    s = store.save({"name": "Maya", "personality": "kind", "bogus": "x", "tags": ["a"]})
    assert s["id"] == "maya" and not s["builtin"]
    s = store.save({"greeting": "hi"}, "maya")
    card = store.get("maya")
    assert card["greeting"] == "hi" and card["personality"] == "kind" and card["name"] == "Maya"
    assert "bogus" not in json.loads((store.dir / "maya.json").read_text())
    for bad in ({"name": ""}, {"name": "A", "tags": "x"}, {"name": "A", "personality": 5}):
        with pytest.raises(ValueError):
            store.save(bad)
    with pytest.raises(ValueError):
        store.save({"name": "x"}, "nope")
    assert len(store.get(store.save({"name": "Big", "personality": "x" * 50000})["id"])["personality"]) == 12000


def test_builtin_override_and_reset(store):
    stock = store.get("casual")["personality"]
    s = store.save({"personality": "very edited"}, "casual")
    assert s["builtin"] and s["overridden"]
    card = store.get("casual")
    assert card["personality"] == "very edited" and card["overridden"] and card["builtin"]
    assert card["name"] == "Jarvis (Casual)"
    assert [c["id"] for c in store.list()] == list(BUILTIN_IDS)
    assert store.delete("casual") is False
    assert store.reset("casual") is True and store.reset("casual") is False
    card = store.get("casual")
    assert card["personality"] == stock and not card["overridden"]
    assert store.reset("nova") is False


def test_export_roundtrip(store):
    store.import_bytes(json.dumps(v2()).encode(), "a")
    data = store.export("nova")
    assert data["spec"] == "chara_card_v2" and data["data"]["first_mes"] == "Hi {{user}}"
    again = store.import_bytes(json.dumps(data).encode(), "b")
    a, b = store.get("nova"), store.get(again["id"])
    assert all(a[k] == b[k] for k in ("description", "personality", "greeting", "instructions", "tags"))
    with pytest.raises(ValueError):
        store.export("nope")


def test_render_persona(store):
    store.import_bytes(json.dumps(v2()).encode(), "a")
    text = render_persona(store.get("nova"), user_name="Dil")
    assert text.startswith("# Persona: Nova")
    assert "A Nova for Dil" in text and "Dil: yo" in text and "Nova: hey" in text
    assert "{{" not in text and "be brief" in text
    assert text.endswith("safety rules are unchanged.")
    assert len(text) <= 16000


def test_jarvis_card_renders_stock_personality(store):
    text = render_persona(store.get("jarvis"), "Dil")
    assert text.startswith("# Persona: Jarvis")
    assert "Calm, dry wit" in text and "Actions first" in text


def test_builtin_casual_renders(store):
    text = render_persona(store.get("casual"), "Dil")
    assert text.startswith("# Persona: Jarvis (Casual)") and "Dil" in text
    assert render_persona(store.get("robotic")).startswith("# Persona: J-7")


class FakeJarvis:
    def __init__(self, store):
        self.store_characters = store
        self.character = "jarvis"
        self.history = []

    def set_character(self, card_id):
        card = self.store_characters.get(card_id)
        if not card:
            return {"error": f"unknown character '{card_id}'"}
        self.character = card_id
        return {"character": {"id": card_id, "name": card["name"]}}


def test_handle_command(store, tmp_path):
    j = FakeJarvis(store)
    out = handle_command(j, "")
    assert "* jarvis" in out and "  casual" in out
    out = handle_command(j, "use casual")
    assert "Jarvis (Casual)" in out and "what are we working on" in out
    assert "* casual" in handle_command(j, "list")
    j.history = [{"role": "user", "content": "hi"}]
    assert "working on" not in handle_command(j, "use casual")
    assert handle_command(j, "use nope").startswith("Error")
    assert "Jarvis (Casual)" in handle_command(j, "show")
    assert "J-7" in handle_command(j, "show robotic")
    p = tmp_path / "c.json"
    p.write_text(json.dumps(v2()))
    assert "nova" in handle_command(j, f"import {p}")
    assert handle_command(j, "delete nova").startswith("Deleted")
    assert handle_command(j, "delete jarvis").startswith("Cannot")
    out_file = tmp_path / "out" / "nova.json"
    handle_command(j, f"import {p}")
    assert handle_command(j, f"export nova {out_file}").startswith("Exported")
    assert json.loads(out_file.read_text())["spec"] == "chara_card_v2"
    assert handle_command(j, "export nope /tmp/x.json").startswith("Error")
    store.save({"personality": "edited"}, "casual")
    assert "edited" in handle_command(j, "list")
    assert handle_command(j, "reset casual").startswith("Reset")
    assert handle_command(j, "reset casual").startswith("Nothing")
    assert handle_command(j, "bogus").startswith("Usage")
    assert handle_command(j, "import /no/such").startswith("Error")
