import asyncio
import time
import aiohttp

class TokenManager:
    def __init__(self, corpid, secret, hardcoded_token=None):
        self.corpid = corpid
        self.secret = secret
        self.access_token = hardcoded_token
        self.expires_at = 0 # 0 表示未知或硬编码
        self._lock = asyncio.Lock()

    async def get_token(self, force_refresh=False):
        async with self._lock:
            now = time.time()
            
            # 优化：如果是强制刷新，但发现当前 Token 其实很"新鲜"（剩余有效期 > 10分钟），
            # 说明刚刚已经被其他并发任务刷新过了，直接返回新 Token，避免重复请求。
            if force_refresh and self.expires_at > 0 and now + 600 < self.expires_at:
                return self.access_token

            # 1. 如果不强制刷新，且当前有token
            if not force_refresh and self.access_token:
                # 如果有过期时间记录（说明是自动获取的），且剩余时间 > 10分钟 (600秒)
                if self.expires_at > 0:
                    if now + 600 < self.expires_at:
                        return self.access_token
                else:
                    # 硬编码Token，默认认为有效，除非外部强制刷新
                    return self.access_token
            
            # 2. 执行刷新
            return await self._do_refresh(now)

    async def _do_refresh(self, now):
        print(f"🔄 正在刷新 Access Token...")
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corpid}&corpsecret={self.secret}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("errcode") == 0:
                        self.access_token = data.get("access_token")
                        self.expires_at = now + data.get("expires_in", 7200)
                        print(f"✅ Token 更新成功! 有效期至: {time.strftime('%H:%M:%S', time.localtime(self.expires_at))}")
                        print(f"🔑 新 Token: {self.access_token}")
                        return self.access_token
                    else:
                        print(f"❌ 刷新 Token 失败: {data}")
                        return None
        except Exception as e:
             print(f"❌ 刷新 Token 异常: {e}")
             return None
