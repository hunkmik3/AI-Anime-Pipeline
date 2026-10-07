import { useEffect, useRef } from "react";
import { type AutoNode } from "../store/automation";
import type { ClipMaterial } from "./clipGroups";
import {
  AutoAssetNode, AutoCharacterNode, AutoEnvironmentNode,
} from "./nodes";

const KIND_LABEL = { character: "Nhân vật", environment: "Bối cảnh", prop: "Đạo cụ", background_group: "Quần chúng", reference: "Reference", keyframe: "Keyframe" };

export function MaterialCard({ material, onEdit }: { material: ClipMaterial; onEdit(id: string): void }) {
  return (
    <article className="clip-material">
      <div className="clip-material__type">
        <span>{KIND_LABEL[material.kind]}</span>
        {material.usedBy > 1 && <span title={`Cùng một material được dùng trong ${material.usedBy} clip`}>Dùng chung · {material.usedBy}</span>}
      </div>
      <strong>{material.name}</strong>
      {material.previews.length ? material.previews.map(({ key, name, plate }) => {
        const url = plate.referenceUrl || plate.image;
        return <div key={key} className="clip-material__preview">
          {url ? <a draggable={false} href={url} target="_blank" rel="noreferrer" title={`Xem ảnh ${material.name} — ${name}`}>
            <img src={url} alt={`${material.name} — ${name}`} loading="lazy" draggable={false} />
          </a> : <div className="clip-material__placeholder">{plate.status === "running" ? "Đang tạo ảnh…" : plate.status === "error" ? "Tạo ảnh lỗi" : "Chưa có ảnh"}</div>}
          <small>{name}</small>
          {plate.error && <p className="auto-error">{plate.error}</p>}
        </div>;
      }) : <p className="clip-material__placeholder">Chưa có material trên board</p>}
      {material.node && <button type="button" className="auto-btn nodrag" onClick={() => onEdit(material.node!.id)}>Tùy chỉnh ảnh & prompt</button>}
    </article>
  );
}

export function MaterialEditor({ node, onClose }: { node: AutoNode; onClose(): void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  const props = { id: node.id, data: node.data, selected: false };
  return <dialog ref={dialog} className="clip-material-dialog" onCancel={onClose} aria-label="Chi tiết material">
    <div className="clip-material-dialog__head">
      <strong>Chi tiết material</strong>
      <button type="button" className="auto-btn" onClick={onClose} autoFocus>Đóng</button>
    </div>
    <p className="auto-hint">Chỉnh sửa ở đây sẽ cập nhật material dùng chung trong các clip liên quan.</p>
    {node.data.kind === "character" && <AutoCharacterNode {...props} />}
    {node.data.kind === "environment" && <AutoEnvironmentNode {...props} />}
    {node.data.kind === "asset" && <AutoAssetNode {...props} />}
  </dialog>;
}
