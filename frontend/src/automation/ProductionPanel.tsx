import { useEffect, useState } from "react";
import { ProductionProgress } from "./ProductionProgress";
import { ProductionRun } from "./ProductionRun";
import { ShotPreparation } from "./ShotPreparation";
import { api } from "../api/client";
import { useAutomation, type AutoNodeData } from "../store/automation";

type Manifest = { version: string; assets: Record<string, { version: string }>; shots: { id: string; sequence_key: string; source_shots: number[]; start_state: unknown; end_state: unknown; transitions: { asset_id: string; field: string; to: unknown; explained: boolean }[] }[]; issues: { shot: string; code: string }[] };
const labels: Record<string,string> = { queued: "Chờ chạy", preparing: "Chuẩn bị", submitting: "Đang gửi", running: "Đang xử lý", succeeded: "Hoàn thành", failed: "Lỗi", unknown: "Cần đối soát", cancelled: "Đã hủy" };

/** Keep job polling alive even while advanced controls are out of view. */
export function ProductionPanel() {
  const projectId = useAutomation(s => s.currentProjectId);
  const refresh = useAutomation(s => s.refreshJobs);
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
    if (!projectId) return;
    let active = true;
    const poll = () => void refresh().then(() => { if (active) setError(""); })
      .catch(e => { if (active) setError(e.message); });
    poll(); const timer = setInterval(poll, 4000);
    return () => { active = false; clearInterval(timer); };
  }, [projectId, refresh]);
  return <ProductionProgress key={projectId ?? "draft"} jobError={error} />;
}

export function ProductionControls() {
  const projectId = useAutomation((s) => s.currentProjectId);
  const jobs = useAutomation((s) => s.jobs);
  const refresh = useAutomation((s) => s.refreshJobs);
  const preserve = useAutomation((s) => s.preserveSourceShots);
  const setPreserve = useAutomation((s) => s.setPreserveSourceShots);
  const [error, setError] = useState("");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [providerIds, setProviderIds] = useState<Record<string,string>>({});
  const [absenceNotes, setAbsenceNotes] = useState<Record<string,string>>({});
  const [revisions, setRevisions] = useState<{revision:number;created_at:string}[]>([]);
  const [selectedRevision, setSelectedRevision] = useState("");
  const [notes, setNotes] = useState<Record<string,string>>({});
  useEffect(() => {
    setManifest(null);setError("");setRevisions([]);setSelectedRevision("");
  }, [projectId]);
  async function resolveAbsent(id:string) {
    try {
      await api(`/api/automation/projects/${projectId}/jobs/${id}/resolve-absent`, { method:"POST", body:JSON.stringify({provider_checked_no_submission:true,note:absenceNotes[id]}) });
      await refresh();setError("");
    } catch(e) {setError((e as Error).message)}
  }
  async function loadManifest() {
    await useAutomation.getState().saveNow();
    if(useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError);
    setManifest(await api<Manifest>(`/api/automation/projects/${projectId}/production`));
    setRevisions(await api(`/api/automation/projects/${projectId}/revisions`));
    setSelectedRevision("");
  }
  async function action(id:string, kind:"cancel"|"resume") {
    try { await api(`/api/automation/projects/${projectId}/jobs/${id}/${kind}`, { method:"POST", body:JSON.stringify({provider_job_id:providerIds[id] || ""}) });await refresh();setError(""); }
    catch(e) {setError((e as Error).message)}
  }
  function recordTransition(shot:Manifest["shots"][number], transition:Manifest["shots"][number]["transitions"][number], key:string) {
    const reason=notes[key]?.trim();if(!reason)return;
    const state=useAutomation.getState();const node=state.nodes.find((n)=>n.id===`seq:${shot.sequence_key}`);
    if(node?.data.kind!=="sequence")return;
    const shots=node.data.shots.map((s)=>{
      const source=s.source_shots?.length?s.source_shots:[(s as any).source_shot];
      if(s.shot_uid!==shot.id && !(shot.source_shots.length===1 && source.length===1 && source[0]===shot.source_shots[0]))return s;
      return {...s,continuity_events:[...(s.continuity_events || []),{asset_id:transition.asset_id,field:transition.field,to:transition.to,reason}]};
    });
    state.patchNode(node.id,{shots} as Partial<AutoNodeData>);
    void loadManifest().catch((e)=>setError(e.message));
  }
  return <details className="production-advanced">
    <summary>Điều khiển sản xuất nâng cao · {jobs.filter((j)=>["queued","preparing","submitting","running"].includes(j.status)).length} job đang xử lý · {jobs.filter((j)=>j.status==="unknown").length} cần đối soát</summary>
    <p><label><input type="checkbox" checked={preserve} onChange={(e)=>setPreserve(e.target.checked)}/> Giữ từng shot nguồn khi nhập board (không gộp cut ngắn)</label></p>
    <p>Job ảnh/video của board đã lưu chạy trên server. Đóng tab không hủy job. Đối soát chỉ tiếp tục theo dõi mã provider, không gửi gen mới.</p>
    {error && <p role="alert">{error}</p>}
    <button disabled={!projectId} onClick={()=>void loadManifest().catch((e)=>setError(e.message))}>Kiểm tra trạng thái cảnh và phiên bản tài sản</button>
    {revisions.length>0 && <label> Phiên bản <select value={selectedRevision} onChange={(e)=>{
      const revision=e.target.value;setSelectedRevision(revision);
      void api<Manifest>(`/api/automation/projects/${projectId}/${revision ? `revisions/${revision}` : "production"}`).then(setManifest).catch((e)=>setError(e.message));
    }}><option value="">Hiện tại</option>{revisions.map((r)=><option key={r.revision} value={r.revision}>#{r.revision} · {new Date(r.created_at).toLocaleString()}</option>)}</select></label>}
    {jobs.filter(j=>!["production_run","material_binding"].includes(j.kind)).slice(-20).reverse().map((j)=><div key={j.id} style={{margin:"10px 0"}}>
      <strong>{j.kind === "raccord" ? "Kế hoạch raccord toàn cảnh" : j.node_id} {j.slot}</strong> · {labels[j.status] || j.status} {j.error && <span>— {j.error}</span>}
      {j.kind==="raccord" && j.status==="succeeded" && <span> · {j.result.mode === "ai" ? "Luna đã lập kế hoạch" : j.result.mode === "ai_with_rules" ? "Luna + quy tắc xử lý chi tiết thiếu căn cứ" : "Đã dùng quy tắc dự phòng"} · {j.result.shots?.length ?? 0} shot</span>}
      {j.kind==="write" && j.status==="succeeded" && <details><summary>Xem prompt đã lưu</summary><pre style={{whiteSpace:"pre-wrap"}}>{j.result.prompt}</pre></details>}
      {j.status==="queued" && <button onClick={()=>void action(j.id,"cancel")}>Hủy trước khi gửi</button>}
      {j.status==="unknown" && j.kind==="clip" && <><input aria-label={`Mã provider ${j.node_id}`} value={providerIds[j.id] ?? j.provider_job_id ?? ""} placeholder="Mã job từ Avis" onChange={(e)=>setProviderIds({...providerIds,[j.id]:e.target.value})}/><button onClick={()=>void action(j.id,"resume")}>Đối soát / tiếp tục</button></>}
      {j.status==="unknown" && !j.provider_job_id && <div><input aria-label={`Kết quả kiểm tra Avis ${j.node_id}`} placeholder="Ghi kết quả đã kiểm tra trực tiếp trên Avis" value={absenceNotes[j.id] || ""} onChange={(e)=>setAbsenceNotes({...absenceNotes,[j.id]:e.target.value})}/><button disabled={(absenceNotes[j.id]?.trim().length ?? 0)<8} onClick={()=>void resolveAbsent(j.id)}>Đã kiểm tra: provider chưa tạo job</button></div>}
    </div>)}
    <ProductionRun key={`run:${projectId}`}/>
    <ShotPreparation key={projectId ?? "draft"}/>
    {manifest && <><p>{manifest.shots.length} shot · {Object.keys(manifest.assets).length} tài sản · {manifest.issues.length} ghi chú. Trạng thái kế thừa là thông tin lần thấy gần nhất, không phải bằng chứng mới.</p>
      {manifest.shots.map((shot)=><details key={shot.id}><summary>{shot.id} · {shot.sequence_key}</summary><pre style={{whiteSpace:"pre-wrap"}}>{JSON.stringify({start:shot.start_state,end:shot.end_state},null,2)}</pre>
        {shot.transitions.filter((t)=>!t.explained && !selectedRevision).map((t,i)=>{const k=shot.id+":"+i;return <div key={k}><p>{t.asset_id}: thay đổi {t.field}</p><input aria-label={`Lý do ${k}`} placeholder="Hành động chuyển / lý do từ nguồn" value={notes[k] || ""} onChange={(e)=>setNotes({...notes,[k]:e.target.value})}/><button onClick={()=>recordTransition(shot,t,k)}>Ghi nhận chuyển tiếp</button></div>})}
      </details>)}
      {manifest.issues.map((issue,i)=><p key={i}>{issue.shot}: {issue.code}</p>)}
    </>}
  </details>;
}
