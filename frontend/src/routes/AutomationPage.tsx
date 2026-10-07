/**
 * /automation — the drama-film pipeline.
 *
 * A premise becomes a cast, a set of places and a run of sequences. The cast
 * and places generate their reference plates; each sequence is cut into shots
 * and then becomes one clip, wired on the board to the plates it consumes.
 *
 * Its own board, not the shot canvas. Nothing here touches a real project.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  SelectionMode,
} from "@xyflow/react";

import { ProductionPanel, ProductionControls } from "../automation/ProductionPanel";
import { AutomationSidebar } from "../automation/AutomationSidebar";
import { PrimaryMaterials } from "../automation/PrimaryMaterials";
import { ClipCanvas } from "../automation/ClipCanvas";
import { AutomationAssistant } from "../automation/AutomationAssistant";
import { automationNodeTypes } from "../automation/nodes";
import { VideoAnalysisPanel } from "../automation/VideoAnalysisPanel";
import { IMAGE_MODEL_LABELS, useAutomation } from "../store/automation";
import { STYLE_PRESETS, useVideoAnalysis } from "../store/videoAnalysis";

function BoardSettings({ title, logline, onClose, children }: {
  title: string; logline: string; onClose(): void; children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  return <dialog ref={dialog} className="auto-settings" aria-labelledby="auto-settings-title"
    onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="auto-settings__content">
      <header className="auto-settings__head">
        <h2 id="auto-settings-title">Cài đặt & công cụ</h2>
        <button type="button" className="auto-btn" onClick={onClose} autoFocus>Đóng</button>
      </header>
      <div className="auto-settings__body">
        <div className="auto-settings__film">
          <strong>{title || "Chưa có phim"}</strong>
          {logline && <p>{logline}</p>}
        </div>
        {children}
      </div>
      <footer className="auto-settings__foot">Các thay đổi được áp dụng ngay.</footer>
    </div>
  </dialog>;
}

export function AutomationPage() {
  const nodes = useAutomation((s) => s.nodes);
  const edges = useAutomation((s) => s.edges);
  const onNodesChange = useAutomation((s) => s.onNodesChange);
  const onEdgesChange = useAutomation((s) => s.onEdgesChange);
  const onConnect = useAutomation((s) => s.onConnect);

  const title = useAutomation((s) => s.title);
  const logline = useAutomation((s) => s.logline);
  const style = useAutomation((s) => s.style);
  const setStyle = useAutomation((s) => s.setStyle);
  const imageModel = useAutomation((s) => s.imageModel);
  const aspectRatio = useAutomation((s) => s.aspectRatio);
  const clipSeconds = useAutomation((s) => s.clipSeconds);
  const setClipSeconds = useAutomation((s) => s.setClipSeconds);
  const capabilities = useAutomation((s) => s.capabilities);
  const setImageModel = useAutomation((s) => s.setImageModel);
  const imageSize = useAutomation((s) => s.imageSize);
  const setImageSize = useAutomation((s) => s.setImageSize);
  const setAspectRatio = useAutomation((s) => s.setAspectRatio);
  const loadCapabilities = useAutomation((s) => s.loadCapabilities);
  const reset = useAutomation((s) => s.reset);
  const exportBoard = useAutomation((s) => s.exportBoard);
  const relayout = useAutomation((s) => s.relayout);
  const importBoard = useAutomation((s) => s.importBoard);
  const currentProjectId = useAutomation((s) => s.currentProjectId);
  const unmoderated = useAutomation((s) => s.unmoderated);
  const setUnmoderated = useAutomation((s) => s.setUnmoderated);
  const kyc = useAutomation((s) => s.kyc);
  const setKyc = useAutomation((s) => s.setKyc);
  const downloadClips = useAutomation((s) => s.downloadClips);
  const clipCount = useAutomation((s) => s.clipCount());
  const downloadPlates = useAutomation((s) => s.downloadPlates);
  const plateCount = useAutomation((s) => s.plateCount());
  const [workspaceTab, setWorkspaceTab] = useState<"canvas" | "primary">("canvas");
  const jobs = useAutomation(s => s.jobs);
  const reviewRun = jobs.find(j => j.kind === "production_run" && j.status === "paused" && j.result.stage === "master_review");
  const seenReview = useRef("");
  useEffect(() => { setWorkspaceTab("canvas"); seenReview.current = ""; }, [currentProjectId]);
  useEffect(() => {
    if (reviewRun && seenReview.current !== reviewRun.id) { seenReview.current = reviewRun.id; setWorkspaceTab("primary"); }
  }, [reviewRun?.id]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(true);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatSelection, setChatSelection] = useState<string[]>([]);
  const selectChatNodes = useCallback((ids: string[]) => setChatSelection(old => old.join("|") === ids.join("|") ? old : ids), []);
  useEffect(() => { setChatSelection([]); }, [currentProjectId]);
  const [importError, setImportError] = useState<string | null>(null);
  const cutAll = useAutomation((s) => s.cutAll);
  const cuttingAll = useAutomation((s) => s.cuttingAll);
  const seqCount = useAutomation((s) => s.nodes.filter((n) => n.data.kind === "sequence").length);
  const [zipping, setZipping] = useState(false);
  const [zippingPlates, setZippingPlates] = useState(false);
  const [boardView, setBoardView] = useState<"clips" | "nodes">(() => {
    try { return localStorage.getItem("flowboard:automation-view") === "nodes" ? "nodes" : "clips"; }
    catch { return "clips"; }
  });
  useEffect(() => {
    try { localStorage.setItem("flowboard:automation-view", boardView); }
    catch { /* View switching also works with storage disabled. */ }
  }, [boardView]);

  const loadVideos = useVideoAnalysis((s) => s.loadVideos);

  useEffect(() => {
    void loadCapabilities();
  }, [loadCapabilities]);

  // Reference videos belong to a board; switching boards switches the list.
  useEffect(() => {
    void loadVideos(currentProjectId);
  }, [loadVideos, currentProjectId]);

  const models = capabilities?.image_models ?? [imageModel];

  return (
    <div className="auto-shell">
      <AutomationSidebar />
      <div className="auto-page">
        <header className="auto-bar">
          <h1 className="auto-bar__title" title={title || "Chưa có phim"}>{title || "Chưa có phim"}</h1>
          <span className="auto-bar__summary">{seqCount} clip · {clipCount} video</span>
          <button type="button" className="auto-btn" aria-expanded={chatOpen} onClick={() => setChatOpen(!chatOpen)}>
            {chatBusy ? "◌ Agent đang làm" : "✧ Chat Agent"}
          </button>
          <button type="button" className="auto-btn auto-bar__settings" aria-haspopup="dialog"
            disabled={chatBusy} aria-expanded={settingsOpen} onClick={() => setSettingsOpen(true)}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" fill="var(--panel)" /><circle cx="15" cy="17" r="3" fill="var(--panel)" />
            </svg>
            Cài đặt
          </button>
        </header>
        {chatBusy && <div className="agent-work-status" role="status">Agent đang xử lý board này. Bạn có thể xem tiến độ hoặc dừng trong chat.</div>}
        <div className="auto-workbench" ref={element => { element?.toggleAttribute('inert', chatBusy); }} aria-busy={chatBusy}>
        {settingsOpen && <BoardSettings title={title} logline={logline} onClose={() => setSettingsOpen(false)}>
          <section className="auto-settings__section" aria-labelledby="auto-settings-generation">
            <h3 id="auto-settings-generation">Tạo hình & định dạng</h3>
            <div className="auto-settings__grid">
            <label className="auto-select" title="Sau khi đã gen tài sản, chọn style mới khi tải video vào project mới để giữ nguyên bản cũ.">
              <span>Style</span><select value={style} disabled={plateCount>0 || clipCount>0 || useAutomation.getState().autoSourceFilm || useAutomation.getState().jobs.some(j=>["queued","preparing","submitting","running"].includes(j.status))} onChange={e=>setStyle(e.target.value as typeof style)}>
                {!STYLE_PRESETS.some(p => p.key === style) && <option value={style} disabled hidden>Style cũ của project</option>}
                {STYLE_PRESETS.map(p=><option key={p.key} value={p.key}>{p.label}</option>)}
              </select>
            </label>
            <label className="auto-select">
              <span>Model ảnh</span>
              <select value={imageModel} onChange={(e) => setImageModel(e.target.value)}>
                {models.map((m) => (
                  <option key={m} value={m}>
                    {IMAGE_MODEL_LABELS[m] ?? m}
                    {capabilities?.image_model_max?.[m] ? ` · ${capabilities.image_model_max[m]}` : ""}
                  </option>
                ))}
              </select>
            </label>
            <label className="auto-select" title="Bị giới hạn theo model: Seedream tối đa 2K.">
              <span>Độ phân giải</span>
              <select value={imageSize} onChange={(e) => setImageSize(e.target.value)}>
                {(capabilities?.image_sizes ?? ["1K", "2K", "4K"]).map((sz) => (
                  <option key={sz} value={sz}>
                    {sz}
                  </option>
                ))}
              </select>
            </label>
            <label
              className="auto-select"
              title="Độ dài tối đa của một clip khi dựng board từ video mẫu. Seedance 2.5 nhận tới 30s — clip dài hơn nghĩa là ít mối nối hơn, nên nhân vật và ánh sáng ít trôi."
            >
              <span>Độ dài clip</span>
              <select value={clipSeconds} onChange={(e) => setClipSeconds(Number(e.target.value))}>
                {[10, 15, 20, 25, 30].map((sec) => (
                  <option key={sec} value={sec}>
                    ≤ {sec}s
                  </option>
                ))}
              </select>
            </label>
            <label className="auto-select">
              <span>Khung phim</span>
              <select value={aspectRatio} onChange={(e) => setAspectRatio(e.target.value)}>
                <option value="1:1">1:1 vuông</option>
                <option value="16:9">16:9 ngang</option>
                <option value="9:16">9:16 dọc</option>
              </select>
            </label>
            <label
              className="auto-check"
              title="Đường B2B không từ chối ảnh người thật. Tắt đi thì mọi sheet nhân vật đều bị kiểm duyệt chặn."
            >
              <input
                type="checkbox"
                checked={unmoderated}
                onChange={(e) => setUnmoderated(e.target.checked)}
              />
              <span>Người thật</span>
            </label>
            <label
              className="auto-check"
              title="Dùng identity asset KYC qua Avis, giữ thứ tự các reference nhân vật và bối cảnh của clip."
            >
              <input type="checkbox" checked={kyc} onChange={(e) => setKyc(e.target.checked)} />
              <span>KYC</span>
            </label>
            </div>
          </section>
          <section className="auto-settings__section" aria-labelledby="auto-settings-canvas">
            <h3 id="auto-settings-canvas">Canvas</h3>
            <div className="auto-settings__actions">
            <div className="auto-view-switch" role="group" aria-label="Cách hiển thị board">
              <button type="button" className="auto-btn" aria-pressed={boardView === "clips"} onClick={() => setBoardView("clips")}>Khung clip</button>
              <button type="button" className="auto-btn" aria-pressed={boardView === "nodes"} onClick={() => setBoardView("nodes")}>Node tự do</button>
            </div>
            <button
              type="button"
              className="auto-btn"
              title={boardView === "clips" ? "Xếp lại khung và node bên trong theo thứ tự clip." : "Xếp lại mọi node theo cột."}
              onClick={() => {
                if (boardView === "nodes") relayout();
                else useAutomation.setState(state => ({ nodes: state.nodes.map(n => {
                  const { clipLayout: _layout, ...rest } = n;
                  return rest;
                }) }));
                setSettingsOpen(false);
              }}
            >
        {boardView === "clips" ? "Xếp lại khung" : "Xếp lại node"}
            </button>
            </div>
          </section>
          <section className="auto-settings__section" aria-labelledby="auto-settings-assets">
            <h3 id="auto-settings-assets">Clip & material</h3>
            <div className="auto-settings__actions">
            <button
              type="button"
              className="auto-btn auto-btn--primary"
              disabled={!seqCount || cuttingAll}
              title="Cắt lần lượt từ đầu tới cuối, mỗi sequence nối vào trạng thái kết của sequence trước. Cắt lẻ từng cái sẽ đứt mạch."
              onClick={() => void cutAll()}
            >
              {cuttingAll ? "đang cắt…" : `Cắt hết (${seqCount})`}
            </button>
            <button
              type="button"
              className="auto-btn"
              disabled={!clipCount || zipping}
              title={
                clipCount
                  ? "Nén mọi clip đã gen, đánh số theo đúng thứ tự phim."
                  : "Chưa có clip nào."
              }
              onClick={async () => {
                setZipping(true);
                setImportError(null);
                try {
                  await downloadClips();
                } catch (err) {
                  setImportError((err as Error).message);
                } finally {
                  setZipping(false);
                }
              }}
            >
              {zipping ? "đang nén…" : `Tải video (${clipCount})`}
            </button>
            <button
              type="button"
              className="auto-btn"
              disabled={!plateCount || zippingPlates}
              title={
                plateCount
                  ? "Tải mọi sheet nhân vật và plate bối cảnh. Mỗi file mang đúng tên nhân vật hoặc bối cảnh của nó."
                  : "Chưa gen ảnh nhân vật hay bối cảnh nào."
              }
              onClick={async () => {
                setZippingPlates(true);
                setImportError(null);
                try {
                  await downloadPlates();
                } catch (err) {
                  setImportError((err as Error).message);
                } finally {
                  setZippingPlates(false);
                }
              }}
            >
              {zippingPlates ? "đang nén…" : `Tải tạo hình (${plateCount})`}
            </button>
            </div>
          </section>
          <section className="auto-settings__section" aria-labelledby="auto-settings-files">
            <h3 id="auto-settings-files">Dữ liệu board</h3>
            <div className="auto-settings__actions">
            <button type="button" className="auto-btn" onClick={exportBoard}>
              Xuất file
            </button>
            <label className="auto-btn auto-btn--file">
              Nhập file
              <input
                type="file"
                accept="application/json,.json"
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  e.target.value = ""; // so re-picking the same file fires again
                  if (!file) return;
                  try {
                    importBoard(await file.text());
                    setImportError(null);
                  } catch (err) {
                    setImportError((err as Error).message);
                  }
                }}
              />
            </label>
            <button
              type="button"
              className="auto-btn auto-settings__danger"
              onClick={() => {
                if (confirm("Xoá sạch board hiện tại? Xuất file trước nếu muốn giữ.")) reset();
              }}
            >
              Xoá board
            </button>
            </div>
          </section>
          <section className="auto-settings__section"><ProductionControls /></section>
          {importError && <p className="auto-banner auto-banner--stop" role="alert">{importError}</p>}
        </BoardSettings>}
        <ProductionPanel />

        {importError && <p className="auto-banner auto-banner--stop">{importError}</p>}
        {!currentProjectId && (
          <p className="auto-banner">
            Chưa mở board nào — công việc chỉ nằm trong trình duyệt và{" "}
            <b>không được lưu lên server</b>. Tạo một board ở cột trái để nó tự lưu.
          </p>
        )}
        {capabilities && !capabilities.atrium_configured && (
          <p className="auto-banner auto-banner--stop">
            Atrium chưa cấu hình — phân tích kịch bản vẫn chạy, nhưng chưa gen được ảnh nào.
            Đặt <code>ATRIUM_CLIENT_ID</code> và <code>ATRIUM_CLIENT_SECRET</code> trong <code>.env</code>.
          </p>
        )}
        {capabilities?.atrium_configured && !capabilities.reference_chain && (
          <p className="auto-banner">
            R2 chưa cấu hình nên <b>chuỗi identity không chạy</b>: Atrium chỉ đọc ref qua URL public,
            nên mỗi sheet sẽ gen ra một khuôn mặt khác. Ảnh vẫn ra, nhưng đừng dùng cho nhân vật.
          </p>
        )}

              <nav className="auto-workspace-tabs" aria-label="Không gian làm việc">
          <button type="button" aria-pressed={workspaceTab === "canvas"} onClick={() => setWorkspaceTab("canvas")}>Canvas</button>
          <button type="button" aria-pressed={workspaceTab === "primary"} onClick={() => setWorkspaceTab("primary")}>Tạo hình chính {reviewRun && <span>Chờ chốt</span>}</button>
        </nav>
        {workspaceTab === "primary" && <PrimaryMaterials key={currentProjectId ?? "draft"} />}
        <div className="auto-canvas-workspace" style={{ display: workspaceTab === "canvas" ? "flex" : "none" }}>
        {boardView === "clips" ? <ClipCanvas key={currentProjectId ?? "draft"} onSelection={selectChatNodes} /> : <div className="auto-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={automationNodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            fitView
            minZoom={0.2}
            proOptions={{ hideAttribution: true }}
            // Alt + drag draws a selection box (plain drag still pans, which is
            // what a board this wide needs); anything the box touches is
            // selected, and dragging one selected node moves the whole set.
            selectionKeyCode="Alt"
            selectionMode={SelectionMode.Partial}
            multiSelectionKeyCode={["Meta", "Control", "Shift"]}
            panOnDrag
            selectNodesOnDrag={false}
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>}
        </div>
        <VideoAnalysisPanel />
        </div>
      </div>
      <AutomationAssistant selectedIds={boardView === "clips" ? chatSelection : undefined} open={chatOpen} onClose={() => setChatOpen(false)} onBusy={setChatBusy} />
    </div>
  );
}
