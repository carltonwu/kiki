-- Custom emoji leave the lingo list. Listed in every prompt, the few seeded in
-- 0014 became all Kiki used; she finds the server's emoji with search_emojis
-- (`guild_emojis`, 0017) instead.
DELETE FROM guild_lingo WHERE phrase ~ '^<a?:\w+:\d+>$';
