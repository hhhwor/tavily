# 宁夏主服务迁移运行手册

迁移日期：2026-08-07

## 当前拓扑

- 公网入口仍为旧数据机上的 Cloudflare quick tunnel：
  `https://items-scheduled-shown-antiques.trycloudflare.com`。
- 旧数据机的 `tavily-ingress-forward.service` 把本机 `127.0.0.1:8000`
  转发到宁夏主机 `127.0.0.1:8000`。
- 宁夏主机：`ec2-161-189-133-95.cn-northwest-1.compute.amazonaws.com.cn`。
- 宁夏主机的 `tavily-8000.service` 运行主服务，代码目录为
  `/home/ec2-user/tavily`，状态库为
  `/var/lib/tavily/chukonu-state.sqlite3`。
- 旧数据机的 `tavily-openalex-tunnel.service` 将旧机 OpenAlex `:9001`
  映射到宁夏机 `127.0.0.1:19001`。
- 同一个 SSH 通道将专利 ES `search.houdutech.cn:9243` 映射到宁夏机
  `127.0.0.1:9243`；宁夏机 `/etc/hosts` 将该域名解析到 loopback，TLS
  仍按原域名校验。
- 豆包 MCP 使用迁移后的固定版本离线环境 `.doubao-mcp`，避免运行时访问
  宁夏机不可达的 GitHub。

## 服务检查

旧数据机：

```bash
systemctl status tavily-ingress-forward.service
systemctl status tavily-openalex-tunnel.service
systemctl status chukonu-api-9001.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:9001/health
```

宁夏主机：

```bash
ssh -i /home/ec2-user/ningxia-hhq.pem \
  ec2-user@ec2-161-189-133-95.cn-northwest-1.compute.amazonaws.com.cn
systemctl status tavily-8000.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:19001/health
curl -fsS https://search.houdutech.cn:9243/
```

## 安全回滚

SQLite 不能双主写入。回滚时先隔离入口并停止宁夏主服务，再将宁夏状态库
一致性复制回旧机，最后启动旧主服务：

1. 旧机停止 `tavily-ingress-forward.service`，公网入口进入维护状态。
2. 宁夏机停止 `tavily-8000.service`。
3. 将宁夏机 `/var/lib/tavily/chukonu-state.sqlite3` 复制回旧机临时文件，执行
   `PRAGMA integrity_check` 后原子替换 `/tmp/chukonu-state.sqlite3`。
4. 旧机执行 `systemctl enable --now tavily-8000.service`。
5. 验证旧机本地和公网 `/health`、`/search`、`/mcp`。

## 后续收口

- 为宁夏固定出口 IP `161.189.133.95/32` 放行专利 ES 后，可移除专利 ES
  转发和宁夏机 `/etc/hosts` 覆盖，改为直接访问。
- 当前公网入口是不可恢复原 URL 的 quick tunnel。应迁为 named Cloudflare
  Tunnel 或其它固定域名 HTTPS 入口，然后更新 `MCP_ALLOWED_HOSTS`。
- 当前发布来自迁移时的未提交工作区快照；后续应整理为 commit/tag，并生成
  锁定依赖文件。
