import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useAutomation, type AutoNodeData } from "../store/automation";
import { ProductionRun } from "./ProductionRun";
import { STYLE_PRESETS } from "../store/videoAnalysis";

type Finding = { key?: string; severity?: "blocking" | "warning"; clip?: string; shot?: string; criterion?: string;
  message?: string; evidence?: string; finding?: string; description?: string; expected?: string; heard?: string };

function findingText(f: Finding) {
  return f.message || f.evidence || f.finding || f.description || f.criterion || f.key || JSON.stringify(f);
}

function FindingLine({ f, children }: { f: Finding; children?: React.ReactNode }) {
  return <p className="reskin-panel__finding">{children}<b>{f.clip} {f.shot}</b> · {findingText(f)}
    {f.expected && <><br />Shotlist: {f.expected}<br />Nghe lại: {f.heard}</>}</p>;
}

export function ReskinPanel() {
  const state = useAutomation();
  const { currentProjectId: pid, jobs, nodes } = state;
  const [source,setSource]=useState<any>(null), [runtime,setRuntime]=useState<any>(null);
  const [error,setError]=useState(""), [busy,setBusy]=useState(false);
  const [editId,setEditId]=useState(""), [draft,setDraft]=useState("");
  const [picked,setPicked]=useState<string[]>([]), [note,setNote]=useState("");
  const jobVersion=jobs.map(j=>`${j.id}:${j.status}`).join("|");
  const active=jobs.some(j=>["running","queued","preparing","submitting","unknown"].includes(j.status));
  const runs=jobs.filter(j=>j.kind === "production_run"), latest=runs[runs.length-1];
  const refs=latest?.result.reskin_references || {};
  useEffect(()=>{
    let live=true; if(!pid)return;
    void Promise.all([api(`/api/automation/projects/${pid}/reskin/status`),api('/api/automation/reskin/capabilities')])
      .then(([s,r])=>{if(live){setSource(s);setRuntime(r)}}).catch(e=>{if(live)setError(e.message)});
    return ()=>{live=false};
  },[pid,jobVersion]);
  async function recheck() {
    setBusy(true);setError("");
    try {
      await state.saveNow(); const current=useAutomation.getState();
      if(current.saveState === "error")throw new Error(current.saveError);
      if(current.currentProjectId !== pid)throw new Error("Board đã đổi.");
      const job=await api<any>(`/api/automation/projects/${pid}/reskin/review`,{method:"POST",body:JSON.stringify({expected_revision:current.projectRevision})});
      if(["failed","unknown"].includes(job.status))throw new Error(job.error || "Giữ checkpoint đang lỗi; không tự gửi lại lượt đã tính phí.");
      await current.refreshJobs();
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  const findings:Finding[]=source?.source_review?.findings||[], accepted=source?.dispositions||{};
  // Reports written before findings carried a severity show every item as needing a decision.
  const holding=findings.filter(f=>f.severity!=="warning"&&!(f.key&&accepted[f.key]));
  const warnings=findings.filter(f=>f.severity==="warning"), acceptedFindings=findings.filter(f=>f.key&&accepted[f.key]);
  const heldClips=Object.keys(source?.held_clips||{}), readyClips:string[]=source?.ready_clips||[];
  async function acceptPicked() {
    setBusy(true);setError("");
    try {
      await state.saveNow(); const current=useAutomation.getState();
      if(current.saveState === "error")throw new Error(current.saveError);
      await api(`/api/automation/projects/${pid}/reskin/findings/accept`,{method:"POST",
        body:JSON.stringify({expected_revision:current.projectRevision,keys:picked,note:note.trim()})});
      setPicked([]);setNote("");
      await current.openProject(pid!);
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  function applyShots() {
    try {
      const shots=JSON.parse(draft);
      if(!Array.isArray(shots)||!shots.length||shots.some(s=>!Number.isFinite(s.source_start)||!Number.isFinite(s.source_end)||s.source_end<=s.source_start))throw new Error("Mỗi shot cần mốc source_start/source_end hợp lệ.");
      state.patchNode(editId,{shots} as Partial<AutoNodeData>);
      setSource((s:any)=>({...s,ready:false,issues:["Shotlist vừa thay đổi; lưu và đối chiếu lại phần đã sửa trước khi gen."]}));
      setEditId("");setError("");
    }catch(e){setError((e as Error).message)}
  }
  return <section className="reskin-panel" aria-label="Pipeline Reskin">
    <header><div><span className="primary-materials__eyebrow">RESKIN · THEO CHUYỂN ĐỘNG NGUỒN</span><h2>Từ video gốc đến bản dựng</h2><p>Chốt tạo hình riêng, giữ hành động và thoại nguồn, rồi gen và ghép các clip.</p></div>
      <div className="reskin-panel__selectors"><label>Style<select value={state.style} disabled={active} onChange={e=>state.setStyle(e.target.value as typeof state.style)}>{STYLE_PRESETS.map(s=><option key={s.key} value={s.key}>{s.label}</option>)}</select></label>
        <label>Video đầu ra<select value={state.aspectRatio} disabled={active} onChange={e=>state.setAspectRatio(e.target.value as typeof state.aspectRatio)}><option>1:1</option><option>9:16</option><option>16:9</option></select></label></div></header>
    <div className="reskin-panel__ratios"><span>Video xám <b>Giữ khung nguồn</b></span><span>Ảnh dàn cảnh <b>Một cảnh rộng 21:9</b></span><span>Video xuất <b>{state.aspectRatio}</b></span></div>
    <p className="reskin-panel__muted">Ảnh rộng hướng dẫn vị trí nhân vật. Khi đổi khung đầu ra, model mở rộng phần ngoài nguồn nên vẫn cần xem lại bố cục.</p>
    {runtime&&!runtime.ready&&<p role="alert" className="auto-banner auto-banner--stop">Máy chủ cần cấu hình: {runtime.missing.join(", ")}. Chưa bắt đầu gen.</p>}
    {error&&<p role="alert" className="auto-banner auto-banner--stop">{error}</p>}
    <details className="reskin-panel__source" open={!source?.ready}><summary>Shotlist nguồn · {source?.ready?"Đã có đối chiếu hình và âm thanh":"Cần đối chiếu / xử lý"}</summary>
      {source?.source_review?.counts&&<p>Đã nhận kết quả: {source.source_review.counts.received}/{source.source_review.counts.total} đoạn. Đọc xong chưa có nghĩa shotlist đã đạt để gen; các nhận xét cần đối soát nằm bên dưới.</p>}
      {source?.issues?.map((text:string,i:number)=><p key={i}>{text}</p>)}<p>Đối chiếu tự động có thể nhầm. Giữ đầy đủ phát hiện, dẫn chứng và giới hạn; không tự duyệt phần chưa rõ.</p>
      {state.sourceVideoId&&<video controls preload="none" src={`/api/automation/videos/${state.sourceVideoId}/source`}/>}
      {source?.source_review&&<p><b>{readyClips.length}</b> clip đã sẵn sàng gen{heldClips.length>0&&<> · <b>{heldClips.length}</b> clip chờ xử lý: {heldClips.join(", ")}</>}{warnings.length>0&&<> · {warnings.length} cảnh báo không chặn</>}.</p>}
      {holding.length>0&&<fieldset className="reskin-panel__decisions"><legend>Cần xử lý trước khi gen (lời thoại, người nói, danh tính, trang phục)</legend>
        {holding.map((f,i)=><FindingLine key={f.key||i} f={f}>{f.key&&f.criterion!=="review_incomplete"&&<input type="checkbox" aria-label="Chấp nhận phát hiện này"
          checked={picked.includes(f.key)} disabled={busy||active} onChange={e=>setPicked(p=>e.target.checked?[...p,f.key!]:p.filter(k=>k!==f.key))}/>}</FindingLine>)}
        <p className="reskin-panel__muted">Sửa shotlist nếu nguồn đúng là khác. Nếu shotlist đã đúng (ví dụ máy nghe nhầm), chọn và chấp nhận; lần sửa shot sau sẽ hỏi lại.</p>
        <label>Ghi chú<input value={note} maxLength={500} onChange={e=>setNote(e.target.value)} placeholder="Ví dụ: đã nghe lại, câu thoại trong shotlist đúng."/></label>
        <button className="auto-btn" disabled={busy||active||!picked.length} onClick={()=>void acceptPicked()}>Chấp nhận {picked.length||""} mục đã chọn</button>
      </fieldset>}
      {warnings.length>0&&<details><summary>{warnings.length} cảnh báo không chặn · camera, vị trí, bối cảnh, ghi chú của model</summary>
        <p className="reskin-panel__muted">Video chuyển động nguồn đã mang góc máy, điểm cắt và vị trí; các mục này chỉ để tham khảo.</p>
        {warnings.map((f,i)=><FindingLine key={f.key||i} f={f}/>)}</details>}
      {acceptedFindings.length>0&&<details><summary>{acceptedFindings.length} mục đã chấp nhận</summary>
        {acceptedFindings.map((f,i)=><FindingLine key={f.key||i} f={f}><span>{accepted[f.key!]?.by} · </span></FindingLine>)}</details>}
      <details><summary>Dẫn chứng từng đoạn</summary>{source?.source_review?.receipts?.map((r:any)=><details key={r.clip}><summary>{r.clip}</summary><pre>{JSON.stringify(r.observations,null,2)}</pre></details>)}</details>
      <details><summary>Sửa shotlist nâng cao</summary><p>Xem/nghe nguồn rồi sửa đúng đoạn có lỗi. Các trường chưa sửa được giữ nguyên; thay nguồn sẽ cần đối chiếu lại.</p>
        {nodes.filter(n=>n.data.kind==="sequence").map(n=><button className="auto-btn" disabled={active||busy} key={n.id} onClick={()=>{setEditId(n.id);setDraft(JSON.stringify((n.data as any).shots,null,2))}}>{(n.data as any).sequence.title||n.id.replace('seq:','')}</button>)}
        {editId&&<div><textarea aria-label="Dữ liệu shot nguồn" rows={18} value={draft} onChange={e=>setDraft(e.target.value)}/><button className="auto-btn" onClick={applyShots}>Áp dụng bản sửa</button><button className="auto-btn" onClick={()=>setEditId("")}>Hủy</button></div>}
      </details>
      <button className="auto-btn" disabled={busy||active||!!source?.ready} onClick={()=>void recheck()}>Đối chiếu bản sửa · có dùng lượt đọc video</button>
      <button className="auto-btn" disabled={busy||active} onClick={()=>void state.openProject(pid!)}>Tải lại kết quả đã lưu</button>
    </details>
    <details className="reskin-panel__run"><summary>Chạy pipeline & bản dựng</summary><ProductionRun key={pid}/></details>
    {!!Object.keys(refs).length&&<div className="reskin-panel__clips">{Object.entries(refs).map(([key,r]:[string,any])=>{
      const clip=latest?.result.preview_clips?.[key], audio=latest?.result.audio_results?.[key];
      return <article key={key}><h3>{key}</h3><div className="reskin-panel__media">
        <figure><figcaption>Chuyển động nguồn · không âm thanh</figcaption><video controls preload="none" src={r.motion.url}/></figure>
        <figure><figcaption>Dàn cảnh 21:9</figcaption><a href={r.staging.reference_url} target="_blank" rel="noreferrer"><img loading="lazy" src={r.staging.reference_url} alt={`Dàn cảnh ${key}`}/></a></figure>
        {clip&&<figure><figcaption>Clip đã gen · chưa phải xác nhận chất lượng</figcaption><video controls preload="none" src={clip.url}/><a href={clip.url} target="_blank" rel="noreferrer">Mở clip</a></figure>}
      </div>{audio?.limitations?.length>0&&<p className="reskin-panel__muted">Đã xử lý âm thanh: có thể giảm cả tiếng động và âm phòng; cần nghe lại bản xuất. <a href={audio.native_url} target="_blank" rel="noreferrer">Giữ bản âm thanh gốc</a></p>}</article>
    })}</div>}
  </section>;
}
