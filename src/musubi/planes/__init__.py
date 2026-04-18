"""Plane implementations — CRUD + lifecycle surface for each memory plane.

Each plane owns one Qdrant collection (via ``collection_for_plane``). Writes
go through the plane; no other module calls :meth:`QdrantClient.upsert`
directly. State changes always emit a :class:`LifecycleEvent` — that's the
audit log for everything that ever happened.
"""
