# plugins/GroupInsight/main.py

import asyncio
import base64
import os
import requests
import traceback
import logging
import time
import html
import re
import sys
from collections import defaultdict, deque
from typing import Dict, Any, Optional, List, Tuple

from pkg.plugin.context import register, handler, BasePlugin, APIHost, EventContext
from pkg.plugin.events import GroupNormalMessageReceived, GroupMessageReceived
from pkg.platform.types import MessageChain, Plain, Image

try:
    import graphviz
except ImportError:
    graphviz = None
    print("警告: [GroupInsight] 未找到 'graphviz' 库。请运行 'pip install -r plugins/GroupInsight/requirements.txt' 安装。图形生成功能将不可用。")

@register(name="GroupInsight", description="强大的群组管理与关系分析插件", version="1.1.0", author="俊宏")
class GroupInsightPlugin(BasePlugin):
    
    host: APIHost
    logger: logging.Logger
    
    # --- 指令关键词定义 ---
    TRIGGER_KEYWORD = "#邀请关系"
    TRIGGER_KEYWORD_NETWORK = "#查关系网"
    TRIGGER_KEYWORD_KICK_MEMBER = "#踢人"
    TRIGGER_KEYWORD_KICK_DOWNLINE = "#踢关系网"
    TRIGGER_KEYWORD_HELP = "#帮助"

    # --- Graphviz 静态配置 ---
    IMAGE_FORMAT = 'png'
    GRAPH_ATTR = {'rankdir': 'TB', 'dpi': '300'}
    NODE_ATTR = {'style': 'rounded,filled', 'fillcolor': 'lightblue'}
    EDGE_ATTR = {'arrowsize': '0.7'}
    
    # --- 正则表达式预编译模板 ---
    GROUP_ID_REGEX = r'[\w\-\.]+(?:@chatroom)?'
    MEMBER_ID_REGEX = r'[\w\-\.]+'

    def __init__(self, host: APIHost):
        self.host = host
        self.logger = logging.getLogger("GroupInsightPlugin")
        
        # --- 从 self.config 读取配置 (由 manifest.yaml 定义) ---
        self.API_BASE_URL = self.config.get('api_base_url', '').strip()
        self.API_KEY = self.config.get('api_key', '').strip()
        self.ADMIN_USER_IDS = self.config.get('admin_user_ids', [])
        
        # --- 预编译指令解析的正则表达式 ---
        self.PATTERN_INVITE_FULL = re.compile(
            r'^\s*' + re.escape(self.TRIGGER_KEYWORD) + r'\s+' +
            r'(?P<fetch_id>' + self.GROUP_ID_REGEX + r')\s+到\s+' +
            r'(?P<send_id>' + self.GROUP_ID_REGEX + r')\s*$'
        )
        self.PATTERN_INVITE_FETCH_ONLY = re.compile(
            r'^\s*' + re.escape(self.TRIGGER_KEYWORD) + r'\s+' +
            r'(?P<fetch_id>' + self.GROUP_ID_REGEX + r')\s*$'
        )
        self.PATTERN_INVITE_SEND_TO_ONLY = re.compile(
            r'^\s*' + re.escape(self.TRIGGER_KEYWORD) + r'到\s+' +
            r'(?P<send_id>' + self.GROUP_ID_REGEX + r')\s*$'
        )
        self.PATTERN_INVITE_DEFAULT = re.compile(r'^\s*' + re.escape(self.TRIGGER_KEYWORD) + r'\s*$')
        self.PATTERN_NETWORK = re.compile(
            r'^\s*' + re.escape(self.TRIGGER_KEYWORD_NETWORK) + r'\s+(?P<member_id>' + self.MEMBER_ID_REGEX + r')\s*$'
        )
        self.PATTERN_KICK_MEMBER = re.compile(
            r'^\s*' + re.escape(self.TRIGGER_KEYWORD_KICK_MEMBER) + r'\s+(?P<member_id>' + self.MEMBER_ID_REGEX + r')\s*$'
        )
        self.PATTERN_KICK_DOWNLINE = re.compile(
            r'^\s*' + re.escape(self.TRIGGER_KEYWORD_KICK_DOWNLINE) + r'\s+(?P<member_id>' + self.MEMBER_ID_REGEX + r')\s*$'
        )
        
        self.logger.info("GroupInsight 插件初始化完成 (v1.1.0)。")
        if not self.API_BASE_URL or not self.API_KEY:
            self.logger.error("API URL 或 API Key 未配置！请在 WebUI 插件页面配置 GroupInsight 插件。")
        if not self.ADMIN_USER_IDS:
            self.logger.warning("管理员列表为空，没有人能使用此插件！请在 WebUI 插件页面配置。")
        if graphviz is None:
            self.logger.warning("Graphviz 系统依赖未找到，图形渲染功能将不可用。请确保已在系统层面安装 Graphviz。")

    def _clean_whitespace_and_special_chars(self, text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        text = re.sub(r'[\u200B-\u200F\u202F\u205F\uFEFF\u00A0\u00AD\u2800]', '', text)
        text = text.replace('　', ' ')
        full_to_half_map = str.maketrans(
            '【】（）《》“”‘’：；，。？！——……',
            '[]()<>"\'\':;,.?!-...'
        )
        text = text.translate(full_to_half_map)
        text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)
        return text.strip()

    @handler(GroupMessageReceived)
    @handler(GroupNormalMessageReceived)
    async def group_message_handler(self, ctx: EventContext):
        try:
            raw_msg = ctx.event.query.message_chain.get_plain_text().strip()
        except AttributeError:
            try: 
                raw_msg = ctx.event.text_message.strip()
            except Exception: 
                self.logger.warning("无法从事件中解析消息文本。")
                return
        
        all_triggers = [
            self.TRIGGER_KEYWORD, self.TRIGGER_KEYWORD_NETWORK, self.TRIGGER_KEYWORD_KICK_MEMBER,
            self.TRIGGER_KEYWORD_KICK_DOWNLINE, self.TRIGGER_KEYWORD_HELP
        ]
        if not any(raw_msg.startswith(trigger) for trigger in all_triggers):
            return

        sender_id = str(ctx.event.sender_id)
        current_group_id = str(ctx.event.query.launcher_id)
        
        self.logger.info(f"接收到指令: '{raw_msg}' 来自用户 [{sender_id}] @ 群 [{current_group_id}]。")
        
        if not self.ADMIN_USER_IDS:
             self.logger.warning(f"插件未配置任何管理员，指令被忽略。")
             return
             
        if sender_id not in self.ADMIN_USER_IDS:
            self.logger.warning(f"权限检查失败: 用户 [{sender_id}] 不在管理员列表 {self.ADMIN_USER_IDS} 中。")
            await ctx.reply(MessageChain([Plain("抱歉，你没有权限使用此功能。")]))
            ctx.prevent_default()
            return
        
        self.logger.info(f"权限检查成功，用户 [{sender_id}] 是管理员。")
        ctx.prevent_default()
        ctx.prevent_postorder()

        if not self.API_BASE_URL or not self.API_KEY:
            self.logger.error("API URL 或 API Key 未配置，无法执行指令。")
            await ctx.reply(MessageChain([Plain("插件核心配置缺失，请联系机器人管理员检查后台日志。")]))
            return

        if raw_msg.strip() == self.TRIGGER_KEYWORD_HELP:
            await self._handle_help_command(ctx, current_group_id)
        elif raw_msg.startswith(self.TRIGGER_KEYWORD):
            await self._parse_and_handle_invite_tree(ctx, raw_msg, current_group_id)
        elif raw_msg.startswith(self.TRIGGER_KEYWORD_NETWORK):
            await self._parse_and_handle_network(ctx, raw_msg, current_group_id)
        elif raw_msg.startswith(self.TRIGGER_KEYWORD_KICK_MEMBER):
            await self._parse_and_handle_kick_member(ctx, raw_msg, current_group_id)
        elif raw_msg.startswith(self.TRIGGER_KEYWORD_KICK_DOWNLINE):
            await self._parse_and_handle_kick_downline(ctx, raw_msg, current_group_id)
        else:
            await self._send_error_message(ctx, current_group_id, raw_msg, "无法识别的指令")

    async def _parse_and_handle_invite_tree(self, ctx: EventContext, raw_msg: str, current_group_id: str):
        if (match := self.PATTERN_INVITE_FULL.match(raw_msg)):
            fetch_id, send_id = match.groups()
            await self._handle_invite_tree_command(ctx, fetch_id, send_id)
        elif (match := self.PATTERN_INVITE_FETCH_ONLY.match(raw_msg)):
            await self._handle_invite_tree_command(ctx, match.group('fetch_id'), current_group_id)
        elif (match := self.PATTERN_INVITE_SEND_TO_ONLY.match(raw_msg)):
            await self._handle_invite_tree_command(ctx, current_group_id, match.group('send_id'))
        elif self.PATTERN_INVITE_DEFAULT.match(raw_msg):
            await self._handle_invite_tree_command(ctx, current_group_id, current_group_id)
        else:
            await self._send_error_message(ctx, current_group_id, raw_msg, "指令格式错误")

    async def _parse_and_handle_network(self, ctx: EventContext, raw_msg: str, current_group_id: str):
        if (match := self.PATTERN_NETWORK.match(raw_msg)):
            await self._handle_network_command(ctx, current_group_id, match.group('member_id'))
        else:
            await self._send_error_message(ctx, current_group_id, raw_msg, f"正确格式: {self.TRIGGER_KEYWORD_NETWORK} wxid_xxxx")

    async def _parse_and_handle_kick_member(self, ctx: EventContext, raw_msg: str, current_group_id: str):
        if (match := self.PATTERN_KICK_MEMBER.match(raw_msg)):
            await self._handle_kick_member_command(ctx, current_group_id, match.group('member_id'))
        else:
            await self._send_error_message(ctx, current_group_id, raw_msg, f"正确格式: {self.TRIGGER_KEYWORD_KICK_MEMBER} wxid_xxxx")

    async def _parse_and_handle_kick_downline(self, ctx: EventContext, raw_msg: str, current_group_id: str):
        if (match := self.PATTERN_KICK_DOWNLINE.match(raw_msg)):
            await self._handle_kick_downline_command(ctx, current_group_id, match.group('member_id'))
        else:
            await self._send_error_message(ctx, current_group_id, raw_msg, f"正确格式: {self.TRIGGER_KEYWORD_KICK_DOWNLINE} wxid_xxxx")

    async def _send_error_message(self, ctx: EventContext, group_id: str, raw_msg: str, reason: str):
        self.logger.warning(f"{reason}: '{raw_msg}'")
        await self.host.send_active_message(
            ctx.event.query.adapter, "group", group_id, 
            MessageChain([Plain(f"{reason}。请检查指令或输入 {self.TRIGGER_KEYWORD_HELP} 获取帮助。")])
        )

    async def _handle_invite_tree_command(self, ctx: EventContext, fetch_group_id: str, send_group_id: str):
        image_path = None
        try:
            await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain(f"正在获取群 '{fetch_group_id}' 的成员邀请关系，请稍候...")]))
            
            if graphviz is None:
                await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain("错误：Graphviz 未安装，无法生成关系图。")]))
                return

            group_info = await self._fetch_group_details(fetch_group_id)
            if not group_info:
                await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain(f"获取群 '{fetch_group_id}' 信息失败。")]))
                return
            
            group_name = group_info.get('nickName', {}).get('str', fetch_group_id)
            member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])

            if not member_list:
                await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain(f"群 '{fetch_group_id}' 成员列表为空。")]))
                return

            self.logger.info(f"成功获取群 '{group_name}' ({len(member_list)}名成员)，开始渲染...")
            
            filename_id = fetch_group_id.replace('@chatroom', '_')
            image_path = await self._generate_invite_tree_image(member_list, filename_id, group_name)
            
            if not image_path:
                await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain("生成关系图失败，请检查后台日志。")]))
                return

            with open(image_path, 'rb') as f:
                img_base64 = base64.b64encode(f.read()).decode()
            
            await self.host.send_active_message(ctx.event.query.adapter, "group", send_group_id, MessageChain([Image(base64=img_base64)]))
            self.logger.info(f"已成功将邀请关系图发送到群 '{send_group_id}'。")

        except Exception as e:
            self.logger.error(f"处理邀请关系图命令时发生内部错误: {e}\n{traceback.format_exc()}")
            await self.host.send_active_message(ctx.event.query.adapter, "group", ctx.event.query.launcher_id, MessageChain([Plain("发生内部错误，请查看后台日志。")]))
        finally:
            if image_path and os.path.exists(image_path):
                try: 
                    os.remove(image_path)
                    self.logger.info(f"已清理临时图片文件: {image_path}")
                except OSError as e: 
                    self.logger.error(f"清理临时文件 {image_path} 失败: {e}")

    async def _handle_network_command(self, ctx: EventContext, group_id: str, member_id: str):
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"正在查询成员 '{member_id}' 的邀请关系网络...")]))
        
        group_info = await self._fetch_group_details(group_id)
        if not group_info:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"获取群 '{group_id}' 信息失败。")]))
            return
        
        member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
        if not member_list:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"群 '{group_id}' 成员列表为空。")]))
            return

        if not any(m['user_name'] == member_id for m in member_list):
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"成员 '{member_id}' 不在本群中。")]))
            return

        upstream, downstream = self._get_member_network(member_list, member_id)
        
        parts = [f"成员 '{self._get_member_display_name(member_list, member_id)} ({member_id})' 的邀请关系网络如下：\n"]
        parts.append("\n--- 上级邀请链 ---\n")
        parts.append(" -> ".join(upstream) + "\n" if upstream else "该成员是顶级邀请人或其上级已退群。\n")
        parts.append(f"\n--- 下级被邀请人 (共 {len(downstream)} 位) ---\n")
        if downstream:
            for wxid, nickname in downstream.items():
                parts.append(f"- {nickname} ({wxid})\n")
        else:
            parts.append("该成员没有邀请任何下级成员。\n")
        
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain("".join(parts))]))
        self.logger.info(f"已将成员 '{member_id}' 的邀请关系网络发送到群 '{group_id}'。")

    async def _handle_kick_member_command(self, ctx: EventContext, group_id: str, member_id: str):
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"正在尝试将成员 '{member_id}' 从群中踢出...")]))
        
        group_info = await self._fetch_group_details(group_id)
        if not group_info:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"获取群信息失败，无法踢出。")]))
            return
        
        member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
        if not any(m['user_name'] == member_id for m in member_list):
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"成员 '{member_id}' 不在本群。")]))
            return
        
        name = self._get_member_display_name(member_list, member_id)
        success, kicked = await self._kick_chatroom_members(group_id, [member_id])
        
        if success and member_id in kicked:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"✅ 成员 '{name} ({member_id})' 已成功踢出。")]))
            self.logger.info(f"成员 '{member_id}' 已从群 '{group_id}' 踢出。")
        else:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"❌ 未能踢出成员 '{name}'。请检查机器人权限。")]))
            self.logger.error(f"未能将成员 '{member_id}' 从群 '{group_id}' 踢出。")

    async def _handle_kick_downline_command(self, ctx: EventContext, group_id: str, member_id: str):
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"正在查询成员 '{member_id}' 及其所有下级...")]))
        
        group_info = await self._fetch_group_details(group_id)
        if not group_info:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain("获取群信息失败。")]))
            return
        
        member_list = group_info.get('newChatroomData', {}).get('chatroom_member_list', [])
        if not any(m['user_name'] == member_id for m in member_list):
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"目标成员 '{member_id}' 不在本群。")]))
            return

        members_map = {m['user_name']: (m.get('nick_name') or m['user_name']) for m in member_list}
        _, downstream_map = self._get_member_network(member_list, member_id)
        
        to_kick = list(downstream_map.keys())
        to_kick.insert(0, member_id)

        if len(to_kick) <= 1:
             await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"成员 '{members_map.get(member_id, member_id)}' 没有可一同踢出的下级。")]))
             return
        
        names = [f"{members_map.get(wxid, wxid)} ({wxid})" for wxid in to_kick]
        kick_list_str = "\n - ".join(names)
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"⚠️ 高危操作警告 ⚠️\n即将踢出以下 {len(names)} 名成员：\n - {kick_list_str}\n\n操作将在5秒后执行，此操作不可逆！")]))
        await asyncio.sleep(5)

        success, kicked = await self._kick_chatroom_members(group_id, to_kick)
        
        if success and kicked:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(f"✅ 操作完成：已成功踢出 {len(kicked)} 名成员。")]))
            self.logger.info(f"已成功踢出群 '{group_id}' 中的成员及其下级：{', '.join(kicked)}。")
        else:
            await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain("❌ 踢出操作失败。请检查机器人权限。")]))
            self.logger.error(f"未能将成员 '{member_id}' 及其下级踢出。")

    async def _handle_help_command(self, ctx: EventContext, group_id: str):
        help_message = f"""====== GroupInsight 插件帮助 ======
> By: 俊宏 | v1.1.0

1️⃣ 生成邀请关系图
   可视化群成员的邀请链条。
   `{self.TRIGGER_KEYWORD}`
   `{self.TRIGGER_KEYWORD} <群ID>`
   `{self.TRIGGER_KEYWORD}到 <目标群ID>`
   `{self.TRIGGER_KEYWORD} <源群ID> 到 <目标群ID>`

2️⃣ 查询关系网络
   分析指定成员的完整上下级。
   `{self.TRIGGER_KEYWORD_NETWORK} <成员ID>`

3️⃣ 踢出指定成员 (管理员)
   将某人移出群聊。
   `{self.TRIGGER_KEYWORD_KICK_MEMBER} <成员ID>`

4️⃣ 踢出关系网 (⚠️高危)
   踢出某人及其所有下级。
   `{self.TRIGGER_KEYWORD_KICK_DOWNLINE} <成员ID>`

5️⃣ 显示本帮助
   `{self.TRIGGER_KEYWORD_HELP}`

💡 小提示: <群ID> 和 <成员ID> 可以在关系图中找到。
"""
        await self.host.send_active_message(ctx.event.query.adapter, "group", group_id, MessageChain([Plain(help_message)]))
        self.logger.info(f"已发送插件帮助信息到群 '{group_id}'。")

    def _get_member_display_name(self, member_list: List[Dict[str, Any]], wxid: str) -> str:
        for member in member_list:
            if member.get('user_name') == wxid:
                return member.get('nick_name') or wxid
        return wxid

    async def _fetch_group_details(self, group_id: str) -> Optional[Dict[str, Any]]:
        url = f"{self.API_BASE_URL}/group/GetChatRoomInfo?key={self.API_KEY}"
        payload = {"ChatRoomWxIdList": [group_id]}
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(None, lambda: requests.post(url, json=payload, timeout=20))
            response.raise_for_status()
            data = response.json()
            if data.get("Code") == 200 and data.get("Data", {}).get("contactCount", 0) > 0:
                return data["Data"]["contactList"][0]
            self.logger.error(f"API 请求群组 {group_id} 返回错误: {data}")
            return None
        except requests.RequestException as e:
            self.logger.error(f"请求 API {url} 失败: {e}")
            return None

    def _build_invite_relationship(self, member_list: List[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
        parent_map, children_map = {}, defaultdict(list)
        for member in member_list:
            inviter, invitee = member.get('unknow'), member.get('user_name')
            if inviter and invitee:
                parent_map[invitee] = inviter
                children_map[inviter].append(invitee)
        return parent_map, children_map

    def _get_member_network(self, member_list: List[Dict[str, Any]], member_id: str) -> Tuple[List[str], Dict[str, str]]:
        members_map = {m['user_name']: (m.get('nick_name') or m['user_name']) for m in member_list}
        parent_map, children_map = self._build_invite_relationship(member_list)
        
        upstream_path, current, path_set = [], member_id, {member_id}
        while current in parent_map:
            inviter = parent_map[current]
            if inviter in path_set:
                self.logger.warning(f"检测到上级邀请链循环: {inviter}")
                break
            upstream_path.insert(0, f"{members_map.get(inviter, inviter)} ({inviter})") 
            path_set.add(inviter)
            current = inviter
            if current not in members_map: break
        
        downstream_map, queue, visited = {}, deque([member_id]), {member_id}
        while queue:
            node = queue.popleft()
            for child in children_map.get(node, []):
                if child not in visited and child in members_map:
                    visited.add(child)
                    downstream_map[child] = members_map[child]
                    queue.append(child)
        return upstream_path, downstream_map

    async def _kick_chatroom_members(self, group_id: str, user_list: List[str]) -> Tuple[bool, List[str]]:
        if not user_list: return False, []
        url = f"{self.API_BASE_URL}/group/SendDelDelChatRoomMember?key={self.API_KEY}"
        payload = {"ChatRoomName": group_id, "UserList": user_list}
        self.logger.info(f"发送踢人请求 -> 群: {group_id}, 成员: {user_list}")
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(None, lambda: requests.post(url, json=payload, timeout=20))
            response.raise_for_status()
            data = response.json()
            if data.get("Code") == 200 and data.get("Data", {}).get("baseResponse", {}).get("ret") == 0:
                kicked = [m.get('memberName', {}).get('str') for m in data.get("Data", {}).get("memberList", []) if m.get('memberName', {}).get('str')]
                self.logger.info(f"API 成功踢出成员: {kicked}")
                return True, kicked
            self.logger.error(f"踢人 API 返回错误: {data}")
            return False, []
        except requests.RequestException as e:
            self.logger.error(f"请求踢人 API {url} 失败: {e}")
            return False, []

    async def _generate_invite_tree_image(self, member_list: list, filename_id: str, group_name: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._render_graph, member_list, filename_id, group_name)
        except Exception as e:
            self.logger.error(f"渲染 Graphviz 图片时出错: {e}\n{traceback.format_exc()}")
            return None

    def _render_graph(self, member_list: list, group_id: str, group_name: str) -> Optional[str]:
        if graphviz is None: return None

        members_map = {m['user_name']: (m.get('nick_name') or m['user_name']) for m in member_list}
        invite_tree, all_invitees = defaultdict(list), set()
        for member in member_list:
            inviter, invitee = member.get('unknow'), member.get('user_name')
            if inviter and invitee:
                invite_tree[inviter].append(invitee)
                all_invitees.add(invitee)

        font_fallback = "WenQuanYi Zen Hei, Sarasa Gothic SC, Noto Sans CJK SC, Microsoft YaHei"
        dot = graphviz.Digraph(f'invite_tree_{group_id}', engine='dot')
        
        graph_attrs = self.GRAPH_ATTR.copy()
        graph_attrs.update({'fontname': font_fallback, 'pad': '1.0', 'splines': 'true', 'overlap': 'false', 'nodesep': '0.8', 'center': 'true', 'ranksep': '1.2'})
        dot.attr('graph', **graph_attrs)
        dot.attr('node', **self.NODE_ATTR, fontname=font_fallback, shape='plain')
        dot.attr('edge', **self.EDGE_ATTR, fontname=font_fallback)

        safe_group_name = html.escape(self._clean_whitespace_and_special_chars(group_name))
        safe_group_id = html.escape(self._clean_whitespace_and_special_chars(group_id))
        title = f"""<
        <TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="8">
            <TR><TD><B><FONT POINT-SIZE="28">群聊邀请关系图</FONT></B></TD></TR>
            <TR><TD ALIGN="CENTER"><FONT POINT-SIZE="22">{safe_group_name}</FONT></TD></TR>
            <TR><TD ALIGN="CENTER"><FONT POINT-SIZE="14" FACE="Courier New">{safe_group_id}</FONT></TD></TR>
            <TR><TD><BR/></TD></TR>
        </TABLE>>"""
        dot.node('group_title_node', label=title, style='invis')
        
        all_in_group = set(members_map.keys())
        all_inviters = set(invite_tree.keys())
        all_nodes = all_in_group.union(all_inviters)

        for wxid in all_nodes:
            is_leaver = wxid not in all_in_group
            bg_color = "#E0E0E0" if is_leaver else "white"
            name = "<i>邀请人 (已退群)</i>" if is_leaver else (html.escape(self._clean_whitespace_and_special_chars(str(members_map.get(wxid, '')))) or ' ')
            label = f"""<
            <TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4" BGCOLOR="{bg_color}">
                <TR><TD ALIGN="CENTER">{name}</TD></TR>
                <TR><TD ALIGN="CENTER"><FONT POINT-SIZE="10" FACE="Courier New">{html.escape(wxid)}</FONT></TD></TR>
            </TABLE>>"""
            dot.node(wxid, label)
            
        for inviter, invitee_list in invite_tree.items():
            for invitee in invitee_list:
                if inviter in all_nodes and invitee in all_nodes:
                    dot.edge(inviter, invitee)
        
        root_nodes = all_in_group - all_invitees
        for node in root_nodes:
            dot.edge('group_title_node', node, style='invis', len='1.5')

        output_path = os.path.join(os.getcwd(), f'invite_tree_{group_id}_{int(time.time() * 1000)}')
        dot.encoding = 'utf-8'
        rendered_path = dot.render(output_path, format=self.IMAGE_FORMAT, view=False, cleanup=True)
        
        self.logger.info(f"图形成功渲染到: {rendered_path}")
        return rendered_path
