import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useAutomation, type Plate, type RuntimeJob } from "../store/automation";

type Item = {
  node_id: string; kind: "character" | "environment"; slot: string; required: boolean;
  profile: { name: string; role?: string; summary?: string; identity_anchor?: string; lock?: string };
  plate: Plate; states: { key: string; label: string; stale: boolean; ready: boolean }[]; clips: string[];
};
type Review = { items: Item[]; waiting_run: (RuntimeJob & { config: { mode: string } }) | null };
const active = (j: RuntimeJob) => ["queued", "preparing", "submitting", "running", "unknown"].includes(j.status);

/** One canonical identity/location per card, never one card per costume. */
export function PrimaryMaterials() {
  const pid = useAutomation(s => s.currentProjectId);
  const nodes = useAutomation(s => s.nodes);
  const jobs = useAutomation(s => s.jobs);
  const [review, setReview] = useState<Review | null>(null);
  const [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [mode, setMode] = useState("prepare");
  const epoch = useRef(0);
  // Job polling updates image progress without refetching on every prompt keystroke.
  const jobVersion = jobs.map(j => `${j.id}:${j.status}`).join("|");
  useEffect(() => {
    ++epoch.current;
    setReview(null); setError(""); setBusy("");
    return () => { epoch.current++; };
  }, [pid]);
  useEffect(() => {
    if (!pid) return;
    let cancelled = false;
    void api<Review>(`/api/automation/projects/${pid}/primary-materials`)
      .then(r => { if (!cancelled) setReview(r); }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [pid, jobVersion]);

  const items: Item[] = (review?.items ?? []).map((item): Item => {
    const data = nodes.find(n => n.id === item.node_id)?.data;
    if (data?.kind === "character") return { ...item, profile: data.character, plate: data.identity,
      states: data.character.states.map(s => ({ key: s.key, label: s.label,
        ready: !!data.states[s.key]?.referenceUrl, stale: !!data.states[s.key]?.needsIdentityRefresh })) };
    if (data?.kind === "environment") return { ...item, profile: data.environment, plate: data.plate };
    return item;
  });
  const required = items.filter(i => i.required);
  const missing = required.filter(i => !i.plate.referenceUrl);
  const running = jobs.some(j => j.kind === "production_run" && j.status === "running");
  const waiting = review?.waiting_run;
  const unresolved = jobs.some(j => active(j) && j.kind !== "production_run");
  const locked = !!busy || running;

  async function savedRevision() {
    await useAutomation.getState().saveNow();
    const s = useAutomation.getState();
    if (s.currentProjectId !== pid) throw new Error("Board đã đổi.");
    if (s.saveState === "error") throw new Error(s.saveError || "Không lưu được board.");
    return s.projectRevision;
  }
  async function act(label: string, fn: () => Promise<void>) {
    const current = epoch.current;
    setBusy(label); setError("");
    try {
      await fn();
      if (current !== epoch.current) return;
      await useAutomation.getState().refreshJobs();
      const next = await api<Review>(`/api/automation/projects/${pid}/primary-materials`);
      if (current === epoch.current) setReview(next);
    } catch (e) { if (current === epoch.current) setError((e as Error).message); }
    finally { if (current === epoch.current) setBusy(""); }
  }
  async function generate(list: Item[]) {
    await act("Đang xếp sheet chính…", async () => {
      for (const item of list) {
        const revision = await savedRevision();
        const storageKey = `primary:${pid}:${item.node_id}:${item.plate.prompt}`;
        const requestKey = sessionStorage.getItem(storageKey) || crypto.randomUUID();
        sessionStorage.setItem(storageKey, requestKey);
        await api(`/api/automation/projects/${pid}/primary-materials/generate`, { method: "POST", body: JSON.stringify({
          node_id: item.node_id, expected_revision: revision, request_key: requestKey,
        }) });
        sessionStorage.removeItem(storageKey);
      }
    });
  }
  async function begin() {
    await act("Đang mở bước chốt…", async () => {
      const revision = await savedRevision();
      const storageKey = `primary-review:${pid}:${mode}`;
      const requestKey = sessionStorage.getItem(storageKey) || crypto.randomUUID();
      sessionStorage.setItem(storageKey, requestKey);
      await api(`/api/automation/projects/${pid}/production-runs`, { method: "POST", body: JSON.stringify({
        expected_revision: revision, request_key: requestKey,
        config: { review_masters: true, mode, resolution: "480p", timing_policy: "full_take" },
      }) });
      sessionStorage.removeItem(storageKey);
    });
  }
  async function approve() {
    if (!waiting) return;
    await act("Đang chốt sheet…", async () => {
      const revision = await savedRevision();
      await api(`/api/automation/projects/${pid}/primary-materials/continue`, { method: "POST", body: JSON.stringify({
        run_id: waiting.id, expected_revision: revision,
      }) });
    });
  }

  return <section className="primary-materials" aria-label="Tạo hình chính">
    <header className="primary-materials__intro">
      <div><span className="primary-materials__eyebrow">01 · TẠO HÌNH GỐC</span>
        <h2>Chốt nhân vật & bối cảnh</h2>
        <p>Upload sheet của bạn hoặc gen từ hồ sơ. Các trang phục và trạng thái phụ sẽ dùng sheet nhân vật này làm gốc.</p>
      </div>
      <div className="primary-materials__count"><strong>{required.length - missing.length}<small> / {required.length}</small></strong><span>sheet chính sẵn sàng</span></div>
    </header>
    {error && <p className="auto-banner auto-banner--stop" role="alert">{error}</p>}
    {!pid ? <p>Tạo hoặc mở một board để lưu sheet.</p> : !review ? <p>Đang đọc hồ sơ…</p> : !items.length ?
      <div className="primary-materials__empty"><h3>Chưa có hồ sơ nhân vật và bối cảnh</h3><p>Đưa video gốc vào Canvas. Khi phân tích xong, các hồ sơ sẽ xuất hiện ở đây.</p></div> : <>
      <div className="primary-materials__toolbar">
        <div className="primary-materials__filters" aria-label="Lọc hồ sơ">
          {[["all", "Tất cả"], ["character", "Nhân vật"], ["environment", "Bối cảnh"]].map(([key, label]) =>
            <button type="button" className="auto-btn" aria-pressed={filter === key} key={key} onClick={() => setFilter(key)}>{label} <small>{items.filter(i => key === "all" || i.kind === key).length}</small></button>)}
        </div>
        <button className="auto-btn" disabled={locked || !missing.length || unresolved}
          onClick={() => void generate(missing)}>Gen {missing.length || ""} sheet chính còn thiếu</button>
      </div>
      <div className="primary-materials__grid">
        {items.filter(i => filter === "all" || i.kind === filter).map(item => {
          const inFlight = jobs.some(j => j.node_id === item.node_id && j.slot === item.slot && active(j));
          const sheet = item.plate;
          return <article className="primary-card" key={item.node_id}>
            <header><div><span>{item.kind === "character" ? "NHÂN VẬT · SHEET GỐC" : "BỐI CẢNH · SHEET GỐC"}</span><h3>{item.profile.name}</h3></div>
              <span className={`primary-card__status ${sheet.referenceUrl ? "is-ready" : ""}`}>{inFlight ? "Đang xử lý" : sheet.uploaded ? "Đã upload" : sheet.referenceUrl ? "Có sheet" : "Chưa có sheet"}</span></header>
            <div className="primary-card__image">{sheet.image || sheet.referenceUrl ?
              <a href={sheet.image || sheet.referenceUrl} target="_blank" rel="noreferrer"><img loading="lazy" decoding="async" src={sheet.image || sheet.referenceUrl} alt={`Sheet chính — ${item.profile.name}`} /></a> :
              <div><span>{item.kind === "character" ? "◯" : "▧"}</span><p>Upload hoặc gen sheet chính</p></div>}</div>
            <div className="primary-card__body">
              <div className="primary-card__actions">
                <label className={`auto-btn primary-card__upload ${locked || inFlight ? "is-disabled" : ""}`}>↑ Upload sheet
                  <input type="file" accept="image/png,image/jpeg,image/webp" disabled={locked || inFlight}
                    aria-label={`Upload sheet ${item.profile.name}`} onChange={e => {
                      const file = e.target.files?.[0]; e.target.value = "";
                      if (file) void act("Đang upload sheet…", () => useAutomation.getState().uploadPlate(item.node_id, item.slot, file));
                    }} /></label>
                <button className="auto-btn" disabled={locked || inFlight} onClick={() => void generate([item])}>{sheet.referenceUrl ? "Gen lại sheet" : "Gen sheet chính"}</button>
              </div>
              {item.profile.role && <b>{item.profile.role}</b>}
              <p className="primary-card__summary">{item.profile.summary || item.profile.identity_anchor || item.profile.lock}</p>
              <div className="primary-card__usage">{item.states.length > 0 && <span>{item.states.length} diện mạo / trạng thái</span>}{item.clips.length > 0 && <span>{item.clips.length} clip sử dụng</span>}{!item.required && <span>Không dùng trong lượt đang chọn</span>}</div>
              {item.states.length > 0 && <details><summary>Các diện mạo phụ</summary><ul>{item.states.map(s => <li key={s.key}>{s.label}<small> · {s.stale ? "Cần gen theo sheet mới" : s.ready ? "Có sheet" : "Chưa gen"}</small></li>)}</ul></details>}
              <details><summary>Hồ sơ & prompt</summary><p>{item.profile.summary}</p><p>{item.profile.identity_anchor || item.profile.lock}</p>
                <label>Prompt sheet chính<textarea rows={6} value={sheet.prompt || ""} disabled={locked || inFlight}
                  placeholder="Để trống để dùng master prompt theo hồ sơ và style phim."
                  onChange={e => useAutomation.getState().editPrompt(item.node_id, item.slot, e.target.value)} /></label>
              </details>
              {sheet.error && <p className="primary-card__error">{sheet.error}</p>}

            </div>
          </article>;
        })}
      </div>
    </>}
    {!!items.length && <footer className="primary-materials__footer">
      <div><strong>{busy || (running ? "Pipeline đang chạy" : waiting ? "Chờ bạn chốt sheet chính" : "Sheet chính của bộ phim")}</strong>
        <p>{waiting ? `${missing.length ? `Còn ${missing.length} sheet cần upload hoặc gen. ` : "Các sheet chính đã đủ. "}Sau khi chốt: ${waiting.config.mode === "render" ? "gen diện mạo phụ, đạo cụ, prompt, video và ghép phim" : "gen diện mạo phụ, đạo cụ và viết prompt; chưa gen video"}.` : running ? "Theo dõi tiến độ trong mục Sản xuất. Tạm dừng trước khi thay sheet." : "Mở bước chốt để chọn thời điểm bắt đầu gen các diện mạo phụ."}</p></div>
      {waiting ? <button className="auto-btn auto-btn--primary" disabled={locked || unresolved || !!missing.length} onClick={() => void approve()}>Chốt sheet & tiếp tục →</button> : !running && <div className="primary-materials__continue">
        <select aria-label="Sau khi chốt sheet" value={mode} disabled={locked} onChange={e => setMode(e.target.value)}><option value="prepare">Material + prompt</option><option value="render">Đến video hoàn chỉnh</option></select>
        <button className="auto-btn" disabled={locked || !pid || unresolved} onClick={() => void begin()}>Mở bước chốt</button></div>}
    </footer>}
    <p className="primary-materials__note">Gen ảnh và chạy tiếp pipeline có dùng lượt gen. Upload chỉ thay sheet chính; các sheet trạng thái bạn tự upload được giữ riêng.</p>
  </section>;
}
