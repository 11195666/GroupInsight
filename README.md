# GroupInsight (群洞察) 插件

> By: 俊宏
> Version: 1.1.0

<p align="center">
  <img src="https://img.shields.io/badge/LangBot-%3E%3D%204.0-blue?style=for-the-badge" alt="LangBot Version">
  <img src="https://img.shields.io/badge/Adapter-WeChatPad-green?style=for-the-badge" alt="Adapter">
</p>

一款为 LangBot 设计的、依赖 **WeChatPad 适配器** 的微信群组管理与分析插件。它能将复杂的群成员邀请关系可视化，深度分析任意成员的社交网络，并提供精准的成员管理工具，帮助管理员轻松维护群聊生态。

---

### ✨ 核心功能

*   **📈 可视化邀请关系图**: 一键生成高清、美观的群成员邀请关系图，自动识别并标记已退群的邀请人。
*   **🌐 深度关系网络分析**: 查询任一成员完整的“上下线”关系，向上追溯所有上级，向下展开所有下级。
*   **🛡️ 精准成员管理**:
    *   **定点移除**: 根据 `wxid` 精准踢出任意群成员。
    *   **关系网清理 (高危)**: 一键踢出目标成员及其邀请的所有下级成员。
*   **💬 友好交互体验**: 所有功能均通过简单的中文指令触发，并内置详细的 `#帮助` 指令。

---

### 🚀 安装与配置

#### ⚠️ 系统要求
本插件**仅支持 LangBot v4.0 及以上版本**。请勿在旧版系统上安装。

#### 1. 下载插件
将整个 `GroupInsight` 文件夹放入你的 LangBot 项目的 `plugins` 目录下。

#### 2. 安装依赖
在你的终端中，进入项目根目录，然后运行以下命令安装所需库：
```bash
pip install -r plugins/GroupInsight/requirements.txt
```

#### 3. 安装 Graphviz (重要)
本插件的图形功能依赖于 Graphviz 软件。你**必须**在运行机器的操作系统上安装它：
*   **Ubuntu/Debian**: `sudo apt-get update && sudo apt-get install graphviz`
*   **CentOS/RHEL**: `sudo yum install graphviz`
*   **macOS (使用 Homebrew)**: `brew install graphviz`

#### 4. 配置插件 (v4.0 新方式)
本插件使用 LangBot 4.0 的 WebUI 进行配置，无需手动修改任何代码文件。

1.  **重启 LangBot** 以加载新插件。
2.  进入 **LangBot WebUI** 的 **插件管理** 页面。
3.  找到 **GroupInsight (群洞察)** 插件，点击右侧的 **配置** 按钮。
4.  在弹出的配置窗口中，填入以下必填信息：
    *   **API 基础 URL**: 你的 WeChatPadPro API 地址。
    *   **API Key**: 你的 WeChatPadPro API 密钥。
    *   **管理员用户ID列表**: 你的微信号 `wxid`或者群ID，例如`xxxxxx@chatroom`，只有这里的用户才能使用插件功能。
5.  点击 **保存**，插件即可使用。

<br>

---

### 📝 指令用法

| 功能 | 指令 | 示例 |
| :--- | :--- | :--- |
| **显示帮助** | `#帮助` | `#帮助` |
| **生成关系图** | `#邀请关系` <br> `#邀请关系 <群ID>` <br> `#邀请关系到 <群ID>` <br> `#邀请关系 <源群ID> 到 <目标群ID>` | `#邀请关系` (当前群) <br> `#邀请关系 123@chatroom` <br> `#邀请关系到 456@chatroom` <br> `#邀请关系 123@chatroom 到 456@chatroom` |
| **查询关系网** | `#查关系网 <成员ID>` <br> `#查关系网 <成员ID> 在 <源群ID>` <br> `#查关系网 <成员ID> 到 <目标群ID>` <br> `#查关系网 <成员ID> 在 <源群ID> 到 <目标群ID>` | `#查关系网 wxid_xxx` <br> `#查关系网 wxid_xxx 在 123@chatroom` <br> `#查关系网 wxid_xxx 到 456@chatroom` <br> `#查关系网 wxid_xxx 在 123@chatroom 到 456@chatroom` |
| **踢出成员** | `#踢人 <成员ID>` | `#踢人 wxid_xxxxxxxx` |
| **踢出关系网 (高危)** | `#踢关系网 <成员ID>` | `#踢关系网 wxid_xxxxxxxx` |
---

### ⚠️ 重要：使用前必读

1.  **依赖适配器**: 本插件的功能强依赖 **WeChatPad** 适配器返回的邀请人信息，其他适配器可能无法使用。

2.  **机器人权限**:
    *   **踢人功能**：机器人必须是目标群聊的**群主或管理员**。
    *   **数据获取**：机器人必须是目标群聊的成员。

3.  **高危操作警告**:
    *   `#踢关系网` 指令是一个**极度危险**的批量操作功能，它会**永久性地**将目标成员及其所有下级从群聊中移除。
    *   **此操作不可逆！** 执行前会有一个简短的倒计时，请务必确认目标 `wxid` 是否正确，避免误操作造成无法挽回的损失。

4.  **数据准确性**: 插件数据依赖于 API 返回结果。对于通过群二维码等方式入群的成员，API 可能无法提供邀请人信息，这些成员在关系图中会显示为“根节点”。
5.  **待修复问题**：目前对于简单的星支点邀请关系无法正确渲染图片，后期再做修复。
