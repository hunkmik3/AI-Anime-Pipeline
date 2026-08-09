import { useCallback, useEffect, useState } from "react";

import { toast } from "../../store/toast";

/**
 * Sequences that have used their attempts and are waiting on a decision.
 *
 * A queue, not a notification. Nothing is sent when the fifth attempt lands:
 * the sequence appears here and leaves when the limit is lifted or the work
 * moves on. A notification would need somewhere to live, would need marking
 * read, and would still be announcing a state that is already on the screen.
 *
 * The number of attempts is the point of the page, so it is the thing shown —
 * "7 / 5" says at a glance that this one has been fought with, which is the
 * signal a PM is here to act on.
 */

type Row = {
  shot_id: string;
  code: string;
  episode: string;
  episode_id: string;
  series: string;
  project_id: string;
  used: number;
  limit: number;
  unlocked: boolean;
};

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export function BlockedSequencesTab() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await json<Row[]>("/api/admin/sequences/blocked"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(row: Row, what: "unlock" | "relock", extra = 5) {
    setBusy(row.shot_id);
    try {
      await json(`/api/admin/sequences/${row.shot_id}/${what}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: what === "unlock" ? JSON.stringify({ extra }) : undefined,
      });
      toast(
        what === "unlock"
          ? `${row.code || "Sequence"} mở thêm ${extra} lượt`
          : `${row.code || "Sequence"} về hạn mức mặc định`,
      );
      await load();
    } catch {
      toast("Không được", "error");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <div className="admin-error">{error}</div>;
  if (!rows) return <div className="admin-loading">Loading…</div>;

  if (rows.length === 0) {
    return (
      <div className="blockseq">
        <p className="blockseq__note">
          Không có sequence nào đang chờ. Một sequence xuất hiện ở đây khi nó
          dùng hết 5 lượt gen — nghĩa là những cách hiển nhiên đã thử rồi, và
          bước tiếp theo thường là đổi prompt hoặc đổi ảnh tham chiếu chứ không
          phải gen lại lần nữa.
        </p>
      </div>
    );
  }

  return (
    <div className="blockseq">
      <p className="blockseq__note">
        <b>{rows.length}</b> sequence đã dùng hết lượt gen và đang chờ bạn xem.
        Mở khoá là cấp thêm lượt cho <em>chính sequence đó</em> — không đụng tới
        ngân sách của ai.
      </p>

      <div className="blockseq__scroll">
        <table className="blockseq__table">
          <thead>
            <tr>
              <th>Sequence</th>
              <th>Episode</th>
              <th>Series</th>
              <th className="num">Đã gen</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.shot_id}>
                <td className="blockseq__code">{r.code || "—"}</td>
                <td>{r.episode}</td>
                <td className="blockseq__muted">{r.series}</td>
                <td className="num">
                  {/* Over the ceiling is possible: a run already in flight is
                      not refused retrospectively. Saying 7/5 is more use than
                      clamping it to 5/5 and hiding that it was fought with. */}
                  <b className={r.used > r.limit ? "is-over" : ""}>{r.used}</b>
                  <span className="blockseq__of"> / {r.limit}</span>
                  {r.unlocked ? <em className="blockseq__tag">đã mở</em> : null}
                </td>
                <td className="blockseq__acts">
                  <a
                    className="blockseq__open"
                    href={`/projects/${r.project_id}/scenes/${r.episode_id}`}
                  >
                    Xem canvas →
                  </a>
                  <button
                    className="blockseq__btn"
                    disabled={busy === r.shot_id}
                    onClick={() => void act(r, "unlock", 5)}
                  >
                    + 5 lượt
                  </button>
                  {r.unlocked ? (
                    <button
                      className="blockseq__btn blockseq__btn--quiet"
                      disabled={busy === r.shot_id}
                      title="Về hạn mức mặc định"
                      onClick={() => void act(r, "relock")}
                    >
                      Khoá lại
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
