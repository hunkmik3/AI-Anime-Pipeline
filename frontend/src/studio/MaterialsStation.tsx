import { useEffect, useMemo, useRef, useState } from "react";
import { IMAGE_MODEL_LABELS, studioApi, uploadMaterialImage, type FilmDetail, type Material, type MaterialSlot } from "./api";
import type { StationProps } from "./StudioPage";

type Group = "character" | "environment" | "other";
const GROUPS: [Group, string][] = [["character", "Nhân vật"], ["environment", "Bối cảnh"], ["other", "Đạo cụ & quần chúng"]];
const group = (m: Material): Group => (m.kind === "character" ? "character" : m.kind === "environment" ? "environment" : "other");
const SLOT_STATUS: Record<string, string> = {
  succeeded: "Đã vẽ", ready: "Có ảnh", missing: "Chưa vẽ", unused: "Không clip nào dùng", failed: "Lỗi", unknown: "Chưa rõ",
  queued: "Đang chờ", preparing: "Đang chuẩn bị", submitting: "Đang gửi", running: "Đang vẽ", blocked: "Bị chặn",
};

function SlotCard({ film, material, slot, act, busy }: { film: FilmDetail; material: Material; slot: MaterialSlot; act: StationProps["act"]; busy: string }) {
  const input = useRef<HTMLInputElement>(null);
  const [zoom, setZoom] = useState(false);
  const working = ["queued", "preparing", "submitting", "running"].includes(slot.status);
  async function upload(file: File) {
    await act(`upload-${material.node_id}-${slot.slot}`, async () => {
      const { url, mediaId } = await uploadMaterialImage(film.project_id!, file);
      await studioApi.chooseImage(film.id, material.node_id, slot.slot, film.revision!, url, mediaId);
    });
  }
  return <figure className={`st-slot st-slot--${slot.status}${slot.used ? "" : " is-unused"}`}>
    <button type="button" className="st-slot__img" onClick={() => slot.image && setZoom(true)} disabled={!slot.image}
      aria-label={slot.image ? `Phóng to ${slot.label}` : `${slot.label} chưa có ảnh`}>
      {slot.image ? <img src={slot.image} alt={`${material.name} — ${slot.label}`} loading="lazy" /> : <span>{working ? "Đang vẽ…" : "Chưa có ảnh"}</span>}
    </button>
    <figcaption>
      <strong>{slot.label}</strong>
      <small>{SLOT_STATUS[slot.status] ?? slot.status}{slot.uploaded ? " · ảnh của bạn" : ""}</small>
      {slot.error && <small className="st-slot__error" title={slot.error}>{slot.hint || slot.error.slice(0, 120)}</small>}
      {slot.used && <span className="st-slot__actions">
        <button type="button" className="st-link" disabled={!!busy || working || film.revision == null}
          onClick={() => void act(`redraw-${material.node_id}-${slot.slot}`, () => studioApi.regenerate(film.id, material.node_id, slot.slot, film.revision!))}>Vẽ lại</button>
        <button type="button" className="st-link" disabled={!!busy || !film.project_id} onClick={() => input.current?.click()}>Dùng ảnh của tôi</button>
        <input ref={input} type="file" accept="image/*" hidden onChange={e => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void upload(f); }} />
      </span>}
    </figcaption>
    {zoom && slot.image && <div className="st-zoom" role="dialog" aria-label={slot.label} onClick={() => setZoom(false)}>
      <img src={slot.image} alt={`${material.name} — ${slot.label}`} />
    </div>}
  </figure>;
}

function MaterialCard({ film, material, act, busy }: { film: FilmDetail; material: Material; act: StationProps["act"]; busy: string }) {
  const [note, setNote] = useState(material.target_description);
  const [model, setModel] = useState(material.image_model);
  const [impact, setImpact] = useState("");
  useEffect(() => { setNote(material.target_description); setModel(material.image_model); }, [material.target_description, material.image_model]);
  const dirty = note.trim() !== material.target_description || model !== material.image_model;
  async function save() {
    await act(`material-${material.node_id}`, async () => {
      const out = await studioApi.editMaterial(film.id, material.node_id, film.revision!, {
        ...(note.trim() !== material.target_description ? { target_description: note.trim() } : {}),
        ...(model !== material.image_model ? { image_model: model } : {}),
      });
      setImpact(out.impact.clips.length ? `Các sheet sẽ được vẽ lại ở lượt chạy tới; ${out.impact.clips.length} clip dùng material này cần render lại.` : "Đã lưu.");
    });
  }
  const slots = material.slots.filter(s => s.used || s.image);
  return <article className="st-material">
    <header>
      <h3>{material.name}</h3>
      <p>{material.role || material.description.slice(0, 140)}</p>
      <small>{material.clips.length ? `Dùng trong ${material.clips.length} clip` : "Không clip nào dùng"}</small>
    </header>
    <div className="st-material__slots">
      {slots.map(s => <SlotCard key={s.slot} film={film} material={material} slot={s} act={act} busy={busy} />)}
    </div>
    <details className="st-material__edit">
      <summary>Đổi thiết kế</summary>
      <label className="st-field st-field--compact">
        <span>Thiết kế mong muốn (thay vẻ ngoài của bản gốc trên mọi sheet)</span>
        <textarea rows={3} value={note} onChange={e => setNote(e.target.value)}
          placeholder={material.kind === "character" ? "Ví dụ: tóc vàng dài, mắt xanh, váy lụa đỏ" : "Ví dụ: tường gạch đỏ, cửa gỗ cổ"} />
      </label>
      <label className="st-field st-field--compact">
        <span>Model vẽ material này</span>
        <select value={model} onChange={e => setModel(e.target.value)}>
          {Object.entries(IMAGE_MODEL_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      <div className="st-actions">
        <button type="button" className="st-primary" disabled={!dirty || !!busy || film.revision == null} onClick={() => void save()}>Lưu thiết kế</button>
        {impact && !dirty && <span className="st-muted">{impact}</span>}
      </div>
    </details>
  </article>;
}

export function MaterialsStation({ film, act, busy }: StationProps) {
  const [tab, setTab] = useState<Group>("character");
  const [onlyUsed, setOnlyUsed] = useState(true);
  const shown = useMemo(() => film.materials.filter(m => group(m) === tab && (!onlyUsed || m.clips.length > 0)), [film.materials, tab, onlyUsed]);
  if (!film.materials.length) return <p className="st-empty">Danh mục chưa có — trạm Nguồn đang phân tích video.</p>;
  return <div className="st-materials">
    {film.waiting_materials_run && <div className="st-gatebar">
      <span>Thẻ chính đã vẽ xong. Xem lại gương mặt và bối cảnh; bấm Chốt để vẽ tiếp trang phục và clip.</span>
      <button type="button" className="st-primary" disabled={!!busy || film.revision == null || !film.project_id}
        onClick={() => void act("masters", () => studioApi.approveMasters(film.project_id!, film.waiting_materials_run!, film.revision!))}>Chốt thẻ chính</button>
    </div>}
    <div className="st-tabs" role="tablist">
      {GROUPS.map(([g, label]) => <button type="button" role="tab" key={g} aria-selected={tab === g} className={tab === g ? "is-on" : ""}
        onClick={() => setTab(g)}>{label} <small>{film.materials.filter(m => group(m) === g && (!onlyUsed || m.clips.length)).length}</small></button>)}
      <label className="st-tabs__filter"><input type="checkbox" checked={onlyUsed} onChange={e => setOnlyUsed(e.target.checked)} /> Chỉ material đang dùng</label>
    </div>
    <div className="st-material-grid">
      {shown.map(m => <MaterialCard key={m.node_id} film={film} material={m} act={act} busy={busy} />)}
      {!shown.length && <p className="st-empty">Không có material nào ở nhóm này.</p>}
    </div>
  </div>;
}
