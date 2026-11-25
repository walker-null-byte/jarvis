"""Shared LLM error type (re-exported by jarvis.llm)."""


class LLMError(RuntimeError):
    """Normalized LLM request failure.

    ``retryable`` is an explicit hint for the facade's retry loop; leave it
    as None to let the loop classify the underlying exception.
    """

    def __init__(self, message, retryable=None, status=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
