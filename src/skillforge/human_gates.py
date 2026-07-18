"""Record explicit, private and evidence-bound human gate confirmations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import validate_document
from .demo import ROOT
from .final_recording import final_recording_qa_issue
from .final_rehearsal import final_rehearsal_qa_issue
from .team_roster import TeamRosterError, verify_team_roster
from .training_video_review import training_video_review_qa_issue


DEFAULT_RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
DEFAULT_STORE = ROOT / "outputs/submission/human_gate_confirmations.json"


class HumanGateError(ValueError):
    """Raised when a private confirmation cannot be trusted or recorded."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HumanGateError("人工门禁确认记录无法读取或不是合法JSON") from exc
    if not isinstance(payload, dict):
        raise HumanGateError("人工门禁确认记录必须是JSON对象")
    return payload


def _clean_text(value: str, label: str, *, required: bool, limit: int) -> str:
    normalized = " ".join(value.split())
    if required and not normalized:
        raise HumanGateError(f"{label}不能为空")
    if len(normalized) > limit:
        raise HumanGateError(f"{label}不能超过{limit}个字符")
    return normalized


def _safe_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HumanGateError("证据网址必须是无账号、查询参数和片段的HTTPS地址")
    return value


def _evidence_from_file(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise HumanGateError("证据文件不存在或不是普通文件")
    size = resolved.stat().st_size
    if size < 1:
        raise HumanGateError("证据文件不能为空")
    return {
        "kind": "LOCAL_FILE",
        "locator": str(resolved),
        "sha256": _sha256(resolved),
        "size_bytes": size,
    }


def _evidence_from_url(url: str) -> dict[str, Any]:
    return {
        "kind": "HTTPS_URL",
        "locator": _safe_https_url(url),
        "sha256": None,
        "size_bytes": None,
    }


class HumanGateStore:
    """Git-ignored confirmation store bound to the exact runbook and evidence."""

    def __init__(
        self,
        path: Path = DEFAULT_STORE,
        *,
        runbook_path: Path = DEFAULT_RUNBOOK,
        final_recording_qa_path: Path | None = None,
        final_rehearsal_qa_path: Path | None = None,
        team_roster_path: Path | None = None,
        training_video_review_qa_path: Path | None = None,
        training_video_manifest_path: Path | None = None,
        training_video_path: Path | None = None,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.runbook_path = runbook_path.expanduser().resolve()
        self.final_recording_qa_path = (
            final_recording_qa_path.expanduser().resolve()
            if final_recording_qa_path is not None
            else self.path.parent / "final_recording_qa.json"
        )
        self.final_rehearsal_qa_path = (
            final_rehearsal_qa_path.expanduser().resolve()
            if final_rehearsal_qa_path is not None
            else self.path.parent / "final_stage_rehearsal_qa.json"
        )
        self.team_roster_path = (
            team_roster_path.expanduser().resolve()
            if team_roster_path is not None
            else self.path.parent / "team_roster.json"
        )
        self.training_video_review_qa_path = (
            training_video_review_qa_path.expanduser().resolve()
            if training_video_review_qa_path is not None
            else self.path.parent / "training_video_review_qa.json"
        )
        self.training_video_manifest_path = (
            training_video_manifest_path.expanduser().resolve()
            if training_video_manifest_path is not None
            else ROOT / "output/video/n31_training_video_manifest_v1.json"
        )
        self.training_video_path = (
            training_video_path.expanduser().resolve()
            if training_video_path is not None
            else ROOT / "output/video/n31_training_video_v1.mp4"
        )

    def _runbook(self) -> tuple[dict[str, Any], str]:
        if not self.runbook_path.is_file():
            raise HumanGateError("路演运行单不存在")
        runbook = validate_document(
            _read_json(self.runbook_path),
            "pitch_runbook.schema.json",
        )
        return runbook, _sha256(self.runbook_path)

    @staticmethod
    def _new_document(runbook_sha256: str) -> dict[str, Any]:
        return {
            "version": 1,
            "case_id": "n31_media_change",
            "runbook_sha256": runbook_sha256,
            "updated_at": _now(),
            "confirmations": [],
            "history": [],
            "data_policy": {
                "private_local_state": True,
                "contains_evidence_content": False,
                "contains_credentials": False,
            },
        }

    def _load(self) -> dict[str, Any]:
        document = _read_json(self.path)
        try:
            validate_document(document, "human_gate_confirmations.schema.json")
        except ValueError as exc:
            raise HumanGateError("人工门禁确认记录不符合严格Schema") from exc
        ids = [item["gate_id"] for item in document["confirmations"]]
        if len(ids) != len(set(ids)):
            raise HumanGateError("同一人工门禁出现重复确认")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        validate_document(document, "human_gate_confirmations.schema.json")
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed or self.path == DEFAULT_STORE.resolve():
            os.chmod(self.path.parent, 0o700)
        elif stat.S_IMODE(self.path.parent.stat().st_mode) != 0o700:
            raise HumanGateError("自定义人工门禁存储目录权限必须为700")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _security_issue(self) -> str | None:
        if not self.path.is_file():
            return None
        if stat.S_IMODE(self.path.stat().st_mode) != 0o600:
            return "STORE_MODE_NOT_600"
        if stat.S_IMODE(self.path.parent.stat().st_mode) != 0o700:
            return "STORE_DIRECTORY_MODE_NOT_700"
        return None

    @staticmethod
    def _evidence_issue(evidence: dict[str, Any]) -> str | None:
        if evidence["kind"] == "HTTPS_URL":
            try:
                _safe_https_url(evidence["locator"])
            except HumanGateError:
                return "UNSAFE_EVIDENCE_URL"
            return None
        path = Path(evidence["locator"])
        if not path.is_file():
            return "EVIDENCE_FILE_MISSING"
        if path.stat().st_size != evidence["size_bytes"]:
            return "EVIDENCE_SIZE_CHANGED"
        if _sha256(path) != evidence["sha256"]:
            return "EVIDENCE_HASH_CHANGED"
        return None

    def _gate_evidence_issue(
        self,
        gate_id: str,
        evidence: dict[str, Any],
    ) -> str | None:
        if gate_id == "TRAINING_VIDEO_FULL_WATCH":
            return training_video_review_qa_issue(
                self.training_video_review_qa_path,
                evidence,
                manifest_path=self.training_video_manifest_path,
                video_path=self.training_video_path,
            )
        if gate_id == "FINAL_STAGE_REHEARSAL":
            return final_rehearsal_qa_issue(
                self.final_rehearsal_qa_path,
                evidence,
                runbook_path=self.runbook_path,
            )
        if gate_id == "FINAL_RECORDING_REVIEW":
            return final_recording_qa_issue(self.final_recording_qa_path, evidence)
        return None

    def _team_roster_report(self) -> dict[str, Any]:
        try:
            return verify_team_roster(
                self.team_roster_path,
                private_root=self.team_roster_path.parent,
            )
        except (OSError, TeamRosterError, ValueError) as exc:
            raise HumanGateError("团队资格确认需要先通过私有团队名单QA") from exc

    def _gate_context_for_confirmation(self, gate_id: str) -> dict[str, Any] | None:
        if gate_id != "TEAM_ELIGIBILITY_CONFIRMED":
            return None
        report = self._team_roster_report()
        return {
            "kind": "TEAM_ROSTER",
            "roster_sha256": report["roster_sha256"],
            "qa_status": report["status"],
        }

    def _gate_context_issue(
        self,
        gate_id: str,
        context: dict[str, Any] | None,
    ) -> str | None:
        if gate_id != "TEAM_ELIGIBILITY_CONFIRMED":
            return "UNEXPECTED_GATE_CONTEXT" if context is not None else None
        if not context or context.get("kind") != "TEAM_ROSTER":
            return "TEAM_ROSTER_BINDING_MISSING"
        try:
            report = self._team_roster_report()
        except HumanGateError:
            return "TEAM_ROSTER_QA_INVALID"
        if context.get("qa_status") != report["status"]:
            return "TEAM_ROSTER_QA_STATUS_CHANGED"
        if context.get("roster_sha256") != report["roster_sha256"]:
            return "TEAM_ROSTER_HASH_CHANGED"
        return None

    def audit(self) -> dict[str, Any]:
        runbook, runbook_sha256 = self._runbook()
        gates = {item["gate_id"]: item for item in runbook["human_gates"]}
        if not self.path.exists():
            effective = {
                gate_id for gate_id, item in gates.items() if item["status"] == "PASSED"
            }
            return self._audit_result(
                gates,
                effective,
                state="ABSENT",
                valid=True,
                issues=[],
            )

        issues: list[str] = []
        security_issue = self._security_issue()
        if security_issue:
            issues.append(security_issue)
        try:
            document = self._load()
        except HumanGateError as exc:
            return self._audit_result(
                gates,
                set(),
                state="INVALID",
                valid=False,
                issues=[*issues, str(exc)],
            )
        if document["runbook_sha256"] != runbook_sha256:
            issues.append("RUNBOOK_HASH_CHANGED")
            return self._audit_result(
                gates,
                {
                    gate_id
                    for gate_id, item in gates.items()
                    if item["status"] == "PASSED"
                },
                state="STALE",
                valid=False,
                issues=issues,
            )

        effective = {
            gate_id for gate_id, item in gates.items() if item["status"] == "PASSED"
        }
        for item in document["confirmations"]:
            gate = gates.get(item["gate_id"])
            if gate is None:
                issues.append(f"UNKNOWN_GATE:{item['gate_id']}")
                continue
            if item["gate_label"] != gate["label"]:
                issues.append(f"GATE_LABEL_CHANGED:{item['gate_id']}")
                continue
            evidence_issue = self._evidence_issue(item["evidence"])
            if evidence_issue:
                issues.append(f"{evidence_issue}:{item['gate_id']}")
                continue
            gate_evidence_issue = self._gate_evidence_issue(
                item["gate_id"], item["evidence"]
            )
            if gate_evidence_issue:
                issues.append(f"{gate_evidence_issue}:{item['gate_id']}")
                continue
            gate_context_issue = self._gate_context_issue(
                item["gate_id"], item.get("gate_context")
            )
            if gate_context_issue:
                issues.append(f"{gate_context_issue}:{item['gate_id']}")
                continue
            effective.add(item["gate_id"])
        return self._audit_result(
            gates,
            effective,
            state="VALID" if not issues else "INVALID",
            valid=not issues,
            issues=issues,
        )

    @staticmethod
    def _audit_result(
        gates: dict[str, dict[str, Any]],
        effective: set[str],
        *,
        state: str,
        valid: bool,
        issues: list[str],
    ) -> dict[str, Any]:
        gate_items = [
            {
                "gate_id": gate_id,
                "label": gate["label"],
                "status": "PASSED" if gate_id in effective else "PENDING",
            }
            for gate_id, gate in gates.items()
        ]
        return {
            "case_id": "n31_media_change",
            "store_state": state,
            "valid": valid,
            "confirmed_gate_ids": sorted(effective),
            "pending_gate_ids": [
                item["gate_id"] for item in gate_items if item["status"] == "PENDING"
            ],
            "issues": issues,
            "gates": gate_items,
            "summary": {
                "passed": len(effective),
                "pending": len(gates) - len(effective),
                "total": len(gates),
            },
        }

    def confirm(
        self,
        gate_id: str,
        *,
        reviewer: str,
        evidence_file: Path | None = None,
        evidence_url: str | None = None,
        note: str = "",
        replace: bool = False,
    ) -> dict[str, Any]:
        if (evidence_file is None) == (evidence_url is None):
            raise HumanGateError("必须且只能提供一种证据：本地文件或HTTPS网址")
        reviewer = _clean_text(reviewer, "确认人", required=True, limit=200)
        note = _clean_text(note, "说明", required=False, limit=1000)
        runbook, runbook_sha256 = self._runbook()
        gates = {item["gate_id"]: item for item in runbook["human_gates"]}
        gate = gates.get(gate_id)
        if gate is None:
            raise HumanGateError("未知人工门禁")
        if gate["status"] == "PASSED":
            raise HumanGateError("该门禁已在受控运行单中通过，无需重复确认")
        evidence = (
            _evidence_from_file(evidence_file)
            if evidence_file is not None
            else _evidence_from_url(evidence_url or "")
        )
        gate_evidence_issue = self._gate_evidence_issue(gate_id, evidence)
        if gate_evidence_issue:
            raise HumanGateError(
                f"人工门禁证据未满足专用QA要求：{gate_evidence_issue}"
            )
        gate_context = self._gate_context_for_confirmation(gate_id)
        if self.path.exists():
            if self._security_issue():
                raise HumanGateError("人工门禁确认记录权限不安全，拒绝写入")
            document = self._load()
            if document["runbook_sha256"] != runbook_sha256:
                raise HumanGateError("运行单已变化；请先显式重置过期确认")
        else:
            document = self._new_document(runbook_sha256)
        existing = next(
            (item for item in document["confirmations"] if item["gate_id"] == gate_id),
            None,
        )
        if existing and not replace:
            raise HumanGateError("该门禁已有确认；如需更新请显式使用--replace")
        confirmed_at = _now()
        confirmation = {
            "gate_id": gate_id,
            "gate_label": gate["label"],
            "reviewer": reviewer,
            "confirmed_at": confirmed_at,
            "note": note,
            "evidence": evidence,
            "gate_context": gate_context,
        }
        document["confirmations"] = [
            item for item in document["confirmations"] if item["gate_id"] != gate_id
        ]
        document["confirmations"].append(confirmation)
        order = {item["gate_id"]: index for index, item in enumerate(runbook["human_gates"])}
        document["confirmations"].sort(key=lambda item: order[item["gate_id"]])
        document["history"].append(
            {
                "action": "CONFIRMED",
                "gate_id": gate_id,
                "reviewer": reviewer,
                "occurred_at": confirmed_at,
                "note": note,
                "evidence_sha256": evidence["sha256"],
            }
        )
        document["updated_at"] = confirmed_at
        self._write(document)
        return self.audit()

    def revoke(self, gate_id: str, *, reviewer: str, note: str) -> dict[str, Any]:
        reviewer = _clean_text(reviewer, "撤销人", required=True, limit=200)
        note = _clean_text(note, "撤销原因", required=True, limit=1000)
        _, runbook_sha256 = self._runbook()
        if not self.path.is_file():
            raise HumanGateError("没有可撤销的人工确认")
        if self._security_issue():
            raise HumanGateError("人工门禁确认记录权限不安全，拒绝写入")
        document = self._load()
        if document["runbook_sha256"] != runbook_sha256:
            raise HumanGateError("运行单已变化；请先显式重置过期确认")
        selected = next(
            (item for item in document["confirmations"] if item["gate_id"] == gate_id),
            None,
        )
        if selected is None:
            raise HumanGateError("该门禁没有私有确认")
        occurred_at = _now()
        document["confirmations"] = [
            item for item in document["confirmations"] if item["gate_id"] != gate_id
        ]
        document["history"].append(
            {
                "action": "REVOKED",
                "gate_id": gate_id,
                "reviewer": reviewer,
                "occurred_at": occurred_at,
                "note": note,
                "evidence_sha256": selected["evidence"]["sha256"],
            }
        )
        document["updated_at"] = occurred_at
        self._write(document)
        return self.audit()

    def reset_stale(self, *, reviewer: str, note: str) -> dict[str, Any]:
        reviewer = _clean_text(reviewer, "重置人", required=True, limit=200)
        note = _clean_text(note, "重置原因", required=True, limit=1000)
        _, runbook_sha256 = self._runbook()
        if not self.path.is_file():
            raise HumanGateError("没有可重置的人工确认记录")
        document = self._load()
        if document["runbook_sha256"] == runbook_sha256:
            raise HumanGateError("确认记录尚未过期，无需重置")
        occurred_at = _now()
        document["runbook_sha256"] = runbook_sha256
        document["confirmations"] = []
        document["history"].append(
            {
                "action": "RESET_STALE",
                "gate_id": "ALL_GATES",
                "reviewer": reviewer,
                "occurred_at": occurred_at,
                "note": note,
                "evidence_sha256": None,
            }
        )
        document["updated_at"] = occurred_at
        self._write(document)
        return self.audit()


def _print_status(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="只显示安全摘要，不显示确认人或证据路径")

    confirm = subparsers.add_parser("confirm", help="显式确认一个人工门禁")
    confirm.add_argument("--gate", required=True)
    confirm.add_argument("--reviewer", required=True)
    evidence = confirm.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--evidence-file", type=Path)
    evidence.add_argument("--evidence-url")
    confirm.add_argument("--note", default="")
    confirm.add_argument("--replace", action="store_true")

    revoke = subparsers.add_parser("revoke", help="撤销一个私有人工确认")
    revoke.add_argument("--gate", required=True)
    revoke.add_argument("--reviewer", required=True)
    revoke.add_argument("--note", required=True)

    reset = subparsers.add_parser("reset-stale", help="运行单变化后显式清空过期确认")
    reset.add_argument("--reviewer", required=True)
    reset.add_argument("--note", required=True)

    args = parser.parse_args()
    store = HumanGateStore(args.store, runbook_path=args.runbook)
    try:
        if args.command == "status":
            result = store.audit()
        elif args.command == "confirm":
            result = store.confirm(
                args.gate,
                reviewer=args.reviewer,
                evidence_file=args.evidence_file,
                evidence_url=args.evidence_url,
                note=args.note,
                replace=args.replace,
            )
        elif args.command == "revoke":
            result = store.revoke(
                args.gate,
                reviewer=args.reviewer,
                note=args.note,
            )
        else:
            result = store.reset_stale(reviewer=args.reviewer, note=args.note)
    except HumanGateError as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False))
        return 1
    _print_status(result)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
