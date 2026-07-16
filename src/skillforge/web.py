"""Native FastAPI demo for uploads and the evidence-revision comparison."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .demo import ROOT, run_demo
from .gold_rehearsal import run_gold_rehearsal
from .ingest import IngestionPipeline


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
ALLOWED_SUFFIXES = {
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi"},
    "pdf": {".pdf"},
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"},
}
N31_DOWNLOADS = {
    "final-sop": ("active", "after_sop.json", "n31_final_sop.json"),
    "checklist": ("active", "checklist.json", "n31_mobile_checklist.json"),
    "quiz": ("active", "quiz.json", "n31_training_quiz.json"),
    "revision-audit": (
        "active",
        "revision_audit.json",
        "n31_revision_audit.json",
    ),
    "poster": (
        "project",
        "output/pdf/n31_a4_training_poster.pdf",
        "n31_a4_training_poster.pdf",
    ),
}


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SkillForge 匠传</title>
  <style>
    :root{color-scheme:dark;--bg:#07110d;--panel:#102019;--line:#294235;--text:#eef7f0;--muted:#a6b9ac;--green:#73e2a7;--amber:#ffc766;--red:#ff7b7b}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#163327 0,transparent 38%),var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif}
    main{max-width:1180px;margin:auto;padding:34px 20px 70px}h1{font-size:38px;margin:0 0 4px}h2{font-size:20px;margin:0 0 16px}p{color:var(--muted)}.tag{color:var(--green);letter-spacing:.14em;text-transform:uppercase;font-weight:700}.panel{background:color-mix(in srgb,var(--panel) 92%,transparent);border:1px solid var(--line);border-radius:18px;padding:20px;margin-top:18px;box-shadow:0 16px 50px #0004}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric{padding:15px;border:1px solid var(--line);border-radius:14px;background:#0a1712}.metric strong{display:block;font-size:28px;color:var(--green)}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}.step,.issue,.change,.result{border:1px solid var(--line);border-radius:12px;padding:13px;margin:9px 0;background:#0b1813}.issue{border-left:4px solid var(--red)}.change,.result{border-left:4px solid var(--green)}.evidence{color:var(--amber);font-size:13px;margin-top:8px}.muted{color:var(--muted)}.notice{padding:10px 12px;border:1px solid var(--amber);border-radius:10px;color:var(--amber);background:#251d0b;margin-bottom:14px}.downloads{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}.download{display:inline-block;border:1px solid var(--green);border-radius:9px;padding:8px 12px;color:var(--green);text-decoration:none;font-weight:700}button{border:0;border-radius:10px;padding:11px 16px;background:var(--green);color:#062011;font-weight:800;cursor:pointer}input{width:100%;margin:6px 0 12px;padding:9px;border:1px solid var(--line);border-radius:8px;background:#08130f;color:var(--text)}label{display:block;color:var(--muted)}#status{margin-left:10px;color:var(--amber)}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted)}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:800px){.grid,.cols{grid-template-columns:1fr}h1{font-size:30px}}
  </style>
</head>
<body><main>
  <div class="tag">DGX native pipeline · no Docker required</div>
  <h1>匠传 SkillForge</h1>
  <p>从素材证据到 SOP，再到发现问题、引用证据和局部自动修订。</p>
  <section class="panel"><h2 id="metrics-title">闭环指标</h2><div id="basis" class="notice"></div><div id="metrics" class="grid"></div></section>
  <section class="panel"><h2>现场演示控制</h2><button id="rerun">重新运行 Gold 质检与局部修订</button><span id="rerun-status"></span><p>只处理已审核的结构化证据，不发送原始素材，也不调用外部模型。</p></section>
  <section class="panel" id="multisource-panel" hidden><h2>为什么需要多源证据</h2><div id="source-metrics" class="grid"></div><p id="visual-note"></p></section>
  <section class="panel"><h2>发现问题 → 展示证据</h2><div id="issues"></div></section>
  <section class="panel"><h2>修订前后对比</h2><div class="cols"><div><h3>修订前</h3><div id="before"></div></div><div><h3>修订后</h3><div id="after"></div></div></div></section>
  <section class="panel"><h2>局部修订审计</h2><div id="changes"></div></section>
  <section class="panel" id="results-panel" hidden><h2>培训成果</h2><div class="downloads" id="n31-downloads"><a class="download" href="/api/n31/artifacts/final-sop">下载最终 SOP</a><a class="download" href="/api/n31/artifacts/checklist">下载手机检查清单</a><a class="download" href="/api/n31/artifacts/quiz">下载培训测验</a><a class="download" href="/api/n31/artifacts/poster">下载 A4 培训海报</a><a class="download" href="/api/n31/artifacts/revision-audit">下载修订记录</a></div><div class="cols"><div><h3>手机端检查清单</h3><div id="checklist"></div></div><div><h3>培训测验</h3><div id="quiz"></div></div></div></section>
  <section class="panel"><h2>上传素材并原生预处理</h2><p>上传内容只写入被 Git 忽略的本地输出目录。本页面不会自动把原始素材发送给外部模型。</p>
    <form id="upload"><label>操作视频<input type="file" name="video" accept="video/*"></label><label>设备 PDF<input type="file" name="pdf" accept="application/pdf"></label><label>专家录音<input type="file" name="audio" accept="audio/*"></label><label><input style="width:auto" type="checkbox" name="transcribe" value="true">调用 StepAudio ASR</label><label><input style="width:auto" type="checkbox" name="analyze_visuals" value="true">调用 Step 3.7 分析关键帧</label><label><input style="width:auto" type="checkbox" name="plan_sop" value="true">根据证据规划 SOP</label><label><input style="width:auto" type="checkbox" name="external_processing_authorized" value="true">已确认允许把选定派生内容发送给外部 API</label><button>开始处理</button><span id="status"></span></form><pre id="ingest"></pre>
  </section>
</main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>`${(Number(v)*100).toFixed(0)}%`;
function renderDemo(d){const b=d.summary.before,a=d.summary.after,isReal=d.summary.synthetic===false,isGold=d.summary.gold_status==='GOLD';
document.querySelector('#metrics-title').textContent=isGold?'N31 真实素材 Gold 闭环':isReal?'N31 真实素材闭环彩排':'无版权模拟闭环';
document.querySelector('#basis').textContent=isGold?'实际操作者口述审核 · Gold v1 · 最终评测指标。':isReal?'候选基准 · 非 Gold · 指标仅用于证明闭环可运行，等待操作者审核后重跑最终评测。':'明确标注的无版权模拟数据，不作为真实案例评测。';
document.querySelector('#metrics').innerHTML=[['必要步骤',`${pct(b.required_step_coverage)} → ${pct(a.required_step_coverage)}`],['证据覆盖',`${pct(b.evidence_supported_required_steps)} → ${pct(a.evidence_supported_required_steps)}`],['严重错误',`${b.severe_error_count} → ${a.severe_error_count}`],['局部修改',d.summary.revision_count],['状态',isReal?d.summary.gold_status||'NOT_GOLD':d.summary.workflow_state]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');
if(d.multisource_comparison&&d.visual_review){const s=d.multisource_comparison.source_ablation,v=d.visual_review.summary,p=d.multisource_comparison.privacy_comparison;document.querySelector('#multisource-panel').hidden=false;document.querySelector('#source-metrics').innerHTML=[['手册单源',pct(s.manual_only.coverage)],['专家口述单源',pct(s.expert_audio_only.coverage)],['两种以上来源',pct(s.two_or_more_source_types.coverage)],['视频部分可观察',pct(s.video_observable_partial_or_better.coverage)],['视觉矛盾',v.contradicted_count]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#visual-note').textContent=`严格视觉复核：${v.supported_count}步完整支持、${v.partial_count}步部分可见、${v.not_visible_count}步不可见、${v.contradicted_count}步矛盾。模型标记${p.model_flagged_step_count}步需隐私复核；本地安全派生QA为${p.local_safe_derivative_qa}，标记保留但不自动推翻人工检查。`}
document.querySelector('#issues').innerHTML=d.initial_conflicts.conflicts.map(c=>`<div class="issue"><b>${esc(c.kind)}</b> · ${esc(c.message)}<div class="evidence">${c.evidence.map(e=>`${esc(e.evidence_id)}｜${esc(e.source_ref)}｜${esc(JSON.stringify(e.locator))}`).join('<br>')||'无来源内容：按规则拒绝'}</div></div>`).join('');
const render=s=>s.steps.map(x=>`<div class="step"><b>${esc(x.step_id)} ${esc(x.title)}</b><div class="muted">${esc(x.action)}</div><div class="evidence">证据：${esc(x.evidence.join(', ')||'无')}</div></div>`).join('');document.querySelector('#before').innerHTML=render(d.before_sop);document.querySelector('#after').innerHTML=render(d.after_sop);
document.querySelector('#changes').innerHTML=d.revision_audit.changes.map(c=>`<div class="change"><b>${esc(c.action)} · ${esc(c.path)}</b><div>${esc(c.reason)}</div><div class="evidence">证据：${esc(c.evidence_ids.join(', ')||'无依据，已删除')}</div></div>`).join('');
if(d.checklist&&d.quiz){document.querySelector('#results-panel').hidden=false;document.querySelector('#n31-downloads').hidden=d.summary.synthetic!==false;document.querySelector('#checklist').innerHTML=d.checklist.items.map(x=>`<div class="result"><b>□ ${esc(x.step_id)} ${esc(x.title)}</b><div>${esc(x.check)}</div><div class="evidence">证据：${esc(x.evidence_ids.join(', '))}</div></div>`).join('');document.querySelector('#quiz').innerHTML=d.quiz.questions.map(x=>`<div class="result"><b>${esc(x.question_id)} ${esc(x.prompt)}</b><div>答案：${esc(x.answer===true?'正确':x.answer===false?'错误':x.answer)}</div><div class="muted">${esc(x.explanation)}</div><div class="evidence">证据：${esc(x.evidence_ids.join(', '))}</div></div>`).join('')}}
async function loadDemo(){let r=await fetch('/api/n31');if(r.ok){renderDemo(await r.json());return}r=await fetch('/api/demo');if(!r.ok){await fetch('/api/demo/run',{method:'POST'});r=await fetch('/api/demo')}renderDemo(await r.json())}
document.querySelector('#rerun').addEventListener('click',async()=>{const s=document.querySelector('#rerun-status');s.textContent=' 运行中…';const r=await fetch('/api/n31/run',{method:'POST'});const d=await r.json();s.textContent=r.ok?` 完成：严重错误 ${d.before.severe_error_count} → ${d.after.severe_error_count}`:` 失败：${d.detail||'未知错误'}`;if(r.ok)await loadDemo()});
document.querySelector('#upload').addEventListener('submit',async e=>{e.preventDefault();const status=document.querySelector('#status');status.textContent='处理中…';const r=await fetch('/api/ingest',{method:'POST',body:new FormData(e.target)});const d=await r.json();status.textContent=r.ok?'完成':'失败';document.querySelector('#ingest').textContent=JSON.stringify(d,null,2)});loadDemo();
</script></body></html>"""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _demo_payload(directory: Path) -> dict[str, Any]:
    names = [
        "summary",
        "before_sop",
        "after_sop",
        "initial_conflicts",
        "final_conflicts",
        "revision_audit",
    ]
    missing = [name for name in names if not (directory / f"{name}.json").is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    payload = {name: _read_json(directory / f"{name}.json") for name in names}
    for name in ("checklist", "quiz", "workflow"):
        path = directory / f"{name}.json"
        if path.is_file():
            payload[name] = _read_json(path)
    return payload


async def _save_upload(upload: UploadFile, path: Path) -> None:
    total = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="单个文件不能超过 512 MiB")
            handle.write(chunk)
    await upload.close()


def _suffix(kind: str, upload: UploadFile) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES[kind]:
        raise HTTPException(status_code=400, detail=f"不支持的 {kind} 文件类型")
    return suffix


def create_app(
    output_root: Path | None = None,
    n31_rehearsal_dir: Path | None = None,
) -> FastAPI:
    root = (output_root or ROOT / "outputs").resolve()
    root.mkdir(parents=True, exist_ok=True)
    demo_dir = root / "demo_run"
    if n31_rehearsal_dir:
        n31_dir = n31_rehearsal_dir.resolve()
    else:
        gold_dir = ROOT / "cases" / "n31" / "output" / "gold_rehearsal_v1"
        bundle_dir = ROOT / "cases" / "n31" / "demo_bundle"
        provisional_dir = ROOT / "cases" / "n31" / "output" / "rehearsal_v1"
        configured_dir = os.getenv("SKILLFORGE_N31_DIR")
        if configured_dir:
            n31_dir = Path(configured_dir).expanduser().resolve()
        elif (gold_dir / "summary.json").is_file():
            n31_dir = gold_dir.resolve()
        elif (bundle_dir / "summary.json").is_file():
            n31_dir = bundle_dir.resolve()
        else:
            n31_dir = provisional_dir.resolve()
    active_n31_dir = {"path": n31_dir}
    app = FastAPI(title="SkillForge", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return HTML

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "runtime": "native-python",
            "docker_required": False,
            "n31_rehearsal_available": (
                active_n31_dir["path"] / "summary.json"
            ).is_file(),
        }

    @app.get("/api/n31")
    def n31_data() -> JSONResponse:
        try:
            payload = _demo_payload(active_n31_dir["path"])
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="N31彩排结果尚未生成") from exc
        summary = payload["summary"]
        status_pair = (
            summary.get("gold_status"),
            summary.get("metrics_status"),
        )
        if summary.get("synthetic") is not False or status_pair not in {
            ("NOT_GOLD", "PROVISIONAL_ONLY"),
            ("GOLD", "FINAL"),
        }:
            raise HTTPException(status_code=409, detail="N31彩排标记不完整")
        if status_pair == ("GOLD", "FINAL"):
            evaluation_dir = ROOT / "cases" / "n31" / "evaluations"
            visual_path = evaluation_dir / "visual_sequence_review_v1.json"
            comparison_path = evaluation_dir / "multisource_comparison_v1.json"
            if visual_path.is_file() and comparison_path.is_file():
                payload["visual_review"] = _read_json(visual_path)
                payload["multisource_comparison"] = _read_json(comparison_path)
        return JSONResponse(payload)

    @app.post("/api/n31/run")
    def execute_n31_gold() -> dict[str, Any]:
        case = ROOT / "cases" / "n31" / "gold"
        live_dir = root / "n31_live_run"
        try:
            summary = run_gold_rehearsal(
                case / "gold_sop.json",
                case / "constraints.json",
                case / "fault_injection.json",
                live_dir,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"N31 Gold现场运行失败: {str(exc)[:300]}",
            ) from exc
        active_n31_dir["path"] = live_dir
        return summary

    @app.get("/api/n31/artifacts/{artifact_name}")
    def download_n31_artifact(artifact_name: str) -> FileResponse:
        selected = N31_DOWNLOADS.get(artifact_name)
        if selected is None:
            raise HTTPException(status_code=404)
        scope, source_name, download_name = selected
        base = ROOT if scope == "project" else active_n31_dir["path"]
        path = base / source_name
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type=(
                "application/pdf"
                if path.suffix.lower() == ".pdf"
                else "application/json"
            ),
            filename=download_name,
        )

    @app.get("/api/demo")
    def demo_data() -> JSONResponse:
        try:
            return JSONResponse(_demo_payload(demo_dir))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="模拟结果尚未生成") from exc

    @app.post("/api/demo/run")
    def execute_demo() -> dict[str, Any]:
        return run_demo(ROOT / "cases" / "demo_case" / "synthetic", demo_dir)

    @app.post("/api/ingest")
    async def ingest_uploads(
        video: Annotated[UploadFile | None, File()] = None,
        pdf: Annotated[UploadFile | None, File()] = None,
        audio: Annotated[UploadFile | None, File()] = None,
        transcribe: Annotated[bool, Form()] = False,
        analyze_visuals: Annotated[bool, Form()] = False,
        plan_sop: Annotated[bool, Form()] = False,
        external_processing_authorized: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        uploads = {"video": video, "pdf": pdf, "audio": audio}
        if not any(uploads.values()):
            raise HTTPException(status_code=400, detail="至少上传一个文件")
        if (transcribe or analyze_visuals or plan_sop) and not external_processing_authorized:
            raise HTTPException(
                status_code=400,
                detail="启用 ASR 前必须明确确认外部处理授权",
            )
        run_id = uuid.uuid4().hex[:12]
        run_dir = root / "web_runs" / run_id
        paths: dict[str, Path] = {}
        for kind, upload in uploads.items():
            if upload is None:
                continue
            path = run_dir / "input" / f"{kind}{_suffix(kind, upload)}"
            await _save_upload(upload, path)
            paths[kind] = path
        pipeline = IngestionPipeline(run_dir / "result")
        manifest = await run_in_threadpool(
            pipeline.run,
            video=paths.get("video"),
            pdf=paths.get("pdf"),
            audio=paths.get("audio"),
            transcribe=transcribe,
            analyze_visuals=analyze_visuals,
            plan_sop=plan_sop,
            external_processing_authorized=external_processing_authorized,
            synthetic=False,
            case_id=f"WEB-{run_id}",
            title="上传素材生成的 SOP 草稿",
        )
        return {"run_id": run_id, "manifest": manifest}

    @app.get("/artifacts/{run_id}/{artifact_path:path}")
    def artifact(run_id: str, artifact_path: str) -> FileResponse:
        if not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise HTTPException(status_code=404)
        candidate = (root / "web_runs" / run_id / "result" / artifact_path).resolve()
        allowed = (root / "web_runs" / run_id / "result").resolve()
        if allowed not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(candidate)
    return app


app = create_app()


def main() -> int:
    host = os.getenv("SKILLFORGE_HOST", "0.0.0.0")
    port_text = os.getenv("SKILLFORGE_PORT", "7860")
    if not re.fullmatch(r"\d{2,5}", port_text):
        raise ValueError("SKILLFORGE_PORT 必须是端口数字")
    uvicorn.run("skillforge.web:app", host=host, port=int(port_text), reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
