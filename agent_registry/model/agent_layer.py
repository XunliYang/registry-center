# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Registration metadata for the network layer assigned to an agent."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class AgentLayer(str, Enum):
    """The layer declared by an agent registration.

    These values are deliberately kept outside ``AgentCard``.  They are
    registry metadata and therefore do not participate in AgentCard signing.
    """

    OMC = "omc"
    DOMAIN_WORKBENCH = "domain_workbench"
    CROSS_DOMAIN_COORDINATION = "cross_domain_coordination"
    UNKNOWN = "unknown"


# A separate sentinel is required for updates: an omitted layer preserves the
# stored value, while an explicit ``unknown`` clears it.
LAYER_UNSET = object()


def normalize_layer(value: Any) -> str:
    """Validate and return a layer token.

    ``None``, an empty string and unknown tokens are rejected.  Callers that
    need the update-preserve behaviour should check ``LAYER_UNSET`` before
    calling this function.
    """

    if isinstance(value, AgentLayer):
        return value.value
    if not isinstance(value, str) or not value:
        raise ValueError("layer must be a non-empty string enum value")
    try:
        return AgentLayer(value).value
    except ValueError as exc:
        values = ", ".join(layer.value for layer in AgentLayer)
        raise ValueError(f"invalid layer '{value}', expected one of: {values}") from exc


def default_layer(value: Any = LAYER_UNSET) -> str:
    """Return a normalized layer, treating an omitted value as unknown."""

    if value is LAYER_UNSET:
        return AgentLayer.UNKNOWN.value
    return normalize_layer(value)


@dataclass(frozen=True)
class RegistrationItem:
    """Normalized request item used by the register and update endpoints."""

    agent_card: Dict[str, Any]
    layer: Any = LAYER_UNSET
    wrapped: bool = False


def normalize_registration_item(item: Any) -> RegistrationItem:
    """Normalize the legacy and layer-aware registration item formats.

    Legacy items are plain AgentCard objects.  Layer-aware items wrap the
    card in ``agentCard`` and put ``layer`` beside it.  Mixing the two shapes
    is rejected so a submitted layer can never be silently discarded.
    """

    if not isinstance(item, dict):
        raise ValueError("each agentCards item must be an object")

    if "agentCard" in item:
        extra = set(item) - {"agentCard", "layer"}
        if extra:
            fields = ", ".join(sorted(extra))
            raise ValueError(f"layer-aware registration item cannot mix AgentCard fields: {fields}")
        card = item["agentCard"]
        if not isinstance(card, dict):
            raise ValueError("agentCard must be an object")
        layer = item["layer"] if "layer" in item else LAYER_UNSET
        if layer is not LAYER_UNSET:
            layer = normalize_layer(layer)
        return RegistrationItem(agent_card=card, layer=layer, wrapped=True)

    if "layer" in item:
        raise ValueError("layer must be placed beside agentCard in the layer-aware format")
    return RegistrationItem(agent_card=item, layer=LAYER_UNSET, wrapped=False)
