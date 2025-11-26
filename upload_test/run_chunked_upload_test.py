import asyncio
import aiohttp
import os
import sys
import base64
import struct
import time

# ==============================================================================
# ⚠️ 用户配置区
# ==============================================================================
CORPID = "wwa9748681bdece041"
SECRET = "uZMI2VQluqGxhGIdRxdNZRH0MF_7foL2Cb5JuAc2gBk"
WEPAN_SPACE_ID = "s.wwa9748681bdece041.763567975WNL"

# 文件路径
FILE_TO_UPLOAD = "2.docx"

# Access Token (如有需要请填入，否则设为 None)
HARDCODED_ACCESS_TOKEN = "jlEY2fX7ewg8aAuv5-W-PC_4wiDAcxI6ulnAg01-hqIHWbcqhc-KVhMouJ4Cr8iFJGmyC76OtFDkYC3OpWNsvsCHwrccXuHJiMIzh6813WkSSLrKu8XEk4AoJaZxsacz0cooEIrgdiOat-DQQVLGRMWqCqXxanqUv0atsdYmaacDPyoQkl7csH7XRrmK4vpRDUbfuIcFDi3u5_943mAtHw"

# 固定分块大小 2MB
CHUNK_SIZE = 2 * 1024 * 1024 
# 并发上传数量 (建议 3-5，过高可能触发频率限制或内存溢出)
MAX_CONCURRENT_UPLOADS = 2
# ==============================================================================

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


async def _get_access_token(corpid, secret):
    if HARDCODED_ACCESS_TOKEN:
        return HARDCODED_ACCESS_TOKEN
    # 这里省略自动获取逻辑
    return None

def calculate_block_shas(file_path):
    """
    计算文件分块 SHA。此函数为 CPU 密集型。
    """
    print(f"🧮 正在计算 SHA (纯Python实现，大文件请耐心等待)...")
    
    if not os.path.exists(file_path):
        print(f"❌ 文件不存在")
        return None, 0

    file_size = os.path.getsize(file_path)
    block_shas = []
    sha1 = SafeSHA1()
    
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    last_print_time = 0
    
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
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
            
            # 优化：每0.5秒刷新一次进度，避免频繁IO
            current_time = time.time()
            if current_time - last_print_time > 0.5 or is_last:
                progress = len(block_shas)
                sys.stdout.write(f"\r   - 进度: {progress}/{total_chunks} ({(progress/total_chunks)*100:.1f}%)")
                sys.stdout.flush()
                last_print_time = current_time
            
    print(f"\n✅ 计算完成")
    return block_shas, file_size

async def upload_part_task(session, access_token, upload_key, index, chunk_data, sem):
    """
    单个分块上传任务，受信号量 sem 控制并发数
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_part?access_token={access_token}"
    
    # 转换为 Base64 (注意：这会增加内存消耗，并发数不宜过大)
    b64_content = base64.b64encode(chunk_data).decode('utf-8')
    payload = {
        "upload_key": upload_key,
        "index": index,
        "file_base64_content": b64_content
    }
    
    async with sem: # 获取并发锁
        for retry in range(3):
            try:
                # 使用 post，并在出错时打印
                async with session.post(url, json=payload, timeout=120) as response:
                    res_data = await response.json()
                    if res_data.get("errcode") == 0:
                        print(f"   ⬆️ 分块 {index} 上传成功")
                        return True
                    else:
                        print(f"   ⚠️ 分块 {index} 失败 (Retrying): {res_data}")
            except Exception as e:
                print(f"   ⚠️ 分块 {index} 网络异常: {e}")
                await asyncio.sleep(1)
                
        print(f"   ❌ 分块 {index} 最终失败")
        return False

async def main():
    # 1. 准备 Token
    access_token = await _get_access_token(CORPID, SECRET)
    if not access_token: 
        print("❌ 无法获取 Access Token")
        return

    # 2. 计算 SHA (在单独线程中运行，不阻塞事件循环)
    block_shas, file_size = await asyncio.to_thread(calculate_block_shas, FILE_TO_UPLOAD)
    if not block_shas: return

    async with aiohttp.ClientSession() as session:
        # 3. 初始化上传
        print(f"\n📡 [1/3] 初始化上传...")
        init_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_init?access_token={access_token}"
        init_payload = {
            "spaceid": WEPAN_SPACE_ID,
            "fatherid": WEPAN_SPACE_ID,
            "file_name": os.path.basename(FILE_TO_UPLOAD),
            "size": file_size,
            "block_sha": block_shas,
            "skip_push_card": False
        }
        
        async with session.post(init_url, json=init_payload) as resp:
            init_res = await resp.json()
        
        if init_res.get("errcode") != 0:
            print(f"❌ 初始化失败: {init_res}")
            return
        
        if init_res.get("hit_exist"):
            print(f"🎉 秒传成功! FileID: {init_res.get('fileid')}")
            return

        upload_key = init_res["upload_key"]
        print(f"✅ 初始化成功, Key: {upload_key[:10]}...")

        # 4. 并发上传分块
        print(f"\n📡 [2/3] 正在并发上传 (并发数: {MAX_CONCURRENT_UPLOADS})...")
        
        # 信号量控制并发数
        sem = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
        pending_tasks = set()
        
        with open(FILE_TO_UPLOAD, "rb") as f:
            index = 1
            while True:
                chunk_data = f.read(CHUNK_SIZE)
                if not chunk_data: break
                
                # 创建上传任务
                task = asyncio.create_task(
                    upload_part_task(session, access_token, upload_key, index, chunk_data, sem)
                )
                pending_tasks.add(task)
                
                # 内存保护机制：
                # 如果积压的任务超过并发数，等待其中一个完成再继续读取文件
                # 这样可以防止读取整个大文件到内存中
                if len(pending_tasks) >= MAX_CONCURRENT_UPLOADS:
                    done, pending_tasks = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                    # 检查已完成的任务是否有失败的 (这里简单处理，实际生产中可能需要终止)
                    for d in done:
                        if not d.result():
                            print("❌ 检测到分块上传失败，停止上传")
                            return

                index += 1
        
        # 等待剩余任务完成
        if pending_tasks:
            await asyncio.wait(pending_tasks)

        # 5. 完成合并
        print(f"\n📡 [3/3] 合并文件...")
        finish_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_finish?access_token={access_token}"
        async with session.post(finish_url, json={"upload_key": upload_key}) as resp:
            finish_res = await resp.json()
            if finish_res.get("errcode") == 0:
                print(f"✨ 上传完毕! FileID: {finish_res.get('fileid')}")
            else:
                print(f"❌ 合并失败: {finish_res}")

if __name__ == "__main__":
    # Windows下aiohttp需要的策略设置
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        start_time = time.time()
        asyncio.run(main())
        print(f"\n⏱️ 总耗时: {time.time() - start_time:.2f}秒")
    except KeyboardInterrupt:
        print("\n🚫 用户取消")