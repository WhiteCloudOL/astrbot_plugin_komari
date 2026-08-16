# AstrBot Komari 监控插件

署名：清蒸云鸭

插件提供 Komari 节点状态图片查询、指令帮助，以及节点离线/恢复在线提醒。提醒使用 AstrBot 的 unified message origin（UMO）会话标识；状态边沿会保存到 AstrBot 插件数据目录，因此插件重载后也不会重复提醒同一个状态。

## 配置

- `base_url`：Komari 站点地址。
- `api_token`：可选 Token，插件通过 `Authorization: Bearer` 请求头发送。
- `notification_targets`：UMO 会话列表，每行一个。
- `poll_interval`：后台轮询间隔，最小 30 秒。

## 指令

- `/komari` 或 `/komari状态`：返回粉色二次元风格状态图片。
- `/komari刷新`：跳过缓存并重新请求。
- `/komari指令` 或 `/komari帮助`：查看帮助。

## 许可证

本项目采用 GNU Affero General Public License version 3（AGPL-3.0）。
