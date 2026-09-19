# 通用视频生成接口修复与生产回归报告（2026-09-09）

## 结论

`POST /v1/video-generations/jobs` 的 jmapi、libtv 两个渠道均已恢复。纯文本、参考视频、720P以及480P自动路由均已完成生产闭环，成功结果会转存到 AI Centre OSS。

## 逐项问题与修复

1. 纯文本被上游拒绝：无媒体请求在服务端自动注入中性空白参考图；显式提供任一媒体时不注入。
2. jmapi 成功结果丢失：增加 `stdout.result_json.videos[].video_url` 解析。
3. libtv 一直查不到任务：查询路径由 `/api/v1/video/query/{task_id}` 修正为 `/libtv/api/v1/video/query/{task_id}`。
4. libtv 嵌套结果丢失：增加 `task.status` 与 `task.result.urls` 解析。
5. HTTP 200 内的业务失败被误判：检查 jmapi `exit_code`、`gen_status`、`fail_reason` 和 libtv `ok`、嵌套失败状态。
6. 上游完成但无结果仍标成功：连续三次查询不到结果地址后明确失败。
7. 上游临时 CDN 地址不稳定：下载成片并转存现有 AI Centre OSS，再发布 `result_urls`。
8. 取消状态不准确：排队任务立即取消；运行任务使用 `cancel_requested` 协作取消，Worker停止轮询和结果发布。
9. 480P契约不一致：jmapi 的 `seedance2.0_vip` 明确不接受480P；指定jmapi时提交前返回422，`channel=auto`时自动选择libtv。
10. libtv图片能力声明过低：按已验证契约开放最多9张图片，音频仍保持不支持。
11. jmapi忽略 `sound=false` 并返回AAC音轨：OSS转存前使用FFmpeg仅复制视频码流并移除音轨，不重新编码画面；`sound=true`继续保留上游音轨。
12. 自动渠道故障转移不完整：上游明确进入失败终态时允许 `channel=auto` 切到下一渠道；超时和OSS转存错误仍不重复生成，避免双份成片与费用。

## 生产回归

| 场景 | 实际渠道 | 状态 | 有效耗时 | 媒体检查 |
|---|---|---:|---:|---|
| 纯文本，720P | jmapi | 成功 | 182.107秒 | H.264，1280×720，5.088秒 |
| 纯文本，720P | libtv | 成功 | 209.963秒 | H.264，1280×720，5.042秒 |
| 参考视频，720P | jmapi | 成功 | 325.995秒 | H.264，1280×720，5.088秒 |
| 合规参考视频，720P | libtv | 成功 | 188.182秒 | H.264，1280×720，5.042秒 |
| 纯文本，480P，`auto` | libtv | 成功 | 182.363秒 | H.264，864×496，5.042秒 |
| 纯文本，720P，`auto`，强制无音轨 | jmapi | 成功 | 240.502秒 | H.264，1280×720，5.017秒，音轨数0 |

所有成功结果均为 `https://oss-imgai.sligenai.cn/ai-video-kernel/...` 稳定地址，并通过 FFprobe 完整解码。公网 `/api-docs.md` 返回 HTTP 200，内容类型为 `text/markdown; charset=utf-8`。

额外验证：使用未录入 libtv 合规素材库的视频时，上游在11.687秒后返回“素材未通过Seedance2.0合规检测”。AI Centre现在会原样呈现该业务错误，不再误报为查询接口故障。

## 自动测试与部署

- 服务器 Python 3.11 环境：22项生成模块与OpenAPI测试全部通过。
- 使用一条实际含AAC音轨的jmapi成片验证无重编码去音轨：输入2,838,704字节，输出2,752,541字节，输出音轨数为0。
- 两个服务均为 `active`：`ai-centre-control.service`、`ai-centre-video-generation-worker.service`。
- 部署后的服务错误日志为空。
- 回滚备份：`runtime/deploy-backups/video-generation-fix-20260909-01/`。

## 已知上游限制

- jmapi/libtv当前没有可靠的上游取消接口。运行中取消可阻止AI Centre继续发布结果，但上游已经开始的生成可能继续消耗资源。
- libtv会执行自己的素材合规资产门禁；未登记素材被拒绝不表示接口或Worker离线。
- `sound=false`会由AI Centre在结果转存阶段强制移除音轨；`sound=true`只表示保留上游音轨，不保证上游一定生成声音。
