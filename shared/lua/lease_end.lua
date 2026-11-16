-- 释放自己持有的租约；可同时移出在途成员。
-- KEYS: lease [set]   ARGV: value [member]
local removed = 0
if redis.call('GET', KEYS[1]) == ARGV[1] then removed = redis.call('DEL', KEYS[1]) end
if KEYS[2] and ARGV[2] then redis.call('SREM', KEYS[2], ARGV[2]) end
return removed
