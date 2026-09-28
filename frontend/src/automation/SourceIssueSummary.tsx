import type { SourceIssueCategory, SourceVerification } from "./contracts";

export const SOURCE_ISSUE_LABELS: Record<SourceIssueCategory, string> = {
  technical: "Dữ liệu cần kiểm tra",
  visual: "Hình ảnh cần đối chiếu",
  uncertainty: "Chưa đủ bằng chứng",
};

/** A read-only account of machine work; accepting findings never rewrites it. */
export function SourceIssueSummary({ report }: { report: SourceVerification }) {
  const summary = report.issue_summary;
  if (!summary) return null;
  const accepted = Boolean(report.review?.accepted_by);
  const unresolved = new Set(report.unresolved_shots ?? summary.unresolved_shots);
  const correctedVerified = new Set(summary.corrected_and_verified_shots.filter((n) => !unresolved.has(n)));
  return <div>
    <p className="va-qa-summary">
      <strong>Đã sửa và xác nhận: {correctedVerified.size} shot</strong>
      {!accepted && <> · Cần kiểm tra: {unresolved.size} shot · {summary.active_issues} mục đang mở</>}
    </p>
    {accepted && <p className="auto-hint">
      {summary.active_issues} mục đã được chấp nhận thủ công; đây không phải xác nhận tự động của AI.
    </p>}
    <p className="auto-hint">
      {SOURCE_ISSUE_LABELS.technical}: {summary.by_category.technical}
      {` · ${SOURCE_ISSUE_LABELS.visual}: ${summary.by_category.visual}`}
      {` · ${SOURCE_ISSUE_LABELS.uncertainty}: ${summary.by_category.uncertainty}`}
    </p>
    {summary.by_category.uncertainty > 0 && <p className="auto-hint">
      Chưa đủ bằng chứng không đồng nghĩa với mô tả sai.
    </p>}
    <details>
      <summary>Chi tiết lần đối chiếu</summary>
      <p className="auto-hint">
        Đã xử lý {new Set(summary.processed_shots).size} shot
        {` · Đã sửa ${new Set(summary.corrected_shots).size} shot`}
        {` · Giữ kết quả đã xác nhận của ${new Set(summary.retained_verified_shots).size} shot`}
      </p>
      <p className="auto-hint">
        Đã gộp {summary.duplicates_collapsed} ghi chú lặp từ {summary.input_findings} ghi chú đầu vào.
        {accepted ? " Các mục đã chấp nhận vẫn được giữ bên dưới." : " Các mục còn mở vẫn được liệt kê bên dưới."}
      </p>
      {!!correctedVerified.size && <p className="auto-hint">
        Shot đã sửa và xác nhận: {[...correctedVerified].sort((a, b) => a - b).join(", ")}
      </p>}
    </details>
  </div>;
}
