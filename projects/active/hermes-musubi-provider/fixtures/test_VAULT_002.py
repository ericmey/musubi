"""Fixture for VAULT-002 boot-scan-fix.

Tests-first red contract for the boot_scan relative-path
silent-swallow bug. Source is forbidden in this slice; the fix
lands in a separate follow-up PR.

This fixture is a placeholder. The actual test contract
is in tests/vault/test_watcher_boot_scan_vault_002.py
(VAULT-002's red contract). The fixture here is reserved
for any future shared in-memory Qdrant + vault-root harness
that the test contract may need.

The test contract itself imports only stdlib + the
existing pytest/unittest.mock + musubi.vault.watcher; it does
NOT need this fixture today. If a future change requires
a shared harness, the fixture is created in a follow-up
PR.
"""
