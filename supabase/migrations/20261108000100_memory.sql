-- Memory schema for the Discord bot.
--
-- Apply this once in the Supabase SQL editor (or with `supabase db push`).
-- The old `chat_history` and `channel_context` tables are left untouched and
-- are no longer used by the bot. See DEPLOY.md for the optional backfill.
--
-- Design notes:
--   * `conversations` is the scope: DM, channel or thread.
--   * `messages` always belongs to exactly one conversation.
--   * `user_facts` is long-term semantic memory with bi-temporal validity.
--   * `memory_jobs` is a transactional outbox processed inside the web process.

create table if not exists bot_settings (
    key        text primary key,
    value      text,
    updated_at timestamptz not null default now()
);

create table if not exists users (
    user_id        text primary key,
    username       text,
    global_name    text,
    locale         text,
    timezone       text,
    memory_enabled boolean not null default true,
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now()
);

create table if not exists user_personas (
    user_id     text primary key references users(user_id) on delete cascade,
    persona     jsonb not null default '{}'::jsonb,
    prompt_hint text,
    updated_at  timestamptz not null default now()
);

create table if not exists user_facts (
    id                uuid primary key default gen_random_uuid(),
    user_id           text not null references users(user_id) on delete cascade,
    scope             text not null default 'global'
                      check (scope in ('global', 'guild')),
    guild_id          text,
    kind              text not null
                      check (kind in ('identity', 'preference', 'event', 'opinion', 'relation')),
    content           text not null,
    content_hash      text not null,
    confidence        real not null default 0.7,
    importance        real not null default 0.5,
    visibility        text not null default 'private'
                      check (visibility in ('private', 'public')),
    valid_from        timestamptz not null default now(),
    valid_to          timestamptz,
    superseded_by     uuid references user_facts(id) on delete set null,
    source_message_id bigint,
    source_quote      text,
    last_confirmed_at timestamptz not null default now(),
    created_at        timestamptz not null default now(),
    unique (user_id, content_hash)
);

create table if not exists conversations (
    id                        uuid primary key default gen_random_uuid(),
    scope_type                text not null
                              check (scope_type in ('dm', 'channel', 'thread')),
    scope_id                  text not null,
    guild_id                  text,
    parent_id                 uuid references conversations(id) on delete set null,
    title                     text,
    last_message_at           timestamptz,
    last_seen_message_id      text,
    last_seen_at              timestamptz,
    last_summary_message_id   bigint not null default 0,
    last_extracted_message_id bigint not null default 0,
    summary_at                timestamptz,
    created_at                timestamptz not null default now(),
    unique (scope_type, scope_id)
);

create table if not exists conversation_sessions (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references conversations(id) on delete cascade,
    started_at      timestamptz not null default now(),
    ended_at        timestamptz,
    message_count   int not null default 0,
    summary         text
);

create table if not exists conversation_summaries (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references conversations(id) on delete cascade,
    session_id      uuid references conversation_sessions(id) on delete set null,
    level           text not null check (level in ('session', 'conversation')),
    content         text not null,
    covers_from     timestamptz,
    covers_to       timestamptz,
    created_at      timestamptz not null default now()
);

create table if not exists messages (
    id                 bigint generated always as identity primary key,
    conversation_id    uuid not null references conversations(id) on delete cascade,
    session_id         uuid references conversation_sessions(id) on delete set null,
    discord_message_id text unique,
    author_id          text,
    author_name        text not null,
    is_bot             boolean not null default false,
    role               text not null check (role in ('user', 'assistant', 'system')),
    addressed_to       text not null default 'ambient'
                       check (addressed_to in ('bot', 'human', 'ambient')),
    reply_to_id        text,
    content            text not null,
    content_hash       text,
    tokens             int,
    created_at         timestamptz not null default now(),
    edited_at          timestamptz,
    deleted_at         timestamptz,
    metadata           jsonb not null default '{}'::jsonb
);

create table if not exists memory_jobs (
    id              bigint generated always as identity primary key,
    kind            text not null,
    conversation_id uuid references conversations(id) on delete cascade,
    target_id       bigint not null default 0,
    payload         jsonb not null default '{}'::jsonb,
    status          text not null default 'pending'
                    check (status in ('pending', 'running', 'done', 'failed')),
    attempts        int not null default 0,
    not_before      timestamptz not null default now(),
    locked_at       timestamptz,
    last_error      text,
    created_at      timestamptz not null default now()
);

create index if not exists messages_conversation_id_idx
    on messages (conversation_id, id);
create index if not exists messages_author_time_idx
    on messages (author_id, created_at desc);

create index if not exists user_facts_active_idx
    on user_facts (user_id, valid_to);

create index if not exists conversations_last_message_idx
    on conversations (last_message_at desc);
create index if not exists conversation_sessions_open_idx
    on conversation_sessions (conversation_id, started_at desc);
create index if not exists conversation_summaries_conversation_idx
    on conversation_summaries (conversation_id, created_at desc);

create index if not exists memory_jobs_ready_idx
    on memory_jobs (not_before) where status = 'pending';
create unique index if not exists memory_jobs_dedupe_idx
    on memory_jobs (kind, conversation_id, target_id) where status = 'pending';

-- Atomically ingest one Discord message: conversation, session, message and
-- last-seen marker. Returns the existing row when Discord redelivers a message.
--
-- `#variable_conflict use_column` is required: the OUT parameter
-- `conversation_id` shadows the column of the same name in PL/pgSQL.
create or replace function ingest_message(
    p_scope_type text,
    p_scope_id text,
    p_guild_id text,
    p_discord_message_id text,
    p_author_id text,
    p_author_name text,
    p_is_bot boolean,
    p_role text,
    p_addressed_to text,
    p_reply_to_id text,
    p_content text,
    p_session_gap_minutes int default 30,
    p_metadata jsonb default '{}'::jsonb
) returns table (
    message_id bigint,
    conversation_id uuid,
    session_id uuid,
    duplicate boolean
)
language plpgsql
security definer
set search_path = public
as $$
#variable_conflict use_column
declare
    v_conv conversations%rowtype;
    v_session_id uuid;
    v_message_id bigint;
    v_existing_conv uuid;
    v_existing_session uuid;
    v_now timestamptz := now();
    v_gap interval;
begin
    if p_discord_message_id is not null then
        select m.id, m.conversation_id, m.session_id
          into v_message_id, v_existing_conv, v_existing_session
          from messages m
         where m.discord_message_id = p_discord_message_id;

        if v_message_id is not null then
            return query
                select v_message_id, v_existing_conv, v_existing_session, true;
            return;
        end if;
    end if;

    insert into conversations (scope_type, scope_id, guild_id)
    values (p_scope_type, p_scope_id, p_guild_id)
    on conflict (scope_type, scope_id) do nothing;

    select * into v_conv
      from conversations
     where scope_type = p_scope_type and scope_id = p_scope_id
     for update;

    v_gap := make_interval(mins => greatest(coalesce(p_session_gap_minutes, 30), 1));

    if v_conv.last_message_at is null
       or v_now - v_conv.last_message_at > v_gap then
        update conversation_sessions s
           set ended_at = coalesce(v_conv.last_message_at, v_now)
         where s.conversation_id = v_conv.id and s.ended_at is null;

        insert into conversation_sessions (conversation_id, started_at)
        values (v_conv.id, v_now)
        returning id into v_session_id;
    else
        select s.id into v_session_id
          from conversation_sessions s
         where s.conversation_id = v_conv.id and s.ended_at is null
         order by s.started_at desc
         limit 1;

        if v_session_id is null then
            insert into conversation_sessions (conversation_id, started_at)
            values (v_conv.id, v_now)
            returning id into v_session_id;
        end if;
    end if;

    insert into messages (
        conversation_id, session_id, discord_message_id, author_id, author_name,
        is_bot, role, addressed_to, reply_to_id, content, metadata, created_at
    ) values (
        v_conv.id, v_session_id, p_discord_message_id, p_author_id, p_author_name,
        p_is_bot, p_role, p_addressed_to, p_reply_to_id, p_content, p_metadata, v_now
    )
    returning id into v_message_id;

    update conversation_sessions
       set message_count = message_count + 1
     where id = v_session_id;

    update conversations
       set last_message_at = v_now,
           last_seen_message_id = coalesce(p_discord_message_id, last_seen_message_id),
           last_seen_at = case
               when p_discord_message_id is null then last_seen_at
               else v_now
           end
     where id = v_conv.id;

    if p_author_id is not null and not p_is_bot then
        insert into users (user_id, username, last_seen_at)
        values (p_author_id, p_author_name, v_now)
        on conflict (user_id) do update
           set username = excluded.username,
               last_seen_at = excluded.last_seen_at;
    end if;

    return query select v_message_id, v_conv.id, v_session_id, false;
end;
$$;

create or replace function history_before(
    p_conversation_id uuid,
    p_before_id bigint,
    p_limit int default 20
) returns table (
    id bigint,
    discord_message_id text,
    author_id text,
    author_name text,
    is_bot boolean,
    role text,
    addressed_to text,
    reply_to_id text,
    content text,
    created_at timestamptz,
    session_id uuid
)
language sql
stable
as $$
    select *
      from (
          select m.id, m.discord_message_id, m.author_id, m.author_name,
                 m.is_bot, m.role, m.addressed_to, m.reply_to_id, m.content,
                 m.created_at, m.session_id
            from messages m
           where m.conversation_id = p_conversation_id
             and m.deleted_at is null
             and (p_before_id is null or m.id < p_before_id)
           order by m.id desc
           limit greatest(p_limit, 1)
      ) recent
     order by recent.id asc;
$$;

create or replace function soft_delete_message(
    p_discord_message_id text
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    update user_facts f
       set valid_to = now()
      from messages m
     where m.discord_message_id = p_discord_message_id
       and f.source_message_id = m.id
       and f.valid_to is null;

    update messages
       set deleted_at = now()
     where discord_message_id = p_discord_message_id
       and deleted_at is null;
end;
$$;

create or replace function update_message_content(
    p_discord_message_id text,
    p_content text
) returns void
language sql
security definer
set search_path = public
as $$
    update messages
       set content = p_content,
           edited_at = now()
     where discord_message_id = p_discord_message_id;
$$;

create or replace function enqueue_memory_job(
    p_kind text,
    p_conversation_id uuid,
    p_target_id bigint default 0,
    p_not_before timestamptz default now(),
    p_payload jsonb default '{}'::jsonb
) returns void
language sql
security definer
set search_path = public
as $$
    insert into memory_jobs (kind, conversation_id, target_id, not_before, payload)
    values (p_kind, p_conversation_id, p_target_id, p_not_before, p_payload)
    on conflict (kind, conversation_id, target_id) where status = 'pending'
    do update set
        not_before = greatest(memory_jobs.not_before, excluded.not_before),
        payload = excluded.payload;
$$;

create or replace function claim_memory_jobs(
    p_batch int default 5
) returns setof memory_jobs
language sql
security definer
set search_path = public
as $$
    with picked as (
        select id
          from memory_jobs
         where status = 'pending' and not_before <= now()
         order by not_before
         limit greatest(p_batch, 1)
         for update skip locked
    )
    update memory_jobs j
       set status = 'running',
           locked_at = now(),
           attempts = j.attempts + 1
      from picked
     where j.id = picked.id
    returning j.*;
$$;

create or replace function finish_memory_job(
    p_id bigint
) returns void
language sql
security definer
set search_path = public
as $$
    update memory_jobs
       set status = 'done', locked_at = null
     where id = p_id;
$$;

create or replace function fail_memory_job(
    p_id bigint,
    p_error text,
    p_delay_seconds int default 300
) returns void
language sql
security definer
set search_path = public
as $$
    update memory_jobs j
       set status = case
               when j.attempts >= 3 then 'failed'
               when exists (
                   select 1
                     from memory_jobs p
                    where p.status = 'pending'
                      and p.kind = j.kind
                      and p.conversation_id is not distinct from j.conversation_id
                      and p.target_id = j.target_id
                      and p.id <> j.id
               ) then 'failed'
               else 'pending'
           end,
           not_before = case
               when j.attempts >= 3 then j.not_before
               else now() + make_interval(secs => greatest(p_delay_seconds, 1))
           end,
           locked_at = null,
           last_error = left(coalesce(p_error, ''), 2000)
     where j.id = p_id;
$$;

create or replace function requeue_stale_jobs() returns void
language sql
security definer
set search_path = public
as $$
    update memory_jobs j
       set status = case
               when exists (
                   select 1
                     from memory_jobs p
                    where p.status = 'pending'
                      and p.kind = j.kind
                      and p.conversation_id is not distinct from j.conversation_id
                      and p.target_id = j.target_id
                      and p.id <> j.id
               ) then 'failed'
               else 'pending'
           end,
           locked_at = null
     where j.status = 'running'
       and j.locked_at < now() - interval '10 minutes';
$$;
