# plugins/groupinsight/main.py

import asyncio
import base64
import os
import requests
import logging
import time
import html
import re
import traceback
from collections import defaultdict, deque
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, timedelta

from pkg.plugin.context import BasePlugin, APIHost, EventContext
from pkg.plugin.events import GroupNormalMessageReceived, GroupMessageReceived
from pkg.platform.types import MessageChain, Plain, Image

try:
    import graphviz
except ImportError:
    graphviz = None

TRIGGER_KEYWORD = "#邀请关系"
TRIGGER_KEYWORD_NETWORK = "#查关系网"
TRIGGER_KEYWORD_KICK_MEMBER = "#踢人"
TRIGGER_KEYWORD_KICK_DOWNLINE = "#踢关系网"
TRIGGER_KEYWORD_HELP = "#帮助"

IMAGE_FORMAT = 'png'
# 【重构】默认的 dot 引擎参数，使用 ortho 优化线条
GRAPH_ATTR_DOT = {
    'rankdir': 'TB',
    'dpi': '150',
    'nodesep': '0.6',
    'ranksep': '1.2',
    'pad': '1.0,1.0',
    'splines': 'ortho',  # 使用直角线，更整洁
    'concentrate': 'false',
}
# 【重构】为 twopi 引擎定制的参数，解决高密度问题
GRAPH_ATTR_TWOPI = {
    'dpi': '150',
    'pad': '1.5,1.5', # 更大的边距
    'splines': 'spline',
    'overlap': 'false', # 禁止节点重叠
    'sep': '+25,25', # 强制增加节点间距
}
NODE_ATTR = {
    'style': 'filled',
    'shape': 'box',
    'fontname': 'WenQuanYi Zen Hei',
    'fontsize': '12',
    'fixedsize': 'false',
    'margin': '0.25,0.15',
}
EDGE_ATTR = {'arrowsize': '0.7'}
MAX_NODES_TO_RENDER = 500
RENDER_TIMEOUT = 90 # 适当延长超时以应对复杂图
CACHE_DURATION = 60
STAR_GRAPH_THRESHOLD_RATIO = 0.3
STAR_GRAPH_THRESHOLD_ABSOLUTE = 15

class GroupInsightPlugin(BasePlugin):
    
    def __init__(self, host: APIHost):
        super().__init__(host)
        self.logger = logging.getLogger("GroupInsightPlugin")
        self.API_BASE_URL = None
        self.API_KEY = None
        self.ADMIN_USER_IDS = []
        self.group_info_cache = {}
        if graphviz is None:
            self.logger.warning("[GroupInsight] 'graphviz' 库或其系统依赖未找到。")

    # ... (initialize, _manual_register_handlers, _normalize_group_id, _clean_whitespace_and_special_chars 函数保持不变) ...
    async def initialize(self):
        self.logger.info("GroupInsight 插件正在进行异步初始化...")
        self.API_BASE_URL = self.config.get('api_base_url', '').strip()
        self.API_KEY = self.config.get('api_key', '').strip()
        self.ADMIN_USER_IDS = self.config.get('admin_user_ids', [])
        self._manual_register_handlers()
        self.logger.info("GroupInsight 插件初始化完成。")

    def _manual_register_handlers(self):
        container = self.ap.plugin_mgr.get_plugin(author='junhong', plugin_name='groupinsight')
        if container:
            handler_func = GroupInsightPlugin.group_message_handler
            container.event_handlers[GroupMessageReceived] = handler_func
            container.event_handlers[GroupNormalMessageReceived] = handler_func

    def _normalize_group_id(self, group_id: str) -> str:
        if group_id and not group_id.endswith('@chatroom'):
            return f"{group_id}@chatroom"
        return group_id

    def _clean_whitespace_and_special_chars(self, text: str) -> str:
        if not isinstance(text, str): 
            return ""
        text = re.sub(r'[\u200B-\u200F\u202F\u205F\uFEFF\u00A0\u00AD\u2800]', '', text)
        return text.strip()
    
    # ... (group_message_handler 和所有 _handle_... 函数保持不变) ...
    async def group_message_handler(self, ctx: EventContext):
        try:
            raw_msg = ctx.event.query.message_chain.get_plain_text().strip()
        except AttributeError:
            try: 
                raw_msg = ctx.event.text_message.strip()
            except Exception: return
        
        all_triggers = [TRIGGER_KEYWORD, TRIGGER_KEYWORD_NETWORK, TRIGGER_KEYWORD_KICK_MEMBER, TRIGGER_KEYWORD_KICK_DOWNLINE, TRIGGER_KEYWORD_HELP]
        if not any(raw_msg.startswith(trigger) for trigger in all_triggers):
            return

        sender_id = str(ctx.event.sender_id)
        current_group_id = str(ctx.event.query.launcher_id)
        
        is_admin = sender_id in self.ADMIN_USER_IDS or current_group_id in self.ADMIN_USER_IDS
        if not self.ADMIN_USER_IDS or not is_admin: return
        
        ctx.prevent_default()
        ctx.prevent_postorder()
        if not self.API_BASE_URL or not self.API_KEY:
            await ctx.reply(MessageChain([Plain("插件核心配置缺失，请联系机器人管理员。")]))
            return

        GROUP_ID_REGEX = r'[\w\-\.]+(?:@chatroom)?'
        MEMBER_ID_REGEX = r'[\w\-\.]+'
        
        try:
            if raw_msg.strip() == TRIGGER_KEYWORD_HELP:
                await self._handle_help_command(ctx, current_group_id)
            elif raw_msg.startswith(TRIGGER_KEYWORD):
                pattern_full = re.compile(r'^\s*' + re.escape(TRIGGER_KEYWORD) + r'\s+(?P<fetch_id>' + GROUP_ID_REGEX + r')\s+到\s+(?P<send_id>' + GROUP_ID_REGEX + r')\s*$')
                pattern_fetch_only = re.compile(r'^\s*' + re.escape(TRIGGER_KEYWORD) + r'\s+(?P<fetch_id>' + GROUP_ID_REGEX + r')\s*$')
                pattern_send_to_only = re.compile(r'^\s*' + re.escape(TRIGGER_KEYWORD) + r'到\s+(?P<send_id>' + GROUP_ID_REGEX + r')\s*$')
                pattern_default = re.compile(r'^\s*' + re.escape(TRIGGER_KEYWORD) + r'\s*$')
                if (match := pattern_full.match(raw_msg)):
                    await self._handle_invite_tree_command(ctx, self._normalize_group_id(match.group('fetch_id')), self._normalize_group_id(match.group('send_id')))
                elif (match := pattern_fetch_only.match(raw_msg)):
                    await self._handle_invite_tree_command(ctx, self._normalize_group_id(match.group('fetch_id')), self._normalize_group_id(current_group_id))
                elif (match := pattern_send_to_only.match(raw_msg)):
                    await self._handle_invite_tree_command(ctx, self._normalize_group_id(current_group_id), self._normalize_group_id(match.group('send_id')))
                elif pattern_default.match(raw_msg):
                    await self._handle_invite_tree_command(ctx, self._normalize_group_id(current_group_id), self._normalize_group_id(current_group_id))
                else:
                    await self._send_error_message(ctx, current_group_id, raw_msg, "指令格式错误")

            elif raw_msg.startswith(TRIGGER_KEYWORD_NETWORK):
                base_pattern = r'^\s*' + re.escape(TRIGGER_KEYWORD_NETWORK) + r'\s+(?P<member_id>' + MEMBER_ID_REGEX + r')'
                pattern_full = re.compile(base_pattern + r'\s+在\s+(?P<fetch_id>' + GROUP_ID_REGEX + r')\s+到\s+(?P<send_id>' + GROUP_ID_REGEX + r')\s*$')
                pattern_fetch_only = re.compile(base_pattern + r'\s+在\s+(?P<fetch_id>' + GROUP_ID_REGEX + r')\s*$')
                pattern_send_to_only = re.compile(base_pattern + r'\s+到\s+(?P<send_id>' + GROUP_ID_REGEX + r')\s*$')
                pattern_default = re.compile(base_pattern + r'\s*$')

                if (match := pattern_full.match(raw_msg)):
                    await self._handle_network_command(ctx, match.group('member_id'), self._normalize_group_id(match.group('fetch_id')), self._normalize_group_id(match.group('send_id')))
                elif (match := pattern_fetch_only.match(raw_msg)):
                    await self._handle_network_command(ctx, match.group('member_id'), self._normalize_group_id(match.group('fetch_id')), self._normalize_group_id(current_group_id))
                elif (match := pattern_send_to_only.match(raw_msg)):
                    await self._handle_network_command(ctx, match.group('member_id'), self._normalize_group_id(current_group_id), self._normalize_group_id(match.group('send_id')))
                elif (match := pattern_default.match(raw_msg)):
                    await self._handle_network_command(ctx, match.group('member_id'), self._normalize_group_id(current_group_id), self._normalize_group_id(current_group_id))
                else:
                    await self._send_error_message(ctx, current_group_id, raw_msg, f"格式错误, 示例: {TRIGGER_KEYWORD_NETWORK} wxid_xxxx")

            elif raw_msg.startswith(TRIGGER_KEYWORD_KICK_MEMBER):
                pattern = re.compile(r'^\s*' + re.escape(TRIGGER_KEYWORD_KICK_MEMBER) + r'\s+(?P<member_id>' + MEMBER_ID_REGEX + r')\s*$')
                if (match := pattern.match(raw_msg)):
                    await self._handle_kick_member_command(ctx, current_group_id, match.group('member_id'))
                else:
                    await self._send_error_message(ctx, current_group_id, raw_msg, f"格式错误, 示例: {TRIGGER_KEYWORD_KICK_MEMBER} wxid_xxxx")
            
            elif raw_msg.startswith(TRIGGER_KEYWORD_KICK_DOWNLINE):
                pattern = re.compile(r'^\s*' + re.escape(TRIGGER_KEYWORD_KICK_DOWNLINE) + r'\s+(?P<member_id>' + MEMBER_ID_REGEX + r')\s*$')
                if (match := pattern.match(raw_msg)):
                    await self._handle_kick_downline_command(ctx, current_group_id, match.group('member_id'))
                else:
                    await self._send_error_message(ctx, current_group_id, raw_msg, f"格式错误, 示例: {TRIGGER_KEYWORD_KICK_DOWNLINE} wxid_xxxx")

        except Exception as e:
            self.logger.error(f"指令处理时发生顶层异常: {e}\n{traceback.format_exc()}")
    
    async def _send_error_message(self, ctx: EventContext, group_id: str, raw_msg: str, reason: str):
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"{reason}。输入 {TRIGGER_KEYWORD_HELP} 获取帮助。")]))

    async def _handle_invite_tree_command(self, ctx: EventContext, fetch_group_id: str, send_group_id: str):
        image_path = None
        initiator_group_id = str(ctx.event.query.launcher_id)
        
        try:
            group_info = await self._fetch_group_details(fetch_group_id)
            if not group_info:
                await self.host.send_active_message(
                    ctx.event.query.adapter, "group", initiator_group_id, 
                    MessageChain([Plain(f"获取群 '{fetch_group_id}' 信息失败。请检查群ID是否正确或API是否可用。")])
                )
                return

            group_name = group_info.get('nickName', {}).get('str', fetch_group_id)
            
            await self.host.send_active_message(
                ctx.event.query.adapter, "group", initiator_group_id, 
                MessageChain([Plain(f"正在生成群 '{group_name}' ({fetch_group_id}) 的邀请关系图...")])
            )
            
            if graphviz is None:
                await self.host.send_active_message(
                    ctx.event.query.adapter, "group", initiator_group_id,
                    MessageChain([Plain("错误：'graphviz' 库未安装或未找到，无法生成关系图。")])
                )
                return
            
            member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
            
            if not member_list:
                await self.host.send_active_message(
                    ctx.event.query.adapter, "group", initiator_group_id, 
                    MessageChain([Plain(f"群 '{group_name}' ({fetch_group_id}) 成员列表为空或获取失败。")])
                )
                return
            
            if len(member_list) > MAX_NODES_TO_RENDER:
                await self.host.send_active_message(
                    ctx.event.query.adapter, "group", initiator_group_id,
                    MessageChain([Plain(f"生成失败：群 '{group_name}' ({fetch_group_id}) 成员数量 ({len(member_list)}) 超过了最大渲染限制 ({MAX_NODES_TO_RENDER})。")])
                )
                return

            filename_id = fetch_group_id.replace('@chatroom', '_')
            image_path = await self._generate_invite_tree_image(member_list, filename_id, group_name, ctx)
            
            if not image_path:
                await self.host.send_active_message(
                    ctx.event.query.adapter, "group", initiator_group_id,
                    MessageChain([Plain(f"生成群 '{group_name}' ({fetch_group_id}) 的关系图失败。可能原因：内部渲染错误或配置问题。详情请查看机器人后台日志。")])
                )
                return
            
            with open(image_path, 'rb') as f:
                img_base64 = base64.b64encode(f.read()).decode()
            
            await self.host.send_active_message(
                ctx.event.query.adapter, "group", send_group_id, 
                MessageChain([Image(base64=img_base64)])
            )

        except Exception as e:
            self.logger.error(f"处理邀请关系图命令时发生错误: {e}\n{traceback.format_exc()}")
            await self.host.send_active_message(
                ctx.event.query.adapter, "group", initiator_group_id, 
                MessageChain([Plain(f"处理命令时发生严重错误，请联系管理员。")])
            )
        finally:
            if image_path and os.path.exists(image_path):
                try: 
                    os.remove(image_path)
                except OSError as e: 
                    self.logger.error(f"清理临时文件 {image_path} 失败: {e}")

    async def _handle_network_command(self, ctx: EventContext, member_id: str, fetch_group_id: str, send_group_id: str):
        initiator_group_id = str(ctx.event.query.launcher_id)
        try:
            group_info = await self._fetch_group_details(fetch_group_id)
            if not group_info:
                await self.host.send_active_message(ctx.event.query.adapter, "group", initiator_group_id, MessageChain([Plain(f"获取群 '{fetch_group_id}' 信息失败。")]))
                return

            group_name = group_info.get('nickName', {}).get('str', fetch_group_id)
            member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
            members_map = {m['user_name']: self._clean_whitespace_and_special_chars(m.get('nick_name', '') or m['user_name']) for m in member_list}
            member_name = members_map.get(member_id, member_id)

            await self.host.send_active_message(ctx.event.query.adapter, "group", initiator_group_id, MessageChain([Plain(f"正在群 '{group_name}' ({fetch_group_id}) 中查询成员 '{member_name}' 的关系网络...")]))

            if not member_list:
                await self.host.send_active_message(ctx.event.query.adapter, "group", initiator_group_id, MessageChain([Plain(f"群 '{group_name}' ({fetch_group_id}) 成员列表为空。")]))
                return
                
            if member_id not in members_map:
                await self.host.send_active_message(ctx.event.query.adapter, "group", initiator_group_id, MessageChain([Plain(f"成员 '{member_id}' 不在群 '{group_name}' ({fetch_group_id}) 中。")]))
                return

            parent_map, children_map = self._build_invite_relationship(member_list)
            network_data = self._get_member_direct_network(member_id, parent_map, children_map, members_map)
            if network_data is None: return

            upstream, downstream = network_data
            display_name = members_map.get(member_id, member_id)
            parts = [f"群 '{group_name}' ({fetch_group_id}) 内成员 '{display_name} ({member_id})' 的邀请关系网络如下：\n"]
            parts.append("\n--- 上级邀请链 ---\n")
            parts.append(" -> ".join(upstream) + "\n" if upstream else "该成员是顶级邀请人（始祖人）或其上级已退群。\n")
            parts.append(f"\n--- 直接邀请的下级 (共 {len(downstream)} 位) ---\n")
            if downstream:
                for wxid, nickname in downstream.items():
                    parts.append(f"- {nickname} ({wxid})\n")
            else:
                parts.append("该成员没有直接邀请任何下级成员。\n")
            await self.host.send_active_message(ctx.event.query.adapter, "group", send_group_id, MessageChain([Plain("".join(parts))]))
        except Exception as e:
            self.logger.error(f"处理 #查关系网 命令时发生错误: {e}\n{traceback.format_exc()}")
            await self.host.send_active_message(ctx.event.query.adapter, "group", initiator_group_id, MessageChain([Plain(f"处理命令时发生未知错误。")]))

    async def _handle_kick_member_command(self, ctx: EventContext, group_id: str, member_id: str):
        try:
            group_id = self._normalize_group_id(group_id)
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"正在尝试踢出成员 '{member_id}'...")]))
            group_info = await self._fetch_group_details(group_id)
            if not group_info: return

            member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
            if not any(m['user_name'] == member_id for m in member_list):
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"成员 '{member_id}' 不在本群。")]))
                return

            name = self._get_member_display_name(member_list, member_id)
            success, message = await self._kick_chatroom_members(group_id, [member_id])
            if success:
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"✅ 成员 '{name} ({member_id})' 已被移出群聊。")]))
            else:
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"❌ 未能踢出成员 '{name}'。原因: {message}")]))
        except Exception as e:
            self.logger.error(f"处理踢人命令时发生错误: {e}\n{traceback.format_exc()}")

    async def _handle_kick_downline_command(self, ctx: EventContext, group_id: str, member_id: str):
        try:
            group_id = self._normalize_group_id(group_id)
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"正在查询成员 '{member_id}' 的完整关系网并准备批量踢出...")]))
            group_info = await self._fetch_group_details(group_id)
            if not group_info: return
            
            member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
            members_map = {m['user_name']: (m.get('nick_name') or m['user_name']) for m in member_list}
            
            if member_id not in members_map:
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"目标成员 '{member_id}' 不在本群。")]))
                return
            
            parent_map, children_map = self._build_invite_relationship(member_list)
            downstream_map = self._get_recursive_downstream(member_id, parent_map, children_map, members_map)
            if downstream_map is None: return

            to_kick = list(downstream_map.keys())
            to_kick.insert(0, member_id)

            if len(to_kick) <= 1:
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"成员 '{members_map.get(member_id, member_id)}' 没有可一同踢出的下级。")]))
                return

            names = [f"{members_map.get(wxid, wxid)} ({wxid})" for wxid in to_kick]
            kick_list_str = "\n - ".join(names)
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"⚠️ 高危操作警告 ⚠️\n即将踢出以下 {len(names)} 名成员：\n - {kick_list_str}\n\n操作将在5秒后执行，此操作不可逆！")]))
            await asyncio.sleep(5)

            success, message = await self._kick_chatroom_members(group_id, to_kick)
            if success:
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"✅ 操作完成：已成功踢出 {len(names)} 名成员。")]))
            else:
                await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"❌ 踢出操作失败。原因: {message}")]))
        except Exception as e:
            self.logger.error(f"处理踢关系网命令时发生错误: {e}\n{traceback.format_exc()}")
            
    async def _handle_help_command(self, ctx: EventContext, group_id: str):
        help_message = f"""==== GroupInsight 插件 ====
> By: 俊宏 | v1.1.0 

1️⃣ 生成邀请关系图
   {TRIGGER_KEYWORD}
   {TRIGGER_KEYWORD} <群ID>
   {TRIGGER_KEYWORD}到 <目标群ID>
   {TRIGGER_KEYWORD} <群ID> 到 <目标群ID>

2️⃣ 查询关系网络 
   {TRIGGER_KEYWORD_NETWORK} <成员ID>
   {TRIGGER_KEYWORD_NETWORK} <成员ID> 在 <数据源群ID>
   {TRIGGER_KEYWORD_NETWORK} <成员ID> 到 <目标群ID>
   {TRIGGER_KEYWORD_NETWORK} <成员ID> 在 <源群ID> 到 <目标群ID>

3️⃣ 踢出指定成员
   {TRIGGER_KEYWORD_KICK_MEMBER} <成员ID>

4️⃣ 踢出关系网 (⚠️高危)
   {TRIGGER_KEYWORD_KICK_DOWNLINE} <成员ID>
"""
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(help_message)]))
    
    # ... (其他辅助函数 _get_member_display_name, _fetch_group_details,等保持不变) ...
    def _get_member_display_name(self, member_list: List[Dict[str, Any]], wxid: str) -> str:
        for member in member_list:
            if member and member.get('user_name') == wxid:
                return self._clean_whitespace_and_special_chars(member.get('nick_name', '') or wxid)
        return wxid

    async def _fetch_group_details(self, group_id: str) -> Optional[Dict[str, Any]]:
        normalized_id = self._normalize_group_id(group_id)
        if normalized_id in self.group_info_cache:
            timestamp, data = self.group_info_cache[normalized_id]
            if time.time() - timestamp < CACHE_DURATION:
                return data

        url = f"{self.API_BASE_URL}/group/GetChatRoomInfo?key={self.API_KEY}"
        payload = {"ChatRoomWxIdList": [normalized_id]}
        loop = asyncio.get_running_loop()
        
        try:
            response = await loop.run_in_executor(
                None, 
                lambda: requests.post(url, json=payload, timeout=15)
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("Code") == 200 and data.get("Data", {}).get("contactCount", 0) > 0:
                group_data = data["Data"]["contactList"][0]
                self.group_info_cache[normalized_id] = (time.time(), group_data)
                return group_data
            
            self.logger.error(f"API 请求群组 {normalized_id} 返回错误: {data}")
            return None
        except Exception as e:
            self.logger.error(f"获取群 {normalized_id} 信息时出错: {e}")
            return None
    
    async def _kick_chatroom_members(self, group_id: str, member_ids: List[str]) -> Tuple[bool, str]:
        url = f"{self.API_BASE_URL}/group/SendDelDelChatRoomMember?key={self.API_KEY}"
        payload = {
            "ChatRoomName": self._normalize_group_id(group_id),
            "UserList": member_ids
        }
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, timeout=20)
            )
            response.raise_for_status()
            data = response.json()
            if data.get("Code") == 200:
                return True, "操作成功"
            else:
                error_message = data.get("Text", "未知API错误")
                return False, error_message
        except Exception as e:
            self.logger.error(f"调用踢人API时出错: {e}")
            return False, "网络请求失败或API异常"

    def _build_invite_relationship(self, member_list: List[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
        parent_map, children_map = {}, defaultdict(list)
        for member in member_list:
            if not isinstance(member, dict): continue
            
            inviter = member.get('unknow')
            invitee = member.get('user_name')

            if isinstance(inviter, str) and inviter.strip() and isinstance(invitee, str) and invitee.strip():
                parent_map[invitee] = inviter
                children_map[inviter].append(invitee)
                
        return parent_map, children_map
    
    def _get_member_direct_network(self, member_id: str, parent_map: Dict, children_map: Dict, members_map: Dict) -> Optional[Tuple[List[str], Dict[str, str]]]:
        try:
            upstream_path, current, visited = [], member_id, {member_id}
            max_depth = 50
            for _ in range(max_depth):
                inviter = parent_map.get(current)
                if not inviter: break
                if inviter in visited:
                    upstream_path.insert(0, f"⚠️循环于: {inviter}")
                    break
                display_name = members_map.get(inviter, f"已退群({inviter[:12]}...)")
                upstream_path.insert(0, f"{display_name} ({inviter})")
                visited.add(inviter)
                current = inviter
            else:
                 upstream_path.insert(0, "⚠️路径过深")

            downstream_map = {}
            for child in children_map.get(member_id, []):
                if child in members_map:
                    downstream_map[child] = members_map[child]
            
            return upstream_path, downstream_map
        except Exception as e:
            self.logger.error(f"查询直接关系网时发生异常: {e}")
            return None

    def _get_recursive_downstream(self, member_id: str, parent_map: Dict, children_map: Dict, members_map: Dict) -> Optional[Dict[str, str]]:
        try:
            downstream_map, queue, visited = {}, deque([member_id]), {member_id}
            max_nodes = 500
            count = 0
            while queue:
                if count > max_nodes: break
                current_node = queue.popleft()
                count += 1
                for child in children_map.get(current_node, []):
                    if child in visited: continue
                    if parent_map.get(child) != current_node: continue
                    if child in members_map:
                        visited.add(child)
                        downstream_map[child] = members_map[child]
                        queue.append(child)
            return downstream_map
        except Exception as e:
            self.logger.error(f"递归查询下游时发生异常: {e}")
            return None
    
    async def _generate_invite_tree_image(self, member_list: list, filename_id: str, group_name: str, ctx: EventContext) -> Optional[str]:
        loop = asyncio.get_running_loop()
        try:
            render_task = loop.run_in_executor(None, self._render_graph, member_list, filename_id, group_name)
            return await asyncio.wait_for(render_task, timeout=RENDER_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger.error(f"渲染图片超时（超过 {RENDER_TIMEOUT} 秒）")
            await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain("生成关系图超时，可能群成员过多或服务器负载过高。")]))
            return None
        except Exception as e:
            self.logger.error(f"渲染 Graphviz 图片时发生未知异常: {e}")
            return None

    def _render_graph(self, member_list: list, group_id: str, group_name: str) -> Optional[str]:
        if graphviz is None:
            return None

        try:
            cleaned_member_list = [m for m in member_list if m and isinstance(m, dict) and m.get('user_name')]
            if not cleaned_member_list:
                self.logger.warning(f"渲染中止：群 {group_id} 清理后的成员列表为空。")
                return None
            
            if len(cleaned_member_list) > MAX_NODES_TO_RENDER:
                self.logger.warning(f"渲染中止：群成员数量 ({len(cleaned_member_list)}) 超过最大渲染限制 ({MAX_NODES_TO_RENDER})。")
                return None

            members_map = {m['user_name']: self._clean_whitespace_and_special_chars(m.get('nick_name', '') or m['user_name']) for m in cleaned_member_list}
            all_in_group = set(members_map.keys())
            parent_map, children_map = self._build_invite_relationship(cleaned_member_list)
            
            all_ids_in_graph = all_in_group.union(children_map.keys()).union(parent_map.values())
            if not all_ids_in_graph:
                self.logger.warning(f"无法为群 {group_id} 确定任何图节点，渲染中止。")
                return None

            # --- 【重构】引擎选择与图属性设置 ---
            engine = 'dot'
            graph_attrs = GRAPH_ATTR_DOT.copy()
            max_inviter = None
            if children_map:
                max_inviter = max(children_map, key=lambda k: len(children_map[k]))
                max_invite_count = len(children_map[max_inviter])
                group_size = len(cleaned_member_list)
                
                if (max_invite_count >= STAR_GRAPH_THRESHOLD_ABSOLUTE and 
                   (max_invite_count / group_size) >= STAR_GRAPH_THRESHOLD_RATIO):
                    engine = 'twopi'
                    graph_attrs = GRAPH_ATTR_TWOPI.copy()
                    graph_attrs['root'] = max_inviter
            
            self.logger.info(f"为群 {group_id} 选择的渲染引擎: {engine}")
            
            # --- 【重构】创建扁平、稳健的图 ---
            dot = graphviz.Digraph(f'invite_tree_{group_id}', engine=engine)
            
            # 设置图、节点、边的全局属性
            dot.attr('graph', **graph_attrs)
            dot.attr('node', **NODE_ATTR)
            dot.attr('edge', **EDGE_ATTR)

            # 使用 graph 的 label 属性设置标题，这是最稳健的方式
            tz_utc_8 = timezone(timedelta(hours=8), name='Asia/Shanghai')
            current_date = datetime.now(tz_utc_8).strftime("%Y年%m月%d日 %H:%M:%S")
            title_text = f"{html.escape(group_name)} 的群成员邀请关系图表\n{current_date} (UTC+8)"
            dot.attr(label=title_text, labelloc='t', fontsize='20', fontname='WenQuanYi Zen Hei')

            # --- 节点与边的创建 ---
            root_nodes_set = {uid for uid in all_in_group if uid not in parent_map}
            for wxid in sorted(list(all_ids_in_graph)):
                is_leaver = wxid not in all_in_group
                nickname = members_map.get(wxid, '')
                
                label_parts = []
                color = "grey88" # 默认颜色
                if is_leaver:
                    label_parts.append("已退群")
                else:
                    label_parts.append(html.escape(nickname or " "))
                    color = "lightblue"
                    if wxid in root_nodes_set:
                        color = "lightgreen"

                label_parts.append(wxid)
                # 使用 r"\n" 来确保在 DOT 源码中是字面上的换行符
                label = r"\n".join(label_parts)
                dot.node(wxid, label=label, fillcolor=color)
                
            for inviter, invitee_list in children_map.items():
                for invitee in invitee_list:
                    if inviter in all_ids_in_graph and invitee in all_ids_in_graph:
                        dot.edge(inviter, invitee)
            
            # --- 【重构】调试与渲染 ---
            self.logger.debug(f"为群 {group_id} 生成的 DOT 源代码:\n{dot.source}")
            
            output_path = os.path.join(os.getcwd(), f'invite_tree_{group_id}_{int(time.time() * 1000)}')
            
            rendered_path = dot.render(output_path, format=IMAGE_FORMAT, view=False, cleanup=True)
            if not os.path.exists(rendered_path):
                self.logger.error("Graphviz 渲染后文件不存在！请检查系统是否已安装Graphviz及相关字体，并查看Graphviz的错误输出。")
                return None
            
            return rendered_path
        
        except Exception as e:
            self.logger.error(f"渲染 Graphviz 图形时发生严重错误: {e}\n{traceback.format_exc()}")
            if 'dot' in locals() and hasattr(dot, 'source'):
                self.logger.error(f"导致错误的 DOT 源代码是:\n{dot.source}")
            return None
