from __future__ import annotations

import json
import hashlib
import stat
from pathlib import Path

import pytest

import skillforge.final_recording as final_recording_module
from skillforge.contracts import validate_document
from skillforge.final_recording import evaluate_final_recording, write_private_report
from skillforge.final_recording_review import (
    _write_private_json as _write_recording_review_json,
    initialize_final_recording_review,
    verify_final_recording_review,
)
from skillforge.final_rehearsal import (
    _write_private_json as _write_rehearsal_json,
    initialize_final_rehearsal,
    verify_final_rehearsal,
)
from skillforge.human_gates import HumanGateError, HumanGateStore
from skillforge.official_rules_review import (
    _write_private_json as _write_rules_review_json,
    attach_local_source,
    initialize_official_rules_review,
    verify_official_rules_review,
)
from skillforge.submission import build_submission_preflight
from skillforge.team_roster import (
    _write_private_json as _write_roster_json,
    initialize_team_roster,
)
from skillforge.training_video_review import (
    _write_private_json as _write_video_review_json,
    initialize_training_video_review,
    verify_training_video_review,
)


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
GATE_IDS = [
    "TRAINING_VIDEO_FULL_WATCH",
    "FINAL_STAGE_REHEARSAL",
    "FINAL_RECORDING_REVIEW",
    "TEAM_ELIGIBILITY_CONFIRMED",
    "OFFICIAL_RULES_VERIFIED",
]


def _copied_runbook(tmp_path: Path) -> Path:
    path = tmp_path / "pitch_runbook.json"
    path.write_bytes(RUNBOOK.read_bytes())
    return path


def _ready_recording(private: Path) -> Path:
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    recording = private / "skillforge_final_recording.mp4"
    recording.write_bytes(b"private synthetic final recording")
    recording.chmod(0o600)
    report = evaluate_final_recording(
        recording,
        private_root=private,
        probe_fn=lambda _: {
            "duration_ms": 178000,
            "video_streams": [
                {"codec": "h264", "width": 1920, "height": 1080, "fps": 30.0}
            ],
            "audio_streams": [
                {"codec": "aac", "sample_rate": 48000, "channels": 2}
            ],
        },
        loudness_fn=lambda _: {
            "integrated_lufs": -18.0,
            "loudness_range_lu": 3.0,
            "true_peak_dbtp": -1.0,
        },
        interruption_fn=lambda *_: {
            "silence_total_ms": 4000,
            "silence_ratio": 0.022472,
            "maximum_contiguous_silence_ms": 1500,
            "black_total_ms": 500,
            "black_ratio": 0.002809,
            "maximum_contiguous_black_ms": 500,
        },
    )
    machine_qa = private / "final_recording_qa.json"
    write_private_report(report, machine_qa)
    sha256 = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    digest = "a" * 64
    build = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_RECORDING_BUILD",
        "generated_at": "2026-07-19T00:00:00+00:00",
        "status": "READY_FOR_HUMAN_REVIEW",
        "storyboard_sha256": sha256(
            ROOT / "config/final_recording_storyboard.json"
        ),
        "scene_count": 9,
        "target_duration_ms": 178000,
        "media": {
            "filename": recording.name,
            "sha256": sha256(recording),
            "bytes": recording.stat().st_size,
            "duration_ms": 178000,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
        },
        "scenes": [
            {
                "scene_id": f"R{order:02d}",
                "order": order,
                "duration_ms": 15000,
                "visual_kind": "SCREENSHOT",
                "visual_source_sha256": digest,
                "narration_sha256": digest,
                "tts_audio_sha256": digest,
                "rendered_sha256": digest,
                "output_probe_ms": order * 10000,
                "difference_hash_distance": 0,
                "sequence_match": True,
            }
            for order in range(1, 10)
        ],
        "tts": {
            "model": "stepaudio-2.5-tts",
            "voice": "zhixingjiejie",
            "scene_count": 9,
            "generated_count": 0,
            "reused_count": 9,
            "external_model_calls": 0,
            "text_only": True,
        },
        "machine_qa": {
            "status": "READY_FOR_HUMAN_REVIEW",
            "report_sha256": sha256(machine_qa),
            "all_checks_passed": True,
            "scene_sequence_all_matched": True,
        },
        "human_review": {
            "required": True,
            "status": "PENDING",
            "automatic_approval": False,
        },
        "data_policy": {
            "private_local_state": True,
            "screenshot_assets_private": True,
            "raw_media_sent_to_tts": False,
            "tts_text_only": True,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "automatic_human_approval": False,
        },
    }
    build_path = private / "final_recording_build.json"
    _write_recording_review_json(build, build_path, private_root=private)
    review = private / "final_recording_review.json"
    review_qa = private / "final_recording_review_qa.json"
    review.unlink(missing_ok=True)
    review_qa.unlink(missing_ok=True)
    initialize_final_recording_review(
        review,
        recording_path=recording,
        machine_qa_path=machine_qa,
        build_report_path=build_path,
        private_root=private,
    )
    review_document = json.loads(review.read_text(encoding="utf-8"))
    review_document.update(
        {
            "updated_at": "2026-07-19T00:03:01+00:00",
            "status": "READY_FOR_CHECK",
            "watch_started_at": "2026-07-19T00:00:00+00:00",
            "watch_completed_at": "2026-07-19T00:03:00+00:00",
            "playback_method": "LOCAL_PLAYER",
        }
    )
    review_document["checks"] = {
        key: True for key in review_document["checks"]
    }
    _write_recording_review_json(review_document, review, private_root=private)
    review_report = verify_final_recording_review(
        review,
        recording_path=recording,
        machine_qa_path=machine_qa,
        build_report_path=build_path,
        private_root=private,
    )
    _write_recording_review_json(review_report, review_qa, private_root=private)
    return recording


def _ready_training_video_review(
    private: Path,
    *,
    manifest: Path | None = None,
    video: Path | None = None,
) -> Path:
    manifest = manifest or ROOT / "output/video/n31_training_video_manifest_v1.json"
    video = video or ROOT / "output/video/n31_training_video_v1.mp4"
    review = private / "training_video_review.json"
    initialize_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    template = json.loads(review.read_text(encoding="utf-8"))
    template.update(
        {
            "updated_at": "2026-07-19T03:00:00+00:00",
            "status": "READY_FOR_CHECK",
            "watched_at": "2026-07-19T02:30:00+00:00",
            "playback_method": "LOCAL_PLAYER",
            "notes": "",
        }
    )
    template["video"]["duration_ms"] = manifest_document["output"]["duration_ms"]
    template["checks"] = {key: True for key in template["checks"]}
    _write_video_review_json(template, review, private_root=private)
    report = verify_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    _write_video_review_json(
        report,
        private / "training_video_review_qa.json",
        private_root=private,
    )
    return review


def _ready_rehearsal(private: Path, runbook: Path = RUNBOOK) -> Path:
    record = private / "final_stage_rehearsal.json"
    initialize_final_rehearsal(record, runbook_path=runbook, private_root=private)
    runbook_document = json.loads(runbook.read_text(encoding="utf-8"))
    boundaries = [0, 20000, 40000, 70000, 110000, 140000, 160000, 178000]
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": "2026-07-19T01:00:00+00:00",
        "status": "READY_FOR_CHECK",
        "performed_at": "2026-07-19T00:30:00+00:00",
        "run_number": 1,
        "timer_source": "STOPWATCH",
        "total_duration_ms": boundaries[-1],
        "segments": [
            {
                "phase": segment["phase"],
                "planned_start_ms": segment["start_ms"],
                "planned_end_ms": segment["end_ms"],
                "actual_start_ms": boundaries[index],
                "actual_end_ms": boundaries[index + 1],
                "script_completed": True,
                "operator_action_completed": True,
                "proof_points_verified": True,
                "fallback_ready": True,
            }
            for index, segment in enumerate(runbook_document["segments"])
        ],
        "completion": {
            "full_sequence_completed": True,
            "no_unrecovered_failure": True,
            "no_sensitive_material_shown": True,
        },
        "notes": "",
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_credentials": False,
            "git_tracked": False,
        },
    }
    _write_rehearsal_json(document, record, private_root=private)
    report = verify_final_rehearsal(
        record,
        runbook_path=runbook,
        private_root=private,
    )
    _write_rehearsal_json(
        report,
        private / "final_stage_rehearsal_qa.json",
        private_root=private,
    )
    return record


def _ready_team_roster(private: Path) -> Path:
    roster_path = private / "team_roster.json"
    initialize_team_roster(roster_path, private_root=private)
    _write_roster_json(
        {
            "version": 1,
            "case_id": "n31_media_change",
            "updated_at": "2026-07-18T00:00:00+00:00",
            "status": "READY_FOR_CHECK",
            "members": [
                {
                    "member_id": "M1",
                    "name": "测试成员甲",
                    "organization": "测试单位甲",
                    "primary_contact": True,
                    "registration_confirmed": True,
                    "one_team_only_confirmed": True,
                },
                {
                    "member_id": "M2",
                    "name": "测试成员乙",
                    "organization": "测试单位乙",
                    "primary_contact": False,
                    "registration_confirmed": True,
                    "one_team_only_confirmed": True,
                },
            ],
            "role_assignments": [
                {"role_id": "TECHNICAL_OWNER", "member_id": "M1"},
                {"role_id": "EVIDENCE_OWNER", "member_id": "M2"},
                {"role_id": "CONTENT_OWNER", "member_id": "M1"},
                {"role_id": "DEMO_OPERATOR", "member_id": "M2"},
                {"role_id": "SUBMISSION_OWNER", "member_id": "M1"},
                {"role_id": "FINAL_REVIEWER", "member_id": "M2"},
            ],
            "data_policy": {
                "private_local_state": True,
                "contains_personal_data": True,
                "git_tracked": False,
            },
        },
        roster_path,
        private_root=private,
    )
    return roster_path


def _ready_official_rules_review(private: Path) -> Path:
    review_path = private / "official_rules_review.json"
    initialize_official_rules_review(review_path, private_root=private)
    source_path = private.with_name(f"{private.name}_official_rules.txt")
    source_path.write_text("official rules material for tests", encoding="utf-8")
    attach_local_source(source_path, review_path, private_root=private)
    document = json.loads(review_path.read_text(encoding="utf-8"))
    document.update(
        {
            "updated_at": "2026-07-19T04:00:00+00:00",
            "status": "READY_FOR_CHECK",
            "reviewed_at": "2026-07-19T03:30:00+00:00",
        }
    )
    for item in document["requirements"]:
        item.update(
            {
                "finding": f"已核对 {item['requirement_id']}",
                "source_reference": "官方材料测试段落",
                "confirmed": True,
            }
        )
    document["checks"] = {key: True for key in document["checks"]}
    _write_rules_review_json(document, review_path, private_root=private)
    report = verify_official_rules_review(review_path, private_root=private)
    _write_rules_review_json(
        report,
        private / "official_rules_review_qa.json",
        private_root=private,
    )
    return review_path


def test_confirmation_is_private_hash_bound_and_revocable(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "review-note.txt"
    evidence.write_text("团队资格核对证据", encoding="utf-8")
    store_path = tmp_path / "private" / "human_gate_confirmations.json"
    _ready_team_roster(store_path.parent)
    store = HumanGateStore(store_path, runbook_path=runbook)

    result = store.confirm(
        GATE_IDS[3],
        reviewer="仅私有审核名",
        evidence_file=evidence,
        note="已核对团队资格",
    )

    assert result["valid"] is True
    assert result["summary"] == {"passed": 1, "pending": 4, "total": 5}
    assert stat.S_IMODE(store_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    document = json.loads(store_path.read_text(encoding="utf-8"))
    validate_document(document, "human_gate_confirmations.schema.json")
    assert document["confirmations"][0]["evidence"]["sha256"]
    assert "团队资格核对证据" not in store_path.read_text(encoding="utf-8")
    serialized_status = json.dumps(result, ensure_ascii=False)
    assert str(evidence.resolve()) not in serialized_status
    assert "仅私有审核名" not in serialized_status

    revoked = store.revoke(
        GATE_IDS[3],
        reviewer="仅私有审核名",
        note="报名资料更新，需要重新核对",
    )
    assert revoked["summary"] == {"passed": 0, "pending": 5, "total": 5}
    assert json.loads(store_path.read_text(encoding="utf-8"))["history"][-1]["action"] == "REVOKED"


def test_changed_evidence_invalidates_confirmation(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    private = tmp_path / "private"
    evidence = _ready_rehearsal(private, runbook)
    store = HumanGateStore(private / "state.json", runbook_path=runbook)
    store.confirm(GATE_IDS[1], reviewer="审核人", evidence_file=evidence)

    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["notes"] = "彩排记录已变化"
    _write_rehearsal_json(document, evidence, private_root=private)
    audit = store.audit()

    assert audit["valid"] is False
    assert audit["store_state"] == "INVALID"
    assert audit["confirmed_gate_ids"] == []
    assert audit["issues"] == [f"EVIDENCE_SIZE_CHANGED:{GATE_IDS[1]}"]


def test_missing_evidence_invalidates_confirmation(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    private = tmp_path / "private"
    evidence = _ready_recording(private)
    store = HumanGateStore(private / "state.json", runbook_path=runbook)
    store.confirm(GATE_IDS[2], reviewer="审核人", evidence_file=evidence)

    evidence.unlink()
    audit = store.audit()

    assert audit["valid"] is False
    assert audit["issues"] == [f"EVIDENCE_FILE_MISSING:{GATE_IDS[2]}"]


def test_duplicate_confirmation_requires_explicit_replace(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "team.txt"
    evidence.write_text("team evidence", encoding="utf-8")
    store_path = tmp_path / "private" / "state.json"
    _ready_team_roster(store_path.parent)
    store = HumanGateStore(store_path, runbook_path=runbook)
    store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)

    with pytest.raises(HumanGateError, match="--replace"):
        store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)
    with pytest.raises(HumanGateError, match="未知人工门禁"):
        store.confirm("UNKNOWN_GATE", reviewer="审核人", evidence_file=evidence)
    replaced = store.confirm(
        GATE_IDS[3],
        reviewer="复核人",
        evidence_file=evidence,
        note="复核报名信息",
        replace=True,
    )

    assert replaced["summary"]["passed"] == 1
    document = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(document["confirmations"]) == 1
    assert [item["action"] for item in document["history"]] == [
        "CONFIRMED",
        "CONFIRMED",
    ]


def test_team_gate_requires_and_binds_current_private_roster(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    private = tmp_path / "private"
    evidence = tmp_path / "registration-proof.txt"
    evidence.write_text("registration proof", encoding="utf-8")
    store = HumanGateStore(private / "state.json", runbook_path=runbook)

    with pytest.raises(HumanGateError, match="团队名单QA"):
        store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)

    roster_path = _ready_team_roster(private)
    confirmed = store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)
    assert confirmed["summary"]["passed"] == 1
    document = json.loads(store.path.read_text(encoding="utf-8"))
    context = document["confirmations"][0]["gate_context"]
    assert context["kind"] == "TEAM_ROSTER"
    assert context["roster_sha256"]
    assert "测试成员" not in json.dumps(confirmed, ensure_ascii=False)

    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster["members"][0]["organization"] = "变更后的测试单位"
    _write_roster_json(roster, roster_path, private_root=private)
    stale = store.audit()
    assert stale["valid"] is False
    assert stale["confirmed_gate_ids"] == []
    assert stale["issues"] == [f"TEAM_ROSTER_HASH_CHANGED:{GATE_IDS[3]}"]


def test_changed_runbook_makes_all_private_confirmations_stale(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    private = tmp_path / "private"
    evidence = _ready_rehearsal(private, runbook)
    store = HumanGateStore(private / "state.json", runbook_path=runbook)
    store.confirm(GATE_IDS[1], reviewer="审核人", evidence_file=evidence)

    payload = json.loads(runbook.read_text(encoding="utf-8"))
    payload["human_gates"][2]["label"] += "（修订）"
    runbook.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stale = store.audit()

    assert stale["valid"] is False
    assert stale["store_state"] == "STALE"
    assert stale["issues"] == ["RUNBOOK_HASH_CHANGED"]
    reset = store.reset_stale(reviewer="审核人", note="运行单已冻结新版本")
    assert reset["valid"] is True
    assert reset["summary"] == {"passed": 0, "pending": 5, "total": 5}


def test_insecure_store_mode_is_rejected(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "team.txt"
    evidence.write_text("team check", encoding="utf-8")
    store_path = tmp_path / "private" / "state.json"
    _ready_team_roster(store_path.parent)
    store = HumanGateStore(store_path, runbook_path=runbook)
    store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)
    store_path.chmod(0o644)

    audit = store.audit()

    assert audit["valid"] is False
    assert "STORE_MODE_NOT_600" in audit["issues"]
    with pytest.raises(HumanGateError, match="权限不安全"):
        store.confirm(
            GATE_IDS[3],
            reviewer="审核人",
            evidence_url="https://example.com/team-proof",
        )


def test_custom_store_does_not_change_broad_existing_directory(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "rules.txt"
    evidence.write_text("rules evidence", encoding="utf-8")
    broad = tmp_path / "shared"
    broad.mkdir(mode=0o755)
    broad.chmod(0o755)
    roster = _ready_team_roster(tmp_path / "private-roster")
    store = HumanGateStore(
        broad / "state.json",
        runbook_path=runbook,
        team_roster_path=roster,
    )

    with pytest.raises(HumanGateError, match="目录权限必须为700"):
        store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)

    assert stat.S_IMODE(broad.stat().st_mode) == 0o755
    assert not (broad / "state.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/rules",
        "https://user:pass@example.com/rules",
        "https://example.com/rules?token=secret",
        "https://example.com/rules#private",
    ],
)
def test_unsafe_evidence_url_is_rejected(tmp_path: Path, url: str) -> None:
    private = tmp_path / "private"
    _ready_team_roster(private)
    store = HumanGateStore(
        private / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )
    with pytest.raises(HumanGateError, match="证据网址"):
        store.confirm(GATE_IDS[3], reviewer="审核人", evidence_url=url)


def test_valid_private_confirmations_still_require_submission_form_packet(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("explicit human confirmation", encoding="utf-8")
    private = tmp_path / "private"
    video_review = _ready_training_video_review(private)
    recording = _ready_recording(private)
    rehearsal = _ready_rehearsal(private)
    roster_path = _ready_team_roster(private)
    rules_review = _ready_official_rules_review(private)
    store_path = private / "human_gate_confirmations.json"
    store = HumanGateStore(store_path, runbook_path=RUNBOOK)
    for gate_id in GATE_IDS:
        store.confirm(
            gate_id,
            reviewer="测试审核人",
            evidence_file=(
                video_review
                if gate_id == GATE_IDS[0]
                else rehearsal
                if gate_id == GATE_IDS[1]
                else recording
                if gate_id == GATE_IDS[2]
                else rules_review
                if gate_id == GATE_IDS[4]
                else evidence
            ),
        )

    report = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
        confirmations_path=store_path,
        team_roster_path=roster_path,
        final_rehearsal_path=rehearsal,
        final_rehearsal_qa_path=private / "final_stage_rehearsal_qa.json",
        training_video_review_path=video_review,
        training_video_review_qa_path=private / "training_video_review_qa.json",
        official_rules_review_path=rules_review,
        official_rules_review_qa_path=private / "official_rules_review_qa.json",
        submission_form_packet_path=private / "submission_form_packet.json",
        submission_form_prefill_path=private / "submission_form_prefill.json",
        submission_form_packet_qa_path=private / "submission_form_packet_qa.json",
    )
    checks = {item["check_id"]: item for item in report["automatic_checks"]}

    assert report["pending_human_gates"] == []
    assert report["status"] == "NOT_READY"
    assert checks["HUMAN_GATE_CONFIRMATIONS"]["status"] == "PASSED"
    assert checks["TEAM_ROSTER_PRIVATE_STATE"]["status"] == "PASSED"
    assert checks["FINAL_REHEARSAL_PRIVATE_STATE"]["status"] == "PASSED"
    assert checks["TRAINING_VIDEO_REVIEW_PRIVATE_STATE"]["status"] == "PASSED"
    assert checks["OFFICIAL_RULES_REVIEW_PRIVATE_STATE"]["status"] == "PASSED"
    assert checks["SUBMISSION_FORM_PACKET_PRIVATE_STATE"]["status"] == "FAILED"
    assert "私有表单输入缺失" in checks["SUBMISSION_FORM_PACKET_PRIVATE_STATE"]["details"][0]
    assert "人工门禁=CONFIRMED" in checks["TRAINING_VIDEO_REVIEW_PRIVATE_STATE"]["details"][0]
    assert "人工门禁=CONFIRMED" in checks["FINAL_REHEARSAL_PRIVATE_STATE"]["details"][0]
    assert "人工门禁=CONFIRMED" in checks["TEAM_ROSTER_PRIVATE_STATE"]["details"][0]
    assert "人工门禁=CONFIRMED" in checks["OFFICIAL_RULES_REVIEW_PRIVATE_STATE"]["details"][0]
    assert "人工门禁有效=5/5" in checks["HUMAN_GATE_CONFIRMATIONS"]["details"][0]
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(evidence.resolve()) not in serialized
    assert "测试审核人" not in serialized


def test_final_recording_gate_requires_matching_private_machine_qa(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    recording = private / "skillforge_final_recording.mp4"
    recording.write_bytes(b"recording without QA")
    recording.chmod(0o600)
    store = HumanGateStore(
        private / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )

    with pytest.raises(HumanGateError, match="FINAL_RECORDING_QA_MISSING"):
        store.confirm(GATE_IDS[2], reviewer="审核人", evidence_file=recording)
    with pytest.raises(HumanGateError, match="FINAL_RECORDING_REQUIRES_LOCAL_FILE"):
        store.confirm(
            GATE_IDS[2],
            reviewer="审核人",
            evidence_url="https://example.com/final-recording.mp4",
        )


def test_final_recording_gate_requires_completed_full_watch_review(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    recording = _ready_recording(private)
    (private / "final_recording_review_qa.json").unlink()
    store = HumanGateStore(
        private / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )

    with pytest.raises(HumanGateError, match="FINAL_RECORDING_REVIEW_QA_MISSING"):
        store.confirm(GATE_IDS[2], reviewer="审核人", evidence_file=recording)


def test_final_rehearsal_gate_requires_current_timed_record_and_qa(
    tmp_path: Path,
) -> None:
    runbook = _copied_runbook(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    unverified = private / "final_stage_rehearsal.json"
    unverified.write_text("{}", encoding="utf-8")
    unverified.chmod(0o600)
    store = HumanGateStore(private / "state.json", runbook_path=runbook)

    with pytest.raises(HumanGateError, match="FINAL_REHEARSAL_QA_MISSING"):
        store.confirm(GATE_IDS[1], reviewer="审核人", evidence_file=unverified)
    with pytest.raises(HumanGateError, match="FINAL_REHEARSAL_REQUIRES_LOCAL_FILE"):
        store.confirm(
            GATE_IDS[1],
            reviewer="审核人",
            evidence_url="https://example.com/rehearsal.json",
        )

    unverified.unlink()
    rehearsal = _ready_rehearsal(private, runbook)
    store.confirm(GATE_IDS[1], reviewer="审核人", evidence_file=rehearsal)
    (private / "final_stage_rehearsal_qa.json").unlink()
    audit = store.audit()
    assert audit["valid"] is False
    assert audit["issues"] == [f"FINAL_REHEARSAL_QA_MISSING:{GATE_IDS[1]}"]


def test_training_video_gate_requires_current_full_watch_record_and_qa(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    unverified = private / "training_video_review.json"
    unverified.write_text("{}", encoding="utf-8")
    unverified.chmod(0o600)
    store = HumanGateStore(
        private / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )

    with pytest.raises(HumanGateError, match="TRAINING_VIDEO_REVIEW_QA_MISSING"):
        store.confirm(GATE_IDS[0], reviewer="审核人", evidence_file=unverified)
    with pytest.raises(
        HumanGateError, match="TRAINING_VIDEO_REVIEW_REQUIRES_LOCAL_FILE"
    ):
        store.confirm(
            GATE_IDS[0],
            reviewer="审核人",
            evidence_url="https://example.com/video-review.json",
        )

    unverified.unlink()
    review = _ready_training_video_review(private)
    store.confirm(GATE_IDS[0], reviewer="审核人", evidence_file=review)
    (private / "training_video_review_qa.json").unlink()
    audit = store.audit()
    assert audit["valid"] is False
    assert audit["issues"] == [f"TRAINING_VIDEO_REVIEW_QA_MISSING:{GATE_IDS[0]}"]


def test_official_rules_gate_requires_current_private_review_and_qa(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    unverified = private / "official_rules_review.json"
    unverified.write_text("{}", encoding="utf-8")
    unverified.chmod(0o600)
    store = HumanGateStore(
        private / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )

    with pytest.raises(HumanGateError, match="OFFICIAL_RULES_REVIEW_QA_MISSING"):
        store.confirm(GATE_IDS[4], reviewer="审核人", evidence_file=unverified)
    with pytest.raises(
        HumanGateError, match="OFFICIAL_RULES_REVIEW_REQUIRES_LOCAL_FILE"
    ):
        store.confirm(
            GATE_IDS[4],
            reviewer="审核人",
            evidence_url="https://example.com/official-rules",
        )

    unverified.unlink()
    review = _ready_official_rules_review(private)
    store.confirm(GATE_IDS[4], reviewer="审核人", evidence_file=review)
    (private / "official_rules_review_qa.json").unlink()
    audit = store.audit()
    assert audit["valid"] is False
    assert audit["issues"] == [
        f"OFFICIAL_RULES_REVIEW_QA_MISSING:{GATE_IDS[4]}"
    ]


def test_final_recording_confirmation_tracks_qa_and_policy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "private"
    recording = _ready_recording(private)
    store = HumanGateStore(
        private / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )
    store.confirm(GATE_IDS[2], reviewer="审核人", evidence_file=recording)

    (private / "final_recording_qa.json").unlink()
    missing = store.audit()
    assert missing["valid"] is False
    assert missing["issues"] == [
        f"FINAL_RECORDING_QA_MISSING:{GATE_IDS[2]}"
    ]

    _ready_recording(private)
    changed_policy = tmp_path / "changed-policy.json"
    changed_policy.write_text("changed internal policy", encoding="utf-8")
    monkeypatch.setattr(final_recording_module, "DEFAULT_POLICY", changed_policy)
    changed = store.audit()
    assert changed["valid"] is False
    assert changed["issues"] == [
        f"FINAL_RECORDING_QA_POLICY_CHANGED:{GATE_IDS[2]}"
    ]


def test_human_gate_script_is_executable() -> None:
    script = ROOT / "scripts/manage_human_gates.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
