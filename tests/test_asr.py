from skillforge.asr import parse_asr_sse


def test_parses_timestamped_sse_transcription() -> None:
    raw = """event: transcript.text.delta
data: {"type":"transcript.text.delta","delta":"断开","start_time":0,"end_time":300}

data: {"type":"transcript.text.delta","delta":"电源","start_time":300,"end_time":600}

data: {"type":"transcript.text.done","text":"断开电源","usage":{"total_tokens":12}}
"""
    result = parse_asr_sse(raw)
    assert result["text"] == "断开电源"
    assert result["segments"][0] == {"text": "断开", "start_ms": 0, "end_ms": 300}
    assert result["usage"]["total_tokens"] == 12
