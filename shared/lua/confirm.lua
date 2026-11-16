-- 落库后确认：写入报名镜像，释放租约与在途成员。租约已失效时返回 0。
-- KEYS: op reg inflight   ARGV: lease mirror member
if redis.call('GET', KEYS[2]) == ARGV[2] then
  if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
  redis.call('SREM', KEYS[3], ARGV[3])
  return 1
end
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2])
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[3], ARGV[3])
return 1
