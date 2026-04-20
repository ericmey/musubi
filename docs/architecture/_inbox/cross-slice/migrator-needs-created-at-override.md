# Cross-slice: Migrator needs `created_at` override

**Opened by:** slice-poc-data-migration
**Target slice:** slice-api-v0-write / slice-sdk-py

## Observation
The POC data migration requires preserving `created_at` timestamps from the source data into the v1 target. The migration slice explicitly instructs to use the Musubi SDK (`client.memories.capture`) and pass an optional override parameter to preserve `created_at`. 

However, `CaptureRequest` in `src/musubi/api/routers/writes_episodic.py` does not accept a `created_at` field, and the `client.memories.capture` method in the SDK has no such parameter.

## Expectation
The canonical API and SDK should allow operator-scoped or appropriately-permissioned clients to override the `created_at` timestamp during memory capture for migration purposes, or there should be a dedicated bulk-import endpoint.

## Action needed
Update `CaptureRequest`, `BatchCaptureRequest`, and their respective handlers to accept an optional `created_at: datetime | None = None` parameter, and pass it through to the `EpisodicMemory` initialization. Update `musubi.sdk.client._Memories.capture` and `.batch().capture()` to expose this parameter.
