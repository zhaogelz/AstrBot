import json
import os
import aiohttp
import logging
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context
from astrbot.api.message_components import File, Image, Video
from astrbot.core import file_token_service, astrbot_config
from astrbot.core.utils.io import get_local_ip_addresses
from astrbot.core.platform import MessageType
from .token_manager import TokenManager
from .uploader import WeDriveUploader

logger = logging.getLogger("astrbot")

class WeDriveUploaderPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = self._load_config()
        
        if not self.config:
            logger.warning("[WeDriveUploader] 未配置 corpid/secret，插件无法工作。请修改 data/config/wedrive_uploader.json")
            self.uploader = None
        else:
            self.token_mgr = TokenManager(
                corpid=self.config['corpid'],
                secret=self.config['secret'],
                hardcoded_token=self.config.get('debug_token'),
                save_token_callback=self._save_token
            )
            self.uploader = WeDriveUploader(
                token_mgr=self.token_mgr,
                space_id=self.config['space_id'],
                agent_id=self.config.get('agent_id', 1000002)
            )

    def _save_token(self, token):
        """保存 Token 到配置文件"""
        if self.config:
            self.config['debug_token'] = token
            config_path = os.path.join("data/config", "wedrive_uploader.json")
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                logger.info(f"[WeDriveUploader] Token 已更新并保存到配置文件")
            except Exception as e:
                logger.error(f"[WeDriveUploader] 保存配置文件失败: {e}")

    def _load_config(self):
        """加载或创建配置文件"""
        config_dir = "data/config"
        config_path = os.path.join(config_dir, "wedrive_uploader.json")
        
        if not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)
            
        if not os.path.exists(config_path):
            default_config = {
                "corpid": "",
                "secret": "",
                "space_id": "",
                "agent_id": 1000002,
                "webhook_key": "25994ab1-6b0b-4059-a47b-eebf5bd20e19"
            }
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=4, ensure_ascii=False)
            logger.info(f"[WeDriveUploader] 配置文件已生成: {config_path}，请填写后重启 AstrBot")
            return None
            
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                if not all([config.get('corpid'), config.get('secret'), config.get('space_id')]):
                    return None
                return config
        except Exception as e:
            logger.error(f"[WeDriveUploader] 读取配置文件失败: {e}")
            return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，筛选文件进行上传"""
        if not self.uploader:
            return

        # 预处理消息：移除可能的 At 前缀
        # event.message_str 可能会包含 "@机器人名称 "
        message_str = event.message_str.strip()
        
        # 简单的去 At 处理：如果消息以 @ 开头，尝试找到第一个空格并截断
        # 更加鲁棒的方式是遍历 message components，但这需要更多代码。
        # 这里采用简单策略：如果包含 "下微盘" 等指令，直接提取指令及之后的部分
        
        cmd_map = {
            "查微盘": "查微盘",
            "搜微盘": "搜微盘",
            "删微盘": "删微盘",
            "下微盘": "下微盘",
            "帮助": "帮助"
        }
        
        target_cmd = None
        clean_msg = message_str
        
        for cmd in cmd_map:
            if cmd in message_str:
                # 找到指令起始位置
                idx = message_str.find(cmd)
                # 确保指令前是空格或开头 (避免匹配到 "上下微盘")
                if idx == 0 or message_str[idx-1].isspace() or message_str[idx-1] == ']': # ] for [At:xxx]
                    target_cmd = cmd
                    clean_msg = message_str[idx:]
                    break
        
        if not target_cmd:
            # 如果没匹配到指令，再检查是否是普通文件上传消息
            pass
        else:
            message_str = clean_msg

        # 0. 处理 "帮助" 指令
        if message_str == "帮助":
            help_text = (
                "微盘助手指令说明：\n\n"
                "查微盘：列出微盘中所有的文件\n\n"
                "    查微盘\n\n"
                "搜微盘 <关键字>，例如：\n\n"
                "    搜微盘 es\n\n"
                "删微盘 <准确文件名>，例如：\n\n"
                "    删微盘 test.txt\n\n"
                "下微盘 <准确文件名>，例如：\n\n"
                "    下微盘 test.txt"
            )
            yield event.plain_result(help_text)
            event.stop_event()
            return

        # 1. 处理 "查微盘" 指令
        if message_str == "查微盘":
            logger.info(f"[WeDriveUploader] 收到查微盘指令")
            yield event.plain_result(f"📂 正在获取微盘文件列表...")
            
            files = await self.uploader.list_files()
            if files is None:
                 yield event.plain_result(f"❌ 获取文件列表失败，请检查日志。")
            else:
                # Extract list from response structure {'item': [...]}
                file_list = files.get('item', []) if isinstance(files, dict) else files
                if not isinstance(file_list, list):
                    file_list = []

                if not file_list:
                     yield event.plain_result(f"📂 微盘目录为空。")
                else:
                    # 格式化输出
                    msg = f"📂 微盘文件列表 (共{len(file_list)}个):\n"
                    for f in file_list:
                        if isinstance(f, str):
                             name = f"FileID: {f}"
                             size_str = "未知大小"
                        else:
                            name = f.get("file_name", "未知文件")
                            size = int(f.get("file_size", 0))
                            # 简单的大小转换
                            if size < 1024:
                                size_str = f"{size}B"
                            elif size < 1024 * 1024:
                                size_str = f"{size/1024:.1f}KB"
                            else:
                                size_str = f"{size/1024/1024:.1f}MB"
                        msg += f"- {name} ({size_str})\n"
                    yield event.plain_result(msg)
            
            # 停止事件传播，防止 AI 回复
            event.stop_event()
            return

        # 2. 处理 "搜微盘" 指令
        if message_str.startswith("搜微盘"):
            keyword = message_str[3:].strip()
            if not keyword:
                yield event.plain_result("⚠️ 请输入要搜索的文件名，例如：搜微盘 报告")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 搜索文件: {keyword}")
            yield event.plain_result(f"🔍 正在搜索包含 '{keyword}' 的文件...")

            files = await self.uploader.list_files()
            if files is None:
                 yield event.plain_result(f"❌ 获取文件列表失败，请检查日志。")
            else:
                # Extract list
                file_list = files.get('item', []) if isinstance(files, dict) else files
                if not isinstance(file_list, list):
                    file_list = []
                
                matched = [f for f in file_list if isinstance(f, dict) and keyword in f.get("file_name", "")]
                
                if not matched:
                     yield event.plain_result(f"📂 未找到包含 '{keyword}' 的文件。")
                else:
                    msg = f"🔍 搜索结果 (共{len(matched)}个):\n"
                    for f in matched:
                        name = f.get("file_name", "未知文件")
                        size = int(f.get("file_size", 0))
                        if size < 1024:
                            size_str = f"{size}B"
                        elif size < 1024 * 1024:
                            size_str = f"{size/1024:.1f}KB"
                        else:
                            size_str = f"{size/1024/1024:.1f}MB"
                        msg += f"- {name} ({size_str})\n"
                    yield event.plain_result(msg)
            
            event.stop_event()
            return

        # 3. 处理 "删微盘" 指令
        if message_str.startswith("删微盘"):
            filename = message_str[3:].strip()
            if not filename:
                yield event.plain_result("⚠️ 请输入要删除的准确文件名，例如：删微盘 test.txt")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 尝试删除文件: {filename}")
            yield event.plain_result(f"🗑️ 正在查找并删除 '{filename}' ...")

            files = await self.uploader.list_files()
            if files is None:
                 yield event.plain_result(f"❌ 获取文件列表失败，无法删除。")
            else:
                # Extract list
                file_list = files.get('item', []) if isinstance(files, dict) else files
                if not isinstance(file_list, list):
                    file_list = []
                
                # Find exact match
                target_file = None
                for f in file_list:
                    if isinstance(f, dict) and f.get("file_name") == filename:
                        target_file = f
                        break
                
                if not target_file:
                     yield event.plain_result(f"❌ 未找到名为 '{filename}' 的文件。请确认文件名是否完全准确。")
                else:
                    file_id = target_file.get("fileid")
                    if await self.uploader.delete_file(file_id):
                        yield event.plain_result(f"✅ 文件 '{filename}' 已删除。")
                    else:
                        yield event.plain_result(f"❌ 删除失败，请检查日志。")
            
            event.stop_event()
            return

        # 4. 处理 "下微盘" 指令
        if message_str.startswith("下微盘"):
            filename = message_str[3:].strip()
            if not filename:
                yield event.plain_result("⚠️ 请输入要下载的准确文件名，例如：下微盘 test.txt")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 尝试下载文件: {filename}")
            yield event.plain_result(f"🔍 正在查找文件 '{filename}' ...")

            files = await self.uploader.list_files()
            if files is None:
                 yield event.plain_result(f"❌ 获取文件列表失败，无法下载。")
            else:
                # Extract list
                file_list = files.get('item', []) if isinstance(files, dict) else files
                if not isinstance(file_list, list):
                    file_list = []
                
                # Find exact match
                target_file = None
                for f in file_list:
                    if isinstance(f, dict) and f.get("file_name") == filename:
                        target_file = f
                        break
                
                if not target_file:
                     yield event.plain_result(f"❌ 未找到名为 '{filename}' 的文件。请确认文件名是否完全准确。")
                else:
                    file_id = target_file.get("fileid")
                    yield event.plain_result(f"📥 正在下载 '{filename}' 并推送...")
                    
                    local_path = await self.uploader.download_file_to_local(file_id, filename)
                    
                    if local_path:
                        try:
                            # 判断是私聊还是群聊
                            is_group = (hasattr(event.message_obj, 'group_id') and event.message_obj.group_id) or (event.message_obj.type == MessageType.GROUP_MESSAGE)
                            
                            if is_group:
                                # 群聊：走 Webhook 推送
                                webhook_key = self.config.get("webhook_key", "25994ab1-6b0b-4059-a47b-eebf5bd20e19")
                                media_id = await self.uploader.upload_to_webhook(local_path, webhook_key)
                                
                                if media_id:
                                    success = await self.uploader.push_file_via_webhook(media_id, webhook_key)
                                    if success:
                                        yield event.plain_result(f"✅ 文件 '{filename}' 已通过 Webhook 推送到群。")
                                    else:
                                        yield event.plain_result(f"❌ Webhook 推送失败，请检查日志。")
                                else:
                                    yield event.plain_result(f"❌ 上传到 Webhook 失败，请检查日志。")
                            else:
                                # 私聊：走应用消息推送
                                # 获取发送者 UserID
                                to_user = event.message_obj.sender.user_id
                                if not to_user:
                                     yield event.plain_result(f"❌ 无法获取您的 UserID，无法推送。")
                                else:
                                    media_id = await self.uploader.upload_media_via_token(local_path)
                                    
                                    if media_id:
                                        success = await self.uploader.send_file_via_token(to_user, media_id)
                                        if success:
                                            yield event.plain_result(f"✅ 文件 '{filename}' 已推送到您的私聊。")
                                        else:
                                            yield event.plain_result(f"❌ 应用消息推送失败，请检查 AgentID 是否正确 (默认1000002)。")
                                    else:
                                        yield event.plain_result(f"❌ 素材上传失败，请检查日志。")

                        except Exception as e:
                            logger.error(f"[WeDriveUploader] 推送流程异常: {e}")
                            yield event.plain_result(f"❌ 推送异常: {e}")
                    else:
                        yield event.plain_result(f"❌ 下载失败，请检查日志。")
            
            event.stop_event()
            return

        message_chain = event.message_obj.message
        
        # 调试日志：打印收到的消息组件类型
        logger.info(f"[WeDriveUploader] 收到消息: {[type(c) for c in message_chain]}")
        
        for component in message_chain:
            # 检查是否是文件类型 (File, Image, Video)
            # 这里主要针对 File，如果需要支持图片/视频自动归档也可以加上
            if isinstance(component, (File, Image, Video)):
                logger.info(f"[WeDriveUploader] 检测到文件消息，准备处理...")
                
                # 获取文件本地路径 (AstrBot 会自动下载)
                try:
                    # get_file() 通常返回一个路径字符串
                    # 注意：对于 Image/Video，可能需要 save=True 参数或者其他处理，
                    # 但 File 组件通常已经有路径或 url
                    # AstrBot 的 File 组件如果有 file 属性指向本地路径
                    file_path = None
                    
                    if hasattr(component, 'file') and component.file and os.path.exists(component.file):
                        file_path = component.file
                    elif hasattr(component, 'path') and component.path and os.path.exists(component.path):
                        file_path = component.path
                    else:
                        # 尝试调用可能存在的下载方法
                        # 在某些适配器中，可能需要显式下载
                        # 这里假设框架已经处理了下载，或者组件提供了路径
                        pass

                    if not file_path:
                         logger.warning(f"[WeDriveUploader] 无法获取文件本地路径，跳过上传。")
                         continue

                    logger.info(f"[WeDriveUploader] 开始上传文件: {file_path}")
                    yield event.plain_result(f"📥 正在归档文件到微盘...")
                    
                    file_id = await self.uploader.upload_file(file_path)
                    
                    if file_id:
                        yield event.plain_result(f"✅ 文件已归档至微盘。\nFileID: {file_id}")
                    else:
                        yield event.plain_result(f"❌ 文件归档失败，请检查日志。")
                        
                except Exception as e:
                    logger.error(f"[WeDriveUploader] 处理文件异常: {e}")
