"""Text embeddings from OpenRouter, as pgvector halfvec literals."""

from openai import AsyncOpenAI

from .. import telemetry

EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
DIMENSIONS = 1024  # Qwen3 embeddings are Matryoshka: a prefix is a valid embedding.
QUERY_INSTRUCTION = (
    "Instruct: Given a chat search query, retrieve relevant Discord messages\nQuery: "
)
EMBED_BATCH = 64


class Embedder:
    """OpenRouter embeddings as halfvec literals; accumulates reported cost."""

    def __init__(self, client: AsyncOpenAI):
        self.client = client
        self.cost = 0.0

    async def __call__(
        self, texts: list[str], *, query: bool = False, instruction: str = QUERY_INSTRUCTION
    ) -> list[str]:
        if query:
            texts = [instruction + t for t in texts]
        with telemetry.llm_call("embedding", EMBEDDING_MODEL) as call:
            response = await self.client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
            cost = float(getattr(response.usage, "cost", None) or 0)
            call.bill(cost, getattr(response.usage, "prompt_tokens", 0))
        self.cost += cost
        return [
            "[" + ",".join(f"{x:.5g}" for x in item.embedding[:DIMENSIONS]) + "]"
            for item in sorted(response.data, key=lambda item: item.index)
        ]
