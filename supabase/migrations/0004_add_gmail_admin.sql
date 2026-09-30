-- Second dashboard login, added when auth switched from magic-link to
-- email+password (the ai-automation@gmail.com Supabase Auth account itself
-- was created separately via the Admin API, not through SQL -- this just
-- adds it to the allowlist).

insert into allowed_users (email) values ('ai-automation@gmail.com');
