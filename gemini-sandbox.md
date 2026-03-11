# Gemini CLI 沙箱模式

Gemini CLI 的 `--sandbox` 参数通过 Docker 容器隔离执行环境，防止 Gemini
对项目外文件的破坏性操作。

## 前置条件

### Docker Desktop

**Windows**:

下载安装 [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)，
确保启用 WSL2 后端。安装后启动 Docker Desktop。

**Linux**:

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl start docker
```

**macOS**:

```bash
brew install --cask docker
```

### 拉取沙箱镜像

Gemini CLI 首次使用 `--sandbox` 时会自动拉取镜像。也可手动拉取：

```bash
# 镜像 tag 跟随 Gemini CLI 版本
docker pull us-docker.pkg.dev/gemini-code-dev/gemini-cli/sandbox:0.34.0-preview.0
```

或让 CLI 自动拉取：

```bash
gemini --sandbox --prompt "echo ok" -y
```

如果拉取失败，检查网络连接和 Docker 是否运行中。

## 使用方式

### MCP 调用

```python
mcp__gemini__gemini(
  PROMPT: "实现 X 功能",
  cd: "<workspace>",
  model: "gemini-3.1-pro-preview",
  sandbox: true          # 启用 Docker 沙箱
)
```

### 直接 CLI 调用

```bash
gemini "<task>" -y --sandbox --model gemini-3.1-pro-preview
```

## 隔离范围

| 范围 | 容器内访问 |
|------|-----------|
| 项目目录 | 可读写（bind mount） |
| 项目外文件 | 不可见 |
| 系统文件 | 不可见 |
| 网络 | 可访问 |

- 项目目录内的写操作**会同步到宿主**（容器挂载了项目目录）
- 项目外的文件对容器**完全不可见**（`rm -rf /` 不影响宿主）
- **macOS**: 使用 Seatbelt（`sandbox-exec`）原生沙箱，无需 Docker
- **Windows/Linux**: 需要 Docker

## 何时使用

| 场景 | sandbox |
|------|---------|
| 分析、review、Q&A | 不需要（`sandbox: false`） |
| 写代码、改文件 | **必须**（`sandbox: true`） |

> 建议写操作启用沙箱，防止意外文件修改。
