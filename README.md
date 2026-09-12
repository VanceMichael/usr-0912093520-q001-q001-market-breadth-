# 市场宽度事件归档

该服务为收盘监控提供事件接收入口，后续模块会在此基础上加入版本排序、窗口归因和可重放查询。当前工程包含 Fiber 健康检查、容器构建文件以及 PostgreSQL、Redis 的本地编排配置。

运行 `docker compose up --build` 后可访问 `GET /health`。Go 测试使用 `go test ./...`。
