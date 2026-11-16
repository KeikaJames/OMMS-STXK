-- 按学生修正报名镜像。mode=absent 只在镜像缺失时写入；mode=clear 只删除指定的旧值。
-- KEYS: reg   ARGV: mode value
local current = redis.call('GET', KEYS[1])
if ARGV[1] == 'absent' then
  if current then return 0 end
  redis.call('SET', KEYS[1], ARGV[2])
  return 1
end
if ARGV[1] == 'clear' then
  if current == ARGV[2] then return redis.call('DEL', KEYS[1]) end
  return 0
end
return -1
