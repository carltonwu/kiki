-- `/kiki clear` closes every open conversation of a server as `cleared`.
ALTER TABLE conversations DROP CONSTRAINT conversations_closed_reason_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_closed_reason_check
    CHECK (closed_reason IN ('superseded', 'evicted', 'cleared'));
