import { memo, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { NodeResizer, type NodeProps } from "@xyflow/react";

import {
  downloadUpscaleImage,
  downloadUpscaleZip,
  getShot,
  mediaUrl,
  patchShot,
  patchShotGroup,
  thumbUrl,
  upscaleJob,
  upscaleRun,
  upscaleUploadSource,
} from "../../api/client";
import { useShotWorkflowStore } from "../../store/shotWorkflow";

/**
 * A SceneCanvas "sequence" whose kind is "upscale": instead of a node graph it
 * shows the batch 4K upscaler — an INPUT grid of raw images → UPSCALE → a RESULT
 * grid of 4K frames, embedded directly on the episode canvas as one block.
 *
 * Self-contained per sequence: its items (source + version pointers) persist in
 * the Shot's ``workflow_metadata.upscaleItems`` (getShot/patchShot), and the 4K
 * pass runs async via /upscale/run + /job (no Cloudflare 524). Each Redo appends
 * a version (v1, v2, v3…) the user can review/pick/download.
 */

const MIN_W = 640;
const MIN_H = 420;
const JOB_TIMEOUT_MS = 6 * 60 * 1000;

type Status = "uploading" | "ready" | "running" | "done" | "error";
interface Ver {
  resultMediaId: string;
  resultName: string;
}
interface Item {
  key: string;
  file?: File;
  previewUrl: string;
  name: string;
  status: Status;
  sourceMediaId?: string;
  versions: Ver[];
  current: number;
  error?: string;
  startedAt?: number;
}
interface PItem {
  key: string;
  name: string;
  sourceMediaId: string;
  versions: Ver[];
  current: number;
}

export interface UpscaleGroupData extends Record<string, unknown> {
  shotId: string;
  label: string;
  onDelete?: () => void;
  onResize?: () => void;
}

let _seq = 0;
const nextKey = () => `u${Date.now().toString(36)}_${_seq++}`;
const baseName = (n: string) => n.replace(/\.[^.]+$/, "").trim() || "image";

/** Run `worker` over `items` with at most `concurrency` in flight. Used for the
 *  ON-ADD upload/shrink step (canvas decode+encode is main-thread heavy) so
 *  adding a big batch doesn't freeze the UI. The Atrium 4K pass is unaffected —
 *  that still fires all at once. */
async function runPool<T>(items: T[], worker: (t: T) => Promise<void>, concurrency: number) {
  let i = 0;
  const lane = async () => {
    while (i < items.length) {
      const idx = i++;
      await worker(items[idx]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, lane));
}
const curVer = (it: Item): Ver | undefined => it.versions[it.current];

function toItem(p: PItem): Item {
  const versions = Array.isArray(p.versions) ? p.versions.filter((v) => v && v.resultMediaId) : [];
  const current = Math.min(Math.max(0, p.current | 0), Math.max(0, versions.length - 1));
  return {
    key: p.key || nextKey(),
    name: p.name || "image",
    status: versions.length ? "done" : "ready",
    sourceMediaId: p.sourceMediaId,
    versions,
    current,
    previewUrl: thumbUrl(p.sourceMediaId, 220),
  };
}
function toPersisted(items: Item[]): PItem[] {
  return items
    .filter((it) => it.sourceMediaId)
    .map((it) => ({ key: it.key, name: it.name, sourceMediaId: it.sourceMediaId!, versions: it.versions, current: it.current }));
}

// ── styles: match the normal sequence frame (shot-group classes) + theme vars.
//    Tiles are ~3× the old size (minmax 234px) so frames are easy to read. ──
const S = {
  root: { width: "100%", height: "100%", display: "flex", flexDirection: "column" } as React.CSSProperties,
  btn: { padding: "6px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--panel-high)",
    color: "var(--text)", fontWeight: 600, fontSize: 12.5, cursor: "pointer", whiteSpace: "nowrap" } as React.CSSProperties,
  primary: { border: "1px solid var(--accent)", background: "var(--accent)", color: "#06122b" } as React.CSSProperties,
  body: { flex: 1, display: "grid", gridTemplateColumns: "1fr 104px 1fr", gap: 14, padding: "14px 18px 18px", minHeight: 0 } as React.CSSProperties,
  panel: { display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, border: "1px solid var(--border)",
    borderRadius: 10, background: "color-mix(in srgb, var(--panel) 72%, #000)", overflow: "hidden" } as React.CSSProperties,
  panelHead: { padding: "8px 13px", fontSize: 11.5, letterSpacing: ".14em", fontWeight: 700, color: "var(--muted)",
    borderBottom: "1px solid var(--border)" } as React.CSSProperties,
  gridScroll: { flex: 1, overflow: "auto", padding: 12 } as React.CSSProperties,
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(234px,1fr))", gap: 14 } as React.CSSProperties,
  tile: { position: "relative", aspectRatio: "3 / 4", borderRadius: 9, overflow: "hidden", border: "1px solid var(--border)",
    background: "var(--panel-high)",
    // Skip painting tiles scrolled out of the panel → lighter zoom with many images.
    contentVisibility: "auto", containIntrinsicSize: "234px 312px" } as React.CSSProperties,
  arrowCol: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 10,
    color: "var(--accent)", borderRadius: 12,
    background: "linear-gradient(180deg, transparent 6%, color-mix(in srgb, var(--accent) 18%, transparent) 50%, transparent 94%)",
    border: "1px solid color-mix(in srgb, var(--accent) 30%, transparent)" } as React.CSSProperties,
  empty: { color: "var(--muted)", fontSize: 14, textAlign: "center", padding: 32 } as React.CSSProperties,
};

function UpscaleGroupNodeImpl({ data, selected }: NodeProps) {
  const d = data as UpscaleGroupData;
  const shotId = d.shotId;

  const [items, setItems] = useState<Item[]>([]);
  const [zipping, setZipping] = useState(false);
  const [verItem, setVerItem] = useState<string | null>(null); // key
  const [cmp, setCmp] = useState<{ raw: string; result?: string; title: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loaded = useRef(false);
  const wfRef = useRef<Record<string, unknown>>({});
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const saveTimer = useRef<number | null>(null);

  const patchItem = useCallback((key: string, p: Partial<Item>) => {
    setItems((prev) => prev.map((it) => (it.key === key ? { ...it, ...p } : it)));
  }, []);

  // load this sequence's saved items from the Shot
  useEffect(() => {
    let alive = true;
    getShot(shotId)
      .then((shot) => {
        if (!alive) return;
        const wf = (shot.workflow_metadata || {}) as Record<string, unknown>;
        wfRef.current = wf;
        loaded.current = true;
        // If the user already added images while this getShot was in flight
        // (common when you create a new upscale sequence and immediately upload,
        // especially while another sequence is busy so this load is slow), KEEP
        // theirs — do NOT clobber with the (empty) server list. That clobber was
        // eating uploaded images.
        if (itemsRef.current.length) return;
        const raw = Array.isArray(wf.upscaleItems) ? (wf.upscaleItems as PItem[]) : [];
        const mapped = raw.filter((p) => p && p.sourceMediaId).map(toItem);
        setItems(mapped);
        // reconnect any in-flight / finished job for items without a version
        mapped
          .filter((it) => it.sourceMediaId && !it.versions.length)
          .forEach(async (it) => {
            const j = await upscaleJob(it.sourceMediaId!).catch(() => null);
            if (!j) return;
            if (j.status === "running") patchItem(it.key, { status: "running", startedAt: Date.now() });
            else if (j.status === "done" && j.result_media_id)
              addVersion(it.key, { resultMediaId: j.result_media_id, resultName: j.result_name || `${it.name}_4K` });
          });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shotId]);

  // persist (debounced) into the Shot's workflow_metadata
  useEffect(() => {
    if (!loaded.current) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      void patchShot(shotId, {
        workflow_metadata: { ...wfRef.current, kind: "upscale", upscaleItems: toPersisted(itemsRef.current) },
      }).catch(() => {});
    }, 600);
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, [items, shotId]);

  const addVersion = useCallback(
    (key: string, v: Ver) => {
      setItems((prev) =>
        prev.map((it) =>
          it.key === key
            ? { ...it, versions: [...it.versions, v], current: it.versions.length, status: "done", error: undefined }
            : it,
        ),
      );
    },
    [],
  );

  // upload source immediately on add (→ persists across reloads)
  const uploadOne = useCallback(
    async (it: Item) => {
      if (!it.file) return;
      patchItem(it.key, { status: "uploading", error: undefined });
      try {
        const r = await upscaleUploadSource(it.file, it.name);
        // Drop the heavy full-res object-URL preview (and free it) → the tile now
        // shows the light 512px server thumb. Keeping the original blob rendered
        // is what made canvas zoom janky, even after the upload finished.
        if (it.previewUrl.startsWith("blob:")) URL.revokeObjectURL(it.previewUrl);
        patchItem(it.key, {
          status: "ready",
          sourceMediaId: r.source_media_id,
          file: undefined,
          previewUrl: thumbUrl(r.source_media_id, 512),
        });
      } catch (e) {
        patchItem(it.key, { status: "error", error: `upload lỗi: ${e instanceof Error ? e.message : e}` });
      }
    },
    [patchItem],
  );

  // start an async 4K job; polling appends the resulting version
  const runOne = useCallback(
    async (it: Item) => {
      if (!it.sourceMediaId) {
        if (it.file) return uploadOne(it);
        return;
      }
      patchItem(it.key, { status: "running", error: undefined, startedAt: Date.now() });
      try {
        await upscaleRun(it.sourceMediaId, it.name);
      } catch (e) {
        patchItem(it.key, { status: "error", error: e instanceof Error ? e.message : String(e) });
      }
    },
    [patchItem, uploadOne],
  );

  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      const added: Item[] = [];
      for (const f of Array.from(files)) {
        if (!f.type.startsWith("image/")) continue;
        added.push({ key: nextKey(), file: f, previewUrl: URL.createObjectURL(f), name: baseName(f.name), status: "uploading", versions: [], current: 0 });
      }
      if (!added.length) return;
      setItems((prev) => [...prev, ...added]);
      // Upload/shrink at most 4 at a time so a big batch doesn't freeze the UI.
      void runPool(added, uploadOne, 4);
    },
    [uploadOne],
  );

  const upscaleAll = useCallback(() => {
    itemsRef.current
      .filter((it) => it.sourceMediaId && (it.status === "ready" || it.status === "error"))
      .forEach((it) => void runOne(it));
  }, [runOne]);

  // poll running jobs
  const anyRunning = items.some((it) => it.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const tick = async () => {
      const running = itemsRef.current.filter((it) => it.status === "running" && it.sourceMediaId);
      await Promise.all(
        running.map(async (it) => {
          if (it.startedAt && Date.now() - it.startedAt > JOB_TIMEOUT_MS) {
            patchItem(it.key, { status: "error", error: "quá thời gian — thử lại" });
            return;
          }
          const j = await upscaleJob(it.sourceMediaId!).catch(() => null);
          if (!j) return;
          if (j.status === "done" && j.result_media_id)
            addVersion(it.key, { resultMediaId: j.result_media_id, resultName: j.result_name || `${it.name}_4K` });
          else if (j.status === "error") patchItem(it.key, { status: "error", error: j.error || "upscale lỗi" });
          else if (j.status === "none") patchItem(it.key, { status: it.versions.length ? "done" : "ready" });
        }),
      );
    };
    void tick();
    const id = window.setInterval(() => void tick(), 3000);
    return () => window.clearInterval(id);
  }, [anyRunning, patchItem, addVersion]);

  const removeOne = useCallback((key: string) => {
    setItems((prev) => {
      const it = prev.find((x) => x.key === key);
      if (it?.file) URL.revokeObjectURL(it.previewUrl);
      return prev.filter((x) => x.key !== key);
    });
  }, []);

  const downloadAll = useCallback(async () => {
    const ready = itemsRef.current.filter((it) => curVer(it));
    if (!ready.length) return;
    setZipping(true);
    try {
      await downloadUpscaleZip(ready.map((it) => ({ media_id: curVer(it)!.resultMediaId, name: curVer(it)!.resultName })));
    } catch (e) {
      alert(`Download failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setZipping(false);
    }
  }, []);

  const doneCount = items.filter((it) => curVer(it)).length;
  const readyCount = items.filter((it) => it.sourceMediaId && (it.status === "ready" || it.status === "error")).length;

  const verObj = verItem ? items.find((it) => it.key === verItem) : null;

  const frame = (mediaId: string | undefined, blob?: string) => (
    <img
      src={blob || (mediaId ? thumbUrl(mediaId, 512) : "")}
      alt=""
      draggable={false}
      loading="lazy"
      decoding="async"
      style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
    />
  );

  return (
    <div className="shot-group" style={S.root}>
      <NodeResizer
        minWidth={MIN_W}
        minHeight={MIN_H}
        isVisible={selected}
        onResizeEnd={(_e, p) => {
          // Persist the manual size so it survives F5 / reload (groupSize honors
          // g.size over the default). Mirror into the store too so the immediate
          // re-seed keeps it.
          const size = { w: Math.round(p.width), h: Math.round(p.height) };
          useShotWorkflowStore.getState().updateShotGroupLocal(shotId, { size });
          void patchShotGroup(shotId, { size }).catch(() => {});
          d.onResize?.();
        }}
      />
      <input ref={fileRef} type="file" accept="image/*" multiple style={{ display: "none" }}
        onChange={(e) => { onFiles(e.target.files); e.target.value = ""; }} />

      {/* header — same frame chrome as a normal sequence; drag handle for the node */}
      <div className="shot-group__header upscale-drag" style={{ cursor: "grab" }}>
        <span className="shot-group__label" title="Sequence Upscale 4K">
          <span className="shot-group__num">{d.label}</span>
          <span className="shot-group__scene"> — Upscale 4K</span>
        </span>
        <span className="shot-group__badge">{doneCount}/{items.length}</span>
        <span style={{ flex: 1 }} />
        <button className="nodrag" style={S.btn} onClick={() => fileRef.current?.click()}>＋ Add</button>
        <button className="nodrag" style={{ ...S.btn, ...S.primary, opacity: readyCount ? 1 : 0.5 }} disabled={!readyCount} onClick={upscaleAll}>
          ⤴ Upscale all ({readyCount})
        </button>
        <button className="nodrag" style={{ ...S.btn, opacity: doneCount && !zipping ? 1 : 0.5 }} disabled={!doneCount || zipping} onClick={() => void downloadAll()}>
          {zipping ? "Zipping…" : `⬇ All (${doneCount})`}
        </button>
        {d.onDelete ? (
          <button className="shot-group__delete nodrag" title="Xoá sequence" aria-label="Xoá sequence" onClick={() => d.onDelete?.()}>✕</button>
        ) : null}
      </div>

      {/* two panels */}
      <div style={S.body}>
        {/* INPUT */}
        <div style={S.panel}>
          <div style={S.panelHead}>INPUT · RAW</div>
          <div style={S.gridScroll} className="nodrag">
            {!items.length ? (
              <div style={S.empty}>Bấm ＋ Add để tải ảnh raw lên</div>
            ) : (
              <div style={S.grid}>
                {items.map((it) => (
                  <div key={it.key} style={S.tile} title={it.name}>
                    {frame(it.sourceMediaId, it.file ? it.previewUrl : undefined)}
                    <button
                      onClick={() => removeOne(it.key)}
                      title="Bỏ"
                      style={{ position: "absolute", top: 6, right: 6, width: 24, height: 24, borderRadius: 6, border: "none",
                        background: "rgba(0,0,0,.62)", color: "#fff", cursor: "pointer", fontSize: 14, lineHeight: "22px", padding: 0 }}
                    >✕</button>
                    {it.status === "uploading" ? (
                      <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
                        background: "rgba(0,0,0,.45)", fontSize: 13, color: "#e6e9f0" }}>đang lưu…</span>
                    ) : null}
                    <span style={{ position: "absolute", left: 7, bottom: 7, right: 34, fontSize: 11, color: "rgba(255,255,255,.9)",
                      textShadow: "0 1px 3px #000", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{it.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* arrow — the UPSCALE flow between the two panels (made prominent) */}
        <div style={S.arrowCol}>
          <svg
            viewBox="0 0 40 24"
            width="42"
            fill="none"
            style={{ filter: "drop-shadow(0 0 7px color-mix(in srgb, var(--accent) 75%, transparent))" }}
          >
            <path d="M2 12h30M24 5l9 7-9 7" stroke="currentColor" strokeWidth="4.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span
            style={{
              fontSize: 12,
              fontWeight: 800,
              color: "var(--accent)",
              letterSpacing: ".18em",
              whiteSpace: "nowrap",
              textShadow: "0 0 9px color-mix(in srgb, var(--accent) 60%, transparent)",
            }}
          >
            UPSCALE
          </span>
        </div>

        {/* RESULT */}
        <div style={S.panel}>
          <div style={{ ...S.panelHead, color: "var(--accent)" }}>RESULT · 4K</div>
          <div style={S.gridScroll} className="nodrag">
            {!items.length ? (
              <div style={S.empty}>—</div>
            ) : (
              <div style={S.grid}>
                {items.map((it) => {
                  const v = curVer(it);
                  return (
                    <div key={it.key} style={S.tile} title={it.name}>
                      {v ? (
                        <>
                          {frame(v.resultMediaId)}
                          <span style={{ position: "absolute", top: 6, left: 6, fontWeight: 700, fontSize: 11, padding: "2px 7px",
                            borderRadius: 5, background: "#f2b23a", color: "#201400" }}>4K</span>
                          {it.versions.length > 1 ? (
                            <span style={{ position: "absolute", top: 6, right: 6, fontWeight: 700, fontSize: 11, padding: "2px 7px",
                              borderRadius: 5, background: "var(--accent)", color: "#06122b" }}>v{it.current + 1}/{it.versions.length}</span>
                          ) : null}
                          <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column",
                            alignItems: "center", justifyContent: "center", gap: 7, background: "rgba(6,9,14,.5)", opacity: 0, transition: "opacity .13s" }}
                            onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                            onMouseLeave={(e) => (e.currentTarget.style.opacity = "0")}>
                            <button style={{ ...S.btn, ...S.primary, padding: "5px 12px", fontSize: 12.5 }}
                              onClick={() => void downloadUpscaleImage(v.resultMediaId, v.resultName)}>⬇ Tải</button>
                            <button style={{ ...S.btn, padding: "5px 12px", fontSize: 12.5 }} onClick={() => setVerItem(it.key)}>
                              ◫ v1..v{it.versions.length}
                            </button>
                            <button style={{ ...S.btn, padding: "5px 12px", fontSize: 12.5 }}
                              onClick={() => setCmp({ raw: it.sourceMediaId ? mediaUrl(it.sourceMediaId) : it.previewUrl, result: mediaUrl(v.resultMediaId), title: v.resultName })}>⤢ So sánh</button>
                          </div>
                        </>
                      ) : it.status === "running" ? (
                        <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
                          fontSize: 13, color: "var(--muted)", textAlign: "center", padding: 8 }}>⏳ đang xử lý…</span>
                      ) : it.status === "error" ? (
                        <button onClick={() => void runOne(it)} title={it.error}
                          style={{ position: "absolute", inset: 0, border: "none", background: "rgba(255,90,90,.08)", color: "#ff8f8f", fontSize: 13, cursor: "pointer" }}>✗ thử lại</button>
                      ) : it.status === "ready" ? (
                        <button onClick={() => void runOne(it)}
                          style={{ position: "absolute", inset: 0, border: "none", background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: "var(--accent)", fontSize: 14, fontWeight: 700, cursor: "pointer" }}>⤴ Upscale</button>
                      ) : (
                        <span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 13, color: "var(--muted)" }}>…</span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── versions modal (portaled to body so it escapes the canvas transform) ── */}
      {verObj ? createPortal(
        <div className="nodrag nowheel" onClick={() => setVerItem(null)} onWheel={(e) => e.stopPropagation()}
          style={{ position: "fixed", inset: 0, zIndex: 1000, background: "rgba(6,8,12,.82)", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
          <div onClick={(e) => e.stopPropagation()} style={{ background: "#161a23", border: "1px solid #262d3b", borderRadius: 16,
            width: "min(720px,94vw)", maxHeight: "88vh", overflow: "auto", padding: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
              <b style={{ fontSize: 16 }}>Các phiên bản</b>
              <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: 12, color: "#8b94a7" }}>{verObj.name}_4K.png</span>
              <span style={{ flex: 1 }} />
              <button style={{ ...S.btn, ...S.primary }} onClick={() => void runOne(verObj)}>↻ Redo (+v{verObj.versions.length + 1})</button>
              <button style={S.btn} onClick={() => setVerItem(null)}>Đóng</button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))", gap: 14 }}>
              {verObj.versions.map((v, idx) => (
                <div key={idx} onClick={() => patchItem(verObj.key, { current: idx })}
                  style={{ border: `2px solid ${idx === verObj.current ? "#4c8dff" : "#262d3b"}`, borderRadius: 12, overflow: "hidden",
                    cursor: "pointer", background: "#0f131b", boxShadow: idx === verObj.current ? "0 0 0 3px rgba(76,141,255,.18)" : "none" }}>
                  <div style={{ aspectRatio: "3/4" }}>{frame(v.resultMediaId)}</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 10px" }}>
                    <span style={{ fontFamily: "'JetBrains Mono',monospace", fontWeight: 700, fontSize: 12, color: idx === verObj.current ? "#4c8dff" : "#cfe0ff" }}>v{idx + 1}</span>
                    {idx === verObj.current ? <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: 10, color: "#3ecf8e" }}>đang dùng</span> : null}
                    <span style={{ flex: 1 }} />
                    <button style={{ ...S.btn, padding: "3px 8px", fontSize: 11 }}
                      onClick={(e) => { e.stopPropagation(); void downloadUpscaleImage(v.resultMediaId, `${verObj.name}_4K_v${idx + 1}`); }}>⬇</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>,
        document.body,
      ) : null}

      {/* ── compare modal (synced zoom), portaled to body ── */}
      {cmp ? createPortal(<CompareOverlay {...cmp} onClose={() => setCmp(null)} />, document.body) : null}
    </div>
  );
}

/**
 * Memoised so a canvas drag/pan (which churns the position props every frame in
 * @xyflow/react v12) doesn't re-render this heavy two-panel grid. Re-render only
 * when the sequence's own props change — data (label/callbacks), selection, or
 * the frame size (resize). Position is deliberately ignored — the wrapper moves
 * the node via CSS transform, so the inner content never needs to repaint.
 */
export const UpscaleGroupNode = memo(
  UpscaleGroupNodeImpl,
  (a, b) =>
    a.data === b.data &&
    a.selected === b.selected &&
    a.width === b.width &&
    a.height === b.height,
);

function CompareOverlay({ raw, result, title, onClose }: { raw: string; result?: string; title: string; onClose: () => void }) {
  const [t, setT] = useState({ s: 1, x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault(); e.stopPropagation();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const cx = e.clientX - r.left - r.width / 2, cy = e.clientY - r.top - r.height / 2;
    setT((p) => { const s = Math.min(30, Math.max(1, p.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2))); const k = s / p.s; return { s, x: cx - (cx - p.x) * k, y: cy - (cy - p.y) * k }; });
  };
  const pd = (e: React.PointerEvent) => { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); drag.current = { x: e.clientX - t.x, y: e.clientY - t.y }; };
  const pm = (e: React.PointerEvent) => { if (drag.current) setT((p) => ({ ...p, x: e.clientX - drag.current!.x, y: e.clientY - drag.current!.y })); };
  const pu = () => { drag.current = null; };
  const tf = `translate(${t.x}px,${t.y}px) scale(${t.s})`;
  const pane: React.CSSProperties = { flex: 1, minWidth: 0, position: "relative", overflow: "hidden", background: "#07090d", display: "flex", alignItems: "center", justifyContent: "center" };
  const img: React.CSSProperties = { maxWidth: "100%", maxHeight: "100%", transform: tf, transformOrigin: "center", userSelect: "none", pointerEvents: "none" };
  const lbl: React.CSSProperties = { position: "absolute", top: 8, left: 8, zIndex: 2, fontFamily: "'JetBrains Mono',monospace", fontWeight: 700, fontSize: 11, padding: "3px 9px", borderRadius: 6, background: "rgba(0,0,0,.6)", letterSpacing: ".08em" };
  return (
    <div className="nodrag nowheel" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      style={{ position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,.92)", display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 16px", color: "#eee" }}>
        <b style={{ fontSize: 14 }}>{title}</b>
        <span style={{ opacity: .6, fontSize: 12 }}>cuộn để zoom · kéo để di · {t.s.toFixed(1)}×</span>
        <span style={{ flex: 1 }} />
        <button style={S.btn} onClick={() => setT({ s: 1, x: 0, y: 0 })}>Reset</button>
        <button style={S.btn} onClick={onClose}>✕ Close</button>
      </div>
      <div style={{ flex: 1, display: "flex", gap: 2, minHeight: 0 }}>
        <div style={pane} onWheel={onWheel} onPointerDown={pd} onPointerMove={pm} onPointerUp={pu}>
          <span style={lbl}>RAW</span><img src={raw} alt="" style={img} draggable={false} />
        </div>
        <div style={pane} onWheel={onWheel} onPointerDown={pd} onPointerMove={pm} onPointerUp={pu}>
          <span style={{ ...lbl, background: "rgba(242,178,58,.8)", color: "#201400" }}>4K</span>
          {result ? <img src={result} alt="" style={img} draggable={false} /> : <span style={{ color: "#888" }}>—</span>}
        </div>
      </div>
    </div>
  );
}
