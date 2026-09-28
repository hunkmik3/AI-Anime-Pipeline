import { useState } from "react";
import { api } from "../api/client";
import { useAutomation } from "../store/automation";

const stages: Record<string,string> = { materials:"Tạo material", raccord:"Lập raccord", prompts:"Viết prompt", videos:"Viết prompt / gen video", assembly:"Ghép phim", complete:"Hoàn thành", blocked:"Cần xử lý đầu vào" };
export function ProductionRun() {
  const pid=useAutomation(s=>s.currentProjectId);
  const runs=useAutomation(s=>s.jobs).filter(j=>j.kind==="production_run");
  const [mode,setMode]=useState("prepare");
  const [frames,setFrames]=useState(false);
  const [parallel,setParallel]=useState(4);
  const [maxImages,setMaxImages]=useState(100);
  const [maxVideos,setMaxVideos]=useState(100);
  const [report,setReport]=useState<any>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const config={mode,boundary_frames:frames,image_parallel:parallel,video_parallel:parallel,max_images:maxImages,max_videos:maxVideos};
  async function save() {
    const s=useAutomation.getState();await s.saveNow();
    if(useAutomation.getState().saveState==="error")throw new Error(useAutomation.getState().saveError);
    if(useAutomation.getState().currentProjectId!==pid)throw new Error("Board đã đổi");
  }
  async function preview() {
    setBusy(true);setError("");try {await save();setReport(await api(`/api/automation/projects/${pid}/production-runs/preview`,{method:"POST",body:JSON.stringify(config)}));}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function start() {
    setBusy(true);setError("");try {
      await save();
      // Keep the same key after an ambiguous network response; repeated clicks cannot start a second run.
      const storageKey=`production-run:${pid}:${JSON.stringify(config)}`;
      const key=localStorage.getItem(storageKey)||crypto.randomUUID();localStorage.setItem(storageKey,key);
      await api(`/api/automation/projects/${pid}/production-runs`,{method:"POST",body:JSON.stringify({config,request_key:key,expected_revision:useAutomation.getState().projectRevision})});
      localStorage.removeItem(storageKey);await useAutomation.getState().refreshJobs();
    } catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function control(id:string,action:string){try{await api(`/api/automation/projects/${pid}/production-runs/${id}/${action}`,{method:"POST"});await useAutomation.getState().refreshJobs()}catch(e){setError((e as Error).message)}}
  return <section style={{border:"1px solid #485064",padding:12,margin:"12px 0"}}>
    <strong>Chạy sản xuất toàn board</strong>
    <p>Bắt đầu từ shotlist đã nhập và hồ sơ đã chốt. Server tự tạo material thiếu, lập raccord, viết prompt và gen theo phụ thuộc. Đóng tab vẫn chạy.</p>
    <label>Phạm vi <select value={mode} onChange={e=>{setMode(e.target.value);setReport(null)}}><option value="prepare">Material + prompt</option><option value="render">Material + prompt + video + ghép phim</option></select></label>{" "}
    <label><input type="checkbox" checked={frames} onChange={e=>{setFrames(e.target.checked);setReport(null)}}/> Thêm ảnh hướng dẫn đầu/cuối clip (tính lượt ảnh)</label>
    <p><label>Song song ảnh/video <input type="number" min={1} max={32} value={parallel} onChange={e=>setParallel(Number(e.target.value))} style={{width:60}}/></label>{" "}
      <label>Tối đa ảnh mới <input type="number" min={0} value={maxImages} onChange={e=>setMaxImages(Number(e.target.value))} style={{width:70}}/></label>{" "}
      <label>Tối đa video mới <input type="number" min={0} value={maxVideos} onChange={e=>setMaxVideos(Number(e.target.value))} style={{width:70}}/></label></p>
    <p>Dùng lại kết quả có cùng đầu vào. Giữ nút KYC/người thật của board. Giới hạn trên là số lượt mới trong lần chạy, không phải dự toán tiền. Không tự kiểm tra hoặc gen lại để sửa lỗi video.</p>
    <button disabled={!pid||busy} onClick={()=>void preview()}>Kiểm tra kế hoạch (không gen)</button>{" "}
    <button disabled={!pid||busy||runs.some(r=>r.status==="running")} onClick={()=>void start()}>Bắt đầu — có dùng lượt gen</button>
    {error&&<p role="alert">{error}</p>}
    {report&&<div><p>{report.clips.length} clip · {report.shots} shot · {report.missing_materials}/{report.materials} material còn thiếu</p>{report.atlases?.filter((a:any)=>a.pages.length).map((a:any)=><p key={a.clip}>{a.clip}: {a.source_images} ảnh → {a.transport_images} slot reference; tự ghép {a.pages.length} atlas.</p>)}{report.dependency_notes?.map((n:any,i:number)=><p key={i}>{n.assets.join(", ")}: dùng lại ảnh có sẵn, không cần gen theo vòng phụ thuộc.</p>)}{report.issues.map((i:any,n:number)=><p key={n}>{i.clip}: {i.code} {i.message}</p>)}</div>}
    {runs.slice(-5).reverse().map(r=>{const tasks=Object.values(r.result.tasks||{}) as {status:string}[];return <div key={r.id} style={{marginTop:12}}>
      <strong>{stages[r.result.stage]||r.result.stage}</strong> · {r.status} · {tasks.filter(t=>t.status==="succeeded").length}/{tasks.length} tác vụ đã lên lịch hoàn tất
      {r.error&&<p role="alert">{r.error}</p>}
      {r.status==="running"&&<button onClick={()=>void control(r.id,"pause")}>Dừng xếp thêm việc</button>}
      {["paused","blocked"].includes(r.status)&&<button onClick={()=>void control(r.id,"resume")}>Tiếp tục sau khi xử lý</button>}
      {r.result.output?.filename&&<p><a href={`/api/automation/projects/${pid}/production-runs/${r.id}/film`} target="_blank" rel="noreferrer">Mở / tải bản dựng</a></p>}
    </div>})}
    <small>Dừng chỉ ngừng xếp việc mới; job đã gửi vẫn chạy. Nếu dữ liệu nguồn đổi, bắt đầu lượt mới để tính lại phần bị ảnh hưởng.</small>
  </section>;
}
