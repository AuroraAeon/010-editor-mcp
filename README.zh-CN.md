# 010 Editor MCP Bridge

[English](README.md) | [简体中文](README.zh-CN.md)

一个通过 **stdio** 运行的本地 MCP 服务，将二进制文件分析与现有的 010 Editor
会话接入同一组工具。独立文件引擎处理磁盘文件；编辑器桥接读取内存缓冲区，
包括尚未保存的修改。

需要 Python 3.11 及以上版本。编辑器功能已在 Windows 和 010 Editor 16.0.4 上验证。
详见[验证结果](docs/verification.md)。

## 支持的工具

| 工具 | 用途 |
|---|---|
| `server_status`、`editor_status` | 查看后端可用性、功能、资源限制与会话状态 |
| `binary_inspect` | 查看文件元信息、文件标识，并按需计算哈希 |
| `binary_read`、`binary_search`、`binary_diff` | 按范围读取字节、搜索含重叠匹配的字节模式、比较差异区间 |
| `binary_patch_preview`、`binary_patch_apply` | 核对原字节、预览替换，并将结果写入新文件 |
| `editor_context`、`editor_read` | 获取当前已命名缓冲区、未保存状态、选区与内存字节 |
| `editor_select` | 选中当前文档中的指定字节范围 |
| `editor_run_template` | 执行受信任的 `.bt` 模板，返回 XML 和解析后的结果 |
| `editor_patch_preview` | 核对内存中的原字节并预览替换 |
| `editor_patch_apply` | 返回 `CAPABILITY_UNSUPPORTED` |

当前版本不向编辑器缓冲区应用补丁。010 Script 运行时仍允许用户编辑，公开接口
没有提供原子性的“核对原字节并替换”操作。预览结果中的 `can_apply` 为 `false`。
设计依据见[编辑器修改边界](docs/architecture.md#editor-mutation-boundary)。

## 安装与启动

安装 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和
[pnpm](https://pnpm.io/installation)，然后执行：

```powershell
git clone https://github.com/AuroraAeon/010-editor-mcp.git
cd 010-editor-mcp
pnpm setup
pnpm start --allowed-root "C:\BinaryWork"
```

服务启动后等待 MCP 客户端连接，标准输出专用于协议消息。
运行 `pnpm start --help` 可查看全部配置项。

也可以在 Python 环境中执行
`uv pip install editor010_mcp-0.1.0-py3-none-any.whl` 安装 wheel 包。
包内已包含桥接脚本。仓库开发命令通过 pnpm 执行；安装后的 Python 服务入口可独立运行。

## 配置 MCP 客户端

完成 `pnpm setup` 后，在本地 MCP 客户端中填写绝对路径：

```json
{
  "mcpServers": {
    "editor010": {
      "command": "C:\\Projects\\010-editor-mcp\\.venv\\Scripts\\editor010-mcp.exe",
      "args": [
        "--allowed-root", "C:\\BinaryWork",
        "--trusted-template-root", "C:\\Projects\\010-editor-mcp\\bridge\\templates",
        "--editor-exe", "C:\\Program Files\\010 Editor\\010Editor.exe"
      ]
    }
  }
}
```

将示例路径换成实际安装路径。Linux/macOS 的入口为 `.venv/bin/editor010-mcp`，
可使用独立文件工具；编辑器桥接目前需要 Windows。仅处理磁盘文件时，可省略编辑器
和模板相关配置。

## 连接 010 Editor

1. 启动 010 Editor，处理启动提示，并只保留一个 GUI 实例。
2. 在 **Tools → Options → General** 中启用
   **Allow Only One Instance of 010 Editor**。
3. 打开目标文件，将其所在目录加入 `--allowed-root`。
   已命名文档可以保留未保存的修改；无标题缓冲区需要先指定文件名。
4. 配置 `--editor-exe`，按需指定 `--editor-pid`。依次调用 `editor_status`、
   `editor_context`，后续编辑器操作使用返回的 `buffer_id`。

桥接通过 `-script:` 将生成的脚本转发到现有会话，操作对象是当前活动文档。
若文档索引、名称或大小发生变化，工具返回 `STALE_CONTEXT`；此时重新获取上下文。

完整配置、资源限制和故障处理见[编辑器接入指南](docs/editor-setup.md)。
所用接口要求 010 Editor v13+；其他版本尚未验证。

## 使用示例

> 检查 `C:\BinaryWork\sample.bin`，从偏移 32 读取 64 字节，在前 1 MiB 中搜索
> `50 4b 03 04`，再与 `sample.old.bin` 比较。

> 从当前 010 Editor 缓冲区读取选中的字节，与磁盘上的对应内容比较，
> 然后运行受信任的 ByteHeader 模板。

> 预览把磁盘文件偏移 1 处的 `11 22` 替换成 `aa bb`，
> 再根据该预览生成新文件 `sample.patched.bin`。

磁盘补丁分两步执行：

```json
{"tool":"binary_patch_preview","arguments":{"path":"C:\\BinaryWork\\sample.bin","offset":1,"expected_hex":"1122","replacement_hex":"aabb"}}
```

```json
{"tool":"binary_patch_apply","arguments":{"path":"C:\\BinaryWork\\sample.bin","offset":1,"expected_hex":"1122","replacement_hex":"aabb","output_path":"C:\\BinaryWork\\sample.patched.bin","preview_token":"TOKEN_FROM_PREVIEW"}}
```

复制过程中，服务会再次核对源文件标识和原字节。输出文件已存在、试图原地覆盖、
源文件已变化，或预览令牌已过期、已使用时，操作会被拒绝。替换前后的字节数必须相同。

工具中的偏移均为**从文件开头起算、以零为起点的字节偏移**。
编辑器显示地址的起点单独放在 `display_start_address` 中。
模板原始 XML 使用显示地址，`variables[].offset` 则已换算为文件内偏移。
结果中的 `source` 字段以 `disk` 或 `editor` 标明数据来源。

## 访问范围与资源限制

`--allowed-root` 默认是服务启动时的工作目录，可重复指定。
模板只能从本机配置的受信任目录中执行。授权前应检查模板及其引用文件；
模板遵循 010 Editor 的权限设置，执行时可能改变模板结果或选区。

读取、扫描、复制和返回结果均有可配置的上限。
默认值见[配置表](docs/editor-setup.md#configuration)，路径规则、并发处理和错误约定
见[架构文档](docs/architecture.md)。

桥接不向远程服务发送文件内容，默认不记录原始二进制数据。
编辑器读取使用内存缓冲区；编辑器不可用时返回错误，不以磁盘字节代替。

## 开发与验证

```powershell
pnpm lint
pnpm test
pnpm exec uv build --wheel
```

常规测试覆盖磁盘操作、桥接协议和实际 MCP 子进程客户端。
真实 GUI 测试需要显式启用，运行方式见[完整 GUI 验证](docs/editor-setup.md#verify-the-real-gui)。

[文档索引](docs/README.md) · [验证结果](docs/verification.md)

采用 MIT 许可证。本项目为独立集成，与 SweetScape Software 无关联，也未获其背书。
010 Editor 本身使用独立的商业许可证。
