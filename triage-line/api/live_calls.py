"""Interactive call transport: real caller text -> existing deliberation/commit core.

This is an operator-assistance workflow, NOT a dispatch-provider integration.
A FINALIZED action means the caller confirmed the recorded proposal; no truck
or emergency service is contacted. Browser speech, when supported, transcribes
into the same text endpoint. Every displayed event originates on the EventBus.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from api.websocket import ConnectionManager
from core.commit.state_machine import CommitState, CommitStateMachine
from core.deliberation.engine import DeliberationEngine
from core.event_bus import EventBus
from core.events import BargeIn, FinalTranscript, MetricsTick, PartialTranscript, TurnStarted
from persistence.db import Database
from persistence.repository import ActionRepository, CallRepository, DeliberationRepository
from providers.interfaces import ConversationTurn


class CallError(ValueError):
    pass


@dataclass
class Session:
    call_id: str
    bus: EventBus
    deliberation: DeliberationEngine
    commit: CommitStateMachine
    context: list[ConversationTurn] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_id: str | None = None
    speaking: bool = False
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LiveCalls:
    def __init__(self, manager: ConnectionManager, db_path: str = ":memory:") -> None:
        self.manager = manager
        self.db = Database(db_path)
        self.db.initialize_schema()
        self.sessions: dict[str, Session] = {}

    def close(self) -> None:
        self.db.close()

    async def session(self, call_id: str) -> Session:
        if call_id not in self.sessions:
            bus = EventBus()
            session = Session(
                call_id=call_id,
                bus=bus,
                deliberation=DeliberationEngine(bus=bus, repository=DeliberationRepository(self.db)),
                commit=CommitStateMachine(bus=bus, repository=ActionRepository(self.db)),
            )

            async def relay(event):
                data = event.to_dict()
                session.history.append(data)
                await self.manager.broadcast(f"call:{call_id}", data)

            await bus.subscribe(relay)
            self.sessions[call_id] = session
            CallRepository(self.db).ensure_call(call_id, int(time.time() * 1000))
        return self.sessions[call_id]

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    async def turn(self, call_id: str, text: str) -> dict:
        text = text.strip()
        if not text or len(text) > 2000:
            raise CallError("A caller turn must contain 1–2000 characters")
        session = await self.session(call_id)
        async with session.lock:
            if session.closed:
                raise CallError("Call has ended")
            # New caller input supersedes any unconfirmed proposal, even if
            # the next decision is incomplete or changes intent entirely.
            if session.pending_id:
                old = session.commit.get(session.pending_id)
                if old and not old.is_terminal:
                    await session.commit.abort(old.action_id, "new_caller_information")
                session.pending_id = None
            session.speaking = False
            await session.bus.publish(TurnStarted(call_id, self._now(), "caller"))
            await session.bus.publish(FinalTranscript(call_id, self._now(), "caller", text))
            session.context.append(ConversationTurn(speaker="caller", text=text))
            record = await session.deliberation.deliberate(call_id, session.context)
            if record.resolved and record.chosen_option:
                action = await session.commit.propose(
                    call_id=call_id, decision_id=record.decision_id,
                    action_type=record.chosen_option, action_payload=dict(record.known_facts),
                )
                session.pending_id = action.action_id
                answer = (f"I can request {record.chosen_option.replace('_', ' ')}. "
                          f"Please review the dispatch state and explicitly confirm or reject this action.")
            elif record.uncertainties:
                answer = "I need a little more information: " + "; ".join(record.uncertainties[:3]) + "."
            else:
                answer = "Tell me your location, vehicle and what happened. If anyone is in immediate danger, contact local emergency services now."
            await session.bus.publish(TurnStarted(call_id, self._now(), "agent"))
            await session.bus.publish(FinalTranscript(call_id, self._now(), "agent", answer))
            session.context.append(ConversationTurn(speaker="agent", text=answer))
            session.speaking = True
            return {"reply": answer, "pending_action_id": session.pending_id,
                    "decision_id": record.decision_id, "resolved": record.resolved}

    async def interrupt(self, call_id: str) -> dict:
        session = await self.session(call_id)
        async with session.lock:
            if session.closed or not session.speaking:
                return {"interrupted": False}
            session.speaking = False
            # No fabricated latency: the browser stops audio locally and
            # this API cannot measure device playback-stop latency.
            await session.bus.publish(BargeIn(call_id, self._now(), position_ms=0, latency_ms=None))
            await session.bus.publish(MetricsTick(call_id, self._now()))
            return {"interrupted": True}

    async def decide(self, call_id: str, action_id: str, confirm: bool) -> dict:
        session = await self.session(call_id)
        async with session.lock:
            if session.closed or action_id != session.pending_id:
                raise CallError("No matching pending action for this call")
            action = session.commit.get(action_id)
            if action is None or action.current_state != CommitState.PENDING_CONFIRMATION:
                raise CallError("Action is no longer pending")
            if confirm:
                await session.commit.confirm(action_id, confirmed_by="caller_explicit_ui")
            else:
                await session.commit.abort(action_id, "caller_rejected")
            session.pending_id = None
            return {"action_id": action_id, "state": action.current_state.value,
                    "notice": "Recorded only; no external service has been dispatched."}

    async def end(self, call_id: str) -> dict:
        session = await self.session(call_id)
        async with session.lock:
            if not session.closed:
                await session.commit.force_resolve_pending(call_id, "call_ended")
                session.pending_id = None
                session.speaking = False
                session.closed = True
            return {"closed": True}
