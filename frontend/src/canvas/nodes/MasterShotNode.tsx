import { useEffect, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { getSceneBible, mediaUrl, patchNode } from "../../api/client";
import { useSceneStore } from "../../store/scene";
import { useShotStore } from "../../store/shot";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";

function MasterShotBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // The MasterShot mediaId is stored directly on data.mediaId so the
  // existing thumbnail rendering surfaces just work. data.masterShotAssetId
  // is the numeric Asset PK from the scene's master_establishing_asset_id.
  const mediaId = data.mediaId;

  const currentShot = useShotStore((s) => s.currentShot);
  const sceneId = currentShot?.scene_id ?? useSceneStore.getState().currentSceneId;

  async function loadFromScene() {
    if (!sceneId) {
      setError("no scene");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const bible = await getSceneBible(sceneId);
      const assetId = bible.master_establishing_asset_id;
      if (!assetId) {
        setError("scene has no master establishing shot");
        useShotWorkflowStore.getState().updateNodeData(rfId, {
          masterShotAssetId: undefined,
          mediaId: undefined,
        });
        return;
      }
      // Phase 6: the GET response now includes
      // ``master_establishing_media_id`` so we can populate
      // ``data.mediaId`` here — that drives both the node thumbnail
      // (via mediaUrl) and the wire-side ref payload
      // (``collectUpstreamRefMediaIds`` picks it up automatically once
      // ``master_shot`` joined REF_SOURCE_TYPES).
      const resolvedMediaId = bible.master_establishing_media_id ?? undefined;
      useShotWorkflowStore.getState().updateNodeData(rfId, {
        masterShotAssetId: assetId,
        mediaId: resolvedMediaId,
      });
      const dbId = parseInt(rfId, 10);
      if (!isNaN(dbId)) {
        patchNode(dbId, {
          data: { masterShotAssetId: assetId, mediaId: resolvedMediaId },
        }).catch(() => {});
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "load failed");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (data.masterShotAssetId === undefined && data.mediaId === undefined) {
      void loadFromScene();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="node-body node-body--visual-asset">
      {mediaId ? (
        <div className="visual-asset__media">
          <img
            className="visual-asset__image"
            src={mediaUrl(mediaId)}
            alt={data.title}
          />
        </div>
      ) : (
        <div className="visual-asset__empty">
          {data.masterShotAssetId ? (
            <span className="visual-asset__hint">
              Bound to asset #{data.masterShotAssetId}
            </span>
          ) : (
            <span className="visual-asset__hint">
              No master establishing shot set on this scene
            </span>
          )}
        </div>
      )}
      <button
        type="button"
        className="visual-asset__action"
        onClick={loadFromScene}
        disabled={loading}
        style={{ marginTop: 6 }}
      >
        {loading ? "Loading…" : "Pick from scene"}
      </button>
      {error && <p className="visual-asset__error" role="alert">{error}</p>}
    </div>
  );
}

export function MasterShotNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
      showTargetHandle={false}
    >
      <MasterShotBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}
