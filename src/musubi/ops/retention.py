"""Storage retention enforcement."""

import logging

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, Range

from musubi.observability.registry import default_registry
from musubi.sdk import MusubiClient
from musubi.store.names import collection_for_plane
from musubi.types.common import epoch_of, utc_now

RETENTION_DELETED = default_registry().counter(
    "musubi_retention_deleted_total",
    "Total rows hard-deleted by the retention worker",
    labelnames=("plane",),
)


log = logging.getLogger("musubi.ops.retention")

RETENTION_POLICIES_DAYS: dict[str, int] = {
    "thought": 30,
}


def run_retention(
    qdrant: QdrantClient,
    sdk: MusubiClient,
    policies: dict[str, int] | None = None,
) -> dict[str, int]:
    """Sweep Qdrant for rows older than their plane's retention TTL and hard-delete them."""
    if policies is None:
        policies = RETENTION_POLICIES_DAYS

    now_epoch = epoch_of(utc_now())
    metrics: dict[str, int] = {}

    for plane, ttl_days in policies.items():
        if ttl_days <= 0:
            continue

        try:
            collection = collection_for_plane(plane)
        except ValueError:
            continue

        cutoff_epoch = now_epoch - (ttl_days * 24 * 3600)
        offset = None
        count = 0

        while True:
            resp = qdrant.scroll(
                collection_name=collection,
                scroll_filter=Filter(
                    must=[
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

                if plane == "episodic":
                    sdk._json(
                        "DELETE",
                        f"/memories/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                elif plane == "curated":
                    sdk._json(
                        "DELETE",
                        f"/curated-knowledge/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                elif plane == "concept":
                    sdk._json(
                        "DELETE",
                        f"/concepts/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                elif plane == "artifact":
                    sdk._json(
                        "DELETE", f"/artifacts/{obj_id}/purge", params={"namespace": namespace}
                    )
                elif plane == "thought":
                    sdk._json(
                        "DELETE",
                        f"/thoughts/{obj_id}",
                        params={"namespace": namespace, "hard": "true"},
                    )
                count += 1
                RETENTION_DELETED.labels(plane=plane).inc()

            if offset is None:
                break
        metrics[collection] = count

    return metrics
