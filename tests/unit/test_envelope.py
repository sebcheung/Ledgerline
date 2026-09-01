import uuid

from ledger.webhooks.signing import build_envelope, serialize_envelope

EVENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_serialize_envelope_is_byte_stable_across_insertion_order() -> None:
    a = {"event_id": str(EVENT_ID), "event_type": "transaction.posted", "payload": {"x": 1, "y": 2}}
    b = {"payload": {"y": 2, "x": 1}, "event_type": "transaction.posted", "event_id": str(EVENT_ID)}
    assert serialize_envelope(a) == serialize_envelope(b)


def test_serialize_envelope_uses_compact_separators() -> None:
    raw = serialize_envelope({"a": 1, "b": [1, 2]})
    assert b" " not in raw


def test_build_envelope_shape() -> None:
    envelope = build_envelope(EVENT_ID, "transaction.posted", {"transaction": {"id": "x"}})
    assert envelope == {
        "event_id": str(EVENT_ID),
        "event_type": "transaction.posted",
        "payload": {"transaction": {"id": "x"}},
    }


def test_build_then_serialize_round_trip_is_deterministic() -> None:
    envelope1 = build_envelope(EVENT_ID, "transaction.posted", {"a": 1, "b": 2})
    envelope2 = build_envelope(EVENT_ID, "transaction.posted", {"b": 2, "a": 1})
    assert serialize_envelope(envelope1) == serialize_envelope(envelope2)
