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
    def __init__(self, token_mgr, space_id, agent_id):
        self.token_mgr = token_mgr
        self.space_id = space_id
        self.agent_id = agent_id
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

    async def recursive_search(self, keyword, start_father_id=None, start_path=""):
        """
        递归搜索文件
        """
        if not start_father_id:
            start_father_id = self.space_id
            
        results = []
        # Queue for BFS: (father_id, current_path_str)
        queue = [(start_father_id, start_path)]
        
        # Limit to prevent excessively long searches
        max_requests = 100 
        request_count = 0
        
        while queue and request_count < max_requests:
            curr_id, curr_path = queue.pop(0)
            
            start = 0
            limit = 100
            
            while True:
                # Small delay to be nice to the API
                if request_count > 0 and request_count % 10 == 0:
                    await asyncio.sleep(0.5)

                request_count += 1
                # We use list_files to get content of current folder
                # NOTE: list_files returns the 'file_list' object (which contains 'item' list)
                file_list_obj = await self.list_files(fatherid=curr_id, start=start, limit=limit)
                
                if not file_list_obj: break
                
                items = file_list_obj.get('item', [])
                if not isinstance(items, list): items = []
                
                for item in items:
                    f_name = item.get('file_name', '')
                    # file_type: 1=folder, 3=doc, 4=sheet, etc.
                    f_type = item.get('file_type') 
                    
                    item_path = (curr_path + "/" + f_name) if curr_path else f_name
                    
                    # Check match (case insensitive?) Let's do case insensitive
                    if keyword.lower() in f_name.lower():
                        results.append({
                            'name': f_name,
                            'path': item_path,
                            'size': item.get('file_size', 0),
                            'is_folder': (f_type == 1),
                            'fileid': item.get('fileid')
                        })
                    
                    # If folder, add to queue to traverse deeper
                    if f_type == 1: 
                        queue.append((item.get('fileid'), item_path))
                
                if len(items) < limit:
                    break
                start += limit
                
        return results

    async def get_file_by_path(self, path_str):
        """
        根据路径获取文件对象 (支持 a/b/c 格式)
        """
        if not path_str: return None
        # Normalize path, split by /
        parts = [p for p in path_str.replace('\\', '/').split('/') if p]
        
        # Start from root
        current_father_id = self.space_id
        target_item = None
        
        for i, part in enumerate(parts):
            # Find 'part' in 'current_father_id'
            found = False
            start = 0
            limit = 100
            
            # Pagination loop to find the file in the current directory
            while True:
                res = await self.list_files(fatherid=current_father_id, start=start, limit=limit)
                if not res: break
                items = res.get('item', [])
                if not isinstance(items, list) or not items: break
                
                for item in items:
                    # item['file_name'] is the name
                    if item.get('file_name') == part:
                        target_item = item
                        current_father_id = item.get('fileid') # Prepare for next level
                        found = True
                        break
                
                if found: break
                if len(items) < limit: break # No more files
                start += limit
            
            if not found:
                # Path component not found
                return None
        
        return target_item

    async def list_files(self, fatherid=None, start=0, limit=100):
        """
        列出微盘文件
        """
        if not fatherid:
            fatherid = self.space_id

        # logger.info(f"📂 正在获取微盘文件列表 (SpaceID: {self.space_id}, Start: {start})...")
        
        async with aiohttp.ClientSession() as session:
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return None

                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_list?access_token={access_token}"
                payload = {
                    "spaceid": self.space_id,
                    "fatherid": fatherid,
                    "sort_type": 3, # 1: name, 2: size, 3: update time
                    "start": start,
                    "limit": limit
                }
                
                try:
                    async with session.post(url, json=payload) as resp:
                        res_data = await resp.json()
                        if res_data.get("errcode") == 0:
                            # API returns {'errcode': 0, 'file_list': {'item': [...]}}
                            return res_data.get("file_list", {})
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            logger.warning(f"⚠️ 获取列表时Token失效，刷新重试...")
                            await self.token_mgr.get_token(force_refresh=True)
                            continue
                        else:
                            logger.error(f"❌ 获取列表失败: {res_data}")
                            return None
                except Exception as e:
                    logger.error(f"❌ 获取列表网络异常: {e}")
                    return None
        return None

    async def create_folder(self, folder_name, fatherid=None):
        """
        新建微盘文件夹
        """
        if not fatherid:
            fatherid = self.space_id

        logger.info(f"📂 正在创建文件夹: {folder_name} (SpaceID: {self.space_id})...")
        
        async with aiohttp.ClientSession() as session:
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return None

                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_create?access_token={access_token}"
                payload = {
                    "spaceid": self.space_id,
                    "fatherid": fatherid,
                    "file_type": 1,
                    "file_name": folder_name
                }
                
                try:
                    async with session.post(url, json=payload) as resp:
                        res_data = await resp.json()
                        if res_data.get("errcode") == 0:
                            # API returns {'errcode': 0, 'fileid': '...', ...}
                            file_id = res_data.get("fileid")
                            logger.info(f"✅ 文件夹创建成功, FileID: {file_id}")
                            return res_data
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            logger.warning(f"⚠️ 创建文件夹时Token失效，刷新重试...")
                            await self.token_mgr.get_token(force_refresh=True)
                            continue
                        else:
                            logger.error(f"❌ 创建文件夹失败: {res_data}")
                            return None
                except Exception as e:
                    logger.error(f"❌ 创建文件夹网络异常: {e}")
                    return None
        return None

    async def move_files(self, file_ids, target_father_id):
        """
        移动文件
        """
        logger.info(f"🚚 正在移动文件 (FileIDs: {file_ids}) -> {target_father_id}...")
        
        async with aiohttp.ClientSession() as session:
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return False

                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_move?access_token={access_token}"
                payload = {
                    "fatherid": target_father_id,
                    "fileid": file_ids,
                    "replace": False 
                }
                
                try:
                    async with session.post(url, json=payload) as resp:
                        res_data = await resp.json()
                        if res_data.get("errcode") == 0:
                            logger.info(f"✅ 移动成功")
                            return True
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            logger.warning(f"⚠️ 移动时Token失效，刷新重试...")
                            await self.token_mgr.get_token(force_refresh=True)
                            continue
                        else:
                            logger.error(f"❌ 移动失败: {res_data}")
                            return False
                except Exception as e:
                    logger.error(f"❌ 移动网络异常: {e}")
                    return False
        return False

    async def create_folder_by_path(self, path_str):
        """
        递归创建文件夹 (支持 a/b/c)
        """
        if not path_str: return None
        parts = [p for p in path_str.replace('\\', '/').split('/') if p]
        current_father_id = self.space_id
        
        created_any = False
        
        for i, part in enumerate(parts):
            # 1. Check if folder exists in current_father_id
            found_folder_id = None
            
            start = 0
            limit = 100
            while True:
                res = await self.list_files(fatherid=current_father_id, start=start, limit=limit)
                if not res: break
                items = res.get('item', [])
                if not isinstance(items, list): break
                if not items: break
                
                for item in items:
                    # Check for folder type (1) and name match
                    if item.get('file_name') == part and item.get('file_type') == 1:
                        found_folder_id = item.get('fileid')
                        break
                
                if found_folder_id: break
                if len(items) < limit: break
                start += limit
            
            if found_folder_id:
                # Folder exists, move into it
                current_father_id = found_folder_id
            else:
                # Folder doesn't exist, create it
                logger.info(f"📂 递归创建: 在 {current_father_id} 下创建 '{part}'")
                res = await self.create_folder(part, fatherid=current_father_id)
                if res and res.get('errcode') == 0:
                    current_father_id = res.get('fileid')
                    created_any = True
                else:
                    logger.error(f"❌ 创建子文件夹 '{part}' 失败")
                    return None
        
        return current_father_id

    async def delete_file(self, file_id):
        """
        删除微盘文件
        """
        logger.info(f"🗑️ 正在删除文件 (FileID: {file_id})...")
        
        async with aiohttp.ClientSession() as session:
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return False

                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_delete?access_token={access_token}"
                payload = {
                    "fileid": [file_id]
                }
                
                try:
                    async with session.post(url, json=payload) as resp:
                        res_data = await resp.json()
                        if res_data.get("errcode") == 0:
                            logger.info(f"✅ 删除成功")
                            return True
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            logger.warning(f"⚠️ 删除时Token失效，刷新重试...")
                            await self.token_mgr.get_token(force_refresh=True)
                            continue
                        else:
                            logger.error(f"❌ 删除失败: {res_data}")
                            return False
                except Exception as e:
                    logger.error(f"❌ 删除网络异常: {e}")
                    return False
        return False

    async def get_download_info(self, file_id):
        """
        获取文件下载信息 (URL 和 Cookie)
        """
        logger.info(f"📥 正在请求下载地址 (FileID: {file_id})...")
        
        async with aiohttp.ClientSession() as session:
            for retry in range(2):
                access_token = await self.token_mgr.get_token()
                if not access_token: return None

                url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_download?access_token={access_token}"
                payload = {"fileid": file_id}
                
                try:
                    async with session.post(url, json=payload) as resp:
                        res_data = await resp.json()
                        if res_data.get("errcode") == 0:
                            return {
                                "download_url": res_data.get("download_url"),
                                "cookie_name": res_data.get("cookie_name"),
                                "cookie_value": res_data.get("cookie_value")
                            }
                        elif res_data.get("errcode") in [40014, 42001, 41001]:
                            logger.warning(f"⚠️ 下载预备时Token失效，刷新重试...")
                            await self.token_mgr.get_token(force_refresh=True)
                            continue
                        else:
                            logger.error(f"❌ 获取下载地址失败: {res_data}")
                            return None
                except Exception as e:
                    logger.error(f"❌ 获取下载地址异常: {e}")
                    return None
        return None

    async def download_file_to_local(self, file_id, file_name, save_dir="data/temp"):
        """
        下载文件到本地
        """
        # 清理 save_dir 中超过24小时的旧文件
        if os.path.exists(save_dir):
            try:
                now = time.time()
                for f in os.listdir(save_dir):
                    f_path = os.path.join(save_dir, f)
                    if os.path.isfile(f_path):
                        if now - os.path.getmtime(f_path) > 24 * 3600:  # 24小时
                            try:
                                os.remove(f_path)
                                logger.info(f"🧹 已清理过期临时文件: {f_path}")
                            except Exception as e:
                                logger.warning(f"⚠️ 清理文件失败 {f_path}: {e}")
            except Exception as e:
                logger.warning(f"⚠️ 自动清理临时目录异常: {e}")

        info = await self.get_download_info(file_id)
        if not info: return None
        
        download_url = info["download_url"]
        cookie_name = info["cookie_name"]
        cookie_value = info["cookie_value"]

        async with aiohttp.ClientSession() as session:
            # 2. 下载文件流
            logger.info(f"📥 开始下载文件流...")
            logger.info(f"[Debug] URL: {download_url}")
            
            headers = {}
            if cookie_name and cookie_value:
                # 修正: 官方文档指示直接使用 cookie_name=cookie_value
                # 实测替换 & 为 ; 会导致 400 错误，说明 authkey 可能就是一个包含 & 的长字符串，
                # 或者服务端不接受标准分号分隔。
                # 必须手动构造 Header 以避免 aiohttp 对特殊字符进行 URL 编码。
                cookie_str = f"{cookie_name}={cookie_value}"
                headers["Cookie"] = cookie_str
                logger.info(f"[Debug] Cookie Header: {cookie_str}")
            
            try:
                async with session.get(download_url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ 下载请求失败，状态码: {resp.status}")
                        try:
                            logger.error(f"❌ 响应内容: {await resp.text()}")
                        except:
                            pass
                        return None
                    
                    if not os.path.exists(save_dir):
                        os.makedirs(save_dir, exist_ok=True)
                        
                    save_path = os.path.join(save_dir, file_name)
                    
                    with open(save_path, "wb") as f:
                        while True:
                            chunk = await resp.content.read(1024 * 1024) # 1MB chunks
                            if not chunk: break
                            f.write(chunk)
                    
                    logger.info(f"✅ 文件已下载: {save_path}")
                    return os.path.abspath(save_path)
            except Exception as e:
                logger.error(f"❌ 下载文件流异常: {e}")
                return None

    async def upload_to_webhook(self, file_path, webhook_key):
        """
        上传文件到 Webhook 获取 media_id
        (手动构建 multipart 以解决中文乱码问题)
        """
        if not os.path.exists(file_path):
            logger.error(f"❌ 文件不存在: {file_path}")
            return None

        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={webhook_key}&type=file"
        filename = os.path.basename(file_path)
        
        # 使用时间戳生成简单的 boundary
        boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
        content_type = 'application/octet-stream'
        
        async def producer():
            # Part Header
            # 显式使用 UTF-8 编码 filename，不进行 RFC 5987 转义，兼容企微
            safe_filename = filename.replace('"', '\\"')
            header = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="media"; filename="{safe_filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            )
            yield header.encode('utf-8')
            
            # File Content
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    yield chunk
            
            # Footer
            footer = f"\r\n--{boundary}--\r\n"
            yield footer.encode('utf-8')

        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, data=producer(), headers=headers) as resp:
                    res_data = await resp.json()
                    if res_data.get("errcode") == 0:
                        media_id = res_data.get("media_id")
                        logger.info(f"✅ Webhook 素材上传成功, media_id: {media_id}")
                        return media_id
                    else:
                        logger.error(f"❌ Webhook 素材上传失败: {res_data}")
                        return None
            except Exception as e:
                logger.error(f"❌ Webhook 素材上传异常: {e}")
                return None

    async def push_file_via_webhook(self, media_id, webhook_key):
        """
        通过 Webhook 推送文件
        """
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={webhook_key}"
        payload = {
            "msgtype": "file",
            "file": {
                "media_id": media_id
            }
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as resp:
                    res_data = await resp.json()
                    if res_data.get("errcode") == 0:
                        logger.info(f"✅ Webhook 推送成功")
                        return True
                    else:
                        logger.error(f"❌ Webhook 推送失败: {res_data}")
                        return False
            except Exception as e:
                logger.error(f"❌ Webhook 推送异常: {e}")
                return False

    async def upload_media_via_token(self, file_path):
        """
        上传临时素材到应用 (使用 Token) 获取 media_id
        (手动构建 multipart 以解决中文乱码问题)
        """
        if not os.path.exists(file_path):
            logger.error(f"❌ 文件不存在: {file_path}")
            return None
            
        access_token = await self.token_mgr.get_token()
        if not access_token: return None

        url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={access_token}&type=file"
        filename = os.path.basename(file_path)
        
        # 使用时间戳生成简单的 boundary
        boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
        content_type = 'application/octet-stream'
        
        async def producer():
            # Part Header
            # 显式使用 UTF-8 编码 filename，不进行 RFC 5987 转义，兼容企微
            safe_filename = filename.replace('"', '\\"')
            header = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="media"; filename="{safe_filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            )
            yield header.encode('utf-8')
            
            # File Content
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    yield chunk
            
            # Footer
            footer = f"\r\n--{boundary}--\r\n"
            yield footer.encode('utf-8')

        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, data=producer(), headers=headers) as resp:
                    res_data = await resp.json()
                    if res_data.get("errcode") == 0:
                        media_id = res_data.get("media_id")
                        logger.info(f"✅ 应用素材上传成功, media_id: {media_id}")
                        return media_id
                    else:
                        logger.error(f"❌ 应用素材上传失败: {res_data}")
                        return None
            except Exception as e:
                logger.error(f"❌ 应用素材上传异常: {e}")
                return None

    async def send_file_via_token(self, to_user, media_id):
        """
        通过应用 Token 推送文件给用户
        """
        access_token = await self.token_mgr.get_token()
        if not access_token: return False
        
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
        payload = {
            "touser": to_user,
            "msgtype": "file",
            "agentid": self.agent_id, 
            "file": {
                "media_id": media_id
            },
            "safe": 0
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as resp:
                    res_data = await resp.json()
                    if res_data.get("errcode") == 0:
                        logger.info(f"✅ 应用消息推送成功")
                        return True
                    else:
                        logger.error(f"❌ 应用消息推送失败: {res_data}")
                        return False
            except Exception as e:
                logger.error(f"❌ 应用消息推送异常: {e}")
                return False
