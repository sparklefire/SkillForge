from __future__ import annotations

import hashlib
import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.submission import _check_training_video_review_private_state
from skillforge.training_video_review import (
    DEFAULT_MANIFEST,
    DEFAULT_VIDEO,
    TrainingVideoReviewError,
    _write_private_json,
    initialize_training_video_review,
    training_video_review_qa_issue,
    verify_training_video_review,
    verify_training_video_review_document,
)


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_video_basis(tmp_path: Path) -> tuple[Path, Path]:
    public = tmp_path / "public"
    public.mkdir(parents=True)
    video = public / "n31_training_video_v1.mp4"
    video.write_bytes(b"synthetic current training video")
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["output"]["sha256"] = _sha256(video)
    manifest["output"]["bytes"] = video.stat().st_size
    manifest_path = public / "n31_training_video_manifest_v1.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    validate_document(manifest, "training_video_manifest.schema.json")
    return manifest_path, video


def ready_review_document(manifest_path: Path, video_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": "2026-07-19T03:00:00+00:00",
        "status": "READY_FOR_CHECK",
        "watched_at": "2026-07-19T02:30:00+00:00",
        "playback_method": "LOCAL_PLAYER",
        "video": {
            "filename": video_path.name,
            "sha256": _sha256(video_path),
            "bytes": video_path.stat().st_size,
            "duration_ms": manifest["output"]["duration_ms"],
        },
        "manifest_sha256": _sha256(manifest_path),
        "checks": {
            "full_playback_completed": True,
            "narration_audible": True,
            "narration_pacing_acceptable": True,
            "visuals_and_narration_in_sync": True,
            "all_steps_understandable": True,
            "no_sensitive_content_observed": True,
            "no_playback_corruption": True,
            "final_cut_accepted": True,
        },
        "notes": "仅保存在私有记录中的观看备注",
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_credentials": False,
            "git_tracked": False,
        },
    }


def private_ready_review(
    tmp_path: Path,
    *,
    manifest_path: Path | None = None,
    video_path: Path | None = None,
) -> tuple[Path, Path, Path, Path, Path, dict]:
    if manifest_path is None or video_path is None:
        manifest_path, video_path = public_video_basis(tmp_path)
    private = tmp_path / "private"
    review = private / "training_video_review.json"
    qa = private / "training_video_review_qa.json"
    initialize_training_video_review(
        review,
        manifest_path=manifest_path,
        video_path=video_path,
        private_root=private,
    )
    _write_private_json(
        ready_review_document(manifest_path, video_path),
        review,
        private_root=private,
    )
    report = verify_training_video_review(
        review,
        manifest_path=manifest_path,
        video_path=video_path,
        private_root=private,
    )
    _write_private_json(report, qa, private_root=private)
    return private, review, qa, manifest_path, video_path, report


def test_current_tracked_video_matches_its_manifest() -> None:
    manifest = validate_document(
        json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8")),
        "training_video_manifest.schema.json",
    )

    assert manifest["status"] == "READY_FOR_HUMAN_REVIEW"
    assert manifest["final_human_review_required"] is True
    assert manifest["output"]["duration_ms"] == 80000
    assert manifest["output"]["sha256"] == _sha256(DEFAULT_VIDEO)
    assert manifest["output"]["bytes"] == DEFAULT_VIDEO.stat().st_size


def test_template_binds_current_video_is_private_and_never_overwrites(
    tmp_path: Path,
) -> None:
    manifest, video = public_video_basis(tmp_path)
    private = tmp_path / "private"
    review = private / "training_video_review.json"
    initialize_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    document = validate_document(
        json.loads(review.read_text(encoding="utf-8")),
        "training_video_review.schema.json",
    )

    assert document["status"] == "PENDING_INPUT"
    assert document["video"]["sha256"] == _sha256(video)
    assert document["manifest_sha256"] == _sha256(manifest)
    assert not any(document["checks"].values())
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(review.stat().st_mode) == 0o600
    with pytest.raises(TrainingVideoReviewError, match="不会覆盖"):
        initialize_training_video_review(
            review,
            manifest_path=manifest,
            video_path=video,
            private_root=private,
        )


def test_ready_review_passes_without_copying_notes_or_paths_to_qa(
    tmp_path: Path,
) -> None:
    _, review, _, _, _, report = private_ready_review(tmp_path)
    validate_document(report, "training_video_review_qa.schema.json")

    assert report["status"] == "READY_FOR_HUMAN_CONFIRMATION"
    assert report["video"]["duration_ms"] == 80000
    assert all(report["checks"].values())
    serialized = json.dumps(report, ensure_ascii=False)
    assert "私有记录" not in serialized
    assert str(review.resolve()) not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["checks"].__setitem__("full_playback_completed", False),
        lambda value: value["checks"].__setitem__("narration_pacing_acceptable", False),
        lambda value: value["checks"].__setitem__("no_sensitive_content_observed", False),
        lambda value: value["video"].__setitem__("sha256", "0" * 64),
        lambda value: value.__setitem__("manifest_sha256", "0" * 64),
    ],
)
def test_incomplete_or_stale_review_is_rejected(tmp_path: Path, mutation) -> None:
    manifest_path, video_path = public_video_basis(tmp_path)
    document = deepcopy(ready_review_document(manifest_path, video_path))
    mutation(document)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    basis = {
        "manifest": manifest,
        "manifest_sha256": _sha256(manifest_path),
        "video": {
            "filename": video_path.name,
            "sha256": _sha256(video_path),
            "bytes": video_path.stat().st_size,
            "duration_ms": manifest["output"]["duration_ms"],
        },
    }

    with pytest.raises((TrainingVideoReviewError, ContractValidationError)):
        verify_training_video_review_document(
            document,
            review_sha256="1" * 64,
            review_bytes=100,
            basis=basis,
        )


def test_manifest_must_remain_at_human_review_boundary(tmp_path: Path) -> None:
    manifest_path, video_path = public_video_basis(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "FINAL_APPROVED"
    manifest["final_human_review_required"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    document = ready_review_document(manifest_path, video_path)
    basis = {
        "manifest": validate_document(manifest, "training_video_manifest.schema.json"),
        "manifest_sha256": _sha256(manifest_path),
        "video": document["video"],
    }

    with pytest.raises(TrainingVideoReviewError, match="manifest_ready"):
        verify_training_video_review_document(
            document,
            review_sha256="1" * 64,
            review_bytes=100,
            basis=basis,
        )


def test_permission_drift_is_rejected(tmp_path: Path) -> None:
    _, review, _, manifest, video, _ = private_ready_review(tmp_path)
    review.chmod(0o644)

    with pytest.raises(TrainingVideoReviewError, match="权限"):
        verify_training_video_review(
            review,
            manifest_path=manifest,
            video_path=video,
            private_root=review.parent,
        )


def test_qa_binding_rejects_url_permissions_and_changed_record(tmp_path: Path) -> None:
    private, review, qa, manifest, video, report = private_ready_review(tmp_path)
    evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(review.resolve()),
        "sha256": report["review_sha256"],
        "size_bytes": report["review_bytes"],
    }

    assert training_video_review_qa_issue(
        qa, evidence, manifest_path=manifest, video_path=video
    ) is None
    changed_binding = json.loads(review.read_text(encoding="utf-8"))
    changed_binding["video"]["sha256"] = "f" * 64
    _write_private_json(changed_binding, review, private_root=private)
    forged_report = deepcopy(report)
    forged_report["review_sha256"] = _sha256(review)
    forged_report["review_bytes"] = review.stat().st_size
    _write_private_json(forged_report, qa, private_root=private)
    forged_evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(review.resolve()),
        "sha256": _sha256(review),
        "size_bytes": review.stat().st_size,
    }
    assert (
        training_video_review_qa_issue(
            qa,
            forged_evidence,
            manifest_path=manifest,
            video_path=video,
        )
        == "TRAINING_VIDEO_REVIEW_QA_INVALID"
    )
    _write_private_json(
        ready_review_document(manifest, video), review, private_root=private
    )
    _write_private_json(report, qa, private_root=private)
    assert (
        training_video_review_qa_issue(
            qa,
            {"kind": "HTTPS_URL"},
            manifest_path=manifest,
            video_path=video,
        )
        == "TRAINING_VIDEO_REVIEW_REQUIRES_LOCAL_FILE"
    )
    review.chmod(0o644)
    assert (
        training_video_review_qa_issue(
            qa, evidence, manifest_path=manifest, video_path=video
        )
        == "TRAINING_VIDEO_REVIEW_PERMISSIONS_UNSAFE"
    )
    review.chmod(0o600)
    changed = json.loads(review.read_text(encoding="utf-8"))
    changed["notes"] = "changed"
    _write_private_json(changed, review, private_root=private)
    current = verify_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    assert (
        training_video_review_qa_issue(
            qa,
            {
                "kind": "LOCAL_FILE",
                "locator": str(review.resolve()),
                "sha256": current["review_sha256"],
                "size_bytes": current["review_bytes"],
            },
            manifest_path=manifest,
            video_path=video,
        )
        == "TRAINING_VIDEO_REVIEW_RECORD_CHANGED"
    )


def test_submission_state_is_safe_for_absent_ready_and_stale_review(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    manifest, video = public_video_basis(root)
    target = root / "output/video"
    target.mkdir(parents=True)
    target_manifest = target / DEFAULT_MANIFEST.name
    target_video = target / DEFAULT_VIDEO.name
    target_manifest.write_bytes(manifest.read_bytes())
    target_video.write_bytes(video.read_bytes())

    absent = _check_training_video_review_private_state(root)
    assert absent["status"] == "PASSED"
    assert "ABSENT" in absent["details"][0]

    private = root / "outputs/submission"
    review = private / "training_video_review.json"
    qa = private / "training_video_review_qa.json"
    initialize_training_video_review(
        review,
        manifest_path=target_manifest,
        video_path=target_video,
        private_root=private,
    )
    _write_private_json(
        ready_review_document(target_manifest, target_video),
        review,
        private_root=private,
    )
    report = verify_training_video_review(
        review,
        manifest_path=target_manifest,
        video_path=target_video,
        private_root=private,
    )
    _write_private_json(report, qa, private_root=private)

    ready = _check_training_video_review_private_state(root)
    assert ready["status"] == "PASSED"
    assert "时长=80000毫秒" in ready["details"][0]
    changed = json.loads(review.read_text(encoding="utf-8"))
    changed["notes"] = "changed"
    _write_private_json(changed, review, private_root=private)
    stale = _check_training_video_review_private_state(root)
    assert stale["status"] == "FAILED"
    assert str(review.resolve()) not in json.dumps(stale, ensure_ascii=False)


def test_ready_schema_cannot_claim_success_with_failed_check(tmp_path: Path) -> None:
    *_, report = private_ready_review(tmp_path)
    invalid = deepcopy(report)
    invalid["checks"]["final_cut_accepted"] = False

    with pytest.raises(ContractValidationError):
        validate_document(invalid, "training_video_review_qa.schema.json")


def test_training_video_review_script_is_executable() -> None:
    script = ROOT / "scripts/check_training_video_review.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
