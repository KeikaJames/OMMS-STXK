-- 抢位：开放时间、锁定、批次、栅栏、查重、扣减与登记在途成员一步完成。
-- KEYS: stock reg op open_at maint locked fence inflight close_at batch batchmap
-- ARGV: lease ttl member idem_prefix class
-- 分批报名时 batch 为当前批次编号，batchmap 为班级到批次编号；同时报名时 batch 不存在，跳过批次检查。
-- 返回: 1 抢到 / 2 本次重放 / 3 同一请求的其他尝试在处理 / 0 满员
--       -1 已报名或有其他操作 / -2 名额未初始化 / -3 维护或同步中
--       -4 未开放、未设开放时间或所在批次未到 / -5 参数或写入错误 / -6 已锁定、已截止或所在批次已截止
if redis.replicate_commands then redis.replicate_commands() end
local ttl = tonumber(ARGV[2])
if not ttl or ttl <= 0 then return -5 end
local current = redis.call('GET', KEYS[3])
if current == ARGV[1] then return 2 end
if current and string.sub(current, 1, #ARGV[4]) == ARGV[4] then return 3 end
if redis.call('GET', KEYS[6]) == '1' then return -6 end
if KEYS[10] then
  local active = tonumber(redis.call('GET', KEYS[10]))
  if active then
    local mine = tonumber(redis.call('HGET', KEYS[11], ARGV[5] or '') or '')
    if not mine or mine > active then return -4 end
    if mine < active then return -6 end
  end
end
local now = tonumber(redis.call('TIME')[1])
local close_at = redis.call('GET', KEYS[9])
if close_at and now >= tonumber(close_at) then return -6 end
if redis.call('EXISTS', KEYS[5]) == 1 or redis.call('EXISTS', KEYS[7]) == 1 then return -3 end
local left = tonumber(redis.call('GET', KEYS[1]))
if not left then return -2 end
local open_at = redis.call('GET', KEYS[4])
if not open_at then return -4 end
if now < tonumber(open_at) then return -4 end
if redis.call('EXISTS', KEYS[2]) == 1 or current then return -1 end
if left <= 0 then return 0 end
local decremented = redis.pcall('DECR', KEYS[1])
if type(decremented) == 'table' and decremented.err then return -5 end
local leased = redis.pcall('SET', KEYS[3], ARGV[1], 'EX', ttl)
if type(leased) == 'table' and leased.err then
  redis.call('INCR', KEYS[1])
  return -5
end
local added = redis.pcall('SADD', KEYS[8], ARGV[3])
if type(added) == 'table' and added.err then
  redis.call('DEL', KEYS[3])
  redis.call('INCR', KEYS[1])
  return -5
end
return 1
