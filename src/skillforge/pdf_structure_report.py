"""Build a text-free, reproducible PDF structure and retrieval QA report."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .demo import ROOT
from .pdf_ingest import OCRMode, build_pdf_search_index, extract_pdf, search_pdf_index


SCHEMA_NAME = "pdf_structure_report.schema.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"路径必须位于项目目录内: {path}")
    return resolved


def build_pdf_structure_report(
    manifest_path: Path,
    query_path: Path,
    private_output_dir: Path,
    *,
    project_root: Path = ROOT,
    ocr_mode: OCRMode = "disabled",
    ocr_languages: str = "chi_sim+eng",
    ocr_dpi: int = 200,
    ocr_tessdata: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    manifest_path = _inside(manifest_path, project_root)
    query_path = _inside(query_path, project_root)
    private_output_dir = _inside(private_output_dir, project_root)
    if ocr_tessdata is not None:
        ocr_tessdata = _inside(ocr_tessdata, project_root)
    manifest = _read_json(manifest_path)
    query_config = _read_json(query_path)
    if manifest.get("version") != 1 or query_config.get("version") != 1:
        raise ValueError("只支持版本1的摄取清单和检索验证配置")
    if manifest.get("case_id") != query_config.get("case_id"):
        raise ValueError("摄取清单与检索验证配置的case_id不一致")
    if manifest.get("external_processing_authorized") is not False:
        raise ValueError("PDF结构评测必须是本地处理且禁止外部发送")
    queries = query_config.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("至少需要一个检索验证词")
    query_ids = [str(item.get("query_id", "")) for item in queries]
    if len(query_ids) != len(set(query_ids)) or any(
        not re_id.startswith("Q") for re_id in query_ids
    ):
        raise ValueError("query_id必须存在且唯一")

    pdf_sources = [item for item in manifest["sources"] if item["type"] == "pdf"]
    if not pdf_sources:
        raise ValueError("摄取清单中没有PDF来源")
    source_reports: list[dict[str, Any]] = []
    indexes: list[dict[str, Any]] = []
    for source in pdf_sources:
        source_ref = source["source_id"]
        if source.get("approved_for_local_ingest") is not True:
            raise ValueError(f"{source_ref}: 未批准本地摄取")
        if not source.get("rights_status"):
            raise ValueError(f"{source_ref}: 缺少rights_status")
        pdf_path = _inside(project_root / source["path"], project_root)
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        source_dir = private_output_dir / source_ref
        extracted = extract_pdf(
            pdf_path,
            source_dir / "pages",
            ocr_mode=ocr_mode,
            ocr_languages=ocr_languages,
            ocr_dpi=ocr_dpi,
            ocr_tessdata=ocr_tessdata,
        )
        index = build_pdf_search_index(source_ref, extracted)
        _write_json(source_dir / "search_index.json", index)
        indexes.append(index)
        source_reports.append(
            {
                "source_ref": source_ref,
                "input_sha256": _sha256(pdf_path),
                "rights_status": source["rights_status"],
                "page_count": extracted["page_count"],
                "character_count": extracted["character_count"],
                "block_count": extracted["block_count"],
                "block_counts_by_kind": extracted["block_counts_by_kind"],
                "needs_ocr_page_count": extracted["needs_ocr_page_count"],
                "ocr_applied_page_count": extracted["ocr_applied_page_count"],
                "search_chunk_count": index["chunk_count"],
            }
        )

    query_reports: list[dict[str, Any]] = []
    for query in queries:
        combined = [
            hit
            for index in indexes
            for hit in search_pdf_index(index, str(query["query"]), limit=5)
        ]
        combined.sort(
            key=lambda item: (
                -item["score"],
                item["source_ref"],
                item["page"],
                item["block_id"],
            )
        )
        exact_match_count = sum(item["exact_match"] for item in combined)
        query_reports.append(
            {
                "query_id": query["query_id"],
                "query": query["query"],
                "status": "PASSED" if exact_match_count else "FAILED",
                "hit_count": len(combined),
                "exact_match_count": exact_match_count,
                "top_hits": [
                    {
                        key: item[key]
                        for key in (
                            "source_ref",
                            "page",
                            "block_id",
                            "kind",
                            "score",
                            "exact_match",
                        )
                    }
                    for item in combined[:5]
                ],
            }
        )

    block_counts = Counter()
    for source in source_reports:
        block_counts.update(source["block_counts_by_kind"])
    summary = {
        "source_count": len(source_reports),
        "page_count": sum(item["page_count"] for item in source_reports),
        "character_count": sum(item["character_count"] for item in source_reports),
        "block_count": sum(item["block_count"] for item in source_reports),
        "block_counts_by_kind": dict(sorted(block_counts.items())),
        "needs_ocr_page_count": sum(
            item["needs_ocr_page_count"] for item in source_reports
        ),
        "ocr_applied_page_count": sum(
            item["ocr_applied_page_count"] for item in source_reports
        ),
        "search_chunk_count": sum(
            item["search_chunk_count"] for item in source_reports
        ),
        "query_count": len(query_reports),
        "passed_query_count": sum(
            item["status"] == "PASSED" for item in query_reports
        ),
    }
    status = (
        "COMPLETED"
        if summary["needs_ocr_page_count"] == 0
        and summary["passed_query_count"] == summary["query_count"]
        else "NEEDS_REVIEW"
    )
    report = {
        "version": 1,
        "case_id": manifest["case_id"],
        "report_id": "N31_PDF_STRUCTURE_V1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "ocr_mode": ocr_mode,
        "ocr_configuration": {
            "languages": ocr_languages,
            "dpi": ocr_dpi,
            "traineddata_sha256": (
                {
                    path.stem: _sha256(path)
                    for path in sorted(ocr_tessdata.glob("*.traineddata"))
                }
                if ocr_tessdata is not None
                else {}
            ),
        },
        "external_model_calls": 0,
        "sources": source_reports,
        "queries": query_reports,
        "summary": summary,
        "data_policy": {
            "contains_pdf_text": False,
            "contains_page_images": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "external_model_calls": 0,
            "raw_pdf_and_private_index_git_ignored": True,
        },
    }
    return validate_document(report, SCHEMA_NAME)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "cases/n31/ingest_manifest.json",
    )
    parser.add_argument(
        "--queries",
        type=Path,
        default=ROOT / "cases/n31/pdf_validation_queries.json",
    )
    parser.add_argument(
        "--private-output",
        type=Path,
        default=ROOT / "cases/n31/output/pdf_structure_v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "cases/n31/evaluations/pdf_structure_v1.json",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=["disabled", "auto", "required"],
        default="auto",
    )
    parser.add_argument("--ocr-languages", default="chi_sim+eng")
    parser.add_argument("--ocr-dpi", type=int, default=200)
    parser.add_argument(
        "--ocr-tessdata",
        type=Path,
        default=ROOT / "outputs/cache/tessdata",
    )
    args = parser.parse_args()
    report = build_pdf_structure_report(
        args.manifest,
        args.queries,
        args.private_output,
        ocr_mode=args.ocr_mode,
        ocr_languages=args.ocr_languages,
        ocr_dpi=args.ocr_dpi,
        ocr_tessdata=args.ocr_tessdata,
    )
    _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
