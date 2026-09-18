"""Shared, cross-cutting utilities.

Vendored utilities (no external dependency on the SDK they originated from):
* `models`   — `DiscriminatedUnionMixin` for polymorphic pydantic models.
* `env_parser` — typed environment-variable parsing.

Additional shared helpers:
* `secret_serialization` — context-driven serialize/deserialize for
  `SecretStr` / secret-bearing fields (issue #7).
"""
