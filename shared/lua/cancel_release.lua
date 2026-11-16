-- 退选落库后归还名额并清理镜像；每个退选事件只归还一次。
-- KEYS: stock reg op inflight marker   ARGV: mirror legacy_mirror lease member
-- 返回: 1 已归还 / 0 已处理过 / 2 租约已失效（由单社团修复处理库存） / -1 库存键缺失
if redis.call('EXISTS', KEYS[5]) == 1 then return 0 end
local confirmed = redis.call('GET', KEYS[2])
local owned = redis.call('GET', KEYS[3]) == ARGV[3]
if not owned then
  if confirmed == ARGV[1] or confirmed == ARGV[2] then redis.call('DEL', KEYS[2]) end
  redis.call('SREM', KEYS[4], ARGV[4])
  redis.call('SET', KEYS[5], '1', 'EX', 604800)
  return 2
end
local stock = redis.call('GET', KEYS[1])
if not stock or not tonumber(stock) then return -1 end
redis.call('SET', KEYS[5], '1', 'EX', 604800)
if confirmed == ARGV[1] or confirmed == ARGV[2] then redis.call('DEL', KEYS[2]) end
redis.call('INCR', KEYS[1])
redis.call('DEL', KEYS[3])
redis.call('SREM', KEYS[4], ARGV[4])
return 1
