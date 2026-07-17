"""Memory tools Jarvis can call mid-conversation: remember, recall, forget."""

from .memory import FactStore


def build_mind_tools(mind):
    """Return the memory tool functions, bound to this mind/ folder."""
    store = FactStore(mind)

    def remember(fact: str, why: str = "", name: str = ""):
        """Save one durable fact to long-term memory, or correct an existing one.

        Use this when you learn something worth knowing in future sessions: a
        user preference, a machine quirk, a lesson from a mistake. One focused
        fact per call. To correct or reinforce an existing memory, call again
        with the same name.

        Args:
            fact (str): The fact, stated plainly in one or two sentences
            why (str): Why it matters or how it was learned
            name (str): Short kebab-case id (auto-generated from the fact if empty)
        """
        return store.remember(fact, why=why, name=name)

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

    return [remember, recall, forget]
