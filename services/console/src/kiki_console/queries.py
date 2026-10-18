"""Read-only dashboard queries. Every query is scoped by guild ID, which the
caller has already checked against the session."""

from psycopg import AsyncConnection
from psycopg.rows import dict_row

WINDOWS = (("24 h", "24 hours"), ("7 d", "7 days"), ("30 d", "30 days"), ("All", None))
RUN_STATUSES = ("pending", "running", "delivering", "done", "failed", "superseded")

# Per-guild cost sources with a timestamp. Decide cost is stored on every message
# of a pool; the pool's newest message counts it once (as in /kiki stats).
COSTS = """
    SELECT 'chat' AS kind, c.guild_id, coalesce(r.finished_at, r.created_at) AS at,
           r.cost_usd AS cost
    FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
    WHERE r.cost_usd IS NOT NULL
    UNION ALL
    SELECT 'decide', m.guild_id, cl.classified_at, cl.jev_cost_usd
    FROM classifications cl JOIN messages m ON m.id = cl.message_id
    WHERE cl.message_id = cl.pool_id AND cl.jev_cost_usd IS NOT NULL
    UNION ALL
    SELECT 'console', guild_id, created_at, cost_usd
    FROM console_chat_messages WHERE cost_usd IS NOT NULL
"""


async def rows(db: AsyncConnection, query: str, params=()) -> list[dict]:
    cursor = db.cursor(row_factory=dict_row)
    await cursor.execute(query, params)
    return await cursor.fetchall()


async def one(db: AsyncConnection, query: str, params=()) -> dict | None:
    found = await rows(db, query, params)
    return found[0] if found else None


async def guild_names(db: AsyncConnection, guild_ids) -> dict[int, str]:
    found = await rows(
        db, "SELECT id, name FROM guilds WHERE id = ANY(%s) ORDER BY name", (list(guild_ids),)
    )
    return {g["id"]: g["name"] or str(g["id"]) for g in found}


async def windows(db: AsyncConnection, guild_id: int) -> list[dict]:
    """Activity, latency and cost per window, oldest window last."""
    out = []
    for label, interval in WINDOWS:
        since = "now() - %(interval)s::interval" if interval else "'-infinity'::timestamptz"
        row = await one(
            db,
            f"""
            WITH conv AS (SELECT id, created_at FROM conversations WHERE guild_id = %(g)s),
            runs AS (SELECT r.status, r.latency_ms, coalesce(r.finished_at, r.created_at) AS at
                     FROM agent_runs r JOIN conv ON conv.id = r.conversation_id),
            cost AS (SELECT kind, cost FROM ({COSTS}) x WHERE guild_id = %(g)s AND at > {since})
            SELECT
              (SELECT count(*) FROM conv WHERE created_at > {since}) AS conversations,
              (SELECT count(*) FROM runs WHERE status = 'done' AND at > {since}) AS replies,
              (SELECT count(*) FROM runs WHERE status = 'failed' AND at > {since}) AS failed,
              (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                 FROM runs WHERE status = 'done' AND at > {since}) AS p50_ms,
              (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                 FROM runs WHERE status = 'done' AND at > {since}) AS p95_ms,
              (SELECT coalesce(sum(cost), 0) FROM cost WHERE kind = 'chat') AS chat_cost,
              (SELECT coalesce(sum(cost), 0) FROM cost WHERE kind = 'decide') AS decide_cost,
              (SELECT coalesce(sum(cost), 0) FROM cost WHERE kind = 'console') AS console_cost
            """,
            {"g": guild_id, "interval": interval},
        )
        row["label"] = label
        row["total_cost"] = row["chat_cost"] + row["decide_cost"] + row["console_cost"]
        out.append(row)
    topics = await one(
        db,
        "SELECT coalesce(sum(topic_cost_usd), 0) AS cost FROM conversations WHERE guild_id = %s",
        (guild_id,),
    )
    # Topic summaries have no timestamp: they count toward All only.
    out[-1]["topic_cost"] = topics["cost"]
    out[-1]["total_cost"] += topics["cost"]
    return out


async def daily_costs(db: AsyncConnection, guild_id: int | None, days: int = 30) -> list[dict]:
    """One row per day (UTC), oldest first, with chat/decide/console cost."""
    return await rows(
        db,
        f"""
        WITH d AS (SELECT generate_series(current_date - (%(days)s - 1), current_date,
                                          interval '1 day')::date AS day),
        c AS (SELECT kind, (at AT TIME ZONE 'UTC')::date AS day, cost FROM ({COSTS}) x
              WHERE (%(g)s::bigint IS NULL OR guild_id = %(g)s)
                AND at >= current_date - (%(days)s - 1))
        SELECT d.day,
               coalesce(sum(c.cost) FILTER (WHERE c.kind = 'chat'), 0) AS chat,
               coalesce(sum(c.cost) FILTER (WHERE c.kind = 'decide'), 0) AS decide,
               coalesce(sum(c.cost) FILTER (WHERE c.kind = 'console'), 0) AS console
        FROM d LEFT JOIN c ON c.day = d.day
        GROUP BY d.day ORDER BY d.day
        """,
        {"g": guild_id, "days": days},
    )


async def routing(db: AsyncConnection, guild_id: int) -> list[dict]:
    return await rows(
        db,
        """
        SELECT cl.check_hit, count(*) AS messages,
               count(*) FILTER (WHERE cl.outcome <> 'no') AS routed
        FROM classifications cl JOIN messages m ON m.id = cl.message_id
        WHERE m.guild_id = %s AND cl.classified_at > now() - interval '7 days'
        GROUP BY cl.check_hit ORDER BY messages DESC
        """,
        (guild_id,),
    )


async def active_conversations(db: AsyncConnection, guild_id: int) -> list[dict]:
    return await rows(
        db,
        """
        SELECT c.id, coalesce(ch.name, c.channel_id::text) AS channel, c.message_count,
               c.topic, c.last_active_at,
               (SELECT coalesce(sum(cost_usd), 0) FROM agent_runs r
                WHERE r.conversation_id = c.id) + c.topic_cost_usd AS cost
        FROM conversations c LEFT JOIN channels ch ON ch.id = c.channel_id
        WHERE c.guild_id = %s AND c.closed_at IS NULL
        ORDER BY c.last_active_at DESC
        """,
        (guild_id,),
    )


async def top_people(db: AsyncConnection, guild_id: int, limit: int = 10) -> list[dict]:
    """Reply cost over 7 days, split evenly across each run's trigger messages."""
    return await rows(
        db,
        """
        WITH r AS (
            SELECT r.id, r.cost_usd, r.trigger_message_ids
            FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
            WHERE c.guild_id = %(g)s AND r.created_at > now() - interval '7 days'
              AND r.cost_usd IS NOT NULL AND cardinality(r.trigger_message_ids) > 0),
        shares AS (
            SELECT r.id, r.cost_usd / cardinality(r.trigger_message_ids) AS share, m.author_id
            FROM r CROSS JOIN LATERAL unnest(r.trigger_message_ids) AS t(message_id)
            JOIN messages m ON m.id = t.message_id)
        SELECT s.author_id, coalesce(p.nick, p.global_name, p.username, s.author_id::text) AS name,
               sum(s.share) AS cost, count(DISTINCT s.id) AS runs
        FROM shares s LEFT JOIN people p ON p.guild_id = %(g)s AND p.user_id = s.author_id
        GROUP BY s.author_id, p.nick, p.global_name, p.username
        ORDER BY cost DESC LIMIT %(limit)s
        """,
        {"g": guild_id, "limit": limit},
    )


async def runs(
    db: AsyncConnection, guild_id: int, status: str | None, before: int | None, limit: int = 50
) -> list[dict]:
    return await rows(
        db,
        """
        SELECT r.id, r.status, r.model, r.latency_ms, r.cost_usd, r.prompt_tokens,
               r.completion_tokens, r.steps, r.error, r.created_at,
               coalesce(ch.name, c.channel_id::text) AS channel
        FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
        LEFT JOIN channels ch ON ch.id = c.channel_id
        WHERE c.guild_id = %(g)s AND (%(status)s::text IS NULL OR r.status = %(status)s)
          AND (%(before)s::bigint IS NULL OR r.id < %(before)s)
        ORDER BY r.id DESC LIMIT %(limit)s
        """,
        {"g": guild_id, "status": status, "before": before, "limit": limit},
    )


async def run_detail(db: AsyncConnection, guild_id: int, run_id: int) -> dict | None:
    run = await one(
        db,
        """
        SELECT r.*, c.guild_id, coalesce(ch.name, c.channel_id::text) AS channel, c.topic,
               j.result AS host_result, j.status AS host_status, j.error AS host_error
        FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
        LEFT JOIN channels ch ON ch.id = c.channel_id
        LEFT JOIN agent_host_jobs j ON j.id = 'reply:' || r.id
        WHERE r.id = %s AND c.guild_id = %s
        """,
        (run_id, guild_id),
    )
    if run is None:
        return None
    run["triggers"] = await rows(
        db,
        """
        SELECT m.id, m.author_name, m.content, m.created_at, cl.check_hit, cl.jev_choice,
               cl.jev_confidence, cl.outcome
        FROM messages m LEFT JOIN classifications cl ON cl.message_id = m.id
        WHERE m.id = ANY(%s) AND m.guild_id = %s ORDER BY m.id
        """,
        (run["trigger_message_ids"], guild_id),
    )
    run["replies"] = await rows(
        db,
        "SELECT id, content FROM messages WHERE id = ANY(%s) AND guild_id = %s ORDER BY id",
        (run["reply_message_ids"] or [], guild_id),
    )
    return run


async def master(db: AsyncConnection, guild_ids) -> list[dict]:
    """One row per server for the operator."""
    return await rows(
        db,
        f"""
        WITH cost AS (SELECT guild_id, at, cost FROM ({COSTS}) x)
        SELECT g.id, coalesce(g.name, g.id::text) AS name,
          (SELECT coalesce(sum(cost), 0) FROM cost
           WHERE guild_id = g.id AND at > now() - interval '24 hours') AS cost_24h,
          (SELECT coalesce(sum(cost), 0) FROM cost
           WHERE guild_id = g.id AND at > now() - interval '7 days') AS cost_7d,
          (SELECT coalesce(sum(cost), 0) FROM cost
           WHERE guild_id = g.id AND at > now() - interval '30 days') AS cost_30d,
          (SELECT count(*) FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
           WHERE c.guild_id = g.id AND r.status = 'done'
             AND r.created_at > now() - interval '7 days') AS replies_7d,
          (SELECT count(*) FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
           WHERE c.guild_id = g.id AND r.status = 'failed'
             AND r.created_at > now() - interval '7 days') AS failed_7d,
          (SELECT count(*) FROM conversations c
           WHERE c.guild_id = g.id AND c.closed_at IS NULL) AS open_conversations
        FROM guilds g WHERE g.id = ANY(%s) ORDER BY cost_30d DESC, name
        """,
        (list(guild_ids),),
    )


async def recent_failures(db: AsyncConnection, guild_ids, limit: int = 10) -> list[dict]:
    return await rows(
        db,
        """
        SELECT r.id, c.guild_id, coalesce(g.name, c.guild_id::text) AS guild, r.error,
               r.created_at
        FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
        LEFT JOIN guilds g ON g.id = c.guild_id
        WHERE r.status = 'failed' AND c.guild_id = ANY(%s)
        ORDER BY r.id DESC LIMIT %s
        """,
        (list(guild_ids), limit),
    )


async def audit(db: AsyncConnection, guild_id: int | None, limit: int = 100) -> list[dict]:
    return await rows(
        db,
        """
        SELECT a.at, a.actor_id, a.guild_id, a.action, a.detail,
               coalesce(g.name, a.guild_id::text) AS guild,
               (SELECT user_name FROM console_sessions s WHERE s.user_id = a.actor_id
                ORDER BY created_at DESC LIMIT 1) AS actor_name
        FROM console_audit a LEFT JOIN guilds g ON g.id = a.guild_id
        WHERE %(g)s::bigint IS NULL OR a.guild_id = %(g)s
        ORDER BY a.id DESC LIMIT %(limit)s
        """,
        {"g": guild_id, "limit": limit},
    )
