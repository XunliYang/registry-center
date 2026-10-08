# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared input contracts for the main and integration HTTP planes."""
import json

from a2a.types import AgentCard
from fastapi import HTTPException
from google.protobuf.json_format import Parse, ParseError


def card_batch(body: dict) -> list:
    cards = body.get('agentCards')
    if not isinstance(cards, list) or not cards:
        raise HTTPException(422, 'agentCards must be a non-empty list')
    return cards


def parse_card(value, *, name=None, organization=None) -> AgentCard:
    if not isinstance(value, dict):
        raise HTTPException(422, 'Each agentCards entry must be an object')
    try:
        card = Parse(json.dumps(value), AgentCard())
    except (ParseError, TypeError, ValueError) as exc:
        raise HTTPException(422, 'Invalid AgentCard structure') from exc
    if name is not None and (card.name != name or card.provider.organization != organization):
        # Reject, never rewrite: changing a signed payload invalidates its signature.
        raise HTTPException(422, 'AgentCard name and organization must match the path')
    return card


def semantic_query(body: dict, top_n=10):
    task = body.get('task')
    if not isinstance(task, str) or not task.strip() or len(task) > 10000:
        raise HTTPException(422, 'task must be a non-empty string of at most 10000 characters')
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 50:
        raise HTTPException(422, 'topN must be an integer between 1 and 50')
    return task, top_n
