from __future__ import annotations

import functools
import time
from typing import Any

import pytest
from kafka.errors import CommitFailedError, KafkaTimeoutError, RebalanceInProgressError
from kafka.structs import OffsetAndMetadata, TopicPartition

import infra.kafka_client as kafka_client
from infra.kafka_client import KafkaOffsetReset
from tests.helpers.kafka_client_behaviour import (
    ANSWER_TIMEOUT_MS,
    JOIN_TIMEOUT_SEC,
    SHORT_BOUND_MS,
    KafkaClientBehaviour,
    elapsed,
    raises_within,
)

LAPSING_POLL_INTERVAL_MS = 1_000


class KafkaGroupBehaviour(KafkaClientBehaviour):
    @staticmethod
    def _accepts_commit(consumer: Any, offsets: dict[TopicPartition, OffsetAndMetadata]) -> bool:
        try:
            consumer.commit(offsets, timeout_ms=ANSWER_TIMEOUT_MS)
        except (CommitFailedError, RebalanceInProgressError):
            return False
        return True

    @staticmethod
    def _next_offset(consumer: Any) -> int:
        deadline = time.monotonic() + JOIN_TIMEOUT_SEC
        while not (polled := consumer.poll(timeout_ms=100)):
            assert time.monotonic() < deadline, "no record arrived in time"
        return next(iter(polled.values()))[0].offset

    def test_a_subscribed_consumer_joins_only_when_it_polls(self):
        clients, topic, group = self.clients(), self._topic(partitions=2), self._name("group")
        first, idle = self._member(clients, group, topic), self._member(clients, group, topic)

        self._poll_until([first], lambda: len(first.assignment()) == 2)
        noticed = time.monotonic() + 2 * kafka_client._GROUP_HEARTBEAT_INTERVAL_MS / 1000
        while time.monotonic() < noticed:
            first.poll(timeout_ms=50)

        assert len(first.assignment()) == 2
        assert idle.assignment() == set()

    def test_a_rebalance_is_eager_and_waits_for_every_live_member(self):
        clients, topic, group = self.clients(), self._topic(partitions=2), self._name("group")
        first, second = self._member(clients, group, topic), self._member(clients, group, topic)
        self._poll_until([first], lambda: len(first.assignment()) == 2)

        self._poll_until([second, first], lambda: len(first.assignment()) == len(second.assignment()) == 1)

        assert first.assignment() | second.assignment() == {TopicPartition(topic, 0), TopicPartition(topic, 1)}

    def test_a_silent_member_holds_up_the_rebalance_until_its_poll_interval_passes(self):
        clients, topic, group = self.clients(), self._topic(partitions=2), self._name("group")
        first = self._member(clients, group, topic, max_poll_interval_ms=1_000)
        silent = self._member(clients, group, topic, max_poll_interval_ms=1_000)
        self._poll_until([first, silent], lambda: len(first.assignment()) == len(silent.assignment()) == 1)
        silent_since = time.monotonic()
        late = self._member(clients, group, topic, max_poll_interval_ms=1_000)

        self._poll_until([first, late], lambda: len(first.assignment()) == len(late.assignment()) == 1)

        assert time.monotonic() - silent_since >= 0.9

    def test_a_crashed_member_gives_its_partitions_to_the_survivors(self):
        clients, topic, group = self.clients(), self._topic(partitions=2), self._name("group")
        survivor, crashed = self._member(clients, group, topic), self._member(clients, group, topic)
        self._poll_until([survivor, crashed], lambda: len(survivor.assignment()) == len(crashed.assignment()) == 1)

        self.crash(crashed)

        self._poll_until([survivor], lambda: len(survivor.assignment()) == 2)

    def test_a_dropped_member_is_refused_its_commit_and_rejoins_on_its_next_poll(self):
        clients, topic, group = self.clients(), self._topic(partitions=1), self._name("group")
        tp = TopicPartition(topic, 0)
        offsets = {tp: OffsetAndMetadata(0, "", -1)}
        member = self._member(clients, group, topic, max_poll_interval_ms=LAPSING_POLL_INTERVAL_MS)
        self._poll_until([member], lambda: bool(member.assignment()))

        self.drop_membership(member)

        with pytest.raises(CommitFailedError):
            member.commit(offsets, timeout_ms=ANSWER_TIMEOUT_MS)
        self._poll_until([member], lambda: self._accepts_commit(member, offsets))
        assert member.assignment() == {tp}

    def test_a_dropped_member_resumes_at_its_committed_offset_not_at_its_position(self):
        clients, topic, group = self.clients(), self._topic(partitions=1), self._name("group")
        tp = TopicPartition(topic, 0)
        self._produce(clients, topic, 3)
        member = self._member(clients, group, topic, max_poll_interval_ms=LAPSING_POLL_INTERVAL_MS)
        assert self._next_offset(member) == 0
        member.commit({tp: OffsetAndMetadata(1, "", -1)}, timeout_ms=ANSWER_TIMEOUT_MS)
        assert self._next_offset(member) == 1

        self.drop_membership(member)

        assert self._next_offset(member) == 1

    def test_a_poll_during_an_outage_returns_nothing_and_raises_nothing(self):
        clients = self.unreachable_clients()
        consumer = self._member(clients, self._name("group"), "fleet.unreachable")
        reader = clients.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)
        reader.assign([TopicPartition("fleet.unreachable", 0)])

        assert elapsed(lambda: consumer.poll(timeout_ms=SHORT_BOUND_MS)) >= 0.25
        assert consumer.poll(timeout_ms=SHORT_BOUND_MS) == {}
        assert reader.poll(timeout_ms=SHORT_BOUND_MS) == {}
        consumer.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)
        reader.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_commits_time_out_during_an_outage(self):
        clients, tp = self.unreachable_clients(), TopicPartition("fleet.unreachable", 0)
        offsets = {tp: OffsetAndMetadata(1, "", -1)}
        member = self._member(clients, self._name("group"), tp.topic)

        raises_within(
            KafkaTimeoutError,
            functools.partial(member.commit, offsets, timeout_ms=SHORT_BOUND_MS),
            at_least=0.25,
            at_most=3.0,
        )
        raises_within(
            KafkaTimeoutError, functools.partial(member.commit, offsets, timeout_ms=0), at_least=0.0, at_most=1.0
        )
        member.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)

    def test_a_commit_given_no_time_times_out_on_a_healthy_cluster(self):
        clients, topic, group = self.clients(), self._topic(partitions=1), self._name("group")
        tp = TopicPartition(topic, 0)
        member = self._member(clients, group, topic)
        self._poll_until([member], lambda: bool(member.assignment()))

        with pytest.raises(KafkaTimeoutError):
            member.commit({tp: OffsetAndMetadata(0, "", -1)}, timeout_ms=0)

    def test_a_group_consumer_close_is_bounded_during_an_outage(self):
        unreachable = self._member(self.unreachable_clients(), self._name("group"), "fleet.unreachable")
        unreachable.poll(timeout_ms=100)
        assert elapsed(lambda: unreachable.close(autocommit=False, timeout_ms=SHORT_BOUND_MS)) < 1.5

    def test_a_group_consumer_that_leaves_hands_its_partitions_over_within_a_session(self):
        clients, topic, group = self.clients(), self._topic(partitions=1), self._name("group")
        member = self._member(clients, group, topic)
        self._poll_until([member], lambda: bool(member.assignment()))

        member.close(autocommit=False, timeout_ms=ANSWER_TIMEOUT_MS)
        successor = self._member(clients, group, topic)
        started = time.monotonic()
        self._poll_until([successor], lambda: bool(successor.assignment()))

        assert time.monotonic() - started < kafka_client._GROUP_SESSION_TIMEOUT_MS / 1000
        assert successor.assignment() == {TopicPartition(topic, 0)}
