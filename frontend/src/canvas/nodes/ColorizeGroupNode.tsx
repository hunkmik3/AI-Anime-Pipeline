import { memo, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { NodeResizer, type NodeProps } from "@xyflow/react";

import {
  colorizeUploadPage,
  downloadColorizeZip,
  downloadUpscaleImage,
  getShot,
  mediaUrl,
  patchShot,
  patchShotGroup,
  runRequestToCompletion,
  thumbUrl,
  type Box4,
} from "../../api/client";
import { useShotWorkflowStore } from "../../store/shotWorkflow";

/**
 * A SceneCanvas "sequence" whose kind is "colorize": the read-first, color-locked
 * MANGA COLORIZER embedded on the episode canvas as one block, in the spirit of
 * the Upscale sequence. INPUT = B&W pages → (build a colour BIBLE → character
 * SHEETS → colorize) → RESULT = colour pages. Per page: pick a variant, compare,
 * or open the Fix editor (panel / object / SAM-click / drag → re-colorize or
 * free-text edit ONE region).
 *
 * One sequence = one chapter. Its state (pages / bible / sheets / outputs) lives
 * in the Shot's ``workflow_metadata.colorize`` (getShot/patchShot). The heavy jobs
 * run through the worker queue (POST /api/requests type "colorize_*").
 */

const MIN_W = 780;
const MIN_H = 520;
// Fix-vùng (region editor, dạng gọn: SAM click detect + ô prompt → gen lại vùng).
// Đổi thành false để ẩn nút "✎ Fix vùng" trên mỗi trang đã tô.
const FIX_REGION_ENABLED: boolean = true;

type PStatus = "uploading" | "ready" | "error";
interface Page {
  key: string;
  file?: File;
  previewUrl: string;
  name: string;
  mediaId?: string;
  status: PStatus;
  error?: string;
}
interface PPage {
  key: string;
  mediaId: string;
  name: string;
}
interface Output {
  variants: string[]; // result media ids (newest last)
  current: number;
}
type Bible = Record<string, any>;

export interface ColorizeGroupData extends Record<string, unknown> {
  shotId: string;
  label: string;
  onDelete?: () => void;
  onResize?: () => void;
}

let _seq = 0;
const nextKey = () => `c${Date.now().toString(36)}_${_seq++}`;
const baseName = (n: string) => n.replace(/\.[^.]+$/, "").trim() || "page";

async function runPool<T>(items: T[], worker: (t: T) => Promise<void>, concurrency: number) {
  let i = 0;
  const lane = async () => {
    while (i < items.length) await worker(items[i++]);
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, lane));
}

function toPage(p: PPage): Page {
  return { key: p.key || nextKey(), name: p.name || "page", mediaId: p.mediaId, status: "ready", previewUrl: thumbUrl(p.mediaId, 240) };
}
function toPersisted(pages: Page[]): PPage[] {
  return pages.filter((p) => p.mediaId).map((p) => ({ key: p.key, mediaId: p.mediaId!, name: p.name }));
}
const curOut = (o?: Output): string | undefined => (o && o.variants.length ? o.variants[Math.min(o.current, o.variants.length - 1)] : undefined);

const S = {
  root: { width: "100%", height: "100%", display: "flex", flexDirection: "column" } as React.CSSProperties,
  btn: { padding: "6px 11px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--panel-high)",
    color: "var(--text)", fontWeight: 600, fontSize: 12, cursor: "pointer", whiteSpace: "nowrap" } as React.CSSProperties,
  primary: { border: "1px solid var(--accent)", background: "var(--accent)", color: "#06122b" } as React.CSSProperties,
  warn: { border: "1px solid #e6a23a", color: "#f2b23a" } as React.CSSProperties,
  body: { flex: 1, display: "grid", gridTemplateColumns: "1fr 118px 1fr", gap: 14, padding: "12px 16px 16px", minHeight: 0 } as React.CSSProperties,
  panel: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, border: "1px solid var(--border)",
    borderRadius: 10, background: "color-mix(in srgb, var(--panel) 72%, #000)", overflow: "hidden" } as React.CSSProperties,
  panelHead: { padding: "8px 13px", fontSize: 11.5, letterSpacing: ".14em", fontWeight: 700, color: "var(--muted)",
    borderBottom: "1px solid var(--border)" } as React.CSSProperties,
  gridScroll: { flex: 1, overflow: "auto", padding: 12 } as React.CSSProperties,
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))", gap: 12 } as React.CSSProperties,
  tile: { position: "relative", aspectRatio: "3 / 4", borderRadius: 9, overflow: "hidden", border: "1px solid var(--border)",
    background: "var(--panel-high)", contentVisibility: "auto", containIntrinsicSize: "210px 280px" } as React.CSSProperties,
  mid: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8, color: "var(--accent)",
    borderRadius: 12, padding: "10px 6px",
    background: "linear-gradient(180deg, transparent 4%, color-mix(in srgb, var(--accent) 16%, transparent) 50%, transparent 96%)",
    border: "1px solid color-mix(in srgb, var(--accent) 28%, transparent)" } as React.CSSProperties,
  empty: { color: "var(--muted)", fontSize: 13.5, textAlign: "center", padding: 28 } as React.CSSProperties,
  modalWrap: { position: "fixed", inset: 0, zIndex: 1000, background: "rgba(6,8,12,.82)", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 } as React.CSSProperties,
  modalCard: { background: "#161a23", border: "1px solid #262d3b", borderRadius: 16, width: "min(1040px,95vw)", maxHeight: "90vh", overflow: "auto", padding: 20 } as React.CSSProperties,
  swatch: { width: 30, height: 26, padding: 0, border: "1px solid #333", borderRadius: 6, background: "none", cursor: "pointer" } as React.CSSProperties,
};

function tileImg(mediaId: string | undefined, blob?: string) {
  return (
    <img src={blob || (mediaId ? thumbUrl(mediaId, 512) : "")} alt="" draggable={false} loading="lazy" decoding="async"
      style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
  );
}

function ColorizeGroupNodeImpl({ data, selected }: NodeProps) {
  const d = data as ColorizeGroupData;
  const shotId = d.shotId;

  const [pages, setPages] = useState<Page[]>([]);
  const [chapterName, setChapterName] = useState("");
  const [styleRefMediaId, setStyleRefMediaId] = useState<string | undefined>();
  const [bible, setBible] = useState<Bible | undefined>();
  const [summary, setSummary] = useState<string>("");
  const [sheets, setSheets] = useState<Record<string, string>>({});
  const [outputs, setOutputs] = useState<Record<string, Output>>({});
  const [variantCount, setVariantCount] = useState(1);

  const [busy, setBusy] = useState<null | "bible" | "sheets">(null);
  const [coloring, setColoring] = useState<Set<string>>(new Set());
  const [progress, setProgress] = useState<string>("");
  const [zipping, setZipping] = useState(false);

  const [showBible, setShowBible] = useState(false);
  const [showSheets, setShowSheets] = useState(false);
  const [cmp, setCmp] = useState<{ pageMediaId: string; currentOutput: string; title: string; pageIndex: number; allPageMediaIds: string[] } | null>(null);

  const pagesInput = useRef<HTMLInputElement>(null);
  const styleInput = useRef<HTMLInputElement>(null);

  const loaded = useRef(false);
  const wfRef = useRef<Record<string, unknown>>({});
  const pagesRef = useRef(pages);
  pagesRef.current = pages;
  const stateRef = useRef({ chapterName, styleRefMediaId, bible, summary, sheets, outputs, variantCount });
  stateRef.current = { chapterName, styleRefMediaId, bible, summary, sheets, outputs, variantCount };
  const saveTimer = useRef<number | null>(null);

  const patchPage = useCallback((key: string, p: Partial<Page>) => {
    setPages((prev) => prev.map((it) => (it.key === key ? { ...it, ...p } : it)));
  }, []);

  // ── load ────────────────────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    getShot(shotId)
      .then((shot) => {
        if (!alive) return;
        const wf = (shot.workflow_metadata || {}) as Record<string, unknown>;
        wfRef.current = wf;
        loaded.current = true;
        if (pagesRef.current.length) return; // don't clobber optimistic uploads
        const cz = (wf.colorize || {}) as Record<string, any>;
        const raw = Array.isArray(cz.pages) ? (cz.pages as PPage[]) : [];
        setPages(raw.filter((p) => p && p.mediaId).map(toPage));
        setChapterName(cz.chapterName || "");
        setStyleRefMediaId(cz.styleRefMediaId || undefined);
        setBible(cz.bible || undefined);
        setSummary(cz.summary || "");
        setSheets(cz.sheets && typeof cz.sheets === "object" ? cz.sheets : {});
        setOutputs(cz.outputs && typeof cz.outputs === "object" ? cz.outputs : {});
        setVariantCount(Math.min(4, Math.max(1, cz.variantCount || 1)));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shotId]);

  // ── persist (debounced) ───────────────────────────────────────────────────
  useEffect(() => {
    if (!loaded.current) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const st = stateRef.current;
      void patchShot(shotId, {
        workflow_metadata: {
          ...wfRef.current,
          kind: "colorize",
          colorize: {
            chapterName: st.chapterName,
            pages: toPersisted(pagesRef.current),
            styleRefMediaId: st.styleRefMediaId,
            bible: st.bible,
            summary: st.summary,
            sheets: st.sheets,
            outputs: st.outputs,
            variantCount: st.variantCount,
          },
        },
      }).catch(() => {});
    }, 600);
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, [pages, chapterName, styleRefMediaId, bible, summary, sheets, outputs, variantCount, shotId]);

  // ── page upload-on-add ────────────────────────────────────────────────────
  const uploadOne = useCallback(
    async (it: Page) => {
      if (!it.file) return;
      patchPage(it.key, { status: "uploading", error: undefined });
      try {
        const r = await colorizeUploadPage(it.file);
        if (it.previewUrl.startsWith("blob:")) URL.revokeObjectURL(it.previewUrl);
        patchPage(it.key, { status: "ready", mediaId: r.media_id, file: undefined, previewUrl: thumbUrl(r.media_id, 512) });
      } catch (e) {
        patchPage(it.key, { status: "error", error: `upload lỗi: ${e instanceof Error ? e.message : e}` });
      }
    },
    [patchPage],
  );

  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      const added: Page[] = [];
      for (const f of Array.from(files)) {
        if (!f.type.startsWith("image/")) continue;
        added.push({ key: nextKey(), file: f, previewUrl: URL.createObjectURL(f), name: baseName(f.name), status: "uploading" });
      }
      if (!added.length) return;
      setPages((prev) => [...prev, ...added]);
      void runPool(added, uploadOne, 4);
    },
    [uploadOne],
  );

  const onStyleRef = useCallback(async (files: FileList | null) => {
    const f = files?.[0];
    if (!f || !f.type.startsWith("image/")) return;
    try {
      const r = await colorizeUploadPage(f);
      setStyleRefMediaId(r.media_id);
    } catch (e) {
      alert(`Style ref lỗi: ${e instanceof Error ? e.message : e}`);
    }
  }, []);

  const removePage = useCallback((key: string) => {
    setPages((prev) => {
      const it = prev.find((x) => x.key === key);
      if (it?.file) URL.revokeObjectURL(it.previewUrl);
      return prev.filter((x) => x.key !== key);
    });
  }, []);

  const allReady = pages.length > 0 && pages.every((p) => p.mediaId);
  const pageMediaIds = useCallback(() => pagesRef.current.map((p) => p.mediaId).filter(Boolean) as string[], []);

  // ── PASS 1: build bible ───────────────────────────────────────────────────
  const buildBible = useCallback(async () => {
    if (!allReady || busy) return;
    setBusy("bible");
    setProgress("Đang đọc chương → dựng bible…");
    try {
      const res = await runRequestToCompletion("colorize_build_bible", {
        page_media_ids: pageMediaIds(),
        chapter_name: stateRef.current.chapterName,
        style_ref_media_id: stateRef.current.styleRefMediaId,
      });
      const b = res.result.bible as Bible;
      setBible(b);
      setSummary((res.result.summary as string) || "");
      setShowBible(true); // human checkpoint
    } catch (e) {
      alert(`Build bible lỗi: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
      setProgress("");
    }
  }, [allReady, busy, pageMediaIds]);

  // ── PASS 1.5: build sheets ────────────────────────────────────────────────
  const buildSheets = useCallback(async () => {
    if (!bible || busy) return;
    setBusy("sheets");
    setProgress("Đang dựng character sheets…");
    try {
      const res = await runRequestToCompletion("colorize_build_sheets", {
        page_media_ids: pageMediaIds(),
        bible: stateRef.current.bible,
      });
      const sh = (res.result.sheets as Record<string, string>) || {};
      setSheets((prev) => ({ ...prev, ...sh }));
      if (Object.keys(sh).length) setShowSheets(true);
      else alert("Không dựng được sheet nào (không có nhân vật xuất hiện đủ nhiều).");
    } catch (e) {
      alert(`Build sheets lỗi: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
      setProgress("");
    }
  }, [bible, busy, pageMediaIds]);

  // ── PASS 2: colorize page(s) ──────────────────────────────────────────────
  const colorizeOne = useCallback(async (p: Page) => {
    if (!p.mediaId || !stateRef.current.bible) return;
    const mid = p.mediaId;
    setColoring((s) => new Set(s).add(mid));
    try {
      const ids = pageMediaIds();
      const idx = ids.indexOf(mid);
      const res = await runRequestToCompletion("colorize_page", {
        page_media_ids: ids,
        page_index: idx,
        bible: stateRef.current.bible,
        sheets: stateRef.current.sheets,
        style_ref_media_id: stateRef.current.styleRefMediaId,
        variant_count: stateRef.current.variantCount,
      });
      const vids = (res.result.variant_media_ids as string[]) || [];
      if (vids.length) setOutputs((o) => ({ ...o, [mid]: { variants: [...(o[mid]?.variants || []), ...vids], current: (o[mid]?.variants || []).length } }));
    } catch (e) {
      alert(`Tô trang lỗi: ${e instanceof Error ? e.message : e}`);
    } finally {
      setColoring((s) => {
        const n = new Set(s);
        n.delete(mid);
        return n;
      });
    }
  }, [pageMediaIds]);

  const colorizeAll = useCallback(() => {
    if (!bible) return;
    const targets = pagesRef.current.filter((p) => p.mediaId && !curOut(stateRef.current.outputs[p.mediaId!]));
    void runPool(targets, colorizeOne, 4);
  }, [bible, colorizeOne]);

  const downloadAll = useCallback(async () => {
    const items = pagesRef.current
      .map((p, i) => {
        const out = p.mediaId ? curOut(stateRef.current.outputs[p.mediaId]) : undefined;
        return out ? { media_id: out, name: `${String(i + 1).padStart(3, "0")}_${p.name}` } : null;
      })
      .filter(Boolean) as { media_id: string; name: string }[];
    if (!items.length) return;
    setZipping(true);
    try {
      await downloadColorizeZip(items, stateRef.current.chapterName || "chapter");
    } catch (e) {
      alert(`Download lỗi: ${e instanceof Error ? e.message : e}`);
    } finally {
      setZipping(false);
    }
  }, []);

  const setVariant = useCallback((mid: string, idx: number) => {
    setOutputs((o) => (o[mid] ? { ...o, [mid]: { ...o[mid], current: idx } } : o));
  }, []);
  const applyFixResult = useCallback((mid: string, newId: string) => {
    setOutputs((o) => {
      const cur = o[mid] || { variants: [], current: 0 };
      return { ...o, [mid]: { variants: [...cur.variants, newId], current: cur.variants.length } };
    });
  }, []);

  const doneCount = pages.filter((p) => p.mediaId && curOut(outputs[p.mediaId])).length;
  const uncolored = pages.filter((p) => p.mediaId && !curOut(outputs[p.mediaId])).length;

  return (
    <div className="shot-group" style={S.root}>
      <NodeResizer
        minWidth={MIN_W}
        minHeight={MIN_H}
        isVisible={selected}
        onResizeEnd={(_e, p) => {
          const size = { w: Math.round(p.width), h: Math.round(p.height) };
          useShotWorkflowStore.getState().updateShotGroupLocal(shotId, { size });
          void patchShotGroup(shotId, { size }).catch(() => {});
          d.onResize?.();
        }}
      />
      <input ref={pagesInput} type="file" accept="image/*" multiple style={{ display: "none" }}
        onChange={(e) => { onFiles(e.target.files); e.target.value = ""; }} />
      <input ref={styleInput} type="file" accept="image/*" style={{ display: "none" }}
        onChange={(e) => { void onStyleRef(e.target.files); e.target.value = ""; }} />

      {/* header — drag handle + pipeline actions */}
      <div className="shot-group__header colorize-drag" style={{ cursor: "grab", flexWrap: "wrap", rowGap: 6 }}>
        <span className="shot-group__label" title="Sequence Colorize (tô màu manga)">
          <span className="shot-group__num">{d.label}</span>
          <span className="shot-group__scene"> — 🎨 Colorize</span>
        </span>
        <span className="shot-group__badge">{doneCount}/{pages.length}</span>
        <span style={{ flex: 1 }} />
        <button className="nodrag" style={S.btn} onClick={() => pagesInput.current?.click()} title="Thêm trang B&W">＋ Trang</button>
        <button className="nodrag" style={{ ...S.btn, ...(styleRefMediaId ? S.warn : {}) }} onClick={() => styleInput.current?.click()} title="Ảnh style tham chiếu (tuỳ chọn)">
          {styleRefMediaId ? "★ Style" : "☆ Style"}
        </button>
        <button className="nodrag" style={{ ...S.btn, ...S.primary, opacity: allReady && !busy ? 1 : 0.5 }} disabled={!allReady || !!busy}
          onClick={() => void buildBible()} title="Đọc cả chương → bảng màu (bible)">
          {busy === "bible" ? "⏳ Bible…" : bible ? "↻ Bible" : "🎨 Build Bible"}
        </button>
        {bible ? <button className="nodrag" style={S.btn} onClick={() => setShowBible(true)}>✎ Bible</button> : null}
        <button className="nodrag" style={{ ...S.btn, opacity: bible && !busy ? 1 : 0.5 }} disabled={!bible || !!busy}
          onClick={() => void buildSheets()} title="Dựng character sheet (khoá màu áo/chibi)">
          {busy === "sheets" ? "⏳ Sheets…" : Object.keys(sheets).length ? `👤 Sheets (${Object.keys(sheets).length})` : "👤 Sheets"}
        </button>
        <select className="nodrag" style={{ ...S.btn, padding: "5px 6px" }} value={variantCount} title="Số biến thể mỗi trang"
          onChange={(e) => setVariantCount(Math.min(4, Math.max(1, parseInt(e.target.value) || 1)))}>
          {[1, 2, 3, 4].map((n) => <option key={n} value={n}>×{n}</option>)}
        </select>
        <button className="nodrag" style={{ ...S.btn, ...S.primary, opacity: bible && uncolored ? 1 : 0.5 }} disabled={!bible || !uncolored}
          onClick={colorizeAll} title="Tô tất cả trang chưa tô">
          ▶ Tô tất cả ({uncolored})
        </button>
        <button className="nodrag" style={{ ...S.btn, opacity: doneCount && !zipping ? 1 : 0.5 }} disabled={!doneCount || zipping}
          onClick={() => void downloadAll()}>{zipping ? "Zipping…" : `⬇ ZIP (${doneCount})`}</button>
        {d.onDelete ? (
          <button className="shot-group__delete nodrag" title="Xoá sequence" aria-label="Xoá sequence" onClick={() => d.onDelete?.()}>✕</button>
        ) : null}
      </div>

      {/* body: INPUT (B&W) | COLORIZE | RESULT (màu) */}
      <div style={S.body}>
        {/* INPUT */}
        <div style={S.panel}>
          <div style={S.panelHead}>INPUT · B&W ({pages.length})</div>
          <div style={S.gridScroll} className="nodrag">
            {!pages.length ? (
              <div style={S.empty}>Bấm ＋ Trang để tải các trang manga B&W (đúng thứ tự đọc)</div>
            ) : (
              <div style={S.grid}>
                {pages.map((it) => (
                  <div key={it.key} style={S.tile} title={it.name}>
                    {tileImg(it.mediaId, it.file ? it.previewUrl : undefined)}
                    <button onClick={() => removePage(it.key)} title="Bỏ"
                      style={{ position: "absolute", top: 6, right: 6, width: 22, height: 22, borderRadius: 6, border: "none",
                        background: "rgba(0,0,0,.62)", color: "#fff", cursor: "pointer", fontSize: 13, lineHeight: "20px", padding: 0 }}>✕</button>
                    {it.status === "uploading" ? (
                      <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,.45)", fontSize: 12.5, color: "#e6e9f0" }}>đang lưu…</span>
                    ) : it.status === "error" ? (
                      <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(120,20,20,.5)", fontSize: 11.5, color: "#ffbcbc", padding: 6, textAlign: "center" }}>{it.error}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* middle */}
        <div style={S.mid}>
          <svg viewBox="0 0 40 24" width="40" fill="none" style={{ filter: "drop-shadow(0 0 7px color-mix(in srgb, var(--accent) 75%, transparent))" }}>
            <path d="M2 12h30M24 5l9 7-9 7" stroke="currentColor" strokeWidth="4.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span style={{ fontSize: 12, fontWeight: 800, color: "var(--accent)", letterSpacing: ".16em", textShadow: "0 0 9px color-mix(in srgb, var(--accent) 60%, transparent)" }}>COLORIZE</span>
          {progress ? <span style={{ fontSize: 10.5, color: "var(--muted)", textAlign: "center", lineHeight: 1.35 }}>{progress}</span> : null}
          {bible ? <span style={{ fontSize: 10, color: "#3ecf8e", textAlign: "center" }}>✓ bible</span> : null}
        </div>

        {/* RESULT */}
        <div style={S.panel}>
          <div style={{ ...S.panelHead, color: "var(--accent)" }}>RESULT · MÀU</div>
          <div style={S.gridScroll} className="nodrag">
            {!pages.length ? (
              <div style={S.empty}>—</div>
            ) : (
              <div style={S.grid}>
                {pages.map((it) => {
                  const out = it.mediaId ? outputs[it.mediaId] : undefined;
                  const v = curOut(out);
                  const running = it.mediaId ? coloring.has(it.mediaId) : false;
                  return (
                    <div key={it.key} style={S.tile} title={it.name}>
                      {v ? tileImg(v) : null}
                      {v && out && out.variants.length > 1 && !running ? (
                        <span style={{ position: "absolute", top: 6, right: 6, fontWeight: 700, fontSize: 10.5, padding: "2px 6px", borderRadius: 5, background: "var(--accent)", color: "#06122b" }}>v{out.current + 1}/{out.variants.length}</span>
                      ) : null}
                      {running ? (
                        // Loading shows IN this tile — over the old image on a redo — not just the middle column.
                        <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
                          background: v ? "rgba(6,9,14,.62)" : "transparent", color: "var(--accent)", fontSize: 12.5, fontWeight: 600, textShadow: "0 1px 3px #000" }}>
                          ⏳ {v ? "đang gen lại…" : "đang tô…"}
                        </span>
                      ) : v ? (
                        <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 6, background: "rgba(6,9,14,.5)", opacity: 0, transition: "opacity .13s" }}
                          onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")} onMouseLeave={(e) => (e.currentTarget.style.opacity = "0")}>
                          <div style={{ display: "flex", gap: 6 }}>
                            <button style={{ ...S.btn, ...S.primary, padding: "4px 10px" }} title="Phóng to + sửa vùng (Detect vật thể kiểu Grok)"
                              onClick={() => { const ids = pageMediaIds(); setCmp({ pageMediaId: it.mediaId!, currentOutput: v, title: it.name, pageIndex: ids.indexOf(it.mediaId!), allPageMediaIds: ids }); }}>⤢ Xem/Sửa</button>
                            <button style={{ ...S.btn, padding: "4px 9px" }} onClick={() => void downloadUpscaleImage(v, `${it.name}_color`)}>⬇</button>
                            <button style={{ ...S.btn, padding: "4px 9px" }} title="Tô lại" onClick={() => void colorizeOne(it)}>↻</button>
                          </div>
                          {out && out.variants.length > 1 ? (
                            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", justifyContent: "center", maxWidth: "90%" }}>
                              {out.variants.map((_, idx) => (
                                <button key={idx} onClick={() => setVariant(it.mediaId!, idx)}
                                  style={{ ...S.btn, padding: "2px 7px", fontSize: 11, ...(idx === out.current ? S.primary : {}) }}>{idx + 1}</button>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      ) : bible ? (
                        <button onClick={() => void colorizeOne(it)}
                          style={{ position: "absolute", inset: 0, border: "none", background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: "var(--accent)", fontSize: 13, fontWeight: 700, cursor: "pointer" }}>🎨 Tô trang</button>
                      ) : (
                        <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, color: "var(--muted)", textAlign: "center", padding: 8 }}>Dựng Bible trước</span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {showBible && bible ? createPortal(
        <BibleEditor bible={bible} summary={summary} chapterName={chapterName} onChapterName={setChapterName}
          onSave={(b) => { setBible(b); setShowBible(false); }} onClose={() => setShowBible(false)} />, document.body) : null}
      {showSheets ? createPortal(
        <SheetsViewer sheets={sheets} bible={bible} onClose={() => setShowSheets(false)} />, document.body) : null}
      {cmp ? createPortal(
        <CompareOverlay {...cmp} bible={bible} sheets={sheets}
          onApplied={(newId) => applyFixResult(cmp.pageMediaId, newId)}
          onClose={() => setCmp(null)} />, document.body) : null}
    </div>
  );
}

export const ColorizeGroupNode = memo(
  ColorizeGroupNodeImpl,
  (a, b) => a.data === b.data && a.selected === b.selected && a.width === b.width && a.height === b.height,
);

// ── Bible editor (the human checkpoint) ───────────────────────────────────────
function BibleEditor({ bible, summary, chapterName, onChapterName, onSave, onClose }: {
  bible: Bible; summary: string; chapterName: string; onChapterName: (s: string) => void;
  onSave: (b: Bible) => void; onClose: () => void;
}) {
  const [draft, setDraft] = useState<Bible>(() => JSON.parse(JSON.stringify(bible)));
  const [raw, setRaw] = useState(false);
  const [rawText, setRawText] = useState("");

  const chars: Record<string, any> = draft.characters || {};
  const outfits: Record<string, any> = draft.outfits || {};
  const scenes: Record<string, any> = draft.scenes || {};

  const setChar = (id: string, field: string, val: string) =>
    setDraft((d) => ({ ...d, characters: { ...d.characters, [id]: { ...d.characters[id], [field]: val } } }));
  const setOutfit = (id: string, field: string, val: string) =>
    setDraft((d) => ({ ...d, outfits: { ...d.outfits, [id]: { ...d.outfits[id], [field]: val } } }));

  const commit = () => {
    if (raw) {
      try { onSave(JSON.parse(rawText)); return; } catch { alert("JSON không hợp lệ"); return; }
    }
    onSave(draft);
  };

  const hex = (v: any) => (typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v) ? v : "#808080");
  const ColorField = ({ label, val, on }: { label: string; val: any; on: (v: string) => void }) => (
    <label style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11.5, color: "#b9c2d4" }}>
      <input type="color" value={hex(val)} onChange={(e) => on(e.target.value.toUpperCase())} style={S.swatch} />
      {label}
    </label>
  );

  return (
    <div className="nodrag nowheel" style={S.modalWrap} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }} onWheel={(e) => e.stopPropagation()}>
      <div style={S.modalCard} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <b style={{ fontSize: 17 }}>📖 Color Bible</b>
          <input value={chapterName} onChange={(e) => onChapterName(e.target.value)} placeholder="Tên chương…"
            style={{ ...S.btn, cursor: "text", fontWeight: 500, minWidth: 160 }} />
          <span style={{ flex: 1 }} />
          <button style={S.btn} onClick={() => { setRaw((r) => { const nx = !r; if (nx) setRawText(JSON.stringify(draft, null, 2)); return nx; }); }}>{raw ? "◧ Bảng" : "{} JSON"}</button>
          <button style={{ ...S.btn, ...S.primary }} onClick={commit}>✓ Lưu bible</button>
          <button style={S.btn} onClick={onClose}>Đóng</button>
        </div>
        {summary ? <pre style={{ fontSize: 11.5, color: "#8b94a7", whiteSpace: "pre-wrap", margin: "0 0 12px", fontFamily: "'JetBrains Mono',monospace" }}>{summary}</pre> : null}

        {raw ? (
          <textarea value={rawText} onChange={(e) => setRawText(e.target.value)} spellCheck={false}
            style={{ width: "100%", height: 480, background: "#0f131b", color: "#cfe0ff", border: "1px solid #262d3b", borderRadius: 10, padding: 12, fontFamily: "'JetBrains Mono',monospace", fontSize: 12 }} />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <section>
              <h4 style={{ margin: "0 0 8px", color: "#cfe0ff", fontSize: 13 }}>Nhân vật ({Object.keys(chars).length}) — màu tóc/da/mắt cố định</h4>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: 10 }}>
                {Object.entries(chars).map(([id, c]: [string, any]) => (
                  <div key={id} style={{ border: "1px solid #262d3b", borderRadius: 10, padding: 10, background: "#11151d" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 7 }}>
                      <input value={c.name || ""} onChange={(e) => setChar(id, "name", e.target.value)} style={{ ...S.btn, cursor: "text", fontWeight: 700, flex: 1 }} />
                      <span style={{ fontSize: 10, color: c.color_source === "color_page" ? "#3ecf8e" : "#e6a23a" }}>{c.color_source === "color_page" ? "✓ màu thật" : "~ đoán"}</span>
                    </div>
                    <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                      <ColorField label="tóc" val={c.hair} on={(v) => setChar(id, "hair", v)} />
                      <ColorField label="da" val={c.skin} on={(v) => setChar(id, "skin", v)} />
                      <ColorField label="mắt" val={c.eyes} on={(v) => setChar(id, "eyes", v)} />
                    </div>
                  </div>
                ))}
              </div>
            </section>
            <section>
              <h4 style={{ margin: "0 0 8px", color: "#cfe0ff", fontSize: 13 }}>Trang phục ({Object.keys(outfits).length})</h4>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: 10 }}>
                {Object.entries(outfits).map(([id, o]: [string, any]) => (
                  <div key={id} style={{ border: "1px solid #262d3b", borderRadius: 10, padding: 10, background: "#11151d" }}>
                    <div style={{ fontSize: 12, color: "#b9c2d4", marginBottom: 7 }}><b>{id}</b> — {o.desc || ""}</div>
                    <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                      <ColorField label="áo" val={o.top} on={(v) => setOutfit(id, "top", v)} />
                      <ColorField label="quần" val={o.bottom} on={(v) => setOutfit(id, "bottom", v)} />
                      <ColorField label="giày" val={o.shoes} on={(v) => setOutfit(id, "shoes", v)} />
                    </div>
                  </div>
                ))}
              </div>
            </section>
            <section>
              <h4 style={{ margin: "0 0 8px", color: "#cfe0ff", fontSize: 13 }}>Bối cảnh ({Object.keys(scenes).length})</h4>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {Object.entries(scenes).map(([id, sc]: [string, any]) => (
                  <div key={id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "#b9c2d4" }}>
                    <b>{id}</b><span style={{ opacity: 0.8 }}>{sc.desc}</span>
                    <span style={{ display: "flex", gap: 3 }}>{(sc.palette || []).slice(0, 8).map((h: string, i: number) => (
                      <span key={i} style={{ width: 16, height: 16, borderRadius: 4, background: h, border: "1px solid #333" }} title={h} />
                    ))}</span>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Sheets viewer ─────────────────────────────────────────────────────────────
function SheetsViewer({ sheets, bible, onClose }: { sheets: Record<string, string>; bible?: Bible; onClose: () => void }) {
  const outfits: Record<string, any> = bible?.outfits || {};
  const chars: Record<string, any> = bible?.characters || {};
  return (
    <div className="nodrag nowheel" style={S.modalWrap} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }} onWheel={(e) => e.stopPropagation()}>
      <div style={S.modalCard} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <b style={{ fontSize: 17 }}>👤 Character sheets ({Object.keys(sheets).length})</b>
          <span style={{ flex: 1 }} />
          <button style={S.btn} onClick={onClose}>Đóng</button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(320px,1fr))", gap: 14 }}>
          {Object.entries(sheets).map(([oid, mid]) => {
            const o = outfits[oid];
            const c = o ? chars[o.char] : undefined;
            return (
              <div key={oid} style={{ border: "1px solid #262d3b", borderRadius: 12, overflow: "hidden", background: "#0f131b" }}>
                <img src={thumbUrl(mid, 640)} alt={oid} style={{ width: "100%", display: "block" }} />
                <div style={{ padding: "8px 11px", fontSize: 12, color: "#cfe0ff" }}>{c?.name ? `${c.name} · ` : ""}{oid.split("__").pop()}</div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Compare overlay (synced zoom) ─────────────────────────────────────────────
function CompareOverlay({ pageMediaId, currentOutput, title, pageIndex, allPageMediaIds, bible, sheets, onApplied, onClose }: {
  pageMediaId: string; currentOutput: string; title: string; pageIndex: number; allPageMediaIds: string[];
  bible?: Bible; sheets: Record<string, string>; onApplied: (newId: string) => void; onClose: () => void;
}) {
  const [shownOut, setShownOut] = useState(currentOutput); // coloured image on the RIGHT (updates after an edit)
  const [t, setT] = useState({ s: 1, x: 0, y: 0 });
  const pan = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  const [box, setBox] = useState<Box4 | null>(null);
  const boxDrag = useRef<{ x: number; y: number } | null>(null); // rubber-band start, in 0-1000
  const [useMask, setUseMask] = useState(false);
  const [editText, setEditText] = useState("");
  const [applying, setApplying] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null); // the RIGHT (coloured) image

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault(); e.stopPropagation();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const cx = e.clientX - r.left - r.width / 2, cy = e.clientY - r.top - r.height / 2;
    setT((p) => { const s = Math.min(30, Math.max(1, p.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2))); const k = s / p.s; return { s, x: cx - (cx - p.x) * k, y: cy - (cy - p.y) * k }; });
  };
  // Pan by dragging the B&W (left) pane — moves both panes (shared transform), so
  // the RIGHT pane's drag is free for drawing the edit region.
  const panDown = (e: React.PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    pan.current = { x: e.clientX, y: e.clientY, ox: t.x, oy: t.y };
  };
  const panMove = (e: React.PointerEvent) => {
    const d = pan.current; if (!d) return;
    setT((p) => ({ ...p, x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) }));
  };
  const panUp = () => { pan.current = null; };

  // px → 0-1000 within the displayed RIGHT image (works under any zoom/pan).
  const toNorm = (clientX: number, clientY: number): { x: number; y: number } | null => {
    const el = imgRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1000, ((clientX - r.left) / r.width) * 1000)),
      y: Math.max(0, Math.min(1000, ((clientY - r.top) / r.height) * 1000)),
    };
  };
  // Drag on the MÀU image to rubber-band a rectangle = the region to edit.
  const boxDown = (e: React.PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const n = toNorm(e.clientX, e.clientY);
    if (!n) return;
    boxDrag.current = n;
    setBox([n.y, n.x, n.y, n.x]);
  };
  const boxMove = (e: React.PointerEvent) => {
    const s = boxDrag.current; if (!s) return;
    const n = toNorm(e.clientX, e.clientY); if (!n) return;
    setBox([Math.min(s.y, n.y), Math.min(s.x, n.x), Math.max(s.y, n.y), Math.max(s.x, n.x)]);
  };
  const boxUp = () => {
    boxDrag.current = null;
    setBox((b) => (b && (b[2] - b[0] < 15 || b[3] - b[1] < 15) ? null : b)); // a mere click → no region
  };

  const apply = async (withPrompt: boolean) => {
    if (!box || applying) return;
    setApplying(true);
    try {
      const res = await runRequestToCompletion("colorize_fix_region", {
        page_media_ids: allPageMediaIds, page_index: pageIndex, bible, sheets,
        current_output_media_id: shownOut, box, use_mask: useMask, prompt: withPrompt ? editText : "",
      });
      const nid = res.result.output_media_id as string;
      if (nid) { onApplied(nid); setShownOut(nid); setBox(null); setEditText(""); }
      else alert("Fix không trả về ảnh");
    } catch (e) {
      alert(`Fix lỗi: ${e instanceof Error ? e.message : e}`);
    } finally { setApplying(false); }
  };

  const tf = `translate(${t.x}px,${t.y}px) scale(${t.s})`;
  const pane: React.CSSProperties = { flex: 1, minWidth: 0, position: "relative", overflow: "hidden", background: "#07090d", display: "flex", alignItems: "center", justifyContent: "center", touchAction: "none" };
  const wrap: React.CSSProperties = { position: "relative", transform: tf, transformOrigin: "center", lineHeight: 0 };
  // Bound the image by the VIEWPORT (vh/vw), not "100%": the image sits in a
  // shrink-to-fit wrap (so the region box can overlay it), and a percentage height
  // against an auto-sized wrap collapses — which made a tall manga page show cut
  // off. Two panes side by side → ~47vw each; the vertical budget leaves room for
  // the toolbar + edit bar.
  const img: React.CSSProperties = { maxWidth: "47vw", maxHeight: "82vh", display: "block", userSelect: "none", pointerEvents: "none" };
  const lbl: React.CSSProperties = { position: "absolute", top: 8, left: 8, zIndex: 2, fontFamily: "'JetBrains Mono',monospace", fontWeight: 700, fontSize: 11, padding: "3px 9px", borderRadius: 6, background: "rgba(0,0,0,.6)", letterSpacing: ".08em" };
  const bstyle = (b: Box4): React.CSSProperties => ({
    position: "absolute", top: `${b[0] / 10}%`, left: `${b[1] / 10}%`, height: `${(b[2] - b[0]) / 10}%`, width: `${(b[3] - b[1]) / 10}%`,
    border: "2px solid #4ecf8e", background: "rgba(78,207,142,.18)", boxSizing: "border-box", pointerEvents: "none",
  });

  return (
    <div className="nodrag nowheel" onWheel={(e) => e.stopPropagation()}
      style={{ position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,.93)", display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 16px", color: "#eee", flexWrap: "wrap" }}>
        <b style={{ fontSize: 14 }}>{title}</b>
        {FIX_REGION_ENABLED ? (
          <span style={{ fontSize: 12, color: "#9aa4b8" }}>
            ✏️ Kéo trên ảnh MÀU để khoanh vùng cần sửa → nhập mô tả bên dưới
          </span>
        ) : null}
        {FIX_REGION_ENABLED && box ? <button style={S.btn} onClick={() => setBox(null)}>↺ Bỏ vùng</button> : null}
        <span style={{ flex: 1 }} />
        <span style={{ opacity: .6, fontSize: 12 }}>cuộn zoom · kéo ảnh B&W để di · {t.s.toFixed(1)}×</span>
        <button style={S.btn} onClick={() => setT({ s: 1, x: 0, y: 0 })}>Reset</button>
        <button style={S.btn} onClick={onClose}>✕ Close</button>
      </div>
      <div style={{ flex: 1, display: "flex", gap: 2, minHeight: 0 }}>
        <div style={pane} onWheel={onWheel} onPointerDown={panDown} onPointerMove={panMove} onPointerUp={panUp}>
          <span style={lbl}>B&W</span>
          <div style={wrap}><img src={mediaUrl(pageMediaId)} alt="" style={img} draggable={false} /></div>
        </div>
        <div style={{ ...pane, cursor: FIX_REGION_ENABLED ? "crosshair" : "default" }}
          onWheel={onWheel}
          onPointerDown={FIX_REGION_ENABLED ? boxDown : undefined}
          onPointerMove={FIX_REGION_ENABLED ? boxMove : undefined}
          onPointerUp={FIX_REGION_ENABLED ? boxUp : undefined}>
          <span style={{ ...lbl, background: "rgba(78,207,142,.82)", color: "#04140b" }}>MÀU</span>
          <div style={wrap}>
            <img ref={imgRef} src={mediaUrl(shownOut)} alt="" style={img} draggable={false} />
            {box ? <div style={bstyle(box)} /> : null}
          </div>
        </div>
      </div>
      {FIX_REGION_ENABLED ? (
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 16px", flexWrap: "wrap", borderTop: "1px solid #1c2029", background: "#0e1116" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12, color: "#b9c2d4" }} title="Bật: chỉ đổi vật thể trong vùng (bám sát bằng SAM). Tắt: đổi cả vùng chữ nhật.">
            <input type="checkbox" checked={useMask} onChange={(e) => setUseMask(e.target.checked)} /> chỉ vật thể (mask khít)
          </label>
          <input value={editText} onChange={(e) => setEditText(e.target.value)}
            placeholder={box ? "Mô tả chỉnh sửa (vd: đổi áo sang đỏ)… — để trống = chỉ tô lại vùng" : "Kéo 1 vùng trên ảnh MÀU trước…"}
            onKeyDown={(e) => { if (e.key === "Enter" && box && editText.trim() && !applying) void apply(true); }}
            style={{ ...S.btn, cursor: "text", fontWeight: 500, flex: 1, minWidth: 260 }} />
          <button style={{ ...S.btn, opacity: box && !applying ? 1 : 0.45 }} disabled={!box || applying} onClick={() => void apply(false)}>
            {applying ? "⏳…" : "↻ Tô lại vùng"}
          </button>
          <button style={{ ...S.btn, ...S.primary, opacity: box && editText.trim() && !applying ? 1 : 0.45 }} disabled={!box || !editText.trim() || applying} onClick={() => void apply(true)}>
            {applying ? "⏳…" : "✎ Sửa theo mô tả"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
