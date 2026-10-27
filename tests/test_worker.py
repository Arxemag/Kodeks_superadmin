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


@pytest.mark.asyncio
async def test_offset_tracker_accepts_real_offset_gaps():
    tracker = OffsetTracker()
    tp = TopicPartition('t', 0)
    for offset in (5, 8, 12):
        await tracker.register(tp, offset)
    result = await tracker.complete_and_build_commit(tp, 12)
    assert result[tp].offset == 5
    result = await tracker.complete_and_build_commit(tp, 5)
    assert result[tp].offset == 6
    result = await tracker.complete_and_build_commit(tp, 8)
    assert result[tp].offset == 13


@pytest.mark.asyncio
async def test_offset_tracker_never_skips_an_unfinished_delivered_record():
    tracker = OffsetTracker()
    tp = TopicPartition('t', 0)
    for offset in (5, 8, 12):
        await tracker.register(tp, offset)
    await tracker.complete_and_build_commit(tp, 5)
    result = await tracker.complete_and_build_commit(tp, 12)
    assert result[tp].offset == 6


@pytest.mark.asyncio
async def test_offset_tracker_rejects_unregistered_completion():
    tracker = OffsetTracker()
    with pytest.raises(RuntimeError, match='unregistered'):
        await tracker.complete_and_build_commit(TopicPartition('t', 0), 999)


@pytest.mark.asyncio
async def test_partition_batch_orders_effects_but_other_partitions_progress():
    from common.kafka import process_partition_batch
    first, other = TopicPartition('t', 0), TopicPartition('t', 1)
    peer_started = asyncio.Event()
    trace = []
    async def process(partition, offset):
        trace.append(('start', partition.partition, offset))
        if partition == first and offset == 1:
            await peer_started.wait()
        if partition == other:
            peer_started.set()
        trace.append(('done', partition.partition, offset))
    await asyncio.wait_for(process_partition_batch({first: [1, 2], other: [9]}, process), 1)
    assert trace.index(('done', 0, 1)) < trace.index(('start', 0, 2))
    assert trace.index(('start', 1, 9)) < trace.index(('done', 0, 1))


@pytest.mark.asyncio
async def test_partition_failure_stops_later_records_and_cancels_other_tasks():
    from common.kafka import process_partition_batch
    first, other = TopicPartition('t', 0), TopicPartition('t', 1)
    peer_started = asyncio.Event()
    peer_cancelled = asyncio.Event()
    processed = []
    async def process(partition, offset):
        processed.append((partition, offset))
        if partition == other:
            peer_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                peer_cancelled.set()
        else:
            await peer_started.wait()
            raise RuntimeError('DLQ broker unavailable')
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(process_partition_batch({first: [1, 2], other: [9]}, process), 1)
    assert (first, 2) not in processed
    assert peer_cancelled.is_set()


@pytest.mark.asyncio
async def test_assignment_epoch_rejects_revoked_then_reassigned_partition():
    from types import SimpleNamespace
    from common.kafka import _AssignmentGuard, ensure_partition_assigned, get_assignment_epoch
    tp = TopicPartition('t', 0)
    guard = _AssignmentGuard()
    consumer = SimpleNamespace(assignment=lambda: {tp}, _integration_assignment_guard=guard)
    epoch = get_assignment_epoch(consumer)
    await guard.on_partitions_revoked({tp})
    await guard.on_partitions_assigned({tp})
    with pytest.raises(RuntimeError, match='revoked'):
        ensure_partition_assigned(consumer, tp, epoch)
