from skillforge.json_tools import parse_json_response


def test_extracts_fenced_json_and_repairs_trailing_comma() -> None:
    raw = """```json
    {"ok": true, "items": [1, 2,],}
    ```"""
    assert parse_json_response(raw) == {"ok": True, "items": [1, 2]}


def test_extracts_json_from_short_explanation() -> None:
    assert parse_json_response('结果如下： {"ok": true}') == {"ok": True}
