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
        self.search_cache = {} # Key: session_id, Value: list of file objects
        
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

    def _get_cached_file(self, session_id, index_str):
        """Helper to get file from cache by index string"""
        if not index_str.isdigit():
            return None
        
        index = int(index_str)
        cache = self.search_cache.get(session_id)
        if not cache:
            return None
            
        # User index starts at 1
        if 1 <= index <= len(cache):
            return cache[index-1]
        return None

    async def _push_file_to_event(self, event: AstrMessageEvent, target_file: dict):
        """Helper to download and push file to the event source"""
        logger.info(f"[WeDriveUploader] _push_file_to_event called for file: {target_file.get('file_name', target_file.get('name'))}, ID: {target_file.get('fileid')}, FileType: {target_file.get('file_type')}")
        # Check if it's a folder
        is_folder = (target_file.get("file_type") == 1) or target_file.get("is_folder", False)
        if is_folder:
            yield event.plain_result(f"❌ 目标是一个文件夹，无法直接下载。")
            logger.info(f"[WeDriveUploader] _push_file_to_event: Target is a folder, cannot download directly.")
            return

        file_id = target_file.get("fileid")
        filename = target_file.get("file_name")
        yield event.plain_result(f"📥 正在下载 '{filename}' 并推送...")
        logger.debug(f"[WeDriveUploader] _push_file_to_event: Downloading file {filename} with ID {file_id}")
        
        local_path = await self.uploader.download_file_to_local(file_id, filename)
        
        if local_path:
            logger.debug(f"[WeDriveUploader] _push_file_to_event: File downloaded to {local_path}")
            try:
                is_group = (hasattr(event.message_obj, 'group_id') and event.message_obj.group_id) or (event.message_obj.type == MessageType.GROUP_MESSAGE)
                
                if is_group:
                    logger.debug(f"[WeDriveUploader] _push_file_to_event: Sending to group via webhook.")
                    webhook_key = self.config.get("webhook_key", "25994ab1-6b0b-4059-a47b-eebf5bd20e19")
                    media_id = await self.uploader.upload_to_webhook(local_path, webhook_key)
                    
                    if media_id:
                        logger.debug(f"[WeDriveUploader] _push_file_to_event: Uploaded to webhook, media_id: {media_id}")
                        success = await self.uploader.push_file_via_webhook(media_id, webhook_key)
                        if success:
                            yield event.plain_result(f"✅ 文件 '{filename}' 已通过 Webhook 推送到群。")
                            logger.debug(f"[WeDriveUploader] _push_file_to_event: Webhook push successful.")
                        else:
                            yield event.plain_result(f"❌ Webhook 推送失败。")
                            logger.error(f"[WeDriveUploader] _push_file_to_event: Webhook push failed.")
                    else:
                        yield event.plain_result(f"❌ 上传到 Webhook 失败。")
                        logger.error(f"[WeDriveUploader] _push_file_to_event: Upload to webhook failed.")
                else:
                    logger.debug(f"[WeDriveUploader] _push_file_to_event: Sending to private chat.")
                    to_user = event.message_obj.sender.user_id
                    if not to_user:
                            yield event.plain_result(f"❌ 无法获取您的 UserID。")
                            logger.error(f"[WeDriveUploader] _push_file_to_event: Cannot get user ID for private chat.")
                    else:
                        media_id = await self.uploader.upload_media_via_token(local_path)
                        if media_id:
                            logger.debug(f"[WeDriveUploader] _push_file_to_event: Uploaded media, media_id: {media_id}")
                            success = await self.uploader.send_file_via_token(to_user, media_id)
                            if success:
                                yield event.plain_result(f"✅ 文件 '{filename}' 已推送到您的私聊。")
                                logger.debug(f"[WeDriveUploader] _push_file_to_event: Private chat push successful.")
                            else:
                                yield event.plain_result(f"❌ 应用消息推送失败。")
                                logger.error(f"[WeDriveUploader] _push_file_to_event: Private chat push failed.")
                        else:
                            yield event.plain_result(f"❌ 素材上传失败。")
                            logger.error(f"[WeDriveUploader] _push_file_to_event: Media upload failed.")
            except Exception as e:
                logger.error(f"[WeDriveUploader] 推送流程异常: {e}")
                yield event.plain_result(f"❌ 推送异常: {e}")
        else:
            yield event.plain_result(f"❌ 下载失败，请检查日志。")
            logger.error(f"[WeDriveUploader] _push_file_to_event: File download failed, local_path is None.")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，筛选文件进行上传"""
        if not self.uploader:
            return

        logger.debug(f"[WeDriveUploader] on_message received: {repr(event.message_str)}, rules: {self.config.get('auto_download_rules')}")
        message_str = event.message_str.strip()
        cmd_map = {
            "搜": "搜",
            "删": "删",
            "下": "下",
            "建": "建",
            "移": "移",
            "清空回收站": "清空回收站",
            "帮助": "帮助"
        }
        
        target_cmd = None
        clean_msg = message_str
        
        # 1. Check for direct match at start
        for cmd in cmd_map:
            # Modified: Allow prefix match without space (e.g. "搜test")
            if message_str.startswith(cmd):
                target_cmd = cmd
                clean_msg = message_str
                break
        
        # 2. If not found, check if it's inside (e.g. after At)
        if not target_cmd:
             for cmd in cmd_map:
                # Search for " CMD" or "]CMD" (looser check)
                idx = message_str.find(cmd)
                if idx > 0:
                    prev_char = message_str[idx-1]
                    if prev_char.isspace() or prev_char == ']':
                        target_cmd = cmd
                        clean_msg = message_str[idx:]
                        break

        if not target_cmd:
            # Check for auto-download keywords
            rules = self.config.get("auto_download_rules", [])
            matched_rules = []

            for rule in rules:
                keywords = rule.get("keywords", [])
                file_path = rule.get("file_path")
                case_sensitive = rule.get("case_sensitive", False)
                
                if keywords and file_path and len(keywords) >= 2:
                    # Check if ALL keywords are in message
                    matched = False
                    if case_sensitive:
                        if all(k in message_str for k in keywords):
                            matched = True
                    else:
                        if all(k.lower() in message_str.lower() for k in keywords):
                            matched = True

                    if matched:
                        matched_rules.append(rule)
            
            if matched_rules:
                # Select the best match based on total keyword length
                best_rule = max(matched_rules, key=lambda r: sum(len(k) for k in r.get("keywords", [])))
                keywords = best_rule.get("keywords", [])
                file_path = best_rule.get("file_path")

                logger.info(f"[WeDriveUploader] 触发自动下载规则 (Best Match): {keywords} -> {file_path}")
                target_file = await self.uploader.get_file_by_path(file_path)
                if target_file:
                        logger.info(f"[WeDriveUploader] Calling _push_file_to_event for file: {file_path}")
                        async for res in self._push_file_to_event(event, target_file):
                            yield res
                else:
                        logger.warning(f"[WeDriveUploader] 自动下载规则触发，但未找到文件: {file_path}")
                        yield event.plain_result(f"❌ 自动下载失败：微盘中未找到文件 '{file_path}'。")
                
                event.stop_event()
                return
        else:
            message_str = clean_msg

        session_id = event.session_id

        # 0. 处理 "帮助" 指令
        if message_str.startswith("帮助"):
            help_text = (
                "微盘助手指令说明：\n\n"
                "搜<参数>\n"
                "  - 不加参数：列出根目录所有文件\n"
                "  - 加文件名：递归搜索全盘 (如: 搜es)\n"
                "  - 加路径：列出文件夹内容或搜索子目录 (如: 搜资料)\n\n"
                "下<序号/路径>\n"
                "  - 下载指定序号文件 (如: 下1)\n"
                "  - 下载指定路径文件 (如: 下资料/报告.pdf)\n\n"               
                "建<路径>\n"
                "  - 递归创建文件夹 (如: 建资料/2025/备份)\n\n"
                "移<序号/源路径> <目标路径>\n"
                "  - 移动文件或文件夹 (如: 移1 资料/备份)\n"
                "  - 移动到根目录使用 / (如: 移资料/旧文件.txt /)\n\n"
                "删<序号/路径>\n\n"
                "  **(需管理员权限，第一次删除：文件/文件夹将被移入「回收站」，第二次删除：删除「回收站」内文件，将永久删除)**：\n"
                "  - 删除序号1的文件：删1\n"
                "  - 第一次删除示例：删测试/test.txt\n\n"
                "清空回收站\n"
                "  - **(需管理员权限)**：永久删除整个回收站文件夹及其内容，并自动重建新回收站。\n"
            )
            yield event.plain_result(help_text)
            event.stop_event()
            return

        # 0.1 处理 "清空回收站" 指令
        if message_str.startswith("清空回收站"):
            admins = self.config.get("admins", [])
            sender_id = event.message_obj.sender.user_id 
            if sender_id not in admins:
                yield event.plain_result(f"❌ 权限不足。")
                event.stop_event()
                return
            
            if self.recycle_bin_id is None:
                await self._init_recycle_bin()
            
            if self.recycle_bin_id:
                yield event.plain_result(f"🗑️ 正在清空并重建回收站...")
                if await self.uploader.delete_file(self.recycle_bin_id):
                    self.recycle_bin_id = None
                    await self._init_recycle_bin()
                    yield event.plain_result(f"✅ 回收站已清空并完成重建。")
                else:
                    yield event.plain_result(f"❌ 清空失败，请检查日志。")
            else:
                yield event.plain_result(f"💡 回收站尚未初始化或不存在。")
            
            event.stop_event()
            return

        # 1. 处理 "搜" 指令
        if message_str.startswith("搜"):
            args = message_str[1:].strip()
            file_list = []
            
            # case 1: No args -> List root files
            if not args:
                logger.info(f"[WeDriveUploader] 收到搜(根目录)指令")
                yield event.plain_result(f"📂 正在获取微盘根目录文件...")
                
                files = await self.uploader.list_files() # Default lists root
                if files is None:
                     yield event.plain_result(f"❌ 获取文件列表失败，请检查日志。")
                     event.stop_event()
                     return
                else:
                    file_list = files.get('item', []) if isinstance(files, dict) else files
                    if not isinstance(file_list, list): file_list = []

            # case 2: With args
            else:
                # Check if args is a digit (index search)
                cached_file = self._get_cached_file(session_id, args)
                
                if cached_file:
                    # Index search logic
                    logger.info(f"[WeDriveUploader] 使用缓存文件(序号{args}): {cached_file.get('file_name', cached_file.get('name'))}")
                    
                    is_folder = (cached_file.get("file_type") == 1) or cached_file.get("is_folder", False)
                    name = cached_file.get('file_name', cached_file.get('name', '未知'))
                    
                    if is_folder:
                        folder_id = cached_file.get('fileid')
                        yield event.plain_result(f"📂 正在列出 '{name}' 的内容...")
                        files = await self.uploader.list_files(fatherid=folder_id)
                        if files:
                            file_list = files.get('item', [])
                    else:
                        yield event.plain_result(f"❌ '{name}' 是一个文件，无法进入搜索。\n💡 提示：可使用 '下{args}' 下载，或 '删{args}' 删除。")
                        event.stop_event()
                        return
                
                # Path/Keyword search logic (fallback if not digit or not in cache, actually if digit but not in cache _get_cached_file returns None)
                # Note: If user types "1" but cache is empty, _get_cached_file returns None.
                # In that case, should we try to search for file named "1"?
                # The previous logic would treat "1" as a path/keyword. 
                # Let's keep that behavior: if not found in cache (or not digit), treat as path/keyword.
                else:
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
                    
                    # If not a folder match, assume keyword search
                    elif not matched_folder:
                        keyword = args
                        start_node_id = None
                        start_path_str = ""
                        
                        if "/" in keyword:
                            path_part, key_part = keyword.rsplit('/', 1)
                            if not key_part: # "A/B/"
                                 yield event.plain_result(f"❌ 未找到指定文件夹: {path_part}")
                                 event.stop_event()
                                 return
                            
                            logger.info(f"[WeDriveUploader] 正在解析搜索路径: {path_part}")
                            folder = await self.uploader.get_file_by_path(path_part)
                            
                            if not folder or folder.get('file_type') != 1:
                                yield event.plain_result(f"❌ 未找到指定搜索目录: {path_part}")
                                event.stop_event()
                                return

                            start_node_id = folder.get('fileid')
                            start_path_str = path_part
                            keyword = key_part
                        
                        target_scope = start_path_str if start_path_str else "根目录"
                        yield event.plain_result(f"🔍 正在 '{target_scope}' 下递归搜索 '{keyword}' ...")
                        
                        file_list = await self.uploader.recursive_search(keyword, start_father_id=start_node_id, start_path=start_path_str)

            # --- Display Results and Cache ---
            if not file_list:
                yield event.plain_result(f"📂 未找到文件。")
                self.search_cache[session_id] = []
            else:
                # Store in cache
                self.search_cache[session_id] = file_list
                
                msg = f"📂 搜索结果 (共{len(file_list)}个):\n"
                for i, f in enumerate(file_list):
                    name = f.get("file_name", f.get("name", "未知文件")) # recursive_search returns 'name', list_files returns 'file_name'
                    size = int(f.get("file_size", f.get("size", 0)))
                    is_folder = (f.get("file_type") == 1) or f.get("is_folder", False)
                    
                    if size < 1024: size_str = f"{size}B"
                    elif size < 1024 * 1024: size_str = f"{size/1024:.1f}KB"
                    else: size_str = f"{size/1024/1024:.1f}MB"
                    
                    icon = "📁" if is_folder else "📄"
                    msg += f"[{i+1}] {icon} {name} ({size_str})\n\n"
                
                msg += "\n💡 提示：可使用序号操作，如 '搜1' (进入), '下1', '删2', '移1 资料'"
                yield event.plain_result(msg)
            
            event.stop_event()
            return

        # 3. 处理 "删" 指令
        if message_str.startswith("删"):
            if self.uploader is None:
                yield event.plain_result(f"❌ 微盘服务未初始化。")
                event.stop_event()
                return

            if self.recycle_bin_id is None:
                if not await self._init_recycle_bin():
                    yield event.plain_result(f"❌ 回收站初始化失败。")
                    event.stop_event()
                    return

            admins = self.config.get("admins", [])
            sender_id = event.message_obj.sender.user_id 
            if sender_id not in admins:
                yield event.plain_result(f"❌ 权限不足。")
                event.stop_event()
                return

            arg_str = message_str[1:].strip()
            if not arg_str:
                yield event.plain_result("⚠️ 请输入序号或路径，例如：删1 或 删test.txt")
                event.stop_event()
                return

            target_file_obj = None
            cached_file = self._get_cached_file(session_id, arg_str)
            
            if cached_file:
                logger.info(f"[WeDriveUploader] 使用缓存文件(序号{arg_str}): {cached_file.get('file_name', cached_file.get('name'))}")
                target_file_obj = cached_file
                # Normalize key names if needed (recursive_search uses 'name', 'path', others use 'file_name')
                if 'file_name' not in target_file_obj and 'name' in target_file_obj:
                    target_file_obj['file_name'] = target_file_obj['name']
                # Ensure fileid is present
                if 'fileid' not in target_file_obj:
                     yield event.plain_result(f"❌ 缓存文件信息缺失，请重新搜索。")
                     event.stop_event()
                     return
                
                # Need to find fatherid to check recycle bin status?
                # recursive_search items don't strictly have 'fatherid'.
                # list_files items might not either unless we check structure.
                # However, delete logic checks parent to see if it's in recycle bin.
                # If we don't have fatherid, we might need to fetch info? 
                # Or just try to delete. 'delete_file' works by fileid. 
                # The recycle bin logic in original code depended on 'fatherid'.
                
                # Optimization: If cached obj is from recursive search, we might know path but not fatherid directly.
                # Let's try to fetch full info if fatherid is missing, or rely on move logic.
                
                if 'fatherid' not in target_file_obj:
                     # Try to resolve by path if available to get full metadata?
                     # Actually, for delete logic:
                     # 1. Check if current parent is recycle bin -> Permanent Delete
                     # 2. Else -> Move to recycle bin
                     
                     # Since we don't know parent ID easily from search result (unless we query),
                     # we can check if the file's path starts with "回收站/"?
                     path_val = target_file_obj.get('path', target_file_obj.get('file_name'))
                     # If from list_files(root), path is just name.
                     # If from recursive_search, path is full path.
                     
                     if path_val.startswith("回收站/") or path_val == "回收站":
                         # It is in recycle bin
                         # Mock fatherid
                         target_file_obj['fatherid'] = self.recycle_bin_id
                     else:
                         # Assume not in recycle bin
                         target_file_obj['fatherid'] = "unknown"

            else:
                # Path based lookup
                yield event.plain_result(f"🗑️ 正在查找并处理 '{arg_str}' ...")
                target_file_obj = await self.uploader.get_file_by_path(arg_str)

            if not target_file_obj:
                 yield event.plain_result(f"❌ 未找到文件/路径 '{arg_str}'。")
                 event.stop_event()
                 return
            
            file_id_to_delete = target_file_obj.get("fileid")
            file_name_to_delete = target_file_obj.get("file_name", target_file_obj.get("name"))
            
            # Check if target is already in recycle bin
            target_parent_id = target_file_obj.get('fatherid')
            
            if target_parent_id == self.recycle_bin_id:
                logger.info(f"🗑️ 文件 '{file_name_to_delete}' 在回收站中，永久删除。")
                if await self.uploader.delete_file(file_id_to_delete):
                    yield event.plain_result(f"✅ 已从回收站中永久删除 '{file_name_to_delete}'。")
                else:
                    yield event.plain_result(f"❌ 永久删除失败。")
            else:
                logger.info(f"🗑️ 文件 '{file_name_to_delete}' 移入回收站。")
                if await self.uploader.move_files([file_id_to_delete], self.recycle_bin_id):
                    yield event.plain_result(f"✅ 已将 '{file_name_to_delete}' 移动到回收站。")
                else:
                    yield event.plain_result(f"❌ 移动到回收站失败。")
            
            event.stop_event()
            return

        # 4. 处理 "下" 指令
        if message_str.startswith("下"):
            arg_str = message_str[1:].strip()
            if not arg_str:
                yield event.plain_result("⚠️ 请输入序号或文件路径，例如：下1")
                event.stop_event()
                return

            target_file = None
            cached_file = self._get_cached_file(session_id, arg_str)
            
            if cached_file:
                logger.info(f"[WeDriveUploader] 使用缓存文件(序号{arg_str}): {cached_file.get('file_name', cached_file.get('name'))}")
                target_file = cached_file
                if 'file_name' not in target_file and 'name' in target_file:
                    target_file['file_name'] = target_file['name']
            else:
                yield event.plain_result(f"🔍 正在查找文件 '{arg_str}' ...")
                target_file = await self.uploader.get_file_by_path(arg_str)
            
            if not target_file:
                 yield event.plain_result(f"❌ 未找到文件 '{arg_str}'。")
            else:
                # Check if it's a folder
                is_folder = (target_file.get("file_type") == 1) or target_file.get("is_folder", False)
                if is_folder:
                    yield event.plain_result(f"❌ 目标是一个文件夹，无法直接下载。")
                else:
                    file_id = target_file.get("fileid")
                    filename = target_file.get("file_name")
                    yield event.plain_result(f"📥 正在下载 '{filename}' 并推送...")
                    
                    local_path = await self.uploader.download_file_to_local(file_id, filename)
                    
                    if local_path:
                        try:
                            is_group = (hasattr(event.message_obj, 'group_id') and event.message_obj.group_id) or (event.message_obj.type == MessageType.GROUP_MESSAGE)
                            
                            if is_group:
                                webhook_key = self.config.get("webhook_key", "25994ab1-6b0b-4059-a47b-eebf5bd20e19")
                                media_id = await self.uploader.upload_to_webhook(local_path, webhook_key)
                                
                                if media_id:
                                    success = await self.uploader.push_file_via_webhook(media_id, webhook_key)
                                    if success:
                                        yield event.plain_result(f"✅ 文件 '{filename}' 已通过 Webhook 推送到群。")
                                    else:
                                        yield event.plain_result(f"❌ Webhook 推送失败。")
                                else:
                                    yield event.plain_result(f"❌ 上传到 Webhook 失败。")
                            else:
                                to_user = event.message_obj.sender.user_id
                                if not to_user:
                                     yield event.plain_result(f"❌ 无法获取您的 UserID。")
                                else:
                                    media_id = await self.uploader.upload_media_via_token(local_path)
                                    if media_id:
                                        success = await self.uploader.send_file_via_token(to_user, media_id)
                                        if success:
                                            yield event.plain_result(f"✅ 文件 '{filename}' 已推送到您的私聊。")
                                        else:
                                            yield event.plain_result(f"❌ 应用消息推送失败。")
                                    else:
                                        yield event.plain_result(f"❌ 素材上传失败。")
                        except Exception as e:
                            logger.error(f"[WeDriveUploader] 推送流程异常: {e}")
                            yield event.plain_result(f"❌ 推送异常: {e}")
                    else:
                        yield event.plain_result(f"❌ 下载失败，请检查日志。")
            
            event.stop_event()
            return

        # 5. 处理 "建" 指令
        if message_str.startswith("建"):
            path_str = message_str[1:].strip()
            if not path_str:
                yield event.plain_result("⚠️ 请输入要创建的文件夹路径，例如：建资料/2025/备份")
                event.stop_event()
                return

            logger.info(f"[WeDriveUploader] 尝试创建文件夹: {path_str}")
            yield event.plain_result(f"📂 正在创建文件夹 '{path_str}' ...")

            result_id = await self.uploader.create_folder_by_path(path_str)
            
            if result_id:
                 yield event.plain_result(f"✅ 文件夹 '{path_str}' 创建成功。")
            else:
                 yield event.plain_result(f"❌ 创建失败。")
            
            event.stop_event()
            return

        # 6. 处理 "移" 指令
        if message_str.startswith("移"):
            args_str = message_str[1:].strip()
            args = args_str.split()
            
            if len(args) != 2:
                yield event.plain_result("⚠️ 指令格式错误。请使用：移 <序号/源路径> <目标路径>")
                event.stop_event()
                return

            src_arg = args[0]
            dst_path = args[1]
            
            src_file = None
            cached_file = self._get_cached_file(session_id, src_arg)
            if cached_file:
                logger.info(f"[WeDriveUploader] 使用缓存文件(序号{src_arg})作为源")
                src_file = cached_file
            else:
                logger.info(f"[WeDriveUploader] 查找源路径: {src_arg}")
                src_file = await self.uploader.get_file_by_path(src_arg)
            
            if not src_file:
                yield event.plain_result(f"❌ 未找到源文件 '{src_arg}'。")
                event.stop_event()
                return
                
            # Resolve destination
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
            
            yield event.plain_result(f"🚚 正在移动...")
            success = await self.uploader.move_files([src_file['fileid']], dst_folder_id)
            if success:
                src_name = src_file.get('file_name', src_file.get('name'))
                yield event.plain_result(f"✅ 已将 '{src_name}' 移动到 '{dst_name}'。")
            else:
                yield event.plain_result(f"❌ 移动失败。")

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
