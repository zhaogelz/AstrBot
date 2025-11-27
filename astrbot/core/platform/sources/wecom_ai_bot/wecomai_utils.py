"""企业微信智能机器人工具模块
提供常量定义、工具函数和辅助方法
"""

import asyncio
import base64
import hashlib
import os
import secrets
import string
import mimetypes
import re
from typing import Any

import aiohttp
from Crypto.Cipher import AES

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


# 常量定义
class WecomAIBotConstants:
    """企业微信智能机器人常量"""

    # 消息类型
    MSG_TYPE_TEXT = "text"
    MSG_TYPE_IMAGE = "image"
    MSG_TYPE_FILE = "file"
    MSG_TYPE_MIXED = "mixed"
    MSG_TYPE_STREAM = "stream"
    MSG_TYPE_EVENT = "event"

    # 流消息状态
    STREAM_CONTINUE = False
    STREAM_FINISH = True

    # 错误码
    SUCCESS = 0
    DECRYPT_ERROR = -40001
    VALIDATE_SIGNATURE_ERROR = -40002
    PARSE_XML_ERROR = -40003
    COMPUTE_SIGNATURE_ERROR = -40004
    ILLEGAL_AES_KEY = -40005
    VALIDATE_APPID_ERROR = -40006
    ENCRYPT_AES_ERROR = -40007
    ILLEGAL_BUFFER = -40008


def generate_random_string(length: int = 10) -> str:
    """生成随机字符串

    Args:
        length: 字符串长度，默认为 10

    Returns:
        随机字符串

    """
    letters = string.ascii_letters + string.digits
    return "".join(secrets.choice(letters) for _ in range(length))


def calculate_image_md5(image_data: bytes) -> str:
    """计算图片数据的 MD5 值

    Args:
        image_data: 图片二进制数据

    Returns:
        MD5 哈希值（十六进制字符串）

    """
    return hashlib.md5(image_data).hexdigest()


def encode_image_base64(image_data: bytes) -> str:
    """将图片数据编码为 Base64

    Args:
        image_data: 图片二进制数据

    Returns:
        Base64 编码的字符串

    """
    return base64.b64encode(image_data).decode("utf-8")


def format_session_id(session_type: str, session_id: str) -> str:
    """格式化会话 ID

    Args:
        session_type: 会话类型 ("user", "group")
        session_id: 原始会话 ID

    Returns:
        格式化后的会话 ID

    """
    return f"wecom_ai_bot_{session_type}_{session_id}"


def parse_session_id(formatted_session_id: str) -> tuple[str, str]:
    """解析格式化的会话 ID

    Args:
        formatted_session_id: 格式化的会话 ID

    Returns:
        (会话类型, 原始会话ID)

    """
    parts = formatted_session_id.split("_", 3)
    if (
        len(parts) >= 4
        and parts[0] == "wecom"
        and parts[1] == "ai"
        and parts[2] == "bot"
    ):
        return parts[3], "_".join(parts[4:]) if len(parts) > 4 else ""
    return "user", formatted_session_id


def safe_json_loads(json_str: str, default: Any = None) -> Any:
    """安全地解析 JSON 字符串

    Args:
        json_str: JSON 字符串
        default: 解析失败时的默认值

    Returns:
        解析结果或默认值

    """
    import json

    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"JSON 解析失败: {e}, 原始字符串: {json_str}")
        return default


def format_error_response(error_code: int, error_msg: str) -> str:
    """格式化错误响应

    Args:
        error_code: 错误码
        error_msg: 错误信息

    Returns:
        格式化的错误响应字符串

    """
    return f"Error {error_code}: {error_msg}"


async def process_encrypted_image(
    image_url: str,
    aes_key_base64: str,
) -> tuple[bool, str]:
    """下载并解密加密图片

    Args:
        image_url: 加密图片的URL
        aes_key_base64: Base64编码的AES密钥(与回调加解密相同)

    Returns:
        Tuple[bool, str]: status 为 True 时 data 是解密后的图片数据的 base64 编码，
            status 为 False 时 data 是错误信息

    """
    # 1. 下载加密图片
    logger.info("开始下载加密图片: %s", image_url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url, timeout=15) as response:
                response.raise_for_status()
                encrypted_data = await response.read()
        logger.info("图片下载成功，大小: %d 字节", len(encrypted_data))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        error_msg = f"下载图片失败: {e!s}"
        logger.error(error_msg)
        return False, error_msg

    # 2. 准备AES密钥和IV
    if not aes_key_base64:
        raise ValueError("AES密钥不能为空")

    # Base64解码密钥 (自动处理填充)
    aes_key = base64.b64decode(aes_key_base64 + "=" * (-len(aes_key_base64) % 4))
    if len(aes_key) != 32:
        raise ValueError("无效的AES密钥长度: 应为32字节")

    iv = aes_key[:16]  # 初始向量为密钥前16字节

    # 3. 解密图片数据
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted_data = cipher.decrypt(encrypted_data)

    # 4. 去除PKCS#7填充 (Python 3兼容写法)
    pad_len = decrypted_data[-1]  # 直接获取最后一个字节的整数值
    if pad_len > 32:  # AES-256块大小为32字节
        raise ValueError("无效的填充长度 (大于32字节)")

    decrypted_data = decrypted_data[:-pad_len]
    logger.info("图片解密成功，解密后大小: %d 字节", len(decrypted_data))

    # 5. 转换为base64编码
    base64_data = base64.b64encode(decrypted_data).decode("utf-8")
    logger.info("图片已转换为base64编码，编码后长度: %d", len(base64_data))

    return True, base64_data


def _extract_filename_from_cos_meta(b64_str: str) -> str | None:
    """从 x-cos-meta-attr 中提取文件名 (Protobuf field 1)"""
    try:
        data = base64.b64decode(b64_str)
        idx = 0
        while idx < len(data):
            if idx >= len(data): break
            tag = data[idx]
            idx += 1
            
            field_num = tag >> 3
            wire_type = tag & 0x7
            
            # 我们只关心 Length Delimited (Type 2) 的 Field 1
            if wire_type != 2: 
                return None # 简单处理，遇到非字符串类型就停止，假设Field 1在最前
                
            # Read varint length
            length = 0
            shift = 0
            while True:
                if idx >= len(data): return None
                b = data[idx]
                idx += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            
            if idx + length > len(data): return None
            value = data[idx : idx + length]
            idx += length
            
            if field_num == 1:
                try:
                    return value.decode('utf-8')
                except:
                    return None
    except Exception:
        pass
    return None


async def download_and_decrypt_file(
    file_url: str,
    aes_key_base64: str,
    file_name: str = None,
) -> tuple[bool, str]:
    """下载并解密文件，保存到本地

    Args:
        file_url: 加密文件的URL
        aes_key_base64: Base64编码的AES密钥
        file_name: 文件名，如果为None则尝试从响应头获取或生成随机文件名

    Returns:
        Tuple[bool, str]: status 为 True 时 data 是文件绝对路径，
            status 为 False 时 data 是错误信息
    """
    # 1. 下载加密文件
    logger.info("开始下载加密文件: %s", file_url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, timeout=60) as response: # 文件可能较大，增加超时
                response.raise_for_status()
                
                # 尝试从 Header 获取文件名 (如果未提供)
                if not file_name:
                    # Debug: 打印所有 Header 以便排查
                    logger.info(f"[WeComAI DEBUG] Response Headers: {response.headers}")
                    
                    # 0. Try x-cos-meta-attr (Protobuf encoded filename)
                    cos_meta = response.headers.get("x-cos-meta-attr")
                    if cos_meta:
                        file_name = _extract_filename_from_cos_meta(cos_meta)
                        if file_name:
                             logger.info(f"[WeComAI] Extracted filename from x-cos-meta-attr: {file_name}")

                    # 1. Try Content-Disposition
                    if not file_name:
                        content_disposition = response.headers.get("Content-Disposition")
                        if content_disposition:
                            # 优化正则：非贪婪匹配，支持不带引号，优先匹配 filename*
                            # 1. filename*=utf-8''xxx.txt
                            # 2. filename="xxx.txt"
                            # 3. filename=xxx.txt
                            patterns = [
                                r'filename\*=utf-8\'\'(.+?)(?:;|$)',
                                r'filename="([^"]+)"',
                                r'filename=([^; \n]+)'
                            ]
                            
                            for pattern in patterns:
                                match = re.search(pattern, content_disposition, re.IGNORECASE)
                                if match:
                                    from urllib.parse import unquote
                                    file_name = unquote(match.group(1))
                                    break
                    
                    logger.info(f"[WeComAI] Final filename: {file_name}")
                    
                    # 2. Try Content-Type
                    if not file_name:
                        content_type = response.headers.get("Content-Type")
                        if content_type:
                            ext = mimetypes.guess_extension(content_type)
                            if ext:
                                file_name = f"{secrets.token_hex(8)}{ext}"

                encrypted_data = await response.read()
        logger.info("文件下载成功，大小: %d 字节", len(encrypted_data))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        error_msg = f"下载文件失败: {e!s}"
        logger.error(error_msg)
        return False, error_msg

    # 2. 准备AES密钥和IV
    if not aes_key_base64:
        return False, "AES密钥不能为空"

    try:
        aes_key = base64.b64decode(aes_key_base64 + "=" * (-len(aes_key_base64) % 4))
        if len(aes_key) != 32:
            return False, "无效的AES密钥长度: 应为32字节"

        iv = aes_key[:16]

        # 3. 解密数据
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        decrypted_data = cipher.decrypt(encrypted_data)

        # 4. 去除PKCS#7填充
        pad_len = decrypted_data[-1]
        if pad_len > 32:
            return False, "无效的填充长度 (大于32字节)"

        decrypted_data = decrypted_data[:-pad_len]
        logger.info("文件解密成功，解密后大小: %d 字节", len(decrypted_data))
        
        # 5. 保存到文件
        temp_dir = os.path.join(get_astrbot_data_path(), "temp")
        os.makedirs(temp_dir, exist_ok=True)
        
        if not file_name:
            file_name = f"{secrets.token_hex(8)}.bin"
            
        file_path = os.path.join(temp_dir, file_name)
        
        with open(file_path, "wb") as f:
            f.write(decrypted_data)
            
        return True, os.path.abspath(file_path)

    except Exception as e:
        error_msg = f"解密或保存文件失败: {e!s}"
        logger.error(error_msg)
        return False, error_msg
