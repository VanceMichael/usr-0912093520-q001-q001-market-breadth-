# VPS 双并发部署试跑

当前基线面向 2 vCPU / 8 GB RAM / 80 GB 磁盘，默认只启动两个 Claude worker。每题一个短生命周期容器，题目目录、Claude 状态、日志分别挂载到 `runs/<批次>/<任务ID>/`。

```bash
git clone <repository-url> UserSatisfactionRating
cd UserSatisfactionRating
cp .env.example .env
# 填写 CC_SWITCH_BASE_URL / CC_SWITCH_MODEL / CC_SWITCH_API_KEY / CC_USR_SUBMITTER
# 同时设置 HOST_PROJECT_ROOT 为当前项目绝对路径，例如 /opt/UserSatisfactionRating
python3 tools/batch_pipeline.py --db production.sqlite3 relocate --workspace "$PWD"
docker compose build worker-image scheduler
docker compose run --rm scheduler python3 -m tools.orchestrator \
  --batch 090901 --concurrency 2 --image ccusr-claude-worker:local --loop
```

这一步会持续消费已经完成题目质检、状态为 `READY` 的题目。当前版本不会把交付生产和交付质检伪装成纯脚本：这两个阶段需要可用的 Codex agent 凭据，配置完成后再接入同一状态机。

不要把 `/var/run/docker.sock` 暴露给 Claude worker；它只应被调度器使用。生产环境应改为 rootless Docker 或独立 worker 主机，并通过 SSH/Tailscale 访问控制台。

部署前需要把被 `.gitignore` 排除的 `production.sqlite3` 和批次目录一并同步到 VPS。`relocate` 只重写数据库绝对路径，不复制文件，也不修改题目仓库内容。
仅同步部分批次时增加 `--batch <批次名>`，避免要求其他历史批次目录也存在。
