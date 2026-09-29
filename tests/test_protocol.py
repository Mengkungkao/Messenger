"""Message IDs, duplicate suppression, splitting, and packet builders."""

from lora import packet as pk
from lora import protocol


def test_split_short_text_is_one_chunk():
    assert protocol.split_text("Meet me at five o'clock") == ["Meet me at five o'clock"]


def test_split_normalises_whitespace_and_drops_empty():
    assert protocol.split_text("  a \n b\t c ") == ["a b c"]
    assert protocol.split_text("   ") == []


def test_split_at_word_boundaries_within_limit():
    words = " ".join(f"word{i}" for i in range(100))
    chunks = protocol.split_text(words, limit=50)
    assert all(len(c.encode()) <= 50 for c in chunks)
    assert " ".join(chunks) == words


def test_split_cuts_an_overlong_word_without_breaking_characters():
    word = "é" * 150            # 300 bytes, 2 per character
    chunks = protocol.split_text(word, limit=pk.MAX_MESSAGE)
    assert "".join(chunks) == word
    assert all(len(c.encode()) <= pk.MAX_MESSAGE for c in chunks)


def test_clip_utf8():
    assert protocol.clip_utf8("abc", 10) == b"abc"
    assert protocol.clip_utf8("aé", 2) == b"a"     # é would be cut in half


def test_ack_goes_back_to_the_sender_with_the_same_id():
    message = protocol.text_packet(0x0001, protocol.BROADCAST, 1024, "hi")
    ack = protocol.ack_packet(0x0002, message)
    assert (ack.type, ack.msg_id, ack.src, ack.dst) == (pk.ACK, 1024, 0x0002, 0x0001)


def test_is_for():
    assert protocol.is_for(pk.Packet(pk.TEXT, 1, 5, 7), 7)
    assert protocol.is_for(pk.Packet(pk.TEXT, 1, 5, protocol.BROADCAST), 7)
    assert not protocol.is_for(pk.Packet(pk.TEXT, 1, 5, 8), 7)


def test_hello_name_is_clipped():
    hello = protocol.hello_packet(1, 2, "x" * 100)
    assert len(hello.payload) == 24 and hello.type == pk.HELLO
    assert protocol.hello_packet(1, 2, "a", reply=True).type == pk.HELLO_REPLY


def test_message_ids_persist_across_restarts(tmp_path):
    path = tmp_path / "ids.json"
    ids = protocol.MessageIds(path)
    first = ids.next()
    second = ids.next()
    assert second == first % 0xFFFF + 1
    assert protocol.MessageIds(path).next() == second % 0xFFFF + 1


def test_message_ids_wrap_past_zero(tmp_path):
    path = tmp_path / "ids.json"
    path.write_text('{"next_id": 65535}')
    ids = protocol.MessageIds(path)
    assert [ids.next(), ids.next()] == [65535, 1]


def test_message_ids_survive_a_corrupt_file(tmp_path):
    path = tmp_path / "ids.json"
    path.write_text("not json")
    assert 1 <= protocol.MessageIds(path).next() <= 0xFFFF


def test_duplicate_filter_forgets_after_the_window():
    now = [0.0]
    seen = protocol.DuplicateFilter(window=10, clock=lambda: now[0])
    assert not seen.seen(1, 100)
    assert seen.seen(1, 100)
    assert not seen.seen(2, 100)        # same ID, other sender
    now[0] = 11.0
    assert not seen.seen(1, 100)
