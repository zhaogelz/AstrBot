import asyncio
import aiohttp
import os
import base64
import struct
import time
import logging
import json
import shutil

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AutoUploader")

# ==================== SafeSHA1 ====================
class SafeSHA1:
    """
    纯 Python 实现的 SHA1。
    针对企微 C++ 接口特性，增加了字节序调整。
    """
    def __init__(self):
        self._h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
        self._buffer = b''
        self._message_byte_length = 0

    def _left_rotate(self, n, b):
        return ((n << b) | (n >> (32 - b))) & 0xffffffff

    def _process_chunk(self, chunk):
        w = [0] * 80
        # SHA1 标准：Big Endian 解包
        for i in range(16):
            w[i] = struct.unpack(b'>I', chunk[i*4:i*4+4])[0]

        for i in range(16, 80):
            w[i] = self._left_rotate(w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16], 1)

        a, b, c, d, e = self._h

        for i in range(80):
            if 0 <= i <= 19:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif 20 <= i <= 39:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif 40 <= i <= 59:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6

            temp = (self._left_rotate(a, 5) + f + e + k + w[i]) & 0xffffffff
            e = d
            d = c
            c = self._left_rotate(b, 30)
            b = a
            a = temp

        self._h[0] = (self._h[0] + a) & 0xffffffff
        self._h[1] = (self._h[1] + b) & 0xffffffff
        self._h[2] = (self._h[2] + c) & 0xffffffff
        self._h[3] = (self._h[3] + d) & 0xffffffff
        self._h[4] = (self._h[4] + e) & 0xffffffff

    def update(self, data):
        self._message_byte_length += len(data)
        self._buffer += data
        while len(self._buffer) >= 64:
            self._process_chunk(self._buffer[:64])
            self._buffer = self._buffer[64:]

    def get_state_hex(self):
        return b''.join(struct.pack('<I', x) for x in self._h).hex()

    def final_hex(self):
        final_h = list(self._h)
        final_buff = self._buffer
        final_buff += b'\x80'
        while (len(final_buff) + 8) % 64 != 0:
            final_buff += b'\x00'
        bit_len = self._message_byte_length * 8
        final_buff += struct.pack(b'>Q', bit_len)
        temp_runner = SafeSHA1()
        temp_runner._h = final_h
        for i in range(0, len(final_buff), 64):
            temp_runner._process_chunk(final_buff[i:i+64])
        return '{:08x}{:08x}{:08x}{:08x}{:08x}'.format(*temp_runner._h)


# ==================== TokenManager ====================
class TokenManager:
    def __init__(self, corpid, secret):
        self.corpid = corpid
        self.secret = secret
        self.access_token = None
        self.expires_at = 0
        self._lock = asyncio.Lock()

    async def get_token(self, force_refresh=False):
        async with self._lock:
            now = time.time()
            if not force_refresh and self.access_token and self.expires_at > 0:
                if now + 600 < self.expires_at:
                    return self.access_token
            if force_refresh and self.expires_at > 0 and now + 600 < self.expires_at:
                return self.access_token
            return await self._do_refresh(now)

    async def _do_refresh(self, now):
        logger.info(f"🔄 正在刷新 Access Token...")
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corpid}&corpsecret={self.secret}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("errcode") == 0:
                        self.access_token = data.get("access_token")
                        self.expires_at = now + data.get("expires_in", 7200)
                        logger.info(f"✅ Token 更新成功")
                        return self.access_token
                    else:
                        logger.error(f"❌ 刷新 Token 失败: {data}")
                        return None
        except Exception as e:
             logger.error(f"❌ 刷新 Token 异常: {e}")
             return None


# ==================== WeDriveUploader ====================
class WeDriveUploader:
    def __init__(self, token_mgr, space_id):
        self.token_mgr = token_mgr
        self.space_id = space_id
        self.CHUNK_SIZE = 2 * 1024 * 1024
        self.MAX_CONCURRENT_UPLOADS = 3

    def calculate_block_shas(self, file_path):
        logger.info(f"🧮 计算 SHA: {os.path.basename(file_path)}")
        if not os.path.exists(file_path):
            return None, 0
        file_size = os.path.getsize(file_path)
        block_shas = []
        sha1 = SafeSHA1()
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk: break
                sha1.update(chunk)
                is_last = (f.tell() == file_size)
                if is_last:
                    digest = sha1.final_hex()
                    block_shas.append(digest)
                else:
                    state = sha1.get_state_hex()
                    block_shas.append(state)
        return block_shas, file_size

    async def upload_part_task(self, session, upload_key, index, chunk_data, sem):
        b64_content = base64.b64encode(chunk_data).decode('utf-8')
        payload = {"upload_key": upload_key, "index": index, "file_base64_content": b64_content}
        async with sem:
            for retry in range(5):
                access_token = await self.token_mgr.get_token()
                if not access_token: return False
                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_part?access_token={access_token}"
                try:
                    async with session.post(url, json=payload, timeout=120) as response:
                        res_data = await response.json()
                        if res_data.get("errcode") == 0:
                            return True
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            await self.token_mgr.get_token(force_refresh=True)
                            await asyncio.sleep(1)
                            continue
                        else:
                            await asyncio.sleep(1)
                except Exception as e:
                    await asyncio.sleep(1)
            return False

    async def upload_file(self, file_path):
        block_shas, file_size = await asyncio.to_thread(self.calculate_block_shas, file_path)
        if not block_shas: return None

        async with aiohttp.ClientSession() as session:
            upload_key = None
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return None
                init_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_init?access_token={access_token}"
                init_payload = {
                    "spaceid": self.space_id,
                    "fatherid": self.space_id, # 上传到根目录
                    "file_name": os.path.basename(file_path),
                    "size": file_size,
                    "block_sha": block_shas,
                    "skip_push_card": False
                }
                try:
                    async with session.post(init_url, json=init_payload) as resp:
                        init_res = await resp.json()
                except Exception as e:
                    return None
                
                if init_res.get("errcode") == 0:
                    if init_res.get("hit_exist"):
                        logger.info(f"🎉 秒传成功")
                        return init_res.get('fileid')
                    upload_key = init_res["upload_key"]
                    break
                elif init_res.get("errcode") in [40014, 42001, 41001]:
                    await self.token_mgr.get_token(force_refresh=True)
                    continue
                else:
                    logger.error(f"❌ 初始化失败: {init_res}")
                    return None
            
            if not upload_key: return None

            sem = asyncio.Semaphore(self.MAX_CONCURRENT_UPLOADS)
            pending_tasks = set()
            
            with open(file_path, "rb") as f:
                index = 1
                while True:
                    chunk_data = f.read(self.CHUNK_SIZE)
                    if not chunk_data: break
                    task = asyncio.create_task(self.upload_part_task(session, upload_key, index, chunk_data, sem))
                    pending_tasks.add(task)
                    if len(pending_tasks) >= self.MAX_CONCURRENT_UPLOADS:
                        done, pending_tasks = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                        for d in done:
                            if not d.result(): return None
                    index += 1
            if pending_tasks: await asyncio.wait(pending_tasks)

            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                finish_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_finish?access_token={access_token}"
                async with session.post(finish_url, json={"upload_key": upload_key}) as resp:
                    finish_res = await resp.json()
                    if finish_res.get("errcode") == 0:
                        logger.info(f"✨ 上传完成")
                        return finish_res.get('fileid')
                    elif finish_res.get("errcode") in [40014, 42001, 41001]:
                        await self.token_mgr.get_token(force_refresh=True)
                        continue
                    else:
                        logger.error(f"❌ 合并失败: {finish_res}")
                        return None
        return None

# ==================== Main ====================
async def main():
    # 1. 加载配置
    config_path = "data/config/wedrive_uploader.json"
    if not os.path.exists(config_path):
        logger.error(f"❌ 配置文件不存在: {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if not all([config.get('corpid'), config.get('secret'), config.get('space_id')]):
        logger.error("❌ 配置不完整 (缺少 corpid, secret 或 space_id)")
        return

    # 2. 初始化
    token_mgr = TokenManager(config['corpid'], config['secret'])
    uploader = WeDriveUploader(token_mgr, config['space_id'])

    source_dir = "a"
    target_dir = "b"

    if not os.path.exists(source_dir):
        os.makedirs(source_dir)
        logger.info(f"📂 已创建源目录 '{source_dir}'，请将要上传的文件放入此目录。")
        return # 首次创建，等待用户放入文件

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
        logger.info(f"📂 已创建目标目录 '{target_dir}'。")

    # 3. 扫描并上传
    files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
    
    if not files:
        logger.info(f"📂 '{source_dir}' 目录为空，没有需要上传的文件。")
        return

    logger.info(f"🚀 发现 {len(files)} 个文件，开始上传...")

    for filename in files:
        file_path = os.path.join(source_dir, filename)
        logger.info(f"\n======== 处理: {filename} ========")
        
        file_id = await uploader.upload_file(file_path)
        
        if file_id:
            # 上传成功，移动文件
            try:
                shutil.move(file_path, os.path.join(target_dir, filename))
                logger.info(f"✅ 文件已移动到 '{target_dir}'")
            except Exception as e:
                logger.error(f"⚠️ 移动文件失败: {e}")
        else:
            logger.error(f"❌ 上传失败，跳过移动: {filename}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
