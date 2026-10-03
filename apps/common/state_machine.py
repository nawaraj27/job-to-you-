"""
State machines (SRS §7.5).

Five aggregates carry most of the business risk, and each one's transitions
must live in exactly one place. This is the mechanism: an explicit table of
legal transitions, checked before any write.

Deliberately small. No metaclass, no field patching, no implicit save. The
service still performs the write; this only answers whether it may.

    JOB = StateMachine(
        "status",
        transitions=[
            Transition("draft", "published", permission="job.publish"),
            Transition("published", "paused"),
        ],
    )

    JOB.require(job, "published", actor_permissions=membership.permissions)
    job.status = "published"
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import InvalidState, NotAllowed


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    permission: str | None = None


class StateMachine:
    def __init__(self, field: str, transitions: Iterable[Transition]) -> None:
        self.field = field
        self._transitions: dict[tuple[str, str], Transition] = {}
        for transition in transitions:
            key = (transition.source, transition.target)
            if key in self._transitions:
                raise ValueError(f"Duplicate transition {key[0]} -> {key[1]}")
            self._transitions[key] = transition

    def targets_from(self, source: str) -> tuple[str, ...]:
        return tuple(target for (src, target) in self._transitions if src == source)

    def can(self, source: str, target: str) -> bool:
        return (source, target) in self._transitions

    def available(
        self, instance: object, actor_permissions: Iterable[str] | None = None
    ) -> tuple[str, ...]:
        """Where this actor could actually move this instance next.

        Permission-aware so a screen can offer exactly the buttons that would
        be honoured. The check here is for drawing, not for deciding: `require`
        still runs on the write, because a screen rendered a minute ago is not
        an authorization.
        """
        source = getattr(instance, self.field)
        granted = set(actor_permissions or ())
        return tuple(
            transition.target
            for (src, _), transition in self._transitions.items()
            if src == source and (not transition.permission or transition.permission in granted)
        )

    def require(
        self,
        instance: object,
        target: str,
        *,
        actor_permissions: Iterable[str] | None = None,
    ) -> Transition:
        """Validate a transition, or raise.

        Raises InvalidState for an illegal move and NotAllowed when the move is
        legal but the actor lacks the grant key it requires. The two are
        different answers and callers must be able to tell them apart.
        """
        source = getattr(instance, self.field)
        transition = self._transitions.get((source, target))
        if transition is None:
            raise InvalidState(
                f"Cannot move from {source} to {target}.",
                details={"field": self.field, "from": source, "to": target},
            )
        if transition.permission:
            granted = set(actor_permissions or ())
            if transition.permission not in granted:
                raise NotAllowed(
                    "You do not have permission to make that change.",
                    details={"required": transition.permission},
                )
        return transition
