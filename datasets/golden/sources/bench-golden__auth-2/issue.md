# 提交空口令可以登录任何账号

## 现象

登录接口收到空口令时，`auth.password.verify_password()` 直接返回 True，任何账号都能进去。

这是我在排查另一个问题时偶然发现的：前端有个分支在网络超时后会重发一次登录请求，但请求体里的口令字段是空的，结果那次重发居然成功了。

## 复现步骤

```python
from auth.password import Account, hash_password, verify_password

alice = Account(username="alice", password_hash=hash_password("hunter2"))
print(verify_password(alice, ""))
```

期望输出 False，实际输出 True。口令传 None 也是一样的结果。

## 影响

只要知道用户名就能登录，等于整个口令校验形同虚设。这条必须马上修。

## 另一处相关问题

`password_hash` 为空串表示"这个账号还没设过口令"（比如刚从旧系统导过来、还没走过激活流程）。这种账号目前也是任何口令都能通过校验，包括空口令。这类账号在没设置口令之前应该一律拒绝登录，而不是放行。

## 期望行为

空口令、None、以及没设过口令的账号，三种情况都应当校验失败。正常口令的校验逻辑不要改动。
