-- 单社团修复：持有栅栏时写入库存与报名镜像，清空在途集合并释放栅栏。
-- KEYS: fence stock inflight reg...   ARGV: token stock mirror...（空字符串表示删除镜像）
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2])
redis.call('DEL', KEYS[3])
for i = 4, #KEYS do
  local value = ARGV[i - 1]
  if value == '' then redis.call('DEL', KEYS[i]) else redis.call('SET', KEYS[i], value) end
end
redis.call('DEL', KEYS[1])
return 1
