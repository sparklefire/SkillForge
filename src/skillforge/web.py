"""Native FastAPI demo for uploads and the evidence-revision comparison."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .demo import ROOT, run_demo
from .checklist_sessions import ChecklistSessionStore
from .contracts import validate_document
from .evidence_locator import build_evidence_locator
from .gold_rehearsal import run_gold_rehearsal
from .ingest import IngestionPipeline
from .review_sessions import SopReviewSessionStore, rebuild_step_artifacts


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
ALLOWED_SUFFIXES = {
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi"},
    "pdf": {".pdf"},
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"},
}
N31_DOWNLOADS = {
    "final-sop": ("active", "after_sop.json", "n31_final_sop.json"),
    "sop-views": ("active", "sop_views.json", "n31_sop_views.json"),
    "checklist": ("active", "checklist.json", "n31_mobile_checklist.json"),
    "checklist-thumbnails": (
        "project",
        "output/checklist_thumbnails/manifest.json",
        "n31_checklist_thumbnail_manifest.json",
    ),
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
    "training-video": (
        "project",
        "output/video/n31_training_video_v1.mp4",
        "n31_training_video_v1.mp4",
    ),
    "training-video-manifest": (
        "project",
        "output/video/n31_training_video_manifest_v1.json",
        "n31_training_video_manifest_v1.json",
    ),
    "training-video-evidence": (
        "project",
        "output/video/n31_training_video_evidence_pack_v1.json",
        "n31_training_video_evidence_pack_v1.json",
    ),
    "temporal-windows": (
        "project",
        "cases/n31/evaluations/temporal_action_windows_v1.json",
        "n31_temporal_action_windows_v1.json",
    ),
    "pdf-structure": (
        "project",
        "cases/n31/evaluations/pdf_structure_v1.json",
        "n31_pdf_structure_v1.json",
    ),
    "source-candidates": (
        "project",
        "cases/n31/evaluations/source_candidate_synthesis_v1.json",
        "n31_source_candidate_synthesis_v1.json",
    ),
    "grounding-gate": (
        "project",
        "cases/n31/evaluations/deterministic_grounding_gate_v1.json",
        "n31_deterministic_grounding_gate_v1.json",
    ),
    "semantic-review": (
        "project",
        "cases/n31/evaluations/semantic_review_v1.json",
        "n31_semantic_review_v1.json",
    ),
    "selective-rebuild": (
        "project",
        "cases/n31/evaluations/selective_rebuild_v1.json",
        "n31_selective_rebuild_v1.json",
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
    main{max-width:1180px;margin:auto;padding:34px 20px 70px}h1{font-size:38px;margin:0 0 4px}h2{font-size:20px;margin:0 0 16px}p{color:var(--muted)}.tag{color:var(--green);letter-spacing:.14em;text-transform:uppercase;font-weight:700}.panel{background:color-mix(in srgb,var(--panel) 92%,transparent);border:1px solid var(--line);border-radius:18px;padding:20px;margin-top:18px;box-shadow:0 16px 50px #0004}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric{padding:15px;border:1px solid var(--line);border-radius:14px;background:#0a1712}.metric strong{display:block;font-size:28px;color:var(--green)}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}.step,.issue,.change,.result{border:1px solid var(--line);border-radius:12px;padding:13px;margin:9px 0;background:#0b1813}.issue{border-left:4px solid var(--red)}.change,.result{border-left:4px solid var(--green)}.evidence{color:var(--amber);font-size:13px;margin-top:8px}.muted{color:var(--muted)}.notice{padding:10px 12px;border:1px solid var(--amber);border-radius:10px;color:var(--amber);background:#251d0b;margin-bottom:14px}.downloads,.controls{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}.download{display:inline-block;border:1px solid var(--green);border-radius:9px;padding:8px 12px;color:var(--green);text-decoration:none;font-weight:700}button{border:0;border-radius:10px;padding:11px 16px;background:var(--green);color:#062011;font-weight:800;cursor:pointer}button.secondary{background:#1b3328;color:var(--text);border:1px solid var(--line)}button:disabled{opacity:.45;cursor:not-allowed}.evidence-link{padding:2px 7px;margin:2px;border:1px solid var(--amber);border-radius:7px;background:#251d0b;color:var(--amber);font-size:12px}.review-step{display:grid;grid-template-columns:44px 1fr auto;gap:10px;align-items:center}.review-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.review-actions button{padding:6px 9px;font-size:12px}input,select,textarea{width:100%;margin:6px 0 12px;padding:9px;border:1px solid var(--line);border-radius:8px;background:#08130f;color:var(--text)}textarea{min-height:72px;resize:vertical}.check-card{min-height:460px}.check-card img,#evidence-detail img{width:100%;max-height:230px;object-fit:contain;background:#050a08;border-radius:10px;margin:10px 0}.warning-list{color:var(--amber)}details{margin-top:10px;color:var(--muted)}summary{cursor:pointer;color:var(--amber)}label{display:block;color:var(--muted)}#status,#checklist-status,#review-status{margin-left:10px;color:var(--amber)}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted)}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:800px){.grid,.cols{grid-template-columns:1fr}.review-step{grid-template-columns:1fr}.review-actions{justify-content:flex-start}h1{font-size:30px}}
  </style>
</head>
<body><main>
  <div class="tag">DGX native pipeline · no Docker required</div>
  <h1>匠传 SkillForge</h1>
  <p>从素材证据到 SOP，再到发现问题、引用证据和局部自动修订。</p>
  <section class="panel"><h2 id="metrics-title">闭环指标</h2><div id="basis" class="notice"></div><div id="metrics" class="grid"></div></section>
  <section class="panel"><h2>现场演示控制</h2><button id="rerun">重新运行 Gold 质检与局部修订</button><span id="rerun-status"></span><p>只处理已审核的结构化证据，不发送原始素材，也不调用外部模型。</p></section>
  <section class="panel" id="workflow-panel" hidden><h2>可恢复工作流检查点</h2><div id="workflow-metrics" class="grid"></div><p id="workflow-note"></p><div id="workflow-events"></div></section>
  <section class="panel" id="dgx-panel" hidden><h2>DGX Spark 本地视觉计算</h2><div id="dgx-metrics" class="grid"></div><div id="agent-trace"></div></section>
  <section class="panel" id="temporal-panel" hidden><h2>连续动作候选窗口</h2><div id="temporal-metrics" class="grid"></div><p id="temporal-note"></p><div id="temporal-windows"></div></section>
  <section class="panel" id="pdf-panel" hidden><h2>手册结构与检索验证</h2><div id="pdf-metrics" class="grid"></div><p id="pdf-note"></p><div id="pdf-queries"></div></section>
  <section class="panel" id="candidate-panel" hidden><h2>三类来源候选 → 规范步骤</h2><div id="candidate-metrics" class="grid"></div><p id="candidate-note"></p><div id="candidate-groups"></div></section>
  <section class="panel" id="multisource-panel" hidden><h2>为什么需要多源证据</h2><div id="source-metrics" class="grid"></div><p id="visual-note"></p></section>
  <section class="panel" id="grounding-panel" hidden><h2>无来源内容拒绝门禁</h2><div id="grounding-metrics" class="grid"></div><p>四个独立篡改场景均执行：发现问题 → 展示当前步骤Evidence边界 → 局部修订 → 重新质检。</p><div id="grounding-scenarios"></div></section>
  <section class="panel" id="semantic-panel" hidden><h2>高推理语义质检</h2><div id="semantic-metrics" class="grid"></div><p id="semantic-note"></p><div id="semantic-assessments"></div></section>
  <section class="panel" id="selective-panel" hidden><h2>受影响范围与选择性重建</h2><div id="selective-metrics" class="grid"></div><p id="selective-note"></p><div id="selective-plans"></div></section>
  <section class="panel" id="review-panel" hidden><h2>操作者 SOP 审核台</h2><p>审核记录只保存在本机忽略目录。锁定后才能确认；安全重排会校验前置依赖和锁定位置；单步重建不调用外部模型。</p><button id="review-start">开始新的审核会话</button><span id="review-status"></span><div id="review-summary" class="notice" hidden></div><div id="review-steps"></div><div id="review-rebuild-result"></div></section>
  <section class="panel" id="evidence-panel" hidden><h2>Evidence 安全定位</h2><div id="evidence-detail"></div></section>
  <section class="panel"><h2>发现问题 → 展示证据</h2><div id="issues"></div></section>
  <section class="panel"><h2>修订前后对比</h2><div class="cols"><div><h3>修订前</h3><div id="before"></div></div><div><h3>修订后</h3><div id="after"></div></div></div></section>
  <section class="panel"><h2>局部修订审计</h2><div id="changes"></div></section>
  <section class="panel" id="results-panel" hidden><h2>培训成果</h2><div id="training-video-card" hidden><h3>80秒横屏培训视频</h3><div id="training-video-notice" class="notice"></div><div id="training-video-metrics" class="grid"></div><video controls preload="metadata" style="width:100%;margin:14px 0;border-radius:12px;background:#000" src="/api/n31/media/training-video"></video></div><div class="downloads" id="n31-downloads"><a class="download" href="/api/n31/artifacts/final-sop">下载最终 SOP</a><a class="download" href="/api/n31/artifacts/sop-views">下载三种 SOP 视图</a><a class="download" href="/api/n31/artifacts/checklist">下载手机检查清单</a><a class="download" href="/api/n31/artifacts/quiz">下载培训测验</a><a class="download" href="/api/n31/artifacts/poster">下载 A4 培训海报</a><a class="download" href="/api/n31/artifacts/training-video">下载80秒培训视频</a><a class="download" href="/api/n31/artifacts/training-video-manifest">下载视频生成清单</a><a class="download" href="/api/n31/artifacts/training-video-evidence">下载视频证据包</a><a class="download" href="/api/n31/artifacts/temporal-windows">下载连续动作窗口</a><a class="download" href="/api/n31/artifacts/pdf-structure">下载手册结构报告</a><a class="download" href="/api/n31/artifacts/source-candidates">下载候选合并报告</a><a class="download" href="/api/n31/artifacts/grounding-gate">下载无来源内容门禁报告</a><a class="download" href="/api/n31/artifacts/semantic-review">下载语义质检报告</a><a class="download" href="/api/n31/artifacts/selective-rebuild">下载选择性重建报告</a><a class="download" href="/api/n31/artifacts/revision-audit">下载修订记录</a></div><div id="sop-views-card"><h3>三种 SOP 阅读视图</h3><div class="controls"><button class="sop-tab" data-view="concise">简洁版</button><button class="sop-tab secondary" data-view="detailed">详细版</button><button class="sop-tab secondary" data-view="evidence">带证据版</button></div><div id="sop-view"></div></div><div class="cols"><div><h3>手机端检查清单</h3><div id="checklist-progress" class="notice"></div><div id="checklist" class="check-card"></div><div class="controls"><button id="check-prev" class="secondary">上一步</button><button id="check-next" class="secondary">下一步</button></div><label>问题类型<select id="feedback-category"><option value="STEP_BLOCKED">步骤受阻</option><option value="CONTENT_ERROR">内容错误</option><option value="EVIDENCE_ISSUE">证据问题</option><option value="OTHER">其他</option></select></label><label>问题反馈<textarea id="feedback-comment" maxlength="500" placeholder="描述现场问题；记录只保存在本机，不进入Git"></textarea></label><button id="feedback-submit">提交本步反馈</button><span id="checklist-status"></span></div><div><h3>培训测验</h3><div id="quiz"></div></div></div></section>
  <section class="panel"><h2>上传素材并原生预处理</h2><p>上传内容只写入被 Git 忽略的本地输出目录。本页面不会自动把原始素材发送给外部模型。</p>
    <form id="upload"><label>操作视频<input type="file" name="video" accept="video/*"></label><label>设备 PDF<input type="file" name="pdf" accept="application/pdf"></label><label>专家录音<input type="file" name="audio" accept="audio/*"></label><label><input style="width:auto" type="checkbox" name="transcribe" value="true">调用 StepAudio ASR</label><label><input style="width:auto" type="checkbox" name="analyze_visuals" value="true">调用 Step 3.7 分析关键帧</label><label><input style="width:auto" type="checkbox" name="plan_sop" value="true">根据证据规划 SOP</label><label><input style="width:auto" type="checkbox" name="external_processing_authorized" value="true">已确认允许把选定派生内容发送给外部 API</label><button>开始处理</button><span id="status"></span></form><pre id="ingest"></pre>
  </section>
</main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>`${(Number(v)*100).toFixed(0)}%`;
let activeDemo=null,checklistIndex=0,checklistSession=null,sopView='concise',reviewSession=null;
const locator=e=>e.locator.page?`PDF第${e.locator.page}页${e.locator.paragraph?' · '+e.locator.paragraph:''}`:`${(e.locator.start_ms/1000).toFixed(1)}–${(e.locator.end_ms/1000).toFixed(1)}秒`;
const quizAnswer=q=>q.answer===true?'正确':q.answer===false?'错误':Array.isArray(q.answer)?q.answer.join(q.category==='ORDERING'?' → ':'、'):q.answer;
const evidenceButtons=ids=>(ids||[]).map(id=>`<button class="evidence-link" data-evidence="${esc(id)}" title="点击查看安全定位">${esc(id)}</button>`).join('')||'无';
function renderSopView(){if(!activeDemo?.sop_views)return;const v=activeDemo.sop_views.views[sopView];document.querySelector('#sop-view').innerHTML=`<p>${esc(v.description)}</p>`+v.steps.map(x=>`<div class="step"><b>${esc(x.step_id)} ${esc(x.title)}</b><div>${esc(x.action)}</div><div class="muted">原因：${esc(x.reason)}</div><div>完成标志：${esc(x.completion_marker)}</div><div class="warning-list">${x.risks.map(r=>`风险：${esc(r)}`).join('<br>')||'风险：无额外提示'}</div><div class="evidence">来源：${x.sources.map(s=>`${esc(s.source_type)} · ${esc(s.source_ref)}`).join('；')}</div>${x.evidence_details?`<details><summary>展开证据</summary>${x.evidence_details.map(e=>`<div>${evidenceButtons([e.evidence_id])} · ${esc(e.classification)} · ${esc(e.review_status)} · ${esc(locator(e))}</div>`).join('')}</details>`:''}</div>`).join('');document.querySelectorAll('.sop-tab').forEach(b=>b.classList.toggle('secondary',b.dataset.view!==sopView))}
async function ensureChecklistSession(){if(checklistSession)return checklistSession;const r=await fetch('/api/n31/checklist/sessions',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'无法创建完成记录');checklistSession=d;return d}
function renderChecklist(){if(!activeDemo?.checklist)return;const items=activeDemo.checklist.items,item=items[checklistIndex],state=checklistSession?.items.find(x=>x.step_id===item.step_id),done=state?.completed??item.completed,k=item.keyframe,interactive=activeDemo.summary.synthetic===false;document.querySelector('#checklist-progress').textContent=`第 ${checklistIndex+1}/${items.length} 步 · 已完成 ${checklistSession?.progress.completed_items||0}/${items.length} · ${checklistSession?.status||'NOT_STARTED'}`;document.querySelector('#checklist').innerHTML=`<div class="result"><b>${esc(item.step_id)} ${esc(item.title)}</b><div>${esc(item.action)}</div>${k?`<img src="/api/n31/checklist/previews/${esc(item.step_id)}" alt="${esc(item.step_id)}安全训练预览" onerror="this.style.display='none'"><div class="evidence">安全训练预览来自已审核80秒成片；Evidence定位：${evidenceButtons([k.evidence_id])} · ${(k.start_ms/1000).toFixed(1)}–${(k.end_ms/1000).toFixed(1)}秒 · 视觉状态 ${esc(k.visual_status)}</div>`:''}${k?.visual_status==='NOT_VISIBLE'?'<div class="notice">该画面不能证明本步动作，仅用于定位人工复核；本步依据手册和其他已审核来源。</div>':''}<div>完成标志：${esc(item.check)}</div><div class="warning-list">${item.warnings.map(w=>`风险：${esc(w)}`).join('<br>')}</div><details><summary>展开 ${item.evidence_details.length} 条证据</summary>${item.evidence_details.map(e=>`<div>${evidenceButtons([e.evidence_id])} · ${esc(e.source_type)} · ${esc(e.classification)} · ${esc(e.review_status)} · ${esc(locator(e))}</div>`).join('')}</details><label><input id="step-completed" style="width:auto" type="checkbox" ${done?'checked':''} ${interactive?'':'disabled'}> 已完成并记录本步</label></div>`;document.querySelector('#check-prev').disabled=checklistIndex===0;document.querySelector('#check-next').disabled=checklistIndex===items.length-1;document.querySelector('#feedback-submit').disabled=!interactive;const box=document.querySelector('#step-completed');if(interactive)box.addEventListener('change',async()=>{const status=document.querySelector('#checklist-status');status.textContent=' 保存中…';try{const session=await ensureChecklistSession();const r=await fetch(`/api/n31/checklist/sessions/${session.session_id}/items/${item.step_id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({completed:box.checked})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'保存失败');checklistSession=d;status.textContent=' 已保存';renderChecklist()}catch(e){box.checked=!box.checked;status.textContent=` ${e.message}`}})}
async function apiJson(url,options={}){const r=await fetch(url,options),d=await r.json();if(!r.ok)throw new Error(d.detail||`请求失败 ${r.status}`);return d}
function renderReview(){const panel=document.querySelector('#review-panel'),summary=document.querySelector('#review-summary'),steps=document.querySelector('#review-steps');if(!reviewSession){summary.hidden=true;steps.innerHTML='';return}const confirmed=reviewSession.steps.filter(x=>x.confirmed).length,locked=reviewSession.steps.filter(x=>x.locked).length;summary.hidden=false;summary.textContent=`会话 ${reviewSession.session_id.slice(0,8)} · ${reviewSession.status} · 已锁定 ${locked}/13 · 已确认 ${confirmed}/13 · 审计事件 ${reviewSession.events.length}`;steps.innerHTML=reviewSession.steps.map(x=>`<div class="step review-step"><strong>${x.position}</strong><div><b>${esc(x.step_id)} ${esc(x.title)}</b><div class="muted">前置：${esc(x.prerequisites.join(', ')||'无')} · 重建${x.rebuild_count}次 · ${x.locked?'已锁定':'未锁定'} · ${x.confirmed?'已确认':'待确认'}</div></div><div class="review-actions"><button class="secondary" data-review-action="up" data-step="${esc(x.step_id)}" data-position="${x.position}" ${x.position===1||x.locked?'disabled':''}>上移</button><button class="secondary" data-review-action="down" data-step="${esc(x.step_id)}" data-position="${x.position}" ${x.position===reviewSession.steps.length||x.locked?'disabled':''}>下移</button><button class="secondary" data-review-action="rebuild" data-step="${esc(x.step_id)}" ${x.locked?'disabled':''}>单步重建</button><button data-review-action="lock" data-step="${esc(x.step_id)}" ${x.confirmed?'disabled':''}>${x.locked?'解锁':'锁定'}</button><button data-review-action="confirm" data-step="${esc(x.step_id)}" ${!x.locked?'disabled':''}>${x.confirmed?'撤回确认':'人工确认'}</button></div></div>`).join('');panel.hidden=false}
async function startReview(){const status=document.querySelector('#review-status');status.textContent=' 创建中…';try{reviewSession=await apiJson('/api/n31/review/sessions',{method:'POST'});status.textContent=' 审核会话已创建';renderReview()}catch(e){status.textContent=` ${e.message}`}}
async function reviewAction(button){if(!reviewSession)return;const action=button.dataset.reviewAction,stepId=button.dataset.step,status=document.querySelector('#review-status');status.textContent=' 保存中…';try{const current=reviewSession.steps.find(x=>x.step_id===stepId);if(action==='up'||action==='down'){const target=Number(button.dataset.position)+(action==='up'?-1:1);reviewSession=await apiJson(`/api/n31/review/sessions/${reviewSession.session_id}/reorder`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({step_id:stepId,target_position:target})})}else if(action==='lock'){reviewSession=await apiJson(`/api/n31/review/sessions/${reviewSession.session_id}/steps/${stepId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({locked:!current.locked})})}else if(action==='confirm'){reviewSession=await apiJson(`/api/n31/review/sessions/${reviewSession.session_id}/steps/${stepId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:!current.confirmed})})}else if(action==='rebuild'){const d=await apiJson(`/api/n31/review/sessions/${reviewSession.session_id}/steps/${stepId}/rebuild`,{method:'POST'});reviewSession=await apiJson(`/api/n31/review/sessions/${reviewSession.session_id}`);document.querySelector('#review-rebuild-result').innerHTML=`<div class="change"><b>${esc(d.step_id)} 单步重建 #${esc(d.rebuild_number)}</b><div>只重建三种SOP视图、1个检查卡和测验 ${esc(d.scope.quiz_question_ids.join(', ')||'无关联题')}；其余${esc(d.scope.unchanged_step_count)}步保持不变。</div><div class="evidence">Evidence：${evidenceButtons(d.evidence_ids)}｜外部模型调用 ${esc(d.scope.external_model_calls)}</div></div>`}status.textContent=' 已保存';renderReview()}catch(e){status.textContent=` ${e.message}`}}
async function showEvidence(evidenceId){const panel=document.querySelector('#evidence-panel'),detail=document.querySelector('#evidence-detail');panel.hidden=false;detail.innerHTML='<div class="notice">正在读取安全定位…</div>';panel.scrollIntoView({behavior:'smooth',block:'start'});try{const d=await apiJson(`/api/n31/evidence/${evidenceId}`);detail.innerHTML=`<div class="result"><b>${esc(d.evidence_id)} · ${esc(d.navigation.label)}</b><div>${esc(d.claim)}</div><div class="evidence">${esc(d.source_type)} · ${esc(d.source_ref)} · ${esc(d.classification)} · ${esc(d.review_status)} · 绑定步骤 ${esc(d.step_ids.join(', ')||'无')}</div>${d.navigation.safe_preview_url?`<img src="${esc(d.navigation.safe_preview_url)}" alt="${esc(d.evidence_id)}安全预览">`:''}<div class="notice">仅显示结构化页码或时间点${d.navigation.safe_preview_url?'及已审核安全预览':''}；不提供原始素材链接。</div></div>`}catch(e){detail.innerHTML=`<div class="notice">${esc(e.message)}</div>`}}
function renderDemo(d){activeDemo=d;const b=d.summary.before,a=d.summary.after,isReal=d.summary.synthetic===false,isGold=d.summary.gold_status==='GOLD';
document.querySelector('#metrics-title').textContent=isGold?'N31 真实素材 Gold 闭环':isReal?'N31 真实素材闭环彩排':'无版权模拟闭环';
document.querySelector('#basis').textContent=isGold?'实际操作者口述审核 · Gold v1 · 最终评测指标。':isReal?'候选基准 · 非 Gold · 指标仅用于证明闭环可运行，等待操作者审核后重跑最终评测。':'明确标注的无版权模拟数据，不作为真实案例评测。';
document.querySelector('#metrics').innerHTML=[['必要步骤',`${pct(b.required_step_coverage)} → ${pct(a.required_step_coverage)}`],['证据覆盖',`${pct(b.evidence_supported_required_steps)} → ${pct(a.evidence_supported_required_steps)}`],['严重错误',`${b.severe_error_count} → ${a.severe_error_count}`],['局部修改',d.summary.revision_count],['状态',isReal?d.summary.gold_status||'NOT_GOLD':d.summary.workflow_state]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');
if(d.workflow){const w=d.workflow,attempts=w.stage_attempts,events=w.history,failures=events.filter(x=>x.event_type==='FAILURE').length,reruns=events.filter(x=>x.event_type==='RERUN').length;document.querySelector('#workflow-panel').hidden=false;document.querySelector('#workflow-metrics').innerHTML=[['当前状态',w.state],['执行阶段',Object.values(attempts).filter(x=>x>0).length],['质检轮次',attempts.VERIFYING],['失败记录',failures],['阶段重跑',reruns]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#workflow-note').textContent='每次状态迁移都进入Schema校验的检查点；可重试失败可以从原阶段恢复，不可重试失败必须由操作者修复后显式重跑。当前仅声明状态与下游失效审计，未把未执行的产物重建误报为完成。';document.querySelector('#workflow-events').innerHTML=events.slice(-6).map(x=>`<div class="change"><b>${esc(x.event_type)} · ${esc(x.from_state)} → ${esc(x.to_state)} · 第${esc(x.attempt)}次</b><div>${esc(x.reason||'无补充说明')}</div>${x.invalidated_states.length?`<div class="evidence">下游失效：${esc(x.invalidated_states.join(', '))}</div>`:''}</div>`).join('')}
document.querySelector('#review-panel').hidden=!isGold;if(isGold)renderReview();
if(d.dgx_visual_compute){const g=d.dgx_visual_compute,s=g.summary;document.querySelector('#dgx-panel').hidden=false;document.querySelector('#dgx-metrics').innerHTML=[['GPU',g.gpu.device_name],['本地视频',s.processed_video_count],['GPU处理帧',s.sampled_frame_count],['候选帧',s.selected_frame_count],['CUDA核耗时',`${Number(s.gpu_kernel_ms).toFixed(1)}ms`]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#agent-trace').innerHTML=g.agent_trace.map(x=>`<div class="change"><b>${esc(x.event_id)} · ${esc(x.agent)} · ${esc(x.action)}</b><div>${esc(x.decision)}</div><div class="evidence">工具：${esc(x.tool)}｜结果：${esc(x.outcome)}</div></div>`).join('')}
if(d.temporal_action_windows){const t=d.temporal_action_windows,s=t.summary;document.querySelector('#temporal-panel').hidden=false;document.querySelector('#temporal-metrics').innerHTML=[['Gold步骤',s.step_count],['连续窗口',s.window_count],['视频来源',s.source_count],['DGX候选命中窗口',s.window_with_dgx_candidate_count],['独立DGX候选',s.unique_dgx_candidate_count]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#temporal-note').textContent='窗口只把同一视频时间线内的相邻Evidence区间合并，并绑定附近DGX场景候选；它用于定位人工/模型复核范围，不自动证明动作完成。';document.querySelector('#temporal-windows').innerHTML=t.windows.slice(0,6).map(w=>`<div class="change"><b>${esc(w.window_id)} · ${esc(w.title)}</b><div>${esc(w.source_ref)}｜${(w.start_ms/1000).toFixed(1)}–${(w.end_ms/1000).toFixed(1)}秒｜视觉${esc(w.visual_verdict)}</div><div class="evidence">Evidence：${esc(w.evidence_ids.join(', '))}｜DGX候选：${esc(w.dgx_candidate_timestamps_ms.map(v=>(v/1000).toFixed(1)+'s').join(', ')||'无')}</div></div>`).join('')}
if(d.pdf_structure){const p=d.pdf_structure,s=p.summary;document.querySelector('#pdf-panel').hidden=false;document.querySelector('#pdf-metrics').innerHTML=[['手册',s.source_count],['页数',s.page_count],['结构块',s.block_count],['OCR处理页',s.ocr_applied_page_count],['待OCR页',s.needs_ocr_page_count],['检索验证',`${s.passed_query_count}/${s.query_count}`]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#pdf-note').textContent='原始手册、页面图和含正文检索索引只保存在本地；Web只展示不含正文的页码级验证报告。';document.querySelector('#pdf-queries').innerHTML=p.queries.map(q=>{const h=q.top_hits[0];return `<div class="change"><b>${esc(q.query_id)} · ${esc(q.query)}</b><div>${esc(q.status)}｜精确命中${esc(q.exact_match_count)}条</div><div class="evidence">${h?`首条：${esc(h.source_ref)} PDF第${esc(h.page)}页｜${esc(h.kind)}`:'无检索结果'}</div></div>`}).join('')}
if(d.source_candidate_synthesis){const c=d.source_candidate_synthesis,s=c.summary,byStep=Object.fromEntries(c.ordered_steps.map(x=>[x.step_id,x]));document.querySelector('#candidate-panel').hidden=false;document.querySelector('#candidate-metrics').innerHTML=[['视频候选',s.source_candidate_counts.video],['手册候选',s.source_candidate_counts.pdf],['口述候选',s.source_candidate_counts.audio],['合并步骤',s.ordered_step_count],['多源步骤',s.multi_source_step_count],['人工复核',s.review_route_counts.HUMAN_REVIEW_REQUIRED],['粗/细粒度',`${s.coarse_candidate_count}/${s.fine_candidate_count}`]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#candidate-note').textContent='候选先按视频、手册和口述分别保存，再按来源权威性、多源佐证和负面观察计算置信度，随后拆分过粗动作、合并同义或过细动作并校验依赖。S04视频不可见，因此自动进入人工复核。';document.querySelector('#candidate-groups').innerHTML=c.merge_groups.slice(0,8).map(g=>{const a=byStep[g.step_id].confidence_assessment;return `<div class="change"><b>${esc(g.step_id)} · ${esc(g.operations.join(' + '))}</b><div>${esc(g.rationale)}</div><div>置信度 ${esc(a.score)} · ${esc(a.band)} · ${esc(a.route)}</div><div class="evidence">来源：${esc(g.source_types.join('、'))}｜候选：${esc(g.candidate_ids.join(', '))}｜Evidence：${esc(g.evidence_ids.join(', '))}</div></div>`}).join('')}
if(d.multisource_comparison&&d.visual_review){const s=d.multisource_comparison.source_ablation,v=d.visual_review.summary,p=d.multisource_comparison.privacy_comparison;document.querySelector('#multisource-panel').hidden=false;document.querySelector('#source-metrics').innerHTML=[['手册单源',pct(s.manual_only.coverage)],['专家口述单源',pct(s.expert_audio_only.coverage)],['两种以上来源',pct(s.two_or_more_source_types.coverage)],['视频部分可观察',pct(s.video_observable_partial_or_better.coverage)],['视觉矛盾',v.contradicted_count]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#visual-note').textContent=`严格视觉复核：${v.supported_count}步完整支持、${v.partial_count}步部分可见、${v.not_visible_count}步不可见、${v.contradicted_count}步矛盾。模型标记${p.model_flagged_step_count}步需隐私复核；本地安全派生QA为${p.local_safe_derivative_qa}，标记保留但不自动推翻人工检查。`}
if(d.grounding_gate){const g=d.grounding_gate,s=g.summary;document.querySelector('#grounding-panel').hidden=false;document.querySelector('#grounding-metrics').innerHTML=[['篡改场景',s.scenario_count],['成功检出',s.detected_count],['局部恢复',s.revised_count],['残留冲突',s.residual_conflict_count],['外部模型调用',g.model_calls]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#grounding-scenarios').innerHTML=g.scenarios.map(x=>`<div class="issue"><b>${esc(x.scenario_id)} · ${esc(x.expected_conflict_kind)}</b><div>${esc(x.mutation_summary)}</div><div>${esc(x.rejection_reason)}</div><div class="evidence">Evidence边界：${esc(x.reference_evidence_ids.join(', '))}</div><details><summary>查看修订前后</summary><div>修订前：${esc(JSON.stringify(x.before_value))}</div><div>修订后：${esc(JSON.stringify(x.after_value))}</div><div>动作：${esc(x.revision_actions.join(', '))}｜复检残留：${esc(x.residual_conflict_count)}</div></details></div>`).join('')}
if(d.semantic_review){const r=d.semantic_review,s=r.summary;document.querySelector('#semantic-panel').hidden=false;document.querySelector('#semantic-metrics').innerHTML=[['复核步骤',s.step_count],['证据支持',s.supported_count],['语义问题',s.finding_count],['高严重度',s.high_severity_count],['模型调用',r.model_calls]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#semantic-note').textContent=`${r.model} · ${r.reasoning_effort}推理；检查曲解来源、来源冲突、顺序风险和异常遗漏。只发送${r.review_scope.evidence_count}条结构化Evidence陈述，不发送原始媒体、完整转写、手册页面、本地路径或凭证；模型不能自动覆盖Gold。`;document.querySelector('#semantic-assessments').innerHTML=r.assessments.map(x=>`<div class="change"><b>${esc(x.step_id)} · ${esc(x.verdict)} · 置信度${esc(x.confidence)}</b><div>${esc(x.rationale)}</div><div class="evidence">Evidence：${esc(x.evidence_ids.join(', '))}</div>${x.risk_notes.length?`<div class="warning-list">${esc(x.risk_notes.join('；'))}</div>`:''}</div>`).join('')}
if(d.selective_rebuild){const r=d.selective_rebuild,s=r.summary;document.querySelector('#selective-panel').hidden=false;document.querySelector('#selective-metrics').innerHTML=[['受影响步骤',s.affected_step_count],['内容变化',s.content_changed_step_count],['重建测验题',s.quiz_question_count],['重渲染镜头',s.video_scene_count],['外部模型调用',r.data_policy.external_model_calls]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');document.querySelector('#selective-note').textContent='Revision Audit已精确重放为After SOP；4道未变化测验题和8个无关视频镜头逐对象保留，A4因固定单页布局只在依赖变化时整页重建。';document.querySelector('#selective-plans').innerHTML=r.artifact_plans.map(x=>`<div class="change"><b>${esc(x.artifact_type)} · ${esc(x.action)} · ${esc(x.scope)}</b><div>${esc(x.reason)}</div><div class="evidence">重建单元：${esc(x.units.join(', ')||'无')}｜保持不变：${esc(x.unchanged_unit_count)}</div></div>`).join('')}
if(d.training_video){const t=d.training_video,o=t.output,c=t.coverage;document.querySelector('#training-video-card').hidden=false;document.querySelector('#training-video-notice').textContent=t.final_human_review_required?'自动检查与AI辅助抽帧检查已通过；最终提交前仍需参赛者完整观看并确认旁白节奏。':'参赛者已完成最终观看确认。';document.querySelector('#training-video-metrics').innerHTML=[['时长',`${(o.duration_ms/1000).toFixed(0)}秒`],['Gold步骤',`${c.covered_gold_step_count}/${c.gold_step_count}`],['必要步骤',`${c.covered_required_step_count}/${c.required_step_count}`],['证据引用',c.evidence_reference_count],['状态',t.status]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('')}
document.querySelector('#issues').innerHTML=d.initial_conflicts.conflicts.map(c=>`<div class="issue"><b>${esc(c.kind)}</b> · ${esc(c.message)}<div class="evidence">${c.evidence.map(e=>`${evidenceButtons([e.evidence_id])}｜${esc(e.source_ref)}｜${esc(JSON.stringify(e.locator))}`).join('<br>')||'无来源内容：按规则拒绝'}</div></div>`).join('');
const render=s=>s.steps.map(x=>`<div class="step"><b>${esc(x.step_id)} ${esc(x.title)}</b><div class="muted">${esc(x.action)}</div><div class="evidence">证据：${evidenceButtons(x.evidence)}</div></div>`).join('');document.querySelector('#before').innerHTML=render(d.before_sop);document.querySelector('#after').innerHTML=render(d.after_sop);
document.querySelector('#changes').innerHTML=d.revision_audit.changes.map(c=>`<div class="change"><b>${esc(c.action)} · ${esc(c.path)}</b><div>${esc(c.reason)}</div><div class="evidence">证据：${evidenceButtons(c.evidence_ids)}</div></div>`).join('');
if(d.checklist&&d.quiz){document.querySelector('#results-panel').hidden=false;document.querySelector('#n31-downloads').hidden=d.summary.synthetic!==false;document.querySelector('#sop-views-card').hidden=!d.sop_views;renderSopView();renderChecklist();document.querySelector('#quiz').innerHTML=d.quiz.questions.map(x=>`<div class="result"><b>${esc(x.question_id)} · ${esc(x.category)}<br>${esc(x.prompt)}</b>${x.options.length?`<ol>${x.options.map(o=>`<li>${esc(o.text)}</li>`).join('')}</ol>`:''}<div>答案：${esc(quizAnswer(x))}</div><div class="muted">${esc(x.explanation)}</div><div class="evidence">答案来源：${evidenceButtons(x.answer_evidence_ids)}｜解析来源：${evidenceButtons(x.explanation_evidence_ids)}</div><details><summary>展开证据定位</summary>${x.evidence_details.map(e=>`<div>${evidenceButtons([e.evidence_id])} · ${esc(e.source_type)} · ${esc(e.classification)} · ${esc(e.review_status)} · ${esc(locator(e))}</div>`).join('')}</details></div>`).join('')}}
async function loadDemo(){let r=await fetch('/api/n31');if(r.ok){renderDemo(await r.json());return}r=await fetch('/api/demo');if(!r.ok){await fetch('/api/demo/run',{method:'POST'});r=await fetch('/api/demo')}renderDemo(await r.json())}
document.querySelector('#rerun').addEventListener('click',async()=>{const s=document.querySelector('#rerun-status');s.textContent=' 运行中…';const r=await fetch('/api/n31/run',{method:'POST'});const d=await r.json();s.textContent=r.ok?` 完成：严重错误 ${d.before.severe_error_count} → ${d.after.severe_error_count}`:` 失败：${d.detail||'未知错误'}`;if(r.ok)await loadDemo()});
document.querySelectorAll('.sop-tab').forEach(b=>b.addEventListener('click',()=>{sopView=b.dataset.view;renderSopView()}));document.querySelector('#check-prev').addEventListener('click',()=>{if(checklistIndex>0){checklistIndex--;renderChecklist()}});document.querySelector('#check-next').addEventListener('click',()=>{if(checklistIndex<(activeDemo?.checklist.items.length||1)-1){checklistIndex++;renderChecklist()}});document.querySelector('#feedback-submit').addEventListener('click',async()=>{const status=document.querySelector('#checklist-status'),comment=document.querySelector('#feedback-comment').value.trim(),item=activeDemo.checklist.items[checklistIndex];if(!comment){status.textContent=' 请先填写问题';return}status.textContent=' 保存中…';try{const session=await ensureChecklistSession();const r=await fetch(`/api/n31/checklist/sessions/${session.session_id}/items/${item.step_id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({feedback_category:document.querySelector('#feedback-category').value,feedback_comment:comment})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'保存失败');checklistSession=d;document.querySelector('#feedback-comment').value='';status.textContent=' 反馈已保存在本机';renderChecklist()}catch(e){status.textContent=` ${e.message}`}});
document.querySelector('#review-start').addEventListener('click',startReview);document.addEventListener('click',e=>{const evidence=e.target.closest('[data-evidence]');if(evidence){showEvidence(evidence.dataset.evidence);return}const review=e.target.closest('[data-review-action]');if(review)reviewAction(review)});
document.querySelector('#upload').addEventListener('submit',async e=>{e.preventDefault();const status=document.querySelector('#status');status.textContent='处理中…';const r=await fetch('/api/ingest',{method:'POST',body:new FormData(e.target)});const d=await r.json();status.textContent=r.ok?'完成':'失败';document.querySelector('#ingest').textContent=JSON.stringify(d,null,2)});loadDemo();
</script></body></html>"""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_media_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".mp4": "video/mp4",
        ".pdf": "application/pdf",
    }.get(path.suffix.lower(), "application/octet-stream")


def _training_video_manifest() -> dict[str, Any] | None:
    manifest_path = ROOT / "output/video/n31_training_video_manifest_v1.json"
    video_path = ROOT / "output/video/n31_training_video_v1.mp4"
    if not manifest_path.is_file() and not video_path.is_file():
        return None
    if not manifest_path.is_file() or not video_path.is_file():
        raise ValueError("培训视频和证据清单必须同时存在")
    manifest = validate_document(
        _read_json(manifest_path), "training_video_manifest.schema.json"
    )
    output = manifest["output"]
    if output["filename"] != video_path.name:
        raise ValueError("培训视频文件名与证据清单不一致")
    if output["bytes"] != video_path.stat().st_size or output["sha256"] != _sha256(
        video_path
    ):
        raise ValueError("培训视频大小或SHA-256与证据清单不一致")
    evidence_pack_path = ROOT / "output/video" / manifest["evidence_pack"]["filename"]
    if not evidence_pack_path.is_file():
        raise ValueError("培训视频证据包不存在")
    if manifest["evidence_pack"]["sha256"] != _sha256(evidence_pack_path):
        raise ValueError("培训视频证据包SHA-256与生成清单不一致")
    evidence_pack = validate_document(
        _read_json(evidence_pack_path), "training_video_evidence_pack.schema.json"
    )
    if evidence_pack["training_video_sha256"] != output["sha256"]:
        raise ValueError("培训视频证据包未绑定当前成片")
    return manifest


def _checklist_thumbnail_manifest() -> dict[str, Any]:
    manifest_path = ROOT / "output/checklist_thumbnails/manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = validate_document(
        _read_json(manifest_path), "checklist_thumbnail_manifest.schema.json"
    )
    video_path = ROOT / manifest["source_video"]["path"]
    if (
        not video_path.is_file()
        or video_path.stat().st_size != manifest["source_video"]["bytes"]
        or _sha256(video_path) != manifest["source_video"]["sha256"]
    ):
        raise ValueError("检查清单预览未绑定当前培训视频")
    allowed_root = (ROOT / "output/checklist_thumbnails").resolve()
    for item in manifest["items"]:
        path = (ROOT / item["preview_path"]).resolve()
        if (
            allowed_root not in path.parents
            or not path.is_file()
            or path.stat().st_size != item["bytes"]
            or _sha256(path) != item["sha256"]
        ):
            raise ValueError(f"检查清单预览校验失败: {item['step_id']}")
    return manifest


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
    for name in (
        "sop_views",
        "checklist",
        "quiz",
        "workflow",
        "grounding_gate",
        "semantic_review",
        "selective_rebuild",
    ):
        path = directory / f"{name}.json"
        if path.is_file():
            document = _read_json(path)
            if name == "quiz":
                validate_document(document, "training_quiz.schema.json")
            elif name == "workflow":
                validate_document(document, "workflow_run.schema.json")
            elif name == "grounding_gate":
                validate_document(document, "grounding_gate_report.schema.json")
            elif name == "semantic_review":
                validate_document(document, "semantic_review_report.schema.json")
            elif name == "selective_rebuild":
                validate_document(document, "selective_rebuild_report.schema.json")
            payload[name] = document
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
    checklist_sessions = ChecklistSessionStore(root / "checklist_sessions")
    review_sessions = SopReviewSessionStore(root / "sop_review_sessions")
    app = FastAPI(title="SkillForge", version="0.1.0")

    def active_n31_sop() -> dict[str, Any]:
        try:
            payload = _demo_payload(active_n31_dir["path"])
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="N31 Gold结果尚未生成") from exc
        summary = payload["summary"]
        if (
            summary.get("synthetic") is not False
            or summary.get("gold_status") != "GOLD"
            or summary.get("metrics_status") != "FINAL"
        ):
            raise HTTPException(status_code=409, detail="只有Gold最终结果可以进入人工审核")
        sop = dict(payload["after_sop"])
        if "evidence_catalog" not in sop:
            gold = _read_json(ROOT / "cases/n31/gold/gold_sop.json")
            validate_document(gold, "sop.schema.json")
            sop["evidence_catalog"] = gold["evidence_catalog"]
        try:
            return validate_document(sop, "sop.schema.json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="N31最终SOP格式无效") from exc

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return HTML

    @app.get("/health")
    def health() -> dict[str, Any]:
        training_video = _training_video_manifest()
        return {
            "status": "ok",
            "runtime": "native-python",
            "docker_required": False,
            "n31_rehearsal_available": (
                active_n31_dir["path"] / "summary.json"
            ).is_file(),
            "training_video_available": training_video is not None,
            "training_video_status": (
                training_video["status"] if training_video else None
            ),
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
            dgx_visual_path = evaluation_dir / "dgx_visual_compute_v1.json"
            temporal_path = evaluation_dir / "temporal_action_windows_v1.json"
            pdf_structure_path = evaluation_dir / "pdf_structure_v1.json"
            source_candidates_path = evaluation_dir / "source_candidate_synthesis_v1.json"
            grounding_gate_path = evaluation_dir / "deterministic_grounding_gate_v1.json"
            semantic_review_path = evaluation_dir / "semantic_review_v1.json"
            selective_rebuild_path = evaluation_dir / "selective_rebuild_v1.json"
            if visual_path.is_file() and comparison_path.is_file():
                payload["visual_review"] = _read_json(visual_path)
                payload["multisource_comparison"] = _read_json(comparison_path)
            if dgx_visual_path.is_file():
                dgx_visual = _read_json(dgx_visual_path)
                validate_document(dgx_visual, "dgx_visual_compute.schema.json")
                payload["dgx_visual_compute"] = dgx_visual
            if temporal_path.is_file():
                temporal = _read_json(temporal_path)
                validate_document(temporal, "temporal_action_windows.schema.json")
                payload["temporal_action_windows"] = temporal
            if pdf_structure_path.is_file():
                pdf_structure = _read_json(pdf_structure_path)
                validate_document(pdf_structure, "pdf_structure_report.schema.json")
                payload["pdf_structure"] = pdf_structure
            if source_candidates_path.is_file():
                source_candidates = _read_json(source_candidates_path)
                validate_document(
                    source_candidates,
                    "source_candidate_synthesis.schema.json",
                )
                payload["source_candidate_synthesis"] = source_candidates
            if grounding_gate_path.is_file():
                grounding_gate = _read_json(grounding_gate_path)
                validate_document(
                    grounding_gate,
                    "grounding_gate_report.schema.json",
                )
                payload["grounding_gate"] = grounding_gate
            if semantic_review_path.is_file():
                semantic_review = _read_json(semantic_review_path)
                validate_document(
                    semantic_review,
                    "semantic_review_report.schema.json",
                )
                payload["semantic_review"] = semantic_review
            if selective_rebuild_path.is_file():
                selective_rebuild = _read_json(selective_rebuild_path)
                validate_document(
                    selective_rebuild,
                    "selective_rebuild_report.schema.json",
                )
                payload["selective_rebuild"] = selective_rebuild
            try:
                training_video = _training_video_manifest()
            except ValueError as exc:
                raise HTTPException(
                    status_code=409, detail=f"培训视频证据校验失败: {exc}"
                ) from exc
            if training_video:
                payload["training_video"] = training_video
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
            media_type=_artifact_media_type(path),
            filename=download_name,
        )

    @app.post("/api/n31/review/sessions")
    def create_review_session() -> dict[str, Any]:
        return review_sessions.create(active_n31_sop())

    @app.get("/api/n31/review/sessions/{session_id}")
    def get_review_session(session_id: str) -> dict[str, Any]:
        try:
            return review_sessions.get(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="SOP审核会话不存在") from exc

    @app.patch("/api/n31/review/sessions/{session_id}/steps/{step_id}")
    def update_review_step(
        session_id: str,
        step_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if not payload or set(payload) - {"locked", "confirmed"}:
            raise HTTPException(status_code=400, detail="审核步骤更新字段无效")
        if any(not isinstance(value, bool) for value in payload.values()):
            raise HTTPException(status_code=400, detail="locked和confirmed必须为布尔值")
        try:
            return review_sessions.set_step_state(
                session_id,
                step_id,
                active_n31_sop(),
                locked=payload.get("locked") if "locked" in payload else None,
                confirmed=(
                    payload.get("confirmed") if "confirmed" in payload else None
                ),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="SOP审核会话不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/n31/review/sessions/{session_id}/reorder")
    def reorder_review_step(
        session_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if set(payload) != {"step_id", "target_position"}:
            raise HTTPException(status_code=400, detail="重排请求字段无效")
        if (
            not isinstance(payload["step_id"], str)
            or not isinstance(payload["target_position"], int)
            or isinstance(payload["target_position"], bool)
        ):
            raise HTTPException(status_code=400, detail="重排步骤或位置类型无效")
        try:
            return review_sessions.reorder(
                session_id,
                payload["step_id"],
                payload["target_position"],
                active_n31_sop(),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="SOP审核会话不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/n31/review/sessions/{session_id}/steps/{step_id}/rebuild"
    )
    def rebuild_review_step(session_id: str, step_id: str) -> dict[str, Any]:
        visual_path = ROOT / "cases/n31/evaluations/visual_sequence_review_v1.json"
        visual_review = _read_json(visual_path) if visual_path.is_file() else None
        try:
            return rebuild_step_artifacts(
                review_sessions,
                session_id,
                step_id,
                active_n31_sop(),
                visual_review=visual_review,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="SOP审核会话不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/n31/evidence/{evidence_id}")
    def locate_n31_evidence(evidence_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"E[0-9]{3}", evidence_id):
            raise HTTPException(status_code=404, detail="Evidence不存在")
        try:
            return build_evidence_locator(active_n31_sop(), evidence_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Evidence不存在") from exc

    @app.post("/api/n31/checklist/sessions")
    def create_checklist_session() -> dict[str, Any]:
        try:
            checklist = _demo_payload(active_n31_dir["path"])["checklist"]
            return checklist_sessions.create(checklist)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="N31检查清单尚未生成") from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="N31检查清单格式无效") from exc

    @app.get("/api/n31/checklist/sessions/{session_id}")
    def get_checklist_session(session_id: str) -> dict[str, Any]:
        try:
            return checklist_sessions.get(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="检查清单记录不存在") from exc

    @app.patch("/api/n31/checklist/sessions/{session_id}/items/{step_id}")
    def update_checklist_item(
        session_id: str,
        step_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        allowed = {"completed", "feedback_category", "feedback_comment"}
        if not payload or set(payload) - allowed:
            raise HTTPException(status_code=400, detail="检查清单更新字段无效")
        completed = payload.get("completed")
        if "completed" in payload and not isinstance(completed, bool):
            raise HTTPException(status_code=400, detail="completed 必须为布尔值")
        try:
            return checklist_sessions.update_item(
                session_id,
                step_id,
                completed=completed if "completed" in payload else None,
                feedback_category=payload.get("feedback_category"),
                feedback_comment=payload.get("feedback_comment"),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="检查清单记录不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/n31/checklist/keyframes/{evidence_id}")
    def checklist_keyframe(evidence_id: str) -> FileResponse:
        if not re.fullmatch(r"E[0-9]{3}", evidence_id):
            raise HTTPException(status_code=404)
        try:
            checklist = _demo_payload(active_n31_dir["path"])["checklist"]
        except (FileNotFoundError, KeyError) as exc:
            raise HTTPException(status_code=404) from exc
        keyframe = next(
            (
                item["keyframe"]
                for item in checklist.get("items", [])
                if item.get("keyframe")
                and item["keyframe"].get("evidence_id") == evidence_id
            ),
            None,
        )
        if keyframe is None:
            raise HTTPException(status_code=404)
        allowed_root = (
            ROOT / "cases/n31/output/ingest_local_v1"
        ).resolve()
        candidate = (allowed_root / keyframe["keyframe"]).resolve()
        if (
            allowed_root not in candidate.parents
            or candidate.suffix.lower() not in {".jpg", ".jpeg", ".png"}
            or not candidate.is_file()
        ):
            raise HTTPException(status_code=404)
        return FileResponse(
            candidate,
            media_type="image/jpeg" if candidate.suffix.lower() != ".png" else "image/png",
            content_disposition_type="inline",
        )

    @app.get("/api/n31/checklist/previews/{step_id}")
    def checklist_preview(step_id: str) -> FileResponse:
        if not re.fullmatch(r"S[0-9]{2}", step_id):
            raise HTTPException(status_code=404)
        try:
            manifest = _checklist_thumbnail_manifest()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        item = next(
            (candidate for candidate in manifest["items"] if candidate["step_id"] == step_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404)
        return FileResponse(
            ROOT / item["preview_path"],
            media_type="image/jpeg",
            content_disposition_type="inline",
        )

    @app.get("/api/n31/media/training-video")
    def stream_n31_training_video() -> FileResponse:
        path = ROOT / "output/video/n31_training_video_v1.mp4"
        try:
            manifest = _training_video_manifest()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if manifest is None or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=path.name,
            content_disposition_type="inline",
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
