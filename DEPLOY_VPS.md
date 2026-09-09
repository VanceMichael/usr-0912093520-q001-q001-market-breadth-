# VPS 双并发常驻部署

当前基线面向 2 vCPU / 8 GB RAM / 80 GB 磁盘，最多同时启动两个 Claude worker。每题使用一个短生命周期 Docker 容器，默认限制为 1 CPU、2 GB 内存；题目目录、Claude 状态和日志分别挂载到 `runs/<批次>/<任务ID>/`。

## 首次准备

```bash
gh repo clone MonsterRHD/UserSatisfactionRating UserSatisfactionRating-codex
cd UserSatisfactionRating-codex
git switch codex/vps-docker-pipeline
cp .env.example .env
# 填写 CC_SWITCH_BASE_URL / CC_SWITCH_MODEL / CC_SWITCH_API_KEY / CC_USR_SUBMITTER
docker build -f Dockerfile.worker -t ccusr-claude-worker:local .
```

Codex CLI 需要安装在 `/home/ubuntu/.local/bin/codex` 或可由服务的 `PATH` 找到，并完成非交互认证。Docker worker 镜像内需要 Claude Code CLI。验证版本：

```bash
/home/ubuntu/.local/bin/codex --version
docker run --rm ccusr-claude-worker:local claude --version
```

## 单次验证

以下命令执行一个完整周期：抓取新闻主题、接管已有 READY 批次、Docker 双并发跑题、交付生产、交付质检和 Excel 导出。没有可接管批次时会自主创建一个两题批次。

```bash
python3 tools/pipeline_daemon.py --concurrency 2
```

新闻主题默认来自项目内 `tools/news_topics.py` 配置的中国新闻网频道。自动出题每批默认 10 道，可在控制台“运行配置”中调整为 1-20 道；中等、困难、地狱难度可多选并按合计 100% 的比例分配。保存后的题数和难度比例会从下一轮自动出题开始生效。每个成功导出的批次会标记为 `completed`，后续循环不会重复交付。

## systemd 常驻运行

仓库内服务文件按当前 VPS 路径 `/home/ubuntu/UserSatisfactionRating-codex` 和用户 `ubuntu` 配置。路径不同时先修改服务文件。

```bash
sudo install -m 0644 deploy/systemd/ccusr-pipeline.service /etc/systemd/system/ccusr-pipeline.service
sudo systemctl daemon-reload
sudo systemctl enable --now ccusr-pipeline.service
systemctl status ccusr-pipeline.service --no-pager
journalctl -u ccusr-pipeline.service -f
```

流水线自己的详细日志在 `runs/daemon/`。更新代码时先停止服务，确保当前 agent 或 worker 不会与更新并发操作：

```bash
sudo systemctl stop ccusr-pipeline.service
git pull --ff-only origin codex/vps-docker-pipeline
sudo systemctl start ccusr-pipeline.service
```

不要把 `/var/run/docker.sock` 暴露给 Claude worker；它只由宿主机调度器使用。长期生产建议使用 rootless Docker 或独立 worker 主机，并监控磁盘、模型额度、`runs/daemon/` 错误和 systemd 重启次数。
