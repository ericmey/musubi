"""Render operator-owned 1Password references before they reach systemd."""

from pathlib import Path

import pytest
from jinja2 import Environment, StrictUndefined

TEMPLATES = Path(__file__).resolve().parents[2] / "deploy" / "ansible" / "templates"
REF_KEYS = (
    "jwt_signing_key",
    "qdrant_api_key",
    "lifecycle_llm_api_key",
    "tei_dense_url",
    "tei_sparse_url",
    "tei_reranker_url",
    "tei_basic_auth_username",
    "tei_basic_auth_password",
    "shared_inference_tls_cert",
    "shared_inference_tls_key",
)


def _missing(*, hint: str) -> None:
    raise ValueError(hint)


def _render(name: str, **vars: object) -> str:
    env = Environment(trim_blocks=True, keep_trailing_newline=True, undefined=StrictUndefined)
    env.globals["undef"] = _missing
    return env.from_string((TEMPLATES / name).read_text()).render(**vars)


def test_reference_templates_render_operator_values_and_consumer_rows() -> None:
    refs = {key: f"op://Example Vault/{key}/credential" for key in REF_KEYS}
    vars = {
        "musubi_op_refs": refs,
        "musubi_config_dir": "/etc/musubi",
        "musubi_shared_inference_auth_consumers": [
            {"name": "alex", "ref": "op://Example Vault/alex/hash"},
            {"name": "sam", "ref": "op://Example Vault/sam/hash"},
        ],
        "musubi_shared_inference_transition_consumers": [
            {"name": "legacy", "ref": "op://Example Vault/legacy/hash"},
            {"name": "alex", "ref": "op://Example Vault/alex/hash"},
        ],
    }

    secrets = _render("secrets.tpl.j2", **vars)
    assert "JWT_SIGNING_KEY=op://Example Vault/jwt_signing_key/credential" in secrets
    assert (
        "TEI_BASIC_AUTH_PASSWORD=op://Example Vault/tei_basic_auth_password/credential" in secrets
    )
    assert _render("qdrant.token.tpl.j2", **vars) == (
        "{{ op://Example Vault/qdrant_api_key/credential }}\n"
    )
    unit = _render("shared-inference.service.j2", **vars)
    assert "'op://Example Vault/shared_inference_tls_cert/credential'" in unit
    assert "'op://Example Vault/shared_inference_tls_key/credential'" in unit
    assert _render("shared-inference.htpasswd.tpl.j2", **vars) == (
        "alex:op://Example Vault/alex/hash\nsam:op://Example Vault/sam/hash\n"
    )
    assert _render("shared-inference.htpasswd.transition.tpl.j2", **vars) == (
        "legacy:op://Example Vault/legacy/hash\nalex:op://Example Vault/alex/hash\n"
    )


@pytest.mark.parametrize(
    ("template", "vars"),
    [
        ("secrets.tpl.j2", {}),
        ("secrets.tpl.j2", {"musubi_op_refs": {"jwt_signing_key": ""}}),
        ("shared-inference.htpasswd.tpl.j2", {}),
        ("shared-inference.htpasswd.tpl.j2", {"musubi_shared_inference_auth_consumers": []}),
        (
            "shared-inference.htpasswd.transition.tpl.j2",
            {"musubi_shared_inference_transition_consumers": []},
        ),
    ],
)
def test_missing_or_empty_operator_config_fails_render(
    template: str, vars: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _render(template, **vars)
