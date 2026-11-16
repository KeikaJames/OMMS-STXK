-- 登录失败计数；窗口从第一次失败开始计时。
-- KEYS: counter   ARGV: window_seconds
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
return n
