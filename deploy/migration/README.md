# Migration Runbook

This documents the procedure for migrating legacy Node.js POC data into Musubi v1.

## Prerequisites

1.  **Stop the POC:** Ensure nothing is writing to the old POC database.
2.  **Start Musubi v1:** The v1 system must be running on `musubi.example.local`.
3.  **Authentication:** Obtain an operator-scoped token for Musubi v1.
4.  **BACKUP:** You MUST backup the target Musubi v1 Qdrant database before proceeding.
    ```bash
    docker exec musubi-qdrant qdrant-snapshot-create
    ```

## Execution

1.  Navigate to the `deploy/migration` directory on `control.example.local`.
2.  Configure your environment variables:
    ```bash
    export SOURCE_QDRANT_HOST="127.0.0.1"
    export SOURCE_QDRANT_PORT="6333"
    export MUSUBI_URL="https://musubi.example.local/v1"
    export MUSUBI_TOKEN="<your-operator-token>"
    ```
3.  Run a **dry run** to validate schemas and review expected changes:
    ```bash
    python3 poc-to-v1.py --dry-run
    ```
4.  If the dry run reports no unexpected failures, execute the **real migration**:
    ```bash
    python3 poc-to-v1.py --i-have-a-backup
    ```

## Resumption

The migrator keeps track of its progress in `state.json`. If it crashes or is interrupted, simply run it again. It will skip rows that have already been migrated.

## Rollback

If the migration corrupts the v1 data, restore from the snapshot taken in the Prerequisites step:
```bash
docker exec musubi-qdrant qdrant-snapshot-restore <snapshot-name>
```