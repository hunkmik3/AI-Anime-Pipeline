/**
 * /automation — the drama-film pipeline.
 *
 * A premise becomes a cast, a set of places and a run of sequences. The cast
 * and places generate their reference plates; each sequence is cut into shots
 * and then becomes one clip, wired on the board to the plates it consumes.
 *
 * Its own board, not the shot canvas. Nothing here touches a real project.
 */
import { useEffect, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  SelectionMode,
} from "@xyflow/react";

import { ProductionPanel } from "../automation/ProductionPanel";
import { AutomationSidebar } from "../automation/AutomationSidebar";
import { automationNodeTypes } from "../automation/nodes";
import { VideoAnalysisPanel } from "../automation/VideoAnalysisPanel";
import { IMAGE_MODEL_LABELS, useAutomation } from "../store/automation";
import { useVideoAnalysis } from "../store/videoAnalysis";

export function AutomationPage() {
  const nodes = useAutomation((s) => s.nodes);
  const edges = useAutomation((s) => s.edges);
  const onNodesChange = useAutomation((s) => s.onNodesChange);
  const onEdgesChange = useAutomation((s) => s.onEdgesChange);
  const onConnect = useAutomation((s) => s.onConnect);

  const title = useAutomation((s) => s.title);
  const logline = useAutomation((s) => s.logline);
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
  const [importError, setImportError] = useState<string | null>(null);
  const cutAll = useAutomation((s) => s.cutAll);
  const cuttingAll = useAutomation((s) => s.cuttingAll);
  const seqCount = useAutomation((s) => s.nodes.filter((n) => n.data.kind === "sequence").length);
  const [zipping, setZipping] = useState(false);
  const [zippingPlates, setZippingPlates] = useState(false);

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
          <div className="auto-bar__id">
            <span className="auto-bar__eyebrow">Automation · production</span>
            <h1 className="auto-bar__title">{title || "Chưa có phim"}</h1>
            {logline && <p className="auto-bar__logline">{logline}</p>}
          </div>

          <div className="auto-bar__controls">
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
              <span>Khung bối cảnh</span>
              <select value={aspectRatio} onChange={(e) => setAspectRatio(e.target.value)}>
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
              title="Khoá mặt bằng KYC identity asset của Avis. Mạnh hơn ref thường, nhưng nhà cung cấp bỏ hết ref còn lại — plate bối cảnh không tới được model, chỉ còn mô tả bằng chữ."
            >
              <input type="checkbox" checked={kyc} onChange={(e) => setKyc(e.target.checked)} />
              <span>KYC</span>
            </label>
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
            <button
              type="button"
              className="auto-btn"
              title="Xếp lại mọi node theo cột, giãn đủ chiều cao để không đè lên nhau. Alt + kéo để khoanh chọn nhiều node."
              onClick={relayout}
            >
              Xếp lại node
            </button>
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
              className="auto-btn"
              onClick={() => {
                if (confirm("Xoá sạch board hiện tại? Xuất file trước nếu muốn giữ.")) reset();
              }}
            >
              Xoá board
            </button>
          </div>
        </header>
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

        <div className="auto-canvas">
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
        </div>
        <VideoAnalysisPanel />
      </div>
    </div>
  );
}
