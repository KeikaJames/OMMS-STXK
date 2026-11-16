-- 退选开始：取得学生租约并登记为该社团的在途成员。
-- KEYS: maint fence op inflight locked   ARGV: lease ttl member
-- 返回: 1 成功 / -1 有其他操作 / -3 维护或同步中 / -6 已锁定
if redis.call('GET', KEYS[5]) == '1' then return -6 end
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then return -3 end
local current = redis.call('GET', KEYS[3])
if current == ARGV[1] then return 1 end
if current then return -1 end
redis.call('SET', KEYS[3], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('SADD', KEYS[4], ARGV[3])
return 1
