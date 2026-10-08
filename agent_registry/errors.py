# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

class RegistryUnavailableError(RuntimeError):
    """The deployment cannot answer this request (503, not a server fault).

    The request is well formed and the process is healthy; the registry simply
    lacks the store or the model needed for this operation. Wrappers that catch
    ``Exception`` around a handler chain must therefore re-raise these unchanged:
    turning one into "Internal server error" contradicts the documented contract
    and hides an actionable deployment problem.
    """


class SemanticSearchUnavailable(RegistryUnavailableError):
    """Model configuration, invocation or output failed; not a no-match result."""


class AuthoritativeStoreUnavailable(RegistryUnavailableError):
    """The registry has no authoritative record store (``use_vectordb=true``).

    The vector collection holds cards, but it cannot answer the questions the
    registry's own API is defined in terms of: which cards are approved, who owns
    them, what tags they carry, and what a change feed subscriber must be told.
    Answering those from an index would be a guess, so every entry point that
    depends on the authoritative record reports this error instead of returning a
    plausible empty result.
    """


MESSAGE_AUTHORITATIVE_STORE_UNAVAILABLE = (
    "registry.use_vectordb=true replaces the authoritative record store, so "
    "{operation} is unavailable: approval status, ownership, tags and the change "
    "feed cannot be answered from a vector index. Deploy with use_vectordb=false."
)


def authoritative_store_unavailable(operation: str) -> AuthoritativeStoreUnavailable:
    return AuthoritativeStoreUnavailable(
        MESSAGE_AUTHORITATIVE_STORE_UNAVAILABLE.format(operation=operation))
