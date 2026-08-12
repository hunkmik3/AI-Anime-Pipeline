import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import {
  addEditNote,
  listEditNotes,
  listSeriesMaterials,
  resolveEditNote,
  type EditNoteDTO,
  type MaterialsDTO,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { StudioNav } from "../components/shell/StudioNav";
import { toast } from "../store/toast";

/**
 * The editor's cut, with notes pinned to the frame and to the sequence.
 *
 * Two decisions carry this page.
 *
 * **The player is the page.** A note is left while watching, so the video gets the
 * width and the controls sit under it; a form with a video inside it would make
 * the reviewing the secondary act.
 *
 * **The sequence is CHOSEN from pictures, not typed.** The cut is assembled
 * outside the app, so no arithmetic maps 02:47 back to a sequence — and the editor
 * is looking at the shot, not at its name. A dropdown of forty codes asks them to
 * translate what they can see into something they cannot remember; a strip of
 * thumbnails asks them to match a picture to a picture.
 */

function tc(sec: number): string {
  if (!Number.isFinite(sec)) return "00:00.00";
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
}

export function CutReviewPage() {
  const { submissionId = "" } = useParams();
  const [notes, setNotes] = useState<EditNoteDTO[] | null>(null);
  const [material, setMaterial] = useState<MaterialsDTO | null>(null);
  const [at, setAt] = useState(0);
  const [pick, setPick] = useState<string | null>(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const video = useRef<HTMLVideoElement | null>(null);
  const pad = useRef<HTMLCanvasElement | null>(null);
  const drew = useRef(false);

  const load = useCallback(async () => {
    const r = await listEditNotes(submissionId);
    setNotes(r.notes);
    if (r.series_id) setMaterial(await listSeriesMaterials(r.series_id));
  }, [submissionId]);

  useEffect(() => {
    void load();
  }, [load]);

  /** Every sequence in the series, flat — the strip the editor matches against. */
  const strip = useMemo(
    () =>
      (material?.episodes ?? []).flatMap((ep) =>
        ep.sequences.map((sq) => ({
          shot_id: sq.shot_id,
          code: (sq.code || "").split("_").pop() || "—",
          episode: ep.code || ep.name,
          media_id: sq.clips[0]?.media_id ?? null,
        })),
      ),
    [material],
  );

  /** The strokes over the frame, as a PNG — or nothing if the pad is clean. */
  function exportDrawing(): string | null {
    if (!drew.current || !pad.current) return null;
    try {
      return pad.current.toDataURL("image/png");
    } catch {
      // A tainted canvas would throw. The note is worth more than the drawing.
      return null;
    }
  }

  function clearPad() {
    const c = pad.current;
    if (!c) return;
    c.getContext("2d")?.clearRect(0, 0, c.width, c.height);
    drew.current = false;
  }

  async function send() {
    if (busy || !body.trim()) return;
    setBusy(true);
    try {
      await addEditNote(submissionId, {
        at_seconds: at,
        body: body.trim(),
        shot_id: pick,
        drawing_data_url: exportDrawing(),
      });
      clearPad();
      setBody("");
      await load();
      toast("Đã gửi ghi chú");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Không gửi được", "error");
    } finally {
      setBusy(false);
    }
  }

  function seek(sec: number) {
    setAt(sec);
    if (video.current) video.current.currentTime = sec;
  }

  const duration = video.current?.duration || 0;

  return (
    <div className="shellpage">
      <StudioNav />
      <PageHeader
        title="Bản dựng"
        subtitle="Dừng ở khung hình bị lỗi, chọn đúng clip đang trên màn hình, rồi viết ghi chú. Artist sẽ thấy nó ngay trên sequence đó."
      />

      <div className="cut">
        <div className="cut__stage">
          {/* The cut itself is on Drive; the app streams it through its own
              identity so the file can stay Restricted. */}
          <video
            ref={video}
            className="cut__video"
            controls={!drawing}
            src={`/api/submissions/${submissionId}/video`}
            onTimeUpdate={(e) => setAt(e.currentTarget.currentTime)}
            onLoadedMetadata={(e) => {
              // Size the pad to the frame ONCE it is known, so a stroke lands
              // where it was drawn instead of on a stretched guess.
              const c = pad.current;
              if (c) {
                c.width = e.currentTarget.videoWidth || 1280;
                c.height = e.currentTarget.videoHeight || 720;
              }
            }}
          />
          {/* The pad only takes the pointer while the pen is on: an editor
              scrubbing with the pen armed would draw instead of seek, and the
              control they reach for most is the one they would lose. */}
          <canvas
            ref={pad}
            className={`cut__pad${drawing ? " is-on" : ""}`}
            onPointerDown={(e) => {
              if (!drawing) return;
              const c = e.currentTarget;
              c.setPointerCapture(e.pointerId);
              const ctx = c.getContext("2d");
              if (!ctx) return;
              const r = c.getBoundingClientRect();
              ctx.strokeStyle = "#ff5a2e";
              ctx.lineWidth = Math.max(3, c.width / 320);
              ctx.lineCap = "round";
              ctx.lineJoin = "round";
              ctx.beginPath();
              ctx.moveTo(
                ((e.clientX - r.left) / r.width) * c.width,
                ((e.clientY - r.top) / r.height) * c.height,
              );
            }}
            onPointerMove={(e) => {
              if (!drawing || !e.currentTarget.hasPointerCapture(e.pointerId)) return;
              const c = e.currentTarget;
              const ctx = c.getContext("2d");
              const r = c.getBoundingClientRect();
              if (!ctx) return;
              ctx.lineTo(
                ((e.clientX - r.left) / r.width) * c.width,
                ((e.clientY - r.top) / r.height) * c.height,
              );
              ctx.stroke();
              drew.current = true;
            }}
          />
          <div className="cut__tools">
            <button
              className="cut__tool"
              aria-pressed={drawing}
              title="Vẽ lên khung hình"
              onClick={() => setDrawing((v) => !v)}
            >
              ✏
            </button>
            <button className="cut__tool" title="Xoá nét vẽ" onClick={clearPad}>
              ⌫
            </button>
          </div>
        </div>

        {/* Notes as ticks on their own rail. Scanning it answers "how bad is this
            round, and where does it go wrong" — a count in a list cannot. */}
        <div className="cut__rail">
          {(notes ?? []).map((n) => (
            <button
              key={n.id}
              className={`cut__tick${n.resolved ? " is-done" : ""}`}
              style={{ left: duration ? `${(n.at_seconds / duration) * 100}%` : "0%" }}
              title={`${tc(n.at_seconds)} — ${n.shot_code || "cả tập"}`}
              onClick={() => seek(n.at_seconds)}
            />
          ))}
        </div>

        <div className="cut__ask">
          <p className="cut__q">
            <b>Clip nào đang trên màn hình?</b> tại {tc(at)} — bấm cái giống hình
            trên, hoặc bỏ trống nếu ghi chú cho cả tập.
          </p>
          <div className="cut__strip">
            {strip.map((sq) => (
              <button
                key={sq.shot_id}
                className="cut__pick"
                aria-pressed={pick === sq.shot_id}
                onClick={() => setPick(pick === sq.shot_id ? null : sq.shot_id)}
                title={`${sq.episode} · ${sq.code}`}
              >
                {sq.media_id ? (
                  <img className="cut__thumb" src={`/media/${sq.media_id}`} alt="" />
                ) : (
                  <span className="cut__thumb" />
                )}
                <span className="cut__pickn">{sq.code}</span>
              </button>
            ))}
            {strip.length === 0 ? (
              <span className="rfoot">Series này chưa có clip nào để đối chiếu.</span>
            ) : null}
          </div>

          <div className="cut__in">
            <textarea
              value={body}
              placeholder="Mặt nhân vật bị méo ở khung này, gen lại giúp anh…"
              onChange={(e) => setBody(e.target.value)}
            />
            <button
              className="btn2 btn2--primary"
              disabled={busy || !body.trim()}
              onClick={() => void send()}
            >
              Gửi ghi chú
            </button>
          </div>
        </div>

        <ol className="cut__notes">
          {(notes ?? []).map((n) => (
            <li key={n.id} className={`cut__note${n.resolved ? " is-done" : ""}`}>
              <button className="cut__noteat" onClick={() => seek(n.at_seconds)}>
                {tc(n.at_seconds)}
              </button>
              <span className="cut__noteseq">{n.shot_code || "cả tập"}</span>
              {n.drawing_media_id ? (
                <a className="cut__notepic" href={`/media/${n.drawing_media_id}`}
                   target="_blank" rel="noreferrer" title="Nét vẽ trên khung hình">
                  <img src={`/media/${n.drawing_media_id}`} alt="" />
                </a>
              ) : null}
              <span className="cut__notebody">{n.body}</span>
              <span className="cut__notewho">{n.author_name}</span>
              <button
                className="btn2"
                onClick={async () => {
                  await resolveEditNote(n.id, !n.resolved);
                  await load();
                }}
              >
                {n.resolved ? "Mở lại" : "Đã sửa"}
              </button>
            </li>
          ))}
          {notes !== null && notes.length === 0 ? (
            <li className="rfoot">Chưa có ghi chú nào trên bản dựng này.</li>
          ) : null}
        </ol>
      </div>
    </div>
  );
}
