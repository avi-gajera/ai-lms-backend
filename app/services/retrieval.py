"""Vector indexing and retrieval over transcript chunks (Qdrant via `langchain-qdrant`).

- One collection for all videos; every point carries `metadata.video_id` and every search is
  filtered by it, so results never leak across videos.
- Point ids are uuid5(video_id:chunk_index): re-indexing a video overwrites the same points
  (idempotent), and points beyond the new chunk count are deleted.
- Client mode is config-driven: Qdrant server (`QDRANT_URL`), embedded file mode (`QDRANT_PATH`,
  local dev, single process) or in-memory (tests).
"""

import threading
import uuid
from dataclasses import dataclass

from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from app.config import get_settings
from app.core.logging import get_logger
from app.services.embeddings import get_embeddings

logger = get_logger(__name__)

POINT_NAMESPACE = uuid.UUID("6f1d8a52-0c9b-4a7e-9d51-3c2f0e8b7a14")
_client: QdrantClient | None = None
_client_lock = threading.Lock()


def point_id(video_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{video_id}:{chunk_index}"))


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                s = get_settings()
                if s.qdrant_url:
                    _client = QdrantClient(url=s.qdrant_url, timeout=30)
                elif s.qdrant_path == ":memory:":
                    _client = QdrantClient(location=":memory:")
                else:
                    _client = QdrantClient(path=s.qdrant_path)
                _ensure_collection(_client)
    return _client


def reset_client() -> None:
    """Drop the cached client (tests / settings changes)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None


def _ensure_collection(client: QdrantClient) -> None:
    s = get_settings()
    if not client.collection_exists(s.qdrant_collection):
        client.create_collection(
            s.qdrant_collection,
            vectors_config=models.VectorParams(size=s.embedding_dim, distance=models.Distance.COSINE),
        )
        # Payload indexes make the per-video filter cheap on a server (embedded mode has none).
        if s.qdrant_url:
            for key, schema in (
                ("metadata.video_id", models.PayloadSchemaType.KEYWORD),
                ("metadata.chunk_index", models.PayloadSchemaType.INTEGER),
            ):
                client.create_payload_index(s.qdrant_collection, key, field_schema=schema)
        logger.info("created qdrant collection", extra={"collection": s.qdrant_collection})


def get_store() -> QdrantVectorStore:
    return QdrantVectorStore(
        client=get_client(),
        collection_name=get_settings().qdrant_collection,
        embedding=get_embeddings(),
    )


def _video_filter(video_id: str) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key="metadata.video_id", match=models.MatchValue(value=video_id))])


@dataclass
class IndexedChunk:
    chunk_id: str
    video_id: str
    chunk_index: int
    text: str
    start_time: float
    end_time: float
    topic: str


def index_chunks(video_id: str, chunks: list[IndexedChunk]) -> list[str]:
    """Upsert chunk vectors (idempotent) and delete stale points. Returns the point ids."""
    ids = [point_id(video_id, c.chunk_index) for c in chunks]
    if chunks:
        get_store().add_texts(
            texts=[c.text for c in chunks],
            metadatas=[
                {
                    "video_id": video_id,
                    "chunk_id": c.chunk_id,
                    "chunk_index": c.chunk_index,
                    "start_time": c.start_time,
                    "end_time": c.end_time,
                    "topic": c.topic,
                }
                for c in chunks
            ],
            ids=ids,
        )
    # Remove points from a previous, longer run of the same video.
    get_client().delete(
        get_settings().qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(key="metadata.video_id", match=models.MatchValue(value=video_id)),
                    models.FieldCondition(key="metadata.chunk_index", range=models.Range(gte=len(chunks))),
                ]
            )
        ),
    )
    return ids


def count_points(video_id: str) -> int:
    return get_client().count(get_settings().qdrant_collection, count_filter=_video_filter(video_id), exact=True).count


@dataclass
class RetrievedChunk:
    chunk_id: str
    chunk_index: int
    text: str
    start_time: float
    end_time: float
    topic: str
    score: float


def search(video_id: str, query: str, k: int) -> list[RetrievedChunk]:
    results = get_store().similarity_search_with_score(query, k=k, filter=_video_filter(video_id))
    out = []
    for doc, score in results:
        m = doc.metadata
        out.append(
            RetrievedChunk(
                chunk_id=m["chunk_id"],
                chunk_index=m["chunk_index"],
                text=doc.page_content,
                start_time=m["start_time"],
                end_time=m["end_time"],
                topic=m["topic"],
                score=round(float(score), 4),
            )
        )
    return out
