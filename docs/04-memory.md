# 04 — Memory

## 1. Tiers

| Tier | Contents | Where | In context? |
|---|---|---|---|
| **Working** | Current conversation transcript | `agent_conversations` | Always, for the active conversation |
| **Hot** | `profile.md`: curated user profile + agent self-notes. Small, dense. ≤ 32 KiB. | S3 `profile/profile.md`, versioned | Always, injected into the system prompt |
| **Cold** | All memory records with embeddings | `agent_memory` (metadata + body ≤ 4 KiB) + S3 `memories/` (bodies > 4 KiB) + S3 Vectors index | Only via `memory_search` |
| **Diary** | Append-only narrative between heartbeats | `agent_diary` | Last 5 entries always in heartbeat context; last 2 in conversation context |

The agent decides what goes where. The system never auto-writes memory; the only automatic writes are the fallback diary entry (see [05-agent-runtime.md](05-agent-runtime.md)) and episode records produced in maintenance mode.

## 2. Record schema (`agent_memory`)

```
PK: MEM#<ULID>            SK: META
type:            user_fact | user_preference | episode | agent_learning | project | pending
content:         string (≤ 4 KiB inline) | absent when body_s3_key is set
body_s3_key:     string | absent
tags:            string set (≤ 10)
importance:      number 1–5
created_at:      ISO8601
last_accessed:   ISO8601 (initialised to created_at)
access_count:    number (initialised to 0)
version:         number (starts at 1)
tombstone:       bool
forget_reason:   string | absent
consolidated_into: MEM#<ULID> | absent
source:          conversation | heartbeat | maintenance | canary
embedding_model: "intfloat/multilingual-e5-large-instruct"
embedding_dim:   1024
namespace:       "live" | "canary"
```

Version history: `PK: MEM#<ULID>  SK: V#<version>` holds prior `content`/`tags`/`importance` snapshots. `memory_update` writes the snapshot, then updates META with `version + 1` in one transaction.

Indexes:

- `type-last_accessed-index` (GSI, PK `type`, SK `last_accessed`) — consolidation candidate scan and "list by type".
- `namespace-created_at-index` (GSI, PK `namespace`, SK `created_at`) — canary purge and newest-first listing.
- Tags: a separate table `agent_memory_tags` (PK `TAG#<tag>`, SK `MEM#<ULID>`) written in the same transaction as META. Chosen over a sparse GSI because a record has many tags.

Canary namespace: records written in canary mode use `PK: CANARY#MEM#<ULID>` and `namespace = "canary"`. `gate-canary` purges `CANARY#*` after each run. Agent roles cannot delete `CANARY#` items (IAM condition on `dynamodb:LeadingKeys`).

## 3. Retrieval

`memory_search(query, type?, tags?, top_k=8)`:

1. Embed `query` with `intfloat/multilingual-e5-large-instruct` through `gate-model`'s `embed` alias (same model as on write; `embedding_model` recorded per item so re-indexing is possible). Inputs over 514 tokens are chunked; search de-duplicates by `mem_id` and keeps the best-scoring chunk.
2. Query S3 Vectors with metadata filter `{ namespace, tombstone: false, type?, tags? }` for `top_k * 3` candidates.
3. Load META items, compute `relevance` (§4), rank by `0.7 * cosine + 0.3 * normalised_relevance`, return `top_k`.
4. Refresh `last_accessed = now` and `access_count += 1` for the returned items (batch update, best effort).

Step 4 is what keeps useful memories alive. It also means search quality shapes what survives; that is intended and is why consolidation, not deletion, is the forgetting mechanism.

The vector index is behind an interface `core/storage/vectors.py::VectorIndex` with two implementations: `S3VectorsIndex` (production) and `InMemoryIndex` (tests, numpy cosine). The interface is `upsert(id, vector, metadata)`, `query(vector, filter, k)`, `delete(id)`.

## 4. Relevance and decay

```
relevance = importance
          × (1 + ln(1 + access_count))
          × exp(−λ × days_since_last_accessed)

λ = 0.02   (half-life ≈ 35 days)
```

The `(1 + ln(...))` term keeps a never-accessed memory from scoring zero. With this form a fresh memory scores `importance`, an importance-5 memory never accessed again drops below 1.0 after about 80 days, and any retrieval resets the clock.

Relevance is **computed at read time**, never stored. There is no `relevance_score` attribute or GSI, because a stored score would be stale the moment it was written.

## 5. Consolidation

Runs in heartbeat `maintenance` mode (weekly cron, or whenever the agent chooses it in free mode). Uses model role `consolidation` (`zai-org/GLM-5.3`), because rewriting memories is lossy and deserves the stronger of the two models.

Algorithm (`agent/heartbeat/consolidate.py`):

1. Query `type-last_accessed-index` for each type where `last_accessed < now − 45 days`, `tombstone = false`, `namespace = live`, up to 500 items.
2. Compute relevance; keep items with `relevance < 1.0`.
3. Group by `(type, top tag, created_at month)`. Skip groups of size 1.
4. For each group, up to **20 groups and 100 originals per run** (hard cap in code; the supervisor also watches `memory_ops.consolidated`):
   a. Ask the model for a compressed summary that preserves names, dates, decisions and open questions.
   b. `memory_save` the summary with `type` unchanged, `importance = max(originals)`, `tags = union`, `source = maintenance`.
   c. Tombstone originals with `forget_reason = "consolidated"`, `consolidated_into = <summary id>`.
5. Write a diary entry listing every summary id and the count of originals.

Invariants, each covered by a unit test:

- Consolidation never hard-deletes. Tombstoned items stay readable by id.
- The summary links to every original and every original links to the summary.
- A group whose members all have `importance = 5` is never consolidated (they are re-surfaced to the agent in the diary as "review these" instead).
- Maintenance runs are tagged `source = maintenance`; the supervisor's memory-rate rule excludes that source.

Hard deletion: `scripts/purge_tombstones.py --older-than 180d`, human-run only. Agent roles have no `dynamodb:DeleteItem` on `agent_memory`.

## 6. Profile (hot memory) and concurrency

`profile.md` is a single markdown file. Suggested structure, editable by the agent:

```
# User
## Facts        (stable: name, timezone, languages, work)
## Preferences  (how they like to be talked to, formats, taboos)
## Current      (what is going on this month; the agent prunes this)
# Me
## How I work with this person
## Open threads
```

Concurrency: `profile_read` returns an ETag; `profile_write(content, etag)` performs an S3 `PutObject` with `If-Match`. On a 412 the tool returns `{ error: "conflict", current_etag, content }` and the agent must re-read and re-apply its change. The conversation and heartbeat Lambdas can both be running, so this is not theoretical. S3 versioning keeps every prior profile; `scripts/profile_history.py` diffs versions.

Size guard: writes over 32 KiB are rejected with `{ error: "too_large" }`. The prompt tells the agent the profile is for what must always be in context; details belong in cold memory.

## 7. Episodes

A conversation becomes an episode when it has been idle for 2 hours. Detection is deterministic: the heartbeat handler's housekeeping step (before free time, in every mode) queries `agent_conversations` for `status = open AND last_activity_at < now − 2h`, and for each one asks the model (role `heartbeat`) for an episode summary with a fixed schema (`what happened`, `decisions`, `facts learned`, `open threads`), saves it as `type = episode`, and marks the conversation `closed` with `episode_id`. The agent may additionally save separate `user_fact` items from the episode; the housekeeping step only guarantees the episode exists.

## 8. Storage format

Memory bodies, diary entries, the profile and artifacts are plain markdown. The vector index stores only ids and metadata; the text of a memory is never only in the index.

Embeddings are generated on write and on `memory_update`. The `embedding_model` attribute allows `scripts/reindex.py` to re-embed all live records if the model changes.
