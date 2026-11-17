-- 只有账号与角色代际仍等于登录读取时的值，才签发新会话并推进账号代际。
-- KEYS: role_epoch principal_version session role_mutation principal_mutation
-- ARGV: payload ttl expected_epoch expected_version
local epoch = tonumber(redis.call('GET', KEYS[1]) or '0')
local version = tonumber(redis.call('GET', KEYS[2]) or '0')
if not epoch or not version then return -1 end
if redis.call('EXISTS', KEYS[4]) == 1 or redis.call('EXISTS', KEYS[5]) == 1 then return -2 end
if epoch ~= tonumber(ARGV[3]) or version ~= tonumber(ARGV[4]) then return 0 end
local next_version = version + 1
local payload = cjson.decode(ARGV[1])
payload['_session_epoch'] = epoch
payload['_session_version'] = next_version
local created = redis.pcall('SET', KEYS[3], cjson.encode(payload), 'EX', tonumber(ARGV[2]))
if type(created) == 'table' and created.err then return -3 end
local advanced = redis.pcall('INCR', KEYS[2])
if type(advanced) == 'table' and advanced.err then
  redis.call('DEL', KEYS[3])
  return -3
end
if tonumber(advanced) ~= next_version then
  redis.call('DEL', KEYS[3])
  return -3
end
return 1
