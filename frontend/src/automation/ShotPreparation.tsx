import { useState } from "react";
import { api } from "../api/client";
import { useAutomation, ensureRaccord, type AutoNodeData, type ShotPackage } from "../store/automation";

/** Scene plans are generated/cached automatically; image generation remains explicit. */
export function ShotPreparation() {
  const projectId = useAutomation((s) => s.currentProjectId);
  const nodes = useAutomation((s) => s.nodes);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState("");
  const prepared = nodes.filter((n) => n.data.kind === "sequence" && n.data.sequence.shot_package);
  async function prepare() {
    if (!projectId) return;
    setBusy(true);setError("");
    try {
      await useAutomation.getState().saveNow();
      if (useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError);
      await ensureRaccord();
      const packs = await api<ShotPackage[]>(`/api/automation/projects/${projectId}/shot-packages?limit=5`);
      if (useAutomation.getState().currentProjectId !== projectId) return;
      for (const pack of packs) {
        const n = useAutomation.getState().nodes.find((n) => n.id === `seq:${pack.sequence_key}`);
        if (n?.data.kind === "sequence") useAutomation.getState().patchNode(n.id, { sequence: { ...n.data.sequence, shot_package: pack } } as Partial<AutoNodeData>);
      }
      await useAutomation.getState().saveNow();
      if (useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError);
    } catch(e) {setError((e as Error).message)} finally {setBusy(false)}
  }
  async function frame(pack: ShotPackage, index: number, generate: boolean, which: "start"|"end") {
    setBusy(true);setError("");
    try {
      await useAutomation.getState().saveNow();
      if (useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError);
      await ensureRaccord(pack.sequence_key);
      if (useAutomation.getState().currentProjectId !== projectId) return;
      const current = await api<ShotPackage>(`/api/automation/projects/${projectId}/shot-packages?sequence_key=${encodeURIComponent(pack.sequence_key)}`);
      const seq = useAutomation.getState().nodes.find((n)=>n.id===`seq:${pack.sequence_key}`);
      if (seq?.data.kind === "sequence") useAutomation.getState().patchNode(seq.id,{sequence:{...seq.data.sequence,shot_package:current}} as Partial<AutoNodeData>);
      pack = current;
      await useAutomation.getState().saveNow();
      if (useAutomation.getState().saveState === "error") throw new Error(useAutomation.getState().saveError);
      if (generate) {
        const slot = `shotframe:${index}:${which}`;
        // Stable for network retries, cleared only after an accepted response.
        const pending = `shot-frame:${projectId}:${pack.sequence_key}:${pack.version}:${slot}`;
        const requestKey = localStorage.getItem(pending) || crypto.randomUUID();
        localStorage.setItem(pending, requestKey);
        await api(`/api/automation/projects/${projectId}/shot-keyframe`, {method:"POST", body:JSON.stringify({
          sequence_key:pack.sequence_key,shot_index:index,which,expected_revision:useAutomation.getState().projectRevision,request_key:requestKey,
        })});
        localStorage.removeItem(pending);
        await useAutomation.getState().refreshJobs();
      } else {
        const result = await api<{prompt:string}>(`/api/automation/projects/${projectId}/shot-keyframe?sequence_key=${encodeURIComponent(pack.sequence_key)}&shot_index=${index}&which=${which}`);
        if (useAutomation.getState().currentProjectId === projectId) setPreview(result.prompt);
      }
    } catch(e) {setError((e as Error).message)} finally {setBusy(false)}
  }
  return <section>
    <h3>Chuẩn bị từng shot</h3>
    <p>Khóa material, trang phục, quần chúng, đạo cụ và thoại tiếng Anh cho 5 clip đầu. Raccord được lập tự động bằng Luna qua Avis và dùng lại khi dữ liệu không đổi; không yêu cầu duyệt từng shot. Keyframe là ảnh bố cục để xem trước; nút gen ảnh có tính phí theo provider.</p>
    <button disabled={!projectId || busy} onClick={()=>void prepare()}>Chuẩn bị / cập nhật 5 clip đầu</button>
    {error && <p role="alert">{error}</p>}
    {prepared.map((node)=>{
      if (node.data.kind!=="sequence") return null;
      const pack=node.data.sequence.shot_package!;
      const video=nodes.find((n)=>n.id===`vid:${pack.sequence_key}`);
      return <details key={node.id}><summary>{node.data.sequence.label} · {pack.shots.length} shot · {pack.ready ? "Đủ material" : "Cần bổ sung"}</summary>
        {Object.entries(pack.materials).map(([key,m])=><p key={key}>{m.name} · {m.kind} · {m.reference_url ? "Có reference" : "Thiếu reference"}{m.wardrobe ? ` · ${m.wardrobe}` : ""}</p>)}
        {pack.issues.map((i,n)=><p key={n}>{i.blocking ? "Cần sửa" : "Ghi chú"}: {i.asset || i.shot} · {i.code}{i.message ? ` — ${i.message}` : ""}</p>)}
        {pack.shots.map((shot)=><details key={shot.id}><summary>{shot.id} {shot.keyframe_recommended ? "· nên có keyframe" : ""}</summary>
          {shot.raccord && <p><strong>Raccord tự động:</strong> {shot.raccord.direction} {shot.raccord.predecessor_shot_id ? `Nối hành động từ ${shot.raccord.predecessor_shot_id}.` : ""} {shot.raccord.mode === "rules_fallback" ? "(quy tắc dự phòng)" : ""}</p>}
          <p>Giữ continuity: {shot.inherited_asset_ids.join(", ") || "Không có tài sản kế thừa"}</p>
          <pre style={{whiteSpace:"pre-wrap"}}>{JSON.stringify({action:shot.action,dialogue:shot.dialogue},null,2)}</pre>
          {(["start","end"] as const).map((which)=>{
            const plate=video?.data.kind==="video" ? video.data.shotFrames?.[`shotframe:${shot.index}:${which}`] : undefined;
            return <div key={which}><button disabled={busy} onClick={()=>void frame(pack,shot.index,false,which)}>Xem prompt {which==="start" ? "đầu" : "cuối"} shot</button>
              <button disabled={busy || !pack.ready || plate?.status==="running"} onClick={()=>void frame(pack,shot.index,true,which)}>Gen ảnh {which==="start" ? "đầu" : "cuối"} shot</button>
              {plate?.status && <span> · {plate.status}{plate.package_version && plate.package_version!==pack.version ? " · dữ liệu đã đổi" : ""}</span>}
              {plate?.referenceUrl && <a href={plate.referenceUrl} target="_blank" rel="noreferrer"> Xem ảnh</a>}
            </div>;
          })}
        </details>)}
      </details>;
    })}
    {preview && <details open><summary>Prompt keyframe</summary><textarea readOnly aria-label="Prompt keyframe đã chuẩn bị" value={preview} style={{width:"100%",minHeight:220}}/></details>}
  </section>;
}
