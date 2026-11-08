-- Fix: a bot reply is stored after the next user message when the user
-- follows up quickly, so `history_before` used to miss the bot's own previous
-- answer and short replies like "для тебя" lost their context.
--
-- Apply this file on a database that already ran
-- `20261108000100_memory.sql` before the fix. Fresh installs only need the
-- updated `20261108000100_memory.sql`.

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
             and (
                 p_before_id is null
                 or m.id < p_before_id
                 -- A bot reply is written after the next user message when the
                 -- user follows up quickly. It still belongs to the context of
                 -- that next message, so include replies to older messages.
                 or (
                     m.is_bot
                     and m.id > p_before_id
                     and m.reply_to_id in (
                         select prior.discord_message_id
                           from messages prior
                          where prior.conversation_id = p_conversation_id
                            and prior.id < p_before_id
                            and prior.discord_message_id is not null
                     )
                 )
             )
           order by m.id desc
           limit greatest(p_limit, 1)
      ) recent
     order by recent.id asc;
$$;

notify pgrst, 'reload schema';
