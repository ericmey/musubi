# Use it

## Namespaces and planes

Every memory lives in a namespace, `tenant/name/plane`. The plane decides
what kind of memory it is:

- **episodic**: what happened. Agents capture these as they work. The
  lifecycle engine scores, matures and eventually demotes them.
- **curated**: reviewed knowledge. It lives as Markdown in the vault, where a
  human can read and edit it.
- **concept**: patterns the lifecycle engine proposes from many episodic
  memories, waiting to be promoted into curated knowledge.

Agents also send each other short **thoughts**, addressed to a presence.
[The three planes](../../README.md#the-three-planes) explains why they're
split this way.

## Capture and recall with the SDK

```python
from musubi_sdk import MusubiClient

ns = "acme/assistant/episodic"

with MusubiClient(base_url="http://127.0.0.1:8100/v1", token=token) as client:
    client.episodic.capture(
        namespace=ns,
        content="The team agreed to ship the beta on Friday after the security review.",
        tags=["release"],
        importance=7,
    )
    hits = client.retrieve(namespace=ns, query_text="when does the beta ship?", mode="fast")
```

`mode` selects the retrieval path:

- `fast`: hybrid search without the reranker, built for voice and chat latency.
- `deep`: adds the cross-encoder reranker, for when quality matters more than speed.
- `blended`: several planes in one ranked list.
- `recent`: newest first, no query needed.

The `with` block closes the client's connections when it ends; call
`client.close()` yourself if you keep a client open instead.
`AsyncMusubiClient` has the same methods for async code.

## Build a context pack

`musubi-context`, a command in the `musubi` Python package, retrieves a
ranked context pack from a running server for use in an agent prompt. Always
pass `--namespace`; there is no default.

## Where to go next

- [Retrieval](../Musubi/05-retrieval/index.md): scoring, modes and
  degradation warnings.
- [Lifecycle](../Musubi/06-ingestion/lifecycle-engine.md): how captures
  mature, get promoted and fade.
- [Canonical API](../Musubi/07-interfaces/canonical-api.md): every endpoint.
