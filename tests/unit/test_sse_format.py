"""Unit tests for `dashboard.sse.format_sse` -- no DB, no HTTP.

The framing rules pinned here are the highest-value tests in the SSE
implementation: `data:` cannot contain a raw newline, so a multi-line HTML
fragment must become one `data:` line per source line, and every frame must
end with a blank line, per the SSE wire format."""

from dashboard.sse import format_sse


def test_event_and_data_are_separate_lines() -> None:
    frame = format_sse(event="balances", data="<p>hi</p>")
    assert frame == b"event: balances\ndata: <p>hi</p>\n\n"


def test_frame_ends_with_a_blank_line() -> None:
    frame = format_sse(event="x", data="y")
    assert frame.endswith(b"\n\n")


def test_multiline_data_becomes_one_data_line_per_source_line() -> None:
    frame = format_sse(event="balances", data="<table>\n<tr></tr>\n</table>")
    text = frame.decode("utf-8")
    assert "data: <table>\n" in text
    assert "data: <tr></tr>\n" in text
    assert "data: </table>\n" in text
    # No raw newline ever escapes a data: line -- every line of the payload
    # got its own prefix.
    assert text.count("data: ") == 3


def test_comment_frame_uses_colon_prefix() -> None:
    frame = format_sse(comment="keep-alive")
    assert frame == b": keep-alive\n\n"


def test_multiline_comment_gets_one_colon_line_per_source_line() -> None:
    frame = format_sse(comment="line one\nline two")
    text = frame.decode("utf-8")
    assert ": line one\n" in text
    assert ": line two\n" in text


def test_connected_comment_is_exactly_this_shape() -> None:
    assert format_sse(comment="connected") == b": connected\n\n"


def test_format_sse_returns_bytes_not_str() -> None:
    assert isinstance(format_sse(event="x", data="y"), bytes)
    assert isinstance(format_sse(comment="z"), bytes)


def test_carriage_return_variants_are_each_split_into_their_own_line() -> None:
    frame = format_sse(event="x", data="a\r\nb\rc")
    text = frame.decode("utf-8")
    assert "data: a\n" in text
    assert "data: b\n" in text
    assert "data: c\n" in text
