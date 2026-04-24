"""Hard-delete plumbing worker."""

import logging

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

from musubi.config import get_settings
from musubi.observability.registry import default_registry
from musubi.sdk import MusubiClient
from musubi.store.names import COLLECTION_NAMES
from musubi.types.common import epoch_of, utc_now

CLEANUP_DELETED = default_registry().counter(
    "musubi_cleanup_deleted_total",
    "Total rows hard-deleted by the cleanup worker",
    labelnames=("collection",),
)


log = logging.getLogger("musubi.ops.cleanup")


def run_cleanup(
    qdrant: QdrantClient,
    sdk: MusubiClient,
    tombstone_ttl_days: int = 30,
) -> dict[str, int]:
    """Sweep Qdrant collections for archived rows past their TTL and hard-delete them."""
    now_epoch = epoch_of(utc_now())
    cutoff_epoch = now_epoch - (tombstone_ttl_days * 24 * 3600)
    metrics: dict[str, int] = dict.fromkeys(COLLECTION_NAMES, 0)

    settings = get_settings()
    blob_dir = settings.artifact_blob_path

    for collection in COLLECTION_NAMES:
        offset = None
        while True:
            resp = qdrant.scroll(
                collection_name=collection,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="state", match=MatchValue(value="archived")),
                        FieldCondition(key="updated_epoch", range=Range(lt=cutoff_epoch)),
                    ]
                ),
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, offset = resp[0], resp[1]
            for pt in points:
                if not pt.payload:
                    continue
                namespace = pt.payload.get("namespace")
                obj_id = pt.payload.get("object_id")
                if not namespace or not obj_id:
                    continue

                if collection == "musubi_episodic":
                    sdk._json(
                        "DELETE",
                        f"/episodic/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                elif collection == "musubi_curated":
                    sdk._json(
                        "DELETE",
                        f"/curated/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                elif collection == "musubi_concept":
                    sdk._json(
                        "DELETE",
                        f"/concepts/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                elif collection == "musubi_artifact":
                    sdk._json(
                        "DELETE", f"/artifacts/{obj_id}/purge", params={"namespace": namespace}
                    )
                    # Also delete the blob for artifacts
                    blob_path = blob_dir / namespace / obj_id
                    if blob_path.exists():
                        blob_path.unlink()
                elif collection == "musubi_thought":
                    sdk._json(
                        "DELETE",
                        f"/thoughts/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )

                metrics[collection] += 1
                CLEANUP_DELETED.labels(collection=collection).inc()

            if offset is None:
                break
    return metrics
