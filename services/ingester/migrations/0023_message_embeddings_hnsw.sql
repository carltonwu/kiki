-- Approximate nearest-neighbour index for message search. Without one, every
-- search reads every embedding (~550 MB of TOAST at 208k messages), which keeps
-- the whole table in the page cache Railway bills as memory, and takes seconds.
--
-- It indexes the first 512 of the 1024 dimensions: Qwen's embeddings are
-- Matryoshka, so a prefix is a coarser embedding of the same text. The host
-- shortlists with it and reranks by the full vectors, which on production's
-- embeddings matched the full-width index's recall at half the size (241 MB
-- against 476 MB), small enough to stay cached under a tight memory limit.
-- The expression must match the host's query exactly to be used.
--
-- Built in the migration's transaction, so a failure rolls back cleanly; it
-- blocks writes to message_embeddings (new vectors, edits and deletes of
-- embedded messages) for the build, about a minute at this size. The graph
-- fits in memory at 1 GB, which keeps the build fast; workers are off because
-- a parallel build needs that much dynamic shared memory.
SET LOCAL maintenance_work_mem = '1GB';
SET LOCAL max_parallel_maintenance_workers = 0;

CREATE INDEX message_embeddings_hnsw_idx ON message_embeddings
    USING hnsw ((subvector(embedding, 1, 512)::halfvec(512)) halfvec_cosine_ops);
