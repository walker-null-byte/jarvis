"""Memory tools Jarvis can call mid-conversation: remember, recall, forget."""

from .memory import FactStore


def build_mind_tools(mind):
    """Return the memory tool functions, bound to this mind/ folder."""
    store = FactStore(mind)

    def remember(
        fact: str,
        why: str = "",
        name: str = "",
        source: str = "",
        source_episode: str = "",
        source_event_id: str = "",
        confidence: float = None,
        category: str = "",
        observed_at: str = "",
        valid_from: str = "",
        valid_until: str = "",
        correction_of: str = "",
        provenance: str = "",
    ):
        """Save one durable fact to long-term memory, or correct an existing one.

        Use this when you learn something worth knowing in future sessions: a
        user preference, a machine quirk, a lesson from a mistake. One focused
        fact per call. To correct or reinforce an existing memory, call again
        with the same name.

        Args:
            fact (str): The fact, stated plainly in one or two sentences
            why (str): Why it matters or how it was learned
            name (str): Short kebab-case id (auto-generated from the fact if empty)
            source (str): Source kind, such as direct, episode, or consolidation
            source_episode (str): Episode filename or date that supports the fact
            source_event_id (str): Event/turn identifier when available
            confidence (float): Confidence from 0.0 to 1.0
            category (str): Preference, system, procedure, lesson, or general
            observed_at (str): When the fact was observed
            valid_from (str): Optional beginning of its validity
            valid_until (str): Optional expiry or end of validity
            correction_of (str): Memory id this fact corrects
            provenance (str): Short human-readable provenance note
        """
        return store.remember(
            fact,
            why=why,
            name=name,
            source=source,
            source_episode=source_episode,
            source_event_id=source_event_id,
            confidence=confidence,
            category=category,
            observed_at=observed_at,
            valid_from=valid_from,
            valid_until=valid_until,
            correction_of=correction_of,
            provenance=provenance,
        )

    def recall(query: str, top_n: int = 3):
        """Search long-term memory and return the best-matching facts.

        Use this before asking the user something you might already know.
        Recalling a memory strengthens it.

        Args:
            query (str): What you are trying to remember
            top_n (int): Maximum memories to return
        """
        return store.recall(query, top_n=top_n)

    def forget(name: str):
        """Delete a memory by its exact id (the bold name in the MEMORY index).

        Args:
            name (str): The memory's id
        """
        return store.forget(name)

    def memory_review(limit: int = 50, include_approved: bool = False):
        """List memories awaiting review, with provenance and expiry state.

        Args:
            limit (int): Maximum number of memories to return
            include_approved (bool): Include already approved memories too
        """
        return store.review_memories(limit=limit, include_approved=include_approved)

    def memory_review_decide(name: str, decision: str, note: str = ""):
        """Approve, reject, or expire a memory without deleting its source.

        Args:
            name (str): Exact memory id
            decision (str): approve, reject, or expire
            note (str): Optional reason for the review decision
        """
        return store.set_review(name, decision, note=note)

    def memory_expire(name: str, valid_until: str):
        """Set an ISO expiry date for a memory while preserving its record.

        Args:
            name (str): Exact memory id
            valid_until (str): Expiry date in YYYY-MM-DD format
        """
        return store.expire(name, valid_until)

    def memory_never_remember(pattern: str):
        """Prevent future facts containing a sensitive pattern from being saved.

        Args:
            pattern (str): Case-insensitive phrase to block
        """
        return store.add_never_remember(pattern)

    def memory_allow_remember(pattern: str):
        """Remove a previously configured never-remember pattern.

        Args:
            pattern (str): Exact pattern to remove
        """
        return store.remove_never_remember(pattern)

    def memory_rules():
        """List active memory privacy rules."""
        return store.memory_rules()

    return [
        remember,
        recall,
        forget,
        memory_review,
        memory_review_decide,
        memory_expire,
        memory_never_remember,
        memory_allow_remember,
        memory_rules,
    ]
