"""Tests for services.users_service.worker (OffsetTracker, _backoff_with_jitter)."""
from __future__ import annotations

import asyncio

import pytest
from aiokafka import TopicPartition
from aiokafka.structs import OffsetAndMetadata

from services.users_service.worker import OffsetTracker, _backoff_with_jitter


def test_backoff_with_jitter_first_attempt() -> None:
    delay = _backoff_with_jitter(attempt=1, base=0.2, max_delay=3.0)
    assert 0.2 <= delay <= 0.2 + 0.2 * 0.25


def test_backoff_with_jitter_second_attempt() -> None:
    delay = _backoff_with_jitter(attempt=2, base=0.2, max_delay=3.0)
    expected_base = min(3.0, 0.2 * 2)
    assert expected_base <= delay <= expected_base + expected_base * 0.25


def test_backoff_with_jitter_capped_by_max_delay() -> None:
    delay = _backoff_with_jitter(attempt=10, base=1.0, max_delay=2.0)
    assert delay <= 2.0 + 2.0 * 0.25


@pytest.mark.asyncio
async def test_offset_tracker_register_once() -> None:
    tracker = OffsetTracker()
    tp = TopicPartition("t", 0)
    await tracker.register(tp, 5)
    commit_map = await tracker.complete_and_build_commit(tp, 5)
    assert commit_map[tp].offset == 6


@pytest.mark.asyncio
async def test_offset_tracker_sequential_completion() -> None:
    tracker = OffsetTracker()
    tp = TopicPartition("t", 0)
    await tracker.register(tp, 0)
    commit_map = await tracker.complete_and_build_commit(tp, 0)
    assert commit_map[tp].offset == 1
    await tracker.register(tp, 1)
    commit_map = await tracker.complete_and_build_commit(tp, 1)
    assert commit_map[tp].offset == 2


@pytest.mark.asyncio
async def test_offset_tracker_out_of_order_completion() -> None:
    tracker = OffsetTracker()
    tp = TopicPartition("t", 0)
    await tracker.register(tp, 0)
    await tracker.register(tp, 1)
    await tracker.register(tp, 2)
    commit_map = await tracker.complete_and_build_commit(tp, 1)
    assert commit_map[tp].offset == 0
    commit_map = await tracker.complete_and_build_commit(tp, 0)
    assert commit_map[tp].offset == 2
    commit_map = await tracker.complete_and_build_commit(tp, 2)
    assert commit_map[tp].offset == 3
