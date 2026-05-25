# PPT-Web

基于 [ppt-master](https://github.com/hugohe3/ppt-master) 的网页版 AI PPT 生成工具。

用户只需配置一个 OpenAI 兼容的 LLM API Key，即可一键生成可编辑的 PPTX 文件。

## 功能特性

### 🎯 AI 智能生成
- **内容自动分析** — 输入 Markdown/纯文本或上传文件，AI 自动提取要点、规划 PPT 结构
- **七步完整流程** — 内容分析 → 策略规划 → 设计规格 → 页面规划 → 八项确认 → SVG 生成 → PPTX 导出
- **页数智能推荐** — AI 根据内容量自动推荐合适页数，支持手动调整并让 AI 重新规划内容分配
- **思考模式** — 可按请求启用/禁用 LLM 思考模式（支持 DeepSeek 等模型的 enable_thinking 参数）

### 📋 八项交互确认
生成前展示 8 项设计参数供用户确认和调整：
1. **主题/标题** — PPT 主题，可修改
2. **目标受众** — 面向什么人群
3. **核心信息** — 要传达的关键要点
4. **设计风格** — 科技/商务/简约等风格
5. **配色方案** — 主色/辅色/背景色，可自定义
6. **字体选择** — 标题/正文字体
7. **页面规划** — 每页标题和内容要点，支持调页数后 AI 重新分配
8. **页数** — 推荐页数，可增减（增减后自动触发 AI 重新规划）

### 🎨 SVG 在线预览编辑
- **逐页 SVG 预览** — 生成完成后可在线查看每一页的 SVG 渲染效果
- **元素标注** — 点击 SVG 任意元素添加修改标注
- **批量保存** — 保存所有标注并同步到 SVG 文件
- **重新生成** — 修改标注后可重新生成单页 SVG
- **PPTX 重建** — 编辑完成后一键重建 PPTX 文件

### 📥 内容输入
- **文本粘贴** — 直接粘贴 Markdown 或纯文本内容
- **文件上传** — 支持拖拽上传文件（.txt / .md 等）
- **二选一切换** — Tab 切换输入方式，界面简洁不干扰

### 📦 部署与运维
- **Docker 离线部署** — 一键打包为 tar.gz，拷贝到内网服务器即可安装运行
- **自动健康检查** — 启动后自动验证服务可用性
- **数据持久化** — 项目文件和配置通过 Docker Volume 持久化，容器重建不丢失
- **服务管理脚本** — start/stop/restart/status/logs 一套命令管全部

### ⚙️ 兼容性
- **任意 OpenAI 兼容 API** — DeepSeek、GLM、Kimi、Qwen、MiniMax 等国产模型均支持
- **弱模型适配** — 针对能力较弱的模型做了多重防护（SVG 元素限制、格式修正、fallback 兜底）
- **SVG 导出双模式** — 优先使用 native 模式（矢量可编辑），失败自动 fallback 到 legacy 模式（图片嵌入）
- **PPT 格式** — 支持 16:9 宽屏和 4:3 标准比例

## 快速开始（开发模式）

```bash
cd ~/ppt-web

# 安装依赖
pip install -r backend/requirements.txt
pip install -r skills/ppt-master/requirements.txt

# 启动
python start.py
# 浏览器打开 http://localhost:8000
```

## Docker 部署

### 前置条件

- 有网络的机器：安装 Docker，用于构建镜像和打包
- 目标内网服务器：安装 Docker（无需互联网）

### 第一步：打包（联网机器上操作）

```bash
cd ppt-web
chmod +x package.sh
./package.sh
```

脚本自动完成：
1. 构建 Docker 镜像
2. 导出镜像 + 部署脚本为 `dist/ppt-web-deploy.tar.gz`
3. 本地自动部署测试，验证服务是否正常

### 第二步：传输到内网

将 `dist/ppt-web-deploy.tar.gz` 拷贝到目标服务器（U盘、内网传输等）。

### 第三步：安装（内网服务器上操作）

```bash
# 解压
tar -xzf ppt-web-deploy.tar.gz

# 安装（自动加载镜像、创建目录、生成配置）
chmod +x install.sh
./install.sh
```

安装完成后，修改 LLM API 配置：

```bash
cd ~/ppt-web-deploy
vi .env
```

`.env` 配置说明：

```ini
# ── LLM API 配置（必填）──
# OpenAI 兼容的大模型 API 地址和密钥
LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://your-llm-api.com/v1
LLM_MODEL=gpt-4o

# ── 服务端口（默认 8000，一般不改）──
HOST_PORT=8000
```

### 第四步：启动

```bash
cd ~/ppt-web-deploy
./manage.sh start
```

浏览器打开 `http://<服务器IP>:8000` 即可使用。

### 日常管理

```bash
cd ~/ppt-web-deploy

./manage.sh start      # 启动服务
./manage.sh stop       # 停止服务
./manage.sh restart    # 重启服务
./manage.sh status     # 查看运行状态
./manage.sh logs       # 实时查看日志
./manage.sh clean      # 清理容器和数据
```

### 更新版本

1. 在联网机器上重新运行 `./package.sh` 生成新的部署包
2. 将 `ppt-web-deploy.tar.gz` 传输到内网服务器
3. 解压后加载新镜像并重启：

```bash
tar -xzf ppt-web-deploy.tar.gz
docker load -i ppt-web.tar
cd ~/ppt-web-deploy
./manage.sh stop
docker rm ppt-web
./manage.sh start
```

### 数据目录

| 路径 | 说明 |
|------|------|
| `~/ppt-web-deploy/data/projects/` | 生成的 PPT 项目文件（SVG、PPTX） |
| `~/ppt-web-deploy/data/config/` | 运行时配置文件 |
| `~/ppt-web-deploy/.env` | LLM API 和端口配置 |

数据通过 Docker Volume 持久化，容器重建后不丢失。

## 使用说明

1. **配置 API** — 首次使用在 `.env` 中配置好 LLM API（也可通过页面配置）
2. **输入内容** — 粘贴 Markdown/纯文本，或上传文件
3. **AI 预览** — 系统生成设计方案和页面规划
4. **八项确认** — 确认配色、字体、页数等设计参数（可修改后重新规划）
5. **生成 PPT** — AI 逐页生成 SVG → 转换为 PPTX
6. **预览下载** — 在线预览 SVG，或直接下载 PPTX

## 架构

```
前端(单HTML) → FastAPI → LLM API(OpenAI兼容) + ppt-master脚本
```

```
ppt-web/
├── backend/app/
│   ├── main.py          # FastAPI 路由、WebSocket、SVG编辑器API
│   ├── ppt_engine.py    # 核心引擎，编排 ppt-master 七步流水线
│   ├── llm_client.py    # OpenAI 兼容 LLM 客户端
│   ├── config.py        # 配置管理（env优先，config.json兜底）
│   └── svg_annotations.py # SVG标注模块
├── frontend/dist/
│   ├── index.html            # 主界面（生成+确认+下载）
│   ├── svg-editor.html       # SVG预览编辑器
│   ├── svg-editor-app.js     # 编辑器逻辑（复用ppt-master）
│   └── svg-editor-style.css  # 编辑器样式
├── skills/ppt-master/   # 原项目脚本（直接复用，不改动）
├── Dockerfile           # Docker镜像构建
├── package.sh           # 打包脚本（构建+导出+部署测试）
├── install.sh           # 离线安装脚本
├── manage.sh            # 服务管理脚本
├── .env.example         # 环境配置模板
└── docker-compose.yml   # Docker Compose 配置
```

## API 文档

启动后访问 `http://localhost:8000/docs` 查看 Swagger 文档。

| 接口 | 说明 |
|------|------|
| `GET /api/health` | 健康检查 |
| `GET /api/config` | 获取当前配置（脱敏） |
| `POST /api/config` | 保存配置 |
| `POST /api/generate/preview` | 预览阶段（Step 1-4） |
| `POST /api/generate/confirm` | 确认并生成（Step 5-7） |
| `POST /api/generate/replan` | 调整页数后 AI 重新规划 |
| `GET /api/tasks/{id}` | 查询任务进度 |
| `GET /api/download/{id}` | 下载 PPTX |
| `GET /api/preview/{id}` | SVG 预览编辑器 |
| `WS /ws/tasks/{id}` | WebSocket 实时进度推送 |
