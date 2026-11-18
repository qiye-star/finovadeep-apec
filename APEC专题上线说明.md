# APEC 专题上线说明

在官网 finovadeep.com 上线 APEC 专题页（`/apec/`）和它背后的对话服务。服务器同上一次：阿里云，IP 47.82.73.194。

整体结构：页面是静态文件，Nginx 直接提供。页面上的对话请求发到同域名的 `/apec-api/`，由 Nginx 转发给服务器本机 8840 端口上的对话服务，对话服务再去调大模型。**不需要新域名，也不需要新证书。**

## 要发给你的文件

| 文件 | 内容 |
| --- | --- |
| `finova-website-20261118-1216.zip` | 官网全部文件（67 个），比 11-17 版多了 `/apec/` 页面，其他页面内容不变 |
| `apec-chat-20261118.zip` | 对话服务程序（不含模型 key） |
| `nginx.conf` | 更新后的 Nginx 配置示例，新增 `location /apec-api/` 一段 |
| 模型 key | **单独私下发送，不要放进压缩包、聊天群或邮件附件**。需要两项：DeepSeek key（OPENAI_API_KEY）和千问 key（DASHSCOPE_API_KEY） |

服务器需要能访问外网的 `api.deepseek.com` 和 `dashscope.aliyuncs.com`，并装有 Python 3.10 及以上版本。

---

## 第一步：备份当前网站

```bash
tar czf ~/finova-website-backup-$(date +%Y%m%d-%H%M).tar.gz -C /srv/finova-website .
```

如果 11-17 那一版还没上线、服务器上还是旧网站，按 `上线说明.md` 的第一步备份旧网站目录。

## 第二步：部署对话服务

```bash
sudo mkdir -p /srv/apec-chat
sudo unzip -o apec-chat-20261118.zip -d /srv/apec-chat
cd /srv/apec-chat
sudo python3 -m venv .venv
sudo .venv/bin/pip install "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" "python-dotenv>=1.0"
```

国内服务器 pip 慢的话，在 `pip install` 后面加 `-i https://mirrors.aliyun.com/pypi/simple/`。

创建 `.env`，填入单独收到的两个 key：

```bash
sudo cp .env.example .env
sudo nano .env
```

`.env` 里要改的几项：

| 项 | 填什么 |
| --- | --- |
| `OPENAI_API_KEY` | DeepSeek key |
| `DASHSCOPE_API_KEY` | 千问 key |
| `APEC_ALLOW_ORIGINS` | `https://finovadeep.com` |

其余保持默认。然后收紧权限，只有服务账号能读：

```bash
sudo chown -R www-data:www-data /srv/apec-chat
sudo chmod 600 /srv/apec-chat/.env
```

如果服务器上 Nginx 的运行账号不是 `www-data`（CentOS 一般是 `nginx`），把上面两行和下面服务文件里的 `www-data` 都换成那个账号。

装成系统服务，开机自启、崩溃自动重启：

```bash
sudo cp /srv/apec-chat/deploy/apec-chat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now apec-chat
sudo systemctl status apec-chat --no-pager      # 应显示 active (running)
curl -s http://127.0.0.1:8840/health            # 应返回 {"ok":true,"providers":["primary","fallback"]}
```

`providers` 里如果少了一项，说明对应的 key 没填上，回去检查 `.env`。

## 第三步：放上新网站

```bash
sudo unzip -o finova-website-20261118-1216.zip -d /srv/finova-website
ls /srv/finova-website/apec      # 应看到 index.html
```

## 第四步：改 Nginx

在 finovadeep.com 的 `server { listen 443 ... }` 块里，加上 `nginx.conf` 中 `location /apec-api/ { ... }` 这一段，和 `location /` 并列。其他配置不用动。

```bash
sudo nginx -t          # 必须显示 syntax is ok / test is successful
sudo nginx -s reload
```

## 第五步：上线后检查

用手机或电脑的无痕窗口打开 `https://finovadeep.com/apec/`：

- [ ] 页面能打开，标题是“APEC 深圳会议，对你的持仓有什么影响？”
- [ ] 点速览下的蓝色链接，会跳到对应议题并展开
- [ ] 在“我的持仓”里搜“中际旭创”，能搜到并添加
- [ ] 点“今天 APEC 谈了什么？”，回答**逐字出现**，几秒内出完，**不出现**红字“对话服务暂时不可用”
- [ ] 其他页面（首页、`/careers/`、`/features/`）照常能打开

如果回答是一次性整段出现，而不是逐字出现，说明 Nginx 的 `proxy_buffering off` 没生效。如果出现红字提示，看对话服务日志：

```bash
sudo journalctl -u apec-chat -n 50 --no-pager
```

## 会期内的日常更新

| 要改什么 | 谁改 | 怎么上线 |
| --- | --- | --- |
| 今日速览（每天 2 次） | 我们改代码并重新打包，发给你新的 `finova-website-*.zip` | 只做第三步（解压覆盖），不用重启任何服务 |
| 对话提示词 | 我们发新的 `prompt.md` | 覆盖 `/srv/apec-chat/prompt.md`，然后 `sudo systemctl restart apec-chat` |

## 出问题怎么回退

- **只是对话有问题**：`sudo systemctl stop apec-chat`。页面会自动改用映射表生成的标准回答，页面本身照常可用。
- **整个网站有问题**：

```bash
sudo rm -rf /srv/finova-website/*
sudo tar xzf ~/finova-website-backup-<日期>.tar.gz -C /srv/finova-website
```

Nginx 里新增的 `/apec-api/` 那一段可以保留，不影响其他页面。

## 已知情况（不是故障）

- 用户添加的持仓只存在用户自己的浏览器里，换手机或清缓存后需要重新添加。
- 同一 IP 每分钟最多提问 10 次，超过会提示稍后再试。
- 对话评测结果见 `output/APEC专题_对话评测报告.md`。目前还有一类小错：一次问到多家公司时，模型偶尔把议题结论套错公司。
