"""Build safe Evidence navigation metadata without exposing raw sources."""

from __future__ import annotations

from typing import Any

from .contracts import validate_document


def build_evidence_locator(sop: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    validate_document(sop, "sop.schema.json")
    evidence = next(
        (item for item in sop["evidence_catalog"] if item["evidence_id"] == evidence_id),
        None,
    )
    if evidence is None:
        raise KeyError(evidence_id)
    step_ids = [
        step["step_id"] for step in sop["steps"] if evidence_id in step["evidence"]
    ]
    locator = {
        key: value
        for key, value in evidence["locator"].items()
        if key in {"page", "paragraph", "start_ms", "end_ms"}
    }
    if evidence["source_type"] == "pdf":
        kind = "PDF_PAGE"
        label = f"PDF第{locator['page']}页"
        if locator.get("paragraph"):
            label += f" · {locator['paragraph']}"
        safe_preview = None
    else:
        kind = "VIDEO_TIME" if evidence["source_type"] == "video" else "AUDIO_TIME"
        label = f"{locator['start_ms'] / 1000:.1f}–{locator['end_ms'] / 1000:.1f}秒"
        safe_preview = (
            f"/api/n31/checklist/previews/{step_ids[0]}"
            if evidence["source_type"] == "video" and step_ids
            else None
        )
    document = {
        "artifact_type": "SAFE_EVIDENCE_LOCATOR",
        "version": 1,
        "case_id": sop["case_id"],
        "evidence_id": evidence_id,
        "source_type": evidence["source_type"],
        "source_ref": evidence["source_ref"],
        "claim": evidence["claim"],
        "classification": evidence["classification"],
        "review_status": evidence["review_status"],
        "step_ids": step_ids,
        "locator": locator,
        "navigation": {
            "kind": kind,
            "label": label,
            "safe_preview_url": safe_preview,
            "raw_source_url": None,
        },
        "data_policy": {
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "external_model_calls": 0,
        },
    }
    return validate_document(document, "evidence_locator_response.schema.json")
