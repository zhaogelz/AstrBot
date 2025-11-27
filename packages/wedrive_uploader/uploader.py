import asyncio
import aiohttp
import os
import base64
import struct
import time
import logging

# 日志记录器
logger = logging.getLogger("astrbot")

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
        """
        获取中间状态 (Little Endian)。
        """
        return b''.join(struct.pack('<I', x) for x in self._h).hex()

    def final_hex(self):
        """
        获取最终 Digest (Standard Big Endian)。
        """
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


class WeDriveUploader:
    def __init__(self, token_mgr, space_id):
        self.token_mgr = token_mgr
        self.space_id = space_id
        self.CHUNK_SIZE = 2 * 1024 * 1024
        self.MAX_CONCURRENT_UPLOADS = 3

    def calculate_block_shas(self, file_path):
        """
        计算文件分块 SHA。此函数为 CPU 密集型。
        """
        logger.info(f"🧮 正在计算 SHA (文件: {os.path.basename(file_path)})...")
        
        if not os.path.exists(file_path):
            logger.error(f"❌ 文件不存在: {file_path}")
            return None, 0

        file_size = os.path.getsize(file_path)
        block_shas = []
        sha1 = SafeSHA1()
        
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                
                sha1.update(chunk)
                
                is_last = (f.tell() == file_size)
                
                if is_last:
                    digest = sha1.final_hex()
                    block_shas.append(digest)
                else:
                    state = sha1.get_state_hex()
                    block_shas.append(state)
                
        logger.info(f"✅ SHA 计算完成")
        return block_shas, file_size

    async def upload_part_task(self, session, upload_key, index, chunk_data, sem):
        """
        单个分块上传任务，受信号量 sem 控制并发数
        """
        # 转换为 Base64
        b64_content = base64.b64encode(chunk_data).decode('utf-8')
        payload = {
            "upload_key": upload_key,
            "index": index,
            "file_base64_content": b64_content
        }
        
        async with sem: # 获取并发锁
            for retry in range(5): # 增加重试次数以适应Token刷新
                access_token = await self.token_mgr.get_token()
                if not access_token:
                    logger.error(f"   ❌ 分块 {index} 失败: 无法获取Token")
                    return False

                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_part?access_token={access_token}"
                
                try:
                    async with session.post(url, json=payload, timeout=120) as response:
                        res_data = await response.json()
                        if res_data.get("errcode") == 0:
                            # logger.debug(f"   ⬆️ 分块 {index} 上传成功")
                            return True
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            logger.warning(f"   ⚠️ 分块 {index} Token失效 ({res_data.get('errcode')})，正在刷新并重试...")
                            await self.token_mgr.get_token(force_refresh=True)
                            await asyncio.sleep(1) 
                            continue
                        else:
                            logger.warning(f"   ⚠️ 分块 {index} 失败 (Retrying): {res_data}")
                except Exception as e:
                    logger.warning(f"   ⚠️ 分块 {index} 网络异常: {e}")
                    await asyncio.sleep(1)
                    
            logger.error(f"   ❌ 分块 {index} 最终失败")
            return False

    async def upload_file(self, file_path):
        """
        上传文件的主逻辑
        """
        # 1. 计算 SHA (在单独线程中运行，不阻塞事件循环)
        block_shas, file_size = await asyncio.to_thread(self.calculate_block_shas, file_path)
        if not block_shas: return None

        async with aiohttp.ClientSession() as session:
            # 2. 初始化上传 (带Token重试逻辑)
            logger.info(f"\n📡 [1/3] 初始化上传: {os.path.basename(file_path)}")
            upload_key = None
            
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return None

                init_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_init?access_token={access_token}"
                init_payload = {
                    "spaceid": self.space_id,
                    "fatherid": self.space_id,
                    "file_name": os.path.basename(file_path),
                    "size": file_size,
                    "block_sha": block_shas,
                    "skip_push_card": False
                }
                
                try:
                    async with session.post(init_url, json=init_payload) as resp:
                        init_res = await resp.json()
                except Exception as e:
                    logger.error(f"❌ 初始化请求异常: {e}")
                    return None
                
                if init_res.get("errcode") == 0:
                    if init_res.get("hit_exist"):
                        logger.info(f"🎉 秒传成功! FileID: {init_res.get('fileid')}")
                        return init_res.get('fileid')
                    upload_key = init_res["upload_key"]
                    logger.info(f"✅ 初始化成功, Key: {upload_key[:10]}...")
                    break
                elif init_res.get("errcode") in [40014, 42001, 41001]:
                    logger.warning(f"⚠️ 初始化遇到Token失效，刷新重试...")
                    await self.token_mgr.get_token(force_refresh=True)
                    continue
                else:
                    logger.error(f"❌ 初始化失败: {init_res}")
                    return None
            
            if not upload_key: return None

            # 3. 并发上传分块
            logger.info(f"\n📡 [2/3] 正在并发上传...")
            
            sem = asyncio.Semaphore(self.MAX_CONCURRENT_UPLOADS)
            pending_tasks = set()
            
            with open(file_path, "rb") as f:
                index = 1
                while True:
                    chunk_data = f.read(self.CHUNK_SIZE)
                    if not chunk_data: break
                    
                    task = asyncio.create_task(
                        self.upload_part_task(session, upload_key, index, chunk_data, sem)
                    )
                    pending_tasks.add(task)
                    
                    if len(pending_tasks) >= self.MAX_CONCURRENT_UPLOADS:
                        done, pending_tasks = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                        for d in done:
                            if not d.result():
                                logger.error("❌ 检测到分块上传失败，停止上传")
                                return None

                    index += 1
            
            if pending_tasks:
                await asyncio.wait(pending_tasks)

            # 4. 完成合并 (带Token重试逻辑)
            logger.info(f"\n📡 [3/3] 合并文件...")
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                finish_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_finish?access_token={access_token}"
                async with session.post(finish_url, json={"upload_key": upload_key}) as resp:
                    finish_res = await resp.json()
                    if finish_res.get("errcode") == 0:
                        file_id = finish_res.get('fileid')
                        logger.info(f"✨ 上传完毕! FileID: {file_id}")
                        return file_id
                    elif finish_res.get("errcode") in [40014, 42001, 41001]:
                        logger.warning(f"⚠️ 合并时Token失效，刷新重试...")
                        await self.token_mgr.get_token(force_refresh=True)
                        continue
                    else:
                        logger.error(f"❌ 合并失败: {finish_res}")
                        return None
        return None
