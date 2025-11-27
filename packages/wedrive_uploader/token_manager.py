import asyncio
import time
import aiohttp

class TokenManager:
    def __init__(self, corpid, secret, hardcoded_token=None, save_token_callback=None):
        self.corpid = corpid
        self.secret = secret
        self.access_token = hardcoded_token
        self.expires_at = 0 # 0 表示未知或硬编码
        self.save_token_callback = save_token_callback
        self._lock = asyncio.Lock()
        
        if self.access_token:
            print(f"⚠️ [TokenManager] 使用硬编码 Token (调试模式)")

    async def get_token(self, force_refresh=False):
        async with self._lock:
            now = time.time()

            # 1. 检查缓存/硬编码 Token 是否可用
            # 如果没有强制刷新，且当前有 Token
            if not force_refresh and self.access_token:
                # 只有当有过期时间记录时，才检查是否过期
                if self.expires_at > 0:
                    if now + 600 < self.expires_at:
                        return self.access_token
                else:
                    # 硬编码 Token，默认认为有效，除非外部强制刷新
                    return self.access_token
            
            # 2. 如果是强制刷新，但 Token 刚刚被更新过（防止并发刷新）
            if force_refresh and self.expires_at > 0 and now + 600 < self.expires_at:
                return self.access_token

            # 3. 执行刷新 (Token 为空，或过期，或被强制刷新)
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
                        
                        # 如果配置了回调，保存 Token 到配置文件
                        if self.save_token_callback:
                            self.save_token_callback(self.access_token)
                            
                        return self.access_token
                    else:
                        print(f"❌ 刷新 Token 失败: {data}")
                        return None
        except Exception as e:
             print(f"❌ 刷新 Token 异常: {e}")
             return None
