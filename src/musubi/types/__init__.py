"""Shared pydantic types.

Every public payload crossing a module boundary is a pydantic model defined
here. See slice-types in the vault's ``_slices/`` registry and the specs in
``04-data-model/`` for design.

Populated incrementally; import errors before ``slice-types`` is merged are
expected.
"""
