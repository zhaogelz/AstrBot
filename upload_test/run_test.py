import asyncio
import aiohttp
import os
import json
import sys
import base64

# ==============================================================================
# ⚠️ 请在这里填入您的配置信息
# 您可以从 note1 文件或者 data/cmd_config.json 文件中找到这些值
# ==============================================================================
CORPID = "wwa9748681bdece041"
SECRET = "uZMI2VQluqGxhGIdRxdNZRH0MF_7foL2Cb5JuAc2gBk"
WEPAN_SPACE_ID = "s.wwa9748681bdece041.763567975WNL"

# 如果您想直接使用一个已有的 access_token，请将其粘贴到这里。
# 注意：access_token 有有效期（通常2小时），过期后需要更新。
# 如果留空 ("", ""), 脚本会自动获取新的 access_token。
HARDCODED_ACCESS_TOKEN = "VGzebE66rOz0qp5T_NwTizJDt1jBEVujzbZqWfNoekBmqY2Ko-Jz-TnRHkPgCLSqs4mM-oUSgkts7L13xPi3LViBSnzGFJ0WfyP_07QPeY-C_tufpvQoHyYN8KK8IVldq2mf00wQmZqgIumMgichoaNhP8tdukjR8xaxjTTcD_uoaAY6EjNLgxV0RGAYpo9A5o2mKh1Zbl3sWDkyqUCmFQ"
# ==============================================================================
# ⚠️ 注意：此脚本会上传下面这个文件，请确保它存在
# ==============================================================================
FILE_TO_UPLOAD = "test.txt"
# ==============================================================================


async def _get_access_token(corpid: str, secret: str) -> str | None:
    """获取企业微信 access_token"""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
    print(f"🔄 正在从 {url[:50]}... 获取 access_token...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("access_token"):
                        print(f"✅ 成功获取 access_token！")
                        return data["access_token"]
                    else:
                        print(f"❌ 获取 access_token 失败: {data.get('errmsg', '未知错误')}")
                        return None
                else:
                    print(f"❌ 获取 access_token 请求失败，HTTP状态码: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ 网络请求异常: {e}")
            return None

async def main():
    """主测试函数"""
    
    # --- 1. 检查测试文件 ---
    script_dir = os.path.dirname(os.path.abspath(__file__))
    target_file_path = os.path.join(script_dir, FILE_TO_UPLOAD)

    if not os.path.exists(target_file_path):
        print(f"❌ 错误：测试文件未找到！")
        print(f"   请确保 '{FILE_TO_UPLOAD}' 文件与此脚本位于同一目录下。")
        return

    try:
        file_size = os.path.getsize(target_file_path)
    except FileNotFoundError:
        print(f"❌ 错误：文件 '{target_file_path}' 未找到！")
        return

    print(f"📂 找到测试文件：")
    print(f"   - 路径: {target_file_path}")
    print(f"   - 大小: {file_size} bytes\n")

    # --- 2. 读取文件内容并进行 Base64 编码 ---
    file_base64_content = ""
    try:
        with open(target_file_path, "rb") as f:
            file_content = f.read()
            # 检查文件大小，如果超过10MB，则提示
            if len(file_content) > 10 * 1024 * 1024:
                print(f"❌ 错误：文件大小 ({len(file_content) / (1024*1024):.2f}MB) 超过10MB上限，无法使用Base64上传。")
                print("   请更换小于10MB的文件，或使用分块上传接口。")
                return
            file_base64_content = base64.b64encode(file_content).decode("utf-8")
        print("✅ 文件内容已成功读取并进行 Base64 编码。\n")
    except Exception as e:
        print(f"❌ 错误：读取文件或Base64编码失败: {e}")
        return

    # --- 3. 获取 Access Token ---
    access_token = None
    if HARDCODED_ACCESS_TOKEN:
        access_token = HARDCODED_ACCESS_TOKEN
        print("✅ 使用硬编码的 access_token 进行测试。")
        print("⚠️ 请注意：硬编码的 access_token 有有效期，过期后测试可能失败。")
    else:
        access_token = await _get_access_token(CORPID, SECRET)
        if not access_token:
            print("\n❌ 无法获取 access_token，测试终止。")
            return


    # --- 4. 请求上传文件 ---
    upload_file_url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/file_upload?access_token={access_token}"
    
    # ❗️ 使用您的真实 userid
    # 您之前提到需要以 "LiZhen" 的身份上传，我们在这里直接使用
    payload = {
        "spaceid": WEPAN_SPACE_ID,
        "fatherid": WEPAN_SPACE_ID,
        "file_name": os.path.basename(target_file_path),
        "file_base64_content": file_base64_content, # 直接上传Base64内容
    }

    print(f"\n📡 正在请求上传文件...")
    print(f"   - API: {upload_file_url.split('?')[0]}")
    print(f"   - Payload (省略base64内容): {json.dumps({k:v for k,v in payload.items() if k!='file_base64_content'}, ensure_ascii=False, indent=2)}")

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(upload_file_url, json=payload) as response:
                print(f"\n✨ 企业微信服务器响应:")
                print(f"   - HTTP 状态码: {response.status}")
                response_text = await response.text()
                
                if response.status == 200:
                    data = json.loads(response_text)
                    if data.get("errcode") == 0:
                        print("   - ✅ 文件已成功上传！")
                        print(f"   - API 响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
                        print("\n🎉 诊断成功！这表明您的权限和IP白名单已配置正确。")
                        print("   现在您可以让 AstrBot 主程序重新尝试了。")
                    else:
                        print(f"   - ❌ 文件上传失败: {data.get('errmsg')} (错误码: {data.get('errcode')})")
                        print("\n   - 💡 诊断信息：请根据错误码和信息，检查以下可能原因：")
                        print("     - '微盘' 应用权限是否已为该应用开启？")
                        print("     - 服务器的公网IP是否已加入到应用的可信IP列表中？")
                        print("     - 您指定的 userid ('LiZhen') 是否有权限在此空间上传文件？")
                        print("     - 您使用的 'spaceid' 是否是应用自己的专属文件夹 ID？")
                        print("     - 文件内容或 Base64 编码是否有问题？")
                else:
                    print(f"   - ❌ 请求失败，原始响应: {response_text}")
                    print("\n   - 💡 诊断信息：这几乎可以肯定是因为企业微信后台配置问题。请重点检查：")
                    print("     1. '微盘'权限是否已为该应用开启？")
                    print("     2. 服务器的公网IP是否已加入到应用的可信IP列表中？")
        except Exception as e:
            print(f"❌ 执行测试时发生网络或其他异常: {e}")


if __name__ == "__main__":
    # 兼容 Windows 平台的 asyncio 运行策略
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"发生未处理的错误: {e}")
