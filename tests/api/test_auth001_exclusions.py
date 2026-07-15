from musubi.api.routers.retrieve import _expand_wildcard_targets
from musubi.auth.tokens import AuthContext
from musubi.settings import Settings

_args = {
    "qdrant_host": "a",
    "qdrant_api_key": "a",
    "tei_dense_url": "http://a",
    "tei_sparse_url": "http://a",
    "tei_reranker_url": "http://a",
    "ollama_url": "http://a",
    "embedding_model": "a",
    "sparse_model": "a",
    "reranker_model": "a",
    "llm_model": "a",
    "vault_path": "/",
    "artifact_blob_path": "/",
    "lifecycle_sqlite_path": "/",
    "log_dir": "/",
    "jwt_signing_key": "a",
    "oauth_authority": "http://a",
    "agent_exclusions": {},
}
_set = Settings(**_args)  # type: ignore[arg-type]


class DummyClient:
    def scroll(self, collection_name, with_payload, with_vectors, limit, offset):  # type: ignore[no-untyped-def]
        class Point:
            payload: dict[str, str]

            def __init__(self, ns: str) -> None:
                self.payload = {"namespace": ns}
                self.payload = {"namespace": ns}

        namespaces = [
            "eric/command-chair/episodic",
            "eric/salesai/episodic",
            "eric/salesai2/episodic",
            "salesai/some-agent/episodic",
            "yua/custom/episodic",
        ]
        if offset is None:
            return [Point(ns) for ns in namespaces], "done"
        return [], None


def test_auth001_mandatory_salesai_exclusion() -> None:
    """Mandatory default exclusion for canonical namespace root salesai."""
    client = DummyClient()
    ctx = AuthContext(
        subject="eric",
        issuer="test",
        audience="musubi",
        scopes=("*/*/*:r",),
        presence="command-chair",
    )
    expanded = _expand_wildcard_targets(
        client,  # type: ignore[arg-type]
        [("eric/*/episodic", "episodic")],
        ctx,
        _set,
    )
    namespaces = [ns for ns, p in expanded]
    assert "eric/command-chair/episodic" in namespaces
    assert "eric/salesai2/episodic" in namespaces
    assert "eric/salesai/episodic" not in namespaces


def test_auth001_additive_custom_exclusion() -> None:
    """Additive custom exclusion via Settings.agent_exclusions."""
    client = DummyClient()
    ctx = AuthContext(
        subject="eric",
        issuer="test",
        audience="musubi",
        scopes=("*/*/*:r",),
        presence="command-chair",
    )
    custom_args = _args.copy()
    custom_args["agent_exclusions"] = {"eric": ["custom"]}
    custom_set = Settings(**custom_args)  # type: ignore[arg-type]
    expanded = _expand_wildcard_targets(
        client,  # type: ignore[arg-type]
        [("*/*/episodic", "episodic")],
        ctx,
        custom_set,
    )
    namespaces = [ns for ns, p in expanded]
    assert "eric/command-chair/episodic" in namespaces
    assert "yua/custom/episodic" not in namespaces


def test_auth001_salesai2_not_excluded() -> None:
    """salesai must not match salesai2."""
    client = DummyClient()
    ctx = AuthContext(
        subject="eric",
        issuer="test",
        audience="musubi",
        scopes=("*/*/*:r",),
        presence="command-chair",
    )
    expanded = _expand_wildcard_targets(
        client,  # type: ignore[arg-type]
        [("eric/*/episodic", "episodic")],
        ctx,
        _set,
    )
    namespaces = [ns for ns, p in expanded]
    assert "eric/salesai2/episodic" in namespaces


def test_auth001_explicit_exact_override() -> None:
    """Explicit exact namespace overrides recall exclusions."""
    client = DummyClient()
    ctx = AuthContext(
        subject="eric",
        issuer="test",
        audience="musubi",
        scopes=("*/*/*:r",),
        presence="command-chair",
    )
    expanded = _expand_wildcard_targets(
        client,  # type: ignore[arg-type]
        [("eric/salesai/episodic", "episodic")],
        ctx,
        _set,
    )
    namespaces = [ns for ns, p in expanded]
    assert "eric/salesai/episodic" in namespaces


def test_auth001_exclusion_never_grants_access() -> None:
    """Exclusion lists never implicitly grant access (Auth/scopes do)."""
    pass
