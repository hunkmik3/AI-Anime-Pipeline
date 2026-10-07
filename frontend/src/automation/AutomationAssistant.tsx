import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useAutomation } from '../store/automation';
import { VideoSourceInput } from './VideoSourceInput';

type Event = { step: number; tool: string; label: string; status: string; error?: string;
  arguments?: Record<string, unknown>; result?: { job_id?: string; saved?: boolean; before?: unknown; after?: unknown; [key: string]: unknown } };
type Turn = { id: string; message: string; reply: string; status: string; error: string; model: string; events: Event[]; created_at: string };
type Job = { id: string; kind: string; status: string; node_id: string; error: string;
  result: { stage?: string; url?: string; images?: { reference_url?: string; url?: string }[]; output?: { filename?: string }; tasks?: Record<string, { status: string }>; errors?: string[] } };
type History = { turns: Turn[]; jobs: Job[]; older_cursor: string | null };
const running = (s: string) => ['queued', 'running'].includes(s);
const jobLabels: Record<string, string> = { queued: 'Đang chờ', preparing: 'Chuẩn bị', submitting: 'Đang gửi', running: 'Đang chạy',
  succeeded: 'Hoàn tất', failed: 'Lỗi', unknown: 'Cần đối soát', cancelled: 'Đã hủy', paused: 'Tạm dừng', blocked: 'Cần xử lý' };
const turnLabels: Record<string, string> = { queued: 'Đang chờ', running: 'Đang xử lý', completed: 'Đã trả lời',
  failed: 'Chưa hoàn tất', stopped: 'Đã dừng', interrupted: 'Bị ngắt' };
const stageLabels: Record<string, string> = { materials: 'Tạo material', raccord: 'Chuẩn bị liên tục cảnh', prompts: 'Viết prompt', videos: 'Gen video', assembly: 'Ghép phim', complete: 'Hoàn tất' };

export function latestEvents(events: Event[]) {
  return [...new Map(events.map(e => [e.step, e])).values()];
}

function JobReceipt({ job, projectId }: { job: Job; projectId: string }) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState('');
  async function downloadFilm() {
    setDownloading(true); setDownloadError('');
    try {
      const response = await fetch(`/api/automation/projects/${projectId}/production-runs/${job.id}/film`);
      if (!response.ok) throw new Error('Chưa tải được bản dựng (' + response.status + ').');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = job.result.output?.filename || 'film.mp4';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) { setDownloadError((e as Error).message); }
    finally { setDownloading(false); }
  }
  const tasks = Object.values(job.result.tasks ?? {});
  const url = job.result.output?.filename ? `/api/automation/projects/${projectId}/production-runs/${job.id}/film`
    : job.result.url ?? job.result.images?.[0]?.reference_url ?? job.result.images?.[0]?.url;
  return <div className={`agent-chat__job ${job.status === 'succeeded' ? 'is-complete' : ''}`}>
    <strong>{jobLabels[job.status] ?? job.status}</strong>
    <span>{job.kind === 'production_run' ? stageLabels[job.result.stage ?? ''] ?? 'Pipeline' : job.node_id}</span>
    {tasks.length > 0 && <small>{tasks.filter(t => t.status === 'succeeded').length}/{tasks.length} tác vụ đã xong</small>}
    {(job.error || job.result.errors?.length) ? <p role="alert">{job.error || job.result.errors?.join('\n')}</p> : null}
    {url && job.status === 'succeeded' && (job.result.output?.filename
      ? <button type="button" className="auto-btn" disabled={downloading} onClick={() => void downloadFilm()}>{downloading ? 'Đang tải…' : 'Tải bản dựng ↓'}</button>
      : <a href={url} target="_blank" rel="noreferrer">Mở kết quả ↗</a>)}
    {downloadError && <p role="alert">{downloadError}</p>}
  </div>;
}

export function AutomationAssistant({ open, onClose, onBusy, selectedIds }: {
  open: boolean; onClose(): void; onBusy(value: boolean): void; selectedIds?: string[];
}) {
  const pid = useAutomation(s => s.currentProjectId);
  const title = useAutomation(s => s.title);
  const freeSelected = useAutomation(s => s.nodes.filter(n => n.selected).map(n => n.id).join('|'));
  const selected = selectedIds ? selectedIds.join('|') : freeSelected;
  const [data, setData] = useState<History>({ turns: [], jobs: [], older_cursor: null });
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [older, setOlder] = useState<History>({ turns: [], jobs: [], older_cursor: null });
  const [olderLoaded, setOlderLoaded] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const session = useRef(0);
  const applied = useRef(new Set<string>());
  const active = data.turns.find(t => running(t.status));
  const busy = sending || Boolean(active);

  useEffect(() => { onBusy(busy); return () => onBusy(false); }, [busy, onBusy]);
  useEffect(() => {
    const serial = ++session.current;
    setData({ turns: [], jobs: [], older_cursor: null }); setOlder({ turns: [], jobs: [], older_cursor: null }); setOlderLoaded(false); setLoadingOlder(false); setError(''); setDraft(''); setUploadOpen(false); setSending(false);
    if (!pid) return;
    setLoading(true);
    let fetching = false, initialized = false;
    async function poll() {
      if (fetching) return;
      fetching = true;
      try {
        const result = await api<History>(`/api/automation/projects/${pid}/assistant`);
        if (serial !== session.current) return;
        if (!initialized) {
          // Opening a chat should never overwrite a user's in-progress canvas edits.
          for (const turn of result.turns) if (!running(turn.status)) applied.current.add(turn.id);
          initialized = true;
        }
        const changed = result.turns.filter(t => !running(t.status) && !applied.current.has(t.id)
          && t.events.some(e => e.status === 'completed' && e.result?.saved));
        if (changed.length && useAutomation.getState().currentProjectId === pid) {
          await useAutomation.getState().openProject(pid!);
          changed.forEach(t => applied.current.add(t.id));
        }
        if (serial !== session.current) return;
        setData(result); setError('');
      } catch (e) { if (serial === session.current) setError((e as Error).message); }
      finally { fetching = false; if (serial === session.current) setLoading(false); }
    }
    void poll();
    const timer = setInterval(() => void poll(), 2500);
    return () => { ++session.current; clearInterval(timer); };
  }, [pid]);
  const last = data.turns.at(-1);
  useEffect(() => { if (open) end.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, [last?.id, last?.reply, last?.events.length, open]);

  const send = useCallback(async (message: string) => {
    if (!pid || !message.trim() || busy) return;
    const serial = session.current;
    setSending(true); setError('');
    try {
      const state = useAutomation.getState();
      await state.saveNow();
      const saved = useAutomation.getState();
      if (saved.currentProjectId !== pid || serial !== session.current) return;
      if (saved.saveState === 'error') throw new Error(saved.saveError || 'Chưa lưu được board.');
      // Reuse the same request ID after an ambiguous network response, not a new billed turn.
      const storageKey = `automation-assistant:pending:${pid}`;
      let requestKey = crypto.randomUUID();
      try {
        const previous = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
        if (previous?.message === message.trim()) requestKey = previous.key;
        sessionStorage.setItem(storageKey, JSON.stringify({ message: message.trim(), key: requestKey }));
      } catch { /* The in-flight UI guard still applies without browser storage. */ }
      const result = await api<Turn>(`/api/automation/projects/${pid}/assistant`, { method: 'POST', body: JSON.stringify({
        message: message.trim(), request_key: requestKey, expected_revision: saved.projectRevision,
        selected_node_ids: selected ? selected.split('|').slice(0, 40) : [],
      }) });
      if (serial !== session.current) return;
      try { sessionStorage.removeItem(storageKey); } catch { /* optional storage */ }
      setData(current => ({ ...current, turns: [...current.turns.filter(t => t.id !== result.id), result] }));
      setDraft(''); setUploadOpen(false);
    } catch (e) { if (serial === session.current) setError((e as Error).message); }
    finally { if (serial === session.current) setSending(false); }
  }, [pid, busy, selected]);

  async function stop() {
    if (!active || !pid) return;
    try {
      const turn = await api<Turn>(`/api/automation/projects/${pid}/assistant/${active.id}/stop`, { method: 'POST' });
      setData(current => ({ ...current, turns: current.turns.map(t => t.id === turn.id ? turn : t) }));
    } catch (e) { setError((e as Error).message); }
  }
  const olderCursor = olderLoaded ? older.older_cursor : data.older_cursor;
  const visibleTurns = [...new Map([...older.turns, ...data.turns].map(t => [t.id, t])).values()];
  const linkedJobs = [...older.jobs, ...data.jobs];
  async function loadOlder() {
    if (!olderCursor || !pid || loadingOlder) return;
    const serial = session.current;
    setLoadingOlder(true);
    try {
      const history = await api<History>(`/api/automation/projects/${pid}/assistant?before=${olderCursor}`);
      if (serial !== session.current) return;
      setOlder(current => ({ turns: [...history.turns, ...current.turns], jobs: [...history.jobs, ...current.jobs], older_cursor: history.older_cursor }));
      setOlderLoaded(true);
    } catch (e) { if (serial === session.current) setError((e as Error).message); }
    finally { if (serial === session.current) setLoadingOlder(false); }
  }

  if (!open) return null;
  return <aside className="agent-chat" aria-label="Chat với Agent">
    <header className="agent-chat__head">
      <div><strong><span className="agent-chat__dot" />Studio Agent</strong><small>GPT · Avis</small></div>
      <button className="auto-btn" type="button" onClick={onClose} aria-label="Thu gọn chat">✕</button>
    </header>
    <div className="agent-chat__context"><span>BOARD HIỆN TẠI</span><strong title={title}>{title || 'Chưa có phim'}</strong>
      {selected && <small>{selected.split('|').length} node đang chọn</small>}</div>
    <div className="agent-chat__messages" role="log" aria-label="Lịch sử trò chuyện" aria-live="polite">
      {loading && <p className="agent-chat__muted">Đang tải hội thoại…</p>}
      {!loading && !data.turns.length && <div className="agent-chat__welcome">
        <h2>Bạn muốn làm gì tiếp?</h2>
        <p>Giao việc trên board này. Agent đọc shotlist, sửa prompt, tạo ảnh và chạy các clip bạn yêu cầu.</p>
        <div className="agent-chat__suggestions">{['Board này còn thiếu những gì?', 'Đọc shotlist và thoại của clip 1', 'Viết prompt clip 1, chưa gen video'].map(text =>
          <button key={text} type="button" onClick={() => setDraft(text)} disabled={!pid}>{text}<span>↗</span></button>)}</div>
      </div>}
      {olderCursor && <button disabled={loadingOlder} type="button" className="auto-btn" onClick={() => void loadOlder()}>Xem tin nhắn cũ</button>}
      {visibleTurns.map(turn => <div key={turn.id} className="agent-chat__turn">
        <div className="agent-chat__user">{turn.message}</div>
        <div className="agent-chat__answer">
          <div className="agent-chat__answer-label">Studio Agent <small>{turnLabels[turn.status] ?? turn.status}</small></div>
          <p>{turn.reply || (running(turn.status) ? 'Đang đọc yêu cầu và dữ liệu board…' : '')}</p>
          {latestEvents(turn.events).map(event => <div key={event.step} className="agent-chat__action">
            <details><summary><span className={event.status === 'failed' ? 'is-error' : ''}>{event.status === 'failed' ? '!' : event.status === 'completed' ? '✓' : '◌'}</span>{event.label}</summary>
              <div className="agent-chat__action-detail">{event.error && <p role="alert">{event.error}</p>}
                {event.result?.saved && <><strong>Trước</strong><pre>{typeof event.result.before === 'string' ? event.result.before : JSON.stringify(event.result.before, null, 2)}</pre>
                  <strong>Sau</strong><pre>{typeof event.result.after === 'string' ? event.result.after : JSON.stringify(event.result.after, null, 2)}</pre></>}
                {!event.result?.saved && <pre>{JSON.stringify(event.result ?? event.arguments, null, 2)}</pre>}
              </div>
            </details>
            {event.result?.job_id && linkedJobs.find(j => j.id === event.result?.job_id) &&
              <JobReceipt job={linkedJobs.find(j => j.id === event.result?.job_id)!} projectId={pid!} />}
          </div>)}
          {turn.error && <p className="agent-chat__error" role="alert">{turn.error}</p>}
        </div>
      </div>)}
      {uploadOpen && <section className="agent-chat__upload"><header><strong>Thêm video nguồn</strong><button className="auto-btn" onClick={() => setUploadOpen(false)}>Đóng</button></header><VideoSourceInput /></section>}
      <div ref={end} />
    </div>
    <form className="agent-chat__composer" onSubmit={e => { e.preventDefault(); void send(draft); }}>
      {!pid && <p className="agent-chat__muted">Tạo hoặc mở một board để bắt đầu chat.</p>}
      {error && <p className="agent-chat__error" role="alert">{error}</p>}
      <textarea aria-label="Yêu cầu cho Agent" placeholder="Ví dụ: Gen clip 2–4 ở 480p, giữ nguyên thoại…" maxLength={8000}
        value={draft} onChange={e => setDraft(e.target.value)} disabled={!pid || busy} rows={3}
        onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(draft); } }} />
      <div className="agent-chat__composer-actions">
        <button type="button" className="auto-btn" disabled={!pid || busy} onClick={() => setUploadOpen(!uploadOpen)}>＋ Video nguồn</button>
        {active ? <button type="button" className="auto-btn" onClick={() => void stop()}>Dừng agent</button> :
          <button type="submit" className="auto-btn auto-btn--primary" disabled={!pid || busy || !draft.trim()}>{sending ? 'Đang gửi…' : 'Gửi ↑'}</button>}
      </div>
      <small>Yêu cầu gen sẽ dùng lượt tạo ảnh/video. Shift + Enter để xuống dòng.</small>
    </form>
  </aside>;
}
