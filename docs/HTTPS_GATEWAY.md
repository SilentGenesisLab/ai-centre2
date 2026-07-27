# 单公网 IP 的 Caddy HTTPS 网关

## 架构

```text
Internet
  -> 121.15.184.231:80/443
  -> 小米路由器 NAT
  -> 192.168.31.23:18080/18443
  -> Caddy
  -> aicentre2.sligenai.cn
  -> 127.0.0.1:8320
```

服务器普通用户不能监听 1024 以下端口，因此 Caddy 使用：

- 内网 HTTP：`18080`
- 内网 HTTPS：`18443`

公网仍使用标准的 80/443，调用方不需要写高端口。

## 路由器映射

必须配置：

```text
TCP 公网 80  -> 192.168.31.23:18080
TCP 公网 443 -> 192.168.31.23:18443
```

公网 80/443 当前是小米路由器管理页面。配置映射前，应关闭路由器公网管理，
只允许从局域网管理路由器。

## 当前域名路由

```text
aicentre2.sligenai.cn -> 127.0.0.1:8320
```

Caddy 对公网隐藏：

```text
/v1/admin/*
```

GPU 管理接口应通过服务器本机、SSH 隧道或后续的 WireGuard/Tailscale 网络调用。

## 增加其他局域网服务

在 `gateway/Caddyfile` 增加独立站点：

```caddyfile
another.sligenai.cn {
	reverse_proxy 192.168.31.155:8080
}
```

不同域名可以共享同一个公网 IP、80 和 443。其他局域网服务器不需要配置公网
端口，只需允许 Caddy 网关访问对应的内网端口。

跨服务器后端建议使用 WireGuard/Tailscale 地址，或者为后端配置内部 CA 和
mTLS。与 Caddy 在同一台服务器的服务优先通过 `127.0.0.1` 访问。

## 安装

```bash
cd /home/donxu/ai-centre
bash scripts/install_caddy_gateway.sh
```

安装脚本会：

1. 下载固定版本的 Caddy。
2. 校验 SHA-512。
3. 验证 Caddyfile。
4. 安装并启动 user-systemd 服务。

## 验证

服务器检查：

```bash
bash scripts/verify_caddy_gateway.sh
journalctl --user -u ai-centre-caddy.service -n 100 --no-pager
```

路由器映射完成后，从外网检查：

```bash
curl -I https://aicentre2.sligenai.cn/health
curl -I https://aicentre2.sligenai.cn/docs
```

确认 HTTPS 工作后：

1. 将 AI Centre 2 的 `CONTROL_HOST` 恢复为 `127.0.0.1`。
2. 删除公网 `8320 -> 192.168.31.23:8320` 映射。
3. 确认公网无法直接访问 8320。
4. 确认 `/v1/admin/*` 通过公网返回 404。
