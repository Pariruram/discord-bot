-- Fix: `conversation_id` is both an OUT parameter of `RETURNS TABLE` and a
-- column of `conversation_sessions`, so the original UPDATE raised
-- `42702: column reference "conversation_id" is ambiguous`.
--
-- Apply this file on a database that already ran
-- `20261108000100_memory.sql` before the fix. Fresh installs only need the
-- updated `20261108000100_memory.sql`.

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

notify pgrst, 'reload schema';
