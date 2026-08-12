import { useCallback, useEffect, useState } from "react";

import {
  listMyMaterials,
  listSeriesMaterials,
  materialsZipUrl,
  type MaterialSeriesDTO,
  type MaterialsDTO,
} from "../api/client";
import { PageHeader } from "../components/shell/PageHeader";
import { StudioNav } from "../components/shell/StudioNav";

/**
 * What the editor takes out of the app.
 *
 * Their job has exactly two halves — take everything, bring one thing back — so
 * the page says those two things and not a third. The take-all button is the
 * largest thing on it because nine times in ten that is the only control anyone
 * touches; the clip list underneath is for checking what is there and pulling a
 * single replacement, which is the rarer job and reads as the smaller one.
 *
 * Arranged by person, not by project: an editor is handed series across several
 * projects, and "what is waiting for me to cut" is a question no project page can
 * answer.
 */

export function MaterialsPage() {
  const [rows, setRows] = useState<MaterialSeriesDTO[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await listMyMaterials();
      setRows(r.series);
      if (r.series.length === 1) setOpenId(r.series[0].id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="shellpage">
      <StudioNav />
      <PageHeader
        title="Raw material"
        subtitle="Every clip the artists generated, ready to pull. Cut the episode in your own editor, then hand one file back on My work."
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {rows === null ? <p className="rfoot">Loading…</p> : null}

      {rows !== null && rows.length === 0 ? (
        <div className="inbox__empty">
          <b>Chưa có series nào giao cho bạn.</b>
          PM giao vai <em>editor</em> trên một project, series của project đó sẽ hiện ở đây.
        </div>
      ) : null}

      <div className="mat">
        {(rows ?? []).map((s) => (
          <SeriesCard
            key={s.id}
            s={s}
            open={openId === s.id}
            onToggle={() => setOpenId(openId === s.id ? null : s.id)}
          />
        ))}
      </div>
    </div>
  );
}

function SeriesCard({
  s,
  open,
  onToggle,
}: {
  s: MaterialSeriesDTO;
  open: boolean;
  onToggle: () => void;
}) {
  const [detail, setDetail] = useState<MaterialsDTO | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || detail) return;
    setBusy(true);
    void listSeriesMaterials(s.id)
      .then(setDetail)
      .finally(() => setBusy(false));
  }, [open, detail, s.id]);

  return (
    <section className="mat__card">
      <header className="mat__head">
        <div className="mat__id">
          {s.code ? <span className="mat__code">{s.code}</span> : null}
          <h3 className="mat__name">{s.name}</h3>
          <span className="mat__where">{s.project_name}</span>
        </div>
        {/* The way into the cut. Only once there IS one — a button that opens an
            empty review screen teaches people the button does nothing. */}
        {s.latest_edit_id ? (
          <a className="btn2 btn2--primary" href={`/cut/${s.latest_edit_id}`}>
            Mở bản dựng v{s.latest_edit_version}
          </a>
        ) : null}
        <button className="btn2" onClick={onToggle}>
          {open ? "Thu gọn" : "Xem clip"}
        </button>
      </header>

      {/* The one control this page exists for. A plain link, not a fetch: the
          browser's own downloader handles a 2 GB file, a progress bar and a
          resume, none of which is worth rebuilding in JavaScript. */}
      <a className="mat__grab" href={materialsZipUrl(s.id)}>
        <span className="mat__grab-icon" aria-hidden>
          ⬇
        </span>
        <span>
          <span className="mat__grab-t">Tải tất cả raw material</span>
          <span className="mat__grab-sub">
            {s.clip_count} clip · {s.episode_count} tập · .zip
          </span>
        </span>
      </a>

      {open ? (
        busy ? (
          <p className="rfoot">Đang tải danh sách…</p>
        ) : (
          <div className="mat__eps">
            {(detail?.episodes ?? []).map((ep) => (
              <div key={ep.scene_id}>
                <div className="mat__epttl">
                  {ep.code || ep.name}
                  <b>{ep.sequences.length} clip</b>
                </div>
                <div className="mat__clips">
                  {ep.sequences.flatMap((sq) =>
                    sq.clips.map((c) => (
                      <a
                        key={c.media_id}
                        className="mat__clip"
                        href={`/media/${c.media_id}`}
                        download={c.filename}
                        title={c.filename}
                      >
                        <span className="mat__thumb" />
                        <span className="mat__clipf">
                          {/* The sequence code alone: the card already sits under
                              its episode heading, so repeating the episode in
                              every tile spends the width on what is already
                              known. The full filename is the title attribute and
                              is what the download is called. */}
                          <b>{(sq.code || "—").split("_").pop()}</b>
                          <span>
                            v{c.take}
                            {/* The take count only earns its place when there is
                                more than one — otherwise it is noise on every row. */}
                            {sq.take_count > 1 ? ` / ${sq.take_count}` : ""}
                          </span>
                        </span>
                      </a>
                    )),
                  )}
                </div>
              </div>
            ))}
            {detail && detail.clip_count === 0 ? (
              <p className="rfoot">Series này chưa gen clip nào.</p>
            ) : null}
          </div>
        )
      ) : null}
    </section>
  );
}
