import { parseServerTimeMs } from "../utils/serverTime";

/** Board update time, displayed in the viewer's local timezone. */
export function projectTime(iso: string, now = Date.now()) {
  const ms = parseServerTimeMs(iso);
  if (!Number.isFinite(ms)) {
    return { label: "Chưa rõ thời gian", title: "Chưa có thời gian cập nhật hợp lệ", dateTime: undefined };
  }
  const date = new Date(ms);
  const minutes = Math.floor(Math.max(0, now - ms) / 60_000);
  const label = minutes < 1 ? "vừa xong"
    : minutes < 60 ? `${minutes} phút trước`
    : minutes < 1440 ? `${Math.floor(minutes / 60)} giờ trước`
    : date.toLocaleDateString("vi-VN");
  const exact = date.toLocaleString("vi-VN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23", timeZoneName: "short",
  });
  return { label, title: `Cập nhật: ${exact}`, dateTime: date.toISOString() };
}
