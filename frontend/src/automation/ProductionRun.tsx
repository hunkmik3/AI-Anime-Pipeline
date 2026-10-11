import { useState } from "react";
import { api } from "../api/client";
import { useAutomation } from "../store/automation";

const stages: Record<string,string> = { motion:"Chuẩn bị chuyển động gốc", staging:"Tạo ảnh dàn cảnh 21:9", audio_cleanup:"Kiểm tra / tách nhạc", master_review:"Chờ chốt tạo hình chính", materials:"Tạo material", raccord:"Lập raccord", prompts:"Viết prompt", videos:"Viết prompt / gen video", quality:"Kiểm tra video thực tế", assembly:"Ghép phim", complete:"Hoàn tất lượt chạy", blocked:"Cần xử lý" };
export function ProductionRun() {
  const pid=useAutomation(s=>s.currentProjectId);
  const reskin=useAutomation(s=>s.motionReference === "white_model");
  const sequences=useAutomation(s=>s.nodes).filter(n=>n.data.kind === "sequence");
  const [selected,setSelected]=useState<string[]>([]);
  const runs=useAutomation(s=>s.jobs).filter(j=>j.kind==="production_run");
  const [mode,setMode]=useState("prepare");
  const [frames,setFrames]=useState(false);
  const [reviewMasters,setReviewMasters]=useState(true);
  // Provider-bound work: sheets and videos wait on the provider, not on this machine.
  const [imageParallel,setImageParallel]=useState(16), [videoParallel,setVideoParallel]=useState(16);
  const [resolution,setResolution]=useState("480p");
  const [fullTakes,setFullTakes]=useState(true);
  const [maxImages,setMaxImages]=useState(100);
  const [maxVideos,setMaxVideos]=useState(100);
  const [report,setReport]=useState<any>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const config={mode,resolution,sequence_keys:selected,review_masters:reviewMasters,timing_policy:fullTakes?'full_take':'source_duration',boundary_frames:reskin?false:frames,audio_cleanup:reskin,set_continuity:!reskin,image_parallel:imageParallel,video_parallel:videoParallel,max_images:maxImages,max_videos:maxVideos};
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
  return <section className="production-run-settings">
    <strong>Chạy sản xuất toàn board</strong>
    <p>Bắt đầu từ shotlist đã nhập và hồ sơ đã chốt. Server tạo material thiếu, chuẩn bị reference, viết prompt và gen theo phụ thuộc. Đóng tab vẫn chạy.</p>
    <details><summary>{selected.length ? `${selected.length} clip đã chọn` : "Toàn bộ clip"} · chọn phạm vi</summary>
      <button onClick={()=>setSelected([])}>Dùng toàn bộ</button>
      {sequences.map(n=>{const key=n.id.replace(/^seq:/,"");return <label key={n.id} style={{display:"block"}}><input type="checkbox" checked={selected.includes(key)} onChange={e=>{setSelected(e.target.checked?[...selected,key]:selected.filter(k=>k!==key));setReport(null)}}/> {key}</label>})}
      <small>Không chọn riêng clip nào: chạy toàn bộ. Lượt có cùng đầu vào dùng lại kết quả đã xong.</small>
    </details>
    <label>Phạm vi <select value={mode} onChange={e=>{setMode(e.target.value);setReport(null)}}><option value="prepare">Material + prompt</option><option value="render">Material + prompt + video + ghép phim</option></select></label>{" "}
    {!reskin && <label><input type="checkbox" checked={frames} onChange={e=>{setFrames(e.target.checked);setReport(null)}}/> Thêm ảnh hướng dẫn đầu/cuối clip (tính lượt ảnh)</label>}
    <p><label><input type="checkbox" checked={reviewMasters} onChange={e=>setReviewMasters(e.target.checked)}/> Dừng ở Tạo hình chính trước khi gen diện mạo phụ</label></p>
    <p><label>Video <select value={resolution} onChange={e=>setResolution(e.target.value)}><option>480p</option><option>720p</option>{!reskin && <option>1080p</option>}</select></label></p>
    <p><label><input type="checkbox" checked={fullTakes} onChange={e=>{setFullTakes(e.target.checked);setReport(null)}}/> Giữ nguyên đoạn đã gen khi ghép để không cắt mất thoại (thời lượng có thể dài hơn nguồn)</label></p>
    <p><label>Song song ảnh <input type="number" min={1} max={64} value={imageParallel} onChange={e=>setImageParallel(Number(e.target.value))} style={{width:60}}/></label>{" "}
      <label>Song song video <input type="number" min={1} max={64} value={videoParallel} onChange={e=>setVideoParallel(Number(e.target.value))} style={{width:60}}/></label>{" "}
      <label>Tối đa ảnh mới <input type="number" min={0} value={maxImages} onChange={e=>setMaxImages(Number(e.target.value))} style={{width:70}}/></label>{" "}
      <label>Tối đa video mới <input type="number" min={0} value={maxVideos} onChange={e=>setMaxVideos(Number(e.target.value))} style={{width:70}}/></label></p>
    <p>Dùng lại kết quả có cùng đầu vào. Kiểm tra thoại và mẫu hình của clip đã gen trước khi ghép; nếu có lỗi sẽ giữ file và báo vị trí cần sửa. Đối chiếu hình có dùng lượt LLM. Không tự gen lại video. Giới hạn trên là số lượt gen mới, không phải dự toán tiền.</p>
    <button disabled={!pid||busy} onClick={()=>void preview()}>Kiểm tra kế hoạch (không gen)</button>{" "}
    <button disabled={!pid||busy||runs.some(r=>r.status==="running" || r.status==="paused" && r.result.stage==="master_review")} onClick={()=>void start()}>Bắt đầu — có dùng lượt gen</button>
    {error&&<p role="alert">{error}</p>}
    {report&&<div><p>{report.clips.length} clip · {report.shots} shot · {report.missing_materials}/{report.materials} material còn thiếu</p>{report.atlases?.filter((a:any)=>a.pages.length).map((a:any)=><p key={a.clip}>{a.clip}: {a.source_images} ảnh → {a.transport_images} slot reference; tự ghép {a.pages.length} atlas.</p>)}{report.dependency_notes?.map((n:any,i:number)=><p key={i}>{n.assets.join(", ")}: dùng lại ảnh có sẵn, không cần gen theo vòng phụ thuộc.</p>)}{report.issues.map((i:any,n:number)=><p key={n}>{i.clip}: {i.code} {i.message}</p>)}</div>}
    {runs.slice(-5).reverse().map(r=>{const tasks=Object.values(r.result.tasks||{}) as {status:string}[];return <div key={r.id} style={{marginTop:12}}>
      <strong>{stages[r.result.stage]||r.result.stage}</strong> · {r.status} · {tasks.filter(t=>t.status==="succeeded").length}/{tasks.length} tác vụ đã lên lịch hoàn tất
      {r.error&&<p role="alert">{r.error}</p>}
      {r.result.preview_output?.filename&&<p><a href={`/api/automation/projects/${pid}/production-runs/${r.id}/film?preview=true`} target="_blank" rel="noreferrer">Mở / tải bản xem thử · còn mục cần duyệt</a></p>}
      {r.result.quality_results&&Object.entries(r.result.quality_results as Record<string,any>).filter(([,q])=>q.status!=="checks_passed").map(([key,q])=><details key={key}><summary>{key} · cần kiểm tra</summary><ul>{q.findings?.map((f:any,i:number)=><li key={i}>{f.message||f.note||f.code}{f.differences&&<pre>{JSON.stringify(f.differences,null,2)}</pre>}</li>)}</ul></details>)}
      {r.result.quality_status==="automated_checks_passed"&&<small>Đối chiếu tự động đạt. Giọng diễn, nhạc nền và chi tiết giữa các frame mẫu vẫn cần xem/nghe khi duyệt bản giao.</small>}
      {r.status==="running"&&<button onClick={()=>void control(r.id,"pause")}>Dừng xếp thêm việc</button>}
      {["paused","blocked"].includes(r.status)&&r.result.stage!=="master_review"&&<button onClick={()=>void control(r.id,"resume")}>Tiếp tục sau khi xử lý</button>}
      {r.result.stage==="master_review"&&<p>Mở tab Tạo hình chính để upload hoặc gen sheet, rồi chốt để tiếp tục.</p>}
      {r.result.output?.filename&&<p><a href={`/api/automation/projects/${pid}/production-runs/${r.id}/film`} target="_blank" rel="noreferrer">Mở / tải bản dựng</a></p>}
    </div>})}
    <small>Dừng chỉ ngừng xếp việc mới; job đã gửi vẫn chạy. Nếu dữ liệu nguồn đổi, bắt đầu lượt mới để tính lại phần bị ảnh hưởng.</small>
  </section>;
}
