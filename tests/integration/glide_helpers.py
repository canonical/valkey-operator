# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Serialization/deserialization helpers for GlideClientConfiguration objects.

Converts a GlideClientConfiguration (and its nested objects) to/from a JSON
string so it can be passed as a Juju action parameter.

Bytes fields are base64-encoded; enums are stored by name; nested Glide
objects are tagged with ``__class__`` for round-trip reconstruction.
"""

import base64
import json
from enum import Enum
from typing import Any

from glide import (
    AdvancedGlideClientConfiguration,
    BackoffStrategy,
    GlideClientConfiguration,
    NodeAddress,
    ReadFrom,
    ServerCredentials,
    TlsAdvancedConfiguration,
)

# Maps each Glide class to the set of fields that should be serialized.
# Tuple values like ``(list, NodeAddress)`` are documentation only — the
# serialize/deserialize logic recurses structurally, not via this type info.
SCHEMA: dict[type, dict[str, Any]] = {
    GlideClientConfiguration: {
        "addresses": (list, NodeAddress),
        "use_tls": bool,
        "request_timeout": (int, type(None)),
        "read_from": ReadFrom,
        "credentials": (ServerCredentials, type(None)),
        "reconnect_strategy": (BackoffStrategy, type(None)),
        "advanced_config": (AdvancedGlideClientConfiguration, type(None)),
    },
    NodeAddress: {
        "host": str,
        "port": int,
    },
    ServerCredentials: {
        "username": (str, type(None)),
        "password": (str, type(None)),
    },
    BackoffStrategy: {
        "num_of_retries": int,
        "factor": int,
        "exponent_base": int,
        "jitter_percent": (int, type(None)),
    },
    AdvancedGlideClientConfiguration: {
        "connection_timeout": (int, type(None)),
        "tls_config": (TlsAdvancedConfiguration, type(None)),
    },
    TlsAdvancedConfiguration: {
        "use_insecure_tls": bool,
        "client_cert_pem": (bytes, type(None)),
        "client_key_pem": (bytes, type(None)),
        "root_pem_cacerts": (bytes, type(None)),
    },
}

_GLIDE_CLASSES: dict[str, type] = {cls.__name__: cls for cls in SCHEMA}
_ENUM_CLASSES: dict[str, type[Enum]] = {"ReadFrom": ReadFrom}


def serialize(obj: Any) -> Any:
    """Recursively serialize a Glide object to a JSON-compatible structure."""
    if obj is None:
        return None
    if isinstance(obj, bytes):
        return {"__bytes__": base64.b64encode(obj).decode()}
    if isinstance(obj, Enum):
        return {"__enum__": type(obj).__name__, "value": obj.name}
    if type(obj) in SCHEMA:
        return {
            "__class__": type(obj).__name__,
            **{field: serialize(getattr(obj, field)) for field in SCHEMA[type(obj)]},
        }
    if isinstance(obj, list):
        return [serialize(i) for i in obj]
    return obj  # str, int, bool, None


def deserialize(d: Any) -> Any:
    """Recursively deserialize a JSON-compatible structure back to Glide objects."""
    if d is None or not isinstance(d, (dict, list)):
        return d
    if isinstance(d, list):
        return [deserialize(i) for i in d]
    if "__bytes__" in d:
        return base64.b64decode(d["__bytes__"])
    if "__enum__" in d:
        cls = _ENUM_CLASSES[d["__enum__"]]
        return cls[d["value"]]
    if "__class__" in d:
        cls = _GLIDE_CLASSES[d["__class__"]]
        fields = {k: deserialize(v) for k, v in d.items() if k != "__class__"}
        return cls(**fields)
    return d


def serialize_glide_config(config: GlideClientConfiguration) -> str:
    """Serialize a GlideClientConfiguration to a JSON string."""
    return json.dumps(serialize(config))


def deserialize_glide_config(payload: str) -> GlideClientConfiguration:
    """Deserialize a JSON string back to a GlideClientConfiguration."""
    return deserialize(json.loads(payload))
