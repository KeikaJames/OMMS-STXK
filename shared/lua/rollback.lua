-- 撤销未落库的抢位。mode=absorb 时不归还库存（数据库已满，用于消化多出的库存）。
-- KEYS: stock op inflight marker   ARGV: lease member mode
-- 返回: 1 已撤销 / 0 已撤销过 / 2 租约已失效（由单社团修复处理库存） / -1 库存键缺失
if redis.call('EXISTS', KEYS[4]) == 1 then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  redis.call('SREM', KEYS[3], ARGV[2])
  redis.call('SET', KEYS[4], '1', 'EX', 604800)
  return 2
end
local stock = redis.call('GET', KEYS[1])
if not stock or not tonumber(stock) then return -1 end
redis.call('SET', KEYS[4], '1', 'EX', 604800)
redis.call('DEL', KEYS[2])
redis.call('SREM', KEYS[3], ARGV[2])
if ARGV[3] ~= 'absorb' then redis.call('INCR', KEYS[1]) end
return 1
