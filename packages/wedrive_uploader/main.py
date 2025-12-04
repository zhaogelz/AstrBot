import json
import os
import aiohttp
import logging
import asyncio
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
        self.recycle_bin_id = None
        self.init_lock = asyncio.Lock()
        
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

    async def _init_recycle_bin(self):
        async with self.init_lock:
            if self.recycle_bin_id is not None:
                return # Already initialized

            logger.info("[WeDriveUploader] 初始化回收站文件夹...")
            recycle_bin_name = "回收站"
            
            # Check if recycle bin exists, create if not
            recycle_bin_folder = await self.uploader.get_file_by_path(recycle_bin_name)
            if recycle_bin_folder and recycle_bin_folder.get('file_type') == 1:
                self.recycle_bin_id = recycle_bin_folder.get('fileid')
                logger.info(f"✅ 回收站文件夹已存在，ID: {self.recycle_bin_id}")
            else:
                logger.info(f"⚠️ 回收站文件夹不存在，正在创建...")
                created_id = await self.uploader.create_folder_by_path(recycle_bin_name)
                if created_id:
                    self.recycle_bin_id = created_id
                    logger.info(f"✅ 回收站文件夹创建成功，ID: {self.recycle_bin_id}")
                else:
                    logger.error(f"❌ 无法创建回收站文件夹！删除功能将受影响。")
            
            return self.recycle_bin_id is not None

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
        # 这里采用简单策略：如果包含 "下 " 等指令，直接提取指令及之后的部分
        
        # 定义指令映射：Key 为指令触发词，Value 为内部标识
        # 注意：为了防止误触，单字指令必须配合 "指令+空格" 的形式检测
        cmd_map = {
            "搜": "搜",
            "删": "删",
            "下": "下",
            "建": "建",
            "移": "移",
            "帮助": "帮助"
        }
        
        target_cmd = None
        clean_msg = message_str
        
        # 预处理：尝试去除 At 部分（如果存在）
        # 如果消息以 "[At:" 开头，或者以 "@" 开头，找到第一个空格或 "]" 后的内容
        # 这是一个简化的处理，实际情况 AstrBot core 可能已经处理了 clean content，
        # 但这里直接操作 message_str 比较稳妥。
        
        # 实际上，我们只需要检测 message_str 是否以 "CMD " 开头
        # 或者 "@Bot CMD "
        
        # 1. Check for direct match at start
        for cmd in cmd_map:
            # Strict rule: CMD must be followed by space, OR be the exact string (for "搜" with no args, or "帮助")
            if message_str == cmd or message_str.startswith(cmd + " "):
                target_cmd = cmd
                clean_msg = message_str
                break
        
        # 2. If not found, check if it's inside (e.g. after At)
        if not target_cmd:
             for cmd in cmd_map:
                # Search for " CMD " or "] CMD " or "]CMD "
                # Simplest heuristic: Find the cmd, check character before it.
                idx = message_str.find(cmd)
                if idx > 0:
                    # Check char before
                    prev_char = message_str[idx-1]
                    # Check char after (must be space or end of string)
                    is_end = (idx + len(cmd) == len(message_str))
                    next_char_is_space = (not is_end) and message_str[idx+len(cmd)] == ' '
                    
                    if (prev_char.isspace() or prev_char == ']') and (is_end or next_char_is_space):
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
                "微盘助手指令说明 (单字指令需加空格)：\n\n"
                "搜 <参数>\n"
                "  - 不加参数：列出根目录所有文件\n"
                "  - 加文件名：递归搜索全盘 (如: 搜 es)\n"
                "  - 加路径：列出文件夹内容或搜索子目录 (如: 搜 资料)\n\n"
                "下 <路径>\n"
                "  - 下载根目录文件 (如: 下 test.txt)\n"
                "  - 下载指定路径文件 (如: 下 资料/报告.pdf)\n\n"
                "删 <路径>\n"
                "  **(需管理员权限，第一次删除：文件/文件夹将被移入「回收站」，第二次删除：删除「回收站」内文件，将永久删除)**：\n"
                "  - 第一次删除示例：删 测试/test.txt\n\n"
                "  - 第二次删除示例：删 回收站/test.txt\n\n"
                "建 <路径>\n"
                "  - 递归创建文件夹 (如: 建 资料/2025/备份)\n\n"
                "移 <源路径> <目标路径>\n"
                "  - 移动文件或文件夹 (如: 移 test.txt 资料/备份)\n"
                "  - 移动到根目录使用 / (如: 移 资料/旧文件.txt /)"
            )
            yield event.plain_result(help_text)
            event.stop_event()
            return

        # 1. 处理 "搜" 指令
        if message_str == "搜" or message_str.startswith("搜 "):
            # Handle case "搜" (no space, just cmd) -> args is empty
            # Handle case "搜 xxx" -> args is "xxx"
            args = message_str[1:].strip()
            
            # case 1: No args -> List root files
            if not args:
                logger.info(f"[WeDriveUploader] 收到搜(根目录)指令")
                yield event.plain_result(f"📂 正在获取微盘根目录文件...")
                
                files = await self.uploader.list_files() # Default lists root
                if files is None:
                     yield event.plain_result(f"❌ 获取文件列表失败，请检查日志。")
                else:
                    file_list = files.get('item', []) if isinstance(files, dict) else files
                    if not isinstance(file_list, list): file_list = []

                    if not file_list:
                         yield event.plain_result(f"📂 微盘根目录为空。")
                    else:
                        msg = f"📂 根目录文件 (共{len(file_list)}个):\n"
                        for f in file_list:
                            name = f.get("file_name", "未知文件")
                            size = int(f.get("file_size", 0))
                            is_folder = (f.get("file_type") == 1)
                            
                            if size < 1024: size_str = f"{size}B"
                            elif size < 1024 * 1024: size_str = f"{size/1024:.1f}KB"
                            else: size_str = f"{size/1024/1024:.1f}MB"
                            
                            icon = "📁" if is_folder else "📄"
                            msg += f"{icon} {name} ({size_str})\n"
                        yield event.plain_result(msg)
                event.stop_event()
                return

            # case 2: With args -> Check if it's a folder path first
            # If args matches a folder, list its content.
            
            # Try exact path match first
            matched_folder = await self.uploader.get_file_by_path(args)
            
            if matched_folder and matched_folder.get('file_type') == 1:
                folder_name = matched_folder.get('file_name')
                folder_id = matched_folder.get('fileid')
                logger.info(f"[WeDriveUploader] 参数 '{args}' 匹配到文件夹，列出内容...")
                
                yield event.plain_result(f"📂 正在列出 '{args}' 的内容...")
                files = await self.uploader.list_files(fatherid=folder_id)
                
                if files:
                    file_list = files.get('item', [])
                    if not file_list:
                         yield event.plain_result(f"📂 文件夹 '{folder_name}' 为空。")
                    else:
                        msg = f"📂 '{folder_name}' 文件列表 (共{len(file_list)}个):\n"
                        for f in file_list:
                            name = f.get("file_name")
                            size = int(f.get("file_size", 0))
                            is_folder = (f.get("file_type") == 1)
                            
                            if size < 1024: size_str = f"{size}B"
                            elif size < 1024 * 1024: size_str = f"{size/1024:.1f}KB"
                            else: size_str = f"{size/1024/1024:.1f}MB"
                            
                            icon = "📁" if is_folder else "📄"
                            msg += f"{icon} {name} ({size_str})\n"
                        yield event.plain_result(msg)
                else:
                    yield event.plain_result(f"❌ 获取失败或文件夹为空。")
                
                event.stop_event()
                return

            # If not a folder, proceed to recursive search
            keyword = args
            start_node_id = None
            start_path_str = ""
            
            # Check if it's a path search: "Folder/Keyword"
            # Note: If "A/B" was a folder, it would have been caught above.
            # So if we are here, "A/B" is NOT a folder.
            # It could be "Folder/Keyword" where Folder exists but Keyword is just a string.
            
            if "/" in keyword:
                # Split by last slash
                path_part, key_part = keyword.rsplit('/', 1)
                
                # If keyword ends with /, e.g. "A/B/", and it wasn't caught above as a folder,
                # then "A/B" likely doesn't exist as a folder.
                
                if not key_part: # "A/B/"
                     # This means get_file_by_path("A/B") failed (returned None or not folder).
                     yield event.plain_result(f"❌ 未找到指定文件夹: {path_part}")
                     event.stop_event()
                     return
                
                logger.info(f"[WeDriveUploader] 正在解析搜索路径: {path_part}")
                folder = await self.uploader.get_file_by_path(path_part)
                
                if not folder:
                    yield event.plain_result(f"❌ 未找到指定搜索目录: {path_part}")
                    event.stop_event()
                    return
                
                if folder.get('file_type') != 1:
                     yield event.plain_result(f"❌ 路径 '{path_part}' 不是一个文件夹。")
                     event.stop_event()
                     return

                start_node_id = folder.get('fileid')
                start_path_str = path_part
                keyword = key_part # Update keyword to search
            
            # Do recursive search
            # If keyword is empty here, it means user typed "Folder/" but "Folder" logic handled it?
            # No, if "Folder/" and "Folder" exists, it's caught by get_file_by_path("Folder") logic above.
            # So we shouldn't reach here with empty keyword usually.
            
            target_scope = start_path_str if start_path_str else "根目录"
            yield event.plain_result(f"🔍 正在 '{target_scope}' 下递归搜索 '{keyword}' ...")
            
            results = await self.uploader.recursive_search(keyword, start_father_id=start_node_id, start_path=start_path_str)
            
            if not results:
                yield event.plain_result(f"📂 未找到包含 '{keyword}' 的文件。")
            else:
                msg = f"🔍 搜索结果 (共{len(results)}个):\n"
                for res in results:
                    # res: {name, path, size, is_folder}
                    icon = "📁" if res['is_folder'] else "📄"
                    path = res['path']
                    size = res['size']
                    if size < 1024: size_str = f"{size}B"
                    elif size < 1024 * 1024: size_str = f"{size/1024:.1f}KB"
                    else: size_str = f"{size/1024/1024:.1f}MB"
                    
                    msg += f"{icon} {path} ({size_str})\n"
                yield event.plain_result(msg)
            
            event.stop_event()
            return

        # 3. 处理 "删" 指令
        if message_str.startswith("删 "):
            # --- Start: Recycle bin and Admin check ---
            if self.uploader is None:
                yield event.plain_result(f"❌ 微盘服务未初始化，请检查配置。")
                event.stop_event()
                return

            # Init recycle bin if not already
            if self.recycle_bin_id is None:
                if not await self._init_recycle_bin():
                    yield event.plain_result(f"❌ 回收站初始化失败，无法执行删除操作。")
                    event.stop_event()
                    return

            # Admin check
            admins = self.config.get("admins", [])
            sender_id = event.message_obj.sender.user_id # Assuming user_id is reliable and unique
            if sender_id not in admins:
                yield event.plain_result(f"❌ 权限不足：您 ({sender_id}) 没有删除操作的权限。请联系管理员添加您的 UserID。")
                event.stop_event()
                return
            # --- End: Recycle bin and Admin check ---

            path_str = message_str[1:].strip()
            if not path_str:
                yield event.plain_result("⚠️ 请输入要删除的文件或文件夹路径，例如：删 test.txt")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 尝试删除: {path_str}")
            yield event.plain_result(f"🗑️ 正在查找并处理 '{path_str}' ...")

            # Use get_file_by_path to resolve the file/folder
            target_file_obj = await self.uploader.get_file_by_path(path_str)
            
            if not target_file_obj:
                 yield event.plain_result(f"❌ 未找到路径 '{path_str}'。请确认路径是否正确。")
                 event.stop_event()
                 return
            
            file_id_to_delete = target_file_obj.get("fileid")
            file_name_to_delete = target_file_obj.get("file_name")
            
            # Check if target is already in recycle bin
            # path_str might be "回收站/somefile.txt"
            # target_file_obj contains "fileid", "fatherid"
            
            # We need to get the path to the current target_file_obj.
            # Get parent folder's ID
            target_parent_id = target_file_obj.get('fatherid')
            
            # Compare with self.recycle_bin_id
            if target_parent_id == self.recycle_bin_id:
                # File is already in recycle bin, perform permanent delete
                logger.info(f"🗑️ 路径 '{path_str}' 已在回收站中，执行永久删除。")
                if await self.uploader.delete_file(file_id_to_delete):
                    yield event.plain_result(f"✅ 已从回收站中永久删除 '{file_name_to_delete}'。")
                else:
                    yield event.plain_result(f"❌ 永久删除失败，请检查日志。")
            else:
                # File is not in recycle bin, move to recycle bin
                logger.info(f"🗑️ 路径 '{path_str}' 不在回收站中，移动到回收站。")
                if await self.uploader.move_files([file_id_to_delete], self.recycle_bin_id):
                    yield event.plain_result(f"✅ 已将 '{file_name_to_delete}' 移动到回收站。")
                else:
                    yield event.plain_result(f"❌ 移动到回收站失败，请检查日志。")
            
            event.stop_event()
            return

        # 4. 处理 "下" 指令
        if message_str.startswith("下 "):
            path_str = message_str[1:].strip()
            if not path_str:
                yield event.plain_result("⚠️ 请输入要下载的文件路径，例如：下 资料/test.txt")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 尝试下载文件: {path_str}")
            yield event.plain_result(f"🔍 正在查找文件 '{path_str}' ...")

            target_file = await self.uploader.get_file_by_path(path_str)
            
            if not target_file:
                 yield event.plain_result(f"❌ 未找到文件 '{path_str}'。")
            else:
                # Check if it's a folder
                if target_file.get("file_type") == 1:
                    yield event.plain_result(f"❌ '{path_str}' 是一个文件夹，无法直接下载。")
                else:
                    file_id = target_file.get("fileid")
                    filename = target_file.get("file_name")
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

        # 5. 处理 "建" 指令
        if message_str.startswith("建 "):
            path_str = message_str[1:].strip()
            if not path_str:
                yield event.plain_result("⚠️ 请输入要创建的文件夹路径，例如：建 资料/2025/备份")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 尝试创建文件夹: {path_str}")
            yield event.plain_result(f"📂 正在创建文件夹 '{path_str}' ...")

            result_id = await self.uploader.create_folder_by_path(path_str)
            
            if result_id:
                 yield event.plain_result(f"✅ 文件夹 '{path_str}' (及必要父目录) 创建/确认成功。")
            else:
                 yield event.plain_result(f"❌ 创建失败，请检查日志。")
            
            event.stop_event()
            return

        # 6. 处理 "移" 指令
        if message_str.startswith("移"):
            args = message_str[1:].strip().split()
            if len(args) != 2:
                yield event.plain_result("⚠️ 指令格式错误。请使用：移 <源路径> <目标文件夹路径>，例如：移 test.txt 资料/备份")
                event.stop_event()
                return

            src_path = args[0]
            dst_path = args[1]
            
            logger.info(f"[WeDriveUploader] 尝试移动: {src_path} -> {dst_path}")
            yield event.plain_result(f"🚚 正在解析路径并移动...")

            # Resolve source
            src_file = await self.uploader.get_file_by_path(src_path)
            if not src_file:
                yield event.plain_result(f"❌ 未找到源文件/文件夹 '{src_path}'。")
                event.stop_event()
                return
                
            # Resolve destination
            # Support moving to root if dst is "/" or "."? 
            # Assume user provides a folder name. If they want root, maybe they type "root" or "/"?
            # For now, assume explicit path.
            if dst_path == "/" or dst_path == ".":
                dst_folder_id = self.uploader.space_id
                dst_name = "根目录"
            else:
                dst_folder = await self.uploader.get_file_by_path(dst_path)
                if not dst_folder:
                    yield event.plain_result(f"❌ 未找到目标文件夹 '{dst_path}'。")
                    event.stop_event()
                    return
                dst_folder_id = dst_folder.get('fileid')
                dst_name = dst_folder.get('file_name')
                
                # Check if dst is actually a folder (file_type=1 is folder usually, but let API handle or check?)
                # It's safer to try.
            
            success = await self.uploader.move_files([src_file['fileid']], dst_folder_id)
            if success:
                yield event.plain_result(f"✅ 已将 '{src_path}' 移动到 '{dst_name}'。")
            else:
                yield event.plain_result(f"❌ 移动失败，请检查目标是否为有效文件夹或权限问题。")

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
