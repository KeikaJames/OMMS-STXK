-- 退选开始：取得学生租约并登记为该社团的在途成员。
-- KEYS: maint fence op inflight locked close_at batch batchmap   ARGV: lease ttl member class
-- 分批报名时只有当前批次的学生可以退选，名额归还到当前批次。
-- 返回: 1 成功 / -1 有其他操作 / -3 维护或同步中 / -4 所在批次未到 / -6 已锁定、已截止或所在批次已截止
if redis.replicate_commands then redis.replicate_commands() end
if redis.call('GET', KEYS[5]) == '1' then return -6 end
if KEYS[7] then
  local active = tonumber(redis.call('GET', KEYS[7]))
  if active then
    local mine = tonumber(redis.call('HGET', KEYS[8], ARGV[4] or '') or '')
    if not mine or mine > active then return -4 end
    if mine < active then return -6 end
  end
end
local close_at = redis.call('GET', KEYS[6])
if close_at and tonumber(redis.call('TIME')[1]) >= tonumber(close_at) then return -6 end
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then return -3 end
local current = redis.call('GET', KEYS[3])
if current == ARGV[1] then return 1 end
if current then return -1 end
redis.call('SET', KEYS[3], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('SADD', KEYS[4], ARGV[3])
return 1
