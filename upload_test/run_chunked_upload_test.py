import asyncio
import aiohttp
import os
import json
import sys
import base64
import struct
import hashlib

# ==============================================================================
# ⚠️ 用户配置区
# ==============================================================================
CORPID = "wwa9748681bdece041"
SECRET = "uZMI2VQluqGxhGIdRxdNZRH0MF_7foL2Cb5JuAc2gBk"
WEPAN_SPACE_ID = "s.wwa9748681bdece041.763567975WNL"

# 文件路径
FILE_TO_UPLOAD = "2.pdf"

# Access Token
HARDCODED_ACCESS_TOKEN = "VGzebE66rOz0qp5T_NwTizJDt1jBEVujzbZqWfNoekBmqY2Ko-Jz-TnRHkPgCLSqs4mM-oUSgkts7L13xPi3LViBSnzGFJ0WfyP_07QPeY-C_tufpvQoHyYN8KK8IVldq2mf00wQmZqgIumMgichoaNhP8tdukjR8xaxjTTcD_uoaAY6EjNLgxV0RGAYpo9A5o2mKh1Zbl3sWDkyqUCmFQ"

# 固定分块大小 2MB
CHUNK_SIZE = 2 * 1024 * 1024 
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
        🔥 [修改点] 获取中间状态。
        尝试使用 Little Endian (小端序) 输出，模拟 C++ 内存 Dump。
        """
        # 将每个 32位 整数按小端序 ('<I') 打包为 bytes，再转 hex
        return b''.join(struct.pack('<I', x) for x in self._h).hex()

    def final_hex(self):
        """
        获取最终 Digest (含 Padding)。
        最终结果通常标准都一致 (Big Endian)。
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
            
        # 最终 Digest 标准是 Big Endian
        return '{:08x}{:08x}{:08x}{:08x}{:08x}'.format(*temp_runner._h)


async def _get_access_token(corpid, secret):
    if HARDCODED_ACCESS_TOKEN:
        return HARDCODED_ACCESS_TOKEN
    # (省略自动获取)
    return None

def calculate_block_shas(file_path):
    print(f"🧮 正在计算 SHA (尝试 Little-Endian State)...")
    
    if not os.path.exists(file_path):
        print(f"❌ 文件不存在")
        return None, 0

    file_size = os.path.getsize(file_path)
    block_shas = []
    sha1 = SafeSHA1()
    
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            
            sha1.update(chunk)
            
            # 判断最后一块
            is_last = (f.tell() == file_size)
            
            if is_last:
                # 最后一块是完整 SHA1，通常是标准的大端序
                digest = sha1.final_hex()
                block_shas.append(digest)
            else:
                # 🔥 中间块：使用小端序 State
                state = sha1.get_state_hex()
                block_shas.append(state)
            
            sys.stdout.write(f"\r   - 进度: {len(block_shas)}/{total_chunks} (Current: {block_shas[-1][:8]}...)")
            sys.stdout.flush()
            
    print(f"\n✅ 计算完成")
    return block_shas, file_size

async def upload_part(session, access_token, upload_key, index, chunk_data):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_part?access_token={access_token}"
    b64_content = base64.b64encode(chunk_data).decode('utf-8')
    payload = {
        "upload_key": upload_key,
        "index": index,
        "file_base64_content": b64_content
    }
    
    for _ in range(3): # 重试3次
        try:
            async with session.post(url, json=payload, timeout=60) as response:
                return await response.json()
        except Exception:
            await asyncio.sleep(1)
            continue
    return {"errcode": -1, "errmsg": "Network Error"}

async def main():
    # 1. 准备
    access_token = await _get_access_token(CORPID, SECRET)
    if not access_token: return

    # 2. 计算 SHA
    block_shas, file_size = await asyncio.to_thread(calculate_block_shas, FILE_TO_UPLOAD)
    if not block_shas: return

    async with aiohttp.ClientSession() as session:
        # 3. Init
        print(f"\n📡 [1/3] 初始化...")
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
        print(f"✅ 初始化成功")

        # 4. Upload
        print(f"\n📡 [2/3] 上传分块...")
        with open(FILE_TO_UPLOAD, "rb") as f:
            index = 1
            while True:
                chunk_data = f.read(CHUNK_SIZE)
                if not chunk_data: break
                
                print(f"   ⬆️  分块 {index}...", end="", flush=True)
                res = await upload_part(session, access_token, upload_key, index, chunk_data)
                
                if res.get("errcode") == 0:
                    print(" ✅")
                else:
                    print(f" ❌ {res}")
                    return
                index += 1

        # 5. Finish
        print(f"\n📡 [3/3] 合并文件...")
        finish_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload_finish?access_token={access_token}"
        async with session.post(finish_url, json={"upload_key": upload_key}) as resp:
            print(f"✨ 结果: {await resp.json()}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass