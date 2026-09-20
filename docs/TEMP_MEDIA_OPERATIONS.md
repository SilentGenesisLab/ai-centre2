# AI Centre 临时媒体运行手册

AI Centre 的临时音频、视频、图片和媒体工作目录统一位于：

`/home/donxu/temp-data/YYYYMMDD/<uuid4hex>-<清洗后的原文件名>`

- 日期按 `Asia/Shanghai` 自动生成。
- UUID 固定为 32 位小写 `uuid4().hex`，每个临时媒体独立生成。
- 内容签名优先决定上传文件的真实扩展名，伪造扩展名不会沿用。
- 日期目录权限为 `0770`，媒体及 lease 文件权限为 `0660`，服务以 `donxu` 运行。
- 正式成片、注册音色、模型、数据库、日志和长期 QA 产物仍保留原目录。
- 禁止创建新的 `/home/yanghao/...` 中转文件。

## 人工分配和 SCP 上传

禁止手工拼接路径。先在远程分配一个独占文件名：

```bash
cd /home/donxu/ai-centre
target=$(.venv-control/bin/python -m temp_media allocate --name '泰国-8.flac')
printf '%s\n' "$target"
```

再从本机上传到命令返回的完整路径：

```powershell
$target = ssh -p 2222 donxu@121.15.184.231 `
  "cd /home/donxu/ai-centre && .venv-control/bin/python -m temp_media allocate --name '泰国-8.flac'"
scp -P 2222 '.\泰国-8.flac' "donxu@121.15.184.231:$target"
```

人工流程结束后必须显式关闭 lease：

```bash
python -m temp_media success "$target"              # 成功，立即删除
python -m temp_media fail "$target" --error '原因'  # 失败，保留 24 小时
```

## Lease 与清理规则

- 分配时创建 `.lease.json` 活跃标记。
- 成功响应或正式产物落盘后，媒体和 lease 立即删除。
- 失败时活跃 lease 转为 `.failed.json`，从 `failed_at` 起保留 24 小时。
- 活跃 lease 超过 24 小时按崩溃孤儿处理。
- `ai-centre-temp-cleanup.timer` 每小时运行，只扫描 `/home/donxu/temp-data`，并删除空日期目录。

人工审计命令：

```bash
systemctl --user status ai-centre-temp-cleanup.timer
systemctl --user list-timers ai-centre-temp-cleanup.timer
python -m temp_media cleanup --retention-seconds 86400
find /home/donxu/temp-data -maxdepth 2 -type f -printf '%M %u %p\n'
```

当前 `/home/donxu/video-asr-audit-20260818` 不属于自动清理范围，不迁移也不自动删除。
