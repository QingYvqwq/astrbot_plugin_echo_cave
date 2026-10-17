# 回声洞 · AstrBot 插件

一个面向群聊的消息存储与回溯插件。把普通消息、引用消息和合并转发存入“回声洞”，
之后可以随机抽取、按 ID 查看、查询自己的投稿，并支持媒体归档、排行榜、备份恢复与
数据库维护。

- 目标框架：AstrBot >= 4.29
- 插件语言：Python 3.10+
- 许可证：MIT（版权与许可信息见 `LICENSE`）

## 功能

- 引用消息投稿，支持普通消息、视频/文件/语音、合并转发与嵌套转发。
- 图片、视频、语音、文件下载到本地或上传 S3；S3 失败可回退本地或保留原链接。
- 加权随机抽取：`权重 = 1 / (1 + 抽取次数 × alpha)`。
- `cave` 随机发送一条回声洞内容；`查看 <ID>` 精确查看；`查询` 只列出自己的投稿 ID。
- `cave.pic` 独立抽取包含图片的普通消息，使用独立计数。
- 投稿列表、发言回溯、按用户搜索。
- 排行榜：预置时间段 + 自定义时长（如 `2d5h`）。
- 投稿者 / 原始发送者 / 管理员删除权限；管理员消息保护；群白名单。
- 合并转发关联用户选择（超时、自动单人绑定、提示撤回）。
- 特殊用户 `1094950020` 的忽略 / 拒绝 / 二次确认模式。
- 发送失败忽略 / 自动删除 / 每日私聊日报。
- public ID 安全重排、JSON 备份、备份恢复、每日自动重排。
- 媒体 V2 分群迁移、S3 迁移、媒体引用检测、媒体 URL 列回填、跨群合并。
- 拍一拍机器人触发随机抽取（OneBot）。

## 安装

### 方式一：放入插件目录

```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_echo_cave/
            ├── main.py
            ├── metadata.yaml
            ├── _conf_schema.json
            └── ...
```

在 AstrBot WebUI 的插件页重载插件。依赖会在安装时根据 `requirements.txt` 安装：

```bash
pip install -r requirements.txt
```

依赖：`httpx`（异步 HTTP）、`boto3`（S3）。

### 方式二：Git

将本插件目录作为独立仓库放置到 `data/plugins/` 下即可。仓库地址：
https://github.com/QingYvqwq/astrbot_plugin_echo_cave

## 配置

所有配置均可在 WebUI 中修改。

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `enableChannelWhitelist` | `false` | 群白名单开关 |
| `channelWhitelist` | `[]` | 允许使用回声洞的群 ID |
| `adminMessageProtection` | `false` | 管理员消息仅管理员可删 |
| `adminIds` | `[]` | 可在私聊使用管理命令的 ID |
| `allowContributorDelete` | `true` | 投稿者可删除 |
| `allowSenderDelete` | `true` | 原始发送者可删除 |
| `deleteMediaWhenDeletingMsg` | `true` | 删除消息时删除媒体 |
| `enableSizeLimit` | `false` | 启用媒体总量限制 |
| `maxImageSize` / `maxVideoSize` / `maxFileSize` / `maxRecordSize` | `20480` / `5120` / `5120` / `5120` | 各类型媒体总量上限（MB） |
| `oversizedMediaCleanupMode` | `keep-cave` | 超限时仅删媒体 / 同时删记录 |
| `useBase64ForMedia` | `false` | 本地媒体以 base64 发送 |
| `mediaStorage` | `local` | `local` / `s3` |
| `s3UploadFailureFallbackMode` | `local` | `local` / `original-link` |
| `s3Bucket` / `s3Region` / `s3Endpoint` | `""` | S3 配置 |
| `s3AccessKeyId` / `s3SecretAccessKey` | `""` | S3 凭据（Secret 在 WebUI 中遮罩） |
| `s3ForcePathStyle` / `s3PathPrefix` | `false` / `""` | S3 访问方式与前缀 |
| `s3PresignExpiresIn` | `3600` | 预签名有效期（60–604800 秒） |
| `sendAllAsForwardMsg` | `false` | 普通消息也以合并转发发送 |
| `rankingTopCount` | `10` | 排行榜人数 |
| `enableForwardUserSelection` | `true` | 转发用户选择 |
| `forwardSelectTimeout` | `20` | 选择超时（秒） |
| `autoBindSingleForwardUser` | `false` | 单用户自动绑定 |
| `forwardSpecialUserHandlingMode` | `ignore` | `ignore` / `reject` / `confirm` |
| `enablePokeTrigger` | `true` | 拍一拍触发 |
| `alpha` | `0.2` | 权重因子（0–2） |
| `sendFailureHandlingMode` | `ignore` | `ignore` / `auto-delete` / `daily-report` |
| `sendFailureSummaryAdminId` | `""` | 失败日报接收者 |
| `sendFailureSummaryTime` | `09:00` | 失败日报时间 |
| `enableAutoReindex` | `false` | 每日自动重排 |
| `autoReindexTime` | `00:00` | 自动重排时间 |
| `enableDuplicateDetection` | `true` | 同一来源消息去重 |

> 媒体大小单位为 MB，限制的是“该类型媒体总量”，不是单文件大小。

## 命令

命令前缀由 AstrBot 的 `wake_prefix` / @ / 私聊规则决定。所有群聊命令要求机器人被唤醒。

| 命令 | 说明 | 权限 |
|------|------|------|
| `cave` | 随机发送回声洞里面的内容 | 所有人（群聊） |
| `查看 <ID>` | 查看指定 public ID 的回声洞内容 | 所有人（群聊） |
| `投稿 [...用户]` | 引用一条消息并投稿到回声洞 | 所有人（群聊） |
| `查询` | 只返回自己在本频道已投稿的全部 ID | 所有人（群聊） |
| `cave.pic` | 随机抽取包含图片的普通消息 | 所有人（群聊） |
| `cave.drop <id>` | 删除一条 | 管理员 / 投稿者 / 原始发送者 |
| `cave.purge <id...>` | 批量删除 | 同上，逐条鉴权 |
| `cave.search <用户>` | 搜索与用户相关的 ID | 所有人（群聊） |
| `cave.bind <id> <用户...>` | 覆盖关联用户 | 管理员（群聊） |
| `cave.listen` | 自己投稿的 ID 列表 | 所有人（群聊） |
| `cave.trace` | 自己发言被投稿的 ID 列表 | 所有人（群聊） |
| `cave.rank [时间段]` | 排行榜；`lday/lweek/lmonth/day/week/month/all` 或 `2d5h` | 所有人（群聊） |
| `cave.admin.merge <源群> <目标群> [keep]` | 跨群合并 | 私聊 + `adminIds` |
| `cave.admin.migrate-local-v2` | 旧媒体目录迁移到 V2 | 私聊 + `adminIds` |
| `cave.admin.migrate-s3 [keepLocal]` | 本地媒体迁移到 S3 | 私聊 + `adminIds` |
| `cave.admin.inspect-media [范围]` | 只读检测媒体引用 | 私聊 + `adminIds` |
| `cave.admin.backfill-media-urls [范围]` | 回填媒体 URL 列 | 私聊 + `adminIds` |
| `cave.admin.reindex` | 重排 public ID | 私聊 + `adminIds` |
| `cave.admin.restore-reindex <路径>` | 从 JSON 备份恢复 | 私聊 + `adminIds` |

范围参数示例：`0-100,105-240` 或 `12,18-20`。

### 群聊白名单

开启白名单后，只有 `channelWhitelist` 中的群可以使用回声洞命令；其他消息和其他插件不受影响。

### 管理员判定

- 私聊管理命令：严格使用插件 `adminIds`。
- 删除权限：AstrBot 管理员（`admins_id` / `event.is_admin()`）或插件 `adminIds`。
- 管理员消息保护：历史投稿者是否管理员，使用 AstrBot `admins_id` 与插件 `adminIds` 判断。

## 消息模板

普通消息随机使用 5 套 prefix/suffix；转发消息随机使用 5 套模板。占位符：

| 占位符 | 含义 |
|--------|------|
| `{id}` | public ID |
| `{date}` | 创建日期（`YYYY/MM/DD`，本地时区） |
| `{originName}` | 原始发送者 |
| `{userName}` | 投稿者 |
| `{relatedUsers}` | 关联用户名（内置文案未使用，但渲染器支持） |
| `{nl}` | 换行 |
| `{textStartNl}` | 正文以文字开头时插入换行，否则为空 |
| `{textEndNl}` | 正文以文字结尾时插入换行，否则为空 |

示例（普通消息第 1 套）：

```text
🌀 回声洞 #{id}

—— 有人留下了声音 ——{textStartNl}
（原消息内容）
{textEndNl}📅 {date} · 来自 @{originName}
📮 由 @{userName} 投递
```

## 数据存储

```text
data/plugin_data/astrbot_plugin_echo_cave/
├── echo_cave.sqlite3        # 独立 SQLite，schema_version = 1
├── backups/                 # 重排/恢复 JSON 备份
└── media/
    ├── v2/<频道>/images|videos|files|records/
    └── legacy/images|videos|files|records/   # migrate-local-v2 的源目录
```

- `entry_id` 为内部自增主键；`public_id` 为对外 ID。重排只改 `public_id`。
- 内容列保存 CQ 风格 JSON 消息段。
- 媒体引用支持 `file:///绝对路径`、绝对路径、`http(s)://`、`data:`、`base64://`、`s3://bucket/key`。
- 删除媒体时只删除插件数据目录内的本地文件或 S3 对象，不会删除任意外部绝对路径。
- 数据库写入串行化；重排/恢复在单个事务内完成并校验。

### 备份与恢复

重排前必定先写备份：

```text
backups/echo-cave-reindex-<UTC 时间>.json
```

格式：

```json
{
  "createdAt": "...",
  "records": [{"entryId": 1, "id": 1, "...": "..."}],
  "mapping": [{"oldId": 1, "newId": 1, "tempId": 1000001}]
}
```

恢复：

```text
cave.admin.restore-reindex C:/path/to/backup.json
```

恢复会严格校验记录字段、ID 唯一性、内容 JSON；失败自动回滚。
相对路径按 AstrBot 进程工作目录解析；也可以使用绝对路径。

## 已知限制

1. **平台**：完整功能面向 OneBot v11 / aiocqhttp。合并转发、精确撤回、拍一拍在
   其他平台不可用；普通文本/图片/语音/视频/文件命令仍可工作。
2. **撤回**：AstrBot 没有统一撤回 API。提示消息仅在 aiocqhttp 下通过 `delete_msg`
   撤回；其他平台不撤回。
3. **转发读取**：依赖协议端提供 `get_forward_msg` 与 `Reply.chain`；失败时投稿提示
   引用不可读。
4. **S3**：使用 `boto3`；需要可用的 bucket/region/凭据。未对真实 S3 服务做集成测试。
5. **模板**：模板内置；如需自定义，可修改 `templates.py`。
6. **时区**：定时任务使用 AstrBot 进程本地时区。
7. **会话等待**：用户选择与二次确认使用 AstrBot 的 `session_waiter`；若内置
   `astrbot` 插件被禁用，后续消息将无法触发等待器。
8. **合并转发节点**：AstrBot 的 `Node.to_dict()` 会把节点内图片/语音转为 base64，
   因此大媒体转发可能占用较多内存。普通（非转发）消息不受影响。
9. **备份恢复**：备份不包含去重指纹；恢复后旧记录仍可正常抽取，但不会参与去重。
10. 测试使用 AstrBot API mock，不等同于真实 AstrBot/QQ 环境验收。

## 测试

```bash
python -m pytest tests -q
python -m ruff check astrbot_plugin_echo_cave scripts
python -m ruff format --check astrbot_plugin_echo_cave scripts tests
```

测试覆盖数据库、权限、抽取、模板、媒体、S3 回退、数据导入、重排/恢复、调度、
AstrBot 入口 mock。真实平台能力见项目根目录 `docs/acceptance-checklist.md`。

## 许可

MIT。完整许可文本见 `LICENSE`。
