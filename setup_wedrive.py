import asyncio
import aiohttp
import json
import os
import sys

# 配置文件路径
CONFIG_PATH = "data/config/wedrive_uploader.json"

def load_old_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

async def get_token(corpid, secret):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("errcode") == 0:
                return data.get("access_token")
            else:
                print(f"❌ 获取 Token 失败: {data}")
                return None

async def create_space(access_token, space_name):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/space_create?access_token={access_token}"
    # auth_info: type=2 (部门), departmentid=1 (根部门), auth=1 (下载) - 默认全员只读/下载?
    # 或者我们先不设默认权限，只设管理员
    payload = {
        "space_name": space_name,
        "auth_info": [
             # 默认给全公司(部门ID 1) 只读权限(auth=1: 仅下载, 2: 仅预览, 4: 上传/下载)
             # 根据官方文档，通常需要至少一个初始权限配置
             {
                 "type": 2, 
                 "departmentid": 1, 
                 "auth": 1 
             }
        ],
        "space_sub_type": 0
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("errcode") == 0:
                return data.get("spaceid")
            else:
                print(f"❌ 创建空间失败: {data}")
                return None

async def add_space_admin(access_token, space_id, userid):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/wedrive/space_acl_add?access_token={access_token}"
    # auth=7: 管理员权限 (预览/上传/下载/管理)
    payload = {
        "spaceid": space_id,
        "auth_info": [{
            "type": 1, # 成员
            "userid": userid,
            "auth": 7 
        }]
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("errcode") == 0:
                return True
            else:
                print(f"❌ 添加管理员失败: {data}")
                return False

def save_config(new_config, old_config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    
    # Backup
    if os.path.exists(CONFIG_PATH):
        backup_path = CONFIG_PATH + ".bak"
        import shutil
        try:
            shutil.copy(CONFIG_PATH, backup_path)
            print(f"📦 已备份旧配置至: {backup_path}")
        except Exception as e:
            print(f"⚠️ 备份失败: {e}")

    # Merge: update old config with new values
    final_config = old_config.copy()
    final_config.update(new_config)
    
    # Special handling for admins: merge lists if both exist
    if 'admins' in old_config and 'admins' in new_config:
        # Merge and deduplicate
        merged_admins = list(set(old_config['admins'] + new_config['admins']))
        final_config['admins'] = merged_admins
    elif 'admins' in old_config:
        final_config['admins'] = old_config['admins']
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(final_config, f, indent=4, ensure_ascii=False)
    print(f"✅ 配置文件已更新至: {CONFIG_PATH}")

async def main():
    print("=== AstrBot 微盘配置助手 ===")
    print("此脚本将帮助您创建微盘空间并生成配置文件。")
    print("说明：当前插件不支持同时运行两个配置。此脚本将更新现有配置，并自动备份旧文件。")
    print("--------------------------------")

    old_config = load_old_config()
    
    def prompt(msg, key):
        default = old_config.get(key, "")
        if default:
            val = input(f"{msg} [回车复用: {default}]: ").strip()
            return val if val else default
        else:
            return input(f"{msg}: ").strip()

    # 1. 获取基础凭证
    corpid = prompt("请输入企业ID (CorpID)", "corpid")
    if not corpid: return
    
    secret = prompt("请输入应用 Secret", "secret")
    if not secret: return

    print("\n🔄 正在获取 Access Token...")
    token = await get_token(corpid, secret)
    if not token: return
    print("✅ Token 获取成功！")

    # 2. 创建空间
    print("\n--------------------------------")
    create_new = input("是否创建新的微盘空间？(y/n) [默认为 y]: ").strip().lower()
    space_id = ""
    
    if create_new != 'n':
        space_name = input("请输入新空间名称 [默认: 骏芯智能微盘]: ").strip()
        if not space_name: space_name = "骏芯智能微盘"
        
        print(f"🔄 正在创建空间 '{space_name}'...")
        space_id = await create_space(token, space_name)
        
        if space_id:
            print(f"✅ 空间创建成功! SpaceID: {space_id}")
            
            # 3. 添加管理员
            userid = input("\n请输入您的企业微信账号 (UserID) 以添加管理员权限: ").strip()
            if userid:
                print(f"🔄 正在添加管理员 {userid}...")
                if await add_space_admin(token, space_id, userid):
                    print("✅ 管理员添加成功！请在企业微信微盘中查看。")
        else:
            return
    else:
        space_id = prompt("请输入 SpaceID", "space_id")

    if not space_id:
        print("❌ 未获取到 SpaceID，退出。")
        return

    # 4. 其他配置
    print("\n--------------------------------")
    default_agent = old_config.get("agent_id", 1000002)
    agent_id_in = input(f"请输入应用 AgentID [默认: {default_agent}]: ").strip()
    if not agent_id_in: agent_id = int(default_agent)
    else: agent_id = int(agent_id_in)

    webhook_key = prompt("请输入群机器人 Webhook Key (用于推送通知) [可选]", "webhook_key")

    # 5. 生成配置
    new_config = {
        "corpid": corpid,
        "secret": secret,
        "space_id": space_id,
        "agent_id": agent_id,
        "webhook_key": webhook_key,
        "admins": [] # New admins to be merged
    }
    
    # 如果刚才添加了管理员，加入列表待合并
    if 'userid' in locals() and userid:
        new_config['admins'].append(userid)

    save_config(new_config, old_config)
    print("\n🎉 配置完成！请重启 AstrBot 以生效。")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已取消。")
